/** Administration: API tokens, users, sessions and the audit log, one tab
 *  each, so only the Audit tab reads the log (a read that is itself audited). Admins only, never a token; the
 *  server still decides. */
import { useState, type FormEvent } from 'react';
import { ApiError, api, type AuditRow, type Disabled, type NewToken, type SignedIn, type Token, type User } from './api';
import { href, message, useLoad, usePaged, useWho } from './lib';
import { Badge, Err, Named, Table, cell, when, type Col } from './ui';

const TABS = [['tokens', 'Tokens'], ['users', 'Users'], ['sessions', 'Sessions'], ['audit', 'Audit']];
const TOKEN_ROLES = ['viewer', 'operator', 'spec_author'];

export function Admin({ tab = 'tokens', query }: { tab?: string; query: URLSearchParams }) {
  return (
    <>
      <h1>Admin</h1>
      <p className="inline tabs">
        {TABS.map(([t, label]) => (
          <a key={t} href={href(['admin', t])} aria-current={t === tab ? 'page' : undefined}>{label}</a>
        ))}
      </p>
      {tab === 'users' ? <Users /> : tab === 'sessions' ? <Sessions /> : tab === 'audit' ? <Audit query={query} /> : <Tokens />}
    </>
  );
}

/** Runs an action, then `reload`s its table; a refusal becomes `setError`'s message. */
const acting = (setError: (e?: string) => void, reload: () => void) => (done: Promise<unknown>) => {
  setError(undefined);
  done.then(reload, (e) => setError(message(e)));
};

function Tokens() {
  const { me } = useWho();
  const tokens = useLoad(api.tokens, []);
  const users = useLoad(api.users, []);
  const [made, setMade] = useState<NewToken>();
  const [reauth, setReauth] = useState(false);
  const [error, setError] = useState<string>();
  const act = acting(setError, tokens.reload);
  const sso = me?.mode === 'oidc'; // an accountable person is required with SSO

  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const field = (name: string) => String(new FormData(form).get(name) ?? '');
    setError(undefined);
    setReauth(false);
    setMade(undefined);
    try {
      setMade(
        await api.createToken({
          name: field('name'),
          owner: field('owner'),
          owner_user_id: field('owner_user_id') || undefined,
          role: field('role'),
          days: field('days') ? Number(field('days')) : undefined,
        }),
      );
      form.reset();
      tokens.reload();
    } catch (err) {
      if (err instanceof ApiError && err.status === 403 && err.message === 'reauth_required') setReauth(true);
      else setError(message(err));
    }
  }

  const cols: Col<Token>[] = [
    ['Token', (t) => <span className="mono">{t.token_id}</span>],
    ['Name', (t) => t.name],
    ['Owner', (t) => t.owner],
    ['Accountable', (t) => <Named name={t.owner_user_name} actor={t.owner_user_id && `user:${t.owner_user_id}`} />],
    ['Role', (t) => t.role],
    ['State', (t) => <Badge v={t.state} />],
    ['Created by', (t) => <Named name={t.created_by_name} actor={t.created_by} />],
    ['Created', (t) => when(t.created_at)],
    ['Expires', (t) => when(t.expires_at)],
    ['Last used', (t) => when(t.last_used_at)],
    ['', (t) => t.state === 'active' && (
      <button onClick={() => confirm(`Revoke token ${t.name} (${t.token_id})?`) && act(api.revokeToken(t.token_id))}>
        Revoke
      </button>
    )],
  ];
  return (
    <>
      <form className="panel" onSubmit={create}>
        <h2>New API token</h2>
        <div className="inline">
          <label>
            Name <input name="name" required size={16} placeholder="ci-nightly" />
          </label>
          <label>
            Owner <input name="owner" required maxLength={200} size={16} placeholder="team or contact" />
          </label>
          <label>
            Accountable person{' '}
            <select name="owner_user_id" required={sso}>
              <option value="">{sso ? 'choose' : 'none'}</option>
              {users.data?.items
                .filter((u) => !u.disabled_at)
                .map((u) => <option key={u.user_id} value={u.user_id}>{`${u.username} (${u.user_id})`}</option>)}
            </select>
          </label>
          <label>
            Role{' '}
            <select name="role">
              {TOKEN_ROLES.map((r) => <option key={r}>{r}</option>)}
            </select>
          </label>
          <label>
            Days <input name="days" type="number" min={1} size={5} placeholder="90" />
          </label>
          <button>Create</button>
        </div>
        {reauth && (
          <p className="inline">
            <span className="warn">Sign in again to create a token.</span>
            <button
              type="button"
              onClick={() => location.assign('/v1/auth/login?reauth=1&next=' + encodeURIComponent('#/admin'))}
            >
              Sign in again
            </button>
          </p>
        )}
        <Err error={error ?? users.error} />
      </form>
      {made && (
        <section className="panel">
          <h2>
            Token {made.name} <span className="mono muted">({made.token_id})</span>
          </h2>
          <p>
            Copy it now: it is not shown again. Role {made.role}, expires {when(made.expires_at)}.
          </p>
          <p>
            <code className="secret">{made.token}</code>
          </p>
          <p className="inline">
            <button onClick={() => navigator.clipboard.writeText(made.token)}>Copy</button>
            <button onClick={() => setMade(undefined)}>Done</button>
          </p>
        </section>
      )}
      <Err error={tokens.error} />
      <Table rows={tokens.data?.items ?? []} cols={cols} rowId={(t) => t.token_id} />
    </>
  );
}

