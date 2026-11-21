/** How the screens shorten what they show: how long ago a time was, a long id in short, and the step from one
 *  difference to the next. Pure, so that Node runs its tests: it loads no other module. */

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;

/** A time of the last week in words (`5 min ago`), counted whole from `now` (milliseconds). Nothing for an older
 *  time, one ahead of this clock by more than a moment, or none: the exact time is shown instead. */
export function ago(iso: string | null, now: number): string | undefined {
  const seconds = (now - (iso ? Date.parse(iso) : NaN)) / 1000;
  if (Number.isNaN(seconds) || seconds < -45 || seconds >= 7 * DAY) return undefined;
  if (seconds < 45) return 'just now';
  if (seconds < HOUR) return `${Math.max(1, Math.floor(seconds / MINUTE))} min ago`;
  if (seconds < DAY) return `${Math.floor(seconds / HOUR)} h ago`;
  const days = Math.floor(seconds / DAY);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}

/** A long id by its start and its end: ids made in the same hour share their start and differ at their end. */
export const shortId = (id: string): string => (id.length > 16 ? `${id.slice(0, 6)}…${id.slice(-5)}` : id);

/** The difference `delta` (1 or -1) from the one at `at` of `count`, round the ends; from none chosen (-1) the
 *  first or the last. -1 when there is none. */
export function step(at: number, count: number, delta: number): number {
  if (!count) return -1;
  if (at < 0) return delta > 0 ? 0 : count - 1;
  return (at + delta + count) % count;
}
