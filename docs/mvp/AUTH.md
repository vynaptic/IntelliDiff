# SSO, roles and per-user audit: contract

This contract is binding. It follows [PLAN.md](PLAN.md) and builds on `docs/architecture.md` §13.2.

**Goal:**
- People sign in through the site's OIDC identity provider (IdP), and machines use named API
  tokens.
- Every route and agent tool checks one of six roles.
- Every audit row names a person or a named credential. CLI rows name an asserted operator,
  backed by the host's own audit (§4.2).
- Four-eyes approvals and the `pii_reader` reveal build on it (§11).

**Shape:** DeltaSleuth is a confidential OIDC relying party on the server, using the code flow with
PKCE S256, `state` and `nonce`. IdP tokens are discarded after sign-in. The browser holds only an
HttpOnly session cookie. Nothing is written to the database before a person is identified.

## 1. Modes and principals

| Mode | Settings | Bind | People | Machines |
|---|---|---|---|---|
| developer | no `auth` | loopback only, never proxied | `dev:loopback`, acting as admin | the same |
| tokens | `auth` without `oidc` | any | token sign-in, which becomes a cookie session (§5) | `Authorization: Bearer dst_…` |
| oidc | `auth.oidc` | any | SSO (§2) | the same bearer |

- **What each mode is for.**
  - **oidc** is the production mode; an open build, which this one is, asks for no licence for it
    ([LICENCE.md](LICENCE.md) §19), and a vendor build for an Enterprise one. Per-user audit,
    administration over
    the API (`Admin.tsx`, the `/v1/auth` admin routes, `/v1/audit`), four-eyes approvals and the reveal
    all need it.
  - **tokens** is for CI and trials, and in a vendor build the production mode of Community and Pro
    licences ([LICENCE.md](LICENCE.md) D11). Every principal is a named machine credential: people who
    sign in with a token in the browser are audited as that token, not as themselves. No principal
    can hold `admin`, `rule_approver` or `pii_reader` (§5). Administration is therefore CLI-only
    (§4.2), approvals and the reveal are unavailable, and runs are labelled unapproved
    ([APPROVALS.md](APPROVALS.md) §4).
  - **developer** is for one developer's workstation.
- **Licences gate work, never sign-in** ([LICENCE.md](LICENCE.md) §2, D4), and only in a vendor build.
  There an installation that runs
  SSO (this server in oidc mode, or the newest `system.start` saying `auth: oidc`) without an
  Enterprise licence, an unlicensed evaluation included, keeps serving sign-in, sessions, tokens, reads
  and administration, and refuses new runs, compares and soak cycles (409, M5); sign-off and evidence
  packs need an Enterprise licence in every mode (M6, M7). An open build embeds no vendor key, has no
  licence and gates nothing with one: runs, compares, soak cycles, sign-off and packs go ahead in every
  mode, under each mode's own rules (§19).
- **Health.** `GET /v1/health` returns `{"status": "ok", "ai": bool, "auth": "none"|"tokens"|"oidc"}`,
  or 503 `the database is not answering, or the pool gave no connection in 3 s`
  ([FIRSTRUN.md](FIRSTRUN.md) §8, S2).
  - **Bounded:** the wait for a connection of the pool, 3 s, under the image health check's 4. A pool
    that holds none, the database being down, or whose every connection is busy gives the 503 after
    that wait. A connection the database has closed fails as health uses it, and gives the 503 at once.
  - **Not bounded:** the statement health then runs. When the database stops answering on a connection
    the pool still holds, without closing it (a stalled server, a dropped network path), health answers
    neither 200 nor 503 while the statement waits, and each waiting probe keeps one connection of the
    pool. A monitor needs a timeout of its own, as the image's health check has.
- **Serving without `auth`.** Beyond loopback, `ds serve` exits 2 with
  `error: serving on 0.0.0.0 needs the auth settings (docs/mvp/AUTH.md §1)`. On loopback it warns
  `warning: developer mode (no auth settings): every local user and process acts as admin; never expose this port, directly or through a reverse proxy or port forward`.

**`deltasleuth/auth/principal.py`:**
- **Roles.** `ROLES` is `viewer < operator < spec_author < rule_approver < pii_reader < admin`.
  `expand(granted)` adds every lower role, and checks test `role in who.roles`.
- **`TOKEN_ROLES`** is `viewer`, `operator` and `spec_author`: the only roles a token can hold. The
  DDL enforces it (§7).
- **`PERSON_REQUIRED`** is `rule_approver` and `pii_reader`. `require` grants them only to a person,
  whatever roles a principal carries.
- **`Principal`** is a frozen dataclass: `actor`, `name` (audited as `actor_name`) and `roles`,
  plus optional `request_id`, `session_id`, `auth_time`, `client_addr` (`request.client.host` when
  it is an IP), `via` (`chat:S-…` when the chat's agent acts for its asker; `mcp:http` or `mcp:stdio`
  for a caller of the MCP server, [MCP.md](MCP.md) §6) and `os_account` (CLI only). `person` is true
  for `user:` actors.
- **`printable(text)`** is false when any character has Unicode category `Cc` (C0, DEL and C1). It
  checks usernames, display names, `DS_OPERATOR` and token owners; the DDL repeats the check.
- **Fixed principals.** `WORKER`, `SERVER` and `MIGRATION` are `system:` principals with no roles.
  `local()` is the CLI principal (§4.2). `dev()` is `dev:loopback`, named `<account>@<host>`, with
  every role. It acts as admin, and `require` refuses it the person-required roles.

**`api/deps.py principal(request, ctx, bearer)`**, in order:
1. **Developer mode:** the dev principal. Unsafe methods (POST, PUT, PATCH, DELETE) need
   `X-CSRF-Token: dev`. This header is a guard against browser pages only; `_Guard` (§3) keeps
   everything else out.
2. **A bearer header:** the token (§5), else 401 `invalid or expired API token`.
3. **The `__Host-ds_session` cookie:** the session (§3), else 401 `not signed in` (no cookie) or
   `session expired`. On a plain-http loopback `public_url` the cookie is `ds_session_<port>` (§3,
   exception E1). The name is `sessions.named(COOKIE, auth)`, from the settings alone: a server reads
   its own form's name and no other.
4. **Unsafe cookie requests:** `X-CSRF-Token` must equal `csrf_of(cookie)`.

**Failures and checks:**
- A CSRF failure is 403 `missing or invalid CSRF token`, and every 401 carries
  `WWW-Authenticate: Bearer`.
- **`require(role)`** refuses with 403, audited as `authz.denied` (rate-limited, §6):
  - `needs the <role> role` when `role not in who.roles`;
  - `the <role> role needs a signed-in person` when the role is in `PERSON_REQUIRED` and
    `who.person` is false. So no non-person principal, `dev:loopback` included, passes
    `require("rule_approver")` or `require("pii_reader")`. An approval never reaches the
    `decided_by ~ '^user:U-…'` CHECK of `ds.approvals` with one, which would give a 500.
- Each route declares a module-level alias as `who`: `Anyone`, `Viewer`, `Operator`, `SpecAuthor`,
  `RuleApprover` or `Admin`. `require_fresh(role)` is `require(role)` plus a sign-in within 15
  minutes (§5); `fresh(who)` applies the same check inside a route.
- `create_app` raises if a `/v1` route lacks `principal`. Five routes are exempt:
  `GET /v1/health`, `GET /v1/auth/login`, `GET /v1/auth/callback`, `POST /v1/auth/token-login`, and
  `POST /v1/auth/logout`, which resolves its caller itself (§3).

**Deleted:** `require_token`, `deps.ACTOR`, `cycle.ACTOR`, `Context.api_token`,
`Settings.api_token_ref`, and the router-level `Depends(require_token)`.

## 2. OIDC sign-in (`auth/oidc.py`, `api/routes/auth.py`)

`Context.oidc` replaces `Context.api_token`. It is an `auth.oidc.Oidc` holding the discovery and
JWKS caches and the login-cookie key, or `None`.
- **Who builds it.** Only `create_app`'s lifespan, in oidc mode, through
  `Context.open(settings, oidc=True)`. `ds worker` and every CLI command get `None`, so they never
  need the client credential mounted and never contact the IdP. `ds auth check` builds its own
  `Oidc`, without a database.
- **Login-cookie key.** `context.login_key(tokeniser_key)` is
  `HMAC-SHA256(tokeniser_key, b"deltasleuth/login/v1")`, derived like `evidence_id_key`.

**`GET /v1/auth/login?next=<hash route>[&reauth=1]`** is public; outside oidc mode it gives 404
`SSO is not configured`. It writes nothing to the database.
1. **Cross-site refusal.** It checks the request before anything else. It answers 403
   `cross-site sign-in refused`, and sets no cookie, unless:
   - `Sec-Fetch-Site` is absent, `same-origin` or `none`; and
   - `Sec-Fetch-Mode` is absent or `navigate`.

   Otherwise any intranet page could embed `<img src=…/v1/auth/login>` and overwrite a sign-in in
   progress.
2. **`next`** must match `^#/[A-Za-z0-9/_.~%?&=+*!'()-]{0,500}$`, else it becomes `#/runs`. The class
   covers what `web/src/lib.ts href()` produces: `encodeURIComponent` leaves `*!'()` and
   `URLSearchParams` writes `+`. Redirects use `auth.public_url`, never `Host`.
3. **The login cookie.** `__Host-ds_login` (`ds_login_<port>` on a plain-http loopback `public_url`:
   §3, exception E1, here and wherever this section names the two cookies) is `v1.<payload>.<mac>`:
   - `payload` is base64url (no padding) of the JSON
     `{state, nonce, verifier, next, reauth, started, exp, replaces}`;
   - `state` and `nonce` are 32 random bytes and `verifier` 48, each base64url;
   - `started` is now and `exp` is now + 600 s, both in epoch seconds;
   - `replaces` is the `session_id` of a valid `__Host-ds_session` sent with this request, else
     `null`. This navigation is same-origin, so the Strict cookie is sent;
   - `mac` is base64url of `HMAC-SHA256(login_key, "v1." + payload)`.
4. **Redirect.** It answers 303 to `authorization_endpoint` in query mode and sets
   `__Host-ds_login`. The query carries `response_type=code`, `client_id`,
   `redirect_uri={public_url}/v1/auth/callback`, `scope`, `state`, `nonce`, `code_challenge` and
   `code_challenge_method=S256`. With `reauth=1` it adds `prompt=login&max_age=0`. When discovery
   fails (§2.1), it answers 303 to `{public_url}/#/signin?error=idp_unavailable&request_id=<Q-…>`
   instead and sets no cookie.

**`GET /v1/auth/callback`** always answers 303: to `{public_url}/{next}` on success, or to
`{public_url}/#/signin?error=<code>&request_id=<Q-…>`. The codes are fixed (§9), and IdP text is
never reflected.
1. **It checks the login cookie.**
   - A missing cookie, a bad `mac` (`hmac.compare_digest`), a passed `exp`, or a different `state`
     (`hmac.compare_digest`) gives `login_expired`.
   - An IdP `error` gives `access_denied` or `idp_error`.
   - An RFC 9207 `iss` that is not the issuer gives `login_failed`.
2. **It exchanges, validates and maps** (§2.2–2.4).
   - A failure before the identity is known writes **no audit row** and no database row.
   - It writes only a WARNING on `deltasleuth.auth`:
     `auth.login_failed reason=<code> ip=<addr> request_id=<Q-…>: <why>`, where `<why>` names the
     failed check and its values (§8), quoting IdP values; IdP text other than `error` is never logged.
   - Any failure but `login_expired` also clears `__Host-ds_login`, whose code is spent. A
     `login_expired` leaves it, so a stray callback cannot end a sign-in in progress.

   So anonymous callers cannot write to the database.

Steps 3 to 6 run in one transaction, and its audit rows come last (§6).

3. **It upserts `ds.users`** by `(issuer, subject)` with the mapped roles.
   - The upsert writes only when the username, display name or roles differ from the stored row.
     `last_login_at` changes only on success.
   - On `groups_overage` the roles stay unchanged, or `{}` for a new user.
   - A roles change for an existing user is audited `auth.roles_changed {from, to}`.
4. **The session named by the cookie's `replaces`** is revoked, if it is still valid, whatever the
   outcome: a refused re-sign-in must not leave that browser its old roles.
5. **Denials** (`disabled`, `no_role`, `groups_overage`) are audited `auth.login_denied {reason,
   count}` by that user, at most once per user and reason per 10 minutes (§6). For the last two,
   the server log lists the claim names present (never values) and the group count. The redirect
   also expires `__Host-ds_session`.
6. **Success:**
   - delete sessions that ended over 30 days ago;
   - insert a session with a fresh id (no fixation), `roles_sha256` (§3), `auth_time`, `acr` and
     `amr`;
   - audit `auth.login {method: "oidc", roles, reauth}`.

   Then clear `__Host-ds_login` and set `__Host-ds_session`.

**Why `replaces`.** The callback arrives as a cross-site navigation from the IdP. Browsers send the
Lax login cookie there, but never the Strict session cookie. The session to revoke is therefore the
one presented at `/login`, carried in the signed cookie. For the same reason the redirected page load
carries no session cookie either; the SPA's first `/me` fetch does.

### 2.1 Discovery and the IdP client

- **Discovery.** `{issuer}/.well-known/openid-configuration` is fetched when `ds serve` starts and
  cached for 1 hour. A failure at start logs
  `warning: identity provider unreachable at start; sign-in retries on first use`.
- **Checks:**
  - `issuer` matches exactly;
  - endpoints are https (http only for a loopback issuer);
  - `token_endpoint` and `jwks_uri` share the issuer's scheme, host and port;
  - listed `*_supported` values admit S256, an `id_token_algs` entry and `client_auth`.
- **HTTP.** aiohttp with `trust_env=False` and `proxy=auth.oidc.proxy`, one `ClientSession` per
  sign-in, a 10 s timeout, no redirects, JSON of at most 1 MiB, and the system CA store. The IdP is
  reached at the issuer's host from settings. The target SSRF guard does not apply, and no IP
  pinning is done.
- **IdP down.** The server starts, and sessions and tokens keep working, since no request calls the
  IdP. Sign-in gives `idp_unavailable`, and so does a discovery document that fails a check.

### 2.2 Code exchange

`POST token_endpoint` sends `grant_type=authorization_code`, `code`, `redirect_uri` and
`code_verifier`:

| `client_auth` | Sent | Credential |
|---|---|---|
| `client_secret_basic` (default) | `Authorization: Basic b64(urlenc(id):urlenc(secret))` | `client_secret_ref` |
| `client_secret_post` | `client_id` and `client_secret` form fields | `client_secret_ref` |
| `private_key_jwt` | `client_id`, `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`, `client_assertion` | `client_key_ref` (PEM, RSA ≥ 2048 or EC P-256) |

`private_key_jwt` sends `client_id` too: Entra requires it, and RFC 7521 §4.2 allows it.

- **The assertion** carries `iss = sub = client_id`, `aud = token_endpoint`, a 16-byte `jti`,
  `iat` and `exp = iat + 60`. It is signed RS256 or ES256, with header `kid = client_key_id`, and
  `x5t` and `x5t#S256` when `client_cert_file` is set.
- **Credentials** are read when the `Oidc` is built (server start, `ds auth check`) and at every
  exchange, so they rotate without a restart. The new `core/secrets.py read_secret_bytes` reads PEM,
  which `read_secret` refuses as multi-line.
- **Failures.** A non-200 answer gives `login_failed`, and only `error` is logged. Only the string
  `id_token` is kept; access and refresh tokens are dropped.

### 2.3 ID token validation (`validate_id_token`, pure; any failure gives `login_failed`)

1. **Algorithm.** `alg` must be in `id_token_algs`, typed RS/PS/ES 256/384/512, so `none` and `HS*`
   cannot be configured. A `crit` header is refused, and `jku`, `x5u` and `jwk` are ignored.
2. **Key.**
   - The key is the JWKS key for `kid`, with `use` absent or `sig`, and a `kty` and `crv` that fit
     `alg`. Any declared `alg` must be equal.
   - A token without `kid` is accepted only if exactly one key fits.
   - The JWKS is cached for 1 hour. An unknown `kid` refetches at most once per 60 s, and cached
     keys survive a failed refresh.
