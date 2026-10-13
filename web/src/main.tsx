/** DeltaSleuth UI: a hash-routed single page over API v1. */
import { StrictMode, useEffect, useState, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { Admin } from './Admin';
import { Approvals } from './Approvals';
import {
  ApiError, LOGIN_EVENT, api, watchUse, type Health, type Licence, type Me, type Mode, type Notifications,
} from './api';
import { forget } from './building';
import { KeyScreen } from './Key';
import { LicenceScreen } from './Licence';
import { Licensed, Who, message, useRoute } from './lib';
import { NotificationsScreen } from './Notifications';
import { PrecheckScreen, PrechecksScreen } from './Prechecks';
import { bannerLines } from './notify';
import { ReadinessBrief, ReadinessPair, ReadinessScreen } from './Readiness';
import { RunScreen } from './Run';
import { Runs } from './Runs';
import { ScheduleScreen, SchedulesScreen } from './Schedules';
import { TOKEN_HINT, addressHint } from './signin';
import { SpecScreen, Specs } from './Specs';
import { Targets } from './Targets';
import { Err, Named } from './ui';
import './styles.css';

// The callback's fixed codes (docs/mvp/AUTH.md §2, §9); the page never shows text from the URL.
const SIGN_IN_ERRORS: Record<string, string> = {
  login_expired: 'The sign-in expired or was started in another tab. Sign in again.',
  login_failed: 'The sign-in could not be completed.',
  idp_unavailable: 'The identity provider cannot be reached. Try again later.',
  access_denied: 'The identity provider refused the sign-in.',
  idp_error: 'The identity provider reported an error.',
  no_role: 'Your account has no DeltaSleuth role. Ask your administrator for access.',
  groups_overage: 'Your account is in too many groups for the identity provider to list them. Ask your administrator.',
  disabled: 'Your DeltaSleuth account is disabled.',
};

function SignIn({ mode, query, onSignedIn }: { mode: Mode; query: URLSearchParams; onSignedIn: (me: Me) => void }) {
  const [error, setError] = useState<string>();
  const code = query.get('error');
  const shown = code && (Object.hasOwn(SIGN_IN_ERRORS, code) ? SIGN_IN_ERRORS[code] : 'The sign-in failed.');
  const requestId = query.get('request_id') ?? '';
  const hint = addressHint(error, location); // from the address bar alone (docs/mvp/AUTH.md §9)

  function sso() {
    const hash = location.hash;
    const next = !hash || hash.startsWith('#/signin') ? '#/runs' : hash; // never back to this page
    location.assign('/v1/auth/login?next=' + encodeURIComponent(next));
  }

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError(undefined);
    try {
      onSignedIn(await api.tokenLogin(String(new FormData(e.currentTarget).get('token'))));
    } catch (err) {
      setError(message(err));
    }
  }

  return (
    <section className="panel login">
      <h1>Sign in</h1>
      {query.get('signed_out') === '1' && <p>You are signed out.</p>}
      {shown && (
        <p className="error" role="alert">
          {shown}
          {/^Q-[0-9a-f]{16}$/.test(requestId) && (
            <>
              {' '}Quote <span className="mono">{requestId}</span> to your administrator.
            </>
          )}
        </p>
      )}
      {mode === 'oidc' && <button onClick={sso} autoFocus>Sign in with single sign-on</button>}
      {mode === 'tokens' && (
        <>
          <form onSubmit={submit}>
            <label>
              API token <input name="token" type="password" autoComplete="current-password" required autoFocus />
            </label>
            <button>Sign in</button>
            <Err error={error} />
            {hint && <p className="muted">{hint}</p>}
          </form>
          <p className="muted">{TOKEN_HINT}</p>
        </>
      )}
      {mode === 'none' && (
        <p>
          Developer mode needs no sign-in. <a href="#/runs">Continue</a>
        </p>
      )}
    </section>
  );
}

