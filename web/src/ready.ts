/** What the Readiness screens draw and say besides the API's own texts: a state's or a
 *  gate's words and shape, "k of m gates", a programme's line, the sparkline of a pair's last evidence cycles, the
 *  pairs-ready steps of a programme's recorded transitions, and a pair's brief: its figures, where its notes
 *  stand and its trend. Counts, and in a brief the mismatch rate a cycle's counters store, as stored: nothing
 *  here computes a share, a rate, a mean or a forecast, and every gate's text is the API's. */
// Type imports only, so that Node runs the tests of this module without a bundler.
import type {
  BriefCycle, BriefGroup, BriefPoint, CycleRef, GateStatus, Mark, ReadyPair, ReadyProgramme, ReadyState, Score,
  Transition, TrendPoint,
} from './api';

export const STATES: ReadyState[] = [
  'no_evidence', 'not_ready', 'unknown', 'parity_ready', 'signoff_ready', 'signed_off', 'evidenced',
]; // the ladder, lowest first
export const GATES = [
  'population', 'critical', 'targets', 'convergence', 'fresh', 'graph', 'spec_approval', 'adjudication',
  'sample_ledger', 'accuracy', 'signoff', 'pack',
];
export const FILTERS = ['programme', 'entity', 'state', 'gate', 'closed', 'q'];
// A shape beside each word, so that neither a state nor a gate is told by colour alone.
const STATE_SHAPE: Record<ReadyState, string> = {
  no_evidence: '○', not_ready: '✕', unknown: '?', parity_ready: '◔', signoff_ready: '◑', signed_off: '◕', evidenced: '●',
};
const STATUS_SHAPE: Record<GateStatus, string> = { met: '■', not_met: '✕', unknown: '?', not_applicable: '–', unavailable: '□' };
const STATUS_TONE: Record<GateStatus, string> = { met: 'ok', not_met: 'bad', unknown: 'warn', not_applicable: 'muted', unavailable: 'muted' };

const num = (n: number) => n.toLocaleString('en-US');
const round = (n: number) => Math.round(n * 10) / 10;

/** A state, a gate or a status as the API's texts spell it: `signoff_ready` is sign-off ready. */
export const worded = (code: string) => code.replace('signoff', 'sign-off').replaceAll('_', ' ');

export const stateShape = (state: ReadyState) => STATE_SHAPE[state];
export const statusShape = (status: GateStatus) => STATUS_SHAPE[status];
export const statusTone = (status: GateStatus) => STATUS_TONE[status];
export const stateTone = (state: ReadyState) => (state === 'not_ready' ? 'bad' : isReady(state) ? 'ok' : 'warn');

/** Parity-ready or better: what a programme counts as ready. */
export const isReady = (state: ReadyState | null) => state !== null && STATES.indexOf(state) >= STATES.indexOf('parity_ready');

/** The only figure of a pair: its required gates met, of those counted. */
export const scoreLine = (score: Score) => `${score.passed} of ${score.required}`;

/** The Run screen's line on its pair. */
export const runLine = (pair: Pick<ReadyPair, 'state' | 'score'>) =>
  `Pair readiness: ${worded(pair.state)} (${scoreLine(pair.score)} gates)`;

/** A programme's roll-up: its open pairs that are parity-ready or better, and its worst one. */
export function programmeLine(p: ReadyProgramme): string {
  const worst = p.worst ? ` (parity ready or better; worst: ${worded(p.worst)})` : '';
  return `${p.programme ?? '(no programme)'}: ${p.ready} of ${p.pairs} pairs ready${worst}`;
}

/** An entity type's open pairs by state, lowest first. */
export const statesLine = (states: Partial<Record<ReadyState, number>>) =>
  STATES.filter((s) => states[s]).map((s) => `${worded(s)} ${states[s]}`).join(', ');

/** The filters a hash query holds, as GET /v1/readiness takes them. */
export const filtersOf = (query: URLSearchParams): Record<string, string> =>
  Object.fromEntries(FILTERS.flatMap((name) => (query.get(name) ? [[name, query.get(name)!]] : [])));

/** A report the screen offers: its button's label, the report asked for (a later dashboard volume by its
 *  number) and the file it is saved as. */
export interface Download {
  label: string;
  name: string;
  volume?: number;
  file: string;
}

/** The downloads of a selection: dashboard.pdf once for each of its volumes of 100 pairs, so that no pair
 *  is out of the screen's reach, then the two CSVs. */
