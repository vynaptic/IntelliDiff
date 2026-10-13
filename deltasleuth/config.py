"""Settings: one YAML file (``--config`` or ``DS_CONFIG``); credentials only as ``file:`` references.

Relative paths and every ``file:`` reference resolve against the file's directory, so a config travels
with its files.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal, get_args
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    PlainValidator,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    with_config,
)
from pydantic_core import PydanticCustomError

from deltasleuth.auth.principal import Role
from deltasleuth.core.notify import (
    KINDS,
    RESERVED,
    UNSPECIFIC,
    Cause,
    Gate,
    Judged,
    Mode,
    Ready,
    Reason,
    State,
    refusal,
)
from deltasleuth.core.spec.documents import read_document
from deltasleuth.core.spec.durations import DurationError, parse_duration
from deltasleuth.core.spec.errors import SpecError
from deltasleuth.llm.model import DataClass
from deltasleuth.llm.profiles import Environment

PosInt = Annotated[int, Field(strict=True, gt=0)]
SecretRef = Annotated[str, Field(pattern=r"^file:.+")]
Days = Annotated[int, Field(strict=True, ge=7, le=3650)]
_CLOSED = ConfigDict(extra="forbid")  # a mistyped setting is an error, never ignored
LOOPBACK = ("localhost", "127.0.0.1", "::1")
_REPLACED = (
    "api_token_ref: replaced by named API tokens; remove it and create tokens with 'ds token create'"
    " (docs/mvp/AUTH.md §5)"
)
KEY_FILE = "tokeniser.key"  # developer mode's tokeniser key, in the workspace
_MOVED = f"{KEY_FILE} was moved to encryption.tokeniser_key_ref: delete it from the workspace"


class SettingsError(ValueError):
    """The settings file is missing, unreadable or invalid."""


def _url(value: str) -> Any:
    """``value`` split; None when it does not parse (a bad port, an unclosed bracket)."""
    try:
        url = urlsplit(value)
        url.port  # noqa: B018 - raises on a bad port
    except ValueError:
        return None
    return url


def _secure(url: Any) -> bool:
    """https, or http to a loopback host."""
    if url.scheme == "http":
        return url.hostname in LOOPBACK
    return url.scheme == "https" and bool(url.hostname)


def _public_url(value: str) -> str:
    url = _url(value)
    if (
        url is None
        or not _secure(url)
        or url.username is not None
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or value.endswith(("?", "#"))
    ):
        raise PydanticCustomError(
            "public_url",
            "must be https://host[:port] (http only for localhost, 127.0.0.1 or [::1]), without a path, query"
            " or fragment",
        )
    return value.removesuffix("/")


def _issuer(value: str) -> str:
    url = _url(value)
    if url is None or not _secure(url):
        raise PydanticCustomError("issuer", "must be https (http only for a loopback host)")
    return value


def _proxy(value: str) -> str:
    url = _url(value)
    if (
        url is None
        or url.scheme not in ("http", "https")
        or not url.hostname
        or url.port is None
        or url.username is not None
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise PydanticCustomError("proxy", "must be http(s)://host:port without credentials")
    return value


def _network(value: str) -> str:
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        raise PydanticCustomError("network", "must be an IP address or CIDR network") from None


def _scopes(value: tuple[str, ...]) -> tuple[str, ...]:
    if "openid" not in value:
        raise PydanticCustomError("scopes", "must include openid")
    return value


def _group(value: str) -> str:
    if not value or value != value.strip():
        raise PydanticCustomError("group", "must be a non-empty group id without surrounding whitespace")
    return value


def _group_path(value: Any) -> str | tuple[str, ...]:
    """A claim name (tried whole, then split on dots) or a list of names followed segment by segment."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, list | tuple) and value and all(isinstance(v, str) and v for v in value):
        return tuple(value)
    raise PydanticCustomError("group_path", "must be a claim name or a list of non-empty claim names")


