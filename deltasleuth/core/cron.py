"""Cron times in a time zone: what a 5-field expression names, as instants.

``minute hour day-of-month month day-of-week``; each field ``*``, a number, ``a-b``, a list, ``*/n`` or
``a-b/n``, months and weekdays by name too, weekday 0 and 7 both Sunday; restricting both day fields is
refused, as are times under ``SPACING_MIN`` minutes apart. A time is a wall-clock time of the zone read
with ``fold=0``: one the clock skips falls later by the size of the jump, one it repeats at its first
occurrence, and a day's instants are de-duplicated and sorted. Zones are ``UTC`` and the IANA region names
this build's tz data holds (``zones``), but the legacy names of ``RENAMED``. Pure: every function takes its
instants, none reads a clock. ``CronError`` carries the binding messages.
"""

from __future__ import annotations

import functools
import itertools
import re
import zoneinfo
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path

SPACING_MIN = 15  # two times of a cron are at least this far apart on the wall clock
HORIZON_YEARS = 8  # every cron names a time within this long: a 29 February at the latest
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
WEEKDAYS = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")
# Each field: its name, bounds, names (the first standing for ``low``) and how a refusal words its range.
_FIELDS = (
    ("minute", 0, 59, (), "0-59"),
    ("hour", 0, 23, (), "0-23"),
    ("day-of-month", 1, 31, (), "1-31"),
    ("month", 1, 12, MONTHS, "1-12 or JAN-DEC"),
    ("day-of-week", 0, 7, WEEKDAYS, "0-7 or SUN-SAT"),
)
_PART = re.compile(r"(\*|\w+)(?:-(\w+))?(?:/(\w+))?", re.ASCII)  # no letter or digit beyond ASCII
# Legacy names browsers following CLDR report, and the US/* links, with their IANA primaries.
RENAMED = {
    "Africa/Asmera": "Africa/Asmara", "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "America/Catamarca": "America/Argentina/Catamarca", "America/Cordoba": "America/Argentina/Cordoba",
    "America/Godthab": "America/Nuuk", "America/Indianapolis": "America/Indiana/Indianapolis",
    "America/Louisville": "America/Kentucky/Louisville", "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Katmandu": "Asia/Kathmandu", "Asia/Rangoon": "Asia/Yangon", "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Atlantic/Faeroe": "Atlantic/Faroe", "Europe/Kiev": "Europe/Kyiv", "Pacific/Enderbury": "Pacific/Kanton",
    "Pacific/Ponape": "Pacific/Pohnpei", "Pacific/Truk": "Pacific/Chuuk",
    "US/Alaska": "America/Anchorage", "US/Aleutian": "America/Adak", "US/Arizona": "America/Phoenix",
    "US/Central": "America/Chicago", "US/East-Indiana": "America/Indiana/Indianapolis",
    "US/Eastern": "America/New_York", "US/Hawaii": "Pacific/Honolulu",
    "US/Indiana-Starke": "America/Indiana/Knox", "US/Michigan": "America/Detroit",
    "US/Mountain": "America/Denver", "US/Pacific": "America/Los_Angeles", "US/Samoa": "Pacific/Pago_Pago",
}  # fmt: skip
_LEAP_CYCLE = [date(2000, 1, 1) + timedelta(days=n) for n in range(1462)]  # every date and weekday pattern


class CronError(ValueError):
    """A cron expression or a time zone refused."""


@dataclass(frozen=True)
class Cron:
    """A parsed expression: its text, trimmed and single-spaced, and each field's values (a weekday as
    cron numbers it, Sunday 0)."""

    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]

    def on(self, day: date) -> bool:
        return day.month in self.months and day.day in self.days and day.isoweekday() % 7 in self.weekdays


