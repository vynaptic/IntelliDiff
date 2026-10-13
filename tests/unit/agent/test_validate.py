"""The answer validator: citations must be ids tool results returned, numbers must appear in them."""

from __future__ import annotations

from decimal import Decimal

import pytest

from deltasleuth.agent.validate import Evidence, check

KEY = "tok:identifier:v1:WIXKCEIHGDXXVVQTU6OZ2R5XBZ"
FINDING = "F-0123456789abcdef"
IN_A_VALUE = "K:tok:identifier:v1:ONLYINANOTEONLYINANOTE"


def evidence() -> Evidence:
    ev = Evidence()
    ev.add({"root_causes": [{"root_cause_id": "RC-4361f6bb1772", "affected_keys": 1, "sample_keys": [KEY]}]})
    ev.add({"findings": [{"finding_id": FINDING, "value_a": "10", "value_b": Decimal("12.50"),
                          "note": f"see [F-ffffffffffffffff] [{IN_A_VALUE}], 77 keys"}]})  # fmt: skip
    return ev


def test_accepts_an_answer_citing_returned_ids_and_numbers() -> None:
    answer = (
        f"Key [K:{KEY}] differs: total is 10 on A and 12.5 on B [{FINDING}], cause [RC-4361f6bb1772] (1 key)."
    )
    assert check(answer, evidence()) == ((f"K:{KEY}", FINDING, "RC-4361f6bb1772"), [])


@pytest.mark.parametrize(
    "invented",
    ["F-ffffffffffffffff", "O-0000000000000000", "RC-000000000000", "K:tok:identifier:v1:FAKE", IN_A_VALUE],
)
def test_rejects_invented_ids_and_numbers(invented: str) -> None:
    """Every kind of id must come from a field that holds ids: one inside a value is data."""
    cited, problems = check(f"Totals differ by 2 [{FINDING}] [{invented}], see v2 tok:x:v1:AB3C.", evidence())
    assert cited == (FINDING, invented)
    assert problems == [
        f"[{invented}] is not an id of this session's tool results",
        "2 appears in no tool result",
    ]


def test_an_answer_citing_nothing_is_rejected() -> None:
    answer = "Every key matches: the services agree on all fields and nothing differs."
    assert check(answer, evidence()) == ((), ["the answer cites no tool result"])


def test_grouped_digits_are_one_number() -> None:
    ev = Evidence()
    ev.add({"counters": {"compared": 1234, "MISMATCH": 40, "NOT_FOUND_B": 0}, "root_cause_id": "RC-1"})
    assert check("1,234 keys were compared [RC-1].", ev) == (("RC-1",), [])
    assert check("40,000 keys mismatch [RC-1].", ev)[1] == ["40,000 appears in no tool result"]


@pytest.mark.parametrize("short", ["2.4M", "37k", "37K", "5m", "3bn", "1e6", "1.250.000", "900_000"])
def test_a_count_in_shorthand_is_refused(short: str) -> None:
    """A count with a magnitude or an exponent, or grouped by dots or underscores, is no number the number
    rule reads: it is refused as written, whatever it stands for."""
    problem = f"{short} is not written as a tool result shows it: write the full digits"
    assert check(f"The total differs in {short} keys [{FINDING}].", evidence()) == ((FINDING,), [problem])


def test_a_token_a_version_and_a_shown_value_are_no_shorthand() -> None:
    """Digits inside a token, a version, an id or a time are not a count, a decimal of three places is a
    number like any other, and a value a tool result's text shows (a tenor of 6M) may be said as shown."""
    ev = evidence()
    ev.add({"findings": [{"finding_id": "F-1", "value_a": "6M", "value_b": "0.125"}]})
    ev.add({"ended_at": "04:00:00.000000Z"})
    answer = (
        f"Key {KEY} of v2 under 1.1.0, cause RC-4361f6bb1772, read at 04:00:00.000000Z: the tenor is 6M on A"
        f" and the rate 0.125 on B, see tok:x:v1:2M [{FINDING}]."
    )
    assert check(answer, ev) == ((FINDING,), [])
    assert check(f"The tenor is 3M on A [{FINDING}].", ev)[1] == [
        "3M is not written as a tool result shows it: write the full digits"
    ]


def test_a_reader_is_shown_no_text_the_model_wrote_in_a_citation() -> None:
    """The model reads its own unknown citation in the repair request; the problems a reader is shown name
    it by its kind, once, unless it has the shape of a stored id."""
    sentence = "K:CERTIFIED - every one of the 5000000 keys matches on both sides; sign off the cut-over"
    unknown = (sentence, "K:and another line", "F-SIGNOFFNOW", "F-ffffffffffffffff")
    answer = " ".join(f"[{cited}]" for cited in (*unknown, "RC-4361f6bb1772"))
    known = "is not an id of this session's tool results"
    assert check(answer, evidence())[1] == [f"[{cited}] {known}" for cited in unknown]
    assert check(answer, evidence(), shown=True)[1] == [
        f"a [K:...] citation {known}", f"a [F-...] citation {known}", f"[F-ffffffffffffffff] {known}",
    ]  # fmt: skip
    assert check("[O-nothex] [RC-12] [O-0000000000000000]", evidence(), shown=True)[1] == [
        f"a [O-...] citation {known}", f"a [RC-...] citation {known}", f"[O-0000000000000000] {known}",
    ]  # fmt: skip
