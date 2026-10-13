"""Licence keys and entitlements: a contractual control, not DRM.

A vendor-signed file binds a customer, an order, a tier, a pair count and at most a year to one
installation, the store's database that 010 names (``parse``); ``install`` stores it in ds.licences.
Every decision reads the rows again and verifies them against the vendor keys this build embeds
(``standing``, the pure ``judge``), so an install applies at once in every process, with no setting or
secret. Without a licence the installation is an unlicensed evaluation: two pairs, and no runs under
SSO, sign-off or evidence packs. Only new work is refused (``need``, ``admit``, a soak's boundary
``stopped``, which judges the next cycle as of when it falls due, ``pack_problems``); reads, reports,
exports, purge and audit never ask, and nothing calls out. The worker audits each change of state
(``watch``). ``status`` is what ``GET /v1/licence`` and ``ds licence`` show, ``run_label`` and
``file_name`` what a run and its reports say of the licence it was made under.

All of that holds in a build that embeds a vendor key. A build that embeds none is open: its
standing is ``open``, which judges no row, counts no pair, refuses and warns of nothing and records
nothing on a run; a key in ``VENDOR_KEYS`` brings the rest back.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import psycopg

from deltasleuth.auth.principal import WORKER, Principal, printable
from deltasleuth.context import Context
from deltasleuth.core.types import CompiledSpec
from deltasleuth.security import signing
from deltasleuth.store import repo

FORMAT = "deltasleuth-licence/1"
FIELDS = ("format", "licence", "customer", "order", "installation", "tier", "pairs", "starts", "ends",
          "issued", "key", "signature")  # fmt: skip
TIERS = ("community", "pro", "enterprise")  # lowest first
MAX_BYTES = 4096
MAX_DAYS = 365  # ends at most this long after starts: a year of 366 days at most
WARN_DAYS = 30  # a running licence is ending from this long before its ends
GRACE_DAYS = 30  # after its ends, everything works, with warnings
EVALUATION_PAIRS = 2
CLOCK_SLACK = timedelta(minutes=5)  # the newest run ahead of now by more: the clock went back
LICENCE_PAUSE = "run_paused_licence"  # a soak's boundary paused it: its resume needs the licence
OPEN = "open"  # the standing's state in a build that embeds no vendor key: nothing is enforced
# The vendor's public keys (PEM, SPKI, P-256), exactly the trusted ones tests/fixtures/vendor-keys.txt
# lists: none in an open build, which enforces no licence.
SHIPPED_KEYS: tuple[bytes, ...] = ()
VENDOR_KEYS = SHIPPED_KEYS  # the only trust root, read at each call: licence tests set their own key
# The fingerprints of the keys a release stopped trusting after a compromise, each with that day, exactly
# the removed ones vendor-keys.txt lists: a row one signed grants nothing and is not warned of.
REMOVED_KEYS: dict[str, str] = {}
_LICENCE_ID = re.compile(r"LI-[0-9A-Z]{26}")
_INSTALLATION = re.compile(r"I-[0-9a-f]{16}")
_PAIRS = re.compile(r"unlimited|[1-9][0-9]{0,3}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ISSUED = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_KEY = re.compile(r"[0-9a-f]{64}")
_FORMAT_LINE = re.compile(r"format: deltasleuth-licence/([1-9][0-9]*)")
_INSTALL = "(ds licence install FILE)"
_UNAVAILABLE = "licence status unavailable ({}): see the server log"
log = logging.getLogger(__name__)
_logged: set[str] = set()  # the store's problems already logged by this process


class LicenceError(ValueError):
    """A licence file or row refused by ``parse``, ``install`` or ``withdraw``: nothing is written, and
    the CLI exits 2."""


class LicenceRefused(Exception):  # neither ValueError nor LookupError: never a 422 or 404
    """New work the licence does not allow: the API answers 409, the CLI exits 2."""


@dataclass(frozen=True)
class Licence:
    """A licence file as ``parse`` read it from its normalised ``text``."""

    licence_id: str
    customer: str
    order: str
    installation_id: str
    tier: str
    pairs: int | None  # None: unlimited
    starts: date
    ends: date
    issued: datetime
    key: str  # the fingerprint of the vendor key that signed it
    signature: bytes
    text: str

    @property
    def body(self) -> bytes:
        """What the signature signs: the text but its last line."""
        return self.text[: self.text.rindex("\n", 0, -1) + 1].encode()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    @property
    def since(self) -> datetime:
        """Its starts at 00:00 UTC: the window its pairs are counted in opens then."""
        return datetime.combine(self.starts, time(), UTC)

    def fields(self) -> dict[str, Any]:
        """Its values as the ``licence.install`` audit row and ``GET /v1/licence`` show them."""
        return {
            "licence_id": self.licence_id, "sha256": self.sha256, "customer": self.customer,
            "order": self.order, "installation_id": self.installation_id, "tier": self.tier,
            "pairs": self.pairs, "starts": self.starts.isoformat(), "ends": self.ends.isoformat(),
            "issued": f"{self.issued:%Y-%m-%dT%H:%M:%SZ}", "key": self.key,
        }  # fmt: skip

    def named(self) -> str:
        return f"licence {self.licence_id} ({self.tier}, {self.starts} to {self.ends})"


def body(fields: Mapping[str, Any]) -> str:
    """The signed text of a licence of ``fields`` (``licence`` to ``key``, as the file writes them): the
    format line and theirs, each ending in LF."""
    return f"format: {FORMAT}\n" + "".join(f"{name}: {fields[name]}\n" for name in FIELDS[1:-1])


def _not(name: str, why: str) -> LicenceError:
    return LicenceError(f"{name} is not a DeltaSleuth licence ({FORMAT}): {why}")


def normalise(data: bytes, name: str = "the file") -> str:
    """The licence text as stored: ``data`` without a leading BOM, each line's trailing CR and blanks,
    and empty lines, so that an e-mailed file verifies (no value ends with a blank, so no signed byte
    is lost). LicenceError past 4096 bytes or for text that is not UTF-8."""
    if len(data) > MAX_BYTES:
        raise _not(name, f"over {MAX_BYTES} bytes")
    try:
        text = data.decode("utf-8").removeprefix("\ufeff")
    except UnicodeDecodeError:
        raise _not(name, "not UTF-8 text") from None
    lines = (line.removesuffix("\r").rstrip(" \t") for line in text.split("\n"))
    return "".join(f"{line}\n" for line in lines if line)


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if _DATE.fullmatch(value) else None
    except ValueError:  # February 30th
        return None


def _issued(value: str) -> datetime | None:
    try:
        return (
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            if _ISSUED.fullmatch(value)
            else None
        )
    except ValueError:
        return None


def _base64(value: str) -> bytes | None:
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:  # binascii.Error, or text that is not ASCII
        return None


def _read(text: str, name: str) -> Licence:
    """The licence a normalised text holds, its signature unchecked: LicenceError for a later format,
    and for anything but exactly the twelve lines of ``FIELDS``."""
    lines = text.split("\n")[:-1]
    found = _FORMAT_LINE.fullmatch(lines[0]) if lines else None
    if found is None:
        raise _not(name, f"line 1: expected 'format: {FORMAT}'")
    if found[1] != "1":
        raise LicenceError(
            f"licence format 'deltasleuth-licence/{found[1]}' is newer than this build reads: upgrade"
            " DeltaSleuth"
        )
    values = {}
    for number, field in enumerate(FIELDS, 1):
        line = lines[number - 1] if number <= len(lines) else ""
        if not line.startswith(f"{field}: "):
            raise _not(name, f"line {number}: expected '{field}: …'")
        values[field] = line.removeprefix(f"{field}: ")
    if len(lines) > len(FIELDS):
        raise _not(name, f"line {len(FIELDS) + 1}: expected the end of the file")

    def check(ok: Any, why: str) -> None:
        if not ok:
            raise _not(name, why)

    check(_LICENCE_ID.fullmatch(values["licence"]), "licence is not LI- and 26 of 0-9 and A-Z")
    for field, most in (("customer", 200), ("order", 100)):
        value = values[field]
        check(
            1 <= len(value) <= most and printable(value), f"{field} is not 1 to {most} printable characters"
        )
        check(value == value.strip(), f"{field} begins or ends with a blank")
    check(_INSTALLATION.fullmatch(values["installation"]), "installation is not I- and 16 of 0-9 and a-f")
    check(values["tier"] in TIERS, "tier is not community, pro or enterprise")
    check(_PAIRS.fullmatch(values["pairs"]), "pairs is not unlimited or 1 to 9999")
    starts, ends = _date(values["starts"]), _date(values["ends"])
    check(starts, "starts is not a date (YYYY-MM-DD)")
    check(ends, "ends is not a date (YYYY-MM-DD)")
    assert starts is not None and ends is not None
    check(starts <= ends, "ends is before starts")
    check((ends - starts).days <= MAX_DAYS, f"ends is more than {MAX_DAYS} days after starts")
    check(issued := _issued(values["issued"]), "issued is not a UTC time (YYYY-MM-DDTHH:MM:SSZ)")
    check(_KEY.fullmatch(values["key"]), "key is not a fingerprint (64 of 0-9 and a-f)")
    check(signature := _base64(values["signature"]), "signature is not base64")
    pairs = values["pairs"]
    return Licence(
        values["licence"], values["customer"], values["order"], values["installation"], values["tier"],
        None if pairs == "unlimited" else int(pairs), starts, ends, issued, values["key"], signature, text,  # type: ignore[arg-type]
    )  # fmt: skip


def parse(data: bytes, keys: Sequence[bytes] | None = None, *, name: str = "the file") -> Licence:
    """The licence in ``data`` (a file as received, ``name`` in messages), verified against ``keys``
    (default ``VENDOR_KEYS``), checking in order: a trusted key exists (an open build has none); the
    format line; the grammar; its key is trusted; its signature verifies. LicenceError otherwise, echoing
    only accepted values."""
    keys = VENDOR_KEYS if keys is None else keys
    if not keys:
        raise LicenceError("this build embeds no vendor key: it needs no licence and can verify none")
    found = _read(normalise(data, name), name)
    trusted = {signing.fingerprint(pem): pem for pem in keys}
    key = f"{found.key[:12]}…"
    if found.key not in trusted:
        raise LicenceError(
            f"licence {found.licence_id} is signed by key {key}, which this build does not trust: upgrade"
            " DeltaSleuth, or ask for a licence signed by a current key"
        )
    if not signing.verifies(trusted[found.key], found.signature, found.body):
        raise LicenceError(
            f"licence {found.licence_id} does not verify with its key {key}: the file was changed after it"
            " was signed; install the file as received"
        )
    return found


def new_id() -> str:
    """A new ``LI-`` id (``L-`` names LLM calls)."""
    return repo._sortable_id("LI-")


@functools.lru_cache(maxsize=256)
def _verdict(text: str, keys: tuple[bytes, ...], installation_id: str | None) -> Licence | str:
    """A stored text's licence, or why it grants nothing: parsed once per text. No text written with SQL
    makes a decision raise."""
    try:
        found = parse(text.encode(), keys, name="its text")
    except LicenceError as exc:
        return str(exc)
    except Exception as exc:
        return f"its text cannot be read ({type(exc).__name__})"
    if found.installation_id != installation_id:
        return f"it is for installation {found.installation_id}, not this one ({installation_id})"
    return found


def _retired(text: str) -> str | None:
    """Why a stored text grants nothing when a release removed its key after a compromise: its
    reissue replaces it, so it is logged, not warned of; None for any other text."""
    try:
        key = _read(text, "its text").key
    except Exception:
        return None
    if key not in REMOVED_KEYS:
        return None
    return (
        f"signed by key {key[:12]}…, withdrawn on {REMOVED_KEYS[key]} after a compromise; its reissue"
        " replaces it"
    )


def _in_force(licences: Sequence[Licence], day: date) -> Licence | None:
    """The licence in force on ``day``: the latest starting, then the latest issued."""
    running = [found for found in licences if found.starts <= day <= found.ends]
    return max(running, key=lambda found: (found.starts, found.issued), default=None)


def _pairs(found: Licence) -> float:
    return math.inf if found.pairs is None else found.pairs


def _list(names: Sequence[str], last: str = ", ") -> str:
    """The names, the last joined by ``last``; past ten, ``… and 3 more``."""
    if len(names) > 10:
        return f"{', '.join(names[:10])}, … and {len(names) - 10} more"
    return last.join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)


@dataclass(frozen=True)
class Standing:
    """What the installation may do now, as ``judge`` found it, with the pairs counted in its
    window (``standing``)."""

    installation_id: str | None
    now: datetime  # the database's
    state: str  # evaluation, valid, ending, grace or expired; open in a build without a vendor key
    licence: Licence | None  # the licence named; None in evaluation and in an open build
    renewal: Licence | None = None  # the one in force the day after the running licence ends
    pending: tuple[Licence, ...] = ()  # those that apply from a later day, but a renewal warned of or silent
    sso: bool = False  # the installation runs SSO: this process, or its server's last start
    oidc: bool = False  # this process is in oidc mode
    packs: bool = False  # evidence packs are configured: here, or at the server's last start
    clock_behind: timedelta | None = None  # how far the newest run is ahead of now, past 5 minutes
    uncounted: tuple[tuple[str, str], ...] = ()  # (licence_id, why) of the stored rows that grant nothing
    retired: tuple[tuple[str, str], ...] = ()  # the same of those signed by a removed key: never warned
    missing: tuple[tuple[str, int], ...] = ()  # (licence_id, audit row id) of installs without their row
    counted: tuple[repo.Row, ...] = ()  # {name, first_at} of each pair in the window, by first run
    rows: tuple[repo.Row, ...] = ()  # the stored licences, withdrawn ones and those that grant nothing too

    @property
    def today(self) -> date:
        return self.now.astimezone(UTC).date()

    @property
    def limit(self) -> int | None:
        """How many pairs are seated; None: unlimited, as in an open build, which counts none."""
        if self.state == OPEN:
            return None
        return EVALUATION_PAIRS if self.licence is None else self.licence.pairs

    @property
    def since(self) -> datetime | None:
        """Where the window of counted pairs opens; None in evaluation: every run ever."""
        return None if self.licence is None else self.licence.since

    @property
    def names(self) -> list[str]:
        """The counted pairs, by first run in the window."""
        return [row["name"] for row in self.counted]

    @property
    def seats(self) -> list[str]:
        """The pairs that run: the first ``limit`` counted."""
        return self.names if self.limit is None else self.names[: self.limit]

    @property
    def enterprise(self) -> bool:
        """Whether Enterprise's features are allowed: under such a licence, and in an open build."""
        return self.state == OPEN or (self.licence is not None and self.licence.tier == "enterprise")

    @property
    def sso_fenced(self) -> bool:
        """The installation runs SSO without an Enterprise licence (evaluation included)."""
        return self.sso and not self.enterprise

    def record(self) -> dict[str, Any] | None:
        """A new run's ``params["licence"]``: never the customer; None in an open build, whose runs
        carry no record and so no label."""
        if self.state == OPEN:
            return None
        found = self.licence
        record = {
            "licence_id": found and found.licence_id,
            "tier": found and found.tier,
            "state": self.state,
            "ends": found and found.ends.isoformat(),
        }
        return record | ({"clock_behind": True} if self.clock_behind else {})

    def detail(self) -> dict[str, Any]:
        """What ``system.start`` records of it: ``pairs`` is the limit, None when unlimited."""
        found = self.licence
        return {
            "installation_id": self.installation_id, "state": self.state,
            "licence_id": found and found.licence_id, "tier": found and found.tier, "pairs": self.limit,
            "ends": found and found.ends.isoformat(),
        }  # fmt: skip

    def allows(self) -> dict[str, bool]:
        """What ``GET /v1/licence`` says it allows: new runs, runs under SSO, sign-off and packs."""
        return {
            "runs": self.refusal("run") is None,
            "sso": self.enterprise and self.state != "expired",
            "signoff": self.refusal("signoff") is None,
            "packs": self.refusal("packs") is None,
        }

    def _whose(self) -> str:
        if self.licence is None:
            return "this installation is an unlicensed evaluation"
        return f"licence {self.licence.licence_id} is {self.licence.tier}"

    def refusal(self, feature: str) -> str | None:
        """Why the licence refuses ``feature``: ``run`` (a creation, a compare, a soak's next cycle, a
        resume from a licence pause) when it has expired or SSO needs an Enterprise licence, ``signoff``
        without an Enterprise licence, ``packs`` when it has expired or without one; None when it allows
        it."""
        found, whose = self.licence, self._whose()
        if feature == "signoff":
            if self.enterprise:
                return None
            if found is None:
                return (
                    f"four-eyes sign-off is an Enterprise feature and {whose}: install an Enterprise licence"
                    f" for installation {self.installation_id}"
                )
            return f"four-eyes sign-off is an Enterprise feature and {whose}"
        if self.state == "expired":
            assert found is not None
            return (
                f"licence {found.licence_id} ended on {found.ends} and its {GRACE_DAYS} days of grace on"
                f" {found.ends + timedelta(days=GRACE_DAYS)}: new runs, compares, soak cycles and evidence"
                f" packs are refused until a renewed licence is installed {_INSTALL}; reads, reports,"
                " exports, holds, erasure, purge, audit, pack downloads and sign-offs under an Enterprise"
                " licence keep working"
            )
        if feature == "run":
            if not self.sso_fenced:
                return None
            tail = "; keep SSO, which evidence packs need" if self.packs else " or tokens mode"
            text = (
                f"SSO (auth.oidc) is an Enterprise feature and {whose}: under SSO this installation keeps"
                " serving sign-in, reads, reports, exports, pack downloads and administration, and new runs,"
                f" compares and soak cycles need an Enterprise licence {_INSTALL}{tail}"
            )
            return text if self.oidc else f"{text} (this installation's server last started in oidc mode)"
        if feature == "packs":
            if self.enterprise:
                return None
            if found is None:
                return (
                    "no evidence pack is issued in an unlicensed evaluation: install an Enterprise licence"
                    f" for installation {self.installation_id}"
                )
            return f"evidence packs are an Enterprise feature and {whose}"
        raise ValueError(f"unknown licensed feature {feature!r}")

    def unseated(self, name: str) -> str | None:
        """The refusal when the licence counts pairs and ``name`` holds no seat; else None."""
        if self.limit is None or name in self.seats:
            return None
        seats, found = _list(self.seats), self.licence
        if found is None:
            return (
                f"spec {name} is not one of the {EVALUATION_PAIRS} pairs an unlicensed evaluation runs"
                f" ({seats}): its cycles under way end, and a new run or cycle needs a licence for"
                f" installation {self.installation_id}"
            )
        return (
            f"spec {name} is not one of the {self.limit} pairs {found.named()} seats since {found.starts}"
            f" ({seats}): its cycles under way end, and a new run or cycle needs a licence with more pairs"
        )

    def paused(self, name: str) -> tuple[str, dict[str, Any]] | None:
        """Why a soak of ``name`` starts its next cycle paused (the licence expired or SSO needs an
        Enterprise one, else the name holds no seat), and what its ``paused`` row records of it: the run's
        record and the refusal, ``expired``, ``sso`` or ``unseated`` (with the pairs counted, the limit and
        the window's ``since``), as data: never the message, whose numbers and names a pack's raw-value
        gate would read as keys. None: it goes on."""
        record = self.record() or {}  # an open build pauses none
        if (why := self.refusal("run")) is not None:
            return why, record | {"refusal": "expired" if self.state == "expired" else "sso"}
        if (why := self.unseated(name)) is None:
            return None
        since = self.licence and self.licence.starts.isoformat()
        counted = {"count": len(self.counted), "limit": self.limit, "since": since}
        return why, record | {"refusal": "unseated"} | counted

    def full(self, name: str) -> str:
        """The refusal, a licence's or an evaluation's: ``name`` would be a new pair past the limit."""
        seats, found = _list(self.seats), self.licence
        if found is None:
            return (
                f"spec {name} would be a third pair: an unlicensed evaluation runs {EVALUATION_PAIRS} pairs"
                f" ({seats}); install a licence for installation {self.installation_id}"
            )
        return (
            f"spec {name} would be pair {len(self.counted) + 1} of {self.limit} under {found.named()}: its"
            f" seats since {found.starts} are {seats}, whose runs go ahead; a new pair needs a licence with"
            " more pairs"
        )

    @property
    def problems(self) -> list[str]:
        """The stored rows that grant nothing, but retired ones, and the installs whose row is missing, one
        line each."""
        return [
            f"licence {licence_id} in the store does not count: {why}" for licence_id, why in self.uncounted
        ] + [
            f"licence {licence_id} was installed (audit row {audit_id}) but is missing from the store:"
            f" install it again {_INSTALL}"
            for licence_id, audit_id in self.missing
        ]

    def warnings(self) -> list[str]:
        """What every screen and ``ds serve``, ``worker``, ``run`` and ``compare`` warn of: nothing
        in an open build."""
        if self.state == OPEN:
            return []
        found, lines = self.licence, []
        if found is None:
            lines.append(
                f"Unlicensed evaluation of installation {self.installation_id}: at most {EVALUATION_PAIRS}"
                f" pairs ({len(self.counted)} used), no runs under SSO, no sign-off or evidence packs; not"
                " licensed for production use (ds licence)"
            )
        lines += [f"licence {later.licence_id} applies from {later.starts}" for later in self.pending]
        if found is not None and self.state == "ending":
            days = (found.ends - self.today).days
            when = "today" if days == 0 else f"in {days} day{'s' if days != 1 else ''}"
            line = (
                f"licence {found.licence_id} ends on {found.ends} ({when}): install its renewal {_INSTALL};"
                f" new runs and evidence packs stop on {found.ends + timedelta(days=GRACE_DAYS + 1)}"
            )
            if (later := self.renewal) is not None:  # lesser, or the state would be valid
                pairs = "unlimited" if later.pairs is None else later.pairs
                line += f"; {later.licence_id} ({later.tier}, {pairs} pairs) follows from {later.starts}"
            lines.append(line)
        if found is not None and self.state == "grace":
            lines.append(
                f"licence {found.licence_id} ended on {found.ends}: everything works until"
                f" {found.ends + timedelta(days=GRACE_DAYS)}, then new runs and evidence packs stop; install"
                f" its renewal {_INSTALL}"
            )
        if (refused := self.refusal("run")) is not None:  # expired, else SSO without Enterprise
            lines.append(refused)
        if found is not None and found.pairs is not None and self.state != "expired":
            counted = len(self.counted)
            if counted >= found.pairs:
                over = _list(self.names[found.pairs :], " and ")
                then = f"{over} get no new run or cycle" if counted > found.pairs else "a new pair is refused"
                lines.append(
                    f"licence {found.licence_id} allows {found.pairs} pairs and {counted} have runs since"
                    f" {found.starts}: {then}"
                )
        if self.clock_behind is not None:
            newest = (self.now + self.clock_behind).astimezone(UTC)
            lines.append(
                f"the database clock is {self.clock_behind.days} day(s) behind the newest run"
                f" ({newest:%Y-%m-%dT%H:%MZ}): licence dates are judged by a clock that went back"
            )
        return lines + self.problems


