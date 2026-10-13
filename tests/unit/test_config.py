"""Settings are read like specs and targets: a key written twice is an error, never last-wins."""

from __future__ import annotations

import dataclasses
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from deltasleuth import config
from deltasleuth.config import SettingsError, load_settings
from deltasleuth.core import readiness
from deltasleuth.core.secrets import read_secret
from deltasleuth.runner.worker import _Worker


def test_duplicate_settings_key_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "ds.yaml").write_text(
        "database_dsn_ref: file:dsn\nevidence_dir: e\nworkspace_dir: w\ntargets_file: t.yaml\n"
        "llm_profile: gateway\nllm_profile: openai\n"
    )
    with pytest.raises(SettingsError, match=r"duplicate key 'llm_profile' \(line 6"):
        load_settings(tmp_path / "ds.yaml")


BASE = "database_dsn_ref: file:dsn\nevidence_dir: e\nworkspace_dir: w\ntargets_file: t.yaml\n"
OIDC = {
    "issuer": "https://idp.example",
    "client_id": "deltasleuth",
    "client_secret_ref": "file:/run/secrets/oidc_client_secret",
    "roles": {"admin": ["DS.Admin"]},
}
ENCRYPTION = {"kek_ref": "file:/run/secrets/kek", "tokeniser_key_ref": "file:/run/secrets/tokeniser"}


def _load(tmp_path: Path, **doc: Any) -> Any:
    (tmp_path / "ds.yaml").write_text(BASE + (yaml.safe_dump(doc) if doc else ""))
    return load_settings(tmp_path / "ds.yaml")


def _refusal(tmp_path: Path, **doc: Any) -> str:
    with pytest.raises(SettingsError) as refused:
        _load(tmp_path, **doc)
    return str(refused.value).split(": ", 1)[1]  # after "invalid settings <path>"


def test_api_token_ref_is_replaced_by_named_tokens(tmp_path: Path) -> None:
    old = {"api_token_ref": "file:/run/secrets/api_token", "workers": 1}  # checked before validation
    refusal = _refusal(tmp_path, **old)
    assert refusal == (
        "api_token_ref: replaced by named API tokens; remove it and create tokens with 'ds token create'"
    )


def test_a_full_auth_block_loads(tmp_path: Path) -> None:
    (tmp_path / "client.crt").write_text("")
    oidc = OIDC | {
        "client_auth": "private_key_jwt",
        "client_secret_ref": None,
        "client_key_ref": "file:/run/secrets/oidc_client_key",
        "client_key_id": "deltasleuth-2026",
        "client_cert_file": "client.crt",
        "scopes": ["openid", "profile", "allatclaims"],
        "subject_claim": "oid",
        "username_claim": "preferred_username",
        "group_claims": ["roles", ["resource_access", "deltasleuth.bank", "roles"]],
        "id_token_algs": ["RS256", "PS256", "ES256"],
        "clock_skew_seconds": 0,
        "end_session": False,
        "proxy": "http://proxy.bank.example:8080",
        "roles": {"viewer": ["DS.Viewer"], "admin": ["DS.Admin", "0f3c-group-id"]},
    }
    auth = {
        "public_url": "https://deltasleuth.bank.example/",
        "allowed_hosts": ["api.deltasleuth.bank.example"],
        "trusted_proxies": ["172.30.0.0/24", "10.20.0.5"],
        "session_idle_minutes": 5,
        "session_max_hours": 24,
        "token_max_days": 366,
        "oidc": {name: value for name, value in oidc.items() if value is not None},
    }
    settings = _load(tmp_path, auth=auth, encryption=ENCRYPTION)
    loaded = settings.auth
    assert (settings.auth_mode, loaded.public_url) == ("oidc", "https://deltasleuth.bank.example")
    assert loaded.trusted_proxies == ("172.30.0.0/24", "10.20.0.5/32")
    assert loaded.oidc.group_claims == ("roles", ("resource_access", "deltasleuth.bank", "roles"))
    assert loaded.oidc.client_cert_file == tmp_path / "client.crt"
    assert loaded.oidc.roles == {"viewer": ("DS.Viewer",), "admin": ("DS.Admin", "0f3c-group-id")}


def test_auth_defaults_and_modes(tmp_path: Path) -> None:
    assert _load(tmp_path).auth_mode == "none"
    tokens = _load(tmp_path, auth={"public_url": "http://localhost:8000"}, encryption=ENCRYPTION)
    assert (tokens.auth_mode, tokens.auth.session_idle_minutes, tokens.auth.session_max_hours) == (
        "tokens",
        30,
        8,
    )
    local = {"public_url": "http://[::1]:8000", "oidc": OIDC | {"issuer": "http://127.0.0.1:9000"}}
    loopback = _load(tmp_path, auth=local, encryption=ENCRYPTION)
    assert loopback.auth.oidc.issuer == "http://127.0.0.1:9000"  # a loopback IdP
    sso = {"public_url": "https://ds.example", "oidc": OIDC}
    oidc = _load(tmp_path, auth=sso, encryption=ENCRYPTION).auth.oidc
    assert (oidc.client_auth, oidc.scopes, oidc.subject_claim, oidc.group_claims, oidc.id_token_algs) == (
        "client_secret_basic",
        ("openid", "profile"),
        "sub",
        ("groups",),
        ("RS256",),
    )


PUBLIC_URL = (
    "auth.public_url: must be https://host[:port] (http only for localhost, 127.0.0.1 or [::1]), without a"
    " path, query or fragment"
)


