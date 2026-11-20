/** A run. Its head: progress (polled while it is live), its keys by outcome as a bar, what its spec approval and
 *  its licence say, the schedule that started it, what the schema guard says of its tables (a banner while it
 *  holds the run paused), where its pair stands on the readiness ladder, its controls and its reports. Under it,
 *  one tab at a time: the keys that do not match, by outcome; root causes with an AI draft about one of them or
 *  about the run; findings; all keys; soak changes; a sampled run's draw, each ended cycle's sign-off, its census,
 *  adjudications and evidence packs; and its labelled accuracy. The tab and the filters live in the URL, so
 *  root-cause links and citation chips are plain links. A purged cycle shows a banner instead of its tables,
 *  which would answer 410. */
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
import { useDialogs } from './Dialogs';
import { FACTS, NOT_EVIDENCE, NO_DRAFT, answerOf, citedKeys, draftTitle, firstOnly, off, refused, runFacts } from './explain';
import { label, tone } from './labels';
import { LICENCE_PAUSE, pausedLine, refusal, reportFile } from './licensing';
import { href, message, useLicence, useLoad, usePaged, usePoll, useWho } from './lib';
import { DIFFERING, barOf, changesTitle, noChanges, notMatching, segments, tabOf, tabsOf, type Tab } from './outcomes';
import { censusLine, censusNote, censusWarnings, fileSide, packsSsoOnly, scoreLine, signedOff, unfrozen } from './packs';
import { runLine } from './ready';
import { controlLabel, guardBanner, guardLines } from './prechecking';
import { chatClosed, isPurged, purgedBanner, retainedLine } from './retention';
import { CLOSE_TITLE, blockedLines, certainFailures, drawLine, drawState, etaLine, frameLine, ledgerLine, percent } from './samples';
import { Badge, Copy, Err, Menu, MoreRows, Named, OutcomeBar, Stats, Table, Tabs, When, cell, save, when, type Col } from './ui';

const TERMINAL = ['cancelled', 'completed', 'completed_with_issues', 'failed'];
const ACTIONS: Record<string, string[]> = { queued: ['cancel'], running: ['pause', 'cancel'], paused: ['resume', 'cancel'] };
const CONTROLS: Record<string, string> = { pause: 'Pause run', resume: 'Resume run', cancel: 'Cancel run' };
const CONTROL_TONE: Record<string, string> = { resume: 'primary', cancel: 'danger' };
// reports.FORMATS, as GET /v1/runs/{id}/reports/{name} serves them
const REPORTS = ['report.pdf', 'discrepancies.csv', 'retry.csv', 'coverage.csv', 'normalization.csv', 'root_causes.csv',
  'suppressed.csv', 'graph.csv', 'readiness.csv'];
const SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO'];
const OUTCOMES = ['MATCH', 'MISMATCH', 'KNOWN_DIFF', 'NOISE_ONLY', 'DRIFT', 'NOT_FOUND_A', 'NOT_FOUND_B', 'INCONCLUSIVE'];
// What each outcome says of a key: the legend under a run's outcome badges.
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
        {r ? `${r.spec_name} ${r.spec_version}` : `Run ${runId}`} <Badge v={r?.status} />
      </h1>
      <Err error={run.error} />
      {run.loading && <p className="muted" role="status">Loading…</p>}
      {r && (
        <>
          <Header run={r} cycle={f.cycle} viewed={viewed} reload={run.reload} />
          <Parts run={r} f={f} version={version} viewed={viewed} keyLink={keyLink} ai={ai} reload={run.reload} />
          <Chat runId={runId} closed={chatClosed(r)} ai={ai} />
        </>
      )}
    </>
  );
}

const DATA_TABS: Tab[] = ['differences', 'causes', 'findings', 'keys', 'changes']; // what a purge removes

type PartsProps = { run: RunDetail; f: Filters; version: string; viewed: number; keyLink: KeyLink; ai?: boolean; reload: () => void };

