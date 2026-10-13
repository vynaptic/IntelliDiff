"""jsonio.parse_json: strings that are not Unicode text are refused at the parser."""

from __future__ import annotations

import pytest

from deltasleuth.core.jsonio import JSONParseError, LoneSurrogateError, parse_json


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
