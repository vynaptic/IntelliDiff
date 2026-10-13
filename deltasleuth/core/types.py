"""Shared data contracts for DeltaSleuth.

Every module imports these types; nothing redefines them. They mirror docs/architecture.md
(§6 data model, §7.1 outcomes, §7.2 taxonomy, §8 pseudo-code) and the control-plane DDL, so
names here are the names in the DDL and the reports.

Black-box rule: nothing in this module (or in the core) knows how a target system is built.
A target is an endpoint plus a declared behavioural contract (``TargetCapabilities``).

JSON values: after parsing, every JSON number is a ``decimal.Decimal`` (never float), objects
are ``dict[str, JSON]``, arrays ``list[JSON]``. Canonical documents may additionally contain
``KeyedMap`` (keyed / set / multiset arrays) and ``Counted`` (multiset members).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

JSON = Any
"""A parsed JSON value: dict | list | str | bool | None | Decimal (numbers are always Decimal)."""


# ---------------------------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------------------------


class Side(StrEnum):
    A = "a"  # baseline (legacy)
    B = "b"  # candidate (new)

    @property
    def other(self) -> Side:
        return Side.B if self is Side.A else Side.A


class SideStateKind(StrEnum):
    COMPLETE = "COMPLETE"
    ABSENT = "ABSENT"
    INCOMPLETE = "INCOMPLETE"
    ERROR = "ERROR"


class Outcome(StrEnum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    KNOWN_DIFF = "KNOWN_DIFF"
    NOISE_ONLY = "NOISE_ONLY"
    DRIFT = "DRIFT"
    NOT_FOUND_A = "NOT_FOUND_A"
    NOT_FOUND_B = "NOT_FOUND_B"
    INCONCLUSIVE = "INCONCLUSIVE"


class Verdict(StrEnum):
    """Derived three-value verdict (§7.1). Computed, never stored."""

    MATCH = "MATCH"
    DIFFERENT = "DIFFERENT"
    INDETERMINATE = "INDETERMINATE"


def derive_verdict(outcome: str, bug: bool) -> Verdict:
    """The verdict (§7.1) of an outcome, a member or its stored value. ``bug``: a ``bug`` rule matched,
    which makes a KNOWN_DIFF a known bug (DIFFERENT)."""
    if outcome in (Outcome.MATCH, Outcome.NOISE_ONLY) or (outcome == Outcome.KNOWN_DIFF and not bug):
        return Verdict.MATCH
    if outcome in (Outcome.MISMATCH, Outcome.NOT_FOUND_A, Outcome.NOT_FOUND_B, Outcome.KNOWN_DIFF):
        return Verdict.DIFFERENT
    return Verdict.INDETERMINATE


class Confirmation(StrEnum):
    PROVISIONAL = "provisional"
    RECHECKED = "rechecked"
    FROZEN_CUT = "frozen_cut"
    BULK_EXHAUSTIVE = "bulk_exhaustive"


class ChangeType(StrEnum):
    MISSING_IN_B = "missing_in_b"
    EXTRA_IN_B = "extra_in_b"
    VALUE_CHANGED = "value_changed"
    TYPE_CHANGED = "type_changed"
    ORDER_CHANGED = "order_changed"


class Direction(StrEnum):
    A_ONLY = "a_only"
    B_ONLY = "b_only"
    BOTH = "both"


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"

    @property
    def rank(self) -> int:
        """Higher is more severe: CRITICAL=4 … INFO=0."""
        return {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}[self.value]


class DriftReason(StrEnum):
    CHANGED_SINCE = "changed_since"
    A_CHANGED = "a_changed"
    B_CONVERGED = "b_converged"
    WATERMARK_MISMATCH = "watermark_mismatch"
    SKEW_EXCEEDED = "skew_exceeded"
    CUT_WINDOW = "cut_window"


class RuleClassification(StrEnum):
    ACCEPTED = "accepted"
    BUG = "bug"
    EXPECTED_BY_DESIGN = "expected_by_design"


# Inconclusive reasons (§7.1). Precedence when several apply: ERROR_REASONS in order, then
# INCOMPLETE_REASONS in order, then OTHER_REASONS in order.
ERROR_REASONS: tuple[str, ...] = (
    "both_error",
    "error_a",
    "error_b",
    "timeout",
    "rate_limited",
    "circuit_open",
)
INCOMPLETE_REASONS: tuple[str, ...] = (
    "truncated",
    "unstable_pagination",
    "unparseable",
    "resource_limit",
    "schema_drift",
    "absence_unconfirmed",
    "watermark_missing",
    "duplicate_member_name",
    "identity_ambiguous",
)
OTHER_REASONS: tuple[str, ...] = ("absent_both", "skipped", "recheck_failed")
INCONCLUSIVE_PRECEDENCE: tuple[str, ...] = ERROR_REASONS + INCOMPLETE_REASONS + OTHER_REASONS

# Fetcher error classes (Observation.error_class). Set ONLY for transport failures, timeouts,
# or 429/5xx after retries; any other HTTP status is judged by observe(), not the fetcher.
ERROR_CLASSES: tuple[str, ...] = (
    "connect_error",
    "timeout",
    "http_429",
    "http_5xx",
    "circuit_open",
    "resource_limit",
    "auth_error",  # 401/403: the run pauses; the key is INCONCLUSIVE(error_x)
    "identity_ambiguous",  # a table key selects several rows: INCOMPLETE(identity_ambiguous)
    "unconvertible",  # a table value does not convert to its typeMap type: INCOMPLETE(unparseable)
    "not_rows",  # a file side with lines that are not rows holds no row of the key: INCOMPLETE(unparseable)
)

# Value classes of a difference (§8.2 enrich). Attached to findings, never codes of their own.
VALUE_CLASSES: tuple[str, ...] = (
    "case_only",
    "whitespace",
    "format_only",
    "digit_transposition",
    "null_vs_absent",
    "numeric_precision",
    "type_only",
    "duplicate_key",
    "intermittent_b",
    "other",
)


# ---------------------------------------------------------------------------------------------
# Target registry (black-box behavioural contract of an endpoint)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthoritativeAbsence:
    """Which not-found responses mean 'this key does not exist' (side state ABSENT).

    ``shapes`` names body shapes declared in ``TargetCapabilities.absence_shapes``; an empty
    tuple means a 404/410 alone is authoritative. A 2xx is authoritative only with a matching shape.
    """

    status: tuple[int, ...] = ()  # 404, 410, and 2xx only together with shapes
    shapes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TruncationIndicators:
    cap: int
    items_path: str | None = None  # RFC 9535 path to the capped array
    count_path: str | None = None  # RFC 9535 path to the server-reported total


@dataclass(frozen=True)
class TargetCapabilities:
    """Declared, observable behaviour of an endpoint (§4.12). Never implementation details."""

    authoritative_absence: AuthoritativeAbsence | None = None
    absence_shapes: dict[str, str] = field(default_factory=dict)  # shape name -> CEL predicate on body
    truncation_indicators: TruncationIndicators | None = None
    pagination: Literal["none", "cursor", "offset", "page", "link_header"] = "none"
    stable_ordering: bool = False
    watermark: Literal["none", "per_request", "shared"] = "none"
    max_depth: int | None = None


@dataclass(frozen=True)
class TargetAuth:
    """Credentials by reference only. ``secret_ref`` is ``file:<path>``; never an inline value."""

    kind: Literal["none", "bearer", "header"] = "none"
    header: str | None = None  # for kind == "header", e.g. "X-Api-Key"
    secret_ref: str | None = None
    more: tuple[tuple[str, str], ...] = ()  # kind "header": up to three further (header, secret_ref)


@dataclass(frozen=True)
class DatabaseTarget:
    """A database whose endpoint the target declares (docs/mvp/ENGINES.md §4), so the SSRF guard sees it."""

    engine: Literal["mysql", "oracle"]
    host: str  # a DNS name or an IP literal
    port: int
    name: str  # mysql: its database; oracle: its service
    user: str
    password_ref: str  # file:<path>, read at every connect
    tls_ca_ref: str | None = None  # file:<path> of the CA; None: the system trust store
    tls_server_dn: str | None = None  # oracle: the server certificate's subject
    tls_verify_name: bool = True  # False: the chain only, against tls_ca_ref
    allow_unencrypted: bool = False  # no TLS


@dataclass(frozen=True)
class Target:
    target_id: str
    base_url: str  # scheme://host[:port][/base-path]; requests must stay same-origin
    dsn_ref: str | None = None  # file:<path> of a PostgreSQL DSN: a database target (no base_url)
    auth: TargetAuth = field(default_factory=TargetAuth)
    rate_per_s: float = 20.0  # per-target budget shared by all workers
    max_concurrency: int = 4
    capabilities: TargetCapabilities = field(default_factory=TargetCapabilities)
    allow_private_network: bool = False  # SSRF guard: loopback/private IPs need an explicit opt-in
    database: DatabaseTarget | None = None  # a MySQL or Oracle database target (no base_url, no dsn_ref)
    # A files target (docs/mvp/FILES.md §4): the directory its exports are mounted in, read-only.
    root: str | None = None
    settle_s: int = 60  # a file modified more recently is not read
    allow_writable: bool = False  # a mount DeltaSleuth could write to (development)
    allow_insecure_http: bool = False  # a credential over plain http, as declared
    declared_in: Literal["file", "product"] = "file"  # the targets file, or the product's list (targets.py)

    @property
    def engine(self) -> str | None:
        """The engine of a database target, ``files`` for a files target; None for a service."""
        if self.database is not None:
            return self.database.engine
        if self.root is not None:
            return "files"
        return "postgresql" if self.dsn_ref else None


# ---------------------------------------------------------------------------------------------
# Compiled spec (the subset of spec.schema.json the P0 engine executes)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Normalizer:
    """One normaliser step: ``name`` from the schema enum plus its parameters (may be empty)."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FieldCompare:
    mode: Literal["exact", "tolerance", "similarity"] = "exact"
    abs_tol: Decimal | None = None
    rel_tol: Decimal | None = None
    threshold: float | None = None  # similarity in [0, 1]
    method: Literal["token_set", "jaro_winkler", "levenshtein_ratio"] = "token_set"


