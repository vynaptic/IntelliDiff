"""Labelled accuracy, "which side is right" (docs/mvp/ACCURACY.md): a uniform salted sample of an ended
cycle's items, labelled once offline against a truth source named at the draw, and its counts.

``store_draw`` draws in the store (``repo.labelling_draw``): a server salt orders every item (a discrepant
key in one root cause; an accepted or agreed key) by its SHA-256, the ``size`` smallest discrepant items
are the uniform sample, and each root cause is topped up, in rank order, to its ``per_cause`` smallest. A
named person fills the sheet, which shows masked keys; ``store_labels`` checks the upload, keeps the drawn
sheet with its labels and no note, and stores the score once. ``tally`` counts the labels: overall shares
from the uniform items only, a cause's or a rule's from its own, each with its exact two-sided 95%
Clopper-Pearson interval (``sampling.interval``) and, where items are undecidable, the range counting them
either way. Both are refused while the cycle has a requested or approved sign-off, which attests the
newest labelling a person labelled. ``lines``, ``summary``, ``gate`` (cut-over readiness's accuracy gate,
docs/mvp/READINESS.md §9) and ``LIMITS`` are the only texts built from a score: DeltaSleuth counts a
person's labels and never decides which side is right.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from decimal import Context as Rounding
from typing import Any

from psycopg import errors

from deltasleuth.auth.principal import Principal
from deltasleuth.connectors.base import utc_iso
from deltasleuth.context import Context
from deltasleuth.core import jsonio, sampling
from deltasleuth.core.canonical.encoding import sha256_hex
from deltasleuth.core.classify.patterns import segments
from deltasleuth.core.sampling import percent
from deltasleuth.core.types import CompiledSpec
from deltasleuth.reports.adjudicate import (
    MAX_UPLOAD,
    AdjudicationConflict,
    AdjudicationError,
    UploadTooLarge,
    _lines,
    _read_sheet,
    _unfrozen,
)
from deltasleuth.reports.csv import parse_csv, render_csv
from deltasleuth.security import rawvalues
from deltasleuth.security.policy import BUSINESS_CODE, canonical_policy
from deltasleuth.store import repo
from deltasleuth.store.repo import Row

METHOD = "deltasleuth-labelled/1"
MAX_ITEMS = 1000  # items a labelling holds at most
REPEAT_S = 600  # a draw repeated within it returns the first labelling
DRAW_TIMEOUT_S = 300
TOP_CAUSES = TOP_RULES = 20  # cause and rule lines printed; the stored score holds them all
POPULATIONS = ("discrepant", "accepted", "agreed")
LABELS = ("A_RIGHT", "B_RIGHT", "BOTH_WRONG", "BOTH_RIGHT", "UNDECIDABLE")
BASES = ("SYSTEM_OF_RECORD", "SOURCE_DOCUMENT", "BUSINESS_RULE", "OWNER_CONFIRMED")
REASONS = ("NOT_LOCATABLE", "TOKENISED", "NO_TRUTH", "TRUTH_CONFLICTS", "CHANGED_SINCE", "NOT_REVIEWED")
UNCAUSED, MASK = "*", "mask"  # an item without a root cause; an accepted key no rule suppressed
SHEET_COLUMNS = tuple(
    "item key population root_cause_id path_pattern change_type rules engine_outcome selected_for "
    "observed_at obs_a_id obs_b_id label basis reason notes".split()
)
KEYLESS_COLUMNS = tuple(
    "population root_cause_id path_pattern change_type rules engine_outcome selected_for label basis "
    "reason".split()
)
# What lists show of a labelling (``listed``): ids, actors, dates, sizes, truth and hashes.
LISTED = tuple(
    "labelling_id run_id cycle drawn_by drawn_by_name drawn_at labelled_by labelled_by_name labelled_at size "
    "per_cause accepted agreed item_count truth_source truth_as_of salt selection_sha256 sheet_sha256 "
    "labels_sha256 score_sha256 purged_at".split()
)
_ENGINE = ("key", "population", "root_cause_id", "engine_outcome")  # what an upload may not change
_REQUIRED = ("item", *_ENGINE, "label", "basis", "reason")
_SOURCE = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{0,63}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_EVEN = Rounding(prec=4, rounding=ROUND_HALF_EVEN)
_DOWN, _UP = Rounding(prec=4, rounding=ROUND_FLOOR), Rounding(prec=4, rounding=ROUND_CEILING)
LIMITS = (
    "Observed. Labels record what the named labeller found in the truth source named when the sample"
    " was drawn and in the sources their basis codes name, as the truth stood when each item was"
    " observed; DeltaSleuth cannot see or check them, and a label never changes an outcome, a root"
    " cause or a rule. The uploader answers for labels made offline by people DeltaSleuth does not"
    " record; a sign-off attests which labelling was used and its counts, never that any single label"
    " is right. Labels resting on a compared side are circular and measure nothing. Where the key is"
    " tokenised and no path is shown in clear, labellers cannot find records in the truth source until"
    " a per-key reveal exists. The installation drew the sample after the cycle ended, with a salt it"
    " chose and committed with the sample to the audit trail. A cycle may be drawn and labelled more"
    " than once, and an interval holds only for a sheet that was not chosen after its labels were"
    " seen. The discrepancy shares describe the discrepancies this cycle observed (one key's"
    " difference in one root cause): which side the truth favoured on those, not whether either system"
    " is correct overall. Each interval is an exact two-sided 95% Clopper-Pearson interval computed as"
    " for a sample drawn with replacement; for this sample, drawn without replacement, it covered the"
    " true share 95% of the time or more in every population checked, which is not a proof for every"
    " population. Each is a separate statement, not all guaranteed at once, and covers sampling error"
    " only, not labelling error. A label's share is of every sampled item and counts the undecidable"
    " ones, including those nobody reviewed, as not that label's; shares among decided items assume"
    " they are like the rest; the range beside each counts them either way and, for a label, gives its"
    " item counts. A root cause's or a rule's figures come from its own items and describe it only."
    " Agreed and accepted samples bound how often an agreement, a rule or a mask hid an error among"
    " the keys read per key. Nothing here covers keys the cycle did not compare, left unresolved or"
    " matched by row hash; keys outside the compared population, filters, columns and time window; a"
    " sampled cycle's frame beyond its sampled keys; other cycles; or the correctness of the truth"
    " sources."
)
NOT_MEASURED = "Not measured: no labelled sample of this cycle."


def not_measured(drawn: int) -> str:
    """§11's report text 4, of a cycle none of whose ``drawn`` labellings is labelled."""
    if not drawn:
        return NOT_MEASURED
    return f"{NOT_MEASURED} {_n(drawn, 'labelling was', 'labellings were')} drawn, none labelled."


