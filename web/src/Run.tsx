/** A run: progress (polled while it is live), what its spec approval and its licence say, the schedule that
 *  started it, what the schema guard says of its tables (a banner while it holds the run paused), where its
 *  pair stands on the readiness ladder, root causes with an AI draft about one of them or about the run, results,
 *  findings, reports, soak changes, until when its evidence is kept, a sampled run's draw, each ended cycle's
 *  sign-off, its census, adjudications and evidence packs, and its labelled accuracy. The filters live in the
 *  URL, so root-cause links and citation chips are plain links. A purged cycle shows a banner instead of its
 *  tables, which would answer 410. */
import { useEffect, useState, type FormEvent, type ReactNode } from 'react';
import {
  ApiError,
  api,
  type Adjudication,
  type Approval,
  type Change,
  type CycleEvidence,
  type DrawnLabelling,
  type Finding,
  type Graph,
  type Labelling,
  type Pack,
  type Result,
  type RootCause,
  type Run,
  type RunDetail,
  type Scan,
  type Session,
  type LedgerDraw,
  type Stratum,
} from './api';
import { DRAW_HINT, needsAcknowledgement, stated, takesLabelling, takesLabels } from './accuracy';
import { approvalLabel, signoffLabel, ssoOnly } from './four-eyes';
import { Chat, CitedText } from './Chat';
import { FACTS, NOT_EVIDENCE, NO_DRAFT, answerOf, citedKeys, draftTitle, firstOnly, off, refused, runFacts } from './explain';
import { LICENCE_PAUSE, pausedLine, refusal, reportFile } from './licensing';
import { href, message, useLicence, useLoad, usePaged, usePoll, useWho } from './lib';
import { censusLine, censusNote, censusWarnings, fileSide, packsSsoOnly, scoreLine, signedOff, unfrozen } from './packs';
import { runLine } from './ready';
import { controlLabel, guardBanner, guardLines } from './prechecking';
import { chatClosed, isPurged, purgedBanner, retainedLine } from './retention';
import { CLOSE_TITLE, blockedLines, certainFailures, drawLine, drawState, etaLine, frameLine, ledgerLine, percent } from './samples';
import { Badge, Err, Named, Stats, Table, cell, save, when, type Col } from './ui';

const TERMINAL = ['cancelled', 'completed', 'completed_with_issues', 'failed'];
const ACTIONS: Record<string, string[]> = { queued: ['cancel'], running: ['pause', 'cancel'], paused: ['resume', 'cancel'] };
// reports.FORMATS, as GET /v1/runs/{id}/reports/{name} serves them
const REPORTS = ['report.pdf', 'discrepancies.csv', 'retry.csv', 'coverage.csv', 'normalization.csv', 'root_causes.csv',
  'suppressed.csv', 'graph.csv', 'readiness.csv'];
const SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO'];
const OUTCOMES = ['MATCH', 'MISMATCH', 'KNOWN_DIFF', 'NOISE_ONLY', 'DRIFT', 'NOT_FOUND_A', 'NOT_FOUND_B', 'INCONCLUSIVE'];
// What each outcome says of a key (docs/architecture.md §7.1): the legend under a run's outcome badges.
const LEGEND: Record<string, string> = {
  MATCH: 'Both sides answered in full and nothing that matters differs.',
  MISMATCH: 'Both sides answered in full and a difference has no explanation.',
  KNOWN_DIFF: 'A spec rule covers every difference; it counts as a match unless the rule calls it a bug.',
  NOISE_ONLY: 'Only fields the spec declares as noise differ; no key gets it in this build.',
  DRIFT: 'A side changed between the first read and the recheck, so nothing can be said.',
  NOT_FOUND_A: 'Side b holds the record; side a says clearly it has none.',
  NOT_FOUND_B: 'Side a holds the record; side b says clearly it has none.',
  INCONCLUSIVE: 'A side could not be read in full, or neither side holds the key, so nothing can be said.',
};

type Filters = Record<string, string>;
type KeyLink = (key: string) => ReactNode;

export function RunScreen({ runId, query }: { runId: string; query: URLSearchParams }) {
  const run = useLoad(() => api.run(runId), [runId]);
  const ai = useLoad(api.health, []).data?.ai;
  const r = run.data;
  usePoll(run.reload, 2000, !!r && !TERMINAL.includes(r.status));
  const f: Filters = Object.fromEntries(query);
  const keyLink = (key: string) => <a href={href(['runs', runId, 'keys', key], { cycle: f.cycle })}>{key}</a>;
  const version = `${r?.status}/${r?.cycle}`; // the tables reload when the run moves on
  const viewed = Number(f.cycle ?? r?.cycle); // the cycle shown: the current one unless the URL names one
  return (
    <>
      <h1>
        Run {runId} <Badge v={r?.status} />
      </h1>
      <Err error={run.error} />
      {r && (
        <>
          <Header run={r} cycle={f.cycle} viewed={viewed} reload={run.reload} />
          <FilterForm run={r} f={f} />
          {isPurged(r, viewed) ? (
            <p className="banner" role="status">{purgedBanner(viewed)}</p>
          ) : (
            <>
              <RootCauses runId={runId} f={f} version={version} keyLink={keyLink} run={r} ai={ai} />
              <Results runId={runId} f={f} version={version} keyLink={keyLink} />
              <Findings runId={runId} f={f} version={version} keyLink={keyLink} />
            </>
          )}
          {r.mode === 'soak' && <Changes runId={runId} version={version} />}
          {r.sample && <Sample run={r} />}
          <Signoffs run={r} reload={run.reload} />
          <Evidence run={r} reload={run.reload} />
          <Accuracy run={r} reload={run.reload} />
          <Chat runId={runId} closed={chatClosed(r)} ai={ai} />
        </>
      )}
    </>
  );
}