/** The run's tabs and the one the address opens. The count beside the first is the current cycle's. */
function Parts({ run, f, version, viewed, keyLink, ai, reload }: PartsProps) {
  const runId = run.run_id;
  const tab = tabOf(f, run.mode);
  const outcomes = viewed === run.cycle ? run.progress?.outcomes : undefined;
  const counted = segments(outcomes).length > 0; // no count before the cycle's first key has an outcome
  const counts: Partial<Record<Tab, number>> = counted ? { differences: notMatching(outcomes) } : {};
  const table = { runId, f, version, keyLink, cycles: run.mode === 'soak' ? run.cycle : 0 };
  return (
    <>
      <Tabs
        name="Parts of this run"
        current={tab}
        items={tabsOf(run.mode).map(([id, text]) => [
          href(['runs', runId], { cycle: f.cycle, tab: id }),
          <>{text}{counts[id] !== undefined && <span className="count"> {num(counts[id])}</span>}</>,
          id,
        ])}
      />
      {DATA_TABS.includes(tab) && isPurged(run, viewed) ? (
        <p className="banner" role="status">{purgedBanner(viewed)}</p>
      ) : tab === 'differences' ? (
        <Differences {...table} />
      ) : tab === 'causes' ? (
        <RootCauses {...table} run={run} ai={ai} />
      ) : tab === 'findings' ? (
        <Findings {...table} />
      ) : tab === 'keys' ? (
        <Results {...table} />
      ) : tab === 'changes' ? (
        <Changes {...table} viewed={viewed} />
      ) : null}
      {tab === 'evidence' && (
        <>
          {!run.sample && !run.cycles.length && !run.retention.cycles.length && !run.retention.purged.length && (
            <p className="muted">Nothing yet: sign-off and evidence start once a cycle of this run has ended.</p>
          )}
          {run.sample && <Sample run={run} />}
          <Signoffs run={run} reload={reload} />
          <Evidence run={run} reload={reload} />
        </>
      )}
      {tab === 'accuracy' && (
        run.cycles.length ? <Accuracy run={run} reload={reload} /> : <p className="muted">Nothing yet: a cycle is labelled once it has ended.</p>
      )}
    </>
  );
}

/** Readiness judges the pairs with a batch or soak run (a one-shot compare is no pair's cycle). */
const assessed = (run: Run) => run.mode !== 'one_shot';

/** Where the run's pair stands today; nothing for a name readiness does not judge. */
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
  const { confirm } = useDialogs();
  const licence = useLicence();
  // A run paused for the licence resumes once it allows runs (and seats its name: the server says).
  const refused = run.stop_reason === LICENCE_PAUSE ? refusal(licence, 'runs') : undefined;
  const act = (done: Promise<unknown>) => {
    setError(undefined);
    done.catch((e) => setError(message(e)));
  };
  async function control(action: string) {
    const stop = action === 'cancel' && {
      title: `Cancel run ${run.run_id}?`,
      body: 'It stops for good: a cancelled run cannot be resumed.',
      action: 'Cancel run',
      dismiss: 'Keep run',
      danger: true,
    };
    if (stop && !(await confirm(stop))) return;
    act(api.control(run.run_id, action).then(reload));
  }
  const { keys = {}, outcomes = {} } = run.progress ?? {};
  const total = Object.values(keys).reduce((a, b) => a + b, 0);
  const finished = (keys.done ?? 0) + (keys.cancelled ?? 0);
  // the scan lines are the current cycle's; an older cycle's scan is in its reports
  const current = cycle && Number(cycle) !== run.cycle ? `Cycle ${run.cycle} (current): ` : '';
  return (
    <section>
      <p className="muted">
        Run <span className="mono">{run.run_id}</span> <Copy text={run.run_id}>Copy id</Copy> ·{' '}
        <a href={href(['specs', run.spec_id])}>comparison #{run.spec_id}</a>
        {` · ${label(run.mode)} · cycle ${run.cycle} · created `}
        <When at={run.created_at} />
        {run.created_by && <> by <Named name={run.created_by_name} actor={run.created_by} /></>}
        {run.finished_at && <> · finished <When at={run.finished_at} /></>}
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
            className={CONTROL_TONE[a]}
            disabled={a === 'resume' && !!refused}
            title={a === 'resume' ? refused : undefined}
            onClick={() => control(a)}
          >
            {a === 'resume' && run.schema.accepts ? controlLabel(a, run.schema) : CONTROLS[a]}
          </button>
        ))}
        <Menu label="Download">
          {REPORTS.map((name) => (
            <button key={name} onClick={() => act(api.report(run.run_id, name, cycle).then((b) => save(b, reportFile(run, name))))}>
              {name}
            </button>
          ))}
        </Menu>
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
      <OutcomeBar byOutcome={barOf(run)} href={(o) => href(['runs', run.run_id], { cycle, tab: 'keys', outcome: o })} />
      <p className="outcomes">
        {segments(barOf(run) ?? outcomes).map((s) => (
          <a key={s.outcome} href={href(['runs', run.run_id], { cycle, tab: 'keys', outcome: s.outcome })} title={s.outcome}>
            <span className={`dot ${tone(s.outcome) || 'none'}`} aria-hidden="true" />
            {num(s.n)} {label(s.outcome)}
          </a>
        ))}
      </p>
      <details>
        <summary>What the outcomes mean</summary>
        <ul>{OUTCOMES.map((o) => <li key={o}><Badge v={o} /> {LEGEND[o]} <code className="muted">{o}</code></li>)}</ul>
      </details>
      <details>
        <summary>Counters</summary>
        <Stats values={run.counters} />
      </details>
    </section>
  );
}