@pytest.mark.parametrize(
    "public_url",
    ["http://ds.example", "https://ds.example/deltasleuth", "https://ds.example?a=1", "https://ds.example#top",
     "https://user:pw@ds.example", "ftp://ds.example", "https://ds.example:99999", "ds.example"],
)  # fmt: skip
def test_a_public_url_is_an_origin(tmp_path: Path, public_url: str) -> None:
    # http beyond a loopback host is refused: the plain session cookies of an http public_url rest on it.
    assert _refusal(tmp_path, auth={"public_url": public_url}) == PUBLIC_URL


def _oidc(**fields: Any) -> dict[str, Any]:
    oidc = OIDC | fields
    return {"public_url": "https://ds.example", "oidc": {k: v for k, v in oidc.items() if v is not None}}


CROSS = "auth.oidc: Value error, "  # pydantic's prefix of a cross-field message
GROUP = "must be a non-empty group id without surrounding whitespace"
GROUP_PATH = "must be a claim name or a list of non-empty claim names"
PROXY = "auth.oidc.proxy: must be http(s)://host:port without credentials"
ALGORITHMS = "'RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512', 'ES256', 'ES384' or 'ES512'"


@pytest.mark.parametrize(
    ("auth", "refusal"),
    [
        ({"public_url": "http://127.0.0.1:8000", "allowed_hosts": ["ds.example"]},
         "auth.allowed_hosts: needs an https public_url"),
        (_oidc(issuer="http://idp.example"),
         "auth.oidc.issuer: must be https (http only for a loopback host)"),
        (_oidc(client_secret_ref=None), CROSS + "client_auth client_secret_basic needs client_secret_ref"),
        (_oidc(client_auth="client_secret_post", client_secret_ref=None),
         CROSS + "client_auth client_secret_post needs client_secret_ref"),
        (_oidc(client_auth="private_key_jwt", client_secret_ref=None),
         CROSS + "client_auth private_key_jwt needs client_key_ref"),
        (_oidc(client_auth="private_key_jwt", client_key_ref="file:k"),
         CROSS + "client_secret_ref is only for client_secret_basic and client_secret_post"),
        (_oidc(client_key_id="k1"),
         CROSS + "client_key_ref, client_key_id and client_cert_file are only for private_key_jwt"),
        (_oidc(scopes=["profile"]), "auth.oidc.scopes: must include openid"),
        (_oidc(roles={}), "auth.oidc.roles: map at least one IdP group to a role"),
        (_oidc(roles={"admin": []}), "auth.oidc.roles: map at least one IdP group to a role"),
        (_oidc(roles={"admin": [""]}), "auth.oidc.roles.admin.0: " + GROUP),
        (_oidc(roles={"viewer": ["DS.Viewer", "DS.Admin "]}), "auth.oidc.roles.viewer.1: " + GROUP),
        (_oidc(group_claims=["roles", ["resource_access", ""]]), "auth.oidc.group_claims.1: " + GROUP_PATH),
        (_oidc(group_claims=[{"path": "roles"}]), "auth.oidc.group_claims.0: " + GROUP_PATH),
        (_oidc(id_token_algs=["none"]), "auth.oidc.id_token_algs.0: Input should be " + ALGORITHMS),
        (_oidc(id_token_algs=["HS256"]), "auth.oidc.id_token_algs.0: Input should be " + ALGORITHMS),
        (_oidc(proxy="http://user:pw@proxy.example:8080"), PROXY),
        (_oidc(proxy="http://proxy.example"), PROXY),
    ],
)  # fmt: skip
def test_auth_settings_are_refused_naming_the_field_to_fix(
    tmp_path: Path, auth: dict[str, Any], refusal: str
) -> None:
    assert _refusal(tmp_path, auth=auth) == refusal


# -- encryption and retention ---------------------------------------------


def test_encryption_and_retention_settings_load(tmp_path: Path) -> None:
    plain = _load(tmp_path)
    assert (plain.encryption, plain.retention.standard_days, plain.retention.audit_days) == (None, 90, 365)
    settings = _load(
        tmp_path,
        auth={"public_url": "https://ds.example"},
        encryption=ENCRYPTION,
        retention={"standard_days": 7, "audit_days": 7},
    )
    assert (settings.encryption.kek_ref, settings.encryption.tokeniser_key_ref) == (
        "file:/run/secrets/kek",
        "file:/run/secrets/tokeniser",
    )
    assert (settings.retention.standard_days, settings.retention.audit_days) == (7, 7)


def test_precheck_settings_default_and_load(tmp_path: Path) -> None:
    """All defaulted, none a secret."""
    plain = _load(tmp_path).precheck
    assert (plain.sessions_per_target, plain.statement_timeout_s, plain.distinct_max_rows) == (
        1,
        900,
        20_000_000,
    )
    limits = {"sessions_per_target": 4, "statement_timeout_s": 86400, "distinct_max_rows": 0}
    assert dataclasses.asdict(_load(tmp_path, precheck=limits).precheck) == limits


@pytest.mark.parametrize(
    ("setting", "value", "refusal"),
    [
        ("sessions_per_target", 0, "Input should be greater than or equal to 1"),
        ("sessions_per_target", 5, "Input should be less than or equal to 4"),
        ("statement_timeout_s", 59, "Input should be greater than or equal to 60"),
        ("statement_timeout_s", 86401, "Input should be less than or equal to 86400"),
        ("distinct_max_rows", -1, "Input should be greater than or equal to 0"),
        ("distinct_max_rows", 10**12 + 1, "Input should be less than or equal to 1000000000000"),
        ("sessions", 1, "Unexpected keyword argument"),
    ],
)
def test_precheck_settings_are_refused_outside_their_ranges(
    tmp_path: Path, setting: str, value: int, refusal: str
) -> None:
    assert _refusal(tmp_path, precheck={setting: value}).startswith(f"precheck.{setting}: {refusal}")