def judge(facts: Mapping[str, Any], now: datetime, auth_mode: str, *, packs: bool = False) -> Standing:
    """The standing ``facts`` (``repo.licence_facts``) give at ``now`` for a process in ``auth_mode``,
    ``packs`` when it has evidence packs configured, its pairs not yet counted. A stored row
    counts when it verifies against ``VENDOR_KEYS``, names this installation and was not withdrawn
    before its starts; one signed by a key in ``REMOVED_KEYS`` is ``retired``, the others ``uncounted``.
    The licence named is the one running (the latest starting, then issued), else the one in its grace
    (the latest ending, then issued), else the one ended latest; else none: an evaluation. Without a
    vendor key the standing is open and no row is read."""
    installation_id, today = facts["installation_id"], now.astimezone(UTC).date()
    if not VENDOR_KEYS:
        return Standing(installation_id, now, OPEN, None, rows=tuple(facts["rows"]))
    good: list[Licence] = []
    uncounted, retired = [], []
    for row in facts["rows"]:
        found = _verdict(row["text"], tuple(VENDOR_KEYS), installation_id)
        if isinstance(found, str) and (why := _retired(row["text"])) is not None:
            retired.append((row["licence_id"], why))
        elif isinstance(found, str):
            uncounted.append((row["licence_id"], found))
        elif row["withdrawn_at"] is None or row["withdrawn_at"] >= found.since:
            good.append(found)
    grace = timedelta(days=GRACE_DAYS)
    graced = [found for found in good if found.ends < today <= found.ends + grace]
    ended = [found for found in good if found.ends + grace < today]
    named, state, renewal, renewed = _in_force(good, today), "evaluation", None, False
    if named is not None:
        renewal = _in_force(good, named.ends + timedelta(days=1))
        renewed = renewal is not None and (
            TIERS.index(renewal.tier) >= TIERS.index(named.tier) and _pairs(renewal) >= _pairs(named)
        )
        state = "ending" if today >= named.ends - timedelta(days=WARN_DAYS) and not renewed else "valid"
    elif graced:
        named, state = max(graced, key=lambda found: (found.ends, found.issued)), "grace"
    elif ended:
        named, state = max(ended, key=lambda found: (found.ends, found.issued)), "expired"
    stored = {row["licence_id"] for row in facts["rows"]}
    missing = {row["object"]: row["id"] for row in facts["installs"] if row["object"] not in stored}
    start = facts["start"] or {}
    behind = None if facts["newest_run_at"] is None else facts["newest_run_at"] - now
    silent = renewal if renewed or state == "ending" else None  # silenced, or named in the ending warning
    later = sorted(
        (found for found in good if found.starts > today and found != silent), key=lambda f: f.starts
    )
    return Standing(
        installation_id,
        now,
        state,
        named,
        renewal,
        tuple(later),
        sso=auth_mode == "oidc" or start.get("auth") == "oidc",
        oidc=auth_mode == "oidc",
        packs=packs or start.get("pack_key") is not None,
        clock_behind=behind if behind is not None and behind > CLOCK_SLACK else None,
        uncounted=tuple(uncounted),
        retired=tuple(retired),
        missing=tuple(missing.items()),
        rows=tuple(facts["rows"]),
    )


