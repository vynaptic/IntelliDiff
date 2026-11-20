"""LLM profiles and the host trust registry, loaded from one YAML file.

A profile is a reviewable endpoint record (dialect, ``base_url``, model, ``file:`` credential,
allowed data classes and environments, declared capabilities, budgets, limits, pricing). Trust is
never declared: it comes from the endpoint host in ``hosts``, and an unlisted host is external.
``wire`` (``openai_chat`` only) lists the optional Chat Completions fields a strict
OpenAI-compatible gateway rejects, so the adapter omits them instead of every call failing. The
registry also holds each profile's ``Gate``, so its limits bind every client of the profile, and the
chat's ``Rate`` per caller, which count over every question.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import pkgutil
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, get_args
from urllib.parse import urlsplit

from aiolimiter import AsyncLimiter
from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    TypeAdapter,
    ValidationError,
    with_config,
)

from deltasleuth.core.spec.documents import read_document
from deltasleuth.core.spec.errors import SpecError
from deltasleuth.llm import adapters
from deltasleuth.llm.model import DataClass
from deltasleuth.llm.usage import PriceVersion


def _adapters() -> dict[str, dict[str, Any]]:
    """Each dialect, by the name of its module of ``deltasleuth.llm.adapters`` (``base`` aside), with what
    the module declares (``AUTH``, ``LISTED``), in the order it is listed. The literals are read from the
    module's source, never by importing it: an adapter imports its SDK, which takes a good part of a
    second, and the settings name a profile's dialect at every start."""
    found = {}
    for module in pkgutil.iter_modules(adapters.__path__):
        if module.name == "base":
            continue
        spec = importlib.util.find_spec(f"{adapters.__name__}.{module.name}")
        assert spec is not None and spec.loader is not None
        # typeshed's Loader has no get_source; a module file's loader, an InspectLoader, has it
        source = spec.loader.get_source(spec.name)  # type: ignore[attr-defined]
        found[module.name] = {
            target.id: ast.literal_eval(node.value)
            for node in ast.parse(source or "").body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and target.id in ("DIALECT", "AUTH", "LISTED")
        }
    return dict(sorted(found.items(), key=lambda item: item[1]["LISTED"]))


_ADAPTERS = _adapters()
DIALECTS: tuple[str, ...] = tuple(_ADAPTERS)  # one adapter module per dialect
Dialect = Literal[DIALECTS]  # type: ignore[valid-type]  # read from the adapters at import: no static form
Environment = Literal["dev", "test", "eval", "staging", "prod"]
ENVIRONMENTS: tuple[str, ...] = get_args(Environment)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_AUTH: dict[str, tuple[str, str | None]] = {name: tuple(d["AUTH"]) for name, d in _ADAPTERS.items()}

PosInt = Annotated[int, Field(strict=True, gt=0)]
PosFloat = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
ProfileId = Annotated[StrictStr, Field(pattern=r"^[a-z0-9][a-z0-9_.-]{1,63}$")]
_LABEL = "[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
HostName = Annotated[StrictStr, Field(max_length=253, pattern=rf"^{_LABEL}(\.{_LABEL})*$")]
_CLOSED = ConfigDict(extra="forbid")  # unknown keys are errors, never ignored


