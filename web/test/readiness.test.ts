// What the Readiness screens draw and say besides the API's texts (docs/mvp/READINESS.md §10): the words and
// shapes of states and gates, "k of m", the sparkline's and the step chart's geometry, a brief's figures, notes
// and trend, no percentage anywhere, and what the screens ask of /v1/readiness.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { api, type BriefCycle, type BriefPoint, type GateStatus, type ReadyState, type Transition, type TrendPoint } from '../src/api.ts';
import {
  ADD_UP, GATES, STATES, briefChart, briefDownloads, briefFigures, briefNotes, downloads, filtersOf, isReady,
  programmeLine, ratio, reads, readySteps, runLine, scoreLine, sparkTitle, sparkline, stateShape, statesLine,
  statusShape, stepPath, stepTitle, trendChart, worded,
} from '../src/ready.ts';

const point = (cycle: number, value: number | null, allowed: number | null, failed: string[] = [], sampled = false): TrendPoint => ({
  run_id: 'R-1', cycle, ended_at: `2026-10-0${cycle}T04:00:00+00:00`, spec_id: 12, sampled, value, allowed,
  suppressed: value === null ? null : 4, failed,
});
const TREND = [point(1, 12, 999), point(2, 1204, 999, ['targets']), point(3, 0, 999)];
const move = (at: string, pair: string, from: ReadyState | null, to: ReadyState): Transition => ({
  seq: 1, at, detail: { pair, programme: 'cutover-q4', entity: null, from, to, regressed: false,
    score: { passed: 5, required: 5 }, causes: ['cycle'], changed: [] },
});

// A brief's point: 40 keys compared whole, 3 of them different, so the store holds the rate 0.075.
const measured = (cycle: number, rate: string | null, groups: number | null, extra: Partial<BriefPoint> = {}): BriefPoint => ({
  run_id: 'R-1', cycle, ended_at: `2026-10-0${cycle}T04:00:00+00:00`, version: '1.4.0', spec_id: 12, basis: 'census',
  complete: true, evidence: true, total: 41, compared: 40, different: 3, unresolved: 1, absent_both: 0, api_uncomparable: 0,
  suppressed: 4, unexplained_critical: 0, new: 0, persistent: 0, fixed: 0, other: 0, sample_status: null, graph_rows: null,
  fails: [], skipped: false, boundary: false, mismatch_rate: rate, coverage: '0.97561',
  by_outcome: { MATCH: 33, MISMATCH: 2, KNOWN_DIFF: 3, NOISE_ONLY: 1, DRIFT: 0, NOT_FOUND_A: 1, NOT_FOUND_B: 0, INCONCLUSIVE: 1 },
  cancelled: 0, outstanding: 0, root_causes: groups, marks: [], ...extra,
});
const M8 = 'The gates judge cycle 14 of run R-0; these figures are of cycle 2 of run R-1, the newest complete cycle.';
const M4 = 'Sampled (draw D-1): judged per stratum, with no rate or bound for the strata pooled.';
const M5 = 'These counts are of the keys compared, not of a population.';
const M9 = 'Mismatch rate 0.075: 3 different of 40 compared. Known differences and noise-only keys count as matching; a known difference under a bug rule counts as different.';
const reported = (notes: string[], extra: Partial<BriefPoint> = {}): BriefCycle =>
  ({ ...measured(2, '0.075', 12, extra), draw_id: null, claim: 'observed', strata: null, notes });

test('states and gates are worded as the API words them, each with its own shape', () => {
  assert.deepEqual(STATES.map(worded), ['no evidence', 'not ready', 'unknown', 'parity ready', 'sign-off ready', 'signed off', 'evidenced']);
  assert.deepEqual([worded('sample_ledger'), worded('signoff'), worded('not_applicable')], ['sample ledger', 'sign-off', 'not applicable']);
  assert.equal(GATES.length, 12);
  assert.equal(new Set(STATES.map(stateShape)).size, STATES.length); // never colour alone
  const statuses: GateStatus[] = ['met', 'not_met', 'unknown', 'not_applicable', 'unavailable'];
  assert.equal(new Set(statuses.map(statusShape)).size, statuses.length);
  assert.deepEqual(STATES.map(isReady), [false, false, false, true, true, true, true]);
  assert.equal(isReady(null), false);
});

