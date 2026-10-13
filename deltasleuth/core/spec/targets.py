"""Target registry: the endpoints a spec may call and their declared, observable behaviour.

A targets file maps ids to ``base_url``, ``auth`` (``secret_ref: file:...`` only), rate budget,
network opt-ins and ``capabilities`` (``TargetCapabilities`` fields). Specs name target ids only,
so the registry is the SSRF allowlist. A target without ``base_url`` serves file samples only; one
with ``dsn_ref`` (a file holding a PostgreSQL DSN) or ``engine`` (a MySQL or Oracle endpoint, declared
so the guard sees it: docs/mvp/ENGINES.md §4; or ``files``, a directory of exports mounted read-only:
docs/mvp/FILES.md §4) is a database, which reports a missing row as 404 (docs/mvp/TABLES.md). Relative
secret references resolve against the targets file's directory. A service declared in the product
(``deltasleuth.targets``) is judged as a file holding it alone (``declared``).
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cryptography import x509
from jsonschema import Draft202012Validator

from deltasleuth.core.canonical.paths import jsonpath_error, normalise_query
from deltasleuth.core.spec.documents import read_document
from deltasleuth.core.spec.errors import SpecError, SpecIssue, TargetsError, UnknownTargetError, pointer
from deltasleuth.core.spec.expressions import cel_error
from deltasleuth.core.spec.schemas import schema_issues
from deltasleuth.core.types import (
    AuthoritativeAbsence,
    DatabaseTarget,
    Target,
    TargetAuth,
    TargetCapabilities,
    TruncationIndicators,
)

_OBJECT = {"type": "object", "additionalProperties": False}
_SERVICE_ONLY = ("base_url", "auth", "capabilities", "allow_private_network", "allow_insecure_http")
_PATH = {"type": "string", "pattern": "^\\$"}
_FILE_REF = {"type": "string", "pattern": "^file:[^\\x00]+$"}
# No connect descriptor, SID or alias can be written as a service, a database or a user.
_NAME = {"type": "string", "pattern": "^[A-Za-z0-9_.$#@-]{1,128}$"}
_ENGINE_ONLY = {
    "host": {
        "description": "A DNS name or an IP literal.",
        "type": "string",
        "maxLength": 253,
        "pattern": "^(?:[A-Za-z0-9-]{1,63}(?:\\.[A-Za-z0-9-]{1,63})*|[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*)$",
    },
    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
    "service": _NAME,
    "database": _NAME,
    "user": _NAME,
    "password_ref": _FILE_REF,
    "tls_ca_ref": _FILE_REF,
    "tls_server_dn": {"type": "string", "pattern": "^[A-Za-z0-9 .,=@_:/+-]{1,512}$"},
    "tls_verify_name": {"type": "boolean"},
    "allow_unencrypted": {"type": "boolean"},
}
_NOT_ENGINE = ("dsn_ref", "base_url", "auth", "capabilities", "allow_insecure_http")
_PORTS = {"mysql": 3306, "oracle": 1521}
# A header's name and a target's id end at \Z: Python's $ would pass one that ends in a line break.
_HEADER = {
    "description": "The credential's header; headers the HTTP client owns are refused.",
    "type": "string",
    "pattern": "^[!#$%&'*+.^_`|~0-9A-Za-z-]+\\Z",
    "maxLength": 128,
    "not": {"pattern": "^(?i:host|content-length|transfer-encoding|connection|content-type)$"},
}
# A files target's members (docs/mvp/FILES.md §4), checked by ``_files`` so that T8 words them.
_FILES_ONLY = {
    "root": {"type": "string"},
    "settle_s": {"type": "integer"},
    "allow_writable": {"type": "boolean"},
}
_FILES_TAKES = {"engine", *_FILES_ONLY, "rate_per_s", "max_concurrency"}
ROOT_MAX = 1024
_TARGETS_SCHEMA: dict[str, Any] = {
    **_OBJECT,
    "required": ["targets"],
    "properties": {
        "targets": {
            "type": "object",
            "minProperties": 1,
            "propertyNames": {"pattern": "^[a-z][a-z0-9]*(?:-[a-z0-9]+)*\\Z", "maxLength": 63},
            "additionalProperties": {"$ref": "#/$defs/target"},
        }
    },
    "$defs": {
        "target": {
            **_OBJECT,
            "properties": {
                "base_url": {"type": "string", "minLength": 1, "maxLength": 2048},
                "auth": {"$ref": "#/$defs/auth"},
                "rate_per_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 10000},
                "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 256},
                "allow_private_network": {"type": "boolean"},
                "allow_insecure_http": {"type": "boolean"},
                "capabilities": {"$ref": "#/$defs/capabilities"},
                "dsn_ref": {
                    "description": "A PostgreSQL database: file:<path> of its DSN. Never an inline value.",
                    **_FILE_REF,
                },
                "engine": {
                    "description": "A database at a declared endpoint, or files in a mounted directory.",
                    "enum": ["mysql", "oracle", "files"],
                },
                **_ENGINE_ONLY,
                **_FILES_ONLY,
            },
            "allOf": [
                {
                    "if": {"required": ["dsn_ref"]},
                    "then": {"not": {"anyOf": [{"required": [name]} for name in _SERVICE_ONLY]}},
                },
                {
                    "if": {"required": ["engine"], "properties": {"engine": {"enum": ["mysql", "oracle"]}}},
                    "then": {
                        "required": ["host", "user", "password_ref"],
                        "not": {"anyOf": [{"required": [name]} for name in [*_NOT_ENGINE, *_FILES_ONLY]]},
                    },
                },
                {
                    "if": {"required": ["engine"]},
                    "else": {
                        "not": {"anyOf": [{"required": [name]} for name in [*_ENGINE_ONLY, *_FILES_ONLY]]}
                    },
                },
            ],
        },
        "auth": {
            **_OBJECT,
            "required": ["kind"],
            "properties": {
                "kind": {"enum": ["none", "bearer", "header"]},
                "header": _HEADER,
                "secret_ref": {
                    "description": "Credential by reference only: file:<path>. Never an inline value.",
                    "type": "string",
                    "pattern": "^file:[^\\x00]+$",
                },
                "more": {
                    "description": "Kind header: further credential headers, each with its own secret_ref.",
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {
                        **_OBJECT,
                        "required": ["header", "secret_ref"],
                        "properties": {"header": _HEADER, "secret_ref": _FILE_REF},
                    },
                },
            },
            "allOf": [
                {
                    "if": {"properties": {"kind": {"const": "none"}}},
                    "then": {
                        "not": {"anyOf": [{"required": [name]} for name in ("header", "secret_ref", "more")]}
                    },
                },
                {
                    "if": {"properties": {"kind": {"const": "bearer"}}},
                    "then": {
                        "required": ["secret_ref"],
                        "not": {"anyOf": [{"required": ["header"]}, {"required": ["more"]}]},
                    },
                },
                {
                    "if": {"properties": {"kind": {"const": "header"}}},
                    "then": {"required": ["header", "secret_ref"]},
                },
            ],
        },
        "capabilities": {
            **_OBJECT,
            "properties": {
                "authoritative_absence": {
                    **_OBJECT,
                    "required": ["status"],
                    "properties": {
                        "status": {
                            "description": "404/410, or a 2xx status together with shapes.",
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {
                                "anyOf": [
                                    {"enum": [404, 410]},
                                    {"type": "integer", "minimum": 200, "maximum": 299},
                                ]
                            },
                        },
                        "shapes": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                },
                "absence_shapes": {
                    "type": "object",
                    "propertyNames": {"pattern": "^[A-Za-z_][A-Za-z0-9_-]*$"},
                    "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 4000},
                },
                "truncation_indicators": {
                    **_OBJECT,
                    "required": ["cap"],
                    "anyOf": [{"required": ["items_path"]}, {"required": ["count_path"]}],
                    "properties": {
                        "cap": {"type": "integer", "minimum": 1},
                        "items_path": _PATH,
                        "count_path": _PATH,
                    },
                },
                "pagination": {"enum": ["none", "cursor", "offset", "page", "link_header"]},
                "stable_ordering": {"type": "boolean"},
                "watermark": {"enum": ["none", "per_request", "shared"]},
                "max_depth": {"type": "integer", "minimum": 1},
            },
        },
    },
}
_VALIDATOR = Draft202012Validator(_TARGETS_SCHEMA)
# A database answers a key with its row or with none: no row is an authoritative 404.
DATABASE_CAPABILITIES = TargetCapabilities(authoritative_absence=AuthoritativeAbsence((404,)))
_METADATA = ipaddress.ip_address("fd00:ec2::254")  # a ULA metadata address
# IPv6 addresses whose last 32 bits are the IPv4 address they stand for: IPv4-mapped, IPv4-translated,
# the well-known NAT64 prefix and the deprecated IPv4-compatible form.
_EMBEDDING = [ipaddress.ip_network(n) for n in ("::ffff:0:0/96", "::ffff:0:0:0/96", "64:ff9b::/96", "::/96")]


class TargetRegistry:
    """The id -> ``Target`` map, the only way a spec side reaches an endpoint: as loaded, but for the
    targets declared in the product (``put`` and ``drop``, which only ``deltasleuth.targets`` calls)."""

    def __init__(self, targets: Mapping[str, Target]) -> None:
        for target_id, target in targets.items():
            if target.target_id != target_id:
                raise ValueError(f"registry key {target_id!r} differs from target_id {target.target_id!r}")
        self._targets = dict(targets)

    def get(self, target_id: str) -> Target:
        if target_id not in self._targets:
            raise UnknownTargetError(f"target {target_id!r} is not in the registry")
        return self._targets[target_id]

    def live(self, target_id: str) -> Target:
        """The target for a live connector; refuses a file-sample-only target (no base_url, no database)."""
        target = self.get(target_id)
        if not (target.base_url or target.engine):
            message = "a live connector needs base_url; this target is declared for file samples only"
            raise TargetsError(
                [SpecIssue(pointer(("targets", target_id, "base_url")), message, "missing_base_url")]
            )
        return target

    def put(self, target: Target) -> None:
        """Add the target, or replace the one of its id: every connector opened afterwards reads it."""
        self._targets[target.target_id] = target

    def drop(self, target_id: str) -> None:
        self._targets.pop(target_id, None)

    def __contains__(self, target_id: object) -> bool:
        return target_id in self._targets

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._targets))

    def __len__(self) -> int:
        return len(self._targets)


def load_targets(path: str | os.PathLike[str]) -> TargetRegistry:
    """Load and validate a targets file; raises ``TargetsError`` listing every problem."""
    source = Path(path)
    try:
        doc = read_document(source)
    except SpecError as exc:
        raise TargetsError(exc.issues) from None
    issues = schema_issues(_VALIDATOR, doc)
    if issues:
        raise TargetsError(issues)
    targets = {}
    for target_id, entry in doc["targets"].items():
        targets[target_id] = _target(target_id, entry, source.parent.absolute(), issues)
    if issues:
        raise TargetsError(issues)
    return TargetRegistry(targets)


def declared(target_id: str, entry: dict[str, Any], base_dir: Path) -> Target:
    """A target declared in the product, judged as a targets file holding ``entry`` alone: the file's
    schema, codes, defaults and address rules, its references relative to ``base_dir``. Raises
    ``TargetsError``."""
    issues = schema_issues(_VALIDATOR, {"targets": {target_id: entry}})
    if issues:
        raise TargetsError(issues)
    target = _target(target_id, entry, base_dir, issues)
    if issues:
        raise TargetsError(issues)
    return replace(target, declared_in="product")


class _Refused(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _file_ref(ref: str | None, base_dir: Path) -> str | None:
    return None if ref is None else "file:" + str(base_dir / ref.removeprefix("file:"))  # absolute stays


def _target(target_id: str, entry: dict[str, Any], base_dir: Path, issues: list[SpecIssue]) -> Target:
    at = ("targets", target_id)
    auth = entry.get("auth", {"kind": "none"})
    ref = _file_ref(auth.get("secret_ref"), base_dir)
    base_url = ""
    if "base_url" in entry:
        try:
            base_url = _base_url(entry, auth["kind"])
        except _Refused as exc:
            issues.append(SpecIssue(pointer((*at, "base_url")), str(exc), exc.code))
    files = entry.get("engine") == "files"
    if files:
        _files(entry, at, issues)
    more = tuple((m["header"], _file_ref(m["secret_ref"], base_dir) or "") for m in auth.get("more", ()))
    names = [name.lower() for name in (auth.get("header", ""), *(name for name, _ in more))]
    for i, name in enumerate(names[1:]):
        if name in names[: i + 1]:  # whatever its case, as HTTP reads a header's name
            message = "each credential header is named once"
            issues.append(SpecIssue(pointer((*at, "auth", "more", i, "header")), message, "repeated_header"))
    return Target(
        target_id=target_id,
        base_url=base_url,
        auth=TargetAuth(kind=auth["kind"], header=auth.get("header"), secret_ref=ref, more=more),
        rate_per_s=float(entry.get("rate_per_s", 5.0 if files else 20.0)),
        max_concurrency=int(entry.get("max_concurrency", 2 if files else 4)),
        capabilities=(
            DATABASE_CAPABILITIES
            if "dsn_ref" in entry or "engine" in entry
            else _capabilities(entry.get("capabilities", {}), (*at, "capabilities"), issues)
        ),
        allow_private_network=bool(entry.get("allow_private_network", False)),
        allow_insecure_http=bool(entry.get("allow_insecure_http", False)),
        dsn_ref=_file_ref(entry.get("dsn_ref"), base_dir),
        database=_database(entry, base_dir, at, issues) if "engine" in entry and not files else None,
        root=entry.get("root", "") if files else None,
        settle_s=entry.get("settle_s", 60),
        allow_writable=entry.get("allow_writable", False),
    )


def _files(entry: dict[str, Any], at: tuple[str, ...], issues: list[SpecIssue]) -> None:
    """A files target's members (docs/mvp/FILES.md §4, T8) and its root's form (T9); where it may lie needs
    the settings, so ``manifest.forbidden`` judges that (T10)."""
    settle = entry.get("settle_s", 60)
    if "root" not in entry or set(entry) - _FILES_TAKES or not 0 <= settle <= 86400:
        message = (
            "engine files needs root and takes only root, settle_s (0 to 86400), allow_writable, rate_per_s"
            " and max_concurrency"
        )
        issues.append(SpecIssue(pointer(at), message, "invalid_files_target"))
    root = entry.get("root", "/x")
    # In its normal form: with a trailing or doubled / it is not its own real path, which a plan refuses as
    # a symbolic link (P14).
    normal = root.startswith("/") and not root.startswith("//") and os.path.normpath(root) == root
    if not normal or root == "/" or len(root) > ROOT_MAX:
        message = (
            "root must be an absolute path without . or .. segments or a trailing or doubled /, and not /"
        )
        issues.append(SpecIssue(pointer((*at, "root")), message, "invalid_root"))


def address_rule(address: str) -> str | None:
    """What stands between ``address`` and a call: ``never`` (link-local, unspecified, multicast or
    metadata), ``private`` (any other address that is not global: it needs ``allow_private_network``),
    or None for a global one. An IPv6 address that stands for an IPv4 one is judged as that one: a
    translator would reach 10.1.2.3 for 64:ff9b::a01:203. Each caller words its refusal."""
    ip = ipaddress.ip_address(address.strip("[]"))
    if isinstance(ip, ipaddress.IPv6Address) and any(ip in network for network in _EMBEDDING):
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip == _METADATA:
        return "never"
    return None if ip.is_global else "private"


def _is_subject(dn: str) -> bool:
    """Whether ``dn`` parses as the subject an Oracle session's certificate is matched with, whole."""
    try:
        x509.Name.from_rfc4514_string(dn)
    except ValueError:
        return False
    return True


