"""jsonio.parse_json: strings that are not Unicode text are refused at the parser, and so is a body
holding more values than a parse may build."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from deltasleuth.core import jsonio
from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.jsonio import JSONParseError, LoneSurrogateError, ResourceLimitError, parse_json
from deltasleuth.core.types import Side, SideStateKind, TargetCapabilities


@pytest.mark.parametrize(
    "body",
    [
        b'{"name": "Ren\\ud83d"}',  # a string cut inside a surrogate pair
        b'{"name": "\\udc00x"}',  # a lone low surrogate
        b'{"name\\ud800": 1}',  # in a member name
        b'[["deep", {"x": ["\\udfff"]}]]',
        b'"\\uDBFF"',  # upper-case hex
        '"\ud800"',  # text input holding the code point itself (it has no UTF-8 form)
    ],
)
def test_lone_surrogate_is_refused(body: bytes | str) -> None:
    # Every consumer of parsed bodies (observe, measure's CEL shapes, the profiler, the file
    # sources) is protected: the CEL runtime crashed the interpreter on such a string.
    with pytest.raises(LoneSurrogateError) as info:
        parse_json(body)
    assert isinstance(info.value, JSONParseError)  # callers that handle parse errors handle it
    assert "Ren" not in str(info.value)


@pytest.mark.parametrize(
    ("body", "value"),
    [
        (b'{"name": "\\ud83d\\ude00"}', {"name": "\U0001f600"}),  # a complete pair is text
        (b'{"esc": "\\\\ud83d"}', {"esc": "\\ud83d"}),  # an escaped backslash, not an escape
        ('{"t": "\U0001f600"}', {"t": "\U0001f600"}),
    ],
)
def test_surrogate_pairs_and_escaped_backslashes_are_text(body: bytes | str, value: object) -> None:
    assert parse_json(body) == (value, False)


def test_a_parse_builds_at_most_two_million_values() -> None:
    assert jsonio.MAX_VALUES == 2_000_000


# A body of more than ``MAX_VALUES`` values, from one repeated item: the array and one item more.
OVER = {
    "numbers": (b"[", b"1", b"]"),
    "empty arrays": (b"[", b"[]", b"]"),
    "strings": (b'{"items": [', b'"ORG-000123"', b"]}"),
}


@pytest.mark.parametrize("shape", OVER)
def test_a_body_over_the_value_count_is_refused_before_any_value_is_built(
    shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    head, item, tail = OVER[shape]
    body = head + (item + b",") * jsonio.MAX_VALUES + item + tail

    def built(text: str, **_: object) -> None:
        raise AssertionError("the parser ran")

    monkeypatch.setattr(jsonio, "json", SimpleNamespace(loads=built))
    with pytest.raises(ResourceLimitError) as exc:
        parse_json(body)
    assert str(exc.value) == f"body holds more than {jsonio.MAX_VALUES} values"


@pytest.mark.parametrize(
    ("body", "values"),
    [
        (b"[1,2,3,4]", 5),
        (b'{"a": [], "b": {}}', 3),
        (b"[ [ ], { }, [1] ]", 5),  # an empty container is one value
        (b'["a,b,[{", "\\"[,{"]', 3),  # commas and brackets inside a string are text
        (b'{"a": {"b": {"c": [1, 2, {}]}}, "d": null, "e": true}', 9),
        (b'"x"', 1),
    ],
)
def test_every_scalar_array_and_object_counts_once(
    body: bytes, values: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jsonio, "MAX_VALUES", values)
    parse_json(body)  # at the limit
    monkeypatch.setattr(jsonio, "MAX_VALUES", values - 1)
    if values > 1:
        with pytest.raises(ResourceLimitError, match=f"more than {values - 1} values"):
            parse_json(body)


def test_a_side_over_the_value_count_is_incomplete_resource_limit(
    make_spec, make_observation, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jsonio, "MAX_VALUES", 10)
    spec = make_spec()
    side, caps = spec.sides[Side.A], TargetCapabilities()
    state = observe(make_observation(b"[" + b"1," * 10 + b"1]"), side, spec, caps)
    assert (state.kind, state.reason, state.detail) == (
        SideStateKind.INCOMPLETE,
        "resource_limit",
        "body exceeds a parse limit: body holds more than 10 values",
    )
    at_limit = observe(make_observation(b"[" + b"1," * 8 + b"1]"), side, spec, caps)
    assert at_limit.kind is SideStateKind.COMPLETE
