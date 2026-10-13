// What the Run screen says about an AI draft, and what it sends for one (docs/mvp/CONTRACTS.md §6, "Fixed
// questions"): the facts beside a draft are the engine's stored ones, never the answer's.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { api, type Message, type Run } from '../src/api.ts';
import { AI_OFF, CITATION, NO_SUMMARY, answerOf, citedKeys, draftTitle, firstOnly, off, refused, runFacts } from '../src/explain.ts';

const RC = 'RC-4b68f1b25392';
const KEY = 'tok:identifier:v1:KB7XB764PVDAZMIHUMMHBVQY33';
const tool = (name: string, data: unknown): Message => ({ role: 'tool', content: JSON.stringify({ tool: name, data }) });

test('a cited finding or observation leads to its key, as the tool results name it', () => {
  const messages: Message[] = [
    { role: 'user', content: `Explain root cause ${RC} of this run` },
    tool('get_run_summary', { status: 'completed', root_causes: [{ root_cause_id: RC, sample_keys: [KEY] }] }),
    tool('get_findings', { findings: [{ finding_id: 'F-1', key_masked: KEY }, { finding_id: 'F-2', key_masked: 'tok:other' }], more: false }),
    tool('get_observations', {
      key_masked: KEY,
      side_a: { obs_id: 'O-a' },
      findings: [{ finding_id: 'F-3' }],
      observations: [{ obs_id: 'O-a', side: 'a' }, { obs_id: 'O-b', side: 'b' }],
    }),
    { role: 'tool', content: JSON.stringify({ tool: 'get_observations', error: 'no key tok:none in run R-1' }) },
    { role: 'assistant', content: `[${RC}] [F-1] [O-b]`, citations: [RC, 'F-1', 'O-b'] },
  ];
  assert.deepEqual(citedKeys(messages), { 'F-1': KEY, 'F-2': 'tok:other', 'F-3': KEY, 'O-a': KEY, 'O-b': KEY });
  assert.deepEqual(citedKeys([]), {});
});

test('a chip is a citation as the validator reads one: a bracket with a line break inside is none', () => {
  const cited = (text: string) => [...text.matchAll(CITATION)].map((m) => m[1]);
  assert.deepEqual(cited(`One key differs [${RC}], such as [K:${KEY}] [F-1] [O-b].`), [RC, `K:${KEY}`, 'F-1', 'O-b']);
  // The server stored this answer as verified by [RC-…] alone: it never checked the other two brackets.
  const broken = `The total differs in 1 key [${RC}]; the same holds for [K:tok:identifier:ffffffffffff\n] and [K:every other key of the run\n].`;
  assert.deepEqual(cited(broken), [RC]);
});

test('a stored answer that cites nothing is a refusal, and the answer is the last assistant message', () => {
  const abstained = { role: 'assistant', content: 'No verified answer: …', citations: [] };
  const answered = { role: 'assistant', content: `One key differs [${RC}].`, citations: [RC], created_at: '2026-10-07T09:00:00+00:00' };
  assert.ok(refused(abstained) && !refused(answered));
  assert.equal(answerOf([{ role: 'user', content: 'q' }, abstained, tool('get_findings', {}), answered]), answered);
  assert.equal(answerOf([{ role: 'user', content: 'q' }]), undefined); // the turn never stored one: no draft
  assert.equal(draftTitle(RC), `AI draft: root cause ${RC}`);
  assert.equal(draftTitle(null), 'AI draft: run summary');
});

test("a run's facts are its stored counters, not its progress, which leaves out a scan's identical keys", () => {
  const by_outcome = { MATCH: 985, MISMATCH: 4, KNOWN_DIFF: 0, NOT_FOUND_A: 1, NOT_FOUND_B: 11, INCONCLUSIVE: 0 };
  const counters = { total: 1001, match: 985, different: 16, different_provisional: 0, indeterminate: 0, cancelled: 0,
    outstanding: 0, by_outcome, compared: 1001, mismatch_rate: '0.015984', cycle: 1 };
  const scanned = {
    status: 'completed',
    counters,
    scan: { done: true, rows_a: 1000, rows_b: 990, skipped_a: 0, skipped_b: 0, matched: 985, candidates: 16 },
    progress: { keys: { done: 16 }, outcomes: { MISMATCH: 4, NOT_FOUND_A: 1, NOT_FOUND_B: 11 } },
  } as unknown as Run;
  assert.equal(
    runFacts(scanned, 3),
    'status completed · cycle 1 · total 1,001 · match 985 · different 16 · indeterminate 0 · outstanding 0 · cancelled 0' +
      ' · MATCH 985 · MISMATCH 4 · NOT_FOUND_A 1 · NOT_FOUND_B 11 · 3 root causes',
  );
  // a run whose first cycle has not ended holds no counters yet
  assert.equal(runFacts({ status: 'running', counters: {} }, 1), 'status running · 1 root cause');
});

test('a summary of more than ten root causes says that it read the first ten', () => {
  assert.equal(firstOnly(10), undefined);
  assert.equal(firstOnly(11), 'This draft read the first 10 of 11 root causes.');
  assert.equal(firstOnly(1204), 'This draft read the first 10 of 1,204 root causes.');
});

test('no draft is asked while the AI is off, of an older cycle, or without a root cause', () => {
  const run = { cycle: 3 };
  assert.equal(off(false, run, 2, 0), AI_OFF);
  assert.equal(AI_OFF, 'AI is off: no LLM profile is configured (DEPLOY.md, section 8).');
  assert.equal(off(true, run, 2, 4), 'AI drafts read the current cycle (cycle 3) only.');
  assert.equal(off(true, run, 3, 0), NO_SUMMARY);
  assert.equal(
    NO_SUMMARY,
    'No AI summary: a draft must cite a root cause, and this cycle has none (they are written when a cycle ends).',
  );
  // health not loaded leaves the buttons on (the server then answers), as do root causes not loaded yet
  assert.equal(off(undefined, run, 3, 4), undefined);
  assert.equal(off(true, run, 3), undefined);
});

test('one request starts the session and asks: a root cause to explain, none to summarise', async () => {
  const sent: { url: string; body: unknown }[] = [];
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    sent.push({ url, body: JSON.parse(String(init.body)) });
    return new Response(JSON.stringify({ session_id: 'S-1', messages: [] }), { status: 201 });
  }) as typeof fetch;
  assert.equal((await api.explain('R-1', RC)).session_id, 'S-1');
  await api.explain('R-1');
  assert.deepEqual(sent, [
    { url: '/v1/chat/sessions/explain', body: { run_id: 'R-1', root_cause_id: RC } },
    { url: '/v1/chat/sessions/explain', body: { run_id: 'R-1' } },
  ]);
});
