/** Small shared components and formatters. */
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { isRaw, type Issue } from './api';
import { ago } from './format';
import { label, tone } from './labels';
import { segments } from './outcomes';

export type Col<T> = [header: string, cell: (row: T) => ReactNode];

/** Rows get `rowId` as their DOM id too, so citations can scroll to them. Without rows it says that they are
 *  being read (`loading`), or `empty`: what there is none of, and what to do about it. */
export function Table<T>({ rows, cols, rowId, loading, empty }: {
  rows: T[];
  cols: Col<T>[];
  rowId?: (row: T) => string;
  loading?: boolean;
  empty?: ReactNode;
}) {
  if (!rows.length && loading) return <p className="muted" role="status">Loading…</p>;
  if (!rows.length) return <p className="muted">{empty ?? 'None.'}</p>;
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>{cols.map(([h]) => <th key={h}>{h === 'A' || h === 'B' ? <Side side={h === 'A' ? 'a' : 'b'} /> : h}</th>)}</tr>
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

/** A code in words, coloured by its verdict; the code itself is the tooltip. */
export const Badge = ({ v }: { v?: string | null }) =>
  v ? <span className={`badge ${tone(v)}`} title={label(v) === v ? undefined : v}>{label(v)}</span> : null;

export const Err = ({ error }: { error?: string }) => (error ? <p className="error" role="alert">{error}</p> : null);

/** A JSON value as one line of text; strings as they are. */
export function cell(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (isRaw(v)) return v.rawJSON;
  return typeof v === 'object' ? JSON.stringify(v) : String(v);
}

export const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : '');

/** A time of the last week in words, the exact time as its tooltip; an older one exactly. */
export function When({ at }: { at: string | null }) {
  if (!at) return null;
  const words = ago(at, Date.now());
  return <time dateTime={at} title={words ? when(at) : undefined}>{words ?? when(at)}</time>;
}

/** A side by its letter, in the colour that side has on every screen. */
export const Side = ({ side }: { side: 'a' | 'b' }) => <span className={`side ${side}`}>{side.toUpperCase()}</span>;

/** A button that copies `text` and says that it did. */
export function Copy({ text, children }: { text: string; children: ReactNode }) {
  const [done, setDone] = useState(false);
  const copy = () => navigator.clipboard.writeText(text).then(() => setDone(true), () => setDone(false));
  return <button type="button" className="small" onClick={copy}>{done ? 'Copied' : children}</button>;
}

/** Under a table read page by page: how many rows are shown, and the button for the next page while there is one. */
export const MoreRows = ({ shown, more }: { shown: number; more?: () => void }) =>
  more ? (
    <p className="more">
      <span className="muted">{shown.toLocaleString('en-US')} shown so far</span>
      <button onClick={more}>Show more</button>
    </p>
  ) : null;

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

/** A run's keys by outcome as one bar, what does not match first; with `href` each segment is a link that
 *  filters by its outcome. Each segment names its count and outcome, so the bar never rests on colour alone. */
export function OutcomeBar({ byOutcome, href, small }: {
  byOutcome?: Record<string, number>;
  href?: (outcome: string) => string;
  small?: boolean;
}) {
  const parts = segments(byOutcome);
  if (!parts.length) return null;
  const said = parts.map((s) => `${s.n.toLocaleString('en-US')} ${label(s.outcome)}`);
  return (
    <span className={small ? 'obar small' : 'obar'} role={href ? undefined : 'img'} aria-label={href ? undefined : said.join(', ')}>
      {parts.map((s, i) => {
        const shown = { className: tone(s.outcome) || 'none', style: { flexGrow: s.n }, title: said[i] };
        return href ? <a key={s.outcome} href={href(s.outcome)} aria-label={said[i]} {...shown} /> : <span key={s.outcome} {...shown} />;
      })}
    </span>
  );
}

/** A screen's tabs as links: the current one is marked, and the address keeps which. */
export const Tabs = ({ items, current, name }: { items: [href: string, text: ReactNode, id: string][]; current: string; name: string }) => (
  <nav className="tabs" aria-label={name}>
    {items.map(([to, text, id]) => (
      <a key={id} href={to} aria-current={id === current ? 'page' : undefined}>{text}</a>
    ))}
  </nav>
);

/** A button that opens a short list of actions; it closes on a choice, on Escape and on a click elsewhere. */
export function Menu({ label: text, children }: { label: string; children: ReactNode }) {
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const close = (e: Event) => {
      const menu = ref.current;
      if (!menu?.open) return;
      if (e instanceof KeyboardEvent ? e.key === 'Escape' : !menu.contains(e.target as Node)) menu.open = false;
    };
    addEventListener('pointerdown', close);
    addEventListener('keydown', close);
    return () => {
      removeEventListener('pointerdown', close);
      removeEventListener('keydown', close);
    };
  }, []);
  return (
    <details className="menu" ref={ref}>
      <summary>{text}</summary>
      <div onClick={() => (ref.current!.open = false)}>{children}</div>
    </details>
  );
}
