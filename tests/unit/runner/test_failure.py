"""What a failure records: its type and where in this package it was raised, and never its message, which
may hold a key or a value."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import pytest

from deltasleuth.connectors import tables
from deltasleuth.core.spec import parse_duration
from deltasleuth.runner import cycle
from deltasleuth.runner.recheck import RecheckPolicy
from deltasleuth.store import repo

VALUE = "MBR-0045123"  # what a message may hold: an invented member code
# Where a failure was raised, as a run's error or a log line names it after its type.
AT = r" at [\w/]+\.py:\d+ in [^,;]+(?:, from [\w/]+\.py:\d+ in [^,;]+)*"


def raised(call: Any, *args: Any) -> BaseException:
    """What ``call(*args)`` raises, its traceback with it."""
    with pytest.raises(Exception) as caught:  # any exception: the test names its type
        call(*args)
    return caught.value


def test_a_recorded_failure_names_its_type_and_its_frame_never_its_message() -> None:
    exc = raised(parse_duration, VALUE)
    assert VALUE in str(exc)  # the message holds the value
    where = r"core/spec/durations\.py:\d+ in parse_duration"
    assert re.fullmatch(where, cycle.where(exc))
    for said in (cycle.located(exc), cycle.recorded(exc)):  # a log line's words, and a failed run's error
        assert re.fullmatch(f"DurationError at {where}", said) and VALUE not in said
        assert re.fullmatch("DurationError" + AT, said)
    assert cycle.failure(exc) == "DurationError"  # the audit row's reason, which the notifier sends


def test_the_innermost_frames_of_the_package_come_first_three_at_most() -> None:
    exc = raised(RecheckPolicy.from_spec, {"recheck": {"delay": VALUE, "times": 1}})
    assert re.fullmatch(
        r"DurationError at core/spec/durations\.py:\d+ in parse_duration,"
        r" from runner/recheck\.py:\d+ in from_spec",
        cycle.recorded(exc),
    )

    def deep(n: int) -> None:  # this file's frames are not the package's: none of them is named
        if n:
            return deep(n - 1)
        asyncio.run(repo.pages(fetch))

    async def fetch(*args: Any, **kwargs: Any) -> Any:
        raise KeyError(VALUE)

    assert re.fullmatch(r"KeyError at store/repo\.py:\d+ in pages", cycle.recorded(raised(deep, 5)))
    assert cycle.FRAMES == 3 and len(cycle.where(_nested(5)).split(", from ")) == 3


def _nested(depth: int) -> BaseException:
    """An exception that passed ``depth`` frames of the package: a spec's durations, parsed inside
    ``depth`` awaits of ``repo.pages``."""

    async def fetch(*args: Any, level: int = depth - 2, **kwargs: Any) -> Any:
        if level:
            return await repo.pages(fetch, level=level - 1)
        return RecheckPolicy.from_spec({"recheck": {"delay": VALUE, "times": 1}})

    return raised(lambda: asyncio.run(fetch()))


def test_an_exception_that_passed_no_frame_of_the_package_is_its_type_alone() -> None:
    assert (cycle.where(KeyError(VALUE)), cycle.recorded(KeyError(VALUE))) == ("", "KeyError")  # not raised
    exc = raised(lambda: {}[VALUE])  # raised in this file
    assert (cycle.where(exc), cycle.located(exc), cycle.recorded(exc)) == ("", "KeyError", "KeyError")


def test_a_table_error_keeps_its_class_and_code_and_a_log_line_adds_where() -> None:
    """A table side's error says what to put right, as the guard's text and a secret's file do: a failed run
    shows it as it did. Its log line names the frame as well."""
    target = SimpleNamespace(engine="postgresql", target_id="db-b")
    exc = raised(tables.planned, "b", {"engine": "mysql"}, target)
    told = "EngineChanged: side b: target db-b is postgresql now; this run was planned for mysql"
    assert (cycle.failure(exc), cycle.recorded(exc)) == (told, told)
    at = r" at connectors/tables/engine\.py:\d+ in planned"
    assert re.fullmatch(re.escape(told) + at, cycle.located(exc))


def test_keys_that_mask_alike_are_shown_by_their_count() -> None:
    """Its message counts the keys and names none, so a failed run says what to put right."""
    from deltasleuth.security.crypto import MaskedAlike

    assert cycle.failure(MaskedAlike(2)) == f"MaskedAlike: {MaskedAlike(2)}"
