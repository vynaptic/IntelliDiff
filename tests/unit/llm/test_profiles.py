"""Profiles YAML: trust derived from the hosts registry, credential rules, strict validation."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from deltasleuth.llm.model import DataClass, EgressDenied
from deltasleuth.llm.policy import EgressPolicy, check_egress
from deltasleuth.llm.profiles import (
    Budgets,
    Capabilities,
    Limits,
    LLMProfile,
    ProfileError,
    Trust,
    WireOptions,
    load_profiles,
    parse_profiles,
)

YAML = """\
hosts:
  models.internal.example: internal
  api.anthropic.com: external
profiles:
  gateway:
    dialect: openai_chat
    base_url: https://Models.Internal.Example/v1/
    model: gateway-model-3
    auth_ref: file:/var/run/secrets/models.key
    data_classes: [synthetic, production_tokenised]
    environments: [dev, prod]
    capabilities: {tool_calling: true, prompt_caching: automatic, max_context: 128000}
    budgets: {max_input_tokens_per_call: 15000, per_run_tokens: 1000000}
    limits: {timeout_s: 30, max_concurrency: 2, rps: 0.5}
    wire: {max_tokens_param: max_tokens, send_store: false}
  claude-test:
    dialect: anthropic_messages
    base_url: https://api.anthropic.com
    model: claude-opus-5
    auth_ref: file:/var/run/secrets/anthropic.key
    pricing: {version: a-1, currency: USD, input_per_mtok: 5, output_per_mtok: "25", cache_read_mult: 0.1}
  local-vllm:
    dialect: openai_chat
    base_url: http://gpu-box.example:8000/v1
    model: qwen-test
