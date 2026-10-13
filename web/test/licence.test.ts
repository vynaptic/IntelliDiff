// What the screens say about the licence and which controls it disables, from
// GET /v1/licence and a run's `licence`; the server still decides. An open build's screens say that no licence
// is needed, disable nothing and show no licence banner.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { api, type Allows, type Licence, type LicenceFields, type Run } from '../src/api.ts';
import {
  LICENCE_PAUSE, allowsLine, installLine, licenceLine, namedLine, pairsLine, pausedLine, refusal, reportFile,
} from '../src/licensing.ts';

const source = (file: string) => readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');

const ALL: Allows = { runs: true, sso: true, signoff: true, packs: true };
const PRO: LicenceFields = {
  licence_id: 'LI-01JB8Q7ZK3M9W2XR5T4VNH6D0C', customer: 'Example Bank plc', order: 'ORD-2026-014', tier: 'pro',
  pairs: 5, starts: '2026-10-01', ends: '2027-09-30', issued: '2026-09-28T10:00:00Z', key: 'k', sha256: 's',
};
const EVALUATION =
  'Unlicensed evaluation of installation I-3f2a9c1b7d4e5a60: at most 2 pairs (1 used), no runs under SSO, no' +
  ' sign-off or evidence packs; not licensed for production use (ds licence)';

function standing(fields: Partial<Licence>): Licence {
  return { installation_id: 'I-3f2a9c1b7d4e5a60', state: 'valid', warnings: [], licence: PRO, renewal: null,
    allows: ALL, pairs: null, clock_behind_seconds: null, installed: [], ...fields };
}

test('a control the licence refuses is disabled with its first warning as the reason', () => {
  const evaluation = standing({ state: 'evaluation', licence: null, warnings: [EVALUATION], allows: { ...ALL, sso: false, signoff: false, packs: false } });
  assert.equal(refusal(evaluation, 'runs'), undefined); // an evaluation runs its two pairs
  assert.equal(refusal(evaluation, 'signoff'), EVALUATION);
  assert.equal(refusal(evaluation, 'packs'), EVALUATION);
  const expired = 'licence LI-01JB8Q7ZK3M9W2XR5T4VNH6D0C ended on 2027-09-30 and its 30 days of grace on 2027-10-30: …';
  const ended = standing({ state: 'expired', warnings: [expired], allows: { runs: false, sso: false, signoff: false, packs: false } });
  assert.deepEqual([refusal(ended, 'runs'), refusal(ended, 'packs')], [expired, expired]);
  const quiet = standing({ allows: { ...ALL, signoff: false, packs: false } }); // Pro in tokens mode warns of nothing
  assert.equal(refusal(quiet, 'signoff'), 'The licence does not allow four-eyes sign-off: see the Licence screen.');
  assert.equal(refusal(quiet, 'packs'), 'The licence does not allow evidence packs: see the Licence screen.');
});

test('nothing is disabled while the licence is unknown or not loaded: the server decides', () => {
  const unknown = standing({ state: 'unknown', licence: null, allows: null, warnings: ['licence status unavailable (PoolTimeout): see the server log'] });
  assert.equal(refusal(unknown, 'runs'), undefined);
  assert.equal(refusal(undefined, 'packs'), undefined);
});

