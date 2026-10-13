/** Cut-over readiness, open to every role and read-only: per programme the pairs
 *  that are ready and how that count moved, per pair its place on the ladder, "k of m" gates with a cell a gate,
 *  its last evidence cycles and what it rests on; and one pair's gates by group, convergence, trend, strata, live
 *  runs and recorded transitions, with the sign-off precheck on demand; and one pair's cut-over brief: its gates
 *  beside the stored figures of its newest complete cycle, its last measured cycles with their marks and its
 *  root-cause groups, opened, still open and closed. Named gates met or not on observed evidence: never a
 *  probability, a forecast or sign-off evidence, and every gate's text is the API's. The filters live in the URL;
 *  the gates are the host administrator's settings, which no screen changes. */
import { useCallback, useState, type ReactNode } from 'react';
import {
  api, type Brief, type BriefGroup, type BriefPoint, type Gate, type Precheck, type ReadyPair, type ReadyPoint,
  type ReadyStratum, type Readiness, type Transition, type TrendPoint,
} from './api';
import { href, message, useLoad, usePaged, usePoll } from './lib';
import {
  ADD_UP, DAYS, GATES, MARKS, STATES, briefChart, briefDownloads, briefFigures, briefNotes, downloads, filtersOf,
  programmeLine, ratio, reads, readySteps, scoreLine, sparkTitle, sparkline, stateShape, stateTone, statesLine,
  statusShape, statusTone, stepPath, stepTitle, trendChart, worded, type Download, type Strip,
} from './ready';
import { Badge, Err, Table, save, when, type Col } from './ui';

const POLL_MS = 60_000;
const GROUPS = ['parity', 'governance', 'evidence'] as const;

const State = ({ pair }: { pair: Pick<ReadyPair, 'state' | 'regressed'> }) => (
  <>
    <span className={`badge ${stateTone(pair.state)}`}>{stateShape(pair.state)} {worded(pair.state)}</span>
    {pair.regressed && <> <span className="badge bad">regressed</span></>}
  </>
);

const Cycle = ({ at }: { at: { run_id: string; cycle: number } }) => (
  <>
    cycle {at.cycle} of <a className="mono" href={href(['runs', at.run_id], { cycle: at.cycle })}>{at.run_id}</a>
  </>
);

/** A version as the texts name it (two spec ids may share a version string), linked to its stored spec. */
const Version = ({ of }: { of: { version: string | null; spec_id: number | null } }) =>
  of.version === null || of.spec_id === null ? null : <a href={href(['specs', of.spec_id])}>{of.version} (spec {of.spec_id})</a>;

/** What heads a pair's screens: its version, programme, entity type and the readiness settings it is judged under. */
const Meta = ({ pair, children }: { pair: ReadyPair & { config_sha256: string }; children?: ReactNode }) => (
  <p className="muted">
    {pair.spec_id !== null && <><Version of={pair} /> · </>}
    {pair.programme && `programme ${pair.programme} · `}
    {pair.entity && `entity ${pair.entity} · `}
    readiness settings <span className="mono">{pair.config_sha256.slice(0, 12)}</span>
    {children}
  </p>
);

/** A cell a gate, in the ladder's order: its status as a shape, its text on hover; one not required is dimmed. */
const Cells = ({ gates }: { gates: Gate[] }) => (
  <span className="gates">
    {gates.map((g) => (
      <span
        key={g.gate}
        className={g.required ? statusTone(g.status) : 'muted'}
        title={`${worded(g.gate)}: ${worded(g.status)}${g.required ? '' : ' (not required)'}\n${g.text}`}
      >
        {statusShape(g.status)}
      </span>
    ))}
  </span>
);

function Sparkline({ trend }: { trend: TrendPoint[] }) {
  const s = sparkline(trend);
  return (
    <svg className="spark" width={s.width} height={s.height} viewBox={`0 0 ${s.width} ${s.height}`} role="img" aria-label={sparkTitle(trend)}>
      <title>{sparkTitle(trend)}</title>
      <polyline className="allowed" points={s.allowed} />
      <polyline className="value" points={s.value} />
      {s.dots.map((d) => <circle key={d.x} className={d.over ? 'bad' : 'ok'} cx={d.x} cy={d.y} r={d.over ? 2.5 : 1.5} />)}
      {s.marks.map((m) => (
        <text key={m.x} className={m.passed ? 'ok' : 'bad'} x={m.x} y={s.height / 2 + 4} textAnchor="middle">
          {m.passed ? '■' : '✕'}
        </text>
      ))}
    </svg>
  );
}

