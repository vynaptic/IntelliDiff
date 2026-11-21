// The sample pairs a draft is made from: a JSON array of {key, a, b}, or JSON Lines of sample records.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { stringifyExact } from '../src/api.ts';
import { readPairs } from '../src/profiling.ts';

test('a JSON array is taken as the pairs it lists, numbers exact', () => {
  const text = '[{"key": "key-1", "a": {"amount": 1.10}, "b": null}]';
  const [pair] = readPairs(text);
  assert.equal(pair.key, 'key-1');
  assert.equal(stringifyExact(pair.a), '{"amount":1.10}');
  assert.equal(pair.b, null);
});

test('sample records pair by key, and only a 2xx body is a side’s document', () => {
  const lines = [
    '{"key": "key-2", "side": "a", "status": 200, "body": {"name": "x"}}',
    '{"key": "key-2", "side": "b", "status": 404, "body": {"error": "not found"}}',
    '',
    '{"key": "key-3", "side": "b", "body": {"name": "y"}}',
  ].join('\n');
  assert.deepEqual(readPairs(lines).map((p) => [p.key, stringifyExact(p.a), stringifyExact(p.b)]), [
    ['key-2', '{"name":"x"}', 'null'],
    ['key-3', 'null', '{"name":"y"}'],
  ]);
});