function Users() {
  const { me } = useWho();
  const users = useLoad(api.users, []);
  const [revokeTokens, setRevokeTokens] = useState(false);
  const [disabled, setDisabled] = useState<Disabled>();
  const [error, setError] = useState<string>();
  const act = acting(setError, users.reload);

  function disable(u: User) {
    const tokens = revokeTokens ? ', and revoke every token they created or own' : '';
    if (!confirm(`Disable ${u.username} (${u.user_id}) and end their sessions${tokens}?`)) return;
    setDisabled(undefined);
    act(api.disableUser(u.user_id, revokeTokens).then(setDisabled));
  }

  const cols: Col<User>[] = [
    ['User', (u) => <Named name={u.username} actor={`user:${u.user_id}`} />],
    ['Display name', (u) => u.display_name],
    ['Roles at last sign-in', (u) => u.roles.join(', ')],
    ['Last sign-in', (u) => when(u.last_login_at)],
    ['Created', (u) => when(u.created_at)],
    ['Disabled', (u) => u.disabled_at && (
      <>
        {when(u.disabled_at)} by <Named name={u.disabled_by_name} actor={u.disabled_by} />
      </>
    )],
    ['', (u) => (
      <span className="inline">
        {u.disabled_at ? (
          <button onClick={() => act(api.enableUser(u.user_id))}>Enable</button>
        ) : (
          me?.actor !== `user:${u.user_id}` && <button onClick={() => disable(u)}>Disable</button>
        )}
        <a href={href(['admin', 'audit'], { actor: `user:${u.user_id}` })}>audit</a>
      </span>
    )],
  ];
  return (
    <>
      <p className="muted">
        Roles come from the identity provider at each sign-in. Disabling a user ends their sessions at once; their API
        tokens keep working unless they are revoked too.
      </p>
      <label>
        <input type="checkbox" checked={revokeTokens} onChange={(e) => setRevokeTokens(e.target.checked)} /> Disabling
        also revokes every token the user created or owns
      </label>
      <Err error={error ?? users.error} />
      {disabled && (
        <section className="panel">
          <p>
            Disabled {disabled.user_id}. Sessions ended: {disabled.sessions_revoked}. Tokens revoked:{' '}
            {disabled.tokens_revoked.length}. The tokens they created or own:
          </p>
          <Table rows={disabled.tokens} cols={DISABLED_TOKEN_COLS} rowId={(t) => t.token_id} />
        </section>
      )}
      <Table rows={users.data?.items ?? []} cols={cols} rowId={(u) => u.user_id} />
    </>
  );
}