def _field(text: str, name: str, low: int, high: int, names: tuple[str, ...], said: str) -> frozenset[int]:
    def number(value: str) -> int:
        if value.upper() in names:
            return names.index(value.upper()) + low
        if not value.isdigit():
            raise CronError(
                f"cron field {name}: {part!r} is not *, a number, a range a-b, a list a,b or a step */n or"
                " a-b/n"
            )
        if not low <= int(value) <= high:
            raise CronError(f"cron field {name}: {value!r} is outside {said}")
        return int(value)

    found: set[int] = set()
    for part in text.split(","):
        match = _PART.fullmatch(part)
        first, last, step = match.groups() if match else ("", None, None)
        ranged = first == "*" or last is not None
        if step is not None and not (ranged and step.isdigit()):
            first = ""  # a step of a single value, or one that is no number
        start, end = (low, high) if first == "*" and last is None else (number(first), number(last or first))
        if start > end:
            raise CronError(f"cron field {name}: range {part!r} runs backwards")
        if step is not None and int(step) < 1:
            raise CronError(f"cron field {name}: step {step!r} must be at least 1")
        found.update(range(start, end + 1, int(step or 1)))
    return frozenset(value % 7 for value in found) if names is WEEKDAYS else frozenset(found)


def parse(cron: str) -> Cron:
    """The expression parsed; CronError when it is none a schedule takes. Only its text, trimmed and
    single-spaced and at most 100 characters, is remembered."""
    text = " ".join(cron.split())
    if len(text) > 100:
        raise CronError("cron must be at most 100 characters")
    return _parse(text)


@functools.lru_cache(maxsize=512)
def _parse(text: str) -> Cron:
    fields = text.split()
    if len(fields) != 5:
        raise CronError(
            f"cron must have 5 fields (minute hour day-of-month month day-of-week), not {len(fields)}"
        )
    parsed = Cron(text, *(_field(field, *spec) for field, spec in zip(fields, _FIELDS, strict=True)))
    if fields[2] != "*" and fields[4] != "*":
        raise CronError(
            "cron sets both day-of-month and day-of-week: set one and leave the other * (cron dialects"
            " disagree on what both mean)"
        )
    days = [day for day in _LEAP_CYCLE if parsed.on(day)]
    if not days:
        raise CronError(f"cron names no time in the next {HORIZON_YEARS} years")
    times = sorted(hour * 60 + minute for hour in parsed.hours for minute in parsed.minutes)
    if any(b - a == timedelta(days=1) for a, b in itertools.pairwise(days)):  # two days in a row
        times.append(times[0] + 1440)
    for a, b in itertools.pairwise(times):
        if b - a < SPACING_MIN:
            at = " and ".join(f"{t % 1440 // 60:02d}:{t % 60:02d}" for t in (a, b))
            raise CronError(
                f"cron fires at {at}: a schedule fires at most every {SPACING_MIN} minutes (a soak's"
                " execution.cycleInterval repeats faster)"
            )
    return parsed


def tzdata() -> str:
    """The version of this build's tz data, from the first ``tzdata.zi`` on the zone path; ``unknown``
    without one."""
    for root in zoneinfo.TZPATH:
        try:
            with Path(root, "tzdata.zi").open(encoding="ascii", errors="replace") as file:
                return file.readline().removeprefix("# version").strip() or "unknown"
        except OSError:
            continue
    return "unknown"


@functools.cache
def zones() -> frozenset[str]:
    """``UTC`` and the region names this build's tz data holds: never ``localtime``, ``Factory``,
    ``posixrules`` or a bare abbreviation."""
    return frozenset({"UTC"} | {name for name in zoneinfo.available_timezones() if "/" in name})


def zone(name: str) -> tzinfo:
    """The zone ``name``; CronError when it is none a schedule takes."""
    if name == "UTC":
        return UTC
    if "/" not in name:
        raise CronError(
            f"time_zone must be UTC or an IANA region name such as Europe/London: {name!r} is not one"
        )
    if name in RENAMED:
        raise CronError(
            f"{name!r} is not a time zone this build uses (tzdata {tzdata()}): use {RENAMED[name]}"
        )
    if name not in zones():
        raise CronError(
            f"{name!r} is not a time zone this build knows (tzdata {tzdata()}): use an IANA region name such"
            " as Europe/London, or UTC"
        )
    return zoneinfo.ZoneInfo(name)