/** How many of the programme's listed pairs were ready over the last 90 days, from their recorded transitions. */
function Steps({ pairs, rows, now, since }: { pairs: ReadyPair[]; rows: Transition[]; now: string; since: string }) {
  const steps = readySteps(rows, Object.fromEntries(pairs.map((p) => [p.pair, p.state])), now, since);
  const [width, height] = [240, 40];
  return (
    <svg className="spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={stepTitle(steps, pairs.length)}>
      <title>{stepTitle(steps, pairs.length)}</title>
      <line className="allowed" x1={3} x2={width - 3} y1={3} y2={3} />
      <path className="value" d={stepPath(steps, pairs.length, width, height)} />
    </svg>
  );
}

export function ReadinessScreen({ query }: { query: URLSearchParams }) {
  const f = filtersOf(query);
  const asked = JSON.stringify(f);
  const [pages, setPages] = useState(1);
  const found = useLoad(async (): Promise<Readiness> => {
    const first = await api.readiness(f);
    let [items, next] = [first.items, first.next_cursor];
    for (let n = 1; n < pages && next; n++) {
      const page = await api.readiness({ ...f, cursor: next });
      [items, next] = [[...items, ...page.items], page.next_cursor];
    }
    return { ...first, items, next_cursor: next };
  }, [asked, pages]);
  const moves = useLoad(() => api.transitions({ limit: 500 }), []);
  const [error, setError] = useState<string>();
  const reload = useCallback(() => {
    found.reload();
    moves.reload();
  }, [found.reload, moves.reload]);
  usePoll(reload, POLL_MS, true);
  const d = found.data;
  const rows = moves.data?.items ?? [];
  // The steps begin 90 days back, or where the 500 newest rows begin.
  const since = d && (moves.data?.next_cursor ? rows.at(-1)!.at : new Date(Date.parse(d.now) - DAYS * 86_400_000).toISOString());

  const set = (name: string, value: string) => (location.hash = href(['readiness'], { ...f, [name]: value }));
  const select = (name: string, label: string, options: (string | null)[], all = 'all', said = (o: string) => o) => (
    <label>
      {label}{' '}
      <select value={f[name] ?? ''} onChange={(e) => set(name, e.target.value)}>
        <option value="">{all}</option>
        {options.flatMap((o) => (o === null ? [] : [<option key={o} value={o}>{said(o)}</option>]))}
      </select>
    </label>
  );
  const download = (report: Download) => {
    setError(undefined);
    api.readinessReport(report.name, { programme: f.programme, entity: f.entity, volume: report.volume }).then(
      (blob) => save(blob, report.file),
      (e) => setError(message(e)),
    );
  };

  const cols: Col<ReadyPair>[] = [
    ['Pair', (p) => (
      <>
        <a href={href(['readiness', p.pair])}>{p.pair}</a>
        <div className="muted"><Version of={p} />{p.spec_id !== null && p.entity && ' · '}{p.entity}</div>
      </>
    )],
    ['State', (p) => <State pair={p} />],
    ['Gates', (p) => <><span className="mono">{scoreLine(p.score)}</span> <Cells gates={p.gates} /></>],
    ['Last evidence cycles', (p) => <Sparkline trend={p.trend} />],
    ['Evidence cycle', (p) => p.evidence && (
      <>
        <Cycle at={p.evidence} />
        <div className="muted">ended {when(p.evidence.ended_at)} · {p.evidence.basis} · {p.evidence.claim}</div>
      </>
    )],
    ['Since', (p) => (p.since ? when(p.since) : <span className="muted">not recorded yet</span>)],
    ['Sign-off', (p) => p.signoff && <a className="mono" href={href(['approvals', p.signoff])}>{p.signoff}</a>],
    ['Pack', (p) => p.pack && <span className="mono">{p.pack}</span>],
    ['Deviations', (p) => [...p.deviations, p.closed && `closed on ${p.closed}`].filter(Boolean).join('; ')],
  ];
  return (
    <>
      <h1>Readiness</h1>
      <Err error={found.error ?? moves.error ?? error} />
      {d && since && (
        <>
          <p className="muted">{d.definition}</p>
          <p className="muted">
            Readiness settings <span className="mono">{d.config_sha256.slice(0, 12)}</span>, judged {when(d.now)}.
          </p>
          {d.notes.map((note) => <p key={note} className="warn">{note}</p>)}
          <p className="inline">
            {select('programme', 'Programme', d.programmes.map((p) => p.programme))}
            {select('entity', 'Entity', d.entities.map((e) => e.entity))}
            {select('state', 'State', STATES, 'all', worded)}
            {select('gate', 'Gate not met', GATES, 'all', worded)}
            {select('closed', 'Pairs', ['closed', 'all'], 'open')}
            <label>
              Name <input defaultValue={f.q ?? ''} size={18} onBlur={(e) => e.target.value !== (f.q ?? '') && set('q', e.target.value)} />
            </label>
            {downloads(d.volumes).map((report) => <button key={report.file} onClick={() => download(report)}>{report.label}</button>)}
          </p>
          {d.programmes.map((p) => {
            const pairs = d.items.filter((i) => i.programme === p.programme);
            return (
              <section key={p.programme ?? ''}>
                <h2>{programmeLine(p)}</h2>
                <p className="inline muted">
                  <Steps pairs={pairs} rows={rows} now={d.now} since={since} />
                  {p.entities.map((e) => <span key={e.entity ?? ''}>{e.entity ?? '(no entity)'}: {statesLine(e.states)}</span>)}
                </p>
                <Table rows={pairs} cols={cols} rowId={(i) => `pair-${i.pair}`} />
              </section>
            );
          })}
          {!d.programmes.length && <p className="muted">No pair: readiness judges the specs with a batch or soak run.</p>}
          {d.next_cursor && <button onClick={() => setPages(pages + 1)}>More</button>}
        </>
      )}
    </>
  );
}

