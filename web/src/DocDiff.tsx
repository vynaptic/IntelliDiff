/**
 * Side-by-side masked canonical documents: one row per node of either side, A and B aligned.
 *
 * Findings' pointers (RFC 6901; `@i` a duplicate member, a doubled leading `@` a literal one) are
 * located in each document. A keyed array shows as {member key: [members]}, so a lone member's list
 * index is implied. Highlighting is a reading aid; the findings table stays authoritative.
 */
import { useState } from 'react';
import { isRaw, type Finding } from './api';

type Node = Record<string, unknown>;
type Box = { v: unknown } | undefined; // undefined: the side has no node here
interface Row { id: string; depth: number; label: string; a?: string; b?: string; mark?: string }

const isNode = (v: unknown): v is Node => typeof v === 'object' && v !== null && !isRaw(v);
const child = (box: Box, k: string): Box => (box && isNode(box.v) && Object.hasOwn(box.v, k) ? { v: box.v[k] } : undefined);
const keys = (box: Box) => (box && isNode(box.v) ? Object.keys(box.v) : []);

function show(v: unknown): string {
  if (Array.isArray(v)) return `[${v.length}]`;
  if (isNode(v)) return `{${Object.keys(v).length}}`;
  return isRaw(v) ? v.rawJSON : JSON.stringify(v);
}

/** The node path a pointer names in `doc`, or null when `doc` lacks it. */
function locate(doc: unknown, pointer: string): string[] | null {
  const path: string[] = [];
  let node = doc;
  for (const seg of pointer.split('/').slice(1).map((s) => s.replace(/~1/g, '/').replace(/~0/g, '~'))) {
    const dup = /^@(\d+)$/.exec(seg);
    if (Array.isArray(node) && node.length === 1 && !dup && !/^\d+$/.test(seg)) {
      path.push('0');
      node = node[0];
    }
    const name = dup ? dup[1] : seg.startsWith('@@') ? seg.slice(1) : seg;
    if (!isNode(node) || !Object.hasOwn(node, name)) return null;
    path.push(name);
    node = node[name];
  }
  return path;
}

function rows(a: Box, b: Box, marks: Map<string, string>): Row[] {
  const out: Row[] = [];
  const walk = (a: Box, b: Box, path: string[], label: string, inherited?: string) => {
    const id = JSON.stringify(path);
    const mark = marks.get(id) ?? inherited;
    out.push({ id, depth: path.length, label, a: a && show(a.v), b: b && show(b.v), mark });
    const list = Array.isArray(a?.v ?? b?.v);
    for (const k of new Set([...keys(a), ...keys(b)])) walk(child(a, k), child(b, k), [...path, k], list ? `[${k}]` : k, mark);
  };
  walk(a, b, [], '$');
  return out;
}

/** Marked rows and their ancestors. */
function differing(all: Row[], marks: Map<string, string>): Row[] {
  const keep = new Set<string>();
  for (const id of marks.keys()) {
    const path = JSON.parse(id) as string[];
    for (let n = 0; n <= path.length; n++) keep.add(JSON.stringify(path.slice(0, n)));
  }
  return all.filter((r) => r.mark || keep.has(r.id));
}

type Mark = Pick<Finding, 'json_pointer' | 'change_type'>; // a finding, or a change between two spec versions

export function DocDiff({ a, b, findings }: { a: unknown; b: unknown; findings: Mark[] }) {
  const [onlyDiffs, setOnlyDiffs] = useState(false);
  const marks = new Map<string, string>();
  for (const f of findings)
    for (const doc of [a, b]) {
      const path = doc == null ? null : locate(doc, f.json_pointer);
      if (path) marks.set(JSON.stringify(path), f.change_type);
    }
  const all = rows(a == null ? undefined : { v: a }, b == null ? undefined : { v: b }, marks);
  return (
    <div>
      <label className="muted">
        <input type="checkbox" checked={onlyDiffs} onChange={(e) => setOnlyDiffs(e.target.checked)} /> differences only
      </label>
      <span className="legend">
        {['missing_in_b', 'extra_in_b', 'value_changed', 'type_changed', 'order_changed'].map((c) => (
          <span key={c} className={c}>{c}</span>
        ))}
      </span>
      <div className="docdiff scroll">
        <div className="head"><span>path</span><span>A</span><span>B</span></div>
        {(onlyDiffs ? differing(all, marks) : all).map((r) => (
          <div key={r.id} className={`line ${r.mark ?? ''}`}>
            <span style={{ paddingLeft: `${r.depth}em` }}>{r.label}</span>
            <span>{r.a}</span>
            <span>{r.b}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
