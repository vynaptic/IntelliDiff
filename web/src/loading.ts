/** The data hooks' logic (lib.ts): a poll that waits for each answer before the next and backs off from a slow
 *  one, and which loaded data is shown: only the answer to the load of what is shown now. */

/** The timers a poll runs on: the browser's, or a test's clock. */
export interface Timers {
  now: () => number;
  set: (fn: () => void, ms: number) => unknown;
  clear: (handle: unknown) => void;
}

const browser: Timers = {
  now: () => Date.now(),
  set: (fn, ms) => setTimeout(fn, ms),
  clear: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** The wait before the next poll: the interval, or twice as long as the last answer took when that is longer,
 *  so a server slow to answer is asked less often. */
export const nextDelay = (ms: number, took: number) => Math.max(ms, 2 * took);

/** Call `tick` `ms` after the start, then again `nextDelay` after each answer, a failed one too (the screen
 *  shows its error): never while an answer is awaited. The function returned stops it, an answer still awaited
 *  included. */
export function poll(tick: () => unknown, ms: number, timers: Timers = browser): () => void {
  let stopped = false;
  let handle: unknown;
  const after = (wait: number) => {
    if (!stopped) handle = timers.set(ask, wait);
  };
  async function ask() {
    const started = timers.now();
    try {
      await tick();
    } catch {
      // the load shows its own error
    }
    after(nextDelay(ms, timers.now() - started));
  }
  after(ms);
  return () => {
    stopped = true;
    timers.clear(handle);
  };
}

/** Whether two dependency lists are the same, as React compares them (`Object.is`, element by element). */
export function sameDeps(a: readonly unknown[] | undefined, b: readonly unknown[] | undefined): boolean {
  return !!a && !!b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
}

/** What a load shows: its answer only while it answers the current deps, else nothing yet; `loading` until then. */
export function shownState<T>(state: { data?: T; error?: string; deps?: readonly unknown[] }, deps: readonly unknown[]) {
  const own = sameDeps(state.deps, deps);
  const data = own ? state.data : undefined;
  const error = own ? state.error : undefined;
  return { data, error, loading: !data && !error };
}
