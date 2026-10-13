/** Targets (docs/mvp/DEPLOY.md §7), open to every role: each endpoint and database a spec may name, and where it
 *  is declared. Who declares (the role the API names: an admin, in a trial installation a spec author) adds a
 *  service to the product's list, changes one and removes one: its address, its consents, how it authenticates,
 *  its request budget and its credentials, which are typed here once, written to a file on the server and shown
 *  nowhere again. A target of the targets file is changed there. The server still decides. */
import { useEffect, useState } from 'react';
import { ApiError, api, type AuthKind, type Issue, type Target } from './api';
import { AUTH, MORE_HEADERS, blankEndpoint, endpointBody, endpointOf, handing, memory, reached, type Endpoint } from './building';
import { message, useLoad, useWho } from './lib';
import { Err, ISSUE_COLS, Table, type Col } from './ui';

const yes = (v?: boolean) => (v ? 'yes' : 'no');
const COLS: Col<Target>[] = [
  ['Id', (t) => t.target_id],
  ['Declared in', (t) => (t.declared_in === 'file' ? 'targets file' : 'product')],
  ['Kind', (t) => t.engine ?? 'service'],
];
// What a target of the product's list was declared with, as the API shows it to who declares.
const DECLARED: Col<Target>[] = [
  ['Base URL', (t) => t.base_url],
  ['Authentication', (t) => t.auth && [AUTH[t.auth.kind], [t.auth.header, ...t.auth.more].filter(Boolean).join(', ')].filter(Boolean).join(': ')],
  ['Private or local network', (t) => t.auth && yes(t.allow_private_network)],
  ['Credential over plain http', (t) => t.auth && yes(t.allow_insecure_http)],
  ['A 404 means no such record', (t) => t.auth && yes(t.absent_on_404)],
  ['Requests a second', (t) => t.rate_per_s],
  ['At once', (t) => t.max_concurrency],
];