INSIDE = "must not be inside evidence_dir or workspace_dir, whose backups would carry it"


@pytest.mark.parametrize(
    ("doc", "refusal"),
    [
        ({"auth": {"public_url": "https://ds.example"}}, "encryption: required with auth"),
        ({"retention": {"standard_days": 100, "audit_days": 90}},
         "retention: Value error, audit_days must be at least standard_days"),
        ({"retention": {"standard_days": 6}},
         "retention.standard_days: Input should be greater than or equal to 7"),
        ({"retention": {"audit_days": 3651}},
         "retention.audit_days: Input should be less than or equal to 3650"),
        ({"retention": {"days": 30}}, "retention.days: Unexpected keyword argument"),
        ({"encryption": ENCRYPTION | {"kek_ref": "/run/secrets/kek"}},
         "encryption.kek_ref: String should match pattern '^file:.+'"),
        ({"encryption": {"kek_ref": "file:/run/secrets/kek"}},
         "encryption.tokeniser_key_ref: Field required"),
    ],
)  # fmt: skip
def test_encryption_and_retention_errors(tmp_path: Path, doc: dict[str, Any], refusal: str) -> None:
    assert _refusal(tmp_path, **doc).startswith(refusal)


@pytest.mark.parametrize("name", ["kek_ref", "tokeniser_key_ref"])
@pytest.mark.parametrize("volume", ["e", "w"])
def test_the_keys_must_live_outside_the_volumes(tmp_path: Path, name: str, volume: str) -> None:
    inside = ENCRYPTION | {name: f"file:{tmp_path / volume / 'secrets' / 'key'}"}
    assert _refusal(tmp_path, encryption=inside) == f"encryption.{name}: {INSIDE}"
    beside = ENCRYPTION | {name: f"file:{tmp_path / (volume + '-secrets') / 'key'}"}  # a sibling is outside
    assert _load(tmp_path, encryption=beside).encryption is not None


