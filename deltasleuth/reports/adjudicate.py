"""Adjudication, the onboarding exit: a reviewer sheet for at most 50 keys of a run, and its score.

Selection is deterministic (a salted SHA-256 of the masked key orders every choice) and
stratified. Each engine outcome gets a quota: one key, and MATCH a reserved share (``MATCH_SHARE``
of the limit, capped by MATCH's share of the run), because an engine MATCH a human would call
DIFFERENT is the error the exit most needs to rule out. Then, until the limit:

1. one key per root cause, most severe then most widespread first, while the quota still fits;
   a key whose primary root cause it is is preferred;
2. the outcome quota, MATCH first;
3. round-robin over the strata (outcome, primary root cause), lowest hash first.

Root causes and outcomes that do not fit are reported (``unrepresented``), never dropped silently.
The sheet shows masked keys (the UI's key view shows both sides); a sheet with reviewer input is
never overwritten (``force`` archives it) and a new sheet deletes the old score. Reviewers fill the
sheet in place; ``score`` accepts only the selected keys, once each, with the run's engine columns,
and records the sheet's SHA-256 so a later edit makes the score stale.

In the store, ``store_draw`` draws an ended cycle's sheet with a server salt and
``store_score`` scores its upload once, keeping the sheet as drawn with the reviewer's verdicts and the
score, without notes; both are refused while the cycle has a requested or approved sign-off, which
attests the newest adjudication a person scored.

A draw (``draw``) reads the cycle's keys by outcome, one row per root cause and only the keys each choice
can reach (``Pool``), then the chosen keys' results and findings; a score reads the same counts and root
causes and the selected keys' rows. Neither holds more than the limit and the root causes allow.
"""

from __future__ import annotations

import hashlib
import math
import secrets
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from deltasleuth.auth.principal import Principal
from deltasleuth.context import Context
from deltasleuth.core import jsonio
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.types import Verdict
from deltasleuth.errors import NotFound
from deltasleuth.reports.csv import parse_csv, render_csv
from deltasleuth.reports.data import rank, read_json, write_json
from deltasleuth.store import repo
from deltasleuth.store.repo import Row

EXIT_MINIMUM = DEFAULT_LIMIT = 50
DEFAULT_SALT = "p0-adjudication"
SHEET_NAME, SELECTION_NAME, SCORE_NAME = "adjudication.csv", "adjudication.json", "adjudication_score.json"
MATCH_SHARE = Fraction(1, 5)  # enough MATCH keys to test "equal" while most slots go to root causes
VERDICTS = tuple(v.value for v in Verdict)
TOP_FINDINGS = 3
SHEET_COLUMNS = tuple(
    "key engine_outcome engine_verdict confirmation reason top_findings root_cause_id selected_for "
    "obs_a_id obs_b_id reviewer_verdict reviewer_notes".split()
)
_REQUIRED = ("key", "engine_outcome", "engine_verdict", "reviewer_verdict")  # a reviewed sheet's columns
_ROOT_CAUSE = "root cause "
MAX_LIMIT = 500  # the largest stored draw (ds.adjudications.sample_limit)
MAX_UPLOAD = 1 << 20  # a reviewed sheet's upload, in bytes
_MAX_SHEET_COLUMNS = 256  # room for spreadsheet annotations, without quadratic short-row expansion
_MAX_SHEET_CELLS = 1_000_000  # includes blank cells a short row is padded with; also bounds local sheets
_PREFETCH = 2  # keys read ahead of a root cause that is no key's primary one; more once they are chosen


class AdjudicationError(ValueError):
    """The selection, the sheet or its reviewer input cannot be used as they are."""


class AdjudicationConflict(AdjudicationError):
    """The cycle or the adjudication refuses a draw or a score now (a sign-off, a score already): the API
    answers 409."""


class UploadTooLarge(AdjudicationError):
    """A reviewed sheet over ``MAX_UPLOAD``: the API answers 413."""


@dataclass(frozen=True)
class Cause:
    """A root cause of a cycle: the highest rank of its findings' severities and how many keys hold it."""

    root_cause_id: str
    severity: int
    spread: int