test('an open build needs no licence: its screens say so, disable nothing and show no banner', () => {
  // GET /v1/licence in a build that embeds no vendor key.
  const open = standing({ state: 'open', licence: null, pairs: { limit: null, since: null, counted: [] } });
  assert.equal(namedLine(open), 'This build needs no licence and limits nothing.');
  assert.equal(
    installLine(open),
    'Any number of pairs, four-eyes sign-off, evidence packs and runs under SSO work without a licence.' +
      ' ds licence install refuses every file: this build embeds no vendor key to verify one with.',
  );
  assert.match(installLine(standing({})), /^A licence file is installed on the host with ds licence install, /);
  for (const feature of ['runs', 'signoff', 'packs'] as const) assert.equal(refusal(open, feature), undefined);
  assert.equal(allowsLine(open.allows!), 'runs yes, sso yes, signoff yes, packs yes');
  // Every screen prints one banner per warning, and an open build's standing holds none.
  assert.match(source('main.tsx'), /licence\?\.warnings\.map\(\(line\) => \(\n\s*<p key=\{line\} className="banner"/);
  assert.deepEqual(open.warnings, []);
  // The Licence screen: the id, that line and what is allowed; no pair is counted, so no Pairs section, and
  // only a licence an earlier vendor build stored is listed.
  const screen = source('Licence.tsx');
  for (const printed of ['{l.installation_id ??', '<p>{namedLine(l)}</p>', '{allowsLine(l.allows)}', '{installLine(l)}'])
    assert.equal(screen.includes(printed), true, printed);
  assert.match(screen, /const open = l\?\.state === 'open';/);
  assert.match(screen, /\{l\.pairs && !open && \(/);
  assert.match(screen, /\{\(!open \|\| l\.installed\.length > 0\) && \(/);
  assert.match(source('ui.tsx'), /\bopen: 'ok'/); // its badge is no warning's
  // A run an open build made carries no licence record, so no label and a plain report name.
  const run = { run_id: 'R-01JB8Q7ZK3M9W2XR5T4VNH6D0C', licence: null } as Run;
  assert.equal(reportFile(run, 'report.pdf'), 'R-01JB8Q7ZK3M9W2XR5T4VNH6D0C-report.pdf');
});

test('a soak paused for the licence says at the end of which cycle, as ds runs does', () => {
  const run = (fields: Partial<Run>) => ({ status: 'paused', stop_reason: LICENCE_PAUSE, cycle: 5, ...fields }) as Run;
  assert.equal(pausedLine(run({})), 'Paused for the licence at the end of cycle 4');
  assert.equal(pausedLine(run({ stop_reason: 'run_paused_approval' })), undefined);
  assert.equal(pausedLine(run({ status: 'cancelled' })), undefined);
});

test("a run's reports are saved under the name the API and ds report give them", () => {
  const run = (state: string | null) => ({
    run_id: 'R-01JB8Q7ZK3M9W2XR5T4VNH6D0C',
    licence: state && { licence_id: null, tier: null, state, ends: null, label: 'l' },
  }) as Run;
  assert.equal(reportFile(run('evaluation'), 'discrepancies.csv'),
    'R-01JB8Q7ZK3M9W2XR5T4VNH6D0C-unlicensed-evaluation-discrepancies.csv');
  assert.equal(reportFile(run('valid'), 'report.pdf'), 'R-01JB8Q7ZK3M9W2XR5T4VNH6D0C-report.pdf');
  assert.equal(reportFile(run('grace'), 'retry.csv'), 'R-01JB8Q7ZK3M9W2XR5T4VNH6D0C-retry.csv');
  assert.equal(reportFile(run(null), 'report.pdf'), 'R-01JB8Q7ZK3M9W2XR5T4VNH6D0C-report.pdf'); // before 010, or open
});

test('an unreadable standing is unknown, not an unlicensed evaluation', () => {
  assert.equal(namedLine(standing({ state: 'evaluation', licence: null })), 'No licence: an unlicensed evaluation.');
  assert.equal(
    namedLine(standing({ state: 'unknown', licence: null, allows: null })),
    'Licence unknown: the standing could not be read (see the server log).',
  );
  assert.equal(namedLine(standing({})).split(': ')[0], 'Licence LI-01JB8Q7ZK3M9W2XR5T4VNH6D0C');
});

test('a licence, what it allows and its pairs read as ds licence prints them', () => {
  assert.equal(
    licenceLine(PRO),
    'LI-01JB8Q7ZK3M9W2XR5T4VNH6D0C: Example Bank plc (order ORD-2026-014), pro, 5 pairs, 2026-10-01 to 2027-09-30',
  );
  assert.equal(licenceLine({ ...PRO, tier: 'enterprise', pairs: null }).split(', ')[2], 'unlimited pairs');
  assert.equal(licenceLine({ ...PRO, pairs: 1 }).split(', ')[2], '1 pair');
  assert.equal(allowsLine({ ...ALL, sso: false, packs: false }), 'runs yes, sso no, signoff yes, packs no');
  const counted = [{ name: 'accounts', first_at: '2026-10-01T08:00:00+00:00', seated: true, targets: ['svc-a vs svc-b'] }];
  assert.equal(pairsLine({ limit: 5, since: '2026-10-01', counted }), '5 pairs seated; 1 counted from the runs since 2026-10-01.');
  assert.equal(pairsLine({ limit: 2, since: null, counted: [] }), '2 pairs seated; 0 counted from every run.');
  assert.equal(pairsLine({ limit: null, since: '2026-10-01', counted }), 'Unlimited pairs; 1 counted from the runs since 2026-10-01.');
});

test('the licence is read with GET /v1/licence', async () => {
  const urls: string[] = [];
  globalThis.fetch = (async (input: string) => {
    urls.push(input);
    return new Response(JSON.stringify(standing({})), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }) as typeof fetch;
  assert.equal((await api.licence()).licence?.licence_id, PRO.licence_id);
  assert.deepEqual(urls, ['/v1/licence']);
});
