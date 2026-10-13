// What the Pre-checks screens build besides the API's texts: the body a form sends, a report's file name, the
// pairs counted by status and the filter's verdicts in the API's words; that no screen words a pair, a total or a
// banner, shows a tick or paints a verdict; and what the screens ask of /v1/prechecks.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { api, type PrecheckDetail, type Prechecks, type PrecheckVerdict, type TablePrecheck } from '../src/api.ts';
import {
  CATALOGUE_LEVELS, EVALUATION, LEVELS, REPORTS, blank, blankSchemas, bodyOf, catalogueOf, codeWords, controlLabel,
  draftFile, draftable, ended, filterOptions, guardBanner, guardLines, listed, progressOf, reportFile,
} from '../src/prechecking.ts';

const source = (file: string) => readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');
const VERDICTS: PrecheckVerdict[] = [
  'unpaired', 'refused', 'not_read', 'rows_differ', 'keys_null_or_repeated', 'columns_not_compared', 'values_differ',
  'incomplete', 'schema_differs', 'no_difference_observed',
];
const WORDED = Object.fromEntries(VERDICTS.map((verdict) => [verdict, verdict.replaceAll('_', ' ')])) as Record<PrecheckVerdict, string>;
WORDED.no_difference_observed = 'no difference observed in the checks made (not parity)'; // as the API words it
const PC: TablePrecheck = {
  precheck_id: 'PC-01K6GZ3M0V8Q4T2N7R5W9XYABC', source: 'specs', level: 'columns', pairs: 12, skipped: 0, evaluation: false,
  created_by: 'user:U-1', created_by_name: 'jdoe', created_at: '2026-10-07T10:02:11+00:00', cancelled_by: null,
  cancelled_by_name: null, cancelled_at: null, finished_at: null, progress: { done: 7, running: 2, pending: 3 },
  verdicts: { rows_differ: 2, refused: 1, no_difference_observed: 4 },
  totals: '12 pairs: 1 a run would refuse, 2 with differing row counts, 4 with no difference observed (not parity); 5 pending',
};

test('a form sends the versions chosen or every stored spec of two tables, never both and never a name', () => {
  assert.deepEqual(blank(), { every: false, spec_ids: [], level: 'columns' });
  assert.deepEqual(bodyOf(blank('14')), { level: 'columns', spec_ids: [14] }); // opened from the Specs screen
  assert.deepEqual(bodyOf({ every: false, spec_ids: ['12', '14'], level: 'distinct' }), { level: 'distinct', spec_ids: [12, 14] });
  assert.deepEqual(bodyOf({ every: true, spec_ids: ['12'], level: 'schema' }), { level: 'schema', all_specs: true });
  assert.deepEqual(LEVELS, ['schema', 'counts', 'columns', 'distinct']); // each includes the one before
});

test('the form of two schemas sends each side\'s target, its schema when given, the globs given and a level', () => {
  assert.deepEqual(catalogueOf({ ...blankSchemas(), a: 'crm-legacy', b: 'platform' }), {
    a: { target: 'crm-legacy' }, b: { target: 'platform' }, level: 'counts', // each target's default schema
  });
  assert.deepEqual(
    catalogueOf({ a: 'crm', schema_a: ' APP ', b: 'pg', schema_b: '', include: 'customer*, acct_*', exclude: ' tmp_* ', level: 'schema' }),
    { a: { target: 'crm', schema: 'APP' }, b: { target: 'pg' }, level: 'schema', include: ['customer*', 'acct_*'], exclude: ['tmp_*'] },
  );
  assert.deepEqual(CATALOGUE_LEVELS, ['schema', 'counts', 'columns']); // distinct counts need a stored spec
});

test('a pair is drafted only when it is two tables paired by name and read, saved under the CLI\'s name', () => {
  const read = { lines: [], refused: [], schema: [], skipped: [], format: 1, level: 'counts' as const, names: { a: { schema: 'APP', name: 'T' }, b: { schema: 'public', name: 't' } } };
  assert.equal(draftable({ spec_id: null, relations: ['T', 't'], result: read }), true);
  assert.equal(draftable({ spec_id: 12, relations: ['t', 't'], result: read }), false); // a stored spec needs none
  assert.equal(draftable({ spec_id: null, relations: ['T', null], result: null }), false); // unpaired
  assert.equal(draftable({ spec_id: null, relations: ['T', 't'], result: { ...read, names: undefined } }), false); // not read
  assert.equal(draftFile(PC.precheck_id, 7), 'PC-01K6GZ3M0V8Q4T2N7R5W9XYABC-pair-7.draft.yaml');
});

