/** What the screens say about a cycle's census, adjudications and evidence packs (docs/mvp/PACK.md §1.2, §5,
 *  §7.3), and which of their controls a cycle takes. The server still decides. */
// Type imports only, so that Node runs the tests of this module without a bundler.
import type { Adjudication, AttestedAdjudication, Census, CensusSide, Engined, Mode, RunDetail, TablePlan } from './api';

/** The UTC day of an ISO time, as retention.ts and the server print it. */
const day = (iso: string) => new Date(iso).toISOString().slice(0, 10);
const num = (n: number) => n.toLocaleString('en-US');
const on = (iso: string | null) => (iso ? `, ${day(iso)}` : '');

/** How a side its engine could not pin was read, where a snapshot stands, and why (docs/mvp/ENGINES.md §13). */
const UNPINNED = {
  unverified: ['transaction', 'transactions', 'a snapshot not observable (a MySQL view)'],
  none: ['pass', 'passes', 'not a snapshot (a MySQL table outside InnoDB)'],
} as const;
const PINNED = ['snapshot', 'snapshots', ''] as const;
const unpinned = (found: Engined) =>
  found.consistency === 'unverified' || found.consistency === 'none' ? UNPINNED[found.consistency] : PINNED;

/** `in 1 snapshot`, what a side its engine could not pin was read in instead, or the files a file side was
 *  read from (docs/mvp/FILES.md §11): an export, never the system it came from, and no digest. */
function read(found: Engined, count = 1): string {
  if (found.consistency === 'fixed')
    return `from ${num(found.files ?? 0)} fixed file(s), ${num(found.bytes ?? 0)} bytes (an export, not the system it came from)`;
  const [one, several, why] = unpinned(found);
  return `in ${count} ${count === 1 ? one : several}${why && `, ${why}`}`;
}

/** Whose view of the rows a MySQL or Oracle side's census is. */
const asRead = (found: Engined) =>
  found.engine && found.consistency !== 'fixed'
    ? '; rows and values as the account reads them (redaction, masking and row policies apply)'
    : '';

/** One side's census record: what it counted and when, the failure no pack gets past, or nothing. */
export function sideLine(side: 'a' | 'b', found: CensusSide | null | undefined): string {
  if (!found) return `side ${side} not covered`;
  if ('failed' in found) return `census of side ${side} failed (${found.failed}): no pack will cover side ${side}`;
  if (found.basis === 'listing') return `side ${side} counted, ${num(found.items)} keys listed${on(found.last_at)}`;
  if (found.basis === 'frame') // a hash sample's frame (docs/mvp/SAMPLING.md §3)
    return `side ${side} counted, ${num(found.rows)} rows of ${found.relation} read to the end ${read(found)} as the sampling frame${on(found.at)}${asRead(found)}`;
  if (found.basis === 'table') {
    const missing = found.missing ? `${num(found.missing)} key(s) missing` : 'none missing';
    const from = found.consistency === 'fixed' ? ` ${read(found)}` : '';
    return `side ${side} counted, ${num(found.rows)} rows of ${found.relation}${from}, ${missing}${on(found.at)}${asRead(found)}`;
  }
  const passes = found.consistency === 'fixed' || found.snapshots ? ` ${read(found, found.snapshots ?? 1)}` : '';
  return `side ${side} counted, ${num(found.rows)} rows of ${found.relation} read to the end${passes}${on(found.last_at)}${asRead(found)}`;
}

/** An ended cycle's census, side a (the baseline) first. */
export const censusLine = (census: Census) => `${sideLine('a', census.a)}; ${sideLine('b', census.b)}`;

/** The certificate's warning for each side its engine could not read in a snapshot, and a file side's
 *  (docs/mvp/FILES.md §11): side b's lines that were not rows, keys held by several rows and a mount DeltaSleuth
 *  could write to. Side a's lines that were not rows are no warning: its cycle is no census and gets no pack. */
