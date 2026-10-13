"""OIDC sign-in: the IdP client, ID token validation, the claims mapping and the
signed login cookie.

DeltaSleuth is a confidential relying party using the code flow with PKCE S256, ``state`` and
``nonce``. The IdP is reached at the issuer's host from settings, and its tokens are dropped once the
identity is known. A failure before then raises ``SignInError``: its ``code`` is one of the fixed codes
the browser is sent, and its message, for the server log, never reaches the browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import ssl
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote, urlencode, urlsplit

import aiohttp
import jwt
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from deltasleuth.auth.principal import expand, printable
from deltasleuth.config import LOOPBACK, OidcSettings
from deltasleuth.core.secrets import read_secret, read_secret_bytes

LOGIN_COOKIE = "__Host-ds_login"  # under https; sessions.named(LOGIN_COOKIE, auth) is the name set and read
LOGIN_SECONDS = 600
NEXT = re.compile(r"#/[A-Za-z0-9/_.~%?&=+*!'()-]{0,500}")  # what web/src/lib.ts href() produces
DEFAULT_NEXT = "#/runs"
TIMEOUT = 10  # seconds per IdP call
MAX_JSON = 2**20  # bytes of an IdP answer
CACHE_SECONDS = 3600  # discovery and JWKS
REFETCH_SECONDS = 60  # an unknown kid refetches the JWKS at most once in this window
FRESH_SECONDS = 600  # an ID token's iat is at most this old (plus the clock skew)
MAX_SUBJECT_OCTETS = 255
MAX_NAME = 256  # characters of a username, display name or group value
MAX_GROUPS = 1000
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
_REQUIRED = ["iss", "sub", "aud", "exp", "iat"]
_CURVES = {"ES256": "P-256", "ES384": "P-384", "ES512": "P-521"}
log = logging.getLogger("deltasleuth.auth")


class SignInError(Exception):
    """A sign-in that failed before the identity was known: ``code`` is what the browser is told."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code


class UnknownKey(SignInError):
    """The ID token names a key the JWKS at hand lacks: a refetch may find it."""


def _refuse(reason: str) -> NoReturn:
    raise SignInError("login_failed", reason)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _shown(value: Any) -> str:
    """An IdP value for the log: quoted and cut, so it cannot forge a line."""
    return repr(value)[:200]


def _utf8(text: Any) -> bytes | None:
    """``text`` as UTF-8; None for a non-string or a lone surrogate, which PostgreSQL cannot store."""
    try:
        return text.encode() if isinstance(text, str) else None
    except UnicodeEncodeError:
        return None


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def challenge(verifier: str) -> str:
    """The PKCE S256 code challenge of ``verifier`` (RFC 7636)."""
    return _b64(hashlib.sha256(verifier.encode("ascii")).digest())


# -- the login cookie ------------------------------------------------------------------------------


def _mac(key: bytes, payload: str) -> str:
    return _b64(hmac.new(key, ("v1." + payload).encode(), hashlib.sha256).digest())


@dataclass(frozen=True)
class Login:
    """A sign-in in progress, carried by the signed ``__Host-ds_login`` cookie (``ds_login_<port>`` on a
    plain-http loopback ``public_url``)."""

    state: str
    nonce: str
    verifier: str  # the PKCE code verifier
    next: str  # the hash route to land on
    reauth: bool
    started: int  # epoch seconds
    exp: int
    replaces: str | None  # the session presented at /login, revoked once this sign-in succeeds

    @classmethod
    def new(cls, next_: str | None, *, reauth: bool, replaces: str | None) -> Login:
        started = int(time.time())
        return cls(
            state=_b64(secrets.token_bytes(32)),
            nonce=_b64(secrets.token_bytes(32)),
            verifier=_b64(secrets.token_bytes(48)),
            next=next_ if next_ is not None and NEXT.fullmatch(next_) else DEFAULT_NEXT,
            reauth=reauth,
            started=started,
            exp=started + LOGIN_SECONDS,
            replaces=replaces,
        )

    def seal(self, key: bytes) -> str:
        """The cookie value: ``v1.<payload>.<mac>``."""
        payload = _b64(json.dumps(asdict(self), separators=(",", ":")).encode())
        return f"v1.{payload}.{_mac(key, payload)}"

    @classmethod
    def unseal(cls, cookie: str | None, key: bytes) -> Login | None:
        """The sign-in a cookie carries; None when it is missing, altered or expired."""
        version, _, rest = (cookie or "").partition(".")
        payload, _, mac = rest.partition(".")
        if version != "v1" or not hmac.compare_digest(mac.encode(), _mac(key, payload).encode()):
            return None
        login = cls(**json.loads(_unb64(payload)))  # signed by us: well-formed
        return login if login.exp > time.time() else None