3. **Decode.** `jwt.decode(token, key, algorithms=[alg], audience=client_id, issuer=issuer,
   leeway=clock_skew_seconds, options={"require": ["iss", "sub", "aud", "exp", "iat"]})`.
4. **`azp`** must equal `client_id` when `aud` has several values or `azp` is present.
5. **Freshness.** `iat ≥ now − 600 s − skew`.
6. **Nonce.** `nonce` must equal the login cookie's (`hmac.compare_digest`).
7. **Subject.** The `subject_claim` value must be a non-empty string of at most 255 UTF-8 octets,
   the unit of the DDL's `octet_length` CHECK.
8. **Step-up.** When the login cookie has `reauth`, `auth_time` must be present and at least
   `started − clock_skew_seconds`.

### 2.4 Claims

- **Identity** is `(iss, subject_claim)`. The default claim is `sub`; Entra uses `oid`, which
  survives an app re-registration.
- **Username** (`actor_name`) is the `username_claim` value, else the subject. `email` is never
  used: Microsoft documents it as mutable and unverified in Entra, and some IdPs let users edit it.
  `username_claim` must name an attribute that only IdP administrators can change: Entra
  `preferred_username` or `upn`, ADFS `upn`.
- **Display name** is `name`, else null.
- **Both names** are cut to 256 characters. A control character (`printable` false) in either gives
  `login_failed`, so no one can plant terminal escapes or a look-alike name.
- **Groups.** The values of the `group_claims` paths are combined.
  - A path is a string or a list of strings.
  - A string is tried as a whole claim name (which covers URI names), then split on `.`, as in
    `resource_access.deltasleuth.roles`.
  - A list is followed segment by segment, for names that contain dots, as in
    `[resource_access, deltasleuth.bank, roles]`.
  - Strings and lists of strings are read; other types are ignored.
  - At most 1,000 values of at most 256 characters are kept. Values are never trimmed or
    case-folded.
- **Roles** are `expand` of every `auth.oidc.roles` role with a configured value that is exactly
  equal (case-sensitive string equality) to one of the groups. Only immutable ids belong there
  (§8).
- **Overage.** `groups_overage` applies when `_claim_names` holds a group path's first segment, or
  `hasgroups` is true. Microsoft Graph is never called.
- **Kept.** `auth_time`, `acr` and `amr` are stored on the session. Token creation uses `auth_time`
  (§5), and so do approvals.

## 3. Sessions, cookies, CSRF, transport

**Sessions.** A row (§7) stores `sha256` of a 32-byte cookie secret, and the principal at sign-in.
`auth/sessions.py` holds the cookie's name and secret, `csrf_of` and `roles_sha256`, and for exception
E1 below `plain(auth)` and `named(name, auth)`.
- **Validity.** `repo.session_by_secret(conn, secret_hash, roles_sha256, *, idle_minutes)` runs one
  indexed SELECT per request. A session is valid when it is:
  - not revoked;
  - within `expires_at` (`session_max_hours`);
  - seen within `session_idle_minutes`;
  - not disabled (a user's), or not revoked or expired (a token's);
  - for a user's session, granted under the running role mapping: its `roles_sha256` equals the
    server's.
