// Run with: npm test (Node 24 runs TypeScript directly; no test framework is needed).
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { test } from 'node:test';

import { ApiError, LOGIN_EVENT, api, isRaw, parseExact, watchUse } from '../src/api.ts';
import { NOT_KEPT } from '../src/signin.ts';

test('numbers a double would change keep their exact text', () => {
  const doc = parseExact('{"big": 9007199254740993, "long": 0.10000000000000001, "one": 1.0, "e": 1e5}') as Record<
    string,
    unknown
  >;
  for (const [name, text] of [['big', '9007199254740993'], ['long', '0.10000000000000001'], ['one', '1.0'], ['e', '1e5']]) {
    const value = doc[name];
    assert.ok(isRaw(value), name);
    assert.equal(value.rawJSON, text);
  }
});

test('ordinary numbers stay numbers and strings are never touched', () => {
  const text = '{"n": 42, "f": 0.5, "neg": -3, "s": "9007199254740993", "q": "a \\"1e5\\" b", "k": {"12345678901234567890": [true, null]}}';
  assert.deepEqual(parseExact(text), JSON.parse(text));
});

test('invalid JSON is rejected, as JSON.parse rejects it', () => {
  assert.throws(() => parseExact('{"a": 01}'), SyntaxError);
});

// -- sign-in and requests ---------------------------------------------------------------------------

type Sent = { url: string; method: string; headers: Headers };
type Answer = { status: number; body: unknown };
const sent: Sent[] = [];
let answer: Answer = { status: 200, body: {} };
let first: Answer[] = []; // answers given before `answer`, one a request

globalThis.fetch = (async (input: string, init: RequestInit = {}) => {
  sent.push({ url: input, method: init.method ?? 'GET', headers: new Headers(init.headers) });
  const { status, body } = first.shift() ?? answer;
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}) as typeof fetch;

const ME = { actor: 'user:U-0123456789abcdef', name: 'jdoe', display_name: null, kind: 'user', roles: ['viewer'],
  csrf: 'csrf-of-the-session', mode: 'oidc', idle_minutes: 30, expires_at: null };

/** The requests `calls` makes, answered 200 with `body`. */
async function requests(calls: () => Promise<unknown>, body: unknown = {}): Promise<Sent[]> {
  answer = { status: 200, body };
  sent.length = 0;
  await calls();
  return [...sent];
}

test('X-CSRF-Token comes from /me and goes on non-GET requests only; no request carries Authorization', async () => {
  await requests(api.me, ME);
  const made = await requests(async () => {
    await api.runs();
    await api.tokens();
    await api.audit({ actor: 'user:U-0123456789abcdef' });
    await api.createRun({ spec_id: 1, mode: 'batch' });
    await api.createToken({ name: 'ci-nightly', owner: 'data team', role: 'operator' });
    await api.revokeToken('T-0123456789abcdef');
    await api.disableUser('U-0123456789abcdef', true);
    await api.revokeSession('W-0123456789abcdef');
    await api.saveSpec('name: x');
  });
  assert.deepEqual(
    made.map((r) => [r.method, r.headers.get('X-CSRF-Token')]),
    [['GET', null], ['GET', null], ['GET', null], ['POST', ME.csrf], ['POST', ME.csrf], ['DELETE', ME.csrf],
      ['POST', ME.csrf], ['DELETE', ME.csrf], ['POST', ME.csrf]],
  );
  assert.ok(made.every((r) => !r.headers.has('Authorization')));
});

test('token sign-in keeps its CSRF token and sign-out forgets it', async () => {
  const [login] = await requests(() => api.tokenLogin('dst_x'), { ...ME, csrf: 'from-token-login' });
  assert.equal(login.url, '/v1/auth/token-login');
  assert.ok(!login.headers.has('Authorization'));
  const [post] = await requests(() => api.control('R-1', 'pause'));
  assert.equal(post.headers.get('X-CSRF-Token'), 'from-token-login');
  const [logout] = await requests(api.logout, { logout_url: null });
  assert.equal(logout.headers.get('X-CSRF-Token'), 'from-token-login');
  const [after] = await requests(() => api.control('R-1', 'pause'));
  assert.equal(after.headers.get('X-CSRF-Token'), null);
});

/** The requests a token sign-in makes when they are answered with `answers`, in order, and how it ends. */
async function signIn(...answers: Answer[]): Promise<{ made: string[]; me?: Awaited<ReturnType<typeof api.me>>; error?: unknown }> {
  first = answers;
  sent.length = 0;
  const ended = await api.tokenLogin('dst_x').then((me) => ({ me }), (error: unknown) => ({ error }));
  first = [];
  return { made: sent.map((r) => `${r.method} ${r.url}`), ...ended };
}