async def standing(ctx: Context, conn: repo.Conn, *, at: datetime | None = None) -> Standing:
    """The installation's standing now, as each decision reads it, or as of ``at`` when that is later
    (a soak's cycle, judged as of when it falls due), with its window's pairs; each stored row that
    grants nothing, and each install whose row is missing, logged once (a retired row at INFO). An open
    build's counts no pair."""
    facts = await repo.licence_facts(conn)
    now = facts["now"] if at is None else max(facts["now"], at)
    judged = judge(facts, now, ctx.settings.auth_mode, packs=ctx.settings.packs is not None)
    if judged.state == OPEN:
        return judged
    retired = [(logging.INFO, f"licence {licence_id} in the store does not count: {why}")
               for licence_id, why in judged.retired]  # fmt: skip
    for level, line in [(logging.WARNING, line) for line in judged.problems] + retired:
        if line not in _logged:
            _logged.add(line)
            log.log(level, line)
    return replace(judged, counted=tuple(await repo.counted_pairs(conn, judged.since)))


def _refuse(message: str) -> LicenceRefused:
    log.info(message)
    return LicenceRefused(message)


async def need(
    ctx: Context,
    conn: repo.Conn,
    feature: str,
    *,
    name: str | None = None,
    prefix: str = "",
    at: datetime | None = None,
) -> Standing:
    """The standing (as of ``at``, if later than now), when it allows ``feature`` (``Standing.refusal``)
    and seats ``name`` if given; else LicenceRefused, its message after ``prefix``."""
    current = await standing(ctx, conn, at=at)
    why = current.refusal(feature) or (None if name is None else current.unseated(name))
    if why is not None:
        raise _refuse(prefix + _falls_due(current, at) + why)
    return current