test('the only figure of a pair is k of m gates', () => {
  assert.equal(scoreLine({ passed: 6, required: 9 }), '6 of 9');
  assert.equal(runLine({ state: 'not_ready', score: { passed: 6, required: 9 } }), 'Pair readiness: not ready (6 of 9 gates)');
  const programme = { programme: 'cutover-q4', pairs: 5, ready: 2, worst: 'not_ready' as const, entities: [] };
  assert.equal(programmeLine(programme), 'cutover-q4: 2 of 5 pairs ready (parity ready or better; worst: not ready)');
  assert.equal(programmeLine({ ...programme, programme: null, pairs: 0, ready: 0, worst: null }), '(no programme): 0 of 0 pairs ready');
  assert.equal(statesLine({ signed_off: 1, not_ready: 2, parity_ready: 3 }), 'not ready 2, parity ready 3, signed off 1');
});

test('the filters of the hash query are those the API takes', () => {
  const query = new URLSearchParams('programme=cutover-q4&state=not_ready&gate=&volume=2&q=cust');
  assert.deepEqual(filtersOf(query), { programme: 'cutover-q4', state: 'not_ready', q: 'cust' });
});

test('the screen offers dashboard.pdf once for each volume, so that no pair is out of its reach', () => {
  assert.deepEqual(downloads(1), [
    { label: 'dashboard.pdf', name: 'dashboard.pdf', volume: undefined, file: 'readiness-dashboard.pdf' },
    { label: 'dashboard.csv', name: 'dashboard.csv', file: 'readiness-dashboard.csv' },
    { label: 'trends.csv', name: 'trends.csv', file: 'readiness-trends.csv' },
  ]);
  const three = downloads(3); // 201 to 300 pairs
  assert.deepEqual(three.map((d) => d.label), ['dashboard.pdf 1 of 3', 'dashboard.pdf 2 of 3', 'dashboard.pdf 3 of 3', 'dashboard.csv', 'trends.csv']);
  assert.deepEqual(three.map((d) => d.volume), [undefined, 2, 3, undefined, undefined]);
  assert.deepEqual(three.slice(0, 3).map((d) => d.file), ['readiness-dashboard.pdf', 'readiness-dashboard-2.pdf', 'readiness-dashboard-3.pdf']);
  assert.equal(downloads(0).length, 3); // a selection of no pair still has its one volume
});

test('sparkline geometry: counts against their allowance on one scale, oldest first', () => {
  const s = sparkline(TREND); // 120 by 24, 3 of padding: the largest count, 1,204, is the top
  assert.deepEqual([s.width, s.height], [120, 24]);
  assert.equal(s.value, '3,20.8 60,3 117,21');
  assert.equal(s.allowed, '3,6.1 60,6.1 117,6.1');
  assert.deepEqual(s.dots, [{ x: 3, y: 20.8, over: false }, { x: 60, y: 3, over: true }, { x: 117, y: 21, over: false }]);
  assert.deepEqual(s.marks, []);
  // An allowance of 0 with nothing discrepant: both lines on the base line, and no division by zero.
  assert.equal(sparkline([point(1, 0, 0), point(2, 0, 0)]).value, '3,21 117,21');
  // One cycle sits in the middle; none draws nothing.
  assert.deepEqual(sparkline([point(1, 5, 10)]).dots, [{ x: 60, y: 12, over: false }]);
  assert.deepEqual(sparkline([]), { width: 120, height: 24, value: '', allowed: '', dots: [], marks: [] });
});