@dataclass(frozen=True)
class Tallies:
    """What a selection and a score count of a whole cycle: its keys by outcome and its root causes."""

    counts: Mapping[str, int]
    causes: Sequence[Cause]


@dataclass
class Pool:
    """What ``select`` chooses among, each key a row of ``key_masked``, ``outcome``, ``primary`` (its primary
    root cause) and ``digest`` (``_hash``), every list in digest order: the cycle's ``tallies``; of each root
    cause, the ``first`` key whose primary root cause it is; of a root cause that is no key's primary one,
    the first keys ``holding`` it (all of them, or as many as were read); of each outcome, its first keys
    (``by_outcome``, at least its quota); and the keys in the order the strata give them in turn
    (``strata``, at least the limit's, each with its stratum's name)."""

    tallies: Tallies
    first: dict[str, Row]
    holding: dict[str, list[Row]]
    by_outcome: dict[str, list[Row]]
    strata: list[tuple[str, Row]]


class _Unread(Exception):
    """Every key ``Pool.holding`` holds of the root cause is chosen, and more keys hold it."""

    def __init__(self, root_cause_id: str) -> None:
        super().__init__(root_cause_id)
        self.root_cause_id = root_cause_id


def _hash(salt: str, r: Row) -> str:
    return hashlib.sha256(f"{salt}\x1f{r['key_masked']}".encode()).hexdigest()


def _ordered(r: Row) -> list[Row]:
    def order(f: Row) -> tuple[int, int, str]:
        kind = 1 if f["suppressed"] else 0 if rank(f) > 0 else 2
        return kind, -rank(f), f["json_pointer"]

    return sorted(r["findings"], key=order)


def root_causes_of(r: Row) -> list[str]:
    return sorted({f["root_cause_id"] for f in r["findings"] if f["root_cause_id"]})


def primary_root_cause(r: Row) -> str | None:
    return next((f["root_cause_id"] for f in _ordered(r) if f["root_cause_id"]), None)


def _causes(records: Iterable[Row]) -> list[Cause]:
    severity: dict[str, int] = defaultdict(int)
    spread: Counter[str] = Counter()
    for r in records:
        for f in r["findings"]:
            if rc := f["root_cause_id"]:
                severity[rc] = max(severity[rc], rank(f))
        spread.update(root_causes_of(r))
    return [Cause(rc, severity[rc], n) for rc, n in spread.items()]


def _ranked(causes: Iterable[Cause]) -> list[Cause]:
    """Most severe, then most widespread, then by id."""
    return sorted(causes, key=lambda c: (-c.severity, -c.spread, c.root_cause_id))


def root_cause_order(records: Iterable[Row]) -> list[str]:
    """Every root cause of the records: most severe, then most widespread, then by id."""
    return [c.root_cause_id for c in _ranked(_causes(records))]


def _tallied(records: Sequence[Row]) -> Tallies:
    return Tallies(Counter(r["outcome"] for r in records), _causes(records))


def outcome_quota(counts: Mapping[str, int], limit: int) -> dict[str, int]:
    """The keys a draw of ``limit`` owes each outcome of ``counts`` (keys by outcome): one, and MATCH its
    reserved share."""
    quota = {outcome: 1 for outcome, n in counts.items() if n}
    if matches := counts.get("MATCH", 0):
        share = min(MATCH_SHARE, Fraction(matches, sum(counts.values())))
        quota["MATCH"] = min(matches, max(1, math.ceil(limit * share)))
    return quota


def _stratum(key: Row) -> str:
    return f"{key['outcome']}/{key['primary'] or '-'}"


def _in_turn(strata: Iterable[tuple[int, str, Row]]) -> list[tuple[str, Row]]:
    """(place in its stratum, stratum, key) as the strata give them in turn: the first key of each stratum
    by name, then the second, and so on."""
    return [(name, key) for _, name, key in sorted(strata, key=lambda turn: turn[:2])]


