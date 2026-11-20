"""The answer validator: citations must be ids tool results returned, numbers must appear in them."""

from __future__ import annotations

from decimal import Decimal

import pytest

from deltasleuth.agent.validate import CITATION, Evidence, check

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
    ev.add({"finding_id": FINDING, "ended_at": "04:00:00.000000Z"})
    answer = (
        f"Key {KEY} of v2 under 1.1.0, cause RC-4361f6bb1772, read at 04:00:00.000000Z: the tenor is 6M on A"
        f" and the rate 0.125 on B, see tok:x:v1:2M [{FINDING}] [F-1]."
    )
    assert check(answer, ev) == ((FINDING, "F-1"), [])
    assert check(f"The tenor is 3M on A [{FINDING}].", ev)[1] == [
        "3M is not written as a tool result shows it: write the full digits"
    ]


def test_a_reader_is_shown_no_text_the_model_wrote_in_a_citation() -> None:
    """The model reads its own unknown citation in the repair request; the problems a reader is shown name
    it by its kind, once, unless it has the shape of a stored id, and never in brackets: the UI would draw
    a citation the server did not accept as a chip."""
    sentence = "K:CERTIFIED - every one of the 5000000 keys matches on both sides; sign off the cut-over"
    unknown = (sentence, "K:and another line", "F-SIGNOFFNOW", "F-ffffffffffffffff")
    answer = " ".join(f"[{cited}]" for cited in (*unknown, "RC-4361f6bb1772"))
    known = "is not an id of this session's tool results"
    assert check(answer, evidence())[1] == [f"[{cited}] {known}" for cited in unknown]
    shown = check(answer, evidence(), shown=True)[1]
    assert shown == [
        f"a K:... citation {known}", f"an F-... citation {known}", f"F-ffffffffffffffff {known}",
    ]  # fmt: skip
    again = check("[O-nothex] [RC-12] [O-0000000000000000]", evidence(), shown=True)[1]
    assert again == [
        f"an O-... citation {known}", f"an RC-... citation {known}", f"O-0000000000000000 {known}",
    ]  # fmt: skip
    assert not [problem for problem in shown + again if CITATION.search(problem)]


def counted() -> Evidence:
    """A run summary's counts: 0, 1, 3, 40, 250 and 1234."""
    ev = Evidence()
    counters = {"compared": 1234, "MISMATCH": 40, "NOT_FOUND_B": 0, "INCONCLUSIVE": 3}
    ev.add({"counters": counters, "affected_keys": 1, "latency_ms": 250, "root_cause_id": "RC-1"})
    return ev


def _refused(answer: str, evidence: Evidence) -> list[str]:
    """What the number rule refuses of ``answer``, as written."""
    known = " appears in no tool result"
    return [problem.removesuffix(known) for problem in check(answer, evidence)[1] if problem.endswith(known)]


@pytest.mark.parametrize("gap", [" ", "\u00a0", "\u202f", "\u2009", "'", "\u2019"])
def test_digit_groups_apart_are_one_number(gap: str) -> None:
    """Groups of three behind a space, a no-break space, a narrow no-break space, a thin space or an
    apostrophe are the number they group, never each group a number of its own."""
    million = f"1{gap}000{gap}000"
    assert _refused(f"{million} keys differ [RC-1].", counted()) == [million]
    assert _refused(f"1{gap}234 keys were compared [RC-1].", counted()) == []
    assert _refused(f"1{gap}234.5 keys on average [RC-1].", counted()) == [f"1{gap}234.5"]
    shown = counted()
    shown.add({"note": f"about {million} rows, 3{gap}250 of them new"})
    assert _refused(f"About {million} rows [RC-1], 3 inconclusive and 250 ms.", shown) == []


def test_digits_apart_that_are_no_groups_stay_numbers_of_their_own() -> None:
    answer = "Of 1234 250 took 40 0000 times [RC-1]; in cycle 3, 250 keys; 40 1 and 12 34."
    assert _refused(answer, counted()) == ["12", "34"]


