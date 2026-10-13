// What the Run screen and a sign-off's review say about a sampled run's draws, in the server's words, and what
// they send (docs/mvp/SAMPLING.md §9, §10, §14).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { api, type LedgerDraw, type Progress, type SampleDetail, type Stratum } from '../src/api.ts';
import { sideLine } from '../src/packs.ts';
import {
  attestedLines,
  blockedLines,
  certainFailures,
  drawLine,
  drawState,
  etaLine,
  frameLine,
  ledgerLine,
  percent,
  strataLine,
} from '../src/samples.ts';

const AT = '2026-10-01T09:12:30+00:00';

function stratum(fields: Partial<Stratum>): Stratum {
  return {
    label: 'degree=d0', tier: 'standard', target: '0.001', basis: 'sample', frame_keys: 1_204_331, drawn: 3011,
    contrast: 0, x_max: 0, counters: { outstanding: 0 }, n: 3009, x: 0, unresolved: 2, absent_both: 0,
    api_uncomparable: 0, retried: 0, ub95_one_sided: '0.0009951', ub95_one_sided_all_suppressed: null,
    ub95_one_sided_worst_case: null, verdict: 'passed', why: [], ...fields,
  };
}

test('a stratum reads as `ds run` prints it, its share as a percentage with its digits kept', () => {
  assert.equal(
    strataLine(stratum({})),
    'stratum degree=d0 (standard, ≤ 0.1%): 3,011 drawn of 1,204,331, 3,009 compared, 0 discrepant, 2 unresolved, bound 0.09951%: passed',
  );
  assert.equal(
    strataLine(stratum({ label: 'flagged=Y', tier: 'high', target: '0.0001', basis: 'census', frame_keys: 4210, drawn: 4210,
      n: 4210, x: 1, unresolved: 0, ub95_one_sided: null, verdict: 'failed' })),
    'stratum flagged=Y (high, ≤ 0.01%): 4,210 drawn of 4,210, 4,210 compared, 1 discrepant, 0 unresolved, compared whole: failed',
  );
  assert.deepEqual(['0.001', '0.0001', '0.95', '0.7764', '0.001000', '1', '0.5'].map(percent),
    ['0.1%', '0.01%', '95%', '77.64%', '0.1000%', '100%', '50%']);
});

test('the frame, the draw and the side read as the frame', () => {
  assert.equal(
    frameLine({ keys: 20_211_004, origin: { a: 111_134, b: 106_673, both: 19_993_197 }, unfit: 0 }),
    'Frame: 20,211,004 distinct keys (a only 111,134; b only 106,673; both 19,993,197); 0 outside key.pattern.',
  );
  const draw = { draw_id: 'D-1', drawn_at: AT, method: 'bottom_k', salt_label: 'cutover-q4', selection_sha256: '5c1e' } as const;
  assert.equal(drawLine(draw),
    'Draw D-1 at 2026-10-01 09:12 UTC, by bottom_k: salt cutover-q4 with a nonce the installation drew; selection SHA-256 5c1e.');
  const frame = { basis: 'frame', target: 'db-a', relation: 'public.customers', where: null, rows: 21, skipped: 1, keys: 20,
    unfit: 0, at: AT } as const;
  assert.equal(sideLine('a', frame), 'side a counted, 21 rows of public.customers read to the end in 1 snapshot as the sampling frame, 2026-10-01');
});

test("a run's draw state, a live draw's pace and its certain failures", () => {
  assert.equal(drawState({ state: 'drawing', error: null, draw_id: null, drawn_at: null }), 'drawing sample');
  assert.equal(drawState({ state: 'paused', error: 'the frame is empty', draw_id: null, drawn_at: null }), 'The draw was refused: the frame is empty');
  assert.equal(drawState({ state: 'comparing', error: null, draw_id: 'D-1', drawn_at: AT }), 'sample D-1: comparing');
  assert.equal(drawState({ state: null, error: null, draw_id: 'D-1', drawn_at: AT }), 'sample D-1');
  // Paused before its draw: drawn after resume; ended undrawn (stopped while drawing): nothing to say.
  assert.equal(drawState({ state: 'paused', error: null, draw_id: null, drawn_at: null }), 'sample to be drawn after resume');
  assert.equal(drawState({ state: 'paused', error: null, draw_id: 'D-1', drawn_at: AT }), 'sample D-1: paused');
  assert.equal(drawState({ state: null, error: null, draw_id: null, drawn_at: null }), undefined);
  const progress = (done: number, eta: number | null, strata: Stratum[] = []) =>
    ({ status: 'incomplete', done, finished_recently: 10, eta_seconds: eta, strata }) as unknown as Progress;
  assert.equal(etaLine(progress(1204, 3600), 126_453), '1,204 of 126,453 keys done; about 60 min left at the pace of the last 15 minutes');
  assert.equal(etaLine(progress(12, null), 20), '12 of 20 keys done; no key finished in the last 15 minutes');
  assert.equal(etaLine(progress(20, null), 20), '20 of 20 keys done');
  const why = 'is over its 0.1% target: compared whole, its 3 keys allow 0 discrepant or unresolved key(s); it has 1';
  assert.deepEqual(certainFailures(progress(3, null, [stratum({ label: 'segment=other', why: [why] }), stratum({})])),
    [`stratum segment=other ${why}`]);
});