def _pool(records: Sequence[Row], salt: str) -> Pool:
    """The pool of every one of ``records``."""
    keys = sorted(
        ({"key_masked": r["key_masked"], "outcome": r["outcome"], "primary": primary_root_cause(r),
          "digest": _hash(salt, r)} for r in records),
        key=lambda key: key["digest"],
    )  # fmt: skip
    causes = {r["key_masked"]: root_causes_of(r) for r in records}
    pool = Pool(_tallied(records), {}, defaultdict(list), defaultdict(list), [])
    strata: dict[str, list[Row]] = defaultdict(list)
    for key in keys:
        if key["primary"]:
            pool.first.setdefault(key["primary"], key)
        for rc in causes[key["key_masked"]]:
            pool.holding[rc].append(key)
        pool.by_outcome[key["outcome"]].append(key)
        strata[_stratum(key)].append(key)
    pool.strata = _in_turn((n, name, k) for name, members in strata.items() for n, k in enumerate(members))
    return pool


def _select(pool: Pool, limit: int) -> list[tuple[Row, str]]:
    """``select``'s choice from ``pool``: (key, why selected) in digest order. ``_Unread`` when it needs
    more keys of a root cause than ``pool.holding`` holds."""
    quota = outcome_quota(pool.tallies.counts, limit)
    chosen: dict[str, tuple[Row, str]] = {}
    taken: Counter[str] = Counter()

    def take(key: Row, why: str) -> None:
        chosen[key["key_masked"]] = key, why
        taken[key["outcome"]] += 1

    def owed_after(outcome: str) -> int:
        return sum(max(0, q - taken[o] - (o == outcome)) for o, q in quota.items())

    def candidate(cause: Cause) -> Row | None:
        """The key to choose for an unrepresented root cause: the first whose primary root cause it is (a
        key chosen for a root cause represents its primary one, so this one is not chosen), else the first
        holding it not chosen; None when every key holding it is chosen."""
        if (key := pool.first.get(cause.root_cause_id)) is not None:
            return key
        held = pool.holding.get(cause.root_cause_id, [])
        key = next((k for k in held if k["key_masked"] not in chosen), None)
        if key is None and cause.spread > len(held):
            raise _Unread(cause.root_cause_id)
        return key

    represented: set[str | None] = set()
    for cause in _ranked(pool.tallies.causes):
        rc = cause.root_cause_id
        if rc in represented or (key := candidate(cause)) is None:
            continue
        if len(chosen) + 1 + owed_after(key["outcome"]) <= limit:
            take(key, _ROOT_CAUSE + rc)
            represented |= {rc, key["primary"]}
    for outcome in sorted(quota, key=lambda o: (o != "MATCH", o)):
        for key in pool.by_outcome.get(outcome, []):
            if taken[outcome] >= quota[outcome] or len(chosen) >= limit:
                break
            if key["key_masked"] not in chosen:
                take(key, f"outcome {outcome}")
    for name, key in pool.strata:
        if len(chosen) >= limit:
            break
        if key["key_masked"] not in chosen:
            take(key, f"stratum {name}")
    return sorted(chosen.values(), key=lambda pick: pick[0]["digest"])


def select(
    records: Sequence[Row], *, limit: int = DEFAULT_LIMIT, salt: str = DEFAULT_SALT
) -> list[tuple[Row, str]]:
    """(result row, why selected) for at most ``limit`` rows, in hash order."""
    if limit < 1:
        raise AdjudicationError("the adjudication limit must be at least 1")
    by_key = {r["key_masked"]: r for r in records}
    return [(by_key[key["key_masked"]], why) for key, why in _select(_pool(records, salt), limit)]


def unrepresented(records: Sequence[Row], chosen: Mapping[str, str]) -> dict[str, list[str]]:
    """Root causes and outcomes the chosen keys (masked key -> why) do not give a look of their own.

    A root cause whose every key is chosen is represented: no larger limit could do better.
    """
    picked = [(r, chosen[r["key_masked"]]) for r in records if r["key_masked"] in chosen]
    return _left_out(_tallied(records), picked)


