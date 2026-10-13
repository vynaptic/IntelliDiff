"""Target registry loading, SSRF-relevant URL rules, capabilities and credential references."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, NoReturn

import pytest

from deltasleuth.core.secrets import MAX_SECRET_BYTES
from deltasleuth.core.spec import (
    SecretError,
    TargetRegistry,
    TargetsError,
    UnknownTargetError,
    load_targets,
    read_secret,
)
from deltasleuth.core.types import (
    AuthoritativeAbsence,
    DatabaseTarget,
    Target,
    TargetAuth,
    TargetCapabilities,
    TruncationIndicators,
)

WriteYaml = Callable[[str, Any], Path]


def _load(write_yaml: WriteYaml, targets: dict[str, Any]) -> TargetRegistry:
    return load_targets(write_yaml("targets.yaml", {"targets": targets}))


def _problems(write_yaml: WriteYaml, document: dict[str, Any]) -> set[tuple[str, str]]:
    with pytest.raises(TargetsError) as exc:
        load_targets(write_yaml("targets.yaml", document))
    return {(issue.pointer, issue.code) for issue in exc.value.issues}


def test_full_target(write_yaml: WriteYaml, tmp_path: Path) -> None:
    capabilities = {
        "authoritative_absence": {"status": [404, 410, 200], "shapes": ["empty"]},
        "absence_shapes": {"empty": "!has(body.data)"},
        "truncation_indicators": {"cap": 50, "items_path": "$['items']", "count_path": "$.total"},
        "pagination": "cursor",
        "stable_ordering": True,
        "watermark": "per_request",
        "max_depth": 64,
    }
    entry = {
        "base_url": "HTTPS://Customers-V1.Example:8443/api/",
        "auth": {"kind": "header", "header": "X-Api-Key", "secret_ref": "file:secrets/v1.key"},
        "rate_per_s": 12.5,
        "max_concurrency": 2,
        "capabilities": capabilities,
    }
    target = _load(write_yaml, {"customer-v1": entry}).get("customer-v1")
    assert target.base_url == "https://customers-v1.example:8443/api"
    secret_ref = "file:" + str(tmp_path / "secrets" / "v1.key")  # relative to the targets file
    assert target.auth == TargetAuth(kind="header", header="X-Api-Key", secret_ref=secret_ref)
    assert (target.rate_per_s, target.max_concurrency, target.allow_private_network) == (12.5, 2, False)
    caps = target.capabilities
    assert caps.authoritative_absence == AuthoritativeAbsence(status=(404, 410, 200), shapes=("empty",))
    assert caps.absence_shapes == {"empty": "!has(body.data)"}
    assert caps.truncation_indicators == TruncationIndicators(
        cap=50, items_path="$.items", count_path="$.total"
    )
    assert (caps.pagination, caps.stable_ordering, caps.watermark, caps.max_depth) == (
        "cursor",
        True,
        "per_request",
        64,
    )


def test_defaults_and_absolute_secret_paths(write_yaml: WriteYaml) -> None:
    auth = {"kind": "bearer", "secret_ref": "file:/run/secrets/t"}
    registry = _load(
        write_yaml,
        {"svc": {"base_url": "https://svc.example"}, "auth": {"base_url": "https://a.example", "auth": auth}},
    )
    assert registry.get("svc") == Target("svc", "https://svc.example")
    assert registry.get("auth").auth.secret_ref == auth["secret_ref"]  # an absolute path stays as written


def test_registry_api(write_yaml: WriteYaml) -> None:
    registry = _load(write_yaml, {"b-svc": {"base_url": "https://b.example"}, "files-only": {}})
    assert (list(registry), len(registry)) == (["b-svc", "files-only"], 2)
    assert "b-svc" in registry and "c-svc" not in registry
    assert registry.live("b-svc").base_url == "https://b.example"
    assert registry.get("files-only").base_url == ""  # a target read from file samples only
    with pytest.raises(TargetsError) as exc:
        registry.live("files-only")
    assert [(i.pointer, i.code) for i in exc.value.issues] == [
        ("/targets/files-only/base_url", "missing_base_url")
    ]
    with pytest.raises(UnknownTargetError):
        registry.get("c-svc")
    with pytest.raises(ValueError, match="differs"):
        TargetRegistry({"a": Target("b", "https://b.example")})


def test_database_target(write_yaml: WriteYaml, tmp_path: Path) -> None:
    entry = {"dsn_ref": "file:secrets/db.dsn", "rate_per_s": 200, "max_concurrency": 2}
    target = _load(write_yaml, {"db": entry}).live("db")
    dsn_ref = "file:" + str(tmp_path / "secrets" / "db.dsn")  # relative to the targets file
    absence = TargetCapabilities(authoritative_absence=AuthoritativeAbsence(status=(404,)))  # fixed
    assert target == Target(
        "db", "", dsn_ref=dsn_ref, rate_per_s=200.0, max_concurrency=2, capabilities=absence
    )


@pytest.mark.parametrize(
    ("member", "value"),
    [
        ("base_url", "https://db.example"),
        ("auth", {"kind": "bearer", "secret_ref": "file:t"}),
        ("capabilities", {"authoritative_absence": {"status": [404]}}),
    ],
)
def test_a_database_target_takes_no_service_members(write_yaml: WriteYaml, member: str, value: Any) -> None:
    targets = {"targets": {"db": {"dsn_ref": "file:db.dsn", member: value}}}
    assert _problems(write_yaml, targets) == {(f"/targets/db/{member}", "schema.not")}


# --- a database that declares its endpoint ----------------------------------

MYSQL = {
    "engine": "mysql",
    "host": "ledger-db.bank.example",
    "database": "ledger",
    "user": "ds_reader",
    "password_ref": "file:secrets/ledger.password",
}
ORACLE = {
    "engine": "oracle",
    "host": "crm-db.bank.example",
    "service": "CRMPDB",
    "user": "ds_reader",
    "password_ref": "file:/run/secrets/crm_legacy_password",
    "tls_server_dn": "CN=crm-db.bank.example,O=Bank",
}


def test_engine_targets(write_yaml: WriteYaml, tmp_path: Path) -> None:
    oracle = ORACLE | {"port": 2484, "tls_ca_ref": "file:secrets/ca.pem", "allow_private_network": True}
    registry = _load(write_yaml, {"ledger": MYSQL | {"rate_per_s": 50, "max_concurrency": 2}, "crm": oracle})
    ledger, crm = registry.live("ledger"), registry.live("crm")
    absence = TargetCapabilities(authoritative_absence=AuthoritativeAbsence(status=(404,)))  # fixed
    password = "file:" + str(tmp_path / "secrets" / "ledger.password")  # relative to the targets file
    assert ledger == Target(
        "ledger", "", rate_per_s=50.0, max_concurrency=2, capabilities=absence,
        database=DatabaseTarget("mysql", "ledger-db.bank.example", 3306, "ledger", "ds_reader", password),
    )  # fmt: skip
    assert (ledger.engine, crm.engine, registry.get("crm").allow_private_network) == ("mysql", "oracle", True)
    assert crm.database == DatabaseTarget(
        "oracle", "crm-db.bank.example", 2484, "CRMPDB", "ds_reader", ORACLE["password_ref"],
        tls_ca_ref="file:" + str(tmp_path / "secrets" / "ca.pem"), tls_server_dn=ORACLE["tls_server_dn"],
    )  # fmt: skip
    unencrypted = {name: value for name, value in ORACLE.items() if name != "tls_server_dn"}
    plain = _load(write_yaml, {"crm": unencrypted | {"allow_unencrypted": True}})
    assert plain.get("crm").database.port == 1521  # type: ignore[union-attr]
    chain = MYSQL | {"tls_verify_name": False, "tls_ca_ref": "file:ca.pem"}
    assert _load(write_yaml, {"ledger": chain}).get("ledger").database.tls_verify_name is False  # type: ignore[union-attr]
    service, postgres = Target("svc", "https://svc.example"), Target("db", "", dsn_ref="file:dsn")
    assert (service.engine, postgres.engine, Target("files", "").engine) == (None, "postgresql", None)


def _refused(write_yaml: WriteYaml, entry: dict[str, Any]) -> set[tuple[str, str, str]]:
    entry = {name: value for name, value in entry.items() if value is not None}
    with pytest.raises(TargetsError) as exc:
        load_targets(write_yaml("targets.yaml", {"targets": {"db": entry}}))
    return {(issue.pointer, issue.code, issue.message) for issue in exc.value.issues}


NO_SUBJECT = (
    "tls_server_dn is not a certificate's subject as RFC 4514 writes one (CN=crm-db.bank.example,O=Bank,"
    " no space after a comma)"
)


@pytest.mark.parametrize(
    ("entry", "at", "code", "message"),
    [
        (
            MYSQL | {"service": "X"},
            "",
            "invalid_database_target",
            "engine mysql needs database and takes no service",
        ),
        (
            MYSQL | {"database": None},
            "",
            "invalid_database_target",
            "engine mysql needs database and takes no service",
        ),
        (
            ORACLE | {"database": "x", "service": None},
            "",
            "invalid_database_target",
            "engine oracle needs service and takes no database",
        ),
        (
            MYSQL | {"allow_unencrypted": True, "tls_verify_name": True},
            "",
            "invalid_database_target",
            "allow_unencrypted: true takes no tls_ca_ref, tls_server_dn or tls_verify_name",
        ),
        (
            ORACLE | {"allow_unencrypted": True},
            "",
            "invalid_database_target",
            "allow_unencrypted: true takes no tls_ca_ref, tls_server_dn or tls_verify_name",
        ),
        (
            MYSQL | {"tls_server_dn": "CN=ledger"},
            "",
            "invalid_database_target",
            "tls_server_dn is Oracle's",
        ),
        (
            MYSQL | {"host": "10.1.2.3"},
            "/host",
            "private_network",
            "loopback and private hosts need allow_private_network: true",
        ),
        (
            MYSQL | {"host": "::1"},
            "/host",
            "private_network",
            "loopback and private hosts need allow_private_network: true",
        ),
        (
            MYSQL | {"host": "169.254.169.254", "allow_private_network": True},
            "/host",
            "forbidden_host",
            "169.254.169.254 is never a target (link-local, unspecified, multicast or metadata)",
        ),
        (
            MYSQL | {"host": "fd00:ec2::254", "allow_private_network": True},
            "/host",
            "forbidden_host",
            "fd00:ec2::254 is never a target (link-local, unspecified, multicast or metadata)",
        ),
        (
            MYSQL | {"host": "64:ff9b::a01:203"},  # 10.1.2.3 behind a NAT64 translator
            "/host",
            "private_network",
            "loopback and private hosts need allow_private_network: true",
        ),
        (
            MYSQL | {"host": "64:ff9b::a9fe:a9fe", "allow_private_network": True},  # 169.254.169.254
            "/host",
            "forbidden_host",
            "64:ff9b::a9fe:a9fe is never a target (link-local, unspecified, multicast or metadata)",
        ),
        (
            MYSQL | {"host": "db:3306"},
            "/host",
            "invalid_database_target",
            "host is a DNS name or an IP literal",
        ),
        (
            ORACLE | {"tls_server_dn": None},
            "",
            "invalid_database_target",
            "engine oracle with TLS needs tls_server_dn (the server certificate's subject), or"
            " tls_verify_name: false",
        ),
        (
            MYSQL | {"tls_verify_name": False},
            "",
            "invalid_database_target",
            "tls_verify_name: false needs tls_ca_ref and takes no tls_server_dn",
        ),
        (
            ORACLE | {"tls_verify_name": False, "tls_ca_ref": "file:ca.pem"},
            "",
            "invalid_database_target",
            "tls_verify_name: false needs tls_ca_ref and takes no tls_server_dn",
        ),
        *[
            (ORACLE | {"tls_server_dn": dn}, "", "invalid_database_target", NO_SUBJECT)
            for dn in ("crm-db.bank.example", "CN=crm-db.bank.example, O=Bank", "CN=crm,,O=Bank", "X=crm")
        ],
    ],
    ids=[
        "T1",
        "T1 none",
        "T1 oracle",
        "T2",
        "T2 dn",
        "T3",
        "T4",
        "T4 v6",
        "T5",
        "T5 v6",
        "T4 nat64",
        "T5 nat64",
        "host",
        "T6",
        "T7",
        "T7 dn",
        "T8 no attribute",
        "T8 space",
        "T8 empty",
        "T8 unknown type",
    ],
)
def test_an_engine_target_declared_wrongly_is_refused(
    write_yaml: WriteYaml, entry: dict[str, Any], at: str, code: str, message: str
) -> None:
    assert _refused(write_yaml, entry) == {(f"/targets/db{at}", code, message)}


@pytest.mark.parametrize(
    ("entry", "pointer", "code"),
    [
        (MYSQL | {"dsn_ref": "file:dsn"}, "/targets/db/dsn_ref", "schema.not"),
        (MYSQL | {"base_url": "https://db.example"}, "/targets/db/base_url", "schema.not"),
        (MYSQL | {"auth": {"kind": "none"}}, "/targets/db/auth", "schema.not"),
        (MYSQL | {"capabilities": {"pagination": "none"}}, "/targets/db/capabilities", "schema.not"),
        (MYSQL | {"allow_insecure_http": True}, "/targets/db/allow_insecure_http", "schema.not"),
        ({"dsn_ref": "file:dsn", "host": "db.example"}, "/targets/db/host", "schema.not"),  # needs engine
        (
            {"base_url": "https://svc.example", "password_ref": "file:p"},
            "/targets/db/password_ref",
            "schema.not",
        ),
        ({"engine": "mysql", "database": "ledger"}, "/targets/db", "schema.required"),
        (MYSQL | {"engine": "mariadb"}, "/targets/db/engine", "schema.enum"),
        (MYSQL | {"password_ref": "s3cret"}, "/targets/db/password_ref", "schema.pattern"),
        (MYSQL | {"tls_ca_ref": "env:CA"}, "/targets/db/tls_ca_ref", "schema.pattern"),
        (MYSQL | {"port": 0}, "/targets/db/port", "schema.minimum"),
        # No connect descriptor, SID, alias, port or path fits a service, a database, a user or a host.
        (
            ORACLE | {"service": "(DESCRIPTION=(ADDRESS=(HOST=evil)))"},
            "/targets/db/service",
            "schema.pattern",
        ),
        (ORACLE | {"service": "db/pdb"}, "/targets/db/service", "schema.pattern"),
        (MYSQL | {"database": "a b"}, "/targets/db/database", "schema.pattern"),
        (MYSQL | {"user": "reader@%"}, "/targets/db/user", "schema.pattern"),
        (MYSQL | {"host": "db.example/path"}, "/targets/db/host", "schema.pattern"),
        (MYSQL | {"host": "(HOST=evil)"}, "/targets/db/host", "schema.pattern"),
        (MYSQL | {"host": "a" * 64 + ".example"}, "/targets/db/host", "schema.pattern"),
        (ORACLE | {"tls_server_dn": 'CN="x"'}, "/targets/db/tls_server_dn", "schema.pattern"),
        (ORACLE | {"tls_server_dn": "CN=x)(HOST=evil"}, "/targets/db/tls_server_dn", "schema.pattern"),
    ],
)
def test_an_engine_target_takes_only_its_own_members_and_no_descriptor(
    write_yaml: WriteYaml, entry: dict[str, Any], pointer: str, code: str
) -> None:
    assert (pointer, code) in _problems(write_yaml, {"targets": {"db": entry}})


@pytest.mark.parametrize(
    ("entry", "code"),
    [
        (
            {"base_url": "http://svc.example", "auth": {"kind": "bearer", "secret_ref": "file:t"}},
            "insecure_http",
        ),
        ({"base_url": "https://user:pass@svc.example"}, "invalid_base_url"),
        ({"base_url": "https://token@svc.example"}, "invalid_base_url"),
        ({"base_url": "https://svc.example/api?key=1"}, "invalid_base_url"),
        ({"base_url": "https://svc.example/api?"}, "invalid_base_url"),
        ({"base_url": "https://svc.example/#frag"}, "invalid_base_url"),
        ({"base_url": "ftp://svc.example"}, "invalid_base_url"),
        ({"base_url": "file:///etc/passwd"}, "invalid_base_url"),
        ({"base_url": "https://"}, "invalid_base_url"),
        ({"base_url": "https://svc.example:99999"}, "invalid_base_url"),
        ({"base_url": "https://svc.example/a/../b"}, "invalid_base_url"),
        ({"base_url": "https://svc.example/a/%2e%2e/b"}, "invalid_base_url"),
        ({"base_url": "https://svc .example"}, "invalid_base_url"),
        ({"base_url": "https://127.0.0.1:8080"}, "private_network"),
        ({"base_url": "http://localhost:8080"}, "private_network"),
        ({"base_url": "http://[::1]:8080"}, "private_network"),
        ({"base_url": "http://[::ffff:127.0.0.1]"}, "private_network"),
        ({"base_url": "http://169.254.169.254/latest"}, "private_network"),
        ({"base_url": "http://10.1.2.3"}, "private_network"),
        ({"base_url": "http://192.168.0.10"}, "private_network"),
        ({"base_url": "http://0.0.0.0"}, "private_network"),
    ],
)
def test_base_url_rules(write_yaml: WriteYaml, entry: dict[str, Any], code: str) -> None:
    assert _problems(write_yaml, {"targets": {"svc": entry}}) == {("/targets/svc/base_url", code)}


def test_network_opt_ins(write_yaml: WriteYaml) -> None:
    bearer = {"kind": "bearer", "secret_ref": "file:t"}
    targets = {
        "insecure": {"base_url": "http://svc.example", "auth": bearer, "allow_insecure_http": True},
        "plain": {"base_url": "http://svc.example"},  # plain http without credentials
        "mock": {"base_url": "http://127.0.0.1:8081/", "allow_private_network": True},
        "mock6": {"base_url": "http://[::1]:9000", "allow_private_network": True},
    }
    registry = _load(write_yaml, targets)
    assert [registry.get(t).base_url for t in targets] == [
        "http://svc.example",
        "http://svc.example",
        "http://127.0.0.1:8081",
        "http://[::1]:9000",
    ]
    assert registry.get("mock").allow_private_network


@pytest.mark.parametrize(
    ("auth", "expected"),
    [
        (
            {"kind": "bearer", "secret_ref": "env:API_TOKEN"},
            ("/targets/svc/auth/secret_ref", "schema.pattern"),
        ),
        ({"kind": "bearer", "secret_ref": "vault:kv/x"}, ("/targets/svc/auth/secret_ref", "schema.pattern")),
        ({"kind": "bearer", "secret_ref": "file:"}, ("/targets/svc/auth/secret_ref", "schema.pattern")),
        ({"kind": "bearer"}, ("/targets/svc/auth", "schema.required")),
        ({"kind": "header", "secret_ref": "file:k"}, ("/targets/svc/auth", "schema.required")),
        ({"kind": "none", "secret_ref": "file:k"}, ("/targets/svc/auth/secret_ref", "schema.not")),
        (
            {"kind": "bearer", "secret_ref": "file:k", "header": "X-Key"},
            ("/targets/svc/auth/header", "schema.not"),
        ),
        ({"kind": "basic", "secret_ref": "file:k"}, ("/targets/svc/auth/kind", "schema.enum")),
        ({"kind": "bearer", "token": "abc123"}, ("/targets/svc/auth/token", "schema.additionalProperties")),
        (
            {"kind": "header", "header": "Bad Header", "secret_ref": "file:k"},
            ("/targets/svc/auth/header", "schema.pattern"),
        ),
        (
            {"kind": "header", "header": "Host", "secret_ref": "file:k"},
            ("/targets/svc/auth/header", "schema.not"),
        ),
    ],
)
def test_auth_rules(write_yaml: WriteYaml, auth: dict[str, Any], expected: tuple[str, str]) -> None:
    assert expected in _problems(
        write_yaml, {"targets": {"svc": {"base_url": "https://svc.example", "auth": auth}}}
    )


def test_further_credential_headers(write_yaml: WriteYaml, tmp_path: Path) -> None:
    """A session cookie and an anti-forgery header together (``more``: up to three, kind header only)."""
    more = [
        {"header": "X-CSRF-Token", "secret_ref": "file:secrets/csrf"},
        {"header": "X-Tenant", "secret_ref": "file:/run/secrets/tenant"},
    ]
    auth = {"kind": "header", "header": "Cookie", "secret_ref": "file:secrets/session", "more": more}
    target = _load(write_yaml, {"svc": {"base_url": "https://svc.example", "auth": auth}}).get("svc")
    assert target.auth == TargetAuth(
        kind="header",
        header="Cookie",
        secret_ref=f"file:{tmp_path / 'secrets' / 'session'}",
        more=(
            ("X-CSRF-Token", f"file:{tmp_path / 'secrets' / 'csrf'}"),
            ("X-Tenant", "file:/run/secrets/tenant"),
        ),
    )
    refused = {
        "schema.maxItems": auth | {"more": more * 2},
        "schema.minItems": auth | {"more": []},
        "schema.not": {"kind": "bearer", "secret_ref": "file:k", "more": more},
        "schema.required": auth | {"more": [{"header": "X-Tenant"}]},
        "repeated_header": auth | {"more": [{"header": "cookie", "secret_ref": "file:k"}]},
    }
    for code, entry in refused.items():
        problems = _problems(
            write_yaml, {"targets": {"svc": {"base_url": "https://svc.example", "auth": entry}}}
        )
        assert code in {found for _, found in problems}, code


def test_inline_secret_value_is_never_echoed(write_yaml: WriteYaml) -> None:
    with pytest.raises(TargetsError) as exc:
        _load(write_yaml, {"svc": {"auth": {"kind": "bearer", "token": "s3cr3t-value", "secret_ref": 7}}})
    assert not any("s3cr3t-value" in issue.message for issue in exc.value.issues)


_AT = "/targets/svc/capabilities"


@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [
        ({"retries": 3}, (f"{_AT}/retries", "schema.additionalProperties")),
        (
            {"authoritative_absence": {"status": [500]}},
            (f"{_AT}/authoritative_absence/status/0", "schema.enum"),
        ),
        (
            {"authoritative_absence": {"status": []}},
            (f"{_AT}/authoritative_absence/status", "schema.minItems"),
        ),
        (
            {"authoritative_absence": {"status": [404], "shapes": ["gone"]}},
            (f"{_AT}/authoritative_absence/shapes/0", "unknown_absence_shape"),
        ),
        (
            {"authoritative_absence": {"status": [404, 200]}},  # a 2xx body is absence only by its shape
            (f"{_AT}/authoritative_absence/status/1", "absence_2xx_needs_shapes"),
        ),
        ({"absence_shapes": {"empty": "!has(body."}}, (f"{_AT}/absence_shapes/empty", "invalid_cel")),
        ({"absence_shapes": {"empty": "'x'"}}, (f"{_AT}/absence_shapes/empty", "invalid_cel")),  # not a bool
        ({"absence_shapes": {"empty": "body.data"}}, None),  # DYN result: decided at runtime
        ({"truncation_indicators": {"cap": 10}}, (f"{_AT}/truncation_indicators", "schema.anyOf")),
        (
            {"truncation_indicators": {"cap": 0, "items_path": "$.x"}},
            (f"{_AT}/truncation_indicators/cap", "schema.minimum"),
        ),
        (
            {"truncation_indicators": {"cap": 10, "items_path": "$.items["}},
            (f"{_AT}/truncation_indicators/items_path", "invalid_jsonpath"),
        ),
        ({"pagination": "infinite_scroll"}, (f"{_AT}/pagination", "schema.enum")),
        ({"watermark": "always"}, (f"{_AT}/watermark", "schema.enum")),
        ({"max_depth": 0}, (f"{_AT}/max_depth", "schema.minimum")),
    ],
)
def test_capability_rules(
    write_yaml: WriteYaml, capabilities: dict[str, Any], expected: tuple[str, str] | None
) -> None:
    targets = {"svc": {"base_url": "https://svc.example", "capabilities": capabilities}}
    if expected is None:
        _load(write_yaml, targets)
    else:
        assert expected in _problems(write_yaml, {"targets": targets})


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"targets": {"Bad_Id": {}}}, ("/targets/Bad_Id", "schema.propertyNames")),
        ({"targets": {"svc\n": {}}}, ("/targets/svc\n", "schema.propertyNames")),  # no look-alike of svc
        (
            {"targets": {"svc": {"auth": {"kind": "header", "header": "X-Key\n", "secret_ref": "file:k"}}}},
            ("/targets/svc/auth/header", "schema.pattern"),
        ),
        ({"targets": {}}, ("/targets", "schema.minProperties")),
        ({"targets": {"svc": {"rate_per_s": 0}}}, ("/targets/svc/rate_per_s", "schema.exclusiveMinimum")),
        ({"targets": {"svc": {"max_concurrency": 1.5}}}, ("/targets/svc/max_concurrency", "schema.type")),
        (
            {"targets": {"svc": {"allow_private_network": "yes"}}},
            ("/targets/svc/allow_private_network", "schema.type"),
        ),
        ({"targets": {"svc": {}}, "defaults": {}}, ("/defaults", "schema.additionalProperties")),
        ({"services": {}}, ("", "schema.required")),
    ],
)
def test_document_rules(write_yaml: WriteYaml, document: dict[str, Any], expected: tuple[str, str]) -> None:
    assert expected in _problems(write_yaml, document)


def test_every_problem_is_reported_at_once(write_yaml: WriteYaml) -> None:
    targets = {
        "one": {"base_url": "https://user@one.example"},
        "two": {"base_url": "http://10.0.0.1", "capabilities": {"absence_shapes": {"x": "("}}},
    }
    assert _problems(write_yaml, {"targets": targets}) == {
        ("/targets/one/base_url", "invalid_base_url"),
        ("/targets/two/base_url", "private_network"),
        ("/targets/two/capabilities/absence_shapes/x", "invalid_cel"),
    }
    duplicate = write_yaml("dup.yaml", "targets:\n  svc: {}\n  svc: {}\n")
    with pytest.raises(TargetsError) as exc:
        load_targets(duplicate)
    assert [(i.pointer, i.code) for i in exc.value.issues] == [("/targets/svc", "yaml_duplicate_key")]


# -- credentials: file: references only, never the environment ------------------------------------


@pytest.mark.parametrize(
    ("content", "secret"), [(b"abc123", "abc123"), (b"abc\n", "abc"), (b"abc\r\n", "abc")]
)
def test_read_secret(tmp_path: Path, content: bytes, secret: str) -> None:
    (tmp_path / "token").write_bytes(content)
    assert read_secret(f"file:{tmp_path / 'token'}") == secret


@pytest.mark.parametrize(
    "content",
    [b"", b"\n", b"abc\n\n", b"line1\nline2", b"tab\tinside", b"\xff\xfe hunter2", b"del\x7f", b" pad "],
)
def test_read_secret_rejects_unusable_content_without_quoting_it(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "token"
    path.write_bytes(content)
    with pytest.raises(SecretError) as exc:
        read_secret(f"file:{path}")
    message = str(exc.value).replace(str(path), "<path>")
    assert not any(word in message for word in ("line1", "line2", "inside", "hunter2", "del", "pad"))
    assert exc.value.__context__ is None or exc.value.__suppress_context__  # no chained decode error


def test_read_secret_rejects_other_references_and_files(tmp_path: Path) -> None:
    for ref in ("env:API_TOKEN", "API_TOKEN", "$API_TOKEN", "vault:kv/x", "file:", "", "file:a\x00b", None):
        with pytest.raises(SecretError):
            read_secret(ref)  # type: ignore[arg-type]
    (tmp_path / "big").write_bytes(b"x" * (MAX_SECRET_BYTES + 1))
    os.mkfifo(tmp_path / "fifo")
    for name, reason in (
        ("big", "larger than"),
        ("absent", "cannot read"),
        ("", "not a regular file"),
        ("fifo", "not a regular file"),
    ):
        with pytest.raises(SecretError, match=reason):
            read_secret(f"file:{tmp_path / name}")  # a FIFO would block forever if opened


class _EnvironmentCanary(Mapping[str, str]):
    """Stands in for os.environ: any read fails the test."""

    def __getitem__(self, key: str) -> NoReturn:
        raise AssertionError(f"environment variable {key!r} was read")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("the environment was enumerated")

    def __len__(self) -> int:
        raise AssertionError("the environment was inspected")

    def __contains__(self, key: object) -> bool:
        raise AssertionError(f"environment variable {key!r} was probed")


def test_credentials_never_touch_the_environment(
    tmp_path: Path, write_yaml: WriteYaml, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "token").write_bytes(b"from-file\n")
    targets = write_yaml(
        "targets.yaml", {"targets": {"svc": {"auth": {"kind": "bearer", "secret_ref": "file:token"}}}}
    )

    def refuse(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("the environment was consulted")

    refused: list[str] = []
    with monkeypatch.context() as patch:  # restored before pytest reports (it reads COLUMNS)
        patch.setattr(os, "environ", _EnvironmentCanary())
        patch.setattr(os, "environb", _EnvironmentCanary())
        patch.setattr(os, "getenv", refuse)
        patch.setattr(os.path, "expanduser", refuse)
        patch.setattr(os.path, "expandvars", refuse)
        registry = load_targets(targets)
        value = read_secret(registry.get("svc").auth.secret_ref or "")
        for ref in ("env:HOME", "file:~/token", "file:$HOME/token"):
            with pytest.raises(SecretError) as exc:
                read_secret(ref)  # taken literally: no ~ or $VAR expansion, no env lookup
            refused.append(str(exc.value))
    assert value == "from-file"
    assert "~/token" in refused[1] and "$HOME/token" in refused[2]


FILES = {"engine": "files", "root": "/var/lib/deltasleuth/files/legacy"}
T8 = (
    "engine files needs root and takes only root, settle_s (0 to 86400), allow_writable, rate_per_s and"
    " max_concurrency"
)
T9 = "root must be an absolute path without . or .. segments or a trailing or doubled /, and not /"


def test_a_files_target_loads(write_yaml: WriteYaml) -> None:
    """A root, a settle time, the writable opt-in and a smaller rate budget by
    default; a database's capabilities and no secret."""
    registry = _load(write_yaml, {"legacy": FILES, "dev": FILES | {"settle_s": 0, "allow_writable": True}})
    legacy, dev = registry.live("legacy"), registry.get("dev")
    absence = TargetCapabilities(authoritative_absence=AuthoritativeAbsence(status=(404,)))
    assert legacy == Target(
        "legacy", "", rate_per_s=5.0, max_concurrency=2, capabilities=absence, root=FILES["root"]
    )  # fmt: skip
    assert (legacy.engine, legacy.settle_s, legacy.allow_writable) == ("files", 60, False)
    assert (dev.settle_s, dev.allow_writable, dev.database, dev.dsn_ref) == (0, True, None, None)
    budget = _load(write_yaml, {"legacy": FILES | {"rate_per_s": 50, "max_concurrency": 8}}).get("legacy")
    assert (budget.rate_per_s, budget.max_concurrency) == (50.0, 8)
    # Where a root may lie needs the settings: loading the targets never judges it.
    assert _load(write_yaml, {"etc": FILES | {"root": "/etc"}}).get("etc").root == "/etc"