"""
PRICE = {"version": "v", "currency": "USD", "input_per_mtok": 1, "output_per_mtok": 1}


def _doc(**profile: Any) -> dict[str, Any]:
    base = {"dialect": "openai_chat", "base_url": "https://llm.example.com/v1", "model": "m"}
    return {"hosts": {"llm.example.com": "internal"}, "profiles": {"prof": base | profile}}


def _profile(**fields: Any) -> LLMProfile:
    return parse_profiles(_doc(**fields)).get("prof")


def _pointer(doc: Any) -> str:
    with pytest.raises(ProfileError) as info:
        parse_profiles(doc)
    return info.value.pointer


def test_loads_a_profiles_file(tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    path.write_text(YAML)
    registry = load_profiles(path)
    gateway, claude, vllm = (registry.get(p) for p in ("gateway", "claude-test", "local-vllm"))
    assert (gateway.host, gateway.base_url) == (
        "models.internal.example",
        "https://Models.Internal.Example/v1",
    )
    # trust comes from the hosts registry; an unlisted host is external
    assert [p.trust for p in (gateway, claude, vllm)] == [Trust.INTERNAL, Trust.EXTERNAL, Trust.EXTERNAL]
    assert gateway.data_classes == {"synthetic", "production_tokenised"}
    assert gateway.environments == {"dev", "prod"}
    assert gateway.capabilities == Capabilities(
        tool_calling=True, prompt_caching="automatic", max_context=128_000
    )
    assert gateway.limits == Limits(timeout_s=30.0, max_concurrency=2, rps=0.5)
    assert gateway.budgets == Budgets(max_input_tokens_per_call=15_000, per_run_tokens=1_000_000)
    assert gateway.wire == WireOptions(max_tokens_param="max_tokens", send_store=False)
    price = claude.pricing
    assert price is not None
    assert (price.output_per_mtok, price.cache_read_mult, price.cache_write_mult) == (25, Decimal("0.1"), 1)
    # nothing declared: nothing used
    assert (vllm.capabilities, vllm.budgets) == (Capabilities(), Budgets())
    assert (vllm.pricing, vllm.data_classes) == (None, set())
    # the credential header defaults per dialect
    assert (gateway.auth, claude.auth) == (("Authorization", "Bearer"), ("x-api-key", None))
    with pytest.raises(ProfileError, match="no such profile"):
        registry.get("nope")


def test_custom_auth_header_and_scheme() -> None:
    assert _profile(auth_ref="file:/k", auth_header="api-key").auth == ("api-key", None)
    assert _profile(auth_ref="file:/k", auth_scheme="Token").auth == ("Authorization", "Token")


@pytest.mark.parametrize(
    ("base_url", "loopback"),
    [
        ("http://llm.example.com/v1", False),  # a credential is never sent in cleartext
        ("http://127.0.0.1:9/v1", True),  # loopback may opt out explicitly
        ("http://localhost:9/v1", True),
        ("http://[::1]:9/v1", True),
    ],
)
def test_https_with_credentials(base_url: str, loopback: bool) -> None:
    assert _profile(base_url=base_url).auth_ref is None  # no credential, no rule
    assert _pointer(_doc(base_url=base_url, auth_ref="file:/k")) == "/profiles/prof/base_url"
    opted_out = _doc(base_url=base_url, auth_ref="file:/k", allow_insecure_localhost=True)
    if loopback:
        assert parse_profiles(opted_out).get("prof").allow_insecure_localhost
    else:
        assert _pointer(opted_out) == "/profiles/prof/allow_insecure_localhost"


def test_send_store_false_only_for_an_internal_host() -> None:
    assert _profile(wire={"send_store": False}).wire.send_store is False
    for hosts in ({}, {"llm.example.com": "external"}):
        doc = _doc(wire={"send_store": False}) | {"hosts": hosts}
        assert _pointer(doc) == "/profiles/prof/wire/send_store"
    assert parse_profiles(_doc(wire={"max_tokens_param": "max_tokens"}) | {"hosts": {}})  # any host


@pytest.mark.parametrize(
    ("fields", "where"),
    [
        ({"trust": "internal"}, "trust"),  # derived, never declared
        ({"dialect": "openai_responses"}, "dialect"),
        ({"fallbacks": ["other"]}, "fallbacks"),
        ({"base_url": "ftp://llm.example.com"}, "base_url"),
        ({"base_url": "https://user:pw@llm.example.com"}, "base_url"),
        ({"base_url": "https://llm.example.com/v1?api_key=x"}, "base_url"),
        ({"base_url": "https://llm.example.com/v1#frag"}, "base_url"),
        ({"base_url": "https://llm.example.com:99999"}, "base_url"),
        ({"base_url": "https:///v1"}, "base_url"),
        ({"model": "has space"}, "model"),
        ({"auth_ref": "env:OPENAI_API_KEY"}, "auth_ref"),
        ({"auth_header": "Authorization"}, "auth_ref"),  # a header needs a credential
        ({"auth_ref": "file:/k", "auth_header": "Bad Header"}, "auth_header"),
        ({"auth_ref": "file:/k", "auth_scheme": "Bearer token"}, "auth_scheme"),
        ({"data_classes": ["raw_pii"]}, "data_classes/0"),
        ({"data_classes": "synthetic"}, "data_classes"),
        ({"environments": ["qa"]}, "environments/0"),
        ({"capabilities": {"telepathy": True}}, "capabilities/telepathy"),
        ({"capabilities": {"tool_calling": "yes"}}, "capabilities/tool_calling"),
        ({"capabilities": {"max_context": 0}}, "capabilities/max_context"),
        ({"budgets": {"max_tool_calls": True}}, "budgets/max_tool_calls"),
        ({"budgets": {"max_wall_s": -1}}, "budgets/max_wall_s"),
        ({"limits": {"timeout_s": float("inf")}}, "limits/timeout_s"),
        ({"limits": {"max_concurrency": 1.5}}, "limits/max_concurrency"),
        ({"pricing": {"version": "v"}}, "pricing/currency"),
        ({"pricing": PRICE | {"version": ""}}, "pricing/version"),
        ({"pricing": PRICE | {"currency": "usd"}}, "pricing/currency"),
        ({"pricing": PRICE | {"input_per_mtok": -1}}, "pricing/input_per_mtok"),
        ({"pricing": PRICE | {"input_per_mtok": "NaN"}}, "pricing/input_per_mtok"),
        ({"pricing": PRICE | {"input_per_mtok": True}}, "pricing/input_per_mtok"),
        ({"pricing": PRICE | {"tax": 1}}, "pricing/tax"),
        ({"wire": {"max_tokens_param": "max_output_tokens"}}, "wire/max_tokens_param"),
        ({"wire": {"send_store": "no"}}, "wire/send_store"),
        ({"dialect": "anthropic_messages", "wire": {"max_tokens_param": "max_tokens"}}, "wire"),
    ],
)
def test_invalid_fields(fields: dict[str, Any], where: str) -> None:
    assert _pointer(_doc(**fields)) == f"/profiles/prof/{where}"


@pytest.mark.parametrize(
    "doc",
    [
        {"profiles": {"Upper": {}}},
        {"hosts": {"https://llm.example.com": "internal"}},
        {"hosts": {"LLM.example.com": "internal"}},
        {"hosts": {"llm.example.com:443": "internal"}},
        {"hosts": {"llm.example.com": "trusted"}},
        {"targets": {}},
    ],
)
def test_invalid_document(doc: dict[str, Any]) -> None:
    assert _pointer(doc).startswith(f"/{next(iter(doc))}")
    assert parse_profiles(None).profiles == {}


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("hosts: {}\nhosts: {}\n", r"^/hosts: duplicate key 'hosts' \(line 2"),
        ("hosts: [unclosed\n", "expected ',' or ']'"),
        ("hosts: !!python/object/apply:os.system ['true']\n", "is not JSON data"),
        # YAML 1.2, as specs are read: "yes" is text, not a boolean
        ("profiles: {p1: {dialect: openai_chat, base_url: 'http://127.0.0.1:1/v1', model: m,\n"
         "  allow_insecure_localhost: yes}}\n", "allow_insecure_localhost: Input should be a valid boolean"),
    ],
)  # fmt: skip
def test_strict_yaml(tmp_path: Path, text: str, match: str) -> None:
    """Profiles are read by the one document reader specs and targets use."""
    path = tmp_path / "p.yaml"
    path.write_text(text)
    with pytest.raises(ProfileError, match=match):
        load_profiles(path)
    with pytest.raises(ProfileError, match="cannot read"):
        load_profiles(tmp_path / "absent.yaml")


def _example(repo_root: Path, tmp_path: Path) -> Any:
    """The shipped example profiles, with the commented optional lines enabled."""
    text = (repo_root / "deploy" / "config" / "llm_profiles.example.yaml").read_text()
    (tmp_path / "llm.yaml").write_text(text.replace("    # auth_", "    auth_"))
    return load_profiles(tmp_path / "llm.yaml")


def test_only_the_example_gateway_profile_may_serve_the_chat(repo_root: Path, tmp_path: Path) -> None:
    chat = frozenset({DataClass.PRODUCTION_TOKENISED})  # what every chat call carries

    def serves(profile: LLMProfile) -> bool:
        try:
            check_egress(profile, chat, EgressPolicy("prod"))
        except EgressDenied:
            return False
        return True

    profiles = _example(repo_root, tmp_path).profiles
    assert [pid for pid, profile in profiles.items() if serves(profile)] == ["gateway"]


def test_the_example_documents_a_gateway_auth_header(repo_root: Path, tmp_path: Path) -> None:
    assert _example(repo_root, tmp_path).get("gateway").auth == ("api-key", "Bearer")