const GATE_COLS: Col<Gate>[] = [
  ['Gate', (g) => worded(g.gate)],
  ['Required', (g) => (g.required ? 'yes' : 'no')],
  ['Status', (g) => <span className={`badge ${statusTone(g.status)}`}>{statusShape(g.status)} {worded(g.status)}</span>],
  ['Observed', (g) => <span className="pre">{g.text}</span>],
];
const n = (v: number | null) => (v === null ? '' : v.toLocaleString('en-US'));
const POINT_COLS: Col<ReadyPoint>[] = [
  ['Cycle', (p) => <Cycle at={p} />],
  ['Ended', (p) => when(p.ended_at)],
  ['Version', (p) => <Version of={p} />],
  ['Basis', (p) => `${p.basis ?? ''}${p.skipped ? ' (no evidence point)' : ''}`],
  ['Total', (p) => `${n(p.total)}${p.sample_status && p.total !== null ? ' sampled' : ''}`],
  ['Discrepant', (p) => n(p.different)],
  ['Unresolved', (p) => n(p.unresolved)],
  ['Suppressed', (p) => n(p.suppressed)],
  ['Unexplained CRITICAL', (p) => n(p.unexplained_critical)],
  ['New', (p) => n(p.new)],
  ['Persistent', (p) => n(p.persistent)],
  ['Fixed', (p) => n(p.fixed)],
  ['Other', (p) => n(p.other)],
  ['Sample', (p) => p.sample_status ?? ''],
  ['Graph rows', (p) => n(p.graph_rows)],
  ['Fails', (p) => <span className="bad">{p.fails.join(', ')}</span>],
];
const STRATUM_COLS: Col<ReadyStratum>[] = [
  ['Stratum', (s) => s.label],
  ['Tier', (s) => s.tier],
  ['Basis', (s) => s.basis],
  ['Frame keys', (s) => n(s.frame_keys)],
  ['Drawn', (s) => n(s.drawn)],
  ['Compared', (s) => n(s.n)],
  ['Discrepant', (s) => n(s.x)],
  ['Unresolved', (s) => n(s.unresolved)],
  ['Absent both', (s) => n(s.absent_both)],
  ['Truncated', (s) => n(s.api_uncomparable)],
  ['Verdict', (s) => s.verdict],
  ['Why', (s) => s.why.join('; ')],
];
const MOVE_COLS: Col<Transition>[] = [
  ['Recorded', (t) => when(t.at)],
  ['From', (t) => (t.detail.from ? worded(t.detail.from) : 'first recorded')],
  ['To', (t) => <State pair={{ state: t.detail.to, regressed: t.detail.regressed }} />],
  ['Gates', (t) => scoreLine(t.detail.score)],
  ['Causes', (t) => t.detail.causes.join(', ')],
  ['Changed', (t) => t.detail.changed.map((c) => `${worded(c.gate)} ${worded(c.from ?? 'not required')} → ${worded(c.to ?? 'not required')}`).join('; ')],
  ['Audit row', (t) => t.seq],
];