/** Readiness judges the pairs with a batch or soak run (a one-shot compare is no pair's cycle). */
const assessed = (run: Run) => run.mode !== 'one_shot';

/** Where the run's pair stands today (docs/mvp/READINESS.md §10); nothing for a name readiness does not judge. */
function PairReadiness({ name, version }: { name: string; version: string }) {
  const pair = useLoad(() => api.readinessPair(name), [name, version]).data;
  if (!pair) return null;
  return (
    <p className="muted">
      <a href={href(['readiness', name])}>{runLine(pair)}</a> · <a href={href(['readiness', name, 'brief'])}>cut-over brief</a>
    </p>
  );
}

type HeaderProps = { run: RunDetail; cycle?: string; viewed: number; reload: () => void };

function Header({ run, cycle, viewed, reload }: HeaderProps) {
  const [error, setError] = useState<string>();
  const { can } = useWho();
  const licence = useLicence();
  // A run paused for the licence resumes once it allows runs (and seats its name: the server says).
  const refused = run.stop_reason === LICENCE_PAUSE ? refusal(licence, 'runs') : undefined;
  const act = (done: Promise<unknown>) => {
    setError(undefined);
    done.catch((e) => setError(message(e)));
  };
  const { keys = {}, outcomes = {} } = run.progress ?? {};
  const total = Object.values(keys).reduce((a, b) => a + b, 0);
  const finished = (keys.done ?? 0) + (keys.cancelled ?? 0);
  // the scan lines are the current cycle's; an older cycle's scan is in its reports
  const current = cycle && Number(cycle) !== run.cycle ? `Cycle ${run.cycle} (current): ` : '';
  return (
    <section>
      <p className="muted">
        <a href={href(['specs', run.spec_id])}>{run.spec_name}</a>
        {` ${run.spec_version} · ${run.mode} · cycle ${run.cycle} · created ${when(run.created_at)}`}
        {run.created_by && <> by <Named name={run.created_by_name} actor={run.created_by} /></>}
        {run.finished_at && ` · finished ${when(run.finished_at)}`}
        {run.stop_reason && ` · stopped: ${run.stop_reason}`}
      </p>
      {!isPurged(run, viewed) && <p className="muted">{retainedLine(run, viewed)}</p>}
      <p className={['approved', 'not_needed'].includes(run.approval.state) ? 'muted' : 'warn'}>
        {approvalLabel(run.approval)}
        {run.approval.approval_id && <> <a href={href(['approvals', run.approval.approval_id])}>review</a></>}
      </p>
      {run.licence && (
        <p className={['evaluation', 'grace'].includes(run.licence.state) ? 'warn' : 'muted'}>{run.licence.label}</p>
      )}
      {pausedLine(run) && <p className="warn">{pausedLine(run)}</p>}
      {guardBanner(run.schema) && <p className="banner" role="status">{guardBanner(run.schema)}</p>}
      {guardLines(run.schema).map((line) => <p key={line} className="muted">{line}</p>)}
      {run.schedule && <p className="muted"><a href={href(['schedules', run.schedule.schedule_id])}>{run.schedule.text}</a></p>}
      {assessed(run) && <PairReadiness name={run.spec_name} version={`${run.status}/${run.cycle}`} />}
      {run.error && <p className="error">{run.error}</p>}
      <p className="inline">
        {can('operator') && ACTIONS[run.status]?.map((a) => (
          <button
            key={a}
            disabled={a === 'resume' && !!refused}
            title={a === 'resume' ? refused : undefined}
            onClick={() => act(api.control(run.run_id, a).then(reload))}
          >
            {controlLabel(a, run.schema)}
          </button>
        ))}
        {REPORTS.map((name) => (
          <button key={name} onClick={() => act(api.report(run.run_id, name, cycle).then((b) => save(b, reportFile(run, name))))}>
            {name}
          </button>
        ))}
      </p>
      <Err error={error} />
      {run.scan && <p>{current}{scanLine(run, run.scan)}</p>}
      {censusNote(run) && <p className="warn">{current}{censusNote(run)}</p>}
      {run.scan?.graph && <p>{current}{graphLine(run.scan.graph)}</p>}
      {total > 0 && (
        <p>
          <progress value={finished} max={total} /> {finished}/{total} keys ·{' '}
          {Object.entries(keys).map(([state, n]) => `${state} ${n}`).join(' · ')}
        </p>
      )}
      <p className="inline">
        {Object.entries(outcomes).map(([o, n]) => (
          <a key={o} href={href(['runs', run.run_id], { cycle, outcome: o })}>
            <Badge v={o} /> {n}
          </a>
        ))}
      </p>
      <details>
        <summary>What the outcomes mean</summary>
        <ul>{OUTCOMES.map((o) => <li key={o}><Badge v={o} /> {LEGEND[o]}</li>)}</ul>
      </details>
      <details>
        <summary>Counters</summary>
        <Stats values={run.counters} />
      </details>
    </section>
  );
}