export const downloads = (volumes: number): Download[] => [
  ...Array.from({ length: Math.max(1, volumes) }, (_, i) => ({
    label: volumes > 1 ? `dashboard.pdf ${i + 1} of ${volumes}` : 'dashboard.pdf',
    name: 'dashboard.pdf',
    volume: i ? i + 1 : undefined, // the first is the one the API gives without a number
    file: i ? `readiness-dashboard-${i + 1}.pdf` : 'readiness-dashboard.pdf',
  })),
  ...['dashboard.csv', 'trends.csv'].map((name) => ({ label: name, name, file: `readiness-${name}` })),
];

export interface Spark {
  width: number;
  height: number;
  value: string; // polyline points: each counted cycle's discrepant or unresolved keys
  allowed: string; // polyline points: what its tier allows
  dots: { x: number; y: number; over: boolean }[]; // a counted cycle; over: more than allowed
  marks: { x: number; passed: boolean }[]; // a sampled cycle, judged per stratum: passed or failed, no count
}

/** The sparkline of a pair's last evidence cycles, oldest first: the count against its allowance on one scale
 *  (its largest count or allowance, at least 1), a sampled cycle as a pass or fail mark at mid height. */
export function sparkline(trend: TrendPoint[], width = 120, height = 24, pad = 3): Spark {
  const counted = trend.flatMap((p) => [p.value ?? 0, p.allowed ?? 0]);
  const top = Math.max(1, ...counted);
  const step = trend.length > 1 ? (width - 2 * pad) / (trend.length - 1) : 0;
  const x = (i: number) => round(trend.length > 1 ? pad + i * step : width / 2);
  const y = (v: number) => round(height - pad - (v / top) * (height - 2 * pad));
  const spark: Spark = { width, height, value: '', allowed: '', dots: [], marks: [] };
  const value: string[] = [];
  const allowed: string[] = [];
  trend.forEach((p, i) => {
    if (p.sampled) spark.marks.push({ x: x(i), passed: p.failed.length === 0 });
    if (p.value === null || p.allowed === null) return;
    value.push(`${x(i)},${y(p.value)}`);
    allowed.push(`${x(i)},${y(p.allowed)}`);
    spark.dots.push({ x: x(i), y: y(p.value), over: p.value > p.allowed });
  });
  return { ...spark, value: value.join(' '), allowed: allowed.join(' ') };
}

/** What the sparkline shows, in words: each cycle's count and allowance, or a sampled cycle's verdict. */
export function sparkTitle(trend: TrendPoint[]): string {
  if (!trend.length) return 'no evidence cycle';
  const said = trend.map((p) => {
    const failed = p.failed.filter((gate) => gate !== 'unknown');
    const tail = failed.length ? ` (failed ${failed.join(', ')})` : '';
    if (p.sampled) return `sampled, ${p.failed.length ? 'not passed' : 'passed'}${tail}`;
    if (p.value === null || p.allowed === null) return 'not measured';
    return `${num(p.value)} discrepant or unresolved, ${num(p.allowed)} allowed, ${num(p.suppressed ?? 0)} suppressed${tail}`;
  });
  return `last ${trend.length} evidence cycle(s), oldest first: ${said.join('; ')}`;
}

export interface Step { at: number; ready: number }
export const DAYS = 90; // how far back a programme's step chart reads its transitions

/** How many of `states`' pairs (a programme's open pairs, each with its state now) were ready from `since` to
 *  `now`, as their recorded transitions (newest first) tell it: before a row, its pair was in the row's `from`
 *  (not recorded yet: not counted as ready). Oldest first: the count at `since`, each change, the count now. */
export function readySteps(rows: Transition[], states: Record<string, ReadyState>, now: string, since: string): Step[] {
  const start = Date.parse(since);
  const held: Record<string, ReadyState | null> = { ...states };
  let ready = Object.values(held).filter(isReady).length;
  const steps: Step[] = [{ at: Date.parse(now), ready }];
  for (const row of rows) {
    const at = Date.parse(row.at);
    const pair = row.detail.pair;
    if (at < start || !Object.hasOwn(held, pair)) continue;
    steps.push({ at, ready });
    ready += Number(isReady(row.detail.from)) - Number(isReady(held[pair]));
    held[pair] = row.detail.from;
  }
  steps.push({ at: start, ready });
  return steps.reverse();
}

/** The steps as an SVG path over `pairs` pairs: a count holds until the next transition. */
export function stepPath(steps: Step[], pairs: number, width = 240, height = 40, pad = 3): string {
  const [first, last] = [steps[0].at, steps.at(-1)!.at];
  const x = (at: number) => round(last > first ? pad + ((at - first) / (last - first)) * (width - 2 * pad) : pad);
  const y = (n: number) => round(height - pad - (n / Math.max(1, pairs)) * (height - 2 * pad));
  return steps.map((s, i) => (i ? `H${x(s.at)}V${y(s.ready)}` : `M${x(s.at)},${y(s.ready)}`)).join('');
}