def _left_out(tallies: Tallies, picked: Sequence[tuple[Row, str]]) -> dict[str, list[str]]:
    """``unrepresented`` from the cycle's ``tallies`` and the chosen keys' result rows with why each was
    chosen (``picked``): a root cause more keys hold than chosen ones is held elsewhere."""
    seen: set[str | None] = {
        why.removeprefix(_ROOT_CAUSE) for _, why in picked if why.startswith(_ROOT_CAUSE)
    }
    seen |= {primary_root_cause(r) for r, _ in picked}
    inside = Counter(rc for r, _ in picked for rc in root_causes_of(r))
    return {
        "root_causes": [
            c.root_cause_id for c in _ranked(tallies.causes)
            if c.root_cause_id not in seen and c.spread > inside[c.root_cause_id]
        ],
        "outcomes": sorted({o for o, n in tallies.counts.items() if n} - {r["outcome"] for r, _ in picked}),
    }  # fmt: skip


def findings_summary(r: Row) -> str:
    """The top findings in one cell (patterns are stored masked)."""
    ordered = _ordered(r)
    parts = [
        f"{f['severity'] or '-'} {f['taxonomy_code'] or '-'} {f['change_type']} {f['path_pattern']}"
        + (f" (suppressed by {f['rule_id'] or 'a mask'})" if f["suppressed"] else "")
        for f in ordered[:TOP_FINDINGS]
    ]
    more = [f"+{len(ordered) - TOP_FINDINGS} more"] if len(ordered) > TOP_FINDINGS else []
    return " | ".join(parts + more)


def _key_of(row: Row) -> Row:
    """A pool's key from a row of ``repo.adjudication_draw`` or ``repo.adjudication_holders``."""
    return {"key_masked": row["key_masked"], "outcome": row["outcome"], "primary": row["primary_cause"],
            "digest": bytes(row["digest"]).hex()}  # fmt: skip


def _stored(tallies: Tallies, rows: Iterable[Row]) -> Pool:
    """The pool ``repo.adjudication_draw``'s rows give."""
    pool = Pool(tallies, {}, defaultdict(list), defaultdict(list), [])
    strata = []
    for row in sorted(rows, key=lambda row: bytes(row["digest"])):
        key = _key_of(row)
        match row["kind"]:
            case "first":
                pool.first[row["root_cause_id"]] = key
            case "holding":
                pool.holding[row["root_cause_id"]].append(key)
            case "outcome":
                pool.by_outcome[key["outcome"]].append(key)
            case "stratum":
                strata.append((row["place"], row["stratum"], key))
    pool.strata = _in_turn(strata)
    return pool


async def _tallies(conn: repo.Conn, run_id: str, cycle: int) -> Tallies:
    counts = (await repo.cycle_counts(conn, run_id, cycle))["outcomes"]
    return Tallies(counts, [Cause(**row) for row in await repo.adjudication_causes(conn, run_id, cycle)])


async def _records(conn: repo.Conn, run_id: str, cycle: int, keys: Sequence[str]) -> dict[str, Row]:
    """The cycle's results of ``keys`` by masked key, each with its ``findings`` and derived ``verdict``."""
    from deltasleuth.runner.counters import verdict

    results = await repo.get_results(conn, run_id, cycle, keys)
    findings = await repo.get_findings(conn, run_id, cycle, keys)
    for key, r in results.items():
        r["findings"] = findings.get(key, [])
        r["verdict"] = verdict(r, r["findings"]).value
    return results


async def draw(ctx: Context, run_id: str, cycle: int, limit: int, salt: str) -> tuple[dict[str, Any], str]:
    """``select``'s choice of at most ``limit`` of the cycle's per-key results under ``salt`` as a
    selection (``{candidates, keys, unrepresented}``), and the blank reviewer sheet (CSV text). It reads
    the cycle's tallies and pool (more keys of a root cause when the choice needs them), then the chosen
    keys' results and findings."""
    if limit < 1:
        raise AdjudicationError("the adjudication limit must be at least 1")
    async with ctx.pool.connection() as conn:
        tallies = await _tallies(conn, run_id, cycle)
    quota = outcome_quota(tallies.counts, limit).get("MATCH", 0)
    async with ctx.pool.connection() as conn:  # its own transaction: the draw's planner setting ends with it
        rows = await repo.adjudication_draw(
            conn, run_id, cycle, salt=salt, limit=limit, match_quota=quota, prefetch=_PREFETCH
        )
    pool = _stored(tallies, rows)
    async with ctx.pool.connection() as conn:
        while True:
            try:
                chosen = _select(pool, limit)
                break
            except _Unread as unread:  # fewer keys are chosen than limit + 1, so one of those read is not
                more = await repo.adjudication_holders(
                    conn, run_id, cycle, unread.root_cause_id, salt=salt, limit=limit + 1
                )
                pool.holding[unread.root_cause_id] = [_key_of(row) for row in more]
        records = await _records(conn, run_id, cycle, [key["key_masked"] for key, _ in chosen])
    picked = [(records[key["key_masked"]], why) for key, why in chosen]
    keys = {r["key_masked"]: why for r, why in picked}
    sheet = render_csv(SHEET_COLUMNS, [_sheet_row(r, why) for r, why in picked])
    candidates = sum(tallies.counts.values())
    return {"candidates": candidates, "keys": keys, "unrepresented": _left_out(tallies, picked)}, sheet