@dataclass(frozen=True)
class FieldRule:
    path: str  # canonical RFC 9535 path, may use [*]
    normalize: tuple[Normalizer, ...] = ()
    compare: FieldCompare = field(default_factory=FieldCompare)
    severity: Severity | None = None
    taxonomy_code: str | None = None


@dataclass(frozen=True)
class ArrayRule:
    path: str  # canonical RFC 9535 path of the array, may use [*]
    mode: Literal["ordered", "set", "keyed", "multiset"]
    key: tuple[str, ...] = ()  # member field names (keyed only)
    severity: Severity | None = None
    taxonomy_missing_in_b: str | None = None
    taxonomy_extra_in_b: str | None = None


@dataclass(frozen=True)
class CompareConfig:
    arrays: tuple[ArrayRule, ...] = ()
    fields: tuple[FieldRule, ...] = ()
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    value_filter: str | None = None  # CEL
    null_equals_absent: bool = False


@dataclass(frozen=True)
class RequestTemplate:
    method: Literal["GET", "POST"]
    path: str  # "/v1/things/{{ key }}"; key is path-segment encoded when rendered
    query: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    key_parts: tuple[str, ...] = ()  # a multi-part key's part names: each renders as {{ keyParts.<name> }}


@dataclass(frozen=True)
class Pagination:
    kind: Literal["none", "cursor", "offset", "page", "link_header"] = "none"
    items_path: str | None = None
    cursor_path: str | None = None
    cursor_param: str | None = None
    page_param: str | None = None
    size_param: str | None = None
    size: int | None = None
    max_pages: int | None = None