/** What the step chart shows, in words. */
export const stepTitle = (steps: Step[], pairs: number) =>
  `pairs ready over time: ${steps[0].ready} of ${pairs} at the start, ${steps.at(-1)!.ready} of ${pairs} now, ${steps.length - 2} recorded change(s)`;

export interface TrendChart {
  width: number;
  height: number;
  line: string; // polyline points: each counted cycle's discrepant or unresolved keys
  dots: { x: number; y: number; failed: boolean; skipped: boolean; title: string }[];
  boundaries: { x: number; version: string }[]; // where the version changes
}

/** A pair's trend, oldest first: each measured cycle's discrepant or unresolved keys (a cycle without counts sits on
 *  the base line; a sampled cycle's are its sample's own, said so), with its suppressed keys beside them, a cycle that
 *  is no evidence hollow, a failing one marked, and a marker where the version changes. */
export function trendChart(
  points: { cycle: number; run_id: string; version: string | null; different: number | null; unresolved: number | null;
    suppressed: number | null; sample_status: string | null; fails: string[]; skipped: boolean; boundary: boolean }[],
  width = 640, height = 120, pad = 14,
): TrendChart {
  const count = (p: { different: number | null; unresolved: number | null }) =>
    p.different === null ? null : p.different + (p.unresolved ?? 0);
  const top = Math.max(1, ...points.map((p) => count(p) ?? 0));
  const step = points.length > 1 ? (width - 2 * pad) / (points.length - 1) : 0;
  const x = (i: number) => round(points.length > 1 ? pad + i * step : width / 2);
  const y = (v: number) => round(height - pad - (v / top) * (height - 2 * pad));
  const chart: TrendChart = { width, height, line: '', dots: [], boundaries: [] };
  const line: string[] = [];
  points.forEach((p, i) => {
    const n = count(p);
    const failed = p.fails.filter((gate) => gate !== 'unknown');
    if (n !== null) line.push(`${x(i)},${y(n)}`);
    if (p.boundary) chart.boundaries.push({ x: x(i), version: p.version ?? '' });
    chart.dots.push({
      x: x(i), y: y(n ?? 0), failed: failed.length > 0, skipped: p.skipped,
      title: `cycle ${p.cycle} of ${p.run_id}: ` +
        (n === null ? 'not counted' : `${num(n)} discrepant or unresolved${p.sample_status ? ' in its sample' : ''}, ${num(p.suppressed ?? 0)} suppressed`) +
        (p.skipped ? ', no evidence point' : '') + (failed.length ? `, fails ${failed.join(', ')}` : ''),
    });
  });
  return { ...chart, line: line.join(' ') };
}

/** A pair's reports of its brief, saved under the names `ds readiness report --pair` writes. */
export const briefDownloads = (pair: string): Download[] =>
  ['brief.pdf', 'brief.csv', 'causes.csv'].map((name) => ({ label: name, name, file: name.replace('.', `-${pair}.`) }));

/** A listed group's state as a brief words it (`core/readiness.reads`): a closed one with what changed since it
 *  was last seen, which may be why it is no longer observed. */
export const reads = (group: Pick<BriefGroup, 'state' | 'since'>) =>
  worded(group.state) + (group.since.length ? `, ${group.since.join(' and ')} changed since` : '');

// The outcomes of a key compared, as a brief names them: their counts add up to the keys compared.
const COMPARED = [
  ['Match', 'MATCH'], ['Known difference', 'KNOWN_DIFF'], ['Noise only', 'NOISE_ONLY'], ['Mismatch', 'MISMATCH'],
  ['Not found on a', 'NOT_FOUND_A'], ['Not found on b', 'NOT_FOUND_B'],
];
export const ADD_UP = `${COMPARED[0][0]} to ${COMPARED.at(-1)![0]}: these six add up to Compared.`;

export interface Figure { label: string; value: string; outcome?: string } // outcome: what its link filters the run by

/** A stored ratio of `part` in `whole` as a brief prints it (`core/readiness.ratio`). The counters round to six
 *  places: one that reads 0 of a part above none is `under 0.000001`, one that reads 1 of a part short of its
 *  whole `over 0.999999`; any other is printed as stored. */
export function ratio(stored: string | null, part: number | null, whole: number | null): string | null {
  if (stored === '0' && part) return 'under 0.000001';
  return stored === '1' && part !== null && whole !== null && part < whole ? 'over 0.999999' : stored;
}

/** The stored figures of a brief's reported cycle, in the brief's order: counts as stored, and the coverage its
 *  counters hold beside the keys compared. */