def _day(parsed: Cron, tz: tzinfo, day: date) -> list[datetime]:
    """The instants of the cron's times on the zone's ``day``, de-duplicated and sorted."""
    if not parsed.on(day):
        return []
    y, m, d = day.year, day.month, day.day
    hours, minutes = parsed.hours, parsed.minutes
    return sorted({datetime(y, m, d, h, n, tzinfo=tz).astimezone(UTC) for h in hours for n in minutes})


def _after(cron: str, tz: str, t: datetime) -> Iterator[datetime]:
    """The cron's instants after ``t``, in order: each within the horizon of the one before."""
    parsed, where, last, idle = parse(cron), zone(tz), t, 0
    day = t.astimezone(where).date() - timedelta(days=1)  # a skipped time of the day before falls later
    while idle < HORIZON_YEARS * 366 + 2:
        idle += 1
        for at in _day(parsed, where, day):
            if at > last:
                yield at
                last, idle = at, 0
        day += timedelta(days=1)


def after(cron: str, tz: str, t: datetime, until: datetime | None = None) -> datetime | None:
    """The cron's first instant after ``t``; None when it is later than ``until``."""
    at = next(_after(cron, tz, t), None)
    return None if at is None or (until is not None and at > until) else at


def latest(cron: str, tz: str, t: datetime) -> datetime | None:
    """The cron's last instant at or before ``t``; None when the horizon holds none."""
    parsed, where = parse(cron), zone(tz)
    day = t.astimezone(where).date() + timedelta(days=1)
    for _ in range(HORIZON_YEARS * 366 + 2):
        if found := [at for at in _day(parsed, where, day) if at <= t]:
            return found[-1]
        day -= timedelta(days=1)
    return None


def between(cron: str, tz: str, a: datetime, b: datetime) -> list[datetime]:
    """The cron's instants after ``a``, up to ``b``, in order."""
    return list(itertools.takewhile(lambda at: at <= b, _after(cron, tz, a)))


def upcoming(cron: str, tz: str, t: datetime, count: int = 5) -> list[datetime]:
    """The cron's next ``count`` instants after ``t``."""
    return list(itertools.islice(_after(cron, tz, t), count))


def utc(at: datetime) -> str:
    """An instant as schedules write one: ``2026-10-07T01:00Z``."""
    return f"{at.astimezone(UTC):%Y-%m-%dT%H:%MZ}"


def notes(cron: str, tz: str, t: datetime, days: int = 370) -> list[str]:
    """What the zone's clock changes do to the cron's times in the ``days`` after ``t``: each time skipped
    or repeated, ten at most."""
    parsed, where, found = parse(cron), zone(tz), []
    first = t.astimezone(where).date()
    for day in (first + timedelta(days=n) for n in range(days)):
        start = datetime(day.year, day.month, day.day, tzinfo=where)
        if not parsed.on(day) or start.utcoffset() == (start + timedelta(days=1)).utcoffset():
            continue  # no time of the cron, or the clock does not change that day
        for wall in sorted(start.replace(hour=h, minute=n) for h in parsed.hours for n in parsed.minutes):
            at = wall.astimezone(UTC)
            happens = at.astimezone(where)
            if at <= t:
                continue
            if happens.replace(tzinfo=None) != wall.replace(tzinfo=None):
                later = f"{happens:%H:%M}" if happens.date() == day else f"{happens:%Y-%m-%d %H:%M}"
                found.append(
                    f"{day}: {wall:%H:%M} does not exist in {tz} (clocks go forward): that firing happens at"
                    f" {later} ({at:%H:%M} UTC)"
                )
            elif wall.replace(fold=1).utcoffset() != wall.utcoffset():
                found.append(
                    f"{day}: {wall:%H:%M} happens twice in {tz} (clocks go back): the schedule fires at the"
                    f" first ({at:%H:%M} UTC)"
                )
            if len(found) == 10:
                return found
    return found