def _database(
    entry: dict[str, Any], base_dir: Path, at: tuple[str, ...], issues: list[SpecIssue]
) -> DatabaseTarget:
    """An engine target's endpoint (docs/mvp/ENGINES.md §4); its problems are added to ``issues``."""
    engine, host = entry["engine"], entry["host"]
    own, other = ("database", "service") if engine == "mysql" else ("service", "database")
    dn, name = entry.get("tls_server_dn"), entry.get("tls_verify_name", True)
    plain = entry.get("allow_unencrypted", False)
    refused = []
    if own not in entry or other in entry:
        refused.append(f"engine {engine} needs {own} and takes no {other}")
    if dn is not None and engine == "mysql":
        refused.append("tls_server_dn is Oracle's")
    elif dn is not None and not _is_subject(dn):
        written = "CN=crm-db.bank.example,O=Bank, no space after a comma"
        refused.append(f"tls_server_dn is not a certificate's subject as RFC 4514 writes one ({written})")
    if plain:
        if any(member in entry for member in ("tls_ca_ref", "tls_server_dn", "tls_verify_name")):
            refused.append("allow_unencrypted: true takes no tls_ca_ref, tls_server_dn or tls_verify_name")
    elif not name:
        if "tls_ca_ref" not in entry or dn is not None:
            refused.append("tls_verify_name: false needs tls_ca_ref and takes no tls_server_dn")
    elif engine == "oracle" and dn is None:
        subject = "the server certificate's subject"
        refused.append(f"engine oracle with TLS needs tls_server_dn ({subject}), or tls_verify_name: false")
    issues += [SpecIssue(pointer(at), message, "invalid_database_target") for message in refused]

    def issue(code: str, message: str) -> None:
        issues.append(SpecIssue(pointer((*at, "host")), message, code))

    try:
        rule = address_rule(host)
    except ValueError:  # a name: judged at every connect, when it is resolved
        if ":" in host:
            issue("invalid_database_target", "host is a DNS name or an IP literal")
        rule = None
    if rule == "never":
        issue("forbidden_host", f"{host} is never a target (link-local, unspecified, multicast or metadata)")
    elif rule == "private" and not entry.get("allow_private_network", False):
        issue("private_network", "loopback and private hosts need allow_private_network: true")
    return DatabaseTarget(
        engine=engine,
        host=host,
        port=entry.get("port", _PORTS[engine]),
        name=entry.get(own, ""),
        user=entry["user"],
        password_ref=_file_ref(entry["password_ref"], base_dir) or "",
        tls_ca_ref=_file_ref(entry.get("tls_ca_ref"), base_dir),
        tls_server_dn=dn,
        tls_verify_name=name,
        allow_unencrypted=plain,
    )