def test_a_relative_secret_reference_is_the_settings_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative ``file:`` reference names a file beside the settings, like every other path of the file
    and whatever the working directory: the custody check judges the path
    ``read_secret`` reads. An absolute path stays, and a bare ``file:`` is still refused."""
    monkeypatch.chdir(tmp_path.parent)  # another working directory
    (tmp_path / "dsn").write_text("postgresql://u:pw@db/ds\n")
    absolute = HOOK["signing_key_refs"][0]
    channels = {
        "ops-hook": HOOK | {"signing_key_refs": ["file:keys/hook.key", absolute]},  # a list
        "t": TEAMS | {"url_ref": "file:keys/teams.url"},
        "m": MAIL | {"password_ref": "file:../smtp.pw"},
    }
    tokens = {"public_url": "https://ds.example"}
    keys = ENCRYPTION | {"kek_ref": "file:keys/kek"}
    settings = _load(tmp_path, auth=tokens, encryption=keys, **_notify(channels))
    assert settings.database_dsn_ref == f"file:{tmp_path / 'dsn'}"
    assert read_secret(settings.database_dsn_ref) == "postgresql://u:pw@db/ds"
    assert dataclasses.astuple(settings.encryption) == (
        f"file:{tmp_path / 'keys' / 'kek'}",
        "file:/run/secrets/tokeniser",
    )
    assert settings.notify.refs() == {
        "notify.channels.ops-hook.signing_key_refs.0": f"file:{tmp_path / 'keys' / 'hook.key'}",
        "notify.channels.ops-hook.signing_key_refs.1": absolute,
        "notify.channels.t.url_ref": f"file:{tmp_path / 'keys' / 'teams.url'}",
        "notify.channels.m.password_ref": f"file:{tmp_path / '..' / 'smtp.pw'}",
    }
    oidc = OIDC | {"client_secret_ref": "file:oidc_secret"}
    sso = _load(
        tmp_path,
        auth=tokens | {"oidc": oidc},
        encryption=keys,
        packs=PACKS | {"signing_key_ref": "file:p.key"},
    )
    assert sso.auth.oidc.client_secret_ref == f"file:{tmp_path / 'oidc_secret'}"
    assert sso.packs.signing_key_ref == f"file:{tmp_path / 'p.key'}"
    assert _refusal(tmp_path, encryption=ENCRYPTION | {"kek_ref": "file:"}) == (
        "encryption.kek_ref: String should match pattern '^file:.+'"
    )
    inside = ENCRYPTION | {"tokeniser_key_ref": "file:w/tokeniser"}  # in workspace_dir, not the directory's
    assert _refusal(tmp_path, auth=tokens, encryption=inside) == f"encryption.tokeniser_key_ref: {INSIDE}"
    members = set(re.findall(r"^    (\w+): .*\bSecretRef\b", Path(config.__file__).read_text(), re.M))
    assert len(members) == 9 and all(name.endswith(("_ref", "_refs")) for name in members)  # the rule's reach


def test_a_leftover_workspace_tokeniser_key_stops_every_entry_point(tmp_path: Path) -> None:
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / "tokeniser.key").write_text("ab" * 32 + "\n")
    assert _load(tmp_path).encryption is None  # developer mode keeps it there
    with pytest.raises(SettingsError) as refused:
        _load(tmp_path, encryption=ENCRYPTION)
    assert str(refused.value) == (
        "tokeniser.key was moved to encryption.tokeniser_key_ref: delete it from the workspace"
    )


PACKS = {"installation": "prod-eu-1", "signing_key_ref": "file:/run/secrets/pack-signing-key"}
SSO = {"public_url": "https://ds.example", "oidc": OIDC}
NEEDS_SSO = "packs: needs auth.oidc (packs certify sign-offs, which need SSO)"


def test_pack_settings_load_under_sso_only(tmp_path: Path) -> None:
    settings = _load(tmp_path, auth=SSO, encryption=ENCRYPTION, packs=PACKS)
    assert (settings.packs.installation, settings.packs.signing_key_ref) == (
        "prod-eu-1",
        "file:/run/secrets/pack-signing-key",
    )
    assert _load(tmp_path).packs is None
    assert _refusal(tmp_path, packs=PACKS) == NEEDS_SSO  # developer mode
    tokens = {"public_url": "https://ds.example"}
    assert _refusal(tmp_path, auth=tokens, encryption=ENCRYPTION, packs=PACKS) == NEEDS_SSO


@pytest.mark.parametrize("installation", ["", "Prod-EU-1", "prod_eu", "prod eu", "x" * 64])
def test_an_installation_name_is_1_to_63_of_a_small_alphabet(tmp_path: Path, installation: str) -> None:
    packs = PACKS | {"installation": installation}
    assert _refusal(tmp_path, auth=SSO, encryption=ENCRYPTION, packs=packs) == (
        "packs.installation: expected 1 to 63 of a-z, 0-9, '.' and '-'"
    )


@pytest.mark.parametrize("volume", ["e", "w"])
def test_the_signing_key_must_live_outside_the_volumes(tmp_path: Path, volume: str) -> None:
    packs = PACKS | {"signing_key_ref": f"file:{tmp_path / volume / 'pack-signing-key'}"}
    refusal = _refusal(tmp_path, auth=SSO, encryption=ENCRYPTION, packs=packs)
    assert refusal == f"packs.signing_key_ref: {INSIDE}"


# -- notifications ----------------------------------------------------------

HOOK = {
    "kind": "webhook",
    "url": "https://events.bank.example/deltasleuth",
    "signing_key_refs": ["file:/srv/deltasleuth/secrets/hook.key"],
}
TEAMS = {
    "kind": "teams",
    "url_ref": "file:/srv/deltasleuth/secrets/teams.url",
    "host": "prod-00.eastus.logic.azure.com",
}
MAIL = {
    "kind": "email",
    "smtp_host": "smtp.bank.example",
    "smtp_port": 587,
    "username": "ds-alerts",
    "password_ref": "file:/srv/deltasleuth/secrets/smtp.pw",
    "sender": "deltasleuth@bank.example",
    "recipients": ["parity-owners@bank.example"],
    "allow_private_network": True,
}
N = "notify: Value error, "
URL = (
    "notify.channels.ops-hook.url: must be https://host[:port]/path without userinfo, query or fragment (http"
    " only for localhost, 127.0.0.1 or [::1]); a URL carrying a secret goes in url_ref with host"
)
C = "notify.channels.{}: Value error, "


def _notify(
    channels: dict[str, Any] | None = None, rules: list[dict[str, Any]] | None = None, **options: Any
) -> dict:
    return {
        "notify": {"label": "prod", "channels": channels or {"ops-hook": HOOK}, "rules": rules or []}
        | options
    }


def _rule(**fields: Any) -> dict[str, Any]:
    return {"name": "r", "events": ["run.failed"], "channels": ["ops-hook"]} | fields


def test_the_contract_s_notify_block_loads(tmp_path: Path) -> None:
    channels = {
        "ops-hook": HOOK | {"allow_private_network": True},
        "owners-teams": TEAMS | {"proxy": True},
        "owners-mail": MAIL,
    }
    rules = [
        _rule(
            name="new-gaps",
            events=["cycle.ended"],
            channels=["owners-mail", "owners-teams"],
            when={"new_root_causes_at_least": 1},
        ),
        _rule(name="run-health", events=["run.paused", "run.stopped", "run.failed", "notify.dead"]),
    ]
    notify = _load(tmp_path, **_notify(channels, rules, proxy="http://proxy.bank.example:8080")).notify
    assert (
        notify.timeout_seconds,
        notify.max_attempts,
        notify.max_age_hours,
        notify.rate_per_hour,
        notify.dedup_hours,
        notify.daily_hour,
    ) == (10, 8, 24, 30, 6, 7)
    teams, mail = notify.channels["owners-teams"], notify.channels["owners-mail"]
    assert (teams.labels, teams.links, mail.labels, mail.links, mail.tls) == (
        False,
        False,
        True,
        True,
        "starttls",
    )
    assert notify.rules[0].modes == ("batch", "soak") and _load(tmp_path).notify is None
    measured = _load(tmp_path, **_notify(rules=[_rule(events=["accuracy.measured"], specs=["cards-*"])]))
    assert measured.notify.rules[0].events == ("accuracy.measured",)  # built: no longer reserved


@pytest.mark.parametrize(
    ("doc", "refusal"),
    [
        (
            _notify({"ops-slack": {"kind": "slack"}}),
            C.format("ops-slack") + "kind slack needs url_ref and host (its webhook URL is a secret)",
        ),
        (
            _notify({"owners-teams": {"kind": "teams", "url_ref": "file:/t"}}),
            C.format("owners-teams") + "kind teams needs url_ref and host (its webhook URL is a secret)",
        ),
        (
            _notify({"ops-hook": HOOK | {"url": "https://events.bank.example/x?token=1"}}),
            URL,
        ),
        (
            _notify({"ops-hook": HOOK | {"url": "http://events.bank.example/x"}}),
            URL,
        ),
        (
            _notify({"ops-hook": HOOK | {"signing_key_refs": []}}),
            C.format("ops-hook") + "kind webhook needs signing_key_refs (one or two file: references)",
        ),
        (
            _notify({"owners-mail": MAIL | {"tls": "none"}}),
            C.format("owners-mail") + "tls none needs allow_private_network and no username",
        ),
        (
            _notify({"owners-mail": MAIL | {"username": None}}),
            C.format("owners-mail") + "password_ref needs username",
        ),
        (
            _notify({"owners-mail": MAIL | {"proxy": True}}),
            C.format("owners-mail") + "proxy is only for webhook, slack and teams",
        ),
        (
            _notify(
                {"owners-teams": TEAMS | {"proxy": True, "allow_private_network": True}},
                proxy="http://p.example:1",
            ),
            C.format("owners-teams")
            + "allow_private_network with proxy: true is meaningless: the proxy resolves the"
            " destination",
        ),
        (
            _notify({"ops-hook": HOOK | {"url": "https://10.1.4.2/x"}}),
            C.format("ops-hook") + "10.1.4.2 is a private address: set allow_private_network to allow it",
        ),
        (
            _notify({"ops-hook": HOOK | {"url": "https://169.254.169.254/x", "allow_private_network": True}}),
            C.format("ops-hook")
            + "169.254.169.254 is never a notification destination (link-local, unspecified,"
            " multicast or metadata)",
        ),
        (
            _notify({"owners-teams": TEAMS | {"proxy": True}}),
            N + "channels.owners-teams: proxy: true needs notify.proxy",
        ),
        (
            _notify(rules=[_rule(), _rule(name="b", channels=["ops-slak"])]),
            N + "rules.1.channels: unknown channel ops-slak",
        ),
        (
            _notify(rules=[_rule(), _rule(name="x"), _rule(name="sign-off"), _rule(name="sign-off")]),
            N + "rules.3.name: duplicate rule sign-off",
        ),
        (  # readiness.changed: a rule for it names the causes it takes
            _notify(rules=[_rule(events=["readiness.changed"])]),
            "notify.rules.0: Value error, readiness.changed needs causes (baseline and config are never"
            " implied)",
        ),
        (
            _notify(rules=[_rule(events=["readiness.changed", "licence.state"], causes=["cycle"])]),
            "notify.rules.0: Value error, programmes, gates and causes are only for readiness.changed",
        ),
        (
            _notify(rules=[_rule(gates=["targets"])]),
            "notify.rules.0: Value error, programmes, gates and causes are only for readiness.changed",
        ),
        (
            _notify(
                rules=[_rule(events=["readiness.changed"], causes=["cycle"], states=["expired", "ready"])]
            ),
            "notify.rules.0.states.1: expected a licence state or a readiness state",
        ),
        (
            _notify(rules=[_rule(events=["notify.test"])]),
            "notify.rules.0.events.0: notify.test is sent only by a test",
        ),
        (
            _notify(rules=[_rule(when={"new_root_causes_at_least": 1})]),
            "notify.rules.0: Value error, when is only for cycle.ended",
        ),
        (
            _notify(rules=[_rule(events=["run.failed"], reasons=["cancelled"])]),
            "notify.rules.0: Value error, reasons is only for run.paused, run.stopped and run.finished",
        ),
        (  # a missed time's reason is no rule's filter
            _notify(rules=[_rule(events=["schedule.missed"], reasons=["cancelled"])]),
            "notify.rules.0: Value error, reasons is only for run.paused, run.stopped and run.finished",
        ),
        (
            _notify(rules=[_rule(states=["expired"])]),
            "notify.rules.0: Value error, states is only for licence.state and readiness.changed",
        ),
        (
            _notify(rules=[_rule(statuses=["failed"])]),
            "notify.rules.0: Value error, statuses is only for sample.judged",
        ),
        (
            _notify(
                rules=[
                    _rule(),
                    _rule(name="a"),
                    _rule(name="b"),
                    _rule(name="c", events=["licence.state"], specs=["cards-*"]),
                ]
            ),
            "notify.rules.3: Value error, licence.state, notify.dead, notify.skipped and notify.daily match"
            " no spec",
        ),
        (
            _notify({"ops-hook": {"kind": "webhook", "signing_key_refs": ["file:/k"]}}),
            C.format("ops-hook") + "kind webhook needs url, or url_ref and host",
        ),
        (
            _notify({"m": {"kind": "email", "smtp_host": "smtp.bank.example"}}),
            C.format("m") + "kind email needs smtp_host, sender and recipients (1 to 20)",
        ),
        (
            _notify({"m": HOOK | {"smtp_host": "smtp.bank.example"}}),
            C.format("m") + "smtp_host is only for email",
        ),
        (
            _notify({"t": TEAMS | {"signing_key_refs": ["file:/k"]}}),
            C.format("t") + "signing_key_refs is only for webhook",
        ),
        (_notify({"ops-hook": HOOK | {"tls": "none"}}), C.format("ops-hook") + "tls is only for email"),
        (_notify({"t": TEAMS | {"tls": "implicit"}}), C.format("t") + "tls is only for email"),
        (
            _notify(
                {"ops-hook": HOOK | {"url": "http://localhost:8080/hook", "proxy": True}},
                proxy="http://p.example:1",
            ),
            C.format("ops-hook")
            + "proxy: true needs an https url (http is for loopback, never through a proxy)",
        ),
        (
            _notify(proxy="http://169.254.169.254:8080"),
            N
            + "proxy: 169.254.169.254 is never a notification destination (link-local, unspecified, multicast"
            " or metadata)",
        ),
        (
            _notify(rules=[_rule(events=["cycle.ended"], when={})]),
            "notify.rules.0.when: Value error, when names no condition",
        ),
        (_notify(rules=[_rule(events=[])]), "notify.rules.0: Value error, events names no event"),
        (
            _notify(rules=[_rule(events=["run.ended"])]),
            "notify.rules.0.events.0: Input should be 'cycle.ended', 'sample.judged', 'run.finished',"
            " 'run.failed', 'run.stopped', 'run.paused', 'sample.draw_failed', 'accuracy.measured',"
            " 'approval.waiting', 'approval.revoked', 'licence.state', 'readiness.changed',"
            " 'schedule.missed', 'schema.drift', 'notify.dead', 'notify.skipped' or 'notify.daily'",
        ),
        (  # the placeholder of the event that became accuracy.measured
            _notify(rules=[_rule(events=["accuracy.label"])]),
            "notify.rules.0.events.0: Input should be 'cycle.ended', 'sample.judged', 'run.finished',"
            " 'run.failed', 'run.stopped', 'run.paused', 'sample.draw_failed', 'accuracy.measured',"
            " 'approval.waiting', 'approval.revoked', 'licence.state', 'readiness.changed',"
            " 'schedule.missed', 'schema.drift', 'notify.dead', 'notify.skipped' or 'notify.daily'",
        ),
        (_notify(daily_hour=24), "notify.daily_hour: Input should be less than or equal to 23"),
        (_notify(timeout_seconds=61), "notify.timeout_seconds: Input should be less than or equal to 60"),
        (_notify(max_attempts=1), "notify.max_attempts: Input should be greater than or equal to 2"),
        (_notify(label="Prod"), "notify.label: String should match pattern '^[a-z0-9][a-z0-9-]{0,31}$'"),
    ],
)
def test_notify_settings_are_refused_with_the_contract_s_messages(
    tmp_path: Path, doc: dict, refusal: str
) -> None:
    assert _refusal(tmp_path, **doc) == refusal


def test_a_readiness_rule_names_its_causes_and_may_name_programmes_states_and_gates(tmp_path: Path) -> None:
    """The reservation is lifted, and ``states`` takes either kind's."""
    keys = {
        "causes": ["cycle", "time"],
        "programmes": ["cutover-*"],
        "states": ["not_ready"],
        "gates": ["fresh"],
    }
    rules = [
        _rule(name="falls", events=["readiness.changed"], specs=["cards-*"], **keys),
        _rule(name="baselines", events=["readiness.changed"], causes=["baseline", "config"]),
        _rule(name="licence", events=["licence.state"], states=["expired"]),
    ]
    falls, baselines, _ = _load(tmp_path, **_notify(rules=rules)).notify.rules
    assert (falls.causes, falls.programmes, falls.states, falls.gates, falls.specs) == (
        ("cycle", "time"), ("cutover-*",), ("not_ready",), ("fresh",), ("cards-*",),
    )  # fmt: skip
    assert (baselines.causes, baselines.programmes, baselines.gates) == (("baseline", "config"), None, None)


