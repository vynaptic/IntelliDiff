/** Table pre-checks, open to every role: what a pre-check is not, the list with each
 *  one's pairs counted by verdict, and one pre-check with its progress, its totals and its pairs worst first, a
 *  verdict filter and the reports; a pair opens its lines, the refusals a run would meet, its schema codes and each
 *  compared column's figures. Operators make one of stored specs, cancel one and start a comparison of a pair's
 *  spec; spec authors make one of two schemas' tables paired by name and draft a spec of such a pair. A verdict is
 *  words, and every text of a pair, the totals and the banner are the API's. The server still decides. */
import { useState, type FormEvent } from 'react';
import { api, type PrecheckPair, type PrecheckPairDetail, type PrecheckVerdict, type TablePrecheck } from './api';
import { refusal } from './licensing';
import { href, message, useLicence, useLoad, usePoll, useWho } from './lib';
import {
  CATALOGUE_LEVELS, EVALUATION, LEVELS, REPORTS, blank, blankSchemas, bodyOf, catalogueOf, codeWords, draftFile,
  draftable, ended, filterOptions, listed, progressOf, reportFile, type Draft, type Schemas,
} from './prechecking';
import { Err, Named, Table, save, when, type Col } from './ui';

const POLL_MS = 3_000; // while a pre-check is being read
const LIST_MS = 60_000;

/** The operator's form: a pre-check of the stored versions chosen, or of every stored spec of two tables. */
function PrecheckForm({ specId }: { specId?: string }) {
  const specs = useLoad(api.specs, []);
  const [d, setD] = useState<Draft>(() => blank(specId));
  const [error, setError] = useState<string>();

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(undefined);
    try {
      location.hash = href(['prechecks', (await api.createPrecheck(bodyOf(d))).precheck_id]);
    } catch (err) {
      setError(message(err));
    }
  }

  return (
    <form className="panel" onSubmit={submit}>
      <h2>New table pre-check</h2>
      <div className="inline">
        <label>
          <input type="checkbox" checked={d.every} onChange={(e) => setD({ ...d, every: e.target.checked })} /> every stored
          spec of two tables (the newest version of each name)
        </label>
        {!d.every && (
          <label>
            Specs{' '}
            <select
              multiple required size={6} value={d.spec_ids}
              onChange={(e) => setD({ ...d, spec_ids: [...e.target.selectedOptions].map((o) => o.value) })}
            >
              {specs.data?.items.map((s) => <option key={s.spec_id} value={s.spec_id}>{`${s.name} ${s.version} (#${s.spec_id})`}</option>)}
            </select>
          </label>
        )}
        <label>
          Level{' '}
          <select value={d.level} onChange={(e) => setD({ ...d, level: e.target.value as Draft['level'] })}>
            {LEVELS.map((level) => <option key={level}>{level}</option>)}
          </select>
        </label>
        <button>Pre-check</button>
      </div>
      <Err error={error ?? specs.error} />
    </form>
  );
}

/** The spec author's form: a pre-check of two database targets' schemas, their tables paired by name. */
function CatalogueForm() {
  const targets = useLoad(api.targets, []);
  const [d, setD] = useState<Schemas>(blankSchemas);
  const [error, setError] = useState<string>();
  const databases = targets.data?.items.filter((t) => t.engine !== null) ?? [];

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(undefined);
    try {
      location.hash = href(['prechecks', (await api.createCatalogue(catalogueOf(d))).precheck_id]);
    } catch (err) {
      setError(message(err));
    }
  }

  const side = (target: 'a' | 'b', schema: 'schema_a' | 'schema_b') => (
    <>
      <label>
        Side {target}{' '}
        <select required value={d[target]} onChange={(e) => setD({ ...d, [target]: e.target.value })}>
          <option value="" />
          {databases.map((t) => <option key={t.target_id} value={t.target_id}>{`${t.target_id} (${t.engine})`}</option>)}
        </select>
      </label>
      <label>
        Schema <input value={d[schema]} placeholder="its default" onChange={(e) => setD({ ...d, [schema]: e.target.value })} />
      </label>
    </>
  );
  return (
    <form className="panel" onSubmit={submit}>
      <h2>New table pre-check of two schemas, paired by name</h2>
      <div className="inline">
        {side('a', 'schema_a')}
        {side('b', 'schema_b')}
      </div>
      <div className="inline">
        <label>
          Include <input value={d.include} placeholder="customer*" onChange={(e) => setD({ ...d, include: e.target.value })} />
        </label>
        <label>
          Exclude <input value={d.exclude} placeholder="tmp_*" onChange={(e) => setD({ ...d, exclude: e.target.value })} />
        </label>
        <label>
          Level{' '}
          <select value={d.level} onChange={(e) => setD({ ...d, level: e.target.value as Schemas['level'] })}>
            {CATALOGUE_LEVELS.map((level) => <option key={level}>{level}</option>)}
          </select>
        </label>
        <button>Pre-check</button>
      </div>
      <Err error={error ?? targets.error} />
    </form>
  );
}

