/** A run's ended cycles as its screen lists them: the newest the run's answer holds, then older ones a page at a
 *  time from the cycles route, asked for. */
import type { CycleEvidence, RunDetail } from './api';

/** The cycles route's cursor that starts the older cycles (the oldest cycle the run holds); undefined when the
 *  run holds every ended cycle. */
export function olderFrom(run: RunDetail): string | undefined {
  const [oldest] = run.cycles;
  return oldest && run.cycles_ended > run.cycles.length ? String(oldest.cycle) : undefined;
}

/** The cycles listed: the run's own and the older ones loaded, each once (the run's, the newer answer, wins),
 *  oldest first. */
export function listedCycles(run: RunDetail, older: CycleEvidence[]): CycleEvidence[] {
  const listed = new Map<number, CycleEvidence>();
  for (const c of [...older, ...run.cycles]) listed.set(c.cycle, c);
  return [...listed.values()].sort((x, y) => x.cycle - y.cycle);
}

/** What the list says of the cycles it leaves out; nothing when it lists every ended cycle. */
export const cyclesLine = (run: RunDetail, listed: number) =>
  listed < run.cycles_ended ? `The newest ${listed} of ${run.cycles_ended} ended cycles are listed.` : undefined;