export function censusWarnings(census: Census): string[] {
  return (['a', 'b'] as const).flatMap((side) => {
    const found = census[side];
    if (!found || 'failed' in found || found.basis === 'listing') return [];
    const named = `SIDE ${side.toUpperCase()}`;
    const why = unpinned(found)[2];
    return [
      ...(found.rejected && side === 'a'
        ? [`side a's files hold ${found.rejected} line(s) that were not rows: this cycle is not a census, and no pack is issued`]
        : []),
      ...(found.rejected && side === 'b'
        ? [`SIDE B: ${found.rejected} LINE(S) OF ITS FILES WERE NOT ROWS and were not compared: a key on such a line is not compared, and a key not found on this side is inconclusive`]
        : []),
      ...(found.repeated ? [`${named}: ${found.repeated} KEY(S) ARE HELD BY MORE THAN ONE ROW: each is inconclusive, never paired`] : []),
      ...(found.mount === 'writable' ? [`${named} WAS READ FROM A DIRECTORY DELTASLEUTH COULD WRITE TO`] : []),
      ...(why ? [`${named} NOT READ IN A SNAPSHOT (${why}): rows written during the read may be missing or read twice`] : []),
    ];
  });
}

/** A file side in a table scan's line (docs/mvp/FILES.md §16): its format, files and bytes, then its lines that
 *  were not rows and its keys held by several rows, when there are any. Never a file's name or a digest. */
export function fileSide(side: 'A' | 'B', plan: TablePlan, repeated = 0): string {
  return (
    ` Side ${side}: files (${plan.file?.format}), ${num(plan.file_count ?? 0)} files, ${num(plan.bytes ?? 0)} bytes, fixed.` +
    (plan.rejected ? ` ${num(plan.rejected)} lines not rows.` : '') +
    (repeated ? ` ${num(repeated)} repeated keys.` : '')
  );
}

/** The live cycle's key census: pending while the worker has not counted a table side, or a side's failure. */
export function censusNote(run: RunDetail): string | undefined {
  if (run.cycles.some((c) => c.cycle === run.cycle)) return undefined; // ended: its census is the cycle's
  if (run.census_pending.length) return 'census pending';
  const own = run.census?.cycle === run.cycle ? run.census : null;
  const failed = (['a', 'b'] as const).find((side) => {
    const found = own?.[side];
    return found && 'failed' in found;
  });
  return failed && sideLine(failed, own?.[failed]);
}

/** A drawn sheet's score, in the certificate's words. */
export function scoreLine(j: Pick<Adjudication, 'score'>): string {
  if (!j.score) return 'not scored';
  const { adjudicated, agreed, exit } = j.score;
  const verdict = exit.passed ? 'passed' : `did not pass (${exit.reasons.join('; ')})`;
  return `${adjudicated} adjudicated, ${agreed} agreed with the engine: ${verdict}`;
}

/** The adjudication a sign-off attests, or that it attests none. */
export function attestedLine(a: AttestedAdjudication | null | undefined): string {
  if (!a) return 'No adjudication a person scored is attested.';
  const verdict = a.passed ? 'passed' : 'did not pass';
  return `Adjudication ${a.adjudication_id}: ${a.adjudicated} adjudicated, ${a.agreed} agreed with the engine: ${verdict}.`;
}

/** Why a draw, a score or an issue cannot be made here, in the server's words. */
export const packsSsoOnly = (mode?: Mode) =>
  `Evidence packs need SSO (auth.oidc); this deployment runs in ${mode === 'tokens' ? 'tokens mode' : 'developer mode'}.`;

const signoffs = (run: RunDetail, cycle: number) => run.signoffs.filter((s) => s.cycle === cycle);

/** A cycle takes a new adjudication, or a score, while it has no requested or approved sign-off. */
export const unfrozen = (run: RunDetail, cycle: number) =>
  !signoffs(run, cycle).some((s) => s.status === 'requested' || s.status === 'approved');

/** A signed-off cycle can get an evidence pack. */
export const signedOff = (run: RunDetail, cycle: number) => signoffs(run, cycle).some((s) => s.status === 'approved');