class DrawTimeout(Exception):
    """The draw's statement was stopped after ``DRAW_TIMEOUT_S``: nothing is stored; the API answers 504."""


# -- the draw (§3) ----------------------------------------------------------------------------------------


def _parameters(sizes: Mapping[str, int], truth: Mapping[str, Any]) -> tuple[str, date]:
    """The truth's source and as-of date, once the draw's sizes and truth pass (§11's 422s, in order)."""
    size, per_cause, accepted, agreed = (sizes[name] for name in ("size", "per_cause", "accepted", "agreed"))
    if not (0 <= size <= 1000 and 0 <= per_cause <= 20 and 0 <= accepted <= 500 and 0 <= agreed <= 500):
        raise AdjudicationError("size must be 0 to 1,000, per_cause 0 to 20, accepted and agreed 0 to 500")
    if not any(sizes.values()):
        raise AdjudicationError(
            "a labelling draws at least one item: set size, per_cause, accepted or agreed above 0"
        )
    if (asked := size + accepted + agreed) > MAX_ITEMS:
        raise AdjudicationError(
            f"a labelling holds at most {MAX_ITEMS:,} items: size, accepted and agreed ask for {asked:,}"
        )
    source, as_of = truth.get("source"), truth.get("as_of")
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise AdjudicationError(
            "truth.source must be a system identifier: 1 to 64 letters, digits and _ . : / -, starting with a"
            " letter"
        )
    try:
        day = date.fromisoformat(as_of) if isinstance(as_of, str) and _DATE.fullmatch(as_of) else None
    except ValueError:
        day = None
    if day is None or day > datetime.now(UTC).date():
        raise AdjudicationError("truth.as_of must be a date (YYYY-MM-DD) no later than today")
    return source, day


def names_side(source: str, spec: CompiledSpec, tables: Mapping[str, Any] | None = None) -> str | None:
    """The side a truth ``source`` names, ignoring case (§2.3): its target id, alone or followed by ``.``,
    ``:`` or ``/``, or its table's ``name`` or ``schema.name`` (the schema the run's plan, ``tables``,
    read); None when it names neither side. Labels resting on a compared side are circular."""
    found = source.casefold()
    for side, cfg in spec.sides.items():
        target = cfg.target.casefold()
        names = {target}
        if cfg.table is not None:
            schema = ((tables or {}).get(side.value) or {}).get("schema") or cfg.table.schema
            names |= {cfg.table.name.casefold(), f"{schema or ''}.{cfg.table.name}".casefold()}
        if found in names or found.startswith((f"{target}.", f"{target}:", f"{target}/")):
            return side.value
    return None


def _unlocatable(spec: CompiledSpec) -> bool:
    """Whether labellers cannot find a record in the truth source (§4): the key is tokenised and the spec
    shows no path in clear. Judged by the compiled policy, under which a tokenise rule of a PII class voids
    a clear entry beneath it: none of ``security.clear``'s paths resolves clear, nor the root where
    unlisted paths are clear."""
    policy = canonical_policy(spec.security)
    paths = (*spec.security.clear, *(["$"] if spec.security.unlisted == "clear" else []))
    shown = any(policy.resolve(segments(path)).value_class is None for path in paths)
    return spec.key.value_class != BUSINESS_CODE and not shown


def select(
    drawn: Sequence[Row], per_cause: int
) -> tuple[list[tuple[str, str, str, str]], dict[str, Any], list[dict[str, Any]], int]:
    """What ``repo.labelling_draw``'s rows select: the items as (population, masked key, cause, why) by
    population, then in digest order, each cause topped up in rank order to its ``per_cause`` smallest
    items until the labelling holds ``MAX_ITEMS``; the cycle's counts, each population's with the items
    ``drawn`` of it; the drawn causes in rank order (``{root_cause_id, rank, items}``); and how many causes
    have no item of their own."""

    def order(row: Row) -> tuple[bytes, bytes, bytes]:
        return bytes(row["digest"]), row["key_masked"].encode(), row["cause"].encode()

    chosen = {(r["population"], r["key_masked"], r["cause"]): (order(r), "uniform") for r in drawn
              if r["kind"] == "uniform"}  # fmt: skip
    sizes = {r["cause"]: r for r in drawn if r["kind"] == "cause"}
    ranked = sorted(
        (c for c in sizes if c != UNCAUSED), key=lambda c: (sizes[c]["seq"] is None, sizes[c]["seq"], c)
    )
    wanted: dict[str, list[Row]] = {}
    for row in sorted((r for r in drawn if r["kind"] == "coverage"), key=order):
        wanted.setdefault(row["cause"], []).append(row)
    for cause in ranked if per_cause else ():
        for row in wanted.get(cause, []):
            item = (row["population"], row["key_masked"], row["cause"])
            if item not in chosen and len(chosen) < MAX_ITEMS:
                chosen[item] = (order(row), "coverage")
    items = [
        (*item, why)
        for item, (_, why) in sorted(chosen.items(), key=lambda c: (POPULATIONS.index(c[0][0]), c[1][0]))
    ]
    keys = {r["population"]: r["n"] for r in drawn if r["kind"] == "keys"}
    lone = {r["kind"]: r["n"] or 0 for r in drawn if r["kind"] in ("not_compared", "matched_by_row_hash")}
    rules = {r["cause"]: r["n"] for r in drawn if r["kind"] == "rule"}
    taken = Counter(population for population, _, _, _ in items)
    counts = {
        "discrepant": {
            "items": sum(r["n"] for r in sizes.values()),
            "keys": keys.get("discrepant", 0),
            "causes": len(ranked),
            "drawn": taken["discrepant"],
        },
        "accepted": {
            "keys": keys.get("accepted", 0),
            "by_rule": dict(sorted(rules.items())),
            "drawn": taken["accepted"],
        },
        "agreed": {"keys": keys.get("agreed", 0), "drawn": taken["agreed"]},
        "indeterminate": keys.get("indeterminate", 0),
        "not_compared": lone.get("not_compared", 0),
        "matched_by_row_hash": lone.get("matched_by_row_hash", 0),
    }
    held = {cause for population, _, cause, _ in items if population == "discrepant"}
    causes = [
        {"root_cause_id": c, "rank": None if sizes[c]["seq"] is None else sizes[c]["seq"] + 1,
         "items": sizes[c]["n"]}
        for c in ranked
        if c in held
    ]  # fmt: skip
    return items, counts, causes, len(ranked) - len(causes)


def selection_sha256(items: Sequence[Mapping[str, Any]]) -> str:
    """The SHA-256 of a selection's lines ``item⇥key_masked⇥population⇥root_cause_id⇥selected_for``."""
    lines = (
        f"{i['item']}\t{i['key']}\t{i['population']}\t{i['root_cause_id']}\t{i['selected_for']}\n"
        for i in items
    )
    return sha256_hex("".join(lines))