function Trend({ points }: { points: ReadyPoint[] }) {
  const c = trendChart(points);
  return (
    <svg className="spark trend" viewBox={`0 0 ${c.width} ${c.height}`} role="img" aria-label="discrepant or unresolved keys of each measured cycle, oldest first">
      {c.boundaries.map((b) => (
        <g key={b.x}>
          <line className="allowed" x1={b.x} x2={b.x} y1={0} y2={c.height} />
          <text className="muted" x={b.x + 3} y={10}>{b.version}</text>
        </g>
      ))}
      <polyline className="value" points={c.line} />
      {c.dots.map((dot) => (
        <circle key={dot.x} className={`${dot.failed ? 'bad' : 'ok'}${dot.skipped ? ' hollow' : ''}`} cx={dot.x} cy={dot.y} r={dot.failed ? 3.5 : 2.5}>
          <title>{dot.title}</title>
        </circle>
      ))}
    </svg>
  );
}

function Checked({ precheck }: { precheck: Precheck | null }) {
  if (!precheck) return <p className="muted">Nothing to check: the precheck is of an unsigned candidate cycle, under single sign-on.</p>;
  return (
    <div className="panel">
      <p>
        <b>{precheck.label}</b>: <Cycle at={precheck} />
      </p>
      {precheck.purged && <p className="warn">Its evidence was purged: it can no longer be signed off.</p>}
      {!precheck.purged && !precheck.problems.length && <p className="ok">A sign-off request would find no problem now.</p>}
      <ul>{precheck.problems.map((why) => <li key={why}>{why}</li>)}</ul>
    </div>
  );
}

