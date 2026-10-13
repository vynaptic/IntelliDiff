/** Small shared components and formatters. */
import type { ReactNode } from 'react';
import { isRaw, type Issue } from './api';

export type Col<T> = [header: string, cell: (row: T) => ReactNode];

/** Rows get `rowId` as their DOM id too, so citations can scroll to them. */
export function Table<T>({ rows, cols, rowId }: { rows: T[]; cols: Col<T>[]; rowId?: (row: T) => string }) {
  if (!rows.length) return <p className="muted">None.</p>;
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>{cols.map(([h]) => <th key={h}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={rowId?.(row) ?? i} id={rowId?.(row)}>
              {cols.map(([h, c]) => <td key={h}>{c(row)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A spec's or a target's issues, as the API lists them. */
export const ISSUE_COLS: Col<Issue>[] = [
  ['Pointer', (i) => <code>{i.pointer || '/'}</code>],
  ['Severity', (i) => i.severity],
  ['Code', (i) => i.code],
  ['Message', (i) => i.message],
];

const TONE: Record<string, string> = {
  MATCH: 'ok', completed: 'ok', running: 'ok',
  MISMATCH: 'bad', failed: 'bad', CRITICAL: 'bad', HIGH: 'bad',
  INCONCLUSIVE: 'warn', DRIFT: 'warn', NOT_FOUND_A: 'warn', NOT_FOUND_B: 'warn', MEDIUM: 'warn',
  completed_with_issues: 'warn', cancelling: 'warn', cancelled: 'warn', paused: 'warn',
  active: 'ok', revoked: 'bad', expired: 'warn', disabled: 'bad',
  approved: 'ok', rejected: 'bad', requested: 'warn',
  valid: 'ok', open: 'ok', evaluation: 'warn', ending: 'warn', grace: 'warn', unknown: 'warn',
};

export const Badge = ({ v }: { v?: string | null }) => (v ? <span className={`badge ${TONE[v] ?? ''}`}>{v}</span> : null);

export const Err = ({ error }: { error?: string }) => (error ? <p className="error" role="alert">{error}</p> : null);

/** A JSON value as one line of text; strings as they are. */
export function cell(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (isRaw(v)) return v.rawJSON;
  return typeof v === 'object' ? JSON.stringify(v) : String(v);
}

export const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : '');

/** A name followed by its user or token id, as in `jdoe (U-3f2a…)`; other actors
 *  (`local:…`, `system:…`, `dev:loopback`) are their own id. */
export function Named({ name, actor }: { name?: string | null; actor?: string | null }) {
  if (!actor) return null;
  const id = actor.replace(/^(user|token):/, '');
  if (!name || name === actor) return <span className="mono">{id}</span>;
  return (
    <>
      {name} <span className="mono muted">({id})</span>
    </>
  );
}

const isPlain = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

const flat = (obj: Record<string, unknown>, prefix = ''): [string, unknown][] =>
  Object.entries(obj).flatMap(([k, v]) => (isPlain(v) ? flat(v, `${prefix}${k}.`) : [[prefix + k, v] as [string, unknown]]));

/** Nested counters as labelled numbers. */
export const Stats = ({ values }: { values: Record<string, unknown> }) => (
  <dl className="stats">
    {flat(values).map(([k, v]) => (
      <div key={k}>
        <dt>{k}</dt>
        <dd>{cell(v)}</dd>
      </div>
    ))}
  </dl>
);

/** Saves a fetched file, so a refusal shows on the screen instead of replacing the page. */
export function save(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  Object.assign(document.createElement('a'), { href: url, download: name }).click();
  setTimeout(() => URL.revokeObjectURL(url));
}