@pytest.mark.parametrize("setting", ["url_ref", "password_ref", "signing_key_refs"])
def test_notification_secrets_stay_outside_the_volumes(tmp_path: Path, setting: str) -> None:
    inside = f"file:{tmp_path / 'e' / 'secret'}"
    channels = (
        {"ops-hook": HOOK | {"signing_key_refs": [inside]}}
        if setting == "signing_key_refs"
        else {"t": TEAMS | {"url_ref": inside}}
        if setting == "url_ref"
        else {"m": MAIL | {"password_ref": inside}}
    )
    name = next(iter(channels))
    suffix = ".0" if setting == "signing_key_refs" else ""
    refusal = _refusal(
        tmp_path, auth={"public_url": "https://ds.example"}, encryption=ENCRYPTION, **_notify(channels)
    )
    assert refusal == f"notify.channels.{name}.{setting}{suffix}: {INSIDE}"


# -- cut-over readiness -----------------------------------------------------

Q4 = {
    "name": "cutover-q4",
    "gates": {"convergence": {"cycles": 5}},
    "pairs": [
        {"spec": "customer-profile", "entity": "customer"},
        {"spec": "customer-links", "entity": "customer", "gates": {"tier": "high"}},
        {"spec": "legacy-accounts", "entity": "account", "closed": "2026-11-02"},
    ],
}
GATES = {
    "tier": "standard",
    "convergence": {"cycles": 3},
    "fresh": {"max_age": "7d"},
    "graph": {"required": False},
    "accuracy": {"min_decided": 50, "max_b_wrong": 0},
}
SLUG = "expected a lower-case kebab-case name of 1 to 63 characters, as a spec name"
TIGHTENS = "a programme or pair only tightens targets"