export function ReadinessPair({ name }: { name: string }) {
  const found = useLoad(() => api.readinessPair(name), [name]);
  const [bucket, setBucket] = useState('cycle');
  const trend = usePaged((cursor) => api.readinessCycles(name, { bucket, cursor }), [name, bucket]);
  const moves = usePaged((cursor) => api.transitions({ pair: name, cursor }), [name]);
  const [check, setCheck] = useState<{ precheck?: Precheck | null; error?: string }>({});
  usePoll(found.reload, POLL_MS, true);
  const d = found.data;
  const points = [...trend.items].sort((a, b) => a.ended_at.localeCompare(b.ended_at) || a.cycle - b.cycle); // pages go back in time

  const precheck = () =>
    api.readinessPair(name, true).then(
      (pair) => setCheck({ precheck: pair.precheck ?? null }),
      (e) => setCheck({ error: message(e) }),
    );
  return (
    <>
      <h1>
        Readiness of {name} {d && <State pair={d} />}
      </h1>
      <p><a href="#/readiness">All pairs</a> · <a href={href(['readiness', name, 'brief'])}>Cut-over brief</a></p>
      <Err error={found.error ?? check.error} />
      {d && (
        <>
          <p className="muted">{d.definition}</p>
          <Meta pair={d}>{d.since && ` · in this state since ${when(d.since)}`}</Meta>
          {d.notes.map((note) => <p key={note} className="warn">{note}</p>)}
          {d.deviations.length > 0 && <p className="warn">Deviations: {d.deviations.join('; ')}.</p>}
          <ol className="ladder">
            {STATES.map((s) => (
              <li key={s} className={s === d.state ? stateTone(s) : 'muted'} aria-current={s === d.state ? 'step' : undefined}>
                {stateShape(s)} {worded(s)}
              </li>
            ))}
          </ol>
          <p>
            <b>{scoreLine(d.score)} gates</b> <Cells gates={d.gates} />
            {d.evidence && <> · evidence <Cycle at={d.evidence} /> ({d.evidence.basis}, {d.evidence.claim})</>}
            {d.candidate && <> · candidate <Cycle at={d.candidate} /></>}
            {d.signoff && <> · sign-off <a className="mono" href={href(['approvals', d.signoff])}>{d.signoff}</a></>}
            {d.pack && <> · pack <span className="mono">{d.pack}</span></>}
          </p>
          <p className="inline">
            <button onClick={precheck}>Check sign-off now</button>
            <span className="muted">Computed now from the stored evidence; the sign-off decides.</span>
          </p>
          {check.precheck !== undefined && <Checked precheck={check.precheck} />}
          {GROUPS.map((group) => (
            <section key={group}>
              <h2>{group[0].toUpperCase() + group.slice(1)} gates</h2>
              <Table rows={d.gates.filter((g) => g.group === group)} cols={GATE_COLS} rowId={(g) => `gate-${g.gate}`} />
            </section>
          ))}
          <h2>Convergence</h2>
          <p>
            {d.convergence.streak} of the {d.convergence.needed} evidence cycles in a row the gate needs (at most{' '}
            {d.convergence.window} are counted).
          </p>
          <h2>Trend</h2>
          <p className="inline">
            <label>
              By{' '}
              <select value={bucket} onChange={(e) => setBucket(e.target.value)}>
                <option value="cycle">cycle</option>
                <option value="day">day (its last evidence cycle)</option>
                <option value="week">week (its last evidence cycle)</option>
              </select>
            </label>
            <span className="muted">
              Discrepant or unresolved keys of each cycle, oldest first; hollow: no evidence point; red: fails a per-cycle
              gate; a dashed line: a new version. A sampled cycle's counts are its sample's own: no rate and no bound is
              given for the strata pooled.
            </span>
          </p>
          <Err error={trend.error} />
          {points.length > 0 && <Trend points={points} />}
          {trend.more && <button onClick={trend.more}>Older</button>}
          <Table rows={points} cols={POINT_COLS} rowId={(p) => `point-${p.run_id}-${p.cycle}`} />
          {d.strata && (
            <>
              <h2>Strata of the evidence cycle</h2>
              <p className="muted">Counts and verdicts per stratum; no rate and no bound is given for the strata pooled.</p>
              <Table rows={d.strata} cols={STRATUM_COLS} rowId={(s) => `stratum-${s.label}`} />
            </>
          )}
          {d.labelling && (
            <p className="muted">
              Labelling <span className="mono">{d.labelling.labelling_id}</span> of <Cycle at={d.labelling} />, labelled{' '}
              {when(d.labelling.labelled_at)}
              {d.labelling.attested && <>, attested by sign-off <span className="mono">{d.labelling.attested}</span></>}: its
              lines are the accuracy gate's text.
            </p>
          )}
          <h2>Live soaks of this version</h2>
          <Table
            rows={d.runs}
            cols={[
              ['Run', (r) => <a className="mono" href={href(['runs', r.run_id])}>{r.run_id}</a>],
              ['Cycle', (r) => r.cycle],
              ['Status', (r) => `${r.status}${r.stop_reason ? ` (${r.stop_reason})` : ''}`],
              ['Started', (r) => when(r.started_at)],
            ]}
            rowId={(r) => r.run_id}
          />
          <h2>Versions</h2>
          <Table
            rows={d.versions}
            cols={[
              ['Version', (v) => <Version of={v} />],
              ['Newest evidence cycle', (v) => <Cycle at={v} />],
              ['Ended', (v) => when(v.ended_at)],
            ]}
            rowId={(v) => `version-${v.spec_id}`}
          />
          <h2>Transitions</h2>
          <Err error={moves.error} />
          <Table rows={moves.items} cols={MOVE_COLS} rowId={(t) => `seq-${t.seq}`} />
          {moves.more && <button onClick={moves.more}>More</button>}
        </>
      )}
    </>
  );
}

