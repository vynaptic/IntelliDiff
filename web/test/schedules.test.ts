// What the Schedules screens build besides the API's texts: the cron a preset writes, the body a form sends, the
// zone a new schedule starts with and the labels of `ds schedules`; and that no screen words a firing
// (docs/mvp/SCHEDULES.md §9, §10).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { api, type Schedule, type Schedules } from '../src/api.ts';
import {
  HOURS, NOT_A_FORECAST, PRESETS, WEEKDAYS, blank, bodyOf, bySpec, cronOf, defaultZone, draftOf, dueLine, gateLine, listed,
  movedTo, specLabel,
} from '../src/scheduling.ts';

const SPECS = [{ spec_id: 12, name: 'customer-profile' }, { spec_id: 14, name: 'customer-profile' }, { spec_id: 15, name: 'cards' }];
const NIGHTLY: Schedule = {
  schedule_id: 'SC-01K6GZ3M0V8Q4T2N7R5W9XYABC', name: 'nightly-customers', spec_name: 'customer-profile', spec_id: 12,
  runs: { spec_id: 12, version: '1.4.0' }, newer: { spec_id: 14, version: '1.5.0' }, run: { mode: 'batch' },
  cron: '0 2 * * *', time_zone: 'Europe/London', start_within: 60, ends_at: null, state: 'active', auth_mode: 'oidc',
  revision: 4, created_by: 'user:U-1', created_by_name: 'jdoe', created_at: '2026-10-01T09:00:00+00:00', set_by: 'user:U-1',
  set_by_name: 'jdoe', set_at: '2026-10-01T09:00:00+00:00', paused_by: null, paused_by_name: null, paused_at: null,
  next_due: { utc: '2026-10-02T01:00Z', local: '2026-10-02 02:00' }, overdue: false, clock_behind: false, last: null, held: null,
};

test('a preset only writes cron text', () => {
  const at = (preset: keyof typeof PRESETS, fields: object = {}) => cronOf({ ...blank(), preset, time: '02:05', ...fields });
  assert.equal(at('daily'), '5 2 * * *');
  assert.equal(at('weekdays'), '5 2 * * 1-5');
  assert.equal(at('weekly', { weekday: '0' }), '5 2 * * 0');
  assert.equal(at('monthly', { day: '28', time: '23:30' }), '30 23 28 * *');
  assert.equal(at('hours', { hours: '6' }), '0 */6 * * *');
  assert.equal(at('custom', { cron: '15 1 * * MON-FRI' }), '15 1 * * MON-FRI'); // as typed: the server reads it
  assert.equal(cronOf(blank()), '0 2 * * *');
  assert.deepEqual(Object.keys(PRESETS), ['daily', 'weekdays', 'weekly', 'monthly', 'hours', 'custom']);
  assert.equal(WEEKDAYS[0], 'Sunday'); // cron's 0
  assert.deepEqual(HOURS.filter((n) => 24 % n), []); // every step divides a day
});

test('a form sends a pinned version or the name it follows, what a run takes, and never a key', () => {
  const draft = { ...blank('14'), name: 'nightly-customers', time_zone: 'Europe/London' };
  assert.deepEqual(bodyOf(draft, SPECS, 'Europe/London'), {
    name: 'nightly-customers', spec_id: 14, run: { mode: 'batch' }, cron: '0 2 * * *', time_zone: 'Europe/London',
    start_within: 60, ends_at: null,
  });
  const followed = bodyOf({ ...draft, follow: true, mode: 'soak', rolling: true, start_within: '90', ends_at: ' 2026-12-31T00:00Z ' }, SPECS, 'UTC');
  assert.deepEqual(followed, {
    name: 'nightly-customers', spec_name: 'customer-profile', run: { mode: 'soak', soak: { rolling: true } }, cron: '0 2 * * *',
    time_zone: 'UTC', start_within: 90, ends_at: '2026-12-31T00:00Z',
  });
  assert.equal(JSON.stringify(followed).includes('keys'), false);
});