def _programmes(*programmes: dict[str, Any], **gates: Any) -> dict[str, Any]:
    return {"readiness": {"gates": gates, "programmes": list(programmes)}}


def test_the_contract_s_readiness_block_loads_and_turns_the_key_census_on(tmp_path: Path) -> None:
    loaded = _load(tmp_path, readiness={"gates": GATES, "programmes": [Q4]}).readiness
    assert loaded.gates.tier == "standard" and loaded.gates.accuracy.max_b_wrong == 0
    [programme] = loaded.programmes
    assert (programme.name, programme.gates.convergence.cycles, programme.gates.tier) == (
        "cutover-q4",
        5,
        None,
    )
    assert [(pair.spec, pair.entity, pair.gates.tier, pair.closed) for pair in programme.pairs] == [
        ("customer-profile", "customer", None, None),
        ("customer-links", "customer", "high", None),
        ("legacy-accounts", "account", None, date(2026, 11, 2)),
    ]
    assert readiness.policy(loaded, "customer-links").gates == readiness.Gates(
        "high", 5, "7d", False, (50, 0)
    )
    # Present (even {}), the worker counts key censuses; absent, only packs do.
    absent, empty = _load(tmp_path), _load(tmp_path, readiness={})
    assert (
        absent.readiness is None and empty.readiness.gates.tier is None and empty.readiness.programmes == ()
    )
    assert _load(tmp_path, readiness={"gates": {"accuracy": {}}}).readiness.gates.accuracy.min_decided == 50
    counting = {name: _Worker(SimpleNamespace(settings=found)).counting for name, found in  # type: ignore[arg-type]
                (("absent", absent), ("empty", empty))}  # fmt: skip
    assert counting == {"absent": False, "empty": True}
    packed = _load(tmp_path, auth=SSO, encryption=ENCRYPTION, packs=PACKS)
    assert _Worker(SimpleNamespace(settings=packed)).counting  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("doc", "refusal"),
    [
        (
            _programmes(
                {"name": "cutover-q4", "pairs": [{"spec": "customer-profile"}]},
                {"name": "cutover-q1", "pairs": [{"spec": "customer-profile"}]},
            ),
            "readiness: spec customer-profile is in programmes cutover-q4 and cutover-q1: a pair belongs to"
            " one programme",
        ),
        (
            _programmes({"name": "cutover-q4"}, {"name": "cutover-q4"}),
            "readiness: programme cutover-q4 is named twice",
        ),
        (
            _programmes({"name": "cutover-q4", "pairs": [{"spec": "customer-profile"}] * 2}),
            "readiness.programmes.0: spec customer-profile is listed twice",
        ),
        (
            _programmes(tier="low"),
            "readiness.gates.tier: expected standard or high: readiness targets never go below 0.1%",
        ),
        (
            _programmes(Q4 | {"gates": {"tier": 0.05}}),
            "readiness.programmes.0.gates.tier: expected standard or high: readiness targets never go below"
            " 0.1%",
        ),
        (
            _programmes(
                Q4 | {"gates": {"tier": "high"}, "pairs": [{"spec": "x", "gates": {"tier": "standard"}}]}
            ),
            f"readiness.programmes.0: standard is looser than programme cutover-q4's high: {TIGHTENS}",
        ),
        (
            _programmes(Q4 | {"gates": {"tier": "standard"}}, tier="high"),
            f"readiness: standard is looser than the installation's high: {TIGHTENS}",
        ),
        (
            _programmes({"name": "q", "pairs": [{"spec": "x", "gates": {"tier": "standard"}}]}, tier="high"),
            f"readiness: standard is looser than the installation's high: {TIGHTENS}",
        ),
        (
            _programmes(convergence={"cycles": 0}),
            "readiness.gates.convergence.cycles: expected 1 to 50",
        ),
        (_programmes(convergence={"cycles": 51}), "readiness.gates.convergence.cycles: expected 1 to 50"),
        (_programmes(convergence={"cycles": "3"}), "readiness.gates.convergence.cycles: expected 1 to 50"),
        (
            _programmes(fresh={"max_age": "59m"}),
            "readiness.gates.fresh.max_age: expected a duration from 1h to 90d",
        ),
        (
            _programmes(fresh={"max_age": "91d"}),
            "readiness.gates.fresh.max_age: expected a duration from 1h to 90d",
        ),
        (
            _programmes(Q4 | {"gates": {"fresh": {"max_age": 7}}}),
            "readiness.programmes.0.gates.fresh.max_age: expected a duration from 1h to 90d",
        ),
        (_programmes({"name": "Cutover_Q4"}), f"readiness.programmes.0.name: {SLUG}"),
        (
            _programmes({"name": "q", "pairs": [{"spec": "x" * 64}]}),
            f"readiness.programmes.0.pairs.0.spec: {SLUG}",
        ),
        (
            _programmes({"name": "q", "pairs": [{"spec": "x", "entity": ""}]}),
            f"readiness.programmes.0.pairs.0.entity: {SLUG}",
        ),
        (_programmes(targets={"standard": "0.2"}), "readiness.gates.targets: Unexpected keyword argument"),
        (
            _programmes(accuracy={"min_decided": 0}),
            "readiness.gates.accuracy.min_decided: Input should be greater than or equal to 1",
        ),
    ],
)
def test_readiness_settings_are_refused_with_the_contract_s_messages(
    tmp_path: Path, doc: dict, refusal: str
) -> None:
    assert _refusal(tmp_path, **doc) == refusal