def _falls_due(current: Standing, at: datetime | None) -> str:
    """How a refusal judged as of a soak cycle's due time, later than now, begins; else nothing."""
    if at is None or current.now != at:
        return ""
    return f"its cycle falls due on {at.astimezone(UTC):%Y-%m-%dT%H:%MZ}, when "


async def admit(
    ctx: Context, conn: repo.Conn, spec: CompiledSpec, *, lock: bool = False
) -> tuple[dict[str, Any] | None, tuple[str, dict[str, Any]] | None]:
    """Whether a new run of ``spec`` may start, as ``approvals.gate`` decides its approval: the run's
    ``params["licence"]`` record and, for a new pair, its ``licence.pair`` audit row, ``(object, {since,
    name, count, limit, targets})``, the caller adding ``run_id`` (None for a seated name, or when the
    pairs are unlimited). LicenceRefused: when ``need`` refuses a run; for a counted name without a seat;
    for a new name once ``limit`` names are counted. With ``lock`` (in the transaction that inserts the
    run), a new name recounts under the pair lock, so two new names at the limit give one run and one
    refusal. An open build admits every name with neither a record nor a row."""
    current = await need(ctx, conn, "run")
    name, limit = spec.name, current.limit
    if limit is None or name in current.seats:
        return current.record(), None
    if name not in current.names and lock:
        await repo.lock_pairs(conn)
        current = replace(current, counted=tuple(await repo.counted_pairs(conn, current.since)))
        if name in current.seats:
            return current.record(), None
    if name in current.names:
        raise _refuse(current.unseated(name))  # type: ignore[arg-type]
    if len(current.names) >= limit:
        raise _refuse(current.full(name))
    found, targets = current.licence, await repo.pair_targets(conn, [name])
    detail = {
        "since": found and found.starts.isoformat(), "name": name, "count": len(current.names) + 1,
        "limit": limit, "targets": targets.get(name, []),
    }  # fmt: skip
    return current.record(), (found.licence_id if found else str(current.installation_id), detail)