test('a token sign-in reads /me: its CSRF token, or that the browser did not keep the session cookie', async () => {
  globalThis.dispatchEvent = () => true;
  const accepted = { status: 200, body: { ...ME, kind: 'token', csrf: 'of-the-post' } };
  const kept = await signIn(accepted, { status: 200, body: { ...ME, kind: 'token', csrf: 'of-me' } });
  assert.deepEqual(kept.made, ['POST /v1/auth/token-login', 'GET /v1/auth/me']);
  assert.equal(kept.me?.csrf, 'of-me');
  const [post] = await requests(() => api.control('R-1', 'pause'));
  assert.equal(post.headers.get('X-CSRF-Token'), 'of-me');

  const dropped = await signIn(accepted, { status: 401, body: { detail: 'not signed in' } });
  assert.deepEqual(dropped.made, ['POST /v1/auth/token-login', 'GET /v1/auth/me']);
  assert.ok(dropped.error instanceof ApiError && dropped.error.status === 401);
  assert.equal(dropped.error.message, NOT_KEPT);
  assert.deepEqual(dropped.error.body, { detail: 'not signed in' });

  const ended = await signIn(accepted, { status: 401, body: { detail: 'session expired' } }); // another 401 is itself
  assert.ok(ended.error instanceof ApiError && ended.error.message === 'session expired');

  for (const [status, detail] of [[403, 'cross-origin request refused'], [401, 'invalid or expired API token'], [401, 'not signed in']] as const) {
    const refused = await signIn({ status, body: { detail } }); // a refused POST keeps the server's words, and asks no more
    assert.deepEqual(refused.made, ['POST /v1/auth/token-login'], detail);
    assert.ok(refused.error instanceof ApiError && refused.error.status === status && refused.error.message === detail, detail);
  }
});

test('X-DS-Background: 1 goes on requests after 60 s without pointerdown, keydown or wheel', async (t) => {
  t.mock.timers.enable({ apis: ['Date'], now: 1_000_000 });
  const page = new EventTarget();
  watchUse(page);
  const background = async () => (await requests(() => api.run('R-1')))[0].headers.get('X-DS-Background');
  for (const type of ['pointerdown', 'keydown', 'wheel']) {
    page.dispatchEvent(new Event(type));
    assert.equal(await background(), null, type);
    t.mock.timers.tick(60_000);
    assert.equal(await background(), null, type);
    t.mock.timers.tick(1);
    assert.equal(await background(), '1', type);
  }
});

test('a 401 dispatches the login event', async () => {
  const events: string[] = [];
  globalThis.dispatchEvent = (e: Event) => events.push(e.type) > 0;
  answer = { status: 401, body: { detail: 'session expired' } };
  await assert.rejects(api.runs(), (e: unknown) => e instanceof ApiError && e.status === 401 && e.message === 'session expired');
  assert.deepEqual(events, [LOGIN_EVENT]);
  answer = { status: 403, body: { detail: 'needs the operator role' } };
  await assert.rejects(api.createRun({ spec_id: 1, mode: 'batch' }), /needs the operator role/);
  assert.deepEqual(events, [LOGIN_EVENT]);
});

test('a 410 of purged evidence rejects with the server message (what the screens show), without a sign-in', async () => {
  const events: string[] = [];
  globalThis.dispatchEvent = (e: Event) => events.push(e.type) > 0;
  const detail = 'cycle 1 of run R-1 was purged on 2026-12-26 (retention standard): its keys, results, findings and evidence are deleted';
  answer = { status: 410, body: { detail } };
  const reads = [
    () => api.results('R-1', {}),
    () => api.key('R-1', 'tok:identifier:v1:x'),
    () => api.report('R-1', 'report.pdf'),
    () => api.compare(1, 'R-1', 'tok:identifier:v1:x'),
    () => api.newChat('R-1'),
  ];
  for (const read of reads) await assert.rejects(read(), (e: unknown) => e instanceof ApiError && e.status === 410 && e.message === detail);
  assert.deepEqual(events, []);
});

test("the draft form shows each side's unmatched paths, as POST /v1/profile answers them", () => {
  const form = readFileSync(new URL('../src/Specs.tsx', import.meta.url), 'utf8');
  assert.ok(form.includes("unmatched[{side}]: {draft.unmatched[side].length ? <code>{draft.unmatched[side].join(', ')}</code> : 'none'}"));
  assert.match(form, /\(\['a', 'b'\] as const\)\.map\(\(side\) => \(/);
});

test('no source writes HTML or uses localStorage', () => {
  const src = new URL('../src/', import.meta.url);
  const files = readdirSync(src, { recursive: true, encoding: 'utf8' }).filter((f) => /\.(ts|tsx)$/.test(f));
  assert.ok(files.includes('api.ts') && files.includes('Admin.tsx'));
  for (const file of files) {
    const text = readFileSync(new URL(file, src), 'utf8');
    for (const banned of ['dangerouslySetInnerHTML', 'innerHTML', 'localStorage']) assert.ok(!text.includes(banned), `${file}: ${banned}`);
  }
});