@dataclass(frozen=True)
class NotFoundRule:
    status: tuple[int, ...] = (404,)
    when: str | None = None  # CEL over the parsed body (variable ``body``) and ``status``


@dataclass(frozen=True)
class TableConfig:
    """A table side (docs/mvp/TABLES.md §2): a table or view, read by key columns."""

    name: str
    key_columns: tuple[str, ...]
    columns: tuple[tuple[str, str], ...] | None = None  # (column, canonical); None: every non-key one
    schema: str | None = None
    where: str | None = None  # the row filter as written (TABLES.md §9)
    key_maps: tuple[dict[str, Any] | None, ...] = ()  # per key column; None: identity (§10)
    types: dict[str, dict[str, Any]] = field(default_factory=dict)  # typeMap: column -> typeSpec (§10)
    file: dict[str, Any] | None = None  # a file side's files (docs/mvp/FILES.md §5): the spec's table.file


@dataclass(frozen=True)
class SideConfig:
    side: Side
    role: str
    target: str  # target id
    request: RequestTemplate | None = None  # None for file-sample runs
    pagination: Pagination | None = None
    select: str | None = None  # RFC 9535 path applied to the raw body
    map: str | None = None  # JSONata expression -> canonical document
    not_found: NotFoundRule | None = None
    success_status: tuple[int, ...] = (200,)
    truncation: TruncationIndicators | None = None
    truncation_flags: tuple[str, ...] = ()  # RFC 9535 paths to the service's own flags of a cut answer
    table: TableConfig | None = None  # a table side (request is None)


