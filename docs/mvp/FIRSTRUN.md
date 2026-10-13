# First-run fixes: contract

This contract is binding. It fixes what a first-time user
meets between `git clone` and a signed-in installation. It amends [AUTH.md](AUTH.md) §3 with one
exception (§7) and the documents of §11. It adds
no migration, dependency, setting, route, audit action, licence fence or outbound
destination.

**Goal:** `git clone`, then `sh deploy/up.sh`, gives a running trial and a saved first token on a Mac or
a Linux machine, and the same steps by hand need no deletion and no `sed`. **Shape:** example files that
start as copied (token sign-in, AI off, four secrets); a `.env` example for a source build; one POSIX
script that refuses whatever is not a trial of its own folder; a developer settings example and one
command for the four mock services; two sign-in hints made in the browser; cookies that a plain-http
loopback address keeps; eight help texts and six traps. [DEPLOY.md](DEPLOY.md) stays the office procedure.

**Checked on 2026-10-05** (macOS, Docker 29.1.3, Compose 2.40.3, curl 8.2.1, shellcheck 0.9.0, dash,
psycopg 3.3.6 with libpq 18.0, psycopg_pool 3.3.2, typer 0.27.2).
- *Observed:*
  - §2's settings example, built in a scratch folder, loads as copied (`tokens`,
    `http://localhost:8000`, no `trusted_proxies`, AI off); with the head's lines on (`oidc`,
    `172.30.0.1/32`, `DS.Admin`); with every block; and with the one rule applied to the whole file.
    The optional section alone is refused with the packs line.
  - §2's compose files render (`docker compose --profile mocks config`) as `deltasleuth`,
    `deltasleuth:source`, port 8000, one `build`; with a project line and §3.3's generated site file as
    `dsprobe55:source`, port 18044, `172.31.7.0/24`. A quoted `COMPOSE_PROJECT_NAME="dsq"` renders `dsq`.
    `ds-` and `a___b` pass Compose and give `ds-:source` and `a___b:source`, which Docker answers with
    `invalid reference format`. With the image absent, `docker compose --dry-run --profile mocks up -d`
    builds `app` once and then creates both containers.
  - §3.2's commands, read-only, against this machine's two installations, but for U17's, which was
    changed as built and is not observed; `curl` on a closed port with `http_proxy` set (exit 7 with
    `--noproxy '*'`), on an open port (0) and on one that accepts and says nothing (28). As built, the
    tests run the real `curl` with U18's words against a closed and an open local port, with every proxy
    variable naming a local listener: 7 and 0, and the listener is asked once, by the open port's call.
  - §3's read rules, version comparison and trap in a prototype: clean under `shellcheck -s sh`; under
    `dash` and `sh` an INT or TERM ends it with 130 or 143 and leaves no `.new` file.
  - §8 S3's flags, against the tests' `postgres:16-alpine`: a wrong password, user or database reports
    `used_password`; a missing password `needs_password` too; a closed port and a malformed address
    neither.
  - From source, a mistake in the targets file passes `ds init` (exit 0) and stops `ds licence` with
    its `error:` line (exit 2), before the database is asked.
  - As built (§8 S4), against a closed port: `ds mcp` printed one `psycopg.pool WARNING` line and `ds
    migrate` two pool lines, each before the unreachable line, which came about 31 s after the start. libpq words a
    refused connection in two ways (`could not receive data from server: Connection refused`, and
    rarely `Connection refused` with `Is the server running on that host …`), so "once a distinct
    message" is one line or two.
  - As built (§7), in the tests' own cookie jar, which returns no `Secure` cookie over http: with the
    https cookies a token sign-in at `http://localhost:8000` is followed by 401 `not signed in`; with
    E1's it is signed in.
  - Containers carry `com.docker.compose.project.working_dir=<checkout>/deploy`. Of 349 CLI help
    strings, one loses text. The three images a first run needs are local here.
- *Observed against the Docker daemon (§13's steps 0, 1, 2, 4 and 5; macOS 27.0.1,
  Docker 29.1.3, Compose 2.40.3-desktop.1; two scratch clones, the script's as project `dsfirst` on port
  18021 and subnet `172.31.20.0/24`, the by-hand one as `dsguideh` on 18005 and `172.31.12.0/24`):*
  - Step 0: the Dockerfile's two pinned images and `postgres:16-alpine` are local.
  - Step 1: with a local program on 18021 and `http_proxy` naming a dead address the script ended with
    U18, created nothing, and the program was asked once. In a plain shell with the port free it ran
    through in 29 s (the build fetched the Python packages; the base images and the other layers were
    local): the ten files and nothing else, the app healthy at the fourth reading, the token's file one
    `dst_` line of mode 0600, and no secret or token in its output. Its Docker calls, read from a
    pass-through that logged every argv, held no `-p`, no `-f` and no subcommand of I4. A second run
    with the options and a third without them changed no file (hash, mode and time). The containers of
    `deltasleuth` kept their start time.
  - The refusals, each creating nothing: U16 against two other folders' projects (`deltasleuth` and
    `dsguide`) and U17's `docker ps` line (`port 18000 … is published by the container dsguide-app-1`,
    and 8000 by `deltasleuth-app-1`), so §3.2's reading of `:<n>->` holds on a daemon; U19 for two
    networks; U20 after `docker compose down` and a fresh clone; U1 to U8, U3's `.env` form, U10 (the
    client pointed at a socket that does not exist), U11, U12 (two files, and `export`), U14 (`https`,
    and `auth.oidc` on), U15 (port and subnet), U21, U22 and U23. Not provoked: U9, U13 and U24.
  - After step 1: V5 for `ds init` (a misspelt setting) and for `ds migrate` (C5), V6 for a mistake in
    `targets.yaml` (an app already in its loop, and a stopped app the script started: it ended 10 s
    and 8 s after its start, under the app's `error:` line) and W1 (a token file older than the
    volumes; removing the file and running again made a new token). The script ran over an
    installation made by hand, whose site file was the example with its `ports` and `networks` lines
    switched on.
  - A restart loop: of eight readings 1.5 s apart, six were `Up … (health: starting)` and two
    `Restarting (2)`, the `PORTS` column then empty; `RestartCount` was 6 after about 15 s.
  - Step 2, a real build under §2's source override: with the image absent `docker compose --profile
    mocks up -d` built once and started both services on the one image id, and the app applied the
    migrations itself; after a source change `docker compose --profile mocks up -d --build` built once,
    put the new id under `<project>:source` and recreated both containers on it. No second, untagged
    image of the same build exists. The build a rebuild replaces stays, untagged and with the
    project's label, one per rebuild, as after `docker compose build`; `docker image prune --force
    --filter label=com.docker.compose.project=<project>` removed two of them and no other image.
  - Step 4, `admitted` and `opening` as built: with a wrong password in `db_dsn`, `ds migrate`, `ds
    licence` and the app's start answered C5 in under 2 s; with `db` stopped, `ds migrate` printed one
    pool line (`failed to resolve host 'db'`) and the unreachable line 31 s after its start, and
    health answered S2's 503 after 3.0 s. DEPLOY's pre-check in a container: `docker compose run --rm
    app ds licence` printed the start's `error:` line for a misspelt setting, an unknown member of
    `targets.yaml`, a `kek` that is no key and a wrong password, and `installation I-…` once each was
    corrected; `ds init` passed the targets mistake with exit 0.
  - Step 5: `docker compose down -v`, without the profile named, removed the three containers, the
    four volumes and the network of each scratch project.
  - E1 on a container: a token sign-in at `http://localhost:18005` answered `Set-Cookie:
    ds_session_18005=…; HttpOnly; Path=/; SameSite=strict`; one curl cookie store held
    `ds_session_18005` and `ds_session_18021`, was signed in to both installations, and stayed signed
    in to the second after signing out of the first. The sign-in page, with §6's token hint, in
    Chromium 152.0.7977.130.
- *Observed against the Docker daemon a second time (the same day, tools and build; fresh clones: the
  script's as project `dsguide` on port 18000 and subnet `172.31.10.0/24`; a
  by-hand one and then a second script one as `dsguideh` on 18005 and `172.31.12.0/24`, both removed):*
  - Steps 0, 2, 4 and 5 again, with the first pass's results. Step 2 in the by-hand clone: with the
    image absent, one build, both services on the one id and the app applying the migrations; after
    `up -d --build` twice, one tagged id and two untagged predecessors, which the labelled `prune`
    removed, and no other image.
  - Step 1: the script ran through in 19 s with every layer cached; a second run without the options
    and a third with them changed no file. U18 with a dead proxy and a local program was repeated on
    18005 (project `dsguideh`), since the first pass's `dsfirst` still held 18021: the program was asked
    once and nothing was created.
  - U16 for a bare run and for `--port 18000` alone, beside the installation that holds the default
    name: `--port` alone does not take a fresh folder past another folder's `deltasleuth` project.
    Where only the port is taken, `--port` alone was not observed. U17 and U19 against `deltasleuth`
    and `dsfirst`; U1 to U8, U10 to U12, U14, U15 and U21 to U23 again, word for word.
  - U20's way out: in a fresh clone without `.env`, `docker compose -p <project> down -v` removed the
    four orphaned volumes by the project's name alone.
  - A refused `ds token create … > <file>` typed by hand (a wrong role, name or days, or no
    `DS_OPERATOR`) exits 2 and leaves the file the shell made, at 0 bytes; over the file of an existing
    token it empties that file, so the token stays active and its value is lost. Under `set -C` (zsh,
    bash and sh) the shell refuses an existing file before `ds` runs. The script is not affected (I1).
  - E1: `Set-Cookie: ds_session_18000=…; HttpOnly; Path=/; SameSite=strict`; one curl store signed in
    at 18000 and at 18005 and stayed signed in at 18005 after signing out at 18000. The sign-in page,
    with §6's token hint, in Chromium 152.0.7977.130; no token was typed into it.
  - DEPLOY §6's first run in tokens mode, its two API calls sent with the script's token: the upload
    answered `"needs": ["clear", "known_diffs"]`, the run 202, and it ended `completed` with 40 keys
    (35 MATCH, 2 MISMATCH, 2 KNOWN_DIFF, 1 NOT_FOUND_B).
- *After later changes to §7, §8 (C3, C5, C6, H7, H8, S1 to S3) and §14; seen
  in the tests and against their `postgres:16-alpine` only, not on a container:*
  - With a wrong password or a wrong database name, `ds migrate`, `ds licence` and `ds keys migrate`
    each answered C5 after 4.1 s (three tries); with no password in the address, and for C6 (an
    unencoded `%` or `@` in the password), each answered in under 0.1 s. So the "under 2 s" above is
    the earlier build's, as is S2's detail, then `the database did not answer within 3 s`.
  - A role with `CONNECTION LIMIT 1` and its right password: C5 after three tries while its one place
    was held, and no refusal when the place was freed between the tries.
  - The role tests and `test_app.py` pass with the app built over a UI directory that does not exist
    (61 tests).
  - Not observed after those changes: C5, C6 and S2's new text on a container; a real pooler; the
    TLS-only server of §14 against the amended code.
- *The script after later changes to §3 (the header, I1, I5, U16 to U18, U20, the new
  U25 to U27, steps 2, 5 and 6, V5 and the closing block's last line) and §14; seen under `sh` and
  `dash` against the tests' stand-in only, never on a daemon:*
  - The texts quoted above from the two daemon passes are the earlier build's: U17 and U18
    then ended `choose another with --port`, U20 ended `… the keys of that data. Use another project
    name, or put the secret files back; this script deletes nothing`, and the header and the closing
    block named `docs/guide/01-install-docker.md` for the steps by hand.
  - With a kept token file and a `ds token list` that fails under an
    `error:` line, the run ends with that line, V5 and status 2, after every step's title and without
    the closing block.
  - An empty `deploy/secrets` of mode 755, 750, 711 or 701, and one of 755 that holds three kept
    secrets, is refused with U25 before anything is created; under a caller's umask of 0 the folder
    the script makes is 700 and the token 0600.
  - Step 5 beside a container of `docker compose run`: one `docker inspect`, of the service's own id,
    and the token made. That `docker compose ps -aq app` lists such a container, and that Docker
    refuses two ids in one word, was observed with Compose 2.40.3 against a stand-in
    daemon, as was that the three `docker ps` filters print one id.
  - HUP, QUIT and PIPE, sent to the script and the command it waits for in step 4 and in step 6, end it
    with 129, 131 and 141 and leave no `.new` file under either shell; with its output piped into
    `head -n 1` the script left none either.
  - The real `curl` 8.2.1 with U18's words: 7 for a closed port of `127.0.0.1` and of `[::1]`, and for
    a port where a program listens on `[::1]` alone, 7 from `127.0.0.1` and 0 from `[::1]`.
  - `--port 18001` alone, in a fresh folder beside a container that publishes 8000: project
    `deltasleuth`, the base file's subnet, a site file with the `ports` line alone.
  - Not observed after those changes: any of this on a daemon, on Linux, or with a Compose other than the
    stand-in's model.
- *From the source as committed, in a scratch folder, with no Docker and no database:*
  - `ds init`, `ds migrate`, `ds runs`, `ds run`, `ds licence`, `ds serve` and `ds token create`
    printed, word for word, the lines the guide quotes for: no settings file; a settings file that is
    not there; a misspelt setting; an http `public_url` beyond loopback; the Docker example used on a
    workstation (four lines); a targets file that is not found, and one with a mistake; an address file
    that is missing or padded; missing keys; a `kek` that is no key; H6; C2, port 8000 being held here;
    C6 for an unencoded `@`, in 0.8 s; the unreachable line under one or two pool lines, for a closed
    port; no `DS_OPERATOR`; and `--port 70000` and `--port -1` (C3's range, in typer's box under two
    usage lines).
  - `sh deploy/up.sh` typed outside a clone never reaches U6: the shell ends with `sh: deploy/up.sh: No
    such file or directory` and 127 (macOS's `sh`; `dash` says `cannot open deploy/up.sh: No such file`
    and 2). curl 8.2.1, asked as the appendix asks `[::1]`, said `Failed to connect to ::1 port …` and 7.
  - Not run again: anything on a Docker daemon. What the chapters quote of U17, U18, U20, U25 to U27,
    V5 for `docker compose up` and for `ds token list`, C5's tries and its tail, and S2's text is the
    code's and its tests'. The appendix marks each "Not verified here"; chapter 1, which a test holds
    to the script, marks `--port` alone.
- *Not observed:* anything on Linux; any browser signing in with §7's cookies (§13 step 3: Safari,
  Chrome, Firefox); U9, U13 and U24 on a daemon; the script under its default names, which another
  installation holds on this machine, and so `--port` alone for a first installation whose port is
  taken; a first build with no image and no layer cached.

## 1. Decisions (each one seam)

| # | What blocks or confuses | Decision |
|---|---|---|
| 1 | Examples configure SSO, AI and two missing secrets | Fixed in the examples (§2). |
| 2 | No release image; no `.env` example | `deploy/env.source.example`; the base file still runs the release (§2). |
| 3 | No developer settings; four mock commands | `file:` references anchor to the settings file; a developer example; `mocks/targets.yaml`; `python -m mocks.run all` (§5). |
| 4 | A second checkout takes over the first | The source image is tagged by project; the script refuses, and takes `--project`, `--port`, `--subnet` (§3, §4). |
| 5 | Silent sign-in page; bare origin refusal; Safari cannot sign in | Two UI hints (§6); cookie exception E1 (§7). |
| 6 | `ds serve` silent without `web/dist` | A warning and a worded 404 (§8). |
| 7 | Wrong help texts | Eight strings and a guard test (§8). |
| 8 | Small traps | Fixed in §8, but for the restart loop, which the product keeps (D6). |

- **D1** The examples are a trial as copied; `trusted_proxies`, SSO and AI are commented lines. A copy
  left unchanged behind an office proxy admits nobody: no token exists until a host administrator makes
  one, a browser's sign-in carries the proxy's origin (403 `cross-origin request refused`) and `/v1`
  under the office's host name answers 400 `unexpected Host header`. A proxy that rewrites `Host` to a
  loopback name passes the Host check (the gap AUTH §3 records) but not the Origin check.
- **D2** The script is a trial's. It refuses settings that are not a token sign-in at a loopback http
  address, a `.env` without the source override, a loaded release image, another folder's Compose
  project and data whose keys the folder lacks. It ships in the kit under `deploy/` with those refusals
  (§14).
- **D3** On an http `public_url` both cookies lose `__Host-` and `Secure` and carry the port in their
  names (E1). Keeping `Secure` would leave sign-in to each browser's treatment of loopback, which is the
  defect.
- **D4** The hints are the UI's, from `window.location` alone: no server refusal changes, and
  `public_url` is told to nobody.
- **D5** A trial's token keeps working as a bearer once SSO is on (AUTH §1): DEPLOY lists what must not
  survive a trial, and its acceptance list checks the mode (§11).
- **D6** The base compose file changes in comments only. `restart: unless-stopped` stays, since an
  office deployment relies on it. The script ends a start that loops with the app's last `error:` line (V6), and
  DEPLOY gains a pre-check that opens what a start opens (§11).

## 2. Example files

**`deploy/config/deltasleuth.example.yaml`** (changed). Its head, exactly:

```yaml
## DeltaSleuth settings: copy to deltasleuth.yaml. The image reads it from DS_CONFIG
## (/etc/deltasleuth/deltasleuth.yaml). Credentials never appear here, only file: references to
## secrets mounted under /run/secrets. Relative paths resolve against this file's directory.
## As copied it is a trial on one machine: people sign in with an API token (ds token create) at
## http://localhost:8000, and AI chat is off (docs/guide/01-install-docker.md). An office deployment
## (docs/mvp/DEPLOY.md section 3) sets public_url and switches on the lines it uses: a line that starts
## "# " is a setting switched off, and removing those two characters switches it on.

