// What the Notifications screen and every screen's banners say, in `ds notify`'s words, who sees the Mute form,
// and what the screen sends.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { api, type Delivery, type Mute, type Notifications, type NotifyChannel, type NotifyRule } from '../src/api.ts';
import {
  RULES_NOTE, bannerLines, canMute, channelLine, cursorLine, deliveryLine, external, muteLine, ruleLine, testLine,
} from '../src/notify.ts';

const HOOK: NotifyChannel = {
  name: 'ops-hook', kind: 'webhook', host: 'events.bank.example', proxy: false, allow_private_network: true,
  labels: true, links: true, recipients: 0, pending: 3, sent_24h: 1204, dead_24h: 2, suppressed_24h: 0,
  last_error: 'http_5xx', last_status: 503,
};
const MUTED = 'Notifications to ops-hook are muted until 2026-10-04 09:00 UTC by user:U-3f2a (M-01K4ZQ5W9C2H7N0M3T8V6XYA1B)';

function state(fields: Partial<Notifications>): Notifications {
  return { enabled: true, label: 'prod', cursor: { seq: 81234, head: 81240, behind: null }, channels: [HOOK], rules: [],
    mutes: [], banners: [], ...fields };
}

test('the cursor and a channel read as ds notify prints them', () => {
  assert.equal(cursorLine(state({})), 'label prod: audit row 81234 of 81240 notified');
  assert.equal(cursorLine(state({ cursor: { seq: null, head: 7, behind: '2026-10-03T04:00:12+00:00' } })),
    'label prod: audit row - of 7 notified; unread since 2026-10-03 04:00 UTC');
  assert.equal(channelLine(HOOK),
    'channel ops-hook (webhook, events.bank.example): 3 pending; in 24 h 1,204 sent, 2 dead, 0 held back; last http_5xx (503)');
  assert.equal(channelLine({ ...HOOK, last_error: null, last_status: null }).endsWith('0 held back'), true);
});

test('Slack and Teams channels are badged external: their deliveries leave the network', () => {
  assert.deepEqual((['webhook', 'email', 'slack', 'teams'] as const).map((kind) => external({ kind })), [false, false, true, true]);
});

test('rules are read-only, as declared, and say where a change is made', () => {
  const rule: NotifyRule = { name: 'new-gaps', events: ['cycle.ended'], channels: ['owners-mail', 'owners-teams'],
    specs: ['cards-*'], modes: ['batch', 'soak'], when: { new_root_causes_at_least: 1, coverage_below: null },
    reasons: null, states: null, statuses: null };
  assert.equal(ruleLine(rule),
    'new-gaps: cycle.ended to owners-mail, owners-teams (specs cards-*; modes batch, soak; when new_root_causes_at_least 1)');
  assert.equal(RULES_NOTE, 'Declared in deltasleuth.yaml (notify.rules); a change applies at the next restart.');
});

test('a test answers as ds notify test prints it', () => {
  const sent = { delivery_id: 'N-1', state: 'sent', status: 204, error: null, latency_ms: 212 } as const;
  assert.equal(testLine(HOOK, sent), 'sent to ops-hook: HTTP 204 in 212 ms (N-1)');
  assert.equal(testLine(HOOK, { ...sent, state: 'dead', status: 401, error: 'http_4xx' }), 'ops-hook: http_4xx (HTTP 401) (N-1)');
  assert.equal(testLine(HOOK, { ...sent, state: 'dead', status: null, error: 'timeout' }), 'ops-hook: timeout (N-1)');
  const mail = { ...HOOK, name: 'owners-mail', kind: 'email' } as const;
  assert.equal(testLine(mail, { ...sent, status: 250, error: 'recipients_refused' }),
    'sent to owners-mail: SMTP 250, recipients_refused in 212 ms (N-1)');
});

test('mutes and deliveries read as ds notify lists them', () => {
  const mute: Mute = { mute_id: 'M-1', scope: 'run', target: 'R-9', until: '2026-10-04T09:00:00+00:00', reason: 'planned',
    created_by: 'local:ops@ds', created_at: '2026-10-04T05:00:00+00:00' };
  assert.equal(muteLine(mute), 'M-1: run R-9 until 2026-10-04 09:00 UTC by local:ops@ds: planned');
  const delivery = { delivery_id: 'N-2', channel: 'ops-hook', event: 'run.failed', severity: 'alert', state: 'suppressed',
    why: 'muted', attempts: 0, seq: 81234, run_id: 'R-9', last_error: null, last_status: null } as Delivery;
  assert.equal(deliveryLine(delivery), 'N-2: ops-hook run.failed alert suppressed (muted), 0 attempt(s), audit row 81234, run R-9');
});

test('every screen shows the server banners verbatim, viewers included, and none while notifications are off', () => {
  assert.deepEqual(bannerLines(state({ banners: [MUTED] })), [MUTED]);
  assert.deepEqual(bannerLines(state({ enabled: false, banners: [MUTED] })), []);
  assert.deepEqual(bannerLines(undefined), []);
});

test('the Mute form is for admins only, never a token', () => {
  const me = (kind: 'user' | 'token' | 'dev', roles: string[]) => ({ kind, roles });
  assert.equal(canMute(me('user', ['viewer', 'operator', 'spec_author', 'admin'])), true);
  assert.equal(canMute(me('dev', ['admin'])), true); // developer mode acts as admin
  assert.equal(canMute(me('user', ['viewer', 'operator', 'spec_author', 'rule_approver'])), false);
  assert.equal(canMute(me('token', ['admin'])), false);
  assert.equal(canMute(undefined), false);
});

test('the screen reads and acts through /v1/notifications', async () => {
  const calls: [string, string, string | undefined][] = [];
  globalThis.fetch = (async (input: string, init?: RequestInit) => {
    calls.push([init?.method ?? 'GET', input, init?.body as string | undefined]);
    return new Response(JSON.stringify(state({})), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }) as typeof fetch;
  await api.notifications();
  await api.deliveries({ channel: 'ops-hook', state: '' });
  await api.testChannel('ops-hook');
  await api.retryChannel('ops-hook');
  await api.mute({ scope: 'channel', target: 'ops-hook', hours: 4, reason: 'planned' });
  await api.unmute('M-1');
  assert.deepEqual(calls, [
    ['GET', '/v1/notifications', undefined],
    ['GET', '/v1/notifications/deliveries?channel=ops-hook', undefined],
    ['POST', '/v1/notifications/channels/ops-hook/test', '{}'],
    ['POST', '/v1/notifications/channels/ops-hook/retry', '{}'],
    ['POST', '/v1/notifications/mutes', '{"scope":"channel","target":"ops-hook","hours":4,"reason":"planned"}'],
    ['POST', '/v1/notifications/mutes/M-1/end', '{}'],
  ]);
});