const Made = ({ p }: { p: TablePrecheck }) => (
  <>
    <Named name={p.created_by_name} actor={p.created_by} /> {when(p.created_at)}
  </>
);

export function PrechecksScreen({ query }: { query: URLSearchParams }) {
  const { can } = useWho();
  const [pages, setPages] = useState(1);
  const found = useLoad(() => listed((cursor) => api.prechecks({ cursor }), pages), [pages]);
  usePoll(found.reload, LIST_MS, true);
  const d = found.data;

  const cols: Col<TablePrecheck>[] = [
    ['Pre-check', (p) => <a className="mono" href={href(['prechecks', p.precheck_id])}>{p.precheck_id}</a>],
    ['Level', (p) => p.level],
    ['Of', (p) => p.source],
    ['Made', (p) => <Made p={p} />],
    ['State', (p) => ended(p)],
    ['Pairs by verdict, worst first', (p) => p.totals],
    ['Licence', (p) => p.evaluation && EVALUATION],
  ];
  return (
    <>
      <h1>Table pre-checks</h1>
      <Err error={found.error} />
      {d && (
        <>
          <p role="note">{d.not}</p>
          {d.banners.map((line) => <p key={line} className="banner" role="note">{line}</p>)}
          <Table rows={d.items} cols={cols} rowId={(p) => p.precheck_id} />
          {d.next_cursor && <button onClick={() => setPages(pages + 1)}>More</button>}
        </>
      )}
      {can('operator') && <PrecheckForm key={query.get('spec_id')} specId={query.get('spec_id') ?? undefined} />}
      {can('spec_author') && <CatalogueForm />}
    </>
  );
}

/** Everything read of one pair, in the API's words: its lines worst first (the last says how and when each side
 *  was read), the refusals a run of its spec would meet, its schema codes and each compared column's figures. */
function PairPanel({ id, n }: { id: string; n: number }) {
  const { can } = useWho();
  const found = useLoad(() => api.precheckPair(id, n), [id, n]);
  const [error, setError] = useState<string>();
  const refused = refusal(useLicence(), 'runs');
  const p = found.data;
  const r = p?.result;

  async function compare(of: PrecheckPairDetail) {
    setError(undefined);
    try {
      location.hash = href(['runs', (await api.createRun({ spec_id: of.spec_id!, mode: 'batch' })).run_id]);
    } catch (err) {
      setError(message(err));
    }
  }

  async function draft() {
    setError(undefined);
    try {
      save(await api.precheckDraft(id, n), draftFile(id, n));
    } catch (err) {
      setError(message(err));
    }
  }

  return (
    <section className="panel">
      <h2>Pair {n}{p && `: ${p.label}`}</h2>
      <Err error={found.error ?? error} />
      {p && (
        <>
          <p className="muted">
            side a: {p.targets[0]} {p.relations[0]} · side b: {p.targets[1]} {p.relations[1]} · {p.said ?? p.status}
          </p>
          {r ? <ul>{r.lines.map((line) => <li key={line}>{line}</li>)}</ul> : p.text && <p>{p.text}</p>}
          {r && r.refused.length > 0 && (
            <>
              <h3>Refusals</h3>
              <ul>{r.refused.map((line) => <li key={line}>{line}</li>)}</ul>
            </>
          )}
          {r && r.schema.length > 0 && (
            <>
              <h3>Schema</h3>
              <Table
                rows={r.schema}
                cols={[
                  ['Column', (s) => s.column],
                  ['Code', (s) => codeWords(s.code)],
                  ['Side a', (s) => s.a?.declared],
                  ['Side b', (s) => s.b?.declared],
                ]}
              />
            </>
          )}
          {p.columns.length > 0 && (
            <>
              <h3>Columns</h3>
              <Table
                rows={p.columns}
                cols={[
                  ['Column', (c) => c.column],
                  ['Classes', (c) => c.classes.join(' / ')],
                  ['Figures', (c) => c.lines.map((line) => <div key={line}>{line}</div>)],
                ]}
              />
            </>
          )}
          {can('operator') && p.spec_id !== null && (
            <button onClick={() => compare(p)} disabled={!!refused} title={refused}>Run a comparison</button>
          )}
          {can('spec_author') && draftable(p) && <button onClick={draft}>Draft a spec</button>}
        </>
      )}
    </section>
  );
}

