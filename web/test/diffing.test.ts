// Side-by-side documents: a finding's pointer located as the engine writes it, both sides' nodes aligned in rows,
// and the rows a difference marks with their ancestors.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { differing, locate, rows } from '../src/diffing.ts';

test('a pointer is located in a document as RFC 6901 escapes it', () => {
  const doc = { 'a/b': { '~t': 1 }, list: [10, 20] };
  assert.deepEqual(locate(doc, '/a~1b/~0t'), ['a/b', '~t']);
  assert.deepEqual(locate(doc, '/list/1'), ['list', '1']);
  assert.deepEqual(locate(doc, ''), []);
  assert.equal(locate(doc, '/missing'), null);
  assert.equal(locate(doc, '/list/2'), null);
});

test('@i names a duplicate member and a doubled leading @ a member that starts with one', () => {
  const doc = { name: { 1: 'second of two "name"' }, '@type': 'x', '@1': 'literal' };
  assert.deepEqual(locate(doc, '/name/@1'), ['name', '1']);
  assert.deepEqual(locate(doc, '/@@type'), ['@type']);
  assert.deepEqual(locate(doc, '/@@1'), ['@1']);
});

test("a keyed array's lone member is named by its key alone: its list index is implied", () => {
  const doc = { accounts: { 'ACC-1': [{ status: 'open' }], 'ACC-2': [{ status: 'x' }, { status: 'y' }] } };
  assert.deepEqual(locate(doc, '/accounts/ACC-1/status'), ['accounts', 'ACC-1', '0', 'status']);
  assert.deepEqual(locate(doc, '/accounts/ACC-1/0/status'), ['accounts', 'ACC-1', '0', 'status']);
  assert.equal(locate(doc, '/accounts/ACC-2/status'), null); // two members: the index is not implied
});

test("rows align both sides' nodes, and a mark holds for the nodes under it", () => {
  const a = { id: 'K1', tags: ['x'] };
  const b = { id: 'K1', tags: ['x', 'y'], extra: true };
  const marks = new Map([[JSON.stringify(['tags', '1']), 'extra_in_b'], [JSON.stringify(['extra']), 'extra_in_b']]);
  const all = rows({ v: a }, { v: b }, marks);
  assert.deepEqual(
    all.map((r) => [r.label, r.depth, r.a, r.b, r.mark]),
    [
      ['$', 0, '{2}', '{3}', undefined],
      ['id', 1, '"K1"', '"K1"', undefined],
      ['tags', 1, '[1]', '[2]', undefined],
      ['[0]', 2, '"x"', '"x"', undefined],
      ['[1]', 2, undefined, '"y"', 'extra_in_b'],
      ['extra', 1, undefined, 'true', 'extra_in_b'],
    ],
  );
  const nested = rows({ v: { o: { p: 1 } } }, undefined, new Map([[JSON.stringify(['o']), 'missing_in_b']]));
  assert.deepEqual(nested.map((r) => r.mark), [undefined, 'missing_in_b', 'missing_in_b']);
});

test('the differences only are the marked rows and their ancestors', () => {
  const marks = new Map([[JSON.stringify(['tags', '1']), 'extra_in_b']]);
  const all = rows({ v: { id: 'K1', tags: ['x'] } }, { v: { id: 'K1', tags: ['x', 'y'] } }, marks);
  assert.deepEqual(differing(all, marks).map((r) => r.label), ['$', 'tags', '[1]']);
});
