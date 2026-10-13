# DeltaSleuth runbook

This runbook takes a team from "we have two services that should agree" to a signed-off P0 contract
report, with the `ds` command line. It covers what to send, the commands, how to review the profiler
draft and the adjudication sheet, how to label a sample against the truth, how to read cut-over
readiness, the exit criteria, and what to do when something goes wrong.
Installing DeltaSleuth in the office (Docker, secrets, an OpenAI-compatible gateway) is in
[DEPLOY.md](DEPLOY.md), whose section 9 runs these commands in the deployment. The UI and the API
(`/v1`) do everything below except the P0 tools and file-mode runs. Two PostgreSQL tables compare
the same way; [Compare two tables](#compare-two-tables) shows what differs, and
[Graph parity](#graph-parity) compares the structure that tables of link facts make.

## The black-box principle

DeltaSleuth compares **what two endpoints return**. It does not need to know, and never asks, how
either service is built. A target is:

- an endpoint (`base_url`),
- a credential reference (`file:` path; never an inline value or an environment variable),
- a politeness budget (`rate_per_s`, `max_concurrency`),
- and its **declared, observable behaviour** (`capabilities`): which not-found responses mean "this
  key does not exist", how truncation is signalled, whether the endpoint paginates.

If you do not know the declared behaviour, `ds measure` measures it from a run and proposes a
`capabilities` block with the evidence behind each line.

A database target is a DSN reference and a budget. DeltaSleuth reads only the table or view a spec
names, in read-only sessions: what the database returns, never how it is built.

## What to send

Choose one of the two routes.

### Route A: paired responses as files

One directory with a sub-directory per side. Side `a` is the baseline (legacy) service, side `b`
the candidate (new) service.

```
samples/
  a/<key>.json          the response body exactly as served
  a/<key>.meta.json     optional: {"status": 404, "headers": {...}, "latency_ms": 12.5, "observed_at": "2026-09-24T10:00:00Z"}
  b/<key>.json
  b/<key>.meta.json
```

- `<key>` is the key percent-encoded as one file-name segment (`/` becomes `%2F`, `.` becomes `%2E`).
  `python -m mocks.fixtures <pair> --out DIR` writes this layout for the mock pairs.
- Without a meta file a body counts as HTTP 200. **Send not-found responses too**, with their real
  status in the meta file: they are how DeltaSleuth learns the not-found contract.
- A paginated side needs its whole page walk: merge every page's items into the first page's body
  and add `"pages": n, "pages_eof": true, "pagination_unstable": false` to its meta file. Otherwise a
  sample showing a page a walk would follow (a next cursor or `Link`, or any non-empty offset/page
  window) is INCONCLUSIVE `truncated`.
- Alternatively, JSON Lines files at the top level (`samples/*.jsonl`), one record per line:
  `{"key": "...", "side": "a", "status": 200, "body": {...}}` (`body_text` or `body_b64` for non-JSON
  bodies).
- Never include credentials, cookies or authorization headers. DeltaSleuth drops such headers anyway.

### Route B: endpoints and a key list

- `targets.yaml`: one entry per side's target (see `mocks/linker/targets.yaml` for a complete
  example). Credentials are referenced as `secret_ref: file:/path/to/token`. `https` is required
  when auth is set unless you explicitly allow plain HTTP. A service can be declared in the browser
  instead ([Endpoints declared in the product](#endpoints-declared-in-the-product)).
- `keys.txt`: one key per line (`#` starts a comment), or a `key.source` in the spec (an inline
  `list`, a `paginated_endpoint` walked through the target's own budget, or a key table: `kind: table`
  with a `target` and a `table`, a table, a view or CSV or Parquet files of keys, read when the run is
  created and at each roll of a rolling soak, at most 1,000,000 keys).
- A request template per side in the spec (`sides.a.request`, `sides.b.request`), for example
  `GET /v1/customers/{{ key }}`.
- A service asked with several values has a multi-part key: `key.type: composite` with `key.parts`
  (`parts: [region, shelf, code]`), each part fitting `key.pattern` and named in a request as
  `{{ keyParts.region }}`. Such a key is written as its parts joined by `|` (`eu|s1|7`; `\|` and `\\`
  for a `|` or `\` inside a part, and any other `\` refuses the key) in `keys.txt`, the "Keys" box, an
  inline list and `ds compare`.

A live run then calls both endpoints politely: per-target rate budget and in-flight cap, retries
with backoff for timeouts, 429 and 5xx, a circuit breaker, a pause on the first 401/403, and the
spec's `execution.stop` conditions. One worker per database processes keys, so a target's budget
holds across every run and, while that worker is the one inside `ds serve`, the app's (UI and API)
one-key compares and key listings; a command-line `ds compare`, or the key listing of `ds run`, has its
own process's budget, and so has the API process under `ds serve --no-worker`, whose one-key compares
and key listings then come on top of what `ds worker` sends ([DEPLOY.md](DEPLOY.md), section 7). Every
response, failed calls included, is kept as evidence.

The circuit breaker is the spec's own: `execution.circuitBreaker` declares an error rate over a window
of reads and a cooldown, and a spec without the line has no breaker. The process keeps one breaker per
target for each such declaration (rate, window and cooldown), so a run is governed by what its own spec
declares, whichever spec called the target first. Once the window is full and its share of failed reads
(connection errors, timeouts, 429 and 5xx, after the retries) reaches the rate, the breaker opens: for
the cooldown, the calls of the runs and one-key compares whose specs declare the same breaker are not
sent, and their keys end `INCONCLUSIVE` with `circuit_open`. It refuses no call of a spec that declares
another breaker, or none; the target's rate budget and in-flight cap stay shared by them all. After the
cooldown one call probes the target: a success closes the breaker, a failure opens it for another
cooldown. Nothing needs a restart, and a process that starts has every breaker closed.

### Endpoints declared in the product

A service is declared, changed and removed without the targets file and without a restart on the
Targets screen, or through `PUT` and `DELETE /v1/targets/{id}` (DEPLOY.md section 7, "In the product"):
its id, its base URL, the two consents (a private or local address; a credential over plain http), how
it authenticates (nothing, a bearer token, or a header with up to three further credential headers), its
request budget and whether a 404 means "no such record". An admin signed in within 15 minutes declares;
in a one-machine trial (`deploy/up.sh`) a `spec_author` token does; in tokens mode behind https no one
does, and the targets file is the only way. Databases, files targets and every other capability stay in
the targets file, which wins for an id declared in both.

- **Try an endpoint, then build the comparison.** On the Specs screen "New comparison", or a stored
  spec's name, opens the comparison builder: each service side's request as a form, `Import curl…` to
  fill it from a request copied as cURL, and `Send`, which asks that one endpoint once for the sample key
  and stores nothing. A trial shows the answer as it came; any other installation shows its structure,
  every value a stand-in. `Save and compare this key` stores a version and compares the key: build under
  a scratch name, because a stored version is never deleted and is at once the newest of its name.
- **Credentials** are typed once and written in clear, each to a file of its own in the workspace
  (`targets/secrets/`, mode 0600), which the server alone reads. No screen, answer, log line or audit row
  shows one again (where a service answers the request it was sent, `Send` shows
  `[the target's credential]` in place of each string that holds one), and a backup of the workspace
  carries them. To replace one, `Change` the endpoint and
  type it; a box left empty keeps what is stored. A credential that holds DeltaSleuth's own session or
  sign-in cookie is refused: a request to an app on the same host, copied from the browser, carries that
  cookie beside the app's own, and `Import curl…` leaves it out.
- **Audit.** `target.set`, `target.delete` and `target.try` name the target and the caller: header names
  and never a credential, what was asked and never an answer. A `target.try` row holds the sample key
  as reports show it: a stand-in, unless the spec declares the key a business code.
- **Other processes.** `ds serve` applies a change at once. A `ds worker` or `ds mcp` beside it, and a
  `ds run` or `ds compare` on the command line, read the list when they start: restart a long-running one
  after a change.
- **A list that does not load.** The list is the file `targets/targets.json` in the workspace, which
  the server alone writes. Edited or damaged, it stops `ds serve` and every `ds` command at their start
  (exit 2) with its issues, each ending with the file:
  `error: / [json_syntax] Expecting value (line 1, column 21) (the product's list, /var/lib/deltasleuth/workspace/targets/targets.json)`.
  A symbolic link stops them the same way, whether it stands for the `targets` folder, its `secrets`
  folder or a credential the list names (`[link] ... of the product's list is a symbolic link: remove
  the link`): the product reads, sends and removes only files of its own, never through a link.
  Nothing repairs it in place. Remove its folder, which holds the list and its credentials, start the
  app, and declare the endpoints again (the audit log's `target.set` rows say with what; each credential
  is typed again). Until then a spec that names one does not validate, and its runs fail.

  ```bash
  docker compose run --rm --no-deps app rm -r /var/lib/deltasleuth/workspace/targets
  docker compose up -d app
  ```

  In a checkout the folder is `targets` under the settings' `workspace_dir`. The message was observed in
  a checkout on 2026-10-08; the two commands were not run.

## Setup

The office runs DeltaSleuth from a signed release, verified on the Docker host before it is loaded
(DEPLOY.md section 2), with each command in a throwaway container (DEPLOY.md section 9). A checkout,
set up as below, is for development and for trying the product out.

Every command reads one settings file, `--config <file>` or `$DS_CONFIG`: the database DSN and the
targets file (both by path), the evidence and workspace directories, and optionally the LLM profiles.
A checkout's is a copy of `deploy/config/deltasleuth.dev.example.yaml`: four lines, developer mode, and
every path, the DSN's `file:` reference among them, relative to the file's own folder
([FIRSTRUN.md](FIRSTRUN.md) §5). `deploy/config/deltasleuth.example.yaml`, the Docker installation's,
documents every other line. [The guide's second chapter](../guide/02-run-from-source.md) takes these
steps one by one, the database included.

```bash
uv sync                      # installs DeltaSleuth and the `ds` command
mkdir -p .deltasleuth        # git ignores it
cp deploy/config/deltasleuth.dev.example.yaml .deltasleuth/deltasleuth.yaml
# .deltasleuth/db_dsn: one line, your PostgreSQL 16 database's DSN (the example's comment shows its form;
# percent-encode special characters in its user and password, for example @ as %40)
export DS_CONFIG="$PWD/.deltasleuth/deltasleuth.yaml"   # absolute, so that ds finds it from any folder
export DS_OPERATOR=<your user name>   # the name your audit rows carry (with the auth settings)
uv run ds init               # without `encryption`: the workspace's tokeniser.key and kek (mode 0600), once
uv run ds migrate            # creates the database schema (serve, worker and run also apply it)
```

The tokeniser key makes every protected value a stable token (`tok:<class>:v1:...`), so equal values
stay visibly equal across sides, keys and runs. Keep it: a new key gives every value a new token. The
keyring (`kek`) wraps the keys that encrypt every raw value stored ([Retention, purge and
keys](#retention-purge-and-keys)): without it, the runs' raw data cannot be read. Never commit or
share either, and keep a copy of both somewhere safe, as `ds init` says (`a lost key cannot be made
again`). A deployment keeps both as secrets
outside the workspace, named by the `encryption` settings (DEPLOY.md section 3); `ds init` then only
creates the workspace.

The CLI reaches the database directly, outside the role model: it acts as
`local:<DS_OPERATOR>@<host>` with every role. With the `auth` settings, each command that writes
audit rows (`compare`, `run`, the report commands, `token create` and `revoke`, `session revoke`,
`users disable` and `enable`, `audit`, `purge` but for `--dry-run`, `purge hold` and `release`,
`keys migrate`, `approvals revoke`, `packs download`, `licence install` and `withdraw`) exits 2 without
`DS_OPERATOR`. Without them it defaults
to your OS account. The name is asserted, not verified: rows carry `asserted` and your OS account.
Commands that open the database copy their audit rows to stderr as JSON lines, for the SIEM, so their
stdout stays their output.

## Sign-in, roles and tokens

The deployment's `auth` settings decide who may do what ([AUTH.md](AUTH.md); [DEPLOY.md](DEPLOY.md)
section 3):

- **SSO (oidc mode):** people sign in on the UI through the site's identity provider (IdP), and their
  roles come from their IdP groups at each sign-in. Scripts and CI use named API tokens, sent as
  `Authorization: Bearer dst_…`. Its runs need no licence in this build (in a vendor build, an
  Enterprise one: [Licence](#licence)).
- **Tokens mode** (CI and trials; in a vendor build, also production under a Community or Pro licence):
  the UI asks for an API token, and whoever signs in with one is audited as that token.
- **Developer mode** (no `auth`, one workstation): no sign-in, and every local user and process acts
  as admin. The UI shows a banner saying so.

The nav shows who you are: your name and id, as in `jdoe (U-3f2a…)`. A browser session ends after
`session_idle_minutes` without use (30 by default; a Run screen's polls do not count), after
`session_max_hours` (8), and at Sign out. Roles are read at sign-in: after a change in the IdP, sign
out and in again.

### Roles

Each role holds every role above it in this table:

| Role | Can |
|---|---|
| `viewer` | Read runs, results, findings, key views and reports, specs, targets, LLM profiles, the licence and the schedules with their firings; read a cycle's labellings and download their sheets; chat. A chat session is visible only to whoever started it. |
| `operator` | Start, pause, resume and cancel runs; compare one key, also through the chat agent's `compare_key` or, where the settings serve it, an MCP agent's; draw a labelling of an ended cycle and upload its labels ([Labelling against the truth](#labelling-against-the-truth)); request a cycle's sign-off (SSO); declare, edit, pause, resume and delete schedules ([Schedules](#schedules)) |
| `spec_author` | Upload and validate specs; build one in the browser and send one side's request to its endpoint ([Endpoints declared in the product](#endpoints-declared-in-the-product)); draft a spec from samples; request a spec version's approval (SSO); in a one-machine trial, declare endpoints |
| `rule_approver` | Approve, reject and revoke spec versions and sign-offs ([Four-eyes approvals and sign-off](#four-eyes-approvals-and-sign-off)), never what they uploaded, started or requested |
| `pii_reader` | Reserved for revealing raw values (not built yet) |
| `admin` | The Admin screen: API tokens, users, sessions and the audit log; declare, change and remove endpoints, within 15 minutes of signing in |

- `rule_approver` and `pii_reader` are granted only to a person signed in through the IdP, never to a
  token, the CLI or developer mode. A token holds at most `spec_author`.
- The chat agent acts for the asker: for a viewer it never compares a key live.
- An agent of your own, over MCP, acts as its API token and never above `operator`, whatever its
  caller holds ([Your own agent (MCP)](#your-own-agent-mcp)).
- The screens hide the controls a role lacks; the API refuses with 403 `needs the <role> role`.

### API tokens

A token is a named machine credential, `dst_<id>_<secret>`, with an id `T-…`. Admin, Tokens, New API
token asks for:

- **Name:** 3 to 64 characters of `a-z`, `0-9`, `.`, `_` and `-`, starting with a letter or digit.
  Audit rows carry it as the token's name. Names may repeat.
- **Owner:** the team or contact that runs it.
- **Accountable person:** someone who has signed in; required with SSO.
- **Role:** `viewer`, `operator` or `spec_author`.
- **Days:** 1 to `token_max_days` (default 90).

The token is shown once. Creating one needs a sign-in within the last 15 minutes; otherwise the screen
says "Sign in again to create a token" and offers the button. To rotate a token, create a new one
under the same name, switch the job to it, then revoke the old one.

**A token for an agent** ([Your own agent (MCP)](#your-own-agent-mcp)): one token per agent, named
for it (`claude-code-jdoe`), owned by the person who runs it, `viewer` unless the agent must compare a
key (`operator`). Keep it as one line in a file of mode 0600, or in the editor's secret store; never
paste it into a chat, a client's configuration file or an environment variable, and never commit it.
A browser session cannot stand in for it: the MCP route takes a token only. Revoking it stops the
agent at its next request. It is an API token like any other: every `/v1` route its role reaches takes
it, whatever the `mcp` block serves, so an agent that can read its token's file can do with it
whatever the role allows. A `viewer` token reads; an `operator` token also starts, pauses, resumes and
cancels runs.

From the command line (tokens mode, or recovery), `ds token create` prints only the token on stdout:

```bash
uv run ds token create --name ci-nightly --owner "Payments QA" --role operator   # [--owner-user U-...] [--days 30]
uv run ds token list
uv run ds token revoke T-...
```

### Administration

The Admin screen (role `admin`, with SSO or in developer mode) has four tabs:

- **Tokens:** create, list and revoke.
- **Users:** everyone who has signed in, with the roles of their last sign-in, for access reviews.
  Disable ends the user's sessions at their next request and lists the tokens they created or own;
  "Disabling also revokes every token the user created or owns" ends those too. Enable lets them sign
  in again; revoked tokens stay revoked. Nobody can disable themselves.
- **Sessions:** the live sessions; Revoke ends one at its next request.
- **Audit:** the audit log, newest first, filtered by actor, action, object, session and time.
  Reading it is audited too (`audit.read`).

The CLI is the host administrators' channel: for recovery with SSO, and for everything in tokens
mode, which has no admin over the API:

```bash
uv run ds users                                   # --json for the rows
uv run ds users disable U-... --revoke-tokens
uv run ds users enable U-...
uv run ds session revoke --all                    # or --session W-..., --user U-...
uv run ds audit --action token.create --since 2026-09-01   # also --actor, --object, --session, --until, --limit, --json
```

`--since` and `--until` take ISO 8601 times, UTC when they name no offset.

**A leaver.** Removal from the IdP stops their sign-in, but the tokens they created or own keep
working. Disable them here with their tokens: Admin, Users, Disable with the tokens box ticked, or
`ds users disable U-... --revoke-tokens`. The schedules they last saved then start no run until another
operator saves them again ([Schedules](#schedules)).

## Four-eyes approvals and sign-off

With SSO, a spec version whose content can expose values, suppress findings or narrow what is compared
runs only once someone who did not write it approved its content, and an ended cycle becomes parity
evidence only once someone who neither wrote its spec, started its run nor asked for the sign-off
accepted it ([APPROVALS.md](APPROVALS.md) is the contract). Tokens and developer modes have no
approvers: their runs go ahead, labelled unapproved, and are never signed off, even once they resume
or attach an approval under SSO.

**What needs an approval.** A version needs one when its content has any of these scopes; uploading it
answers which (`needs`), and the Specs screen shows its newest request in the Approval column:

| Scope | In the spec |
|---|---|
| `clear` | `security.clear` paths: values stored and shown untokenised |
| `unlisted_clear` | `security.unlisted: clear` |
| `known_diffs` | active `knownDiffs` rules, which suppress findings |
| `population_filters` | a table side's `where` or `columns`, a file side's `csv.skip` above 0, `compare.include`, `exclude` or `valueFilter` |
| `sampling` | a `hash_sample` key source: its frame, strata, sizes, salt label and retries ([Sign-off sampling](#sign-off-sampling)) |

The approval covers the whole content hash, so the approver reviews everything else in the diff too
(tolerances, normalisers, key sources, `select`, `map`). A new version needs a new approval; nothing
expires.

**Request, review, decide.** A spec author requests a version's approval with Request approval on the
Specs screen; one request per version is open at a time. People with `rule_approver` decide on the
Approvals screen (in the nav with SSO only): the list filters by status and marks "Two approvers
needed". A review shows the earlier requests of the version, then Approve and Reject (disabled with the
reason when you may not approve), then the items each scope covers (scope, pointer, value), who may
not approve it, and the diff against the version approved before it (A) and this one (B), keys masked.
Rejecting needs a note. Approving sends back the content hash and scopes you reviewed, so a version
whose content or needs changed meanwhile is refused (reload, or request it again). Approving needs a
sign-in within the last 15 minutes: the review then offers Sign in again, which returns to it.
Rejecting and revoking need no fresh sign-in. The CLI can neither request nor grant an approval.

**Who may not approve.** The requester; for a spec version, everyone who uploaded a version of that
spec name since its newest approved version (an upload through a token counts the token's creator and
its accountable person), so a colleague's bump of your version still leaves you out; for a sign-off,
the authors the spec approval it ran under recorded, and the run's creator. When someone behind that set
cannot be named (an upload through the CLI, a token, developer mode or before 005, a run started by a
token or the CLI, or a request by a token), two approvers outside the set must approve: the first takes
step 1 of 2 (its note is not kept) and cannot take step 2. CI uploads therefore need two approvers.

**Runs.** With SSO, a run, compare or resume of a version that lacks the approval it needs is refused
(409 in the API, exit 2 in the CLI), writing nothing:
`spec 12 (customer-profile 1.4.0) needs a four-eyes approval of clear, known_diffs before it runs`.
`ds run` and `ds compare` find their spec file by content hash: a version that needs an approval must
have been uploaded and approved (`spec customer-profile 1.4.0 has no approved upload: upload it on the
Specs screen, signed in, and have it approved (a CLI or token upload needs two approvers)`); one that
needs none is stored and runs as before. In tokens mode `ds run` and `ds compare` warn that the run is
labelled unapproved. The Run screen and `ds runs` label the run, and `report.pdf`'s header and the P0
summary's "Four-eyes approval" section the reported cycle, by the approval it ran under (a cycle that
ended unapproved stays so, whatever the run holds now), saying one of:

- `Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.`
  (`developer mode has no approvers`; a run from before approvals: `the run started before approvals (008)`);
- `Spec approval A-... was granted by bob on 2026-09-27.` (two approvers: `by amy on ... and bob on ...`);
- `Spec approval A-... was revoked on 2026-09-27 by jdoe (wrong clear path). Not sign-off evidence.`;
- `The spec version needs no four-eyes approval.`

A run started before approvals keeps its `(008)` label whatever it attaches or resumes under.
`report.pdf` and the P0 summary add the cycle's sign-off: `Sign-off A-... was granted by ...`, or
`Sign-off A-... was revoked on ... by ... (...). Not sign-off evidence.`

**Sign-off.** The Run screen lists each ended cycle with its sign-off requests; with SSO an operator
asks for one with Request sign-off. This build asks for no licence for it. In a vendor build
([Licence](#licence)) requesting and approving a sign-off need an Enterprise licence: otherwise both
answer 409 `four-eyes sign-off is an Enterprise feature and licence LI-... is pro (...)` (or `... this
installation is an unlicensed evaluation: ...`), and Request sign-off is disabled with that reason;
rejecting and revoking need none. The request is refused (409),
naming each problem, unless the cycle
belongs to a live batch or soak run started after approvals and under SSO (not one-shot, not from
captured samples, not started in tokens or developer mode or by a CLI whose `--config` lacks
`auth.oidc`, whatever it resumed under since), has ended with counters of its own, is complete (no key
cancelled or owed a recheck, something compared, every counter identity holding, a table scan read to
the end rather than stopped by `until`, `maxDuration` or a cancel), was not cut at `maxKeys`, has no key
with an unexplained CRITICAL finding (a `bug` rule's suppression counts as unexplained) and ran under a
spec approval still approved (or, when its version needs none, the version has one; a resume that gave
the run its first approval covers only its next cycles, unless a soak's boundary paused the cycle before it read any key). Its review shows the checklist, with the mode
the run started in, a table scan's counts, the key source (an explicit key list, `explicit` or `list`,
is flagged: it is not a census), the counters, and each known-diff rule with the keys it suppressed and
each root cause, one per row: **approving a sign-off decides each of them.** The request fixes a hash of
these facts; a cycle whose facts changed since is refused (`... changed since the sign-off was
requested; request it again`). The last approval step holds the run (`signed off: A-...`, [Retention,
purge and keys](#retention-purge-and-keys)). From evidence packs on (migration 009) the facts also hold
the cycle's census and the adjudication it attests, which its review shows, and whoever scored that
adjudication may not approve it ([Evidence packs](#evidence-packs)). From labelled accuracy on (migration
013) they also hold the labelling the sign-off attests, the cycle's newest that a signed-in person
labelled, or none: its review prints what that labelling says, and whoever labelled it may not approve
the sign-off ([Labelling against the truth](#labelling-against-the-truth)).

**Revoke.** A `rule_approver` revokes an approved spec approval or sign-off on its review, with a reason
of 1 to 200 characters (it reaches the audit log and the SIEM: no personal data); host administrators
can with `ds approvals revoke` (break-glass). Revoking a spec approval, in one transaction: its running
runs pause (`run_paused_approval`), its queued one-shot runs stop (`approval_revoked`), the approved
sign-offs citing it are revoked (`spec approval A-... was revoked`), open ones are rejected (or, after a
CLI revoke, refused at their decision), and a run left without an approved sign-off loses its sign-off
hold. Keys already being read finish. Resuming a paused run needs a new approval of its version:
`spec 12 (customer-profile 1.4.0) needs a new four-eyes approval: A-... was revoked`. Releasing a
signed run's hold (`ds purge release`) lets its cycles expire, and purging a signed cycle revokes its
sign-off (`cycle 2 purged`) unless an issued evidence pack cites it. Revoking a sign-off, by any path
(a cascade from its spec approval, `ds approvals revoke`, even SQL), revokes its evidence packs.

```bash
uv run ds approvals                  # newest first; --status requested|approved|rejected|revoked, --kind spec|signoff, --limit N, --json
uv run ds approvals --needed         # spec versions of queued, running or paused runs that need an approval none covers, with those runs
uv run ds approvals revoke A-... --reason "wrong clear path"
```

`--needed` prints `spec 12 (customer-profile 1.4.0) needs clear, known_diffs: run R-... (running), run
R-... (paused)` per version, or `every live run's spec version has the approval it needs`.

**Upgrading to approvals** (migration 008):

1. Map an IdP group to `rule_approver` in `auth.oidc.roles`: without one, `ds serve` and
   `ds auth check` warn `no IdP group maps to rule_approver or above: nothing can be approved`.
2. Stop the app, then run `ds approvals --needed` with the new release: it applies 008, which refuses
   nothing, and lists the versions live runs use that will need an approval. 008 also re-hashes every
   stored spec body: a logged warning and a `spec.rehash` audit row name those that do not match their
   content hash, and with SSO their runs are refused (`spec 12's stored body does not re-hash to its
   content hash (edited, or not storable as uploaded): upload a changed version (a new version)`).
   Uploading the same file again maps to the same stored row: upload a changed version (bump
   `metadata.version`) and have it approved; runs of the old version cannot resume.
3. Start the app. With SSO, new runs of those versions wait for an approval; batches already running
   finish; a live soak attaches the approval at its next cycle boundary, or starts that cycle paused
   (`run_paused_approval`), and one paused longer than its retention is cancelled.
4. Request each listed version's approval as it is (a CLI, token or pre-005 upload in its lineage needs
   two approvers; a token-started run's sign-offs need two approvers, not its spec approval), resume
   the paused runs once approved, and run `ds approvals --needed` again.

Runs started before 008 keep working, labelled unapproved whatever they attach or resume under, and
are never signed off.

## Evidence packs

With SSO and the `packs` settings (DEPLOY.md section 3), and in a vendor build an Enterprise licence
([Licence](#licence)), a
signed-off census cycle, or a sampled one ([Sign-off sampling](#sign-off-sampling)), gets an evidence pack:
one signed ZIP that an auditor checks offline, years later, with stock tools and the site's key register
([PACK.md](PACK.md) is the contract). Every claim in it is *observed*. Tokens and developer modes have no
sign-offs, so no packs (409 `evidence packs need SSO (auth.oidc); this deployment runs in tokens mode`);
without `packs` the pack routes answer 503. This build refuses no pack for a licence, and its packs
name none: the manifest's `licence` is null and the certificate has no licence line. In a vendor build,
without an Enterprise licence, or once it has ended past its
grace, issuing answers 409 with the licence's reason first (`evidence packs are an Enterprise feature and
licence LI-... is pro`, or `licence LI-... ended on ...`), and the pack names the licence it was issued
under in its manifest and certificate. A run made in an unlicensed evaluation, a vendor build's or an
older build's that enforced the licence, never gets one, in this build either (`run R-... was made in
an unlicensed evaluation: no evidence pack is issued for it; start a new run`, which a vendor build
ends `... under the licence`).
Downloads and verification never need a licence.

**1. Census.** A pack covers a cycle whose side a (the baseline) was counted in that cycle: a table
scan read to its end (and graph parity, for a graph spec), a complete key listing of side a's target,
or, for keys given with a run whose side is a table, the worker's key census, which checks the key set
against the filtered table in one snapshot at the start of each cycle (packs configured only; of a MySQL
view or a MySQL table outside InnoDB the census line says what it was read in instead). The Run
screen shows each ended cycle's census (`side a counted, 1,204 keys listed, 2026-09-20; side b not
covered`), and the live cycle's `census pending` or `census of side a failed (QueryCanceled): no pack
will cover side a`. Each statement of a count (the sort, and each fetch of 10,000 rows) is bounded to
an hour, not the count as a whole, and no key of the cycle is leased until it is recorded; a timeout
fails the count, and the cycle goes on.

**2. Adjudicate, before asking for the sign-off.** From 50 per-key results in the cycle, the sign-off
must attest a reviewer sheet a person scored, and it must pass. On the Run screen an operator presses
**Draw** for the ended cycle (up to 50 keys by default, 1 to 500), **Download sheet**, has the reviewers
fill `reviewer_verdict` as in [Reviewing the adjudication sheet](#reviewing-the-adjudication-sheet),
and uploads it with **Upload reviewed sheet** (UTF-8, at most 1 MiB). Each sheet is scored once
(`adjudication J-... is already scored: draw a new sheet`); only the verdicts are kept, never the notes
or anything else typed. Then request the sign-off: it attests the newest sheet a person scored, and
from then on the cycle takes no draw or score (`cycle 2 of run R-... has a sign-off (A-...): revoke it
before a new adjudication`). Every draw is audited and listed in the pack, so redrawing until a sample
passes shows.

**3. Issue.** Once the cycle is signed off, an operator signed in as a person presses **Issue evidence
pack** (a token gets 403 `issuing a pack needs a signed-in person`). A refusal (409) names every reason,
joined by `; `, for example `cycle 2 of run R-... has no scored adjudication in its sign-off A-...:
revoke it, draw and score a reviewer sheet, then request the sign-off again`, `cycle 2 of run R-... is not
a census: side a's key census is pending`, `the sign-off A-... of run R-... cycle 2 was signed before
evidence packs (attests format 0): revoke it and request the sign-off again`, `the pack's texts hold 3
raw key-shaped value(s) (known_diffs[2].when): no pack carries them` or `the audit trail of run R-...
does not verify at row 81234: run ds audit verify`. A pack is issued in one audited transaction
(`pack.issue`, whose SIEM line names its `sums_sha256`); a later pack of the same cycle lists the
earlier ones.

**4. Hand it over.** **Download** on the Run screen (any viewer; audited `pack.download` before it is
sent), or `ds packs download P-... --out DIR` (audited once written). The stored copy is deleted when
the cycle is purged, and downloads answer 410 from the purge's start (`pack P-... was purged with cycle
2 on ...: its stored copy is deleted; copies downloaded before still verify`): archive the downloaded
ZIP with the `pack.issue` SIEM line. A pack holds only masked aggregates, the audit rows of its objects
and, per key, masked keys: the attested sheet's, those of the run's `key.view` rows (each key a viewer
opened, in any cycle) and those of the cycle's AI answers; no raw key, body, header, note or chat text,
and its CSVs no key tokens.

**5. Verify.** The auditor unzips it, files the register's procedure beside it as `FILED-PROCEDURE.txt`,
and runs that filed copy step by step in `sh` in the pack's folder (OpenSSL or LibreSSL, `sha256sum` or
`shasum -a 256`), never text from the pack: step 0 checks `VERIFY.txt` from its line 3 against the filed
copy and reads the pack's values from its line 2, never running it (a line 2 of another form prints
`line 2 of VERIFY.txt is malformed`), step 1 prints the key's fingerprint to compare with the register,
step 2 the signature, step 3 the files, step 5 the decisions, step 6 the audit rows' links, step 7
compares them with the SIEM's export (rows up to the upgrade field by field with their SIEM lines), and
step 8 checks the custodian's revocation list. Where DeltaSleuth is installed, `ds packs verify` runs
the same checks without settings:

```bash
uv run ds packs verify P-....zip --fingerprint <the register's> --revocations revocations/
```

It prints `step N <name>: ok (...)` or `FAILED (<why>)` per step (steps 4 and 7 show what to compare by
hand) and exits 1 on a failure; a ZIP over 64 entries or 200 MiB, compressed over 100:1, with a
symlink, an absolute path or `..`, a name twice, a manifest it cannot read, or of another format is
refused (exit 2).

**Revocation.** A pack is revoked only with its sign-off: revoke the sign-off (Approvals screen, or
`ds approvals revoke`), and its packs are revoked, audited `pack.revoke`. The custodian publishes the
signed list for step 8: `ds packs revocations --out DIR` (or `GET /v1/packs/revocations`, a ZIP) writes
`revoked.txt`, its signature and the signing key. Give auditors a list dated after their decision date.

**The audit chain.** Every audit row is hash-chained; `ds audit verify` checks the whole log in one
snapshot and prints its head (file it daily: DEPLOY.md section 10), and `--siem FILE` also compares the
rows with an export of the SIEM's lines:

```bash
uv run ds audit verify                     # chain: 81,234 row(s) verified, 1 to 5120 chained by 009; head: row 81234 <hash>
uv run ds audit verify --siem siem.jsonl   # also: siem: 76,114 line(s) match: rows 5121 to 81234
```

A break exits 2 naming the first row, for example `error: row 81234 (approval.grant A-...) does not hash
to its chain entry: the audit log was altered after it was written`: stop issuing packs, keep the
database as it is, and compare the row with its SIEM line.

```bash
uv run ds packs                            # newest first; --run R-..., --json
uv run ds packs key --out DIR              # fingerprint.txt and procedure.txt, for the key register
uv run ds packs revocations --out DIR      # revoked.txt, revoked.txt.sig, signing-key.pem
uv run ds packs download P-... --out DIR   # the stored ZIP, audited
```

## Sign-off sampling

A population too large for a census (20M keys take 11.6 days at 20 req/s) is signed off on a stratified
hash sample that nobody chooses, each sign-off stratum judged by the engine on an exact one-sided 95%
Clopper-Pearson rule ([SAMPLING.md](SAMPLING.md) is the contract). Every claim stays *observed*. Draws
work without a licence (in a vendor build, in every tier and in evaluation); the `sampling` approval,
closes and the sign-off need SSO, and in a vendor build the sign-off and its pack an Enterprise licence,
as for a census.

**1. Size.** Each sign-off stratum is a bucket with a `targetSize` and a `riskTier`: `standard` (target
0.1%, the default) or `high` (0.01%). A pass with no discrepant key needs 2,995 (standard) or 29,956 (high)
cleanly compared keys; draw 3,011 or 30,107, the 0.5% margin covering keys that stay unresolved, absent or
truncated. `ds samples size` prints the sizes for the discrepant keys a stratum may still pass with, and
needs no settings:

```bash
uv run ds samples size --tier standard              # standard (≤ 0.1%, one-sided 95%): 2,995 cleanly compared keys pass with at most 0 discrepant; draw 3,011 (0.5% margin); ...
uv run ds samples size --tier high --failures 1     # high (≤ 0.01%, one-sided 95%): 47,437 cleanly compared keys pass with at most 1 discrepant; draw 47,676 (0.5% margin); ...
```

Fix the sizes in the spec before any key is compared: a sample is never extended, and a failed one is
never re-tested. A bucket smaller than its tier's 2,995 or 29,956 is warned `undersized`: it passes only
as a census, when the frame holds no more keys of it than its `targetSize` (every one drawn, judged on the
exact count: discrepant plus unresolved keys at most ⌊target × (keys − absent on both)⌋). A sample holds at
most 1,000,000 keys; the 12 standard and 3 high-risk strata of `docs/architecture.md` §15 make 126,453,
about 1.8 h at 20 req/s.

**2. Write the spec.** A `hash_sample` key source draws from a *frame*: a table side's population (`kind:
table`, read in one snapshot; a MySQL view or a MySQL table outside InnoDB is read without one the
database can show, and the frame's line, the certificate's warning and the pack's claim say so:
[Across engines](#across-engines)), a side's complete key listing (`paginated_endpoint` of that side's target),
or a `union` of one of each. Each stratum dimension reads a column (`strataColumns` of a table,
`strataFields` of a listing) or a CEL expression over `key`, `row` and `now` (the frame's read start), and
lists its buckets, so every label is a spec constant; `signoffStratum` gives each key one sign-off label,
high-risk conditions first:

```yaml
key:
  source:
    kind: hash_sample
    method: bottom_k                 # rate: monitoring only, never signed off
    salt: cutover-q4                 # the campaign label; each draw adds a nonce
    resolve: {times: 3, delay: 10m}  # retries of a never-compared sign-off key (the defaults)
    strata:
      - {name: flagged, column: flagged, buckets: [{label: Y, values: ["Y"], targetSize: 30107, riskTier: high}, {label: N}]}
      - {name: degree, column: degree, buckets: [{label: d0, max: 0, targetSize: 3011}, {label: other, targetSize: 3011}]}
    signoffStratum: "riskTier == 'high' ? 'flagged=Y' : 'degree=' + strata.degree"
    source:
      kind: union
      sources:
        - {kind: table, side: a, strataColumns: [degree, flagged]}
        - {kind: paginated_endpoint, target: new-api, request: {…}, keyPath: $.id, pagination: {…}, strataFields: {degree: $.degree, flagged: $.flagged}}
```

`ds spec validate` refuses what cannot be sampled soundly, for example an inline list as the frame, two
listings of one side, a listing of a third target, a hash sample between two table sides, a dimension
without buckets, a strata column a source does not expose, or `execution.stop.maxKeys` (it would cut the
sample); SAMPLING.md §2.2 lists every message. A salt or bucket label shaped like a key of the spec is
warned `label_key_shaped`, and a bucket's value or bound (`values`, `min`, `max`, numbers too)
`value_key_shaped`: a pack would refuse it.

**3. Approve.** A hash sample needs the `sampling` scope: under SSO someone who did not upload the version
approves its frame, strata, sizes, salt label and retries before any run draws (the review shows
`/key/source` and every earlier draw of the population), and the sign-off cites that approval.

**4. Draw.** Start a batch, or a rolling soak (`ds run spec.yaml --soak --rolling`, or Runs on the UI),
never with `--keys` or `--samples` (`a hash sample draws its keys from the frame: run it without --keys`;
a soak that does not roll: `a soak over a hash sample draws a new sample each cycle: start it rolling (ds
run --soak --rolling)`). Only one `bottom_k` run per population is live at a time, whatever its spec name
(`spec customer-profile samples the population of live sampled run R-... (spec customer-profile-2,
table:legacy-db/customers): stop it before drawing another sample (docs/mvp/SAMPLING.md §5)`). The run is
created at once with no keys; the worker then reads the frame from the declared targets only, draws a
nonce (the salt is `label:nonce`), takes each bucket's keys with the smallest SHA-256 of the salt and the
key, and stores the draw, its keys and the `sample.draw` audit row in one transaction, before any key is
compared. That row holds the salt's SHA-256, never the nonce, and the nonce is withheld from every screen
while the cycle is live, and afterwards, with each stratum's `u_max`, from anyone below rule_approver (with
keys in clear they would tell which keys lie in a stratum compared whole). `ds runs` shows `drawing sample`
while the run is running (`sample to be drawn after resume` while it is paused), then `sample D-...: 15
strata, 126,453 keys`. A
frame that cannot be sampled pauses the run (`run_paused_draw`) and says why on the Run screen and in `ds
run`, for example `the key listing of target new-api was not read completely (HTTP 200: max_pages (30000)
reached before the last page)`, `the key listing of target new-api lists 12 key(s) twice: ...` or `no
frame key falls into sign-off bucket flagged=Y: a stratum without keys certifies nothing (correct its
values, or remove it in a new version)`. Fix it (a new version when the spec is at fault) and resume: the
worker draws again, with a new nonce. A draw of two sources, or of a listing, deduplicates the frame in the
database (DEPLOY.md section 10, `sampling.temp_buffers`).

**5. Watch.** The Run screen's Sample panel shows the draw and its frame (`Frame: 20,211,004 distinct keys
(a only 111,134; b only 106,673; both 19,993,197); 0 outside key.pattern.`), each sign-off stratum's
progress (drawn, done, compared, discrepant, outstanding, unresolved, absent on both sides, truncated,
retrying) with its interim bound and verdict, a stratum already failed for certain in red, an ETA from
the keys finished in the last 15 minutes, and the population's ledger. Keys are read in an order that
advances every stratum at the same pace, so a stopped cycle has compared a uniform part of each. `ds run`
prints each stratum of the cycle it waited for once the run ends:

```
stratum degree=d0 (standard, ≤ 0.1%): 3,011 drawn of 1,204,331, 3,009 compared, 0 discrepant, 2 unresolved, bound 0.09951%: passed
```

**6. Retries.** A sign-off key that no read compared (an error, a timeout, an unstable listing, or
rechecks that all failed) is read again from its first pass after `resolve.delay` plus jitter, at most
`resolve.times`; the panel reads `resolving` once only retries are left. A key that read MATCH or found a
discrepancy is never read again, and truncated, absent-on-both, ambiguous, unparseable and drifting keys
are never retried: they stay unresolved, absent or truncated, and count against the stratum's gates.

**7. The judgement.** When the cycle ends, each sign-off stratum is judged once and stored, never
recomputed: it passes when it is complete, its one-sided 95% bound is within its target, its unresolved
keys are at most ⌊target × compared⌋, its keys absent on both sides at most ⌊target × drawn⌋ (more means a
stale frame: draw again), and no key was truncated above an API cap. The cycle's status is `passed` (every
stratum), `failed` (any) or `incomplete`. `report.pdf` and the P0 summary add "Sample and bounds
(observed)", with two bounds that are reported but never judged: one counting every known or noise-only
difference discrepant, one counting unresolved, absent and truncated keys discrepant (the worst case).
`coverage.csv` adds a row per stratum and a `(contrast)` row: keys another bucket's rate chose, compared
and reported, never bounded. No bound is given for the strata pooled.

**8. Close a draw that did not pass.** A failed sample is spent. Fix the cause, then close every ended
`bottom_k` draw of the population that did not pass and is not closed, with a note of the fix: "Record the
fix and close failed draws" on the Run screen, or `POST /v1/samples/close {spec_name, note}`, as an
operator signed in as a person under SSO (the CLI closes nothing). The note (1 to 1000 characters, no
customer keys: it reaches audit rows and evidence packs) says what was fixed; approvers judge it. A later
draw of the population, under any spec name, is signed off only if each earlier draw passed or was closed
before it was drawn: stopping, failing or abandoning draws until a selection suits is recorded, and an
incomplete or unjudged draw needs a close too. A rolling soak pauses at the boundary after a `bottom_k`
draw that did not pass (`run_paused_sample`); resuming it is refused until that draw is closed (`run R-...
paused after draw D-... did not pass: close it with the fix (POST /v1/samples/close), then resume`), so the
fix and its close come before the next draw. Rate draws are monitoring: never closed, never blocking.

```bash
uv run ds samples                           # every draw in commit order: status, close, "blocking: close it with the fix"
uv run ds samples --spec customer-profile   # the name's ledger: every spec's draws of its population; --json
```

**9. Sign off.** Request the ended cycle's sign-off as for any cycle; the Sample panel's precheck lists
what would refuse it. On top of [Four-eyes approvals and sign-off](#four-eyes-approvals-and-sign-off),
a sampled cycle needs its own `bottom_k` draw, keys that re-hash to that draw's selection (nothing
extended, trimmed or relabelled), no frame key outside `key.pattern`, every sign-off stratum passed (for
example `stratum degree=d6_20 of cycle 2 of run R-... is over its 0.1% target: its one-sided 95% upper
bound is 0.2101% (2 discrepant of 2,995 cleanly compared keys)`), every earlier draw of the population
passed or closed before it was drawn (`draw D-... (spec customer-profile-2, run R-... cycle 2) failed
before draw D-... was made and was not closed before it: ...`), and no later draw failed, ended without a
judgement, or holds a stratum failed for certain. Its review shows the draw, frame, strata and the whole
ledger, each draw before, this or after the signed one; whoever closed an earlier draw of the ledger may
not approve (`you closed draw D-... of its ledger`). The final approval step records the later draws as it
judged them.

**10. Issue the pack.** As in [Evidence packs](#evidence-packs), with two more refusals: side a must be in
the frame (`cycle 2 of run R-... is not a census: its sampling frame does not enumerate side a (the
baseline); ...`), and the draw must match its audit row (`the sample D-... of cycle 2 of run R-... does not
match its sample.draw audit row: no pack`). The certificate reads `Claim level: observed (sampled)`, and
states the frame, the draw, each stratum's bounds, the population's other draws with their close notes,
the later draws at the decision and at issue, and what a sample does not prove. The auditor adds one check
by hand, which the certificate prints: row N of `audit/chain.tsv` is action `sample.draw` and holds both
SHA-256 values, and the salt's SHA-256 is that of `label:nonce`:

```bash
printf '%s' 'cutover-q4:3f9a...' | sha256sum       # the salt SHA-256 the certificate and row N hold
```

`ds packs verify` prints the same check as `step 5 sample draw: ok (row N is draw D-...'s sample.draw row
and holds both SHA-256 values; ...)`, or `FAILED (...)`.

## Licence

**This build needs no licence.** It embeds no vendor key, so it is an open build
([LICENCE.md](LICENCE.md) §19): it enforces no licence and limits nothing. Any number of pairs (spec
names) run in every sign-in mode, sign-off and evidence packs ask for no licence, no new run or report
is labelled, and no screen or command warns of one. `uv run ds licence` prints:

```text
installation I-3f2a9c1b7d4e5a60
licence none: this build needs no licence
state open
allows: runs yes, sso yes, signoff yes, packs yes
```

The Licence screen (in the nav for every role) says `This build needs no licence and limits nothing.`
under the installation id. `ds licence id` prints the id alone; `ds licence install` and `ds licence
show` refuse every file (exit 2, `error: this build embeds no vendor key: it needs no licence and can
verify none (docs/mvp/LICENCE.md §19)`). A database an older build that enforced the licence ran as an
evaluation (every build from migration 010 until this one, or a vendor build) keeps what was recorded
then: those runs keep their "unlicensed evaluation" label and get no evidence pack, and a soak paused
for the licence resumes when a person resumes it.

**The rest of this section is a vendor build's,** a release that embeds the vendor's keys: the licence
mechanism stays in the code for it, and nothing below applies to this build.

A licence is a text file the vendor signs for one installation (one database), naming the customer,
the order, the tier, how many pairs run and a period of at most a year ([LICENCE.md](LICENCE.md) is the
contract). DeltaSleuth stores it in the database and checks it against the vendor keys the build embeds,
offline, at every decision: no setting, secret or phone-home. Without one, the installation is an
**unlicensed evaluation**.

| | Evaluation (none) | `community`, `pro` | `enterprise` |
|---|---|---|---|
| Pairs (spec names) | the first 2 ever | the licence's (by default 2 and 5) | the licence's (by default unlimited) |
| Runs under SSO | no | no: tokens mode | yes |
| Four-eyes sign-off, evidence packs | no | no | yes |
| Labels | every run and its report files say "unlicensed evaluation" | the licence | the licence |

**Pairs and seats.** A pair is a spec name: its versions, re-runs, soak cycles and one-shot compares
count once, and a graph spec once. The first names with a run in the licence's period (from its first
day; in evaluation, ever) hold its seats, and seats never move within a period. A run of another name
is refused: `spec cards would be pair 6 of 5 under licence LI-... (pro, 2026-10-01 to 2027-09-30): its
seats since 2026-10-01 are accounts, customers, loans, payments, transfers, whose runs go ahead; a new
pair needs a licence with more pairs (...)`, or in evaluation `spec cards would be a third pair: ...`. A
name counted without a seat (after a downgrade to fewer pairs) finishes the cycle under way, then its soak
pauses at the next cycle (`run_paused_licence`) until a licence seats it; a boundary judges the licence of
the day the next cycle falls due, so a downgrade starting before then already pauses it.

**Its life.** From 30 days before the end every screen and `ds serve`, `worker`, `run` and `compare`
warn; for 30 days after it everything still works, with warnings (grace); then new runs, compares, soak
cycles and evidence packs are refused, and soaks pause at their next cycle (one due after the grace
pauses at the boundary before it): `licence LI-... ended on 2027-09-30 and its 30 days of grace on
2027-10-30: ...`. Reads, reports, exports, holds, erasure,
purges, the audit log, pack downloads and sign-offs under an Enterprise licence always work, and a cycle
under way always ends. Renewals arrive 60 days ahead: install one when it arrives, and it waits for its
first day and silences the ending warnings. Before a grace ends, finish the sign-offs that wait (each
holds its run) or `ds purge hold` what awaits one; after it, install the renewal and resume the runs
paused for the licence.

**Commands** (installing and withdrawing need `DS_OPERATOR`; in Docker, DEPLOY.md section 3):

```bash
uv run ds licence id                     # the installation id, alone: send it with the request for a licence
uv run ds licence                        # the licence, its state and warnings, what it allows, each counted pair and its seat
uv run ds licence --json                 # the same as GET /v1/licence, plus the audit head: the true-up for a renewal
uv run ds licence show LI-....txt        # a file's fields and signature, offline, without settings
uv run ds licence install LI-....txt     # or - for stdin; applies at once everywhere, or from its first day
uv run ds licence withdraw LI-... --reason "wrong tier"   # only before its first day
```

`install` prints what it installed and when it applies (`in force now`, or `in force from 2026-10-01;
LI-... applies until then`), what changed now (`changes now: tier pro -> enterprise; pairs 5 ->
unlimited`), the names left without a seat, `runs under SSO stay refused (M5)` for a lesser tier under
SSO, and the runs paused for the licence it lets resume: installing resumes nothing, so resume each on its
Run screen. Installing the same file again changes nothing. A started licence is never withdrawn, only
replaced by one that starts later (an upgrade, downgrade or correction takes over from its first day), so
no withdrawal returns an installation to evaluation.

The Licence screen (in the nav for every role) shows the same as `ds licence`, and who installed or
withdrew each licence and why; every screen shows the licence's warnings as banners linking to it, and
the controls it refuses are disabled with the reason. `GET /v1/runs/{id}`, the Run screen, `ds runs` and
`report.pdf` show the licence each run was made under; an evaluation's report files are named
`R-...-unlicensed-evaluation-<name>`.

**A fresh database is a fresh installation:** a new id, a new evaluation, and no licence of the old id
applies; a dump and restore keeps the id and the licences. A backup from before migration 010 gets the
same id when 010 applies to it again (from the store's first audit row), so the licence files install
on it; only a database whose audit log was empty when 010 applied (a new one) gets a random id, which
only a backup taken after 010 keeps.

## Commands, from start to report

Paths below are examples. Every command accepts `--help`.

```bash
# 1. Draft a spec from paired samples, then review it (see below)
uv run ds profile --samples samples/ --out profile/ --name my-parity \
    --target-a legacy-api --target-b new-api --key-name customer_id \
    --request-a "GET /v1/customers/{{ key }}" --request-b "GET /v2/customers/{{ key }}"

# 2. Validate the reviewed spec (schema, loader rules, and the target rules with --targets)
uv run ds spec validate spec.yaml --targets targets.yaml

# 3. Compare every key: from the samples ...
uv run ds run spec.yaml --samples samples/
#    ... or live, with the recheck of discrepant keys (--keys: else the spec's key source)
uv run ds run spec.yaml --keys keys.txt

# 4. Measure the observable contract and get proposed capabilities (--missing-keys: keys you
#    know do not exist; their answers are the only basis of a proposed authoritative absence)
uv run ds measure R-... --out p0/ --missing-keys missing-keys.txt

# 5. Build the adjudication sheet (<= 50 stratified keys), let reviewers fill it, score it
uv run ds adjudicate R-... --out p0/
uv run ds score --out p0/

# 6. Probe the chat's LLM profile (an external one only with --environment eval: see DEPLOY.md)
uv run ds llm-probe --profile gateway

# 7. Write the P0 contract report (report.md and report.html), the PDF and the CSV exports
uv run ds report R-... --out p0/ --kind summary --kind report.pdf --kind discrepancies.csv --kind retry.csv
```

`ds run` prints the run id, processes the run in the same process and prints its counters when it
ends. One worker per database processes keys: while `ds serve` (the API, the UI and the worker in
one process; with `--no-worker`, the API and the UI alone) or `ds worker` runs, `ds run` logs `another
worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this
one takes over only if it stops` and waits for
that worker to finish the run. With `--no-wait` it only starts the run. `ds runs` lists the latest
runs. `--soak` (not with `--samples`) repeats the run every `execution.cycleInterval` (`--rolling`
reads the key source again each cycle; when that listing or key table fails at a cycle boundary, the soak keeps
the previous cycle's keys, logs "run R-... keeps its key set: <reason>" and audits
`rolling_resolve_failed`); the Run screen and the PDF list the keys whose outcome changed between
cycles.
`ds report --kind` takes the report names of the table below, as the API serves them, or `summary`.

Keep one `--out` directory per run for the P0 tools: `ds report --kind summary` reads the
measurement and the score from it.

One key at a time, live, with a concise verdict (outcome, differing fields with masked values, the
one-shot run that holds it and its observation ids):

```bash
uv run ds compare C00012007 --spec spec.yaml
```

Exit codes: `0` success, `1` discrepancies found or still owed a recheck (`ds run` and `ds compare`
with `--fail-on-diff` only), `2` usage, spec, input or configuration errors (one `error:` line
each, `ds serve` included), `3` an internal error (a bug; the traceback is printed), `130`
interrupted (Ctrl-C). A crash never exits `1`, so CI can tell it from a diff.

## Compare two tables

DeltaSleuth also compares two PostgreSQL tables or views, in one database or in two, key by key and
column by column ([TABLES.md](TABLES.md) is the contract). The engine, the outcomes, the reports, the
UI and the chat are those of a service run. `examples/tables/` is a complete demo with seed SQL, a
row filter and a type map. A side may also be a MySQL or Oracle table or view:
[Across engines](#across-engines), below, says what changes.

**1. Declare each database as a target.** A database target is a DSN reference and a budget, nothing
else:

```yaml
targets:
  crm-legacy:
    dsn_ref: file:/run/secrets/crm_legacy_dsn   # one line: a libpq keyword string or a postgresql:// URI
    rate_per_s: 50                              # per-key reads; a scan is one query per side
    max_concurrency: 2
```

The DSN is read when a connection opens and never reaches logs, evidence or errors. Every session is
read-only; give DeltaSleuth a read-only role all the same ([DEPLOY.md](DEPLOY.md), section 7). A
missing row is an authoritative absence, so a table side declares no `notFound` or `truncation`.

**2. Write the spec.** A `table` replaces a side's `request`, `select` and `map`:

```yaml
key: {name: customer_id, type: string, pattern: "^[0-9]{6}$", class: identifier}
sides:
  a:
    role: legacy
    target: crm-legacy
    table:
      schema: crm                                # optional: else the search_path finds the name
      name: customers                            # a table, view, materialized view or foreign table
      keyColumns: [customer_id]
      columns: [{name: email_address, as: email}, status, balance]   # optional
  b:
    role: new
    target: platform-users
    table: {name: users, keyColumns: [user_id]}  # no columns: every non-key column
```

- **Columns.** A listed name is compared under its lower-cased name, `{name, as}` under the new name.
  Without `columns`, every non-key column is compared under its lower-cased name; between two tables,
  only the names both sides have, and the others are listed as columns on one side only. A listed
  column without a counterpart on the other side, or no column to compare, refuses the run. To
  compare only which keys exist, list the key column as the only column (`columns: [customer_id]`
  on side a, `columns: [{name: user_id, as: customer_id}]` on side b).
- **Names** match the column spelled exactly, else the one column with that name in lower case (as
  PostgreSQL folds unquoted names).
- **Keys** are the key columns' values as text, or what a key map makes of them (below). A composite
  key (`key.type: composite` with `key.parts`) lists one key column per part
  and is written `part1|part2`, with `\|` and `\\` for a `|` or `\` inside a part. `key.pattern` is
  optional between two tables, but keys of a value class (`class: identifier`) need one that fits them
  only to get an evidence pack: without it the spec is warned `key_pattern_empty`, and no pack is issued
  for its runs ([PACK.md](PACK.md) §2.3).
- **The rest** (normalisers, `compare`, `knownDiffs`, `recheck`, `execution`, `security`) applies to
  the canonical row as to a service's document. `execution.timeouts.total` bounds each per-key query.
- **Not supported yet** (`[unsupported]`): SQL templates, `session`, `collation`, a key column's
  `part`, a `lookup` key map, `key.normalize`, and between two tables a `compare.valueFilter` that
  reads `side`. `compare.strategy` is ignored with a warning: two tables are always scanned by row
  hash, then read key by key.

**Row filters.** `table.where` narrows one side's population, for example to the accounts a
migration moved: `where: "status <> 'closed' AND country IN ('GB', 'IE')"`.

- The language is small ([TABLES.md](TABLES.md) §9): a column on the left and literals on the right
  (`=`, `<>`, `!=`, `<`, `<=`, `>`, `>=`, `IS [NOT] NULL`, `[NOT] IN (...)`, `[NOT] BETWEEN ... AND
  ...`, `[NOT] LIKE` or `ILIKE` a string, or a bare boolean column), joined by `AND`, `OR`, `NOT` and
  parentheses. There are no functions, casts, subqueries or column-to-column comparisons: compare a
  view for those.
- Literals are sent as text and the database reads them as the column's type, so `balance > 10`
  compares numbers. Creating the run runs the filter once, so a literal the column refuses stops the
  run there.
- The scan reads only the rows the filter selects: `rows_a` and `rows_b` count those, and a row
  outside the filter is neither compared nor counted.
- A per-key read does not exclude a row outside the filter: it records whether the row is inside it
  (of two rows with the key, it reads the one the filter selects). When both sides have
  the row and only one side's filter selects it, the key gets a `FILTER_MEMBERSHIP` finding (MEDIUM,
  path `$`, values `false` and `true`) besides its value differences: the key left one side's
  population, so it is a MISMATCH, never NOT_FOUND. A key without a row is still NOT_FOUND. On a
  recheck, A entering or leaving its filter is DRIFT `a_changed` even when its values are unchanged;
  B doing so is `b_converged`, or `intermittent_b` while something still differs.
- coverage.csv (`filter_a`, `filter_b`) and the PDF's "Tables" line show each filter as the spec
  writes it. The Run screen shows it as the plan resolved it: column names quoted, keywords in upper
  case.

**Key maps.** When the sides write the key differently, a key column maps its values to the canonical
key, the one the run stores and shows. `ORG-000123` on side a against the number `123` on side b:

```yaml
key: {name: entity_code, type: string, class: business_code}
sides:
  a: {role: legacy, target: crm, table: {name: entities, keyColumns: [entity_code]}}   # ORG-000123
  b:
    role: new
    target: platform
    table:
      name: organisations
      keyColumns: [{name: id, keyMap: {kind: prefix_pad, prefix: ORG-, width: 6, sideType: integer}}]
```

- `prefix_pad`: the canonical key is the prefix and the digits padded to `width`; a longer number
  keeps its digits (`1234567` is `ORG-1234567`). With `sideType: integer` the side stores the number
  (`123`). With `sideType: string` a text column may store the digits padded or not (`000123`,
  `0123`, `123`); a key stored two ways selects two rows and is INCONCLUSIVE `identity_ambiguous`.
- `regex`: `{toCanonical: {pattern, replacement}, toSide: {pattern, replacement}, sideType}`, each
  pattern matching the whole value, with `$1` to `$9` in the replacement (`&` and `\` are
  literal). `toCanonical` runs in PostgreSQL and `toSide` in Python, so keep to their common syntax:
  groups, classes, quantifiers and `\d \w \s`, but no named groups.
- `identity` is the default. A `lookup` map (a crosswalk table) and a key column's `part` are not
  supported.
- A side value its map does not match (a negative number for `prefix_pad`) has no canonical key: it
  counts as not compared (`rows_skipped_a`, `rows_skipped_b`). A canonical key that a side cannot map
  back (`ORG-X1`) cannot be there: its read is an authoritative absence, without a query.

**Type maps.** When the sides hold one value in different types, `sides.<side>.typeMap` converts a
compared column, named as that side spells it, to a canonical type before the comparison, for the
scan's row hashes and the per-key reads alike ([TABLES.md](TABLES.md) §10 lists the types and their
options). Keys pair through key maps; type maps apply to compared columns only. **Map the column on
both sides:** a side without a map keeps its database's rendering (`2020-01-02T00:37:00+00:00`
against `2020-01-02T00:37:00.000000Z`), and every row differs.

```yaml
sides:
  a:
    typeMap:
      created_at: {as: timestamp, naiveTimezone: Europe/London}     # timestamp without time zone
      active: {as: boolean, trueValues: ["Y"], falseValues: ["N"]}  # a legacy char(1) flag
      balance: {as: decimal, scale: 2}                              # numeric(14, 4)
  b:
    typeMap:
      created_at: {as: timestamp}                                   # timestamptz
      active: {as: boolean}
      balance: {as: decimal, scale: 2}                              # numeric(12, 2)
```

- `timestamp` gives UTC text with 6 fractional digits (`precision` truncates them). A naive value is
  read in `naiveTimezone` (UTC by default); in the hour a clock change repeats, it is read as the
  first of the two.
- `trueValues` and `falseValues` compare exact text (`y` is not `Y`); without them, PostgreSQL's
  boolean words count, in any case.
- Numbers compare by value in any case, so `11.2500` against `11.25` is MATCH; without a map it is
  read per key first, while `{as: decimal}` on both sides lets the scan count it identical. `scale`
  also rounds, half to even: use it only where the business agrees the rounded value is the value.
- A value that does not convert makes the key INCONCLUSIVE `unparseable`, with a detail such as
  `typeMap: column 'balance' does not convert to decimal`: the column and the type, never the value.

Validate the spec against the targets: `uv run ds spec validate spec.yaml --targets targets.yaml`.

**3. Run a batch: the scan.** A batch run over two tables, with no keys and no `key.source`, scans
both tables:

```bash
uv run ds run spec.yaml            # prints the run id, then the status and counters when it ends
```

In the UI, start a batch run with no keys; through the API, `POST /v1/runs` with
`{"spec_id": 7, "mode": "batch"}`.

- Creating the run connects to both databases, resolves the columns and has each database check the
  side's filter and key maps, reading no row. A wrong DSN, a missing table or column, a table the role may not read, no
  column to compare, a column without a counterpart, or a filter or key map the database refuses
  therefore refuses the run at once, in one line (see the table below).
- Each side then streams its keys, and a hash of each canonical row, in key order from one consistent
  snapshot. An identical key is counted and never read again. Every other key (a changed row, a key on
  one side only, a key repeated on a side) is read per key through the engine while the scan goes on,
  and reported like any key of a service run.
- **Per-key reads come in groups** of up to 200 keys, one query per side ([TABLES.md](TABLES.md) §12,
  with measurements); of up to 4 where a MySQL or Oracle side compares a column that may hold a large
  value (a `TEXT`, `BLOB`, `CLOB` or `JSON` type), whose rows are held together while they are judged. A group holds one `execution.concurrency` lane and costs one `rate_per_s` token
  per side for its first reads (one more for its recheck keys, and one per solo re-read);
  `execution.delay` applies after each group.
- **A scanned key is confirmed by its per-key read:** the scan read it first, so the result is final,
  confirmation `rechecked`, with no recheck pass. A key that stopped differing in between reads as it
  is now.
- The scan checkpoints every 10,000 keys. After a worker restart, a pause and resume, or a lost
  database connection (the worker logs `scan of run R-... paused` and tries again after
  `worker.lease_seconds`), it resumes after the last checkpoint and counts nothing twice.
- `execution.stop.maxKeys` caps the keys to compare: the scan stops there, the run says
  `stop_condition:maxKeys` and still reads the keys found.
- **How long it takes** ([BENCHMARK.md](BENCHMARK.md) Results: one Mac, Apple M3 Max, both databases and
  the store on one stock PostgreSQL 16 in Docker, synthetic 4-column rows, warm cache, worker poll 0.05 s;
  warm medians with their ranges): with 101 differing keys the scan read 145,144 (144,659–145,628) rows a
  side a second (10M rows a side in 68.9 s (68.7–69.1), 36.6 s (36.6–36.7) of it the two sorts). With 1% of
  keys differing, each then costs a per-key read: 627 (558–696) and 670 (546–794) a second at 1M rows a side
  (both flagged noisy), 749 (747–752) and 752 (752–753) at 10M. 10M rows a side with every 100th key
  differing took 211.9 s (211.4–212.3), 41.2 s (41.1–41.3) of it the cycle end, and 1,239 MB
  (1,239–1,239) of peak worker memory. Leave each database about 63 bytes a row of temporary space for a
  `bigint` key ([DEPLOY.md](DEPLOY.md), section 7).
- `execution.stop.until` and `maxDuration` are checked at each checkpoint: when one is reached, the
  scan stops, the keys not yet read are cancelled, and the run ends `completed_with_issues` with
  `stop_condition:until` or `stop_condition:maxDuration`.

**Progress and reports.** While the scan runs and after it, the Run screen shows one line: the rows
read on each side, the identical keys, the keys to compare, the rows not compared, the columns only on
one side and the filters. `GET /v1/runs/{id}` returns the same counters as `scan`, and the columns
compared as `params.tables`. The reports are those of any run; `coverage.csv` adds `rows_a`,
`rows_b`, `rows_skipped_a`, `rows_skipped_b`, `columns_only_a`, `columns_only_b`, `filter_a`,
`filter_b`, `engine_a` and `engine_b` (each table side's engine), and `report.pdf` a "Tables" line.

**4. Compare one key, or chosen keys.** Explicit keys, a `key.source`, a one-shot compare and a pair
of a table and a service read per key and never scan; their discrepant keys are rechecked as in a
service run:

```bash
uv run ds compare 100005 --spec spec.yaml   # or "compare now" on the Key screen, or POST /v1/compare
uv run ds run spec.yaml --keys keys.txt     # or POST /v1/runs with "keys": [...]
```

A per-key read selects the row whose key text is the key (`01` does not read the integer 1, whose
text is `1`). No row is an authoritative absence, two rows are INCONCLUSIVE `identity_ambiguous`
unless the side's filter selects exactly one of them, and a key the column's type or the database's encoding cannot hold (`abc` for
an integer column, `€` in a LATIN1 database) is INCONCLUSIVE `skipped`. Failed reads are not
retried, except inside a group, where a key that timed out is read once more on its own and a group
on a session the server ended is read once more on a new one: retry.csv lists the keys still
failed.

**5. Re-diff on a schedule: a soak over the scan.** A soak over two tables, with no keys and no
`key.source`, scans both anew every cycle:

```bash
uv run ds run spec.yaml --soak     # or POST /v1/runs with "mode": "soak"; needs execution.cycleInterval
```

- Cycles start every `execution.cycleInterval` from the run's start; a cycle that ends late is
  followed at once by the next. `--rolling` changes nothing here. `execution.stop.maxCycles`, `until`
  and `maxDuration` end the soak at a cycle boundary, as for any soak.
- Each cycle has its own scan and counters: pick the cycle on the Run screen, or pass
  `ds report --cycle`.
- The keys that differed last cycle are read again even when identical now, so a fixed row shows
  as an outcome change, for example MISMATCH to MATCH, on the Run screen, in
  `GET /v1/runs/{id}/changes` and in the PDF.
- A key identical last cycle and different now had no result last cycle. It is a discrepancy of this
  cycle, not an outcome change: find it in this cycle's discrepancies.
- The Run screen's scan and graph lines are the current cycle's (labelled so when an older cycle is
  picked); an older cycle's scan counts are in its reports.

**Counters.** Identical keys have no result rows, yet they count: in `total`, `compared` and `match`,
as outcome MATCH with confirmation `bulk_exhaustive`, so the counters reconcile as for any run. The
identical keys and the keys to compare are counted per key, however many rows a key has; `rows_a`
and `rows_b` count every row the scan reads on a side (within its filter). Rows the scan cannot
compare (a NULL key part, a key its key map does not match, an empty key, or a key over 1024 bytes)
are counted per side as not compared (`rows_skipped_a`, `rows_skipped_b`); they are included in
`rows_a` and `rows_b`, but in no run counter (`total`, `match`, ...).

| Message or symptom | What to do |
|---|---|
| `side a: cannot connect to target crm-legacy: ...` when the run is created | libpq's line names the host and user, never the password. Fix the DSN file, the network path or the role, then start the run again. |
| `the DSN of target crm-legacy is not a valid connection string` | The DSN file holds neither a libpq keyword string nor a `postgresql://` URI, or the URI's user or password holds an unencoded `@` (write it `%40`). |
| `side a: table or view crm.customers not found or not visible` | Fix `schema` or `name`; without `schema`, the role's `search_path` must find the name. Or the role lacks USAGE on the schema: grant it. |
| `side a: the role may not read crm.customers: grant it SELECT` (or `... read column 'x' of ...`), or a run `failed` with `InsufficientPrivilege` (a grant revoked after the run started) | Grant the role SELECT on the table or view, or on that column ([DEPLOY.md](DEPLOY.md), section 7). |
| `side a: column 'mobile' is not in crm.customers` | A key column, a listed column, a filter's column or a type map's column is misspelled. |
| `side a: column 'email' has no counterpart on side b` | List the column on both sides, rename it with `as`, or drop it. |
| `side a: no column to compare: list the columns to compare; a key column listed alone compares which keys exist` | The sides share no column besides the key (or the table has none). List the columns to compare, renaming with `as`; to compare only which keys exist, list the key column alone. |
| `side a: column 'amount' cannot be matched: LIKE and ILIKE match text in a deterministic collation ...` | LIKE and ILIKE need a text column, and before PostgreSQL 18 one whose collation is deterministic: compare a view that casts it. |
| `/sides/a/table/where [invalid_where] ...`, for example `unexpected '(' at position 5` | The filter is outside the language: no functions, casts, subqueries or column-to-column comparisons. Compare a view instead. |
| `side a: the database refuses the filter or key map: ...` when the run is created | The database rejected a literal (`invalid input syntax for type integer: "abc"`), a bare column that is not boolean (`argument of AND must be type boolean`), or a key map pattern (`invalid regular expression: ...`, such as a named group). Fix the spec. |
| `[invalid_key_map] invalid regular expression: ...` | Fix the pattern; PostgreSQL must accept it too. |
| `[invalid_key_map] the replacement names a group the pattern lacks` or `a replacement names groups $1 to $9 only` | Name only groups the pattern has, from `$1` to `$9`. |
| `[invalid_type_map] 'Europe/Londn' is not an IANA time zone` | Name a zone of the IANA database, such as `Europe/London`. |
| `side a: typeMap column 'branch_code' is not compared` | A type map names a column the pair does not compare (not listed, or on one side only). Map compared columns only, by their name on that side. |
| Every row MISMATCH on one column, or no identical keys at all | A type map converts the column on one side only: map it on both. |
| INCONCLUSIVE `unparseable`, detail `typeMap: column 'x' does not convert to integer` (retry.csv) | A value is not of the declared type: fix the type map, or the data. |
| `FILTER_MEMBERSHIP` findings | The row exists on both sides, inside one side's filter and outside the other's: often a status the two systems treat differently. |
| Rows not compared (`rows_skipped_*`, "not compared" on the Run screen) | Rows with a NULL key part, a key its key map does not match, an empty key or a key over 1024 bytes. Fix the data, or compare them out of band. |
| INCONCLUSIVE `identity_ambiguous` | The key selects two rows on a side, both inside its filter or both outside it, for example in a view without a unique key. |
| INCONCLUSIVE `error_a`, `error_b`, `both_error` or `timeout` (detail `fetch failed: connect_error` or `fetch failed: timeout`) | The database was unreachable or slow: run the keys of retry.csv again, or raise `execution.timeouts.total`. |

### Across engines

A table side may be a MySQL or Oracle table or view, paired with a PostgreSQL table, with the other
engine, with its own or with a service ([ENGINES.md](ENGINES.md) is the contract, [TABLES.md](TABLES.md)
§13 its summary). The spec is the one above. The target declares its endpoint instead of a DSN, and its
prerequisites, grants, TLS modes and firewall rows are in [DEPLOY.md](DEPLOY.md), section 7:

```yaml
targets:
  crm-legacy:
    engine: oracle                      # or mysql, with `database:` in place of `service:`
    host: crm-db.internal
    service: CRMPDB
    user: ds_reader
    password_ref: file:/run/secrets/crm_legacy_password
    tls_ca_ref: file:/run/secrets/crm_db_ca
    tls_server_dn: "CN=crm-db.internal,O=Bank"
    allow_private_network: true
```

**What differs from two PostgreSQL tables.**

- Names resolve as spelled, else in any case on MySQL and upper-cased on Oracle; canonical names stay
  lower-cased, so Oracle `EMAIL` pairs with PostgreSQL `email`. Oracle follows one synonym.
- Keys pair by their text, which every engine renders as PostgreSQL does. A number key needs one scale
  on both sides, a date key does not pair with a timestamp key (an Oracle `DATE` or a MySQL `DATETIME`
  holds a time), and a float, a timestamp with fractional seconds, a large type and an Oracle
  `NVARCHAR2` cannot be a key: compare a view that casts it.
- **Nothing is coerced silently.** Two columns of different kinds refuse the run and name the type map
  that bridges them:

  | Situation | What to declare |
  |---|---|
  | Oracle NULL against an empty string elsewhere (reported as a difference) | `typeMap: {col: {as: string, emptyAsNull: true}}` on the other side |
  | An Oracle `DATE`, which holds a time, against a date | `{as: date}` on the Oracle side (drops the time), or `{as: timestamp}` on both |
  | `TINYINT(1)` or `NUMBER(1)` against a boolean | `{as: boolean}` on the number side |
  | A timestamp without a zone against one with a zone | `{as: timestamp, naiveTimezone: ...}` on both |
  | Nanoseconds against microseconds | `{as: timestamp}` on both (6 digits kept) |
  | MySQL zero dates, `BINARY` padding, a `uuid` against `RAW(16)` | a view |

- Graph parity, `regex` key maps and `ILIKE` are refused on a MySQL or Oracle side when the spec is
  loaded. A filter's literal must be of its column's kind (a number for a number column), and is
  refused when the run is created otherwise.
- Between two tables of one engine the server hashes the columns both sides declare alike; across
  engines the worker hashes every compared column, which is slower ([TABLES.md](TABLES.md) §13.9 has the
  measured times) and loads the worker: run `ds serve --no-worker` beside `ds worker` for such scans
  ([DEPLOY.md](DEPLOY.md), section 7). The API process then has a budget and sessions of its own for
  one-key compares, key listings and run creation: size each target's `rate_per_s`, `max_concurrency`
  and the account's session limit (`max_user_connections`, `SESSIONS_PER_USER`) for both processes.
- A MySQL view is read in one transaction whose snapshot cannot be observed, and a MySQL table outside
  InnoDB in no snapshot: the Run screen shows `Side A: mysql 8.4 (unverified)`, and the census and the
  certificate warn `SIDE A NOT READ IN A SNAPSHOT`. Every MySQL or Oracle side's census says `rows and
  values as the account reads them (redaction, masking and row policies apply)`.

**The first run on a new target,** smallest first:

1. `uv run ds target check crm-legacy --table APP.CUSTOMERS` for each side: the address, TLS, the
   session and the grants, the server's version, the notes this release has for it, and each column's
   declared type and class. It reads no row.
2. One key at a time: `uv run ds compare KEY --spec spec.yaml`, for a key you know matches and one you
   know differs.
3. A batch capped by `execution.stop.maxKeys` (say 1,000): the scan stops there and the keys found are
   read.
4. Then the full scan.

**The acceptance kit.** This release is observed on MySQL 8.4.11 and Oracle 23.26.3 only. On another
version (MySQL 8.0, Oracle 19c), have the database's DBA run the kit on a server of that version before
any real table: `examples/tables/mysql/` or `examples/tables/oracle/` holds seed SQL, a target, a spec and
the expected result, and its README the steps. It compares two tables of one database with one column of
each type, keys the server's own collation would order otherwise, and seeded differences; the counters
and the differing keys must be exactly `expected.json`'s. Anything else is a finding about that version:
send it to the maintainer before relying on a run there.

**Before a downgrade** to a release without these engines: finish or cancel every run with a MySQL or
Oracle side, and remove the `engine` targets from the targets file. The older release does not start
while one is declared: its targets schema refuses the member.

| Message or symptom | What to do |
|---|---|
| `side a: cannot connect to target crm-legacy (oracle crm-db.internal:1521/CRMPDB as ds_reader): ORA-01017: the credential was refused` | The code and its gloss say what failed: the network path, the name, the service, the credential or TLS ([DEPLOY.md](DEPLOY.md), section 11). `ds target check` repeats the check without a run. |
| `side a: target ledger is MariaDB 11.4: MySQL 8.0.17 or later is supported, MariaDB is not`; `side a: target crm-legacy is Oracle 12.2: 19c or later is supported` | The server is not one DeltaSleuth reads. |
| `side a: key column 'RATE' (BINARY_DOUBLE) cannot be a key: compare a view that casts it` | The column's key text is not one every engine renders alike. |
| `key column 'amount' is NUMBER(10,2) on side a and numeric(12,4) on side b: a number key needs one scale on both sides (compare a view that casts it)` | `1` never pairs with `1.00`: give both sides one scale in a view. |
| `key column 'day' is date on side a and DATE on side b: the key text of a date is never that of a timestamp (compare a view that casts it)` | `2024-01-01` never pairs with `2024-01-01 00:00:00`: cast one side in a view (on Oracle `TO_CHAR(day, 'YYYY-MM-DD')`), so both keys read alike. |
| `side a: column 'GEOM' (SDO_GEOMETRY) cannot be compared: compare a view that casts it` | No plan reads the type; leave the column out of `columns`, or cast it in a view. |
| `side a: column 'opened' is a timestamp (DATE) and side b's a date: declare typeMap {OPENED: {as: date}} on side a, or {as: timestamp} on both` | Declare the type map it names. |
| `side a: the filter compares column 'amount' (NUMBER(10,2)) with a literal that is not a number` | Fix the filter's literal. |
| `side a: column 'secret' is not in ledger.t, or the role may not read it: grant it SELECT` | MySQL lists only the columns the account is granted. |
| `side a: the role may not read APP.T: grant it READ` | Grant the Oracle account `READ` on the table or view. |
| `side a: table or view APP.BROKEN_V cannot be described: ORA-04063` | The database answered with an error while the table or view was described: an Oracle view left invalid by a change to its table, or a session the server ended. Only the code is shown: look it up with the database's owners ([DEPLOY.md](DEPLOY.md), section 11). |
| `/sides/a/target [target_kind] this side needs a database target (dsn_ref or engine)`; `not supported: graph parity with a MySQL or Oracle side; export the link facts into PostgreSQL`; `not supported: a regex key map on a MySQL or Oracle side; compare a view that derives the key`; `not supported: ILIKE on a MySQL or Oracle side; use LIKE` | The spec asks a MySQL or Oracle side for what it does not do. |
| INCONCLUSIVE `unparseable`, detail `column 'opened' does not read as timestamp` (retry.csv) | The server rendered a value that is not of its column's class: the detail names the column and the class, never the value. Report it to the maintainer with the column's declared type. |
| INCONCLUSIVE `resource_limit` | The row's large values (LOBs) total more than 16 MiB: it is never fetched. Compare it out of band. |
| The worker logs `scan of run R-... paused: ORA-03113` | The server was unreachable: the scan resumes from its last checkpoint after `worker.lease_seconds`, without limit. |
| A run `failed` with `SnapshotLost: ORA-01555`, `TargetTempSpace: ORA-01652`, `TargetDenied: ORA-00942`, `TargetRefused: <code>` or `KeysOutOfOrder` | [DEPLOY.md](DEPLOY.md), section 11, for the first four. `KeysOutOfOrder`: a side returned its keys out of code-point order, which DeltaSleuth refuses rather than mis-pair a key: report it to the maintainer with the server's version and character set. |
| A run paused with `the frame of side a could not be held in one snapshot (ORA-01555)` | A sampling frame lost its snapshot: resume the run when the database can hold it. |

### From files

A table side may be CSV or Parquet files your export job writes into a directory on the host, paired
with a database, a service or another export ([FILES.md](FILES.md) is the contract, [TABLES.md](TABLES.md)
§14 its summary; the mount, the target and the settings are in [DEPLOY.md](DEPLOY.md), section 7,
Files). This is not Route A's captured samples (paired responses of services, replayed in file mode):
a file side is a table, scanned and read key by key like one. DeltaSleuth compares **the rows of these
files, named by their digest**: it does not know the system they were exported from, whether the export
is complete, or when it was taken (it prints what the stamp file declares).

1. **Draft the dialect, then declare it.** `ds target check legacy-exports --table
   'customers/current/*.csv' --format csv --header --count` (the path quoted: the shell must not expand
   it) lists the files, drafts `csv:` and `typeMap:` from the first file and counts the rows and the
   lines that are not rows. Copy the draft into the
   spec and correct it: the run reads only what the approved spec declares, and never sniffs. The draft's
   lines are printed whole, however many columns; an export that escapes a quote with a backslash is
   drafted with `escape: "\\"`; an export with a trailer record is drafted from the lines that are rows
   (`note: drafted from the lines of file 1 that read as rows: --count lists the others`), and `--count`
   lists the trailer by position and kind. `--count` of a `.gz` or `.zst` export, and `--manifest`, read
   each compressed stream to its end as a run's creation does: an export cut short in transfer is
   refused (`BadGzipFile`, `ZstdError`), not counted short.

   ```yaml
   sides:
     a:
       role: legacy
       target: legacy-exports
       table:
         name: customers                       # the label reports show
         keyColumns: [customer_id]
         file:
           path: customers/current/*.csv       # relative to the target's root
           format: csv
           asOfFile: _AS_OF                    # the export's stamp file, written last
           maxAge: 26h                         # an export older than this did not arrive
           csv:
             columns: [customer_id, email, balance, opened, status]
             keys: {customer_id: {as: integer}}
             delimiter: "|"
             header: true
       typeMap:
         balance: {as: decimal, decimalSeparator: ","}
         opened: {as: date, format: "%d/%m/%Y"}
   ```

   A Parquet side is `file: {path: customers/current/, format: parquet}`: its columns carry their types.
2. **Types.** Every CSV field is text. Against a database's number or date, or a Parquet column, the run
   is refused until the spec's `typeMap` says how the text reads (`{as: decimal}`, `{as: date, format:
   "%d/%m/%Y"}`, a decimal comma with `decimalSeparator: ","`); the refusal names the entry. A key
   column's type is declared in `csv.keys`.
3. **Leading zeros.** As text `00123` is not `123`. `csv.keys {customer_id: {as: integer}}` reads both
   as the key 123; leave the key as text where the zeros are part of it, and then the other side's key
   must be text too.
4. **Dates.** `format` takes `%Y %m %d %H %M %S %f %z` and the characters `- / . : T` and space. A
   two-digit year or a month's name is refused: ask for another export format.
5. **Lines that are not rows.** A line with another field count, an unterminated quote, invalid encoding
   or over 2 MiB is counted when the run is created, never dropped silently, and never compared. While a
   side has any, a key not found on it is INCONCLUSIVE, not NOT_FOUND. **On side a they cost the pack:**
   no evidence pack is issued and readiness's population gate is not met, because a key on such a line
   is in no result. Fix the export (a trailer record of another shape must be stripped) before a run
   meant for sign-off. Over 1,000 such lines refuse the run: the dialect does not fit.
6. **Duplicates.** A file promises no unique key. A key several rows hold is INCONCLUSIVE
   (`identity_ambiguous`), counted on the Run screen (`5 repeated keys`) and warned of on the
   certificate; nothing is de-duplicated.
7. **One run, one export.** The files are pinned when the run is created; any change fails the run
   (`FilesChanged`). For a nightly export use a **schedule** per export, its cron after the export's
   latest arrival ([Schedules](#schedules)), not a soak: a soak's later cycle fails once the files are
   replaced. A soak is useful against a live other side while the export stays as it is.
8. **Per-key work.** Each group of up to 200 keys is one pass over the files: fast on Parquet (the
   compared columns of every row group are read, whatever the key's type and the file's order: 0.3 s for
   10M rows of four columns on one laptop), a full read on CSV. A large CSV with many differences takes hours; a run whose pass takes
   over half the spec's `execution.timeouts.total` is refused with the timeout to set. A CSV key declared
   by `csv.keys` as an integer or a decimal costs each pass more than the laptop's figures say: in the
   Linux image a pass over 10M such rows took 64 s where the laptop took 10 s ([DEPLOY.md](DEPLOY.md),
   section 7, Files, point 5). Create a first run on your own host and read the pass's seconds there
   (`pass_s` of the side in the run's `params.tables`, or the refusal's text) before you set a spec's
   timeout or promise a duration; `ds target check --count` builds no key and shows a shorter pass.
9. **Which bytes a pack is about.** `--manifest` prints a `sha256sum` line for each file and
   `files_sha256`, the digest on the certificate. That digest is the SHA-256 of the list of the files'
   SHA-256 values, never a file's own: `sha256sum` of a single exported file gives another value than its
   pack, whose certificate and subject say how the digest is formed. The export's owner checks them where
   the files are, with the manifest kept outside the export directory (a file written into it would
   change the export a run reads):

   ```bash
   ds target check legacy-exports --table 'customers/current/*.csv' --format csv --manifest \
     | grep -E '^[0-9a-f]{64}  ' > /tmp/manifest.txt
   (cd /srv/exports/legacy && sha256sum -c /tmp/manifest.txt)   # each file is the one DeltaSleuth hashed
   cut -c1-64 /tmp/manifest.txt | sha256sum                     # files_sha256, as the certificate prints it
   sha256sum < /tmp/manifest.txt                                # the same of a side that declares partitions
   ```

   A side that declares `partitions` compares its directories' names, so its digest binds each file's
   path too: it is the SHA-256 of the lines whole, which the check prints as a second `files_sha256` line.
   Without the manifest, the certificate's own command gives the same from the files in the byte order of
   their paths: `sha256sum FILE... | cut -c1-64 | sha256sum`.

The acceptance kit `examples/tables/files/` is a generator, a target, a spec and the expected result:
run it first, then a small export of your own writer. Refused on a file side: graph parity, `regex` key
maps, `ILIKE`, `table.schema` and table pre-checks.

| Message or symptom | What to do |
|---|---|
| A run `failed` with `TargetTempSpace: OutOfMemoryException` or `TargetTempSpace: spill` | A sort met the file reader's budget or the spill volume was short. Free or grow the volume under `files.spill_dir`, or raise `files.spill` or `files.memory` and restart ([DEPLOY.md](DEPLOY.md), section 7, Files); then create the run again: nothing of the failed run is reused. |
| The worker logs `scan of run R-... paused: OSError`, `paused: Deadline` or `paused: Stuck`, or `file read abandoned after its deadline (3 waiting)`; a new run is refused with `the file reader takes no work of target legacy-exports: 4 reads of its mount, which does not answer, are still waiting` | The export's mount does not answer (a dead NFS or SMB mount). Runs of other targets go on, those of other files targets too: the four hung reads refuse that target's file work alone, and a scan of another files target that is under way reads on to its end. A statement that hangs on the dead mount is given up together with the file reader's process it ran in, which is retired, and a new reader is started for the next read. That costs memory, not work: until the passes in flight in the retired reader end, both stand, each with its own `files.memory` (two budgets where [DEPLOY.md](DEPLOY.md) sizes the container for one: watch the container's memory if a large scan is running); then the retired one is killed, at once where the hung statement was alone in it, and its memory and spill directory go with it. No reader is left behind once the mount answers; while it does not, a killed reader may stay in the kernel (`the file reader's process 4242 did not end within 10 s of being killed: its reads stay counted`). Repair the mount: paused scans resume by themselves, and the reader takes the target's work again once the hung reads return. A restart of the service frees the threads, but the target's runs still running hang four more within minutes: while the mount stays dead, pause or cancel its runs and pause its schedules. With a second mount dead (8 hung reads in all) the reader refuses every files target, `the file reader takes no work: 8 reads of mounts that do not answer are still waiting`, until one mount answers or the service restarts. |
| The log, or a `ds` command's terminal, shows `the file reader started: duckdb 1.5.5 in process 4242, data limit 5,296 MiB, sockets denied` at warning level | Nothing is wrong: the start of a process's reader is logged so (a reader apart, started for a charged or suspect file set, says so only where a limit did not hold), as a warning because that is the lowest level a `ds` process prints for this log, and the line says what that start saw hold. Act only where it says `no data limit (not held on this host)` or `sockets not denied (not held on this host)` on a Linux host whose settings leave `files.os_limits` on: run the reader's self-test and report it ([DEPLOY.md](DEPLOY.md), section 5, item 25). `(files.os_limits is false)` and, on macOS, `(not on this platform)` say what was asked for. |
| The worker logs `scan of run R-... paused: Empty` | The target's directory holds no entry at all: the export is not mounted in the container (the service started before the mount, or the mount dropped). Mount it: the run goes on by itself. A new run meanwhile is refused with `no file matches ...`. |
| The worker logs `scan of run R-... paused: Died`; a new run is refused with `the file reader did not take its files' work (Died): try again` | The file reader's process ended while it read: DuckDB runs in a child process of each `ds` process, and a fault or a memory blow-up there ends that child alone. The log line before it names the cause (`the file reader's process ended (SIGKILL) ...`). Nothing needs doing: a new reader is started, a paused scan sorts its side again from its checkpoint, a per-key group is read once more, a refused request is sent again. Files that were being read when it ended are read in a reader of their own next, so that another end can be laid to them alone; the third such end fails their run (`ReaderDied`, below). |
| The worker logs `scan of run R-... paused: Unstarted`; a new run is refused with `the file reader did not start: its process did not start or did not answer (see the server log)` | The reader's process did not start. The log says how: `its process could not be started (PermissionError)`, `ended as it started (exit 1)`, `did not answer within 20 s`, `was not ready within 50 s`, `answered outside the protocol`. Reads resume when it starts. If it never does on this host, run the reader's self-test ([DEPLOY.md](DEPLOY.md), section 5, item 25) and, where the reader's own limits are what stops it there, set `files.os_limits: false`. |
| The worker logs `scan of run R-... paused: Alone`; a new run is refused with `... (Alone): try again` | Another export is being read apart first (after a reader ended on it), one such read at a time; this one waited 30 s for its turn. Nothing needs doing. |
| A run `failed` with `ReaderDied: side a: the file reader's process ended 3 times (SIGSEGV) while only this side's files were being read: they are read no more until DeltaSleuth restarts`; a new run on those files is refused with the same text | Three times the reader's process ended while this side's files alone were being read, and DeltaSleuth reads them no more. The text says what was seen, not that the files are at fault; the cause in brackets is the last end's. `SIGSEGV`, `SIGBUS`, `SIGILL`, `SIGFPE`: a fault in DuckDB on these bytes: export the files again, and report the file with the DuckDB version; or, on Linux, a statement that met the reader's data limit, which DuckDB does not refuse (observed: `SIGSEGV`). To tell the two, read the files once with `files.os_limits: false`: a fault ends the reader again, memory then meets the container's limit instead (`SIGKILL`) or the read completes. `SIGKILL`: the kernel took the reader for memory ([DEPLOY.md](DEPLOY.md), section 7: size the container, or lower `files.memory`), or an operator killed it. `SIGABRT` or `exit N`: DuckDB's own abort, or an error of the reader's own code (the log names its class and line): report it. `reply`: the reader answered outside its protocol: report it. `killed after its deadline`: a statement that did not end on a mount that answers: report the file. In each case restart the `ds` process (the service, or the worker) to read those files again. |
| The worker logs `scan of run R-... paused: Busy` (or `Dropped`); a new run is refused with `the file reader did not take its files' work (Busy): try again`, or `the file reader plans 8 runs at once, and this one waited ... for its turn` | Every thread of the file reader was busy for 30 s (many large exports hashed or read at once). `Dropped`: the reader's DuckDB instance was discarded as the read asked for it (another read hung past its deadline, see the row above), or DuckDB met an internal error on some file, which discards the instance and fails the later statements of the sessions that still held it. No mount is at fault and nothing needs repair: scans resume (a paused scan sorts its side again from its checkpoint), a schedule's firing tries again, a refused request is sent again. `Dropped` again and again, each time a run of one export is created or read, points at that export's file: its own run is refused or fails with `InternalException` in the message (`its files could not be read while the run was planned (InternalException)`, `TargetRefused: InternalException`), so check that file with its writer. Stagger the creation of runs on large exports. |
| A run `failed` with `FilesChanged: ...`, `RowsMoved: ...` or `TargetRefused: <class>` | [DEPLOY.md](DEPLOY.md), section 11. A statement that fails three passes in a row on unchanged files (`TargetRefused`) is a damaged file: export it again. |
| `side a: its files hold line ends inside quoted fields, and whether a file ends inside one could not be checked` | A CSV whose quoted fields hold line ends could not have its last record checked. Export without line ends inside fields, or as Parquet. |

**Before a downgrade** to a release without file sides: finish or cancel every run with a file side and
remove the `engine: files` targets from the targets file. The older release does not start while one is
declared: its targets schema refuses the member.

## Triage with table pre-checks

Before any per-key comparison, a table pre-check says where to look first across many table pairs
([PRECHECKS.md](PRECHECKS.md)). It reads each side's catalogue and a few aggregate statements inside one
snapshot of its engine, and compares the figures: it never fetches a row, a key or a text. The Pre-checks
screen, `ds precheck` and every export start with what it is not, and each API answer but a download carries
it (`not`; a draft, a starter spec, carries none):

> A table pre-check counts and adds up; it does not compare rows. Tables whose figures agree can hold
> different rows, and figures can differ only because two databases store a value differently. It says where
> to look first. It is observed, not evidence, readiness or a sign-off, and no evidence pack contains it: run a
> comparison to compare rows.

A pre-check is no run: it seats no licence pair (a vendor build's count), is not gated by approvals (but for the two rules below),
counts for no readiness gate and is in no pack. It is deleted `retention.standard_days` after it was made.

**Make one.** An operator pre-checks stored specs whose two sides are tables (the form on the Pre-checks
screen, `Pre-check…` on the Specs screen, or `POST /v1/prechecks`); a spec author pre-checks two schemas
paired by name (the second form, or `POST /v1/prechecks/catalogue`). From the command line:

```bash
uv run ds precheck run customers.yaml cards.yaml --level distinct   # files are stored first, as ds run stores them
uv run ds precheck run --spec customer-profile --spec cards          # each name's newest stored version
uv run ds precheck run --all-specs --fail-on incomplete              # every stored spec of two tables, for a pipeline
uv run ds precheck run --a crm-legacy:crm --b platform-users:app     # two schemas, tables paired by name
uv run ds precheck                                                   # the list; show PC-… for one
```

```
A table pre-check counts and adds up; it does not compare rows. […]
PC-…  specs  level distinct  by local:jdoe@ops-host 2026-10-03T01:21:07+00:00  finished
2 pairs: 1 with differing row counts, 1 with no difference observed (not parity)
1  customer-profile  rows differ: side a 900, side b 899 (1 fewer on side b)
2  cards  no difference observed in the checks made (level distinct): not parity; run a comparison to compare rows
```

The worker reads the pairs (`ds serve`, or `ds worker`; `ds precheck run` reads them beside itself unless
`--no-wait`), at most `precheck.sessions_per_target` sessions a target at once. At most 500 pairs a
pre-check and 20 unfinished pre-checks an installation. `--fail-on` exits 1 when the pre-check was
cancelled or a pair's verdict is, with `refused`, `refused` or `unpaired`; with `differs`, `values differ` or
any worse one (the first seven of the table below); with `incomplete`, `incomplete` or worse. A pair that
was not read fails `differs` too, since a control must not pass on no data.

**Levels**, each including the one before:

| Level | Reads, a side | Figures |
|---|---|---|
| `schema` | the catalogue, and a spec pair's zero-row probe | names, declared types, classes, sizes, nullability, primary keys, each key column's two sides as a column's; whether a run would be refused |
| `counts` (the two-schema form's default) | one statement over the filtered population | `rows`, and the rows without a key |
| `columns` (the default for stored specs) | the same statement, 150 columns at a time, 200 at most | per compared column: `nulls`; for texts `empty` and `length` (shortest to longest, in code points); for numbers `sum`, `min`, `max`; for dates and times `min`, `max`; for booleans `true`; for bytes `length` in octets |
| `distinct` (stored specs only) | then the repeated keys, and the distinct values 4 columns a statement, 40 at most | `duplicate_keys`, `rows_in_duplicates`, `distinct` per column |

Distinct counts and repeated keys sort or hash: a side over `precheck.distinct_max_rows` rows (20,000,000)
has both skipped, and the pair says so (`duplicate keys and distinct values not counted: …`). Each statement is bounded by `precheck.statement_timeout_s` (900 s). What each level
costs a database is in [DEPLOY.md](DEPLOY.md), section 7.

**Read the list.** Pairs come worst first, by verdict, then by how many figures differ, then by label. The
totals count pairs by verdict, never as a percentage or a score:

| Verdict | It means | Look first at |
|---|---|---|
| `unpaired` | a name on one side only, or two of a side alike ignoring case (two schemas paired by name) | the name, or compare them in a spec |
| `refused` | a run of this spec would be refused: the plan's own message follows | what it names |
| `not read` | a side's read failed with a code (`timeout`, `unreachable`, `auth_refused`, `temp_space`, `snapshot_lost`, `denied`, `error`); what was read before stays | DEPLOY section 11 |
| `rows differ` | the row counts differ | a comparison: which keys |
| `keys null or repeated` | a side holds rows without a key, or keys held by several rows | the key column; a scan skips the first and reads the second `identity_ambiguous` |
| `columns not compared` | a comparison would leave a column out: on one side only, of two families, of a type no plan reads, or named alike ignoring case by another column of its side (two schemas paired by name); or it would find no key of one side on the other, two key columns whose key texts are never equal (a number key of two scales, `numeric(10,2)` reading 1.00 where `integer` reads 1; a date against a timestamp) | list the columns, declare the bridge it names, or compare a view that casts the key |
| `values differ` | a compared figure differs | the column it names |
| `incomplete` | a check of the level asked was skipped by a limit, or by SSO's rule for an unapproved filter | narrow the pair, or have the version approved |
| `schema differs` | declared types, sizes, nullability or keys differ (a key column's class, size or nullability among them), and nothing above | whether the difference matters to the migration |
| `no difference observed` | nothing above: read as `no difference observed in the checks made (not parity)` | a comparison, still |

A pair opens (`ds precheck show PC-… --pair N`, or the pair's screen) with its lines, the refusals, the
schema table and each compared column's figures:

```
rows differ: side a 900, side b 899 (1 fewer on side b)
3 of 4 compared columns differ in a figure: email (distinct), balance (sum, distinct), opened (distinct)
schema: 2 differences: balance is numeric(12,2) on side a and numeric(14,2) on side b; opened is NOT NULL on side a and nullable on side b
side a: postgresql 16.14, crm.customers, in 1 snapshot at 2026-10-03 01:21:07 UTC; side b: postgresql 16.14, app.customers, in 1 snapshot, 0 s later: not read at one moment
  balance: sum: differs (figures withheld: side a declares a filter)
  balance: min: same; max: same (values are never shown)
  email: length: side a 17 to 19, side b 17 to 19 characters (same)
```

**What `same` does not mean.** `same` says the two figures were equal, never what they were, and never that
the rows are. Two tables whose every figure agrees can hold different rows: offsetting differences cancel in
a sum, and two swapped values change no count, length, minimum, maximum or distinct count. A differing figure
is no defect either: normalisers, tolerances, known-difference rules and `compare.include` and `exclude` are
not applied (`not applied: …`, said where the spec has any), a bridge a line names (a `typeMap`, Oracle's `''`
stored as NULL and `emptyAsNull`) is never applied, and the two sides are read seconds apart. A sum is shown as a
number only for a column the spec clears, on sides without a filter, when neither engine can have rounded it
and, under SSO, the version is approved; else it is a verdict with the reason. Minima and maxima are verdicts
only, and under a filter so are a protected column's `true`, `empty` and `distinct` counts and lengths (its
NULL counts and the rows stay numbers). A float, a `typeMap` column and a large-object column get null counts only. Rows and values are as the
account reads them.

**Under SSO.** A version that declares a `where` and lacks the approval it needs is read at level `schema`
only, with no probe, so no unapproved filter reaches a target (`not checked: spec 12 (customer-profile
1.4.0) declares a filter and is not approved: only its schema was read`). Elsewhere, figures under a filter
tell more than a run of the same spec: how many rows it selects (yes or no about any column it names), how
many hold NULL in each compared column, and whether the two sides agree in each other figure, which filters
of one row a side turn into facts about those rows (DEPLOY section 7 lists them). Pre-check a spec with a
filter only where whoever reads the pre-check may learn them. A `regex` key map is no filter and needs no
approval of its own, in any mode: its pattern, a predicate over raw key values, reaches the target in runs
and pre-checks alike, and the rows whose key it does not match are counted (a pre-check's rows without a
key, a run's skipped rows), so review a key map as you would a filter.

**Two schemas paired by name, then a draft.** The two-schema form lists each schema's tables and views the
account may read (one short session a side, while the request waits), pairs them by lower-cased name
(`include` and `exclude` take up to 20 globs over it; a PostgreSQL schema lists a partitioned table once,
never its partitions), and reads each pair by an implied plan: the common primary key, figures for the
common columns of one family (a common column of two families, of a type no plan reads, or named alike
ignoring case by another column of its side, is reported as one a comparison would leave out), no filter
and no type map, every value protected (so no sum is shown). It takes level `schema`, `counts` or `columns`. A name on one side only, or two alike, reads `no table on side b is named ledger (side a:
crm.ledger; paired by name)`. A spec author drafts a starter spec of a pair once both catalogues were read
(`Draft a spec`, `GET /v1/prechecks/PC-…/pairs/N/draft.yaml`, or `ds precheck draft PC-… --pair N --out
DIR`, which writes `PC-…-pair-N.draft.yaml`): both `table` blocks as the catalogues spell them, the common
primary key (else `TODO_key_column`, and above `key` a comment when its key texts never meet), the
comparable columns, `security: {unlisted: tokenize}`, a comment for each column left out with the bridge to
declare, never applied, and `validate_spec`'s issues as header comments. It holds names and types only and is never stored: review it, then upload it as any spec.

**Reports.** `ds precheck report PC-… --out DIR` (or the screen's downloads) writes `precheck.csv` (a row a
pair, `claim` `observed; not parity`), `precheck-columns.csv` (a row a pair, column and figure; a withheld
figure's cells empty) and `precheck.pdf`, each headed by what a pre-check is not; each download is audited.

**A nightly triage.** No schedule kind runs pre-checks yet: the site's scheduler runs `ds precheck run
--all-specs --fail-on incomplete` with the CLI's `DS_OPERATOR`, and reads the pairs that failed it.

## When a table changes under a run

The schema guard keeps a run from reading a table that is no longer what its plan, or its version's last
run, read ([PRECHECKS.md](PRECHECKS.md) §7). It looks at each table side's catalogue (names, declared types,
classes, nullability, the primary key and the relation's kind) when a run is created, at each scan pass
before a cursor opens, at a key-list cycle's start, at a resume and after a refused read. It never says a
schema is unchanged: the Run screen and `GET /v1/runs/{id}` (`schema.lines`) say `no change
observed against the run's plan at its creation and each cycle's start (table sides only; a view's own
tables, constraints and defaults are not read)`, and once a look could not be made, `no change observed
against the run's plan where the schema guard looked (…); it could not look at the start of cycle 7`. A
change reverted between two looks, a redefined view and a change of meaning are not seen. Service sides are
not guarded.

**What it does**, by the spec's `schemaDrift.mode` (`finding` unless the spec says `pause`):

| Change | `finding` | `pause` |
|---|---|---|
| breaks the plan: a planned column gone or unreadable, a column of another class, a type within its class that the engine reads otherwise (Oracle's `DATE` and `TIMESTAMP`, MySQL's `BIT`), a key column whose key text would render otherwise (a number key's scale, an Oracle `NUMBER(p,s)` key's precision), the table gone | the run pauses; a resume is refused while it stands | the same |
| narrows a new run: it would not compare what the version's last baseline run compared (while no earlier run is a baseline, what the newest run that stored a plan compared) | the run is created paused, before any table row is read; a resume accepts | the run is refused (422): upload a new version |
| anything else: a column added, a column the run does not read removed, a type changed within its class that the engine reads alike (a widened `varchar`, Oracle's `NUMBER(*,2)` for `NUMBER(38,2)`), nullability, the primary key, the relation's kind | a warning; the run goes on with its plan | the run pauses; a resume accepts |

The schema guard fails no run. A look that cannot be made (the target unreachable) is recorded with a
warning, `the schema guard could not check side a at the start of cycle 7 (TargetUnreachable): the cycle went
on unchecked`, and the cycle goes on; a refused login pauses the run as any refused login does.

**Each pause and what to do.** The first line names the first change that stops the run; each other change
that stands follows as a line of its own.

- `Paused by the schema guard at the start of cycle 7: side b: column 'email' of public.customers is gone,
  or the role may no longer read it. Put it back and resume the run, or cancel it and start a new run, which
  plans the table as it is now.` Restore the column or the grant, then resume.
- `…: side b: column 'amount' of public.customers was numeric(12,2) (decimal) when this run was planned and
  is text (text) now: its values would no longer render as planned. Cancel the run and start a new one,
  which plans the table as it is now, or change the column back and resume.` The same line names a type
  within its class that the engine reads otherwise: `was DATE (timestamp) … and is TIMESTAMP(6) (timestamp)
  now`.
- `…: side a: table or view APP.CUSTOMERS is gone or no longer visible. Put it back and resume the run, or
  cancel it.`
- `…: side a: key column 'ID' of APP.CUSTOMERS was NUMBER(10,2) when this run was planned and is
  NUMBER(10,4) now: its key text would render differently. Cancel the run and start a new one, which plans
  the table as it is now, or change the column back and resume.`
- `Paused by the schema guard before any table row was read: column 'last4' was compared by run R-… of this
  version and is not compared now. Resume to accept the tables as they are, or cancel.` A new run would
  compare less than the last one: resume it to accept that (the run is then a baseline, and the next run is
  judged against it), or cancel it and restore the column. `ds run` prints `paused
  (run_paused_schema_drift)` with the lines and exits 2.
- Under `pause`: `…: side a: column 'EMAIL' was VARCHAR2(120 CHAR) and is VARCHAR2(254 CHAR) now (still
  text). This spec pauses on schema drift: resume to go on with the run's plan, or cancel.`
- At creation under `pause`, a narrowing refuses the run: `spec 12 (customer-profile 1.4.0) pauses on schema
  drift and a run of it would not compare what run R-… compared: …. Upload a new version to compare the
  tables as they are.`

**Accepting.** Where a resume accepts changes (the run was created paused, or its spec pauses on schema drift
and a change that breaks nothing stands, even beside one that must be undone first), the Run screen names its
control `Resume (accept the changes)`. Accepting records who and when (`the change of side a observed at cycle 7 was
accepted by jdoe on 2026-10-07; the run goes on with its plan`); the run goes on reading by the plan it was
created with, and an accepted shape never pauses it again. A resume is refused (409, `run R-… cannot resume:
…`) while a change that breaks the plan stands, or when a side cannot be looked at now (`try again once the
target answers`). A run paused for a breaking change under `finding` resumes, once the change is undone,
without accepting anything.

**Warnings** are lines on the run and `warning: …` from `ds run`, for example `side b gained column
'loyalty_tier' since this run was planned: this run does not compare it; a new run would` or `side b:
column 'opened' was NOT NULL and is nullable now`. A standing change is recorded once, however many cycles
follow.

**Alerts.** The schema guard's pause is a `run.paused` with reason `run_paused_schema_drift`, so a rule that
routes `run.paused` (the `run-health` example) carries it; route `schema.drift` for the warnings and the
looks it could not make ([ALERTS.md](ALERTS.md) §2.1). A channel routed both sends one notification for a
pause, and a second pause for the same change, after a resume, is told again. Neither names a table or a
column: open the run.

**A schedule held in `overlap`.** A run the guard paused, or created paused, is live: its schedule's later
times are recorded as `overlap` until a person resumes or cancels it, or the purge cancels it past its
retention days. Read the run's lines first; cancel it to let the schedule start its next run, which plans the
tables as they are.

**Before a downgrade,** resume or cancel every run paused with run_paused_schema_drift: the older release
knows neither the reason nor its notification.

## Graph parity

Two tables of link facts, one row per (entity, identifier), can also be compared as a structure:
whether each identifier links the same entities, and whether groups of linked entities split or
merge. [GRAPH.md](GRAPH.md) is the contract; `examples/graph/` is a demo with seed SQL and the rows
it reports.

**Black-box.** Each side supplies its facts as a PostgreSQL table or view, for example an export the
system's owners load. DeltaSleuth reads relations only, never a graph database.

**The spec.** A pair of tables whose composite key is the fact adds `graph`:

```yaml
key: {name: link, type: composite, parts: [entity_code, id_type, id_value], class: business_code}
sides:
  a: {role: legacy, target: crm-links, table: {schema: crm, name: entity_identifiers, keyColumns: [entity_code, id_type, id_value], columns: [entity_code]}}
  b: {role: new, target: platform-links, table: {name: links_export, keyColumns: [org_code, kind, value], columns: [{name: org_code, as: entity_code}]}}
graph:
  members: [entity_code]          # the key parts naming an entity
  groups: [id_type, id_value]     # the key parts naming an identifier
  maxComponent: 10000             # optional: members a component is expanded to (default 10,000)
```

`members` and `groups` name every key part once between them. Filters, key maps and type maps apply
as for any pair of tables. Keep `class: identifier` when identifiers are personal data (phone
numbers, e-mail addresses): every label is then a token.

**What a run reports.** A graph run is a scan run (batch or soak), in two layers:

- **L1, the facts.** The scan compares them as rows: a link on one side only is NOT_FOUND_A or
  NOT_FOUND_B, with the counters, findings and reports of any table run.
- **L2, the structure.** When the scan reaches the end, each identifier's set of entities is compared
  between the sides, then each side's linked entities are followed out from the identifiers that
  differ, into components. L2 adds no result or finding and changes no counter: it explains L1's
  findings.

L2's rows are in `graph.csv` (`seq`, `code`, `label`, `count_a`, `count_b`, `only_a`, `only_b`,
`detail`), `GET /v1/runs/{id}/graph?cycle=` (paged, `limit` and `cursor`) and the PDF's "Graph
parity" section (the counts, then the first 20 rows of each code). The Run screen shows a "Graph:"
line of counts, which `GET /v1/runs/{id}` returns as `scan.graph` and the chat's run summary
includes.

| Code | Label | `count_a`, `count_b` | Meaning |
|---|---|---|---|
| `GROUP_MEMBERS_DIFFER` | The identifier, `vat\|GB900001001` | Its entities on each side | The identifier links other entities on each side, or exists on one side only. `only_a` and `only_b` count the entities on one side; `detail` lists up to 5 of each. |
| `CLUSTER_MERGE` | An entity of the component | Components on each side | Several components of side a are one on side b. |
| `CLUSTER_SPLIT` | An entity of the component | Components on each side | One component of side a is several on side b. |
| `CLUSTER_REARRANGE` | An entity of the component | Components on each side | Both at once: entities moved between components. |
| `CLUSTER_OVERSIZED` | The component's first entity, in text order | Entities reached, on the side `detail.side` names | A component over `maxComponent` entities, or reaching a hot identifier: no longer expanded, never classified, never dropped. |

- **Hot identifiers.** An identifier linking more than `maxComponent` entities (a placeholder VAT
  number, a switchboard phone) is compared by its size and member hash only. Its
  `GROUP_MEMBERS_DIFFER` row shows the sizes and `detail.oversized: true`, and every component that
  reaches it is `CLUSTER_OVERSIZED`. Clean the data, or filter the placeholder out on both sides
  (`where: "id_value <> 'GB000000000'"`). A larger `maxComponent` expands more, at the cost of
  memory.
- **An entity on one side only** is L1's finding, not a change of structure: a component that maps
  one to one is not reported.
- **No ARI or VI.** A global index needs every component, and L2 expands only those the differing
  identifiers reach, so neither is reported; the PDF says so.
- **L2 runs only at the end of a scan.** A scan stopped by `maxKeys`, `until` or `maxDuration` has no
  graph rows, and the PDF says "Not computed". A worker killed during L2 resumes the scan from its
  last checkpoint and runs L2 again, counting nothing twice.
- **Performance.** The identifiers' entity sets stream in one pass per side, in constant memory.
  Following components looks up 500 entities or identifiers per query: their side values narrow the
  fact relation with one `IN` list per key column, and their canonical text then selects exactly
  ([GRAPH.md](GRAPH.md) §3). Entity lookups use an index that leads with the entity columns (usually
  the primary key); give identifier lookups an index on the identifier columns, such as
  `(id_type, id_value)`, or each one scans the relation, which adds up for long, chain-shaped
  components. Memory holds only the affected components. 1M facts a side take about 15 s on a laptop;
  the benchmark ([BENCHMARK.md](BENCHMARK.md) Results: an Apple M3 Max, one stock PostgreSQL 16 in
  Docker, warm cache, worker poll 0.05 s) took 17.9 s (17.9–18.0) at 1M and 143.9 s (143.7–144.1) at 10M
  facts a side, with 200 and 2,000 differing groups, the index on `(id_type, id_value)` and
  `maxComponent: 20`.

| Message or symptom | What to do |
|---|---|
| `/graph [invalid_graph] graph parity needs two table sides and a composite key` | Both sides must be tables, and `key.type` composite. |
| `/graph [invalid_graph] members and groups must split key.parts between them` | Name each key part in exactly one of `members` and `groups`. |
| Many `CLUSTER_OVERSIZED` rows | A hot identifier joins many entities: see Hot identifiers above. |
| `/canonical/mode [unsupported] not supported: graph mode` | `canonical.mode: graph`, a graph projection of service responses, is another feature and not supported. |

## What a run records

The database holds the run, its keys, every observation's metadata, each key's result and masked
findings, the root causes and the counters; the evidence directory holds the response bodies,
encrypted. Both go with their cycle at the end of its retention ([Retention, purge and
keys](#retention-purge-and-keys)); the run's row and counters stay. Reports are
generated from the database whenever you ask, by `ds report`, the UI or `GET /v1/runs/{id}/reports/{name}`:

| Report | Content |
|---|---|
| `discrepancies.csv` | One row per discrepant key × finding: unsuppressed findings above INFO, and findings a `bug` rule suppressed (a known bug is still a discrepancy; the row shows `rule_id` and `rule_classification` `bug`). `recheck_obs_a_id`/`recheck_obs_b_id` cite the re-reads. The spec's `reporting.includeSuppressed` adds the other suppressed rows. |
| `suppressed.csv` | Every finding a known-diff rule suppressed, with `rule_id`. |
| `normalization.csv` | NORMALIZATION_ONLY rows (INFO, never a mismatch): raw values differ but normalised values are equal, or two values differ within a field's tolerance (an accepted difference is listed, never silent). |
| `retry.csv` | INCONCLUSIVE and DRIFT keys with their reason and attempts, discrepant keys still owed a recheck (`recheck_owed`), and cancelled keys (outcome `CANCELLED`, reason the run's stop reason). |
| `coverage.csv` | The run counters (below), per outcome, per inconclusive and drift reason, suppressed per rule, selected fields. |
| `root_causes.csv` | Findings grouped into probable causes (member-key fan-out first, then signatures). |
| `graph.csv` | A graph parity run's L2 rows: identifiers whose entities differ, and components that split, merge, rearrange or are too large ([Graph parity](#graph-parity)); only the header for other runs. |
| `report.pdf` | Run and spec, with what its spec approval and the cycle's sign-off say (and, for a run whose tables the schema guard recorded changes of, one sentence counting them up to the cycle: [When a table changes under a run](#when-a-table-changes-under-a-run)), then the root causes first, the coverage counters (a compact grid on one page), the trend against the previous run of the same spec, soak outcome changes, graph parity (graph runs), the top findings, and last an annex of the run's chat answers labelled "AI-generated, not evidence". |
| `measure.json`, `capabilities.proposed.yaml` | Contract measurements and the proposed capabilities (`ds measure`). |
| `adjudication.csv`, `adjudication.json`, `adjudication_score.json` | The reviewer sheet, its selection and its score. |
| `report.md`, `report.html` | The P0 contract report (`ds report --kind summary`), with its "Four-eyes approval" section. |

A NUL character, which PostgreSQL cannot store, shows as the text `\u0000` in stored values and
reports (a legacy side's padding stays visible); the evidence keeps the raw bytes.

Everything except the evidence directory is masked: keys and values of protected classes are
tokens, `date_of_birth` values show only their class, and member keys inside pointers are
tokenised. Member names that are data (an object used as a map, under a `security.tokenize` rule)
are masked in every published path pattern too. Observation ids are keyed with a key derived from
the tokeniser key, so they reveal nothing about the key they were read for. The database keeps raw
keys only where the fetch needs them, sealed with the run's data key: `run_keys.key_enc`, and the key
a table scan resumes after (`ds.scans.state.after`). Every other table joins on the masked key, and
every view shows it. Response headers and chat text are sealed too. The profiler's `draft.spec.yaml`
is the one exception by design: its key list holds the sampled keys, like any spec does, and a spec's
inline key list is stored in clear and never purged (the loader warns `inline_keys_stored` unless the
keys are `business_code`).

CSV files are safe to open in a spreadsheet: a cell starting with `=`, `+`, `-`, `@`, a tab or a
line break (or their full-width forms) is prefixed with `'` and quoted; plain numbers such as
`-0.01` stay numbers. Excel may undo the escaping if a file is saved again.

## Retention, purge and keys

Every raw value DeltaSleuth stores is encrypted, and deleted on a date; [RETENTION.md](RETENTION.md)
is the contract.

- **Encrypted.** Raw keys, response headers, a table scan's resume point and chat text are sealed in
  the database with AES-256-GCM, under a data key per run; response bodies are encrypted in the
  evidence directory, under keyed names. Masked data (findings, root causes, counters, tokens and
  `business_code` keys) stays in clear. The keyring wraps every data key, and the tokeniser key
  derives every token: both live outside the database and its volumes.
- **Kept until.** A cycle's raw keys, observations and blobs, results, findings, root causes, graph
  rows, scan state and chat messages are deleted together once the cycle ended longer ago than its
  run's retention: 90 days for the spec's `retention: standard` (the default) and 365 days for
  `retention: audit`, or the settings' `retention.standard_days` and `audit_days`, read at purge
  time. A finished run's chat sessions and data key go with its last cycle. Findings never outlive
  the raw data.
- **Kept longer.** A running soak keeps its last ended cycle until its next cycle ends (the next scan
  and the outcome changes read it). A held run keeps everything until released. A blob goes 24 h after
  the last observation naming it. A run paused or queued longer than its retention is cancelled (one
  paused for a missing KEK or a missing approval too: ending it needs no key), and a one-shot run queued for 24 h failed, so
  that their cycles expire too (audited `retention.abandoned`).
- **Never deleted:** runs with their counters and retention, a tombstone per ended cycle, specs,
  the audit log and its hash chain, and evidence pack records (a pack's stored ZIP goes with its
  cycle; an adjudication keeps its hashes, counts and score, not its sheet or keys; a labelling keeps
  its hashes, counts, truth source and score, not its sheet or drawn items). A spec's inline key list (`key.source.kind: list`) is stored in clear with the spec:
  the loader warns `inline_keys_stored` for any key class but `business_code`; pass those keys with
  `ds run --keys` instead.
- **Outside the purge:** P0 files (everything in the P0 folder: the `--out` and `--samples`
  directories, samples being raw bodies named by raw key; the profiler's draft spec, which lists the
  sampled raw keys; reviewed specs and `--keys` files), downloaded reports and evidence packs, logs
  and the LLM provider's copy of the chat. In Docker, the daily cron of DEPLOY.md section 10 deletes every
  top-level entry of the P0 folder older than `standard_days`, whatever its name.

**Once a cycle is purged.** A purge is recorded before anything is deleted, and from then on the
cycle's reads answer 410, or one `error:` line from the CLI, naming the day and why:

| Where | Message |
|---|---|
| The cycle's results, findings, root causes, graph rows, key views and reports; `POST /v1/compare` of a key of its run; `ds report`, `measure`, `adjudicate` and `score` | `cycle 3 of run R-... was purged on 2026-12-26 (retention standard): its keys, results, findings and evidence are deleted`, or `(erasure)` |
| A labelling's draw, sheet download and upload of labels (its list and the labelling itself still show its score) | the same 410 |
| The chat, once the run's current cycle is purged | `the run's evidence was purged on 2026-12-26; AI answers need it` |
| A blob missing from a live cycle | `evidence of O-... is missing (purged or lost)` |

The run itself stays readable. The Run screen and `report.pdf`'s header say until when a cycle's
evidence is kept: `Evidence retained until 2026-12-26 (retention standard)` once the cycle has ended,
`Evidence retained until 90 days after the cycle ends (retention standard)` while it runs, and
`Evidence held until released (retention standard)` for a held run. A purged cycle shows a banner in
place of its tables, and the chat's input is disabled once the run's current cycle is purged.
`GET /v1/runs/{id}` carries the same as `retention`: the class and its days, the hold, the cycles
purged, and each ended cycle kept with the day it expires.

**The purge.** The worker runs a pass every hour, from 7 days after migration 006 first applied (time
to set holds after an upgrade); `ds purge` runs one now. A pass ends the abandoned runs, deletes the
blobs whose 24 h are up, purges each due cycle (an interrupted purge first) and deletes blobs again.
Each cycle is audited `retention.expire` when its purge starts and `retention.purged`, with what it
deleted, when it ends; a pass that did anything is audited `retention.finished`. It is safe to
interrupt: the next pass finishes an interrupted cycle first. One purger runs at a time; another gets
`another purge is running; try again later` (exit 2).

```bash
uv run ds purge --dry-run          # what a pass would do, writing nothing
uv run ds purge                    # a pass now: --limit N cycles; --orphans also deletes files nothing names
uv run ds purge hold R-... --reason "legal hold LH-7"
uv run ds purge release R-...
uv run ds purge --run R-... --reason "erasure request E-12"             # erase a whole run now
uv run ds purge --run R-... --cycle 2 --reason "erasure request E-12"   # or one ended cycle
```

The dry run prints a line per due cycle, then the `held:`, `kept:` (a running soak's last ended
cycle), `abandon:` and `outside retention:` (a due run's samples directory, for the cron) lines, or
`nothing to purge`:

```
due: cycle 1 of run R-... (retention standard): expired 2026-12-26
held: cycle 1 of run R-... (retention standard): expired 2026-12-26; held by local:ann@host since 2026-12-01: 'legal hold LH-7'
kept: cycle 2 of run R-... (retention standard): expired 2026-12-26; its running soak reads it until its next cycle ends
abandon: run R-... (paused since 2026-09-01): cancelled, past its 90 days (retention standard); its cycle is then due
outside retention: /p0/samples-1 (run R-...): P0 samples, for the site's cron
```

A pass prints `purged 3 cycle(s) of 2 run(s): 40,000 key(s); 61,200 blob(s) queued; 58,000 deleted
(1.9 GB), 1,112 kept (shared)`, then, when not zero, the runs it abandoned, the held cycles past their
retention and the orphaned files it deleted. The blobs a purge queues are deleted by a pass 24 h
later; one still named by another observation is kept.

- **Sign-off holds.** The last approval step of a cycle's sign-off holds its run, `signed off: A-...`
  by the approver, unless the run is held already; a revoke that leaves the run without an approved
  sign-off releases it. `ds purge release` releases it like any hold, and the purge of a signed cycle
  then revokes its sign-off, unless an issued evidence pack cites it: that sign-off stays approved and
  the purge deletes only the pack's stored ZIP ([Four-eyes approvals and sign-off](#four-eyes-approvals-and-sign-off)).
- **Holds** keep a run past its retention until released: a legal hold, an investigation. A hold
  placed while a pass or an erasure runs keeps every cycle whose purge has not started. Holding a
  held run, or releasing one that is not held, is refused. The reason is audited (`retention.hold`,
  `retention.release`) and reaches the SIEM: write a case number, never personal data. A hold,
  erasure or revoke reason holding a value shaped like one of the spec's keys is refused (`the reason
  holds a key-shaped value: write it without customer keys`): evidence packs carry these reasons. A
  word counts, or a part of it between `-`, `/`, `.` and the like, that passes the spec's key check
  after `key.normalize` ([PACK.md](PACK.md) §2.3): under `^[0-9]{4,12}$` the case number `LH-2026` is
  refused too.
- **Erasure** deletes one run, or one of its ended cycles, now, whatever its retention, audited with
  the reason `erasure: <text>`. A live run is cancelled first. It is refused, writing nothing, for a
  held run (`run R-... is held`), a cycle that has not ended or was purged, a cycle a running soak
  still reads (`... erase the whole run`), and while a worker holds one of the keys (`... still has keys
  in flight; try again in 60 s`). A key leased in the instant between that check and the cancel still
  leaves a live run cancelled while the erasure is refused: run it again once the key finishes.
  Erasing needs no key, so a run whose KEK is lost for good is erased
  with `--reason "key lost: ..."`.

**Keys.**

```bash
uv run ds keys status              # each KEK with the data keys it wraps; --json; --verify reads every blob
uv run ds keys fingerprint FILE    # a keyring file's KEK ids, oldest first, the last (current); a repeated line is refused
```

`ds keys status` prints `K-3a9c...: 12 data key(s) (current)` for each KEK of the keyring, and
`missing K-1f2e...: 3 data key(s); restore the line from the vault and restart` for each KEK in use
that the keyring lacks, and then exits 2. `--verify` also reads back every blob an observation names
(the restore check), `blobs: 1,234 checked: 1,230 readable, 2 missing, 1 corrupted, 1 unreadable (a
KEK is missing)`, and exits 2 while a blob is corrupted or unreadable for a missing KEK. Missing blobs
are counted, not failed: a missing body answers 410, as a purged one does, and one already missing
when the store was migrated stays missing until its cycle is purged. Record the missing count just
before each backup's dump; a restore checks that it finds no more (DEPLOY.md section 10).

A KEK missing from the keyring (a lost line) does not stop the app: it starts with a warning naming
the KEK, reads that need it answer 503 `evidence unreadable: the keyring lacks K-...`, and a run that
needs it pauses with `run_paused_key_missing`. Restore the line from the vault, restart, and resume
the run.

### Rotating the keyring

Yearly, or at the site's cryptoperiod:

1. **Add a KEK:** append `openssl rand -hex 32` to the keyring (in Docker, `deploy/secrets/kek`:
   `chmod u+w` it first and `chmod 444` after), vault the file, check the vault copy with
   `ds keys fingerprint`, and restart the server (`docker compose restart app`). At its start it
   rewraps every data key under the new KEK (audited `keys.rewrap`) and adds an evidence key for new
   blobs; no blob is rewritten. Other commands never rewrap, and until that restart every command
   wraps new data keys under the old KEK, so the running server reads the runs a CLI creates
   meanwhile. Restart best with no CLI run in progress: a command started before you appended the
   line lacks the new KEK, so after the restart it pauses the runs whose keys it had not opened yet
   (`run_paused_key_missing`); resume them once it has ended.
2. **Destroy the old line,** in the file and in every vault copy, B days later, once the backups
   holding keys it wraps have aged out (DEPLOY.md section 10), and only when `ds keys status` shows 0
   data keys under it: a process started before the restart may have wrapped one, and another restart
   rewraps it.

**A leaked KEK:** rotate at once; cancel and recreate the live runs, whose data keys an earlier
database copy opens with the leaked KEK; destroy the line after B days. Someone holding an earlier
database copy and the leaked KEK still reads what was sealed before the rotation.

## Outcomes, verdicts and counters

| Outcome | Meaning | Verdict |
|---|---|---|
| MATCH | No finding above INFO (format-only differences, and differences a tolerance or similarity accepts, are INFO). Two documents that hold no value at all are never a MATCH: INCONCLUSIVE `unparseable`. | MATCH |
| MISMATCH | At least one unsuppressed finding above INFO. | DIFFERENT |
| KNOWN_DIFF | Every finding above INFO is suppressed by a known-diff rule. | MATCH, or DIFFERENT when a `bug` rule suppressed it |
| NOT_FOUND_A / NOT_FOUND_B | Authoritatively absent on one side, present on the other. | DIFFERENT |
| DRIFT | The recheck saw A change (`a_changed`) or B catch up with A (`b_converged`). | INDETERMINATE |
| INCONCLUSIVE | A side could not be read completely (reason: `truncated`, `absence_unconfirmed`, `absent_both`, `error_a`, `timeout`, ...). Never counted as a match. | INDETERMINATE |

- **On the Run screen**, "What the outcomes mean" under the outcome badges says each outcome in a line
  (`NOISE_ONLY` too, which no key gets in this build), and the spec's name opens the stored version
  read-only: as stored and parsed, so without the upload's comments and key order, inline keys masked
  (in clear for `key.class: business_code`, the default: the text's second line says which).
- **Confirmation.** Live runs re-read keys whose first outcome is in the spec's `recheck.outcomes`
  (B first, then A) after `recheck.delay` plus jitter; confirmed results say `rechecked`. File
  samples cannot be re-read, so file-mode results stay `provisional` (they are still counted as
  different). "A changed" means A's state, its *normalised* canonical hash, or (a table side) its
  row filter membership changed: a raw-only change a normaliser removes is not a change.
- **A first read that failed.** When the first read was INCONCLUSIVE because a side failed or was
  incomplete, the recheck is the key's first comparable read: an agreeing recheck gives MATCH
  (never DRIFT), and a discrepant one stays `provisional` and is owed a second read
  (`recheck_owed`), because one read cannot confirm a discrepancy.
- **Counters** reconcile exactly, or the cycle fails: `total = match + different + indeterminate +
  cancelled + outstanding`, and `compared = match + different`. The mismatch rate is
  `different / compared`; coverage is `compared / total`. `outstanding` counts live discrepant keys
  still owed a recheck: a run that stopped before their recheck, or a recheck that was their first
  comparable read.

## Stopping early

- **A target answers 401/403:** the run pauses (`run_paused_auth_error`) and no key is started.
  Fix the credential file, then resume the run (Run screen, or `POST /v1/runs/{id}/resume`): the
  credential files are read again (no restart needed), the refused key is read again, whatever
  `recheck.outcomes` says, and the stop reason is cleared. In file mode a sample recording a 401/403
  is judged INCONCLUSIVE like any failed read; it never pauses the run.
- **An `execution.stop` condition fires** (`stop_condition:<name>`): `maxDuration`, `until`,
  `maxErrorRate` and `maxInconclusiveRate` (judged once their `window` is full),
  `maxConsecutiveErrors` (failed calls in a row, either side) and `maxUnexplainedCritical`. The keys
  not started are cancelled and the run ends `completed_with_issues`. A soak run also stops at a
  cycle boundary on `maxCycles`. `maxUnexplainedCritical` counts each key's latest saved result, so a
  worker restart or an auth pause keeps it; the rate windows and `maxConsecutiveErrors` count the
  current worker process's calls and start over with a new worker.
- **Cancel** (Run screen, or `POST /v1/runs/{id}/cancel`): the same, with status `cancelled`. A table
  scan still running is cancelled with its run, as it is when the run is paused: its sessions are
  closed and its statements cancelled on both databases (observed on MySQL: gone within seconds).
- **Ctrl-C of `ds run`**, or a SIGTERM, stops only the local worker: the run stays running for `ds worker` or
  `ds serve` to finish. A killed worker (even `kill -9`) loses at most the keys in flight; another
  worker reads them again once their lease expires, and no result is ever duplicated.

`maxKeys` caps the population: the run takes the first `maxKeys` keys (rechecks included) and says
`stop_condition:maxKeys`. Keys in flight finish. retry.csv lists every unfinished key; start a new
run with those keys (and your own key list for tokenised keys, which reports show only masked).

## Notifications

With a `notify` block in `deltasleuth.yaml` (DEPLOY section 3, Notifications; [ALERTS.md](ALERTS.md)),
the worker tells the channels its rules name when something needs a person. A notification says what
was observed, with ids, counts and a link, never a key or a value; it is not evidence and not a
sign-off, and says so. Delivery is at least once and in order per channel: a receiver may see one twice
(the same `webhook-id`). A failed channel never touches a run.

| Event | Severity | What it asks |
|---|---|---|
| `cycle.ended` | warning when a condition held or crossed, or keys differ or are indeterminate | Look at the Run screen's Changes and Root causes: new root causes, keys newly discrepant, a rate that crossed its bound |
| `sample.judged` | info when passed | A failed or incomplete sample blocks sign-off: close it with the fix ([Sign-off sampling](#sign-off-sampling)) |
| `run.paused` | alert for a refused credential or a missing key | Fix what the sentence names, then resume the run |
| `sample.draw_failed` | warning | Fix the frame, then resume: the run draws again |
| `run.stopped` | alert for an error, inconclusive or critical stop condition | See why the run stopped ([Stopping early](#stopping-early)) |
| `run.failed` | alert | Its log names the cause; the notification names only the error's type |
| `run.finished` | warning when completed with issues | Read the reports |
| `accuracy.measured` | info | Nothing to fix: a labelling of an ended cycle was labelled; it says how many items were labelled, undecidable and not reviewed, never a share ([Labelling against the truth](#labelling-against-the-truth)) |
| `approval.waiting`, `approval.revoked` | info; alert | Review the request; a revoke paused or stopped the runs it counts |
| `licence.state` | alert when expired; info when the state is `open` (this build's, once) | In a vendor build, install the renewal ([Licence](#licence)) |
| `schedule.missed` | alert, or warning for `overlap`, `approval`, `licence` and `sample` | A schedule's due time, or a counted range of them, started no run: its sentence says why. Or the worker paused a schedule before a time of it came due (its zone or cron did not load in the worker's build): an alert ([Schedules](#schedules)) |
| `notify.dead`, `notify.skipped` | alert; warning | A channel refuses or keeps failing; or audit rows were never notified (an outage beyond `max_age_hours`) |
| `notify.daily` | info | Nothing: the heartbeat. Its absence says the worker, and so the notifier, is down |

- **"New" always names its baseline.** From the second cycle, a root cause is new when no retained
  earlier cycle holds it (`observed for the first time since cycle 4`), and a key is newly discrepant
  only among the keys read in both cycles, so a rolling soak's keys entering or leaving are not counted.
  The first cycle counts against an empty baseline: `2 root causes and 41 discrepant keys observed in the
  first cycle`, so a batch run's gaps notify too. In a `hash_sample` run each cycle is a fresh sample:
  root-cause changes say `(between samples)` and key changes are not counted. A fact that could not be
  counted (a purged cycle, a query over 30 s) reads `not counted`. A level condition (`mismatch_rate_above`,
  `coverage_below`, …) fires when it is crossed and again when it recovers, never while it stays.
- **Explain a cycle.** `ds notify explain R-… [--cycle N]` prints the digest the notifier computes for an
  ended cycle and each rule's verdict (`fires: mismatch_rate_above crossed`, `does not fire`, `takes no
  cycle.ended`), writing nothing. A purged cycle says `cycle 3 is purged: root causes, key changes and
  criticals are unavailable`.
- **Mute** a channel or a run for 1 to 168 hours, with a reason (audited; never personal data or a key):
  an admin on the Notifications screen, signed in within 15 minutes, or `ds notify mute (--channel X |
  --run R-…) --hours N --reason TEXT`. Its notifications are stored as suppressed and never sent; every
  screen shows `Notifications to ops-hook are muted until … by … (M-…)` until it lapses or is ended (End,
  or `ds notify unmute M-…`). Alerts ignore the hourly rate limit, never a mute or de-duplication: an
  alert repeating one pending or sent within `dedup_hours` (a second `notify.dead` of the same channel,
  say) is held back as a duplicate. When notifications were held back, the next one says how many.
- **Recovery.** A channel that fails shows on every screen (`Notifications to … are failing: 3 not
  delivered since …`) and in `notify.dead`. `ds notify deliveries --channel … --state dead` lists the
  closed codes. Once the receiver is fixed, Send failed again (operators; or `ds notify retry CHANNEL
  [--since TIME]`) queues the deliveries that died in the last 72 hours again, out of their original
  order. A delivery rendered before a restart changed the channel's kind, `labels` or `links` is never
  sent: it dies `channel_changed` and a retry leaves it dead. Send test (or `ds notify test CHANNEL`)
  tries one now, outside the queue. `The notifier is behind` means no worker runs: start it; rows older
  than `max_age_hours` are then skipped in one `notify.skipped`.
- **Name specs after the systems they compare** (`customer-profile`, not a customer's name or number):
  the spec name is the one label a notification may carry. It is left out, saying why, when the channel's
  `labels` is off (the default for Slack and Teams), when the spec has no key pattern, or when the name
  looks like a key.

## Cut-over readiness

The Readiness screen (`#/readiness`), `ds readiness` and the readiness reports say, per pair, which named
gates hold on stored evidence and where the pair stands on a fixed ladder ([READINESS.md](READINESS.md)).
A pair is a spec name with a batch or soak run, judged on its current version: the one of its newest
evidence cycle. Every surface starts with the same sentence:

> Readiness is a set of named gates, each met or not on observed evidence, and a place on a fixed ladder.
> It is not a probability that the two systems are at parity, not a forecast, and not sign-off evidence: a
> four-eyes sign-off decides, and its evidence pack certifies what was observed. For a pair observed whole,
> the targets gate is one pooled count over the pair; architecture §15's per-stratum targets are judged only
> for sampled cycles, and otherwise by the approver.

The only figure is `k of m gates`: the required gates met, of those that can be judged here. There is no
percentage, score or average of readiness, and `unknown` is never counted as met.

| State (lowest first) | It means |
|---|---|
| `no evidence` | no complete cycle of the pair counts as evidence yet (each gate says why) |
| `not ready` | a parity gate is not met |
| `unknown` | no parity gate is unmet, but one could not be judged (a cycle not measured yet, or measured after its purge) |
| `parity ready` | every parity gate is met |
| `sign-off ready` | every governance gate is met too: request the sign-off |
| `signed off` | the candidate cycle holds an approved sign-off |
| `evidenced` | and an issued evidence pack |

| Gate | Met when | When it is not |
|---|---|---|
| `population` | the evidence cycle observed side a whole (a table scan read to its end, a key list whose census counts side a, a `bottom_k` sample whose frame does) | A key list: configure `readiness` or `packs` so that its census is counted, or scan the tables (until it is, a pair whose keys were given at creation reads `no evidence`, and says so); a rate sample only monitors |
| `critical` | no key has an unexplained CRITICAL finding (a bug rule counts as unexplained) | Fix or explain them; a partial cycle's CRITICAL keys count too |
| `targets` | not sampled: discrepant plus unresolved keys within the tier's share of the pair (0.1%, or 0.01% for `high`), counted over the whole pair; sampled: its stored judgement passed at the policy's tier | The text says how many keys are over; per-stratum targets of a census pair stay the approver's |
| `convergence` | the newest K evidence cycles of the current version in a row (3 by default) each met the per-cycle gates | The text names the cycle that failed and the earliest cycle of a live soak that can complete the streak, as a schedule, not a forecast; a new spec version starts the count again. It reads `unknown` only while a cycle not measured (or not readable) could still complete the streak |
| `fresh` | the evidence cycle ended within the gate's age (7 days by default) | Run a cycle |
| `graph` | no split, merge, rearrange or differing-members row (only when `gates.graph.required`) | See [Graph parity](#graph-parity) |
| `spec approval`, `adjudication`, `sample ledger`, `accuracy` | the governance a sign-off needs: the spec approval the cycle ran under, a passed adjudication a person scored (from 50 results), a sampled cycle's draw discipline, and, only when `gates.accuracy` is set, a labelling within its bounds | Each text says what to do; outside SSO they read `unavailable` and the pair stops at `parity ready` |
| `sign-off`, `pack` | an approved, unrevoked sign-off of the candidate cycle; an issued, unrevoked pack of it | `unavailable` without SSO or `packs`, and in a vendor build without an Enterprise licence; a stored sign-off or pack counts whatever the installation allows now. Only a sign-off of an evidence cycle counts: one of a key list given at creation without a census, say, is told and leaves the pair unsigned |

- **Configure** (optional). A `readiness` block in `deltasleuth.yaml` groups pairs into programmes and
  entity types and sets the gates (DEPLOY section 3, Cut-over readiness); without it every pair is judged
  by the defaults. The tier never goes below 0.1%, and a programme or pair may only tighten it; fewer
  convergence cycles or an older evidence cycle are allowed and named everywhere as a deviation
  (`convergence 1 cycle (default 3; relaxed)`). The settings apply at the next restart; `Readiness
  settings <12 hex>` on each surface names them, and a server and a worker started with different files
  say so: `this server judges with readiness settings …, the worker with …: restart both after changing
  them`.
- **The census costs a read.** With the block present, each cycle of a key-list run with a table side
  counts that side in one snapshot before it compares ([Evidence packs](#evidence-packs) describes the
  census): one more read of the table per cycle, an hour a statement at most. A scan or a sample costs
  nothing more.
- **Check the settings** before the restart: `ds readiness check` prints each pair's effective gates,
  each tagged `(default)`, `(installation)`, `(programme)` or `(pair)`, its deviations and the settings
  hash, and warns of a listed spec that is not stored (`programme cutover-q4 names spec custmer-profile,
  which is not stored`).
- **Read the gates.** The screen lists each programme (`2 of 5 pairs ready`, with a step chart of how
  that count moved over the last 90 days of recorded changes), then each pair worst first: its state, `6
  of 9` with a cell per gate (hover for its text), a sparkline of its last evidence cycles against their
  allowance, its evidence cycle, since when it has been in that state (a change of a gate alone does not
  move that time), its sign-off and pack. A pair's page shows the ladder, every gate's text by group, the
  trend with a marker where the version changed, each cycle's new, persistent and fixed discrepant keys,
  a sampled cycle's strata, the live soaks and the recorded transitions. Wherever a discrepant count is
  given (the `targets` gate's text, the sparkline's hover text, the trend, the dashboard and the CSVs),
  the keys that known differences or noise suppressed stand beside it; a sampled cycle's counts in the
  trend are its sample's own, with no rate and no bound for the strata pooled. `ds readiness [--programme
  P] [--entity E] [--state S]` prints the same: `cutover-q4: 2 of 5 pairs parity-ready or better (worst:
  not ready)`, then per pair `customer-profile 1.4.0 (spec 12) customer  not ready  6 of 9 gates  cycle
  14 of R-… (2026-10-03)` and each unmet gate's text; `ds readiness pair NAME` prints one pair.
  `--fail-unless parity_ready` exits 1 while a listed pair is below that state, for a pipeline; `--as-of
  2026-10-03T04:00:00Z` shows what the worker had recorded by then.
- **Act on the precheck.** Once a pair reads `sign-off ready`, its `sign-off` gate names the cycle to
  sign off: the newest cycle in the streak whose adjudication passed, else the newest evidence cycle.
  "Check sign-off now" on the pair's page (`ds readiness pair NAME --precheck`) runs the sign-off's own
  checks on that cycle and lists what a request would be refused for; it is labelled `computed now; the
  sign-off decides`, changes nothing, and under tokens or developer mode has nothing to check. Where
  evidence packs are configured it also lists the line that refuses every pack of a spec whose
  `key.pattern` cannot tell a key (`spec 12's key.pattern matches the empty string: a raw key cannot be
  told from other text, so no pack`): the sign-off is not refused for it, its pack would be, so upload a
  version with a pattern and run it before asking for the sign-off. Then
  request the sign-off on the Run screen ([Four-eyes approvals and sign-off](#four-eyes-approvals-and-sign-off)).
  Readiness can be stricter than a sign-off: a signed pair whose newer cycles fail reads `regressed`, its
  sign-off standing as it was.
- **Route the events.** The worker records each change of a pair's state, of `regressed` or of a
  required gate's status as a `readiness.changed` audit row, within about 30 seconds of its cause (15
  minutes when only time changed a gate, as `fresh` does). A pair with a cycle still being measured is
  recorded once it is measured. Each row names its causes from what changed for the pair itself: `cycle`
  (a new evidence cycle, or a parity gate), `version`, `signoff`, `pack`, `adjudication`, `accuracy`,
  `ledger`, `licence` (in a vendor build: sign-off or pack became possible, or no longer is), `closed`, `time` (the evidence
  cycle is no longer fresh), `baseline` (the pair's first row) and `config` (the rows written as the
  worker first judged under changed settings; no later row). A notification rule routes them, and must
  name the causes it wants; it fires when any of them is among the row's:

  ```yaml
  - {name: cutover, events: [readiness.changed], channels: [owners-mail], programmes: [cutover-q4],
     causes: [cycle, version, signoff, pack, adjudication, accuracy, ledger, licence, closed, time]}
  ```

  This rule leaves out the first recording of each pair (`baseline`) and the rows only a settings change
  explains (`config` alone): list those two to receive them. A fall is an alert, which no rate limit
  holds back: a gate that was met is now not met or unknown, a gate went so as the state fell, or the
  pair turned `regressed`. A gate that became `unavailable`, or a state that fell otherwise, is a
  warning; a gate that only became judged, as when a licence is renewed, is information. A pair that
  falls again after its recovery was notified is notified again; the same fall recorded twice in a row
  within `dedup_hours` is one notification. A notification reads `[prod] cutover-q4 / customer-profile:
  readiness not ready -> parity ready (5 of 5 gates; targets met, convergence met; cycle 14 of run R-…).
  Observed; readiness is not a sign-off.` Without notifications, the SIEM line of the row is the signal
  (DEPLOY section 10).
- **Close a pair** that was cut over or dropped: add `closed: 2026-11-02` to its entry and restart. It
  is then judged as of the end of that day, leaves the open pairs and its programme's counts, shows
  under "Pairs: closed" with `closed on 2026-11-02: judged as of that day`, and records nothing more.
- **Reports.** The screen's `dashboard.pdf` (100 pairs a volume, worst first, a button for each volume
  of a larger selection), `dashboard.csv` (a row per pair and gate) and `trends.csv` (a row per pair
  and measured cycle, refused over 100,000 rows:
  narrow it by programme, entity or pair, or by day), or `ds readiness report --out DIR`, which writes
  every volume and, over the limit, one trends file a pair; each download is audited. A run's
  `report.pdf` gains "Pair readiness (observed)": its pair's parity gates as of that cycle, judged on
  the cycles ended by then, and `readiness.csv` among the run's reports lists its pair's cycles up to it.
- **The cut-over brief** ("Cut-over brief" on a pair's page, `cut-over brief` on a run's) is one pair on
  one screen: its state and gates, the stored figures of its newest complete cycle (each a link into the
  run; `Match` to `Not found on b` add up to `Compared`), a trend of its last 30 measured cycles (the
  stored mismatch rate above the count of root-cause groups, lettered `V` where the spec version changed
  and `K` where the key source did, or the size of a key list given with the run or read from a key
  table) and its root-cause groups as `opened`, `still open` or `closed`. It restates stored measures: no
  score, no forecast, and `closed` means not observed in the newest complete cycle, not fixed: where a
  group is closed and that cycle did not compare every key, the line above the groups says how many it
  did not, since the group's keys may be among them. The stored rate and coverage are rounded to six
  places: where one would read a plain 0 or 1 beside counts that say otherwise, the brief prints `under
  0.000001` or `over 0.999999`. A run of a few keys shows there when it is the newest, under a line
  naming the cycle the gates judge. Its buttons, or `ds readiness report --pair <pair> --out DIR`,
  save `brief-<pair>.pdf` (one page), `brief-<pair>.csv` (a row a cycle) and `causes-<pair>.csv` (a row a
  root-cause group; a download is refused over 100,000 rows, the CLI writes them all).
- **After an upgrade** the worker measures the cycles ended before it, newest first (10 every 10
  seconds), and records nothing until they are done; the screen says how many are left (`over 100,000`
  beyond that many). Cycles purged before the upgrade read `unknown`: their findings are gone. A measure
  that fails (a statement the database cancels under a `statement_timeout`, say) is recorded as failed,
  tried again after 1, 2, 4 and 8 hours and then left `unknown`: `readiness: cycle 3 of run R-… not
  measured: QueryCanceled` in the worker's log names it. While a cycle ended before the upgrade still
  waits for another try, nothing is recorded for any pair: 15 hours when each of its tries fails.
- **When readiness itself fails**, no run does. A cycle whose measures this version does not read
  (written by a newer build) is an `unknown` point: `… holds measures of format 2, which this version
  does not read`. A pair that cannot be evaluated shows every gate `unknown` with `<pair> could not be
  evaluated (<type>): see the server log`, the log names it (`readiness: pair <pair> could not be
  evaluated: <type>`), and the other pairs are judged. `readiness evaluation failed: <type>` in the
  worker's log is an evaluation that failed for every pair: it is tried again 30 seconds later, cycles
  are still measured meanwhile, and nothing is recorded until it succeeds.
- **Verify the measures.** `ds readiness verify` checks every stored measure against the
  `readiness.measured` audit row that recorded it, and every such row against the cycle it names: it
  exits 2 on a measure that was edited, removed, replaced (by a failed measure's record, an excluded
  marker or anything else) or removed and measured again, on a measure no row names, and on an excluded
  marker that does not agree with its run. It does not see what was changed before a cycle was measured
  (its results or counters): it checks the measures, not what they were measured from. Run it beside `ds
  audit verify`, which checks the rows themselves.

## Schedules

A schedule starts runs of a stored spec at cron times in a time zone, without a person
([SCHEDULES.md](SCHEDULES.md)). The worker of `ds serve` or `ds worker` fires it; each firing is an
ordinary run, created by `system:worker`, which the four-eyes gate, a sample's rules and, in a vendor
build, the licence judge as any run. Every time that started no run is recorded with a closed reason and can alert. The Schedules
screen (`#/schedules`), `ds schedules` and the API all start with what a schedule never tells:

- a firing starts a run and is not a completed run: the run's status says how it ended;
- next due is a schedule, not a forecast;
- no person is named as a firing's actor;
- a missed time is never shown as run, and a time a save, pause or delete left unstarted is missed, never
  dropped;
- a schedule has no percentage, uptime or success rate: it is not evidence, readiness or a sign-off;
- it holds no key and no value (its runs read the spec's `key.source`, or scan two tables);
- no alert is sent while no worker runs: a missed time is reported when a worker next sweeps.

**Declare one.** An operator fills the form on the Schedules screen (`Schedule…` on the Specs screen
opens it for a version), or:

```bash
uv run ds schedules next '0 2 * * *' --tz Europe/London        # preview: the next ten times, DST notes on stderr
uv run ds schedules add nightly-customers --spec-id 12 --cron '0 2 * * *' --tz Europe/London
# added SC-… (nightly-customers): next 2026-10-02 02:00 Europe/London (2026-10-02T01:00Z)
uv run ds schedules                                            # the list; show NAME for one, with its firings
```

- **Cron** is five fields, `minute hour day-of-month month day-of-week`, with numbers, ranges, lists,
  `*/n` steps and `JAN`–`DEC`, `SUN`–`SAT`; no seconds, macros, `?`, `L`, `W` or `#`, and not both day
  fields at once. Firings are at least 15 minutes apart on the wall clock (a soak's
  `execution.cycleInterval` repeats faster); a clock change can bring two of them closer (Daylight
  saving, below). The screen's presets (daily, weekdays, weekly, monthly, every N hours) only write cron
  text.
- **The zone** is required: `UTC`, or an IANA region name the release's tz data holds (`Europe/London`).
  A renamed name is refused with its primary (`use Asia/Kolkata`). In a deployment, preview with the
  image's `ds` (DEPLOY section 3, Schedules): a host's own tz data may differ. Choose a region's zone or
  `UTC`, not a zone named by its offset (`Etc/GMT+5`): under a key pattern that matches a short number,
  the raw-value gate reads the number in such a name as a key, and the schedule's runs get no evidence
  pack. A save warns of every zone whose name holds a digit, and a preview notes it.
- **The start window** (`--start-within`, 15 to 1440 minutes, default 60) is how long after its time a
  run may still start; it never reaches past the next due time. Only the newest due time is ever started:
  after an outage the older times are recorded as one counted range, never run as a backlog.
- **A soak** (`--soak`, `--rolling`) must end by itself (`execution.stop.maxCycles`, `maxDuration` or
  `until`): while a run of the schedule is live, later times are recorded as `overlap`.
- An installation holds at most 200 schedules.

**Pin or follow.** `--spec-id 12` pins a version: nothing changes when a newer one is stored, the screen
says `pinned to 1.4.0 (spec 12); 1.5.0 (spec 14) is stored`, and moving is a save (Move to the newer
version, or `ds schedules set NAME --spec-id 14`). `--spec customer-profile` follows the name: each firing
runs its newest stored version, never falling back to an older approved one. Under SSO a version without
the approval it needs starts nothing (`approval`) and the next due time is judged anew, so a grant lets
it start with no other act; in tokens mode nothing gates a following schedule, so pin there what should
not change by an upload. A new version restarts the pair's readiness streak.

**Daylight saving.** Times are wall-clock times in the zone. A time the clock skips fires once, later by
the size of the jump; a time the clock repeats fires at its first occurrence only. Saving and previewing
warn of each, for example `2027-03-28: 01:30 does not exist in Europe/London (clocks go forward): that
firing happens at 02:30 (01:30 UTC)` and `2026-10-25: 01:30 happens twice in Europe/London (clocks go
back): the schedule fires at the first (00:30 UTC)`. An hourly local schedule has one two-hour gap on the
night clocks go back; `UTC` has neither. The 15 minutes between firings are measured on the wall clock,
so on a day the clocks go forward a skipped time, firing later by the jump, can land within minutes of
the cron's next time: `0,59 1,3 * * *` in Europe/London fires at 02:59 and 03:00 on 2027-03-28, and
`14,45 2 * * *` in Australia/Lord_Howe at 02:44 and 02:45 on 2026-10-04. The earlier time's window then
closes at the later one, which starts only once the earlier's run has ended (else it is recorded as
`overlap`). The note names the shifted time, not how close it lands.

**Read the firings.** The list shows each schedule's newest firing and its run's status; a schedule's
screen (or `ds schedules show NAME`) lists them all, newest first: `started run R-… 12 min after its
time`, or `not started: …` with the reason. A firing is not a completed run: open the run for how it
ended. The screen's banners name a schedule that is due and not settled (a worker may be retrying or
starting it, or none is running), one the worker paused (its zone or cron did not load in the worker's
build: A schedule the worker paused, below), one pinned behind a stored version, and one the database
clock is behind.

| Reason | It means | What to do |
|---|---|---|
| `late` | its window closed before a worker could start it: no worker that fires schedules ran, the store was down or the clock moved (`down`; several such times are one counted row), or it waited inside its window while the worker was still starting two other schedules (`busy`). Also a time a run exists for already (`fired_already`) | Start a run by hand if it is needed; widen the window, or stagger the minute |
| `superseded` | a save, pause or delete found the time due and not started | Nothing: it is recorded, not dropped |
| `overlap` | a run of the schedule was still live when the window closed | Let the run end, or cancel it; lengthen the interval |
| `mode`, `setter` | the worker runs in another auth mode than the schedule was saved in; or who saved it is disabled or no longer an operator | An operator saves the schedule again |
| `approval`, `licence` | the four-eyes gate refused the run, or a vendor build's licence did. This build refuses no time for a licence: on a database where an older build that enforced the licence did, the range of refused times that build held is recorded once, as `licence` (by the worker when it settles the schedule's next due time or once the range's first time is 24 h old, or at a person's save, pause or delete of the schedule before that), and until that next time is settled the schedule's newest firing reads `not started: the licence refused the run (…): see Licence` or `not started: 1 more time since … for the same reason` | Have the version approved; in a vendor build, install the renewal; in this build nothing |
| `sample` | a live sampled run samples its population; a `bottom_k` draw of it did not pass and is not closed (a rate sample's draws never stop a firing); or the sample's rules refused | Close the draw with the fix ([Sign-off sampling](#sign-off-sampling)) |
| `not_started`, `error` | the run could not be created (a listing, a table side or the store) in three tries, or in as many as its window had room for; or a fault in DeltaSleuth | Start a run by hand to see the refusal; the server log for a fault |
| `zone` | the worker's build could not load the schedule's zone or cron when a time of it was due; the worker paused it | Save it with a zone the build holds, then resume it (A schedule the worker paused, below) |

A refusal that stands (the mode, the setter, the approval, the licence, an unclosed draw) is recorded once,
then as one counted row a day (`14 more times since … for the same reason`), not once per due time.

A refusal that may pass is tried again while the window is open, and records nothing while it waits: a
live run of the schedule, or of its sample's population, every minute; a run that could not be created
every five minutes, three tries at most for one due time. Whether another try fits is judged after each
attempt, on the clock as it then stands: once the wait and one worker sweep would no longer end before
the window does, the time is recorded at once with its own reason, never as `late`. A listing still being
read when the window closes is `not_started` (`TimeoutError`).

**A schedule the worker paused.** A worker whose tz data does not hold a schedule's zone (a release
dropped the name), or whose build cannot read its cron, pauses the schedule as `system:worker` instead of
firing it. If a time of it was due, that time is recorded: `not started, and paused by the worker: the
worker's time zone data (2026c) did not hold 'Europe/Kyiv': save the schedule with a zone the build
holds, then resume it`, an alert; the version it names is the worker's when it paused the schedule,
whichever build shows the row later. If no time was due (the worker meets it at a restart, when it
recomputes every schedule's next due time), nothing is recorded as missed: the `schedule.pause` audit
row (`reason: zone`, with `code`, `tzdata` and `missed: false`) is what a rule for `schedule.missed`
sends, an alert reading `schedule SC-… is paused by the worker before a time of it came due: the worker's
time zone data did not hold its zone. A person saves the schedule with a zone the build holds, then
resumes it.` The list's banner and the paused state say it too, so look at the Schedules screen after an
upgrade where no rule routes `schedule.missed`. To repair it, edit the schedule and
choose its zone: for a stored zone the build does not list, the form selects none and asks for one
(another zone moves every time of the cron). Save, then resume (`ds schedules set NAME --tz ZONE`, or
`--cron TEXT`, then `ds schedules resume NAME`): the times of the pause are neither due nor missed.

**Change, pause, resume, delete.** Edit on the screen, or `ds schedules set NAME --cron '30 2 * * *'`
(the options given replace what is stored; a paused schedule stays paused). A time that is due and not
started when you save, pause or delete is recorded as missed first. `ds schedules pause NAME` (or Pause)
stops it; `resume` starts again from now, and the times of the pause are neither due nor missed. `delete`
frees the name; its runs and firings still name it. Each write is audited as its caller.

**Stagger shared minutes.** The worker starts two schedules at a time. Table scans start within seconds,
but specs with a listed key source start two at a time for as long as their listings take, and a schedule
queued behind them past its window is recorded as `late`, code `busy` (`its time … passed its 60 min
while the worker was still starting other schedules (two at a time)`). Only a time that waited for a
slot while its window was open reads so: times that passed while no worker ran read as the outage's
(`down`), however many schedules then waited. Spread schedules over the hour (`5 2`, `20 2`, `35 2`)
rather than putting them all on `0 2`.

**A leaver.** Disabling a person (Admin, Users, or `ds users disable U-…`) also stops the schedules they
last saved, in person or through a token they own: each due time is recorded as missed (`setter`) until
another operator saves the schedule again. The worker reads who saved a schedule as a firing begins
and again as it inserts the run, so a disable stored while a firing is still reading its key listing
stops that firing too; a run already started is not stopped. List them first: `ds schedules --set-by
user:U-…` (and `--set-by token:T-…` for each of their tokens). Revoking or rotating a token stops nothing
by itself, and in tokens mode no person exists, so there only a pause or a delete stops a schedule.

**Sign-off.** A scheduled run was started by no person, so its sign-off needs two approvers, and whoever
saved the schedule may be one of them. For a sign-off with one approver, start the candidate run by hand.

**Sampled schedules.** Each scheduled run of a hash sample draws afresh, with a new nonce. A `bottom_k`
draw that did not pass stops the later firings until it is closed (`sample`, `unclosed`); a rate sample's
draws never stop a firing. A later scheduled draw that does not pass refuses an earlier draw's sign-off:
pause the schedule before requesting sign-off of a draw that passed, and resume it once the sign-off is
decided.

**Alerts.** With notifications on, route `schedule.missed` (an alert for most reasons, a warning for
`overlap`, `approval`, `licence` and `sample`), with `run.failed` and `readiness.changed`:

```yaml
notify:
  rules:
    - {name: schedules, events: [schedule.missed, run.failed], channels: [ops-hook]}
```

A nightly batch schedule's `cycle.ended` digest baselines inside one run, so every night's run reports
every standing gap as new: watch a scheduled pair through `readiness.changed`, which compares across
runs, rather than `cycle.ended`.

**The outside check.** A schedule is not a dead-man's switch: a worker that is down records and sends
nothing, and reports one `late` range when it is back. Watch from outside: a SIEM rule on the absence of
any `schedule.fire` or `schedule.missed` line for `SC-…` within its period, or a monitor polling `GET
/v1/schedules?limit=1` with a viewer token for `overdue`, alarmed once it stands longer than the largest
start window (a worker legitimately retrying shows there too). `GET /v1/health` does not cover it.

**When the auth mode changes, or a vendor build's licence lapses,** pause the schedules (`ds schedules
pause --all`) rather than let each due time be recorded as missed, and resume them after the renewal; after a move to
SSO, save each schedule again, signed in, since a schedule fires only from a worker in the mode it was
saved in. Before a worker starts on a restored or copied database, pause them all too (DEPLOY section 10,
Backups and restore): it would otherwise start one run of each active schedule whose newest due time is
still in its window.

## Your own agent (MCP)

Your own agent (Claude Code, VS Code, Cursor and the like) can read a run's findings by id over the Model
Context Protocol, with the chat's masking, caps and ids ([MCP.md](MCP.md)). It is off until
`deltasleuth.yaml` has an `mcp` block, which states what tool results may carry (MCP.md section 4). `ds mcp
check` prints the block as judged, the tools a viewer and an operator are offered, each tool not served
with the reason, and what MCP is not.

**What it serves.** Eleven tools. Each answers one JSON text, `{"tool", "data"}` or `{"tool",
"error"}`, of at most 65,536 bytes; a text cell is cut at 512 characters and a canonical document at
4,096 bytes a side, as in the chat. A tool is offered only where `mcp.data_classes` lists what its
results carry, and to a caller whose role reaches it:

| Tool | Gives | Needs |
|---|---|---|
| `list_runs` | the stored runs, newest first, of a spec id, a status or a schedule if given: ids, spec names and versions, modes, statuses, cycles, counters, times and the licence label; 10 a page, 12 at most | `aggregates` |
| `get_run_summary` | a run by id: status, counters, its current cycle's progress and top 10 root causes, graph parity counts, a sampled cycle's judgement, a labelled cycle's stored accuracy, its licence label and its spec approval's state | `production_tokenised` |
| `get_root_causes` | the root causes of a run's current cycle, most probable first, a page from `offset` (10 a page, 20 at most) | `production_tokenised` |
| `get_findings` | the findings of a run's current cycle in key order, by key, root cause, taxonomy code, path pattern or severity if given: 20 unless `limit` says, 50 at most per filter | `production_tokenised` |
| `get_observations` | one key of a run's current cycle by its masked key: its result, the masked canonical documents of both sides, its findings and observation ids | `production_tokenised` |
| `get_readiness` | cut-over readiness of every pair, worst first, of a programme, an entity type or a state if given: each pair's state, each gate's status and what blocks it; at most 10 calls a minute | `aggregates` |
| `get_readiness_pair` | one pair's readiness by its spec name: each gate with its status and figures (never its text, which may name a table or a target), its candidate, convergence, versions, live soaks, strata counts and labelling | `aggregates` |
| `list_schedules` | the schedules, newest first, of a spec name or a state if given: what each runs, its cron and zone, its state, its next due time and newest firing | `aggregates` |
| `get_schedule` | one schedule by id or live name: what a firing would run and the four-eyes gate's state for it, its next times and newest 20 firings with their texts | `aggregates` |
| `list_prechecks` | the table pre-checks, newest first: each one's level and source and its pairs counted by status and by verdict; no pair, verdict line or figure | `aggregates` |
| `compare_key` | one key of a run compared again now, under the run's spec: a one-shot run that reads both targets; at most 10 a minute | `production_tokenised`, `mcp.work`, and the `operator` role |

- A tool that takes a `run_id` reads the run's **current cycle** only, and its result names the `cycle`
  it read (but `compare_key`'s, which is a run of its own).
- A `key` is a key as a result showed it (`key_masked`). It is matched against the run's stored keys
  and never sent to a target as typed.
- **With `production_tokenised` alone an agent reads only runs whose spec declares nothing clear.** A
  run whose spec lists `clear` paths, sets `unlisted: clear` or has a business-code key answers `run
  R-… is not served by this installation: its spec declares clear values or a business-code key
  (mcp.data_classes lacks production_clear_declared)`. Adding `production_clear_declared` to
  `mcp.data_classes` serves those runs too: it is the installation's written decision that the
  clear-declared values and the raw business-code keys may reach the agent's model host.
- `compare_key` seats no licence pair: in a vendor build, of a spec name that holds no seat it answers
  `compare_key over MCP seats no pair: …; a person starts a run of it first`; this build counts no pair
  and refuses none. Under SSO it meets the four-eyes gate as any compare does.
- No tool starts, pauses, resumes or cancels a run, and none reads a report, a pack, a sheet, a chat, a
  spec, a target, the licence, the audit log, a token or a user.

**Wiring a client.** An agent calls `POST /v1/mcp` of `ds serve` (`mcp.http: true`) with an API token of
its own: one token per agent, named for it, `viewer` unless it must compare, owned by its person; never
pasted into a chat or committed. No block below holds a token or reads one from an environment variable:
the token is one line in a file of mode 0600, or in the editor's secret store. A browser session is
refused there (403), and so is a page on another origin: the server sends no CORS header.

**The token's role is what bounds the agent.** The file of the first block below belongs to the account
the agent's own shell tool runs as, so an agent with shell tools can read it, and with it call any `/v1`
route under the token's full role, past everything the `mcp` block cuts (What MCP is not, below). So
give an agent a `viewer` token, and prefer the client's secret store where it has one (VS Code's
prompt, the second block); what a client's store keeps from its own agent is that client's matter.
Give an `operator` token only to an agent you would trust to start, resume and cancel runs.

Claude Code, `.mcp.json`, with a headers helper (`headersHelper`: a command that prints the headers as
JSON):

```json
{"mcpServers": {"deltasleuth": {"type": "http", "url": "https://deltasleuth.bank.example/v1/mcp",
  "headersHelper": "/home/jdoe/.config/deltasleuth/mcp-headers"}}}
```

```sh
#!/bin/sh
# ~/.config/deltasleuth/mcp-headers (0700); the token alone in mcp-token (0600)
printf '{"Authorization": "Bearer %s"}\n' "$(cat "$HOME/.config/deltasleuth/mcp-token")"
```

VS Code, `.vscode/mcp.json` (the editor prompts for the token and keeps it in its secret store):

```json
{"inputs": [{"type": "promptString", "id": "ds-token", "description": "DeltaSleuth API token", "password": true}],
 "servers": {"deltasleuth": {"type": "http", "url": "https://deltasleuth.bank.example/v1/mcp",
  "headers": {"Authorization": "Bearer ${input:ds-token}"}}}}
```

Developer mode over HTTP, on the workstation that runs `ds serve` (Cursor's `.cursor/mcp.json`; Claude
Code adds `"type": "http"`):

```json
{"mcpServers": {"deltasleuth": {"url": "http://127.0.0.1:8000/v1/mcp",
  "headers": {"X-CSRF-Token": "dev"}}}}
```

Developer mode over stdio: the client starts `ds mcp` itself, which serves until the client closes the
stream (VS Code: under `servers`, with `"type": "stdio"`):

```json
{"mcpServers": {"deltasleuth": {"command": "uv",
  "args": ["run", "--directory", "/path/to/deltasleuth", "ds", "mcp", "--config", "/path/to/deltasleuth.yaml"]}}}
```

- **Cursor where the settings have `auth`** offers environment interpolation only (`${env:NAME}` in
  `headers`), which is not recommended, so no block is given: such a team uses Claude Code or VS Code, or
  Cursor in developer mode. `claude mcp add --header` is not recommended either: it stores the token in
  the client's file.
- **`ds mcp` where the settings have `auth`** is the host-administrator channel and serves only with
  `--host-channel`: its caller is the asserted `DS_OPERATOR`, not a signed-in person, and the agent that
  runs it acts with the host account, whatever `--role` says. It is not for an agent with shell tools. For
  a person at the host it is the stdio block with `"command": "sh"`, `"args": ["-c", "cd /srv/deltasleuth
  && exec docker compose run --rm -T -e DS_OPERATOR app ds mcp --host-channel"]` and `"env":
  {"DS_OPERATOR": "jdoe"}` (a name, no credential). The line changes to the installation's folder first:
  Compose reads `.env`, which names both compose files, from its working directory, and a client starts
  a server from a directory of its own. Never use `-f`, an exported `COMPOSE_FILE` or
  `--project-directory` in its place. A TTY corrupts the framing, hence `-T`. Docker's logging driver
  records what that container writes, the protocol's frames with their tool results among it: a driver
  that keeps the lines of `run --rm` containers (journald) keeps them in the host's log.
- **A client that starts `ds mcp` must read its stderr, or let it inherit the client's own** (the MCP
  library's own client does the second unless told otherwise). Every call leaves its line there, and
  every audit row its copy. Behind a pipe that nobody reads `ds mcp` stops once the pipe is full, after
  some hundreds of calls: it answers nothing more and does not see its input end. Reading the pipe
  frees it. A first SIGTERM does not end it then, since the loop that would act on it is blocked; a
  second one does, but not where `ds mcp` is a container's first process, as under the host-channel
  line above: there a SIGTERM after the first does nothing ([MCP.md](MCP.md) §5.2).
- **`ds mcp`'s stderr is the client's to keep,** so its logging writes no text there that might name
  the store or carry data: the start lines, one line a call (and, for a refusal or a fault whose reason
  the agent is not told, the request id with the error's type or code alone), the audit copy, and of
  every other logger's record its name and level alone, such as `psycopg.pool WARNING` while the
  database cannot be reached. The words behind such a line are kept nowhere: a reading command on the
  same host, `ds runs` for one, prints the driver's own.
- A private CA needs `NODE_EXTRA_CA_CERTS` on Node-based clients.
- These blocks are syntax-checked only: the server is observed with the MCP library's own client, not
  with each editor. DEPLOY's acceptance checklist has one item per client, run once and dated
  ([DEPLOY.md](DEPLOY.md) section 5, items 34 to 36).

**Who an agent acts as.** Over HTTP, its token: every audit row and log line names `token:T-…`, not the
person at the keyboard. Inside MCP a caller's roles are cut to `viewer` and `operator`: a `spec_author`
token acts as an operator there, developer mode's caller too, and nothing in MCP approves, signs off,
issues a pack, closes a draw or administers. Over stdio the caller is the CLI's asserted operator
(`local:<DS_OPERATOR>@<host>`; in developer mode the OS account when `DS_OPERATOR` is unset) cut to
`--role`, `viewer` unless given.

**What is recorded.** A key view is audited as on the Key screen (`key.view`), and a compare as any
compare (`compare_one`, `run.finished`), each as the agent's token with `via: mcp:http` (`mcp:stdio`
for `ds mcp`) in its detail. A caller's other reads of a run (`get_run_summary`, `get_root_causes`,
`get_findings`) are marked once an hour per caller, tool and run (`mcp.read`, whose `calls` counts the
calls since the row before); a refused role once a minute (`authz.denied` on `MCP <tool>`). The lists,
readiness, schedules and pre-checks are not audited, as over the API. Every call leaves one line on
the server's stderr with the tool, the caller, the run, the size, the time and a request id; never an
argument or a result. These rows and lines are of MCP calls: what the same token asks of another `/v1`
route is recorded as that route records it (a key view's `key.view`, without `via`), with no
`mcp.read`.

**Limits.** `mcp.calls_per_minute` calls a minute per caller (60), 10 a minute of `compare_key` and of
`get_readiness`, and `mcp.concurrency` calls at once in a process (4), a caller holding all but one of
them at most; a call waits 10 s for its turn, then answers `busy: … call again`. A list's page that does
not fit 65,536 bytes answers `the result is over 65536 bytes: ask for fewer rows (limit)`; any other
result that does not fit is cut from the end of its longest list, and its `truncated` names the lists
cut. The limits are per process and forgotten at restart.

**What MCP is not.**

- DeltaSleuth checks no answer an external agent writes (only the built-in chat's answers are checked):
  such an answer is no run-level evidence and enters no report or pack.
- A result leaves DeltaSleuth's control: the agent's host decides where it goes, the LLM egress policy
  does not apply, and retention, purge and erasure do not reach an agent's copies.
- Masked is not anonymous: `tok:` and `class:` forms are stable pseudonyms, and names and counts are
  visible. Booleans and nulls are never tokenised, nor are the member names of an object no tokenise
  rule covers (a map keyed by data that the spec does not list shows its keys). Under
  `production_clear_declared`, the values of paths a spec declares clear (or leaves unlisted under
  `unlisted: clear`) and business-code keys travel raw as well.
- Framing does not protect an agent from text planted in payloads or catalogue names. Over HTTP
  `mcp.data_classes`, `mcp.work`, the absence of run control, the rate limits and `mcp.read` hold for
  calls made through `/v1/mcp` only. The bearer is a general API token: an agent able to read it acts
  on `/v1` with its token's full role (an operator token starts, resumes and cancels runs whatever
  `mcp.work` says; a viewer token reads clear-declared values there whatever `mcp.data_classes` says).
  Over stdio nothing of DeltaSleuth's bounds a fooled agent: it holds the OS account that runs `ds mcp`
  (in the compose wiring a docker-group member, root-equivalent on the host that holds the DSN and the
  keys), and `--role` only shapes what is offered.
- A call names a token or, over stdio, an asserted `DS_OPERATOR`; never the person at the keyboard.
- An agent's `compare_key` is a one-shot run, observed: not readiness, pack evidence or sign-off. It
  loads the targets (10 a minute per caller) and seats no pair.
- The audit log itemises key views and compares and marks a caller's other reads of a run once an hour
  (`mcp.read`); single calls are in the server log only.
- Observed with the library's own client at the pinned version; client snippets are syntax-checked
  only.
- `list_prechecks` gives each pre-check's pairs counted by verdict. Of a pre-check of one pair that is
  the pair's verdict, which under one-row filters says whether two protected values agree. No tool
  reads a pair or a verdict's line.

**When it does not work.**

| What the agent or its client shows | Why | Do |
|---|---|---|
| 404 `MCP over HTTP is off (mcp.http, docs/mvp/MCP.md §4)` | The settings do not serve MCP over HTTP | Ask the host administrators ([DEPLOY.md](DEPLOY.md) section 3, The MCP server) |
| 401 `invalid or expired API token`; or an OAuth error from the client after it | The token expired or was revoked. Nothing alerts on an expired token. DeltaSleuth has no OAuth for MCP: a client that looks for it after a 401 finds none | Put a new token in the file or the editor's secret store |
| 403 `MCP takes an API token (Authorization: Bearer dst_…), not a browser session` | The request carried a browser's cookie | Wire the client with a token |
| 403 `cross-origin request refused` | The client is a page in a browser on another origin, or sends an `Origin` of its own | Use a client that is not a browser page, or stdio in developer mode |
| 400 `unexpected Host header` | The URL names the host otherwise than the deployment's `public_url` | Use that name, or ask for yours in `auth.allowed_hosts` |
| A tool is missing, or a call answers `… is not served by this installation (…)` or `… needs the operator role` | The settings do not serve it, or the token's role is below it | `ds mcp check` prints what each role is offered, and why a tool is not served |
| 504 during `compare_key` | The proxy's read timeout passed while a target answered slowly. The compare was not cut short: its one-shot run ended and is audited | Call again later; ask for a longer proxy timeout |
| `ds mcp` through `docker compose run` gives the client malformed messages, or never starts | A TTY corrupts the framing; or Compose was not started in the installation's folder, where `.env` names both compose files | The host-channel line above as it stands: `cd` first, `-T`, and no `-f` or `--project-directory` |
| `refused: request … names the reason in the server log`, `internal error: request … names it in the server log` | The reason may name an endpoint, so it is not sent to an agent | The host administrators find the request id in the app's log |

**Stopping an agent:** revoke its token (Admin, Tokens; or `ds token revoke T-…`). Its next request
gets 401.

## Reviewing the profiler draft

`ds profile` writes `draft.spec.yaml` (commented) and `profile_evidence.csv` (every suggestion and
rejected alternative with its scores). The draft is a suggestion. Review:

1. **select**: the payload root on each side (envelope members such as `meta` are dropped).
2. **key**: `key.pattern`, `key.class` (`identifier` keeps keys tokenised; set `business_code` only
   if keys are not personal data) and `key.source`.
3. **arrays**: mode (`keyed` with its key fields, `set`, `multiset`, `ordered`). A wrong array key
   turns one difference into a cascade; check the `array` and `key_candidate` evidence rows.
4. **field alignment**: side b's `map` renames b's fields onto a's names. Every `align` row shows
   the value and name scores; `unmatched` paths need a mapping or an `exclude`. They are the `align`
   rows whose decision is `unmatched`; the Specs screen also lists them by side under a draft's notes
   (`unmatched[a]: …`, `unmatched[b]: …`).
5. **normalisers**: each suggestion cites the value classes it removes (case, whitespace, phone
   format, timestamp precision). Add only what the business agrees is "the same value".
6. **not found and truncation**: declare `notFound`, and `truncation` per side or
   `truncation_indicators` on the target (use the measured proposal).
7. **security**: list every personal-data path under `security.tokenize` with its class; anything
   not listed is tokenised by default (`unlisted: tokenize`). `security.clear` entries and
   `unlisted: clear` are the author's explicit opt-out: with SSO a run of the version needs a
   four-eyes approval of them ([Four-eyes approvals and sign-off](#four-eyes-approvals-and-sign-off));
   without SSO nothing checks them, so check them here: a path they cover with no tokenize rule is
   stored and shown raw. A tokenize rule of a PII-floor class always wins over `clear`.

Then `ds spec validate spec.yaml --targets targets.yaml` must pass.

## Reviewing the adjudication sheet

The sheet a Run screen draw downloads (`J-....csv`, [Evidence packs](#evidence-packs)) has the same
columns and rules as the P0 tool's; only its verdicts are kept, and it is scored when uploaded.

`adjudication.csv` holds up to 50 keys chosen deterministically (salted hash): every engine outcome
of the run gets at least one key, MATCH gets a reserved share (a fifth of the limit, at most its
share of the run), and root causes fill the rest, most severe and most widespread first, each by a
key whose primary cause it is where one exists. Whatever the limit could not hold is printed by
`ds adjudicate` and recorded in `adjudication.json`. Open each key on the UI's Key screen to see its
two canonical documents side by side, masked, with the differing fields highlighted.

For each row, fill `reviewer_verdict` with one of:

- `MATCH`: the two services agree for this key (format-only differences are fine);
- `DIFFERENT`: they disagree in a way the business cares about;
- `INDETERMINATE`: the evidence is not enough to decide (for example a truncated side).

Use `reviewer_notes` for anything the next reader needs. Do not edit the engine columns: the scorer
checks them against the run. Put the filled sheet back in the `--out` directory as
`adjudication.csv`, then run `ds score --out p0/`; disagreements are listed by key so the spec (or
the engine) can be corrected.

- Every row must be a key the run selected, each key once, with the run's engine columns.
- The exit counts distinct keys, and every engine outcome of the run needs at least one adjudicated
  key.
- Save the sheet as UTF-8 ("CSV UTF-8" in a spreadsheet); a plain "CSV" export in another code page
  is refused with that advice rather than guessed.
- `ds adjudicate` never overwrites a sheet that holds reviewer verdicts or notes. With `--force`
  that sheet is archived as `adjudication.<n>.csv` first. A new sheet deletes the previous
  `adjudication_score.json`, and the report fails a score whose sheet (SHA-256) no longer matches.

## Labelling against the truth

Labelled accuracy ([ACCURACY.md](ACCURACY.md) is the contract) tells a cut-over board, for the
discrepancies an ended cycle observed, how often the truth favoured side a, side b, neither, or made no
difference, per root cause. DeltaSleuth draws the sample and counts the labels people give. It never
decides which side is right, changes no outcome, root cause or rule, and judges no result: a low share
refuses nothing. Every figure is *observed*. It works in every auth mode and needs no licence (in a
vendor build, in every tier and in an evaluation); only
a labelling that a signed-in person labelled is attested by a sign-off.

**Before you draw: can labellers find the records?** The sheet holds masked keys. Labellers find a record
in the truth source through a `business_code` key, or a path the spec shows in clear (`security.clear`,
or `unlisted: clear`; a path counts only if the policy resolves it clear: a tokenise rule of a PII class
above a clear entry voids it). For a tokenised key with nothing in clear the draw is refused (422 `the key is
tokenised (class account_number) and the spec shows no path in clear: labellers cannot find records in
the truth source until the per-key reveal (AUTH §11) exists; set acknowledge_unlocatable to draw ...`).
Setting `acknowledge_unlocatable` (the Run screen offers the tick box once the server asks) draws anyway,
for labels a business rule decides without the record; the other rows end `UNDECIDABLE` (`TOKENISED`),
and every output then says that labellers could not find records.

**1. Draw** (an operator: the Run screen's "Labelled accuracy" block of the ended cycle, or `POST
/v1/runs/R-.../cycles/N/labellings`). DeltaSleuth orders the cycle's items by a salted SHA-256 with a
salt it chooses and audits with the sample:

- `size` (default 100; 0 to 1,000): discrepant items drawn uniformly. An item is one discrepant key in
  one root cause (`*` when its findings have none). The overall shares come from these items only. A
  cycle with no more discrepant items than `size` is labelled whole: exact counts, no interval.
- `per_cause` (default 2; 0 to 20): each root cause is topped up to that many items, in rank order,
  until the labelling holds 1,000 items (a warning counts the causes left without one). A root cause's
  figures come from its own items and describe it only.
- `accepted` and `agreed` (default 0; 0 to 500 each): keys a rule or mask accepted, and keys that
  matched. Their samples bound how often an acceptance or an agreement hid an error. `size`, `accepted`
  and `agreed` together are at most 1,000.
- `truth.source` and `truth.as_of`, fixed at the draw and printed with every share. The source is a
  system identifier (`CBS-PROD.account_master`: 1 to 64 letters, digits and `_ . : / -`, starting with a
  letter), never free text or a customer key. `as_of` is the date of the source's state the labellers
  use, no later than today.

Sizing: when the true share is one half, the 95% interval is 35.52% to 64.48% with 50 items, 39.83% to
60.17% with 100, 42.86% to 57.14% with 200 and 44.99% to 55.01% with 400; a label that no item got has
an upper end of 7.113%, 3.622%, 1.828% and 0.9180%. The dialog says `100 items give an interval
about ±10 points wide at worst; 400 about ±5`. Per-cause intervals, from a few items, are wide.

Keys the cycle left INCONCLUSIVE or DRIFT, keys it did not compare and rows a table scan matched by row
hash are never labelled; they are counted, and the last warned of (`4,998,916 rows matched by row hash
cannot be sampled: agreed keys are the 4,210 read per key`).

**The truth and circularity.** Name an authoritative system other than both compared sides. A source
that names a side (its target id, alone or followed by `.`, `:` or `/`, or a table side's name or
`schema.name`, ignoring case) is drawn with a warning that every output repeats: labels resting on a
compared side are circular and measure nothing. DeltaSleuth cannot see the truth source: a circular one
it does not recognise by name is the labeller's responsibility.

**A repeat, a timeout.** The draw answers with the labelling's id (`V-...`), its items by masked key
(linked to the Key screen) and its warnings. The same person drawing again with the same parameters
within ten minutes gets the first labelling back, not a second one (`labelling V-... with these
parameters was drawn by you at 10:02:11 UTC and is returned, not drawn again`): so after a proxy's
timeout or a dropped connection, when the server may have finished the draw, just draw again. The draw
itself is stopped after 300 s (504 `the draw of cycle 2 of run R-... was stopped after 300 s
(QueryCanceled): nothing was drawn; draw again with accepted and agreed at 0, or when the store is less
busy`); that stored nothing.

**2. Label, offline.** **Download sheet** (`V-....csv`) has one row per item: its number, masked `key`,
`population`, `root_cause_id` with its `path_pattern` and `change_type`, an accepted key's `rules`,
`engine_outcome`, `selected_for`, `observed_at`, the two observation ids, and four blank columns. Open
the item on the Key screen (audited `key.view`) to see the two canonical documents, find the record in
the truth source, and fill:

| `label` | The sides differ (MISMATCH, or a known bug) | Only side a holds the record | Only side b holds it | An accepted or agreed key |
|---|---|---|---|---|
| `A_RIGHT` | the truth holds a's values, not b's | it should exist, as a holds it | it should not exist | a hidden difference, a right |
| `B_RIGHT` | b's, not a's | it should not exist (a is stale) | it should exist, as b holds it | likewise b |
| `BOTH_WRONG` | neither side's | it should exist, with other values than a's | it should exist, with other values than b's | a shared error |
| `BOTH_RIGHT` | the difference does not matter | the absence does not matter | the absence does not matter | rightly equal or accepted |
| `UNDECIDABLE` | the truth at `observed_at` cannot be established | same | same | same |

- **`observed_at`, not today.** Label each item as the truth stood when the item was observed (the later
  of its two reads, else the cycle's end). `as_of` says which state of the source the labellers used; a
  draw whose `as_of` lies outside its items' observation dates warns. Where the source can no longer show
  the state at `observed_at`, the row is `UNDECIDABLE` with reason `CHANGED_SINCE`.
- **`basis`**, for every decided label (empty for `UNDECIDABLE`): `SYSTEM_OF_RECORD` (an authoritative
  system other than both compared sides), `SOURCE_DOCUMENT`, `BUSINESS_RULE` or `OWNER_CONFIRMED`.
- **`reason`**, for `UNDECIDABLE` only: `NOT_LOCATABLE` (not found in the truth source), `TOKENISED` (not
  findable because the key or its locator is tokenised), `NO_TRUTH`, `TRUTH_CONFLICTS`, `CHANGED_SINCE`
  or `NOT_REVIEWED`.
- **Several findings in one item** (the members of one root cause for that key): leave out those whose
  difference does not matter; `A_RIGHT` if the truth favours a on all the rest, `B_RIGHT` if b,
  `BOTH_WRONG` if neither or split, `BOTH_RIGHT` if none is left, `UNDECIDABLE` if the truth cannot be
  established for one that matters.
- **Every drawn item needs a label.** A partial sheet is refused and changes nothing. Mark an item nobody
  reached `UNDECIDABLE` with reason `NOT_REVIEWED`: it is counted apart, never ignored. Every share, a
  label's and its item counts too, comes with the range that counts the undecidable ones either way.
- **`notes`** are for the labellers: never stored, hashed, audited or packed (the upload itself is not
  hashed: beside the kept sheet, its hash would be one of the notes). A note may be as long as the 1 MiB
  allows; a quote left open in one swallows the rows after it, and the sheet is then refused for the
  labels it lacks. Only `label`, `basis` and `reason` are read from the upload (upper-cased); `key`,
  `population`, `root_cause_id` and `engine_outcome` must come back as drawn, and a header may name each
  column once (`the labelled sheet names column(s) label more than once`).
- Save the sheet as "CSV UTF-8", at most 1 MiB (empty the notes column if it is larger), and import the
  key column as text: a spreadsheet drops leading zeros (`engine columns differ from the labelling's
  items (lines 5) (import the key column as text: a spreadsheet drops leading zeros)`).

**3. Upload** (an operator: **Upload labels**, or `POST /v1/labellings/V-.../labels` with `text/csv`;
scripts use an operator token). The uploader is the labeller of record and answers for the people who
filled the sheet offline, whom DeltaSleuth does not record. A labelling is labelled once (`labelling
V-... is already labelled: draw a new sheet`). A sheet that fails a check (422) names its lines or
items and changes nothing.

**4. Read it.** The Run screen, `GET /v1/labellings/V-...`, `report.pdf` and the P0 summary ("Labelled
accuracy (observed)"), the sign-off's review and its evidence pack print the same lines, always whole
and followed by what they may not be read as; `ds labellings [--run R-...] [--json]` prints one line
each:

```
Labelling V-... drawn 2026-10-02 by amy, labelled 2026-10-04 by bob against CBS-PROD.account_master as of 2026-10-01, attested by sign-off A-....
Discrepancies: 120 of the 1,204 that cycle 2 of run R-... observed (one key's difference in one root cause; 1,100 keys, 23 root causes), drawn uniformly; side a is legacy, side b is new; each label is the truth when its item was observed.
Undecidable: 3 of 120 (2 NOT_LOCATABLE, 1 NOT_REVIEWED); the ranges below count them either way.
Truth favoured b (new) alone: 84 of 120 (70.00%, 95% interval 60.96% to 78.03%; 60.96% to 80.26% counting the undecidable either way: 733 to 967 of the 1,204).
...
b (new) wrong (truth favoured a, or neither): 15 of 117 decided (12.82%, 95% interval 7.356% to 20.27%; 7.167% to 22.67% counting the undecidable either way).
```

The shares describe the discrepancies this cycle observed: which side the truth favoured on those, not
whether either system is correct overall. Each interval is an exact two-sided 95% Clopper-Pearson
interval and covers sampling error only, not labelling error. A report of an ended cycle without a
labelled labelling says `Not measured: no labelled sample of this cycle.`, with how many were drawn; one
whose cycle has several labellings says so in its second line (`Labelling V-... is one of 3 drawn for
cycle 2 (2 labelled).`): an interval holds only for a sheet that was not chosen after its labels were
seen.

**Label before the sign-off request.** The request attests the cycle's newest labelling that a signed-in
person labelled (a token's or developer mode's is shown, never attested), with its hashes and counts;
from then on the cycle takes no draw and no labels (`cycle 2 of run R-... has a sign-off (A-...): revoke
it before a new labelling`), and its labeller may not approve it (`approval needs an approver other than
you: you labelled its labelling V-...`). A labeller who started the run or wrote its spec approval is
allowed, and every output warns that the labels are not independent of the run.

**Draw once.** Settle the sizes and the truth source before the first draw: an interval holds only for a
sheet that was not chosen after its labels were seen. A labelling is superseded (a wrong truth source, a
sheet that cannot be labelled) by drawing a new one and labelling it before the request, and that shows:
every labelling of the cycle stays listed, the reports count them in their second line, the sign-off
attests how many were drawn and labelled, and the evidence pack's certificate warns (`3 LABELLINGS OF
THIS CYCLE WERE DRAWN (2 LABELLED); THE NEWEST A PERSON LABELLED IS ATTESTED`). Labels uploaded through a
token are never attested, and never hidden. The reports state such a labelling when no person labelled
one (`..., not by a signed-in person: no sign-off attests it.`), and a certificate that attests none then
reads `No labelled accuracy is attested: 1 labelling of this cycle was drawn (1 labelled), none by a
signed-in person; accuracy/attempts.json lists each with its label counts.` and warns `1 LABELLING OF
THIS CYCLE WAS DRAWN (1 LABELLED); NONE WAS LABELLED BY A SIGNED-IN PERSON AND NONE IS ATTESTED`; only a
cycle none of whose labellings is labelled reads `... which side is right was not measured.`

**In the evidence pack.** A pack of a sign-off requested from migration 013 on carries the attested
labelling's labels without keys, item numbers or the sheet's order (`accuracy/labels.csv`), its score
(`accuracy/score.json`) and every labelling of the cycle (`accuracy/attempts.json`); `ds packs verify`
checks the two files against the sign-off and the labelling's audit row (`step 5 labelled accuracy: ok
(...)`). A pack reader who holds masked keys can still confirm which were drawn, and so a drawn key's
label wherever the labels of its population, root cause and `selected_for` are all alike
([ACCURACY.md](ACCURACY.md) §9).

**Purge.** With its cycle a labelling loses its sheet and drawn items and keeps its hashes, counts,
truth source and score; its draw, sheet download and upload then answer 410.

## Exit criteria

P0 is done when:

1. **At least 50 adjudicated keys agree with the reviewers (100% agreement).** `ds score` reports
   `P0 exit: PASSED`.
2. **Profiles are signed off**: the spec, the targets' capabilities (from the measured proposal) and
   the LLM profiles (`ds llm-probe`).
3. **The open items the report lists are answered** by whoever owns the comparison.

The report's checklist tracks the first two criteria (`pass`, `fail`, `pending`, `manual`), and the
report lists the open items: unresolved keys, known diffs in use, measured behaviour that needs a
decision (for example an unannounced not-found shape) and NORMALIZATION_ONLY fields. A run that
stopped early or cancelled keys is marked PARTIAL in the report's title and banner, and its ingest
check fails.

## Measured capabilities

`ds measure` proposes a `capabilities` block with the evidence behind each line; every proposal
still needs a person to confirm it against the service's documentation:

- **Authoritative absence** is proposed only from the answers to keys known not to exist
  (`--missing-keys`), and only when they all share one 404/410 body shape; the proposed shape
  predicate matches that shape exactly (its member names and count), so a gateway's or a proxy's
  404 cannot satisfy it. An empty 404 body is never proposed (it could only be declared by status,
  which would accept every 404). Several not-found shapes are listed with their counts.
- **Truncation**: a cap is proposed only when every document whose reported count exceeded its
  array returned the same number of items and none returned more; page-size, limit and page-counter
  fields are never taken for a total.

## Security and privacy

- Credentials come only from `file:` references (settings, targets and LLM profiles); environment
  variables are never read for credentials. Error messages and outputs never contain secrets or
  payload values.
- `ds serve` needs the `auth` settings unless it listens on loopback only. Without them it runs in
  developer mode and warns: every local user and process acts as admin. **Never put a reverse proxy
  or a port forward in front of developer mode:** it refuses requests with proxy headers, but one that
  adds none (a bare `proxy_pass`, `ssh -L`) cannot be detected.
- With SSO, runs of spec versions that expose values, suppress findings or filter what is compared need
  a four-eyes approval, and approving needs a sign-in within 15 minutes; tokens and developer modes
  label such runs unapproved ([Four-eyes approvals and sign-off](#four-eyes-approvals-and-sign-off)).
- With SSO, tokens, users and sessions are managed on the Admin screen. The CLI, which acts with every
  role under an asserted `DS_OPERATOR`, is break-glass: for recovery, by host administrators.
- A leaver's tokens outlive their removal from the IdP: disable them with their tokens
  (`ds users disable U-... --revoke-tokens`, or the Admin screen).
- A sign-in that fails before the person is known, and a refused token, write nothing to the
  database: the server log records them with the request id.
- Outputs are masked with the tokeniser key. Raw values are stored encrypted: response bodies in
  the evidence directory (owner-only directories 0700 and files 0600), and raw keys, headers, scan
  resume points and chat sealed in the database, under keys the keyring outside both wraps. They go
  with their cycle at the end of its retention ([Retention, purge and keys](#retention-purge-and-keys)).
- Loopback, private and link-local addresses are refused unless the target sets
  `allow_private_network: true` (SSRF guard); requests never leave the target's origin. A database
  target connects only to the host its DSN names. Notification channels are guarded the same way, every
  answer of each name checked at each connection: link-local (the metadata address among them),
  unspecified and multicast addresses never, private ones only with the channel's
  `allow_private_network`; redirects are never followed.
- The app's outbound destinations are the targets, the LLM hosts, the IdP (or `auth.oidc.proxy`) and the
  notification channels you declare (or `notify.proxy`, for channels with `proxy: true`). A Slack or
  Teams webhook URL is a secret file bound to its declared host, never logged, stored or shown, and a
  notification carries ids, counts, closed codes, a spec name (a label) and a link only.
- The MCP server adds no outbound destination and sends a client no request, and no tool takes a URL,
  a host or a path. Where `mcp.work` lists it, `compare_key` reads the run's two declared targets as
  any compare does, at most 10 times a minute per caller. What a tool result holds is what the chat's
  tools return, cut to the data classes `mcp.data_classes` lists; a result then leaves DeltaSleuth's
  control, since the agent's own model host is not under the LLM egress policy below, and no
  retention, purge or erasure reaches an agent's copies. Masked values are stable pseudonyms, not
  anonymous. Those cuts, `mcp.work`,
  the limits and the `mcp.read` rows hold for calls made through `/v1/mcp` only: the agent's bearer is
  a general API token, so an agent that planted text has fooled, and that can read its token, acts on
  `/v1` with the token's full role (an `operator` token starts, resumes and cancels runs; a `viewer`
  token reads clear-declared values there). One that runs `ds mcp` holds the OS account that runs it
  ([Your own agent (MCP)](#your-own-agent-mcp)).
- LLM egress is fail-closed: a profile receives only the data classes its trust level and the
  running environment allow. The chat sends tokenised production findings, so only a profile on an
  internal host can serve it; of a labelled cycle it sends the stored counts, shares and the truth
  source's identifier, and what kind of actor labelled, never a name, a user id or a token id. Chat
  answers cite only ids the tools returned; an answer that cannot be verified is withheld.
- Chat questions are stored and sent to the model as typed (nothing masks free text): refer to keys
  by their masked form and do not type personal data.
- Releases are signed: the office verifies each bundle's signature, files and image with OpenSSL and
  `sha256sum` against the key and procedure its register filed, before `docker load`. Each carries its
  SBOMs, its scan and the maintainer's triage as VEX, and SECURITY.md (at the repository's root and the
  bundle's) says how to report a vulnerability, how the maintainer answers, and how to scan a release again.
- The container runs as uid 10001 on a read-only root filesystem, with no capabilities and no privilege
  gain; it writes only to its volumes and `/tmp`.
- Exported files a files target declares are read in place, from a read-only mount, by DuckDB in a
  child process of each `ds` process, the file reader: no connection leaves it for them, and DuckDB is
  locked to the declared directories and its spill ([From files](#from-files)). A fault or a memory
  blow-up of DuckDB's ends that child alone; the service and the worker go on, and files that keep
  ending it are read no more ([READER.md](READER.md)). The reader is not a sandbox: it runs as the same
  user as the service, and DeltaSleuth's own check of a Parquet footer and of a compressed stream still
  reads a file's bytes in the process that holds the keyring. On Linux the reader sets itself a data
  limit and denies itself sockets, unless `files.os_limits` is false; its start's log line says what
  held. A sort's spill is plaintext on the evidence volume for the length of a
  statement. Each matched file's name, size, times and own digest are sealed under the run's data key
  and sent nowhere; the spec's own path, the count of a side's files, their total bytes and the newest
  modification time are in the run's plan and its evidence pack in clear (of a side of one file, that
  file's size and time), with one SHA-256 over all the side's files. A writer of the export directory on
  the host can make a read take other bytes than the pinned ones: a swapped file fails the run at the
  next check, a parent directory swapped and put back between two checks is seen by no check
  ([FILES.md](FILES.md) §14 point 4).
- No licence exists in this build, which enforces none and calls out for none. A vendor build's
  licence never phones home either: it is a signed file in the database, checked offline against the
  vendor keys the build embeds, and it never blocks sign-in, reads, reports, exports, purges or the
  audit log ([Licence](#licence)). It carries the customer's name and order, which the audit log holds
  only in its `licence.install` row, and evidence packs in their manifest and certificate; a run's record
  and its reports never do.

## Troubleshooting

| Message or symptom | What to do |
|---|---|
| `no settings file: pass --config or set DS_CONFIG` | Pass `--config deltasleuth.yaml` or export `DS_CONFIG`. |
| `cannot read <path>: No such file or directory`, `No such file or directory: <path>` or `sample root '<path>' does not exist`, ending `(ds runs in a container, where this path is /app/<path>: a file or folder of the host must first be copied in, with docker compose cp, or mounted)` | `ds` ran in a container (`docker compose run … app ds`) and read the path there, not in the folder the command was typed in. Copy the file or folder into the `p0` volume and give that path ([DEPLOY.md](DEPLOY.md) section 9). A build of a checkout sees its `mocks/` and `examples/` at their repository paths. Outside a container the message ends before the bracket: check the path. |
| `tokeniser key ... not found; run 'ds init' first` (or `keyring ...`) | Without the `encryption` settings: run `ds init` with the same settings. |
| `tokeniser.key was moved to encryption.tokeniser_key_ref: delete it from the workspace` | With `encryption`, the workspace must not hold a tokeniser key: delete it once it is in the secret and the vault (DEPLOY.md section 10). |
| `legacy plaintext remains: stop the app and run 'ds keys migrate' (docs/mvp/RETENTION.md §7.2)` | The store predates encryption at rest: stop the app and run `ds keys migrate` (DEPLOY.md section 10). |
| `the database is unreachable` | Check `database_dsn_ref` and that the database accepts connections. The command waited 30 s for it; the driver's own line above it, printed once for each of its wordings, says what it met: it names the host and the port, and a server's refusal the account, the database and the client's address, never a piece of the password. A wrong password on a server that takes TLS connections only ends here too when the address has no `sslmode`: write `sslmode=require` (or `verify-full`) in it ([FIRSTRUN.md](FIRSTRUN.md) §14). |
| `the database answered and asked for a password, then refused the connection: check the user, the password and the database name in the file database_dsn_ref names; if they are right, the server, the role or the database is at its connection limit, or a pooler cannot reach its server` | The server was reached and refused the sign-in after the password: said by every command that opens the store, after three tries two seconds apart (at once when the address holds no password). Correct the user, the password or the database name in that file. If they are right, the server, the role or the database is at its connection limit, or a pooler in front of it cannot reach its server: the driver gives no code that tells these apart, and its own words are never printed, so ask the database's administrator which it is. A limit that frees within the three tries is not reported. |
| `the file database_dsn_ref names does not hold an address the driver can read: percent-encode special characters in the user and the password` | The address is none the PostgreSQL driver can parse, or its user or password holds an unencoded `@` or `/`: said at once, before any connection, by every command that opens the store, and no piece of the address is printed. Write it as one line, `postgresql://<user>:<password>@<host>:<port>/<database>`, with `%40` for `@`, `%2F` for `/`, `%25` for `%` and `%20` for a space in the user and the password. |
| `cannot listen on 127.0.0.1:8000: the port is in use; stop the program that holds it, or pass --port` from `ds serve` | Another program holds the port, often another `ds serve` or an installation in Docker: stop it, or pass `--port` (with `auth`, `public_url` names the port people type). Any other reason reads `cannot listen on <host>:<port>: <the system's words>`. Nothing was opened: no `system.start` row, no worker. |
| `warning: the web UI is not built (<checkout>/web/dist is missing): / answers 404 and only the API under /v1 is served; …` from `ds serve`, and 404 `the web UI is not built on this server; its API is under /v1` at `/` | A source checkout whose UI was never built: run `npm --prefix web ci` and `npm --prefix web run build` in the checkout, then start `ds serve` again. The API works meanwhile. |
| `N key(s) do not match key.pattern after key.normalize, or are over 1024 bytes or hold NUL (positions ...)` | Fix those lines of the key list, or the spec's `key.pattern` / `key.normalize`. Keys are never echoed. For a multi-part key the line reads `N key(s) are not 3 parts joined by \|, or hold a part that fails key.pattern after key.normalize, ...`: write each key as its parts in `key.parts` order joined by `\|`. |
| `the key source holds N row(s) without a key: leave them out with table.where`, `the key source holds N line(s) that are not rows of its declared CSV dialect: fix the export or its csv declaration`, `the key source holds over 1,000,000 keys: narrow it with table.where`, `the key source could not be read (<code>)` (or `timed out after 3600 s`, `could not be held in one snapshot`, `could not be read: the target refused the credential`), or a line that starts `the key source:` | The spec's key table (`key.source` with `kind: table`) did not give the run its keys, and no run was created. Filter the rows out or narrow the table with its `where`; for a code, check the target as for a table side (`ds target check`). For lines that are not rows (a CSV key file's, each of which would be a key left out), the server log says where they are (`the key source: N line(s) of its files are not rows: file 1 line 5: columns; ...`). At a rolling soak's cycle end the run keeps its previous keys instead (`rolling_resolve_failed`), and a refused credential pauses it (`run_paused_auth_error`): fix the credential file, then resume. |
| `[unsupported]` | The spec uses a deferred feature (graph mode, a table side's `session` or `collation`, SQL templates, a `lookup` key map, frozen or watermark time, hash sampling, a `sql`/`csv`/`union`/`hash_sample` key source, noise calibration or masks, ...); the message names it. Remove it. |
| `/normalizerVersion [unsupported]` | The spec was written for other normalisers than this build's (the message names both). Set the build's version (what `ds profile` writes), re-check any ground truth that depends on normalisation, and save the spec again (a new content hash). Stored specs keep showing their runs; only running them is refused. |
| `[ignored]` | Accepted but not applied (report formats and fields, `time.skewBudget`, `inferredSchema`, the domain pack's config, ...). Not applying it never turns a difference into a MATCH. `schemaDrift` is applied to table sides ([When a table changes under a run](#when-a-table-changes-under-a-run)); between two service sides `mode: pause` is refused and `finding` does nothing. |
| `[inline_keys_stored]` | The spec lists keys inline, which are stored in clear with it and never purged. Pass them with `ds run --keys` instead ([Retention, purge and keys](#retention-purge-and-keys)). |
| `[key_pattern_empty]` | The keys are of a value class (`key.class` other than `business_code`) and `key.pattern` is missing (two tables need none) or matches the empty string, so an evidence pack cannot tell a raw key from other text: no pack is issued for any run of this spec, however it is signed off (`spec 12's key.pattern matches the empty string: a raw key cannot be told from other text, so no pack`). The spec is valid and runs. Before a run meant for sign-off, upload a new version whose pattern fits its keys only (`^[0-9]{6}$`), or class keys that are not personal `business_code`. |
| `[truncation_undeclared] (X33)` | Declare `sides.<side>.truncation` or the target's `truncation_indicators`; `ds measure` proposes one when a count exceeded its array. |
| `[not_found_undeclared]` | The spec's `notFound` status must be declared authoritative by the target (`capabilities.authoritative_absence`; for an endpoint declared in the product, "A 404 means no such record"). |
| Many `INCONCLUSIVE absence_unconfirmed` | The 404 body does not match any declared absence shape. `ds measure` lists the not-found body shapes and flags unannounced ones; declare only the shape the service documents. |
| `INCONCLUSIVE truncated` | The response reported more items than it returned, or a flag the side declares (`truncation.flagPaths`) said the answer was cut: retry.csv's detail then reads `the truncation flag <path> is true`, or `is not a boolean` for a flag that is neither `true` nor `false`. These keys are listed as `api_uncomparable` and are not compared, so a cut answer is never a difference; compare them with pagination or out of band. This is what a spec shows whose `recheck` says `outcomes: [MISMATCH, NOT_FOUND_A, NOT_FOUND_B]`. Under the default `recheck.outcomes`, which holds INCONCLUSIVE, a cut key is read again and reads `INCONCLUSIVE recheck_failed` with `api_uncomparable` 0: reading it again lifts no cut, so give such a spec that `outcomes` line. A flag path that selects nothing detects nothing: check its spelling against a real answer. |
| The run is `paused` with `run_paused_auth_error` | A 401/403 paused it. Fix the credential file, then resume the run (the file is read again). For an endpoint declared in the product, `Change` it on the Targets screen, type the credential and save, then resume. |
| `the stored credential of target <id> cannot be read: enter it again` (Send, a compare, or a failed run's error) | The file of a credential typed into the product is gone. Restart a `ds worker` or `ds mcp` that started before the credential was replaced; otherwise `Change` the endpoint on the Targets screen, type the credential and save ([Endpoints declared in the product](#endpoints-declared-in-the-product)). |
| `error: … (the product's list, <path>)` when `ds serve` or any `ds` command starts | The list of endpoints declared in the product does not load. Remove it and declare them again ([Endpoints declared in the product](#endpoints-declared-in-the-product)). |
| 409 `target <id> is read by N unfinished run(s): stop them first`, or `target <id> is declared in the targets file: change it there` | An endpoint that unfinished runs read is neither removed nor moved: let them end or cancel them (its credentials and its budget change under them). An id of the targets file is changed in the file. |
| The run is `paused` with `run_paused_key_missing`, or 503 `evidence unreadable: the keyring lacks K-...` | The keyring lacks a KEK the run's keys need: restore the line from the vault, restart, then resume the run ([Retention, purge and keys](#retention-purge-and-keys)). |
| 410 `cycle N of run R-... was purged on ...` | The cycle's retention ended, or it was erased: its raw data is deleted. The run's counters and retention stay (`GET /v1/runs/{id}`). |
| 410 `the run's evidence was purged on ...; AI answers need it` | The chat needs the run's current cycle, which is purged. |
| 410 `evidence of O-... is missing (purged or lost)` | The blob is gone from the evidence directory: restore it from a backup (DEPLOY.md section 10). |
| `another purge is running; try again later` | The worker's hourly pass or another `ds purge` is running: try again in a few minutes. |
| `run R-... is held`, `... erase the whole run` or `... still has keys in flight; try again in 60 s` from `ds purge --run` | See Erasure in [Retention, purge and keys](#retention-purge-and-keys). |
| `completed_with_issues`, `stop_condition:<name>` | An `execution.stop` condition ended the run early. Fix the cause, then run the keys retry.csv lists. |
| retry.csv reason `recheck_owed` | A discrepant key did not get the second read that confirms it (the run stopped first, or its first read failed). Run it again. |
| INCONCLUSIVE `unparseable`: "the captured sample on side ... is malformed" | That key's sample (or its meta file) is invalid; fix or re-capture it. The rest of the run is unaffected. |
| A run `failed` with `cannot read secret file ...` | A target's credential file is missing or unreadable (in Docker: not mounted, see DEPLOY.md section 3). Fix it, then start a new run. |
| A run `failed` with an error type only | A bug in DeltaSleuth, or a value the database refused (for example `DataError`): the worker log has the details. The keys compared so far stay in the database. |
| `another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops` | One worker a database processes keys. Under `ds run`: `ds serve` or `ds worker` processes the run, and `ds run` waits for it to end (`--no-wait` returns at once). Under `ds serve` or `ds worker`: the other process does the work, and this one waits for the lock. |
| A message naming a MySQL or Oracle target or code (`cannot connect to target … (oracle …)`, `MySQL 1142`, `ORA-01555`, `DPY-6001`), `SIDE A NOT READ IN A SNAPSHOT`, `KeysOutOfOrder` | See [Across engines](#across-engines) for the messages of a MySQL or Oracle table side, and `ds target check ID` to repeat a target's connection check without a run. |
| A message naming a file by its position (`side a: file 3 of 12 …`), a files target's directory, `FilesChanged`, `RowsMoved`, `ReaderDied`, `lines of its files are not rows`, `the file reader did not start`, `the file reader's process ended` | See [From files](#from-files) for a file side's messages, [DEPLOY.md](DEPLOY.md) section 11 for the mount and the spill, and `ds target check ID --table PATH --format csv|parquet` to see the files and the lines that are not rows. |
| exit code `3`, `internal error` | A bug in DeltaSleuth. Keep the traceback for the report. |
| `adjudication.csv is not UTF-8 text` | A spreadsheet saved the sheet in its own code page. Save it as "CSV UTF-8" and score again. |
| 422 `the labelled sheet is not UTF-8 text ...`, `engine columns differ from the labelling's items ...` or `every drawn item needs a label ...` | The filled labelling sheet: save it as "CSV UTF-8", import the key column as text, and label every item (`UNDECIDABLE` with reason `NOT_REVIEWED` for one nobody reached). Nothing was stored: upload it again ([Labelling against the truth](#labelling-against-the-truth)). |
| 409 `labelling V-... is already labelled: draw a new sheet`, or `cycle 2 of run R-... has a sign-off (A-...): revoke it before a new labelling` | A labelling is labelled once, and a cycle takes no draw or labels once its sign-off is requested: draw a new labelling, or revoke the sign-off first. |
| 504 `the draw of cycle 2 of run R-... was stopped after 300 s (QueryCanceled): nothing was drawn; ...` | The labelling's draw ran too long: draw again with `accepted` and `agreed` at 0, or when the store is less busy. |
| `... already holds reviewer verdicts or notes` from `ds adjudicate` | The reviewed sheet is the exit evidence: score it, or pass `--force` to archive it (`adjudication.<n>.csv`) and write a new one. |
| `ProbeAborted`: `the baseline request ... failed` from `ds llm-probe` | The endpoint rejected a plain request, so no feature was probed: check `base_url` and `model`. A strict OpenAI-compatible gateway may reject optional Chat Completions fields; declare what it accepts in the profile's `wire` block (below). |
| `rate_limited` or `timeout` reasons | Lower `rate_per_s` / `max_concurrency` on the target, or raise `execution.timeouts`. |
| Refused private address | Set `allow_private_network: true` on that target (internal endpoints only). |
| Many NORMALIZATION_ONLY rows | Expected when formats differ or a tolerance accepts values; they never make a key discrepant. Review `normalization.csv` to confirm they are acceptable. |
| `PolicyDenied` from `ds llm-probe` | The profile's trust and data classes do not allow the environment; see the profile's `environments`, the host trust registry and `--environment` (`eval` for an external profile). |
| `set DS_OPERATOR to your user name (docs/mvp/AUTH.md §4.2)` | The settings have `auth`, and the command writes audit rows: `export DS_OPERATOR=<your user name>` (in Docker, the alias of DEPLOY.md section 9). |
| `DS_OPERATOR must be 1 to 256 characters without control characters` | Set it to your plain user name. |
| `serving on ... needs the auth settings (docs/mvp/AUTH.md §1)` | Without `auth`, `ds serve` listens on loopback only (developer mode). To serve others, add the `auth` settings (DEPLOY.md section 3). |
| `api_token_ref: replaced by named API tokens; ...` | The settings predate named tokens: replace `api_token_ref` with the `auth` settings (DEPLOY.md section 10). |
| 401 `not signed in` or `session expired` | Sign in again. A session expires when idle, at its maximum age, when revoked or disabled, and for everyone when an admin edits the role mapping. |
| 401 `invalid or expired API token` | The token is wrong, expired or revoked: `ds token list` or Admin, Tokens shows its state. The server log names its id. |
| 403 `needs the <role> role` | Your IdP groups, or the token, do not grant that role (see Roles). Ask an admin; a token holds at most `spec_author`. |
| 403 `the <role> role needs a signed-in person` | `rule_approver` and `pii_reader` are for people signed in through the IdP only. |
| 403 `reauth_required`, "Sign in again to create a token" | The last sign-in is over 15 minutes old: use the button, which signs you in again. |
| 403 `missing or invalid CSRF token` | A script sent a browser session's cookie: use an API token instead. In developer mode, scripts send `X-CSRF-Token: dev` on POST and DELETE. In the UI, reload the page. |
| 403 `cross-origin request refused` or `cross-site request refused` | A write, or a read, came from another site's page, for example a link to a `/v1` report URL on an intranet page; open it from the app instead. API clients send neither header (curl and CI tools send none). |
| 400 `unexpected Host header` | The request's `Host` is not the `public_url` host: call `public_url`, or have its name added to `auth.allowed_hosts` (DEPLOY.md section 5). |
| 403 `developer mode serves loopback clients only` or `developer mode refuses proxied requests` | Developer mode serves this machine only, never through a proxy or port forward. |
| 404 `token sign-in is not enabled` | Token sign-in exists in tokens mode only; with SSO, sign in with SSO, and send tokens as a bearer. |
| A sign-in error page with `Quote Q-...` | Send the request id to your administrator: the server log has the reason on it (DEPLOY.md section 11, Sign-in). |
| 409 `you cannot disable yourself` | Another admin must, or a host administrator with `ds users disable`. |
| 409 `spec 12 (...) needs a four-eyes approval of clear, ... before it runs`, or `... needs a new four-eyes approval: A-... was revoked` | Request the version's approval on the Specs screen and have a `rule_approver` who did not write it approve it; then start or resume the run ([Four-eyes approvals and sign-off](#four-eyes-approvals-and-sign-off)). |
| `... needs a new four-eyes approval: A-... covers clear; this build also needs raw_clear` | This build gates a scope the approval predates: request the version's approval again. |
| `spec 12's stored body does not re-hash to its content hash (...): upload a changed version (a new version)` | The stored body was edited, or could not be stored as uploaded. The same file maps to the same row: upload a changed version (bump `metadata.version`) and have it approved. |
| `spec ... has no approved upload: upload it on the Specs screen, signed in, and have it approved ...` from `ds run` or `ds compare` | With SSO the CLI runs only an uploaded version: upload the file on the Specs screen and have it approved. |
| 422 `spec body holds NUL at /...`, `... holds -0.0 at /..., which the store keeps as 0: write 0` or `... holds 1e+16 at /..., which the store keeps as 10000000000000000: write 10000000000000000` (also from `ds run`, exit 2) | The store cannot keep that value as uploaded, so its approval would not match: fix the spec as the message says. |
| 422 `spec 12's stored body differs from this upload at /... (edited, or not storable as uploaded): upload a changed version (a new version)` | The stored version of this content was edited: upload a changed version and have it approved. |
| 409 `approvals need SSO (auth.oidc); this deployment runs in tokens mode` | Tokens and developer modes have no approvers; their runs are labelled unapproved. |
| 409 `approval needs an approver other than you: ...` | You wrote, uploaded, started or requested what is approved (the message says which): another `rule_approver` must approve it. |
| 409 `you approved step 1 of A-...; another rule_approver must approve it` | The request needs two approvers and you took the first step. |
| 409 `step 1 of A-... was taken by ..., who is now an author of it; reject it and request it again` | The first approver joined the exclusion set since (a revoke moved the lineage back, or they uploaded the version): nobody can take step 2. Reject it and request it again. |
| 409 `run R-... started in tokens mode: only a run started under SSO is signed off` (or `developer mode`) | The run started without approvers; resuming it under SSO does not change that. Start a new run under SSO. |
| 409 `cycle 2 of run R-... scanned only part of its tables (...)` | Its table scan stopped before the end (`until`, `maxDuration` or a cancel): sign off a cycle whose scan completed. |
| 409 `the reviewed content hash or scopes are not spec 12's; reload and review again`, `spec 12's needs changed since the request; request it again`, or `cycle 2 of run R-... changed since the sign-off was requested; request it again` | What you reviewed is no longer what would be approved: reload the review, or request again. |
| 403 `reauth_required` on Approve | The last sign-in is over 15 minutes old: use Sign in again on the review. |
| `warning: no IdP group maps to rule_approver or above: nothing can be approved` | Map an IdP group to `rule_approver` in `auth.oidc.roles` (DEPLOY.md section 5). |
| The run is `paused` with `run_paused_approval` | Its spec approval was revoked, or a soak reached a cycle boundary without the approval its version needs: have the version approved, then resume. |
| 503 `evidence packs are not configured: set packs.signing_key_ref (docs/mvp/PACK.md §3.1)` | Add the `packs` settings and the signing key (DEPLOY.md section 3), then restart. |
| `packs.signing_key_ref: expected an unencrypted PEM EC P-256 private key (docs/mvp/PACK.md §3.1)` | Generate the key as DEPLOY.md section 3 shows (`openssl genpkey ... ec_paramgen_curve:P-256`). |
| 409 `cycle 2 of run R-... is not a census: ...` | The pack cannot say over which population of side a parity was observed: see Census in [Evidence packs](#evidence-packs). A key census that failed or is pending covers no pack for that cycle. |
| 409 `the attested adjudication J-... did not pass (...): no pack` | Revoke the sign-off, draw and score a new sheet (fix the spec or report the engine first), then request the sign-off again. |
| 409 `sign-off A-... was revoked meanwhile` | The sign-off was revoked while the pack was built: nothing was stored. |
| `error: row N (...) does not hash to its chain entry: ...` from `ds audit verify` | The audit log changed after it was written: compare the row with its SIEM line, and issue no pack until it is explained ([Evidence packs](#evidence-packs)). |
| `warning: Unlicensed evaluation of installation I-...: at most 2 pairs (...)` | A vendor build's, like the next three rows: this build needs no licence, warns of none and refuses nothing for one. There, no licence is installed: request one with `ds licence id` and install it ([Licence](#licence)). |
| 409 `SSO (auth.oidc) is an Enterprise feature and licence LI-... is pro: ...` (or `... an unlicensed evaluation: ...`) | Under SSO, a vendor build's runs, compares and soak cycles need an Enterprise licence. Otherwise run in tokens mode, which means removing `auth.oidc` and the `packs` block too (`packs: needs auth.oidc`); with `packs` set, the message says `keep SSO, which evidence packs need` instead. |
| 409 `spec ... would be pair 6 of 5 under licence ...`, `... would be a third pair: ...` or `spec ... is not one of the 5 pairs licence ... seats ...` | The licence's pairs are taken: run a seated name (`ds licence`), or install a licence with more pairs ([Licence](#licence)). |
| 409 `licence LI-... ended on ... and its 30 days of grace on ...: ...`, or the run is `paused` with `run_paused_licence` | Install the renewal (`ds licence install`), then resume the paused runs on their Run screen. In this build nothing is installed: a run an older build that enforced the licence paused so resumes on its Run screen. |
| The run is `paused` with `run_paused_draw` | Its frame refused the draw (the Run screen and `ds run` say why: an incomplete or shifting key listing, a label no bucket takes, a sign-off bucket without keys, an empty frame, over 1,000,000 keys). Fix the listing, the data or the spec (a new version), then resume: the worker draws again with a new nonce ([Sign-off sampling](#sign-off-sampling)). |
| The run is `paused` with `run_paused_sample`, or 409 `run R-... paused after draw D-... did not pass: close it with the fix ...` | The previous cycle's `bottom_k` draw did not pass. Fix the cause, close the draw with a note of the fix (Run screen, "Record the fix and close failed draws"), then resume. |
| 409 `spec ... samples the population of live sampled run R-... (...)` | Another `bottom_k` run, under any spec name, is live over the same table or listing: stop it first. |
| 409 `draw D-... (...) failed before draw D-... was made and was not closed before it: ...` | An earlier draw of the population did not pass and was closed only after this one was drawn, or never: close it with the fix, then sign off a draw made after the close. |
| `this build embeds no vendor key: it needs no licence and can verify none (...)` from `ds licence install` or `show` | This build needs no licence and reads no file: nothing to do, and nothing was written. |
| `... is not a DeltaSleuth licence ...`, `... does not verify with its key ...`, `... is for installation I-..., not this one ...` from a vendor build's `ds licence install` | See DEPLOY.md section 11, The licence. Nothing was written. |
| `Read-only file system` from a command in the container | The container's root filesystem is read-only: write into the `p0` volume (DEPLOY.md section 9) or `/tmp`. |
| `No such image: deltasleuth:...` from `docker compose` | Compose never builds or pulls the image: install the release (DEPLOY.md section 2), or build from source. |
| A release's verification prints anything its procedure does not expect | Stop and load nothing: DEPLOY.md section 11, Installing a release. |

### OpenAI-compatible gateways

An `openai_chat` profile sends the optional Chat Completions fields the OpenAI API accepts. A
gateway that rejects some of them is described in the profile's `wire` block (`openai_chat` only):

```yaml
wire:
  max_tokens_param: max_tokens        # default max_completion_tokens
  send_store: false                   # default true (sends store: false); internal hosts only
  send_parallel_tool_calls: false     # default true
```

The adapter changes only what the wire block names; nothing is retried without a field
automatically.

## Try it on the mock pairs

The repository ships two black-box mock pairs (`mocks/linker`, `mocks/customer`) with seeded
discrepancies and their ground truth (`expected.json`). The end-to-end tests run them through the
CLI, the API and the worker; to see it by hand, with a settings file whose `targets_file` is
`mocks/targets.yaml`, which holds the four targets of both pairs (Setup's developer example names it):

```bash
uv run ds init && uv run ds migrate
uv run python -m mocks.fixtures linker --out demo/linker-samples
uv run ds run mocks/linker/spec.yaml --samples demo/linker-samples

uv run python -m mocks.run all &                     # both variants of both pairs, on 127.0.0.1:18081, 18082, 18091 and 18092
uv run ds run mocks/linker/spec.yaml                 # live: prints the run id R-...
uv run ds compare ORG-000007 --spec mocks/linker/spec.yaml
uv run ds measure R-... --out demo/p0
uv run ds adjudicate R-... --out demo/p0
uv run ds report R-... --out demo/p0 --kind summary --kind report.pdf
```

The Specs screen's "Draft a spec from sample pairs" takes one file, not a folder: `uv run python -m
mocks.fixtures customer --pairs demo/customer-pairs.json` writes it for a mock pair (`wrote 40 pairs for
customer to demo/customer-pairs.json`), a JSON array of `{"key": …, "a": …, "b": …}` with a line for each
key, a side being its recorded body, or null where its recorded status is not 2xx. The same array is the
`pairs` of `POST /v1/profile`. `--out` and `--pairs` may be given together; with neither the command
prints its usage and `python -m mocks.fixtures: error: give --out DIR, --pairs FILE or both` (exit 2).

The mock services count calls per key (the drift key answers stale once, the 5xx key fails once).
Restart them, or `POST /__reset` to each, before another live run. `python -m mocks.run all` prints the
four addresses, stops all four at Ctrl-C (or a TERM), and starts none when one of the ports is taken
(`error: port 18081 is in use (linker a): nothing was started`, exit 2); one variant alone is
`python -m mocks.run linker --variant a --port 18081`.

No licence is needed and no pair is counted ([Licence](#licence)): the linker pair, the customer pair,
the table demo, the graph demo and your own specs all run on one database, with no licence warning and
no label on a run or its report files. (In a vendor build a database without a licence is an unlicensed
evaluation: `ds run` and `ds compare` warn so, the runs and their report files are labelled, and it runs
two pairs, ever; a third name is refused, `spec ... would be a third pair: an unlicensed evaluation runs
2 pairs (...)`, and a fresh database, another DSN, is a fresh evaluation.)