async def stopped(
    ctx: Context, run: Mapping[str, Any], spec: CompiledSpec, at: datetime
) -> dict[str, Any] | None:
    """Whether a soak's next cycle, due at ``at``, starts paused at its boundary, judged as of then
    (now, if later), as ``StopConditions.at_boundary`` judges ``until``: its ``paused`` row's ``licence``
    (``Standing.paused``: a refused run, else a name that holds no seat), the message logged; None when it
    goes on, never counted. Any failure but an unreachable store's is logged and lets the soak go on: the
    sweep would otherwise fail a live soak."""
    try:
        async with ctx.pool.connection() as conn:
            current = await standing(ctx, conn, at=at)
        why = current.paused(spec.name)
    except psycopg.OperationalError:
        raise
    except Exception as exc:
        log.warning(
            "licence check at the boundary of run %s failed (%s): the soak goes on",
            run["run_id"],
            type(exc).__name__,
        )
        return None
    if why is None:
        return None
    log.info("run %s pauses at its boundary: %s%s", run["run_id"], _falls_due(current, at), why[0])
    return why[1]


def pack_problems(current: Standing, run: Mapping[str, Any]) -> list[str]:
    """Why the licence gives the run's cycles no evidence pack: that it has expired or is not Enterprise,
    and that the run was made in evaluation, whose label no certificate may contradict (in an open build
    too); a run without a record is judged by the licence now."""
    found = [why] if (why := current.refusal("packs")) is not None else []
    record = run["params"].get("licence")
    if record is not None and record["state"] == "evaluation":
        found.append(
            f"run {run['run_id']} was made in an unlicensed evaluation: no evidence pack is issued for it;"
            f" start a new run{'' if current.state == OPEN else ' under the licence'}"
        )
    return found