const SHOWN = 10; // root-cause groups a brief shows; the others listed are under All groups
const BRIEF_POINT_COLS: Col<BriefPoint>[] = [
  ['Cycle', (p) => <Cycle at={p} />],
  ['Ended', (p) => when(p.ended_at)],
  ['Version', (p) => <Version of={p} />],
  ['Basis', (p) => `${worded(p.basis ?? '')}${p.complete ? '' : ' (incomplete)'}`],
  ['Compared', (p) => n(p.compared)],
  ['Different', (p) => n(p.different)],
  ['Known difference', (p) => n(p.by_outcome?.KNOWN_DIFF ?? null)],
  ['Noise only', (p) => n(p.by_outcome?.NOISE_ONLY ?? null)],
  ['Mismatch rate', (p) => ratio(p.mismatch_rate, p.different, p.compared) ?? (p.compared === null ? '' : p.sample_status ? 'sampled, no rate' : 'no rate')],
  ['Root-cause groups', (p) => (p.root_causes === null && p.compared !== null ? 'not kept' : n(p.root_causes))],
  ['Marks', (p) => p.marks.map((mark) => MARKS[mark]).join(' ')],
];
const GROUP_COLS: Col<BriefGroup>[] = [
  ['Group', (g) => (
    <a className="mono" href={href(['runs', g.last.run_id], { cycle: g.last.cycle, root_cause_id: g.root_cause_id })}>
      {g.root_cause_id}
    </a>
  )],
  ['State', (g) => reads(g)],
  ['Severity', (g) => <Badge v={g.max_severity} />],
  ['Taxonomy', (g) => g.taxonomy_code],
  ['Change', (g) => g.change_type],
  ['Keys', (g) => n(g.affected_keys)],
  ['Cycles', (g) => n(g.seen)],
  ['First', (g) => <><Cycle at={g.first} />, {when(g.first.ended_at)}</>],
  ['Last', (g) => <><Cycle at={g.last} />, {when(g.last.ended_at)}</>],
];

/** A brief's trend: each cycle's stored mismatch rate above its root-cause groups on one x axis, each strip from 0
 *  to its own printed maximum; a dot links to its cycle, and a lettered line marks a point. */
function BriefTrend({ points }: { points: BriefPoint[] }) {
  const c = briefChart(points);
  const strip = (s: Strip, name: string) => (
    <g>
      <text className="muted" x={0} y={s.top - 8}>{name}</text>
      {s.max !== null && s.max !== '0' && <text className="muted" x={c.left - 6} y={s.top + 4} textAnchor="end">{s.max}</text>}
      <text className="muted" x={c.left - 6} y={s.base + 4} textAnchor="end">0</text>
      <line className="axis" x1={c.left} x2={c.width} y1={s.base} y2={s.base} />
      <path className="value" d={s.line} />
      {s.dots.map((dot) => (
        <a key={dot.x} href={href(['runs', dot.run_id], { cycle: dot.cycle })}>
          <circle className={dot.hollow ? 'hollow' : undefined} cx={dot.x} cy={dot.y} r={3}>
            <title>{dot.title}</title>
          </circle>
        </a>
      ))}
    </g>
  );
  return (
    <svg className="spark trend" viewBox={`0 0 ${c.width} ${c.height}`} role="img" aria-label="stored mismatch rate and root-cause groups of each measured cycle, oldest first">
      {c.marks.map((mark) => (
        <g key={mark.x}>
          <line className="allowed" x1={mark.x} x2={mark.x} y1={14} y2={c.height} />
          <text className="muted" x={mark.x} y={10} textAnchor="middle">{mark.letters}</text>
        </g>
      ))}
      {strip(c.rate, 'Mismatch rate')}
      {strip(c.groups, 'Root-cause groups')}
    </svg>
  );
}

/** The brief's reported cycle: its stored figures between its notes, each figure a link to the run's cycle. */
function Reported({ brief: d }: { brief: Brief }) {
  const c = d.cycle;
  if (!c) return <p className="warn">{d.notes.at(-1)}</p>; // why none is reported
  const notes = briefNotes(c, d.evidence);
  const at = (outcome?: string) => href(['runs', c.run_id], { cycle: c.cycle, outcome });
  return (
    <>
      {notes.above.map((note) => <p key={note} className="warn">{note}</p>)}
      <p>
        <Cycle at={c} /> · ended {when(c.ended_at)} · <Version of={c} /> · basis {worded(c.basis ?? '')} · {c.claim}
      </p>
      <dl className="stats">
        {briefFigures(c).map((f) => (
          <div key={f.label}>
            <dt>{f.label}</dt>
            <dd><a href={at(f.outcome)}>{f.value}</a></dd>
          </div>
        ))}
      </dl>
      <p className="muted">{ADD_UP}</p>
      {[...notes.rate, ...notes.sample].map((note) => <p key={note}>{note}</p>)}
      {c.strata && <ul>{c.strata.map((line) => <li key={line}><a href={at()}>{line}</a></li>)}</ul>}
      {notes.rest.map((note) => <p key={note} className="warn">{note}</p>)}
    </>
  );
}

