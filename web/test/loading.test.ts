// The data hooks' logic: a poll that waits for each answer and backs off from a slow one, and a load whose
// data is dropped once what it was loaded for changes.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { nextDelay, poll, sameDeps, shownState, type Timers } from '../src/loading.ts';

/** Timers on a clock the test moves: `pending` lists each timer's wait from now. */
function clock() {
  let now = 0;
  let id = 0;
  const timers = new Map<number, { at: number; fn: () => void }>();
  const timersOf: Timers = {
    now: () => now,
    set: (fn, ms) => (timers.set(++id, { at: now + ms, fn }), id),
    clear: (handle) => void timers.delete(handle as number),
  };
  return {
    timers: timersOf,
    pending: () => [...timers.values()].map((t) => t.at - now),
    /** Move the clock on by `ms`, firing the timers due by then. */
    tick(ms: number) {
      now += ms;
      for (const [key, t] of [...timers]) if (t.at <= now) (timers.delete(key), t.fn());
    },
  };
}

const settled = () => new Promise<void>((resolve) => setImmediate(() => resolve())); // its callbacks have run

/** A tick whose answers the test gives: `answer(ms)` moves the clock on by `ms`, then answers. */
function asked(c: ReturnType<typeof clock>) {
  const waiting: (() => void)[] = [];
  let calls = 0;
  return {
    tick: () => (calls++, new Promise<void>((resolve) => waiting.push(resolve))),
    calls: () => calls,
    async answer(ms: number) {
      c.tick(ms);
      waiting.shift()?.();
      await settled();
    },
  };
}

test('the next poll waits for the answer before it, then for the interval', async () => {
  const c = clock();
  const server = asked(c);
  poll(server.tick, 2000, c.timers);
  assert.deepEqual(c.pending(), [2000]);
  c.tick(2000);
  assert.equal(server.calls(), 1);
  c.tick(10_000); // a slow answer: no poll is sent meanwhile
  assert.deepEqual([server.calls(), c.pending()], [1, []]);
  await server.answer(0);
  assert.deepEqual(c.pending(), [20_000]); // twice the 10 s it took
});

test('a quick answer keeps the interval and a slow one backs off to twice its time', async () => {
  const c = clock();
  const server = asked(c);
  poll(server.tick, 2000, c.timers);
  c.tick(2000);
  await server.answer(300);
  assert.deepEqual(c.pending(), [2000]);
  c.tick(2000);
  await server.answer(1500);
  assert.deepEqual(c.pending(), [3000]);
  assert.deepEqual([nextDelay(2000, 0), nextDelay(2000, 999), nextDelay(2000, 1001)], [2000, 2000, 2002]);
});

test('a failed answer keeps the polling going, and a stop ends it, an answer still awaited included', async () => {
  const c = clock();
  let calls = 0;
  const stop = poll(() => (calls++, Promise.reject(new Error('down'))), 2000, c.timers);
  c.tick(2000);
  await settled();
  assert.deepEqual([calls, c.pending()], [1, [2000]]);
  stop();
  assert.deepEqual(c.pending(), []);
  const server = asked(c);
  const again = poll(server.tick, 2000, c.timers);
  c.tick(2000);
  again(); // while its answer is awaited
  await server.answer(5);
  assert.deepEqual([server.calls(), c.pending()], [1, []]);
});

test('a load whose deps changed shows no data until its own answer', () => {
  assert.ok(sameDeps(['R-1', '2', 1], ['R-1', '2', 1]));
  assert.ok(!sameDeps(['R-1', '1'], ['R-1', '2']));
  assert.ok(!sameDeps(['R-1'], ['R-1', undefined]));
  assert.ok(!sameDeps(undefined, []));
  assert.ok(sameDeps([NaN], [NaN])); // as React compares them
  const loaded = { data: 'cycle 1', error: undefined, deps: ['R-1', '1'] };
  assert.deepEqual(shownState(loaded, ['R-1', '1']), { data: 'cycle 1', error: undefined, loading: false });
  assert.deepEqual(shownState(loaded, ['R-1', '2']), { data: undefined, error: undefined, loading: true });
  const failed = { error: 'down', deps: ['R-1', '2'] };
  assert.deepEqual(shownState(failed, ['R-1', '2']), { data: undefined, error: 'down', loading: false });
  assert.deepEqual(shownState({}, []), { data: undefined, error: undefined, loading: true });
});