def _sheet_row(r: Row, why: str) -> dict[str, Any]:
    values = (r["key_masked"], r["outcome"], r["verdict"], r["confirmation"],
              r["inconclusive_reason"] or r["drift_reason"], findings_summary(r), primary_root_cause(r), why,
              r["obs_a_id"], r["obs_b_id"])  # fmt: skip
    return dict(zip(SHEET_COLUMNS, values, strict=False))  # the reviewer columns stay blank


def has_reviewer_input(sheet: Path) -> bool:
    """Any reviewer verdict or note; a sheet that cannot be read counts as reviewed."""
    try:
        rows = _read_sheet(sheet.read_bytes(), sheet.name, required=())
    except ValueError:
        return True
    return any(
        row.get("reviewer_verdict", "").strip() or row.get("reviewer_notes", "").strip() for row in rows
    )


def _archive(sheet: Path) -> Path:
    n = 1
    while (target := sheet.with_name(f"{sheet.stem}.{n}{sheet.suffix}")).exists():
        n += 1
    return sheet.rename(target)


async def _cycle_of(ctx: Context, run_id: str, cycle: int | None) -> int:
    """The cycle a local sheet is drawn from or scored against, the run's current one by default;
    LookupError if the run is unknown, ``runner.purge.CyclePurged`` once the cycle's purge has started."""
    from deltasleuth.runner.purge import refuse_purged

    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        if run is None:
            raise NotFound(f"run {run_id} not found")
        cycle = cycle or run["cycle"]
        await refuse_purged(conn, run_id, cycle)
    return cycle


async def adjudicate(
    ctx: Context,
    run_id: str,
    out_dir: Path,
    *,
    cycle: int | None = None,
    limit: int = DEFAULT_LIMIT,
    salt: str = DEFAULT_SALT,
    force: bool = False,
) -> dict[str, Any]:
    """Write the reviewer sheet and its selection into ``out_dir``; return the selection."""
    cycle = await _cycle_of(ctx, run_id, cycle)
    sheet = out_dir / SHEET_NAME
    reviewed = sheet.exists() and has_reviewer_input(sheet)
    if reviewed and not force:
        raise AdjudicationError(
            f"{sheet} already holds reviewer verdicts or notes; score it, or force a new sheet "
            "(the reviewed one is archived)"
        )
    drawn, text = await draw(ctx, run_id, cycle, limit, salt)
    out_dir.mkdir(parents=True, exist_ok=True)
    archived = _archive(sheet) if reviewed else None
    sheet.write_text(text, newline="")
    (out_dir / SCORE_NAME).unlink(missing_ok=True)  # it scored a sheet that is no longer the run's
    selection = {
        "run_id": run_id, "cycle": cycle, "salt": salt, "limit": limit, **drawn,
        "archived_sheet": archived.name if archived else None,
    }  # fmt: skip
    write_json(out_dir / SELECTION_NAME, selection)
    return selection


def _lines(numbers: Sequence[int]) -> str:
    return ", ".join(map(str, numbers[:20])) + (" ..." if len(numbers) > 20 else "")


