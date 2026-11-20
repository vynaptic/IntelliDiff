/**
 * Side-by-side masked canonical documents: one row per node of either side, A and B aligned.
 *
 * Findings' pointers are located in each document as diffing.ts reads them. Highlighting is a reading aid; the
 * findings table stays authoritative.
 *
 * A row only side A holds has side A's colour, one only side B holds side B's, and a changed value a third:
 * no row is green or red, which would read as a verdict. With findings it opens on the differences only, and
 * two buttons step through the rows a finding points at.
 */
import { useRef, useState } from 'react';
import type { Finding } from './api';
import { differing, locate, rows } from './diffing';
import { step } from './format';
import { label } from './labels';
import { Side } from './ui';

type Mark = Pick<Finding, 'json_pointer' | 'change_type'>; // a finding, or a change between two spec versions

export function DocDiff({ a, b, findings }: { a: unknown; b: unknown; findings: Mark[] }) {
  const [onlyDiffs, setOnlyDiffs] = useState(findings.length > 0);
  const [at, setAt] = useState(-1); // the difference stepped to; none yet
  const box = useRef<HTMLDivElement>(null);
  const marks = new Map<string, string>();
  for (const f of findings)
    for (const doc of [a, b]) {
      const path = doc == null ? null : locate(doc, f.json_pointer);
      if (path) marks.set(JSON.stringify(path), f.change_type);
    }
  const all = rows(a == null ? undefined : { v: a }, b == null ? undefined : { v: b }, marks);
  const shown = onlyDiffs ? differing(all, marks) : all;
  const stops = shown.filter((r) => marks.has(r.id)).map((r) => r.id); // the rows a finding points at

  function go(delta: number) {
    const next = step(at, stops.length, delta);
    setAt(next);
    box.current?.querySelector(`[data-stop="${next}"]`)?.scrollIntoView({ block: 'center' });
  }

  return (
    <div>
      <div className="inline">
        <label className="muted">
          <input type="checkbox" checked={onlyDiffs} onChange={(e) => setOnlyDiffs(e.target.checked)} /> differences only
        </label>
        {stops.length > 0 && (
          <>
            <button className="small" onClick={() => go(-1)}>Previous difference</button>
            <button className="small" onClick={() => go(1)}>Next difference</button>
            <span className="muted" role="status">{at < 0 ? `${stops.length} marked` : `${at + 1} of ${stops.length}`}</span>
          </>
        )}
        <span className="legend">
          {['missing_in_b', 'extra_in_b', 'value_changed', 'type_changed', 'order_changed'].map((c) => (
            <span key={c} className={c} title={c}>{label(c)}</span>
          ))}
        </span>
      </div>
      <div className="docdiff scroll" ref={box}>
        <div className="head"><span>Path</span><span><Side side="a" /></span><span><Side side="b" /></span></div>
        {shown.map((r) => {
          const stop = stops.indexOf(r.id);
          return (
            <div key={r.id} className={`line ${r.mark ?? ''}${stop >= 0 && stop === at ? ' at' : ''}`} data-stop={stop >= 0 ? stop : undefined}>
              <span style={{ paddingLeft: `${r.depth}em` }}>{r.label}</span>
              <span>{r.a}</span>
              <span>{r.b}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