# -- the client credential -------------------------------------------------------------------------


@dataclass(frozen=True)
class Credential:
    """A client secret, or a private key with its signing algorithm and, given a certificate, the
    certificate's thumbprints."""

    secret: str | None = None
    key: Any = None
    algorithm: str | None = None
    thumbprints: Mapping[str, str] = field(default_factory=dict)


def load_credential(settings: OidcSettings) -> Credential:
    """Read the client credential. It is read at server start and at every exchange, so it rotates
    without a restart. Errors (SecretError, ValueError) name the file, never its content."""
    if settings.client_auth != "private_key_jwt":
        return Credential(secret=read_secret(str(settings.client_secret_ref)))
    ref = str(settings.client_key_ref)
    data = read_secret_bytes(ref)  # PEM spans lines, which read_secret refuses
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):  # TypeError: an encrypted key
        key = None
    if isinstance(key, rsa.RSAPrivateKey) and key.key_size >= 2048:
        algorithm = "RS256"
    elif isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1):
        algorithm = "ES256"
    else:
        path = ref.removeprefix("file:")
        raise ValueError(
            f"auth.oidc.client_key_ref: {path} is not a PEM RSA (>= 2048 bits) or EC P-256 private key"
        )
    cert = settings.client_cert_file
    return Credential(
        key=key, algorithm=algorithm, thumbprints={} if cert is None else _thumbprints(cert, key)
    )


