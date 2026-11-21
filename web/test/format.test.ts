// How long ago something happened, a long id in short, and the step from one difference to the next.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ago, shortId, step } from '../src/format.ts';

const NOW = Date.parse('2026-11-19T22:00:00Z');
const before = (seconds: number) => new Date(NOW - seconds * 1000).toISOString();

test('the last moments read as just now', () => {
  assert.equal(ago(before(0), NOW), 'just now');
  assert.equal(ago(before(44), NOW), 'just now');
});

test('minutes, hours and days are counted whole', () => {
  assert.equal(ago(before(45), NOW), '1 min ago');
  assert.equal(ago(before(59 * 60), NOW), '59 min ago');
  assert.equal(ago(before(60 * 60), NOW), '1 h ago');
  assert.equal(ago(before(23 * 3600 + 59 * 60), NOW), '23 h ago');
  assert.equal(ago(before(24 * 3600), NOW), '1 day ago');
  assert.equal(ago(before(6 * 86400 + 3600), NOW), '6 days ago');
});

test('a week or more ago, a time ahead of this clock and no time at all get no relative wording', () => {
  assert.equal(ago(before(7 * 86400), NOW), undefined);
  assert.equal(ago(before(-120), NOW), undefined); // the server's clock is ahead: the exact time is shown
  assert.equal(ago(null, NOW), undefined);
  assert.equal(ago('not a time', NOW), undefined);
});

test('a time a few seconds ahead of this clock is still just now', () => {
  assert.equal(ago(before(-5), NOW), 'just now');
});

test('a long id shows its start and its end, where two ids of the same hour differ', () => {
  assert.equal(shortId('R-3619Y0G6TP9FZV75MRWA3KE8ME'), 'R-3619…KE8ME');
  assert.notEqual(shortId('R-3619Y0G6TP9FZV75MRWA3KE8ME'), shortId('R-3619Y0HS0JKF9QYR5YVE171ZHC'));
});

test('an id that is short already is shown whole', () => {
  assert.equal(shortId('R-1'), 'R-1');
  assert.equal(shortId('SC-0123456789ab'), 'SC-0123456789ab');
});

test('the next difference follows the last one round to the first, and back', () => {
  assert.equal(step(0, 3, 1), 1);
  assert.equal(step(2, 3, 1), 0);
  assert.equal(step(0, 3, -1), 2);
  assert.equal(step(-1, 3, 1), 0); // none chosen yet: the first
  assert.equal(step(-1, 3, -1), 2); // none chosen yet: the last
});

test('without a difference there is nowhere to step', () => {
  assert.equal(step(-1, 0, 1), -1);
});