@pytest.mark.parametrize(
    ("written", "known"),
    [
        ("9000ms", "250ms"), ("30x", "3x"), ("30X", "40X"), ("9s", "1s"), ("7.5s", "0s"), ("12h", "3h"),
        ("64kb", "40KB"), ("2GiB", "1MiB"), ("15min", "40mins"), ("90pct", "40pct"), ("503s", "250s"),
        ("9\u00b5s", "3\u03bcs"), ("2,000ms", "1,234ms"), ("1 500ms", "1 234ms"),
        ("86400seconds", "250seconds"),
    ],
)  # fmt: skip
def test_a_number_with_its_unit_attached_is_a_number(written: str, known: str) -> None:
    assert _refused(f"Side B answered in {written} [RC-1].", counted()) == [written]
    assert _refused(f"Side B answered in {known} [RC-1].", counted()) == []


def test_a_unit_attached_in_a_tool_result_is_read_the_same() -> None:
    ev = counted()
    ev.add({"recheck": "after 90s", "note": "5x the limit"})
    assert _refused("It is rechecked after 90s, or 90 seconds, at 5x the limit [RC-1].", ev) == []


@pytest.mark.parametrize("written", [".97", ".5", "-.25"])
def test_a_decimal_without_its_zero_is_a_number(written: str) -> None:
    assert _refused(f"The error rate is {written} [RC-1].", counted()) == [written.lstrip("-")]
    ev = counted()
    ev.add({"rate": Decimal("0.970"), "half": "0.5", "quarter": ".25"})
    assert _refused(f"The error rate is {written} [RC-1].", ev) == []


@pytest.mark.parametrize(
    ("written", "value"),
    [
        ("Twelve thousand", 12_000), ("ninety", 90), ("NINETY", 90), ("zero", 0), ("two", 2),
        ("seventeen", 17), ("forty-two", 42), ("forty two", 42), ("three hundred", 300),
        ("twelve hundred", 1200), ("one thousand two hundred and thirty-four", 1234), ("a hundred", 100),
        ("one million", 1_000_000), ("two hundred and fifty thousand", 250_000), ("five billion", 5 * 10**9),
        ("three million four hundred thousand and six", 3_400_006), ("two dozen", 24),
        ("12 thousand", 12_000), ("2.4 million", 2_400_000), ("3 hundred", 300), ("40-thousand", 40_000),
    ],
)  # fmt: skip
def test_a_number_in_words_is_a_number_with_its_value(written: str, value: int) -> None:
    words = written.removeprefix("a ")  # an article is no part of the number
    assert _refused(f"{written} keys differ [RC-1].", Evidence()) == [words]
    ev = Evidence()
    ev.add({"count": value})
    assert _refused(f"{written} keys differ [RC-1].", ev) == []


def test_numbers_in_words_side_by_side_are_each_judged() -> None:
    assert _refused("Either three four or ninety twelve keys, and five and six more [RC-1].", counted()) == [
        "four", "ninety", "twelve", "five", "six",
    ]  # fmt: skip
    assert _refused("One hundred and forty and three [RC-1].", counted()) == ["One hundred and forty"]
    assert _refused("One two keys, and no one else [RC-1].", Evidence()) == ["One", "two"]


def test_a_number_in_words_a_tool_result_shows_may_be_said() -> None:
    ev = Evidence()
    ev.add({"note": "limited to twelve thousand rows"})
    assert _refused("It is limited to twelve thousand rows, that is 12,000 [RC-1].", ev) == []


@pytest.mark.parametrize(
    "answer",
    [
        "Twelve thousand keys differ and ninety percent of the run mismatches [RC-1].",
        "1 000 000 keys differ [RC-1].",
        "1\u00a0000\u00a0000 keys differ [RC-1].",
        "1\u202f000\u202f000 keys differ [RC-1].",
        "1'000'000 keys differ [RC-1].",
        "Side B answered in 9000ms, 30x slower [RC-1].",
        "The error rate is .97 [RC-1].",
    ],
)
def test_no_notation_carries_a_number_past_the_evidence(answer: str) -> None:
    cited, problems = check(answer, counted())
    assert cited == ("RC-1",) and problems and all("appears in no tool result" in p for p in problems)