def _thumbprints(path: Path, key: Any) -> dict[str, str]:
    """``x5t`` and ``x5t#S256`` of the certificate at ``path``, which must certify ``key``."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"auth.oidc.client_cert_file: cannot read {path}: {exc.strerror}") from None
    try:
        cert = x509.load_pem_x509_certificate(data)
        certified = cert.public_key().public_bytes(serialization.Encoding.DER, _SPKI)
    except (ValueError, UnsupportedAlgorithm):
        certified = None
    if certified != key.public_key().public_bytes(serialization.Encoding.DER, _SPKI):
        raise ValueError(
            f"auth.oidc.client_cert_file: {path} is not a PEM certificate of client_key_ref's key"
        )
    der = cert.public_bytes(serialization.Encoding.DER)
    return {
        "x5t": _b64(hashlib.sha1(der, usedforsecurity=False).digest()),
        "x5t#S256": _b64(hashlib.sha256(der).digest()),
    }


_SPKI = serialization.PublicFormat.SubjectPublicKeyInfo


def client_assertion(settings: OidcSettings, credential: Credential, audience: str) -> str:
    """The private_key_jwt assertion: valid for 60 s, for the token endpoint only."""
    iat = int(time.time())
    claims = {
        "iss": settings.client_id,
        "sub": settings.client_id,
        "aud": audience,
        "jti": _b64(secrets.token_bytes(16)),
        "iat": iat,
        "exp": iat + 60,
    }
    headers = dict(credential.thumbprints)
    if settings.client_key_id is not None:
        headers["kid"] = settings.client_key_id
    return jwt.encode(claims, credential.key, algorithm=credential.algorithm, headers=headers)


# -- discovery -------------------------------------------------------------------------------------


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None
    return parts.scheme, (parts.hostname or "").lower(), port


def check_discovery(doc: Mapping[str, Any], settings: OidcSettings) -> dict[str, Any]:
    """``doc`` when it passes the discovery checks; else SignInError naming the values."""

    def refuse(reason: str) -> NoReturn:
        raise SignInError("idp_unavailable", f"discovery {reason}")

    if doc.get("issuer") != settings.issuer:
        refuse(f"issuer {_shown(doc.get('issuer'))} differs from auth.oidc.issuer {settings.issuer}")
    loopback = urlsplit(settings.issuer).hostname in LOOPBACK
    for name in ("authorization_endpoint", "token_endpoint", "jwks_uri", "end_session_endpoint"):
        url = doc.get(name)
        if url is None and name == "end_session_endpoint":
            continue
        origin = _origin(url) if isinstance(url, str) else None
        if (
            origin is None
            or not origin[1]
            or origin[0] not in (("https", "http") if loopback else ("https",))
        ):
            refuse(f"{name} {_shown(url)} is not an https URL")
    for name in ("token_endpoint", "jwks_uri"):
        if _origin(doc[name]) != _origin(settings.issuer):
            refuse(f"{name} {_shown(doc[name])} is not at the issuer's scheme, host and port")

    def admits(name: str, wanted: Iterable[str]) -> None:
        listed = doc.get(name)
        strings = isinstance(listed, list) and all(isinstance(v, str) for v in listed)
        if listed is not None and not (strings and set(wanted) & set(listed)):
            refuse(f"{name} {_shown(listed)} admits none of {', '.join(wanted)}")

    admits("code_challenge_methods_supported", ["S256"])
    admits("id_token_signing_alg_values_supported", settings.id_token_algs)
    admits("token_endpoint_auth_methods_supported", [settings.client_auth])
    return dict(doc)


# -- the ID token ---------------------------------------------------------------------------


def _fits(jwk: Mapping[str, Any], alg: str) -> bool:
    """``jwk`` may verify ``alg``: a signing key of the right type and curve, declaring no other alg."""
    kty, crv = ("EC", _CURVES[alg]) if alg.startswith("ES") else ("RSA", None)
    return (
        jwk.get("use", "sig") == "sig"
        and jwk.get("kty") == kty
        and jwk.get("crv") == crv
        and jwk.get("alg", alg) == alg
    )


def fitting(jwk: Mapping[str, Any], algs: Iterable[str]) -> list[str]:
    """The algorithms of ``algs`` that ``jwk`` may verify ID tokens with."""
    return [alg for alg in algs if _fits(jwk, alg)]


def _key(header: Mapping[str, Any], keys: Iterable[Mapping[str, Any]], alg: str) -> Any:
    kid = header.get("kid")
    named = list(keys) if kid is None else [k for k in keys if k.get("kid") == kid]
    if kid is not None and not named:
        raise UnknownKey("login_failed", f"the JWKS has no key {_shown(kid)}")
    fitting = [k for k in named if _fits(k, alg)]
    if len(fitting) != 1:  # without a kid, exactly one key may fit
        _refuse(f"{len(fitting)} JWKS keys fit kid {_shown(kid)} and alg {alg}")
    try:
        return jwt.PyJWK(dict(fitting[0]), algorithm=alg).key
    except (jwt.PyJWTError, TypeError, ValueError, KeyError) as exc:
        _refuse(f"the JWKS key {_shown(kid)} is unusable ({type(exc).__name__})")


def validate_id_token(
    token: str, keys: Iterable[Mapping[str, Any]], settings: OidcSettings, login: Login
) -> dict[str, Any]:
    """The claims of a valid ID token; SignInError(login_failed) for any defect, UnknownKey when it
    names a key ``keys`` lacks. No I/O: the caller fetches the keys."""
    skew = settings.clock_skew_seconds
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        _refuse("the ID token is malformed")
    alg = header.get("alg")
    if alg not in settings.id_token_algs:  # so never none or HS*: settings cannot name them
        _refuse(f"alg {_shown(alg)} is not in auth.oidc.id_token_algs")
    if "crit" in header:
        _refuse("the ID token has a crit header")
    key = _key(header, keys, alg)  # jku, x5u and jwk are never read
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=[alg],
            audience=settings.client_id,
            issuer=settings.issuer,
            leeway=skew,
            options={"require": _REQUIRED},
        )
    except (jwt.PyJWTError, TypeError, ValueError, OverflowError) as exc:
        _refuse(f"the ID token is invalid ({type(exc).__name__})")
    audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if (len(audiences) > 1 or "azp" in claims) and claims.get("azp") != settings.client_id:
        _refuse("azp is not auth.oidc.client_id")
    if not _number(claims["iat"]) or claims["iat"] < time.time() - FRESH_SECONDS - skew:
        _refuse("the ID token was issued too long ago")
    nonce = _utf8(claims.get("nonce"))
    if nonce is None or not hmac.compare_digest(nonce, login.nonce.encode()):
        _refuse("nonce differs from the login's")
    subject = _utf8(claims.get(settings.subject_claim))
    if not subject or len(subject) > MAX_SUBJECT_OCTETS or b"\x00" in subject:
        _refuse(f"{settings.subject_claim} is not a string of 1 to {MAX_SUBJECT_OCTETS} UTF-8 octets")
    if login.reauth:
        auth_time = claims.get("auth_time")
        if not _number(auth_time) or auth_time < login.started - skew:
            _refuse("auth_time is missing or older than the sign-in that asked for it")
    return claims


# -- claims ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """Who signed in, as the claims map them."""

    subject: str
    username: str  # audited as actor_name
    display_name: str | None
    roles: frozenset[str] | None  # None: a groups overage, which tells nothing of the roles
    groups: int  # how many group values were read
    claim_names: tuple[str, ...]  # for the log of a denial: names, never values
    auth_time: datetime | None
    acr: str | None
    amr: tuple[str, ...] | None


def _at(claims: Mapping[str, Any], path: str | tuple[str, ...]) -> Any:
    """The value at a group path: a string is tried as a whole claim name, then split on dots."""
    if isinstance(path, str):
        if path in claims:
            return claims[path]
        path = tuple(path.split("."))
    value: Any = claims
    for segment in path:
        if not isinstance(value, dict) or segment not in value:
            return None
        value = value[segment]
    return value


def group_values(claims: Mapping[str, Any], paths: Iterable[str | tuple[str, ...]]) -> list[str]:
    """The strings at every group path, as given (never trimmed or case-folded): at most 1,000 values
    of at most 256 characters."""
    values: list[str] = []
    for path in paths:
        found = _at(claims, path)
        if isinstance(found, str):
            values.append(found)
        elif isinstance(found, list):  # other types are ignored
            values += [value for value in found if isinstance(value, str)]
    return [value for value in values if len(value) <= MAX_NAME][:MAX_GROUPS]


def _overage(claims: Mapping[str, Any], paths: Iterable[str | tuple[str, ...]]) -> bool:
    """The IdP left the groups out (too many for a token); Microsoft Graph is never called."""
    names = claims.get("_claim_names")
    firsts = set()  # a path's first segment (and a string path whole: it may be a URI name)
    for path in paths:
        firsts |= {path, path.split(".")[0]} if isinstance(path, str) else {path[0]}
    return claims.get("hasgroups") is True or (isinstance(names, dict) and not firsts.isdisjoint(names))


def _name(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if not printable(value) or _utf8(value) is None:  # no terminal escapes, no look-alike names
        _refuse("a name claim holds a control character")
    return value[:MAX_NAME]


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(value, UTC) if _number(value) else None
    except (OverflowError, OSError, ValueError):
        return None


def identity(claims: Mapping[str, Any], settings: OidcSettings) -> Identity:
    """Map a valid ID token's claims (``validate_id_token``'s); SignInError(login_failed) for a name
    with a control character."""
    subject = claims[settings.subject_claim]
    username = _name(claims.get(settings.username_claim)) if settings.username_claim else None  # never email
    groups = group_values(claims, settings.group_claims)
    held = set(groups)
    roles = expand(role for role, ids in settings.roles.items() if not held.isdisjoint(ids))
    amr = claims.get("amr")
    return Identity(
        subject=subject,
        username=username or _name(subject) or subject,  # the subject is a name then, checked as one
        display_name=_name(claims.get("name")),
        roles=None if _overage(claims, settings.group_claims) else roles,
        groups=len(groups),
        claim_names=tuple(sorted(claims)),
        auth_time=_time(claims.get("auth_time")),
        acr=claims["acr"] if isinstance(claims.get("acr"), str) else None,
        amr=tuple(amr) if isinstance(amr, list) and all(isinstance(m, str) for m in amr) else None,
    )


# -- the IdP client --------------------------------------------------------------------------------


async def _read(response: aiohttp.ClientResponse) -> bytes:
    """The body, at most MAX_JSON bytes (ValueError beyond)."""
    if response.content_length is not None and response.content_length > MAX_JSON:
        raise ValueError(f"an answer over {MAX_JSON} bytes")
    chunks, total = [], 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > MAX_JSON:
            raise ValueError(f"an answer over {MAX_JSON} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _offset(date: str | None) -> float | None:
    """How many seconds the clock behind an HTTP ``Date`` header is ahead of ours; None without one.
    A Date is cut to the second, so it is taken as the middle of its second."""
    try:
        return parsedate_to_datetime(date).timestamp() + 0.5 - time.time() if date else None
    except (TypeError, ValueError):
        return None


def _object(body: bytes) -> dict[str, Any]:
    doc = json.loads(body)
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    return doc


_UNREACHABLE = (aiohttp.ClientError, TimeoutError, OSError, ValueError, RecursionError)


class Oidc:
    """Oidc mode's IdP client: the discovery and JWKS caches and the login cookie's key.

    One aiohttp session per sign-in, taking nothing from the environment (no proxy or netrc), with a
    10 s timeout, no redirects and the system CA store. Building one reads the client credential, so
    a missing or bad credential stops the server's start.
    """

    def __init__(
        self,
        settings: OidcSettings,
        public_url: str,
        login_key: bytes,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings, self.public_url, self.login_key, self.clock = settings, public_url, login_key, clock
        self.redirect_uri = f"{public_url}/v1/auth/callback"
        self.post_logout_redirect_uri = f"{public_url}/"
        load_credential(settings)
        self._ssl = ssl.create_default_context()
        self._discovery: tuple[float, dict[str, Any]] | None = None  # (fetched at, document)
        self._jwks: tuple[float, list[dict[str, Any]]] | None = None  # (fetched at, keys)
        self._refetched: float | None = None  # when an unknown kid last refetched the JWKS
        self.date_offset: float | None = None  # the IdP's clock ahead of ours at its last answer (s)

    def _client(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=self._ssl),
            timeout=aiohttp.ClientTimeout(total=TIMEOUT),
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
            headers={"Accept": "application/json"},
        )

    async def start(self) -> None:
        """Fetch discovery as the server starts; without it the server starts all the same."""
        try:
            async with self._client() as http:
                await self._metadata(http)
        except SignInError as exc:
            log.warning("warning: identity provider unreachable at start; sign-in retries on first use")
            log.warning("identity provider: %s", exc)

    async def check(self) -> list[dict[str, Any]]:
        """For ``ds auth check``: fetch discovery (checked) and the JWKS now; the JWKS keys.
        SignInError names a failed check."""
        async with self._client() as http:
            return await self._keys(http, (await self._metadata(http))["jwks_uri"])

    async def authorization_url(self, login: Login) -> str:
        async with self._client() as http:
            endpoint = (await self._metadata(http))["authorization_endpoint"]
        query = {
            "response_type": "code",
            "client_id": self.settings.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.settings.scopes),
            "state": login.state,
            "nonce": login.nonce,
            "code_challenge": challenge(login.verifier),
            "code_challenge_method": "S256",
        }
        if login.reauth:  # the IdP authenticates again, whatever session it holds
            query |= {"prompt": "login", "max_age": "0"}
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(query)}"

    async def sign_in(self, code: str, login: Login) -> dict[str, Any]:
        """Exchange ``code`` and return the validated ID token's claims."""
        async with self._client() as http:
            metadata = await self._metadata(http)
            id_token = await self._exchange(http, metadata["token_endpoint"], code, login.verifier)
            keys = await self._keys(http, metadata["jwks_uri"])
            try:
                return validate_id_token(id_token, keys, self.settings, login)
            except UnknownKey:  # the IdP may have rotated its keys
                keys = await self._keys(http, metadata["jwks_uri"], unknown=True)
                return validate_id_token(id_token, keys, self.settings, login)

    async def logout_url(self) -> str | None:
        """Where the browser signs out of the IdP; None without ``end_session`` or an endpoint."""
        if not self.settings.end_session:
            return None
        try:
            async with self._client() as http:
                endpoint = (await self._metadata(http)).get("end_session_endpoint")
        except SignInError:  # a failed refresh: the document last checked still names the endpoint
            endpoint = self._discovery[1].get("end_session_endpoint") if self._discovery else None
        if endpoint is None:
            return None
        query = urlencode(
            {"client_id": self.settings.client_id, "post_logout_redirect_uri": self.post_logout_redirect_uri}
        )
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    async def _get(self, http: aiohttp.ClientSession, what: str, url: str) -> dict[str, Any]:
        """A JSON object from the IdP; SignInError(idp_unavailable) for anything else."""
        try:
            async with http.get(url, allow_redirects=False, proxy=self.settings.proxy) as response:
                self.date_offset = _offset(response.headers.get("Date"))
                if response.status != 200:
                    raise SignInError("idp_unavailable", f"{what} answered HTTP {response.status}")
                return _object(await _read(response))
        except _UNREACHABLE as exc:
            raise SignInError("idp_unavailable", f"{what} unreachable ({type(exc).__name__})") from None

    async def _metadata(self, http: aiohttp.ClientSession) -> dict[str, Any]:
        """The discovery document, checked, cached for an hour."""
        if self._discovery is None or self.clock() - self._discovery[0] >= CACHE_SECONDS:
            url = self.settings.issuer.rstrip("/") + "/.well-known/openid-configuration"
            doc = check_discovery(await self._get(http, "discovery", url), self.settings)
            self._discovery = (self.clock(), doc)
        return self._discovery[1]

    async def _keys(
        self, http: aiohttp.ClientSession, url: str, *, unknown: bool = False
    ) -> list[dict[str, Any]]:
        """The JWKS, cached for an hour. An unknown kid refetches it at most once per 60 s, and cached
        keys survive a failed refresh."""
        now, cached = self.clock(), self._jwks
        stale = cached is None or now - cached[0] >= CACHE_SECONDS
        if unknown and (self._refetched is None or now - self._refetched >= REFETCH_SECONDS):
            self._refetched, stale = now, True
        if not stale and cached is not None:
            return cached[1]
        try:
            doc = await self._get(http, "jwks", url)
        except SignInError:
            if cached is None:
                raise
            return cached[1]
        listed = doc.get("keys")
        keys = [key for key in listed if isinstance(key, dict)] if isinstance(listed, list) else []
        self._jwks = (now, keys)
        return keys

    async def _exchange(self, http: aiohttp.ClientSession, url: str, code: str, verifier: str) -> str:
        """The ID token for ``code``; the access and refresh tokens are dropped unread."""
        settings = self.settings
        try:
            credential = load_credential(settings)
        except ValueError as exc:  # SecretError too: the file names its problem, never its content
            raise SignInError("login_failed", str(exc)) from None
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
        }
        headers = {}
        if settings.client_auth == "client_secret_basic":
            pair = f"{quote(settings.client_id, safe='')}:{quote(str(credential.secret), safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        elif settings.client_auth == "client_secret_post":
            form |= {"client_id": settings.client_id, "client_secret": str(credential.secret)}
        else:
            form |= {
                "client_id": settings.client_id,
                "client_assertion_type": ASSERTION_TYPE,
                "client_assertion": client_assertion(settings, credential, url),
            }
        try:
            async with http.post(
                url, data=form, headers=headers, allow_redirects=False, proxy=settings.proxy
            ) as response:
                status, body = response.status, await _read(response)
        except _UNREACHABLE as exc:
            reason = f"token endpoint unreachable ({type(exc).__name__})"
            raise SignInError("idp_unavailable", reason) from None
        try:
            doc = _object(body)
        except (ValueError, RecursionError):
            doc = {}
        if status != 200:  # only error is logged: error_description is IdP text
            reason = f"token endpoint answered HTTP {status} error={_shown(doc.get('error'))}"
            raise SignInError("login_failed", reason)
        id_token = doc.get("id_token")
        if not isinstance(id_token, str):
            raise SignInError("login_failed", "token endpoint answered without an id_token")
        return id_token