@pytest.mark.parametrize(
    ("entry", "at", "code", "message"),
    [
        ({"engine": "files"}, "", "invalid_files_target", T8),
        (FILES | {"host": "db.example"}, "", "invalid_files_target", T8),
        (FILES | {"user": "reader", "password_ref": "file:p"}, "", "invalid_files_target", T8),
        (FILES | {"dsn_ref": "file:dsn"}, "", "invalid_files_target", T8),
        (FILES | {"base_url": "https://svc.example"}, "", "invalid_files_target", T8),
        (FILES | {"capabilities": {"pagination": "none"}}, "", "invalid_files_target", T8),
        (FILES | {"allow_private_network": True}, "", "invalid_files_target", T8),
        (FILES | {"settle_s": -1}, "", "invalid_files_target", T8),
        (FILES | {"settle_s": 86401}, "", "invalid_files_target", T8),
        (FILES | {"root": "exports/legacy"}, "/root", "invalid_root", T9),
        (FILES | {"root": "/"}, "/root", "invalid_root", T9),
        (FILES | {"root": "/var/lib/deltasleuth/files/../secrets"}, "/root", "invalid_root", T9),
        (FILES | {"root": "/var/lib/deltasleuth/files/./legacy"}, "/root", "invalid_root", T9),
        (FILES | {"root": "/var/lib/deltasleuth/files/.."}, "/root", "invalid_root", T9),
        (FILES | {"root": "/" + "a" * 1024}, "/root", "invalid_root", T9),
        # Not its own real path: a plan refused each as "is, or is reached through, a symbolic link".
        (FILES | {"root": "/var/lib/deltasleuth/files/legacy/"}, "/root", "invalid_root", T9),
        (FILES | {"root": "/var/lib/deltasleuth/files//legacy"}, "/root", "invalid_root", T9),
        (FILES | {"root": "//var/lib/deltasleuth/files/legacy"}, "/root", "invalid_root", T9),
    ],
    ids=[
        "no root", "host", "credentials", "dsn", "url", "capabilities", "private", "settle-", "settle+",
        "relative", "slash", "dotdot", "dot", "trailing dotdot", "long", "trailing slash", "doubled slash",
        "leading slashes",
    ],
)  # fmt: skip
def test_a_files_target_declared_wrongly_is_refused(
    write_yaml: WriteYaml, entry: dict[str, Any], at: str, code: str, message: str
) -> None:
    assert _refused(write_yaml, entry) == {(f"/targets/db{at}", code, message)}


@pytest.mark.parametrize(
    ("entry", "pointer", "code"),
    [
        (MYSQL | {"root": "/var/lib/deltasleuth/files/x"}, "/targets/db/root", "schema.not"),
        ({"base_url": "https://svc.example", "root": "/x"}, "/targets/db/root", "schema.not"),
        ({"dsn_ref": "file:dsn", "allow_writable": True}, "/targets/db/allow_writable", "schema.not"),
        (FILES | {"root": 7}, "/targets/db/root", "schema.type"),
        (FILES | {"settle_s": "60"}, "/targets/db/settle_s", "schema.type"),
        (FILES | {"engine": "parquet"}, "/targets/db/engine", "schema.enum"),
    ],
)
def test_files_members_belong_to_a_files_target(
    write_yaml: WriteYaml, entry: dict[str, Any], pointer: str, code: str
) -> None:
    assert (pointer, code) in _problems(write_yaml, {"targets": {"db": entry}})