@dataclass(frozen=True)
class KeyConfig:
    name: str
    type: Literal["string", "integer", "uuid", "composite"] = "string"
    pattern: str | None = None
    value_class: str = "business_code"  # "business_code" or a valueClass (then the key is PII)
    normalize: tuple[Normalizer, ...] = ()
    parts: tuple[str, ...] = ()


@dataclass(frozen=True)
class KnownDiffRule:
    rule_id: str
    when: str  # CEL, variables per spec.schema.json $defs/knownDiff
    classification: RuleClassification
    reason: str
    ticket: str
    expires: str | None = None  # ISO date


@dataclass(frozen=True)
class TokenizeRule:
    path: str
    value_class: str


@dataclass(frozen=True)
class SecurityPolicy:
    tokenize: tuple[TokenizeRule, ...] = ()
    clear: tuple[str, ...] = ()
    unlisted: Literal["tokenize", "clear"] = "tokenize"


@dataclass(frozen=True)
class RetryPolicy:
    max: int = 2  # retries after the first attempt
    initial_s: float = 0.25
    max_s: float = 4.0
    multiplier: float = 2.0
    retry_on: tuple[str, ...] = ("connect_error", "timeout", "http_429", "http_5xx")


@dataclass(frozen=True)
class ExecutionConfig:
    """Politeness and resilience settings (spec ``execution``); rate budgets live on the Target."""

    concurrency: int = 1
    delay_s: float = 0.0  # minimum pause between consecutive keys per worker
    jitter_s: float = 0.0
    connect_timeout_s: float = 5.0
    read_timeout_s: float = 30.0
    total_timeout_s: float = 60.0
    retries: RetryPolicy = field(default_factory=RetryPolicy)
    breaker_error_rate: float | None = None  # open the circuit above this error rate …
    breaker_window: int = 50  # … over this many recent calls
    breaker_cooldown_s: float = 60.0


@dataclass(frozen=True)
class StratumBucket:
    """One bucket of a hash sample's stratum dimension; the first a key's value matches wins."""

    label: str
    values: tuple[str | int | bool, ...] | None = None  # categorical match, JSON values
    min: Decimal | None = None  # inclusive numeric bounds; without values, min or max: a catch-all
    max: Decimal | None = None
    rate: int | None = None  # a u threshold, int(Decimal(str(rate)) * 2**64)
    target_size: int | None = None  # a bottom_k sign-off bucket's size
    risk_tier: Literal["standard", "high"] = "standard"


@dataclass(frozen=True)
class StratumDimension:
    name: str
    buckets: tuple[StratumBucket, ...]
    column: str | None = None  # the value: a frame column, or ``cel`` over key, row and now
    cel: str | None = None


@dataclass(frozen=True)
class FrameMember:
    """One source of a hash sample's frame (docs/mvp/SAMPLING.md §3): a table side's population, or a
    listing of a side's keys by its target."""

    kind: Literal["table", "paginated_endpoint"]
    side: Side  # the side whose keys it enumerates
    member_id: str  # table:<target>/<name> or listing:<target>:<METHOD> <path> (§9)
    columns: tuple[str, ...] = ()  # a table's strataColumns
    fields: dict[str, str] = field(default_factory=dict)  # a listing's strataFields: column -> JSONPath
    source: dict[str, Any] = field(default_factory=dict)  # the member as written