def _read_sheet(
    data: bytes,
    name: str,
    *,
    required: Sequence[str] = _REQUIRED,
    again: str = "score it again",
    max_rows: int | None = None,
) -> list[dict[str, str]]:
    """The rows of a reviewer sheet's bytes (``name`` names it in errors), which holds the ``required``
    columns and names no column twice (a row would keep the last one's cell, not the one reviewed);
    ``again`` says what follows saving it as UTF-8."""
    def check_header(header: list[str]) -> None:
        counts = Counter(header)
        if twice := [c for c, n in counts.items() if c and n > 1]:  # unnamed cells: unread
            raise AdjudicationError(f"{name} names column(s) {', '.join(twice)} more than once")
        if missing := [c for c in required if c not in counts]:
            raise AdjudicationError(f"{name} lacks column(s) {', '.join(missing)}")

    try:
        _, rows = parse_csv(
            data, name, max_columns=_MAX_SHEET_COLUMNS, max_rows=max_rows,
            max_cells=_MAX_SHEET_CELLS, check_header=check_header,
        )
    except UnicodeDecodeError:  # never guessed: a mis-decoded sheet could garble the notes
        raise AdjudicationError(
            f"{name} is not UTF-8 text (a spreadsheet's plain 'CSV' export writes Windows-1252): "
            f"save it as 'CSV UTF-8' and {again}"
        ) from None
    except AdjudicationError:
        raise
    except ValueError as exc:
        raise AdjudicationError(f"{name} cannot be read as a CSV sheet: {exc}") from None
    return rows


def _check_rows(
    rows: Sequence[Mapping[str, str]], selected: Mapping[str, str], engine: Mapping[str, Any]
) -> None:
    """Every row is a selected key, appears once and carries the run's engine columns."""
    lines: dict[str, list[int]] = defaultdict(list)
    for line_no, row in enumerate(rows, start=2):
        lines[row["key"]].append(line_no)
    if repeated := sorted(n for ns in lines.values() if len(ns) > 1 for n in ns):
        raise AdjudicationError(f"the sheet repeats key(s): each may appear once (lines {_lines(repeated)})")
    if foreign := sorted(n for key, ns in lines.items() if key not in selected for n in ns):
        raise AdjudicationError(f"row(s) for keys the run did not select (lines {_lines(foreign)})")
    edited = [
        n
        for n, row in enumerate(rows, start=2)
        if engine.get(row["key"]) != (row["engine_outcome"], row["engine_verdict"])
    ]
    if edited:
        raise AdjudicationError(f"engine columns differ from the run's results (lines {_lines(edited)})")


def sheet_sha256(sheet: Path) -> str:
    return "sha256:" + hashlib.sha256(sheet.read_bytes()).hexdigest()


def _tally(rows: Sequence[Mapping[str, str]]) -> tuple[dict, dict, list[dict[str, str]], set[str]]:
    """(confusion, per outcome, disagreements, adjudicated keys) of the rows with a verdict."""
    confusion: defaultdict[str, Counter[str]] = defaultdict(Counter)
    per_outcome: defaultdict[str, Counter[str]] = defaultdict(Counter)
    disagreements, invalid, adjudicated = [], [], set()
    for line_no, row in enumerate(rows, start=2):
        verdict = row["reviewer_verdict"].strip().upper()
        if verdict and verdict not in VERDICTS:
            invalid.append(line_no)
        elif verdict:
            adjudicated.add(row["key"])
            confusion[row["engine_verdict"]][verdict] += 1
            agreed = verdict == row["engine_verdict"]
            per_outcome[row["engine_outcome"]].update({"adjudicated": 1, "agreed": int(agreed)})
            if not agreed:
                said = {
                    name: row[name] for name in ("key", "engine_outcome", "engine_verdict", "reviewer_notes")
                }
                disagreements.append(said | {"reviewer_verdict": verdict})
    if invalid:
        raise AdjudicationError(
            f"reviewer_verdict must be one of {', '.join(VERDICTS)} (lines {_lines(invalid)})"
        )
    sorted_confusion = {k: dict(sorted(v.items())) for k, v in sorted(confusion.items())}
    return sorted_confusion, {k: dict(v) for k, v in sorted(per_outcome.items())}, disagreements, adjudicated