- **Idle.** `last_seen_at` is touched only when it is older than 60 s and the request lacks
  `X-DS-Background: 1`. The SPA sends that header when no `pointerdown`, `keydown` or `wheel` came in
  60 s (the Run screen's polls), so the server enforces idle even with a polling tab open.
- **Roles are a snapshot of the IdP.**
  - An IdP change, including a leaver's removal, applies at the next sign-in, at most
    `session_max_hours` later. For immediate effect, disable the user (§3 Revocation).
- **A role-mapping change ends every SSO session.**
  - `roles_sha256` is the hex sha256 of the canonical JSON (sorted keys, no whitespace) of
    `{"group_claims": …, "roles": …}` from `auth.oidc`. `system.start` records the same value.
  - Any edit to either setting, for example removing an over-broad admin group, gives every live
    SSO session 401 `session expired` at its next request. Nobody has to remember to revoke them.
- **`GET /v1/auth/me`** returns `{actor, name, display_name, kind: user|token|dev, roles, csrf, mode,
  idle_minutes, expires_at}`. In developer mode `csrf` is `dev`.

| Cookie | Value | Attributes |
|---|---|---|
| `__Host-ds_session` | 32 random bytes, base64url | `HttpOnly; Secure; SameSite=Strict; Path=/`; no `Domain` or `Max-Age` |
| `__Host-ds_login` | the signed login state (§2) | `HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=600` |
| `ds_session_<port>` (E1: an http `public_url` only) | as `__Host-ds_session` | `HttpOnly; SameSite=Strict; Path=/`; no `Secure`, `Domain` or `Max-Age` |
| `ds_login_<port>` (E1) | as `__Host-ds_login` | `HttpOnly; SameSite=Lax; Path=/; Max-Age=600`; no `Secure` |

**A plain-http `public_url` (exception E1, [FIRSTRUN.md](FIRSTRUN.md) §7).** When
`urlsplit(auth.public_url).scheme` is `http`, which `config._public_url` admits for `localhost`,
`127.0.0.1` and `[::1]` only, the two cookies are named `ds_session_<port>` and `ds_login_<port>`, with
the port of `public_url` (80 when it names none), and carry no `Secure`. A one-machine trial's address,
`http://localhost:8000`, is such a `public_url`.
- **What stays.** `HttpOnly`, `SameSite`, `Path=/`, no `Domain`, the login cookie's `Max-Age=600`, the
  CSRF token, and the Origin and `Sec-Fetch-Site` checks. Under https, a loopback https address
  included, names, attributes and HSTS are as this section says above, byte for byte.
- **The settings decide, never a request.** `sessions.plain(auth)` reads `auth.public_url` (http, and a
  loopback host), and `sessions.named(name, auth)` gives the name: the name without its prefix, `_` and
  the port. No `Host`, `Origin` or forwarded header changes either. A server sets and reads its own
  form's names only: a plain name is never accepted under https, nor `__Host-ds_session` under http, nor
  another port's name. `COOKIE` and `oidc.LOGIN_COOKIE` stay the https names.
- **Why plain.** Only a browser that treats loopback as secure keeps a `__Host-` or `Secure` cookie
  over http, and a dropped cookie fails with no message. *Observed on 2026-10-05, with a test page and
  not a sign-in:* Chrome 154 and Chromium 152 kept such a cookie over `http://localhost`; WebKit on
  macOS 27.0.1 dropped it, and kept the same cookie without the prefix. *Observed on a container, on
  2026-10-05:* a token sign-in at `http://localhost:18005` answered `Set-Cookie: ds_session_18005=…;
  HttpOnly; Path=/; SameSite=strict`, and curl's cookie store returned it (200 from `/v1/auth/me`).
  *Not observed:* Safari itself, Firefox, and any browser signing in with E1's cookies. The tests' own
  cookie jar returns no `Secure` cookie over http, and signs in with E1's (§10).
- **Why the port.** A browser keeps cookies by host, not by port. With one name, two installations at
  `http://localhost:8000` and `http://localhost:18000` would replace each other's session at every
  sign-in, silently. With the port in the name one browser stays signed in to both (seen with one
  curl cookie store and two installations, at ports 18005 and 18021, on 2026-10-05: both signed in,
  and signing out of one left the other; not seen in a browser).
- **Every port of the host receives them.** For the same reason a browser sends these cookies to an
  application at another port of the host, such as one a comparison reads, and a request to it copied
  from the browser as cURL carries them. The builder's import leaves DeltaSleuth's session and sign-in
  cookies out by name, and `PUT /v1/targets/{id}` refuses a credential that holds this installation's
  own ([DEPLOY.md](DEPLOY.md) section 7, Credentials).
- **Given up, on loopback http only:**
  - where a browser honours the prefix, its refusal of a same-named cookie planted from another
    loopback origin. A planted value the server never issued matches no session row (401), and every
    sign-in mints a new secret, so no planted value becomes the victim's session. **A session swap
    remains:** a planter who holds a live session of its own on that installation (any valid token, or
    an SSO account) can plant that session's secret, and the server, which cannot tell who presents a
    secret, answers the victim's browser as the planter. The victim then works and is audited as the
    planter, what is kept by actor (a chat session) is the planter's, and the victim's sign-out ends
    the planter's session, not its own, which stays live until it idles out. The Origin check binds
    unsafe requests to the port and does not help here: after the swap the requests come from the
    installation's own page. *Seen on 2026-10-05 with a cookie header in the tests' client, in a
    check the suite does not hold. Not seen in a browser;*
  - no TLS channel is there for `Secure` to protect. **A plain cookie reaches every http service on
    that loopback name:** the browser sends it whatever the port, so another installation, or any local
    program serving `localhost`, receives the session secret. By the same rule a browser that keeps the
    prefixed cookie over loopback http sends it to those ports too (not observed).
- **Consequence.** Moving `public_url` between http and https, or to another loopback port, signs
  every browser out once, as did this change for a browser that held `__Host-ds_session` at
  `http://localhost`: the server no longer reads the cookie the browser holds. **The session itself is
  not ended.** Its row is found by the secret's hash alone (Sessions, above), whatever the cookie's
  name, so the same secret sent under the new form's name is still that session until it idles out,
  reaches `session_max_hours` or is revoked. *Seen on 2026-10-05 with the tests' client and one
  database, in checks the suite does not hold:* a secret minted at
  `http://localhost:8000` answered 200 as `__Host-ds_session` at `https://ds.test`, in tokens mode and
  in oidc mode, and as `ds_session_18000` at `http://localhost:18000`; a sign-out carrying only the old
  name answered 400 `only a browser session signs out` and revoked nothing. A token's session is no
  session once the mode is `oidc` (401 `session expired`), whatever its cookie. Where the move must
  end the sessions, those whose secret travelled without `Secure` among them, run `ds session revoke
  --all` ([DEPLOY.md](DEPLOY.md), "From a one-machine trial").

**CSRF has four layers.** Bearer requests skip layer 2 only.
1. `SameSite=Strict`.
2. `X-CSRF-Token = b64url_nopad(sha256(b"ds-csrf/v1\x00" + cookie.encode()))`, compared in constant
   time. The SPA reads it from `/me` and keeps it in memory. No CORS is enabled.
3. The Origin check.
4. The `Sec-Fetch-Site` check.

**`_Guard`**, the existing middleware, in order:
1. **Request id.** `scope["state"]["request_id"] = "Q-" + token_hex(8)`, returned as
   `X-Request-ID`. Inbound ids are ignored.
2. **Developer mode**, on every path:
   - a client that is not loopback gets 403 `developer mode serves loopback clients only`;
   - a request carrying any of `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host`,
     `X-Forwarded-Proto`, `X-Real-IP` or `Via` gets 403 `developer mode refuses proxied requests`.

   A reverse proxy on the same host connects from 127.0.0.1 and can send a loopback `Host`, so the
   loopback checks alone would let every client of the proxy in. **Remaining gap:** a proxy or port
   forward that adds none of these headers (for example a bare `proxy_pass`, or `ssh -L`) cannot be
   detected. DEPLOY and RUNBOOK say that developer mode must never be proxied or forwarded.
3. **Host.** On `/v1` paths except `GET /v1/health` (probes send an IP), `Host` must be:
   - the `public_url` host and port;
   - an `auth.allowed_hosts` entry;
   - or a loopback name (`localhost`, `127.0.0.1` or `[::1]`, any port).

   Hosts compare case-insensitively. A value without a port takes the default port of
   `public_url`'s scheme (443 or 80), on both sides. So nginx's `$host`, which drops the port,
   matches `https://ds.bank.example`. Developer mode allows loopback only. Anything else gets 400
   `unexpected Host header`, which blocks DNS rebinding.
4. **Cross-site requests.**
   - Unsafe methods: a present `Origin` must be the `public_url` origin (loopback in developer mode),
     else 403 `cross-origin request refused`; a present `Sec-Fetch-Site` must be `same-origin`, else
     403 `cross-site request refused`. This closes today's cross-site `text/plain` POST, which
     `specs._document` parses as YAML.
   - Other `/v1` requests: a present `Sec-Fetch-Site` must be `same-origin` or `none`, else 403
     `cross-site request refused`, except `GET /v1/health`, `GET /v1/auth/callback` and
     `GET /v1/auth/login`, which refuses cross-site requests itself (§2). A page
     elsewhere on the site (an `<img>` on the intranet wiki) would otherwise make reads the audit
     records, such as `key.view` and `report.download`, in its viewer's name.
5. **Headers.**
   - Kept: the body cap and security headers. The CSP gains `form-action 'self'`.
   - A 500, which Starlette sends from outside `_Guard`, carries the same headers (request id,
     `no-store`, HSTS).
   - New: `/v1` responses carry `Cache-Control: no-store`, and every response carries HSTS
     (`max-age=31536000`) when `public_url` is https.

**Client address.**
- **Resolution.** With `auth.trusted_proxies` set, `create_app` adds
  `uvicorn.middleware.proxy_headers.ProxyHeadersMiddleware` with
  `trusted_hosts=auth.trusted_proxies`. It is added after `_Guard`, so it runs outermost (Starlette
  runs the last-added middleware first), and `_Guard`, `principal()` and the audit all see the
  resolved address. The same code runs under TestClient (§10).
- **Uvicorn.** `ds serve` always passes `proxy_headers=False`, in every mode, so uvicorn's own
  default (trust 127.0.0.1) never applies.
- **Result.** `request.client.host` is the rightmost untrusted `X-Forwarded-For` hop, which a client
  cannot forge. Without `trusted_proxies` it is the TCP peer.
- **Docker.** When nginx runs on the host, the app sees the compose network's gateway, not nginx's
  address. DEPLOY pins the subnet and the gateway (`172.30.0.1`), and `trusted_proxies` names the
  gateway alone (`[172.30.0.1/32]`), so containers on the network are not trusted. Every host process
  that can reach the published port also arrives through the gateway, so in that layout the address
  is advisory; it is exact when the TLS proxy is the app's only client (DEPLOY §5). A wrong value
  fails safe, but every user then shares one address.

**Access log.** `ds serve` adds `api.app.AuthQueryFilter` to the `uvicorn.access` logger. For a
record whose path starts with `/v1/auth/`, the filter drops the query from the path argument. The
callback's `code`, `state` and `iss` therefore never reach `docker logs` or the SIEM.

**`POST /v1/auth/logout`** needs the session cookie; a caller without one gets 400
`only a browser session signs out`.
- A live session needs its CSRF token; it is revoked and audited `auth.logout`.
- A session that no longer resolves (idle, or granted under another mode or role mapping) is revoked
  without an audit row, so a later settings change cannot revive it; the answer below is the same,
  so an idle browser still signs out of the IdP. `auth.logout` is written only when this request
  revoked the session, so a double click or a second tab writes one row.
- The SPA treats a 400 or 401 from sign-out (another tab already signed out) as signed out.
- `logout_url` is built after the revocation. When a discovery refresh fails, the endpoint of the
  document last checked is used.
- It expires the cookie and sends `Clear-Site-Data: "storage"`. It never sends `"cookies"`, which
  would clear the whole registrable domain.
- It returns `{"logout_url": "<end_session_endpoint>?client_id=…&post_logout_redirect_uri={public_url}/"}`,
  or `null` when `end_session` is false or the IdP has no endpoint.
- The SPA clears `sessionStorage` and navigates there, or to `#/signin?signed_out=1`.

**Revocation** applies at the next request.
- **Admins:** `DELETE /v1/auth/sessions/{session_id}` and
  `POST /v1/auth/users/{user_id}/disable {revoke_tokens?}`.
- **Host administrators (break-glass, §4.2):** `ds session revoke (--session W-…|--user U-…|--all)`
  and `ds users disable U-… [--revoke-tokens]`.
- **Disable** sets `disabled_at` and `disabled_by` and revokes the user's sessions.
  - With `revoke_tokens: true` (default false) it also revokes every live token whose `created_by`
    or `owner_user_id` is that user.
  - It returns `{user_id, disabled_at, sessions_revoked, tokens, tokens_revoked}`. `tokens` lists
    `{token_id, name, role, state}` for every such token, so the admin sees what would outlive the
    person.
  - It answers 404 for an unknown user, and 409 `you cannot disable yourself` or
    `user U-… is already disabled`.
- **`/enable`** returns `{user_id, disabled_at: null}`. It answers 404 for an unknown user, and 409
  `user U-… is not disabled` when the user is not disabled. The CLI can always re-enable. Enabling
  never restores revoked tokens.
- **`DELETE /v1/auth/sessions/{session_id}`** returns `{session_id, count}`, or 404
  `session W-… not found` when no live session has that id.
- **Leavers.** Removal in the IdP stops sign-in, but tokens stay valid until the user is disabled
  here with `revoke_tokens`. RUNBOOK's leaver step says so.
- **`GET /v1/auth/users`** shows the roles at the last sign-in, for access reviews.
- **Deferred:** back-channel logout. `id_token_hint` is not stored, so Okta needs
  `end_session: false`.

## 4. Roles and permissions

### 4.1 Routes

| Route | Role | Audit action |
|---|---|---|
| `GET /v1/health` | public | — |
| `GET /v1/auth/login`, `GET /v1/auth/callback` | public (oidc) | `auth.login`, `auth.login_denied`, `auth.roles_changed` |
| `POST /v1/auth/token-login` | public (tokens mode; else 404 `token sign-in is not enabled`) | `auth.login {method: "token"}` |
| `GET /v1/auth/me` | any principal | — |
| `POST /v1/auth/logout` | a cookie session | `auth.logout` |
| `GET /v1/targets`, `/llm/profiles`, `/specs`, `/specs/{spec_id}` | viewer | — |
| `PUT /v1/targets/{target_id}`; `DELETE /v1/targets/{target_id}` | who declares (`deps.Declarer`, from the settings at each request; `GET /v1/targets` names the role as `declarer`, and shows what a target of the product's list was declared with to its holders only): admin, signed in within 15 minutes (§5); in a trial, which is tokens mode with a plain-http loopback `public_url` (§3, E1) as `deploy/up.sh` writes, spec_author. Tokens mode behind https has no admin and keeps the targets file (DEPLOY section 7) | `target.set`; `target.delete` |
| `GET /v1/licence` | viewer | — ([LICENCE.md](LICENCE.md) §12) |
| `POST /v1/specs/validate`, `POST /v1/profile` | spec_author | — |
| `POST /v1/specs/try` | spec_author: one side's request of a form sent once to its endpoint, nothing stored. A trial (tokens mode with a plain-http loopback `public_url`) reads the answer as it came; any other installation its structure only, every value a stand-in; neither a string that holds a credential stored for the target | `target.try`, committed before the call |
| `POST /v1/specs` | spec_author | `spec.create` (every upload) |
| `POST /v1/runs` | operator | `create_run`; a new pair's `licence.pair`; for a run the schema guard creates paused, `paused` and `schema.drift`, and `schema.drift` alone for a change it only warns of ([PRECHECKS.md](PRECHECKS.md) §7, V1) |
| `POST /v1/runs/{run_id}/{action}` | operator | `paused`, `running`, `cancelling`; a cancel's `run.finished`; a resume of a run the schema guard paused adds `schema` to `running` when it accepts the changes, then `schema.drift` when its look saw a new state (V4) |
| `POST /v1/compare` | operator | `compare_one`, a new pair's `licence.pair`, `run.finished` |
| `GET /v1/approvals`, `/approvals/{approval_id}` | viewer | — |
| `POST /v1/specs/{spec_id}/approvals` | spec_author (SSO only) | `approval.request` |
| `POST /v1/runs/{run_id}/cycles/{cycle}/signoff` | operator (SSO only) | `approval.request` |
| `POST /v1/approvals/{approval_id}/decision` | rule_approver; approving needs a sign-in within 15 minutes (§5) | `approval.grant` (a sign-off's last step also `retention.hold`), `approval.reject` |
| `POST /v1/approvals/{approval_id}/revoke` | rule_approver | `approval.revoke`; the runs it `paused` and stopped (`cancelling`); a cascade's `approval.revoke` and `approval.reject`; `retention.release`; the revoked sign-offs' `pack.revoke` |
| `POST /v1/runs/{run_id}/cycles/{cycle}/adjudications`; `GET` the same | operator; viewer (SSO only, [PACK.md](PACK.md) §7.1) | `adjudication.draw`; — |
| `GET /v1/adjudications/{adjudication_id}/sheet.csv`; `POST /v1/adjudications/{adjudication_id}/score` | viewer; operator (SSO only) | —; `adjudication.score` |
| `POST /v1/runs/{run_id}/cycles/{cycle}/labellings`; `GET` the same | operator; viewer (every auth mode, [ACCURACY.md](ACCURACY.md) §10) | `accuracy.draw` (a draw repeated within ten minutes returns the first labelling and audits nothing); — |
| `GET /v1/labellings/{labelling_id}`, `/labellings/{labelling_id}/sheet.csv`; `POST /v1/labellings/{labelling_id}/labels` | viewer; operator | —; `accuracy.label` |
| `POST /v1/runs/{run_id}/cycles/{cycle}/packs` | operator, a person (SSO only; else 403 `issuing a pack needs a signed-in person`) | `pack.issue` |
| `GET /v1/packs`, `/packs/{pack_id}`, `/packs/revocations`; `GET /v1/packs/{pack_id}/download` | viewer (SSO only) | —; `pack.download`, before the bytes are sent |
| `GET /v1/samples`, `/samples/{draw_id}` | viewer ([SAMPLING.md](SAMPLING.md) §14) | — |
| `POST /v1/samples/close` | operator, a person (SSO only: else 409 `closing draws needs SSO (auth.oidc); …`, and 403 `closing draws needs a signed-in person`) | `sample.close`, one per draw closed |
| `GET /v1/notifications`, `/notifications/deliveries` | viewer ([ALERTS.md](ALERTS.md) §11) | — |
| `POST /v1/notifications/channels/{channel}/test`, `/retry` | operator (a test: one per channel in 10 s, else 429) | `notify.test`; `notify.retry` |
| `POST /v1/notifications/mutes`; `POST /v1/notifications/mutes/{mute_id}/end` | admin, signed in within 15 minutes (§5); admin | `notify.mute`; `notify.unmute` |
| `GET /v1/readiness`, `/readiness/pairs/{name}`, `/readiness/pairs/{name}/cycles`, `/readiness/pairs/{name}/brief`, `/readiness/transitions` | viewer ([READINESS.md](READINESS.md) §10, §12): the transitions and `as_of` are projections of the worker's `readiness.changed` rows only (ids, states, counts and slugs; no actor, address or request data), so reading them is not `audit.read` | — |
| `GET /v1/readiness/reports/{name}` | viewer | `report.download {name, programme, config_sha256}` on object `readiness`, once the report is built; a pair's `brief.pdf`, `brief.csv` and `causes.csv` add `pair`, their `programme` null |
| `GET /v1/schedules`, `/schedules/zones`, `/schedules/{schedule_id}`, `/schedules/{schedule_id}/firings` | viewer ([SCHEDULES.md](SCHEDULES.md) §10): the firings and a schedule's `last` are projections of the `schedule.fire`, `schedule.missed` and `schedule.rezone` rows only (ids, times and closed codes; no actor, address or request data), so reading them is not `audit.read` | — |
| `POST /v1/schedules/preview` | operator | — (it writes nothing) |
| `POST /v1/schedules`; `PUT /v1/schedules/{schedule_id}`; `POST /v1/schedules/{schedule_id}/pause`, `/resume`; `DELETE /v1/schedules/{schedule_id}` | operator, in every auth mode (a token and developer mode too); no step-up, and never the licence | `schedule.create`; `schedule.update`; `schedule.pause`; `schedule.resume`; `schedule.delete`. A `PUT`, a pause and a delete first write `schedule.missed` for the times they left unstarted (one counted row) and for a held range, as the caller |
| `GET /v1/prechecks`, `/prechecks/{precheck_id}`, `/prechecks/{precheck_id}/pairs/{n}` | viewer ([PRECHECKS.md](PRECHECKS.md) §9, §11) | — |
| `POST /v1/prechecks`; `POST /v1/prechecks/{precheck_id}/cancel` | operator, in every auth mode; no step-up, no four-eyes object, and never the licence | `precheck.create`; `precheck.cancel` |
| `POST /v1/prechecks/catalogue` | spec_author (it is authoring, as `POST /v1/profile`) | `precheck.create`; when every name is unpaired, `precheck.finished` by `system:worker` in the same transaction |
| `GET /v1/prechecks/{precheck_id}/reports/{name}` | viewer | `report.download {name}` on object `PC-…`, before the bytes |
| `GET /v1/prechecks/{precheck_id}/pairs/{n}/draft.yaml` | spec_author | — (unaudited, as `POST /v1/profile`: never stored) |
| `GET /v1/runs`, `/runs/{run_id}` and its `/results`, `/findings`, `/root-causes`, `/changes`, `/graph` | viewer | — |
| `GET /v1/runs/{run_id}/keys/{key:path}` | viewer | `key.view {key_masked, cycle}` |
| `GET /v1/runs/{run_id}/reports/{name}` | viewer | `report.download {name, cycle}` |
| `POST /v1/chat/sessions` | viewer | `chat.create` |
| `POST /v1/chat/sessions/{session_id}/messages` | viewer and creator | `chat.ask`; the agent's `compare_one`, `licence.pair` and `run.finished` |
| `POST /v1/chat/sessions/explain` | viewer, in every auth mode; its agent acts as a viewer whoever asks, so it compares nothing (CONTRACTS §6) | `chat.create`, then `chat.ask {question: "explain", root_cause_id}` or `chat.ask {question: "summarise"}` |
| `GET /v1/chat/sessions/{session_id}` | viewer and creator | — |
| `POST /v1/mcp` | viewer, by an API token or in developer mode: a cookie session gets 403 `MCP takes an API token (Authorization: Bearer dst_…), not a browser session`; 404 `MCP over HTTP is off (mcp.http, docs/mvp/MCP.md §4)` unless the settings serve it ([MCP.md](MCP.md) §5.1) | a tool call's own: `key.view`; `mcp.read`; `compare_key`'s `compare_one` and `run.finished`; a role refusal's `authz.denied` (§6). Inside, the caller's roles are cut to viewer and operator (§4.3) |
| `GET /v1/mcp`; `DELETE /v1/mcp` | viewer | — (405 `MCP is served by POST only: no stream and no session`, with `Allow: POST`; 404 while MCP over HTTP is off) |
| `GET /v1/auth/tokens`; `DELETE /v1/auth/tokens/{token_id}` | admin | `token.revoke` |
| `POST /v1/auth/tokens` | admin, signed in within 15 minutes (§5) | `token.create` |
| `GET /v1/auth/sessions`; `DELETE /v1/auth/sessions/{session_id}` | admin | `session.revoke` |
| `GET /v1/auth/users`; `POST /v1/auth/users/{user_id}/disable`, `/enable` | admin | `user.disable`, `user.enable` |
| `GET /v1/audit` | admin | `audit.read {filters}` |

**Rules:**
- **Chat.** A session whose `ds.chat_sessions.created_by` is not `who.actor` gives 404
  `chat session S-… not found`. Pre-005 rows match no one.
  - **What it protects:** the transcript. Questions are free text stored as typed (CONTRACTS §6),
    so only the creator may read or continue a session.
  - **What it does not protect:** answers. They are validated, cite only the run's masked data,
    and are run-level evidence: `report.pdf`'s AI annex (`reports/pdf.py`, `repo.chat_answers`)
    shows every session's answers, without the questions, to any viewer of the run.
- **AI off.** `require_ai` takes `_: Viewer`, so a caller gets 401 or 403 before 503 `AI disabled`.
- **Status codes.**
  - 401: no credential, an ended session, or a bad token.
  - 403: a role, CSRF, Origin, `Sec-Fetch-Site`, developer-mode or `reauth_required` refusal.
  - 400: a bad `Host`.
  - 409: a run of a spec version that lacks its four-eyes approval, and an approval request or
    decision its state refuses (APPROVALS.md §6.2); new work a vendor build's licence refuses (`LicenceRefused`:
    runs, compares, a licence-paused resume, sign-off requests and approval steps, packs;
    [LICENCE.md](LICENCE.md) §8, §9), after the route's role check; a hash sample's run refused
    (`SampleRefused`: 409 for R1 and R5, 422 for R2–R4) and a close with nothing to close (C4),
    [SAMPLING.md](SAMPLING.md) §13; every notification POST while `notify` is absent, and a mute ended
    or lapsed ([ALERTS.md](ALERTS.md) §10: `notifications.Refused` carries its status, 404, 409, 422 or
    429); a labelling's draw or upload while its cycle has a requested or approved sign-off, has not
    ended or lacks a population asked for, and a labelling labelled already
    ([ACCURACY.md](ACCURACY.md) §11, with its 410, 413 and 422).
  - 504: a labelling's draw stopped after 300 s (`accuracy.DrawTimeout`): nothing was drawn.
  - Other codes are unchanged.
- **Developer mode** acts as admin, except for the person-required roles (§1).

### 4.2 CLI

The direct-database CLI is the **host-administrator channel**, outside the role model, because
whoever holds the DSN file and `tokeniser.key` can bypass any check with `psql`.
- **Break-glass.** In oidc mode the admin API and `Admin.tsx` are the normal path for tokens, users
  and sessions. The CLI equivalents are for recovery, and for tokens mode, which has no admin over
  the API.
- **Attribution is only as strong as host access control.**
  - `DS_OPERATOR` is asserted, not verified. In the documented deployment every CLI container runs
    as `deltasleuth`.
  - The authoritative record of who ran a command is the host's audit of `docker compose run` and
    `exec` (DEPLOY §10). architecture.md D.1 says the same.
- **Principal.** `local:<operator>@<host>`, with every role (the CLI calls no `require`).
  - `operator` is `DS_OPERATOR`, and `host` is `socket.gethostname()`. DEPLOY gives the `app`
    service a stable `hostname:`, so `host` does not change with each container.
  - In each part, characters outside `[A-Za-z0-9._-]` become `_`. The parts are then cut to 64 and
    253 characters, which the actor CHECK allows, and an empty part becomes `unknown`.
- **Name.** `actor_name` is `DS_OPERATOR` as given. It must be 1 to 256 printable characters, else
  exit 2 with `error: DS_OPERATOR must be 1 to 256 characters without control characters`.
- **Detail.** Every `local:` row merges `{"asserted": true, "os_account": <pwd.getpwuid(os.getuid()).pw_name>}`
  (the numeric uid when it has no passwd entry, as in some containers).
- **Required with `auth`.** When the settings have an `auth` block, a command marked "yes" below
  exits 2, before any work, with `error: set DS_OPERATOR to your user name (docs/mvp/AUTH.md §4.2)`
  when `DS_OPERATOR` is unset or empty. Without `auth` (developer mode) the operator defaults to the
  OS account, so a workstation needs no setup.
- **Request id.** One `C-<16 hex>` per invocation; `ds mcp`, which serves many calls in one, gives each
  tool call its own ([MCP.md](MCP.md) §5.2).

[RETENTION.md](RETENTION.md) §8 extends this table and §6's with the retention and key commands and
actions; their rows are repeated here.

| Command | Actor | Audit | `DS_OPERATOR` |
|---|---|---|---|
| `ds init`, `spec validate`, `profile`, `llm-probe` | — (no store) | — | no |
| `ds migrate` (and every command that opens the store, at start) | `system:migration` | `system.migrate {versions}`; `keys.created {kek}` (the first start's address and evidence keys); `keys.rewrap {from, to, rows}` and a new evidence key's `keys.created` (`ds serve` only, after a KEK is appended; RETENTION §6.1); `spec.rehash {spec_ids}` when 008 finds stored bodies that do not re-hash (APPROVALS.md §7); `installation.created {installation_id}` when 010 applies ([LICENCE.md](LICENCE.md) §5) | no |
| `ds serve` | `system:server`; the API's principals | `system.start {version, auth, roles_sha256, retention, keks, current_kek, missing_keks, pack_key, licence, mcp, audit_head}` | no |
| `ds worker`, and `ds run`'s in-process worker | `system:worker` | `paused` (auth pause, a missing KEK's, a refused draw's, and a soak's boundary without the approval it needs, refused by the licence or after a `bottom_k` draw that did not pass), `cancelling` (stop conditions), `rolling_resolve_failed`, `run.finished`, `run.cycle`; a sampled run's `sample.draw` and `sample.draw_failed` ([SAMPLING.md](SAMPLING.md) §5); the hourly purge's `retention.abandoned` (with an abandoned run's `cancelling` and `run.finished`), `retention.expire`, `retention.purged` and `retention.finished`; `licence.state` when the licence's state changes (looked at hourly); the notifier's `notify.config` (when the routing changes), `notify.skipped` and `notify.dead` ([ALERTS.md](ALERTS.md) §12); a schedule's `create_run` (with a new pair's `licence.pair`) and `schedule.fire`, `schedule.missed`, `schedule.rezone` and, for a cron or zone it cannot load, `schedule.pause` ([SCHEDULES.md](SCHEDULES.md) §5, §11): by the worker of `ds worker` or `ds serve` only, since `ds run`'s in-process worker fires and rezones nothing; the schema guard's `paused {reason: run_paused_schema_drift}` and `schema.drift` ([PRECHECKS.md](PRECHECKS.md) §7, V2, V3, V5); a table pre-check's `precheck.finished` (§5) | no |
| `ds compare`, `ds run` | local | `spec.create` (not for a version already stored, under SSO), then `compare_one` and `run.finished`, or `create_run`; a new pair's `licence.pair` | yes |
| `ds runs` | local | — (each run with its approval and licence labels, and the schema guard's pause line under a run it paused) | no |
| `ds precheck [--json]`; `ds precheck show PC-… [--pair N] [--verdict V] [--json]` | local | — ([PRECHECKS.md](PRECHECKS.md) §11) | no |
| `ds precheck run [SPEC.yaml…] [--spec-id N]… [--spec NAME]… [--all-specs] [--a TARGET[:SCHEMA] --b TARGET[:SCHEMA] [--include GLOB]… [--exclude GLOB]…] [--level L] [--no-wait] [--fail-on refused\|differs\|incomplete] [--json]` | local; the pairs are read by an in-process worker (`system:worker`) unless `--no-wait` | `spec.create` for a file not stored yet, `precheck.create`; the worker's `precheck.finished` | yes |
| `ds precheck cancel PC-…` | local | `precheck.cancel` | yes |
| `ds precheck report PC-… --out DIR` | local | `report.download {name}` on object `PC-…`, one row per file | yes |
| `ds precheck draft PC-… (--pair N \| --all) --out DIR` | local | — | no |
| `ds approvals [--status S] [--kind spec\|signoff] [--needed] [--limit N] [--json]` | local | — | no |
| `ds approvals revoke A-… --reason TEXT` (break-glass) | local | as the API's revoke | yes |
| `ds report`, `measure`, `adjudicate`, `score` | local | `report.download {name, cycle}` | yes |
| `ds token create\|revoke`; `ds token list` | local | `token.create`, `token.revoke`; — | yes; no |
| `ds session revoke` | local | `session.revoke` | yes |
| `ds users disable U-… [--revoke-tokens]\|enable U-…`; `ds users [--json]` | local | `user.disable`, `user.enable`; — | yes; no |
| `ds audit [--actor --action --object --session --since --until --limit --json]` | local | `audit.read` | yes |
| `ds audit verify [--siem FILE]` | — | — (checks the hash chain, [PACK.md](PACK.md) §4.2) | no |
| `ds packs [--run R-…] [--json]`; `ds packs key --out DIR`; `ds packs revocations --out DIR` | local; — (the settings and key only); local | — | no |
| `ds packs download P-… --out DIR` | local | `pack.download`, once the file is written | yes |
| `ds packs verify FILE.zip --fingerprint HEX [--revocations DIR]` | — (a file only) | — | no |
| `ds auth check` | — (IdP only) | exit 0, or 2 naming the failing check | no |
| `ds purge [--limit N] [--orphans]` | local | as the worker's hourly purge | yes |
| `ds purge --run R-… [--cycle N] --reason TEXT` (an erasure) | local | a live run's `cancelling` and `run.finished`; `retention.expire` (reason `erasure: <text>`), `retention.purged` per cycle; `retention.finished` | yes |
| `ds purge --dry-run` | local | — | no |
| `ds purge hold R-… --reason TEXT`, `ds purge release R-…` | local | `retention.hold`, `retention.release` | yes |
| `ds keys status [--verify] [--json]`; `ds keys fingerprint FILE` | local; — (a file only) | — | no |
| `ds keys migrate [--accept-missing]` | local | `keys.migrate` | yes |
| `ds licence [--json]`; `ds licence id`; `ds licence show FILE\|-` | local; local; — (a file only) | — ([LICENCE.md](LICENCE.md) §12) | no |
| `ds licence install FILE\|-`; `ds licence withdraw LI-… --reason TEXT` | local | `licence.install`; `licence.withdraw` | yes |
| `ds samples [--spec NAME] [--json]`; `ds samples size --tier standard\|high [--failures N]` | local; — (no store, no settings) | — ([SAMPLING.md](SAMPLING.md) §6, §14) | no |
| `ds labellings [--run R-…] [--json]` | local | — ([ACCURACY.md](ACCURACY.md) §10) | no |
| `ds readiness [--programme P] [--entity E] [--state S] [--as-of T] [--json] [--fail-unless STATE]`; `ds readiness pair NAME [--bucket B] [--precheck] [--json]`; `ds readiness check`; `ds readiness verify` | local | — ([READINESS.md](READINESS.md) §10, §12) | no |
| `ds readiness report --out DIR [--programme P \| --pair NAME]` | local | `report.download {name, programme, config_sha256}` on object `readiness`, one row per file; with `--pair`, the pair's three files (`brief-<pair>.pdf`, `brief-<pair>.csv`, `causes-<pair>.csv`), each row with `pair` | yes |
| `ds schedules [--spec NAME] [--state S] [--set-by ACTOR] [--json]`; `ds schedules show NAME\|SC-… [--json]` | local | — ([SCHEDULES.md](SCHEDULES.md) §10) | no |
| `ds schedules next CRON --tz ZONE [--count N]` | — (no store, no settings) | — | no |
| `ds schedules add NAME (--spec-id N \| --spec NAME) [--soak [--rolling]] --cron TEXT --tz ZONE [--start-within MIN] [--ends TIME]`; `ds schedules set NAME\|SC-… …` | local | `schedule.create`; `schedule.update` (after `schedule.missed` if a time was due) | yes |
| `ds schedules pause (NAME\|SC-…\|--all)`; `ds schedules resume NAME\|SC-…`; `ds schedules delete NAME\|SC-…` | local | `schedule.pause`, one per schedule paused; `schedule.resume`; `schedule.delete` (a pause and a delete after `schedule.missed` if a time was due) | yes |
| `ds notify [--json]`; `ds notify explain R-… [--cycle N] [--json]`; `ds notify deliveries [--channel] [--state] [--run] [--limit N] [--json]` | local | — ([ALERTS.md](ALERTS.md) §11) | no |
| `ds notify check` | — (the settings, secrets and DNS only; sends nothing) | exit 0, or 2 naming each failing check | no |
| `ds notify test CHANNEL`; `ds notify retry CHANNEL [--since TIME]` | local | `notify.test`; `notify.retry` | yes |
| `ds notify mute (--channel X\|--run R-…) --hours N --reason TEXT`; `ds notify unmute M-…` | local | `notify.mute`; `notify.unmute` | yes |
| `ds mcp [--role viewer\|operator] [--host-channel]` | local, its roles cut to `--role` (viewer unless given), `via: mcp:stdio`; a request id per tool call | as `POST /v1/mcp`'s tool calls ([MCP.md](MCP.md) §9) | yes |
| `ds mcp check` | — (the settings only: no store) | — | no |

- **`ds token create --name --owner [--owner-user U-…] --role [--days]`** prints only the token on
  stdout. Its id, role and expiry go to stderr. Without `auth`, `days` is at most 90, as over the API.
- **Refusals** are one `error:` line and exit 2, writing no row: `token T-… not found`,
  `token T-… is already revoked`, `session W-… not found` (no live session has that id),
  `user U-… not found`, `user U-… is already disabled`, `user U-… is not disabled`, and
  `name one of --session W-…, --user U-… or --all`. `--user` and `--all` are audited even when they
  revoke nothing.
- **`ds users --json`** prints the rows of `GET /v1/auth/users`. `ds users` options go before a
  subcommand's name only for the listing; `ds users --config … disable` is refused with
  `put the options after 'disable'`.
- **`ds audit`** takes `--since` and `--until` as ISO 8601 (UTC without an offset) and `--limit` from 1
  to 500 (default 100). `--json` prints the rows; `audit.read` records the filters as the API does.
- **Report commands.** Each written report is audited once it is written, before its path is printed:
  `ds report` one row per `--kind` (the P0 summary as `summary`), and `measure`, `adjudicate` and
  `score` the file they write (`measure.json`, `adjudication.csv`, `adjudication_score.json`). The P0
  tools build and write in one call, so the row cannot come before the file, as a download's does
  (§6); the CLI operator can read the store directly anyway.
- **Licence** ([LICENCE.md](LICENCE.md) §10, §12). `ds serve`, `worker`, `run` and `compare` print each
  licence warning as `warning: <line>` on stderr; no other command does. A refusal is one `error:` line,
  exit 2 (`LicenceRefused` in `_EXPECTED`), writing no run: `ds run` and `ds compare` have stored the
  spec file first (a new version, audited `spec.create`), as an upload, which always works. Licences are
  installed and withdrawn only here: no route writes one. `ds report` names an unlicensed evaluation's CSV and PDF files
  `R-…-unlicensed-evaluation-<name>`. All of that is a vendor build's: an open build prints no licence
  warning, refuses no run for one, labels no new run's files and refuses `ds licence install` and `show`
  (I1, exit 2), while `ds licence` prints `licence none: this build needs no licence` and `state open`
  (LICENCE §19).
- **Notifications** ([ALERTS.md](ALERTS.md) §10, §11). Every `ds notify` command but the listing and
  `deliveries` exits 2 with `error: notifications are off: …` while the settings have no `notify`; the
  listing then prints that sentence and exits 0. `ds serve`, `worker` and `run` (with its in-process
  worker) print `warning: notify channel <name>: <SecretError text>; its deliveries retry until it is
  readable` for each channel whose secret cannot be read, and start anyway. The CLI mutes as the admin it
  is: tokens mode has no admin session, so muting there is the CLI's.
- **Readiness** ([READINESS.md](READINESS.md) §10, §12). The CLI reads readiness and writes none: only
  the worker measures cycles and records transitions, and the gates are the settings file's. `ds
  readiness` exits 1 with `--fail-unless STATE` when a listed pair is below that state, for a pipeline;
  a refusal (an unknown pair, programme or state, a malformed `--as-of`) is one `error:` line, exit 2.
  `ds readiness verify` exits 2 naming the first cycle whose stored measures do not hash to their
  `readiness.measured` row (edited, replaced or removed since), have no such row or more than one, are
  neither measures nor an excluded marker, or whose excluded marker does not agree with its run.
  `ds readiness check` prints each pair's effective gates and warns on stderr of a listed spec that is
  not stored. `ds readiness report --pair NAME` exits 2, writing and auditing nothing, for an unknown
  pair and with `--programme`.
- **Schedules** ([SCHEDULES.md](SCHEDULES.md) §10). `ds schedules` and `show` only read, and `next`
  opens neither the settings nor the store. `add`, `set`, `pause`, `resume` and `delete` write as
  `local:<operator>@<host>`, so they need `DS_OPERATOR` before any work when auth is configured; a
  refusal is one `error:` line with SCHEDULES §9's message, exit 2, and a warning is `warning: …` on
  stderr. The CLI starts no run of a schedule: only a worker fires. A schedule the CLI saved is set by
  a `local:` actor, which the setter check takes as live (SCHEDULES §6), and it is stamped with the auth
  mode of the settings the command ran under, the mode a worker must run in to fire it.
- **Table pre-checks** ([PRECHECKS.md](PRECHECKS.md) §11). `ds precheck` and `show` only read, and
  `draft` writes files of names it chose, auditing nothing. `run`, `cancel` and `report` write as
  `local:<operator>@<host>`, so they need `DS_OPERATOR` before any work when auth is configured; a
  refusal is one `error:` line with PRECHECKS §10's message, exit 2. `run` stores a spec file as `ds run`
  does but for the approval gate (a pre-check is not gated; under SSO a file whose content was never
  uploaded and needs an approval is still refused) and reads its pairs beside an in-process
  `run_worker(schedules=False)`; it exits 1 under `--fail-on` when a pair ranks at or above it or the
  pre-check was cancelled. `ds run` prints a creation warning as `warning: …`, and a run the schema guard
  created or paused as `paused (run_paused_schema_drift)` with its lines, exit 2.
- **Sign-off sampling** ([SAMPLING.md](SAMPLING.md) §13, §14). `ds run` of a hash sample refuses
  `--keys`, a soak without `--rolling` and `--samples` (`SampleRefused` in `_EXPECTED`, exit 2), and
  prints a refused draw's text before `error: the run is paused`. The CLI closes no draw:
  `sample_draws.closed_by` admits only a person (`user:U-…`), which a CLI actor never is.
- **Labelled accuracy** ([ACCURACY.md](ACCURACY.md) §10). `ds labellings` only reads: one
  `accuracy.summary` line per labelling, newest first, or with `--json` each labelling as the API lists
  it (ids, actors, dates, sizes, truth and hashes, `summary` and `attested`; no sheet, selection or
  score); an unknown `--run` exits 2 (`run R-… not found`). The CLI draws and labels nothing: an operator
  does, over the API or on the Run screen, so a labelling's labeller is a person, a token or developer
  mode, never a `local:` actor.
- **Approvals** (APPROVALS.md §4, §6.3). The CLI can neither request nor grant an approval. Under
  SSO, `ds run` and `ds compare` find their spec file by content hash and gate before writing; in
  tokens mode they warn that the run is labelled unapproved. `ds serve` and `ds auth check` warn
  `no IdP group maps to rule_approver or above: nothing can be approved`.
- **`ds auth check`** applies §2.1 and loads the client credential. It prints the key ids and
  algorithms, the clock offset from the IdP's `Date` header (with a warning above
  `clock_skew_seconds`), and the redirect and post-logout URIs to register.
  - A `Date` is cut to the second, so it is taken as the middle of its second.
  - It also exits 2 when no JWKS key fits `id_token_algs` (`jwks: no key fits auth.oidc.id_token_algs
    …`), and without `auth.oidc` (`ds auth check needs the auth.oidc settings: these settings give
    tokens mode`, or `developer mode`).
- **The MCP server** ([MCP.md](MCP.md) §5.2, §12). `ds mcp` serves the MCP tools over its stdin and
  stdout to the agent that started it, as `local:<operator>@<host>` cut to `--role`: a workstation's
  channel, in developer mode. Where the settings have `auth` it is this host-administrator channel and
  refuses to serve without `--host-channel` (`error: ds mcp under auth is the host-administrator channel:
  use /v1/mcp with a named token, or pass --host-channel (docs/mvp/MCP.md §5.2)`, exit 2); with it, it
  needs `DS_OPERATOR` and prints a warning that its caller is asserted, not signed in. `--role` shapes
  what the agent is offered and is no control: the agent holds the OS account that runs the command.
  `ds mcp check` reads the settings alone and prints the `mcp` block as judged.
- **Names beside ids.** `ds users`, `ds token list` and `ds audit` print every name next to its
  actor or user id. The names come from `ds.actor_name`: `repo.list_tokens` adds `created_by_name` and
  `owner_user_name`, and `repo.list_users` adds `disabled_by_name`, so the admin routes carry them too.

### 4.3 Runner and agent

- **Runner.** `create_run`, `pause_run`, `resume_run`, `stop_run`, `cancel_run`, `compare_one`,
  `runs._move`, `cycle.finish_cycle` and `cycle.fail_run` take a required keyword-only
  `who: Principal`. Role checks stay in the API; the four-eyes gate is the runner's (`approvals.gate`
  in `create_run`, `compare_one`, `resume_run` and a soak's boundary; APPROVALS.md §4), and so is the
  licence, after the gate ([LICENCE.md](LICENCE.md) §8): `licence.admit` in `create_run` and
  `compare_one`, `licence.need` in `resume_run` of a run paused for the licence, `licence.stopped` at a
  soak's boundary. The agent's `compare_key` returns either refusal as a `ToolError`. A hash sample's
  refusals are the runner's too ([SAMPLING.md](SAMPLING.md) §5): `sample.refuse` and `sample.overlapping`
  (under the DSSM locks) in `create_run`, `sample.refuse_resume` (R5) in `resume_run`.
  - The worker passes `WORKER`, and `cycle._rolled` uses it.
  - `runs.cancel_run` passes its caller's `who` to `finish_cycle` and `fail_run`; `runs.compare_one`
    passes it to `fail_run` and audits `run.finished` itself. Its completion transaction takes the
    run's row lock first, in the order a stop takes its locks, and any failure there fails the run.
- **Readiness** ([READINESS.md](READINESS.md) §4, §8, §12). Only the worker writes readiness: as
  `WORKER` it stores each ended cycle's measures (`runner/readiness.watch`, outside `finish_cycle`, so no
  readiness fault fails a run) and audits `readiness.measured`, `readiness.config` and
  `readiness.changed`. No route, CLI command or agent tool writes a measure or a transition; a caller's
  role decides nothing about a pair's state.
- **Schedules** ([SCHEDULES.md](SCHEDULES.md) §5, §6). Only the worker of `ds worker` or `ds serve`
  fires: `runner/schedule.fire` calls `create_run(…, who=WORKER, schedule=Firing(…))`, so a scheduled
  run's `created_by` and the actor of its `create_run`, `licence.pair` and `schedule.fire` rows are
  `system:worker`, and no person is recorded as doing what no session did. The gate, the licence and a
  sample's refusals judge each firing in `create_run` as they judge any run. Before it, the worker
  refuses a schedule saved in another auth mode than its own (`mode`) and one whose setter no longer
  answers for it (`setter`, `repo.setter_live`: a person enabled whose stored roles hold `operator`; a
  token whose `owner_user_id`, if it has one, is not disabled; a `local:` actor or `dev:loopback`
  always). The setter is read again in the transaction that inserts the run, once the schedule's row
  is locked (`SetterGone` rolls the run back), so a person disabled or demoted while a firing read its
  key listing gets no run; one disabled after that read does not stop it. No caller's role is read at
  a firing: the worker starts it. The setter's stored `operator` role is the only role a firing
  depends on. The chat's agent has no schedule tool; the MCP server's `list_schedules` and
  `get_schedule` read them, as `GET /v1/schedules` does, and write none.
- **Table pre-checks and the schema guard** ([PRECHECKS.md](PRECHECKS.md) §5, §7). Only the worker reads
  a pre-check's pairs, as `system:worker`, which audits `precheck.finished`; a pre-check touches no run.
  The schema guard's looks at a scan pass, a cycle's start and a refused read (V2, V3, V5) pause and
  audit as `system:worker`; its look at a run's creation (V1) and at a resume (V4) audit as the caller
  of `create_run` or `resume_run`. A resume that accepts the changes that stand records who accepted
  them (`guard.accepted.by` and `name`) and audits `running {…, schema: {accepted}}`. No caller's role
  is read by the guard: it decides on the tables alone. The chat's agent has no pre-check tool, and its
  `compare_key` reads no pre-check; the MCP server's `list_prechecks` gives each pre-check's pairs
  counted by status and verdict, and no tool reads a pair, a verdict's line or a figure
  ([PRECHECKS.md](PRECHECKS.md) §6).
- **Run transitions.** `finish_cycle` audits `run.finished {status, reason}` when it sets a final
  status, and `run.cycle {cycle}` when a soak starts its next cycle. `fail_run` audits
  `run.finished {status: "failed", reason: <failure()>}` when it moves the run, and
  `runs.compare_one` audits `run.finished {status, reason: null}` as its caller when it completes
  the one-shot run (API, CLI or the agent's `compare_key`, with `via`). So an evidence pack has the
  full run trail.
- **Audit last.** The audit insert is the last statement of its transaction (§6). So `runs._move`
  and `fail_run` audit after `repo.cancel_keys`, and `runs.compare_one` reads `get_run` before its
  insert.
- **Creators.** `repo.save_spec` (the first uploader), `create_run` and `create_chat_session` take
  `created_by`. `get_run` and `list_runs` add `created_by_name = ds.actor_name(created_by)`.
- **The agent acts for the asker.** `answer(ctx, session_id, question, who)` builds
  `Scope(run_id, who, compares)`. Its tools follow the asker's role:
  - `get_run_summary`, `get_findings` and `get_observations` need viewer;
  - `compare_key` needs operator.
- **Enforcement.** `_Turn._request` offers `tools_for(scope.who)`, not `TOOLS`, so a viewer's model
  never sees `compare_key`. `run_tool` refuses the rest with
  `ToolError("compare_key needs the operator role")`.
- **Attribution.** `answer` puts `replace(who, via=f"chat:{session_id}")` in the `Scope`, and
  `compare_key` passes `who=scope.who`.
- **Unchanged:** the scope rules, the 10-per-minute limit, and no identity in prompts. From 013
  `get_run_summary` also gives a labelled cycle's stored accuracy ([ACCURACY.md](ACCURACY.md) §10): its
  one line is built without names (`accuracy.summary(…, named=False)`) and says what kind of actor
  labelled (`by a person`, `through a token`, `in developer mode`), never who, and the stored score holds
  no actor, so no staff name, user id or token id reaches the model.
- **MCP acts for its caller** ([MCP.md](MCP.md) §6). An MCP tool call runs as the principal that made
  it, `mcp.tools.caller(who, via)`: its roles cut to `MCP_ROLES` (`viewer` and `operator`) and `via` set
  to `mcp:http` or `mcp:stdio`. So `spec_author`, `rule_approver`, `pii_reader` and `admin` never act
  inside MCP, for `dev:loopback` and a `local:` caller too, and no tool does what this contract reserves
  to a person (approvals, sign-off, packs, closing draws, the reveal, tokens, users, the audit log).
  - The ten reads need viewer; `compare_key` needs operator and the settings' `mcp.work`.
  - `tools/list` offers a caller only what its role and the settings serve (`mcp.tools.offered`), as
    `tools_for` does for the chat; `tools/call` refuses the rest (`<tool> needs the <role> role`,
    audited as `authz.denied`, §6).
  - The four tools the chat has too call `run_tool(ctx, Scope(run_id, who), …)`, so the runner's gate
    and licence judge an MCP caller's `compare_key` as they judge the chat's.
  - MCP creates no session and grants no role, and over HTTP it takes no cookie session (§4.1).

## 5. API tokens (`deltasleuth/auth/tokens.py`)

- **Format.** `dst_<16 hex id>_<64 hex secret>`, with id `T-<16 hex>`. The row stores
  `sha256(token)`, found by id and compared in constant time. The token appears once, in a
  `no-store` 201.
- **Fields:**
  - `name` matches `^[a-z0-9][a-z0-9._-]{2,63}$`, becomes `actor_name`, and may repeat;
  - `owner` is a required label for the team or contact, 1 to 200 printable characters;
  - `owner_user_id` is the accountable person. It must be an enabled user (else 422
    `owner_user_id U-… is not an enabled user`). It is required over the API when `auth.oidc` is
    set (else 422 `owner_user_id is required`); the CLI takes `--owner-user`;
  - `role` is one of `TOKEN_ROLES`;
  - `days` runs from 1 to `token_max_days`, default `min(90, token_max_days)`;
  - `last_used_at` is touched at most once a minute.
- **Endpoints:**
  - `POST /v1/auth/tokens {name, owner, owner_user_id?, role, days?}` gives 201
    `{token_id, name, owner, owner_user_id, role, expires_at, token}`, or 422 for a role outside
    `TOKEN_ROLES`, bad `days` or a bad owner;
  - `GET` lists newest first, with `state`, `created_by` and `owner_user_id`, but never the hash;
  - `DELETE /v1/auth/tokens/{token_id}` gives 200 `{token_id, revoked_at}`, 404, or 409
    `token T-… is already revoked`.
- **Fresh sign-in.** For a person, `POST /v1/auth/tokens` needs the session's `auth_time` within the
  last 15 minutes, else 403 `reauth_required`; so does an approval step (APPROVALS.md §5).
  - The Admin screen then sends the admin through `/v1/auth/login?reauth=1` (§2), which makes the
    IdP authenticate again.
  - A hijacked 8-hour session therefore cannot mint long-lived credentials.
  - Developer mode has no session and is exempt.
- **Accountability** for the four-eyes rule (who uploaded a spec, through which person's token) is
  computed by `ds.accountable` and `ds.spec_accountable` from `ds.specs.created_by`, the `spec.create`
  rows, `ds.api_tokens.created_by` and `owner_user_id`, all written from 005 on (§11,
  APPROVALS.md §3).
- **Rotation:** create a token under the same name, switch CI, revoke the old one.
- **Schedules a token saved** ([SCHEDULES.md](SCHEDULES.md) §6). A token's expiry or revoke does not
  stop the schedules it saved: the firing worker reads neither, so rotation and the 90-day default
  stop nothing. They stop when the token's `owner_user_id` is disabled (`user.disable`), until another
  operator saves them; a token without one, and every schedule in tokens mode, stops only by a pause
  or a delete. `ds schedules --set-by token:T-…` lists what a token saved.
- **An agent's token** ([MCP.md](MCP.md) §6, §13). An agent calls `POST /v1/mcp` with a bearer token as
  any machine does: one token per agent, named for it, `viewer` unless it must compare. Inside MCP a
  `spec_author` token acts as an operator (§4.3). The token's role, expiry and revocation apply at its
  next request, since the server keeps no session; under oidc it has an `owner_user_id`, as every token
  created over the API. It is no token of MCP's own: `ds.api_tokens` holds a role and no scope, and
  `deps.principal` takes the token on every route its role reaches (§4.1). So the `mcp` block's data
  classes and work tools, MCP's limits and its cut to `operator` hold for calls made through `/v1/mcp`
  only, and an agent that can read its token acts on `/v1` with the token's full role
  ([MCP.md](MCP.md) N4): the role given to it is the bound.
- **Failed checks** get 401 and a server-log line with the token id. They are not audited: an
  attacker controls their volume.
- **Tokens mode.** `POST /v1/auth/token-login {"token"}` creates a cookie session for `token:T-…`
  under §3's rules and returns the `/me` body. In oidc and developer mode it gives 404, so a machine
  token cannot bypass SSO and MFA in a browser.

## 6. Audit

| Actor (CHECK on new rows) | Who | `actor_name` |
|---|---|---|
| `user:U-<16 hex>` | a person signed in by OIDC | the username then |
| `token:T-<16 hex>` | an API token (bearer or token sign-in) | the token name |
| `local:<operator>@<host>` | the CLI, asserted (§4.2) | `DS_OPERATOR` |
| `system:worker`, `system:server`, `system:migration` | DeltaSleuth | `worker`, `server`, `migration` |
| `dev:loopback` | developer mode | `<account>@<host>` |
| `api`, `ds` | rows before 005 (not re-checked) | `NULL` |

`client:<id>` is reserved for IdP client-credentials principals; a later migration widens the CHECK.

**Writing.**
- **`repo.insert_audit(conn, who, action, obj, detail=None) -> Row`** writes the principal's
  `actor`, `name`, `request_id`, `session_id` (NULL for bearer, CLI and system principals) and
  `client_addr`. It merges `{"via": …}`, and for `local:` actors `{"asserted": true, "os_account":
  …}`. It returns the inserted row.
- **`repo.audited(pool)`** is the one way callers write audit rows:

  ```python
  async with repo.audited(ctx.pool) as (conn, audit):
      run_id = await repo.create_run(conn, ...)
      await audit(who, "create_run", run_id, detail)   # the last statement before commit
  # committed: the SIEM lines are logged here
  ```

  It opens a pool connection and collects the rows `audit` inserts. It logs their SIEM lines only
  after the connection's transaction has committed, and logs nothing if the block raises.
- **Rule: the audit insert is the last statement before commit, and no I/O follows it,** so the row
  commits with the action it records and a failure after it cannot leave a row for an action that
  did not happen. Several audit rows in one transaction come together at the end.
  - Reads that do I/O come first. `finish_cycle` audits after `summarise`, whose re-derivation reads
    evidence.
  - The exception is `target.set` and `target.delete` (`deltasleuth/targets.py`): the row is committed
    first, with the count of unfinished runs that refuses the change, and the credential files and the
    list are written after it. A write that fails before the list is replaced answers 500
    `the target could not be written: <the OS error>` and the row stays, for a change that was asked for
    and did not hold: the list and its credentials are as they were. The replace of the list is the
    change: a failure after it (the sync of the list's folder) undoes nothing, since the list on disk
    names the new credentials. The change is answered as made, and the log says
    `warning: the product's list of targets holds the change, but its write did not end cleanly (<the OS
    error>)`.
  - `key.view` and `report.download` are audited in their own transaction, after the content is
    built and before it is sent; so are an MCP tool's `key.view` and `mcp.read`, and the call fails
    if its row cannot be written ([MCP.md](MCP.md) §9). `chat.ask` is audited after the agent has
    stored the question (in its own transaction, even when the model then fails); a model client
    that cannot open stores nothing and writes no row. A fixed question's row names the question
    (`{question: "explain", root_cause_id}` or `{question: "summarise"}`), a typed question's nothing: no
    row holds a question's text. The CLI's report commands audit each report once it is written (§4.2).
- **SIEM copy.** Logger `deltasleuth.audit` gets one JSON line per row:
  `{id, at, actor, actor_name, action, object, detail, request_id, session_id, client_addr, chain}`,
  `chain` being the row's `{seq, hash}` in the hash chain (from 009, [PACK.md](PACK.md) §4.1).
  - `ds serve`, `ds worker` and every CLI command that writes audit rows attach a handler at INFO,
    `%(message)s`, `propagate=False`, so the host's log agent ships an independent copy.
  - The services write it to stdout. Other commands write it to stderr, so their stdout stays their
    output: `ds token create` prints only the token, and `--json` stays JSON. Every command that opens
    the store attaches it, since opening applies migrations (`system.migrate`). Under `ds mcp` stderr is
    the launching agent's, so that copy reaches the agent; a host log agent sees it only where the
    command runs in a container whose streams Docker's logging driver keeps
    ([MCP.md](MCP.md) §5.2, DEPLOY section 9).
- **Rate-limited denials.** An authenticated caller controls how many denials it causes. So
  `auth/principal.py`'s `Throttle`, which `require` and the callback share, keeps an in-process
  window per key:
  - `authz.denied`: at most one row per `(actor, route template)` per minute, an MCP tool's role
    refusal under the key `(actor, MCP <tool>)` in the same table (`ds mcp` keeps its own);
  - `auth.login_denied`: at most one row per `(user_id, reason)` per 10 minutes.

  The MCP server keeps a third such window, one hour per `(actor, tool, run_id)`, for its `mcp.read`
  rows (per process; `Throttle.forget` drops a key whose row could not be written, so the next read
  writes it). A read that arrives while its key's row is being written waits for that commit, so none
  is answered under a row that was not written ([MCP.md](MCP.md) §9).

  A row's `count` is the number of denials for its key since the previous row, this one included.
  Suppressed denials go to WARNING lines on `deltasleuth.auth`, an MCP tool's too; under `ds mcp`,
  whose stderr is the launching agent's, such a line is its logger's name and level alone
  ([MCP.md](MCP.md) §9). The table holds at most 10,000 keys, and the oldest go first.

**Actions.** The existing ones keep their objects and details: `spec.create`, `create_run`,
`compare_one` (plus `via`), `paused`, `running`, `cancelling`, `rolling_resolve_failed`, `chat.create`
and `chat.ask`. From 008 the `create_run` and `compare_one` details add `approval_id` and `approval`
(`{needs, mode}`), from 009 `create_run`'s adds `plan_sha256` (the SHA-256 of the stored `params`
without `approval`, which a pack's `facts/plan.json` holds), from 010 both add `licence` (the run's
licence record, [LICENCE.md](LICENCE.md) §10; null in an open build, §19), from 015 a scheduled run's `create_run` adds `schedule`
(`{schedule_id, due, revision, time_zone}`, as its `params`; [SCHEDULES.md](SCHEDULES.md) §11), from 016 the
schema guard's pause is `paused {reason: "run_paused_schema_drift", schema: {a, b}}` (each side's shape hash) and a
resume that accepts its changes writes `running {reason, approval_id, schema: {accepted: {a, b}}}`
([PRECHECKS.md](PRECHECKS.md) §9), and the `paused`, `running` and `cancelling` rows a
resume, a revoke or a soak's boundary writes carry `{reason, approval_id}` (`reason`:
`run_paused_approval`, `approval_revoked` or null; APPROVALS.md §6.4), or at a boundary the licence
refuses `{reason: "run_paused_licence", approval_id, licence}`, `licence` being its refusal as data (the
run's licence record and `refusal`: `expired`, `sso` or `unseated` with `count`, `limit` and `since`; never
the M3, M4 or M5 message, [LICENCE.md](LICENCE.md) §11). From 011 a sampled soak's boundary after a
`bottom_k` draw that did not pass writes `paused {reason: "run_paused_sample", approval_id, draw_id}`, a
refused draw `paused {reason: "run_paused_draw"}` by `system:worker`, and a sampled sign-off's final
`approval.grant` adds `later_draws` ([SAMPLING.md](SAMPLING.md) §14). From 013 a labelling's draw and
its labels are audited ([ACCURACY.md](ACCURACY.md) §12). A run whose keys are read from a key table
adds `key_source` to its `create_run` detail, and to `run.cycle`'s after each roll of a rolling soak:
the read's record `{basis: "key_table", target, relation, rows, keys, at}` ([CONTRACTS.md](CONTRACTS.md)
§11), a target id, a relation's label, counts and a time, never a key. The new ones:

| Action | Object | Detail |
|---|---|---|
| `auth.login`, `auth.logout` | session_id | `{method, roles, reauth}`, — |
| `auth.login_denied` | user_id | `{reason: disabled\|no_role\|groups_overage, count}` |
| `auth.roles_changed` | user_id | `{from, to}` |
| `session.revoke` | session_id, user_id or `all` | `{count}` |
| `user.disable`, `user.enable` | user_id | `{sessions_revoked, tokens_revoked: [T-…]}`, — |
| `token.create`, `token.revoke` | token_id | `{name, owner, owner_user_id, role, expires_at}`, — |
| `authz.denied` | `METHOD /v1/<route template>`; `MCP <tool>` for an MCP tool call refused for its role | `{needed, count}` |
| `mcp.read` | run_id | `{tool, calls}`: an MCP caller read a run through `get_run_summary`, `get_root_causes` or `get_findings`; at most one row per caller, tool and run an hour, `calls` counting the calls since that key's previous row, this one included (a count is lost at a restart, and with a row that could not be written); by the caller, with `via` ([MCP.md](MCP.md) §9) |
| `key.view`, `report.download` | run_id (a readiness report: `readiness`) | `{key_masked, cycle}`, `{name, cycle}` (a readiness report: `{name, programme, config_sha256}`, and `pair` for a pair's brief reports) |
| `run.finished` | run_id | `{status, reason}` |
| `run.cycle` | run_id | `{cycle}`: the soak cycle that starts; with `key_source` after a key table's roll (above) |
| `audit.read` | `audit` | `{filters}` |
| `system.start` | `server` | `{version, auth, roles_sha256, retention, keks, current_kek, missing_keks, pack_key, licence, mcp, audit_head}`: `roles_sha256` is §3's value, or null outside oidc; `keks` to `missing_keks` RETENTION §6.1's (the retention days, the keyring's KEK ids, the current one and those in use it lacks); `pack_key` the evidence packs' signing key fingerprint (null without `packs`) and `audit_head` the hash chain's head `{seq, hash}` as the server starts ([PACK.md](PACK.md) §3.1, §4.1); `licence` the standing at start, `{installation_id, state, licence_id, tier, pairs, ends}` (`pairs` the limit; in an open build `state: "open"` and the other four null), or null when it could not be read ([LICENCE.md](LICENCE.md) §11, §19); `mcp` the settings' `mcp` block as loaded, `{data_classes, http, work, calls_per_minute, concurrency}`, or null without one ([MCP.md](MCP.md) §4) |
| `system.migrate` | `schema` | `{versions}`: the migrations this transaction applied |
| `retention.expire`, `retention.purged` | run_id | per purged cycle, RETENTION §8 |
| `retention.abandoned`, `retention.hold`, `retention.release` | run_id | RETENTION §8 |
| `retention.finished` | `purge` | a pass's or an erasure's counts, RETENTION §8 |
| `keys.created`, `keys.rewrap` | `address`, `E-…`; `keys` | `{kek}`; `{from, to, rows}`, by `system:migration` |
| `keys.migrate` | `keys` | the migration's counts and `complete`, RETENTION §8 |
| `approval.request` | approval_id | `{kind, spec_id}` and a spec version's `{content_hash, scopes}` or a sign-off's `{run_id, cycle, attests_sha256}` (APPROVALS.md §6.4) |
| `approval.grant` | approval_id | the request's fields plus `{step, of, authors, auth_time}` |
| `approval.reject` | approval_id | `{kind, cascade_of}` |
| `approval.revoke` | approval_id | `{reason, paused, stopped, cascade_of}`, by the revoker, or the purger of a signed cycle |
| `spec.rehash` | `specs` | `{spec_ids}`: stored bodies that do not re-hash, by `system:migration` |
| `run.census` | run_id | `{cycle, side, basis, rows, missing, extra, snapshots}`, or a failed count's `{cycle, side, basis, failed}`, by `system:worker` ([PACK.md](PACK.md) §1.3) |
| `adjudication.draw`, `adjudication.score` | adjudication_id | `{run_id, cycle, salt}`; `{upload_sha256, sheet_sha256, score_sha256, passed}` |
| `pack.issue` | pack_id | `{signoff_id, sums_sha256, manifest_sha256, zip_sha256, key, audit_head, licence_id}` (`licence_id` null in an open build) |
| `pack.download` | pack_id | `{run_id, cycle}` |
| `pack.revoke` | pack_id | `{reason, cascade_of}`: the sign-off's revoke reason and the sign-off, by its revoker |
| `installation.created` | `I-…` | `{installation_id}`, by `system:migration` when 010 applies ([LICENCE.md](LICENCE.md) §5) |
| `licence.install`, `licence.withdraw` | `LI-…` | `{sha256, customer, order, installation_id, tier, pairs, starts, ends, issued, key, applies}`; `{reason}`, by the CLI operator ([LICENCE.md](LICENCE.md) §11) |
| `licence.pair` | `LI-…` (`I-…` in evaluation) | `{since, name, run_id, count, limit, targets}`: a new pair counted, by the run's creator; never written by an open build, which counts none |
| `licence.state` | `I-…` | `{from, to, licence_id, ends}`: the licence's state or licence changed, by `system:worker`; an open build writes it once, `to: "open"` with `licence_id` and `ends` null |
| `sample.draw` | run_id | `{draw_id, cycle, method, population, salt_label, salt_sha256, keys, selection_sha256, frame, strata: [{label, frame_keys, drawn}]}`, by `system:worker`, last in the transaction that stores the draw and its keys; never the nonce ([SAMPLING.md](SAMPLING.md) §5, §14) |
| `sample.draw_failed` | run_id | `{cycle, error}`: the frame's refusal (W1–W9), after the `paused` row, by `system:worker` |
| `sample.close` | draw_id | `{spec_name, note}`: a person closing a draw that did not pass, one row per draw in draw-id order |
| `accuracy.draw` | labelling_id (`V-…`) | `{run_id, cycle, salt, truth: {source, as_of, names_side}, size, per_cause, accepted, agreed, unlocatable, item_count, counts, selection_sha256}`: `counts` the items drawn per population; by the operator who drew, last in the transaction that stores the labelling ([ACCURACY.md](ACCURACY.md) §12) |
| `accuracy.label` | labelling_id | `{run_id, cycle, sheet_sha256, labels_sha256, score_sha256, labelled, undecidable, not_reviewed}`: hashes of what is kept and counts (no hash of the upload, which would hash its notes), by the operator who uploaded the labels, the labeller of record |
| `notify.config` | `notify` | `{label, channels: {name: {kind, host, proxy, allow_private_network, labels, links, recipients}}, rules, routing}`, or `{routing: null}` once `notify` is removed; `routing` a keyed token of the whole routing (addresses included), by `system:worker` when it differs from the newest row's ([ALERTS.md](ALERTS.md) §12) |
| `notify.dead` | `notify:<channel>` | `{delivery_id, event, attempts, error, status, body_sha256, dead_since_last}`: a delivery died, at most one row per channel an hour, by `system:worker` |
| `notify.skipped` | `notify` | `{reason: stale, from_seq, to_seq}` or `{reason: unsafe\|error, rows, seqs}`: audit rows the feed did not turn into notifications, by `system:worker` |
| `notify.test`, `notify.retry` | `notify:<channel>` | `{delivery_id, state, status, error}`; `{since, count}`, by the caller |
| `notify.mute`, `notify.unmute` | `M-…` | `{scope, target, until, reason}`; `{}`, by the caller |
| `readiness.measured` | `readiness` | `{cycles: [[run_id, cycle, sha256], …]}`: the cycles a batch measured (at most 100 a row), each with the hash of its stored measures, last in the batch's transaction, by `system:worker` ([READINESS.md](READINESS.md) §4, §12) |
| `readiness.config` | `I-…` | `{from_sha256, sha256, gates, programmes, deviations}`: the readiness settings as judged, when they differ from the newest row's, by `system:worker` at its first evaluation, before the `readiness.changed` rows that evaluation writes |
| `readiness.changed` | `readiness:<pair>` | `{pair, programme, entity, from, to, was_regressed, regressed, score, causes, gates, changed, evidence, candidate, signoff_id, pack_id, config_sha256}`: a pair's first recorded state, or a change of its state, `regressed` or a required gate's status, by `system:worker` ([READINESS.md](READINESS.md) §8); ids, states, counts and slugs only |
| `target.set` | the target id | `{created, base_url, allow_private_network, allow_insecure_http, auth: {kind, header, more: [header]}, secret: replaced\|kept\|none, rate_per_s, max_concurrency, absent_on_404}`: header names, never a credential, its reference or its file; by the caller (DEPLOY section 7) |
| `target.delete` | the target id | `{}`, by the caller |
| `target.try` | the target id | `{spec, version, content_hash, side, method, request_sha256, key_masked, raw}`: what was asked, not what came of it; `request_sha256` is over the side's `target` and `request` as written (a template: no key), `raw` whether the answer was shown as it came (a trial); `key_masked` is the key as reports show it, a stand-in unless the spec declares the key a business code (`key.class: business_code`, the default), which is recorded in clear; never a URL or a value of the answer; by the caller |
| `schedule.create`, `schedule.update` | `SC-…` | `{name, spec_name, spec_id, run, cron, time_zone, start_within, ends_at, auth_mode, revision, next_due}`; an update adds `changed: [field]`; by the caller ([SCHEDULES.md](SCHEDULES.md) §11) |
| `schedule.pause`, `schedule.resume`, `schedule.delete` | `SC-…` | `{revision, next_due}`, by the caller; a pause by `system:worker` (a cron or zone its build cannot load) adds `reason: zone`, `code` (`zone` or `cron`), `tzdata`, `missed` (whether a `schedule.missed` row of reason `zone` is chained before it; false: no time was due, and the notifier sends the pause's row) and `spec_name` and `mode`, which rules match |
| `schedule.fire` | `SC-…` | `{due, run_id, spec_id, spec_name, mode, revision, time_zone, late_s, next}`, by `system:worker`, last in the transaction that created the run |
| `schedule.missed` | `SC-…` | `{reason, code, due, to, count, spec_name, mode, spec_id, run_id, draw_id, error_type, revision, time_zone, start_within, auth_mode, worker_mode, tzdata, next}`: a due time, or a counted range of them, that started no run, with a closed reason and code; by `system:worker`, or by the caller whose save, pause or delete left it unstarted (`superseded`) or chained a held range. Never a refusal's message |
| `schedule.rezone` | `SC-…` | `{from, to, tzdata}`: the next due time a firing worker recomputed from its tz data, by `system:worker` |
| `precheck.create` | `PC-…` | `{source, level, pairs, skipped, spec_ids}` (at most 50 ids, else their count), or a catalogue's `{source: catalogue, level, pairs, skipped: 0, a: {target, schema}, b}`; by the caller ([PRECHECKS.md](PRECHECKS.md) §9) |
| `precheck.finished` | `PC-…` | `{pairs, verdicts: {<verdict>: n}, failed: {<code>: n}, seconds}`, by `system:worker`, in the transaction that settled its last pair |
| `precheck.cancel` | `PC-…` | `{pending, running}`: the pairs it cancelled, by the caller |
| `schema.drift` | `R-…` | `{cycle, where, mode, checked, paused, paused_seq, basis, sides: {a, b: {breaking, other, kinds: {<kind>: n}, from, to}}, error_type}`: counts, closed kinds, the seq of its trip's `paused` row (null: it paused nothing), the basis run's id and 64-hex hashes, never a relation's or a column's name or a declared type; by `system:worker`, or by the caller at a creation (V1) and a resume (V4); last in its transaction ([PRECHECKS.md](PRECHECKS.md) §7, §9) |

- **`system.migrate`.** `store/db.migrate` runs in `repo.audited` and writes it as the last
  statement of the transaction that applies migrations, once the schema is at 005 or later. The
  upgrade to 005 records `{versions: [5]}`, and an empty database `[1, …, 5]`. So every later
  migration, including one that alters the audit triggers, leaves a row.

**Not audited:**
- anything before an identity is known (401s, failed sign-ins, failed tokens), which goes to the
  server log instead;
- list and chat reads, every readiness read but a report download, every schedule read and preview, and
  every table pre-check read but a report download (a draft too);
- over MCP ([MCP.md](MCP.md) §9): `tools/list`, `list_runs` and the readiness, schedule and pre-check
  tools, and every refused call but a role refusal; each call leaves one line on the server log's
  `deltasleuth.mcp`;
- a labelling's sheet download, and a draw repeated within ten minutes (it returns the first labelling);
- health, validation and profiling.

**`detail` never holds** raw keys, payload values, chat text, tokens, cookies, IdP tokens, secrets,
`error_description`, or claims beyond the username; nor a notification channel's URL, path or address
([ALERTS.md](ALERTS.md) §12); nor a labelling's drawn keys, masked or raw, a labeller's notes or a
hash of the uploaded sheet, which would hash them ([ACCURACY.md](ACCURACY.md) §12).

**Integrity.**
1. **Append-only.** The existing statement trigger stays: UPDATE, DELETE and TRUNCATE are refused.
2. **An independent copy.** Every row also leaves the database as a SIEM line (above), so rows
   altered later by someone with database-owner rights can be detected against the SIEM.
3. **A hash chain** (009, [PACK.md](PACK.md) §4). Every row, whoever inserts it, gets a
   `ds.audit_chain` entry: its stored text, the previous row's hash and its object's previous hash.
   `ds audit verify [--siem FILE]` checks every link and hash, and the rows against a SIEM export;
   a pack carries its objects' rows. The anchors off the host are the SIEM lines (`chain`), the heads
   `system.start`, `pack.issue` and `ds audit verify` record, and issued packs.
4. **Limits.** The app connects as the database owner (DEPLOY §10), who can disable the trigger and
   rewrite the log and the chain consistently: only the SIEM copy, ingested at write time, shows that.
   DEPLOY recommends an INSERT/SELECT-only app role once a migration DSN exists.

**Reading.** `GET /v1/audit?actor=&action=&object=&session_id=&since=&until=&limit=&cursor=`:
- exact filters, newest first, keyset on `id` (`repo._page`);
- `limit` from 1 to 500, default 100;
- returns `{"items", "next_cursor"}`. Each item carries both `actor` and `actor_name`, and the UI
  shows them side by side, and `chain`, its `{seq, hash}` in the hash chain.

## 7. Migration `005_identity.sql`

**Checked on PostgreSQL 16** over 001–004, both with legacy rows and on an empty database:
- the CHECKs, including a C0 or C1 control character in a name and a non-ASCII subject over 255
  octets;

```sql
-- People, sessions, API tokens and an attributable audit log (docs/mvp/AUTH.md).
-- Roles come from the IdP at each sign-in; no table grants them. '[\x01-\x1f\x7f-\x9f]' is every
-- C0 and C1 control character, spelled out so the check does not depend on the database locale.
CREATE TABLE ds.users (
    user_id       text PRIMARY KEY CHECK (user_id ~ '^U-[0-9a-f]{16}$'),
    issuer        text NOT NULL,
    subject       text NOT NULL CHECK (subject <> '' AND octet_length(subject) <= 255),
    username      text NOT NULL CHECK (username <> '' AND length(username) <= 256
                                       AND username !~ '[\x01-\x1f\x7f-\x9f]'),
    display_name  text CHECK (length(display_name) <= 256 AND display_name !~ '[\x01-\x1f\x7f-\x9f]'),
    roles         text[] NOT NULL CHECK (roles <@ '{viewer,operator,spec_author,rule_approver,pii_reader,admin}'),
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz,
    disabled_at   timestamptz,
    disabled_by   text,
    UNIQUE (issuer, subject),
    CHECK ((disabled_at IS NULL) = (disabled_by IS NULL))
);

CREATE TABLE ds.api_tokens (
    token_id      text PRIMARY KEY CHECK (token_id ~ '^T-[0-9a-f]{16}$'),
    name          text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9._-]{2,63}$'),
    owner         text NOT NULL CHECK (owner <> '' AND length(owner) <= 200 AND owner !~ '[\x01-\x1f\x7f-\x9f]'),
    owner_user_id text REFERENCES ds.users,       -- the accountable person; required over the API in oidc (§5)
    role          text NOT NULL CHECK (role IN ('viewer', 'operator', 'spec_author')),  -- TOKEN_ROLES
    secret_hash   bytea NOT NULL CHECK (octet_length(secret_hash) = 32),              -- sha256(token)
    created_by    text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    last_used_at  timestamptz,
    revoked_at    timestamptz,
    revoked_by    text,
    CHECK (expires_at > created_at),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
);
CREATE INDEX api_tokens_owner_idx ON ds.api_tokens (owner_user_id) WHERE revoked_at IS NULL;

CREATE TABLE ds.sessions (
    session_id   text PRIMARY KEY CHECK (session_id ~ '^W-[0-9a-f]{16}$'),
    secret_hash  bytea NOT NULL UNIQUE CHECK (octet_length(secret_hash) = 32),
    user_id      text REFERENCES ds.users,
    token_id     text REFERENCES ds.api_tokens,
    actor        text NOT NULL,
    name         text NOT NULL,
    roles        text[] NOT NULL CHECK (roles <@ '{viewer,operator,spec_author,rule_approver,pii_reader,admin}'),
    roles_sha256 text CHECK (roles_sha256 ~ '^[0-9a-f]{64}$'),   -- the role mapping it was granted under
    client_addr  inet,
    auth_time    timestamptz,          -- ID token claims: token creation (§5), step-up
    acr          text,
    amr          text[],
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz,
    revoked_by   text,
    CHECK ((user_id IS NULL) <> (token_id IS NULL)),
    CHECK ((user_id IS NULL) = (roles_sha256 IS NULL)),
    CHECK (actor = coalesce('user:' || user_id, 'token:' || token_id)),
    CHECK (expires_at > created_at),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
);
CREATE INDEX sessions_user_idx ON ds.sessions (user_id) WHERE revoked_at IS NULL;
CREATE INDEX sessions_token_idx ON ds.sessions (token_id) WHERE revoked_at IS NULL;
CREATE INDEX sessions_expiry_idx ON ds.sessions (expires_at);

ALTER TABLE ds.specs ADD COLUMN created_by text;          -- NULL before 005
ALTER TABLE ds.runs ADD COLUMN created_by text;
ALTER TABLE ds.chat_sessions ADD COLUMN created_by text;

CREATE FUNCTION ds.actor_name(actor text) RETURNS text LANGUAGE sql STABLE AS $$
    SELECT coalesce(CASE split_part(actor, ':', 1)
        WHEN 'user' THEN (SELECT username FROM ds.users WHERE user_id = split_part(actor, ':', 2))
        WHEN 'token' THEN (SELECT name FROM ds.api_tokens WHERE token_id = split_part(actor, ':', 2))
    END, actor)
$$;

-- ADD COLUMN updates no row, so the append-only trigger does not fire; old rows are not re-checked.
ALTER TABLE ds.audit_log
    ADD COLUMN actor_name  text CHECK (actor_name !~ '[\x01-\x1f\x7f-\x9f]'),
    ADD COLUMN request_id  text CHECK (request_id ~ '^[QC]-[0-9a-f]{16}$'),
    ADD COLUMN session_id  text CHECK (session_id ~ '^W-[0-9a-f]{16}$'),
    ADD COLUMN client_addr inet;
ALTER TABLE ds.audit_log ADD CONSTRAINT audit_actor CHECK (actor ~ ('^(user:U-[0-9a-f]{16}'
    '|token:T-[0-9a-f]{16}|local:[A-Za-z0-9._-]{1,64}@[A-Za-z0-9._-]{1,253}'
    '|system:(worker|server|migration)|dev:loopback)$')) NOT VALID;
CREATE INDEX audit_log_actor_idx ON ds.audit_log (actor, id);
CREATE INDEX audit_log_action_idx ON ds.audit_log (action, id);
CREATE INDEX audit_log_object_idx ON ds.audit_log (object, id);
CREATE INDEX audit_log_session_idx ON ds.audit_log (session_id, id) WHERE session_id IS NOT NULL;
```

- **Restores.** A full `pg_restore` creates the trigger after the data. A data-only restore needs
  `--disable-triggers`.
- **New repo functions:**
  - `upsert_user` (a disabled user's sign-in never sets `last_login_at`, so a refused user's sign-ins
    write no row version), `get_user`, `list_users`, `set_user_disabled`, `user_tokens`;
  - `create_session`, `session_by_secret`, `get_session`, `touch_session`, `revoke_sessions`,
    `list_sessions`, `delete_ended_sessions`;
  - `create_token`, `token_by_id`, `touch_token`, `list_tokens`, `revoke_token`, `revoke_user_tokens`;
  - `audited`, `list_audit`.

## 8. Settings

```yaml
auth:                                   # absent: developer mode (loopback only, never proxied)
  public_url: https://deltasleuth.bank.example   # https://host[:port]; http only on loopback
  allowed_hosts: []                     # extra Host values for API clients; needs an https public_url
  trusted_proxies: [172.30.0.1/32]      # IPs/CIDRs whose X-Forwarded-For is resolved (default none); §3
  session_idle_minutes: 30              # 5..480
  session_max_hours: 8                  # 1..24
  token_max_days: 90                    # 1..366
  oidc:                                 # absent: tokens mode (CI and trials; a vendor build's Community and Pro, §1)
    issuer: https://login.microsoftonline.com/<tenant-id>/v2.0
    client_id: 11111111-1111-1111-1111-111111111111
    client_auth: private_key_jwt        # client_secret_basic (default) | client_secret_post | private_key_jwt
    # client_secret_ref: file:/run/secrets/oidc_client_secret
    client_key_ref: file:/run/secrets/oidc_client_key
    client_key_id: deltasleuth-2026     # optional kid
    client_cert_file: oidc_client.crt   # optional: x5t, x5t#S256
    scopes: [openid, profile]           # ADFS: add allatclaims; email is never used (§2.4)
    subject_claim: oid                  # default sub
    username_claim: preferred_username  # admin-controlled only: Entra preferred_username or upn; ADFS upn
    group_claims: [roles]               # default [groups]; a path is a string or a list:
                                        #   Keycloak [[resource_access, deltasleuth.bank, roles]]
    id_token_algs: [RS256, PS256, ES256]
    clock_skew_seconds: 60              # 0..300
    end_session: true                   # Okta: false
    # proxy: http://proxy.bank.example:8080
    roles:                              # immutable ids only, matched exactly (here: Entra app role values)
      {viewer: [DS.Viewer], operator: [DS.Operator], spec_author: [DS.SpecAuthor],
       rule_approver: [DS.RuleApprover], pii_reader: [DS.PiiReader], admin: [DS.Admin]}
```

**How the settings are built:**
- `config.py` gains frozen, closed `AuthSettings` and `OidcSettings`. Credentials are `SecretRef`s
  only, cross-field rules live in `__post_init__` (except the `allowed_hosts` rule, a field validator
  so that its error names the field), and relative paths resolve against the settings file.
- `username_claim` has no default: without it the username is the subject.
- **Group values** in `auth.oidc.roles` must be immutable ids that only IdP administrators assign:
  Entra app role values or group object ids, Keycloak client roles, ADFS group SIDs, Okta or Ping
  group ids through a custom claim. Never display names: where names are not unique (Entra
  `cloud_displayName`, Okta), anyone who can create a group named `DS.Admin` would become admin.
  DEPLOY §5's per-IdP table maps ids only.
- Entra registers both `{public_url}/v1/auth/callback` and `{public_url}/`. DEPLOY §5 gets the full
  per-IdP table.

**Errors.** Each is one `error:` line, with exit 2. Pydantic prefixes cross-field messages with
`Value error, `.
- **At load:**
  - `api_token_ref: replaced by named API tokens; remove it and create tokens with 'ds token create' (docs/mvp/AUTH.md §5)`,
    checked before validation;
  - `auth.public_url: must be https://host[:port] (http only for localhost, 127.0.0.1 or [::1]), without a path, query or fragment`;
  - `auth.allowed_hosts: needs an https public_url`, so bearer tokens and cookies never cross a
    network in plain text;
  - `auth.oidc.issuer: must be https (http only for a loopback host)`;
  - `auth.oidc: client_auth <method> needs client_secret_ref` (or `client_key_ref`);
  - `auth.oidc: client_secret_ref is only for client_secret_basic and client_secret_post`;
  - `auth.oidc: client_key_ref, client_key_id and client_cert_file are only for private_key_jwt`;
  - `auth.oidc.scopes: must include openid`;
  - `auth.oidc.roles: map at least one IdP group to a role`;
  - `auth.oidc.roles.<role>.<i>: must be a non-empty group id without surrounding whitespace`;
  - `auth.oidc.group_claims.<i>: must be a claim name or a list of non-empty claim names`;
  - `auth.oidc.id_token_algs.0: Input should be 'RS256', …`;
  - `auth.oidc.proxy: must be http(s)://host:port without credentials`.
- **When `ds serve` starts (it reads the client credential before it starts the server), and in
  `ds auth check`:**
  - `cannot read secret file <path>: …`;
  - `auth.oidc.client_key_ref: <path> is not a PEM RSA (>= 2048 bits) or EC P-256 private key`;
  - `auth.oidc.client_cert_file: <path> is not a PEM certificate of client_key_ref's key`.
- **At sign-in and in `ds auth check`:** each §2.1 check names its values, for example
  `discovery issuer <a> differs from auth.oidc.issuer <b>`.

## 9. UI

**`api.ts`:**
- Drop `TOKEN`, `setToken`, the stored token and `Authorization`.
- Send `X-CSRF-Token` (from `me`, kept in memory) on non-GET requests, and `X-DS-Background: 1`
  (§3).
- A 401 dispatches `LOGIN_EVENT`.
- **`tokenLogin`** posts the token, then reads `/auth/me`, whose body it returns and whose CSRF token
  it keeps. A 401 `not signed in` from that read means the browser did not send the session cookie
  back: it is rethrown as `ApiError(401, body, NOT_KEPT)`. A refused POST passes through unchanged. The
  sign-in still wrote its `auth.login` row and a live session the browser cannot use.

**`signin.ts`** ([FIRSTRUN.md](FIRSTRUN.md) §6) is pure: no import and no request. Nothing in it comes
from the server, so an anonymous visitor learns its own address bar and a command's name, and
`public_url` is told to nobody. Its texts are binding:
- **`TOKEN_HINT`**, under the token form: `An API token comes from the command line of the machine that
  runs DeltaSleuth: whoever administers it runs "ds token create" there and passes the token on. It is
  shown once. This page cannot create one.`
- **`addressHint(detail, at)`**, under the form's error, only when `detail` is `cross-origin request
  refused`; `at` is `window.location`. On a loopback page: `DeltaSleuth takes a sign-in from one address
  only, the one in its settings (auth.public_url). This page was opened at http://127.0.0.1:8000. On the
  machine that runs it, try http://localhost:8000 instead; if that is refused too, the settings name
  another address.` It names the other of `localhost` and `127.0.0.1`, with the same scheme and port,
  and at `[::1]` both, joined by ` or `. Elsewhere the third sentence is `Ask whoever runs it for the
  address.` The hint is a guess from the address bar.
- **`NOT_KEPT`**: `The token was accepted, but this browser did not send the session cookie back, so you
  are not signed in. Allow cookies for this address, or use another browser.`

**`main.tsx`:**
- Load `health`, then `me`. A `Who` context gives `can(role)`.
- **Sign-in.** oidc shows a button that calls
  `location.assign('/v1/auth/login?next=' + encodeURIComponent(location.hash || '#/runs'))`, with
  `#/runs` also when the page is `#/signin` itself, so a sign-in never returns to its own error. Tokens
  mode shows a password field that calls `tokenLogin`, then leaves `#/signin` for `#/runs`; under it
  stand a refusal's `addressHint`, when it has one, and `TOKEN_HINT` (`signin.ts`).
- **Errors.** `#/signin?error=` codes map to fixed messages: `login_expired`, `login_failed`,
  `idp_unavailable`, `access_denied`, `idp_error`, `no_role`, `groups_overage` and `disabled`. Any
  other code shows "The sign-in failed." When `request_id` is present and has the form `Q-<16 hex>`,
  the message adds "Quote Q-… to your administrator". So a crafted link cannot put its own text on the
  page.
- **Signed out.** `#/signin?signed_out=1` shows "You are signed out." It is a notice, not an error
  code.
- **Nav.** The name and Sign out, plus a developer-mode banner. Developer mode has no session to end,
  so it shows no Sign out (`/logout` would answer 400).
- **Names beside ids.** Every name the UI shows (nav, Run screen, Admin) is followed by its user or
  token id, for example `jdoe (U-3f2a…)`. Other actors are their own id, and one without a separate
  name (`ds.actor_name` names only users and tokens, so a run by `dev:loopback`) shows once.
- **Role gates** (the server still decides):
  - operator: run and compare controls;
  - spec_author: spec controls, and "Request approval" under SSO;
  - rule_approver (a person): Approve, Reject and Revoke on `Approvals.tsx`, which exists under SSO only
    (APPROVALS.md §6.3);
  - admin: a new `Admin.tsx` with audit, tokens, users and sessions. It is hidden for
    `kind: token`, which can never hold admin (§1), and shown in developer mode. Each is a tab:
    `#/admin` (tokens), `#/admin/users`, `#/admin/sessions` and `#/admin/audit`. Only the audit tab
    reads the log, so opening the Admin screen writes no `audit.read` row.
- **Fresh sign-in.** A 403 `reauth_required` from token creation shows "Sign in again to create a
  token". Its button calls
  `location.assign('/v1/auth/login?reauth=1&next=' + encodeURIComponent('#/admin'))`. An approval
  step does the same with `next` set to its review, `#/approvals/A-…` (APPROVALS.md §5).
- **Run screen.** It shows `created_by_name`, with `created_by` beside it.
- **Licence** ([LICENCE.md](LICENCE.md) §12). `main.tsx` loads `api.licence()` after `me` (no banner
  if that fails) and shows each warning as a banner on every screen, linking to `#/licence`, the Licence
  screen (`Licence.tsx`), which the nav offers every role. Without the licence's `allows`, Runs' "Start",
  Key's "Compare now", the resume of a run paused for the licence, "Request sign-off" and "Issue
  evidence pack" are disabled, with the first warning as the reason (`web/src/licensing.ts`); the server
  still decides. An open build's body holds no warning and allows everything, so no banner shows and
  nothing is disabled, and the Licence screen says `This build needs no licence and limits nothing.`
  under the installation id (LICENCE §19).
- **Readiness** ([READINESS.md](READINESS.md) §10). The nav offers `#/readiness` to every role; the
  screens (`Readiness.tsx`) only read, so no control is hidden by role, and their polls every 60 s carry
  `X-DS-Background: 1` once the tab is idle (§3).
- **Table pre-checks** ([PRECHECKS.md](PRECHECKS.md) §11). The nav offers `#/prechecks` to every role,
  after Schedules. Viewers read the list, a pre-check, a pair and the reports; the stored-spec form,
  Cancel, `Run a comparison` and `Pre-check…` on the Specs screen show to operators, the two-schemas form
  and `Draft a spec` to spec authors (`Prechecks.tsx`), and the server still decides. The Run screen's
  `Resume (accept the changes)` is the resume control renamed where a resume would accept the schema
  guard's changes, shown to operators as Resume is.
- **Schedules** ([SCHEDULES.md](SCHEDULES.md) §10). The nav offers `#/schedules` to every role, after
  Specs. Viewers read the list, a schedule and its firings; the form (with Preview), Edit, Pause,
  Resume, Delete and Move to the newer version show to operators (`Schedules.tsx`), as does `Schedule…`
  on the Specs screen, and the server still decides. The screens poll every 60 s, with
  `X-DS-Background: 1` once the tab is idle (§3). A firing names no person: its text is the server's.
- **Notifications** ([ALERTS.md](ALERTS.md) §11). `main.tsx` loads `api.notifications()` after `me` (no
  banner if that fails), shows each of its `banners` on every screen, viewers included, linking to
  `#/notifications`, and offers that screen (`Notifications.tsx`) in the nav while notifications are on.
  Send test and Send failed again show to operators, the Mute form and End to admins other than tokens
  (`web/src/notify.ts`); a 403 `reauth_required` from a mute shows "Sign in again to mute", whose button
  returns to `#/notifications`.

## 10. Tests

No image is pulled; PostgreSQL stays the local `postgres:16-alpine` fixture.

**Fake IdP (`tests/unit/auth/fake_idp.py`).**
- **Server.** A `ThreadingHTTPServer` on `127.0.0.1:0` (the `_Sides` pattern of
  `tests/unit/api/conftest.py`), with issuer `http://127.0.0.1:<port>`.
- **Keys.** RSA-2048 and EC P-256, generated per test with `cryptography` and published with
  PyJWT's `to_jwk`.
- **Endpoints:**
  - overridable discovery;
  - `/jwks`, counting fetches;
  - `/logout`;
  - `/token`, which checks a one-time `code`, the exact `redirect_uri`, PKCE, and each client
    authentication method. It verifies a `client_assertion` with PyJWT for `aud`,
    `iss = sub = client_id`, `exp ≤ iat + 60` and a unique `jti`.
- **Controls.** `authorize(location, **claims) -> code` overrides any claim or header, and records
  `prompt` and `max_age`. There are also `rotate()`, `down`, `slow = 11`, `date_offset` and
  `requests`. The `slow` test lowers `auth.oidc.TIMEOUT`, so the suite does not wait 10 s.
- **Helper.** `sign_in(client, idp, groups=…, reauth=False)` drives login, authorize and callback.

**Clients and fixtures.**
- **Auth modes:** `TestClient(app, base_url="https://ds.test", client=("10.1.2.3", 50000))`, so
  `__Host-` cookies round-trip.
- **A plain-http loopback `public_url` (E1):** the same client at `base_url="http://localhost:8000"`
  (`open_client(auth={"public_url": "http://localhost:8000"})`), and a second installation of its own
  folder, keys and database at `http://localhost:18000`. The client's jar returns no `Secure` cookie
  over http, as a browser that does not take loopback for secure.
- **Behind a proxy:** `TestClient(app, base_url="https://ds.test", client=("10.20.0.5", 50000))`
  with `trusted_proxies: [10.20.0.5]`. This exercises the deployed `ProxyHeadersMiddleware` (§3).
- **Developer mode:** `open_client` (`tests/unit/api/conftest.py`) passes `base_url="http://127.0.0.1"`,
  `client=("127.0.0.1", 50000)` and the header `X-CSRF-Token: dev`. Its `token=` option becomes
  `auth=`. `tests/e2e/test_api.py` builds its own `TestClient(create_app(...))` and takes the same
  three settings.
- **Principals and `DS_OPERATOR`:**
  - `tests/conftest.py` provides `TESTER`, a `local:tester@test` principal, for every call that
    now takes `who` or `created_by`;
  - tests that configure `auth` set `DS_OPERATOR=tester`, and the tests of its absence delete it.
- **Too low for viewer routes:** no role sits below viewer. So the too-low principal is a
  `ds.sessions` row inserted directly with roles `'{}'`.

**Behaviours** (one test each). Every error message in this contract is asserted, and so is:

| File | Behaviours |
|---|---|
| `tests/unit/test_config.py` | Every §8 load error, including `allowed_hosts` with an http `public_url`, an empty or padded group, and a malformed group path. A full `auth` block, with string and list group paths |
| `tests/unit/auth/test_principal.py` | `expand`; the token format; `printable` on C0, DEL and C1. `local()`: `DS_OPERATOR` in the actor, the character reduction, the cuts at 64 and 253 characters, and `unknown` |
| `tests/unit/auth/test_sessions.py` | E1: `ds_session_8000`, `ds_login_18000` and `ds_session_80` for `http://localhost`; the https names unchanged, a loopback https address included; an http address beyond loopback, which the settings refuse, is never plain; nor is developer mode |
| `tests/unit/auth/test_oidc.py` | RS256 and ES256 pass. A parametrised refusal for each §2.3 defect, including `none`, HS256 signed with the client secret, a 200-character non-ASCII subject (over 255 octets), and a stale or missing `auth_time` under `reauth`. Discovery checks. One refetch per rotation, and one per 60 s for unknown kids. Each `group_claims` form, including `[resource_access, deltasleuth.bank, roles]`. Exact matching: `ds.admin` and `DS.Admin ` do not match `DS.Admin`. `email` is never the username. A control character in a name gives `login_failed`. Overage both ways. `x5t#S256`. The RFC 7636 Appendix B vector. The login cookie: a round trip, a changed byte, and expiry |
| `tests/unit/agent/test_tools.py` | `tools_for(viewer)` lacks `compare_key`; `run_tool` refuses it |
| `tests/unit/api/test_auth_flow.py` | The redirect and cookie flags. Both cross-site refusals (`Sec-Fetch-Site: cross-site`, `Sec-Fetch-Mode: no-cors`) set no cookie and write no row. The callback's `auth.login`. Each `client_auth`. Re-sign-in keeps `user_id` and revokes exactly the session presented at `/login`, not one presented only at the callback. `reauth=1` sends `prompt=login&max_age=0`. `subject_claim: oid`. Each pre-identity failure code writes **no audit row**, and its redirect carries the request id. Each denial reason; `auth.roles_changed`. A refused user's repeated sign-ins write no new `ds.users` version and one `auth.login_denied` per 10 minutes. Sessions and tokens during an IdP outage. An evil `next`, and a `next` with `+`, `*`, `'` and `(`. Each session ending, background polls, and an edited role mapping (401 `session expired`). Each CSRF layer; bearer is exempt. Tokens mode, 404 under oidc. `X-Forwarded-User` and `SM_USER` get 401. No secret, cookie or token in `ds` tables or `caplog`. E1: at `http://localhost:8000` the session cookie is `ds_session_8000` with exactly `HttpOnly; Path=/; SameSite=strict`, the client's own jar returns it for `/me` and a CSRF write, sign-out expires it, and its secret under any other name is 401 `not signed in`; two loopback installations share one jar and keep their sessions, and signing out of one leaves the other; `https://ds.test` and `https://localhost:8443` set the prefixed `Secure` cookie byte for byte and take no plain name; no `Host`, `Origin` or forwarded header chooses the form; the login cookie follows the same rule |
| `tests/unit/api/test_app.py` | Health modes, and its 503, in one text, when the pool gives no connection in 3 s and when the connection it gives fails at once. The developer-mode refusals: a non-loopback client, and one parametrised case per proxy header (`Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host`, `X-Forwarded-Proto`, `X-Real-IP`, `Via`). `/v1/health` with an IP `Host`. `Host` with and without the default port, in upper case, and with a non-default `public_url` port. `no-store`; HSTS. `AuthQueryFilter`, called directly: it strips `/v1/auth/*` queries and leaves other paths alone |
| `tests/unit/api/test_roles.py` | A dict copies §4.1. For every `app.openapi()` operation, no credential gets 401, one role too low gets 403, and the right role gets neither. Unlisted routes fail, with the UI built and without it (the unbuilt UI's `GET /` is outside the schema). `create_app` refuses a route without `principal`. `require("rule_approver")` and `require("pii_reader")`: `dev()` gets `the … role needs a signed-in person`, a token gets `needs the … role`, and a user holding the role passes. `authz.denied`; 100 denied calls to one route write one row, and the next window's row carries the count. Chat tool offers, the forced call, `via`, and 404 for another user |
| `tests/unit/api/test_admin.py` | Tokens shown once. `owner_user_id` is required under oidc and must be an enabled user. 401 and 422 cases; 403 for non-admins; 403 `reauth_required` for an `auth_time` over 15 minutes old. Disable, enable and the self-disable 409. Disable lists the tokens created or owned by the user, and revokes them only with `revoke_tokens`. Sessions |
| `tests/unit/api/test_audit.py` | `request_id` and `X-Request-ID`. `session_id` on cookie-session rows, NULL on bearer, CLI and system rows, and the `session_id` filter. `client_addr` behind the proxy client: `X-Forwarded-For: 203.0.113.9, 10.1.2.3` records `10.1.2.3`, and the same header from an untrusted peer is ignored. Worker and `local:` actors, with `asserted` and `os_account`. `key.view`, `report.download`. `run.finished` and `run.cycle` by `system:worker`, and a cancel's `run.finished` by its caller. `system.migrate`. `/v1/audit` paging. `roles_sha256`. The SIEM line appears after commit, and a rolled-back transaction emits none. UPDATE and DELETE are refused |
| `tests/unit/store/test_repo.py` | 005 over legacy rows; `created_by`; `actor_name()`; the control-character and subject CHECKs |
| `tests/unit/store/test_context.py` | A CLI context has `oidc = None` and reads no client credential |
| `tests/unit/cli/test_cli.py` | The serve refusal and warning; `ds serve` passes `proxy_headers=False` and adds `AuthQueryFilter`, and refuses a port it cannot listen on before the store is opened ([FIRSTRUN.md](FIRSTRUN.md) §8). With `auth` set, every "yes" command exits 2 without `DS_OPERATOR`; without `auth` it runs as the OS account. `ds runs` works with the client key file absent. `ds token` stdout; `session`, `users` (`--revoke-tokens`) and `audit` commands. `auth check` and its clock warning |
| `tests/unit/deploy/test_config_examples.py` | The example with `auth` loads |
| `web/test/signin.test.ts` | Each text of `signin.ts` exact, at `127.0.0.1:8000`, `localhost:18000`, `[::1]` and an https host; no hint for another refusal; the module holds no `import` and no `fetch` |
| `web/test/api.test.ts` | `X-CSRF-Token` from `/me` or `tokenLogin` on non-GET only, and forgotten at sign-out; `tokenLogin` reads `/me`: a 401 `not signed in` there rejects with `NOT_KEPT`, a refused POST keeps the server's detail, and the CSRF token is `/me`'s; no `Authorization`; `X-DS-Background: 1` after 60 s without `pointerdown`, `keydown` or `wheel`; a 401 dispatches `LOGIN_EVENT`; a source gate against `dangerouslySetInnerHTML`, `innerHTML` and `localStorage` |

**The MCP server** ([MCP.md](MCP.md) §16). `tests/unit/api/test_roles.py`'s dict holds the three
`/v1/mcp` operations as `viewer`. `tests/unit/api/test_mcp_http.py` asserts the route's refusals in each
mode, that no body is read before the caller is known, that a revoked token stops at its next call, that
a machine token never bypasses SSO (a token an admin made meets the four-eyes gate and leaves no session;
`token-login` stays 404 under oidc), that no `pii_reader` acts inside MCP, and that the `mcp` block's cuts
are the route's: what MCP refuses a token, `/v1` serves the same bearer with its full role (§5).
`tests/unit/mcp_server/test_audit.py` holds `mcp.read`, `key.view` and `authz.denied` with `via`, and
`tests/unit/auth/test_principal.py` `Throttle.forget`.

## 11. What four-eyes approvals and the reveal build on

**This contract provides:**
- `user:U-…` ids and `Principal.person`;
- `TOKEN_ROLES`, which the DDL keeps tokens to;
- `require("rule_approver")` and `require("pii_reader")`, which refuse any non-person (§1);
- `ds.specs.created_by` and every uploader's `spec.create` row;
- `ds.api_tokens.owner_user_id`, so a token's uploads name a person;
- step-up: `/v1/auth/login?reauth=1` sends `prompt=login&max_age=0` and requires a fresh
  `auth_time` (§2.3);
- `ds.sessions.auth_time`, `acr` and `amr`;
- `ds.audit_log.session_id`, so an approval shows the session it came from.

**Four-eyes approvals are built** ([APPROVALS.md](APPROVALS.md)), on the list above:
- `ds.approvals` (008), one row per request of kind `spec` (a content hash) or `signoff` (an ended
  cycle), whose `decided_by` and `first_by` CHECKs admit `user:U-…` actors only, and whose trigger
  `ds.tg_approvals` checks every write: the decider is an enabled `ds.users` row whose stored roles hold
  `rule_approver`, outside the exclusion set;
- `ds.accountable(actor)`: a `user:` actor is accountable itself; a `token:` actor makes its creator (if
  a `user:`) and `user:<owner_user_id>` accountable; `local:`, `dev:`, `system:` and pre-005 actors make
  nobody accountable. `ds.attributed(actor)` is true for `user:` actors only;
- **the four-eyes rule** (APPROVALS.md §3): the exclusion set is the requester plus, for a spec version,
  every uploader of the name's versions since its newest approved version (`ds.spec_accountable`), or,
  for a sign-off, the cited approval's authors and the run's creator. An unattributed actor behind the
  set (a token, CLI, developer-mode or pre-005 upload, a token-started run, a token requester) makes
  two approvers outside the set necessary, so a version saved by the CLI or before 005 is approved by
  two people rather than refused. The route refuses with 409 `approval needs an approver other than
  you: <why>`;
- `approval.request`, `approval.grant`, `approval.reject` and `approval.revoke` (§6);
- an approval step needs an `auth_time` at most 15 minutes old, else 403 `reauth_required` (§5).

**The reveal adds** (not built; labelled accuracy needs it to locate records of a tokenised key with no
path in clear, and until then asks for `acknowledge_unlocatable`: [ACCURACY.md](ACCURACY.md) §4):
- `POST /v1/runs/{run_id}/reveal {obs_id, json_pointer, reason}` for `pii_reader`;
- a `pii.reveal` row committed in its own transaction **before** the evidence is read, so it fails
  closed;
- the same step-up;
- `no-store`.

**Deferred:**
- back-channel logout (needs a `sid`);
- `client:<id>` principals;
- `acr_values`;
- mTLS client authentication to the IdP;
- TLS and mTLS on `ds serve` itself. DEPLOY's TLS reverse proxy terminates TLS today;
- ABAC;
- DB role separation.

The hash-chained audit log with off-host anchors (`ds audit verify`) is built with evidence packs
([PACK.md](PACK.md) §4).

## 12. Dependency, upgrade, acceptance

**Dependency: `PyJWT[crypto] >= 2.10, < 3`** (MIT).
- **It pulls** `cryptography` (Apache-2.0 OR BSD-3-Clause), `cffi` (MIT) and `pycparser`
  (BSD-3-Clause). None is in `uv.lock` today. All go through the C.7 gate and `exclude-newer`.
- **Why:** it replaces the riskiest hand-written code: JWS and JWK handling, claim checks, assertion
  signing, and PEM and thumbprint handling. The login cookie needs only the standard library's
  `hmac`.
- **Rejected:** Authlib (its own OAuth client and sessions) and python-jose (unmaintained).

**Upgrade:**
1. Start fails on `api_token_ref`.
2. Replace it with `auth`.
3. Run `ds auth check`.
4. An admin signs in and creates each CI token on the Admin screen, with its owner. In tokens mode,
   use `DS_OPERATOR=<you> ds token create` instead.

History keeps `api` and `ds`. A spec stored before 005 has an unattributable `spec.create` row,
and the same content keeps the same `spec_id`, so its approval needs two approvers (§11).

**Acceptance checklist (DEPLOY §5):**
1. No credential gets 401.
2. A forged `X-Forwarded-User` or a foreign ID token gets 401.
3. A cross-origin POST gets 403, and so does a cross-site `<img>` sign-in.
4. `no_role` is refused.
5. A polling tab still idles out.
6. Sign-out reaches the IdP.
7. Disable is immediate, and `--revoke-tokens` ends the person's tokens.
8. A group removal applies at the next sign-in; a role-mapping edit ends every session.
9. Token creation asks for a fresh sign-in.
10. Every step above appears in `/v1/audit` and in the SIEM copy, attributed to a person or a named token.