test('a report is saved under the server\'s name, which says so of an unlicensed evaluation', () => {
  assert.deepEqual(REPORTS, ['precheck.csv', 'precheck-columns.csv', 'precheck.pdf']);
  assert.equal(reportFile(PC, 'precheck.pdf'), 'PC-01K6GZ3M0V8Q4T2N7R5W9XYABC-precheck.pdf');
  assert.equal(reportFile({ ...PC, evaluation: true }, 'precheck.csv'), 'PC-01K6GZ3M0V8Q4T2N7R5W9XYABC-unlicensed-evaluation-precheck.csv');
  assert.equal(EVALUATION, 'Unlicensed evaluation');
});

test('progress is counts by status and the filter offers the verdicts met, worst first, in the API\'s words', () => {
  assert.deepEqual(progressOf(PC), [['pending', 3], ['running', 2], ['done', 7]]);
  assert.deepEqual(progressOf({ progress: { cancelled: 2, done: 1 } }), [['done', 1], ['cancelled', 2]]);
  assert.deepEqual([ended(PC), ended({ ...PC, finished_at: 'x' }), ended({ ...PC, finished_at: 'x', cancelled_at: 'x' })],
    ['not finished', 'finished', 'cancelled']);
  assert.deepEqual(filterOptions({ verdicts: PC.verdicts, worded: WORDED }), [
    { verdict: 'refused', label: 'refused (1)' },
    { verdict: 'rows_differ', label: 'rows differ (2)' },
    { verdict: 'no_difference_observed', label: 'no difference observed in the checks made (not parity) (4)' },
  ]);
  assert.equal(codeWords('class_differs'), 'class differs');
});

test('the list is read over its pages', async () => {
  const page = (id: string, next_cursor: string | null): Prechecks => (
    { not: 'what it is not', banners: id === 'a' ? ['a banner'] : [], next_cursor, items: [{ ...PC, precheck_id: id }] });
  const pages: Record<string, Prechecks> = { '': page('a', '1'), 1: page('b', '2'), 2: page('c', null) };
  const asked: (string | undefined)[] = [];
  const load = async (cursor?: string) => (asked.push(cursor), structuredClone(pages[cursor ?? '']));
  const found = await listed(load, 2);
  assert.deepEqual([asked, found.items.map((p) => p.precheck_id), found.banners, found.next_cursor],
    [[undefined, '1'], ['a', 'b'], ['a banner'], '2']);
  assert.equal((await listed(load, 9)).items.length, 3); // it stops where the list ends
});

test('no screen builds a text, shows a tick or paints a verdict: they print the server\'s words', () => {
  // The words of a pair's lines, the totals and the banner, and what a pre-check never says.
  const worded = /rows differ|not checked|not compared|would be refused|not parity|no difference observed|differ in a figure|read at one moment|no worker has taken|\bpairs?: |fewer on side|not approved/i;
  const never = /\b(match|matches|matched|identical|passed|verified|validated|reconciled)\b|[✓✔☑]|%|percent|\bscore\b/i;
  for (const file of ['prechecking.ts', 'Prechecks.tsx']) {
    assert.equal(worded.test(source(file)), false, file);
    assert.equal(never.test(source(file)), false, file);
  }
  const screens = source('Prechecks.tsx');
  assert.equal(/<Badge|className="(ok|bad)"|green/.test(screens), false); // a verdict is words, never a colour
  // What a pre-check is not heads both screens, and the label of an unlicensed evaluation stands under it.
  assert.equal(screens.match(/<p role="note">\{d\.not\}<\/p>/g)?.length, 2);
  assert.match(screens, /\{d\.not\}<\/p>\n\s*\{d\.evaluation && <p className="warn">\{EVALUATION\}<\/p>\}/);
  for (const printed of ['{d.totals}', '{p.text}', 'd?.worded[p.verdict]', 'r.lines.map', 'r.refused.map', 'c.lines.map', 'p.totals', 'd.banners.map'])
    assert.equal(screens.includes(printed), true, printed);
  // Operators make and cancel one and start a comparison; the link stands after Schedules, for every role.
  assert.match(screens, /can\('operator'\) && <PrecheckForm/);
  assert.match(screens, /can\('operator'\) && d\.finished_at === null && <button onClick=\{\(\) => act\(api\.cancelPrecheck\(id\)\)\}>Cancel/);
  assert.match(screens, /can\('operator'\) && p\.spec_id !== null && \(\n\s*<button onClick=\{\(\) => compare\(p\)\}.*>Run a comparison</);
  // Spec authors pair two schemas' tables by name and draft a spec of such a pair.
  assert.match(screens, /can\('spec_author'\) && <CatalogueForm \/>/);
  assert.match(screens, /can\('spec_author'\) && draftable\(p\) && <button onClick=\{draft\}>Draft a spec<\/button>/);
  assert.match(source('main.tsx'), /<a href="#\/schedules">Schedules<\/a>\n\s*<a href="#\/prechecks">Pre-checks<\/a>/);
  assert.equal(/prechecks|pre-check/i.test(source('Readiness.tsx') + source('ready.ts')), false); // never there
  assert.match(source('Specs.tsx'), /can\('operator'\)\) cols\.push\(\['Table pre-check', \(s\) => <a href=\{href\(\['prechecks'\], \{ spec_id: s\.spec_id \}\)\}>Pre-check…<\/a>\]\)/);
});

