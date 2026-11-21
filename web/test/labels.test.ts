// The words the screens show for the engine's codes, and each code's tone.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { label, tone } from '../src/labels.ts';

test('an outcome reads as words, its side letter kept upper-case', () => {
  assert.deepEqual(
    ['MATCH', 'MISMATCH', 'KNOWN_DIFF', 'NOISE_ONLY', 'DRIFT', 'NOT_FOUND_A', 'NOT_FOUND_B', 'INCONCLUSIVE'].map(label),
    ['Match', 'Mismatch', 'Known difference', 'Noise only', 'Drift', 'Not found in A', 'Not found in B', 'Inconclusive'],
  );
});

test('a status, a severity and a taxonomy code read as a sentence-case phrase', () => {
  assert.equal(label('completed_with_issues'), 'Completed with issues');
  assert.equal(label('CRITICAL'), 'Critical');
  assert.equal(label('FIELD_MISSING'), 'Field missing');
  assert.equal(label('absence_unconfirmed'), 'Absence unconfirmed');
});

test('a change and its direction name the side by its letter', () => {
  assert.deepEqual(['missing_in_b', 'extra_in_b', 'value_changed'].map(label), ['Missing in B', 'Extra in B', 'Value changed']);
  assert.deepEqual(['a_only', 'b_only', 'both', 'b_converged'].map(label), ['A only', 'B only', 'Both', 'B converged']);
});

test('a filter is labelled by what it filters, not by its parameter name', () => {
  assert.deepEqual(
    ['key', 'taxonomy_code', 'path_pattern', 'root_cause_id', 'session_id'].map(label),
    ['Key', 'Taxonomy', 'Path pattern', 'Root cause', 'Session'],
  );
  assert.deepEqual(['spec_name', 'target_a', 'target_b', 'key_name'].map(label), ['Comparison name', 'Target A', 'Target B', 'Key name']);
});

test('text that is no code is left as it is', () => {
  assert.equal(label(''), '');
  assert.equal(label('Two approvers needed'), 'Two approvers needed');
  assert.equal(label('INCOMPLETE: absence_unconfirmed'), 'INCOMPLETE: absence_unconfirmed');
});

test('a code has the tone of its verdict, and an unknown code has none', () => {
  assert.deepEqual(['MATCH', 'MISMATCH', 'DRIFT', 'completed', 'failed', 'paused'].map(tone), ['ok', 'bad', 'warn', 'ok', 'bad', 'warn']);
  assert.equal(tone('KNOWN_DIFF'), '');
  assert.equal(tone('never-heard-of'), '');
});
