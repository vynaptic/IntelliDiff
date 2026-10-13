// Which controls the Run screen's "Labelled accuracy" block offers, which labelling it prints, what the screens send,
// and that every result text is the API's.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { ApiError, api, type Approval, type Labelling, type RunDetail } from '../src/api.ts';
import { DRAW_HINT, needsAcknowledgement, stated, takesLabelling, takesLabels } from '../src/accuracy.ts';

const signoff = (cycle: number, status: Approval['status']) => ({ approval_id: `A-${status}`, cycle, status }) as Approval;
const run = (signoffs: Approval[], purged: number[] = []) => ({ run_id: 'R-1', signoffs, retention: { purged } }) as RunDetail;
const labelling = (id: string, fields: Partial<Labelling> = {}) =>
  ({ labelling_id: id, cycle: 1, labelled_at: null, labelled_by: null, attested: null, ...fields }) as Labelling;
const labelled = (id: string, by: string, attested: string | null = null) =>
  labelling(id, { labelled_at: '2026-10-04T09:00:00+00:00', labelled_by: by, attested });

test('a cycle takes a draw or labels until its sign-off is requested or its purge starts, and a labelling its labels once', () => {
  const soak = run([signoff(1, 'approved'), signoff(2, 'requested'), signoff(3, 'rejected'), signoff(4, 'revoked')], [5]);
  assert.deepEqual([1, 2, 3, 4, 5, 6].map((c) => takesLabelling(soak, c)), [false, false, true, true, false, true]);
  assert.ok(takesLabels(run([]), labelling('V-1')));
  assert.ok(!takesLabels(run([]), labelled('V-1', 'user:U-1')));
  assert.ok(!takesLabels(run([signoff(1, 'requested')]), labelling('V-1')));
  assert.ok(!takesLabels(run([], [1]), labelling('V-1')));
});

test('the block prints the attested labelling, else the newest a person labelled, else the newest labelled', () => {
  const blank = labelling('V-5');
  const [token, person, older] = [labelled('V-4', 'token:T-1'), labelled('V-3', 'user:U-1'), labelled('V-2', 'user:U-2')];
  assert.equal(stated([blank, token, person, labelled('V-2', 'user:U-2', 'A-1')])?.labelling_id, 'V-2');
  assert.equal(stated([blank, token, person, older])?.labelling_id, 'V-3');
  assert.equal(stated([blank, token, labelled('V-1', 'dev:loopback')])?.labelling_id, 'V-4');
  assert.equal(stated([blank]), undefined);
  assert.equal(stated([]), undefined);
});

test('the draw dialog offers the acknowledgement only when the server asks for it, and hints at the size', () => {
  const asked =
    'the key is tokenised (class account_number) and the spec shows no path in clear: labellers cannot find records in the ' +
    'truth source until the per-key reveal exists; set acknowledge_unlocatable to draw for labels a business ' +
    'rule decides without the record';
  assert.ok(needsAcknowledgement(new ApiError(422, { detail: asked }, asked)));
  const other = 'truth.as_of must be a date (YYYY-MM-DD) no later than today';
  assert.ok(!needsAcknowledgement(new ApiError(422, { detail: other }, other)));
  assert.ok(!needsAcknowledgement(new ApiError(409, { detail: asked }, asked)));
  assert.ok(!needsAcknowledgement(asked));
  assert.equal(DRAW_HINT, '100 items give an interval about ±10 points wide at worst; 400 about ±5');
});

type Sent = { url: string; method: string; type: string | null; body: unknown; csrf: string | null };
const sent: Sent[] = [];
globalThis.fetch = (async (input: string, init: RequestInit = {}) => {
  const headers = new Headers(init.headers);
  const body = init.body instanceof Blob ? await init.body.text() : init.body;
  sent.push({ url: input, method: init.method ?? 'GET', type: headers.get('Content-Type'), body, csrf: headers.get('X-CSRF-Token') });
  return new Response(JSON.stringify({ csrf: 'the-csrf' }), { status: 200, headers: { 'Content-Type': 'application/json' } });
}) as typeof fetch;

test('the screens draw, list, read, download and label the file as it is, with the CSRF token', async () => {
  await api.me();
  sent.length = 0;
  const truth = { source: 'CBS-PROD.account_master', as_of: '2026-10-01' };
  const sheet = new Blob(['item,label\r\n1,b_right\r\n'], { type: 'text/csv' });
  await api.drawLabelling('R-1', 2, { size: 100, per_cause: 2, accepted: 0, agreed: 50, truth });
  await api.drawLabelling('R-1', 2, { truth, acknowledge_unlocatable: true });
  await api.labellings('R-1', 2);
  await api.labelling('V-1');
  await api.labellingSheet('V-1');
  await api.label('V-1', sheet);
  const json = 'application/json';
  const asked = JSON.stringify(truth);
  assert.deepEqual(sent, [
    { url: '/v1/runs/R-1/cycles/2/labellings', method: 'POST', type: json, csrf: 'the-csrf',
      body: `{"size":100,"per_cause":2,"accepted":0,"agreed":50,"truth":${asked}}` },
    { url: '/v1/runs/R-1/cycles/2/labellings', method: 'POST', type: json, csrf: 'the-csrf',
      body: `{"truth":${asked},"acknowledge_unlocatable":true}` },
    { url: '/v1/runs/R-1/cycles/2/labellings', method: 'GET', type: null, body: undefined, csrf: null },
    { url: '/v1/labellings/V-1', method: 'GET', type: null, body: undefined, csrf: null },
    { url: '/v1/labellings/V-1/sheet.csv', method: 'GET', type: null, body: undefined, csrf: null },
    { url: '/v1/labellings/V-1/labels', method: 'POST', type: 'text/csv', body: 'item,label\r\n1,b_right\r\n', csrf: 'the-csrf' },
  ]);
});

test('no screen builds a result text: shares, intervals, warnings and limits are the API\'s lines, summary and limits', () => {
  // Words only accuracy.lines, summary and LIMITS state: no screen may state them itself.
  const statedOnly = /favoured|alone right|wrong on|hid an error|undecidable|decided \(|circular|certified|overall accuracy/i;
  for (const name of ['accuracy.ts', 'Run.tsx', 'Approvals.tsx']) {
    const source = readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8');
    assert.ok(!statedOnly.test(source), `${name}: ${source.match(statedOnly)?.[0]}`);
  }
  const screens = readFileSync(new URL('../src/Run.tsx', import.meta.url), 'utf8') + readFileSync(new URL('../src/Approvals.tsx', import.meta.url), 'utf8');
  for (const shown of ['v.summary', 'said.data?.lines.map', 'said.data.limits', 'drawn.warnings.map', 'a.review.lines?.map', 'found.data.limits'])
    assert.ok(screens.includes(shown), shown);
});