test('a sampled cycle is a pass or fail mark, never a count', () => {
  const s = sparkline([point(1, null, null, [], true), point(2, 3, 10), point(3, null, null, ['targets'], true)]);
  assert.deepEqual(s.marks, [{ x: 3, passed: true }, { x: 117, passed: false }]);
  assert.equal(s.value, '60,15.6');
  assert.equal(
    sparkTitle([point(1, null, null, [], true), point(2, 1204, 999, ['targets']), point(3, null, null, ['targets'], true)]),
    'last 3 evidence cycle(s), oldest first: sampled, passed; 1,204 discrepant or unresolved, 999 allowed, 4 suppressed' +
      ' (failed targets); sampled, not passed (failed targets)',
  );
  assert.equal(sparkTitle([]), 'no evidence cycle');
});

test('the step chart counts the pairs ready from their recorded transitions, walking back from now', () => {
  const now = '2026-10-10T00:00:00+00:00';
  const since = '2026-07-12T00:00:00+00:00';
  const rows = [ // newest first, as the API lists them
    move('2026-10-08T00:00:00+00:00', 'customer-links', 'parity_ready', 'not_ready'),
    move('2026-10-05T00:00:00+00:00', 'other-programme-pair', 'not_ready', 'parity_ready'),
    move('2026-10-04T00:00:00+00:00', 'customer-profile', 'not_ready', 'parity_ready'),
    move('2026-09-01T00:00:00+00:00', 'customer-links', null, 'parity_ready'),
    move('2026-07-01T00:00:00+00:00', 'customer-profile', null, 'not_ready'), // before the window
  ];
  const states = { 'customer-profile': 'signed_off', 'customer-links': 'not_ready', 'legacy-accounts': 'evidenced' } as const;
  const steps = readySteps(rows, states, now, since);
  assert.deepEqual(steps.map((s) => [new Date(s.at).toISOString().slice(0, 10), s.ready]), [
    ['2026-07-12', 1], ['2026-09-01', 2], ['2026-10-04', 3], ['2026-10-08', 2], ['2026-10-10', 2],
  ]);
  assert.equal(stepTitle(steps, 3), 'pairs ready over time: 1 of 3 at the start, 2 of 3 now, 3 recorded change(s)');
  // 240 by 40, 3 of padding: a count holds until the next transition, then steps.
  assert.equal(stepPath(steps, 3), 'M3,25.7H135.6V14.3H221.4V3H231.8V14.3H237V14.3');
  const flat = readySteps([], states, now, since);
  assert.deepEqual(flat.map((s) => s.ready), [2, 2]);
  assert.equal(stepPath(flat, 3), 'M3,14.3H237V14.3');
});

test('a trend marks its version boundaries, its failing cycles and the points that are no evidence', () => {
  const at = (cycle: number, different: number | null, extra: object = {}) => ({
    cycle, run_id: 'R-1', version: '1.4.0', different, unresolved: different === null ? null : 1,
    suppressed: different === null ? null : 3, sample_status: null as string | null, fails: [] as string[],
    skipped: false, boundary: false, ...extra,
  });
  const chart = trendChart([
    at(1, 9), at(2, null, { skipped: true, fails: ['unknown'] }), at(3, 19, { fails: ['targets'], version: '1.5.0', boundary: true }),
  ]);
  assert.equal(chart.line, '14,60 626,14'); // 640 by 120, 14 of padding: 10 and 20 keys of a top of 20
  assert.deepEqual(chart.boundaries, [{ x: 626, version: '1.5.0' }]);
  assert.deepEqual(chart.dots.map((d) => [d.x, d.y, d.failed, d.skipped]), [[14, 60, false, false], [320, 106, false, true], [626, 14, true, false]]);
  assert.deepEqual(chart.dots.map((d) => d.title), [
    'cycle 1 of R-1: 10 discrepant or unresolved, 3 suppressed',
    'cycle 2 of R-1: not counted, no evidence point',
    'cycle 3 of R-1: 20 discrepant or unresolved, 3 suppressed, fails targets',
  ]);
  // A sampled cycle's counts are its sample's own, and said to be: the trend gives no rate and no bound of it.
  assert.equal(
    trendChart([at(4, 6, { sample_status: 'passed' })]).dots[0].title,
    'cycle 4 of R-1: 7 discrepant or unresolved in its sample, 3 suppressed',
  );
});

