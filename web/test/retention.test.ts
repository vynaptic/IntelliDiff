// What the Run screen and the chat say about retention (docs/mvp/RETENTION.md §4.4), from GET /v1/runs/{id}.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { Retention, RunDetail } from '../src/api.ts';
import { chatClosed, day, isPurged, purgedBanner, retainedLine } from '../src/retention.ts';

function run(cycle: number, retention: Partial<Retention> = {}): RunDetail {
  const kept: Retention = { class: 'standard', days: 90, hold: null, purged: [], last_purged_at: null, cycles: [] };
  return { run_id: 'R-1', cycle, retention: { ...kept, ...retention } } as RunDetail;
}

const ENDED = { cycle: 1, ended_at: '2026-09-27T23:30:00+00:00', expires_at: '2026-12-26T23:30:00+00:00' };

test('an ended cycle shows the day its evidence expires, as report.pdf does', () => {
  assert.equal(retainedLine(run(2, { cycles: [ENDED] }), 1), 'Evidence retained until 2026-12-26 (retention standard)');
});

test('a live cycle shows its days after it ends', () => {
  const audit = run(2, { class: 'audit', days: 365, cycles: [ENDED] });
  assert.equal(retainedLine(audit, 2), 'Evidence retained until 365 days after the cycle ends (retention audit)');
});

test('a held run shows the hold instead of a date', () => {
  const hold = { reason: 'legal hold LH-7', by: 'local:ann@host', at: '2026-12-01T09:00:00+00:00' };
  assert.equal(
    retainedLine(run(1, { hold, cycles: [ENDED] }), 1),
    'Evidence held until released (retention standard): held by local:ann@host since 2026-12-01, legal hold LH-7',
  );
});

test('days are UTC, whatever offset the server wrote', () => {
  assert.equal(day('2026-12-26T01:00:00+05:00'), '2026-12-25');
});

test('a purged cycle gets the banner, and the chat closes once the current cycle is purged', () => {
  const soak = run(3, { purged: [1] });
  assert.ok(isPurged(soak, 1) && !isPurged(soak, 2));
  assert.equal(purgedBanner(1), 'Cycle 1 was purged: its keys, results, findings and evidence are deleted.');
  assert.equal(chatClosed(soak), undefined);
  assert.equal(chatClosed(run(1, { purged: [1] })), "The run's evidence was purged; AI answers need it.");
});
