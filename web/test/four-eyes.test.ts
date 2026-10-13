// What the screens say about four-eyes approvals and what a review sends.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { ApiError, api, type Approval, type ApprovalDetail, type Attests, type RunApproval } from '../src/api.ts';
import {
  approvalLabel,
  bodyChanges,
  checklist,
  decisionBody,
  explicitKeys,
  isReauth,
  reauthUrl,
  revokedLine,
  signoffLabel,
  ssoOnly,
} from '../src/four-eyes.ts';

const ID = 'A-01K6ZQ3V8W2N4P6R8T0V2X4Z6B';
const DAY_1 = '2026-09-27T23:30:00+00:00';
const DAY_2 = '2026-09-28T03:00:00+02:00'; // 01:00 UTC
const AMY = { actor: 'user:U-1', name: 'amy', at: DAY_1 };
const BOB = { actor: 'user:U-2', name: 'bob', at: DAY_2 };
const UNREVOKED = { revoked_by: null, revoked_by_name: null, revoked_at: null, reason: null };

function label(approval: Partial<RunApproval>): string {
  return approvalLabel({ approval_id: ID, deciders: [], ...UNREVOKED, ...approval } as RunApproval);
}

test("a run's label says what report.pdf, the P0 summary and ds runs say", () => {
  assert.equal(
    label({ state: 'unapproved', needs: ['clear', 'known_diffs'], mode: 'tokens', approval_id: null }),
    'Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.',
  );
  assert.equal(
    label({ state: 'unapproved', needs: ['clear'], mode: 'none', approval_id: null }),
    'Unapproved spec version: clear was not approved by a second person (developer mode has no approvers). Not sign-off evidence.',
  );
  assert.equal(
    label({ state: 'unapproved', needs: null, mode: null, approval_id: null }),
    'Unapproved spec version: the run started before approvals (008). Not sign-off evidence.',
  );
  assert.equal(label({ state: 'not_needed', needs: [], mode: 'oidc' }), 'The spec version needs no four-eyes approval.');
  assert.equal(label({ state: 'approved', deciders: [BOB] }), `Spec approval ${ID} was granted by bob on 2026-09-28.`);
  assert.equal(
    label({ state: 'approved', deciders: [AMY, BOB] }),
    `Spec approval ${ID} was granted by amy on 2026-09-27 and bob on 2026-09-28.`,
  );
  assert.equal(
    label({ state: 'approved', deciders: [BOB], mode: 'tokens' }), // attached to a run started outside SSO
    `Spec approval ${ID} was granted by bob on 2026-09-28. Not sign-off evidence: the run started in tokens mode.`,
  );
  assert.equal(
    label({ state: 'revoked', deciders: [BOB], revoked_by_name: 'jdoe', revoked_at: DAY_1, reason: 'wrong clear path' }),
    `Spec approval ${ID} was revoked on 2026-09-27 by jdoe (wrong clear path). Not sign-off evidence.`,
  );
});

function signoff(approval_id: string, status: Approval['status'], fields: Partial<Approval> = {}): Approval {
  const steps = { first_by: null, first_by_name: null, first_at: null, decided_by: null, decided_by_name: null, decided_at: null };
  return { ...steps, ...UNREVOKED, revoke_reason: null, approval_id, status, ...fields } as Approval;
}

test("a cycle's sign-off label names the approved one, else the newest revoked", () => {
  const decided = { decided_by: 'user:U-2', decided_by_name: 'bob', decided_at: DAY_2 };
  const revoked = { revoked_by: 'system:worker', revoked_by_name: 'system:worker', revoked_at: DAY_2 };
  const newest = signoff('A-3', 'revoked', { ...decided, ...revoked, revoke_reason: 'cycle 1 purged' });
  const older = signoff('A-2', 'revoked', { ...decided, ...revoked, revoke_reason: 'spec approval A-1 was revoked' });
  const approved = signoff('A-1', 'approved', { first_by: 'user:U-1', first_by_name: 'amy', first_at: DAY_1, ...decided });
  assert.equal(
    signoffLabel([signoff('A-4', 'requested'), newest, older, approved]),
    'Sign-off A-1 was granted by amy on 2026-09-27 and bob on 2026-09-28.',
  );
  assert.equal(
    signoffLabel([newest, older]),
    'Sign-off A-3 was revoked on 2026-09-28 by system:worker (cycle 1 purged). Not sign-off evidence.',
  );
  assert.equal(signoffLabel([signoff('A-4', 'rejected', decided)]), undefined);
});

