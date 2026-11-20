// A table scan's line and bulk graph parity's, as the run screen prints them.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { Run, Scan, TablePlan } from '../src/api.ts';
import { graphLine, scanLine } from '../src/scanning.ts';

const SCAN: Scan = { done: true, rows_a: 120, rows_b: 118, skipped_a: 1, skipped_b: 2, matched: 100, candidates: 19 };
const plan = (more: Partial<TablePlan>): TablePlan => ({ schema: null, name: 't', keys: ['id'], columns: {}, ...more });
const run = (status: string, tables?: Run['params']['tables']) => ({ status, params: { tables } }) as Run;

test('a scan says what it read and left, and its state while it runs', () => {
  assert.equal(
    scanLine(run('running'), { ...SCAN, done: false }),
    'Table scan (running): 120 rows on A, 118 on B; 100 identical; 19 to compare; 3 not compared (NULL, empty or over-long key).',
  );
  assert.ok(scanLine(run('completed'), SCAN).startsWith('Table scan (done): '));
});

test("a scan names the columns one side alone holds, each side's filter and engine, and a file side's files", () => {
  const tables = {
    a: plan({ where: 'region = 7', engine: 'oracle', server: '23.4', consistency: 'snapshot' as TablePlan['consistency'] }),
    b: plan({ engine: 'files', file: { path: '*.csv', format: 'csv' }, file_count: 2, bytes: 640 }),
    only_a: ['legacy_flag'],
    only_b: [],
  };
  const line = scanLine(run('completed', tables), { ...SCAN, repeated_b: 3 });
  assert.ok(
    line.endsWith(
      ' Columns only on A: legacy_flag. Filter on A: region = 7. Side A: oracle 23.4 (snapshot).' +
        ' Side B: files (csv), 2 files, 640 bytes, fixed. 3 repeated keys.',
    ),
    line,
  );
});

test('graph parity says how the groups and their components differ', () => {
  const graph = { groups_a: 40, groups_b: 41, groups_equal: 37, groups_different: 3, split: 1, merge: 2, rearrange: 0, oversized: 0 };
  assert.equal(
    graphLine(graph),
    'Graph: 40 groups on A, 41 on B; 3 differ; 1 split, 2 merge, 0 rearranged, 0 too large to expand.',
  );
});
