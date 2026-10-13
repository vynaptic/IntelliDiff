/** The comparison builder (docs/mvp/CONTRACTS.md §7), a spec author's form of a spec: a sample key; each service
 *  side's target and request as rows, with a curl command read into them and Send, which asks that one endpoint
 *  once for the sample key and stores nothing; and everything else as YAML. It validates, saves a new version and
 *  compares the sample key under the version saved. The sample key and the unsaved work stay in memory only, and a
 *  credential a command carried goes to the Targets screen, never into the spec. The server still decides. */
import { useEffect, useRef, useState } from 'react';
import { ApiError, api, stringifyExact, type Issue, type KeyDetail, type Saved, type SpecForm, type Target, type Tried } from './api';
import {
  AS_IT_CAME, MASKED, NOT_JSON, PLACEHOLDERS, REPLACE_ID, STAND_INS, carried, draftOf, filled, formOf, fromCurl,
  isJson, memory, sentLine, type Draft, type Imported, type Rows,
} from './building';
import { Detail } from './Key';
import { refusal } from './licensing';
import { href, message, useLicence, useLoad, useWho } from './lib';
import { Badge, Err, ISSUE_COLS, Table } from './ui';

const issuesOf = (e: unknown) => (e instanceof ApiError ? (e.body as { issues?: Issue[] } | null)?.issues : undefined);

/** The builder of the spec `id` (`new`: one not stored yet), opened on `form`. `standIns`: its text shows masked
 *  inline keys, which the server will not take back. */
