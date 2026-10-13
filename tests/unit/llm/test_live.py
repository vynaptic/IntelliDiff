"""Optional live probe against the Claude API (marked ``live_llm``; deselected by default).

Run with ``DS_LIVE_LLM_KEY_FILE=/path/to/key .venv/bin/python -m pytest -m live_llm tests/unit/llm``.
Only the key file's path comes from the environment; the key is read through the profile's
``file:`` reference like any other credential. The probe sends synthetic prompts only.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from deltasleuth.llm.client import LLMClient
from deltasleuth.llm.policy import EgressPolicy
from deltasleuth.llm.probe import probe_capabilities
from deltasleuth.llm.profiles import Trust, parse_profiles

from .conftest import profile_doc


@pytest.mark.live_llm
async def test_live_claude_probe() -> None:
    key_file = os.environ.get("DS_LIVE_LLM_KEY_FILE")
    if not key_file:
        pytest.skip("DS_LIVE_LLM_KEY_FILE is not set")
    doc = profile_doc(
        "anthropic_messages",
        "https://api.anthropic.com",
        hosts={"api.anthropic.com": "external"},
        auth_ref=f"file:{Path(key_file).resolve()}",
        data_classes=["synthetic", "golden"],
        environments=["dev"],
        budgets={"max_input_tokens_per_call": 20000, "per_run_tokens": 200000},
    )
    profile = parse_profiles(doc).get("test-profile")
    assert profile.trust is Trust.EXTERNAL
    async with LLMClient.from_profile(profile, egress=EgressPolicy("dev")) as client:
        report = await probe_capabilities(client)
    print(report.to_json())  # the report is the point of a manual live run
    assert {r.name: r.status for r in report.results}["tool_roundtrip"] == "pass"
    assert report.model_reported