async def score(ctx: Context, out_dir: Path) -> dict[str, Any]:
    """Score the reviewer sheet in ``out_dir`` against the run it was drawn from; write the score."""
    if not (out_dir / SELECTION_NAME).exists():
        raise AdjudicationError(f"{out_dir} holds no adjudication selection: adjudicate the run first")
    selection = read_json(out_dir / SELECTION_NAME)
    sheet = out_dir / SHEET_NAME
    rows = _read_sheet(sheet.read_bytes(), sheet.name)
    cycle = await _cycle_of(ctx, selection["run_id"], selection["cycle"])
    report = await _score(ctx, cycle, rows, selection) | {"sheet_sha256": sheet_sha256(sheet)}
    write_json(out_dir / SCORE_NAME, report)
    return report


async def _score(
    ctx: Context, cycle: int, rows: Sequence[Mapping[str, str]], selection: Mapping[str, Any]
) -> dict[str, Any]:
    """``score_rows``'s score from the cycle's tallies and the selected keys' rows alone."""
    async with ctx.pool.connection() as conn:
        tallies = await _tallies(conn, selection["run_id"], cycle)
        records = await _records(conn, selection["run_id"], cycle, list(selection["keys"]))
    return score_rows(rows, selection, list(records.values()), tallies)


def score_rows(
    rows: Sequence[Mapping[str, str]],
    selection: Mapping[str, Any],
    records: Sequence[Row],
    tallies: Tallies | None = None,
) -> dict[str, Any]:
    """The score of a reviewer sheet's ``rows`` against the per-key results it was drawn from
    (``selection``: its ``run_id`` and selected ``keys``), without the sheet's own hash. ``records`` hold
    at least the selected keys' results, and every one of the cycle's without its ``tallies``."""
    tallies = tallies or _tallied(records)
    _check_rows(rows, selection["keys"], {r["key_masked"]: (r["outcome"], r["verdict"]) for r in records})
    confusion, per_outcome, disagreements, adjudicated = _tally(rows)
    reasons = []
    if len(adjudicated) < EXIT_MINIMUM:
        reasons.append(f"{len(adjudicated)} key(s) adjudicated; the exit needs at least {EXIT_MINIMUM}")
    if disagreements:
        reasons.append(f"{len(disagreements)} disagreement(s) between reviewers and the engine")
    reasons += [f"no adjudicated key has engine outcome {o} ({n} in the run)"
                for o, n in sorted(tallies.counts.items()) if n and o not in per_outcome]  # fmt: skip
    agreed = len(adjudicated) - len(disagreements)
    by_key = {r["key_masked"]: r for r in records}
    left_out = _left_out(tallies, [(by_key[k], selection["keys"][k]) for k in adjudicated])["root_causes"]
    return {
        "run_id": selection["run_id"], "rows": len(rows), "adjudicated": len(adjudicated), "agreed": agreed,
        "agreement_rate": f"{agreed / len(adjudicated):.4f}" if adjudicated else "unavailable",
        "confusion": confusion, "per_outcome": per_outcome, "disagreements": disagreements,
        "exit": {"passed": not reasons, "minimum": EXIT_MINIMUM, "reasons": reasons},
        "root_causes_not_adjudicated": left_out,
    }  # fmt: skip


# -- in the store ----------------------------------------------------------------


async def _unfrozen(conn: repo.Conn, run_id: str, cycle: int, what: str = "adjudication") -> None:
    """Refuse a draw or score of a cycle that has not ended, was purged (CyclePurged) or has a requested or
    approved sign-off, judged under the cycle's row lock (``repo.open_signoff``); ``what`` a new one would
    be (a labelling)."""
    from deltasleuth.runner.purge import refuse_purged

    signoff = await repo.open_signoff(conn, run_id, cycle)
    await refuse_purged(conn, run_id, cycle)
    if await repo.get_cycle(conn, run_id, cycle) is None:
        raise AdjudicationConflict(f"cycle {cycle} of run {run_id} has not ended")
    if signoff is not None:
        raise AdjudicationConflict(
            f"cycle {cycle} of run {run_id} has a sign-off ({signoff}): revoke it before a new {what}"
        )