function App() {
  const { parts, query } = useRoute();
  const [screen, id, sub, key] = parts;
  const [health, setHealth] = useState<Health>();
  const [me, setMe] = useState<Me | null>(); // undefined while loading, null when signed out
  const [error, setError] = useState<string>();
  const [signIns, setSignIns] = useState(0); // remounts the screen so it loads again as the new caller
  const [licence, setLicence] = useState<Licence>();
  const [notify, setNotify] = useState<Notifications>();

  useEffect(() => {
    const onLogin = () => setMe(null);
    addEventListener(LOGIN_EVENT, onLogin);
    api
      .health()
      .then((h) => {
        setHealth(h);
        return api.me();
      })
      .then(setMe, (e) => {
        if (!(e instanceof ApiError && e.status === 401)) setError(message(e)); // a 401 shows the sign-in
      });
    return () => removeEventListener(LOGIN_EVENT, onLogin);
  }, []);

  useEffect(() => {
    if (me) api.licence().then(setLicence, () => setLicence(undefined)); // no banner if it fails
    if (me) api.notifications().then(setNotify, () => setNotify(undefined)); // likewise (docs/mvp/ALERTS.md §10)
  }, [me]);

  function signedIn(who: Me) {
    forget(); // a builder's unsaved work is the person's who left
    setMe(who);
    setSignIns((n) => n + 1);
    if (screen === 'signin') location.hash = '#/runs';
  }

  async function signOut() {
    setError(undefined);
    let logoutUrl: string | null = null;
    try {
      logoutUrl = (await api.logout()).logout_url;
    } catch (e) {
      // 400 or 401: another tab already signed this browser out, so this one is signed out too
      if (!(e instanceof ApiError && (e.status === 400 || e.status === 401))) {
        setError(message(e));
        return;
      }
    }
    sessionStorage.clear();
    forget();
    setMe(null);
    location.assign(logoutUrl ?? '#/signin?signed_out=1');
  }

  const can = (role: string) => !!me?.roles.includes(role);
  const admin = can('admin') && me?.kind !== 'token'; // a token never holds admin (docs/mvp/AUTH.md §1)
  const page =
    !health || me === undefined ? null : me === null || screen === 'signin' ? (
      <SignIn mode={health.auth} query={query} onSignedIn={signedIn} />
    ) : screen === 'specs' && id ? (
      <SpecScreen key={id} id={id} />
    ) : screen === 'specs' ? (
      <Specs />
    ) : screen === 'targets' ? (
      <Targets />
    ) : screen === 'approvals' ? (
      <Approvals key={id} id={id} query={query} />
    ) : screen === 'admin' && admin ? (
      <Admin tab={id} query={query} />
    ) : screen === 'licence' ? (
      <LicenceScreen />
    ) : screen === 'notifications' ? (
      <NotificationsScreen />
    ) : screen === 'readiness' && id && sub === 'brief' ? (
      <ReadinessBrief key={id} name={id} />
    ) : screen === 'readiness' && id ? (
      <ReadinessPair key={id} name={id} />
    ) : screen === 'readiness' ? (
      <ReadinessScreen query={query} />
    ) : screen === 'schedules' && id ? (
      <ScheduleScreen key={id} id={id} />
    ) : screen === 'schedules' ? (
      <SchedulesScreen query={query} />
    ) : screen === 'prechecks' && id ? (
      <PrecheckScreen key={id} id={id} pair={sub === 'pairs' ? key : undefined} />
    ) : screen === 'prechecks' ? (
      <PrechecksScreen query={query} />
    ) : screen === 'runs' && id && sub === 'keys' && key ? (
      <KeyScreen key={`${id}/${key}`} runId={id} keyMasked={key} query={query} />
    ) : screen === 'runs' && id ? (
      <RunScreen key={id} runId={id} query={query} />
    ) : (
      <Runs />
    );
  return (
    <Who.Provider value={{ me: me ?? undefined, can }}>
      <Licensed.Provider value={me ? licence : undefined}>
        {health?.auth === 'none' && (
          <p className="banner" role="note">
            Developer mode: no sign-in, and every local user and process acts as admin. Never expose this port, directly
            or through a reverse proxy or port forward.
          </p>
        )}
        {me &&
          licence?.warnings.map((line) => (
            <p key={line} className="banner" role="note">
              <a href="#/licence">{line}</a>
            </p>
          ))}
        {me &&
          bannerLines(notify).map((line) => (
            <p key={line} className="banner" role="note">
              <a href="#/notifications">{line}</a>
            </p>
          ))}
        <nav>
          <b>DeltaSleuth</b>
          {me && (
            <>
              <a href="#/runs">Runs</a>
              <a href="#/readiness">Readiness</a>
              <a href="#/specs">Specs</a>
              <a href="#/targets">Targets</a>
              <a href="#/schedules">Schedules</a>
              <a href="#/prechecks">Pre-checks</a>
              {me.mode === 'oidc' && <a href="#/approvals">Approvals</a>}
              {admin && <a href="#/admin">Admin</a>}
              <a href="#/licence">Licence</a>
              {notify?.enabled && <a href="#/notifications">Notifications</a>}
              <span className="who">
                <Named name={me.name} actor={me.actor} />
              </span>
              {me.kind !== 'dev' && <button onClick={signOut}>Sign out</button>}
            </>
          )}
        </nav>
        <main key={signIns}>
          <Err error={error} />
          {page}
        </main>
      </Licensed.Provider>
    </Who.Provider>
  );
}

watchUse(window);
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