test("a brief's figures are the cycle's stored counts, six of them adding up to the keys compared", () => {
  const figures = briefFigures(reported([M9]));
  assert.deepEqual(figures.map((f) => [f.label, f.value, f.outcome]), [
    ['Keys attempted', '41', undefined], ['Compared', '40 (coverage 0.97561)', undefined], ['Match', '33', 'MATCH'],
    ['Known difference', '3', 'KNOWN_DIFF'], ['Noise only', '1', 'NOISE_ONLY'], ['Mismatch', '2', 'MISMATCH'],
    ['Not found on a', '1', 'NOT_FOUND_A'], ['Not found on b', '0', 'NOT_FOUND_B'], ['Inconclusive', '1', 'INCONCLUSIVE'],
    ['Drift', '0', 'DRIFT'], ['Unexplained CRITICAL', '0', undefined], ['Root-cause groups', '12', undefined],
  ]);
  assert.equal(figures.slice(2, 8).reduce((sum, f) => sum + Number(f.value), 0), 40);
  assert.equal(ADD_UP, 'Match to Not found on b: these six add up to Compared.');
  // A sampled cycle has no coverage; counters from before the fields, and a purged cycle's groups, are said so.
  const bare = briefFigures(reported([], { coverage: null, by_outcome: null, root_causes: null, compared: 1204 }));
  assert.deepEqual(bare.map((f) => f.value), ['41', '1,204', ...Array(8).fill('not measured'), '0', 'not kept']);
});

test('a stored ratio that rounded its count away is said to be under or over, never a plain 0 or 1', () => {
  // The counters round to six places: 1 different of 3,000,000 compared is stored as 0, and 3,000,000 of 3,000,001 as 1.
  const large = { total: 3_000_001, compared: 3_000_000, different: 1, mismatch_rate: '0', coverage: '1' };
  assert.equal(briefFigures(reported([], large))[1].value, '3,000,000 (coverage over 0.999999)');
  assert.equal(
    briefChart([measured(1, '0', 1, large)]).rate.dots[0].title,
    'cycle 1 of R-1: mismatch rate under 0.000001, 1 different of 3,000,000 compared, 4 suppressed',
  );
  assert.deepEqual([ratio('0', 1, 3_000_000), ratio('1', 3_000_000, 3_000_001)], ['under 0.000001', 'over 0.999999']);
  // Any other is printed as stored: none different, all compared, a sampled cycle's null, counts not held.
  const stored: [string | null, number | null, number | null][] =
    [['0', 0, 40], ['1', 40, 40], ['0.075', 3, 40], [null, 6, 6], ['0', null, null], ['1', null, 5]];
  assert.deepEqual(stored.map((s) => ratio(...s)), ['0', '1', '0.075', null, '0', '1']);
});

test("a brief's notes stand where its report puts them, told by what the cycle holds", () => {
  const judged = { run_id: 'R-0', cycle: 14 };
  assert.deepEqual(briefNotes(reported([M9]), { run_id: 'R-1', cycle: 2 }), { above: [], rate: [M9], sample: [], rest: [] });
  assert.deepEqual(briefNotes(reported([M8, M5, M9], { basis: 'keys' }), judged), { above: [M8], rate: [M9], sample: [], rest: [M5] });
  const sampled = reported([M8, M4, M5], { mismatch_rate: null, sample_status: 'passed', basis: 'rate_sample' });
  assert.deepEqual(briefNotes(sampled, judged), { above: [M8], rate: [], sample: [M4], rest: [M5] });
  // Without an evidence cycle nothing says which the gates judge: its counts stand under the keys note.
  assert.deepEqual(briefNotes(reported([M5, M9], { basis: 'keys' }), null), { above: [], rate: [M9], sample: [], rest: [M5] });
});