function draw(fields: Partial<LedgerDraw>): LedgerDraw {
  return {
    draw_id: 'D-1', run_id: 'R-1', spec_id: 1, spec_name: 'customer-profile', cycle: 9, method: 'bottom_k', population: [],
    salt_label: 'cutover-q4', drawn_at: AT, keys: 6, closed_by: null, closed_by_name: null, closed_at: null, close_note: null,
    ended_at: AT, status: 'failed', failed_strata: ['kind=PERSON'], draw_seq: 10, close_seq: null, ...fields,
  };
}

test('the ledger: each draw, its status and close, and what blocks the sign-off of a later draw', () => {
  const ledger = [
    draw({ blocking: true }),
    draw({ draw_id: 'D-2', status: 'unjudged', failed_strata: [], draw_seq: 11, blocking: true }),
    draw({ draw_id: 'D-3', status: 'failed', draw_seq: 12, closed_by: 'user:U-1', closed_by_name: 'amy', closed_at: AT,
      close_note: 'fixed the mapping', blocking: false }),
    draw({ draw_id: 'D-4', status: 'passed', failed_strata: [], draw_seq: 13 }),
    draw({ draw_id: 'D-5', status: 'failed', draw_seq: 14, blocking: true }), // later: R-c, not R-a
  ];
  const shown = { draw_seq: 13, ledger } as Pick<SampleDetail, 'draw_seq' | 'ledger'>;
  assert.deepEqual(blockedLines(shown), [
    'blocked: draw D-1 (cycle 9) failed and is not closed',
    'blocked: draw D-2 (cycle 9) ended without a judgement and is not closed',
  ]);
  assert.equal(ledgerLine(ledger[0]), 'D-1: spec customer-profile, run R-1 cycle 9: failed (kind=PERSON)');
  assert.equal(ledgerLine(ledger[2]), 'D-3: spec customer-profile, run R-1 cycle 9: failed (kind=PERSON); closed by amy on 2026-10-01: fixed the mapping');
  assert.equal(ledgerLine(draw({ method: 'rate', status: 'live', failed_strata: [] })), 'D-1: spec customer-profile, run R-1 cycle 9, monitoring: live');
});

test('what a sampled sign-off attests of its draw', () => {
  const lines = attestedLines({
    draw_id: 'D-1', method: 'bottom_k', salt_label: 'cutover-q4', nonce: '3f9a', salt_sha256: '9ab0', drawn_at: AT,
    draw_seq: 81_200, population: ['table:legacy-db/customers'], targets: { standard: '0.001', high: '0.0001' },
    confidence: '0.95', sided: 'one-sided upper', margin: '0.005', resolve: { times: 3, delay_s: 600 },
    frame: { keys: 6, origin: { a: 6, b: 0, both: 0 }, unfit: 0 }, strata: [], selection_sha256: '5c1e', ledger: [],
    judgement: null,
  });
  assert.deepEqual(lines, [
    'Draw D-1 at 2026-10-01 09:12 UTC, by bottom_k: salt cutover-q4 with a nonce the installation drew; selection SHA-256 5c1e. Salt SHA-256 9ab0; audit row 81,200.',
    'Frame: 6 distinct keys (a only 6; b only 0; both 0); 0 outside key.pattern.',
    'Targets: standard ≤ 0.1%, high ≤ 0.01%; one-sided 95%; retried up to 3 times.',
    'The cycle ended without a judgement.',
  ]);
});

type Sent = { url: string; method: string; body: unknown; csrf: string | null };
const sent: Sent[] = [];
globalThis.fetch = (async (input: string, init: RequestInit = {}) => {
  const headers = new Headers(init.headers);
  sent.push({ url: input, method: init.method ?? 'GET', body: init.body, csrf: headers.get('X-CSRF-Token') });
  return new Response(JSON.stringify({ csrf: 'the-csrf', items: [], next_cursor: null, closed: ['D-1'] }), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  });
}) as typeof fetch;

test('the screens read the ledger and a draw, and close draws with a note and the CSRF token', async () => {
  await api.me();
  sent.length = 0;
  await api.samples({ spec_name: 'customer-profile' });
  await api.sample('D-1');
  assert.deepEqual(await api.closeDraws('customer-profile', 'fixed the mapping'), { csrf: 'the-csrf', items: [], next_cursor: null, closed: ['D-1'] });
  assert.deepEqual(sent, [
    { url: '/v1/samples?spec_name=customer-profile', method: 'GET', body: undefined, csrf: null },
    { url: '/v1/samples/D-1', method: 'GET', body: undefined, csrf: null },
    { url: '/v1/samples/close', method: 'POST', body: '{"spec_name":"customer-profile","note":"fixed the mapping"}', csrf: 'the-csrf' },
  ]);
});