export function briefFigures(c: BriefCycle): Figure[] {
  const count = (v: number | null | undefined, none = 'not measured') => (v == null ? none : num(v));
  const of = ([label, outcome]: string[]): Figure => ({ label, value: count(c.by_outcome?.[outcome]), outcome });
  const coverage = ratio(c.coverage, c.compared, c.total);
  return [
    { label: 'Keys attempted', value: count(c.total) },
    { label: 'Compared', value: count(c.compared) + (coverage === null ? '' : ` (coverage ${coverage})`) },
    ...COMPARED.map(of),
    ...[['Inconclusive', 'INCONCLUSIVE'], ['Drift', 'DRIFT']].map(of),
    { label: 'Unexplained CRITICAL', value: count(c.unexplained_critical) },
    { label: 'Root-cause groups', value: count(c.root_causes, 'not kept') },
  ];
}

/** Where a reported cycle's notes stand, told by what the cycle holds: that the gates judge another cycle
 *  (`evidence`: the pair's) above its figures; under them its stored rate, or how its sample is judged, which
 *  its strata follow; and last that its counts are of keys only. */
export function briefNotes(c: BriefCycle, evidence: CycleRef | null) {
  const rest = [...c.notes];
  const above = evidence && (evidence.run_id !== c.run_id || evidence.cycle !== c.cycle) ? rest.splice(0, 1) : [];
  const rate = c.mismatch_rate !== null ? rest.splice(-1) : [];
  const sample = c.sample_status !== null ? rest.splice(0, 1) : [];
  return { above, rate, sample, rest };
}

export interface Strip {
  top: number; // y of its maximum
  base: number; // y of 0
  max: string | null; // its maximum as printed: the largest value's own digits; null when no point holds one
  line: string; // an SVG path through its dots, broken where a point holds no value
  dots: { x: number; y: number; hollow: boolean; run_id: string; cycle: number; title: string }[];
}
export interface BriefChart {
  width: number;
  height: number;
  left: number; // where the strips begin: their maxima are printed before it
  rate: Strip; // each cycle's stored mismatch rate
  groups: Strip; // each cycle's root-cause groups
  marks: { x: number; letters: string }[]; // a marked point, lettered as the brief's legend says
}
export const MARKS: Record<Mark, string> = { version: 'V', keys: 'K' };

/** A brief's trend, oldest first, as two strips on one x axis: each cycle's stored mismatch rate above its
 *  root-cause groups, each strip from 0 to its own maximum, which is printed. A point without the value has no
 *  dot and breaks the line (a sampled cycle has no rate, a purged one keeps no groups); an incomplete cycle's dots
 *  are hollow; a marked point gets a lettered vertical line. The rate is the store's own text, placed: none is
 *  computed. */
export function briefChart(points: BriefPoint[], width = 640, strip = 44, left = 56, pad = 14): BriefChart {
  const step = points.length > 1 ? (width - left - pad) / (points.length - 1) : 0;
  const x = (i: number) => round(points.length > 1 ? left + i * step : (left + width - pad) / 2);
  const drawn = (top: number, read: (p: BriefPoint) => { v: number; text: string; said: string } | null): Strip => {
    const base = top + strip;
    const held = points.map(read);
    const most = held.reduce((m, h) => (h && (!m || h.v > m.v) ? h : m), null);
    const y = (v: number) => round(most && most.v > 0 ? base - (v / most.v) * strip : base);
    const found: Strip = { top, base, max: most && most.text, line: '', dots: [] };
    held.forEach((h, i) => {
      if (!h) return;
      const { run_id, cycle, complete } = points[i];
      found.line += `${held[i - 1] ? 'L' : 'M'}${x(i)},${y(h.v)}`;
      const title = `cycle ${cycle} of ${run_id}: ${h.said}${complete ? '' : ', incomplete'}`;
      found.dots.push({ x: x(i), y: y(h.v), hollow: !complete, run_id, cycle, title });
    });
    return found;
  };
  const rate = drawn(pad + 16, (p) => {
    if (p.mismatch_rate === null) return null;
    const counted = p.different === null || p.compared === null ? '' :
      `, ${num(p.different)} different of ${num(p.compared)} compared, ${num(p.suppressed ?? 0)} suppressed`;
    const said = `mismatch rate ${ratio(p.mismatch_rate, p.different, p.compared)}${counted}`;
    return { v: Number(p.mismatch_rate), text: p.mismatch_rate, said };
  });
  const groups = drawn(rate.base + 28, (p) =>
    p.root_causes === null ? null : { v: p.root_causes, text: num(p.root_causes), said: `${num(p.root_causes)} root-cause group(s)` });
  const marks = points.flatMap((p, i) => (p.marks.length ? [{ x: x(i), letters: p.marks.map((m) => MARKS[m]).join(' ') }] : []));
  return { width, height: groups.base + pad, left, rate, groups, marks };
}
