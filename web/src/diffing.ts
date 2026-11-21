/** The rows of a side-by-side view of two documents (DocDiff.tsx): one row per node of either side, A and B
 *  aligned, and where a finding's pointer lies in each.
 *
 *  Findings' pointers (RFC 6901; `@i` a duplicate member, a doubled leading `@` a literal one) are located in
 *  each document. A keyed array shows as {member key: [members]}, so a lone member's list index is implied. */
import { isRaw } from './api.ts';

type Node = Record<string, unknown>;
export type Box = { v: unknown } | undefined; // undefined: the side has no node here
export interface Row { id: string; depth: number; label: string; a?: string; b?: string; mark?: string }

const isNode = (v: unknown): v is Node => typeof v === 'object' && v !== null && !isRaw(v);
const child = (box: Box, k: string): Box => (box && isNode(box.v) && Object.hasOwn(box.v, k) ? { v: box.v[k] } : undefined);
const keys = (box: Box) => (box && isNode(box.v) ? Object.keys(box.v) : []);

function show(v: unknown): string {
  if (Array.isArray(v)) return `[${v.length}]`;
  if (isNode(v)) return `{${Object.keys(v).length}}`;
  return isRaw(v) ? v.rawJSON : JSON.stringify(v);
}

/** The node path a pointer names in `doc`, or null when `doc` lacks it. */
export function locate(doc: unknown, pointer: string): string[] | null {
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

/** Every node of either side, in document order; a row is marked by `marks` (by its path as JSON) or by the
 *  nearest marked row above it. */
export function rows(a: Box, b: Box, marks: Map<string, string>): Row[] {
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
export function differing(all: Row[], marks: Map<string, string>): Row[] {
  const keep = new Set<string>();
  for (const id of marks.keys()) {
    const path = JSON.parse(id) as string[];
    for (let n = 0; n <= path.length; n++) keep.add(JSON.stringify(path.slice(0, n)));
  }
  return all.filter((r) => r.mark || keep.has(r.id));
}
