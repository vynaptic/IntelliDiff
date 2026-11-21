/** What the run screen says of a table scan and of bulk graph parity. */
import type { Graph, Run, Scan } from './api';
import { fileSide } from './packs.ts';

const num = (v: number) => v.toLocaleString();

/** A table scan, while it runs and after: what it read and what it left, and each table side's engine where
 *  the plan names it; a file side's files in its place. */
export function scanLine(run: Run, scan: Scan): string {
  const t = run.params.tables;
  const only = ([['A', t?.only_a ?? []], ['B', t?.only_b ?? []]] as const).filter(([, cols]) => cols.length);
  const filters = ([['A', t?.a?.where], ['B', t?.b?.where]] as const).filter(([, where]) => where);
  const engines = ([['A', t?.a], ['B', t?.b]] as const).filter(([, plan]) => plan?.engine);
  return (
    `Table scan (${scan.done ? 'done' : run.status}): ${num(scan.rows_a)} rows on A, ${num(scan.rows_b)} on B; ` +
    `${num(scan.matched)} identical; ${num(scan.candidates)} to compare; ` +
    `${num(scan.skipped_a + scan.skipped_b)} not compared (NULL, empty or over-long key).` +
    only.map(([side, cols]) => ` Columns only on ${side}: ${cols.join(', ')}.`).join('') +
    filters.map(([side, where]) => ` Filter on ${side}: ${where}.`).join('') +
    engines
      .map(([side, plan]) =>
        plan?.engine === 'files'
          ? fileSide(side, plan, side === 'A' ? scan.repeated_a : scan.repeated_b)
          : ` Side ${side}: ${plan?.engine} ${plan?.server} (${plan?.consistency}).`,
      )
      .join('')
  );
}

/** Bulk graph parity: the groups whose members differ and the components that changed. */
export function graphLine(g: Graph): string {
  return (
    `Graph: ${num(g.groups_a)} groups on A, ${num(g.groups_b)} on B; ${num(g.groups_different)} differ; ` +
    `${num(g.split)} split, ${num(g.merge)} merge, ${num(g.rearrange)} rearranged, ${num(g.oversized)} too large to expand.`
  );
}