test('the screens read and act through /v1/prechecks', async () => {
  const calls: [string, string, string | undefined][] = [];
  globalThis.fetch = (async (input: string, init?: RequestInit) => {
    calls.push([init?.method ?? 'GET', input, init?.body as string | undefined]);
    return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
  }) as typeof fetch;
  const id = PC.precheck_id;
  await api.prechecks({ cursor: undefined });
  await api.precheck(id, { verdict: 'rows_differ', limit: 500 });
  await api.precheck(id, { verdict: '', limit: 500 });
  await api.precheckPair(id, 7);
  await api.createPrecheck(bodyOf(blank('14')));
  await api.createPrecheck(bodyOf({ every: true, spec_ids: [], level: 'counts' }));
  await api.cancelPrecheck(id);
  await api.precheckReport(id, 'precheck-columns.csv');
  await api.createCatalogue(catalogueOf({ ...blankSchemas(), a: 'crm', b: 'pg', exclude: 'tmp_*' }));
  await api.precheckDraft(id, 7);
  assert.deepEqual(calls, [
    ['GET', '/v1/prechecks', undefined],
    ['GET', `/v1/prechecks/${id}?verdict=rows_differ&limit=500`, undefined],
    ['GET', `/v1/prechecks/${id}?limit=500`, undefined],
    ['GET', `/v1/prechecks/${id}/pairs/7`, undefined],
    ['POST', '/v1/prechecks', '{"level":"columns","spec_ids":[14]}'],
    ['POST', '/v1/prechecks', '{"level":"counts","all_specs":true}'],
    ['POST', `/v1/prechecks/${id}/cancel`, '{}'],
    ['GET', `/v1/prechecks/${id}/reports/precheck-columns.csv`, undefined],
    ['POST', '/v1/prechecks/catalogue', '{"a":{"target":"crm"},"b":{"target":"pg"},"level":"counts","exclude":["tmp_*"]}'],
    ['GET', `/v1/prechecks/${id}/pairs/7/draft.yaml`, undefined],
  ]);
  const detail: Pick<PrecheckDetail, 'worded'> = { worded: WORDED };
  assert.equal(Object.keys(detail.worded).length, 10);
});

test('the Run screen prints the schema guard\'s lines as the API words them, a banner while it holds the run', () => {
  const paused = 'Paused by the schema guard at the start of cycle 7: side b: column \'email\' of public.customers is gone, or the role may no longer read it. Put it back and resume the run, or cancel it and start a new run, which plans the table as it is now.';
  const gained = 'side b gained column \'loyalty_tier\' since this run was planned: this run does not compare it';
  const held = { paused: true, accepts: false, lines: [paused, gained] };
  assert.equal(guardBanner(held), paused);
  assert.deepEqual(guardLines(held), [gained]);
  const running = { paused: false, accepts: false, lines: [gained] };
  assert.equal(guardBanner(running), undefined);
  assert.deepEqual(guardLines(running), [gained]);
  assert.deepEqual(guardLines({ paused: false, lines: [] }), []); // a run without a table side: nothing
  // A resume that would accept the changes that stand says so; any other control is its action's name.
  assert.equal(controlLabel('resume', { accepts: true }), 'Resume (accept the changes)');
  assert.equal(controlLabel('resume', { accepts: false }), 'resume');
  assert.equal(controlLabel('cancel', { accepts: true }), 'cancel');
  // No screen builds one of its texts, says a schema is unchanged, or shows a tick.
  const screen = source('Run.tsx');
  assert.match(screen, /guardBanner\(run\.schema\)/);
  assert.match(screen, /guardLines\(run\.schema\)\.map/);
  assert.match(screen, /controlLabel\(a, run\.schema\)/);
  for (const file of ['Run.tsx', 'prechecking.ts']) {
    assert.doesNotMatch(source(file), /schema guard (at|before|could)|Paused by the schema guard|no change observed|unchanged|[✓✔]/);
  }
});