export function PrecheckScreen({ id, pair }: { id: string; pair?: string }) {
  const { can } = useWho();
  const [verdict, setVerdict] = useState<PrecheckVerdict | ''>('');
  const found = useLoad(() => api.precheck(id, { verdict, limit: 500 }), [id, verdict]);
  const [error, setError] = useState<string>();
  const d = found.data;
  usePoll(found.reload, POLL_MS, !!d && d.finished_at === null);

  const act = (done: Promise<unknown>) => {
    setError(undefined);
    done.then(found.reload, (e) => setError(message(e)));
  };
  const cols: Col<PrecheckPair>[] = [
    ['Pair', (p) => <a href={href(['prechecks', id, 'pairs', p.n])}>{p.n}</a>],
    ['Label', (p) => p.label],
    ['Verdict', (p) => (p.verdict ? d?.worded[p.verdict] : p.status)],
    ['Differing figures', (p) => p.differing],
    ['Rows a', (p) => p.rows[0]?.toLocaleString('en-US')],
    ['Rows b', (p) => p.rows[1]?.toLocaleString('en-US')],
    ['Observed', (p) => p.text],
  ];
  return (
    <>
      <h1>Table pre-check <span className="mono">{id}</span></h1>
      <p><a href="#/prechecks">All pre-checks</a></p>
      <Err error={found.error ?? error} />
      {d && (
        <>
          <p role="note">{d.not}</p>
          {d.evaluation && <p className="warn">{EVALUATION}</p>}
          {d.banners.map((line) => <p key={line} className="banner" role="note">{line}</p>)}
          <p className="muted">
            level {d.level} · {d.source} · made by <Made p={d} /> · {ended(d)}
            {d.cancelled_at && <> by <Named name={d.cancelled_by_name} actor={d.cancelled_by} /> {when(d.cancelled_at)}</>}
            {progressOf(d).map(([status, count]) => ` · ${count.toLocaleString('en-US')} ${status}`)}
          </p>
          <p><b>{d.totals}</b></p>
          <p className="inline">
            <label>
              Verdict{' '}
              <select value={verdict} onChange={(e) => setVerdict(e.target.value as PrecheckVerdict | '')}>
                <option value="">every pair</option>
                {filterOptions(d).map((o) => <option key={o.verdict} value={o.verdict}>{o.label}</option>)}
              </select>
            </label>
            {REPORTS.map((name) => (
              <button key={name} onClick={() => act(api.precheckReport(id, name).then((blob) => save(blob, reportFile(d, name))))}>
                {name}
              </button>
            ))}
            {can('operator') && d.finished_at === null && <button onClick={() => act(api.cancelPrecheck(id))}>Cancel</button>}
          </p>
          <Table rows={d.items} cols={cols} rowId={(p) => `pair-${p.n}`} />
          {pair && <PairPanel key={pair} id={id} n={Number(pair)} />}
        </>
      )}
    </>
  );
}