database_dsn_ref: file:/run/secrets/db_dsn       # postgresql://deltasleuth:<password>@db:5432/deltasleuth
evidence_dir: /var/lib/deltasleuth/evidence      # response bodies, encrypted and content-addressed (volume)
workspace_dir: /var/lib/deltasleuth/workspace    # working files (volume)
targets_file: targets.yaml
auth:                                            # sign-in and roles (docs/mvp/AUTH.md §8)
  public_url: http://localhost:8000              # the address people type, exactly; an office: https://<name>, through its TLS proxy
## Behind the TLS reverse proxy (DEPLOY section 5): the compose gateway, through which nginx on the host connects.
#   trusted_proxies: [172.30.0.1/32]
## Single sign-on (DEPLOY section 5): the ten lines below, and the oidc_client_secret secret in the site file.
## Without them people sign in with API tokens (tokens mode: trials and CI).
#   oidc:
#     issuer: https://login.microsoftonline.com/<tenant-id>/v2.0   # per IdP: DEPLOY section 5
```

- The other eight `oidc` lines keep their text, each with `# ` at column 0 before its present
  indentation. `encryption`, `retention` and `worker` are unchanged.
- The marker becomes `## ---- optional blocks: remove the leading "# " of the lines you use; each loads
  as written ----`. The three `llm_` lines open the optional blocks, under `## AI chat (docs/mvp/DEPLOY.md
  section 8): your gateway's profile in llm_profiles.yaml, and the llm_key secret in the site
  file. Without these three lines AI is off.`, as `# llm_profiles_file: llm_profiles.yaml`,
  `# llm_profile: gateway` and `# llm_environment: prod`. `llm_external_data_classes` becomes a
  `## ` prose line, so no rule switches it on. The packs heading gains
  "needs the single sign-on lines above".
- **One rule, for people and tests, over the whole file:** a line that starts `# ` is a setting switched
  off, and removing those two characters switches it on (`(?m)^# `); every other comment line starts
  `##`. The header's three old lines and the marker change from `# ` to `## ` for that reason.
- The text `  public_url: http://localhost:8000 ` occurs once: §3.3 edits it in a copy.

**`deploy/docker-compose.site.example.yml`** (changed). As copied it changes nothing:

```yaml
services:
  app:
    secrets: []                                # single sign-on: oidc_client_secret; AI chat: llm_key; each with its line under secrets: below
```

- The header gains "As copied it changes nothing: token sign-in with AI off needs no secret beyond the
  base file's four." The top-level block becomes `# secrets:`, `#   oidc_client_secret: {file:
  ./secrets/oidc_client_secret}`, `#   llm_key: {file: ./secrets/llm_key}`. The ports comment gains
  "(auth.public_url then names it)". The other comments stay.
- An office writes `secrets: [oidc_client_secret, llm_key]` and removes three `# `.

**`deploy/env.source.example`** (new; holds no secret):

```sh
# .env for a build of this source, the only kind until a release exists: copy it to .env in the
# installation's folder (cp deploy/env.source.example .env). Compose reads .env from the folder a
# command is run in; never export these variables, which would override it. A loaded signed release
# names the first two files only (docs/mvp/DEPLOY.md).
COMPOSE_FILE=deploy/docker-compose.yml:deploy/docker-compose.site.yml:deploy/docker-compose.source.yml
# A second installation on one machine: its own project name here, and its own port and subnet in the
# site file (deploy/up.sh --project NAME --port N --subnet A.B.C.0/24 writes all three).
# COMPOSE_PROJECT_NAME=deltasleuth2
```

**`deploy/docker-compose.source.yml`** (changed):

```yaml
# An unsigned build of this checkout (DEPLOY section 2), tagged with the Compose project's name, so two
# checkouts on one machine share no image; after taking new source: docker compose up -d --build
# The app also sees the checkout's mocks/ and examples/, read-only, in the image's working directory:
# `docker compose run --rm app ds run mocks/customer/spec.yaml` reads them by their path in the checkout.
x-image: &image "${COMPOSE_PROJECT_NAME:-deltasleuth}:source"
services:
  app:
    build: {context: .., dockerfile: deploy/Dockerfile}
    image: *image
    volumes: ["../mocks:/app/mocks:ro", "../examples:/app/examples:ro"]
  mocks: {image: *image}                       # the app's build: one image, built once
```

The default names the base file's project, so the tag never depends on Compose handing a file's `name:`
to interpolation. One build ends the second, untagged image. That `up -d --build` builds once and starts
both services on it was observed on a daemon on 2026-10-05 (the Checked block).