def _warnings(
    spec: CompiledSpec,
    sizes: Mapping[str, int],
    truth: Mapping[str, Any],
    counts: Mapping[str, Any],
    uncovered: int,
    unlocatable: bool,
    observed: Sequence[datetime],
) -> list[str]:
    found = []
    if (side := truth["names_side"]) is not None:
        role = next(cfg.role for s, cfg in spec.sides.items() if s.value == side)
        found.append(
            f"truth.source {truth['source']} names side {side} ({role}): labels resting on a compared side"
            " are circular and measure nothing; name an authoritative system other than both sides"
            " (docs/mvp/ACCURACY.md §2.3)"
        )
    if unlocatable:
        found.append(
            f"the key is tokenised (class {spec.key.value_class}) and the spec shows no path in clear: expect"
            " UNDECIDABLE (TOKENISED) rows (acknowledged)"
        )
    days = sorted({f"{at.astimezone(UTC):%Y-%m-%d}" for at in observed})
    if days and not days[0] <= truth["as_of"] <= days[-1]:
        found.append(
            f"truth.as_of {truth['as_of']} is outside the drawn items' observations ({days[0]} to"
            f" {days[-1]}): label each item as the truth stood at its observed_at, UNDECIDABLE"
            " (CHANGED_SINCE) where the source cannot show it"
        )
    if sizes["per_cause"] and uncovered:
        causes = counts["discrepant"]["causes"]
        found.append(
            f"per_cause {sizes['per_cause']} covered {causes - uncovered:,} of {causes:,} root causes within"
            f" the {MAX_ITEMS:,} items a labelling holds; {uncovered:,} have no item of their own"
        )
    if sizes["agreed"] and (matched := counts["matched_by_row_hash"]):
        found.append(
            f"{matched:,} rows matched by row hash cannot be sampled: agreed keys are the"
            f" {counts['agreed']['keys']:,} read per key"
        )
    return found


def repeated(row: Mapping[str, Any]) -> str:
    """What a repeated draw is answered with: the first labelling, not drawn again (§11)."""
    return (
        f"labelling {row['labelling_id']} with these parameters was drawn by you at"
        f" {row['drawn_at'].astimezone(UTC):%H:%M:%S} UTC and is returned, not drawn again"
    )