const num = (v: number) => v.toLocaleString();

/** A table scan, while it runs and after: what it read and what it left, and each table side's engine where
 *  the plan names it; a file side's files in its place. */
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

/** Bulk graph parity: the groups whose members differ and the components that changed. */
function graphLine(g: Graph): string {
  return (
    `Graph: ${num(g.groups_a)} groups on A, ${num(g.groups_b)} on B; ${num(g.groups_different)} differ; ` +
    `${num(g.split)} split, ${num(g.merge)} merge, ${num(g.rearrange)} rearranged, ${num(g.oversized)} too large to expand.`
  );
}

/** A tab's filters, kept in the URL with the cycle and the tab: a select applies at once, the text fields on
 *  Filter. `selects` and `texts` name the query's parameters. */
function FilterBar({ runId, f, tab, cycles, selects, texts = [] }: {
  runId: string;
  f: Filters;
  tab: Tab;
  cycles: number; // a soak run's cycles so far; 0 for a run without a choice of cycle
  selects: [name: string, text: string, options: string[]][];
  texts?: string[];
}) {
  const at = (values: Filters) => href(['runs', runId], { ...values, tab });
  function apply(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    location.hash = at(Object.fromEntries([...new FormData(e.currentTarget)].map(([k, v]) => [k, String(v).trim()])));
  }
  const submit = (e: { currentTarget: HTMLSelectElement }) => e.currentTarget.form?.requestSubmit();
  const select = (name: string, text: string, options: string[], any = 'any') => (
    <label key={name}>
      {text}{' '}
      <select name={name} defaultValue={f[name] ?? ''} onChange={submit}>
        <option value="">{any}</option>
        {options.map((o) => <option key={o} value={o}>{label(o)}</option>)}
      </select>
    </label>
  );
  if (!cycles && !selects.length && !texts.length) return null;
  return (
    <form className="panel inline" key={JSON.stringify(f)} onSubmit={apply}>
      {cycles > 0 && select('cycle', 'Cycle', Array.from({ length: cycles }, (_, i) => String(i + 1)), 'current')}
      {selects.map(([name, text, options]) => select(name, text, options))}
      {texts.map((name) => (
        <label key={name}>
          {label(name)}{' '}
          <input name={name} defaultValue={f[name]} size={14} />
        </label>
      ))}
      {texts.length > 0 && <button>Filter</button>}
      {selects.length + texts.length > 0 && <a href={at({ cycle: f.cycle })}>Clear filters</a>}
    </form>
  );
}

