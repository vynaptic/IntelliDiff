// What the Run screen and a review say about a cycle's census, adjudications and evidence packs, which controls
// a cycle takes, and what the screens send (docs/mvp/PACK.md §1.2, §5, §7.1, §7.3).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { api, type Approval, type Census, type CycleEvidence, type RunDetail, type TablePlan } from '../src/api.ts';
import { attestedLine, censusLine, censusNote, censusWarnings, fileSide, packsSsoOnly, scoreLine, sideLine, signedOff, unfrozen } from '../src/packs.ts';

const AT = '2026-09-20T23:30:00+00:00'; // 23:30 UTC: the UTC day
const TABLE = { basis: 'table', target: 'legacy', relation: 'legacy.accounts', where: null, rows: 5_000_120, skipped: 0,
  missing: 0, extra: 0, at: AT } as const;

test("a side's census says what it counted and when, a failure that no pack will cover it, or that it is not covered", () => {
  const census: Census = { cycle: 2, at: AT, a: TABLE, b: null };
  assert.equal(censusLine(census), 'side a counted, 5,000,120 rows of legacy.accounts, none missing, 2026-09-20; side b not covered');
  assert.equal(sideLine('a', { ...TABLE, missing: 12 }), 'side a counted, 5,000,120 rows of legacy.accounts, 12 key(s) missing, 2026-09-20');
  assert.equal(
    sideLine('a', { basis: 'listing', target: 'svc-a', items: 1204, first_at: AT, last_at: '2026-09-21T00:10:00+01:00' }),
    'side a counted, 1,204 keys listed, 2026-09-20',
  );
  const scan = { basis: 'scan', target: 'db-a', relation: 'public.accounts', rows: 10, skipped: 1, first_at: AT, last_at: AT } as const;
  assert.equal(sideLine('b', { ...scan, snapshots: 1 }), 'side b counted, 10 rows of public.accounts read to the end in 1 snapshot, 2026-09-20');
  assert.equal(sideLine('a', { ...scan, snapshots: 3 }), 'side a counted, 10 rows of public.accounts read to the end in 3 snapshots, 2026-09-20');
  assert.equal(
    sideLine('a', { failed: 'QueryCanceled' }),
    'census of side a failed (QueryCanceled): no pack will cover side a',
  );
});

test('a side on MySQL or Oracle says how it was read where a snapshot stands, whose view it is, and warns when it was not pinned', () => {
  const scan = { basis: 'scan', target: 'db-a', relation: 'ledger.accounts', rows: 10, skipped: 0, snapshots: 1, first_at: AT, last_at: AT, engine: 'mysql' } as const;
  const asRead = '; rows and values as the account reads them (redaction, masking and row policies apply)';
  const view = 'a snapshot not observable (a MySQL view)';
  const other = 'not a snapshot (a MySQL table outside InnoDB)';
  const counted = 'side a counted, 10 rows of ledger.accounts read to the end in';
  assert.equal(sideLine('a', { ...scan, consistency: 'snapshot' }), `${counted} 1 snapshot, 2026-09-20${asRead}`);
  assert.equal(sideLine('a', { ...scan, consistency: 'unverified' }), `${counted} 1 transaction, ${view}, 2026-09-20${asRead}`);
  assert.equal(sideLine('a', { ...scan, consistency: 'none' }), `${counted} 1 pass, ${other}, 2026-09-20${asRead}`);
  assert.equal(sideLine('a', { ...scan, consistency: 'none', snapshots: 3 }), `${counted} 3 passes, ${other}, 2026-09-20${asRead}`);
  const frame = { basis: 'frame', target: 'db-a', relation: 'ledger.v', where: null, rows: 21, skipped: 0, keys: 21, unfit: 0, at: AT, engine: 'mysql', consistency: 'unverified' } as const;
  assert.equal(sideLine('b', frame), `side b counted, 21 rows of ledger.v read to the end in 1 transaction, ${view} as the sampling frame, 2026-09-20${asRead}`);
  assert.equal(sideLine('a', { ...TABLE, engine: 'oracle', consistency: 'snapshot' }), `side a counted, 5,000,120 rows of legacy.accounts, none missing, 2026-09-20${asRead}`);
  const notPinned = 'rows written during the read may be missing or read twice';
  const census: Census = { cycle: 1, at: AT, a: { ...scan, consistency: 'unverified' }, b: { ...scan, consistency: 'none' } };
  assert.deepEqual(censusWarnings(census), [`SIDE A NOT READ IN A SNAPSHOT (${view}): ${notPinned}`, `SIDE B NOT READ IN A SNAPSHOT (${other}): ${notPinned}`]);
  // A snapshot, a PostgreSQL side (its record names no engine), a failed count and an uncovered side warn of nothing.
  assert.deepEqual(censusWarnings({ cycle: 1, at: AT, a: { ...scan, consistency: 'snapshot' }, b: TABLE }), []);
  assert.deepEqual(censusWarnings({ cycle: 1, at: AT, a: { failed: 'TargetDenied' }, b: null }), []);
});