def test_readiness_durations_and_tiers_that_load(tmp_path: Path) -> None:
    for max_age in ("1h", "60m", "90d", "36h"):
        assert (
            _load(tmp_path, **_programmes(fresh={"max_age": max_age})).readiness.gates.fresh.max_age
            == max_age
        )
    tightened = _programmes(Q4 | {"gates": {"tier": "high"}}, tier="standard")  # a programme may tighten
    assert _load(tmp_path, **tightened).readiness.programmes[0].gates.tier == "high"
    assert (
        _load(tmp_path, **_programmes(convergence={"cycles": 1}, tier="high")).readiness.gates.tier == "high"
    )


X1 = (
    "files.memory and files.spill are sizes such as 2GB (MB, GB or TB); threads is 1 to 64, max_files 1 to"
    " 20,000, plan_s 10 to 86,400; spill_dir and roots_under are absolute paths; os_limits is true or false"
)


def test_files_settings_default_and_load(tmp_path: Path) -> None:
    """Every member defaulted, the spill under the evidence volume unless declared, and
    the reader's limits on."""
    from deltasleuth.connectors.tables.manifest import size_bytes

    plain = _load(tmp_path)
    files = plain.files
    assert (files.memory, files.spill, files.threads, files.max_files, files.plan_s) == (
        "2GB",
        "20GB",
        2,
        1000,
        300,
    )
    assert (files.spill_dir, files.roots_under) == (None, Path("/var/lib/deltasleuth/files"))
    assert plain.spill_dir == tmp_path / "e" / "spill" and files.os_limits is True
    limits = {"memory": "512MB", "spill": "1TB", "threads": 64, "spill_dir": "/srv/spill",
              "roots_under": "/srv/exports", "max_files": 20_000, "plan_s": 86_400,
              "os_limits": False}  # fmt: skip
    loaded = _load(tmp_path, files=limits)
    assert dataclasses.asdict(loaded.files) == limits | {
        "spill_dir": Path("/srv/spill"),
        "roots_under": Path("/srv/exports"),
    }
    assert loaded.spill_dir == Path("/srv/spill")
    # Binary units, as sampling.temp_buffers reads them; handed to DuckDB as whole MiB.
    assert [size_bytes(s) for s in ("512MB", "2GB", "1TB")] == [512 * 2**20, 2 * 2**30, 2**40]