def _some_algorithm(value: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        raise PydanticCustomError("algorithms", "must name at least one algorithm")
    return value


def _mapped(value: dict[Role, tuple[str, ...]]) -> dict[Role, tuple[str, ...]]:
    if not any(value.values()):
        raise PydanticCustomError("roles", "map at least one IdP group to a role")
    return value


Algorithm = Literal["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
ClientAuth = Literal["client_secret_basic", "client_secret_post", "private_key_jwt"]
GroupId = Annotated[str, AfterValidator(_group)]


@with_config(_CLOSED)
@dataclass(frozen=True)
class OidcSettings:
    """The IdP relying party (docs/mvp/AUTH.md §2, §8). ``roles`` maps each role to immutable group
    ids that only IdP administrators assign, matched exactly."""

    issuer: Annotated[str, AfterValidator(_issuer)]
    client_id: Annotated[str, Field(min_length=1)]
    roles: Annotated[dict[Role, tuple[GroupId, ...]], AfterValidator(_mapped)]
    client_auth: ClientAuth = "client_secret_basic"
    client_secret_ref: SecretRef | None = None
    client_key_ref: SecretRef | None = None  # PEM: RSA >= 2048 bits or EC P-256
    client_key_id: str | None = None
    client_cert_file: Path | None = None
    scopes: Annotated[tuple[str, ...], AfterValidator(_scopes)] = ("openid", "profile")
    subject_claim: Annotated[str, Field(min_length=1)] = "sub"
    username_claim: Annotated[str, Field(min_length=1)] | None = None  # absent: the subject
    group_claims: tuple[Annotated[Any, PlainValidator(_group_path)], ...] = ("groups",)
    id_token_algs: Annotated[tuple[Algorithm, ...], AfterValidator(_some_algorithm)] = ("RS256",)
    clock_skew_seconds: Annotated[int, Field(strict=True, ge=0, le=300)] = 60
    end_session: bool = True
    proxy: Annotated[str, AfterValidator(_proxy)] | None = None

    def __post_init__(self) -> None:
        secret = self.client_auth != "private_key_jwt"
        needed = "client_secret_ref" if secret else "client_key_ref"
        if getattr(self, needed) is None:
            raise ValueError(f"client_auth {self.client_auth} needs {needed}")
        if not secret and self.client_secret_ref is not None:
            raise ValueError("client_secret_ref is only for client_secret_basic and client_secret_post")
        if secret and (self.client_key_ref, self.client_key_id, self.client_cert_file) != (None,) * 3:
            raise ValueError(
                "client_key_ref, client_key_id and client_cert_file are only for private_key_jwt"
            )


@with_config(_CLOSED)
@dataclass(frozen=True)
class AuthSettings:
    """Sign-in and sessions (docs/mvp/AUTH.md §8): tokens mode without ``oidc``, else oidc mode."""

    public_url: Annotated[str, AfterValidator(_public_url)]  # kept without a trailing slash
    allowed_hosts: tuple[str, ...] = ()  # extra Host values for API clients
    trusted_proxies: tuple[Annotated[str, AfterValidator(_network)], ...] = ()
    session_idle_minutes: Annotated[int, Field(strict=True, ge=5, le=480)] = 30
    session_max_hours: Annotated[int, Field(strict=True, ge=1, le=24)] = 8
    token_max_days: Annotated[int, Field(strict=True, ge=1, le=366)] = 90
    oidc: OidcSettings | None = None

    @field_validator("allowed_hosts")
    @classmethod
    def _over_https(cls, value: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        """Bearer tokens and cookies never cross a network in plain text."""
        public_url = info.data.get("public_url")
        if value and public_url is not None and not public_url.startswith("https://"):
            raise PydanticCustomError("allowed_hosts", "needs an https public_url")
        return value


@with_config(_CLOSED)
@dataclass(frozen=True)
class EncryptionSettings:
    """The keyring and the tokeniser key, as ``file:`` secrets outside every volume and backup
    (docs/mvp/RETENTION.md §1)."""

    kek_ref: SecretRef
    tokeniser_key_ref: SecretRef


@with_config(_CLOSED)
@dataclass(frozen=True)
class RetentionSettings:
    """Days a cycle's raw data is kept after it ends, by the spec's retention class (read at purge time)."""

    standard_days: Days = 90
    audit_days: Days = 365

    def __post_init__(self) -> None:
        if self.audit_days < self.standard_days:
            raise ValueError("audit_days must be at least standard_days")


def _installation(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9.-]{1,63}", value):
        raise PydanticCustomError("installation", "expected 1 to 63 of a-z, 0-9, '.' and '-'")
    return value


@with_config(_CLOSED)
@dataclass(frozen=True)
class PackSettings:
    """Cut-over evidence packs (docs/mvp/PACK.md §3.1), which need oidc mode: the installation's name
    and its signing key (PEM PKCS#8, EC P-256, unencrypted), a secret outside every volume. With them,
    the worker counts each cycle's key census (§1.3)."""

    installation: Annotated[str, AfterValidator(_installation)]
    signing_key_ref: SecretRef


@with_config(_CLOSED)
@dataclass(frozen=True)
class WorkerSettings:
    concurrency: PosInt = 4
    lease_seconds: PosInt = 120
    poll_seconds: Annotated[float, Field(gt=0)] = 1.0


@with_config(_CLOSED)
@dataclass(frozen=True)
class SamplingSettings:
    """A draw's session (docs/mvp/SAMPLING.md §3): the temporary buffers it dedupes a frame of two sources
    in (PostgreSQL's ``temp_buffers``); a larger table spills to temporary files (DEPLOY §10)."""

    temp_buffers: Annotated[str, Field(pattern=r"^[1-9][0-9]*(kB|MB|GB|TB)$")] = "2GB"


@with_config(_CLOSED)
@dataclass(frozen=True)
class PrecheckSettings:
    """Table pre-checks (docs/mvp/PRECHECKS.md §5): the sessions the worker holds on a target at once to
    read them, the seconds each of their statements may take, and the rows over which a side's distinct
    values and duplicate keys, which sort or hash, are not counted (0: never counted)."""

    sessions_per_target: Annotated[int, Field(strict=True, ge=1, le=4)] = 1
    statement_timeout_s: Annotated[int, Field(strict=True, ge=60, le=86400)] = 900
    distinct_max_rows: Annotated[int, Field(strict=True, ge=0, le=10**12)] = 20_000_000


def _absolute(value: Path) -> Path:
    if not value.is_absolute():
        raise PydanticCustomError("absolute", "must be an absolute path")
    return value


Size = Annotated[str, Field(pattern=r"^[1-9][0-9]*(MB|GB|TB)$")]  # binary units, as temp_buffers reads them
Absolute = Annotated[Path, AfterValidator(_absolute)]
FILES_REFUSED = (
    "files.memory and files.spill are sizes such as 2GB (MB, GB or TB); threads is 1 to 64, max_files 1 to"
    " 20,000, plan_s 10 to 86,400; spill_dir and roots_under are absolute paths; os_limits is true or false"
)


@with_config(_CLOSED)
@dataclass(frozen=True)
class FilesSettings:
    """Files as table sides (docs/mvp/FILES.md §4), budgets of each process that runs ``ds``, applied at
    restart: DuckDB's memory and spill, its threads, the directory it spills in (absent:
    ``<evidence_dir>/spill``), the directory every files target's root lies under, a side's files at most,
    the seconds a run's creation may spend planning its file sides, and whether the reader's process sets
    itself a data limit and denies itself sockets (docs/mvp/READER.md §4, §7; Linux)."""

    memory: Size = "2GB"
    spill: Size = "20GB"
    threads: Annotated[int, Field(strict=True, ge=1, le=64)] = 2
    spill_dir: Absolute | None = None
    roots_under: Absolute = Path("/var/lib/deltasleuth/files")
    max_files: Annotated[int, Field(strict=True, ge=1, le=20_000)] = 1_000
    plan_s: Annotated[int, Field(strict=True, ge=10, le=86_400)] = 300
    os_limits: Annotated[bool, Field(strict=True)] = True


def _destination(value: str) -> str:
    url = _url(value)
    if (
        url is None
        or not _secure(url)
        or url.username is not None
        or url.query
        or url.fragment
        or value.endswith(("?", "#"))
    ):
        raise PydanticCustomError(
            "destination",
            "must be https://host[:port]/path without userinfo, query or fragment (http only for localhost,"
            " 127.0.0.1 or [::1]); a URL carrying a secret goes in url_ref with host",
        )
    return value


def _host(value: str) -> str:
    if value != value.lower() or not (
        re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", value) or _literal(value)
    ):
        raise PydanticCustomError("host", "must be a lower-case DNS name or an IP literal")
    return value


def _literal(host: str) -> str | None:
    """``host`` as an IP address when it is one."""
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        return None


def _event(value: str) -> str:
    """An event a rule may name (docs/mvp/ALERTS.md §2.1): neither a reserved one nor a test's."""
    if value in RESERVED:
        raise PydanticCustomError(
            "reserved", "{event} is reserved for {feature} and not built",
            {"event": value, "feature": RESERVED[value]},
        )  # fmt: skip
    if value == "notify.test":
        raise PydanticCustomError("test", "notify.test is sent only by a test")
    if value not in KINDS:
        named = [repr(kind) for kind in KINDS if kind != "notify.test"]
        raise PydanticCustomError("event", f"Input should be {', '.join(named[:-1])} or {named[-1]}")
    return value


Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", max_length=63)]
Address = Annotated[str, Field(pattern=r"^[^@\s<>,;\"]+@[A-Za-z0-9.-]+$", max_length=254)]
Bound = Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]+)?$")]
NotifyKind = Literal["webhook", "slack", "teams", "email"]
_ONLY = {  # a channel setting and the kinds it is for
    "url": ("webhook",), "signing_key_refs": ("webhook",), "proxy": ("webhook", "slack", "teams"),
    "url_ref": ("webhook", "slack", "teams"), "host": ("webhook", "slack", "teams"), "smtp_host": ("email",),
    "smtp_port": ("email",), "tls": ("email",), "username": ("email",), "password_ref": ("email",),
    "sender": ("email",), "recipients": ("email",),
}  # fmt: skip


@with_config(_CLOSED)
@dataclass(frozen=True)
class ChannelSettings:
    """A notification channel (docs/mvp/ALERTS.md §8): a signed HTTPS webhook, Slack, Teams (a Workflows
    webhook) or SMTP e-mail. A URL holding a secret is itself a ``file:`` secret bound to a declared host;
    a literal host is judged by §7's rule here. ``labels`` and ``links`` default to true for webhook and
    e-mail, false for Slack and Teams (§6.4)."""

    kind: NotifyKind
    url: Annotated[str, AfterValidator(_destination)] | None = None
    url_ref: SecretRef | None = None
    host: Annotated[str, AfterValidator(_host)] | None = None
    signing_key_refs: Annotated[tuple[SecretRef, ...], Field(max_length=2)] = ()
    proxy: bool = False
    allow_private_network: bool = False
    labels: bool | None = None
    links: bool | None = None
    smtp_host: Annotated[str, AfterValidator(_host)] | None = None
    smtp_port: Annotated[int, Field(strict=True, ge=1, le=65535)] | None = None
    tls: Literal["starttls", "implicit", "none"] | None = None  # e-mail's: starttls when absent
    username: Annotated[str, Field(min_length=1, pattern=r"^[^\x00-\x1f\x7f]+$")] | None = None
    password_ref: SecretRef | None = None
    sender: Address | None = None
    recipients: Annotated[tuple[Address, ...], Field(max_length=20)] = ()

    def __post_init__(self) -> None:
        kind = self.kind
        for name, kinds in _ONLY.items():
            if kind not in kinds and getattr(self, name) not in (None, (), False):
                raise ValueError(f"{name} is only for {' and '.join(', '.join(kinds).rsplit(', ', 1))}")
        if kind == "email" and self.tls is None:
            object.__setattr__(self, "tls", "starttls")
        if kind in ("slack", "teams") and (self.url_ref is None or self.host is None):
            raise ValueError(f"kind {kind} needs url_ref and host (its webhook URL is a secret)")
        if kind == "webhook":
            if (self.url is None) == (self.url_ref is None) or (self.url_ref is None) != (self.host is None):
                raise ValueError("kind webhook needs url, or url_ref and host")
            if not self.signing_key_refs:
                raise ValueError("kind webhook needs signing_key_refs (one or two file: references)")
        if kind == "email":
            if self.smtp_host is None or self.sender is None or not self.recipients:
                raise ValueError("kind email needs smtp_host, sender and recipients (1 to 20)")
            if self.tls == "none" and (not self.allow_private_network or self.username is not None):
                raise ValueError("tls none needs allow_private_network and no username")
            if self.password_ref is not None and self.username is None:
                raise ValueError("password_ref needs username")
        if self.proxy and self.allow_private_network:
            raise ValueError(
                "allow_private_network with proxy: true is meaningless: the proxy resolves the destination"
            )
        if self.proxy and self.url is not None and urlsplit(self.url).scheme != "https":
            raise ValueError("proxy: true needs an https url (http is for loopback, never through a proxy)")
        for host in filter(None, [self.destination, self.smtp_host]):
            if (address := _literal(host)) and (why := refusal(address, self.allow_private_network)):
                raise ValueError(why)
        default = kind in ("webhook", "email")
        object.__setattr__(self, "labels", default if self.labels is None else self.labels)
        object.__setattr__(self, "links", default if self.links is None else self.links)
        if kind == "email" and self.smtp_port is None:
            object.__setattr__(self, "smtp_port", {"starttls": 587, "implicit": 465, "none": 25}[self.tls])

    @property
    def destination(self) -> str | None:
        """The host a send reaches (the proxy aside): the declared one, the URL's or the relay's."""
        if self.url is not None:
            return urlsplit(self.url).hostname
        return self.host or self.smtp_host


@with_config(_CLOSED)
@dataclass(frozen=True)
class WhenSettings:
    """A ``cycle.ended`` rule's conditions, any of which fires it (docs/mvp/ALERTS.md §3): a change holds
    each cycle its fact reaches the bound, a level fires when it crosses or recovers."""

    new_root_causes_at_least: PosInt | None = None
    newly_discrepant_at_least: PosInt | None = None
    mismatch_rate_above: Bound | None = None
    indeterminate_rate_above: Bound | None = None
    coverage_below: Bound | None = None
    unexplained_critical_at_least: PosInt | None = None

    def __post_init__(self) -> None:
        if not any(vars(self).values()):
            raise ValueError("when names no condition")


_FILTERS = {  # a rule's filter and the events it is for
    "when": ("cycle.ended",), "reasons": ("run.paused", "run.stopped", "run.finished"),
    "states": ("licence.state", "readiness.changed"), "statuses": ("sample.judged",),
}  # fmt: skip
_READINESS = ("programmes", "gates", "causes")  # a rule's keys for readiness.changed (READINESS.md §8)


def _state(value: Any) -> str:
    if value not in (*get_args(State), *get_args(Ready)):
        raise PydanticCustomError("state", "expected a licence state or a readiness state")
    return value


@with_config(_CLOSED)
@dataclass(frozen=True)
class RuleSettings:
    """Which events go to which channels (docs/mvp/ALERTS.md §3): ``specs`` globs (absent: every spec),
    ``modes`` (one_shot only when named) and each kind's own filter. A rule for ``readiness.changed``
    names the causes it takes, and may name programmes (globs) and gates (docs/mvp/READINESS.md §8)."""

    name: Slug
    events: tuple[Annotated[str, AfterValidator(_event)], ...]
    channels: Annotated[tuple[Slug, ...], Field(min_length=1, max_length=5)]
    specs: Annotated[tuple[str, ...], Field(min_length=1)] | None = None
    modes: Annotated[tuple[Mode, ...], Field(min_length=1)] = ("batch", "soak")
    when: WhenSettings | None = None
    reasons: Annotated[tuple[Reason, ...], Field(min_length=1)] | None = None
    states: Annotated[tuple[Annotated[str, PlainValidator(_state)], ...], Field(min_length=1)] | None = None
    statuses: Annotated[tuple[Judged, ...], Field(min_length=1)] | None = None
    programmes: Annotated[tuple[str, ...], Field(min_length=1)] | None = None
    gates: Annotated[tuple[Gate, ...], Field(min_length=1)] | None = None
    causes: Annotated[tuple[Cause, ...], Field(min_length=1)] | None = None

    def __post_init__(self) -> None:
        if not self.events:
            raise ValueError("events names no event")
        keyed = any(getattr(self, name) is not None for name in _READINESS)
        if keyed and set(self.events) != {"readiness.changed"}:
            raise ValueError("programmes, gates and causes are only for readiness.changed")
        if "readiness.changed" in self.events and self.causes is None:
            raise ValueError("readiness.changed needs causes (baseline and config are never implied)")
        for name, kinds in _FILTERS.items():
            if getattr(self, name) is not None and any(event not in kinds for event in self.events):
                raise ValueError(f"{name} is only for {' and '.join(', '.join(kinds).rsplit(', ', 1))}")
        if self.specs is not None and any(event in UNSPECIFIC for event in self.events):
            raise ValueError("licence.state, notify.dead, notify.skipped and notify.daily match no spec")


@with_config(_CLOSED)
@dataclass(frozen=True)
class NotifySettings:
    """Alerts and notifications (docs/mvp/ALERTS.md §8): channels and rules, applied at restart; absent, no
    notifier runs. ``proxy`` is for channels with ``proxy: true`` (declared as ``auth.oidc.proxy``)."""

    label: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")]
    channels: Annotated[dict[Slug, ChannelSettings], Field(max_length=20)]
    rules: Annotated[tuple[RuleSettings, ...], Field(max_length=50)] = ()
    proxy: Annotated[str, AfterValidator(_proxy)] | None = None
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=60)] = 10  # the whole send
    max_attempts: Annotated[int, Field(strict=True, ge=2, le=20)] = 8
    max_age_hours: Annotated[int, Field(strict=True, ge=1, le=72)] = 24
    rate_per_hour: Annotated[int, Field(strict=True, ge=1, le=600)] = 30  # per channel
    dedup_hours: Annotated[int, Field(strict=True, ge=0, le=168)] = 6
    daily_hour: Annotated[int, Field(strict=True, ge=0, le=23)] = 7  # UTC

    def __post_init__(self) -> None:
        for name, channel in self.channels.items():
            if channel.proxy and self.proxy is None:
                raise ValueError(f"channels.{name}: proxy: true needs notify.proxy")
        if self.proxy is not None and (address := _literal(urlsplit(self.proxy).hostname or "")):
            if why := refusal(address, True):
                raise ValueError(f"proxy: {why}")
        names: set[str] = set()
        for i, rule in enumerate(self.rules):
            if rule.name in names:
                raise ValueError(f"rules.{i}.name: duplicate rule {rule.name}")
            names.add(rule.name)
            if unknown := next((c for c in rule.channels if c not in self.channels), None):
                raise ValueError(f"rules.{i}.channels: unknown channel {unknown}")

    def refs(self) -> dict[str, str]:
        """Every secret reference of the channels, by setting."""
        found = {}
        for name, channel in self.channels.items():
            for setting in ("url_ref", "password_ref"):
                if (ref := getattr(channel, setting)) is not None:
                    found[f"notify.channels.{name}.{setting}"] = ref
            for i, ref in enumerate(channel.signing_key_refs):
                found[f"notify.channels.{name}.signing_key_refs.{i}"] = ref
        return found