@dataclass(frozen=True)
class Resolve:
    """How often a sign-off member no read compared is retried, and after how long (§7)."""

    times: int = 3
    delay_s: float = 600.0


@dataclass(frozen=True)
class Sample:
    """A ``hash_sample`` key source (docs/mvp/SAMPLING.md §2.1)."""

    method: Literal["bottom_k", "rate"]
    label: str  # the salt: the campaign label, to which each draw adds a nonce
    members: tuple[FrameMember, ...]
    population: tuple[str, ...]  # the members' ids, sorted: its draws share one ledger (§9)
    dimensions: tuple[StratumDimension, ...] = ()
    rate: int | None = None  # the base rate's u threshold
    signoff_stratum: str | None = None  # CEL
    resolve: Resolve = field(default_factory=Resolve)


@dataclass(frozen=True)
class CompiledSpec:
    name: str
    version: str
    content_hash: str  # sha256 of the canonical spec JSON
    normalizer_version: str
    key: KeyConfig
    sides: dict[Side, SideConfig]
    compare: CompareConfig
    known_diffs: tuple[KnownDiffRule, ...] = ()
    security: SecurityPolicy = field(default_factory=SecurityPolicy)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    domain_pack: str | None = None
    sample: Sample | None = None  # a hash_sample key source
    raw: dict[str, Any] = field(default_factory=dict)  # the validated spec document

    @property
    def key_parts(self) -> tuple[str, ...]:
        """The part names of a multi-part key, a composite key beside a service side: each part is
        normalised, matched and templated on its own. Empty for any other key, whose text is one part."""
        service = any(cfg.table is None for cfg in self.sides.values())
        return self.key.parts if self.key.type == "composite" and service else ()


# ---------------------------------------------------------------------------------------------
# Observations and per-side state
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One call's evidence for one key and side. Content-addressed by ``content_hash``."""

    key: str
    side: Side
    http_status: int | None
    body: bytes | None
    observed_at: str  # ISO 8601 UTC
    latency_ms: float | None = None
    headers: dict[str, str] = field(default_factory=dict)  # response headers, lower-cased names
    error_class: str | None = None  # one of ERROR_CLASSES, else None
    error_detail: str | None = None
    attempts: int = 1
    pages: int = 1
    request_url: str | None = None  # without secrets
    source: Literal["live", "file"] = "live"
    pages_eof: bool = True  # False when max_pages stopped pagination before the last page
    pagination_unstable: bool = False  # offset/page paging without stable ordering, or repeats
    content_hash: str | None = None  # "sha256:<hex>" of body
    obs_id: str | None = None  # assigned by the evidence store


@dataclass(frozen=True)
class Canonical:
    doc: JSON  # normalised canonical document (KeyedMap / Counted allowed)
    hash: str  # sha256 of canonical_json(doc)
    raw_doc: JSON  # the same shape before normalisers (members keyed by NORMALISED key values)
    raw_hash: str  # sha256 of exact_json(raw_doc)
    # Members removed by compare.valueFilter: concrete array pointer -> member keys. The differ
    # turns a one-sided member whose key the other side filtered out into FILTER_MEMBERSHIP.
    filtered_members: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # One flag per compare.include pattern (spec order): did it select at least one node in this
    # side's shaped document? The run reports patterns that never matched (a mistyped include
    # must not turn every key into MATCH).
    include_matched: tuple[bool, ...] = ()


@dataclass(frozen=True)
class SideState:
    kind: SideStateKind
    reason: str | None = None  # INCOMPLETE / ERROR reason, from INCONCLUSIVE_PRECEDENCE
    canonical: Canonical | None = None  # set iff kind == COMPLETE
    detail: str | None = None  # diagnostic text (never contains raw payload values)
    selected: bool = True  # False: a table row outside its side's filter (TABLES.md §9)

    @classmethod
    def complete(cls, canonical: Canonical, *, selected: bool = True) -> SideState:
        return cls(SideStateKind.COMPLETE, None, canonical, selected=selected)

    @classmethod
    def absent(cls) -> SideState:
        return cls(SideStateKind.ABSENT)

    @classmethod
    def incomplete(cls, reason: str, detail: str | None = None) -> SideState:
        return cls(SideStateKind.INCOMPLETE, reason, detail=detail)

    @classmethod
    def error(cls, reason: str, detail: str | None = None) -> SideState:
        return cls(SideStateKind.ERROR, reason, detail=detail)