async def watch(ctx: Context) -> None:
    """Audit ``licence.state`` as ``system:worker`` when the installation's state or licence differs from
    the newest such row's: the SIEM's alert 30 days ahead, at the end, after the grace, and the
    one row that says a build is open. Never raises: a failure is logged."""
    try:
        async with repo.audited(ctx.pool) as (conn, audit):
            current, last = await standing(ctx, conn), await repo.last_audit(conn, "licence.state")
            was, found = last and last["detail"], current.licence
            licence_id = found and found.licence_id
            if was is None or (was["to"], was["licence_id"]) != (current.state, licence_id):
                detail = {"from": was and was["to"], "to": current.state, "licence_id": licence_id}
                level = logging.INFO if current.state == OPEN else logging.WARNING
                log.log(level, "licence state %s -> %s (%s)", detail["from"], current.state, licence_id)
                detail["ends"] = found and found.ends.isoformat()
                await audit(WORKER, "licence.state", str(current.installation_id), detail)
    except Exception:  # the next hour tries again
        log.warning("licence state not watched", exc_info=True)


async def install(ctx: Context, data: bytes, *, who: Principal, name: str = "the file") -> dict[str, Any]:
    """Install the licence in ``data`` as ``who``: ``parse``; then, in one transaction under the pair lock,
    for this installation and not past its grace, a licence withdrawn or installed with other content is
    refused, the same one installed already writes nothing (``installed`` false), else it is stored and
    ``licence.install`` audited last. Returns ``{licence, installed, applies, before, after}``, the
    standings before and after."""
    found = parse(data, name=name)
    async with repo.audited(ctx.pool) as (conn, audit):
        await repo.lock_pairs(conn)  # installs serialise, and take it before the audit lock as runs do
        before = await standing(ctx, conn)
        if found.installation_id != before.installation_id:
            ours = before.installation_id
            raise LicenceError(
                f"licence {found.licence_id} is for installation {found.installation_id}, not this one"
                f" ({ours}): ask for a licence for {ours} (ds licence id)"
            )
        if found.ends + timedelta(days=GRACE_DAYS) < before.today:
            raise LicenceError(
                f"licence {found.licence_id} ended on {found.ends} and its grace on"
                f" {found.ends + timedelta(days=GRACE_DAYS)}: install a current licence"
            )
        stored = await repo.get_licence(conn, found.licence_id)
        if stored is not None and stored["withdrawn_at"] is not None:
            on = stored["withdrawn_at"].astimezone(UTC)
            raise LicenceError(
                f"licence {found.licence_id} was withdrawn on {on:%Y-%m-%d}: it is not installed again; ask"
                " for a new licence"
            )
        if stored is not None and stored["sha256"] != found.sha256:
            raise LicenceError(
                f"licence {found.licence_id} is installed already with other content: ask your DeltaSleuth"
                " contact which file is genuine"
            )
        if stored is not None:
            return {"licence": found, "installed": False, "applies": None, "before": before, "after": before}
        await repo.insert_licence(conn, found.licence_id, found.text, found.sha256, installed_by=who.actor)
        after = await standing(ctx, conn)
        named = after.licence
        if named is not None and named.licence_id == found.licence_id:
            applies = "now"
        elif found.starts > after.today:
            applies = f"from {found.starts}"
        else:
            applies = f"no: {named and named.licence_id} applies"
        detail = {field: value for field, value in found.fields().items() if field != "licence_id"}
        await audit(who, "licence.install", found.licence_id, detail | {"applies": applies})
    return {"licence": found, "installed": True, "applies": applies, "before": before, "after": after}


