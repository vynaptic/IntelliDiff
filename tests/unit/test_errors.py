"""Refusals: which messages are the package's own words, and what is told of any other exception."""

from __future__ import annotations

from decimal import Decimal

import pytest

from deltasleuth import errors
from deltasleuth.core.jsonio import parse_json


def _raised(make: object) -> BaseException:
    try:
        make()  # type: ignore[operator]
    except BaseException as exc:  # the test keeps what any call raises
        return exc
    raise AssertionError("nothing raised")


@pytest.mark.parametrize(
    ("make", "told"),
    [
        (lambda: (_ for _ in ()).throw(errors.Invalid("the key set is empty")), "the key set is empty"),
        (lambda: int("MBR-0045123"), "ValueError"),
        (lambda: Decimal("MBR-0045123"), "InvalidOperation"),
        (lambda: {"K": 1}["MBR-0045123"], "KeyError"),
    ],
)
def test_told_is_the_message_of_a_refusal_and_the_type_of_anything_else(make: object, told: str) -> None:
    exc = _raised(make)
    assert errors.told(exc) == told
    assert "MBR-0045123" not in errors.told(exc)


def test_a_document_that_does_not_parse_is_told_without_its_text() -> None:
    exc = _raised(lambda: parse_json(b'{"id": MBR-0045123}'))
    assert "MBR-0045123" not in errors.told(exc)
