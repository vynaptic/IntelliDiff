/** What the screens say about a cycle's evidence (docs/mvp/RETENTION.md §4.4): until when it is kept, in
 *  report.pdf's words, or that it was purged; and whether the chat can still answer. */
import type { RunDetail } from './api';

/** The UTC day of an ISO time, as report.pdf and the CLI print it. */
export const day = (iso: string) => new Date(iso).toISOString().slice(0, 10);

/** Whether the cycle's purge has started: its results, findings, key views and reports then answer 410. */
export const isPurged = (run: RunDetail, cycle: number) => run.retention.purged.includes(cycle);

/** Until when the evidence of a cycle not purged is kept. */
export function retainedLine(run: RunDetail, cycle: number): string {
  const { class: kind, days, hold, cycles } = run.retention;
  const tag = `(retention ${kind})`;
  if (hold) return `Evidence held until released ${tag}: held by ${hold.by} since ${day(hold.at)}, ${hold.reason}`;
  const ended = cycles.find((c) => c.cycle === cycle);
  if (!ended) return `Evidence retained until ${days} days after the cycle ends ${tag}`;
  return `Evidence retained until ${day(ended.expires_at)} ${tag}`;
}

export const purgedBanner = (cycle: number) =>
  `Cycle ${cycle} was purged: its keys, results, findings and evidence are deleted.`;

/** Why the chat takes no question, if it takes none: answers need the current cycle's evidence. */
export const chatClosed = (run: RunDetail) =>
  isPurged(run, run.cycle) ? "The run's evidence was purged; AI answers need it." : undefined;