/** A pair's cut-over brief, the content of its brief.pdf: stored measures restated, with a link to each
 *  cycle, group, version and sign-off it names. The API judges and words it; this screen adds no figure. */
export function ReadinessBrief({ name }: { name: string }) {
  const found = useLoad(() => api.readinessBrief(name), [name]);
  const [error, setError] = useState<string>();
  usePoll(found.reload, POLL_MS, true);
  const d = found.data;
  const download = (report: Download) => {
    setError(undefined);
    api.readinessReport(report.name, { pair: name }).then(
      (blob) => save(blob, report.file),
      (e) => setError(message(e)),
    );
  };
  // Every gate's status; the text of those that block, and of the approval, the sign-off and the pack.
  const told = new Set([...(d?.blocking ?? []), 'spec_approval', 'signoff', 'pack']);
  const gateCols: Col<Gate>[] = [
    ['Gate', (g) => (g.run_id && g.cycle !== null ? <a href={href(['runs', g.run_id], { cycle: g.cycle })}>{worded(g.gate)}</a> : worded(g.gate))],
    ...GATE_COLS.slice(1, 3),
    ['Observed', (g) => told.has(g.gate) && (
      <>
        <span className="pre">{g.text}</span>
        {g.gate === 'signoff' && d?.signoff && <> <a className="mono" href={href(['approvals', d.signoff])}>{d.signoff}</a></>}
        {g.gate === 'pack' && d?.pack && <> <span className="mono">{d.pack}</span></>}
      </>
    )],
  ];
  return (
    <>
      <h1>
        Cut-over brief of {name} {d && <State pair={d} />}
      </h1>
      <p><a href="#/readiness">All pairs</a> · <a href={href(['readiness', name])}>Gates in full</a></p>
      <Err error={found.error ?? error} />
      {d && (
        <>
          <p className="muted">{d.definition}</p>
          <p className="muted">{d.brief}</p>
          <Meta pair={d}> · judged {when(d.now)}</Meta>
          {d.notes.slice(0, d.causes ? undefined : -1).map((note) => <p key={note} className="warn">{note}</p>)}
          {d.deviations.length > 0 && <p className="warn">Deviations: {d.deviations.join('; ')}.</p>}
          <p className="inline">
            {briefDownloads(name).map((report) => <button key={report.name} onClick={() => download(report)}>{report.label}</button>)}
          </p>
          <h2>Newest complete cycle</h2>
          <Reported brief={d} />
          <h2>Gates, approval and sign-off</h2>
          <p>
            <b>{scoreLine(d.score)} gates</b> <Cells gates={d.gates} />
          </p>
          <Table rows={d.gates} cols={gateCols} rowId={(g) => `gate-${g.gate}`} />
          <h2>Trend</h2>
          {d.points.length > 0 && <BriefTrend points={d.points} />}
          <p className="muted">{d.legend}</p>
          <Table rows={d.points} cols={BRIEF_POINT_COLS} rowId={(p) => `point-${p.run_id}-${p.cycle}`} />
          {d.cycle && (
            <>
              <h2>Root-cause groups</h2>
              {d.causes ? (
                <>
                  <p>{d.causes.text}</p>
                  <Table rows={d.causes.items.slice(0, SHOWN)} cols={GROUP_COLS} rowId={(g) => g.root_cause_id} />
                  {d.causes.items.length > SHOWN && (
                    <details>
                      <summary>All groups</summary>
                      <Table rows={d.causes.items.slice(SHOWN)} cols={GROUP_COLS} rowId={(g) => g.root_cause_id} />
                    </details>
                  )}
                </>
              ) : (
                <p className="warn">{d.notes.at(-1)}</p>
              )}
            </>
          )}
        </>
      )}
    </>
  );
}
