"""A fake OIDC identity provider: a ThreadingHTTPServer on 127.0.0.1:0, issuer
``http://127.0.0.1:<port>``, with discovery, JWKS, token and logout endpoints and RSA-2048 and EC P-256
signing keys made per instance.

The token endpoint checks a one-time code, the exact redirect_uri, PKCE and the configured client
authentication. A test turns the controls: ``authorize`` (any claim or header of the ID token),
``rotate``, ``down``, ``slow``, ``date_offset``, and reads what was asked in ``requests``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self
from urllib.parse import parse_qsl, unquote_plus, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

CLIENT_ID = "deltasleuth"
CLIENT_SECRET = "client-s3cret"  # noqa: S105 - a test value, what open_client writes to its oidc_secret file
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ROLES = {
    "viewer": ["G-view"],
    "operator": ["G-op"],
    "spec_author": ["G-author"],
    "rule_approver": ["G-approver"],
    "pii_reader": ["G-pii"],
    "admin": ["G-admin"],
}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwk(kid: str, key: Any, alg: str) -> dict[str, Any]:
    public = key.public_key()
    jwk = (ECAlgorithm if alg.startswith("ES") else RSAAlgorithm).to_jwk(public, as_dict=True)
    return jwk | {"kid": kid, "use": "sig", "alg": alg}


@dataclass(frozen=True)
class Seen:
    """A request the IdP received."""

    method: str
    path: str
    headers: dict[str, str]
    form: dict[str, str]


class FakeIdp:
    def __init__(self) -> None:
        self.client_id, self.client_secret = CLIENT_ID, CLIENT_SECRET
        self.client_auth = "client_secret_basic"
        self.client_public_key: Any = None  # private_key_jwt's
        self.down = False  # no answer at all: the IdP is unreachable
        self.slow = 0.0  # seconds before each answer
        self.date_offset = 0  # seconds the IdP's clock is ahead: its Date header and token times
        self.requests: list[Seen] = []
        self.jwks_fetches = 0
        self.authorized: list[dict[str, str]] = []  # the query of each authorize: prompt, max_age, ...
        self.assertions: list[dict[str, Any]] = []  # the header of each client assertion
        self.issued: list[dict[str, Any]] = []  # each token endpoint answer: access, refresh, ID token
        self._codes: dict[str, dict[str, str]] = {}
        self._jtis: set[str] = set()
        self._stopping = threading.Event()
        self.rotate()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.idp = self  # type: ignore[attr-defined]
        self.issuer = f"http://127.0.0.1:{self._server.server_address[1]}"
        self.discovery: dict[str, Any] = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
            "end_session_endpoint": f"{self.issuer}/logout",
            "response_types_supported": ["code"],
            "id_token_signing_alg_values_supported": ["RS256", "ES256"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
                "private_key_jwt",
            ],
        }

    def __enter__(self) -> Self:
        serve = {"poll_interval": 0.01}  # shutdown waits for one poll
        threading.Thread(target=self._server.serve_forever, kwargs=serve, daemon=True).start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stopping.set()  # a slow answer stops waiting
        self._server.shutdown()
        self._server.server_close()

    def settings(self, **overrides: Any) -> dict[str, Any]:
        """``auth.oidc`` settings for this IdP; an override of None leaves the setting out."""
        base = {
            "issuer": self.issuer,
            "client_id": CLIENT_ID,
            "client_secret_ref": "file:oidc_secret",
            "roles": ROLES,
        }
        return {name: value for name, value in (base | overrides).items() if value is not None}

    # -- controls ------------------------------------------------------------------------------

    def rotate(self) -> None:
        """New signing keys under new kids; the JWKS publishes only them."""
        self.keys = {
            "RS256": (
                "rsa-" + secrets.token_hex(4),
                rsa.generate_private_key(public_exponent=65537, key_size=2048),
            ),
            "ES256": ("ec-" + secrets.token_hex(4), ec.generate_private_key(ec.SECP256R1())),
        }

    def jwks(self) -> dict[str, Any]:
        return {"keys": [_jwk(kid, key, alg) for alg, (kid, key) in self.keys.items()]}

    def id_token(self, *, headers: dict[str, Any] | None = None, key: Any = None, **claims: Any) -> str:
        """An ID token with default claims, ``claims`` over them (None drops one) and ``headers`` over
        the JOSE header (None drops one). It is signed with this IdP's key for the header's alg (the
        client secret for HS*, nothing for none), or with ``key``."""
        now = int(time.time()) + self.date_offset
        payload = {
            "iss": self.issuer,
            "sub": "alice-sub",
            "aud": self.client_id,
            "iat": now,
            "exp": now + 300,
            "auth_time": now,
            "nonce": "nonce",
            "name": "Alice Example",
            "preferred_username": "alice",
            "email": "alice@evil.example",
        } | claims
        payload = {name: value for name, value in payload.items() if value is not None}
        alg = (headers or {}).get("alg", "RS256")
        kid, own = self.keys.get(alg, self.keys["RS256"])
        header = {"kid": kid, "alg": alg} | (headers or {})
        header = {name: value for name, value in header.items() if value is not None}
        if alg == "none":
            parts = [json.dumps(header).encode(), json.dumps(payload).encode()]
            return ".".join(_b64(part) for part in parts) + "."
        signer = key or (self.client_secret.encode() if alg.startswith("HS") else own)
        with warnings.catch_warnings():  # the client secret is a short HMAC key, on purpose
            warnings.simplefilter("ignore", jwt.InsecureKeyLengthWarning)
            return jwt.encode(payload, signer, algorithm=alg, headers=header)

    def authorize(self, location: str, **claims: Any) -> str:
        """Authorize the sign-in that ``location`` (the /login redirect) starts; the one-time code.
        ``claims`` go to ``id_token``."""
        endpoint, _, query = location.partition("?")
        assert endpoint == self.discovery["authorization_endpoint"], location
        asked = dict(parse_qsl(query))
        self.authorized.append(asked)
        code = secrets.token_urlsafe(16)
        self._codes[code] = {
            "redirect_uri": asked["redirect_uri"],
            "code_challenge": asked["code_challenge"],
            "id_token": self.id_token(**{"nonce": asked["nonce"]} | claims),
        }
        return code

    # -- endpoints -----------------------------------------------------------------------------

    def answer(
        self, method: str, path: str, form: dict[str, str], headers: dict[str, str]
    ) -> tuple[int, Any]:
        if path == "/.well-known/openid-configuration":
            return 200, self.discovery
        if path == "/jwks":
            self.jwks_fetches += 1
            return 200, self.jwks()
        if path == "/token" and method == "POST":
            return self._token(form, headers)
        if path == "/logout":
            return 200, {}
        return 404, {"error": "not_found"}

    def _token(self, form: dict[str, str], headers: dict[str, str]) -> tuple[int, Any]:
        grant = self._codes.pop(form.get("code", ""), None)  # one use only
        if form.get("grant_type") != "authorization_code" or grant is None:
            return 400, {"error": "invalid_grant"}
        verifier = form.get("code_verifier", "").encode()
        challenge = _b64(hashlib.sha256(verifier).digest())
        if form.get("redirect_uri") != grant["redirect_uri"] or challenge != grant["code_challenge"]:
            return 400, {"error": "invalid_grant", "error_description": "redirect_uri or PKCE"}
        if not self._client(form, headers):
            return 401, {"error": "invalid_client", "error_description": "IdP text: <script>"}
        self.issued.append(
            {
                "access_token": "idp-access-" + secrets.token_hex(8),
                "refresh_token": "idp-refresh-" + secrets.token_hex(8),
                "token_type": "Bearer",
                "expires_in": 300,
                "id_token": grant["id_token"],
            }
        )
        return 200, self.issued[-1]

    def _client(self, form: dict[str, str], headers: dict[str, str]) -> bool:
        """The client authenticates by the configured method, and by no other."""
        basic = {name.lower(): value for name, value in headers.items()}.get("authorization", "")
        if self.client_auth == "client_secret_basic":
            if not basic.startswith("Basic ") or "client_secret" in form:
                return False
            user, _, password = base64.b64decode(basic.removeprefix("Basic ")).decode().partition(":")
            return (unquote_plus(user), unquote_plus(password)) == (self.client_id, self.client_secret)
        if basic:
            return False
        if self.client_auth == "client_secret_post":
            return (form.get("client_id"), form.get("client_secret")) == (self.client_id, self.client_secret)
        if form.get("client_assertion_type") != ASSERTION_TYPE or "client_secret" in form:
            return False
        assertion = form.get("client_assertion", "")
        self.assertions.append(jwt.get_unverified_header(assertion))
        try:
            claims = jwt.decode(
                assertion,
                self.client_public_key,
                algorithms=["RS256", "ES256"],
                audience=self.discovery["token_endpoint"],
                options={"require": ["iss", "sub", "aud", "exp", "iat", "jti"]},
            )
        except jwt.PyJWTError:
            return False
        fresh = claims["jti"] not in self._jtis and claims["exp"] <= claims["iat"] + 60
        self._jtis.add(claims["jti"])
        return fresh and claims["iss"] == claims["sub"] == self.client_id


class _Handler(BaseHTTPRequestHandler):
    def _serve(self) -> None:
        idp: FakeIdp = self.server.idp  # type: ignore[attr-defined]
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path = urlsplit(self.path).path
        form = dict(parse_qsl(body.decode())) if self.command == "POST" else {}
        idp.requests.append(Seen(self.command, path, dict(self.headers.items()), form))
        if idp.down:
            self.close_connection = True
            return
        if idp.slow:
            idp._stopping.wait(idp.slow)
        status, doc = idp.answer(self.command, path, form, dict(self.headers.items()))
        data = json.dumps(doc).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:  # the client gave up waiting
            pass

    do_GET = do_POST = _serve

    def date_time_string(self, timestamp: float | None = None) -> str:
        return formatdate((timestamp or time.time()) + self.server.idp.date_offset, usegmt=True)  # type: ignore[attr-defined]

    def log_message(self, *_: Any) -> None:
        pass


def sign_in(
    client: TestClient,
    idp: FakeIdp,
    groups: Iterable[str] = ("G-admin",),
    reauth: bool = False,
    next_: str = "#/runs",
    **claims: Any,
) -> httpx.Response:
    """Sign in through ``idp``: /v1/auth/login, the IdP's authorize and /v1/auth/callback. Returns the
    callback's answer; on success the client holds the session cookie."""
    params = {"next": next_} | ({"reauth": "1"} if reauth else {})
    started = client.get("/v1/auth/login", params=params, follow_redirects=False)
    assert started.status_code == 303, started.text
    location = started.headers["location"]
    code = idp.authorize(location, groups=list(groups), **claims)
    state = dict(parse_qsl(urlsplit(location).query))["state"]
    query = {"code": code, "state": state, "iss": idp.issuer}
    return client.get("/v1/auth/callback", params=query, follow_redirects=False)