test('a root-cause group reads opened, still open or closed, with what changed since it was last seen', () => {
  assert.deepEqual(
    [reads({ state: 'opened', since: [] }), reads({ state: 'still_open', since: [] }), reads({ state: 'closed', since: [] })],
    ['opened', 'still open', 'closed'],
  );
  assert.equal(reads({ state: 'closed', since: ['keys'] }), 'closed, keys changed since');
  assert.equal(reads({ state: 'closed', since: ['version'] }), 'closed, version changed since');
  assert.equal(reads({ state: 'closed', since: ['version', 'keys'] }), 'closed, version and keys changed since');
});

test("a brief's trend is two strips on one x axis, each from 0 to its own printed maximum", () => {
  const chart = briefChart([
    measured(1, '0.075', 12),
    measured(2, null, 6, { sample_status: 'passed', marks: ['keys'] }), // sampled: no rate
    measured(3, '0.2', null, { complete: false, marks: ['version', 'keys'] }), // purged: no groups; incomplete
    measured(4, '0', 3, { different: 0 }),
  ]); // 640 wide, the strips 44 high from x 56, 14 of padding
  assert.deepEqual([chart.width, chart.height, chart.left], [640, 160, 56]);
  assert.deepEqual([chart.rate.top, chart.rate.base, chart.rate.max], [30, 74, '0.2']); // the stored digits, not a new figure
  assert.deepEqual([chart.groups.top, chart.groups.base, chart.groups.max], [102, 146, '12']);
  // A point without the value has no dot, and the line breaks there.
  assert.equal(chart.rate.line, 'M56,57.5M436,30L626,74');
  assert.equal(chart.groups.line, 'M56,102L246,124M626,135');
  assert.deepEqual(chart.rate.dots.map((d) => [d.x, d.y, d.hollow, d.cycle]), [[56, 57.5, false, 1], [436, 30, true, 3], [626, 74, false, 4]]);
  assert.deepEqual(chart.groups.dots.map((d) => [d.x, d.y, d.hollow, d.cycle]), [[56, 102, false, 1], [246, 124, false, 2], [626, 135, false, 4]]);
  assert.deepEqual(chart.marks, [{ x: 246, letters: 'K' }, { x: 436, letters: 'V K' }]);
  assert.deepEqual(chart.rate.dots.map((d) => d.title), [
    'cycle 1 of R-1: mismatch rate 0.075, 3 different of 40 compared, 4 suppressed',
    'cycle 3 of R-1: mismatch rate 0.2, 3 different of 40 compared, 4 suppressed, incomplete',
    'cycle 4 of R-1: mismatch rate 0, 0 different of 40 compared, 4 suppressed',
  ]);
  assert.equal(chart.groups.dots[1].title, 'cycle 2 of R-1: 6 root-cause group(s)');
  assert.deepEqual(chart.groups.dots.map((d) => d.run_id), ['R-1', 'R-1', 'R-1']); // what a dot links to
  // Nothing above 0: the dots sit on the base line, and nothing is divided by zero. One cycle sits in the middle.
  const flat = briefChart([measured(1, '0', 0), measured(2, '0', 0)]);
  assert.deepEqual([flat.rate.max, flat.rate.line, flat.groups.max, flat.groups.line], ['0', 'M56,74L626,74', '0', 'M56,146L626,146']);
  const one = briefChart([measured(1, null, 1204)]);
  assert.deepEqual([one.rate.max, one.rate.dots, one.groups.max], [null, [], '1,204']);
  assert.deepEqual(one.groups.dots.map((d) => [d.x, d.y]), [[341, 102]]);
  const none = briefChart([]);
  assert.deepEqual([none.rate.dots, none.groups.dots, none.marks, none.rate.line, none.groups.max], [[], [], [], '', null]);
});