async def store_draw(
    ctx: Context, run_id: str, cycle: int, *, who: Principal, limit: int = DEFAULT_LIMIT
) -> Row:
    """Draw a reviewer sheet of at most ``limit`` (1 to ``MAX_LIMIT``) of the ended cycle's per-key results
    with a server salt, stored and audited ``adjudication.draw``: the stored row."""
    if not 1 <= limit <= MAX_LIMIT:
        raise AdjudicationError(f"the adjudication limit must be 1 to {MAX_LIMIT}")
    async with ctx.pool.connection() as conn:
        if await repo.get_run(conn, run_id) is None:
            raise NotFound(f"run {run_id} not found")
        await _unfrozen(conn, run_id, cycle)  # before the cycle is read
    salt = secrets.token_hex(16)
    selection, sheet = await draw(ctx, run_id, cycle, limit, salt)
    async with repo.audited(ctx.pool) as (conn, audit):
        await _unfrozen(conn, run_id, cycle)
        row = await repo.insert_adjudication(
            conn, run_id, cycle, salt=salt, limit=limit, selection=selection, sheet=sheet, drawn_by=who.actor
        )
        detail = {"run_id": run_id, "cycle": cycle, "salt": salt}
        await audit(who, "adjudication.draw", row["adjudication_id"], detail)
    return row


def _already(adjudication_id: str) -> AdjudicationConflict:
    return AdjudicationConflict(f"adjudication {adjudication_id} is already scored: draw a new sheet")


async def store_score(ctx: Context, adjudication_id: str, data: bytes, *, who: Principal) -> Row:
    """Score an uploaded reviewer sheet (``text/csv``, at most ``MAX_UPLOAD`` bytes) against the cycle it was
    drawn from, once, audited ``adjudication.score``: the stored row. The sheet kept is the drawn one with
    the reviewer's verdicts, and the score keeps no note (nothing else a reviewer typed is stored); the
    score names its upload (``upload_sha256``), cycle, adjudication and salt."""
    if len(data) > MAX_UPLOAD:
        raise UploadTooLarge(f"the reviewed sheet is over {MAX_UPLOAD // (1 << 20)} MiB")
    async with ctx.pool.connection() as conn:
        found = await repo.get_adjudication(conn, adjudication_id)
        if found is None:
            raise NotFound(f"adjudication {adjudication_id} not found")
        run_id, cycle = found["run_id"], found["cycle"]
        await _unfrozen(conn, run_id, cycle)
        if found["scored_at"] is not None:
            raise _already(adjudication_id)
    rows = _read_sheet(data, "the reviewed sheet", max_rows=MAX_LIMIT)
    report = await _score(ctx, cycle, rows, {"run_id": run_id, "keys": found["selection"]["keys"]})
    unnoted = [{k: v for k, v in d.items() if k != "reviewer_notes"} for d in report["disagreements"]]
    upload = f"sha256:{hashlib.sha256(data).hexdigest()}"
    stored = report | {"disagreements": unnoted, "upload_sha256": upload, "cycle": cycle}
    stored |= {"adjudication_id": adjudication_id, "salt": found["salt"]}
    sheet = _reviewed(found["sheet"], rows)
    hashes = {
        "upload_sha256": stored["upload_sha256"],
        "sheet_sha256": hashlib.sha256(sheet.encode()).hexdigest(),
        "score_sha256": sha256_hex(jsonio.dumps(stored, sort_keys=True)),
    }
    passed = stored["exit"]["passed"]
    async with repo.audited(ctx.pool) as (conn, audit):
        await _unfrozen(conn, run_id, cycle)
        row = await repo.score_adjudication(
            conn, adjudication_id, sheet=sheet, score=stored, passed=passed, scored_by=who.actor, **hashes
        )
        if row is None:  # scored meanwhile
            raise _already(adjudication_id)
        await audit(who, "adjudication.score", adjudication_id, hashes | {"passed": passed})
    return row


def _reviewed(sheet: str, rows: Sequence[Mapping[str, str]]) -> str:
    """The drawn sheet with each reviewer verdict of ``rows`` (upper-cased) and no notes."""
    verdicts = {row["key"]: row["reviewer_verdict"].strip().upper() for row in rows}
    _, drawn = parse_csv(sheet.encode())
    reviewed = [r | {"reviewer_verdict": verdicts.get(r["key"], ""), "reviewer_notes": ""} for r in drawn]
    return render_csv(SHEET_COLUMNS, reviewed)