**The app's two mounts (added 2026-10-06).** The image copies `deltasleuth/` only, and `ds` under
`docker compose run` reads a path inside its container, whose working directory is `/app`. A source
build therefore mounts the checkout's `mocks/` and `examples/` read-only at `/app/mocks` and
`/app/examples`, in the `app` service (the service's own container and every `docker compose run app`),
so that `ds run mocks/customer/spec.yaml`, `ds spec validate examples/tables/spec.yaml` and `python -m
mocks.fixtures` read the checkout's files by their repository path. The paths are relative to
`deploy/`, the folder of the first compose file, as the base file's. The base file is unchanged: an
installation that names no source override (a loaded release) gives the app no such mount, and its
`mocks` service keeps its own. A path that is not in the container is refused with where the container
read it ([DEPLOY.md](DEPLOY.md) section 9). *Observed on 2026-10-06 (macOS, Docker 29.1.3; a fresh
clone with this change, `sh deploy/up.sh --project dsfix --port 18600 --subnet 172.31.230.0/24`, removed
afterwards):* the script ran through, and again over what it had made; `docker compose run --rm -e
DS_OPERATOR="$(id -un)" app ds run mocks/customer/spec.yaml` ended `completed` with 35 MATCH, 2
MISMATCH, 2 KNOWN_DIFF and 1 NOT_FOUND_B; `ds spec validate examples/tables/spec.yaml` answered `ok`;
both folders refused a write (`Read-only file system`).

**`deploy/docker-compose.yml`** (comments only, without the words the hardening test refuses: `--build`,
`build:`, `x-build`). Header line 8 becomes the three lines below, and the comment of the `ports` line
becomes `# loopback only: an office puts its TLS reverse proxy in front`:

```yaml
# An office reaches the app through a TLS reverse proxy on the host (DEPLOY section 5), never this port
# directly; a one-machine trial opens http://localhost:8000 (deploy/up.sh, docs/guide/01-install-docker.md).
# A checkout has no release image to load: its .env names the source override too (deploy/env.source.example).
```

The targets and LLM profile examples are unchanged.

## 3. `deploy/up.sh`

`sh deploy/up.sh [--project NAME] [--port N] [--subnet A.B.C.0/24]`, run from the checkout's folder.
New, mode 100755, POSIX `sh` with `set -eu`, clean under `shellcheck -s sh`, run under `dash` too.
This section is its whole specification, and §12's tests bind it.

**Header.** The comment block after `#!/bin/sh`, each line `# ` and the text below (`#` alone for an
empty line); `-h` and `--help`, wherever they stand among the arguments, print it without that prefix
and exit 0, before any check:

```
deploy/up.sh: from a fresh clone to a running one-machine trial of DeltaSleuth, with the mock services
and a first API token saved to a file. Run it from the checkout's folder:

  sh deploy/up.sh [--project NAME] [--port N] [--subnet A.B.C.0/24]

It creates what is missing and changes, overwrites and deletes no file: the settings (deploy/config),
the site file and .env, four secrets (deploy/secrets), an unsigned image built from this checkout, the
database, the containers and the token (.deltasleuth/tokens/first-run, never printed).
It runs no "docker pull". The build and the first start fetch what this machine does not hold: the
images python:3.13-slim and node:24-alpine (pinned by digest in deploy/Dockerfile) and
postgres:16-alpine, and the Python and npm packages. Without that network access, load or mirror them
first.
The options are for a second installation on this machine: its own project name, port and subnet. For
a first one whose port 8000 is taken, --port N alone is enough.
Every step by hand: docs/guide/A1-install-by-hand.md. An office deployment: docs/mvp/DEPLOY.md.
```

"Alone is enough" is §3.2's rules read together: without `--project` and `--subnet` the project stays
`deltasleuth` and the subnet `172.30.0.0/24`, which pass U16 and U19 where no other installation and no
other Docker network holds them, and the site file gets its `ports` line alone (which needs Compose
2.24.4, U24). The tests hold it against the stand-in; on a daemon it is not observed (the Checked block).

### 3.1 Invariants

- **I1** It overwrites, edits and deletes no file that exists; a symbolic link is a file that exists,
  whatever it names (U27). Each file it makes is completed as `<file>.new` and then moved. A trap on
  EXIT removes the one `.new` file in progress; HUP, INT, QUIT, PIPE and TERM end the script through it,
  with 129, 130, 131, 141 and 143, under `dash` as under `sh`. It removes nothing else.
- **I2** No secret and no token reaches the terminal, a shell variable, a command line or the
  environment. `db_dsn` is assembled file to file. A token's id (the `T-` name every list shows) is not
  secret.
- **I3** Everything in §3.2 runs before anything is created.
- **I4** No argv of its Docker calls has the subcommand `pull`, `down`, `rm`, `prune`, `volume rm`,
  `network rm` or `image rm`. That is not a promise that nothing is fetched: `build` and the first `run`
  fetch a missing base image, `postgres:16-alpine` and the packages, as the header says. The script
  holds no compose file list (no text `docker-compose.yml:`): the `.env` line has one source, the
  example.
- **I5** It calls `docker`, `openssl`, `id` and POSIX tools (`cat`, `chmod`, `cp`, `cut`, `grep`, `ls`,
  `mkdir`, `mv`, `rm`, `sed`, `sleep`, `tail`, `tr`, `uname`); `curl`, where installed, twice, with no
  proxy, against this machine's two loopback addresses, `127.0.0.1` and `[::1]` (U18). It names the
  clipboard tools and runs none. It exports one variable, `LC_ALL=C`, so that its patterns and the
  tools' read bytes the same way everywhere.
- **I6** Options shape a new installation. Given again with the values the files hold, they pass.

### 3.2 Refusals before anything is created (stderr, `error: ` first, exit 2)

**What it reads, and how.** Anything it does not understand is refused, never guessed.
- **`.env`**, where it exists. Lines that start `#` are skipped. Every other line that holds
  `COMPOSE_FILE` or `COMPOSE_PROJECT_NAME` must start with that name and `=`, at most once each; the
  value is the rest of the line as written. Quotes, `export`, indentation, a trailing blank or `\r` and a
  second line are refused (U12, U3). The files are the value split at `:`.
- **The settings**, where they exist: a line that starts `auth:`; no line that starts `  oidc:`; exactly
  one line that starts `  public_url: `, whose value, the text up to the next blank, matches
  `http://(localhost|127\.0\.0\.1|\[::1\]):[0-9]+` (U14).
- **Compose's rendering:** whole lines, stripped of leading blanks: `name: <p>`, `image: <p>:source`,
  `published: "<port>"`, `- subnet: <subnet>`.

**The values.**
- *Project:* `.env`'s line; else `deltasleuth` where `.env` exists; else `--project`; else
  `deltasleuth`.
- *Port:* the port of the settings' `public_url` where the settings exist; else `--port`; else 8000.
- *Subnet:* `--subnet`, else `172.30.0.0/24`; the gateway is its `.1`. An existing site file's subnet is
  only compared (U15).

The rows are in checking order; a label is a name, not a position.

| # | Message |
|---|---|
| U1 | `unknown option <x> (--project NAME, --port N, --subnet A.B.C.0/24)` |
| U2 | `<option> needs a value (--project NAME, --port N, --subnet A.B.C.0/24)` |
| U3 | `<where>: a project name is lower-case letters and digits, joined by -, _ or __ (for example ds2)` |
| U4 | `--port: a number from 1024 to 65535` |
| U5 | `--subnet: a.b.c.0/24, for example 172.31.7.0/24` |
| U6 | `deploy/docker-compose.yml is not in this folder: cd to the DeltaSleuth checkout, then sh deploy/up.sh` |
| U7 | `unset COMPOSE_FILE and COMPOSE_PROJECT_NAME in this shell: they override .env` |
| U8 | `<tool> is not installed (docs/guide/01-install-docker.md, section 1)` (docker, openssl) |
| U9 | `docker compose (version 2) is not installed` |
| U10 | `Docker is not running, or this account may not use it` |
| U23 | `<file>.new is left by an interrupted run: remove it yourself, then run this script again` |
| U27 | `<file> is a link to a file that is not there: put that file back, or remove the link yourself; this script changes no existing file` |
| U22 | `<file> is empty: remove it yourself, then run this script again` (a secret, the token file) |
| U12 | `.env has no line COMPOSE_FILE=… that names deploy/docker-compose.source.yml (once, unquoted): this script runs an unsigned build of this checkout for a trial; a loaded release is run as docs/mvp/DEPLOY.md says` |
| U11 | `.env makes this folder project "<a>", not "<b>": this script changes no existing file (leave --project out, or edit .env yourself)` |
| U13 | `the release image deltasleuth:<version> is loaded on this machine and this folder has no .env: this script runs an unsigned build of this checkout for a trial; a release is run as docs/mvp/DEPLOY.md says (for a trial all the same: cp deploy/env.source.example .env and, for another project than deltasleuth, add a line COMPOSE_PROJECT_NAME=<name> to it; then run this script again)` |
| U14 | `deploy/config/deltasleuth.yaml is not a token sign-in trial at a loopback http address (<why>): this script sets up a trial only; docs/mvp/DEPLOY.md is the office deployment` |
| U15 | `<file> exists and does not say <what>: this script changes no existing file (edit it yourself, or give the option that matches it)` |
| U24 | `Docker Compose <version> is older than 2.24.4, which a site file for another port or subnet needs (!override): update Compose, or keep port 8000 and subnet 172.30.0.0/24` |
| U25 | `deploy/secrets is open to other accounts: chmod 700 it yourself, then run this script again` |
| U21 | `deploy/secrets holds some of db_password, db_dsn, kek and tokeniser but not all: put the missing ones back; new ones cannot open what the old ones protected` |
| U26 | `deploy/secrets/db_password holds characters an address cannot carry as written: write deploy/secrets/db_dsn yourself (postgresql://deltasleuth:<the password, percent-encoded>@db:5432/deltasleuth), then run this script again` |
| U16 | `Compose project "<p>" on this machine was started from <dir>: this script takes over no other installation. For a second one here: sh deploy/up.sh --project <name> --port <port> --subnet <a.b.c.0/24>` |
| U20 | `Docker volumes of project "<p>" exist, but deploy/secrets holds no db_password, kek and tokeniser: this folder does not hold the keys of that data, which DeltaSleuth cannot open without them. Use another project name, or put the secret files back. If the data is no longer wanted, docker volume ls --filter label=com.docker.compose.project=<p> lists its volumes, for you to remove with docker volume rm; this script deletes nothing` |
| U17 | `port <n> of this machine is published by the container <name>: choose another with --port N (for a first installation on this machine that option alone is enough)` |
| U18 | `port <n> of this machine answers already: choose another with --port N (for a first installation on this machine that option alone is enough)` |
| U19 | `the subnet <s> is the Docker network <name>'s: choose another with --subnet a.b.c.0/24` |

- **U3** The pattern is `[a-z0-9]+((_|__|-+)[a-z0-9]+)*`: what Compose takes as a project and Docker as
  an image name. `<where>` is `--project`, or, after U12, `.env's COMPOSE_PROJECT_NAME (read unquoted
  and once)`.
- **U9, U10** `docker compose version --short` and `docker info` must succeed.
- **U23, U27, U22** U23 looks for a `.new` beside each of §3.3's ten files, a symbolic link of that name
  included, whatever it names; U22 at the four secrets and the token file. U27 is one of the ten that is
  a symbolic link to a file that is not there (`-L` and not `-e`: secrets or settings kept as links
  into a folder that is not mounted now). Past it a link among the ten is a link to a file, which the
  later checks read and step 1, 2 or 6 keeps. The folders are not looked at: a `deploy/secrets` that is
  such a link ends step 2, at `mkdir`'s own words.
- **U11** fires when `.env` exists, `--project` is given and the two differ; `<a>` is `.env`'s project,
  `deltasleuth` when it has no such line.
- **U13** reads the base file's line `  image: deltasleuth:<version>` and asks `docker image inspect`.
- **U14** `<why>` is `it has no auth block`, `auth.oidc is on`, `it has no single line "  public_url:
  <address>"` or `public_url is <value>`.
- **U15** `<what>` is `port <port> in public_url` (the settings, when `--port` differs from them), or
  `port <port>` or `subnet <subnet>` (the site file). The site file is judged as rendered, by `docker
  compose -f deploy/docker-compose.yml -f deploy/docker-compose.site.yml config`, the one command with
  `-f` (it starts nothing): the port must be published, and a given `--subnet` must be its subnet. So
  `--port 8000` beside a plain copy of the example passes. A rendering that fails is V1.
- **U24** applies when step 1 would write `!override` (no site file; a port other than 8000 or a subnet
  other than `172.30.0.0/24`). The version is the first three numbers of `docker compose version
  --short`, a leading `v` dropped; a text without them is refused the same way.
- **U25** `deploy/secrets` is a folder and `ls -ldL deploy/secrets` does not begin `d`, the owner's
  three letters, then `--` with `-` or `S`, then `--` with `-` or `T`: the group or anyone else has a
  permission on it. A setgid folder (`S`, which a checkout under a group-shared folder inherits and
  `chmod 700` keeps on Linux) and a sticky one (`T`) give nobody else a permission and pass; a plain
  pattern, `d???------`, would refuse the folder the script itself makes there. Whether a secret would be written
  makes no difference: the secrets are 444 for the containers' users, so the folder's mode is all that
  keeps them, kept ones too, from the machine's other accounts. A folder the script makes itself is 700
  under any umask of the caller (step 2).
- **U21** One case passes: `db_dsn` alone is missing, and step 2 makes it from `db_password`.
- **U26** In that case, `tr -d '\n' < deploy/secrets/db_password | grep -q '[^A-Za-z0-9._~-]'` finds a
  character: step 2 writes the password into the address as it stands, and with a `%` or a blank that
  is an address the driver cannot read, with an `@` or a `/` one it reads otherwise (the password's
  tail as a host, a port or a database name). Step 4 would stop at C6 (§8), over a `db_dsn` the script
  has made and will not rewrite. The password is read through a pipe, into no variable (I2); the
  passwords step 2 makes are hexadecimal.
- **U16** `docker ps -a --filter label=com.docker.compose.project=<p> --format 'x{{.Label
  "com.docker.compose.project.working_dir"}}'`: every container, stopped ones too, must name this
  folder's `deploy`, both compared after `cd … && pwd -P`; a folder that is gone differs. The `x`
  makes a line of a container without that label, wherever Docker lists it: it names no folder, so it
  is another installation's, and `<dir>` is then `a folder its container does not name`. When Docker
  does not answer that command the script ends there, with Docker's own words and status: it never goes
  on without the answer.
- **U20** `docker volume ls -q --filter label=com.docker.compose.project=<p>` prints a name. The message
  names that listing and `docker volume rm`, and the script runs neither removal (I4): whether the data
  is still wanted is the reader's to say.
- **U17 to U19** apply while the folder holds neither settings nor a site file.
  - U17: a line of `docker ps --format '{{.Names}} {{.Ports}}'` holds `:<n>->`, the host's side of a
    published port (`127.0.0.1:18000->8000/tcp`); the container is the line's first word. Not `--filter
    publish=<n>`: Docker's reference documents that filter by the container's own port (its example,
    `--publish=80` on a random host port, is found by `publish=80`), so for 8000 it would name another
    installation's app that publishes 18000, and for 18000 nothing. A published range is not matched;
    U18 answers for it. The `docker ps` line was observed on a daemon on 2026-10-05 (the Checked
    block); `--filter publish=` was not tried.
  - U18: `curl -q -g -s -o /dev/null --noproxy '*' --max-time 2 http://127.0.0.1:<n>/`, and then the
    same words with `http://[::1]:<n>/`, ends with any status but 7, so a program outside Docker is seen
    too, and no proxy of the shell is asked. Both addresses, because the closing block sends the reader
    to `http://localhost:<n>`, which is either, and Docker publishes on `127.0.0.1` alone: a program on
    `[::1]:<n>` would answer the browser in the app's place. `-g` keeps curl from reading the brackets
    as a range. Without `curl` the check is skipped.
  - U17 and U18 end `--port N (for a first installation on this machine that option alone is enough)`:
    the header's sentence, and the paragraph under it.
  - U19: `docker network inspect --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}'` on the
    ids of `docker network ls -q`: a network other than `<p>_default` has that subnet.

### 3.3 Steps (each printed as `== n/6 <title>`; each file as `created  <path>` or `kept     <path>`)

The titles: `Settings, site file and .env`, `Secrets`, `Build the image <p>:source`, `Workspace and
database`, `Start`, `First token`. The ten files: `.env`, `deploy/docker-compose.site.yml`,
`deploy/config/{deltasleuth,targets,llm_profiles}.yaml`, `deploy/secrets/{db_password,db_dsn,kek,tokeniser}`
and `.deltasleuth/tokens/first-run`. Nothing else is created in the folder but their folders.

1. **Settings, site file and `.env`**, only where missing.
   - `deploy/config/deltasleuth.example.yaml`, `targets.example.yaml` and `llm_profiles.example.yaml`
     are copied to `deltasleuth.yaml`, `targets.yaml` and `llm_profiles.yaml`, mode 644 (the app reads
     them as another user); the developer example is not. For a port other than 8000 the settings copy
     passes through `sed`, which writes `http://localhost:<port>` on the `public_url` line; the result
     must hold that line once before it is moved (V8).
   - `deploy/docker-compose.site.yml` is a copy of the example. For a port other than 8000 or a subnet
     other than `172.30.0.0/24` it is this file instead (the `ports` line only for such a port, the
     `networks` line only for such a subnet):
     ```yaml
     # This installation's changes to docker-compose.yml, written by deploy/up.sh for its own port and subnet;
     # deploy/docker-compose.site.example.yml shows what else goes here.
     services:
       app:
         secrets: []
         ports: !override ["127.0.0.1:<port>:8000"]
     networks: {default: {ipam: {config: !override [{subnet: <subnet>, gateway: <gateway>}]}}}
     ```
   - `.env` is a copy of `deploy/env.source.example`, with `COMPOSE_PROJECT_NAME=<p>` appended when the
     project is not `deltasleuth`.
   - Then `docker compose --profile mocks config`, through `.env`, must succeed and hold the lines
     `name: <p>`, `image: <p>:source` and `published: "<port>"` (V1 to V4).
2. **Secrets**, only where missing, in `deploy/secrets`, which the script makes under `umask 077` (700)
   where it is missing and which U25 has found private where it is not; each made under `umask 077` and
   set to 444: `openssl rand -hex 24` for `db_password`; `openssl rand -hex 32` for `kek` and
   `tokeniser`; `db_dsn` from `printf 'postgresql://deltasleuth:'`, `tr -d '\n' <
   deploy/secrets/db_password` and `printf '@db:5432/deltasleuth'` (a kept password has passed U26).
3. **Build:** `docker compose build`.
4. **Workspace and database:** `docker compose run --rm app ds init`, then `ds migrate` the same way. A
   mistake in the settings file or the database's address ends here, in the product's one `error:`
   line; one in the targets file ends at V6 where step 5 starts or recreates the app, and at step 6
   where the app was running already and stays as it is.
5. **Start:** `docker compose --profile mocks up -d`. With the first id that `docker ps -aq --filter
   label=com.docker.compose.project=<p> --filter label=com.docker.compose.service=app --filter
   label=com.docker.compose.oneoff=False` prints, the script reads `docker inspect --format
   '{{.State.Health.Status}} {{.RestartCount}}' <id>` once, then every 2 s, at most 90 times. `healthy`
   ends the wait; a count above the first reading ends it at once (V6), and so does a `ps` that prints
   no id. The filters name the service's own container: `docker compose ps -aq app` also lists the
   containers of `docker compose run … app` (an agent's `ds mcp`, a `ds run`, one left without `--rm`),
   and `docker inspect` takes two ids in one word for no container. One id is read, the first, should
   Docker ever list two.
6. **Token**, only when `.deltasleuth/tokens/first-run` is missing. Folders it makes are 700. Under
   `umask 077`: `docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds token create --name
   first-run --owner "$(id -un)" --role spec_author > .deltasleuth/tokens/first-run.new`. The file must
   hold exactly one line starting `dst_` before it is moved. For a kept file the script takes its id
   (`cut -d_ -f2`) and runs `docker compose run --rm -T app ds token list`. When that fails the run
   ends there, with V5 under what `ds` said: both token commands open what a start opens (the three
   files of `deploy/config`, the keys and the database), so a mistake that the running app has not
   read, which steps 4 and 5 pass, ends the run in the product's `error:` line, with a kept token (V5)
   as with a new one (V7), and never in the closing block. When it succeeds and no line starts `T-<id> `
   and holds `  active  `, the script prints W1.

### 3.4 Refusals after step 1 has written (exit 2; what exists stays)

| # | Message |
|---|---|
| V1 | `Compose does not accept .env and deploy/docker-compose.site.yml (the lines above say why)` |
| V2 | `.env does not make this folder project "<p>"` |
| V3 | `Compose would not run the image <p>:source: this script runs a build of this checkout only (see COMPOSE_FILE in .env)` |
| V4 | `deploy/docker-compose.site.yml does not publish port <n>, which deploy/config/deltasleuth.yaml names in public_url` |
| V5 | `<step> failed (the lines above say why): correct it and run this script again; it keeps what exists` (`the build`, `ds init`, `ds migrate`, `docker compose up`, `ds token list`) |
| V6 | `the app did not become healthy; the line above, if any, is its last error (all of it: docker compose logs --tail 40 app)`, after the last `^error:` line of `docker compose logs --no-log-prefix --tail 40 app` |
| V7 | `the token was not created (the lines above say why)`; its `.new` file is removed |
| V8 | `deploy/config/deltasleuth.example.yaml does not hold the line "  public_url: http://localhost:8000 " once: this script writes the port nowhere else` (a port other than 8000; it is the first file, so nothing was created) |
| W1 | `warning: the token in .deltasleuth/tokens/first-run (T-<id>) is not an active token of this database (revoked, expired, or another database's) and will be refused: remove the file and run this script again for a new one` (exit 0) |

### 3.5 The closing block (stdout, exact; the macOS form)

```

DeltaSleuth is running: Compose project "deltasleuth", an unsigned build of this checkout, for a trial.

  Open            http://localhost:8000   in a browser on this machine, typed exactly so
  Copy the token  tr -d '\n' < .deltasleuth/tokens/first-run | pbcopy
  Sign in         paste it into "API token", press "Sign in", then empty the clipboard: pbcopy < /dev/null
  You act as      the token "first-run" (role spec_author; it expires at most 90 days after it was made): the audit log names the token, not you
  ds commands     docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds --help
  Stop, start     docker compose stop        docker compose --profile mocks up -d
  Every step      docs/guide/A1-install-by-hand.md        The office deployment: docs/mvp/DEPLOY.md
```

The address is the settings' `public_url`; 90 is `tokens.DEFAULT_DAYS`. `Every step` names the guide's
appendix, where the steps by hand are, as the header does; U8 names chapter 1, whose section 1 is what
a machine needs. Outside macOS the clipboard
commands are `wl-copy` and `wl-copy --clear` where `WAYLAND_DISPLAY` is set, else `xclip -selection
clipboard`, emptied by the same command reading `/dev/null`. Where the tool is not installed, both lines
end `(install xclip or wl-clipboard to copy it; the file holds the token)`.

## 4. A second installation

- By script: `sh deploy/up.sh --project ds2 --port 18000 --subnet 172.31.10.0/24`.
- By hand, three edits: `COMPOSE_PROJECT_NAME` in `.env`; the site file's `ports` and `networks` lines;
  the port in `public_url`. The file order stays base, site, source, and no command needs `-p`.
- One browser stays signed in to both: E1 names each installation's cookies by its port (§7).
- Not changed: `name: deltasleuth` in the base file (without it Compose names every checkout `deploy`)
  and `hostname: deltasleuth-app`, which audit actor names carry. The CA override's tag
  `deltasleuth:<version>-ca` is not per project (§11).

## 5. The developer path

**`deltasleuth/config.py`, `load_settings`.** Before validation, `doc = _anchored(doc, path.parent)`.
Under every member whose name ends `_ref` or `_refs`, a string `file:<relative path>` becomes
`"file:" + str(base / path)`; an absolute path and a bare `file:` stay.
- It is `_file_ref` of `core/spec/targets.py`, and makes true what the module text, the example's
  header and AUTH §8 say. The module text gains "and every `file:` reference".
- The nine members: `database_dsn_ref`, `client_secret_ref`, `client_key_ref`, `kek_ref`,
  `tokeniser_key_ref`, `signing_key_ref`, `url_ref`, `password_ref`, `signing_key_refs`.
- `_key_custody` then judges the path that `read_secret` reads. `tests/unit/api/conftest.py` drops its
  hand rebasing of the oidc references.

**`deploy/config/deltasleuth.dev.example.yaml`** (new), copied to `.deltasleuth/deltasleuth.yaml`:

```yaml
# DeltaSleuth settings for a source checkout without Docker (docs/guide/02-run-from-source.md): copy to
# .deltasleuth/deltasleuth.yaml in the checkout, which git ignores, and name that file in DS_CONFIG.
# Developer mode: no auth block, so ds serve listens on this machine only and every local user and
# process acts as admin; never put a proxy or a port forward in front of it. The keys are files in the
# workspace (ds init) and AI chat is off. Every path below is relative to this file's folder.

database_dsn_ref: file:db_dsn          # one line: postgresql://deltasleuth:<password>@127.0.0.1:5432/deltasleuth
evidence_dir: evidence                 # response bodies, encrypted
workspace_dir: workspace               # working files, and the tokeniser key and keyring ds init creates
targets_file: ../mocks/targets.yaml    # the four mock services on 127.0.0.1 (python -m mocks.run all)
```

**`mocks/targets.yaml`** (new): the four entries of `mocks/linker/targets.yaml` and
`mocks/customer/targets.yaml`, unchanged, under one `targets:`.

**`mocks/registry.py`**: `PORTS = {"linker": {"a": 18081, "b": 18082}, "customer": {"a": 18091, "b": 18092}}`.

**`mocks/run.py`**: `python -m mocks.run all [--host 127.0.0.1] [--seed S]` serves the four in one
process (a `web.AppRunner` and `TCPSite` each, `access_log=None`, SIGINT and SIGTERM handled); the
module text names both forms. It prints:

```
linker a: http://127.0.0.1:18081
linker b: http://127.0.0.1:18082
customer a: http://127.0.0.1:18091
customer b: http://127.0.0.1:18092
Ctrl-C stops all four; POST /__reset on each forgets its call counts.
```

- argparse, exit 2: `all serves both variants of both pairs on their own ports: give neither --variant
  nor --port`; for one pair, `--variant and --port are required`.
- A taken port, on stderr, exit 2, once the sites already bound are closed: `error: port 18081 is in use
  (linker a): nothing was started`.

## 6. The sign-in page

New `web/src/signin.ts`, pure (no import, no `fetch`), used by `SignIn` in `web/src/main.tsx` and by
`tokenLogin` in `web/src/api.ts`. Nothing in it comes from the server: an anonymous visitor learns its
own address bar and a command's name. `api.ts` imports it as `./signin.ts`, with the extension, its one
import: Node runs `api.test.ts` without a bundler and resolves no import without one. So
`web/tsconfig.json` gains `"allowImportingTsExtensions": true` (it already has `noEmit`).

- **`TOKEN_HINT`**, under the token form as `<p className="muted">`: `An API token comes from the command
  line of the machine that runs DeltaSleuth: whoever administers it runs "ds token create" there and
  passes the token on. It is shown once. This page cannot create one.`
- **`addressHint(detail, at)`**, under `<Err>`, only when `detail` is `cross-origin request refused`;
  `at` is `window.location` (its `protocol`, `hostname`, `port` and `origin`).
  - On a loopback page: `DeltaSleuth takes a sign-in from one address only, the one in its settings
    (auth.public_url). This page was opened at http://127.0.0.1:8000. On the machine that runs it, try
    http://localhost:8000 instead; if that is refused too, the settings name another address.` It names
    the other of `localhost` and `127.0.0.1`, with the same scheme and port; at `[::1]` both, joined by
    ` or `.
  - Elsewhere the third sentence is `Ask whoever runs it for the address.`
- **`NOT_KEPT`**: `The token was accepted, but this browser did not send the session cookie back, so you
  are not signed in. Allow cookies for this address, or use another browser.` `tokenLogin` posts, then
  reads `/auth/me`; a 401 `not signed in` there is rethrown as `ApiError(401, body, NOT_KEPT)`. A refused
  POST passes through unchanged, and the CSRF token is `/me`'s.

## 7. Session cookies on a plain-http `public_url` (AUTH §3, exception E1)

**E1.** When `urlsplit(auth.public_url).scheme` is `http`, which `config._public_url` admits for
`localhost`, `127.0.0.1` and `[::1]` only, the cookies are named `ds_session_<port>` and
`ds_login_<port>`, with the port of `public_url` (80 when it names none), and carry no `Secure`.
`HttpOnly`, `SameSite`, `Path=/`, no `Domain`, the login cookie's `Max-Age=600`, the CSRF token, and the
Origin and `Sec-Fetch-Site` checks are unchanged. Under https, a loopback https address included, names,
attributes and HSTS are today's, byte for byte. A server reads only its own form's names: a plain name
is never accepted under https, nor `__Host-ds_session` under http, nor another port's name.

- **Why plain.** Only a browser that treats loopback as secure keeps a `__Host-` or `Secure` cookie over
  http. Observed on 2026-10-05, with a test page and not a sign-in: Chrome 154 and
  Chromium 152 kept it; WebKit on macOS 27.0.1 dropped it, and kept the same cookie without the prefix;
  Safari itself and Firefox were not opened. A dropped cookie fails with no message.
- **Why the port.** A browser keeps cookies by host, not by port. With one name, two installations at
  `http://localhost:8000` and `http://localhost:18000`, which §4 makes ordinary, would replace each
  other's session at every sign-in, silently.
- **Given up, on loopback http only:**
  - Where a browser honours the prefix, its refusal of a same-named cookie planted from another loopback
    origin. A planted value the server never issued matches no session row (401), and every sign-in
    mints a new secret, so no planted value becomes the victim's session. But a planter who holds a
    live session of its own on that installation (any valid token, or an SSO account) can plant that
    session's secret: the server cannot tell who presents a secret, and answers the victim's browser as
    the planter (a session swap; seen in the same checks as the Consequence below, with a cookie header
    and not in a browser). The victim then works and is audited as the planter, what is kept by actor
    (a chat session) is the planter's, and the victim's sign-out ends the planter's session, not its
    own, which stays live until it idles out. The Origin check binds unsafe requests to the port and
    does not help here: after the swap the requests come from the installation's own page.
  - No TLS channel is there for `Secure` to protect. The browser sends the cookie to every http service
    on that loopback name, whatever its port: another installation, or any local program serving
    `localhost`, receives the session secret. By the same rule a browser that keeps today's prefixed
    cookie over loopback http sends it to those ports too (not observed).
- **Consequence.** Moving `public_url` between http and https, or to another loopback port, signs every
  browser out once, as does this change for a browser that held `__Host-ds_session` at
  `http://localhost`: the server no longer reads the cookie the browser holds. The session itself is not
  ended. Its row is found by the secret's hash alone, whatever the cookie's name, so the same secret
  sent under the new form's name is still that session until it idles out, reaches `session_max_hours`
  or is revoked (seen on 2026-10-05 with the tests' client and one database, in checks
  the suite does not hold: a secret minted at `http://localhost:8000` answered 200 as
  `__Host-ds_session` at `https://ds.test`, in tokens mode and in oidc mode, and as `ds_session_18000`
  at `http://localhost:18000`). A sign-out that carries only the old name answers 400 and revokes
  nothing. A token's session is no session once the mode is `oidc`, whatever its cookie. Where the move
  must end the sessions, those whose secret travelled without `Secure` among them, run `ds session
  revoke --all` (DEPLOY, "From a one-machine trial"). How sessions are stored is not changed here.
- **Code.** `deltasleuth/auth/sessions.py` gains `plain(auth) -> bool` and `named(name, auth) -> str`
  (under E1 the name without its prefix, `_` and the port); `COOKIE` and `oidc.LOGIN_COOKIE` stay the
  https names. `plain` is true for an http scheme on a loopback host: it repeats `_public_url`'s rule,
  so settings built past the loader are never plain, and `None` (developer mode, which sets no cookie)
  is not. Both read the settings alone: no `Host`, `Origin` or forwarded header of a request changes
  the name or `Secure`. `api/deps.py principal` reads `named(COOKIE, auth)`. In `api/routes/auth.py` every use
  follows (twelve lines, in `login`, `callback`, `_signed_in`, `token_login`, `me` and `logout`), and
  `_SESSION_COOKIE` and `_LOGIN_COOKIE` take `secure=not plain(auth)`. No other module reads a cookie.
  The texts that name one cookie form (`auth/sessions.py`'s module text, `oidc.Login`,
  `context.login_key`, `routes/auth.py login`) name both.

## 8. CLI and server fixes (messages binding; each asserted)

`deltasleuth/cli/main.py`:

| # | Where | Text |
|---|---|---|
| H1 | `app` help | `DeltaSleuth: compare what two sources hold, key by key, black-box: REST services, database tables, CSV and Parquet files.` |
| H2 | `target_app` help | `Database and files targets: the pre-flight check (docs/mvp/ENGINES.md §13, FILES.md §16).` |
| H3 | `token create --name` | `Token name, audited as its actor_name: 3 to 64 of a-z, 0-9, '.', '_' and '-', starting with a letter or a digit.` |
| H4 | `precheck draft`, last sentence | `Never stored: when it is ready, upload it on the Specs screen or with POST /v1/specs, or, outside SSO, give the file to ds run, which stores the spec it runs.` |
| H5 | `migrate` | `Apply the pending database migrations (every command that opens the store applies them too).` |
| H6 | `init`, developer mode, stdout | `workspace ready: tokeniser key <path>, keyring <path>; keep a copy of both somewhere safe: a lost key cannot be made again` |
| H7 | `runs` | `List the latest runs, newest first, each with what its spec approval says.` and, as a paragraph of its own, which `ds --help` does not list: `A run made in a build that enforces a licence also shows the licence it was made under; a build without a vendor key enforces none (docs/mvp/LICENCE.md §19).` |
| H8 | `report` | `Write a run's reports, masked, as <run>-<name>: the CSV exports and the PDF the API serves, or the P0 summary (report.md and report.html).` and, as a paragraph of its own: `A run made in the unlicensed evaluation of a build that enforces a licence is written as <run>-unlicensed-evaluation-<name>; a build without a vendor key enforces none (docs/mvp/LICENCE.md §19).` |
| C1 | `auth check`, exit 2 | `error: ds auth check needs the auth.oidc settings: these settings give tokens mode` (or `developer mode`) |
| C2 | `serve`, exit 2 | `error: cannot listen on 127.0.0.1:8000: the port is in use; stop the program that holds it, or pass --port` |
| C3 | `serve`, exit 2 | `error: cannot listen on <host>:<port>: <strerror>` (any other `OSError`). `--port` is an integer from 0 to 65535: any other ends as a mistyped option does, exit 2, before the settings are read (`Invalid value for '--port': 70000 is not in the range 0<=x<=65535.`) |
| C4 | `serve`, stderr | `warning: the web UI is not built (<WEB_DIST> is missing): / answers 404 and only the API under /v1 is served; run 'npm --prefix web ci' and 'npm --prefix web run build' in the checkout, then start ds serve again` |
| C5 | every command, exit 2 | `error: the database answered and asked for a password, then refused the connection: check the user, the password and the database name in the file database_dsn_ref names; if they are right, the server, the role or the database is at its connection limit, or a pooler cannot reach its server` |
| C6 | every command, exit 2 | `error: the file database_dsn_ref names does not hold an address the driver can read: percent-encode special characters in the user and the password` |

- **C2, C3.** New `_bindable(host, port)`: `socket.create_server((host, port), family=AF_INET6 if ":" in
  host else AF_INET).close()`, with the options asyncio binds with; an `OSError` becomes a
  `SettingsError`. C3's `<strerror>` is the system's words alone (`Can't assign requested address`):
  `create_server` appends ` (while attempting to bind on address …)`, which is cut. It is the last statement of `serve`'s first `_errors()` block: after every settings
  and credential refusal, and before the store is opened, so no `system.start` row is written and no
  worker starts. The option is bounded (`typer.Option(min=0, max=65535)`) because `create_server`
  answers a number outside that range with an `OverflowError`, which is no `OSError` and would end as
  a bug does, with a traceback and exit 3.
- **C4** is printed after that block, when `api.app.WEB_DIST` is no directory.
- **C5, C6** are S3's, below. "Every command" is every command that opens the store, `ds keys migrate`
  among them, and the app's start.
- **H7, H8.** Only a run made by a build that enforces a licence carries a licence line or the
  `unlicensed-evaluation` file name (LICENCE §19): an older build's, or a vendor build's. The first
  paragraph of a help text is what `ds --help` lists, and it no longer speaks of a licence.

Elsewhere:

| # | Where | Behaviour |
|---|---|---|
| S1 | `api/app.py create_app` | Without a `web_dir`, `GET /` answers 404 `{"detail": "the web UI is not built on this server; its API is under /v1"}`. Other paths stay `Not Found`. The route is added with `include_in_schema=False`: it is no operation of the API, whose schema the role tests read (AUTH §4.1), so they pass in a checkout whose UI is not built. |
| S2 | `api/routes/meta.py health` | `ctx.pool.connection(timeout=3)`; a `psycopg.OperationalError`, `PoolTimeout` among them, becomes 503 `{"detail": "the database is not answering, or the pool gave no connection in 3 s"}`, with the guard's headers. It comes after 3 s when the pool hands out no connection (it holds none, the database being down, or every one is busy), and at once when the connection it hands out fails, which a pooled connection does once the database has closed it: the text names no wait as passed. Three seconds is under the image health check's four. Only the wait for a connection is bounded, not the statement (§14). |
| S3 | `store/db.py` | `DatabaseRefused(ValueError)` and `async admitted(dsn)`. **The address first, with no connection:** one that `psycopg.conninfo.conninfo_to_dict` cannot parse (a `ProgrammingError`, whose text quotes the address, or a `UnicodeError` for a `%XX` that is not UTF-8), or one whose host or database name, as the driver read them, holds an `@` (an unencoded `@` or `/` in the user or the password, whose tail the driver reads as a host or a database name and a connection error would print; an `@` in a value of the query, a `user` or an `application_name`, is the driver's to take), raises `DatabaseRefused` with C6's text. **Then the probe:** `psycopg.AsyncConnection.connect(dsn, connect_timeout=5)`, closed at once, up to `TRIES = 3` times with `APART = 2.0` seconds between two. An `OperationalError` whose `pgconn` is set and reports `needs_password` (the server asked and the address gives none) raises `DatabaseRefused` with C5's text at once. One that reports `used_password` alone is tried again, and raises it after the third try: a server, a role or a database at its connection limit, and a pooler that cannot reach its server, refuse a right password in the same way, with no SQLSTATE to tell them apart, and such a limit is often free again in seconds. A try that connects, or fails in any other way (any other `psycopg.Error`), returns, and the pool's wait applies as today. It is called before the pool opens in `Context.open` and in `ds migrate`, and before the lock's connection, its first, in `security/legacy.py migrate` (`ds keys migrate`). The driver's text is never printed, nor a piece of the address. |
| S4 | `store/db.py` | `WAIT = 30.0` and `opening()`, a context manager: while it is active, a filter on the `psycopg.pool` logger passes each distinct message once. It spans the wait for the first connection: `opened.open(wait=True, timeout=WAIT)` in `Context.open`, and in `ds migrate` a new `await pool.open(wait=True, timeout=WAIT)` as the first statement inside its `async with` (entering the pool does not wait, so today its lines are logged later, during the migration). A server's later logs are unchanged: once its start is over the pool logs every failed try again (§14). |
| S5 | `runner/worker.py _sole` | `another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops` |

`the database is unreachable (<class>); check database_dsn_ref` stays for every other case, so MCP §12's
line, RUNBOOK's row and the canary test stand; the pool's lines before it are now one for each wording
of the driver's, not five: one as a rule, two when libpq words the refusal both ways (§11). Such a line
names what the driver read of the address, the host and the port, and from a server's refusal the
account, the database and the client's address. It holds no piece of the password: an address that
would put one there is C6's.

## 9. Interfaces, in one place

- **Settings.** None new. A relative `file:` reference is now the settings file's (§5).
- **Routes and roles.** None new, and roles are AUTH §4.1's. `GET /v1/health` (public) may answer 503
  (S2). `GET /` without a built UI answers S1 (public). `POST /v1/auth/token-login`, `GET
  /v1/auth/login`, `GET /v1/auth/callback`, `GET /v1/auth/me`, `POST /v1/auth/logout` and every
  cookie-authenticated route take E1's names.
- **Audit.** No new action. A token sign-in whose cookie the browser drops still writes `auth.login`.
- **CLI.** No new `ds` command. `ds serve` gains C2 to C4 and its `--port` a range; a refusal exits 2,
  like every expected failure of `ds`. `python -m mocks.run all` and `deploy/up.sh` are new entry points
  outside `ds`.
- **UI.** §6 only.
- **Migration.** None.
- **Outbound.** None from DeltaSleuth. The script's two `curl` calls ask `127.0.0.1` and `[::1]` of its
  own machine, past any proxy. Its `docker compose build` and first `run` fetch what Docker and the package indexes
  hold, as every source build does (I4).

## 10. Release

`tools/release.py`: `SHIPPED` gains `"docs/guide"` after `"docs/mvp"`, and nothing else: the script,
both new examples and `mocks/targets.yaml` lie under `deploy` and `mocks`. `release.TOOLS` is unchanged;
RELEASE §13 says that the checks, which run under `DS_REQUIRE_TOOLS=1`, need `shellcheck` on the build
machine, as they need `openssl`. `tests/unit/releases.py` adds `docs/guide/01-install-docker.md` to the
scratch checkout (`git archive` fails on a pathspec that matches nothing), and RELEASE §2 lists the
entry. The guide's index, `docs/guide/README.md`, ships with the chapters. Chapter 2 ships whole: its
section "For people who change the code: lint and tests" (as built it has no number and follows the
main path) says in its first paragraph that it needs a git checkout, since a release kit holds no
`tests/` and no `tools/`.

## 11. Documents to amend

- **AUTH.md.** §1: item 3, and Health (503, what its wait bounds and what it does not). §2: the login
  cookie's name under E1. §3: E1's rows in the cookie table, and a paragraph "A plain-http
  `public_url`" holding §7 with its observation, its date, what was not observed, that a plain cookie
  reaches every http service on that loopback name, the session swap, and that a move of `public_url`
  signs browsers out and ends no session. §4.2:
  C1. §9: the three UI texts and `tokenLogin`. §10: the tests, and a client at `http://localhost:8000`
  beside the https ones.
- **DEPLOY.md.**
  - Introduction: a one-machine trial is `sh deploy/up.sh` or the guide's chapter 1, never this
    procedure; `cp deploy/env.source.example .env` beside the source build's `printf`.
  - Changes take effect: before a restart, `docker compose run --rm app ds licence` opens what a start
    opens but for the IdP's credential and the packs' signing key (the three files of `deploy/config`,
    the keys and the database, applying pending migrations as a start would) and prints `installation
    I-…` and the lines after it, or the `error:` line a start would log. `ds init` reads
    `deltasleuth.yaml` alone, and `ds auth check` the IdP.
  - Section 2: the tag is `<project>:source`, `deltasleuth:source` by default.
  - Section 3. Settings and Sign-in: the copy is a trial until `public_url` is set and the `# ` of
    `trusted_proxies` and of the oidc lines is removed. Secrets: the four base secrets go straight to
    their files (no `pw=` variable, no glob `chmod`); a `db_dsn` written by hand percent-encodes the
    special characters of its user and password (C6); `oidc_client_secret` moves to section 5 and
    `llm_key` to section 8, each with "name it in `app.secrets` and remove the `# ` of its line". The
    tokens-mode "delete" sentence goes. "A key instead of a secret" names `oidc_client_key` in
    `app.secrets` and makes the commented `oidc_client_secret` line the key's: the example no longer
    holds a name to replace wherever it appears. "Upgrading from the shared API token", step 2, says
    that the example holds the IdP secret as a commented line.
  - Section 5, Start: "There is no plain-HTTP option beyond this machine: under https the cookies are
    `Secure` and `__Host-`; a loopback `http://` public_url, a one-machine trial's, gets plain cookies
    (AUTH.md §3)." And: an app that never turns `(healthy)` has its reason as the last `error:` line of
    `docker compose logs --tail 20 app`.
  - Acceptance checklist, new item 0: `curl -s $DS/v1/health` reads `"auth": "oidc"`; `tokens` means the
    oidc lines are still commented.
  - New "From a one-machine trial", before Moving from a source checkout. Revoke every token the trial
    made (`ds token list`, `ds token revoke T-…`), since a bearer token keeps working under SSO, and
    every session (`ds session revoke --all`), since the new address ends none (§7), and remove
    `.deltasleuth/tokens`. Set the https `public_url`, switch on `trusted_proxies` and oidc, and
    run `ds auth check`. Stop `mocks` and delete its four targets. Vault `kek` and `tokeniser`, or start
    on a new database. Install the signed release in place of the source build. Everyone signs in again.
  - Section 8, AI off, and section 10, Moving from a source checkout, step 4: reworded for commented
    lines.
  - Section 11: the "password authentication failed" row becomes C5's, its fix without the password on
    a command line, with its other causes (a connection limit, a pooler) and the TLS-only server of §14; a row
    for C6; a new row for C2 and C3, and C4 and S1 in the row of the UI's 404. TLS:
    `BASE` and the `docker build -t` of a source build read `<project>:source`, and a sentence says that
    the CA layer's tag, `deltasleuth:<version>-ca`, is one a machine's installations share.
- **RELEASE.md.** §2: `SHIPPED`. §6: the site example, the source override and `.env`'s project name,
  quoted anew. §12's outline: "`__Host-` sessions, and E1's plain cookies on loopback http". §13: the
  test rows, and shellcheck.
- **PENTEST.md.** Sessions: the cookies are `__Host-` under https; E1's plain ones, which a loopback
  http `public_url` gives, are named as in scope, with §7's two residuals (the session swap, and a
  session that outlives a move of `public_url`).
- **CONTRACTS.md.** §2: the `file:` rule. §7: health 503. §8, Sign in: the token hint, the address hint
  and `NOT_KEPT`. §9: `<project>:source`, and the script. §10: the testing note names the
  `http://localhost:8000` clients beside `https://ds.test`.
- **MCP.md.** §12: one `psycopg.pool WARNING` line precedes the unreachable line, not five, and a
  second when the driver words the refusal both ways (S4); C5 and C6 come with no pool line, and the
  TLS-only server of §14 is the exception.
- **RUNBOOK.md.** Setup: the developer example, an absolute `DS_CONFIG`, and H6 in place of "back both
  up to the vault". Try it on the mock pairs: `mocks/targets.yaml` and `python -m mocks.run all`.
  Troubleshooting: C2, C4, C5 with its other causes and the TLS-only server, C6, and S5's wording.
- **RETENTION.md.** §8's `ds init` row (H6).
- **README.md.** Quickstart: `sh deploy/up.sh` first, then the guide's index for a newcomer; the
  developer path as `cp deploy/config/deltasleuth.dev.example.yaml .deltasleuth/deltasleuth.yaml` and
  `uv run python -m mocks.run all`; this contract in the list.
- **docs/guide/README.md** (new): the guide's index. What DeltaSleuth is, which of the three
  files to read for what, and that the chapters on its features follow.
- **docs/guide/01-install-docker.md and its appendix, docs/guide/A1-install-by-hand.md** (the chapter
  is the script's short path, and the steps by hand are in the appendix).
  The chapter's section 1 says what a first run downloads (the header's sentence), then the script,
  the sign-in, `ds`, stop, start, update and removal. The appendix takes the same steps by hand: three
  `cp` for the settings, `cp` of the site example and of the env example; no `sed` and no deletion.
  What it says of the sign-in is written from §13's observation only. Its "A second installation" is
  §4, and its "If something goes wrong" takes the new lines. Every line they quote of the script is
  the script's as committed, which `test_guide.py`
  holds for the entries they head with an `error:` or `warning:` line and for the closing block; a
  line worded after their runs on Docker is marked "Not verified here". The chapter gives the Linux
  clipboard commands beside the macOS ones, and names what the shell says when the script is typed
  outside a clone. It says what a run that ends in `error:` needs: the appendix's entry for it is
  V5's, for `ds token list`. Its "Next" names the runbook's "Try it on the mock pairs" until a chapter
  on a first comparison exists; a comment there names `docs/guide/03-first-comparison.md`. The chapter
  stays within 260 lines.
- **docs/guide/02-run-from-source.md.** Section 6 by `cp`; section 8 as one command; sections 7, 9 and
  10 and "If something goes wrong" take H6, C2, C3's range, C4, C5 with its three tries, C6, S2's text
  and S5. As built, its numbered sections end with 11: "How this differs from chapter 1" and "For people
  who change the code: lint and tests", once sections 12 and 13, follow the two alternatives a reader
  of the main path is sent to (a PostgreSQL server of one's own, a second installation), and the
  second takes §10's sentence. Section 1 states the versions it was written with, and the introduction
  and the steps concerned send a reader to the two alternatives. Both chapters lose the licence limit
  (LICENCE §19), and neither names a commit.

## 12. Tests (all offline; named)

**`tests/unit/deploy/test_config_examples.py`.** `MARK` is `## ---- optional blocks`; `on(text)` is
`re.sub(r"(?m)^# ", "", text)`; `office(head)` is `on(head)` with the https `public_url`.
- `test_the_examples_start_a_trial_as_copied` (replaces `…load_and_serve_the_mock_specs`): `tokens`,
  `http://localhost:8000`, `trusted_proxies == ()`, no `llm_profiles_file`; both mock specs compile;
  every `file:/run/secrets/<name>` the settings name is in the base file's `app.secrets`; the pinned
  `public_url` text occurs once.
- `test_one_rule_switches_every_setting_on`: every line that starts `#` starts `# ` or `##`; `on` of
  the whole file, with the https `public_url`, loads with `oidc`, the chat profile and every block.
- `test_the_office_lines_load_when_uncommented`: `oidc`, `("172.30.0.1/32",)`, the client secret
  reference, `roles["admin"] == ("DS.Admin",)`.
- `test_the_example_optional_blocks_load_when_uncommented`: applies `office` first and gains the chat
  profile; the optional section alone is refused with the packs line.
- `test_only_the_example_chat_profile_may_receive_what_the_chat_sends`: applies `on` first.
- `test_site_example_adds_no_secret_until_its_lines_are_on` (replaces `…restores_todays_secrets`): as
  copied the document equals `{"services": {"app": {"secrets": []}}}`; after the documented edit,
  today's equality.
- `test_the_source_override_builds_once_and_tags_by_project`: one `build`, under `app`; both images are
  the text `${COMPOSE_PROJECT_NAME:-deltasleuth}:source`.
- `test_the_source_override_shows_ds_the_checkouts_mocks_and_examples`: the override's `app.volumes` are
  the two read-only mounts of §2; the base file's app mounts the settings and its three volumes only;
  the image's runtime stage works in `/app` and copies neither folder; the bundled specs lie where
  `../mocks` and `../examples` point from `deploy/`.
- `test_the_env_example_is_the_source_builds`: its one active line is DEPLOY section 2's value, and
  `deploy/up.sh` holds no `docker-compose.yml:`.
- `test_every_compose_file_value_keeps_the_site_file` also reads the env example and
  every chapter of `docs/guide`. As built: chapter 1 runs the script and its appendix copies the env
  example, so neither gives a value of its own, and a chapter is not required to; a value one gives is
  held to the rule, and U12's `COMPOSE_FILE=…`, which the appendix quotes, is no value.
- `test_the_examples_render_in_compose_as_copied`: `docker compose config` on a temporary copy, the
  default and a second installation, each with the app's host folders (`deploy/config`, `mocks` and
  `examples`, read-only); and with a release's `.env`, which names no source override: the release
  image, no build, and `deploy/config` as the app's one host folder. Skipped without docker, failing
  under `DS_REQUIRE_TOOLS=1`.
- `test_the_developer_example_runs_from_a_checkout`: copied to `<tmp>/.deltasleuth/` and loaded from
  another working directory: `auth_mode == "none"`, the reference is `<tmp>/.deltasleuth/db_dsn`,
  `init_workspace` makes the keys, both mock specs compile against `mocks/targets.yaml`.

**`tests/unit/deploy/test_up_script.py`** (new). A scratch checkout; a `PATH` of one folder that holds
links to I5's POSIX tools and `openssl`, and stand-ins for `docker`, `curl`, `sleep` and `uname` that
follow a scenario and log argv and environment, so that any other program is "not found"; real `sh`,
and `dash` where installed, each test under both. The stand-in's `compose config` is a small model of
Compose's (project, images, published port, subnet, `!override`); `test_config_examples.py` renders the
same files in Compose itself. Its `docker ps` prints each container through the `--format` it is given
and, asked for the app service's containers, those of `docker compose run` too unless the `oneoff=False`
filter is there; its `docker inspect` refuses a name it does not hold, as Docker does two ids in one
word. In a *watched* trial the real tools log their words too, so that every argv of a run is read.
Each message is also matched against its row of §3.2 or §3.4.
- `test_up_sh_passes_shellcheck` (skipped without shellcheck, failing under `DS_REQUIRE_TOOLS=1`).
- `test_help_prints_the_header_and_calls_nothing`: §3's text; the three images it names are the
  Dockerfile's two pins and the base file's `db` image.
- `test_a_fresh_clone_gets_every_file_and_a_token_that_is_never_printed`: the files created are exactly
  §3.3's ten; the copies are byte-equal to the examples, `.env` among them; secrets 0444 in 0700 and
  read by `read_secret`; `db_dsn` built from `db_password`; the token 0600; no secret or token in stdout,
  stderr, any argv or any environment value; no argv has a subcommand of I4, and `-f` occurs in none
  (no site file existed); the calls in order; §3.5 verbatim, its 90 being `tokens.DEFAULT_DAYS`.
- `test_running_again_overwrites_nothing`: also with the options given again, and `--port 8000
  --subnet 172.30.0.0/24` beside the plain copies (U15 judges the rendering); the folder under another
  spelling than Docker recorded.
- `test_settings_and_secrets_that_exist_are_never_overwritten`: nine files written by someone else stay
  byte for byte, mode and time included; `db_dsn`, alone missing, is made from their `db_password`,
  which no output, argv or environment carries.
- `test_secrets_and_the_token_are_private_whatever_the_callers_umask`: under a caller's umask of 0, 027
  and 077 the secrets are 0444 in a folder of 0700, made by the script or there already, and the token
  0600 in folders of 0700.
- `test_a_kept_password_an_address_cannot_carry_is_refused_unprinted` (U26): a kept password with `%`,
  `/` or `@` and no `db_dsn`; the folder unchanged, and the password in no output, argv or environment;
  one of `A-Za-z0-9._~-` goes into `db_dsn`.
- `test_a_link_to_a_file_that_is_not_there_is_never_replaced` (U27): the four secrets, the settings and
  `.env` as links into a folder that is not there; the folder unchanged and nothing made at the links'
  ends; with their files there the links are kept, and stay links.
- `test_options_make_a_second_installation`; `test_options_never_change_an_existing_file`.
- `test_every_row_of_the_contracts_table_is_tried`; `test_a_fresh_folder_passes_where_only_its_own_things_are_there`
  (Compose `2.20.0` without options, the project's own network, another installation that publishes
  18000 to its 8000, no `curl`).
- `test_port_alone_moves_a_first_installation_whose_port_is_taken`: port 8000 published by a container
  (U17) or answering (U18), each message ending as the header says; then `--port 18001` alone runs
  through as project `deltasleuth` on the base file's subnet, the port in the settings and in the site
  file's one `ports` line, `.env` the example.
- `test_it_takes_over_no_other_folders_compose_project`: a running or a stopped container of another
  folder, a folder that is gone, one such container beside the folder's own, and containers that carry
  the project's label and no folder's (alone, and listed before and after the folder's own), in a fresh
  folder and in a complete one: U16, the folder unchanged, and that `docker ps -a` the last call; a
  `docker ps` that fails ends the run too.
- `test_every_early_refusal_creates_nothing`: every row of §3.2, each message exact, the folder
  unchanged. Among the cases:
  - `--project ds-` and `a__-b`; the same names, a quoted name, `export` and a second line in `.env`;
  - `.env` without a project line beside `--project b` (U11 names `deltasleuth`); the `.env` that
    U13's advice makes passes with its added line;
  - settings with `auth.oidc` on, without an auth block, with a quoted `public_url`;
  - a site file that publishes 8001 with no option, and Compose `2.20.0` with `--port` (U24);
  - `db_dsn` alone (U21), and `db_dsn` alone missing (passes); a stopped container of another folder
    (U16, whose argv holds `-a`); a `.new` file left behind (U23), and a `.new` that is a link to
    nothing, through which nothing is written;
  - each of the ten as a link to a file that is not there (U27);
  - a `deploy/secrets` of mode 755, 750, 711 and 701, empty, and one of 755 that holds `db_password`,
    `kek` and `tokeniser` (U25); the tests' other secrets folders are 700, as DEPLOY's command makes
    them;
  - a kept `db_password` with `%`, `/`, `@`, a blank, `:` or a letter beyond ASCII, and no `db_dsn` (U26).
- `test_the_port_probe_asks_no_proxy`: `http_proxy` set in the script's environment; `curl`'s two argv
  are U18's, word for word, `127.0.0.1` first; exit 7 from both lets the run go on, 0 and 28 from
  either refuse. The real `curl`, given those words, answers 7 for `127.0.0.1` and 0 for `[::1]` on a
  port where a program listens on `[::1]` alone (skipped on a machine without that address).
- `test_it_refuses_an_office_or_a_release_installation`: https, a host name, a two-file `.env`, a
  loaded release image; no `ds`, `up` or `token` call follows.
- `test_an_interrupt_leaves_no_new_file`: HUP, INT, QUIT, PIPE and TERM during a step; exit 129, 130,
  131, 141 and 143.
- `test_a_restart_loop_ends_at_once_with_the_apps_last_error_line`; a count that was not 0 and does not
  rise is no failure.
- `test_the_health_wait_reads_the_services_own_container`: a container of `docker compose run` beside
  the service's own; step 5's `docker ps` argv, one `docker inspect` of the service's id, no wait, and
  the token made; of two ids of the service the first is read.
- `test_later_refusals_keep_what_exists` (V1 to V7; a second run then completes the rest and rewrites
  nothing); `test_an_example_without_its_address_line_gets_no_other_port` (V8);
  `test_the_closing_block_names_the_desktops_clipboard_tool_and_runs_none` (§3.5 outside macOS, each
  tool installed and not); `test_a_failed_token_leaves_no_file`;
  `test_a_kept_token_the_database_does_not_know_is_warned_of`: another active `first-run` in the list
  does not silence W1; the file's own id, active, does; a `ds token list` that fails ends the run with
  V5 under the line `ds` printed, with every step's title and no closing block.

**`tests/unit/deploy/test_guide.py`** (new). The tests that read "the guide" read every
`docs/guide/*.md` there is, so a chapter added later is held to them and breaks none; those of the
script read chapter 1 and its appendix, `docs/guide/A1-install-by-hand.md`, where the steps by hand
are (§11).
- `test_the_guide_shows_every_step_up_sh_takes`: each `docker compose` command the stand-in logged in
  a fresh run from step 1 on, the caller's name written `"$(id -un)"`, and each file created, is in a
  code block of chapter 1 or of its appendix.
- `test_the_guide_needs_no_sed_before_a_second_installation`: no code block of the two runs `sed`; the
  five copies of §11, §4's script line, and its edits by hand, four lines of three files, are there,
  each edited line being its example's own, with the `# ` removed and `<project>`, `<port>`,
  `<subnet>` or `<gateway>` in the example's value's place.
- `test_guide_links_resolve_inside_the_kit`: every relative link of `docs/guide/*.md`, and README's
  links into `docs/guide`, name a path under `release.SHIPPED` (README also links
  `docs/architecture.md`, which does not ship).
- `test_every_guide_link_leads_to_a_file_that_exists` (as built): the same links lead to a file and,
  where one names a heading, to a heading of that file, by the anchor GitHub gives it.
- `test_a_guide_step_outside_the_kit_says_so`: a `##` section whose commands (its `bash` blocks) name
  `tests` or `tools` holds "needs a git checkout"; chapter 2 has one.
- `test_the_guide_holds_no_host_path`: `/Users/`, `/home/`, `/private/` and `/var/folders/`, the
  release tool's needles for a bundle.
- `test_the_guide_quotes_the_script_word_for_word` (as built): every line that heads an entry of
  chapter 1, or of the appendix's sections "The script …", as an `error:` or a `warning:`, and every
  line a chapter shows from `DeltaSleuth is running:` on, is one `deploy/up.sh` holds: each text of a
  `die`, its warning and its closing block, the texts it names in capitals written out and any other
  variable as any text.

**Elsewhere.**
- `tests/unit/test_config.py::test_a_relative_secret_reference_is_the_settings_files`: relative,
  absolute, a list, a bare `file:`, another working directory; a relative key inside `workspace_dir` is
  refused. The test that refuses `http://ds.example` gains a comment naming E1.
- `tests/unit/auth/test_sessions.py::test_only_an_http_public_url_gives_plain_cookies_named_by_port`
  (new file): `ds_session_8000`, `ds_login_18000`, `ds_session_80` for `http://localhost`; the https
  names unchanged, and an http address beyond loopback, built past the loader, never plain.
  `test_developer_mode_has_no_plain_cookie`.
- `tests/unit/api/test_auth_flow.py`:
  - `test_an_http_loopback_public_url_sets_cookies_a_strict_jar_returns`: tokens mode at
    `http://localhost:8000` sets `ds_session_8000` with exactly `HttpOnly; Path=/; SameSite=strict`; the
    test client's own jar, which returns no `Secure` cookie over http, then gets 200 from `/v1/auth/me`
    and from a CSRF write; logout expires it; the secret sent as `__Host-ds_session`, as `ds_session` or
    as `ds_session_18000` gets 401 `not signed in`.
  - `test_two_loopback_installations_share_a_jar_and_keep_their_sessions`: apps at
    `http://localhost:8000` and `http://localhost:18000`, one cookie jar: after both sign-ins each
    `/v1/auth/me` answers 200, and signing out of one leaves the other signed in.
  - `test_an_https_public_url_keeps_the_prefixed_secure_cookies`: `https://ds.test` and
    `https://localhost:8443`; the `Set-Cookie` header whole, byte for byte; a valid secret sent under
    a plain name gets 401.
  - `test_a_request_cannot_choose_the_cookie_form`: under `https://ds.test` a sign-in sent with
    `Host: localhost:8000`, which the Host check admits, and `X-Forwarded-Proto: http` sets the
    prefixed `Secure` cookie, and its secret under `ds_session_8000` with a loopback `Origin` is 401;
    under `http://localhost:8000` one sent with `Host: 127.0.0.1:8443` and `X-Forwarded-Proto: https`
    sets `ds_session_8000`, and no other name is read.
  - `test_the_login_cookie_follows_the_same_rule`: also that a callback carrying the sealed state as
    `__Host-ds_login` is `login_expired`, and the https header whole.
- `tests/unit/api/test_app.py`: `test_health_says_when_the_database_does_not_answer` (a pool that raises
  `PoolTimeout`, and one whose connection fails at its statement: S2's one text for both);
  `test_the_ui_is_served_with_a_single_page_fallback` gains S1's body.
- `tests/unit/api/test_roles.py::test_every_operation_is_listed` builds the app twice, over a directory
  and over a path that does not exist, so the operations are compared with AUTH §4.1's in a checkout
  whose UI is not built too, where S1's route exists.
- `tests/unit/cli/test_cli.py`:
  - an autouse fixture replaces `_bindable` and points `api.app.WEB_DIST` at a directory: the existing
    `serve` tests use port 8000, which may be taken on the machine that runs them, and assert its stderr whole,
    which C4 would lengthen in a checkout whose UI is not built (the release's checks run in one). For
    the same reason `test_serve_loads_the_mcp_library_before_it_opens_the_store`, a process of its
    own, reads the lines that are not warnings;
  - `test_serve_refuses_a_taken_port_before_it_opens_the_store` (a held socket; `_in_context` and
    `uvicorn.run` must not run; one `error:` line; C3 with `--host 192.0.2.1`, no address of this
    machine; `--port 70000` and `--port -1` exit 2 with the option's range and no traceback);
  - `test_serve_warns_when_the_ui_is_not_built`;
  - `test_no_help_text_loses_words_to_markup` (`rich.text.Text.from_markup(text).plain == text` for
    every help of the command tree);
  - `test_help_says_what_ds_does` (H1 to H5; no `ds spec or`; H4 holds `outside SSO`; H7 and H8, whose
    lines in `ds --help` end before any word of a licence);
  - `test_a_refused_database_is_one_line` (C5 from `runs`, `migrate`, `token list` and `keys migrate`,
    with `db.APART` set to a tenth of a second);
  - `test_an_address_the_driver_cannot_read_is_one_line_without_a_piece_of_it` (C6 from `runs`,
    `migrate` and `keys migrate` for a password with an unencoded `%`, a space, a `%XX` that is not
    UTF-8 and an `@`; `connect` must not be called, and no `psycopg` record is logged);
  - `test_migrate_logs_one_pool_line_for_a_closed_port`: `db.WAIT` set to 2.5 s, in which the pool
    tries twice (three connections are counted, `admitted`'s first); one `psycopg.pool` record for
    each wording of those two tries, which is one unless libpq worded them differently; exit 2, the
    unreachable line;
  - `test_the_documented_precheck_reads_all_three_files`: with a mistake in `targets.yaml`, and then in
    `llm_profiles.yaml`, `ds licence` exits 2 with the start's line while `ds init` exits 0;
  - the `init` and `auth check` tests take H6 and C1; a `token list` line starts `T-<id> (name)` and
    holds `  active  `.
- `tests/unit/store/test_context.py`, with `db.APART` shortened and every `connect` counted:
  - `test_admitted_tells_a_refusal_from_no_answer` (the local `postgres:16-alpine`: a wrong password, a
    wrong user and a wrong database raise C5 after three tries, each `APART` after the one before, and a
    missing password after one; the right ones, a closed port and `port=x` return after one);
  - `test_a_connection_limit_that_frees_is_no_refusal` (a role with `CONNECTION LIMIT 1` and its right
    password, one connection held: C5 after three tries while it is held, and no refusal when it is
    closed between the tries);
  - `test_an_address_the_driver_cannot_read_is_refused_before_any_connection` (C6 for an unencoded `%`,
    a space, a `%XX` that is not UTF-8, an `@`, a `/`, an unclosed bracket and a keyword without its
    value; no `connect`, and neither the refusal nor its traceback holds a piece of the password);
  - `test_a_refusal_never_carries_the_drivers_words` (a canary);
    `test_a_pool_line_is_logged_once_while_the_store_opens` (the same message three times and another
    once inside `opening()` leave two records; a repeat after it passes).
- `tests/unit/runner/test_worker.py`: the held-lock case asserts S5.
- `tests/unit/mocks/test_mock_run.py`: `all` parses, refuses `--variant` or `--port`, serves four
  `/__health` answers on free ports in a process of its own, which a TERM ends with 0, and starts nothing
  on a taken port (the two sites bound before it are closed when the refusal is printed).
  `test_mock_specs.py::test_the_combined_targets_are_the_pairs_own`: equality, and `PORTS` equals the
  ports of every targets file and of the Compose command.
- `tests/unit/test_release_tool.py::test_build_writes_the_bundle` compares
  `{e.split("/")[0] for e in release.SHIPPED}` and asserts that the guide file shipped.
- `web/test/signin.test.ts`: each text exact, for `127.0.0.1:8000`, `localhost:18000`, `[::1]` and an
  https host; no hint for another detail; the module holds no `import` and no `fetch`.
  `web/test/api.test.ts`: POST 200 then GET 401 rejects with `NOT_KEPT`; a refused POST keeps the
  server's detail; both 200 resolve with `/me`'s CSRF token.

## 13. Acceptance

Acceptance, on a machine that pulls no image:
0. `docker image inspect` finds the Dockerfile's two pinned images and `postgres:16-alpine`. One missing
   stops the acceptance: the script would fetch it. (A build whose cached layers are gone fetches Python
   and npm packages, as every source build here has.)
1. In a scratch clone, with `http_proxy` naming a dead address and a local program listening on 18021,
   `sh deploy/up.sh --project dsfirst --port 18021 --subnet 172.31.20.0/24` is refused with U18 and
   creates nothing. In a plain shell the same command with `--port 18020` runs through. The containers
   of the machine's other installations keep their age. A second run changes no file.
2. A second scratch clone, by hand as chapter 1's appendix says, with its own project, port and subnet:
   `docker compose --profile mocks up -d` with the image absent builds once and starts both services;
   after a source change,
   `docker compose --profile mocks up -d --build` leaves one image id under `<project>:source`, no
   dangling image of the project, and both containers on that id.
3. One sign-in with the saved token in Safari and one in Chrome, and in Firefox if installed, and both
   scratch installations signed in at once in one browser: versions and results go into AUTH §3 and the
   guide. Anything not seen stays "Not verified here".
4. A wrong `db_password` in a throwaway copy shows C5; a misspelt setting shows V5 or V6; a mistake in
   `targets.yaml` shows in `docker compose run --rm app ds licence`, which prints `installation I-…`
   once it is corrected.
5. `docker compose down -v` in each scratch clone removes it.

Linux is not observed here, and the guide says so until it is.

## 14. Limits; not changed

- **The script is unobserved on Linux;** on a daemon it was run on macOS only (the Checked block). It
  sees equal subnets, not overlapping ones, which Docker reports at step 5. A clone made under umask 077 leaves `deploy/config`, `mocks`
  and `examples` unreadable to the container's user on Linux.
- **A first run downloads.** The script types no pull, but a machine without the three images or the
  package caches fetches them in steps 3 and 4. Nothing checks that for the reader beyond the header and
  the guide's section 1.
- **In a fresh kit folder,** without `.env` and without a loaded image, the script builds the kit's
  source unsigned. U12, U13, U14 and the closing block's first line stand against that; nothing else.
- **Volumes carry only the project's name.** A folder that holds all its secrets and reuses the name of
  a removed installation's volumes starts on them and stops at C5.
- **Tokens.** Names are not unique: a removed token file leads to a second active `first-run`. An
  interrupt between the token's creation and its file's move leaves an active token whose value is lost
  (`ds token list`, `ds token revoke`). The closing block's 90 days is the most; `ds token list` shows
  the day.
- **A `.new` file survives a kill -9 or a power cut,** and nothing else: HUP, INT, QUIT, PIPE and TERM
  end the script through its trap (I1). U23 names the file; three of the ten are not git-ignored and
  dirty the tree until removed. One left in step 6, `.deltasleuth/tokens/first-run.new`, may hold a
  token that is active: look at `ds token list` and revoke a `first-run` that no file of yours holds.
- **The port probe sees the moment it runs.** U17 and U18 look once, before anything is created. A
  program started later on `[::1]:<port>` is not seen, and still answers `http://localhost:<port>` in
  the app's place in a browser that tries that address first: Docker holds `127.0.0.1:<port>` only.
  On a machine without `curl` neither address is asked. The second call was seen on one machine only
  (curl 8.2.1: 7 for a closed port of `[::1]`, 0 for a listener there); a machine without an IPv6
  loopback address, and a `curl` built without IPv6, are not observed.
- **U25 reads the mode's nine letters.** An access-control list, which `ls` only marks after them, is
  not read, and neither are the folders above `deploy/secrets`.
- **U26 is stricter than an address needs:** it refuses every character outside `A-Za-z0-9._~-`, among
  them `:`, `#` and `?`, which the driver takes in a password as written.
- **E1 rests on cookie rules and one WebKit observation** until §13. A dropped cookie leaves a live
  session and an `auth.login` row. A plain cookie is sent to every http service on its loopback name.
- **The address hint is a guess** from the address bar; it says what a second refusal means.
- **A relative `file:` reference** that relied on the working directory now fails with `cannot read
  secret file <path>`; the release notes say so. Docker installations use absolute paths.
- **The probes are best effort.** `ds serve`'s port can be taken between the check and uvicorn's bind
  (then today's exit 3), and a host name is probed on IPv4 only. `admitted` adds a connection per
  command (two for `ds keys migrate`), and a `trust` database that refuses an unknown name still waits
  30 s. For a host name with several addresses the driver reports the last one tried: a refusal on an
  earlier address and no answer on the last still waits 30 s and reads "unreachable".
- **C5 cannot tell its causes apart.** psycopg gives no SQLSTATE for a failed connect, so every refusal
  that follows the password reads alike: a wrong user, password or database name; a connection limit
  (seen on 2026-10-05, on PostgreSQL 16: `too many connections for role` for a role's
  own limit, and `remaining connection slots are reserved for roles with the SUPERUSER attribute` for
  a role that is no superuser on a full server); a pooler that cannot reach its server (seen with a
  stand-in that answered in PgBouncer's words, not with a pooler). The three tries let a limit that
  frees within about 4 s pass (in the tests: a role with `CONNECTION LIMIT 1`); one that stays ends in
  C5, whose text names it. A wrong password is therefore said after about 4 s, not at once; only a
  missing one is said at once. The bundled `db` service's account is the image's superuser, which
  these limits do not bind.
- **A server that takes TLS connections only** (`hostssl` lines alone in `pg_hba.conf`), reached by an
  address without `sslmode`, does not give C5 for a wrong password. libpq's default, `prefer`, tries
  with TLS, is refused after the password, then tries without TLS inside the same connection and is
  refused before any password, which clears both flags. The command waits its 30 s, prints the pool's
  line with both of the driver's messages (the account, the database and the client's address) and
  ends `unreachable`. With `sslmode=require` or `verify-full` in the address the refusal is C5. Seen
  on 2026-10-05 (PostgreSQL 16, libpq 18.0); not closed in code; the bundled `db`
  service speaks no TLS to the app, so a Compose installation does not meet it.
- **C6 judges the host and the database name the driver read, not the password.** A host or a
  database name that truly holds an `@` is refused too, and every address the driver cannot parse is
  worded alike, a mistyped option of its query among them. An `@` in a value of the query passes
  (`?user=svc@REALM.EXAMPLE`, an `application_name`). Past it, S4 prints the driver's line once: it names the host and the
  port as the driver read them, and a server's refusal the account, the database and the client's
  address. The developer example's comment on `database_dsn_ref` (§5) does not speak of encoding; the
  line itself, DEPLOY section 3, RUNBOOK's setup and the guide's second chapter do.
- **Health** answers 503 for a pool that is busy for 3 s too, as its text says. It bounds the wait for
  a pooled connection, not the statement: when the database stops answering on a connection the pool
  holds without closing it (a stalled server, a dropped network path), health answers neither 200 nor
  503 while the statement waits, and each waiting probe keeps one connection of the pool; the image's
  health check gives up after its own 4 s. Seen on 2026-10-05 (a relay that stopped
  forwarding: no answer in 12 s). Not bounded in code here: a plain 3 s `asyncio` timeout around the
  block answered after 13 s in that check, the driver spending about 10 s on cancelling the
  statement, and the bound that answered in 3.0 s leaves the statement's task behind, unawaited.
- **A running server's pool lines repeat.** `opening()` spans the first wait only. Once a server is
  up and its database goes away, the pool logs `error connecting in 'pool-…'` at every failed try,
  many a minute, until the database is back (seen on a container on 2026-10-05). They are kept: a
  filter for the life of the process would also drop the same words at a second outage, hours later,
  and the log would then show nothing of it.
- **The pre-check** (`ds licence`) reads neither the IdP's credential nor the packs' signing key, and
  applies pending migrations.
- **Not changed:** audit rows as JSON lines on stderr (the SIEM copy); settings and targets read at
  start only; the eight placeholder targets of the targets example; the mocks' health check and exit
  137; no build identity in health; the Vite proxy's port; the linker pair's counts on a second run.