async def store_draw(
    ctx: Context,
    run_id: str,
    cycle: int,
    *,
    who: Principal,
    size: int = 100,
    per_cause: int = 2,
    accepted: int = 0,
    agreed: int = 0,
    truth: Mapping[str, Any],
    acknowledge_unlocatable: bool = False,
) -> tuple[Row, str | None]:
    """Draw a labelling of the ended cycle against ``truth`` (``{source, as_of}``): ``size`` uniform
    discrepant items, each root cause topped up to ``per_cause``, and ``accepted`` and ``agreed`` keys,
    stored with its sheet and audited ``accuracy.draw``. The stored row and None; or, for a draw ``who``
    repeats within ``REPEAT_S``, the first labelling and ``repeated``'s warning, nothing drawn or audited.

    AdjudicationError (422) for sizes, a truth or a tokenised key not acknowledged; LookupError for an
    unknown run; AdjudicationConflict (409) for a cycle that has not ended, has a sign-off or lacks a
    population asked for; CyclePurged (410); DrawTimeout (504)."""
    sizes = {"size": size, "per_cause": per_cause, "accepted": accepted, "agreed": agreed}
    source, as_of = _parameters(sizes, truth)
    at = f"cycle {cycle} of run {run_id}"
    async with ctx.pool.connection() as conn:
        run = await repo.get_run(conn, run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found")
        spec = await ctx.spec(conn, run["spec_id"])
        if rawvalues.key_shaped(spec, source):
            raise AdjudicationError(
                "truth.source holds a key-shaped value: name the system without customer keys"
            )
        await _unfrozen(conn, run_id, cycle, "labelling")
        unlocatable = _unlocatable(spec)
        asked = sizes | {
            "run_id": run_id, "cycle": cycle, "drawn_by": who.actor, "truth_source": source,
            "truth_as_of": as_of, "unlocatable": unlocatable and acknowledge_unlocatable,
        }  # fmt: skip
        if (first := await repo.repeated_labelling(conn, asked, REPEAT_S)) is not None:
            return first, repeated(first)
    if unlocatable and not acknowledge_unlocatable:
        raise AdjudicationError(
            f"the key is tokenised (class {spec.key.value_class}) and the spec shows no path in clear:"
            " labellers cannot find records in the truth source until the per-key reveal (AUTH §11) exists;"
            " set acknowledge_unlocatable to draw for labels a business rule decides without the record"
            " (docs/mvp/ACCURACY.md §4)"
        )
    salt = sampling.nonce()
    try:
        async with ctx.pool.connection() as conn:  # its own transaction: its settings end with it
            drawn = await repo.labelling_draw(
                conn, run_id, cycle, salt=salt, timeout_s=DRAW_TIMEOUT_S, **sizes
            )
    except errors.QueryCanceled:
        raise DrawTimeout(
            f"the draw of {at} was stopped after {DRAW_TIMEOUT_S} s (QueryCanceled): nothing was drawn; draw"
            " again with accepted and agreed at 0, or when the store is less busy"
        ) from None
    items, counts, causes, uncovered = select(drawn, per_cause)
    async with ctx.pool.connection() as conn:
        shown = await repo.labelling_items(conn, run_id, cycle, [(key, cause) for _, key, cause, _ in items])
        await _unfrozen(conn, run_id, cycle, "labelling")  # a purge begun since the draw took its rows: 410
    empty = [
        f"{at} has no {what} to label: set {names} to 0"
        for what, names, wanted, held in (
            ("discrepant item", "size and per_cause", size or per_cause, counts["discrepant"]["items"]),
            ("accepted key", "accepted", accepted, counts["accepted"]["keys"]),
            ("agreed key", "agreed", agreed, counts["agreed"]["keys"]),
        )
        if wanted and not held
    ]
    if not empty and not items:  # per_cause alone, and no discrepant item has a root cause
        empty = [f"{at} has no root cause to cover: set size above 0"]
    if empty:
        raise AdjudicationConflict("; ".join(empty))
    named = {
        "source": source,
        "as_of": f"{as_of}",
        "names_side": names_side(source, spec, run["params"].get("tables")),
    }
    listed = [
        {"item": n, "key": key, "population": population, "root_cause_id": cause, "selected_for": why}
        for n, (population, key, cause, why) in enumerate(items, start=1)
    ]
    sheet = render_csv(SHEET_COLUMNS, [_sheet_row(i, shown[i["key"], i["root_cause_id"]]) for i in listed])
    if len(sheet.encode()) > MAX_UPLOAD // 2:
        raise AdjudicationError(
            f"the sheet of this labelling would be over {MAX_UPLOAD // 2 // 1024} KiB, leaving too little"
            " room for notes: draw fewer items"
        )
    observed = [row["observed_at"] for row in shown.values()]
    selection = {
        "items": listed, "counts": counts, "causes": causes, "uncovered": uncovered,
        "names_side": named["names_side"], "unlocatable": unlocatable,
        "warnings": _warnings(spec, sizes, named, counts, uncovered, unlocatable, observed),
    }  # fmt: skip
    labelling = asked | {"salt": salt, "selection": selection, "sheet": sheet}
    labelling["selection_sha256"] = selection_sha256(listed)
    async with repo.audited(ctx.pool) as (conn, audit):
        await _unfrozen(conn, run_id, cycle, "labelling")
        if (first := await repo.repeated_labelling(conn, asked, REPEAT_S)) is not None:
            return first, repeated(first)  # a racing retry gets the other's labelling
        row = await repo.insert_labelling(conn, labelling)
        detail = sizes | {
            "run_id": run_id, "cycle": cycle, "salt": salt, "truth": named, "unlocatable": unlocatable,
            "item_count": len(listed), "counts": {name: counts[name]["drawn"] for name in POPULATIONS},
            "selection_sha256": row["selection_sha256"],
        }  # fmt: skip
        await audit(who, "accuracy.draw", row["labelling_id"], detail)
    return row, None


def _sheet_row(item: Mapping[str, Any], shown: Mapping[str, Any]) -> dict[str, Any]:
    """A drawn item's row of the blank sheet: the label columns and the notes stay blank."""
    rules = " ".join(shown["rules"]) or MASK if item["population"] == "accepted" else ""
    return dict(item) | {
        "path_pattern": shown["path_pattern"], "change_type": shown["change_type"], "rules": rules,
        "engine_outcome": shown["outcome"], "observed_at": utc_iso(shown["observed_at"]),
        "obs_a_id": shown["obs_a_id"], "obs_b_id": shown["obs_b_id"],
    }  # fmt: skip


# -- upload and scoring (§5, §6.2) ------------------------------------------------------------------------


def _check_rows(rows: Sequence[Mapping[str, str]], drawn: Sequence[Mapping[str, str]]) -> None:
    """§11's checks of an upload, in order: every row is a drawn item, once, with the labelling's engine
    columns; every drawn item is labelled; label, basis and reason are their enumerations' and agree."""
    lines: dict[str, list[int]] = {}
    for line_no, row in enumerate(rows, start=2):
        lines.setdefault(row["item"].strip(), []).append(line_no)
    items = {row["item"]: row for row in drawn}
    if twice := sorted(n for ns in lines.values() if len(ns) > 1 for n in ns):
        raise AdjudicationError(f"the sheet repeats item(s): each may appear once (lines {_lines(twice)})")
    if foreign := sorted(n for item, ns in lines.items() if item not in items for n in ns):
        raise AdjudicationError(f"row(s) for items the labelling did not draw (lines {_lines(foreign)})")
    labelled = {row["item"].strip() for row in rows if row["label"].strip()}
    if blank := [int(item) for item in items if item not in labelled]:
        raise AdjudicationError(
            "every drawn item needs a label: mark UNDECIDABLE with a reason where the truth cannot be"
            f" established, with reason NOT_REVIEWED where nobody reviewed it (items {_lines(blank)})"
        )
    numbered = list(enumerate(rows, start=2))
    if edited := [n for n, row in numbered if any(row[c] != items[row["item"].strip()][c] for c in _ENGINE)]:
        raise AdjudicationError(
            f"engine columns differ from the labelling's items (lines {_lines(edited)}) (import the key"
            " column as text: a spreadsheet drops leading zeros)"
        )
    said = [(n, *(row[c].strip().upper() for c in ("label", "basis", "reason"))) for n, row in numbered]
    if wrong := [n for n, label, _, _ in said if label not in LABELS]:
        raise AdjudicationError(f"label must be one of {', '.join(LABELS)} (lines {_lines(wrong)})")
    undecidable = LABELS[-1]
    if wrong := [n for n, label, basis, _ in said if (basis if label == undecidable else basis not in BASES)]:
        raise AdjudicationError(
            f"basis must be one of {', '.join(BASES)} for a decided label, and empty for {undecidable} (lines"
            f" {_lines(wrong)})"
        )
    if wrong := [
        n for n, label, _, reason in said if (reason not in REASONS if label == undecidable else reason)
    ]:
        raise AdjudicationError(
            f"reason must be one of {', '.join(REASONS)} for {undecidable}, and empty otherwise (lines"
            f" {_lines(wrong)})"
        )


def _labelled(drawn: Sequence[Mapping[str, str]], rows: Sequence[Mapping[str, str]]) -> str:
    """The drawn sheet with each item's label, basis and reason from ``rows`` (upper-cased) and no notes."""
    said = {
        row["item"].strip(): {c: row[c].strip().upper() for c in ("label", "basis", "reason")} for row in rows
    }
    return render_csv(SHEET_COLUMNS, [row | said[row["item"]] | {"notes": ""} for row in drawn])


def keyless(sheet: str) -> str:
    """A labelled sheet without its item numbers, keys, observations and notes, its rows sorted by their own
    cells (``accuracy/labels.csv``): neither a number nor the sheet's order, which follows the keys'
    digests, links a row to a key."""
    rows = parse_csv(sheet.encode())[1]
    return render_csv(KEYLESS_COLUMNS, sorted(rows, key=lambda row: [row[c] for c in KEYLESS_COLUMNS]))


def score_rows(
    rows: Sequence[Mapping[str, str]], sheet: str, selection: Mapping[str, Any], truth: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    """The labelled sheet (the drawn ``sheet`` with the labels of the uploaded ``rows``) and its score
    (``tally``), once ``rows`` pass ``_check_rows``. Only the labels come from the upload."""
    drawn = parse_csv(sheet.encode())[1]
    _check_rows(rows, drawn)
    labelled = _labelled(drawn, rows)
    return labelled, tally(parse_csv(labelled.encode())[1], selection, truth)


def _digits(value: Decimal) -> str:
    """A share as text with its 4 significant digits (0.7 is 0.7000), 0 as ``0``."""
    return format(value.quantize(Decimal(1).scaleb(value.adjusted() - 3)), "f") if value else "0"


def share(x: int, n: int, N: int | None = None, *, census: bool = False) -> dict[str, Any]:
    """``{x, n, share, low, high, items_low, items_high}`` (§6.2): x/n to 4 significant digits, half-even,
    and its interval; ``N``, a label share's population, bounds the items the interval stands for. A
    census (every item labelled) has exact counts and no interval; nothing is shared out of n = 0."""
    low, high = (None, None) if census or not n else sampling.interval(x, n)
    return {
        "x": x,
        "n": n,
        "share": _digits(_EVEN.divide(Decimal(x), Decimal(n))) if n else None,
        "low": low,
        "high": high,
        "items_low": None if N is None or low is None else math.floor(N * Decimal(low)),
        "items_high": None if N is None or high is None else math.ceil(N * Decimal(high)),
    }


def _either_way(x: int, n: int, u: int, census: bool) -> dict[str, str] | None:
    """The range of x among n items counting the u undecidable ones of them either way (as none of the x,
    or all), each end rounded outward: a census's exact shares, else the intervals' outer ends. None
    without an undecidable item."""
    if not u:
        return None
    if census:
        ends = _DOWN.divide(Decimal(x), Decimal(n)), _UP.divide(Decimal(x + u), Decimal(n))
        low, high = (format(end, "f") for end in ends)
    else:
        low, high = sampling.interval(x, n)[0], sampling.interval(x + u, n)[1]
    return {"low": low, "high": high}


def _decided(x: int, d: int, u: int, census: bool) -> dict[str, Any]:
    """``share`` of x among the d decided items, with ``worst_case``: the range counting the u undecidable
    ones either way (None without any)."""
    return share(x, d, census=census) | {"worst_case": _either_way(x, d + u, u, census)}


def _labels(labels: Mapping[str, int], N: int, census: bool) -> dict[str, Any]:
    """Each label's ``share`` of the n items labelled, of a population of ``N``. Where items are
    undecidable, a decided label's share has ``worst_case``, the range counting them either way, and its
    ``items_low`` and ``items_high`` come from that range: an undecidable item may be any label's."""
    n, u = sum(labels.values()), labels["UNDECIDABLE"]
    found = {}
    for label in LABELS:
        x = labels[label]
        worst = None if label == "UNDECIDABLE" else _either_way(x, n, u, census)
        found[label] = share(x, n, N, census=census) | {"worst_case": worst}
        if worst is not None:
            low, high = (x, x + u) if census else (N * Decimal(worst["low"]), N * Decimal(worst["high"]))
            found[label] |= {"items_low": math.floor(low), "items_high": math.ceil(high)}
    return found


def _sides(labels: Mapping[str, int], census: bool) -> dict[str, Any]:
    """Each side's shares among the decided items: right or immaterial (its own _RIGHT and BOTH_RIGHT) and
    wrong (the other's _RIGHT and BOTH_WRONG); BOTH_RIGHT never counts a side wrong."""
    u = labels["UNDECIDABLE"]
    d = sum(labels.values()) - u
    return {
        side: {
            "right_or_immaterial": _decided(labels[own] + labels["BOTH_RIGHT"], d, u, census),
            "wrong": _decided(labels[other] + labels["BOTH_WRONG"], d, u, census),
        }
        for side, own, other in (("a", "A_RIGHT", "B_RIGHT"), ("b", "B_RIGHT", "A_RIGHT"))
    }


def _counted(rows: Sequence[Mapping[str, str]], column: str, names: Sequence[str]) -> dict[str, int]:
    found = Counter(row[column] for row in rows)
    return {name: found[name] for name in names if found[name]}


def _hidden(rows: Sequence[Mapping[str, str]], keys: int) -> dict[str, Any]:
    """An accepted or agreed sample of ``keys`` keys: its label shares and how often an error was hidden
    (A_RIGHT, B_RIGHT or BOTH_WRONG among the decided)."""
    labels = Counter(row["label"] for row in rows)
    n, u = len(rows), labels["UNDECIDABLE"]
    census = n == keys
    return {
        "keys": keys,
        "drawn": n,
        "census": census,
        "labels": _labels(labels, keys, census),
        "hidden_error": _decided(
            labels["A_RIGHT"] + labels["B_RIGHT"] + labels["BOTH_WRONG"], n - u, u, census
        ),
    }


def tally(
    rows: Sequence[Mapping[str, str]], selection: Mapping[str, Any], truth: Mapping[str, Any]
) -> dict[str, Any]:
    """The score (§6.2) of a labelled sheet's ``rows`` (its keyless columns suffice), from its
    ``selection``'s counts, causes and acknowledgement and its ``truth`` (``{source, as_of, names_side}``).
    Pure and without actor or time, so a stored score is re-derived from ``keyless``'s rows."""
    counts = selection["counts"]
    by = {name: [row for row in rows if row["population"] == name] for name in POPULATIONS}
    found: dict[str, Any] = {
        "method": METHOD, "confidence": "0.95", "sided": "two-sided", "truth": dict(truth),
        "unlocatable": selection["unlocatable"],
        **{name: counts[name] for name in ("indeterminate", "not_compared", "matched_by_row_hash")},
        "discrepant": None, "accepted": None, "agreed": None,
    }  # fmt: skip
    if by["discrepant"]:
        found["discrepant"] = _discrepant(by["discrepant"], selection)
    if by["accepted"]:
        hidden = _hidden(by["accepted"], counts["accepted"]["keys"])
        ruled = {
            rule: [r for r in by["accepted"] if rule in r["rules"].split()]
            for rule in sorted(counts["accepted"]["by_rule"])
        }
        found["accepted"] = hidden | {
            "by_rule": [
                {"rule_id": rule, "keys": keys, "labelled": len(ruled[rule]),
                 "labels": _counted(ruled[rule], "label", LABELS),
                 "hidden_error": _hidden(ruled[rule], keys)["hidden_error"]}
                for rule, keys in sorted(counts["accepted"]["by_rule"].items())
            ]
        }  # fmt: skip
    if by["agreed"]:
        found["agreed"] = _hidden(by["agreed"], counts["agreed"]["keys"])
    return found


def _discrepant(rows: Sequence[Mapping[str, str]], selection: Mapping[str, Any]) -> dict[str, Any]:
    """The discrepant items' score: overall shares from the uniform items only, a cause's from all its
    items (§3's validity)."""
    population = selection["counts"]["discrepant"]
    uniform = [row for row in rows if row["selected_for"] == "uniform"]
    labels = Counter(row["label"] for row in uniform)
    n, N, u = len(uniform), population["items"], labels["UNDECIDABLE"]
    census = n == N
    by_cause = []
    for cause in selection["causes"]:
        own = [row for row in rows if row["root_cause_id"] == cause["root_cause_id"]]
        counted = Counter(row["label"] for row in own)
        by_cause.append(
            cause
            | {"labelled": len(own), "census": len(own) == cause["items"],
               "labels": {label: counted[label] for label in LABELS if counted[label]},
               "sides": _sides(counted, len(own) == cause["items"])}
        )  # fmt: skip
    single = labels["A_RIGHT"] + labels["B_RIGHT"]
    return {
        **{name: population[name] for name in ("items", "keys", "causes")},
        "uniform": n,
        "census": census,
        "labels": _labels(labels, N, census),
        "reasons": _counted(uniform, "reason", REASONS),
        "basis_codes": _counted(uniform, "basis", BASES),
        "sides": _sides(labels, census),
        "b_share_of_single_side_right": _decided(labels["B_RIGHT"], single, u, census),
        "by_outcome": {
            outcome: _counted([r for r in uniform if r["engine_outcome"] == outcome], "label", LABELS)
            for outcome in sorted({r["engine_outcome"] for r in uniform})
        },
        "by_cause": by_cause,
        "uncovered": selection["uncovered"],
    }


def _truth(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": row["truth_source"],
        "as_of": f"{row['truth_as_of']}",
        "names_side": row["selection"]["names_side"],
    }


def _already(labelling_id: str) -> AdjudicationConflict:
    return AdjudicationConflict(f"labelling {labelling_id} is already labelled: draw a new sheet")


async def store_labels(ctx: Context, labelling_id: str, data: bytes, *, who: Principal) -> Row:
    """Label a labelling with its filled sheet (``text/csv``, at most ``MAX_UPLOAD`` bytes), once, audited
    ``accuracy.label``: the stored row. The sheet kept is the drawn one with the labels, upper-cased, and
    no notes: nothing else a labeller typed is stored, hashed, audited or packed (no hash of the upload is
    taken: it would be one of the notes).

    UploadTooLarge (413); LookupError for an unknown labelling; AdjudicationConflict (409) for a cycle with
    a sign-off or a labelling labelled already; CyclePurged (410); AdjudicationError (422) for a sheet
    that fails §11's checks, which changes nothing."""
    if len(data) > MAX_UPLOAD:
        raise UploadTooLarge(
            f"the labelled sheet is over {MAX_UPLOAD // (1 << 20)} MiB: empty the notes column and upload it"
            " again"
        )
    async with ctx.pool.connection() as conn:
        found = await repo.get_labelling(conn, labelling_id)
        if found is None:
            raise LookupError(f"labelling {labelling_id} not found")
        run_id, cycle = found["run_id"], found["cycle"]
        await _unfrozen(conn, run_id, cycle, "labelling")
        if found["labelled_at"] is not None:
            raise _already(labelling_id)
    rows = _read_sheet(data, "the labelled sheet", required=_REQUIRED, again="upload it again")
    sheet, accuracy = score_rows(rows, found["sheet"], found["selection"], _truth(found))
    hashes = {
        "sheet_sha256": sha256_hex(sheet),
        "labels_sha256": sha256_hex(keyless(sheet)),
        "score_sha256": sha256_hex(jsonio.dumps(accuracy, sort_keys=True)),
    }
    said = Counter(row["reason"] or row["label"] for row in parse_csv(sheet.encode())[1])
    undecidable = sum(said[reason] for reason in REASONS)
    counted = {
        "labelled": sum(said.values()),
        "undecidable": undecidable,
        "not_reviewed": said["NOT_REVIEWED"],
    }
    async with repo.audited(ctx.pool) as (conn, audit):
        await _unfrozen(conn, run_id, cycle, "labelling")
        row = await repo.label_labelling(
            conn, labelling_id, sheet=sheet, accuracy=accuracy, labelled_by=who.actor, **hashes
        )
        if row is None:  # labelled meanwhile
            raise _already(labelling_id)
        await audit(
            who, "accuracy.label", labelling_id, {"run_id": run_id, "cycle": cycle} | hashes | counted
        )
    return row


def listed(row: Mapping[str, Any], attested: str | None) -> dict[str, Any]:
    """A labelling as lists show it (a row of ``repo.list_labellings``): its ids, actors, dates, sizes,
    truth and hashes (``LISTED``), its one-line ``summary`` and ``attested``, the requested or approved
    sign-off attesting it. Its selection and score are ``detailed``'s."""
    return {name: row[name] for name in LISTED} | {
        "summary": summary(row, signoff=attested),
        "attested": attested,
    }


def detailed(row: Mapping[str, Any], attested: str | None) -> dict[str, Any]:
    """A labelling in full (``repo.get_labelling``'s row) without its sheet and drawn items (``sheet.csv``
    holds them): its selection's counts and its score, its ``summary``, what it says (``lines``, empty
    until it is labelled) and may not be read as (``limits``), and ``attested``."""
    selection = {name: value for name, value in row["selection"].items() if name != "items"}
    kept = {name: value for name, value in row.items() if name != "sheet"}
    return kept | {
        "selection": selection,
        "summary": summary(row, signoff=attested),
        "lines": lines(row, signoff=attested),
        "limits": LIMITS,
        "attested": attested,
    }


def attesting(signoffs: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Each labelling a requested or approved sign-off of ``signoffs`` attests, with that sign-off's id."""
    return {
        named["labelling_id"]: signoff["approval_id"]
        for signoff in signoffs
        if signoff["status"] in ("requested", "approved") and (named := signoff["attests"].get("accuracy"))
    }


async def reported(
    conn: repo.Conn, run_id: str, cycle: int, signoffs: Sequence[Mapping[str, Any]]
) -> tuple[Row | None, str | None]:
    """The labelling a report of the ended cycle states (§10) and the sign-off attesting it: the one its
    requested or approved sign-off (of ``signoffs``) attests; else, attested by none, the newest a person
    labelled, else the newest labelled; None without one."""
    active = next((s for s in signoffs if s["status"] in ("requested", "approved")), None)
    if named := active and active["attests"].get("accuracy"):
        return await repo.cycle_labelling(conn, run_id, cycle, named["labelling_id"]), active["approval_id"]
    found = await repo.attested_labelling(conn, run_id, cycle, lock=False)
    if found is None:
        drawn = await repo.list_labellings(conn, run_id, cycle)
        newest = next((v["labelling_id"] for v in drawn if v["labelled_at"] is not None), None)
        found = newest and await repo.get_labelling(conn, newest)
    return found, None


# -- what it claims (§7) ----------------------------------------------------------------------------------

_ALONE = {"B_RIGHT": "b", "A_RIGHT": "a"}
_SHORT = {"BOTH_WRONG": "neither", "BOTH_RIGHT": "did not matter", "UNDECIDABLE": "undecidable"}


def _day(at: datetime) -> str:
    return f"{at.astimezone(UTC):%Y-%m-%d}"


def _n(value: int, one: str, many: str) -> str:
    return f"{value:,} {one if value == 1 else many}"


def _side(row: Mapping[str, Any], side: str) -> str:
    return f"{side} ({row[f'role_{side}']})"


def _by(row: Mapping[str, Any], *, named: bool = True) -> str:
    """Who labelled: a person by name, a token or developer mode as what it is; not ``named``, the kind of
    actor alone, without a name or an id."""
    actor, name = row["labelled_by"], row["labelled_by_name"] if named else None
    if actor.startswith("user:"):
        return f"by {name}" if named else "by a person"
    if actor.startswith("token:"):
        return f"through token {name} ({actor.removeprefix('token:')})" if named else "through a token"
    if actor.startswith("dev:"):
        return f"in developer mode ({actor})" if named else "in developer mode"
    return f"by {name} ({actor})" if named else "by no signed-in person"


def _against(row: Mapping[str, Any]) -> str:
    return f"against {row['truth_source']} as of {row['truth_as_of']}"


def _interval(found: Mapping[str, Any]) -> str:
    """A share's interval as lines print it; a census has none."""
    if found["low"] is None:
        return "every item labelled: exact"
    return f"95% interval {percent(found['low'])} to {percent(found['high'])}"


def _either(found: Mapping[str, Any]) -> str:
    worst = found["worst_case"]
    if worst is None:
        return ""
    return f"; {percent(worst['low'])} to {percent(worst['high'])} counting the undecidable either way"


def _among(found: Mapping[str, Any]) -> str:
    """``15 of 117 decided (12.82%, 95% interval …; … counting the undecidable either way)``."""
    if not found["n"]:
        return "0 of 0 decided (no item decided)"
    stated = f"{percent(found['share'])}, {_interval(found)}{_either(found)}"
    return f"{found['x']:,} of {found['n']:,} decided ({stated})"


def _said(row: Mapping[str, Any], labels: Mapping[str, int], order: Sequence[str]) -> str:
    """Label counts in ``order``, zeros left out: ``b (new) alone 17, a (legacy) alone 2, neither 1``."""
    names = {label: f"{_side(row, side)} alone" for label, side in _ALONE.items()} | _SHORT
    return ", ".join(f"{names[label]} {labels[label]:,}" for label in order if labels.get(label)) or "none"


def _warned(row: Mapping[str, Any]) -> list[str]:
    """The warnings after the first line: a circular truth, records not locatable, a labeller not
    independent of the run or of its spec approval."""
    score, found = row["accuracy"], []
    if (side := score["truth"]["names_side"]) is not None:
        found.append(
            f"Warning: the truth source names side {_side(row, side)}: labels resting on a compared side are"
            " circular and measure nothing."
        )
    if score["unlocatable"]:
        found.append(
            "Warning: the key is tokenised and the spec showed no path in clear: labellers could not find"
            " records in the truth source."
        )
    if row["started_run"]:
        found.append(
            f"Warning: the labeller started run {row['run_id']}: the labels are not independent of the run."
        )
    if row["spec_author"]:
        found.append(
            f"Warning: the labeller wrote spec approval {row['spec_author']}: the labels are not independent"
            " of the run."
        )
    return found


def _first(row: Mapping[str, Any], signoff: str | None, *, named: bool = True) -> str:
    """§11's report texts 1 to 3; not ``named``, without who drew it and with the kind of actor that
    labelled it alone."""
    drawn = f" by {row['drawn_by_name']}" if named else ""
    head = (
        f"Labelling {row['labelling_id']} drawn {_day(row['drawn_at'])}{drawn}, labelled"
        f" {_day(row['labelled_at'])} {_by(row, named=named)} {_against(row)}"
    )
    if not row["labelled_by"].startswith("user:"):
        return f"{head}, not by a signed-in person: no sign-off attests it."
    return (
        f"{head}; no sign-off attests it." if signoff is None else f"{head}, attested by sign-off {signoff}."
    )


def lines(row: Mapping[str, Any], *, signoff: str | None) -> list[str]:
    """What a labelled labelling (``repo.get_labelling``'s row) says, printed whole, never a line alone
    (§7): who labelled against what and whether ``signoff`` (the attesting sign-off's id) attests it, its
    warnings, the uniform discrepancy shares with their intervals, each root cause's and rule's own
    figures and the agreed and accepted samples. Empty for a labelling not labelled yet."""
    score = row["accuracy"]
    if score is None:
        return []
    found = [_first(row, signoff)]
    if row["cycle_drawn"] > 1:
        found.append(
            f"Labelling {row['labelling_id']} is one of {row['cycle_drawn']:,} drawn for cycle {row['cycle']}"
            f" ({row['cycle_labelled']:,} labelled)."
        )
    found += _warned(row)
    a, b = _side(row, "a"), _side(row, "b")
    more = f"GET /v1/labellings/{row['labelling_id']} lists them (accuracy/score.json in a pack attesting it)"
    if (d := score["discrepant"]) is not None:
        total, labels, sides = f"{d['items']:,}", d["labels"], d["sides"]
        counted = f"{_n(d['keys'], 'key', 'keys')}, {_n(d['causes'], 'root cause', 'root causes')}"
        found.append(
            f"Discrepancies: {d['uniform']:,} of the {total} that cycle {row['cycle']} of run {row['run_id']}"
            f" observed (one key's difference in one root cause; {counted}), drawn uniformly; side a is"
            f" {row['role_a']}, side b is {row['role_b']}; each label is the truth when its item was"
            " observed."
        )
        if d["uniform"]:
            if u := labels["UNDECIDABLE"]["x"]:
                reasons = ", ".join(f"{d['reasons'][r]:,} {r}" for r in REASONS if r in d["reasons"])
                found.append(
                    f"Undecidable: {u:,} of {d['uniform']:,} ({reasons}); the ranges below count them either"
                    " way."
                )
            else:
                found.append("Undecidable: none.")
            for label, text in (
                ("B_RIGHT", f"Truth favoured {b} alone"),
                ("A_RIGHT", f"Truth favoured {a} alone"),
                ("BOTH_WRONG", "Truth held neither side's values"),
                ("BOTH_RIGHT", "The difference did not matter"),
            ):
                s = labels[label]
                stated = f"{percent(s['share'])}, {_interval(s)}{_either(s)}"
                if s["items_low"] is not None:
                    stated += f": {s['items_low']:,} to {s['items_high']:,} of the {total}"
                found.append(f"{text}: {s['x']:,} of {s['n']:,} ({stated}).")
            found.append(_b_wrong(row, sides))
            found.append(f"{a} wrong (truth favoured b, or neither): {_among(sides['a']['wrong'])}.")
            if (single := d["b_share_of_single_side_right"])["n"]:
                stated = f"{percent(single['share'])}, {_interval(single)}{_either(single)}"
                found.append(
                    f"Where one side alone was right, it was {b} on {single['x']:,} of {single['n']:,}"
                    f" ({stated})."
                )
            decided = d["uniform"] - labels["UNDECIDABLE"]["x"]
            bases = ", ".join(f"{d['basis_codes'][c]:,} {c}" for c in BASES if c in d["basis_codes"])
            found.append(f"Basis of the {decided:,} decided: {bases or 'none'}.")
        order = ("B_RIGHT", "A_RIGHT", "BOTH_WRONG", "BOTH_RIGHT", "UNDECIDABLE")
        for cause in d["by_cause"][:TOP_CAUSES]:
            found.append(
                f"Root cause {cause['root_cause_id']} (rank {cause['rank']} of {d['causes']:,},"
                f" {_n(cause['items'], 'item', 'items')}): of {cause['labelled']:,} labelled,"
                f" {_said(row, cause['labels'], order)}; {b} wrong on {_among(cause['sides']['b']['wrong'])}."
            )
        if (left := len(d["by_cause"]) - TOP_CAUSES) > 0:
            found.append(f"{_n(left, 'more root cause', 'more root causes')}: {more}")
    order = ("BOTH_RIGHT", "A_RIGHT", "B_RIGHT", "BOTH_WRONG", "UNDECIDABLE")
    if (g := score["agreed"]) is not None:
        found.append(
            f"Agreed keys (outcome MATCH): {g['drawn']:,} of {g['keys']:,} drawn; {_drawn(row, g, order)}; an"
            f" agreement hid an error on {_among(g['hidden_error'])}."
        )
    if (k := score["accepted"]) is not None:
        found.append(
            f"Accepted keys (a rule or mask hid the difference): {k['drawn']:,} of {k['keys']:,} drawn;"
            f" {_drawn(row, k, order)}; a rule or mask hid an error on {_among(k['hidden_error'])}."
        )
        ruled = [rule for rule in k["by_rule"] if rule["labelled"] and rule["rule_id"] != MASK]
        for rule in ruled[:TOP_RULES]:
            found.append(
                f"Rule {rule['rule_id']} ({_n(rule['keys'], 'accepted key', 'accepted keys')}): hid an error"
                f" on {_among(rule['hidden_error'])}."
            )
        if (left := len(ruled) - TOP_RULES) > 0:
            found.append(f"{_n(left, 'more rule', 'more rules')}: {more}")
    return found


def _b_wrong(row: Mapping[str, Any], sides: Mapping[str, Any]) -> str:
    return f"{_side(row, 'b')} wrong (truth favoured a, or neither): {_among(sides['b']['wrong'])}."


def gate(
    row: Mapping[str, Any] | None, *, at: str, min_decided: int, max_b_wrong: int, signoff: str | None = None
) -> tuple[str, list[str]]:
    """Cut-over readiness's accuracy gate (docs/mvp/READINESS.md §9 Y18) over the labelling of the cycle
    ``at`` names (``row``: ``repo.readiness_governance``'s, None without one; ``signoff``: the sign-off
    attesting it): ``met`` or ``not_met``, and its lines, the verdict, then ``lines``' labelling line
    without its actors' names (readiness names no person) and side b's wrong share verbatim. Met when at
    most ``max_b_wrong`` of the decided uniform items found side b wrong and ``min_decided`` are decided; a
    census of the discrepant items is judged on all its decided ones, however few, but never on none. A
    labelling that labels no discrepant item counts as none."""
    d = row and row["accuracy"]["discrepant"]
    if not d:
        return "not_met", [
            f"no labelling of {at} labels its discrepant items: draw and label one (Labelled accuracy, Run"
            " screen)"
        ]
    assert row is not None
    wrong, named = d["sides"]["b"]["wrong"], f"labelling {row['labelling_id']} of cycle {row['cycle']}"
    shown = [_first(row, signoff, named=False), _b_wrong(row, d["sides"])]
    if wrong["n"] < (least := 1 if d["census"] else min_decided):
        return "not_met", [
            f"{named} decided {wrong['n']:,} items, under the {least:,} the gate needs",
            *shown,
        ]
    met = wrong["x"] <= max_b_wrong
    return "met" if met else "not_met", [
        f"{named} {'meets' if met else 'does not meet'} the accuracy gate, which allows {max_b_wrong:,}"
        f" decided items where side b was wrong and needs {min_decided:,} decided (all, for a census of the"
        " discrepant items)",
        *shown,
    ]


def _drawn(row: Mapping[str, Any], found: Mapping[str, Any], order: Sequence[str]) -> str:
    return _said(row, {label: s["x"] for label, s in found["labels"].items()}, order)


def _range(found: Mapping[str, Any]) -> str:
    """A decided share as ``summary`` states it: its interval and the range either way."""
    if not found["n"]:
        return "0 of 0 decided (no item decided)"
    stated = (
        "every item labelled: exact"
        if found["low"] is None
        else f"{percent(found['low'])} to {percent(found['high'])}"
    )
    return f"{found['x']:,} of {found['n']:,} decided ({stated}{_either(found)})"


def summary(row: Mapping[str, Any], *, signoff: str | None, named: bool = True) -> str:
    """A labelling in one line (the CLI, run lists; §7): its uniform discrepancy counts and side b's wrong
    share, else what its agreed or accepted sample found; whether ``signoff`` attests it. Not ``named``
    (the agent's, §10), a labelled labelling's line says what kind of actor labelled it, without a name
    or an id."""
    head = f"{row['labelling_id']} run {row['run_id']} cycle {row['cycle']}:"
    score = row["accuracy"]
    if score is None:
        return f"{head} drawn {_day(row['drawn_at'])} by {row['drawn_by_name']} {_against(row)}; not labelled"
    attested = signoff is not None and row["labelled_by"].startswith("user:")
    said = f"labelled {_day(row['labelled_at'])} {_by(row, named=named)} {_against(row)}"
    said += f" (attested by {signoff}):" if attested else " (no sign-off attests it):"
    d, b = score["discrepant"], _side(row, "b")
    if d is not None and d["uniform"]:
        counts = {label: s["x"] for label, s in d["labels"].items()}
        names = {label: f"{_side(row, side)} alone right" for label, side in _ALONE.items()} | _SHORT
        said = f"{d['uniform']:,} of {d['items']:,} discrepancies {said}"
        clauses = [
            ", ".join(f"{names[label]} {counts[label]:,}" for label in ("B_RIGHT", "A_RIGHT", *_SHORT)),
            f"{b} wrong on {_range(d['sides']['b']['wrong'])}",
        ]
    else:
        clauses = [
            f"{what} hid an error on {_range(found['hidden_error'])}"
            for what, found in (("an agreement", score["agreed"]), ("a rule or mask", score["accepted"]))
            if found is not None
        ] or ["no uniform item: its root causes' figures only"]
    if (side := score["truth"]["names_side"]) is not None:
        clauses.append(f"CIRCULAR: the truth source names side {side}")
    if score["unlocatable"]:
        clauses.append("records not locatable (tokenised key)")
    return f"{head} {said} {'; '.join(clauses)}"