async def withdraw(ctx: Context, licence_id: str, reason: str, *, who: Principal) -> dict[str, Any]:
    """Withdraw an installed licence whose starts has not come as ``who``, once, as of the
    database's now(), and audit ``licence.withdraw``; LicenceError otherwise (a started one is refused). A
    started licence is only replaced, so no withdrawal returns an installation to evaluation."""
    if not (1 <= len(reason) <= 200 and printable(reason)):
        raise LicenceError("--reason must be 1 to 200 characters without control characters")
    async with repo.audited(ctx.pool) as (conn, audit):
        stored = await repo.get_licence(conn, licence_id)
        if stored is None:
            raise LicenceError(f"licence {licence_id} is not installed")
        if stored["withdrawn_at"] is not None:
            raise LicenceError(f"licence {licence_id} is withdrawn already")
        starts = _read(stored["text"], "its text").starts
        row = await repo.withdraw_licence(conn, licence_id, who.actor, reason)
        if row is None:  # a concurrent withdrawal committed first
            raise LicenceError(f"licence {licence_id} is withdrawn already")
        if row["withdrawn_at"] >= datetime.combine(starts, time(), UTC):  # raising rolls it back
            raise LicenceError(
                f"licence {licence_id} started on {starts}: a started licence is replaced by a new one, not"
                " withdrawn"
            )
        await audit(who, "licence.withdraw", licence_id, {"reason": reason})
    return row


