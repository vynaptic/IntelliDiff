// A run's ended cycles as its screen lists them: the run's newest, then older pages asked for.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { CycleEvidence, RunDetail } from '../src/api.ts';
import { cyclesLine, listedCycles, olderFrom } from '../src/cycles.ts';

const cycle = (n: number, packs = 0) => ({ cycle: n, census: null, adjudications: [], labellings: [], packs: Array(packs) }) as CycleEvidence;
const run = (newest: number[], ended: number) => ({ cycles: newest.map((n) => cycle(n)), cycles_ended: ended }) as RunDetail;

test('a run that holds every ended cycle has no older ones to ask for', () => {
  assert.equal(olderFrom(run([1, 2, 3], 3)), undefined);
  assert.equal(olderFrom(run([], 0)), undefined);
  assert.equal(cyclesLine(run([1, 2, 3], 3), 3), undefined);
});

test('older cycles start below the oldest the run holds, and the list says how many it shows', () => {
  const newest = run([4, 5, 6], 6);
  assert.equal(olderFrom(newest), '4'); // the cycles route's cursor: the oldest cycle a page holds
  assert.equal(cyclesLine(newest, 3), 'The newest 3 of 6 ended cycles are listed.');
  assert.equal(cyclesLine(newest, 6), undefined);
});

test('the cycles listed are the run’s and the older ones asked for, each once, oldest first', () => {
  const newest = run([4, 5, 6], 6);
  newest.cycles[0] = cycle(4, 1); // the run's own answer is the newer
  const listed = listedCycles(newest, [cycle(4), cycle(3), cycle(2)]);
  assert.deepEqual(listed.map((c) => [c.cycle, c.packs.length]), [[2, 0], [3, 0], [4, 1], [5, 0], [6, 0]]);
  assert.deepEqual(listedCycles(newest, []).map((c) => c.cycle), [4, 5, 6]);
});