@pytest.mark.parametrize(
    "files",
    [
        {"memory": "2G"}, {"memory": "2kB"}, {"memory": "0GB"}, {"spill": "1.5GB"}, {"spill": 20},
        {"threads": 0}, {"threads": 65}, {"max_files": 0}, {"max_files": 20_001}, {"plan_s": 9},
        {"plan_s": 86_401}, {"spill_dir": "spill"}, {"roots_under": "exports"}, {"threads": "2"},
        {"os_limits": "false"}, {"os_limits": 0}, {"os_limits": None},
    ],
)  # fmt: skip
def test_files_settings_are_refused_with_x1(tmp_path: Path, files: dict[str, Any]) -> None:
    assert _refusal(tmp_path, files=files) == X1


def test_a_mistyped_files_setting_is_named(tmp_path: Path) -> None:
    assert _refusal(tmp_path, files={"memry": "2GB"}).startswith("files.memry: Unexpected keyword argument")


# -- the MCP server ---------------------------------------------------------


def test_mcp_is_off_unless_its_block_is_there_and_states_its_data_classes(tmp_path: Path) -> None:
    assert _load(tmp_path).mcp is None  # absent: no MCP at all
    least = _load(tmp_path, mcp={"data_classes": ["aggregates"]}).mcp
    assert dataclasses.asdict(least) == {
        "data_classes": ("aggregates",), "http": False, "work": (), "calls_per_minute": 60, "concurrency": 4,
    }  # fmt: skip
    block = {
        "data_classes": ["aggregates", "production_tokenised", "production_clear_declared"], "http": True,
        "work": ["compare_key"], "calls_per_minute": 600, "concurrency": 16,
    }  # fmt: skip
    assert dataclasses.asdict(_load(tmp_path, mcp=block).mcp) == block | {
        "data_classes": tuple(block["data_classes"]),
        "work": ("compare_key",),
    }
    assert "ref" not in " ".join(dataclasses.asdict(least))  # the block holds no secret


@pytest.mark.parametrize(
    ("mcp", "refusal"),
    [
        ({}, "mcp.data_classes: Field required"),
        ({"data_classes": []}, "mcp.data_classes: Tuple should have at least 1 item after validation, not 0"),
        ({"data_classes": ["aggregates", "raw"]},
         "mcp.data_classes.1: Input should be 'aggregates', 'production_tokenised' or"
         " 'production_clear_declared'"),
        # a block none of whose classes is one is told that it lists none, too
        ({"data_classes": ["production"]},
         "mcp.data_classes.0: Input should be 'aggregates', 'production_tokenised' or"
         " 'production_clear_declared'; mcp.data_classes: Tuple should have at least 1 item after"
         " validation, not 0"),
        ({"data_classes": ["aggregates"], "work": ["start_run"]},
         "mcp.work.0: Input should be 'compare_key'"),
        ({"data_classes": ["aggregates"], "work": ["compare_key"]},
         "mcp: Value error, work names compare_key, whose results are production_tokenised: add it to"
         " data_classes"),
        ({"data_classes": ["aggregates", "production_clear_declared"]},
         "mcp: Value error, production_clear_declared widens production_tokenised: add it to data_classes"),
        ({"data_classes": ["aggregates"], "http": "yes"}, "mcp.http: Input should be a valid boolean"),
        ({"data_classes": ["aggregates"], "calls_per_minute": 601},
         "mcp.calls_per_minute: Input should be less than or equal to 600"),
        ({"data_classes": ["aggregates"], "concurrency": 0},
         "mcp.concurrency: Input should be greater than or equal to 1"),
        ({"data_classes": ["aggregates"], "token_ref": "file:/x"},
         "mcp.token_ref: Unexpected keyword argument"),
    ],
)  # fmt: skip
def test_mcp_settings_are_refused_with_the_contract_s_messages(
    tmp_path: Path, mcp: dict[str, Any], refusal: str
) -> None:
    assert _refusal(tmp_path, mcp=mcp) == refusal