test('a stored schedule is edited as custom cron, and moved to the newer version as it stands', () => {
  const draft = draftOf(NIGHTLY);
  assert.deepEqual(bodyOf(draft, SPECS, draft.time_zone), {
    name: 'nightly-customers', spec_id: 12, run: { mode: 'batch' }, cron: '0 2 * * *', time_zone: 'Europe/London',
    start_within: 60, ends_at: null,
  });
  // One following its name is edited as following it: the version it runs now names the spec.
  const following = draftOf({ ...NIGHTLY, spec_id: null, runs: { spec_id: 14, version: '1.5.0' }, run: { mode: 'soak', soak: { rolling: true } } });
  assert.deepEqual([following.follow, following.spec_id, following.mode, following.rolling], [true, '14', 'soak', true]);
  assert.equal(bodyOf(following, SPECS, 'UTC').spec_name, 'customer-profile');
  assert.deepEqual(movedTo(NIGHTLY, 14), {
    name: 'nightly-customers', spec_id: 14, run: { mode: 'batch' }, cron: '0 2 * * *', time_zone: 'Europe/London',
    start_within: 60, ends_at: null, revision: 4,
  });
});

test('the default zone is the browser\'s when listed, else what it was renamed to, else UTC with a note', () => {
  const zones = { zones: ['Asia/Kolkata', 'Europe/London', 'UTC'], renamed: { 'Asia/Calcutta': 'Asia/Kolkata', 'Europe/Kiev': 'Europe/Kyiv' } };
  assert.deepEqual(defaultZone(zones, 'Europe/London'), { zone: 'Europe/London' });
  assert.deepEqual(defaultZone(zones, 'Asia/Calcutta'), { zone: 'Asia/Kolkata' });
  assert.deepEqual(defaultZone(zones, 'Foo/Bar'), {
    zone: 'UTC', note: "your browser's time zone (Foo/Bar) is not in this build's zone data: UTC is selected",
  });
  // A renamed zone whose primary this build does not hold either, and a name no object should answer for.
  assert.equal(defaultZone(zones, 'Europe/Kiev').zone, 'UTC');
  assert.equal(defaultZone(zones, 'constructor').zone, 'UTC');
});

test('a stored schedule whose zone this build does not list is edited with no zone chosen, and a note naming it', () => {
  const zones = { zones: ['Europe/London', 'UTC'], renamed: { 'Europe/Kiev': 'Europe/Kyiv' } };
  const stored = draftOf({ ...NIGHTLY, time_zone: 'Europe/Kyiv' }).time_zone; // as the worker paused it (T6)
  assert.deepEqual(defaultZone(zones, stored, true), {
    zone: '', note: "this build's zone data does not hold Europe/Kyiv, the zone this schedule was saved with: choose its time zone",
  });
  // Never UTC unasked: 02:00 in the stored zone would become 02:00 UTC with nothing said.
  assert.equal(bodyOf(draftOf(NIGHTLY), SPECS, defaultZone(zones, stored, true).zone).time_zone, '');
  assert.deepEqual(defaultZone(zones, 'Europe/London', true), { zone: 'Europe/London' });
  assert.deepEqual(defaultZone({ ...zones, zones: ['Europe/Kyiv', 'UTC'] }, 'Europe/Kiev', true), { zone: 'Europe/Kyiv' });
  // The form asks for the stored zone as stored, requires a choice, and shows the note whichever zone it is about.
  const form = readFileSync(new URL('../src/Schedules.tsx', import.meta.url), 'utf8');
  assert.match(form, /defaultZone\(zones\.data, d\.time_zone \|\| [^,]+, !!d\.time_zone\)/);
  assert.match(form, /<select value=\{zone\.zone\}.* required>\n\s*\{!zone\.zone && <option value="">choose a zone<\/option>\}/);
  assert.match(form, /\{zone\.note && <p className="warn">\{zone\.note\}<\/p>\}/);
});

test('a schedule reads as ds schedules prints it', () => {
  assert.equal(specLabel(NIGHTLY), 'pinned 1.4.0 (spec 12)');
  assert.equal(specLabel({ ...NIGHTLY, spec_id: null }), 'follows customer-profile: 1.4.0 (spec 12) now');
  assert.equal(dueLine(NIGHTLY.next_due!, 'Europe/London'), '2026-10-02 02:00 Europe/London (2026-10-02T01:00Z)');
  assert.equal(dueLine({ utc: '2026-10-02T01:00Z', local: null }, 'Europe/Kyiv'), '2026-10-02T01:00Z'); // a zone that does not load
  assert.equal(gateLine({ state: 'not_needed', refusal: null }), 'Four-eyes approval: not needed');
  assert.equal(gateLine({ state: 'unapproved', refusal: 'spec 12 is not approved' }), 'Four-eyes approval: unapproved: spec 12 is not approved');
  assert.equal(NOT_A_FORECAST, 'a schedule, not a forecast');
});

