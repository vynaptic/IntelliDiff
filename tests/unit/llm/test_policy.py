"""Fail-closed egress: data classes x trust x environment, enforced before any network I/O."""

from __future__ import annotations

from typing import Any

import pytest

from deltasleuth.llm.model import DataClass, EgressDenied, LLMRequest, Message, Overloaded
from deltasleuth.llm.policy import EXTERNAL_BASELINE, EgressPolicy, check_egress
from deltasleuth.llm.profiles import LLMProfile, parse_profiles

SYN = frozenset({DataClass.SYNTHETIC})
TOK = frozenset({DataClass.PRODUCTION_TOKENISED})
AGG = frozenset({DataClass.AGGREGATES})


def _profile(
    host: str, *, classes: tuple[str, ...] = tuple(DataClass), envs: tuple[str, ...] | None = None
) -> LLMProfile:
    profile = {
        "dialect": "openai_chat",
        "base_url": f"https://{host}/v1",
        "model": "m",
        "data_classes": list(classes),
        "environments": list(envs or ("dev", "test", "eval", "staging", "prod")),
    }
    hosts = {"models.internal.example": "internal", "api.anthropic.com": "external"}
    return parse_profiles({"hosts": hosts, "profiles": {"prof": profile}}).get("prof")


INTERNAL = _profile("models.internal.example")
EXTERNAL = _profile("api.anthropic.com")
UNLISTED = _profile("new-gateway.example.org")


def _reason(
    profile: LLMProfile, classes: frozenset[DataClass], env: str = "dev", **policy: Any
) -> str | None:
    try:
        check_egress(profile, classes, EgressPolicy(env, **policy))
    except EgressDenied as exc:
        assert exc.error_class == "PolicyDenied" and profile.profile_id in str(exc)
        return exc.reason
    return None


@pytest.mark.parametrize("cls", sorted(EXTERNAL_BASELINE))
def test_external_baseline_in_dev(cls: DataClass) -> None:
    assert _reason(EXTERNAL, frozenset({cls})) is None


def test_external_never_gets_production_data_by_default() -> None:
    assert _reason(EXTERNAL, TOK) == "external_class"
    assert _reason(UNLISTED, TOK) == "external_class"  # an unlisted host is external
    assert _reason(INTERNAL, TOK | AGG, "prod") is None


def test_external_is_confined_to_dev_test_eval() -> None:
    for env in ("staging", "prod"):
        assert _reason(EXTERNAL, SYN, env) == "external_environment"
        assert _reason(EXTERNAL, SYN, env, external_extra_classes={"aggregates"}) == "external_environment"


def test_extra_class_needs_profile_listing_and_environment_approval() -> None:
    assert _reason(EXTERNAL, AGG) == "external_class"  # listed, not approved
    assert _reason(EXTERNAL, AGG, external_extra_classes={"aggregates"}) is None
    narrow = _profile("api.anthropic.com", classes=("synthetic",))
    assert _reason(narrow, AGG, external_extra_classes={"aggregates"}) == "class_not_allowed"  # approved only


def test_profile_lists_bound_every_profile() -> None:
    assert (
        _reason(_profile("models.internal.example", envs=("dev",)), SYN, "prod") == "environment_not_allowed"
    )
    assert (
        _reason(_profile("models.internal.example", classes=("synthetic",)), SYN | TOK) == "class_not_allowed"
    )
    assert _reason(INTERNAL, frozenset()) == "no_data_classes"  # undeclared data is denied


def test_policy_is_validated() -> None:
    with pytest.raises(ValueError, match="environment"):
        EgressPolicy("qa")
    with pytest.raises(ValueError):
        EgressPolicy("dev", external_extra_classes={"raw"})  # type: ignore[arg-type]


async def test_denied_call_never_reaches_the_network(start_mock: Any, make_client: Any) -> None:
    mock = await start_mock("openai")
    client = make_client(mock, hosts={})  # loopback unlisted: external
    with pytest.raises(EgressDenied, match="external_class"):
        await client.complete(LLMRequest(messages=(Message.user("secret finding"),), data_classes=TOK))
    assert mock.requests == []
    assert client.counters.denied == 1
    row = client.call_log[-1]
    assert (row["error_class"], row["denial_reason"]) == ("PolicyDenied", "external_class")
    assert row["trust"] == "external"


async def test_no_failover_when_the_internal_endpoint_is_overloaded(
    start_mock: Any, make_client: Any
) -> None:
    internal, external = await start_mock("openai"), await start_mock("anthropic")
    for _ in range(3):
        internal.enqueue(529, {"error": {"message": "overloaded", "type": "overloaded", "code": None}})
    with pytest.raises(Overloaded):
        await make_client(internal, environment="prod").complete(
            LLMRequest(messages=(Message.user("hi"),), data_classes=TOK)
        )
    assert (len(internal.requests), external.requests) == (3, [])