export function Targets() {
  const targets = useLoad(api.targets, []);
  const { can } = useWho();
  // What the builder read from a curl command fills the form, with where to go back to: laid over the target its
  // side names where the command reached that one (`Change <id>`), else a new endpoint.
  const [handed, setHanded] = useState(() => memory.handed?.found);
  const [back] = useState(() => memory.handed?.back);
  const [stored, setStored] = useState(() => handed && reached(handed.origin, memory.handed?.target)); // the target being changed
  const [e, setE] = useState<Endpoint>(() => (handed ? handing(handed, stored) : blankEndpoint()));
  const [done, setDone] = useState<string>();
  const [issues, setIssues] = useState<Issue[]>();
  const [error, setError] = useState<string>();
  const [reauth, setReauth] = useState(false);
  const role = targets.data?.declarer;
  const set = (fields: Partial<Endpoint>) => setE((was) => ({ ...was, ...fields }));
  const further = (i: number, fields: Partial<Endpoint['more'][number]>) =>
    set({ more: e.more.map((m, at) => (at === i ? { ...m, ...fields } : m)) });

  useEffect(() => {
    memory.handed = undefined; // read once: its credentials stay in this form alone
  }, []);

  /** The form on `t`, or empty. A credential a command carried stays with a target the command reached; before
   *  another target's form drops it, the person is asked. */
  function open(t?: Target) {
    const mine = handed && reached(handed.origin, t);
    const dropped = t && handed && !mine && `It was sent to ${handed.origin}, not to this endpoint.`;
    if (dropped && !confirm(`Change ${t.target_id} without the credential the command carried? ${dropped}`)) return;
    setE(mine ? handing(handed, mine) : t ? endpointOf(t) : blankEndpoint());
    setStored(t);
    if (!mine) setHanded(undefined);
  }

  /** Saves or removes `id`, then lists the targets again; a refusal shows with its issues. */
  async function act(change: Promise<unknown>, id: string, said: string) {
    for (const clear of [setDone, setIssues, setError]) clear(undefined);
    setReauth(false);
    try {
      await change;
      setDone(said);
      if (id === e.id.trim()) open(); // the form's credentials leave the page
      targets.reload();
    } catch (err) {
      if (err instanceof ApiError && err.status === 403 && err.message === 'reauth_required') return setReauth(true);
      setIssues(err instanceof ApiError ? (err.body as { issues?: Issue[] } | null)?.issues : undefined);
      setError(message(err));
    }
  }

  const id = e.id.trim();
  const remove = (t: Target) =>
    confirm(`Remove ${t.target_id}? Stored specs and schedules that name ${t.target_id} stop running.`) &&
    act(api.removeTarget(t.target_id), t.target_id, `Removed endpoint ${t.target_id}.`);
  const changes: Col<Target> = ['', (t) => t.declared_in === 'product' && (
    <span className="inline">
      <button onClick={() => open(t)}>Change</button>
      <button onClick={() => remove(t)}>Remove</button>
    </span>
  )];
  const declares = !!role && can(role);
  // A credential's box: masked, never offered to the browser's own stores and inside no form that submits.
  const credential = (value: string, kept: boolean | undefined, onChange: (value: string) => void) => (
    <input type="password" autoComplete="off" size={32} value={value} onChange={(ev) => onChange(ev.target.value)}
      placeholder={kept ? 'stored: leave empty to keep it' : undefined} />
  );

  return (
    <>
      <h1>Targets</h1>
      <p className="muted">A target is an endpoint a comparison reads: a service's address, how it authenticates and its request budget.</p>
      <Err error={targets.error} />
      <Table rows={targets.data?.items ?? []} cols={declares ? [...COLS, ...DECLARED, changes] : COLS} rowId={(t) => t.target_id} />
      {role && !declares && <p className="muted">Endpoints are declared by the {role} role, or in the targets file.</p>}
      {declares && (
        <section className="panel">
          <h2>{stored ? `Change ${stored.target_id}` : 'Declare an endpoint'}</h2>
          <div className="inline">
            <label>
              Id <input value={e.id} onChange={(ev) => set({ id: ev.target.value })} readOnly={!!stored} size={24} maxLength={63} spellCheck={false} placeholder="orders-legacy" />
            </label>
            <label>
              Base URL{' '}
              <input value={e.base_url} onChange={(ev) => set({ base_url: ev.target.value })} size={48} spellCheck={false} placeholder="https://host:port/base/path" />
            </label>
          </div>
          <div className="inline">
            <label>
              <input type="checkbox" checked={e.private_network} onChange={(ev) => set({ private_network: ev.target.checked })} /> Private or
              local network
            </label>
            <label>
              <input type="checkbox" checked={e.insecure_http} onChange={(ev) => set({ insecure_http: ev.target.checked })} /> Credential over
              plain http
            </label>
            <label>
              <input type="checkbox" checked={e.absent_on_404} onChange={(ev) => set({ absent_on_404: ev.target.checked })} /> A 404 means no
              such record
            </label>
          </div>
          <div className="inline">
            <label>
              Authentication{' '}
              <select value={e.kind} onChange={(ev) => set({ kind: ev.target.value as AuthKind })}>
                {Object.entries(AUTH).map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}
              </select>
            </label>
            {e.kind === 'header' && (
              <label>
                Header name <input value={e.header} onChange={(ev) => set({ header: ev.target.value })} size={20} spellCheck={false} placeholder="Cookie" />
              </label>
            )}
            {e.kind !== 'none' && (
              <label>
                Credential {credential(e.secret, stored?.has_secret, (secret) => set({ secret }))}
              </label>
            )}
          </div>
          {e.kind === 'header' && e.more.map((m, i) => (
            <div className="inline" key={i}>
              <label>
                Header name <input value={m.header} onChange={(ev) => further(i, { header: ev.target.value })} size={20} spellCheck={false} />
              </label>
              <label>
                Credential {credential(m.secret, stored?.auth?.more.includes(m.header), (secret) => further(i, { secret }))}
              </label>
              <button onClick={() => set({ more: e.more.filter((_, at) => at !== i) })}>Remove</button>
            </div>
          ))}
          {e.kind !== 'none' && <p className="muted">Written once to a file on the server and shown nowhere again.</p>}
          {e.kind === 'header' && (
            <p>
              <button onClick={() => set({ more: [...e.more, { header: '', secret: '' }] })} disabled={e.more.length >= MORE_HEADERS}>
                Add another credential header
              </button>
            </p>
          )}
          <div className="inline">
            <label>
              Requests a second{' '}
              <input type="number" min={0} step="any" size={6} value={e.rate_per_s} onChange={(ev) => set({ rate_per_s: ev.target.value })} placeholder="20" />
            </label>
            <label>
              At once{' '}
              <input type="number" min={1} size={6} value={e.max_concurrency} onChange={(ev) => set({ max_concurrency: ev.target.value })} placeholder="4" />
            </label>
            <button onClick={() => act(api.declareTarget(id, endpointBody(e)), id, `Saved endpoint ${id}.`)} disabled={!id || !e.base_url.trim()}>
              Save endpoint
            </button>
            {stored && <button onClick={() => open()}>Cancel</button>}
          </div>
        </section>
      )}
      {reauth && (
        <p className="inline">
          <span className="warn">Sign in again to declare, change or remove an endpoint.</span>
          <button onClick={() => location.assign('/v1/auth/login?reauth=1&next=' + encodeURIComponent('#/targets'))}>
            Sign in again
          </button>
        </p>
      )}
      {done && (
        <p className="inline">
          <span>{done}</span>
          {back && <a href={back}>Back to the comparison</a>}
        </p>
      )}
      <Err error={error} />
      {!!issues?.length && <Table rows={issues} cols={ISSUE_COLS} />}
    </>
  );
}