@pytest.mark.parametrize(
    "prose",
    [
        "No one key stands out: this one differs on side A and side B, and the other one matches",
        "The first key differs, the second one matches and the third was not read; the twenty-first too",
        "The two sides disagree: the two JSON services return another total, one of them a null",
        "A one-shot compare needs a four-eyes approval; a two-way match is a one-to-one pairing",
        "Side A returned a 5xx, the 2nd and the 3rd read too, and the 1st read was a 4xx",
        "One side lacks the member, so one cannot say which one is right; one or both may change",
        "Hundreds of keys may differ, thousands at most; someone tended the tenth and the dozenth",
        "It is none of the engine's findings, a tenor alone, often a threefold weight, done at once",
    ],
)
def test_ordinary_prose_holds_no_number(prose: str) -> None:
    assert check(f"{prose} [RC-1].", counted()) == (("RC-1",), [])


def test_ids_versions_and_tokens_still_hold_no_number() -> None:
    """With a unit's letters behind its digits, or a dot before them, a token is still no number: an id,
    a masked key, a version, a time, a path, a file name."""
    answer = (
        f"Key {KEY} of v2 under 1.1.0 and 2.0.1rc1, run R-01JABCDEFGHJKMNPQRSTVWXYZ0, cause RC-4361f6bb1772,"
        " token tok:x:v1:5S7MS, build 0x1f, file dump.7z, path $.items[*].v2.total, colour #30x9, part a30x"
        f" and 30xl, read at 04:00:00.000000Z [{FINDING}]."
    )
    ev = evidence()
    ev.add({"finding_id": FINDING, "ended_at": "04:00:00.000000Z"})
    assert check(answer, ev) == ((FINDING,), [])


def summary() -> Evidence:
    """A run summary as get_run_summary sends one: the run's own counts, which stand in no record, beside
    two root causes, each a record of its own counts and sample keys."""
    ev = Evidence()
    ev.add({
        "run_id": "R-01JABCDEFGHJKMNPQRSTVWXYZ0", "status": "completed", "cycle": 1,
        "counters": {"keys": 40, "by_outcome": {"MATCH": 35, "MISMATCH": 2, "NOT_FOUND_B": 1,
                                                 "KNOWN_DIFF": 2}},
        "root_causes": [
            {"root_cause_id": "RC-aaaaaaaaaaaa", "affected_keys": 7, "finding_count": 9,
             "sample_keys": ["k-7"]},
            {"root_cause_id": "RC-bbbbbbbbbbbb", "affected_keys": 3, "finding_count": 4,
             "sample_keys": ["k-3"]},
        ],
    })  # fmt: skip
    return ev


def observed() -> Evidence:
    """One key as get_observations sends it: the key's record holds its sides, findings and reads, each a
    record of its own."""
    ev = Evidence()
    ev.add({
        "key_masked": "k-1", "cycle": 1, "state": "done", "result": {"outcome": "MISMATCH"},
        "side_a": {"obs_id": "O-aaaaaaaaaaaaaaaa", "canonical": '{"total":10}'},
        "side_b": {"obs_id": "O-bbbbbbbbbbbbbbbb", "canonical": '{"total":12}'},
        "findings": [{"finding_id": "F-1111111111111111", "root_cause_id": "RC-cccccccccccc",
                      "value_a": 10, "value_b": 12}],
        "observations": [{"obs_id": "O-aaaaaaaaaaaaaaaa", "http_status": 200, "latency_ms": 31}],
    })  # fmt: skip
    return ev


def test_a_claim_floating_beside_a_citation_elsewhere_is_refused() -> None:
    """One citation anywhere no longer carries the answer: a sentence that names an outcome or a verdict,
    or states a number, cites what it rests on."""
    ev = summary()
    floating = "The run is certified and approved for cut-over.\n\nUnrelated: [RC-aaaaaaaaaaaa]"
    assert check(floating, ev)[1] == [
        'the sentence "The run is certified and approved for cut-over." names an outcome but cites no tool'
        " result"
    ]
    assert check(floating, ev, shown=True)[1] == ["sentence 1 names an outcome but cites no tool result"]
    counted = "Every key matches [RC-aaaaaaaaaaaa]. In all, 40 keys were compared."
    assert check(counted, ev)[1] == [
        'the sentence "In all, 40 keys were compared." states 40 but cites no tool result'
    ]