class Trust(StrEnum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class ProfileError(ValueError):
    """An invalid profiles file; ``pointer`` locates the problem."""

    def __init__(self, pointer: str, message: str) -> None:
        self.pointer = pointer
        super().__init__(f"{pointer or '/'}: {message}")


def _check_base_url(value: str) -> str:
    """http(s) with a host; no userinfo, query or fragment (places a credential could hide)."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ValueError("base_url must use http or https")
    if parts.username is not None or parts.password is not None:
        raise ValueError("base_url must not contain userinfo")
    if parts.query or parts.fragment or value.endswith(("?", "#")):
        raise ValueError("base_url must not contain a query or fragment")
    try:
        parts.port  # noqa: B018 - property access validates the port
    except ValueError:
        raise ValueError("base_url has an invalid port") from None
    if not parts.hostname:
        raise ValueError("base_url needs a host")
    return value.rstrip("/")


@with_config(_CLOSED)
@dataclass(frozen=True)
class Capabilities:
    """What a profile declares (and ``ds llm-probe`` checks). Undeclared means unavailable."""

    tool_calling: StrictBool = False
    prompt_caching: Literal["none", "automatic", "explicit"] = "none"
    max_context: PosInt | None = None
    max_output: PosInt | None = None

    def intersect(self, other: Capabilities) -> Capabilities:
        """Self ∩ other. Limits and the caching mechanism stay as declared (self)."""
        caching = self.prompt_caching if other.prompt_caching != "none" else "none"
        return replace(self, tool_calling=self.tool_calling and other.tool_calling, prompt_caching=caching)


@with_config(_CLOSED)
@dataclass(frozen=True)
class Budgets:
    """Hard limits checked before each call; the per-turn ones through a ``TurnMeter``. The last two
    (``STANDING``) hold over questions: None is no bound."""

    max_tool_calls: PosInt = 8
    max_input_tokens_per_call: PosInt = 20_000
    max_input_tokens_per_turn: PosInt | None = None  # default (max_tool_calls + 1) * per call
    max_wall_s: PosFloat = 60.0
    per_run_tokens: PosInt | None = None  # input + output over every chat about one run (a probe: its own)
    # Input + output over the chats one caller started, in any 24 hours: about ten turns that spend every
    # default budget, or a hundred of an ordinary size.
    per_actor_tokens_per_day: PosInt | None = 2_000_000


# The budgets that outlive one client: the chat checks each against the sum its stored calls were charged.
STANDING = ("per_run_tokens", "per_actor_tokens_per_day")


@with_config(_CLOSED)
@dataclass(frozen=True)
class Limits:
    timeout_s: PosFloat = 60.0
    max_concurrency: PosInt = 4
    rps: PosFloat | None = None


@with_config(_CLOSED)
@dataclass(frozen=True)
class WireOptions:
    """Optional Chat Completions fields the endpoint accepts. The defaults are the OpenAI API's;
    ``store: false`` also keeps the provider from retaining requests."""

    max_tokens_param: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    send_store: StrictBool = True
    send_parallel_tool_calls: StrictBool = True


@with_config(_CLOSED)
@dataclass(frozen=True)
class LLMProfile:
    profile_id: str
    dialect: Dialect
    base_url: Annotated[StrictStr, AfterValidator(_check_base_url)]
    model: Annotated[StrictStr, Field(pattern=r"^[^\s\x00-\x1f]{1,200}$")]
    auth_ref: Annotated[StrictStr, Field(pattern=r"^file:")] | None = None
    auth_header: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9-]{1,64}$")] | None = None
    auth_scheme: Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9._-]{1,32}$")] | None = None  # e.g. Bearer
    data_classes: frozenset[DataClass] = frozenset()
    environments: frozenset[Environment] = frozenset()
    capabilities: Capabilities = field(default_factory=Capabilities)
    budgets: Budgets = field(default_factory=Budgets)
    limits: Limits = field(default_factory=Limits)
    pricing: PriceVersion | None = None
    wire: WireOptions = field(default_factory=WireOptions)
    allow_insecure_localhost: StrictBool = False  # plain http with a credential, loopback only
    trust: Trust = Trust.EXTERNAL  # derived from the hosts registry

    @property
    def host(self) -> str:
        return str(urlsplit(self.base_url).hostname)

    @property
    def auth(self) -> tuple[str, str | None]:
        """The header that carries the credential and its scheme (the dialect's by default)."""
        default_header, default_scheme = _DEFAULT_AUTH[self.dialect]
        if self.auth_header is None and self.auth_scheme is None:
            return default_header, default_scheme
        return self.auth_header or default_header, self.auth_scheme


class Gate:
    """A profile's ``limits`` in force. Every client given the same gate shares them, so they hold
    across chat questions, not only within one."""

    def __init__(self, limits: Limits) -> None:
        self.semaphore = asyncio.Semaphore(limits.max_concurrency)
        # The bucket must hold one call, so a rate below 1/s becomes one call per 1/rps seconds.
        rps = limits.rps
        self.limiter = AsyncLimiter(max(rps, 1.0), max(rps, 1.0) / rps) if rps else None


class Rate:
    """At most ``limit`` hits per key in a sliding ``window`` of seconds, in memory. ``hit`` says whether
    one more is within it and counts it if so: a call counts whatever it is answered, but one this refuses.
    The keys that hit longest ago go past ``size``."""

    def __init__(
        self,
        limit: int,
        window: float = 60.0,
        size: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit, self.window, self.size, self.clock = limit, window, size, clock
        self._keys: OrderedDict[Hashable, deque[float]] = OrderedDict()

    def hit(self, key: Hashable) -> bool:
        now, times = self.clock(), self._keys.pop(key, None) or deque()
        while times and now - times[0] >= self.window:
            times.popleft()
        within = len(times) < self.limit
        if within:
            times.append(now)
        self._keys[key] = times
        while len(self._keys) > self.size:
            self._keys.popitem(last=False)
        return within


@dataclass(frozen=True)
class ProfileRegistry:
    hosts: Mapping[str, Trust]
    profiles: Mapping[str, LLMProfile]
    gates: dict[str, Gate] = field(default_factory=dict, repr=False, compare=False)  # made on first use
    rates: dict[str, Rate] = field(default_factory=dict, repr=False, compare=False)  # made on first use

    def get(self, profile_id: str) -> LLMProfile:
        try:
            return self.profiles[profile_id]
        except KeyError:
            raise ProfileError(f"/profiles/{profile_id}", "no such profile") from None

    def gate(self, profile_id: str) -> Gate:
        """The one gate of the profile for as long as this registry is loaded."""
        if profile_id not in self.gates:
            self.gates[profile_id] = Gate(self.get(profile_id).limits)
        return self.gates[profile_id]

    def rate(self, name: str, limit: int) -> Rate:
        """The one rate of this name for as long as this registry is loaded: ``limit`` a minute per key,
        over every chat question of the process and forgotten at its restart."""
        if name not in self.rates:
            self.rates[name] = Rate(limit)
        return self.rates[name]


@with_config(_CLOSED)
@dataclass(frozen=True)
class _Document:
    hosts: dict[HostName, Trust] = field(default_factory=dict)
    profiles: dict[ProfileId, dict[str, Any]] = field(default_factory=dict)


_DOCUMENT = TypeAdapter(_Document)
_PROFILE = TypeAdapter(LLMProfile)


def load_profiles(path: str | Path) -> ProfileRegistry:
    """Load and validate a profiles YAML file, read as specs are (no duplicate keys, bounded aliases)."""
    try:
        doc = read_document(Path(path))
    except SpecError as exc:
        raise ProfileError(exc.issues[0].pointer, exc.issues[0].message) from None
    return parse_profiles(doc)


def parse_profiles(doc: Any) -> ProfileRegistry:
    """Validate an already-parsed profiles document."""
    document = _validate(_DOCUMENT, {} if doc is None else doc, "")
    profiles = {pid: _profile(pid, raw, document.hosts) for pid, raw in document.profiles.items()}
    return ProfileRegistry(document.hosts, profiles)


def _profile(profile_id: str, raw: dict[str, Any], hosts: Mapping[str, Trust]) -> LLMProfile:
    at = f"/profiles/{profile_id}"
    if derived := sorted({"profile_id", "trust"} & raw.keys()):
        raise ProfileError(f"{at}/{derived[0]}", "is derived (profile key, hosts registry); remove it")
    profile = _validate(_PROFILE, {**raw, "profile_id": profile_id}, at)
    profile = replace(profile, trust=hosts.get(profile.host, Trust.EXTERNAL))
    if profile.auth_ref is None and (profile.auth_header or profile.auth_scheme):
        raise ProfileError(f"{at}/auth_ref", "auth_header and auth_scheme need an auth_ref")
    if profile.allow_insecure_localhost and profile.host not in LOOPBACK_HOSTS:
        raise ProfileError(f"{at}/allow_insecure_localhost", "only loopback hosts may opt out of https")
    insecure = urlsplit(profile.base_url).scheme != "https" and not profile.allow_insecure_localhost
    if profile.auth_ref and insecure:
        raise ProfileError(f"{at}/base_url", "https is required when auth_ref is set")
    if profile.wire != WireOptions() and profile.dialect != "openai_chat":
        raise ProfileError(f"{at}/wire", "wire options apply to the openai_chat dialect only")
    if not profile.wire.send_store and profile.trust is not Trust.INTERNAL:
        raise ProfileError(f"{at}/wire/send_store", "only an internal host may omit store: false")
    return profile


def _validate[T](adapter: TypeAdapter[T], value: Any, at: str) -> T:
    try:
        return adapter.validate_python(value)
    except ValidationError as exc:
        error = exc.errors()[0]
        where = "".join(f"/{part}" for part in error["loc"] if part != "[key]")
        raise ProfileError(at + where, error["msg"]) from None