test('no percentage, probability or forecast: neither built here nor written in the screens', () => {
  const steps = readySteps([], { a: 'not_ready' }, '2026-10-10T00:00:00Z', '2026-07-12T00:00:00Z');
  const brief = briefChart([measured(1, '0.075', 12), measured(2, null, null, { complete: false })]);
  const built = [
    ...STATES.map(worded), ...GATES.map(worded), runLine({ state: 'parity_ready', score: { passed: 5, required: 5 } }),
    programmeLine({ programme: 'p', pairs: 3, ready: 1, worst: 'unknown', entities: [] }), statesLine({ unknown: 2 }),
    sparkTitle(TREND), stepTitle(steps, 1), ...trendChart([]).dots.map((d) => d.title),
    ...[brief.rate, brief.groups].flatMap((strip) => [strip.max ?? '', ...strip.dots.map((d) => d.title)]),
    ...briefFigures(reported([M9])).flatMap((f) => [f.label, f.value]), ADD_UP, reads({ state: 'closed', since: ['version', 'keys'] }),
    ...briefDownloads('customer-profile').flatMap((d) => [d.label, d.file]),
  ];
  const banned = /%|percent|probab|likel|forecast|predict|estimat|weighted|average|\bmean\b|\bETA\b/i;
  assert.deepEqual(built.filter((text) => banned.test(text)), []);
  for (const file of ['../src/ready.ts', '../src/Readiness.tsx']) {
    const source = readFileSync(new URL(file, import.meta.url), 'utf8');
    assert.equal(source.includes('%'), false, file);
    assert.equal(/toFixed|Math\.round\(.*100\)/.test(source), false, file); // no share is ever computed
  }
});

test('the screens read /v1/readiness and write nothing', async () => {
  const calls: [string, string][] = [];
  globalThis.fetch = (async (input: string, init?: RequestInit) => {
    calls.push([init?.method ?? 'GET', input]);
    return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
  }) as typeof fetch;
  await api.readiness({ programme: 'cutover-q4', state: '', cursor: undefined });
  await api.readinessPair('customer-profile');
  await api.readinessPair('customer-profile', true);
  await api.readinessCycles('customer-profile', { bucket: 'day', cursor: '1.R-1.3' });
  await api.transitions({ limit: 500 });
  await api.transitions({ pair: 'customer-profile' });
  await api.readinessReport('trends.csv', { programme: 'cutover-q4', entity: undefined });
  for (const report of downloads(2).slice(0, 2)) await api.readinessReport(report.name, { programme: 'cutover-q4', volume: report.volume });
  await api.readinessBrief('customer-profile');
  const reports = briefDownloads('customer-profile'); // saved under the names `ds readiness report --pair` writes
  assert.deepEqual(reports.map((r) => r.file), ['brief-customer-profile.pdf', 'brief-customer-profile.csv', 'causes-customer-profile.csv']);
  for (const report of reports) await api.readinessReport(report.name, { pair: 'customer-profile' });
  await api.spec('12'); // a version's link: the stored spec, read-only
  assert.deepEqual(calls, [
    ['GET', '/v1/readiness?programme=cutover-q4'],
    ['GET', '/v1/readiness/pairs/customer-profile'],
    ['GET', '/v1/readiness/pairs/customer-profile?precheck=1'],
    ['GET', '/v1/readiness/pairs/customer-profile/cycles?bucket=day&cursor=1.R-1.3'],
    ['GET', '/v1/readiness/transitions?limit=500'],
    ['GET', '/v1/readiness/transitions?pair=customer-profile'],
    ['GET', '/v1/readiness/reports/trends.csv?programme=cutover-q4'],
    ['GET', '/v1/readiness/reports/dashboard.pdf?programme=cutover-q4'],
    ['GET', '/v1/readiness/reports/dashboard.pdf?programme=cutover-q4&volume=2'],
    ['GET', '/v1/readiness/pairs/customer-profile/brief'],
    ['GET', '/v1/readiness/reports/brief.pdf?pair=customer-profile'],
    ['GET', '/v1/readiness/reports/brief.csv?pair=customer-profile'],
    ['GET', '/v1/readiness/reports/causes.csv?pair=customer-profile'],
    ['GET', '/v1/specs/12'],
  ]);
});
