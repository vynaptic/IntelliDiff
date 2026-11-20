/** What the Runs list and a run's page make of a run's counts: the outcome bar's segments, the one-line result,
 *  the tab the page opens on and what the changes tab says it lists. Every count is the API's; nothing is judged here.
 *  Pure, so that Node runs its tests: it loads no other module. */

// What does not match, worst first; an outcome this build does not know follows them, and the matches come last.
const ORDER = ['MISMATCH', 'NOT_FOUND_A', 'NOT_FOUND_B', 'DRIFT', 'INCONCLUSIVE', 'KNOWN_DIFF', 'NOISE_ONLY'];

/** Every known outcome but a match, in the bar's order. */
export const DIFFERING: readonly string[] = ORDER;

export interface Segment { outcome: string; n: number; share: number }

const rank = (outcome: string) =>
  outcome === 'MATCH' ? ORDER.length + 1 : ORDER.includes(outcome) ? ORDER.indexOf(outcome) : ORDER.length;

/** The bar's segments, of the outcomes that hold a key: each with its count and its share of the keys counted. */
export function segments(byOutcome?: Record<string, number>): Segment[] {
  const counted = Object.entries(byOutcome ?? {}).filter(([, n]) => n > 0);
  const total = counted.reduce((sum, [, n]) => sum + n, 0);
  return counted.sort(([a], [b]) => rank(a) - rank(b)).map(([outcome, n]) => ({ outcome, n, share: n / total }));
}

interface Counted {
  cycle: number;
  counters: Record<string, unknown>;
  scan: unknown; // a table scan's progress; null when the run does not scan
  progress?: { outcomes: Record<string, number> };
}

/** The counts a run's bar is drawn from. Once its current cycle has ended, the stored counters, which hold a
 *  table scan's identical keys. Before that, the keys done so far, and nothing for a scan, whose progress leaves
 *  the identical keys out: a bar of the rest alone would show a share that is not the cycle's. */
export function barOf(run: Counted): Record<string, number> | undefined {
  const stored = run.counters.cycle === run.cycle ? (run.counters.by_outcome as Record<string, number> | undefined) : undefined;
  return stored ?? (run.scan ? undefined : run.progress?.outcomes);
}

/** The keys of every outcome but a match. */
export const notMatching = (byOutcome?: Record<string, number>): number =>
  segments(byOutcome).reduce((sum, s) => sum + (s.outcome === 'MATCH' ? 0 : s.n), 0);

const count = (v: unknown) => (typeof v === 'number' ? v : 0);
const num = (v: number) => v.toLocaleString('en-US');

/** A run's result in a line, from its counters: `5 of 61 differ, 4 not determined`. Nothing before a key is counted. */
export function resultLine(counters: Record<string, unknown>): string | undefined {
  const total = count(counters.total);
  if (!total) return undefined;
  const rest = ([['indeterminate', 'not determined'], ['outstanding', 'to go'], ['cancelled', 'cancelled']] as const)
    .map(([name, words]) => [count(counters[name]), words] as const)
    .filter(([n]) => n > 0);
  const different = count(counters.different);
  if (!different && !rest.length) return `All ${num(total)} match`;
  return [`${num(different)} of ${num(total)} differ`, ...rest.map(([n, words]) => `${num(n)} ${words}`)].join(', ');
}

export type Tab = 'differences' | 'causes' | 'findings' | 'keys' | 'changes' | 'evidence' | 'accuracy';

const TABS: [Tab, string][] = [
  ['differences', 'Not matching'],
  ['causes', 'Root causes'],
  ['findings', 'Findings'],
  ['keys', 'All results'],
  ['changes', 'Changes across cycles'],
  ['evidence', 'Evidence and sign-off'],
  ['accuracy', 'Labelled accuracy'],
];

/** A run's tabs, in order; only a soak run has cycles to change across. */
export const tabsOf = (mode: string): [Tab, string][] => TABS.filter(([tab]) => tab !== 'changes' || mode === 'soak');

const FINDING_FILTERS = ['key', 'taxonomy_code', 'path_pattern', 'root_cause_id', 'severity'];

/** The tab an address opens: the one it names; else the findings when it filters them, the keys when it names an
 *  outcome, and what does not match otherwise. So a root cause's link and a citation stay plain links. */
export function tabOf(query: Record<string, string>, mode: string): Tab {
  const named = tabsOf(mode).find(([tab]) => tab === query.tab);
  if (named) return named[0];
  if (FINDING_FILTERS.some((name) => query[name])) return 'findings';
  return query.outcome ? 'keys' : 'differences';
}

/** The heading of the changes tab: it lists the keys of the cycle shown whose outcome differs from the cycle
 *  before's, and a run's first cycle has none before it. */
export const changesTitle = (cycle: number): string =>
  cycle > 1 ? `Outcome changes from cycle ${cycle - 1} to cycle ${cycle}` : 'Outcome changes across cycles';

/** What the changes tab says when it lists no key. */
export const noChanges = (cycle: number): string =>
  cycle > 1
    ? `No key changed its outcome from cycle ${cycle - 1} to cycle ${cycle}.`
    : "The run's first cycle has none before it to compare with.";