const num = (v: number) => v.toLocaleString();

/** A table scan (docs/mvp/TABLES.md §6, §9), while it runs and after: what it read and what it left, and each
 *  table side's engine where the plan names it (docs/mvp/ENGINES.md §13); a file side's files in its place
 *  (docs/mvp/FILES.md §16). */
function scanLine(run: Run, scan: Scan): string {
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

/** Bulk graph parity (docs/mvp/GRAPH.md §4): the groups whose members differ and the components that changed. */
function graphLine(g: Graph): string {
  return (
    `Graph: ${num(g.groups_a)} groups on A, ${num(g.groups_b)} on B; ${num(g.groups_different)} differ; ` +
    `${num(g.split)} split, ${num(g.merge)} merge, ${num(g.rearrange)} rearranged, ${num(g.oversized)} too large to expand.`
  );
}

function FilterForm({ run, f }: { run: Run; f: Filters }) {
  function apply(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const values = Object.fromEntries([...new FormData(e.currentTarget)].map(([k, v]) => [k, String(v)]));
    location.hash = href(['runs', run.run_id], values);
  }
  const submit = (e: { currentTarget: HTMLSelectElement }) => e.currentTarget.form?.requestSubmit();
  const select = (name: string, label: string, options: (string | number)[]) => (
    <label>
      {label}{' '}
      <select name={name} defaultValue={f[name] ?? ''} onChange={submit}>
        <option value="">any</option>
        {options.map((o) => <option key={o}>{o}</option>)}
      </select>
    </label>
  );
  return (
    <form className="panel inline" key={JSON.stringify(f)} onSubmit={apply}>
      {run.mode === 'soak' && select('cycle', 'Cycle', Array.from({ length: run.cycle }, (_, i) => i + 1))}
      {select('outcome', 'Outcome', OUTCOMES)}
      {select('severity', 'Severity', SEVERITIES)}
      {['key', 'taxonomy_code', 'path_pattern', 'root_cause_id'].map((name) => (
        <label key={name}>
          {name}{' '}
          <input name={name} defaultValue={f[name]} size={14} />
        </label>
      ))}
      <button>Filter</button>
      <a href={href(['runs', run.run_id])}>clear</a>
    </form>
  );
}

type TableProps = { runId: string; f: Filters; version: string; keyLink: KeyLink };

type Subject = string | null; // a root cause's id; null: the run
type Draft = { subject: Subject; cycle: number; session: Session };

/** The root causes, with `Explain` on each and `Summarise this run`: an AI draft answering a fixed question
 *  (docs/mvp/CONTRACTS.md §6), shown under the table beside the engine's stored facts. One draft is open per
 *  run and cycle, and the browser tab keeps which. */
function RootCauses({ runId, f, version, keyLink, run, ai }: TableProps & { run: RunDetail; ai?: boolean }) {
  const causes = useLoad(() => api.rootCauses(runId, f.cycle), [runId, f.cycle, version]);
  const scope = `ds.explain:${runId}:${run.cycle}`;
  const [draft, setDraft] = useState<Draft>();
  const [asking, setAsking] = useState<Subject>(); // undefined: nothing is asked
  const [error, setError] = useState<string>();
  const rows = causes.data?.items;
  const viewed = Number(f.cycle ?? run.cycle);
  const reason = off(ai, run, viewed, rows?.length);

  useEffect(() => {
    const kept = JSON.parse(sessionStorage.getItem(scope) ?? 'null') as { subject: Subject; session: string } | null;
    if (!kept) return;
    const drop = () => sessionStorage.removeItem(scope);
    api.chat(kept.session).then(
      (session) => (answerOf(session.messages) ? setDraft({ subject: kept.subject, cycle: run.cycle, session }) : drop()),
      (e) => e instanceof ApiError && [404, 410].includes(e.status) && drop(), // it went with its cycle's evidence
    );
  }, [scope]); // the stored draft, once: only a click asks

  async function ask(subject: Subject) {
    setAsking(subject);
    setError(undefined);
    try {
      const session = await api.explain(runId, subject ?? undefined);
      sessionStorage.setItem(scope, JSON.stringify({ subject, session: session.session_id }));
      setDraft({ subject, cycle: run.cycle, session });
    } catch (e) {
      setError(message(e));
    } finally {
      setAsking(undefined);
    }
  }

  function close() {
    sessionStorage.removeItem(scope);
    setDraft(undefined);
  }

  const button = (subject: Subject, label: string) => (
    <button disabled={!!reason || asking !== undefined} title={reason} onClick={() => ask(subject)}>
      {asking === subject ? 'Thinking…' : label}
    </button>
  );
  const cols: Col<RootCause>[] = [
    ['Root cause', (c) => <a href={href(['runs', runId], { ...f, root_cause_id: c.root_cause_id })}>{c.root_cause_id}</a>],
    ['Severity', (c) => <Badge v={c.max_severity} />],
    ['Taxonomy', (c) => c.taxonomy_code],
    ['Path', (c) => <code>{c.path_pattern}</code>],
    ['Change', (c) => `${c.change_type} (${c.direction})`],
    ['Keys', (c) => c.affected_keys],
    ['Findings', (c) => c.finding_count],
    ['Sample keys', (c) => c.sample_keys.map((k) => <span key={k}>{keyLink(k)} </span>)],
  ];
  return (
    <section>
      <div className="inline">
        <h2>Root causes</h2>
        {button(null, 'Summarise this run')}
      </div>
      {reason && <p className="muted">{reason}</p>}
      <Err error={error} />
      <Err error={causes.error} />
      <Table rows={rows ?? []} cols={[...cols, ['', (c) => button(c.root_cause_id, 'Explain')]]} rowId={(c) => c.root_cause_id} />
      {draft && rows && draft.cycle === run.cycle && viewed === run.cycle && (
        <Drafted run={run} rows={rows} cols={cols} draft={draft}>
          {button(draft.subject, 'Ask again')} <button onClick={close}>Close</button>
        </Drafted>
      )}
    </section>
  );
}

type DraftedProps = { run: RunDetail; rows: RootCause[]; cols: Col<RootCause>[]; draft: Draft; children: ReactNode };

/** An open draft: the engine's stored facts (the root cause's row, or the run's counters), then the answer with
 *  its citations as links or, for a refusal, its message as plain text. Nothing once its root cause left the table. */
function Drafted({ run, rows, cols, draft, children }: DraftedProps) {
  const { subject, cycle, session } = draft;
  const answer = answerOf(session.messages);
  const row = rows.find((c) => c.root_cause_id === subject);
  if (!answer || (subject && !row)) return null;
  return (
    <div className="panel" role="status">
      <h3>{refused(answer) ? NO_DRAFT : draftTitle(subject)}</h3>
      <p className="muted">{FACTS}</p>
      {row ? (
        <Table rows={[row]} cols={cols} />
      ) : (
        <>
          <p>{runFacts(run, rows.length)}</p>
          {run.scan && <p>{scanLine(run, run.scan)}</p>}
          {firstOnly(rows.length) && <p className="warn">{firstOnly(rows.length)}</p>}
        </>
      )}
      {refused(answer) ? (
        <p className="pre">{answer.content}</p>
      ) : (
        <>
          <p className="muted">{NOT_EVIDENCE}</p>
          <p className="pre">
            <CitedText text={answer.content} runId={run.run_id} keys={citedKeys(session.messages)} cycle={cycle} />
          </p>
        </>
      )}
      <p className="inline muted">asked {when(answer.created_at ?? null)} {children}</p>
    </div>
  );
}

function Results({ runId, f, version, keyLink }: TableProps) {
  const q = { cycle: f.cycle, outcome: f.outcome };
  const results = usePaged((cursor) => api.results(runId, { ...q, cursor }), [runId, JSON.stringify(q), version]);
  const cols: Col<Result>[] = [
    ['Key', (r) => keyLink(r.key_masked)],
    ['Outcome', (r) => <Badge v={r.outcome} />],
    ['Why', (r) => r.inconclusive_reason ?? r.drift_reason],
    ['Confirmation', (r) => r.confirmation],
    ['A', (r) => [r.side_a_state, r.side_a_reason].filter(Boolean).join(': ')],
    ['B', (r) => [r.side_b_state, r.side_b_reason].filter(Boolean).join(': ')],
  ];
  return (
    <section>
      <h2>Results</h2>
      <Err error={results.error} />
      <Table rows={results.items} cols={cols} />
      {results.more && <button onClick={results.more}>More</button>}
    </section>
  );
}

const FINDING_COLS: Col<Finding>[] = [
  ['Finding', (x) => <span className="mono muted">{x.finding_id}</span>],
  ['Pointer', (x) => <code>{x.json_pointer || '/'}</code>],
  ['Change', (x) => x.change_type],
  ['Severity', (x) => <Badge v={x.severity} />],
  ['Taxonomy', (x) => x.taxonomy_code],
  ['A', (x) => <code>{cell(x.value_a)}</code>],
  ['B', (x) => <code>{cell(x.value_b)}</code>],
  ['Class', (x) => x.value_class],
  ['Rule', (x) => [x.rule_id, x.suppressed && '(suppressed)'].filter(Boolean).join(' ')],
];

export const findingCols = (keyLink?: KeyLink): Col<Finding>[] =>
  keyLink ? [['Key', (x) => keyLink(x.key_masked ?? '')], ...FINDING_COLS] : FINDING_COLS;

function Findings({ runId, f, version, keyLink }: TableProps) {
  const { cycle, key, root_cause_id, taxonomy_code, path_pattern, severity } = f;
  const q = { cycle, key, root_cause_id, taxonomy_code, path_pattern, severity };
  const findings = usePaged((cursor) => api.findings(runId, { ...q, cursor }), [runId, JSON.stringify(q), version]);
  return (
    <section>
      <h2>Findings</h2>
      <Err error={findings.error} />
      <Table rows={findings.items} cols={findingCols(keyLink)} rowId={(x) => x.finding_id} />
      {findings.more && <button onClick={findings.more}>More</button>}
    </section>
  );
}

function Changes({ runId, version }: { runId: string; version: string }) {
  const changes = useLoad(() => api.changes(runId), [runId, version]);
  const cols: Col<Change>[] = [
    ['Cycle', (c) => c.cycle],
    ['Key', (c) => <a href={href(['runs', runId, 'keys', c.key_masked], { cycle: c.cycle })}>{c.key_masked}</a>],
    ['From', (c) => <Badge v={c.from_outcome} />],
    ['To', (c) => <Badge v={c.to_outcome} />],
  ];
  return (
    <section>
      <h2>Outcome changes across cycles</h2>
      <Err error={changes.error} />
      <Table rows={changes.data?.items ?? []} cols={cols} />
    </section>
  );
}

const pct = (share: string | null) => (share === null ? '' : percent(share));
const STRATUM_COLS: Col<Stratum>[] = [
  ['Stratum', (s) => s.label],
  ['Tier', (s) => `${s.tier}, ≤ ${percent(s.target)}`],
  ['Basis', (s) => s.basis],
  ['Frame keys', (s) => num(s.frame_keys)],
  ['Drawn', (s) => num(s.drawn)],
  ['Done', (s) => (s.done === undefined ? '' : num(s.done))],
  ['Compared', (s) => num(s.n)],
  ['Discrepant', (s) => num(s.x)],
  ['Outstanding', (s) => num(s.counters.outstanding)],
  ['Unresolved', (s) => num(s.unresolved)],
  ['Absent both', (s) => num(s.absent_both)],
  ['Truncated', (s) => num(s.api_uncomparable)],
  ['Retrying', (s) => (s.retrying === undefined ? num(s.retried) : num(s.retrying))],
  ['Bound', (s) => pct(s.ub95_one_sided)],
  ['Worst case', (s) => pct(s.ub95_one_sided_worst_case)],
  ['Verdict', (s) => <Badge v={s.verdict} />],
];

/** A sampled run's draw (docs/mvp/SAMPLING.md §14), polled while live: its frame, each stratum's progress, interim
 *  bound and certain failures, an ETA; once ended, its sign-off precheck; its population's ledger; and, for an
 *  operator signed in under SSO, the close of the name's draws that did not pass, with a note of the fix. */
function Sample({ run }: { run: RunDetail }) {
  const { can, me } = useWho();
  const [error, setError] = useState<string>();
  const [done, setDone] = useState<string>();
  const sample = run.sample!;
  const drawId = sample.draw_id ?? [...run.cycles].reverse().find((c) => c.sample)?.sample?.draw.draw_id;
  const found = useLoad(() => (drawId ? api.sample(drawId) : Promise.resolve(undefined)), [drawId]);
  const d = found.data;
  usePoll(found.reload, 5000, d?.status === 'live');
  const closer = me?.mode === 'oidc' && me?.kind === 'user' && can('operator');

  async function close() {
    const note = prompt(`${CLOSE_TITLE} of ${run.spec_name}: what was fixed? (no customer keys)`);
    if (!note) return;
    setError(undefined);
    try {
      const { closed } = await api.closeDraws(run.spec_name, note);
      setDone(`Closed ${closed.join(', ')}.`);
      found.reload();
    } catch (e) {
      setError(message(e));
    }
  }

  const ledgerCols: Col<LedgerDraw>[] = [
    ['Draw', (x) => <span className={x.draw_id === d?.draw_id ? 'mono' : 'mono muted'}>{x.draw_id}</span>],
    ['Spec', (x) => x.spec_name],
    ['Run', (x) => <a href={href(['runs', x.run_id])}>{`${x.run_id} cycle ${x.cycle}`}</a>],
    ['Drawn', (x) => when(x.drawn_at)],
    ['Status', (x) => <><Badge v={x.status} />{x.method === 'rate' && ' monitoring'}{x.blocking && ' blocking'}</>],
    ['Draw and close', (x) => ledgerLine(x)],
  ];
  return (
    <section>
      <h2>Sample</h2>
      <p className={sample.error ? 'warn' : 'muted'}>{drawState(sample) ?? 'No draw yet.'}</p>
      <Err error={found.error} />
      {d && (
        <>
          <p className="muted">{drawLine(d)}</p>
          <p>{frameLine(d.frame)}</p>
          {d.progress && <p>{etaLine(d.progress, d.keys)}</p>}
          {d.progress && certainFailures(d.progress).map((line) => <p key={line} className="error">{line}</p>)}
          <Table rows={d.progress?.strata ?? run.cycles.find((c) => c.cycle === d.cycle)?.sample?.judgement?.strata ?? []}
            cols={STRATUM_COLS} rowId={(s) => `stratum-${s.label}`} />
          {d.progress && <p className="muted">Bounds are interim until the cycle ends; no bound is given for the strata pooled.</p>}
          {blockedLines(d).map((line) => <p key={line} className="warn">{line}</p>)}
          {d.precheck && (
            <>
              <h3>Sign-off precheck</h3>
              {d.precheck.length ? d.precheck.map((line) => <p key={line} className="warn">{line}</p>) : <p>Nothing refuses its sign-off.</p>}
            </>
          )}
          <h3>Draws of this population</h3>
          <Table rows={d.ledger} cols={ledgerCols} rowId={(x) => `draw-${x.draw_id}`} />
          {closer && d.ledger.some((x) => x.blocking) && <button onClick={close}>{CLOSE_TITLE}</button>}
        </>
      )}
      {done && <p role="status">{done}</p>}
      <Err error={error} />
    </section>
  );
}

/** Each ended cycle's sign-off requests and what they say; under SSO an operator asks for a cycle's here. */
function Signoffs({ run, reload }: { run: RunDetail; reload: () => void }) {
  const { can, me } = useWho();
  const [error, setError] = useState<string>();
  const refused = refusal(useLicence(), 'signoff');
  const { cycles, purged } = run.retention;
  const ended = [...new Set([...cycles.map((c) => c.cycle), ...purged])].sort((x, y) => x - y);
  if (!ended.length) return null;
  const of = (cycle: number) => run.signoffs.filter((s) => s.cycle === cycle);
  const open = (cycle: number) =>
    !isPurged(run, cycle) && !of(cycle).some((s) => s.status === 'requested' || s.status === 'approved');

  async function request(cycle: number) {
    const note = prompt(`Ask for a sign-off of cycle ${cycle}. A note for the approver (optional):`);
    if (note === null) return;
    setError(undefined);
    try {
      location.hash = href(['approvals', (await api.requestSignoff(run.run_id, cycle, note || undefined)).approval_id]);
    } catch (e) {
      setError(message(e));
      reload();
    }
  }

  const cols: [string, (cycle: number) => ReactNode][] = [
    ['Cycle', (cycle) => cycle],
    ['Requests', (cycle) => of(cycle).map((s: Approval) => (
      <span key={s.approval_id}>
        <a className="mono" href={href(['approvals', s.approval_id])}>{s.approval_id}</a> <Badge v={s.status} />{' '}
      </span>
    ))],
    ['Sign-off', (cycle) => signoffLabel(of(cycle)) ?? 'Not signed off.'],
  ];
  if (me?.mode === 'oidc' && can('operator'))
    cols.push(['', (cycle) => open(cycle) && (
      <button disabled={!!refused} title={refused} onClick={() => request(cycle)}>Request sign-off</button>
    )]);
  return (
    <section>
      <h2>Sign-off</h2>
      <p className="muted">
        A sign-off accepts one ended cycle as parity evidence, once someone who neither wrote its spec, started this run
        nor asked for it approved its facts.{me?.mode !== 'oidc' && ` ${ssoOnly(me?.mode)}`}
      </p>
      <Table rows={ended} cols={cols} rowId={(cycle) => `signoff-${cycle}`} />
      <Err error={error} />
    </section>
  );
}

/** Each ended cycle's census, adjudications and evidence packs (docs/mvp/PACK.md §7.3). Under SSO an operator
 *  draws a reviewer sheet and scores its reviewed upload before the cycle's sign-off is requested, which then
 *  attests it; a signed-in person issues the signed-off cycle's pack, which anyone who reads the run downloads. */
function Evidence({ run, reload }: { run: RunDetail; reload: () => void }) {
  const { can, me } = useWho();
  const [error, setError] = useState<string>();
  const [done, setDone] = useState<string>();
  const refused = refusal(useLicence(), 'packs');
  if (!run.cycles.length) return null;
  const operator = me?.mode === 'oidc' && can('operator');

  async function act(work: () => Promise<string | void>) {
    setError(undefined);
    setDone(undefined);
    try {
      const said = await work();
      if (said) setDone(said);
    } catch (e) {
      setError(message(e));
    }
    reload();
  }
  const draw = (cycle: number) =>
    act(async () => {
      const limit = prompt(`Draw a reviewer sheet of cycle ${cycle}: how many per-key results (1 to 500)?`, '50');
      if (limit === null) return;
      const drawn = await api.draw(run.run_id, cycle, Number(limit));
      return `Drew ${drawn.adjudication_id}: ${drawn.keys.length} key(s) to review. Download the sheet, fill in reviewer_verdict, then upload it.`;
    });
  const chosen = (j: Adjudication, input: HTMLInputElement) => {
    const file = input.files?.[0];
    input.value = ''; // the same file chosen again, once corrected, is sent again
    if (file) act(async () => `Scored ${j.adjudication_id}: ${scoreLine(await api.score(j.adjudication_id, file))}.`);
  };
  const issue = (cycle: number) =>
    act(async () => {
      if (!confirm(`Issue the evidence pack of cycle ${cycle}, signed with this installation's key?`)) return;
      const issued = await api.issuePack(run.run_id, cycle);
      return `Issued ${issued.pack_id}, signed by the key with fingerprint ${issued.key_fingerprint}.`;
    });

  const adjudicationCols = (cycle: number): Col<Adjudication>[] => [
    ['Adjudication', (j) => <span className="mono">{j.adjudication_id}</span>],
    ['Drawn', (j) => <><Named name={j.drawn_by_name} actor={j.drawn_by} /> {when(j.drawn_at)}</>],
    ['Sample', (j) => `up to ${j.sample_limit} of ${j.selection.candidates} per-key results`],
    ['Score', (j) => (
      <>
        {scoreLine(j)}
        {j.scored_by && <> · by <Named name={j.scored_by_name} actor={j.scored_by} /> {when(j.scored_at)}</>}
      </>
    )],
    ['', (j) => !isPurged(run, cycle) && (
      <span className="inline">
        <button onClick={() => act(async () => save(await api.sheet(j.adjudication_id), `${j.adjudication_id}.csv`))}>
          Download sheet
        </button>
        {operator && !j.scored_at && unfrozen(run, cycle) && (
          <label>
            Upload reviewed sheet{' '}
            <input type="file" accept=".csv,text/csv" onChange={(e) => chosen(j, e.currentTarget)} />
          </label>
        )}
      </span>
    )],
  ];
  const packCols: Col<Pack>[] = [
    ['Pack', (p) => <span className="mono">{p.pack_id}</span>],
    ['Status', (p) => <Badge v={p.status} />],
    ['Issued', (p) => <><Named name={p.issued_by_name} actor={p.issued_by} /> {when(p.issued_at)}</>],
    ['Key', (p) => <span className="mono">{p.key_fingerprint}</span>],
    ['Revoked', (p) => p.revoked_at && <><Named name={p.revoked_by_name} actor={p.revoked_by} /> {when(p.revoked_at)}: {p.revoke_reason}</>],
    ['', (p) => p.purged_at
      ? `stored copy deleted ${when(p.purged_at)}; copies downloaded before still verify`
      : <button onClick={() => act(async () => save(await api.downloadPack(p.pack_id), `${p.pack_id}.zip`))}>Download</button>],
  ];

  return (
    <section>
      <h2>Census, adjudication and evidence packs</h2>
      <p className="muted">
        An evidence pack is one signed ZIP per signed-off census cycle that an auditor checks offline with stock tools and
        the site's key register. From 50 per-key results, the sign-off must attest a reviewer sheet a person scored,
        drawn and scored before the sign-off is requested.{me?.mode !== 'oidc' && ` ${packsSsoOnly(me?.mode)}`}
      </p>
      {run.cycles.map((c) => {
        const purged = isPurged(run, c.cycle);
        return (
          <div className="panel" key={c.cycle} id={`evidence-${c.cycle}`}>
            <h3>Cycle {c.cycle}</h3>
            <p>{purged ? purgedBanner(c.cycle) : c.census && `Census: ${censusLine(c.census)}`}</p>
            {!purged && c.census && censusWarnings(c.census).map((w) => <p className="warn" key={w}>{w}</p>)}
            <h4>Adjudications</h4>
            <Table rows={c.adjudications} cols={adjudicationCols(c.cycle)} rowId={(j) => j.adjudication_id} />
            <h4>Evidence packs</h4>
            <Table rows={c.packs} cols={packCols} rowId={(p) => p.pack_id} />
            <p className="inline">
              {operator && !purged && unfrozen(run, c.cycle) && <button onClick={() => draw(c.cycle)}>Draw</button>}
              {operator && me?.kind === 'user' && !purged && signedOff(run, c.cycle) && (
                <button disabled={!!refused} title={refused} onClick={() => issue(c.cycle)}>Issue evidence pack</button>
              )}
            </p>
          </div>
        );
      })}
      {done && <p role="status">{done}</p>}
      <Err error={error} />
    </section>
  );
}

/** Each ended cycle's labelled accuracy (docs/mvp/ACCURACY.md §10), in every auth mode: an operator draws a sample
 *  against a named truth source and uploads its filled sheet once, before the cycle's sign-off is requested, which
 *  attests the newest labelling a person labelled. Every figure, warning and limit shown is the server's text. */
function Accuracy({ run, reload }: { run: RunDetail; reload: () => void }) {
  if (!run.cycles.length) return null;
  return (
    <section>
      <h2>Labelled accuracy</h2>
      {run.cycles.map((c) => <Labellings key={c.cycle} run={run} cycle={c} reload={reload} />)}
    </section>
  );
}

const SIZES = [['size', 'Discrepant items', 1000, 100], ['per_cause', 'Per root cause', 20, 2],
  ['accepted', 'Accepted keys', 500, 0], ['agreed', 'Agreed keys', 500, 0]] as const;

function Labellings({ run, cycle: c, reload }: { run: RunDetail; cycle: CycleEvidence; reload: () => void }) {
  const { can } = useWho();
  const [error, setError] = useState<string>();
  const [drawn, setDrawn] = useState<DrawnLabelling>();
  const [unlocatable, setUnlocatable] = useState(false); // the server asked for the acknowledgement
  const printed = stated(c.labellings);
  const labelled = c.labellings.filter((v) => v.labelled_at !== null).length; // its lines count the cycle's
  const said = useLoad(
    () => (printed ? api.labelling(printed.labelling_id) : Promise.resolve(undefined)),
    [printed?.labelling_id, printed?.attested, c.labellings.length, labelled],
  );
  const operator = can('operator');

  async function act(work: () => Promise<unknown>) {
    setError(undefined);
    try {
      await work();
    } catch (e) {
      if (needsAcknowledgement(e)) setUnlocatable(true);
      setError(message(e));
    }
    reload();
  }
  function draw(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const sizes = Object.fromEntries(SIZES.map(([name]) => [name, Number(f.get(name))]));
    const truth = { source: String(f.get('source')), as_of: String(f.get('as_of')) };
    const acknowledge_unlocatable = f.has('acknowledge') || undefined;
    act(async () => setDrawn(await api.drawLabelling(run.run_id, c.cycle, { ...sizes, truth, acknowledge_unlocatable })));
  }
  const chosen = (v: Labelling, input: HTMLInputElement) => {
    const file = input.files?.[0];
    input.value = ''; // the same file chosen again, once corrected, is sent again
    if (file) act(() => api.label(v.labelling_id, file));
  };

  const cols: Col<Labelling>[] = [
    ['Labelling', (v) => <span className="mono">{v.labelling_id}</span>],
    ['What it says', (v) => v.summary],
    ['', (v) => !isPurged(run, c.cycle) && (
      <span className="inline">
        <button onClick={() => act(async () => save(await api.labellingSheet(v.labelling_id), `${v.labelling_id}.csv`))}>
          Download sheet
        </button>
        {operator && takesLabels(run, v) && (
          <label>
            Upload labels{' '}
            <input type="file" accept=".csv,text/csv" onChange={(e) => chosen(v, e.currentTarget)} />
          </label>
        )}
      </span>
    )],
  ];
  return (
    <div className="panel" id={`accuracy-${c.cycle}`}>
      <h3>Cycle {c.cycle}</h3>
      <Table rows={c.labellings} cols={cols} rowId={(v) => v.labelling_id} />
      {operator && takesLabelling(run, c.cycle) && (
        <form className="inline" onSubmit={draw}>
          {SIZES.map(([name, label, max, value]) => (
            <label key={name}>
              {label} <input name={name} type="number" min={0} max={max} defaultValue={value} required />
            </label>
          ))}
          <label>Truth source <input name="source" size={28} maxLength={64} required /></label>
          <label>As of <input name="as_of" type="date" required /></label>
          {unlocatable && <label><input name="acknowledge" type="checkbox" /> acknowledge_unlocatable</label>}
          <button>Draw</button>
          <span className="muted">{DRAW_HINT}</span>
        </form>
      )}
      {drawn && (
        <>
          <p role="status">Drew {drawn.labelling_id}: download its sheet, label every item, then upload it.</p>
          {drawn.warnings.map((line) => <p key={line} className="warn">{line}</p>)}
          <p className="inline">
            {drawn.items.map((i) => (
              <a key={i.item} href={href(['runs', run.run_id, 'keys', i.key], { cycle: c.cycle })}>{i.key}</a>
            ))}
          </p>
        </>
      )}
      <Err error={error ?? said.error} />
      {said.data?.lines.map((line) => <p key={line}>{line}</p>)}
      {said.data && <p className="muted">{said.data.limits}</p>}
    </div>
  );
}