# ---------------------------------------------------------------------------------------------
# Canonical containers
# ---------------------------------------------------------------------------------------------


class KeyedMap(dict):  # type: ignore[type-arg]
    """A keyed / set / multiset array: member key (str) -> list of members.

    Keyed: key = canonical encoding of the member's key fields; list sorted by canonical form;
    duplicates are kept. Set: key = canonical form of the member, list of one. Multiset: key =
    canonical form, list of one ``Counted``.
    """

    __slots__ = ("mode",)

    def __init__(self, *args: Any, mode: str = "keyed", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.mode = mode


@dataclass(frozen=True)
class Counted:
    value: JSON
    n: int


# ---------------------------------------------------------------------------------------------
# Findings, results, root causes
# ---------------------------------------------------------------------------------------------


@dataclass
class Finding:
    """One difference at one pointer for one key (long format, §6 ``ds.findings``)."""

    key: str
    json_pointer: str  # RFC 6901, grammar per §6 (member keys escaped; '@i' duplicates; '@@')
    path_pattern: str  # RFC 9535 normalised form with every index / member key replaced by [*]
    change_type: ChangeType
    a_value: JSON = None  # normalised value on side a (None when absent)
    b_value: JSON = None
    a_raw: JSON = None  # pre-normaliser value (for value_class and NORMALIZATION_ONLY)
    b_raw: JSON = None
    value_class: str | None = None
    member_key: str | None = None  # member key of the innermost enclosing keyed/set member
    member_key_tok: str | None = None
    # Structured pointer: ("field", name) | ("member", member_key) | ("dup", "i") | ("index", "i"),
    # unescaped. json_pointer is rendered from it; the tokeniser re-renders member segments.
    pointer_parts: tuple[tuple[str, str], ...] = ()
    array_path: str | None = None  # path_pattern of the innermost enclosing keyed/set array
    taxonomy_code: str | None = None  # None until classify.enrich (the differ presets only
    severity: Severity | None = None  #   FILTER_MEMBERSHIP and NORMALIZATION_ONLY)
    direction: Direction = Direction.BOTH
    signature_hash: str | None = None
    rule_id: str | None = None
    rule_classification: RuleClassification | None = None
    mask_id: str | None = None
    raw_only: bool = False  # True for NORMALIZATION_ONLY findings from the raw walk
    finding_id: str | None = None

    @property
    def suppressed(self) -> bool:
        return self.rule_id is not None or self.mask_id is not None


@dataclass
class KeyResult:
    """One result row per key (§6 ``ds.results``), matches included."""

    key: str
    outcome: Outcome
    side_a: SideState
    side_b: SideState
    inconclusive_reason: str | None = None
    drift_reason: DriftReason | None = None
    confirmation: Confirmation = Confirmation.PROVISIONAL
    first_outcome: Outcome | None = None
    revision: int = 0
    findings: list[Finding] = field(default_factory=list)
    suppressed_count: int = 0
    stratum: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    obs_a_id: str | None = None
    obs_b_id: str | None = None

    @property
    def verdict(self) -> Verdict:
        """Derived verdict (§7.1)."""
        bug = any(f.rule_classification is RuleClassification.BUG for f in self.findings if f.rule_id)
        return derive_verdict(self.outcome, bug)


@dataclass
class RootCause:
    """A group of findings with one probable cause (§8.2)."""

    root_cause_id: str
    kind: Literal["member_key", "identifier", "signature"]
    path_pattern: str
    change_type: ChangeType
    direction: Direction
    affected_keys: int
    finding_count: int
    max_severity: Severity
    member_key_tok: str | None = None
    signature_hash: str | None = None
    value_class: str | None = None
    taxonomy_code: str | None = None
    sample_keys: tuple[str, ...] = ()