test('the reauth URL signs in afresh and comes back to the review', () => {
  const url = reauthUrl(ID);
  assert.equal(url, `/v1/auth/login?reauth=1&next=%23%2Fapprovals%2F${ID}`);
  const query = new URL(url, 'https://ds.test').searchParams;
  assert.equal(query.get('reauth'), '1');
  assert.equal(query.get('next'), `#/approvals/${ID}`);
  assert.match(query.get('next')!, /^#\/[A-Za-z0-9/_.~%?&=+*!'()-]{0,500}$/); // the server's check
});

const SPEC_REVIEW = {
  approval_id: ID,
  kind: 'spec',
  scopes: ['clear', 'known_diffs'],
  attests_sha256: null,
  review: { content_hash: 'c'.repeat(64) },
} as unknown as ApprovalDetail;
const SIGNOFF_REVIEW = { approval_id: ID, kind: 'signoff', scopes: [], attests_sha256: 'a'.repeat(64) } as unknown as ApprovalDetail;

test('the review page approves exactly what it showed, and a rejection carries its note', () => {
  assert.deepEqual(decisionBody(SPEC_REVIEW, 'approve', ''), {
    decision: 'approve',
    note: undefined,
    content_hash: 'c'.repeat(64),
    scopes: ['clear', 'known_diffs'],
  });
  assert.deepEqual(decisionBody(SIGNOFF_REVIEW, 'approve', 'counts checked'), {
    decision: 'approve',
    note: 'counts checked',
    attests_sha256: 'a'.repeat(64),
  });
  assert.deepEqual(decisionBody(SPEC_REVIEW, 'reject', 'the clear path exposes names'), {
    decision: 'reject',
    note: 'the clear path exposes names',
  });
});

type Sent = { url: string; method: string; body: unknown; csrf: string | null };
const sent: Sent[] = [];
let answer: { status: number; body: unknown } = { status: 200, body: {} };
globalThis.fetch = (async (input: string, init: RequestInit = {}) => {
  const headers = new Headers(init.headers);
  sent.push({ url: input, method: init.method ?? 'GET', body: init.body && JSON.parse(String(init.body)), csrf: headers.get('X-CSRF-Token') });
  return new Response(JSON.stringify(answer.body), { status: answer.status, headers: { 'Content-Type': 'application/json' } });
}) as typeof fetch;
globalThis.dispatchEvent = () => true;

test('the review page reads the request and posts its decision, with the CSRF token', async () => {
  answer = { status: 200, body: { csrf: 'the-csrf', mode: 'oidc' } };
  await api.me();
  sent.length = 0;
  await api.approvals({ status: 'requested' });
  await api.approval(ID);
  await api.decide(ID, decisionBody(SPEC_REVIEW, 'approve', ''));
  await api.revoke(ID, 'wrong clear path');
  await api.requestApproval(12);
  await api.requestSignoff('R-1', 2, 'cycle 2 is complete');
  assert.deepEqual(sent, [
    { url: '/v1/approvals?status=requested', method: 'GET', body: undefined, csrf: null },
    { url: `/v1/approvals/${ID}`, method: 'GET', body: undefined, csrf: null },
    {
      url: `/v1/approvals/${ID}/decision`,
      method: 'POST',
      body: { decision: 'approve', content_hash: 'c'.repeat(64), scopes: ['clear', 'known_diffs'] },
      csrf: 'the-csrf',
    },
    { url: `/v1/approvals/${ID}/revoke`, method: 'POST', body: { reason: 'wrong clear path' }, csrf: 'the-csrf' },
    { url: '/v1/specs/12/approvals', method: 'POST', body: {}, csrf: 'the-csrf' },
    { url: '/v1/runs/R-1/cycles/2/signoff', method: 'POST', body: { note: 'cycle 2 is complete' }, csrf: 'the-csrf' },
  ]);
});

test('the review page offers a fresh sign-in for reauth_required only', async () => {
  answer = { status: 403, body: { detail: 'reauth_required' } };
  const refused = await api.decide(ID, decisionBody(SPEC_REVIEW, 'approve', '')).catch((e: unknown) => e);
  assert.ok(isReauth(refused));
  for (const [status, detail] of [[403, 'needs the rule_approver role'], [409, 'approval needs an approver other than you: you requested it']] as const)
    assert.ok(!isReauth(new ApiError(status, { detail }, detail)));
});

test("a revoke's answer stays on the review after the reload that unmounts the Revoke panel", () => {
  const answered = { approval_id: ID, status: 'revoked', paused: ['R-1', 'R-2'], stopped: [], revoked: ['A-9'] };
  assert.equal(revokedLine(answered), 'Runs paused: R-1, R-2. Runs stopped: none. Sign-offs revoked: A-9.');
  assert.equal(
    revokedLine({ ...answered, paused: [], stopped: ['R-3'], revoked: [] }),
    'Runs paused: none. Runs stopped: R-3. Sign-offs revoked: none.',
  );
  // The reload answers status revoked, and the panel is drawn for an approved request only: the answer is the
  // review's state, drawn whatever the status, and the panel hands it over and keeps none of its own.
  const screen = readFileSync(new URL('../src/Approvals.tsx', import.meta.url), 'utf8');
  const [review, panel] = ['Review', 'Revoke'].map((name) => screen.match(new RegExp(`\\nfunction ${name}\\(.*?\\n}\\n`, 's'))![0]);
  assert.match(review, /const \[revoked, setRevoked\] = useState<Revoked>\(\);/);
  assert.match(review, /\n {10}\{revoked && \(\s*<section className="panel">\s*<h2>Revoked<\/h2>\s*<p>\{revokedLine\(revoked\)\}<\/p>/);
  assert.match(review, /done=\{\(answer\) => \{\s*setRevoked\(answer\);\s*found\.reload\(\);/);
  assert.match(panel, /done\(await api\.revoke\(a\.approval_id, reason\)\);/);
  assert.ok(!panel.includes('useState<Revoked>') && !panel.includes('revokedLine'));
});

function attests(keys_source: string | null): Attests {
  const counters = { compared: 2, cancelled: 0, outstanding: 0, indeterminate: 1, coverage: 0.6667, absent_both: 1,
    api_uncomparable: 0, reconciliation: [{ identity: 'a', holds: true }, { identity: 'b', holds: true }] };
  const scan = keys_source === 'table_scan'
    ? { done: true, rows_a: 35, rows_b: 34, skipped_a: 1, skipped_b: 0, matched: 32, candidates: 2 }
    : null;
  return { run_id: 'R-1', cycle: 2, mode: 'soak', auth_mode: 'oidc', keys_source, scan, spec_id: 12,
    content_hash: 'c'.repeat(64), spec_approval_id: 'A-1', created_by: 'user:U-3', ended_at: DAY_1, counters,
    unexplained_critical: 0, rules: [], root_causes: [], claim: 'observed' } as Attests;
}

test("a sign-off's checklist flags an explicit key list, which is not a census", () => {
  const source = (keys: string | null) => checklist(attests(keys)).find((c) => c.check === 'Key source')!;
  for (const keys of ['explicit', 'list']) assert.deepEqual(source(keys), {
    check: 'Key source', attested: keys, note: 'An explicit key list, not a census of either side.', flag: true,
  });
  for (const keys of ['table_scan', 'paginated_endpoint']) assert.equal(source(keys).flag, undefined);
  assert.deepEqual(
    checklist(attests('table_scan')).map((c) => [c.check, c.attested]).slice(0, 5),
    [
      ['A live run made under approvals', 'soak run R-1, started in oidc mode'],
      ['Ended, with its own counters', 'cycle 2, ended 2026-09-27'],
      ['Complete', '2 compared, 0 cancelled, 0 owed a recheck'],
      ['Scanned to the end', 'done: rows a 35, b 34 read, skipped a 1, b 0; 32 identical, 2 compared by key'],
      ['Counters reconcile', '2 of 2 identities hold'],
    ],
  );
  assert.ok(!checklist(attests('explicit')).some((c) => c.check === 'Scanned to the end')); // no table scan
});

test("a sign-off's checklist flags a key table's keys too: a key list, not a census of either side", () => {
  const source = checklist(attests('table')).find((c) => c.check === 'Key source')!;
  assert.deepEqual(source, {
    check: 'Key source', attested: 'table', note: 'An explicit key list, not a census of either side.', flag: true,
  });
  assert.deepEqual(['explicit', 'list', 'table', 'hash_sample', null].map(explicitKeys), [true, true, true, false, false]);
});

test('a spec diff marks what changed since the approved version', () => {
  const before = { security: { clear: ['$.a'] }, knownDiffs: [], compare: { '@x': 1, tol: 1 }, gone: true };
  const after = { security: { clear: ['$.a', '$.b'] }, knownDiffs: [{ id: 'KD-1' }], compare: { '@x': 2, tol: '1' }, key: null };
  assert.deepEqual(bodyChanges(before, after), [
    { json_pointer: '/security/clear/1', change_type: 'extra_in_b' },
    { json_pointer: '/knownDiffs/0', change_type: 'extra_in_b' },
    { json_pointer: '/compare/@@x', change_type: 'value_changed' },
    { json_pointer: '/compare/tol', change_type: 'type_changed' },
    { json_pointer: '/gone', change_type: 'missing_in_b' },
    { json_pointer: '/key', change_type: 'extra_in_b' },
  ]);
  assert.deepEqual(bodyChanges(undefined, after), [{ json_pointer: '', change_type: 'extra_in_b' }]); // nothing approved before
  assert.deepEqual(bodyChanges(after, structuredClone(after)), []);
});

test('outside SSO the screens say what the server says', () => {
  assert.equal(ssoOnly('tokens'), 'Approvals need SSO (auth.oidc); this deployment runs in tokens mode.');
  assert.equal(ssoOnly('none'), 'Approvals need SSO (auth.oidc); this deployment runs in developer mode.');
});