export function Builder({ id, form, standIns = false }: { id: string; form: SpecForm; standIns?: boolean }) {
  const targets = useLoad(api.targets, []);
  const { can } = useWho();
  const refused = refusal(useLicence(), 'runs');
  const [restored, setRestored] = useState(() => memory.drafts.has(id));
  const [d, setD] = useState<Draft>(() => memory.drafts.get(id) ?? draftOf(form));
  const opened = useRef(d);
  const [issues, setIssues] = useState<Issue[]>();
  const [status, setStatus] = useState<string>();
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState<Saved>();
  const [now, setNow] = useState<KeyDetail>();
  const [busy, setBusy] = useState(false);
  const key = d.key.trim();

  useEffect(() => {
    if (d !== opened.current) memory.drafts.set(id, d); // unsaved work outlives a visit to another screen
  }, [d]);

  function discard() {
    memory.drafts.delete(id);
    opened.current = draftOf(form);
    setD(opened.current);
    setRestored(false);
  }

  async function check(action: 'validate' | 'save' | 'compare') {
    for (const clear of [setIssues, setStatus, setError, setSaved, setNow]) clear(undefined);
    setBusy(true);
    try {
      const sent = formOf(d);
      if (action === 'validate') {
        const answer = await api.validateForm(sent);
        setIssues(answer.issues);
        if (!answer.issues.length) setStatus('No issues.');
        if (answer.form) setD((was) => filled(was, answer.form!)); // text pasted whole fills the rows it left empty
        return;
      }
      const stored = await api.saveForm(sent);
      setSaved(stored);
      if (action === 'compare') setNow(await api.compare(stored.spec_id, undefined, key));
    } catch (e) {
      setIssues(issuesOf(e));
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {restored && (
        <p className="inline">
          <span className="warn">Your unsaved work on this page is back.</span>
          <button onClick={discard}>Discard it</button>
        </p>
      )}
      <section className="panel">
        <label>
          Sample key{' '}
          <input value={d.key} onChange={(e) => setD({ ...d, key: e.target.value })} size={48} spellCheck={false} autoComplete="off" />
        </label>
        <p className="muted">A multi-part key: its parts joined by |</p>
      </section>
      <div className="sides">
        {(['a', 'b'] as const).map((side) => {
          const rows = d[side];
          return (
            <section className="panel" key={side}>
              <h2>Side {side.toUpperCase()}</h2>
              {rows ? (
                <SideRows
                  rows={rows}
                  items={targets.data?.items ?? []}
                  declares={!!targets.data && can(targets.data.declarer)}
                  keyed={!!key}
                  send={() => api.trySide(formOf(d), side, key)}
                  onRows={(next) => setD((was) => ({ ...was, [side]: next }))}
                />
              ) : (
                <p className="muted">A table side: edit it in the text.</p>
              )}
            </section>
          );
        })}
      </div>
      <Err error={targets.error} />
      <section className="panel">
        <h2>Everything else (YAML)</h2>
        <p className="muted">The requests above replace each side's target and request in this text.</p>
        {standIns && <p className="muted">{STAND_INS}</p>}
        <textarea rows={22} value={d.text} onChange={(e) => setD({ ...d, text: e.target.value })} spellCheck={false} />
        <p className="inline">
          <button onClick={() => check('validate')} disabled={busy}>Validate</button>
          <button onClick={() => check('save')} disabled={busy}>Save as a new version</button>
          <button onClick={() => check('compare')} disabled={busy || !key || !!refused} title={refused}>
            Save and compare this key
          </button>
          {saved && <a href={href(['specs', saved.spec_id])}>Saved as spec #{saved.spec_id}.</a>}
          <span>{status}</span>
        </p>
        <p className="muted">Saving makes this the newest version of its name: schedules that follow the name run it next.</p>
        <Err error={error} />
        {!!issues?.length && <Table rows={issues} cols={ISSUE_COLS} />}
      </section>
      {now && (
        <section className="panel">
          <h2>
            Compared now (<a href={href(['runs', now.run_id, 'keys', now.key_masked])}>run {now.run_id}</a>){' '}
            <Badge v={now.result?.outcome} />
          </h2>
          <Detail d={now} />
        </section>
      )}
    </>
  );
}

/** A service side: its target, its request's rows, a curl command read into them, and Send with what came back. */
function SideRows({ rows, items, declares, keyed, send, onRows }: {
  rows: Rows;
  items: Target[];
  declares: boolean; // whether the caller may declare a target, so the import offers to
  keyed: boolean;
  send: () => Promise<Tried>;
  onRows: (rows: Rows) => void;
}) {
  const [command, setCommand] = useState<string>(); // the pasted curl command, while its box is open
  const [found, setFound] = useState<Imported>();
  const [sent, setSent] = useState<Tried>();
  const [issues, setIssues] = useState<Issue[]>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const edit = (fields: Partial<Rows>) => onRows({ ...rows, ...fields, stored: undefined }); // the stored request no longer stands
  // Service targets; the side's own stays listed when it is declared no more.
  const ids = [...new Set([...items.filter((t) => t.engine === null).map((t) => t.target_id), rows.target])].filter(Boolean);

  function read() {
    setError(undefined);
    try {
      const got = fromCurl(command ?? '', items.find((t) => t.target_id === rows.target)?.base_url);
      edit({ method: got.method, path: got.path, query: got.query, headers: got.headers, body: got.body });
      setFound(got);
      setCommand(undefined);
    } catch (e) {
      setError(message(e));
    }
  }

  function declare() {
    // In memory only: the Targets screen reads it once, and gives the credential to the side's target where the
    // command reached it.
    memory.handed = { found: found!, target: items.find((t) => t.target_id === rows.target), back: location.hash };
    location.hash = '#/targets';
  }

  async function go() {
    for (const clear of [setSent, setIssues, setError]) clear(undefined);
    setBusy(true);
    try {
      setSent(await send());
    } catch (e) {
      setIssues(issuesOf(e));
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="inline">
        <label>
          Target{' '}
          <select value={rows.target} onChange={(e) => onRows({ ...rows, target: e.target.value })}>
            <option value="">choose</option>
            {ids.map((id) => <option key={id}>{id}</option>)}
          </select>
        </label>
        <a href="#/targets">Targets</a>
        <label>
          Method{' '}
          <select value={rows.method} onChange={(e) => edit({ method: e.target.value as Rows['method'] })}>
            <option>GET</option>
            <option>POST</option>
          </select>
        </label>
      </div>
      <label>
        Path <span className="muted">{PLACEHOLDERS}</span>
        <input className="wide" value={rows.path} onChange={(e) => edit({ path: e.target.value })} spellCheck={false} />
      </label>
      <label>
        Query
        <textarea rows={2} value={rows.query} onChange={(e) => edit({ query: e.target.value })} spellCheck={false} placeholder="name=value, one a line" />
      </label>
      <label>
        Headers
        <textarea rows={2} value={rows.headers} onChange={(e) => edit({ headers: e.target.value })} spellCheck={false} placeholder="Name: value, one a line" />
      </label>
      {rows.method === 'POST' && (
        <label>
          Body (JSON)
          <textarea rows={5} value={rows.body} onChange={(e) => edit({ body: e.target.value })} spellCheck={false} />
        </label>
      )}
      {rows.method === 'POST' && !!rows.body.trim() && !isJson(rows.body) && <Err error={NOT_JSON} />}
      {command !== undefined && (
        <textarea rows={5} value={command} onChange={(e) => setCommand(e.target.value)} spellCheck={false} autoFocus placeholder="curl 'https://…' -H '…'" />
      )}
      <p className="inline">
        {command === undefined ? (
          <button onClick={() => setCommand('')}>Import curl…</button>
        ) : (
          <>
            <button onClick={read} disabled={!command.trim()}>Import</button>
            <button onClick={() => setCommand(undefined)}>Cancel</button>
          </>
        )}
        <button onClick={go} disabled={busy || !keyed}>{busy ? 'Sending…' : 'Send'}</button>
        {!keyed && <span className="muted">Send needs a sample key.</span>}
      </p>
      {found && (
        <>
          <p className="muted">{REPLACE_ID}</p>
          {found.credentials.length > 0 && (
            <p className="inline">
              <span className="warn">{carried(found)}</span>
              {declares && <button onClick={declare}>Declare the endpoint…</button>}
            </p>
          )}
        </>
      )}
      <Err error={error} />
      {!!issues?.length && <Table rows={issues} cols={ISSUE_COLS} />}
      {sent && (
        <>
          <p>
            <b>{sentLine(sent)}</b>{' '}
            <span className="warn">{[sent.error_class, sent.error_detail].filter(Boolean).join(': ')}</span>
          </p>
          {sent.answer === null ? (
            sent.note && <p className="muted">{sent.note}</p>
          ) : (
            <>
              <p className="muted">{sent.raw ? AS_IT_CAME : MASKED}</p>
              <pre className="panel answer">{stringifyExact(sent.answer, 2)}</pre>
            </>
          )}
        </>
      )}
    </>
  );
}
