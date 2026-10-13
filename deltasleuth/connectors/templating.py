"""Request templates: ``{{ name }}`` placeholders, key validation and same-origin URLs.

A value is encoded for where it lands: one path segment (``a/b`` -> ``a%2Fb``), a query
component, a header value, or a string in the JSON body. The body template is a JSON value whose
string leaves are rendered before the whole value is serialised, so a key is always JSON data,
never JSON text (GraphQL keys belong in ``variables``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from yarl import URL

from deltasleuth.connectors.base import ConnectorConfigError, KeyRejectedError, UnsafeTargetError
from deltasleuth.core import jsonio
from deltasleuth.core.spec.keys import split_parts
from deltasleuth.core.types import RequestTemplate

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")
_PATH_LITERAL_SAFE = "/:@!$&'()*+,;=%-._~"  # RFC 3986 pchar, "/" and existing escapes
_PATH_FORBIDDEN_LITERAL = "{}?# \t\r\n\f\v"
_DOT_SEGMENTS = frozenset({".", "..", "%2e", "%2e%2e", ".%2e", "%2e."})
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")  # RFC 9110 token

# Setting these would bypass the same-origin rule or desynchronise the connection.
FRAMING_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection"})
# Credentials come only from the target's auth reference, never from a template.
FORBIDDEN_TEMPLATE_HEADERS = FRAMING_HEADERS | set(
    "authorization proxy-authorization cookie x-api-key api-key x-auth-token x-access-token".split()
)


def template_variables(key: str, run_id: str, call_seq: int, parts: Sequence[str] = ()) -> dict[str, str]:
    """What a request template renders; ``parts`` names a multi-part key's parts, each of which is
    ``keyParts.<name>`` beside the joined ``key``."""
    # "." and ".." survive segment encoding as dot segments; an empty key collapses the path.
    if not isinstance(key, str) or key in ("", ".", ".."):
        raise KeyRejectedError("key is not a string, or is empty or a dot segment, and cannot be sent")
    variables = {"key": key, "run_id": str(run_id), "call_seq": str(int(call_seq))}
    if parts:
        values = split_parts(key, len(parts))
        if values is None or any(value in ("", ".", "..") for value in values):
            raise KeyRejectedError(
                "key is not its parts joined by |, or a part is empty or a dot segment, and cannot be sent"
            )
        variables |= {f"keyParts.{name}": value for name, value in zip(parts, values, strict=True)}
    return variables


def render(
    template: str,
    variables: Mapping[str, str],
    *,
    value: Callable[[str], str] = str,
    literal: Callable[[str], str] = str,
) -> str:
    """Substitute placeholders; literal text and values are encoded separately.

    Templates have no other syntax, so a stray ``{{``/``}}`` or an unknown name is an error
    rather than text sent to the target.
    """
    parts = _PLACEHOLDER.split(template)  # literal, name, literal, ..., literal
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2:
            if part not in variables:
                raise ConnectorConfigError(f"unknown placeholder {{{{ {part} }}}}")
            out.append(value(variables[part]))
        elif "{{" in part or "}}" in part:
            raise ConnectorConfigError("malformed placeholder in template text")
        else:
            out.append(literal(part))
    return "".join(out)


def _path_literal(text: str) -> str:
    # The schema's path grammar: encoding these instead would change what the author wrote.
    if any(ch in text for ch in _PATH_FORBIDDEN_LITERAL):
        raise ConnectorConfigError("request path text may not contain a query, fragment, space or brace")
    return quote(text, safe=_PATH_LITERAL_SAFE)


def render_body(template: Any, variables: Mapping[str, str]) -> Any:
    """Render the string leaves of a JSON body template; member names are never templated."""
    if isinstance(template, str):
        return render(template, variables)
    if isinstance(template, dict):
        return {name: render_body(v, variables) for name, v in template.items()}
    if isinstance(template, list):
        return [render_body(v, variables) for v in template]
    return template


def query_pairs(
    query: Mapping[str, Any], variables: Mapping[str, str], overrides: Sequence[tuple[str, str]] = ()
) -> list[tuple[str, str]]:
    """Ordered query pairs (lists repeat the parameter); ``overrides`` replace same-named ones."""
    replaced = {name for name, _ in overrides}
    pairs: list[tuple[str, str]] = []
    for name, values in query.items():
        if name in replaced:
            continue
        for v in values if isinstance(values, list | tuple) else [values]:
            if not isinstance(v, str | bool | int | float | Decimal):
                raise ConnectorConfigError(f"query values must be scalars, not {type(v).__name__}")
            pairs.append((name, render(v, variables) if isinstance(v, str) else jsonio.dumps(v)))
    return pairs + list(overrides)


def check_path(raw_path: str) -> None:
    """Reject scheme-relative, empty, dot and control-character segments in a raw (encoded) path.

    Checking the encoded form means ``%2e%2e`` cannot pass as data and be normalised later.
    """
    if not raw_path.startswith("/") or raw_path.startswith("//"):
        raise UnsafeTargetError("request path must start with a single '/'")
    if _CONTROL_CHARS.search(raw_path) or any(ch in raw_path for ch in " ?#\\"):
        raise UnsafeTargetError("request path contains whitespace, '?', '#', '\\' or control characters")
    segments = raw_path.split("/")[1:]
    if any(s.lower() in _DOT_SEGMENTS for s in segments):
        raise UnsafeTargetError("request path contains a '.' or '..' segment")
    if "" in segments[:-1]:  # a trailing slash is allowed
        raise UnsafeTargetError("request path contains an empty segment")


def check_header_name(name: str) -> None:
    if not _HEADER_NAME.fullmatch(name):
        raise ConnectorConfigError(f"request header name {name!r} is not a valid HTTP token")


@dataclass(frozen=True)
class Endpoint:
    """A registered base URL: the origin every request keeps, and the base path it stays under."""

    scheme: str
    host: str
    port: int
    base_path: str  # raw, without a trailing slash; "" for the root
    origin: str

    @classmethod
    def parse(cls, base_url: str) -> Endpoint:
        if not base_url:
            raise ConnectorConfigError("target has no base_url (file-only target); it cannot be fetched")
        url = URL(base_url)
        if url.scheme not in ("http", "https") or not url.raw_host or url.port is None:
            raise ConnectorConfigError("target base_url must be an absolute http(s) URL")
        if url.user is not None or url.password is not None or url.raw_query_string or url.raw_fragment:
            raise ConnectorConfigError("target base_url must not carry userinfo, a query or a fragment")
        base_path = url.raw_path.rstrip("/")
        if base_path:
            check_path(base_path)
        return cls(url.scheme, url.host or url.raw_host, url.port, base_path, str(url.origin()))

    def url(self, path: str, query: Sequence[tuple[str, str]] = ()) -> URL:
        """Absolute URL for an already-encoded ``path`` (starting with "/") under the base path."""
        check_path(path)
        text = self.origin + self.base_path + path
        if query:
            text += "?" + "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in query)
        return URL(text, encoded=True)

    def admits(self, url: URL) -> bool:
        """Does ``url`` keep this origin and stay under the base path?"""
        if (url.scheme, url.host, url.port) != (self.scheme, self.host, self.port):
            return False
        if url.user is not None or url.password is not None or url.raw_fragment:
            return False
        path = url.raw_path
        if self.base_path and path != self.base_path and not path.startswith(self.base_path + "/"):
            return False
        try:
            check_path(path)
        except UnsafeTargetError:
            return False
        return True


@dataclass(frozen=True)
class RenderedRequest:
    """One concrete HTTP request. It holds auth headers, so it is never logged or recorded."""

    method: str
    url: URL
    headers: dict[str, str]
    body: bytes | None


def build_request(
    endpoint: Endpoint,
    template: RequestTemplate,
    variables: Mapping[str, str],
    *,
    auth_headers: Mapping[str, str],
    page_params: Sequence[tuple[str, str]] = (),
) -> RenderedRequest:
    """Render ``template`` for one call; the URL stays on the endpoint's origin and base path."""
    if template.method not in ("GET", "POST"):  # read semantics only
        raise ConnectorConfigError("request method must be GET or POST")
    path = render(template.path, variables, value=lambda v: quote(v, safe=""), literal=_path_literal)
    url = endpoint.url(path, query_pairs(template.query, variables, page_params))
    reserved = FORBIDDEN_TEMPLATE_HEADERS | {name.lower() for name in auth_headers}
    headers: dict[str, str] = {}
    for name, text in template.headers.items():
        check_header_name(name)
        if name.lower() in reserved:
            raise ConnectorConfigError(f"request header {name!r} may not be set by a template")
        headers[name] = render(text, variables)
        if _CONTROL_CHARS.search(headers[name]):  # CR/LF in a value would inject headers
            raise KeyRejectedError(f"rendered value of header {name!r} contains control characters")
    headers.update(auth_headers)
    body = None
    if template.body is not None:
        if template.method != "POST":
            raise ConnectorConfigError("a request body is only allowed with POST")
        body = jsonio.dumps(render_body(template.body, variables)).encode("utf-8")
        if not any(name.lower() == "content-type" for name in headers):
            headers["Content-Type"] = "application/json"
    return RenderedRequest(template.method, url, headers, body)