test('a file side says what it was read from where a snapshot stands, without a digest, and warns as the certificate does', () => {
  // docs/mvp/FILES.md §11: fixed files, never a snapshot and never the system they were exported from.
  const fixed = { engine: 'files', consistency: 'fixed', files: 12, bytes: 3_650_722_201, mount: 'read-only', rejected: 0, repeated: 0 } as const;
  const from = 'from 12 fixed file(s), 3,650,722,201 bytes (an export, not the system it came from)';
  const scan = { basis: 'scan', target: 'exports', relation: 'customers', rows: 10, skipped: 0, first_at: AT, last_at: AT, ...fixed } as const;
  assert.equal(sideLine('a', scan), `side a counted, 10 rows of customers read to the end ${from}, 2026-09-20`);
  const frame = { basis: 'frame', target: 'exports', relation: 'customers', where: null, rows: 21, skipped: 0, keys: 21, unfit: 0, at: AT, ...fixed, repeated: null } as const;
  assert.equal(sideLine('a', frame), `side a counted, 21 rows of customers read to the end ${from} as the sampling frame, 2026-09-20`);
  assert.equal(sideLine('b', { ...TABLE, relation: 'customers', ...fixed }), `side b counted, 5,000,120 rows of customers ${from}, none missing, 2026-09-20`);
  for (const line of [sideLine('a', scan), sideLine('a', frame)]) assert.doesNotMatch(line, /snapshot|sha256|as the account reads/);
  assert.deepEqual(censusWarnings({ cycle: 1, at: AT, a: scan, b: frame }), []);
  const b = { ...scan, rejected: 3, repeated: 2, mount: 'writable' } as const;
  assert.deepEqual(censusWarnings({ cycle: 1, at: AT, a: { ...scan, repeated: 5 }, b }), [
    'SIDE A: 5 KEY(S) ARE HELD BY MORE THAN ONE ROW: each is inconclusive, never paired',
    'SIDE B: 3 LINE(S) OF ITS FILES WERE NOT ROWS and were not compared: a key on such a line is not compared, and a key not found on this side is inconclusive',
    'SIDE B: 2 KEY(S) ARE HELD BY MORE THAN ONE ROW: each is inconclusive, never paired',
    'SIDE B WAS READ FROM A DIRECTORY DELTASLEUTH COULD WRITE TO',
  ]);
  // On side a such lines are no warning: the cycle is no census (C1).
  assert.deepEqual(censusWarnings({ cycle: 1, at: AT, a: { ...scan, rejected: 3 }, b: null }), [
    "side a's files hold 3 line(s) that were not rows: this cycle is not a census, and no pack is issued",
  ]);
});

test("a table scan's line names a file side's format, files and bytes, then its lines that were not rows and its repeated keys", () => {
  const plan: TablePlan = { schema: null, name: 'customers', keys: ['id'], columns: {}, engine: 'files', server: 'duckdb 1.5', consistency: 'fixed',
    file: { path: 'customers/current/*.csv', format: 'csv' }, file_count: 12, bytes: 3_650_722_201, rejected: 0 };
  assert.equal(fileSide('A', plan), ' Side A: files (csv), 12 files, 3,650,722,201 bytes, fixed.');
  assert.equal(fileSide('B', { ...plan, rejected: 3 }, 5), ' Side B: files (csv), 12 files, 3,650,722,201 bytes, fixed. 3 lines not rows. 5 repeated keys.');
  assert.equal(fileSide('A', { ...plan, file: { path: 'c.parquet', format: 'parquet' } }, 5), ' Side A: files (parquet), 12 files, 3,650,722,201 bytes, fixed. 5 repeated keys.');
});

function run(fields: Partial<RunDetail>): RunDetail {
  return { run_id: 'R-1', cycle: 3, census: null, census_pending: [], cycles: [], signoffs: [], ...fields } as RunDetail;
}
const ended = (cycle: number): CycleEvidence => ({ cycle, census: null, adjudications: [], labellings: [], packs: [] });

test("the live cycle's census is pending until the worker counts it, then shows a failure; an ended cycle has its own", () => {
  assert.equal(censusNote(run({ census_pending: ['a'] })), 'census pending');
  const failed: Census = { cycle: 3, at: AT, a: { failed: 'QueryCanceled' }, b: null };
  assert.equal(censusNote(run({ census: failed })), 'census of side a failed (QueryCanceled): no pack will cover side a');
  assert.equal(censusNote(run({ census: { ...failed, cycle: 2 } })), undefined); // another cycle's
  assert.equal(censusNote(run({ census: { ...failed, a: TABLE } })), undefined);
  assert.equal(censusNote(run({ census: failed, cycles: [ended(3)] })), undefined);
});