test('the list is read over its pages, banners with them, and grouped by spec name', async () => {
  const page = (name: string, next_cursor: string | null): Schedules => ({
    not: [], tzdata: '2026c', overdue: 0, banners: [`banner of ${name}`], next_cursor,
    items: [{ ...NIGHTLY, name, spec_name: name === 'c' ? 'cards' : 'customer-profile' }],
  });
  const pages: Record<string, Schedules> = { '': page('a', '1'), 1: page('b', '2'), 2: page('c', '3'), 3: page('d', null) };
  const asked: (string | undefined)[] = [];
  const load = async (cursor?: string) => (asked.push(cursor), structuredClone(pages[cursor ?? '']));
  const found = await listed(load, 3);
  assert.deepEqual(asked, [undefined, '1', '2']);
  assert.deepEqual([found.items.map((s) => s.name), found.banners, found.next_cursor],
    [['a', 'b', 'c'], ['banner of a', 'banner of b', 'banner of c'], '3']);
  assert.equal((await listed(load, 9)).items.length, 4); // it stops where the list ends
  assert.deepEqual([...bySpec(found.items)].map(([name, items]) => [name, items.map((s) => s.name)]),
    [['customer-profile', ['a', 'b']], ['cards', ['c']]]);
});

test('no screen builds a firing text, a banner or a share: they print the server\'s', () => {
  const worded = /started run\b|not started|(after|at) its time|started by schedule|times moved|more times? since|is not settled|paused by the worker/i;
  for (const file of ['../src/scheduling.ts', '../src/Schedules.tsx', '../src/Run.tsx', '../src/Specs.tsx']) {
    const source = readFileSync(new URL(file, import.meta.url), 'utf8');
    assert.equal(worded.test(source), false, file);
  }
  for (const file of ['../src/scheduling.ts', '../src/Schedules.tsx']) {
    const source = readFileSync(new URL(file, import.meta.url), 'utf8');
    assert.equal(/%|percent|uptime|success rate/i.test(source), false, file); // N5
  }
  assert.equal(readFileSync(new URL('../src/Run.tsx', import.meta.url), 'utf8').includes('{run.schedule.text}'), true);
});

test('the screens read and act through /v1/schedules', async () => {
  const calls: [string, string, string | undefined][] = [];
  globalThis.fetch = (async (input: string, init?: RequestInit) => {
    calls.push([init?.method ?? 'GET', input, init?.body as string | undefined]);
    return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
  }) as typeof fetch;
  const id = NIGHTLY.schedule_id;
  const body = bodyOf(draftOf(NIGHTLY), SPECS, 'Europe/London');
  await api.schedules({ cursor: undefined });
  await api.scheduleZones();
  await api.schedule(id);
  await api.scheduleFirings(id, { cursor: '81234' });
  await api.previewSchedule('0 2 * * *', 'Europe/London');
  await api.createSchedule(body);
  await api.saveSchedule(id, movedTo(NIGHTLY, 14));
  await api.pauseSchedule(id);
  await api.resumeSchedule(id);
  await api.deleteSchedule(id);
  const sent = '"run":{"mode":"batch"},"cron":"0 2 * * *","time_zone":"Europe/London","start_within":60,"ends_at":null';
  assert.deepEqual(calls, [
    ['GET', '/v1/schedules', undefined],
    ['GET', '/v1/schedules/zones', undefined],
    ['GET', `/v1/schedules/${id}`, undefined],
    ['GET', `/v1/schedules/${id}/firings?cursor=81234`, undefined],
    ['POST', '/v1/schedules/preview', '{"cron":"0 2 * * *","time_zone":"Europe/London"}'],
    ['POST', '/v1/schedules', `{"name":"nightly-customers","spec_id":12,${sent}}`],
    ['PUT', `/v1/schedules/${id}`, `{"name":"nightly-customers","spec_id":14,${sent},"revision":4}`],
    ['POST', `/v1/schedules/${id}/pause`, '{}'],
    ['POST', `/v1/schedules/${id}/resume`, '{}'],
    ['DELETE', `/v1/schedules/${id}`, undefined],
  ]);
});