def _tier(value: Any) -> str:
    if value not in ("standard", "high"):
        raise PydanticCustomError(
            "tier", "expected standard or high: readiness targets never go below architecture §15's 0.1%"
        )
    return value


def _cycles(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 50:
        raise PydanticCustomError("cycles", "expected 1 to 50")
    return value


def _max_age(value: Any) -> str:
    try:
        seconds = parse_duration(value)
    except DurationError:
        seconds = 0
    if not 3600 <= seconds <= 90 * 86400:
        raise PydanticCustomError("max_age", "expected a duration from 1h to 90d")
    return value


def _name(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?=.{1,63}$)[a-z][a-z0-9]*(-[a-z0-9]+)*", value):
        raise PydanticCustomError(
            "name", "expected a lower-case kebab-case name of 1 to 63 characters, as a spec name"
        )
    return value


def _looser(tier: str | None, above: str | None, whose: str) -> None:
    if tier == "standard" and above == "high":
        raise PydanticCustomError(
            "looser",
            "standard is looser than {whose} high: a programme or pair only tightens targets",
            {"whose": whose},
        )


Name = Annotated[str, PlainValidator(_name)]


@with_config(_CLOSED)
@dataclass(frozen=True)
class ConvergenceSettings:
    cycles: Annotated[int, PlainValidator(_cycles)]


@with_config(_CLOSED)
@dataclass(frozen=True)
class FreshSettings:
    max_age: Annotated[str, PlainValidator(_max_age)]


@with_config(_CLOSED)
@dataclass(frozen=True)
class GraphSettings:
    required: bool


@with_config(_CLOSED)
@dataclass(frozen=True)
class AccuracySettings:
    min_decided: Annotated[int, Field(strict=True, ge=1, le=1000)] = 50
    max_b_wrong: Annotated[int, Field(strict=True, ge=0, le=1000)] = 0


@with_config(_CLOSED)
@dataclass(frozen=True)
class GateSettings:
    """The readiness gates one level sets (docs/mvp/READINESS.md §7): an unset one is the level above's,
    at last the contract's default (``core.readiness.policy``)."""

    tier: Annotated[str, PlainValidator(_tier)] | None = None
    convergence: ConvergenceSettings | None = None
    fresh: FreshSettings | None = None
    graph: GraphSettings | None = None
    accuracy: AccuracySettings | None = None  # labelled accuracy (docs/mvp/ACCURACY.md): unset, not required


@with_config(_CLOSED)
@dataclass(frozen=True)
class PairSettings:
    spec: Name
    entity: Name | None = None
    gates: GateSettings = field(default_factory=GateSettings)
    closed: date | None = None  # judged as of that day, then silent


@with_config(_CLOSED)
@dataclass(frozen=True)
class ProgrammeSettings:
    name: Name
    gates: GateSettings = field(default_factory=GateSettings)
    pairs: tuple[PairSettings, ...] = ()

    def __post_init__(self) -> None:
        specs = [pair.spec for pair in self.pairs]
        if twice := next((spec for spec in specs if specs.count(spec) > 1), None):
            raise PydanticCustomError("twice", "spec {spec} is listed twice", {"spec": twice})
        for pair in self.pairs:
            _looser(pair.gates.tier, self.gates.tier, f"programme {self.name}'s")


@with_config(_CLOSED)
@dataclass(frozen=True)
class ReadinessSettings:
    """Cut-over readiness (docs/mvp/READINESS.md §7): the installation's gates and its programmes of pairs,
    applied at restart. Present (even ``{}``), the worker counts each cycle's key census (§4)."""

    gates: GateSettings = field(default_factory=GateSettings)
    programmes: tuple[ProgrammeSettings, ...] = ()

    def __post_init__(self) -> None:
        names = [programme.name for programme in self.programmes]
        if twice := next((name for name in names if names.count(name) > 1), None):
            raise PydanticCustomError("twice", "programme {name} is named twice", {"name": twice})
        held: dict[str, str] = {}
        for programme in self.programmes:
            _looser(programme.gates.tier, self.gates.tier, "the installation's")
            for pair in programme.pairs:
                if (other := held.setdefault(pair.spec, programme.name)) != programme.name:
                    raise PydanticCustomError(
                        "programmes",
                        "spec {spec} is in programmes {a} and {b}: a pair belongs to one programme",
                        {"spec": pair.spec, "a": other, "b": programme.name},
                    )
                if programme.gates.tier is None:
                    _looser(pair.gates.tier, self.gates.tier, "the installation's")


@with_config(_CLOSED)
@dataclass(frozen=True)
class McpSettings:
    """The MCP server (docs/mvp/MCP.md §4), applied at restart; absent: no MCP at all. ``data_classes`` is
    what tool results may carry, written down by the installation: ``production_clear_declared`` (MCP's
    own, no ``DataClass``) also serves the runs whose spec declares clear values or a business-code key."""

    data_classes: Annotated[
        tuple[Literal["aggregates", "production_tokenised", "production_clear_declared"], ...],
        Field(min_length=1),
    ]
    http: Annotated[bool, Field(strict=True)] = False  # POST /v1/mcp in ds serve
    work: tuple[Literal["compare_key"], ...] = ()  # work tools served
    calls_per_minute: Annotated[int, Field(strict=True, ge=1, le=600)] = 60  # per caller
    concurrency: Annotated[int, Field(strict=True, ge=1, le=16)] = 4  # calls at once, per process

    def __post_init__(self) -> None:
        tokenised = "production_tokenised" in self.data_classes
        if self.work and not tokenised:
            raise ValueError(
                "work names compare_key, whose results are production_tokenised: add it to data_classes"
            )
        if "production_clear_declared" in self.data_classes and not tokenised:
            raise ValueError("production_clear_declared widens production_tokenised: add it to data_classes")


@with_config(_CLOSED)
@dataclass(frozen=True)
class Settings:
    database_dsn_ref: SecretRef  # the DSN holds the password
    evidence_dir: Path
    workspace_dir: Path  # without encryption: tokeniser.key and kek (``ds init``)
    targets_file: Path
    llm_profiles_file: Path | None = None  # absent: AI off
    llm_profile: str | None = None  # the default chat profile
    llm_environment: Environment = "prod"
    # Data classes the operator approves for external (non-internal) LLM hosts, beyond synthetic
    # data; they still apply only in dev/test/eval (e.g. testing the chat with a Claude key).
    llm_external_data_classes: frozenset[DataClass] = frozenset()
    auth: AuthSettings | None = None  # absent: developer mode (loopback only, never proxied)
    encryption: EncryptionSettings | None = None  # required with auth; absent: the workspace's keys
    retention: RetentionSettings = field(default_factory=RetentionSettings)
    worker: WorkerSettings = field(default_factory=WorkerSettings)
    packs: PackSettings | None = None  # absent: no evidence packs
    sampling: SamplingSettings = field(default_factory=SamplingSettings)
    precheck: PrecheckSettings = field(default_factory=PrecheckSettings)
    notify: NotifySettings | None = None  # absent: no notifier runs
    readiness: ReadinessSettings | None = None  # absent: the contract's gates, and no key census for them
    files: FilesSettings = field(default_factory=FilesSettings)
    mcp: McpSettings | None = None  # absent: no MCP server (docs/mvp/MCP.md §4)

    @property
    def auth_mode(self) -> str:
        """none (developer mode), tokens or oidc (docs/mvp/AUTH.md §1)."""
        return "none" if self.auth is None else "tokens" if self.auth.oidc is None else "oidc"

    @property
    def spill_dir(self) -> Path:
        """Where DuckDB spills (docs/mvp/FILES.md §10): ``files.spill_dir``, else in the evidence volume."""
        return self.files.spill_dir or self.evidence_dir / "spill"


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Load ``path``, else the file named by ``DS_CONFIG``."""
    path = path or os.environ.get("DS_CONFIG")
    if not path:
        raise SettingsError("no settings file: pass --config or set DS_CONFIG")
    path = Path(path)
    try:
        doc = read_document(path)  # the specs' reader: a duplicate key is an error, never last-wins
    except SpecError as exc:
        raise SettingsError(f"invalid settings {path}: {exc.issues[0].message}") from None
    if isinstance(doc, dict) and "api_token_ref" in doc:
        raise SettingsError(f"invalid settings {path}: {_REPLACED}")
    doc = _anchored(doc, path.parent)
    try:
        settings = TypeAdapter(Settings).validate_python(doc)
    except ValidationError as exc:
        if any(e["loc"][:1] == ("files",) and "keyword" not in e["type"] for e in exc.errors()):
            raise SettingsError(f"invalid settings {path}: {FILES_REFUSED}") from None  # X1, one text
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise SettingsError(f"invalid settings {path}: {problems}") from None
    base = path.parent
    auth = settings.auth
    if auth and auth.oidc and auth.oidc.client_cert_file:
        auth = replace(auth, oidc=replace(auth.oidc, client_cert_file=base / auth.oidc.client_cert_file))
    settings = replace(
        settings,
        evidence_dir=base / settings.evidence_dir,
        workspace_dir=base / settings.workspace_dir,
        targets_file=base / settings.targets_file,
        llm_profiles_file=settings.llm_profiles_file and base / settings.llm_profiles_file,
        auth=auth,
    )
    if problem := _key_custody(settings):
        raise SettingsError(f"invalid settings {path}: {problem}")
    if settings.encryption and (settings.workspace_dir / KEY_FILE).exists():
        raise SettingsError(_MOVED)  # stops every entry point until the workspace copy is gone
    return settings


def _anchored(node: Any, base: Path, ref: bool = False) -> Any:
    """``node`` with every relative ``file:`` reference under a member named ``*_ref`` or ``*_refs`` made
    the settings file's, as the targets file's are; an absolute path and a bare ``file:`` stay."""
    if isinstance(node, dict):
        return {
            name: _anchored(value, base, isinstance(name, str) and name.endswith(("_ref", "_refs")))
            for name, value in node.items()
        }
    if isinstance(node, list):
        return [_anchored(value, base, ref) for value in node]
    if ref and isinstance(node, str) and node.startswith("file:") and node != "file:":
        return "file:" + str(base / node.removeprefix("file:"))
    return node


def _key_custody(settings: Settings) -> str | None:
    """Why the keys' custody is unsafe, if it is: with auth they must be secrets outside the volumes, as
    must the evidence packs' signing key and every notification secret, and packs need oidc mode."""
    encryption, packs = settings.encryption, settings.packs
    if packs is not None and settings.auth_mode != "oidc":
        return "packs: needs auth.oidc (packs certify sign-offs, which need SSO)"
    if encryption is None:
        return "encryption: required with auth" if settings.auth else None
    secrets = {f"encryption.{name}": getattr(encryption, name) for name in ("kek_ref", "tokeniser_key_ref")}
    if packs is not None:
        secrets["packs.signing_key_ref"] = packs.signing_key_ref
    if settings.notify is not None:
        secrets |= settings.notify.refs()
    volumes = (settings.evidence_dir.resolve(), settings.workspace_dir.resolve())
    for name, ref in secrets.items():
        secret = Path(ref.removeprefix("file:")).resolve()  # as read_secret reads it
        if any(secret.is_relative_to(volume) for volume in volumes):
            return f"{name}: must not be inside evidence_dir or workspace_dir, whose backups would carry it"
    return None