test('a score and the attested adjudication read as the certificate says them', () => {
  assert.equal(scoreLine({ score: null }), 'not scored');
  assert.equal(scoreLine({ score: { adjudicated: 50, agreed: 50, exit: { passed: true, reasons: [] } } }), '50 adjudicated, 50 agreed with the engine: passed');
  const reasons = ['49 key(s) adjudicated; the exit needs at least 50', '1 disagreement(s) between reviewers and the engine'];
  assert.equal(
    scoreLine({ score: { adjudicated: 49, agreed: 48, exit: { passed: false, reasons } } }),
    `49 adjudicated, 48 agreed with the engine: did not pass (${reasons.join('; ')})`,
  );
  const attested = { adjudication_id: 'J-1', sheet_sha256: 's', score_sha256: 't', adjudicated: 50, agreed: 50, passed: true };
  assert.equal(attestedLine(attested), 'Adjudication J-1: 50 adjudicated, 50 agreed with the engine: passed.');
  assert.equal(attestedLine(null), 'No adjudication a person scored is attested.');
  assert.equal(packsSsoOnly('tokens'), 'Evidence packs need SSO (auth.oidc); this deployment runs in tokens mode.');
  assert.equal(packsSsoOnly('none'), 'Evidence packs need SSO (auth.oidc); this deployment runs in developer mode.');
});

test('a cycle takes a draw or a score until its sign-off is requested, and a pack once it is approved', () => {
  const signoff = (cycle: number, status: Approval['status']) => ({ approval_id: `A-${status}`, cycle, status }) as Approval;
  const soak = run({ signoffs: [signoff(1, 'approved'), signoff(2, 'requested'), signoff(3, 'rejected'), signoff(4, 'revoked')] });
  assert.deepEqual([1, 2, 3, 4].map((c) => unfrozen(soak, c)), [false, false, true, true]);
  assert.deepEqual([1, 2, 3, 4].map((c) => signedOff(soak, c)), [true, false, false, false]);
});

type Sent = { url: string; method: string; type: string | null; body: unknown; csrf: string | null };
const sent: Sent[] = [];
globalThis.fetch = (async (input: string, init: RequestInit = {}) => {
  const headers = new Headers(init.headers);
  const body = init.body instanceof Blob ? await init.body.text() : init.body;
  sent.push({ url: input, method: init.method ?? 'GET', type: headers.get('Content-Type'), body, csrf: headers.get('X-CSRF-Token') });
  return new Response(JSON.stringify({ csrf: 'the-csrf' }), { status: 200, headers: { 'Content-Type': 'application/json' } });
}) as typeof fetch;

test('the Run screen draws, downloads, scores the file as it is, issues and downloads packs, with the CSRF token', async () => {
  await api.me();
  sent.length = 0;
  const sheet = new Blob(['key,reviewer_verdict\r\nK001,match\r\n'], { type: 'text/csv' });
  await api.draw('R-1', 2, 50);
  await api.draw('R-1', 2);
  await api.adjudications('R-1', 2);
  await api.sheet('J-1');
  await api.score('J-1', sheet);
  await api.issuePack('R-1', 2);
  await api.packs({ run_id: 'R-1', status: 'issued' });
  await api.pack('P-1');
  await api.downloadPack('P-1');
  await api.revocations();
  const json = 'application/json';
  assert.deepEqual(sent, [
    { url: '/v1/runs/R-1/cycles/2/adjudications', method: 'POST', type: json, body: '{"limit":50}', csrf: 'the-csrf' },
    { url: '/v1/runs/R-1/cycles/2/adjudications', method: 'POST', type: json, body: '{}', csrf: 'the-csrf' },
    { url: '/v1/runs/R-1/cycles/2/adjudications', method: 'GET', type: null, body: undefined, csrf: null },
    { url: '/v1/adjudications/J-1/sheet.csv', method: 'GET', type: null, body: undefined, csrf: null },
    { url: '/v1/adjudications/J-1/score', method: 'POST', type: 'text/csv', body: 'key,reviewer_verdict\r\nK001,match\r\n', csrf: 'the-csrf' },
    { url: '/v1/runs/R-1/cycles/2/packs', method: 'POST', type: json, body: '{}', csrf: 'the-csrf' },
    { url: '/v1/packs?run_id=R-1&status=issued', method: 'GET', type: null, body: undefined, csrf: null },
    { url: '/v1/packs/P-1', method: 'GET', type: null, body: undefined, csrf: null },
    { url: '/v1/packs/P-1/download', method: 'GET', type: null, body: undefined, csrf: null },
    { url: '/v1/packs/revocations', method: 'GET', type: null, body: undefined, csrf: null },
  ]);
});