const DISABLED_TOKEN_COLS: Col<Disabled['tokens'][number]>[] = [
  ['Token', (t) => <span className="mono">{t.token_id}</span>],
  ['Name', (t) => t.name],
  ['Role', (t) => t.role],
  ['State', (t) => <Badge v={t.state} />],
];

function Sessions() {
  const sessions = useLoad(api.sessions, []);
  const [error, setError] = useState<string>();
  const act = acting(setError, sessions.reload);
  const cols: Col<SignedIn>[] = [
    ['Session', (s) => <a className="mono" href={href(['admin', 'audit'], { session_id: s.session_id })}>{s.session_id}</a>],
    ['Who', (s) => <Named name={s.name} actor={s.actor} />],
    ['Roles', (s) => s.roles.join(', ')],
    ['Client', (s) => s.client_addr],
    ['Started', (s) => when(s.created_at)],
    ['Authenticated', (s) => when(s.auth_time)], // at the identity provider: token creation needs it recent
    ['Last seen', (s) => when(s.last_seen_at)],
    ['Ends', (s) => when(s.expires_at)],
    ['', (s) => (
      <button onClick={() => confirm(`End session ${s.session_id} of ${s.name}?`) && act(api.revokeSession(s.session_id))}>
        Revoke
      </button>
    )],
  ];
  return (
    <>
      <p className="muted">Live sessions, newest first. A revoked session ends at its next request.</p>
      <Err error={error ?? sessions.error} />
      <Table rows={sessions.data?.items ?? []} cols={cols} rowId={(s) => s.session_id} />
    </>
  );
}

const AUDIT_COLS: Col<AuditRow>[] = [
  ['Id', (r) => r.id],
  ['At', (r) => when(r.at)],
  ['Actor', (r) => <Named name={r.actor_name} actor={r.actor} />],
  ['Action', (r) => r.action],
  ['Object', (r) => <span className="mono">{r.object}</span>],
  ['Detail', (r) => <code className="detail">{cell(r.detail)}</code>],
  ['Request', (r) => <span className="mono">{r.request_id}</span>],
  ['Session', (r) => <span className="mono">{r.session_id}</span>],
  ['Client', (r) => r.client_addr],
  ['Chain', (r) => r.chain && <span className="mono" title={r.chain.hash}>{r.chain.seq}</span>],
];
const FILTERS = ['actor', 'action', 'object', 'session_id'];

/** A `datetime-local` value (the viewer's time) as ISO 8601 UTC; anything else as given, for the API to refuse. */
function utc(local?: string): string | undefined {
  const t = local ? new Date(local) : undefined;
  return t && !Number.isNaN(t.getTime()) ? t.toISOString() : local;
}

/** The audit log, newest first; the filters live in the URL, and each read is itself audited. */
function Audit({ query }: { query: URLSearchParams }) {
  const f: Record<string, string> = Object.fromEntries(query);
  const q = { actor: f.actor, action: f.action, object: f.object, session_id: f.session_id, since: utc(f.since), until: utc(f.until) };
  const rows = usePaged((cursor) => api.audit({ ...q, cursor }), [JSON.stringify(q)]);

  function apply(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const values = Object.fromEntries([...new FormData(e.currentTarget)].map(([k, v]) => [k, String(v).trim()]));
    location.hash = href(['admin', 'audit'], values);
  }

  return (
    <>
      <form className="panel inline" key={JSON.stringify(f)} onSubmit={apply}>
        {FILTERS.map((name) => (
          <label key={name}>
            {name} <input name={name} defaultValue={f[name]} size={16} />
          </label>
        ))}
        {['since', 'until'].map((name) => (
          <label key={name}>
            {name} <input name={name} type="datetime-local" defaultValue={f[name]} />
          </label>
        ))}
        <button>Filter</button>
        <a href={href(['admin', 'audit'])}>clear</a>
      </form>
      <Err error={rows.error} />
      <Table rows={rows.items} cols={AUDIT_COLS} rowId={(r) => `audit-${r.id}`} />
      {rows.more && <button onClick={rows.more}>More</button>}
    </>
  );
}
