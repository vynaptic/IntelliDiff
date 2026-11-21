// The Node built-ins the tests use, typed as far as they use them, so that the type check reads the tests too:
// the UI's dependencies hold no Node types. A test that needs more of Node declares it here.

declare module 'node:assert/strict' {
  type Expected = RegExp | ((error: unknown) => boolean) | (new (...args: never[]) => unknown) | object;
  function assert(value: unknown, message?: string | Error): asserts value;
  namespace assert {
    function ok(value: unknown, message?: string | Error): asserts value;
    function equal<T>(actual: unknown, expected: T, message?: string | Error): asserts actual is T;
    function notEqual(actual: unknown, expected: unknown, message?: string | Error): void;
    function deepEqual<T>(actual: unknown, expected: T, message?: string | Error): asserts actual is T;
    function match(value: string, pattern: RegExp, message?: string | Error): void;
    function doesNotMatch(value: string, pattern: RegExp, message?: string | Error): void;
    function throws(block: () => unknown, error?: Expected, message?: string | Error): void;
    function rejects(
      block: Promise<unknown> | (() => Promise<unknown>),
      error?: Expected,
      message?: string | Error,
    ): Promise<void>;
  }
  export default assert;
}

declare module 'node:test' {
  interface MockTimers {
    enable(options?: { apis?: ('setInterval' | 'setTimeout' | 'setImmediate' | 'Date')[]; now?: number }): void;
    tick(ms: number): void;
    reset(): void;
  }
  interface TestContext {
    mock: { timers: MockTimers };
  }
  export function test(name: string, fn: (t: TestContext) => unknown): Promise<void>;
}

declare module 'node:fs' {
  export function readFileSync(path: string | URL, encoding: 'utf8' | 'utf-8'): string;
  export function readdirSync(path: string | URL, options: { recursive?: boolean; encoding: 'utf8' }): string[];
}

declare function setImmediate(callback: () => void): unknown;
