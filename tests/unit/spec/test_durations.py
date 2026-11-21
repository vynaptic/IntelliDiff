from __future__ import annotations

import pytest

from deltasleuth.core.spec import DurationError, parse_duration


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("250ms", 0.25), ("0ms", 0), ("1.5s", 1.5), ("5m", 300), ("1.5m", 90), ("2h", 7200), ("1d", 86400)],
)
def test_parse_duration(text: str, seconds: float) -> None:
    assert parse_duration(text) == seconds


def test_units_scale_exactly() -> None:
    assert parse_duration("100ms") == parse_duration("0.1s") == 0.1


@pytest.mark.parametrize(
    "text", ["", "5", "ms", "5 s", " 5s", "-1s", "1.s", ".5s", "5sec", "5M", "1e3ms", "1,5s", 5, None, b"5s"]
)
def test_rejects_anything_else(text: object) -> None:
    with pytest.raises(DurationError):
        parse_duration(text)  # type: ignore[arg-type]
    assert issubclass(DurationError, ValueError)
