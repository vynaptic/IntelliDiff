"""Cron times in a time zone (docs/mvp/SCHEDULES.md §4, §9): the binding messages, the zones a schedule
takes, a skipped time firing once and later, a repeated one at its first occurrence, the 15-minute floor
across midnight, the notes of each clock change, and the functions' laws over generated expressions. The
zones are names every tz release holds; the version in a message is a patched one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from deltasleuth.core import cron
from deltasleuth.core.cron import CronError

ZONES = [
    "UTC",
    "Europe/London",
    "America/New_York",
    "Australia/Lord_Howe",
    "Asia/Kolkata",
    "Pacific/Auckland",
]
NOT_ONE = " is not *, a number, a range a-b, a list a,b or a step */n or a-b/n"
FLOOR = ": a schedule fires at most every 15 minutes (a soak's execution.cycleInterval repeats faster)"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def times(expression: str, zone: str, after: str, count: int) -> list[str]:
    return [cron.utc(t) for t in cron.upcoming(expression, zone, at(after), count)]


@pytest.fixture(autouse=True)
def tzdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron, "tzdata", lambda: "2026c")


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("0 2 * * * *", "cron must have 5 fields (minute hour day-of-month month day-of-week), not 6"),
        ("0 2 * *", "cron must have 5 fields (minute hour day-of-month month day-of-week), not 4"),
        ("", "cron must have 5 fields (minute hour day-of-month month day-of-week), not 0"),
        ("61 * * * *", "cron field minute: '61' is outside 0-59"),
        ("0 24 * * *", "cron field hour: '24' is outside 0-23"),
        ("0 2 0 * *", "cron field day-of-month: '0' is outside 1-31"),
        ("0 2 * 13 *", "cron field month: '13' is outside 1-12 or JAN-DEC"),
        ("0 2 * * 8", "cron field day-of-week: '8' is outside 0-7 or SUN-SAT"),
        ("0 2- * * *", f"cron field hour: '2-'{NOT_ONE}"),
        ("0 2 ? * *", f"cron field day-of-month: '?'{NOT_ONE}"),
        ("0 2 L * *", f"cron field day-of-month: 'L'{NOT_ONE}"),
        ("0 2 * * 1#2", f"cron field day-of-week: '1#2'{NOT_ONE}"),
        ("0 2 * FOO *", f"cron field month: 'FOO'{NOT_ONE}"),
        ("MON 2 * * *", f"cron field minute: 'MON'{NOT_ONE}"),
        ("5/15 2 * * *", f"cron field minute: '5/15'{NOT_ONE}"),
        *(  # letters and digits beyond ASCII: U+017F upper-cases to S, U+0131 to I, U+0662 is a digit
            (expression.format(odd), f"cron field {field}: '{part.format(odd)}'{NOT_ONE}")
            for expression, odd, field, part in (
                ("0 2 * * {}un", "\u017f", "day-of-week", "{}un"),
                ("0 2 * * mon-fr{}/2", "\u0131", "day-of-week", "mon-fr{}/2"),
                ("0 2 * {}ep *", "\u017f", "month", "{}ep"),
                ("0 {} * * *", "\u0662", "hour", "{}"),
                ("0 */{} * * *", "\u0662", "hour", "*/{}"),
            )
        ),
        ("0 1,,2 * * *", f"cron field hour: ''{NOT_ONE}"),
        ("@daily", "cron must have 5 fields (minute hour day-of-month month day-of-week), not 1"),
        ("0 2 * * 5-1", "cron field day-of-week: range '5-1' runs backwards"),
        ("*/0 2 * * *", "cron field minute: step '0' must be at least 1"),
        ("0 2 1 * MON", "cron sets both day-of-month and day-of-week: set one and leave the other * (cron"
                        " dialects disagree on what both mean)"),
        ("0 2 30 2 *", "cron names no time in the next 8 years"),
        ("0 2 31 4,6,9,11 *", "cron names no time in the next 8 years"),
        ("*/5 2 * * *", f"cron fires at 02:00 and 02:05{FLOOR}"),
        ("* * * * *", f"cron fires at 00:00 and 00:01{FLOOR}"),
        ("0,10 9 * * 1-5", f"cron fires at 09:00 and 09:10{FLOOR}"),
        ("0 2 * * * " + "1," * 46 + "1", "cron must be at most 100 characters"),
    ],
)  # fmt: skip
def test_messages(expression: str, message: str) -> None:
    with pytest.raises(CronError) as refused:
        cron.parse(expression)
    assert str(refused.value) == message


def test_what_a_field_takes() -> None:
    parsed = cron.parse("  0,30   */6  *   jan-MAR,12   mon-Fri ")
    assert parsed.text == "0,30 */6 * jan-MAR,12 mon-Fri"  # stored trimmed and single-spaced
    assert (sorted(parsed.minutes), sorted(parsed.hours)) == ([0, 30], [0, 6, 12, 18])
    assert (sorted(parsed.months), sorted(parsed.weekdays)) == ([1, 2, 3, 12], [1, 2, 3, 4, 5])
    assert cron.parse("0 2 * * 7").weekdays == cron.parse("0 2 * * SUN").weekdays == frozenset({0})
    assert sorted(cron.parse("0 2 * * 5-7").weekdays) == [0, 5, 6]
    assert sorted(cron.parse("0 2 1-31/10 * *").days) == [1, 11, 21, 31]
    assert sorted(cron.parse("15 */90 * * *").hours) == [0]
    assert cron.parse("0 2 * * *") is cron.parse("0 2 * * *")  # parsed once


def test_only_the_trimmed_text_is_remembered() -> None:
    cron._parse.cache_clear()
    padded = [cron.parse("0 2 * * *" + " " * 1000 * n) for n in range(1, 30)]  # as a request may pad it
    assert all(parsed is padded[0] for parsed in padded) and cron._parse.cache_info().currsize == 1
    for text in ("0 2 * * " + "1," * 46 + "1" + " " * 100_000, "61 2 * * *" + " " * 100_000):
        with pytest.raises(CronError):
            cron.parse(text)
    assert cron._parse.cache_info().currsize == 1  # and a refused text is never kept


def test_zone_messages() -> None:
    with pytest.raises(CronError) as refused:
        cron.zone("Asia/Calcutta")
    assert str(refused.value) == (
        "'Asia/Calcutta' is not a time zone this build uses (tzdata 2026c): use Asia/Kolkata"
    )
    with pytest.raises(CronError) as refused:
        cron.zone("Mars/Olympus")
    assert str(refused.value) == (
        "'Mars/Olympus' is not a time zone this build knows (tzdata 2026c): use an IANA region name such as"
        " Europe/London, or UTC"
    )
    with pytest.raises(CronError) as refused:
        cron.zone("localtime")
    assert str(refused.value) == (
        "time_zone must be UTC or an IANA region name such as Europe/London: 'localtime' is not one"
    )
    for legacy, primary in cron.RENAMED.items():  # refused with its primary even where the host holds it
        with pytest.raises(CronError, match=f"use {primary}$"):
            cron.zone(legacy)
        assert "/" in legacy and "/" in primary and primary not in cron.RENAMED
    assert cron.RENAMED["Europe/Kiev"] == "Europe/Kyiv" and cron.RENAMED["US/Eastern"] == "America/New_York"


@pytest.mark.parametrize(
    "name", ["localtime", "Factory", "posixrules", "GMT", "EST", "CET", "Zulu", "", "utc"]
)
def test_localtime_factory_and_bare_names_refused(name: str) -> None:
    with pytest.raises(CronError, match=r"^time_zone must be UTC or an IANA region name"):
        cron.zone(name)
    assert name not in cron.zones()
    assert "UTC" in cron.zones() and all("/" in zone for zone in cron.zones() - {"UTC"})
    assert cron.zone("UTC") is UTC and str(cron.zone("Europe/London")) == "Europe/London"


def test_the_version_is_the_zone_path_s_first(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the real reader
    monkeypatch.setattr(cron.zoneinfo, "TZPATH", (f"{tmp_path}/none", f"{tmp_path}"))
    assert cron.tzdata() == "unknown"
    with open(f"{tmp_path}/tzdata.zi", "w") as file:
        file.write("# version 2026c\n# This zic input file is in the public domain.\n")
    assert cron.tzdata() == "2026c"


def test_gap_fires_once_later() -> None:
    # 01:30 does not exist in London on 2027-03-28: it fires at 02:30 BST, which is 01:30 UTC.
    assert times("30 1 * * *", "Europe/London", "2027-03-27T12:00", 2) == [
        "2027-03-28T01:30Z",
        "2027-03-29T00:30Z",
    ]
    # 02:30 does not exist in New York on 2027-03-14: 03:30 EDT.
    assert times("30 2 * * *", "America/New_York", "2027-03-13T12:00", 2) == [
        "2027-03-14T07:30Z",
        "2027-03-15T06:30Z",
    ]
    # Lord Howe's clocks jump half an hour: 02:15 fires at 02:45 on 2026-10-04.
    assert times("15 2 * * *", "Australia/Lord_Howe", "2026-10-03T00:00", 2) == [
        "2026-10-03T15:45Z",
        "2026-10-04T15:15Z",
    ]
    # Two times on one instant fire once: the skipped 01:30 and 02:30 BST.
    assert times("30 1,2 * * *", "Europe/London", "2027-03-27T12:00", 3) == [
        "2027-03-28T01:30Z",
        "2027-03-29T00:30Z",
        "2027-03-29T01:30Z",
    ]


def test_repeat_fires_first() -> None:
    # 01:30 happens twice in London on 2026-10-25: the first, in BST, is 00:30 UTC.
    assert times("30 1 * * *", "Europe/London", "2026-10-24T12:00", 2) == [
        "2026-10-25T00:30Z",
        "2026-10-26T01:30Z",
    ]
    assert cron.after("30 1 * * *", "Europe/London", at("2026-10-25T00:30")) == at("2026-10-26T01:30")


def test_hourly_through_both_nights() -> None:
    back = cron.between("0 * * * *", "Europe/London", at("2026-10-24T21:30"), at("2026-10-25T04:30"))
    assert [cron.utc(t) for t in back] == [
        "2026-10-24T22:00Z", "2026-10-24T23:00Z", "2026-10-25T00:00Z",  # 01:00 BST, never the second 01:00
        "2026-10-25T02:00Z", "2026-10-25T03:00Z", "2026-10-25T04:00Z",
    ]  # fmt: skip
    forward = cron.between("0 * * * *", "Europe/London", at("2027-03-27T22:30"), at("2027-03-28T03:30"))
    assert [cron.utc(t) for t in forward] == [  # 01:00 GMT is skipped to 02:00 BST, the instant of 01:00 UTC
        "2027-03-27T23:00Z", "2027-03-28T00:00Z", "2027-03-28T01:00Z", "2027-03-28T02:00Z",
        "2027-03-28T03:00Z",
    ]  # fmt: skip
    utc = cron.between("0 * * * *", "UTC", at("2026-10-24T21:30"), at("2026-10-25T04:30"))
    assert len(utc) == 7 and len(set(utc)) == 7  # T4: neither


def test_leap_day() -> None:
    assert times("0 6 29 2 *", "UTC", "2026-10-01T00:00", 2) == ["2028-02-29T06:00Z", "2032-02-29T06:00Z"]
    # 2100 is no leap year: the next one after 2096 is 8 years on, the horizon.
    assert times("0 6 29 2 *", "UTC", "2096-03-01T00:00", 1) == ["2104-02-29T06:00Z"]
    assert cron.latest("0 6 29 2 *", "UTC", at("2104-02-28T00:00")) == at("2096-02-29T06:00")
    assert cron.after("0 6 29 2 *", "UTC", at("2026-10-01T00:00"), until=at("2028-02-29T05:59")) is None
    assert cron.after("0 6 29 2 *", "UTC", at("2026-10-01T00:00"), until=at("2028-02-29T06:00")) == at(
        "2028-02-29T06:00"
    )


def test_spacing_across_midnight() -> None:
    with pytest.raises(CronError, match=r"^cron fires at 23:50 and 00:00: a schedule fires at most every 15"):
        cron.parse("0,50 0,23 * * *")
    with pytest.raises(CronError, match=r"^cron fires at 23:55 and 00:05: "):
        cron.parse("5,55 0,23 * * 1-2")  # Monday's last and Tuesday's first
    with pytest.raises(CronError, match=r"^cron fires at 23:55 and 00:05: "):
        cron.parse("5,55 0,23 31,1 * *")  # a month's last day and the next one's first
    with pytest.raises(CronError, match=r"^cron fires at 23:55 and 00:05: "):
        cron.parse("5,55 0,23 * * 6,0")  # Saturday and Sunday
    assert cron.parse("5,55 0,23 * * 1,3")  # never two days in a row
    assert cron.parse("5,55 0,23 1,15 * *")
    assert cron.parse("0,15,30,45 * * * *") and cron.parse("45 23 * * *") and cron.parse("0 0 * * *")


def test_notes_name_each_shift() -> None:
    assert cron.notes("30 1 * * *", "Europe/London", at("2026-10-01T00:00")) == [
        "2026-10-25: 01:30 happens twice in Europe/London (clocks go back): the schedule fires at the first"
        " (00:30 UTC)",
        "2027-03-28: 01:30 does not exist in Europe/London (clocks go forward): that firing happens at 02:30"
        " (01:30 UTC)",
    ]
    assert cron.notes("15 2 * * *", "Australia/Lord_Howe", at("2026-10-01T00:00"), days=30) == [
        "2026-10-04: 02:15 does not exist in Australia/Lord_Howe (clocks go forward): that firing happens at"
        " 02:45 (15:45 UTC)"
    ]
    # A gap that crosses midnight: the firing is on the next day, which the note says.
    assert cron.notes("30 23 * * SAT", "America/Nuuk", at("2027-03-01T00:00"), days=60) == [
        "2027-03-27: 23:30 does not exist in America/Nuuk (clocks go forward): that firing happens at"
        " 2027-03-28 00:30 (01:30 UTC)"
    ]
    assert cron.notes("30 3 * * *", "Europe/London", at("2026-10-01T00:00")) == []  # no shift touches 03:30
    assert cron.notes("30 1 * * *", "UTC", at("2026-10-01T00:00")) == []
    assert cron.notes("30 1 * * 1", "Europe/London", at("2026-10-01T00:00")) == []  # the shifts are Sundays
    assert len(cron.notes("*/15 * * * *", "Europe/London", at("2026-10-01T00:00"))) == 8  # each night's four
    assert len(cron.notes("*/15 * * * *", "Europe/London", at("2026-10-01T00:00"), days=3700)) == 10


FIELDS = st.tuples(
    st.sampled_from(["0", "30", "0,30", "*/15", "15,45", "7"]),
    st.sampled_from(["*", "2", "1,2", "*/6", "0-3", "23", "0,23"]),
    st.sampled_from(["*", "1", "31", "29", "1,15", "*/10"]),
    st.sampled_from(["*", "2", "3,10", "MAR-NOV", "*/3"]),
    st.sampled_from(["*", "0", "1-5", "SAT,SUN", "7"]),
).filter(lambda fields: fields[2] == "*" or fields[4] == "*")
INSTANTS = st.datetimes(datetime(2024, 1, 1), datetime(2031, 1, 1), timezones=st.just(UTC))


def named(parsed: cron.Cron, wall: datetime) -> bool:
    """Whether the wall-clock time is one the expression names."""
    return wall.minute in parsed.minutes and wall.hour in parsed.hours and parsed.on(wall.date())


@given(FIELDS, st.sampled_from(ZONES), INSTANTS)
@settings(max_examples=300, deadline=None)
def test_the_functions_agree(fields: tuple[str, ...], zone: str, t: datetime) -> None:
    expression = " ".join(fields)
    try:
        parsed = cron.parse(expression)
    except CronError as refused:  # no time, or two under 15 minutes apart: never anything else here
        assert str(refused).startswith(("cron names no time", "cron fires at "))
        return
    found = cron.upcoming(expression, zone, t, 5)
    assert len(found) == 5 and found[0] == cron.after(expression, zone, t)
    assert all(a < b for a, b in zip([t, *found], found, strict=False))  # after strictly increases
    walk, last = [], t
    for _ in found:
        last = cron.after(expression, zone, last)  # type: ignore[assignment]
        walk.append(last)
        assert cron.latest(expression, zone, last) == last
    assert walk == found == cron.between(expression, zone, t, found[-1])  # iterated after
    assert cron.between(expression, zone, t, found[-1] - timedelta(minutes=1)) == found[:-1]
    assert cron.after(expression, zone, t, until=found[0] - timedelta(minutes=1)) is None
    before = cron.latest(expression, zone, t)  # the time before the first found: none lies between
    assert before is not None and before <= t and cron.after(expression, zone, before) == found[0]
    where = ZoneInfo(zone)
    for instant in found:
        wall = instant.astimezone(where)
        if named(parsed, wall):
            continue
        # Else a time the clock skipped, fired later by the size of the jump: some named wall time of that
        # day or the day before does not exist and reads, with fold 0, as this instant.
        skipped = [
            candidate
            for day in (wall.date(), wall.date() - timedelta(days=1))
            for hour in parsed.hours
            for minute in parsed.minutes
            if parsed.on(day)
            for candidate in [datetime(day.year, day.month, day.day, hour, minute, tzinfo=where)]
            if candidate.astimezone(UTC) == instant
        ]
        assert skipped and all(
            s.astimezone(UTC).astimezone(where).replace(tzinfo=None) != s.replace(tzinfo=None)
            for s in skipped
        )
