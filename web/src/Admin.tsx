/** Administration: API tokens, users, sessions and the audit log, one tab
 *  each, so only the Audit tab reads the log (a read that is itself audited). Admins only, never a token; the
 *  server still decides. */
import { useState, type FormEvent } from 'react';
import { ApiError, api, type AuditRow, type Disabled, type NewToken, type SignedIn, type Token, type User } from './api';
import { useDialogs } from './Dialogs';
import { label } from './labels';
import { href, message, useLoad, usePaged, useWho } from './lib';
import { Badge, Err, MoreRows, Named, Table, Tabs, cell, when, type Col } from './ui';

const TABS = [['tokens', 'Tokens'], ['users', 'Users'], ['sessions', 'Sessions'], ['audit', 'Audit']];
const TOKEN_ROLES = ['viewer', 'operator', 'spec_author'];

export function Admin({ tab = 'tokens', query }: { tab?: string; query: URLSearchParams }) {
  return (
    <>
      <h1>Admin</h1>
      <Tabs name="Parts of administration" current={tab} items={TABS.map(([t, text]) => [href(['admin', t]), text, t])} />
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
  const { confirm } = useDialogs();
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
      <button
        className="danger"
        onClick={async () =>
          (await confirm({ title: `Revoke token ${t.name}?`, body: `${t.token_id} stops working at once.`, action: 'Revoke token', danger: true })) &&
          act(api.revokeToken(t.token_id))}
      >
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
          <button className="primary">Create token</button>
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
      <Table rows={tokens.data?.items ?? []} cols={cols} rowId={(t) => t.token_id} loading={tokens.loading} empty="No API token yet." />
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
  const { confirm } = useDialogs();

  async function disable(u: User) {
    const tokens = revokeTokens ? ', and every token they created or own is revoked' : '';
    const asked = { title: `Disable ${u.username}?`, body: `${u.user_id}: their sessions end at once${tokens}.`, action: 'Disable user', danger: true };
    if (!(await confirm(asked))) return;
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
          me?.actor !== `user:${u.user_id}` && <button className="danger" onClick={() => disable(u)}>Disable</button>
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
      <Table rows={users.data?.items ?? []} cols={cols} rowId={(u) => u.user_id} loading={users.loading} empty="No user has signed in yet." />
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
  const { confirm } = useDialogs();
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
      <button
        className="danger"
        onClick={async () =>
          (await confirm({ title: `End this session of ${s.name}?`, body: `${s.session_id} ends at its next request.`, action: 'End session', danger: true })) &&
          act(api.revokeSession(s.session_id))}
      >
        End session
      </button>
    )],
  ];
  return (
    <>
      <p className="muted">Live sessions, newest first. A revoked session ends at its next request.</p>
      <Err error={error ?? sessions.error} />
      <Table rows={sessions.data?.items ?? []} cols={cols} rowId={(s) => s.session_id} loading={sessions.loading} empty="No live session." />
    </>
  );
}

/** A row's detail on one line; a long one folded under its start. */
const Detail = ({ text }: { text: string }) =>
  text.length <= 120 ? (
    <code className="detail">{text}</code>
  ) : (
    <details>
      <summary className="mono">{text.slice(0, 80)}…</summary>
      <code className="detail">{text}</code>
    </details>
  );

const AUDIT_COLS: Col<AuditRow>[] = [
  ['Id', (r) => r.id],
  ['At', (r) => when(r.at)],
  ['Actor', (r) => <Named name={r.actor_name} actor={r.actor} />],
  ['Action', (r) => r.action],
  ['Object', (r) => <span className="mono">{r.object}</span>],
  ['Detail', (r) => <Detail text={cell(r.detail)} />],
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
            {label(name)} <input name={name} defaultValue={f[name]} size={16} />
          </label>
        ))}
        {['since', 'until'].map((name) => (
          <label key={name}>
            {label(name)} <input name={name} type="datetime-local" defaultValue={f[name]} />
          </label>
        ))}
        <button>Filter</button>
        <a href={href(['admin', 'audit'])}>Clear filters</a>
      </form>
      <Err error={rows.error} />
      <Table rows={rows.items} cols={AUDIT_COLS} rowId={(r) => `audit-${r.id}`} loading={rows.loading} empty="No audit row passes these filters." />
      <MoreRows shown={rows.items.length} more={rows.more} />
    </>
  );
}