def _base_url(entry: dict[str, Any], auth_kind: str) -> str:
    """Normalised base URL: lower-case scheme and host, no trailing slash; raises ``_Refused``."""
    raw: str = entry["base_url"]
    if any(ch.isspace() or ord(ch) < 0x20 for ch in raw) or "?" in raw or "#" in raw:
        raise _Refused(
            "invalid_base_url", "base_url must not contain whitespace, a query (?) or a fragment (#)"
        )
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise _Refused("invalid_base_url", f"base_url is not a valid URL: {exc}") from None
    scheme, host = parts.scheme.lower(), parts.hostname
    if scheme not in ("http", "https"):
        raise _Refused("invalid_base_url", "base_url must use http or https")
    if "@" in parts.netloc:
        raise _Refused(
            "invalid_base_url", "base_url must not carry userinfo; credentials come from secret_ref"
        )
    if not host:
        raise _Refused("invalid_base_url", "base_url has no host")
    if any(s in (".", "..") or s.lower() in ("%2e", "%2e%2e") for s in parts.path.split("/")):
        raise _Refused("invalid_base_url", "base_url path must not contain . or .. segments")
    if scheme == "http" and auth_kind != "none" and not entry.get("allow_insecure_http", False):
        raise _Refused("insecure_http", "credentials require https unless allow_insecure_http is true")
    if _is_private_host(host) and not entry.get("allow_private_network", False):
        raise _Refused(
            "private_network", "loopback, private and link-local hosts need allow_private_network: true"
        )
    netloc = f"[{host}]" if ":" in host else host  # an IPv6 literal
    netloc += "" if port is None else f":{port}"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), "", ""))


