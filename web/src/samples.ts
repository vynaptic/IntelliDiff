/** What the screens say about a sampled run's draws, in the server's words: a
 *  stratum's line as `ds run` prints it, the frame, a draw's state and progress, the ledger and what blocks a
 *  sign-off. The server still decides. */
// Type imports only, so that Node runs the tests of this module without a bundler.
import type { AttestedSample, Frame, LedgerDraw, LedgerStatus, Progress, RunSample, SampleDetail, Stratum } from './api';

export const CLOSE_TITLE = 'Record the fix and close failed draws';
const day = (iso: string) => new Date(iso).toISOString().slice(0, 10);
const minute = (iso: string) => new Date(iso).toISOString().slice(0, 16).replace('T', ' ');
const num = (n: number) => n.toLocaleString('en-US');

/** A share as a percentage, its digits kept: "0.001" is 0.1%, "0.0009951" is 0.09951% (sampling.percent). */
export function percent(share: string): string {
  const [whole, fraction = ''] = share.split('.');
  const digits = `${whole}${fraction.padEnd(2, '0')}`;
  const cut = whole.length + 2;
  const text = `${digits.slice(0, cut).replace(/^0+(?=\d)/, '')}.${digits.slice(cut)}`.replace(/\.$/, '');
  return `${text}%`;
}

/** A sign-off stratum as `ds run` prints it (sample.lines). */
export function strataLine(s: Stratum): string {
  const bound = s.ub95_one_sided === null ? 'compared whole' : `bound ${percent(s.ub95_one_sided)}`;
  return (
    `stratum ${s.label} (${s.tier}, ≤ ${percent(s.target)}): ${num(s.drawn)} drawn of ${num(s.frame_keys)}, ` +
    `${num(s.n)} compared, ${num(s.x)} discrepant, ${num(s.unresolved)} unresolved, ${bound}: ${s.verdict}`
  );
}

/** The frame a draw read, as the report says it. */
export const frameLine = (f: Frame) =>
  `Frame: ${num(f.keys)} distinct keys (a only ${num(f.origin.a)}; b only ${num(f.origin.b)}; both ${num(f.origin.both)}); ` +
  `${num(f.unfit)} outside key.pattern.`;

/** A draw, and its commitment: the salt's label and the selection's SHA-256. */
export const drawLine = (d: Pick<LedgerDraw, 'draw_id' | 'drawn_at' | 'method' | 'salt_label'> & { selection_sha256: string }) =>
  `Draw ${d.draw_id} at ${minute(d.drawn_at)} UTC, by ${d.method}: salt ${d.salt_label} with a nonce the installation drew; ` +
  `selection SHA-256 ${d.selection_sha256}.`;

/** A sampled run's current draw, as `ds runs` says it, or its frame's refusal; nothing for an ended run
 *  never drawn. */
export function drawState(s: RunSample): string | undefined {
  if (s.error) return `The draw was refused: ${s.error}`;
  if (s.state === 'drawing') return 'drawing sample';
  if (!s.draw_id) return s.state === 'paused' ? 'sample to be drawn after resume' : undefined;
  return `sample ${s.draw_id}${s.state ? `: ${s.state}` : ''}`;
}

/** A live draw's pace: keys done, and how long the rest takes at the pace of the last 15 minutes. */
export function etaLine(p: Progress, keys: number): string {
  const done = `${num(p.done)} of ${num(keys)} keys done`;
  if (p.eta_seconds === null) return p.done >= keys ? done : `${done}; no key finished in the last 15 minutes`;
  return `${done}; about ${num(Math.max(1, Math.round(p.eta_seconds / 60)))} min left at the pace of the last 15 minutes`;
}

const UNPASSED: Record<LedgerStatus, string> = {
  failed: 'failed', incomplete: 'ended incomplete', unjudged: 'ended without a judgement', live: 'is live', passed: 'passed',
};

/** Each earlier draw of the population that did not pass and is not closed: the draw's sign-off waits for its close. */
export function blockedLines(draw: Pick<SampleDetail, 'draw_seq' | 'ledger'>): string[] {
  return draw.ledger
    .filter((d) => d.draw_seq < draw.draw_seq && d.blocking)
    .map((d) => `blocked: draw ${d.draw_id} (cycle ${d.cycle}) ${UNPASSED[d.status]} and is not closed`);
}

/** A ledger draw: its name, run and cycle, its status (a failure's strata) and its close. */
export function ledgerLine(d: Pick<LedgerDraw, 'draw_id' | 'spec_name' | 'run_id' | 'cycle' | 'method' | 'status' |
  'failed_strata' | 'closed_by' | 'close_note'> & { closed_by_name?: string | null; closed_at?: string | null }): string {
  const failed = d.status === 'failed' && d.failed_strata.length ? ` (${d.failed_strata.join(', ')})` : '';
  const monitoring = d.method === 'rate' ? ', monitoring' : '';
  const closed = d.closed_by
    ? `; closed by ${d.closed_by_name ?? d.closed_by}${d.closed_at ? ` on ${day(d.closed_at)}` : ''}: ${d.close_note}`
    : '';
  return `${d.draw_id}: spec ${d.spec_name}, run ${d.run_id} cycle ${d.cycle}${monitoring}: ${d.status}${failed}${closed}`;
}

/** The strata a live draw has failed for certain, in the sign-off's words. */
export const certainFailures = (p: Progress) => p.strata.flatMap((s) => s.why.map((why) => `stratum ${s.label} ${why}`));

/** What a sampled sign-off attests of its draw (attests format 2). */
export function attestedLines(s: AttestedSample): string[] {
  return [
    `${drawLine(s)} Salt SHA-256 ${s.salt_sha256}; audit row ${num(s.draw_seq)}.`,
    frameLine(s.frame),
    `Targets: standard ≤ ${percent(s.targets.standard)}, high ≤ ${percent(s.targets.high)}; one-sided ${percent(s.confidence)}; ` +
      `retried up to ${s.resolve.times} times.`,
    s.judgement ? `Judgement: ${s.judgement.status}.` : 'The cycle ended without a judgement.',
  ];
}
