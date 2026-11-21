"""OIDC sign-in: ID token validation, discovery checks, the JWKS cache, the claims
mapping, the client credential and assertion, PKCE and the signed login cookie. No database: the IdP
client is driven directly against the fake IdP."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from pydantic import TypeAdapter

from deltasleuth.auth import oidc
from deltasleuth.auth.oidc import (
    DEFAULT_NEXT,
    Login,
    Oidc,
    SignInError,
    challenge,
    check_discovery,
    client_assertion,
    identity,
    load_credential,
    validate_id_token,
)
from deltasleuth.config import OidcSettings
from deltasleuth.context import login_key
from deltasleuth.core.secrets import SecretError, read_secret, read_secret_bytes

from .fake_idp import CLIENT_SECRET, ROLES, FakeIdp

NOW = int(time.time())


@pytest.fixture
def idp() -> Iterator[FakeIdp]:
    with FakeIdp() as server:
        yield server


def _settings(tmp_path: Path, issuer: str = "https://idp.example", **fields: Any) -> OidcSettings:
    (tmp_path / "secret").write_text(CLIENT_SECRET + "\n")
    base = {"issuer": issuer, "client_id": "deltasleuth", "client_secret_ref": f"file:{tmp_path / 'secret'}"}
    doc = {name: value for name, value in (base | {"roles": ROLES} | fields).items() if value is not None}
    return TypeAdapter(OidcSettings).validate_python(doc)


def _login(**fields: Any) -> Login:
    return replace(Login.new(None, reauth=False, replaces=None), nonce="nonce", **fields)


def _pem(key: Any) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def _certificate(key: Any) -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "deltasleuth")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


# -- the ID token ---------------------------------------------------------------------------


@pytest.mark.parametrize("alg", ["RS256", "ES256"])
def test_rs256_and_es256_tokens_pass(idp: FakeIdp, tmp_path: Path, alg: str) -> None:
    settings = _settings(tmp_path, idp.issuer, id_token_algs=["RS256", "ES256"])
    claims = validate_id_token(idp.id_token(headers={"alg": alg}), idp.jwks()["keys"], settings, _login())
    assert (claims["sub"], claims["aud"]) == ("alice-sub", "deltasleuth")


def test_a_token_without_kid_needs_exactly_one_fitting_key(idp: FakeIdp, tmp_path: Path) -> None:
    settings = _settings(tmp_path, idp.issuer)
    token = idp.id_token(headers={"kid": None})
    assert validate_id_token(token, idp.jwks()["keys"], settings, _login())["sub"] == "alice-sub"
    keys = idp.jwks()["keys"]
    idp.rotate()
    with pytest.raises(SignInError, match="2 JWKS keys fit"):
        validate_id_token(token, keys + idp.jwks()["keys"], settings, _login())


FOREIGN = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _defects(now: int) -> dict[str, dict[str, Any]]:
    """Each defect of an ID token, as ``id_token`` arguments (``keys`` changes the JWKS), at time ``now``."""
    return {
        "alg none": {"headers": {"alg": "none"}},
        "HS256 signed with the client secret": {"headers": {"alg": "HS256"}},
        "an alg not configured": {"headers": {"alg": "PS256"}},
        "a crit header": {"headers": {"crit": ["exp"]}},
        "an unknown kid": {"headers": {"kid": "stranger"}},
        "an EC key for RS256": {"headers": {"kid": "EC"}},
        "an encryption key": {"keys": {"use": "enc"}},
        "a key declaring another alg": {"keys": {"alg": "RS384"}},
        "a signature by another key": {"key": FOREIGN},
        "another audience": {"aud": "someone-else"},
        "several audiences without azp": {"aud": ["deltasleuth", "someone-else"]},
        "an azp for another client": {"azp": "someone-else"},
        "another issuer": {"iss": "https://evil.example"},
        "expired": {"exp": now - 120},
        "issued in the future": {"iat": now + 120},
        "issued too long ago": {"iat": now - 700},
        "no iss": {"iss": None},
        "no sub": {"sub": None},
        "no aud": {"aud": None},
        "no exp": {"exp": None},
        "no iat": {"iat": None},
        "an iat that is not a number": {"iat": "123"},
        "another nonce": {"nonce": "other"},
        "no nonce": {"nonce": None},
        "an empty subject": {"sub": ""},
        "a non-string subject": {"sub": 42},
        "a 200-character non-ASCII subject": {"sub": "é" * 200},  # 400 octets
        "no auth_time under reauth": {"auth_time": None, "reauth": True},
        "a stale auth_time under reauth": {"auth_time": now - 3600, "reauth": True},
    }


DEFECTS = list(_defects(0))


@pytest.mark.parametrize("defect", DEFECTS)
def test_every_id_token_defect_is_refused(idp: FakeIdp, tmp_path: Path, defect: str) -> None:
    now = int(time.time())
    case = _defects(now)[defect]
    login = _login(reauth=case.pop("reauth", False), started=now)
    keys = [key | case.pop("keys", {}) for key in idp.jwks()["keys"]]
    if case.get("headers", {}).get("kid") == "EC":
        case["headers"] = {"kid": idp.keys["ES256"][0]}
    token = idp.id_token(**case)
    with pytest.raises(SignInError) as refused:
        validate_id_token(token, keys, _settings(tmp_path, idp.issuer), login)
    assert refused.value.code == "login_failed"


def test_a_fresh_auth_time_passes_under_reauth(idp: FakeIdp, tmp_path: Path) -> None:
    now = int(time.time())
    login = _login(reauth=True, started=now)
    token = idp.id_token(auth_time=now - 30)  # within the 60 s clock skew
    assert validate_id_token(token, idp.jwks()["keys"], _settings(tmp_path, idp.issuer), login)


def test_a_subject_of_255_octets_passes_and_the_subject_claim_is_configurable(
    idp: FakeIdp, tmp_path: Path
) -> None:
    settings = _settings(tmp_path, idp.issuer, subject_claim="oid")
    keys = idp.jwks()["keys"]
    assert validate_id_token(idp.id_token(oid="é" * 127 + "x"), keys, settings, _login())
    with pytest.raises(SignInError, match="oid is not a string"):
        validate_id_token(idp.id_token(), keys, settings, _login())


# -- discovery ------------------------------------------------------------------------------


def _discovery(issuer: str = "https://idp.example") -> dict[str, Any]:
    return {
        "issuer": issuer,
        "authorization_endpoint": "https://login.idp.example/authorize",
        "token_endpoint": f"{issuer}/token",
        "jwks_uri": f"{issuer}/keys",
        "code_challenge_methods_supported": ["S256"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"issuer": "https://evil.example"}, "discovery issuer 'https://evil.example' differs from"
         " auth.oidc.issuer https://idp.example"),
        ({"authorization_endpoint": "http://login.idp.example/authorize"}, "authorization_endpoint"
         " 'http://login.idp.example/authorize' is not an https URL"),
        ({"end_session_endpoint": "http://idp.example/logout"}, "end_session_endpoint"),
        ({"token_endpoint": None}, "token_endpoint None is not an https URL"),
        ({"token_endpoint": "https://other.example/token"}, "token_endpoint 'https://other.example/token'"
         " is not at the issuer's scheme, host and port"),
        ({"jwks_uri": "https://idp.example:8443/keys"}, "jwks_uri 'https://idp.example:8443/keys' is not at"),
        ({"code_challenge_methods_supported": ["plain"]}, "code_challenge_methods_supported ['plain']"
         " admits none of S256"),
        ({"id_token_signing_alg_values_supported": ["HS256"]}, "admits none of RS256"),
        ({"token_endpoint_auth_methods_supported": ["private_key_jwt"]}, "admits none of"
         " client_secret_basic"),
    ],
)  # fmt: skip
def test_discovery_is_checked(tmp_path: Path, change: dict[str, Any], message: str) -> None:
    doc = {name: value for name, value in (_discovery() | change).items() if value is not None}
    with pytest.raises(SignInError, match=r"^discovery ") as refused:
        check_discovery(doc, _settings(tmp_path))
    assert refused.value.code == "idp_unavailable" and message in str(refused.value)


def test_discovery_passes_with_unlisted_support_and_http_for_a_loopback_issuer(
    idp: FakeIdp, tmp_path: Path
) -> None:
    unlisted = {name: value for name, value in _discovery().items() if not name.endswith("_supported")}
    assert check_discovery(unlisted, _settings(tmp_path)) == unlisted
    assert check_discovery(idp.discovery, _settings(tmp_path, idp.issuer))["issuer"] == idp.issuer


# -- the IdP client --------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _client(idp: FakeIdp, tmp_path: Path, **fields: Any) -> tuple[Oidc, Clock]:
    clock = Clock()
    return Oidc(_settings(tmp_path, idp.issuer, **fields), "https://ds.test", b"k" * 32, clock=clock), clock


async def _sign_in(client: Oidc, idp: FakeIdp, login: Login | None = None, **claims: Any) -> dict[str, Any]:
    login = login or Login.new(None, reauth=False, replaces=None)
    code = idp.authorize(await client.authorization_url(login), **claims)
    return await client.sign_in(code, login)


async def test_sign_in_asks_with_pkce_and_client_authentication(idp: FakeIdp, tmp_path: Path) -> None:
    client, _ = _client(idp, tmp_path)
    login = Login.new("#/runs", reauth=False, replaces=None)
    assert (await _sign_in(client, idp, login))["sub"] == "alice-sub"
    asked = idp.authorized[-1]
    assert asked == {
        "response_type": "code",
        "client_id": "deltasleuth",
        "redirect_uri": "https://ds.test/v1/auth/callback",
        "scope": "openid profile",
        "state": login.state,
        "nonce": login.nonce,
        "code_challenge": challenge(login.verifier),
        "code_challenge_method": "S256",
    }
    [token] = [r for r in idp.requests if r.path == "/token"]
    assert token.form["code_verifier"] == login.verifier and "client_secret" not in token.form
    assert token.headers["Authorization"].startswith("Basic ")


async def test_the_jwks_is_refetched_once_per_rotation(idp: FakeIdp, tmp_path: Path) -> None:
    client, _ = _client(idp, tmp_path)
    for _ in range(2):
        await _sign_in(client, idp)
    idp.rotate()
    for _ in range(2):
        await _sign_in(client, idp)
    assert idp.jwks_fetches == 2


async def test_an_unknown_kid_refetches_the_jwks_at_most_once_a_minute(idp: FakeIdp, tmp_path: Path) -> None:
    client, clock = _client(idp, tmp_path)
    stranger = {"key": rsa.generate_private_key(public_exponent=65537, key_size=2048)}
    stranger["headers"] = {"kid": "stranger"}
    fetches = []
    for step in (0, 30, 31):  # t = 0, 30 and 61 s
        clock.now += step
        with pytest.raises(SignInError, match="the JWKS has no key 'stranger'"):
            await _sign_in(client, idp, **stranger)
        fetches.append(idp.jwks_fetches)
    assert fetches == [2, 2, 3]  # the first fetch, then one refetch per 60 s


async def test_cached_keys_survive_a_failed_refresh(idp: FakeIdp, tmp_path: Path) -> None:
    client, clock = _client(idp, tmp_path)
    await _sign_in(client, idp)
    idp.discovery["jwks_uri"] = f"{idp.issuer}/gone"  # 404 once discovery is fetched again
    clock.now += 3601
    assert (await _sign_in(client, idp))["sub"] == "alice-sub"
    assert [r.path for r in idp.requests if r.path in ("/jwks", "/gone")] == ["/jwks", "/gone"]


async def test_an_unreachable_idp_gives_idp_unavailable(
    idp: FakeIdp, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client(idp, tmp_path)
    idp.down = True
    await client.start()
    with pytest.raises(SignInError) as down:
        await client.authorization_url(Login.new(None, reauth=False, replaces=None))
    assert "warning: identity provider unreachable at start; sign-in retries on first use" in caplog.messages
    idp.down = False
    login = Login.new(None, reauth=False, replaces=None)
    code = idp.authorize(await client.authorization_url(login))
    monkeypatch.setattr(oidc, "TIMEOUT", 0.5)  # an IdP slower than the timeout
    idp.slow = 11
    with pytest.raises(SignInError) as slow:
        await client.sign_in(code, login)
    assert (down.value.code, slow.value.code) == ("idp_unavailable", "idp_unavailable")
    assert str(slow.value).startswith("token endpoint unreachable (")


async def test_a_refused_exchange_logs_the_error_code_only(idp: FakeIdp, tmp_path: Path) -> None:
    client, _ = _client(idp, tmp_path)
    idp.client_secret = "another"  # noqa: S105 - the IdP expects another secret
    with pytest.raises(SignInError) as refused:
        await _sign_in(client, idp)
    assert (refused.value.code, str(refused.value)) == (
        "login_failed",
        "token endpoint answered HTTP 401 error='invalid_client'",
    )


async def test_an_oversized_answer_is_refused(
    idp: FakeIdp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client(idp, tmp_path)
    monkeypatch.setattr(oidc, "MAX_JSON", 100)
    with pytest.raises(SignInError, match=r"discovery unreachable \(ValueError\)"):
        await client.authorization_url(Login.new(None, reauth=False, replaces=None))


async def test_the_logout_url_follows_end_session(idp: FakeIdp, tmp_path: Path) -> None:
    client, _ = _client(idp, tmp_path)
    assert await client.logout_url() == (
        f"{idp.issuer}/logout?client_id=deltasleuth&post_logout_redirect_uri=https%3A%2F%2Fds.test%2F"
    )
    assert await _client(idp, tmp_path, end_session=False)[0].logout_url() is None
    del idp.discovery["end_session_endpoint"]
    assert await _client(idp, tmp_path)[0].logout_url() is None


# -- claims ---------------------------------------------------------------------------------


def _identity(tmp_path: Path, claims: dict[str, Any], **fields: Any) -> Any:
    base = {"iss": "https://idp.example", "sub": "alice-sub", "aud": "deltasleuth", "iat": NOW, "exp": NOW}
    return identity(base | claims, _settings(tmp_path, **fields))


@pytest.mark.parametrize(
    ("group_claims", "claims"),
    [
        (["groups"], {"groups": ["G-op"]}),
        (["roles"], {"roles": "G-op"}),  # a single string
        (["http://schemas.example/claims/groups"], {"http://schemas.example/claims/groups": ["G-op"]}),
        (["resource_access.deltasleuth.roles"], {"resource_access": {"deltasleuth": {"roles": ["G-op"]}}}),
        (
            [["resource_access", "deltasleuth.bank", "roles"]],
            {"resource_access": {"deltasleuth.bank": {"roles": ["G-op", 7, {"G-admin": 1}]}}},
        ),
        (["groups", "roles"], {"groups": 5, "roles": ["G-op"]}),  # other types are ignored
    ],
)
def test_every_group_claims_form_is_read(
    tmp_path: Path, group_claims: list[Any], claims: dict[str, Any]
) -> None:
    found = _identity(tmp_path, claims, group_claims=group_claims)
    assert (found.roles, found.groups) == ({"viewer", "operator"}, 1)


def test_groups_match_exactly_and_are_capped(tmp_path: Path) -> None:
    near = _identity(tmp_path, {"groups": ["ds.admin", "DS.Admin ", " G-admin", "g-admin"]})
    assert (near.roles, near.groups) == (frozenset(), 4)  # nothing is trimmed or case-folded
    many = _identity(tmp_path, {"groups": ["x" * 257, *(f"G-{i}" for i in range(1000)), "G-admin"]})
    assert (many.roles, many.groups) == (frozenset(), 1000)  # the long value and the 1,001st are dropped
    assert _identity(tmp_path, {"groups": ["G-view", "G-pii"]}).roles == {
        "viewer", "operator", "spec_author", "rule_approver", "pii_reader"
    }  # fmt: skip


@pytest.mark.parametrize(
    "claims", [{"_claim_names": {"groups": "src1"}, "groups": ["G-admin"]}, {"hasgroups": True}]
)
def test_a_groups_overage_leaves_the_roles_unknown(tmp_path: Path, claims: dict[str, Any]) -> None:
    assert _identity(tmp_path, claims).roles is None


def test_an_overage_names_a_path_s_first_segment(tmp_path: Path) -> None:
    keycloak = [["resource_access", "deltasleuth.bank", "roles"]]
    claims = {"_claim_names": {"resource_access": "src1"}}
    assert _identity(tmp_path, claims, group_claims=keycloak).roles is None
    assert _identity(tmp_path, {"_claim_names": {"other": "src1"}}).roles == frozenset()


def test_the_username_is_the_configured_claim_else_the_subject_and_never_email(tmp_path: Path) -> None:
    claims = {"preferred_username": "alice", "email": "alice@evil.example", "name": "Alice"}
    named = _identity(tmp_path, claims, username_claim="preferred_username")
    assert (named.username, named.display_name) == ("alice", "Alice")
    assert _identity(tmp_path, claims).username == "alice-sub"
    assert _identity(tmp_path, {"email": "a@evil.example"}, username_claim="upn").username == "alice-sub"
    long = _identity(tmp_path, {"upn": "u" * 300, "name": "n" * 300}, username_claim="upn")
    assert (len(long.username), len(long.display_name)) == (256, 256)


@pytest.mark.parametrize(
    "claims",
    [{"upn": "alice\x1b[31m"}, {"name": "Alice\x7f"}, {"name": "Alice\x85"}, {"upn": "\ud800"}],
)
def test_a_control_character_in_a_name_fails_the_sign_in(tmp_path: Path, claims: dict[str, Any]) -> None:
    with pytest.raises(SignInError) as refused:
        _identity(tmp_path, claims, username_claim="upn")
    assert refused.value.code == "login_failed"


def test_the_session_keeps_auth_time_acr_and_amr(tmp_path: Path) -> None:
    kept = _identity(tmp_path, {"auth_time": NOW, "acr": "mfa", "amr": ["pwd", "otp"]})
    assert (kept.auth_time, kept.acr, kept.amr) == (datetime.fromtimestamp(NOW, UTC), "mfa", ("pwd", "otp"))
    assert _identity(tmp_path, {"auth_time": "x", "amr": "pwd"}).amr is None


# -- the client credential ------------------------------------------------------------------


def _key_settings(tmp_path: Path, key: Any, cert: bytes | None = None) -> OidcSettings:
    (tmp_path / "client.key").write_bytes(_pem(key))
    fields: dict[str, Any] = {"client_auth": "private_key_jwt", "client_secret_ref": None}
    fields |= {"client_key_ref": f"file:{tmp_path / 'client.key'}", "client_key_id": "ds-2026"}
    if cert is not None:
        (tmp_path / "client.crt").write_bytes(cert)
        fields["client_cert_file"] = tmp_path / "client.crt"
    return _settings(tmp_path, **fields)


@pytest.mark.parametrize(
    "key",
    [rsa.generate_private_key(public_exponent=65537, key_size=2048), ec.generate_private_key(ec.SECP256R1())],
)
def test_the_client_assertion_names_the_key_and_the_certificate(tmp_path: Path, key: Any) -> None:
    cert = _certificate(key)
    credential = load_credential(_key_settings(tmp_path, key, cert))
    assertion = client_assertion(_key_settings(tmp_path, key, cert), credential, "https://idp.example/token")
    header = jwt.get_unverified_header(assertion)
    der = x509.load_pem_x509_certificate(cert).public_bytes(serialization.Encoding.DER)
    assert header["kid"] == "ds-2026" and header["alg"] == (
        "RS256" if isinstance(key, rsa.RSAPrivateKey) else "ES256"
    )
    assert header["x5t#S256"] == oidc._b64(hashlib.sha256(der).digest())
    assert header["x5t"] == oidc._b64(hashlib.sha1(der, usedforsecurity=False).digest())
    claims = jwt.decode(
        assertion, key.public_key(), algorithms=[header["alg"]], audience="https://idp.example/token"
    )
    assert claims["iss"] == claims["sub"] == "deltasleuth" and claims["exp"] == claims["iat"] + 60
    assert len(oidc._unb64(claims["jti"])) == 16


@pytest.mark.parametrize(
    "key",
    [
        rsa.generate_private_key(public_exponent=65537, key_size=1024),  # noqa: S505 - refused, as it should be
        ec.generate_private_key(ec.SECP384R1()),
    ],
)
def test_a_weak_or_unsupported_client_key_is_refused(tmp_path: Path, key: Any) -> None:
    with pytest.raises(ValueError) as refused:
        load_credential(_key_settings(tmp_path, key))
    assert str(refused.value) == (
        f"auth.oidc.client_key_ref: {tmp_path / 'client.key'} is not a PEM RSA (>= 2048 bits) or EC P-256"
        " private key"
    )


def test_a_certificate_of_another_key_is_refused(tmp_path: Path) -> None:
    key, other = (ec.generate_private_key(ec.SECP256R1()) for _ in range(2))
    with pytest.raises(ValueError) as refused:
        load_credential(_key_settings(tmp_path, key, _certificate(other)))
    assert str(refused.value) == (
        f"auth.oidc.client_cert_file: {tmp_path / 'client.crt'} is not a PEM certificate of"
        " client_key_ref's key"
    )


def test_a_missing_secret_file_is_named(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), client_secret_ref=f"file:{tmp_path / 'absent'}")
    with pytest.raises(SecretError, match=f"^cannot read secret file {tmp_path / 'absent'}: "):
        Oidc(settings, "https://ds.test", b"k" * 32)


def test_a_pem_is_read_as_bytes_and_refused_as_a_one_line_secret(tmp_path: Path) -> None:
    pem = _pem(ec.generate_private_key(ec.SECP256R1()))
    (tmp_path / "key.pem").write_bytes(pem)
    assert read_secret_bytes(f"file:{tmp_path / 'key.pem'}") == pem
    with pytest.raises(SecretError, match="line breaks"):
        read_secret(f"file:{tmp_path / 'key.pem'}")


# -- PKCE, next and the login cookie ---------------------------------------------------------------


def test_pkce_matches_rfc_7636_appendix_b() -> None:
    assert (
        challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_a_login_holds_fresh_random_values() -> None:
    first, second = (Login.new("#/runs/R-1", reauth=True, replaces="W-0123456789abcdef") for _ in range(2))
    assert [len(oidc._unb64(v)) for v in (first.state, first.nonce, first.verifier)] == [32, 32, 48]
    assert first.state != second.state and first.exp - first.started == 600
    assert (first.next, first.reauth, first.replaces) == ("#/runs/R-1", True, "W-0123456789abcdef")


@pytest.mark.parametrize(
    "evil",
    ["https://evil.example", "//evil.example", "#/runs\n", "#runs", "#/<script>", "#/" + "a" * 501, ""],
)
def test_a_next_that_is_not_a_hash_route_becomes_runs(evil: str) -> None:
    assert Login.new(evil, reauth=False, replaces=None).next == DEFAULT_NEXT


def test_the_login_cookie_round_trips_and_refuses_a_changed_byte_or_expiry() -> None:
    key = login_key(b"t" * 32)
    login = Login.new("#/runs/R-1?tab=keys&q=a+b*c'(d)!~%2F", reauth=True, replaces=None)
    sealed = login.seal(key)
    assert sealed.startswith("v1.") and Login.unseal(sealed, key) == login
    assert login.next == "#/runs/R-1?tab=keys&q=a+b*c'(d)!~%2F"
    changed = [
        sealed[:i] + ("A" if sealed[i] != "A" else "B") + sealed[i + 1 :] for i in (3, len(sealed) - 1)
    ]
    assert [Login.unseal(c, key) for c in changed] == [None, None]
    assert Login.unseal(sealed, login_key(b"u" * 32)) is None
    assert Login.unseal(replace(login, exp=int(time.time()) - 1).seal(key), key) is None
    assert Login.unseal(None, key) is None and Login.unseal("v2." + sealed[3:], key) is None


def test_the_login_key_is_derived_from_the_tokeniser_key() -> None:
    assert login_key(b"t" * 32) == login_key(b"t" * 32) != login_key(b"u" * 32)
    assert len(login_key(b"t" * 32)) == 32
