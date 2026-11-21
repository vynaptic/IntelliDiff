"""Shared adapter plumbing: Retry-After, SDK neutralisation, header-safe credentials."""

from __future__ import annotations

import email.utils
import time
from typing import Any

import anthropic
import httpx2
import pytest

from deltasleuth.core.secrets import SecretError
from deltasleuth.llm.adapters.base import neutralise_sdk_environment, parse_retry_after, sdk_error
from deltasleuth.llm.client import LLMClient
from deltasleuth.llm.model import BadRequest
from deltasleuth.llm.policy import EgressPolicy


def test_retry_after_forms() -> None:
    assert parse_retry_after({"retry-after-ms": "250"}) == 0.25
    assert parse_retry_after({"retry-after": "1.5"}) == 1.5
    assert parse_retry_after({"retry-after": "-3"}) == 0.0
    assert parse_retry_after({"retry-after-ms": "soon", "retry-after": "2"}) == 2.0  # garbage falls through
    assert parse_retry_after({"retry-after-ms": "nan", "retry-after": "2"}) == 2.0  # so does nan
    assert parse_retry_after({"retry-after": "whenever"}) is None
    assert parse_retry_after({}) is None
    future = email.utils.formatdate(time.time() + 30, usegmt=True)
    assert 25 <= parse_retry_after({"retry-after": future}) <= 31  # type: ignore[operator]
    assert parse_retry_after({"retry-after": email.utils.formatdate(time.time() - 30, usegmt=True)}) == 0.0


def test_neutralise_fails_closed_if_the_sdk_changes_shape() -> None:
    with pytest.raises(RuntimeError, match="credential hygiene"):
        neutralise_sdk_environment(object(), logger_name="x")


@pytest.mark.parametrize(
    "secret", ["﻿sk-Q7zX", "sk-Q7zX ", "\tsk-Q7zX", "sk-Q7zX\x00", "sk Q7zX", "sk-Q7zXé", ""]
)
async def test_credential_must_be_header_safe(secret: str, start_mock: Any, make_profile: Any) -> None:
    """Any secret reader (a vault, not only read_secret) gets a typed error before any I/O,
    never an untyped encoding error or a 'connection failed' after retries."""
    mock = await start_mock("openai")
    profile = make_profile("openai_chat", mock.base_url)
    with pytest.raises(SecretError, match="Authorization") as info:
        LLMClient.from_profile(profile, secret_reader=lambda _: secret, egress=EgressPolicy("dev"))
    assert "Q7zX" not in str(info.value) and mock.requests == []


@pytest.mark.parametrize("body", [None, "text", {"error": "flat"}, {"error": {"message": 5}}])
def test_odd_error_bodies_are_plain_bad_requests(body: Any) -> None:
    exc = anthropic.BadRequestError(
        "x", response=httpx2.Response(400, request=httpx2.Request("POST", "http://h")), body=body
    )
    assert type(sdk_error(exc, anthropic, code=None, context_overflow=False)) is BadRequest
