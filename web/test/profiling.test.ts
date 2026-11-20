// The sample pairs a draft is made from: a JSON array of {key, a, b}, or JSON Lines of sample records.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { stringifyExact } from '../src/api.ts';
import { readPairs } from '../src/profiling.ts';

test('a JSON array is taken as the pairs it lists, numbers exact', () => {
  const text = '[{"key": "MBR-0045123", "a": {"amount": 1.10}, "b": null}]';
  const [pair] = readPairs(text);
  assert.equal(pair.key, 'MBR-0045123');
  assert.equal(stringifyExact(pair.a), '{"amount":1.10}');
  assert.equal(pair.b, null);
});

test('sample records pair by key, and only a 2xx body is a side’s document', () => {
  const lines = [
    '{"key": "ORG-000123", "side": "a", "status": 200, "body": {"name": "x"}}',
    '{"key": "ORG-000123", "side": "b", "status": 404, "body": {"error": "not found"}}',
    '',
    '{"key": "ORG-000124", "side": "b", "body": {"name": "y"}}',
  ].join('\n');
  assert.deepEqual(readPairs(lines).map((p) => [p.key, stringifyExact(p.a), stringifyExact(p.b)]), [
    ['ORG-000123', '{"name":"x"}', 'null'],
    ['ORG-000124', 'null', '{"name":"y"}'],
  ]);
});