type TableProps = { runId: string; f: Filters; version: string; keyLink: KeyLink; cycles: number };

type Subject = string | null; // a root cause's id; null: the run
type Draft = { subject: Subject; cycle: number; session: Session };

/** The root causes, with `Explain` on each and `Summarise this run`: an AI draft answering a fixed question,
 *  shown under the table beside the engine's stored facts. One draft is open per run and cycle, and the
 *  browser tab keeps which. */
function RootCauses({ runId, f, version, keyLink, cycles, run, ai }: TableProps & { run: RunDetail; ai?: boolean }) {
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
    ['Root cause', (c) => (
      <a href={href(['runs', runId], { cycle: f.cycle, tab: 'findings', root_cause_id: c.root_cause_id })}>{c.root_cause_id}</a>
    )],
    ['Severity', (c) => <Badge v={c.max_severity} />],
    ['Taxonomy', (c) => <span title={c.taxonomy_code ?? undefined}>{label(c.taxonomy_code ?? '')}</span>],
    ['Path', (c) => <code>{c.path_pattern}</code>],
    ['Change', (c) => <span title={`${c.change_type} (${c.direction})`}>{label(c.change_type)} ({label(c.direction)})</span>],
    ['Keys', (c) => c.affected_keys],
    ['Findings', (c) => c.finding_count],
    ['Sample keys', (c) => c.sample_keys.map((k) => <span key={k}>{keyLink(k)} </span>)],
  ];
  return (
    <section>
      <FilterBar runId={runId} f={f} tab="causes" cycles={cycles} selects={[]} />
      <div className="inline">
        <h2>Root causes</h2>
        {button(null, 'Summarise this run')}
      </div>
      {reason && <p className="muted">{reason}</p>}
      <Err error={error} />
      <Err error={causes.error} />
      <Table
        rows={rows ?? []}
        cols={[...cols, ['', (c) => button(c.root_cause_id, 'Explain')]]}
        rowId={(c) => c.root_cause_id}
        loading={causes.loading}
        empty="No root cause in this cycle."
      />
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

const sideLine = (state: string, reason: string | null) => [state, reason].flatMap((v) => (v ? [label(v)] : [])).join(': ');

const resultCols = (keyLink: KeyLink): Col<Result>[] => [
  ['Key', (r) => keyLink(r.key_masked)],
  ['Outcome', (r) => <Badge v={r.outcome} />],
  ['Why', (r) => label(r.inconclusive_reason ?? r.drift_reason ?? '')],
  ['Confirmation', (r) => label(r.confirmation)],
  ['A', (r) => sideLine(r.side_a_state, r.side_a_reason)],
  ['B', (r) => sideLine(r.side_b_state, r.side_b_reason)],
];

/** The keys of every outcome but a match, by outcome, worst first: each outcome's first page, with a link to
 *  all of its keys under "All results". */
function Differences({ runId, f, version, keyLink, cycles }: TableProps) {
  const found = useLoad(
    () => Promise.all(DIFFERING.map(async (outcome) => ({ outcome, page: await api.results(runId, { cycle: f.cycle, outcome }) }))),
    [runId, f.cycle, version],
  );
  const groups = found.data?.filter((g) => g.page.items.length > 0);
  const cols = resultCols(keyLink).filter(([head]) => head !== 'Outcome');
  return (
    <section>
      <FilterBar runId={runId} f={f} tab="differences" cycles={cycles} selects={[]} />
      <Err error={found.error} />
      {found.loading && <p className="muted" role="status">Loading…</p>}
      {groups?.length === 0 && <p className="muted">No key of this cycle has an outcome other than a match so far.</p>}
      {groups?.map(({ outcome, page }) => (
        <div key={outcome}>
          <h2><Badge v={outcome} /></h2>
          <p className="muted">{LEGEND[outcome]}</p>
          <Table rows={page.items} cols={cols} />
          {page.next_cursor && (
            <p className="muted">
              The first {num(page.items.length)} are shown.{' '}
              <a href={href(['runs', runId], { cycle: f.cycle, tab: 'keys', outcome })}>All keys with this outcome</a>
            </p>
          )}
        </div>
      ))}
    </section>
  );
}

function Results({ runId, f, version, keyLink, cycles }: TableProps) {
  const q = { cycle: f.cycle, outcome: f.outcome };
  const results = usePaged((cursor) => api.results(runId, { ...q, cursor }), [runId, JSON.stringify(q), version]);
  return (
    <section>
      <FilterBar runId={runId} f={f} tab="keys" cycles={cycles} selects={[['outcome', 'Outcome', OUTCOMES]]} />
      <Err error={results.error} />
      <Table
        rows={results.items}
        cols={resultCols(keyLink)}
        loading={results.loading}
        empty={f.outcome ? 'No key of this cycle has this outcome.' : 'No key of this cycle has a result yet.'}
      />
      <MoreRows shown={results.items.length} more={results.more} />
    </section>
  );
}

const FINDING_COLS: Col<Finding>[] = [
  ['Finding', (x) => <span className="mono muted">{x.finding_id}</span>],
  ['Pointer', (x) => <code>{x.json_pointer || '/'}</code>],
  ['Change', (x) => <span title={x.change_type}>{label(x.change_type)}</span>],
  ['Severity', (x) => <Badge v={x.severity} />],
  ['Taxonomy', (x) => <span title={x.taxonomy_code ?? undefined}>{label(x.taxonomy_code ?? '')}</span>],
  ['A', (x) => <code>{cell(x.value_a)}</code>],
  ['B', (x) => <code>{cell(x.value_b)}</code>],
  ['Class', (x) => x.value_class],
  ['Rule', (x) => [x.rule_id, x.suppressed && '(suppressed)'].filter(Boolean).join(' ')],
];

export const findingCols = (keyLink?: KeyLink): Col<Finding>[] =>
  keyLink ? [['Key', (x) => keyLink(x.key_masked ?? '')], ...FINDING_COLS] : FINDING_COLS;

function Findings({ runId, f, version, keyLink, cycles }: TableProps) {
  const { cycle, key, root_cause_id, taxonomy_code, path_pattern, severity } = f;
  const q = { cycle, key, root_cause_id, taxonomy_code, path_pattern, severity };
  const findings = usePaged((cursor) => api.findings(runId, { ...q, cursor }), [runId, JSON.stringify(q), version]);
  const filtered = [key, root_cause_id, taxonomy_code, path_pattern, severity].some(Boolean);
  return (
    <section>
      <FilterBar
        runId={runId}
        f={f}
        tab="findings"
        cycles={cycles}
        selects={[['severity', 'Severity', SEVERITIES]]}
        texts={['key', 'taxonomy_code', 'path_pattern', 'root_cause_id']}
      />
      <Err error={findings.error} />
      <Table
        rows={findings.items}
        cols={findingCols(keyLink)}
        rowId={(x) => x.finding_id}
        loading={findings.loading}
        empty={filtered ? 'No finding of this cycle passes these filters.' : 'No finding in this cycle.'}
      />
      <MoreRows shown={findings.items.length} more={findings.more} />
    </section>
  );
}

/** The keys of the cycle shown whose outcome differs from the cycle before's, a page at a time. */
function Changes({ runId, f, version, cycles, viewed }: TableProps & { viewed: number }) {
  const changes = usePaged((cursor) => api.changes(runId, { cycle: f.cycle, cursor }), [runId, f.cycle, version]);
  const cols: Col<Change>[] = [
    ['Cycle', (c) => c.cycle],
    ['Key', (c) => <a href={href(['runs', runId, 'keys', c.key_masked], { cycle: c.cycle })}>{c.key_masked}</a>],
    ['From', (c) => <Badge v={c.from_outcome} />],
    ['To', (c) => <Badge v={c.to_outcome} />],
  ];
  return (
    <section>
      <FilterBar runId={runId} f={f} tab="changes" cycles={cycles} selects={[]} />
      <h2>{changesTitle(viewed)}</h2>
      <Err error={changes.error} />
      <Table rows={changes.items} cols={cols} loading={changes.loading} empty={noChanges(viewed)} />
      <MoreRows shown={changes.items.length} more={changes.more} />
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

/** A sampled run's draw, polled while live: its frame, each stratum's progress, interim
 *  bound and certain failures, an ETA; once ended, its sign-off precheck; its population's ledger; and, for an
 *  operator signed in under SSO, the close of the name's draws that did not pass, with a note of the fix. */
function Sample({ run }: { run: RunDetail }) {
  const { can, me } = useWho();
  const { prompt } = useDialogs();
  const [error, setError] = useState<string>();
  const [done, setDone] = useState<string>();
  const sample = run.sample!;
  const drawId = sample.draw_id ?? [...run.cycles].reverse().find((c) => c.sample)?.sample?.draw.draw_id;
  const found = useLoad(() => (drawId ? api.sample(drawId) : Promise.resolve(undefined)), [drawId]);
  const d = found.data;
  usePoll(found.reload, 5000, d?.status === 'live');
  const closer = me?.mode === 'oidc' && me?.kind === 'user' && can('operator');

  async function close() {
    const note = await prompt({
      title: `${CLOSE_TITLE} of ${run.spec_name}`,
      body: 'The note is kept with the draws it closes. Name no customer key in it.',
      input: { label: 'What was fixed', required: true },
      action: 'Record and close',
    });
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
    ['Comparison', (x) => x.spec_name],
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
  const { prompt } = useDialogs();
  const [error, setError] = useState<string>();
  const refused = refusal(useLicence(), 'signoff');
  const { cycles, purged } = run.retention;
  const ended = [...new Set([...cycles.map((c) => c.cycle), ...purged])].sort((x, y) => x - y);
  if (!ended.length) return null;
  const of = (cycle: number) => run.signoffs.filter((s) => s.cycle === cycle);
  const open = (cycle: number) =>
    !isPurged(run, cycle) && !of(cycle).some((s) => s.status === 'requested' || s.status === 'approved');

  async function request(cycle: number) {
    const note = await prompt({
      title: `Request a sign-off of cycle ${cycle}`,
      input: { label: 'Note for the approver (optional)' },
      action: 'Request sign-off',
    });
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
        A sign-off accepts one ended cycle as parity evidence, once someone who neither wrote its comparison, started this run
        nor asked for it approved its facts.{me?.mode !== 'oidc' && ` ${ssoOnly(me?.mode)}`}
      </p>
      <Table rows={ended} cols={cols} rowId={(cycle) => `signoff-${cycle}`} />
      <Err error={error} />
    </section>
  );
}

/** Each ended cycle's census, adjudications and evidence packs. Under SSO an operator
 *  draws a reviewer sheet and scores its reviewed upload before the cycle's sign-off is requested, which then
 *  attests it; a signed-in person issues the signed-off cycle's pack, which anyone who reads the run downloads. */
function Evidence({ run, reload }: { run: RunDetail; reload: () => void }) {
  const { can, me } = useWho();
  const { confirm, prompt } = useDialogs();
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
      const limit = await prompt({
        title: `Draw a reviewer sheet of cycle ${cycle}`,
        input: { label: 'Per-key results to draw (1 to 500)', initial: '50', required: true, number: { min: 1, max: 500 } },
        action: 'Draw sheet',
      });
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
      const issuing = { title: `Issue the evidence pack of cycle ${cycle}?`, body: "It is signed with this installation's key.", action: 'Issue evidence pack' };
      if (!(await confirm(issuing))) return;
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

/** Each ended cycle's labelled accuracy, in every auth mode: an operator draws a sample
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
          {unlocatable && (
            <label title="acknowledge_unlocatable">
              <input name="acknowledge" type="checkbox" /> I acknowledge that labellers cannot find these records by key
            </label>
          )}
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
