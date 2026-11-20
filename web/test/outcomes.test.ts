// A run's outcome bar, its one-line result and the tab its page opens on.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { DIFFERING, barOf, changesTitle, noChanges, notMatching, resultLine, segments, tabOf, tabsOf } from '../src/outcomes.ts';

const LINKER = { MATCH: 52, MISMATCH: 4, KNOWN_DIFF: 0, NOISE_ONLY: 0, DRIFT: 1, NOT_FOUND_A: 0, NOT_FOUND_B: 1, INCONCLUSIVE: 3 };

test('the bar lists what does not match first, the matches last, and drops empty outcomes', () => {
  assert.deepEqual(
    segments(LINKER).map((s) => [s.outcome, s.n]),
    [['MISMATCH', 4], ['NOT_FOUND_B', 1], ['DRIFT', 1], ['INCONCLUSIVE', 3], ['MATCH', 52]],
  );
});

test('a segment is as wide as its share of the keys', () => {
  const [mismatch] = segments({ MISMATCH: 1, MATCH: 3 });
  assert.equal(mismatch.share, 0.25);
});

test('an outcome this build does not know still gets a segment, before the matches', () => {
  assert.deepEqual(segments({ MATCH: 2, NEWER: 1, MISMATCH: 1 }).map((s) => s.outcome), ['MISMATCH', 'NEWER', 'MATCH']);
});

test('a run without counts has no bar', () => {
  assert.deepEqual(segments(undefined), []);
  assert.deepEqual(segments({ MATCH: 0 }), []);
});

test('the outcomes read for the keys that do not match are every known one but a match, in the bar\'s order', () => {
  assert.deepEqual(DIFFERING, ['MISMATCH', 'NOT_FOUND_A', 'NOT_FOUND_B', 'DRIFT', 'INCONCLUSIVE', 'KNOWN_DIFF', 'NOISE_ONLY']);
  assert.deepEqual(segments(Object.fromEntries(DIFFERING.map((o) => [o, 1]))).map((s) => s.outcome), DIFFERING);
});

// A table scan's identical keys have no result row: the progress leaves them out, the stored counters hold them.
const SCANNED = { MATCH: 985, MISMATCH: 4, NOT_FOUND_A: 1, NOT_FOUND_B: 11 };
const COMPARED = { MISMATCH: 4, NOT_FOUND_A: 1, NOT_FOUND_B: 11 };

test('the bar of an ended cycle is drawn from its stored counters, a scan\'s identical keys included', () => {
  const run = { cycle: 1, counters: { cycle: 1, by_outcome: SCANNED }, scan: { done: true }, progress: { outcomes: COMPARED } };
  assert.equal(barOf(run), SCANNED);
});

test('a scan still at work has no bar: its progress leaves out the identical keys', () => {
  assert.equal(barOf({ cycle: 1, counters: {}, scan: { done: false }, progress: { outcomes: COMPARED } }), undefined);
  const next = { cycle: 2, counters: { cycle: 1, by_outcome: SCANNED }, scan: { done: false }, progress: { outcomes: COMPARED } };
  assert.equal(barOf(next), undefined); // the counters are the cycle before's
});

test('a run of listed keys still at work draws its bar from the keys done so far', () => {
  const live = { MATCH: 20, MISMATCH: 1 };
  assert.equal(barOf({ cycle: 1, counters: {}, scan: null, progress: { outcomes: live } }), live);
  assert.equal(barOf({ cycle: 2, counters: { cycle: 1, by_outcome: SCANNED }, scan: null, progress: { outcomes: live } }), live);
  assert.equal(barOf({ cycle: 1, counters: {}, scan: null }), undefined);
});

test('the keys that need a look are every outcome but a match', () => {
  assert.equal(notMatching(LINKER), 9);
  assert.equal(notMatching(undefined), 0);
});

test('a finished run says how many keys differ and how many could not be determined', () => {
  const counters = { total: 61, match: 52, different: 5, indeterminate: 4, outstanding: 0, cancelled: 0 };
  assert.equal(resultLine(counters), '5 of 61 differ, 4 not determined');
});

test('a run whose keys all match says so', () => {
  assert.equal(resultLine({ total: 1200000, match: 1200000, different: 0, indeterminate: 0, outstanding: 0, cancelled: 0 }), 'All 1,200,000 match');
});

test('a run still at work says how many keys are to go', () => {
  assert.equal(resultLine({ total: 61, match: 20, different: 1, indeterminate: 0, outstanding: 40, cancelled: 0 }), '1 of 61 differ, 40 to go');
});

test('a cancelled run does not claim that all match', () => {
  assert.equal(resultLine({ total: 10, match: 4, different: 0, indeterminate: 0, outstanding: 0, cancelled: 6 }), '0 of 10 differ, 6 cancelled');
});

test('a run with no counters yet has no result line', () => {
  assert.equal(resultLine({}), undefined);
  assert.equal(resultLine({ total: 0 }), undefined);
});

test('a run opens on the keys that do not match', () => {
  assert.equal(tabOf({}, 'batch'), 'differences');
});

test('a link that filters findings opens the findings, and one that names an outcome the keys', () => {
  assert.equal(tabOf({ root_cause_id: 'RC-1' }, 'batch'), 'findings');
  assert.equal(tabOf({ severity: 'HIGH', cycle: '2' }, 'soak'), 'findings');
  assert.equal(tabOf({ outcome: 'DRIFT' }, 'batch'), 'keys');
});

test('a named tab wins over the filters, and an unknown one falls back', () => {
  assert.equal(tabOf({ tab: 'causes', outcome: 'DRIFT' }, 'batch'), 'causes');
  assert.equal(tabOf({ tab: 'nonsense' }, 'batch'), 'differences');
});

test('only a soak run has the tab of changes across cycles', () => {
  assert.equal(tabsOf('batch').some(([tab]) => tab === 'changes'), false);
  assert.equal(tabsOf('soak').some(([tab]) => tab === 'changes'), true);
  assert.equal(tabOf({ tab: 'changes' }, 'batch'), 'differences');
});

test('the changes tab names the two cycles it compares, and a first cycle has none before it', () => {
  assert.equal(changesTitle(5), 'Outcome changes from cycle 4 to cycle 5');
  assert.equal(noChanges(5), 'No key changed its outcome from cycle 4 to cycle 5.');
  assert.equal(changesTitle(1), 'Outcome changes across cycles');
  assert.equal(noChanges(1), "The run's first cycle has none before it to compare with.");
});