def _is_private_host(host: str) -> bool:
    """Literal loopback/private/link-local/reserved hosts; names are resolved and guarded at call time."""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _capabilities(entry: dict[str, Any], at: tuple[str, ...], issues: list[SpecIssue]) -> TargetCapabilities:
    def issue(parts: tuple[str | int, ...], code: str, message: str) -> None:
        issues.append(SpecIssue(pointer((*at, *parts)), message, code))

    shapes: dict[str, str] = dict(entry.get("absence_shapes", {}))
    for name, source in shapes.items():
        if problem := cel_error(source, "absence_shape"):
            issue(("absence_shapes", name), "invalid_cel", problem)
    absence = entry.get("authoritative_absence")
    if absence is not None:
        for i, shape in enumerate(absence.get("shapes", [])):
            if shape not in shapes:
                message = f"shape {shape!r} is not declared in absence_shapes"
                issue(("authoritative_absence", "shapes", i), "unknown_absence_shape", message)
        for i, status in enumerate(absence["status"]):
            if status < 300 and not absence.get("shapes"):  # a 2xx body is absence only by its shape
                message = "a 2xx status is authoritative absence only together with shapes"
                issue(("authoritative_absence", "status", i), "absence_2xx_needs_shapes", message)
        absence = AuthoritativeAbsence(tuple(absence["status"]), tuple(absence.get("shapes", ())))
    truncation = entry.get("truncation_indicators")
    if truncation is not None:
        paths = {name: truncation[name] for name in ("items_path", "count_path") if name in truncation}
        for name, path in paths.items():
            if problem := jsonpath_error(path):
                issue(("truncation_indicators", name), "invalid_jsonpath", problem)
        truncation = TruncationIndicators(
            truncation["cap"], **{n: normalise_query(p) for n, p in paths.items()}
        )
    return TargetCapabilities(
        authoritative_absence=absence,
        absence_shapes=shapes,
        truncation_indicators=truncation,
        pagination=entry.get("pagination", "none"),
        stable_ordering=entry.get("stable_ordering", False),
        watermark=entry.get("watermark", "none"),
        max_depth=entry.get("max_depth"),
    )