def run_label(run: Mapping[str, Any]) -> str | None:
    """What ``ds runs``, the Run screen and report.pdf say of the licence a run was made under, from
    its ``params["licence"]``; None for a run without a record (before 010, or an older build's)."""
    record, run_id = run["params"].get("licence"), run["run_id"]
    if record is None:
        return None
    if record["state"] == "evaluation":
        text = (
            f"Unlicensed evaluation: run {run_id} was made without a licence. Not for production use; no"
            " evidence pack is issued for it."
        )
    elif record["state"] == "grace":
        text = (
            f"Run {run_id} was made in the {GRACE_DAYS}-day grace of licence {record['licence_id']}, which"
            f" ended on {record['ends']}."
        )
    else:
        text = f"Run {run_id} was made under licence {record['licence_id']} ({record['tier']})."
    if record.get("clock_behind"):
        text += " The database clock was behind the newest run when it was made."
    return text


def file_name(run: Mapping[str, Any], name: str) -> str:
    """The name a run's report ``name`` downloads and is written as: ``R-…-<name>``, or
    ``R-…-unlicensed-evaluation-<name>`` for a run made in evaluation."""
    record = run["params"].get("licence")
    evaluation = record is not None and record["state"] == "evaluation"
    return f"{run['run_id']}-unlicensed-evaluation-{name}" if evaluation else f"{run['run_id']}-{name}"


async def shown(ctx: Context, say: Callable[[str], None] = log.warning) -> tuple[Standing | None, list[str]]:
    """The standing and its warnings as a process starts, each given to ``say`` (logged; the CLI prints
    them); never raises: when the standing cannot be read, None and one warning, the failure logged."""
    try:
        async with ctx.pool.connection() as conn:
            current = await standing(ctx, conn)
    except Exception as exc:  # startup never fails for the licence
        log.warning("licence status unavailable", exc_info=True)
        current, lines = None, [_UNAVAILABLE.format(type(exc).__name__)]
    else:
        lines = current.warnings()
    for line in lines:
        say(line)
    return current, lines


def _shown(found: Licence | None) -> dict[str, Any] | None:
    return found and {name: value for name, value in found.fields().items() if name != "installation_id"}


def _installed(current: Standing, row: repo.Row) -> dict[str, Any]:
    """A stored licence as ``GET /v1/licence`` lists it: its dates and tier when it verifies, who installed
    or withdrew it, whether it is in force and, if it grants nothing, why (in an open build, that it
    embeds no vendor key)."""
    found = _verdict(row["text"], tuple(VENDOR_KEYS), current.installation_id)
    read = found if isinstance(found, Licence) else None
    named, licence_id = current.licence, row["licence_id"]
    why = str(found) if current.state == OPEN else dict(current.uncounted + current.retired).get(licence_id)
    people = ("installed_by", "installed_by_name", "installed_at", "withdrawn_by", "withdrawn_by_name",
              "withdrawn_at", "withdraw_reason")  # fmt: skip
    return (
        {"licence_id": licence_id, "tier": read and read.tier}
        | {"starts": read and read.starts.isoformat(), "ends": read and read.ends.isoformat()}
        | {name: row[name] for name in people}
        | {"in_force": named is not None and named.licence_id == licence_id and current.state != "expired"}
        | {"problem": why}
    )


async def status(ctx: Context) -> dict[str, Any]:
    """``GET /v1/licence``'s body, which ``ds licence`` prints: the installation, its state and
    warnings, the licence named and its renewal, what it allows, the pairs counted in its window with their
    seats and targets, and each stored licence. Never raises: ``state: unknown`` when the standing cannot
    be read, the failure logged, so that no read depends on the licence."""
    try:
        async with ctx.pool.connection() as conn:
            current = await standing(ctx, conn)
            targets = await repo.pair_targets(conn, current.names)
    except Exception as exc:
        log.warning("licence status unavailable", exc_info=True)
        empty = ("installation_id", "licence", "renewal", "allows", "pairs", "clock_behind_seconds")
        why = [_UNAVAILABLE.format(type(exc).__name__)]
        return dict.fromkeys(empty) | {"state": "unknown", "warnings": why, "installed": []}
    found, behind = current.licence, current.clock_behind
    counted = [
        {"name": row["name"], "first_at": row["first_at"], "seated": row["name"] in current.seats}
        | {"targets": targets.get(row["name"], [])}
        for row in current.counted
    ]
    return {
        "installation_id": current.installation_id,
        "state": current.state,
        "warnings": current.warnings(),
        "licence": _shown(found),
        "renewal": _shown(current.renewal),
        "allows": current.allows(),
        "pairs": {"limit": current.limit, "since": found and found.starts.isoformat(), "counted": counted},
        "clock_behind_seconds": behind and int(behind.total_seconds()),
        "installed": [_installed(current, row) for row in current.rows],
    }