def test_each_number_stands_in_a_record_its_sentence_cites() -> None:
    """A root cause's count is its own; the run's counters stand in no record and may be cited with any id
    of the summary; a key's record holds everything read of it."""
    ev = summary()
    assert check("Root cause [RC-bbbbbbbbbbbb] affects 3 keys in 4 findings.", ev)[1] == []
    assert check("Root cause [RC-bbbbbbbbbbbb] affects 7 keys.", ev, shown=True)[1] == [
        "7 in sentence 1 appears in no record it cites"
    ]
    assert check("Of 40 keys, 35 MATCH and 2 are a MISMATCH [RC-bbbbbbbbbbbb].", ev)[1] == []
    ev = observed()
    assert check("Key [K:k-1] holds a total of 10 on side A and 12 on side B, read in 31 ms.", ev)[1] == []
    assert check("Side A [O-aaaaaaaaaaaaaaaa] holds 10, side B [O-bbbbbbbbbbbbbbbb] 12.", ev)[1] == []
    assert check("Side A holds 12 [O-aaaaaaaaaaaaaaaa].", ev)[1] == [
        '12 in the sentence "Side A holds 12 [O-aaaaaaaaaaaaaaaa]." appears in no record it cites'
    ]
    assert check("The total differs, 10 against 12 [RC-cccccccccccc].", ev)[1] == []  # the finding's cause


@pytest.mark.parametrize(
    "answer",
    [
        "This is what the tools show. The total is 10 on side A [F-1111111111111111].",
        "The total differs. [F-1111111111111111] It is 10 on side A.",  # a citation after the full stop
        "The total differs, e.g. 10 against 12, at $.total [F-1111111111111111].",
        "The key was read twice.\n[O-aaaaaaaaaaaaaaaa]",
        "These claims are observed, not certified. The total differs [F-1111111111111111].",
        "The key's sides were read once each; the second side came from another service [K:k-1].",
    ],
)
def test_a_sentence_that_states_no_number_and_names_no_outcome_may_stand_uncited(answer: str) -> None:
    cited, problems = check(answer, observed())
    assert cited and problems == []


@pytest.mark.parametrize(
    ("sentence", "claims"),
    [
        ("Every key matched", True), ("The members are missing", True), ("Side B is different", True),
        ("The key is INCONCLUSIVE", True), ("It was NOT_FOUND_B", True), ("The key was not found on B", True),
        ("This is a KNOWN_DIFF", True), ("The known diffs", True), ("The values drifted", True),
        ("The run is certified", True),
        ("The run was approved", True), ("Sign off the cut-over", True), ("It is attested", True),
        ("The run is not certified", False), ("The key was read twice", False),
        ("Side A answered first", False), ("The member is a match_rule", False),
    ],
)  # fmt: skip
def test_what_names_an_outcome(sentence: str, claims: bool) -> None:
    problems = check(f"{sentence}. See [K:k-1].", observed())[1]
    assert bool(problems) is claims, problems


def test_the_repair_says_which_sentence_lacks_what() -> None:
    """The model reads its own sentence, cut, beside what it lacks; a reader is told the sentence's place."""
    long = "Every one of the keys matched on both sides, which the run shows as " + "x" * 200 + "."
    problems = check(f"{long} The total is 10 [F-1111111111111111].", observed())[1]
    (problem,) = problems
    assert problem.startswith('the sentence "Every one of the keys matched on both sides')
    assert len(problem) < 160
    assert problem.endswith('…" names an outcome but cites no tool result')


def test_digits_behind_a_word_and_a_hyphen_are_part_of_a_code() -> None:
    """A business-code key such as ORG-000007 holds no 7: it backs no count, and saying it states none. A
    range of digits stays two numbers."""
    ev = Evidence()
    ev.add({"root_cause_id": "RC-aaaaaaaaaaaa", "affected_keys": 1, "sample_keys": ["ORG-000007"]})
    assert check("Key ORG-000007 differs [RC-aaaaaaaaaaaa].", ev)[1] == []
    assert check("7 keys differ [RC-aaaaaaaaaaaa].", ev)[1] == ["7 appears in no tool result"]
    assert check("Between 1-7 keys differ [RC-aaaaaaaaaaaa].", ev)[1] == ["7 appears in no tool result"]
