# Deploying DeltaSleuth in the office

This guide takes one Linux host from a signed release, or a checkout built from source, to a running
DeltaSleuth that compares your endpoints and answers questions through your model gateway. A trial on
one machine is never this procedure: `sh deploy/up.sh` in a checkout sets one up, with token sign-in at
`http://localhost:8000`, [the guide's first chapter](../guide/01-install-docker.md) walks through it,
and [its appendix](../guide/A1-install-by-hand.md) takes the same steps by hand
([FIRSTRUN.md](FIRSTRUN.md) is its contract; From a one-machine trial, in section 10, is the way from
one to an office deployment). It runs two containers:

- **`db`**: PostgreSQL 16. It holds runs, results, masked findings and chat.
- **`app`**: `ds serve`. It runs the API, the UI and the worker in one process, on port 8000.

State lives in four Docker volumes:

| Volume | Holds | Treat it as |
|---|---|---|
| `pgdata` | The database: masked results and findings, and each run's raw keys, response headers and chat, sealed with AES-256-GCM | Production data |
| `evidence` | Response bodies, each encrypted with AES-256-GCM under a keyed name | Production data |
| `workspace` | Working files, and the targets declared in the product with their credentials, in clear (section 7) | Production data |
| `p0` | Samples, specs and reviewer sheets for the CLI (section 9), in clear | Production data |

The keys that open the sealed values and bodies are two files outside every volume and backup: the
keyring and the tokeniser key (section 3), held by the host administrators and the office vault. Raw
data expires: each cycle's raw keys, results, findings, evidence and chat are purged 90 days
(retention `standard`) or 365 days (`audit`) after the cycle ends ([RETENTION.md](RETENTION.md);
the [runbook](RUNBOOK.md#retention-purge-and-keys)).

The app makes outbound calls only to the targets you declare, to the LLM hosts you list, to your
identity provider (IdP) and to the notification channels you declare, directly or through the proxy you
name for them (section 3, Notifications). The MCP server, which lets a team's own agent read a run's
findings, adds no destination: it answers its callers, and its one work tool reads the targets a run
already declares (section 3, The MCP server). People sign in through the IdP, and machines use named API tokens; every
audit row names the person or token ([AUTH.md](AUTH.md)). This build needs no licence and enforces
none, and nothing calls out for one (section 3, The licence).

The installation's folder, `/srv/deltasleuth` here, holds the deployment kit that section 2 extracts
from the release (or a checkout, for a source build). Run every command from it. Once the kit is there,
copy the site file and name it in `.env`, which Compose reads from the working directory, so that every
command below and the cron (section 10) use both files without `-f`:

```sh
cp deploy/docker-compose.site.example.yml deploy/docker-compose.site.yml
printf '%s\n' 'COMPOSE_FILE=deploy/docker-compose.yml:deploy/docker-compose.site.yml' > .env
```

These two files run a loaded release. A source build, the only kind until a release exists, names a
third file in `.env` (section 2, Build from source): with only these two, `docker compose up` fails with
`No such image: deltasleuth:<version>`. `cp deploy/env.source.example .env` writes a source build's
`.env`, in place of the `printf` above.

The site file, `deploy/docker-compose.site.yml`, holds every compose change of this installation: the
optional secrets, target DSNs and the pack key, the PostgreSQL pin, limits and the subnet. Compose adds
its lists to the base file's. It and `.env` are in no kit and git-ignored, so no upgrade overwrites
them.

Never edit the tracked compose files (`deploy/docker-compose.yml` and its source and CA overrides): an
upgrade replaces them.

Run every compose command from this folder through `.env`: never with `-f`, and never under an exported
`COMPOSE_FILE`, which overrides `.env`. A command without the site file runs without its secrets, port
and limits, and, when the site file moves the subnet (section 5), recreates the network on the base
file's subnet, which cuts the running app off from `db`. `docker compose up -d --force-recreate` repairs
it; a plain `up -d` recreates the network but leaves the containers without their service names on it.

Changes take effect as follows:

- **The site file** (a new secret, a port): `docker compose up -d` recreates the containers.
  `docker compose restart` does not apply these changes.
- **`deploy/config/*.yaml`, or the content of an existing secret:** `docker compose restart app`.
  Before the restart, `docker compose run --rm app ds licence` opens what a start opens but for the
  IdP's credential and the packs' signing key (the three files of `deploy/config`, the keys and the
  database, applying pending migrations as a start would) and prints `installation I-…` and the lines
  after it, or the `error:` line a start would log. `ds init` reads `deltasleuth.yaml` alone, and `ds
  auth check` the IdP. (Seen in a container on 2026-10-05 for a misspelt setting, an unknown member of
  `targets.yaml`, a `kek` that is no key and a wrong database password; `ds init` passed the targets
  mistake.)

## 1. Prerequisites

- **Docker:** Docker Engine 24 or later with Compose v2 (2.24.4 or later for the site file's
  `!override`, which moves the subnet, section 5, or the host port). BuildKit is the default.
- **To verify a release** (section 2), on the Docker host: OpenSSL or LibreSSL, and `sha256sum` (or
  `shasum -a 256`). A release needs no build-time access: its image is loaded, never built or pulled.
- **PostgreSQL 16,** the `db` service's image, which no release carries: from your registry mirror,
  pinned by digest in the site file (its commented `db` line), or a `postgres:16-alpine` you loaded and
  verified yourselves.
- **Build-time access,** for a source build only (section 2, Build from source):
  - to Docker Hub for `node:24-alpine`, `python:3.13-slim` and `postgres:16-alpine`;
  - to PyPI and npm.

  Behind a proxy, set `proxies` in `~/.docker/config.json`. Docker also passes these settings to
  the containers, where DeltaSleuth ignores them.
- **Run-time access:** the host must reach every target endpoint and the model gateway **directly**.
  DeltaSleuth ignores `HTTP(S)_PROXY` on purpose, so no call goes through a machine you did not
  declare. Its health check ignores them too. The IdP is reached directly as well, unless
  `auth.oidc.proxy` names a proxy, and so are notification channels, unless `notify.proxy` names one
  for those with `proxy: true`.
- **Sign-in:** an OIDC IdP where you can register a confidential web application (section 5), and a
  TLS reverse proxy on the host, such as nginx, with a certificate for DeltaSleuth's host name.

## 2. Install a signed release

A release is one folder, `deltasleuth-<version>-linux-amd64/`, delivered as a tar of it whose sha256 is
on the transfer ticket. The folder holds the image, its SBOMs, scan and triage (`image/`, `sbom/`,
`scan/`), the deployment kit (`source/`), `SECURITY.md`, the release notes and the signature. Your key
register holds the release key's fingerprint and the verification procedure (`procedure.txt`), filed
once per key ([SECURITY.md](../../SECURITY.md), Verifying a release).

Install as a non-root user in the `docker` group, on the Docker host, in a folder of your own outside
the installation's, here `~/releases`. The commands name release 0.1.0:

1. **Check the transport tar:** its sha256 is the ticket's, it holds only files and folders, and only
   under the release's folder (both `grep` lines print nothing):

   ```sh
   cd ~/releases
   sha256sum deltasleuth-0.1.0-linux-amd64.tar
   tar -tvf deltasleuth-0.1.0-linux-amd64.tar | grep -v '^[-d]'
   tar -tf deltasleuth-0.1.0-linux-amd64.tar | grep -v '^deltasleuth-0.1.0-linux-amd64/'
   ```
2. **Extract it:** `tar -xf deltasleuth-0.1.0-linux-amd64.tar --no-same-owner --no-same-permissions`.
3. **Only then** copy the procedure your register filed with the key beside the folder, as
   `~/releases/FILED-RELEASE-PROCEDURE.txt`.
4. **Run the whole procedure** in the folder (`cd deltasleuth-0.1.0-linux-amd64`), in one sitting,
   typing each command from the filed copy, never from `VERIFY.txt`. A pass on another machine does not
   count: on the way to this host the image, `RELEASE.txt` and `SHA256SUMS` can be swapped together,
   and only step 2 here catches that. The procedure of `deltasleuth-release/1`:

   ```text
   # deltasleuth-release/1 verification
   Needs OpenSSL or LibreSSL, POSIX tools, sha256sum (or: sha256sum() { shasum -a 256 "$@"; }) and, for step 5, Docker. Run every step on the Docker host.
   0. Use the copy filed with the release key; VERIFY.txt must match it from its line 3 (no output):
      sed -n '3,$p' VERIFY.txt | diff - ../FILED-RELEASE-PROCEDURE.txt
      Line 2 names this release; never run it: this checks it and sets its values (no output):
      F='VERSION=[0-9]+\.[0-9]+\.[0-9]+(rc[0-9]+)? PLATFORM=linux-(amd64|arm64)'
      L=$(sed -n 2p VERIFY.txt | LC_ALL=C grep -Ex "$F") || echo "line 2 of VERIFY.txt is malformed"
      v() { printf '%s\n' "$L" | cut -d' ' -f"$1" | cut -d= -f2; }
      VERSION=$(v 1) PLATFORM=$(v 2)
   1. The key: prints a fingerprint your register lists as a DeltaSleuth release key, in service on step 4's signed date (a withdrawn key: only if step 4's digest is in its compromise advisory).
      openssl pkey -pubin -in release-key.pem -outform DER | sha256sum
   2. It signed the list:
      openssl dgst -sha256 -verify release-key.pem -signature SHA256SUMS.sig SHA256SUMS  -> Verified OK
   3. Files unchanged, none added (the grep prints nothing; every line OK; diff prints nothing):
      LC_ALL=C grep -vE '^[0-9a-f]{64}  [^/]' SHA256SUMS
      sha256sum -c SHA256SUMS
      find . -type f | sed 's|^\./||' | LC_ALL=C sort > ../have.txt
      { cut -c67- SHA256SUMS; printf '%s\n' SHA256SUMS SHA256SUMS.sig release-key.pem; } | LC_ALL=C sort > ../want.txt
      diff ../want.txt ../have.txt
   4. The release: match it with your change record, and file the list's digest there:
      grep -E '^(version|revision|created|platform|image|config|manifest|key|signed): ' RELEASE.txt
      sha256sum SHA256SUMS | cut -c1-64
   5. Only now: load the image; its tag must name the image the release lists (prints 1):
      docker load -i "image/deltasleuth-$VERSION-$PLATFORM.tar.gz"
      docker image inspect --format '{{.Id}}' "deltasleuth:$VERSION" > ../id.txt
      grep -E '^(config|manifest): ' RELEASE.txt | cut -d' ' -f2 | grep -cxF -f ../id.txt
   What a valid result proves: SECURITY.md, "Verifying a release".
   ```

   Step 0 prints nothing; step 1 a fingerprint your register lists; step 2 `Verified OK`; step 3 an
   `OK` per file and nothing from `diff`; step 4 the release's lines, to match with your change record,
   and the list digest to file there; step 5 `Loaded image: deltasleuth:0.1.0`, then `1`. Anything else:
   stop, and see section 11, Installing a release.
5. **Extract the kit** into the installation's folder, which the first install creates:

   ```sh
   sudo install -d -o "$(id -un)" /srv/deltasleuth
   tar -xzf source/deltasleuth-0.1.0-src.tar.gz -C /srv/deltasleuth --strip-components=1 --no-same-owner
   ```

A first install goes on with the introduction (the site file and `.env`) and section 3. Keep the
release's folder: the procedure's steps 0 and 5, run again in it, re-check the loaded image (step 5
reads the values step 0 sets, in the same shell), section 10 re-scans its SBOM, and it is the rollback
point of the next upgrade.

The image is multi-stage, each base image pinned by digest:

1. Node builds the UI (`web/dist`).
2. uv, pinned by hash, installs the locked Python dependencies (`uv sync --frozen --no-dev`).
3. The runtime stage holds neither uv nor pip. The app runs as uid and gid 10001 (`deltasleuth`), with
   a health check on `/v1/health`.

Compose runs `app` and `mocks` hardened: a read-only root filesystem, where they write only to their
volumes and a 64 MB `/tmp`, no capabilities, and no privilege gain.

### Build from source (unsigned)

The compose file runs `deltasleuth:<version>`, the image a signed release loads, and never builds or
pulls it. To build a checkout instead, add the source override to `.env`, then build:

```sh
printf '%s\n' 'COMPOSE_FILE=deploy/docker-compose.yml:deploy/docker-compose.site.yml:deploy/docker-compose.source.yml' > .env
docker compose build
```

`cp deploy/env.source.example .env` writes the same line, with a commented one for the project name of a
second installation on the machine.

The build is tagged with the Compose project's name, `<project>:source`: `deltasleuth:source` unless
`.env` holds a `COMPOSE_PROJECT_NAME` line, so two checkouts on one machine, each under its own project
name, share no image. After taking new source, `docker compose up -d --build` rebuilds it: one build,
on which `app` then runs, and `mocks` too when `--profile mocks` is given. The build it replaces stays
on the host, untagged, until `docker image prune --force --filter
label=com.docker.compose.project=<project>` removes it. A source build is unsigned, and carries no
SBOM, scan or triage of its own.

The source override also shows the app the checkout's `mocks/` and `examples/`, read-only, at
`/app/mocks` and `/app/examples`: the image's working directory is `/app`, so `ds` in a container reads
the bundled specs by their path in the checkout (`docker compose run --rm app ds run
mocks/customer/spec.yaml`, `ds spec validate examples/tables/spec.yaml`). A loaded release has no such
mount: there a spec is copied into the `p0` volume first (section 9).

## 3. Configure

### Settings

```sh
cd deploy/config
for f in deltasleuth targets llm_profiles; do cp "$f.example.yaml" "$f.yaml"; done
cd -
```

The three files are mounted read-only at `/etc/deltasleuth`:

- **`deltasleuth.yaml`**: where the secrets, volumes and the other two files are, the `auth` block
  (below), the `encryption` block naming the two key secrets, and `retention` (the days each class
  keeps raw data, 90 and 365 by default). The defaults fit this compose file. An optional `sampling`
  block sizes a sign-off draw's session memory (section 10, Operations); an optional `readiness` block
  sets the cut-over readiness gates and programmes (Cut-over readiness, below); an optional `precheck`
  block bounds table pre-checks (`sessions_per_target`, 1 to 4, default 1; `statement_timeout_s`, 60 to
  86,400, default 900; `distinct_max_rows`, default 20,000,000, 0 for never: section 7, Table pre-checks
  and the schema guard); an optional `files` block is the file reader's budget (section 7, Files); an
  optional `mcp` block turns the MCP server on and states what its tool results may carry (The MCP
  server, below).
- **`targets.yaml`**: your endpoints (section 7).
- **`llm_profiles.yaml`**: your model gateway (section 8).

No credential is ever written in these files. They only hold `file:` references.

**As copied, `deltasleuth.yaml` is a trial on one machine,** not an office deployment: people sign in
with an API token at `http://localhost:8000`, no proxy is trusted and AI chat is off. In that file a line
that starts `# ` is a setting switched off, and removing those two characters switches it on (a line
that starts `##` is prose). The copy stays a trial until you set `public_url` and remove the `# ` of the
`trusted_proxies` line and of the ten `oidc` lines (Sign-in, below, and section 5). Left as copied
behind your proxy it admits nobody: no token exists until a host administrator creates one, a browser's
sign-in carries the proxy's origin and is refused (403 `cross-origin request refused`), and `/v1` under
your host name answers 400 `unexpected Host header`.

### Sign-in

The `auth` block decides how people and machines sign in ([AUTH.md](AUTH.md) §1, every setting in
§8):

| Mode | `auth` | For |
|---|---|---|
| oidc | with `oidc` | Production. People sign in through your IdP, with its MFA; machines use named API tokens. Per-user audit, the Admin screen and four-eyes approvals ([APPROVALS.md](APPROVALS.md)) need it. This build asks for no licence. In a vendor build (The licence, below) it is production under an Enterprise licence: without one people still sign in, read and administer, but new runs, compares and soak cycles are refused. |
| tokens | without `oidc` | CI and trials (in a vendor build, also production under a Community or Pro licence). Every principal, in the browser too, is a named API token and is audited as that token. No token can hold `admin`, so administration is CLI-only (section 9). Nothing can be approved, so runs are labelled unapproved. |
| developer | absent | One developer's workstation (`uv run ds serve` on loopback). Never here: the container listens on all its interfaces, so `ds serve` refuses to start without `auth`. |

In the example:

- **`public_url`** is `http://localhost:8000` as copied. Set it to the URL people open, through your
  TLS proxy (section 5): `https://` and your host name. Redirects go to it, never to the request's
  `Host`.
- **`trusted_proxies`**, one commented line, names the compose network's gateway, whose
  `X-Forwarded-For` the app resolves (section 5). Remove its `# `.
- **`oidc`**, ten commented lines, comes from your IdP registration (section 5): the issuer, the client
  id and credential, the claims to read, and `roles`, which maps each role to the IdP's immutable group
  ids. Remove the `# ` of each of the ten, and fill them in. Without them the mode is tokens.

The session settings keep their defaults unless you set them: `session_idle_minutes: 30`,
`session_max_hours: 8` and `token_max_days: 90`.

### Secrets

Each secret is a file in `deploy/secrets/` holding one value. Compose mounts it read-only at
`/run/secrets/<name>`. The directory is git-ignored.

The base compose file mounts four, and each goes straight from the command that makes it into its file,
through no shell variable:

```sh
mkdir -p deploy/secrets && chmod 700 deploy/secrets
(
  umask 077
  openssl rand -hex 24 > deploy/secrets/db_password
  {
    printf 'postgresql://deltasleuth:'
    tr -d '\n' < deploy/secrets/db_password
    printf '@db:5432/deltasleuth'
  } > deploy/secrets/db_dsn
  openssl rand -hex 32 > deploy/secrets/kek
  openssl rand -hex 32 > deploy/secrets/tokeniser
)
chmod 444 deploy/secrets/db_password deploy/secrets/db_dsn deploy/secrets/kek deploy/secrets/tokeniser
```

`kek` and `tokeniser` must stay outside `evidence_dir` and `workspace_dir`, whose backups would carry
them; the app refuses to start otherwise. With `auth`, the `encryption` block is required.

**The one exception: a target declared in the product** (section 7). Its credential is typed into the
product, so it cannot be a mounted secret: the app writes it to the `workspace` volume, one line in
clear in a file of its own (`targets/secrets/`, mode 0600 in 0700 directories, readable by the
`deltasleuth` user alone). Backups of that volume carry it: protect them as you protect
`deploy/secrets/`, or declare a service that needs a credential in the targets file instead.

A `db_dsn` written by hand, for a database of your own or a password someone else issued, is one line,
`postgresql://<user>:<password>@<host>:<port>/<database>`, with the special characters of the user and
the password percent-encoded: `%40` for `@`, `%2F` for `/`, `%25` for `%`, `%20` for a space. The
password the lines above make is hexadecimal and needs none. An address the driver cannot read, or one
with an unencoded `@` or `/` in its password, stops the app and every command at once (section 11).

The `chmod 700` on the directory keeps the files private on the host. The `chmod 444` on the files
lets the container users read them (postgres, and `deltasleuth`). To change a secret later, run
`chmod u+w` on it, rewrite it, run `chmod 444` again, then `docker compose restart app`.

The site example mounts no other secret as copied. `oidc_client_secret` (section 5) and `llm_key`
(section 8) are each written where its section says, named in the site file's `app.secrets`, and
switched on by removing the `# ` of its line under `secrets:` (and of the `secrets:` line itself).

| Secret | Used by |
|---|---|
| `db_password` | PostgreSQL, when the `pgdata` volume is first created |
| `db_dsn` | The app's database connection (the same password) |
| `kek` | The keyring (`encryption.kek_ref`): one key-encryption key (KEK) per line, 64 hex, the last one current. Its KEKs wrap the data keys that seal raw values and encrypt bodies (section 4) |
| `tokeniser` | The tokeniser key (`encryption.tokeniser_key_ref`): every masked value is a token derived from it (section 4) |
| `oidc_client_secret` | DeltaSleuth's client secret at the IdP (`auth.oidc.client_secret_ref`), read at start and at each sign-in, so it rotates without a restart (section 5) |
| `llm_key` | The `gateway` profile (`auth_ref: file:/run/secrets/llm_key`) (section 8) |

**A key instead of a secret** (`client_auth: private_key_jwt`). Create a key and its certificate, and
register the certificate at the IdP:

```sh
openssl req -x509 -newkey rsa:3072 -nodes -days 730 -subj /CN=deltasleuth \
    -keyout deploy/secrets/oidc_client_key -out deploy/config/oidc_client.crt
chmod 444 deploy/secrets/oidc_client_key
```

In the site file, name the key in `app.secrets` (`secrets: [oidc_client_key]`), remove the `# ` of the
`secrets:` line and of its `oidc_client_secret` line, and make that line
`oidc_client_key: {file: ./secrets/oidc_client_key}`. In
`auth.oidc`, replace `client_secret_ref` with `client_auth: private_key_jwt`,
`client_key_ref: file:/run/secrets/oidc_client_key` and `client_cert_file: oidc_client.crt`. The
certificate adds its thumbprints to each assertion, which Entra ID needs to find the key. The key is
RSA of at least 2048 bits or EC P-256.

To add a credential, such as a target's token, a database's DSN, or a MySQL or Oracle target's password
and the CA certificate of its server (section 7):

1. Write the file to `deploy/secrets/<name>`, then `chmod 444` it.
2. Add `<name>: {file: ./secrets/<name>}` under the top-level `secrets:` in the site file (a commented
   line in the example: remove its `# ` for the first one).
3. Add `<name>` to `app.secrets` in the same file.
4. Reference it as `file:/run/secrets/<name>`.
5. Run `docker compose up -d`, which mounts the new secret.

### The evidence pack signing key

Evidence packs ([PACK.md](PACK.md)) are signed ZIPs an auditor checks offline against the site's key
register. They need oidc mode; without the `packs` block their routes answer 503 and nothing else
changes. Keep **one key per installation, in production only**: never copy it to a staging or test
installation, whose packs would then pass for production's. Its custody is the keyring's (section 4):
the host administrators and the office vault.

1. **Generate it** on the host:

   ```sh
   (umask 077; openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out deploy/secrets/pack_signing_key)
   chmod 444 deploy/secrets/pack_signing_key
   ```

   It must be an unencrypted PEM EC P-256 private key (this command writes PKCS#8), outside
   `evidence_dir` and `workspace_dir`. `ds serve` refuses anything else with one `error:` line that
   never echoes the file. Vault it now.
2. **Mount it** as a secret (the five steps above, name `pack_signing_key`), and add to
   `deltasleuth.yaml`:

   ```yaml
   packs:
     installation: prod-eu-1        # 1 to 63 of a-z, 0-9, '.' and '-': the name the register lists
     signing_key_ref: file:/run/secrets/pack_signing_key
   ```

   After `docker compose up -d`, the `system.start` audit row carries the key's fingerprint
   (`pack_key`).
3. **File it in the site's key register**, dated, before the first pack is issued:

   ```sh
   docker compose run --rm app ds packs key --out /var/lib/deltasleuth/p0/pack-key
   docker compose cp app:/var/lib/deltasleuth/p0/pack-key ~/pack-key
   ```

   `fingerprint.txt` holds `installation: prod-eu-1` and `fingerprint: <hex>` (the SHA-256 of the
   public key, as a pack's verification step 1 prints it); `procedure.txt` is the verification
   procedure every pack's `VERIFY.txt` must match (step 0). File both with the date the key entered
   service. Auditors verify against the register's copies, never a pack's own.

**Rotating the key** (the site's cryptoperiod, or a suspected leak): generate a new key as in step 1
under a new file name, point `signing_key_ref` at it, mount it and run `docker compose up -d`, then
file the new entry from `ds packs key` and end-date the old one, keeping it: packs issued before
still name the old fingerprint. From then on `ds packs revocations` signs the revocation list with the
new key, still listing the revoked packs of every key used; `ds packs verify --revocations` checks
only a list signed by the pack's own key, so check step 8 of an older pack by hand (its `VERIFY.txt`).
Destroy the old key in the file and every vault copy once the register has the new entry.

**Decommissioning** the installation: write the final revocation list
(`ds packs revocations --out /var/lib/deltasleuth/p0/revocations`), file its `revoked.txt`,
`revoked.txt.sig` and `signing-key.pem` in the register as the final list, which step 8 then uses,
and destroy the key.

### The licence

**This build needs no licence.** It is an open build ([LICENCE.md](LICENCE.md) §19): its image embeds no
vendor key, so it enforces no licence and limits nothing. Any number of pairs (spec names) run, in every
sign-in mode, and four-eyes sign-off and evidence packs ask for no licence; each keeps its own rules
(SSO, the `packs` block and its signing key). There is nothing to install or renew, and no step
below is yours to take:

- `ds licence` (section 9) prints `licence none: this build needs no licence`, `state open` and
  `allows: runs yes, sso yes, signoff yes, packs yes`, and the Licence screen, open to every role, says
  `This build needs no licence and limits nothing.` under the installation id;
- no screen shows a licence banner, no command prints a licence warning, and no new run, report or
  pack carries a licence label;
- `ds licence install` and `ds licence show` refuse every file, with exit 2 and `error: this build
  embeds no vendor key: it needs no licence and can verify none (docs/mvp/LICENCE.md §19)`;
- `ds licence id` still prints the installation id, which names this database in the audit log.

**The rest of this section describes a vendor build,** a release whose image embeds the vendor's keys.
From this build on, only such a build has licences, tiers, pairs that are counted and an "unlicensed
evaluation", and wherever this guide names one of those it means a vendor build. The builds before this
one, from migration 010, embedded no key and enforced the licence all the same: each ran its database
as an unlicensed evaluation, and "Upgrading to licence keys" (section 10) says what such a database
keeps.

A licence ([LICENCE.md](LICENCE.md)) is a text file the vendor signs for one installation, this
deployment's database. It names the customer, the order, the tier, how many pairs run and a period of
at most a year. It is not a secret and needs no setting: DeltaSleuth stores it in the database and
checks its signature against the vendor keys the image embeds at every decision, offline, so an install
applies at once in the app, the worker and every command, with no restart.

| Licence | Pairs (spec names) | Runs under SSO (oidc) | Four-eyes sign-off, evidence packs |
|---|---|---|---|
| none: an unlicensed evaluation | the first 2 ever | no | no |
| `community`, `pro` | the licence's (by default 2 and 5) | no: run in tokens mode | no |
| `enterprise` | the licence's (by default unlimited) | yes | yes |

A **pair** is a spec name: all its versions, re-runs, soak cycles and one-shot compares count once. The
first names with a run in the licence's period hold its seats; a run of another name is refused. An
evaluation's runs and report files are labelled "unlicensed evaluation", and never get a pack.

1. **The installation id.** The first command that opens the store makes it (migration 010, section 4).
   Print it and send it to the vendor:

   ```sh
   docker compose run --rm app ds licence id          # I- and 16 hex, alone on stdout
   ```

   A dump and restore keeps it (section 10), a backup from before migration 010 too: 010 applied to it
   again makes the same id from the store's first audit row. A new, empty database is a new
   installation with a random id: a fresh unlicensed evaluation, which the old id's licence does not
   cover (a database rebuilt without a backup needs a reissued licence).
2. **Keep the files** the vendor sends in `deploy/licence/` (`LI-….txt`). Installing
   one again changes nothing (`licence LI-… is installed already`), so a restore from before an install
   takes it again.
3. **Check and install** it (the first command needs no settings or database):

   ```sh
   docker compose run --rm --no-deps -T app ds licence show - < deploy/licence/LI-....txt   # its fields; "verifies with key …"
   docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds licence install - < deploy/licence/LI-....txt
   ```

   `install` prints `installed LI-… (enterprise, Example Bank plc, 2026-10-01 to 2027-09-30, unlimited
   pairs): in force now` (or `in force from <day>`), what changed now, and the runs paused for the
   licence that it lets resume: resume each on its Run screen. It is audited `licence.install`, with the
   customer and order. A refused file writes nothing and names why (section 11).
4. **Check the standing:** `ds licence` (section 9) prints the licence, its state, what it allows and each
   counted pair with its seat; the Licence screen, open to every role, shows the same, and every screen
   shows the licence's warnings as banners.

**Renewals** arrive 60 days before the end. Install one when it arrives: it waits for its first day and
silences the ending warnings. From 30 days before the end every screen warns; for 30 days after it
everything still works, with warnings (grace); then new runs, compares, soak cycles and evidence packs
are refused until a renewal is installed, and soaks pause at their next cycle (one due after the grace,
at the boundary before it). Reads, reports, exports, holds, purges, the audit log, pack downloads and
sign-offs under an Enterprise licence always work. A genuine but wrong file is withdrawn before its first
day with `ds licence withdraw LI-… --reason TEXT`; a started licence is only replaced by a new one.

### Notifications

Optional ([ALERTS.md](ALERTS.md)). With a `notify` block, owners hear on a signed HTTPS webhook, SMTP
e-mail, Slack or Teams when a cycle observes new gaps or crosses a threshold, a run stops, pauses or
fails, a draw is refused or a sample judged, a labelling is labelled, an approval waits or is revoked, or
the licence's state changes (in this build once, to `open`: section 3, The licence). A notification carries ids, counts, closed codes, a spec name (a label: off for Slack and Teams)
and a link to the screen, never a key or a value. A failing channel never touches a run. Channels and
rules live only in `deltasleuth.yaml` and apply at the next `docker compose restart app`: no screen or
route creates a destination. Without the block no notifier runs.

1. **Secrets.** A webhook's signing key is `whsec_` and base64 of 24 to 64 random bytes. A Slack or Teams
   webhook URL carries its own secret, so it lives in a file too, bound to the host you declare; type or
   paste it (then Ctrl-D), never on a command line, which the shell's history keeps:

   ```sh
   (umask 077; printf 'whsec_%s\n' "$(openssl rand -base64 32)" > deploy/secrets/notify_hook_key)
   (umask 077; cat > deploy/secrets/notify_teams_url)
   chmod 444 deploy/secrets/notify_hook_key deploy/secrets/notify_teams_url
   ```

   Give the receiver's owner the key through the vault. Mount each file as a secret (section 3, Secrets,
   the five steps). All are read at every send, so a rotation needs no restart; a webhook takes two keys
   and signs with both while the receiver moves over.

   | Secret | Used by |
   |---|---|
   | `notify_hook_key` | a webhook channel's `signing_key_refs` |
   | `notify_teams_url` | a Teams (or Slack) channel's `url_ref`: https to its `host` exactly, else the delivery dies `url_mismatch`; never logged, stored, audited or shown |
   | `notify_smtp_password` | an e-mail channel's `password_ref`, with `username` (no XOAUTH2: use an internal relay) |

2. **Settings** ([ALERTS.md](ALERTS.md) §8 lists every one):

   ```yaml
   notify:
     label: prod                                  # names this installation in every notification
     proxy: http://proxy.bank.example:8080        # only for channels with proxy: true
     channels:
       ops-hook:     {kind: webhook, url: https://events.bank.example/deltasleuth,
                      signing_key_refs: [file:/run/secrets/notify_hook_key], allow_private_network: true}
       owners-teams: {kind: teams, url_ref: file:/run/secrets/notify_teams_url,
                      host: prod-00.eastus.logic.azure.com, proxy: true}
       owners-mail:  {kind: email, smtp_host: smtp.bank.example, username: ds-alerts,
                      password_ref: file:/run/secrets/notify_smtp_password, sender: deltasleuth@bank.example,
                      recipients: [parity-owners@bank.example], allow_private_network: true}
     rules:                                       # pause and fail to operators, gaps to owners
       - {name: new-gaps, events: [cycle.ended], channels: [owners-mail, owners-teams], when: {new_root_causes_at_least: 1}}
       - {name: run-health, events: [run.paused, run.stopped, run.failed, notify.dead], channels: [ops-hook]}
       - {name: heartbeat, events: [notify.daily], channels: [ops-hook]}
   ```

   A setting the rules do not allow stops the app at start with one `error: invalid settings` line. A
   secret it cannot read does not: `ds serve` prints `warning: notify channel <name>: …; its deliveries
   retry until it is readable` and starts.
3. **The firewall.** The app's new outbound connections:

   | To | Port | For |
   |---|---|---|
   | each webhook, Slack or Teams channel's host | its URL's (443) | channels without `proxy: true` (the default), the app connecting directly |
   | the SMTP relay | `smtp_port` (587; 465 with `tls: implicit`; 25 with `tls: none`, a private relay only) | `kind: email` |
   | `notify.proxy` | its port | channels with `proxy: true`: the proxy reaches the channel's host, which the app checks only as declared |

   Every address a send reaches is checked first: link-local (the cloud metadata address among them),
   unspecified, multicast and `fd00:ec2::254` never; any other private address only with the channel's
   `allow_private_network` (the proxy may be internal). Redirects are never followed, and
   `HTTP(S)_PROXY` is ignored.
4. **The receiver verifies each webhook** (Standard Webhooks): `webhook-signature` holds `v1,` and the
   base64 HMAC-SHA256, under the key's decoded bytes, of `webhook-id`, `.`, `webhook-timestamp`, `.` and
   the raw body. Delivery is at least once: deduplicate on `webhook-id`.

   ```python
   import base64, hashlib, hmac, time

   def verified(key: str, headers, body: bytes, tolerance: int = 300) -> bool:
       secret = base64.b64decode(key.removeprefix("whsec_"))
       sent, stamp = headers["webhook-id"], headers["webhook-timestamp"]
       if tolerance < abs(time.time() - int(stamp)):
           return False
       mac = hmac.new(secret, f"{sent}.{stamp}.".encode() + body, hashlib.sha256).digest()
       expected = base64.b64encode(mac).decode()
       return any(hmac.compare_digest(s.removeprefix("v1,"), expected)
                  for s in headers["webhook-signature"].split() if s.startswith("v1,"))
   ```

5. **NTP.** Receivers reject a `webhook-timestamp` outside their tolerance (five minutes above): keep
   the host's clock on NTP.
6. **Check** every channel before the first run, without sending anything and without the database:
   `docker compose run --rm --no-deps app ds notify check` resolves each destination, reads each secret
   and prints one `ok:` or `error:` line per channel (section 11, Notifications).

### Cut-over readiness

Optional ([READINESS.md](READINESS.md)). The Readiness screen, `ds readiness` and the readiness reports
work without any setting: every spec name with a batch or soak run is a pair, judged by the contract's
gates (the 0.1% tier, 3 clean evidence cycles in a row, an evidence cycle at most 7 days old). A
`readiness` block groups pairs into programmes and entity types and sets the gates; it holds no secret
and no destination:

```yaml
readiness:
  gates:                              # the installation's; unset values keep the contract's defaults
    tier: standard                    # standard (0.1%) or high (0.01%); never looser
    convergence: {cycles: 3}          # 1 to 50 clean evidence cycles in a row; fewer than 3 relaxes
    fresh: {max_age: 7d}              # 1h to 90d; longer than 7d relaxes
  programmes:
    - name: cutover-q4
      pairs:
        - {spec: customer-profile, entity: customer}
        - {spec: customer-links, entity: customer, gates: {tier: high}}   # a programme or pair only tightens the tier
        - {spec: legacy-accounts, entity: account, closed: 2026-11-02}    # judged as of that day, then silent
```

1. **It applies at restart** of the app (`docker compose restart app`): the worker then records the
   settings as judged (`readiness.config`), and every surface names them by one hash. A relaxed or
   tightened gate is named as a deviation on every surface. `ds readiness check` prints each pair's
   effective gates, where each comes from and the hash, and warns of a listed spec that is not stored;
   a refused block stops every entry point with `invalid settings …: readiness…` (exit 2).
2. **The key census.** With the block present (even `readiness: {}`), each cycle of a key-list run with
   a table side first counts that side in one snapshot, as with evidence packs ([PACK.md](PACK.md)
   §1.3): one more read of each table side per cycle, an hour a statement at most, before the cycle
   compares. Without it (and without `packs`) such a pair's population gate reads `not met`: its counts
   describe those keys only; a pair whose keys were given at creation (pasted, or `ds run --keys`) has no
   evidence cycle until a census counts side a, and reads `no evidence`, each gate saying `… is a key list
   given at creation whose census does not count side a (side a is counted only with packs or readiness
   configured, PACK §1.3)`. Table scans and hash samples are not affected.
3. **It gates nothing.** Readiness never refuses a run, a sign-off or a pack, and no licence fences
   it; a four-eyes sign-off decides, whatever a pair's state reads. The settings are the host
   administrator's file: recorded in the audit log, not four-eyes approved.
4. **Events.** A `notify` rule for `readiness.changed` must name its `causes` (section 3,
   Notifications; [ALERTS.md](ALERTS.md) §3) and fires when any of them is among a row's; without
   notifications, alert on the SIEM's `readiness.changed` lines (section 10).

`deploy/config/deltasleuth.example.yaml` holds this block, and the `packs`, `notify`, `precheck`,
`files` and `mcp` ones, commented at its end: remove the leading `# ` of the lines you use.

### Schedules

Nothing to configure ([SCHEDULES.md](SCHEDULES.md)): no setting, no secret and no outbound call. A
schedule starts runs of a stored spec at cron times in a time zone; operators declare one on the
Schedules screen, through the API or with `ds schedules add`, and the worker of the app (`ds serve`,
or a `ds worker` beside `ds serve --no-worker`) fires it. Three things on the host matter:

1. **The clock.** Due times are judged by the database's `now()`. Keep the database host on NTP: a
   clock set forward misses the times in between, and one set back stalls every schedule until it
   passes the last settled time (the Schedules screen then says the database clock is behind).
2. **Time zone rules ship in the image.** A schedule's zone is `UTC` or an IANA region name the
   image's tz data holds, and the rules (daylight saving dates, a renamed zone) change only with a
   release. Run `ds schedules` in the release image, as the API and the worker do: `docker compose run
   --rm app ds schedules next '0 2 * * *' --tz Europe/London` (the alias of section 9). A host's own
   `ds`, from a source checkout, may hold other zones and other rules. After an upgrade whose tz data
   moved a schedule's next time, the worker records it (`schedule.rezone`); a zone the new data dropped
   pauses its schedules, each with its `schedule.pause` row and, if a time of it was due, a
   `schedule.missed` row (`zone`). One paused before its time is due has its `schedule.pause` row alone,
   which a `notify` rule for `schedule.missed` sends as an alert (`schedule SC-… is paused by the
   worker before a time of it came due: …`); the Schedules screen's banner names it too.
3. **The auth mode.** A schedule fires only from a worker in the auth mode it was last saved in. After
   a move from tokens mode to SSO, every due time is recorded as missed (`mode`) until an operator
   saves each schedule again, signed in; a `--config` without `auth.oidc` never starts an ungated run
   of a schedule saved under SSO.

A schedule is not a dead-man's switch: while no worker runs, nothing is recorded or sent. Section 10
(Operations, Schedules) gives the outside check, and the [runbook](RUNBOOK.md#schedules) the daily
use.

### The MCP server

Optional ([MCP.md](MCP.md)). With an `mcp` block, a team's own agent (Claude Code, VS Code, Cursor and
the like) reads a run's findings by id over the Model Context Protocol: eleven tools, ten reads and
`compare_key`, with the chat's masking, caps and ids, as a named API token under the role it holds.
Without the block there is no MCP at all: `POST /v1/mcp` answers a caller it knows 404 and `ds mcp`
exits 2. The block holds no secret and no destination:

```yaml
mcp:
  data_classes: [aggregates, production_tokenised]   # required, no default: what tool results may carry
  http: true                # serve POST /v1/mcp in ds serve; false (the default): ds mcp only
  work: []                  # work tools served: [compare_key] lets an operator's agent compare one key again
  calls_per_minute: 60      # tools/call requests per caller in a minute, 1 to 600
  concurrency: 4            # calls at once per process, 1 to 16
```

1. **What results may carry** is this installation's written decision, `data_classes`. It is required
   and has no default:
   - `aggregates`: names of specs, schedules, programmes and pairs, ids, states, times and counts. It
     serves the list of runs, readiness, the schedules and the table pre-checks (`list_runs`,
     `get_readiness`, `get_readiness_pair`, `list_schedules`, `get_schedule`, `list_prechecks`).
   - `production_tokenised`: a run's masked per-key data as the chat's tools return it (field names and
     path patterns, tokens, classes), for a run whose spec declares nothing clear. It serves
     `get_run_summary`, `get_root_causes`, `get_findings`, `get_observations` and `compare_key`.
   - `production_clear_declared`: the same tools for the other runs too, those whose spec lists `clear`
     paths, sets `unlisted: clear` or has a business-code key. Their results hold those clear values
     and the raw business-code keys. It needs `production_tokenised` beside it. Without it such a run
     answers `run R-… is not served by this installation: its spec declares clear values or a
     business-code key (mcp.data_classes lacks production_clear_declared)`; with it `ds mcp check`,
     and `ds serve` where `http` is true, print `warning: mcp: values a spec declares clear and
     business-code keys leave the installation in tool results`.

   The LLM egress policy of section 8 governs the model host of the built-in chat. An agent's model
   host is the agent's own, which that policy does not reach: this list is what holds for what MCP's
   tools answer. **It holds for calls made through `/v1/mcp` only.** An agent's bearer is a general API
   token, which every other `/v1` route takes with its role, whatever this list says: a `viewer` token
   reads a run's clear-declared values on the Key screen's route (`GET /v1/runs/{id}/keys/{key}`)
   without `production_clear_declared` here.
2. **Work.** `work: [compare_key]` serves the one tool that calls your targets: a one-shot compare of
   one key of a stored run, for a caller holding `operator`, at most 10 a minute per caller. It needs
   `production_tokenised`. No tool starts, pauses, resumes or cancels a run. That is what MCP's tools
   do, not what an agent's token can: an `operator` token compares, starts, pauses, resumes and cancels
   runs on `/v1` whatever `work` lists, so give one only to an agent you would let do so.
3. **It applies at restart** of the app (`docker compose restart app`); each `ds mcp` reads the block
   at its start. A token's role, expiry and revocation apply at its next request. With `http: true`
   `ds serve` prints `mcp: POST /v1/mcp is served; data classes …; work tools …` on stderr, and every
   start records the block in its `system.start` audit row (`mcp`, or null without one). A refused
   block stops every entry point with `invalid settings …: mcp…` (exit 2). Check it without the
   database:

   ```sh
   docker compose run --rm --no-deps app ds mcp check
   ```

   It prints the block as judged, the tools a viewer and an operator are offered, each tool not served
   with the reason, whether runs whose spec declares clear values are served, and what MCP is not
   (below).
4. **The firewall: no new outbound destination.** MCP adds none to the app's. An agent's client
   connects in, to `public_url` through your TLS proxy, as a browser does; the server sends a client no
   request, and no tool takes a URL, a host or a path. Where `work` lists it, `compare_key` reads the
   run's two declared targets as any compare does, under their own budgets (section 7). So the only
   rule MCP
   may need is the inbound one that lets agents' workstations reach `public_url`. The connection from
   an agent to its model host is made by the agent's client on its own machine: DeltaSleuth neither
   makes nor sees it.
5. **Agents use HTTP with a token of their own.** Create one token per agent, named for it, `viewer`
   unless it must compare (Admin, Tokens; or `ds token create`, section 9), and wire the client as the
   [runbook](RUNBOOK.md#your-own-agent-mcp) shows: the token is a line in a file of mode 0600 or in the
   editor's secret store, never in a client's configuration and never in an environment variable.
   The token's role is what bounds the agent: its file is readable by an agent that has shell tools,
   which with the token calls any `/v1` route its role reaches, so prefer the client's secret store
   where it has one, and count what the role allows on `/v1` as what the agent can do.
   `ds mcp`, the same tools over a process's stdin and stdout, is for a developer's workstation in
   developer mode. On this host it is the host-administrator channel (section 9): never give an agent
   that has shell tools the host's `docker compose`.

**What MCP is not** (`ds mcp check` prints the same nine statements):

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

## 4. Initialise

```sh
docker compose run --rm app ds init      # creates the workspace; the keys are the secrets of section 3
docker compose run --rm app ds migrate   # creates the schema (ds serve also migrates at start)
docker compose run --rm app ds licence id   # the installation id migration 010 made, which names this database
```

No licence is installed, now or later: this build needs none (section 3, The licence). `ds licence`
prints `state open`, no screen or command warns of a licence, and runs start in every sign-in mode. (A
vendor build is an unlicensed evaluation until its licence is installed: every screen, and `ds serve`,
`ds worker`, `ds run` and `ds compare`, say so, and in oidc mode new runs are refused.)

The two key secrets:

- **The tokeniser key.** Every masked value is a token derived from it: keys, finding values, report
  cells and chat citations. Anyone who holds the key can test guesses of short values, such as phone
  or customer numbers, against any token. A new key gives every value a new token, and then no run
  can be compared with an older one: never replace it.
- **The keyring.** Every raw value stored is encrypted: raw keys, response headers, scan resume
  points and chat in the database, response bodies in the evidence volume. Each is encrypted under a
  data key held in `ds.data_keys`, wrapped by a KEK of the keyring. A database dump or the evidence
  volume is unreadable without the keyring, and a KEK that is lost loses everything it wraps. The
  first command that opens the store creates its evidence keys (audited `keys.created`), and each run
  gets a data key of its own.

**Vault both files now**, and the keyring again after each rotation (section 10): copy
`deploy/secrets/kek` and `deploy/secrets/tokeniser` into the office password vault, never beside the
database or evidence backups. Check that the vault holds the keyring the app reads: both commands
print the KEK ids, oldest first, the last marked `(current)`, and must agree. The second reads the
vault copy, written to `~/kek.from-vault` (mode 0600) and run as your own user so that it can read it.

```sh
docker compose run --rm --no-deps app ds keys fingerprint /run/secrets/kek
docker compose run --rm --no-deps -u "$(id -u)" -v ~/kek.from-vault:/tmp/kek:ro app ds keys fingerprint /tmp/kek
rm ~/kek.from-vault
```

Whoever holds the keyring and a database dump can read the raw keys, headers and chat; with the
evidence too, every body ([RETENTION.md](RETENTION.md) §11). A backup of the whole host, such as a VM
image or a snapshot of its root filesystem, includes `deploy/secrets`: exclude it, or treat that
backup as plaintext.

## 5. Serve

### Register DeltaSleuth at your IdP

Register a confidential web application that uses the authorization code flow, with:

- the redirect URI `{public_url}/v1/auth/callback` and the post-logout redirect URI `{public_url}/`;
- the scopes `openid` and `profile` (the default `scopes`);
- a client secret (section 3), or the certificate of a key (`private_key_jwt`, section 3).

Then fill in `auth.oidc` from this table. `roles` maps each role to IdP values that must be
**immutable ids that only IdP administrators assign**. They are compared exactly, so case and spaces
count. Never map display names: where names are not unique, anyone who can create a group called
`DS.Admin` would become admin. `username_claim` is audited as the person's name, so it must name an
attribute that only IdP administrators can change; without it the name is the subject. `email` is
never used. Map at least one group to `rule_approver` (or a role above it): without one, nothing can
be approved, and `ds serve` and `ds auth check` warn `no IdP group maps to rule_approver or above:
nothing can be approved`.

| IdP | `issuer` | `subject_claim` | `username_claim` | `group_claims` and the `roles` values | Also |
|---|---|---|---|---|---|
| Entra ID | `https://login.microsoftonline.com/<tenant-id>/v2.0` | `oid` | `preferred_username`, or `upn` added as an optional claim | `[roles]`: the app role values you define on the registration (`DS.Admin`), assigned to users or groups | Register both URIs as Web redirect URIs. Add the `auth_time` optional claim to the ID token: creating an API token, and approving, need a sign-in at most 15 minutes old. With `private_key_jwt`, set `client_cert_file`. Group object ids in the `groups` claim also work, but a user in more than 200 groups then gets `groups_overage`. |
| ADFS | `https://<adfs-host>/adfs` | `sub` | `upn` | Group SIDs, issued by a claim rule; `group_claims` names the claim as the token carries it (a short name such as `groupsid`, or the full claim type URI) | Add `allatclaims` to `scopes`, so the ID token carries the issued claims. |
| Okta | `https://<org>.okta.com/oauth2/<authorization-server-id>` | `sub` | `preferred_username` (the Okta username), if users cannot change it | A custom claim of the authorization server, such as `ds_groups`, whose values are the user's group ids (`00g…`), not group names | `end_session: false`: Okta's sign-out needs an `id_token_hint`, which DeltaSleuth does not keep. |
| Ping | PingFederate `https://<host>`; PingOne `https://auth.pingone.com/<environment-id>/as` | `sub` | An attribute that only administrators change | A custom claim whose values are group ids (PingOne group ids, or the directory group's objectGUID or SID), not names | |
| Keycloak | `https://<host>/realms/<realm>` | `sub` | `preferred_username`, with the realm's "Edit username" off (the default) | Client roles: `[[resource_access, <client-id>, roles]]`, and the role names as values | Turn on "Add to ID token" in the `client roles` mapper of the `roles` client scope. The list form suits a client id that holds dots. |

A string path is read as one claim name first, then split on dots; a list is followed segment by
segment.

Write the client secret the IdP issued to its file. In the site file, name it in `app.secrets`
(`secrets: [oidc_client_secret]`) and remove the `# ` of the `secrets:` line and of its
`oidc_client_secret` line:

```sh
printf '%s' 'THE-CLIENT-SECRET' > deploy/secrets/oidc_client_secret
chmod 444 deploy/secrets/oidc_client_secret
```

Check the settings against the IdP before you start the app. The check needs no database and
changes nothing:

```sh
docker compose run --rm --no-deps app ds auth check
```

It checks the IdP's discovery document, reads the client credential, and lists the IdP's signing keys
with the `id_token_algs` each one fits. It prints the offset between the IdP's clock and the host's,
with a warning above `clock_skew_seconds`, and the two URIs to register. A failing check exits 2 and
names itself (section 11).

### Start

```sh
docker compose up -d
docker compose ps                                  # app turns "healthy"
curl -fsS http://127.0.0.1:8000/v1/health          # status, AI on or off, and the auth mode
```

The port is published on the host's loopback only. People open `public_url` through your TLS
reverse proxy; anyone in a group mapped to `admin` is an admin from their first sign-in. There is no
plain-HTTP option beyond this machine: under https the cookies are `Secure` and `__Host-`; a loopback
`http://` public_url, a one-machine trial's, gets plain cookies ([AUTH.md](AUTH.md) §3).

An app that never turns `(healthy)` has its reason as the last `error:` line of
`docker compose logs --tail 20 app`.

### The TLS reverse proxy

The proxy terminates TLS and passes the whole host to the app: the UI at `/`, the API at `/v1` and
sign-in at `/v1/auth/`. DeltaSleuth cannot run under a path prefix. With nginx on the host:

```nginx
server {
    listen 443 ssl;
    server_name deltasleuth.bank.example;               # the host of auth.public_url
    ssl_certificate     /etc/nginx/tls/deltasleuth.crt;
    ssl_certificate_key /etc/nginx/tls/deltasleuth.key;
    client_max_body_size 10m;                           # the app's own limit

    # $uri, not $request: the sign-in callback's code and state stay out of the access log
    access_log /var/log/nginx/deltasleuth.access.log deltasleuth;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;                    # $http_host when public_url has a port
        proxy_set_header X-Forwarded-For $remote_addr;
        # proxy_read_timeout 330s;                      # with file sides: over files.plan_s (section 7, Files)
    }
}
# in the http block:
# log_format deltasleuth '$remote_addr [$time_local] "$request_method $uri" $status $body_bytes_sent';
```

- **`Host`.** The app answers `/v1` only for the `public_url` host, an `auth.allowed_hosts` entry or a
  loopback name, and gives 400 `unexpected Host header` to anything else, which blocks DNS
  rebinding. A value without a port takes `public_url`'s default port, so `$host`, which drops the
  port, matches a `public_url` without one. When `public_url` has a port, pass `$http_host`. API
  clients that use another name for the host need it in `allowed_hosts`.
- **`X-Forwarded-For`** carries the client's address, which the audit records. The app reads it only
  from a peer in `trusted_proxies`. nginx on the host reaches the app through the compose network's
  gateway, so `trusted_proxies` names that gateway: `172.30.0.1/32`, pinned with the subnet
  `172.30.0.0/24`. If your network uses that range, change both: the subnet in the site file (its
  commented `networks` line), then `trusted_proxies`. A wrong value fails safe, but every user then
  shares one address in the audit. With the subnet moved, a compose command that leaves out the site
  file (`-f`, or an exported `COMPOSE_FILE`) recreates the network on the base file's subnet and cuts the
  app off from `db`: run each one from the installation's folder through `.env`, and repair a slip with
  `docker compose up -d --force-recreate` (the introduction).
- **How far the audited address holds.** Every process on the host that can reach
  `127.0.0.1:8000` also arrives through the gateway, so it could set `X-Forwarded-For` too. In this
  layout the audited address is advisory: it is exact only when nginx is the one client that can
  reach the app's port. For that, run the TLS proxy as a compose service with a fixed address, trust
  that address alone, and stop publishing port 8000 on the host.
- **No sign-in in the proxy.** Headers such as `X-Forwarded-User` or `SM_USER` are ignored, and a
  request that carries only them gets 401.

### Access rules

- Every `/v1` request needs a credential, except `/v1/health` and sign-in, and each route a role
  ([RUNBOOK](RUNBOOK.md#roles) lists them). A request without a credential or with an ended session
  gets 401; one without the role gets 403.
- A browser session is an HttpOnly cookie. It ends after `session_idle_minutes` without use (a Run
  screen's polls do not count), after `session_max_hours`, at sign-out, or when it is revoked.
  Sign-out also sends the browser to the IdP's sign-out, unless `end_session` is false or the IdP has
  none.
- Roles are read from the IdP at each sign-in. A group removal applies at the next sign-in, at most
  `session_max_hours` later. To end someone's access now, disable them (Admin, Users; or
  `ds users disable`, section 9). Editing `auth.oidc.roles` or `group_claims` ends every SSO session
  at its next request.
- Creating an API token needs a sign-in within the last 15 minutes; the Admin screen asks for one. So
  does approving a spec version or a sign-off, on the Approvals screen.
- With SSO, a run of a spec version whose content has `clear` paths, `unlisted: clear`, known diffs or
  population filters waits for a four-eyes approval ([runbook](RUNBOOK.md#four-eyes-approvals-and-sign-off)).
- No licence is asked for: this build enforces none (section 3, The licence). In a vendor build, runs,
  compares and soak cycles under SSO also need an Enterprise licence, and so do sign-off and evidence
  packs in every mode; the licence never refuses sign-in, reads, reports or administration.
- Writes from another origin or site are refused (403), and so are reads another site's page makes,
  such as a link or embed of a `/v1` report URL on the intranet (open reports from the app instead).
- A request body over 10 MiB is refused with 413.
- No interactive API docs are served.
- `POST /v1/mcp` (section 3, The MCP server) is off unless `mcp.http` is true (404). It takes an API
  token as bearer and never a browser session (403 `MCP takes an API token (Authorization: Bearer
  dst_…), not a browser session`), and has no OAuth. A `viewer` token is offered the reads, and
  `compare_key` only a token holding `operator` where `mcp.work` lists it; inside MCP no caller acts
  above `operator`, whatever it holds. Those cuts, and the `mcp` block's, are that route's: every other
  `/v1` route takes the same token with its full role. The server keeps no stream and no session (a
  GET or a DELETE answers 405), and takes a body of 65,536 bytes at most. A request with another origin's `Origin` is
  refused like any write (403) and no CORS header is sent, so an MCP client that is a page on another
  origin does not work, by design.

**Tokens mode** (`auth` without `oidc`) is for CI and trials (in a vendor build, also production under
a Community or Pro licence: section 3, The licence). The UI asks for an API token, and
whoever signs in with one is audited as that token. No token can hold `admin`, `rule_approver` or
`pii_reader`, so tokens, sessions and the audit log are managed with the CLI (section 9).

**Developer mode** (no `auth`) is for one developer's workstation: every local user and process acts
as admin. It serves loopback clients only and refuses requests that carry a proxy header
(`Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host`, `X-Forwarded-Proto`, `X-Real-IP`, `Via`).
**Never put a reverse proxy or a port forward in front of it:** one that adds none of these headers,
such as a bare `proxy_pass` or `ssh -L`, cannot be detected.

### Acceptance checklist

Run these checks before the office uses DeltaSleuth, with a test user in each role and one in no
mapped group:

```sh
DS=https://deltasleuth.bank.example
curl -s $DS/v1/health                                                              # 0: "auth": "oidc"
curl -s -o /dev/null -w '%{http_code}\n' $DS/v1/runs                               # 1: 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Forwarded-User: admin' $DS/v1/runs  # 2: 401
curl -s -X POST -H 'Origin: https://evil.example' $DS/v1/specs                     # 3: 403 cross-origin request refused
curl -s -H 'Sec-Fetch-Site: cross-site' -H 'Sec-Fetch-Mode: no-cors' $DS/v1/auth/login   # 3: 403 cross-site sign-in refused
```

0. Health reads `"auth": "oidc"`. `"auth": "tokens"` means the `oidc` lines of `deltasleuth.yaml` are
   still commented (section 3): the installation is a trial's, and nobody signs in through the IdP.
1. A request without a credential gets 401.
2. A forged `X-Forwarded-User`, or an ID token of another application sent as a bearer, gets 401.
3. A cross-origin POST gets 403, and so does a cross-site sign-in (what an `<img>` on another page
   would send). In tokens mode, which has no SSO, the fourth command answers 404 `SSO is not
   configured`, and items 4 to 18, which need SSO, do not apply.
4. The user in no mapped group is refused: "Your account has no DeltaSleuth role".
5. A Run screen left open, untouched, returns to the sign-in page after `session_idle_minutes`.
6. After Sign out, the next sign-in asks the IdP for credentials again.
7. Disabling a user (Admin, Users) takes effect at their next request, and with the tokens box
   ticked their tokens fail with 401 too.
8. Removing a user from a group changes their roles at their next sign-in. Editing `auth.oidc.roles`
   and restarting the app ends every session.
9. Creating a token more than 15 minutes after signing in asks for a new sign-in.

Then the four-eyes bypasses ([APPROVALS.md](APPROVALS.md) §3), with a spec whose `security.clear` lists
a path, and test users A (`spec_author` and `rule_approver`), B and C (`rule_approver`) and O
(`operator`):

10. O cannot run A's upload before it is approved: "needs a four-eyes approval of clear before it
    runs".
11. A, who uploaded and requested it, cannot approve it, and neither can the person a token that
    uploaded it names as accountable: the review disables Approve with "approval needs an approver
    other than you: …".
12. The same spec uploaded through a token shows "Two approvers needed": B's approval is step 1, B
    cannot take step 2, and C's completes it. (With SSO, `ds run` uploads no version that needs an
    approval.)
13. An API token and the CLI cannot approve: a token's decision gets 403 `needs the rule_approver
    role`, and the CLI has no grant command.
14. B uploading the same content again, or a new version of A's spec, leaves A excluded from
    approving it.
15. A decision that sends a content hash or scopes other than the version's is refused ("the reviewed
    content hash or scopes are not spec 12's; reload and review again"), and so is approving more than
    15 minutes after signing in ("Sign in again").
16. Revoking the approval pauses O's running run with `run_paused_approval`, and resuming it is refused
    ("needs a new four-eyes approval: A-… was revoked") until a new approval.
17. A sign-off of a cycle that ran without an approval, or of a partial or cut cycle, is refused, and
    whoever started the run or requested the sign-off cannot approve it (B starts a run and requests
    its sign-off; B's Approve is disabled, C's is not).
18. Every step above appears on Admin, Audit and in the SIEM copy (section 10), attributed to a
    person (`user:U-…`) or a named token (`token:T-…`); an `approval.grant` row carries the
    approver's `auth_time`.

Then the licence, of which this build needs none (section 3, The licence):

19. `ds licence` prints `licence none: this build needs no licence`, `state open` and `allows: runs yes,
    sso yes, signoff yes, packs yes`; the UI shows no licence banner, and the Licence screen says `This
    build needs no licence and limits nothing.` under the installation id.
20. Within the hour the worker's one `licence.state` row, `to: open`, is on Admin, Audit and in the SIEM
    copy. No `licence.install` row exists.

In a vendor build the two items read: `ds licence` names the licence installed, `state valid`, and under
Enterprise the same `allows` line, and the Licence screen lists the licence with who installed it;
`licence.install` (with the customer and order) is on Admin, Audit and in the SIEM copy, and so, within
the hour, is the worker's `licence.state` row.

Then the release (section 2), in its folder:

```sh
grep -E '^(config|manifest): ' RELEASE.txt
docker inspect --format '{{.Image}}' deltasleuth-app-1
docker image inspect --format '{{index .Config.Labels "org.deltasleuth.base"}}' deltasleuth:0.1.0-ca
docker image inspect --format '{{join .RootFS.Layers " "}}' deltasleuth:0.1.0-ca | grep -cF "$(docker image inspect --format '{{join .RootFS.Layers " "}}' deltasleuth:0.1.0)"
```

21. The app runs the signed image: the second command prints the `config` or the `manifest` digest
    that the first lists. With a private CA it prints the `-ca` image's id instead (section 11, TLS):
    then the third prints `config` or `manifest`, and the fourth `1`, since the `-ca` image's layers
    begin with the release's.

Then notifications, when the settings have `notify` (section 3, Notifications):

22. `ds notify check` prints an `ok:` line for each channel and exits 0.
23. `ds notify test <channel>`, as an operator, prints `sent to <channel>: HTTP 2xx …` (`SMTP 250` for
    e-mail) for each channel, the receiver verifies the webhook's signature, and `notify.test` is on
    Admin, Audit and in the SIEM copy. So does Send test on the Notifications screen.
24. The next `notify.daily` arrives, the day's first hour from `daily_hour` (UTC) with a rule routing it:
    it is the heartbeat whose absence shows the notifier down.

Then the file reader, when the targets file declares a files target (section 7, Files):

```sh
docker compose run --rm app python -c "from deltasleuth.connectors.tables import duck; duck.selftest()"
```

25. It prints `file reader: duckdb 1.5.5 in a process of its own, replaced when it ended; data limit
    held, sockets denied` and exits 0. Above that line stand one or two lines of the self-test's own
    doing, `the file reader's process ended (SIGSEGV) with no statement in flight`: it faults one reader
    to see it replaced, and its small data limit ends another. **On an x86_64 host this is the first
    time an x86_64 kernel loads the reader's socket filter and enforces its data limit under DuckDB:**
    both were run natively on aarch64 only (in an image built from the source tree, 2026-10-04), the
    filter's x86_64 arm was judged by an interpreter of its program in the test suite, and the amd64
    image was run under emulation, which is no x86_64 kernel. **Where a limit does not hold the
    self-test prints no such line: it fails.** The command exits 1, and its output ends in a Python
    traceback whose last line names both limits, the one that did not hold with its reason: `RuntimeError:
    the reader's limits did not hold: no data limit (not held on this host), sockets denied` (observed
    in the image on aarch64 under a hard data limit of the host's own, `docker run --ulimit data=…`, below the
    reader's), or `…: data limit 3,328 MiB, sockets not denied (not held on this host)`. Its other
    failures are tracebacks too, among them `RuntimeError: the data limit did not hold` (a statement
    completed under the small limit), `the reader's process or its directory outlived its end`, `the
    sort did not spill` and `the files were not read`. Report any of them with the whole output. `ds target check <id>` of a files
    target (section 9) starts a reader under your own settings and prints in its first line
    what that start saw: `… read in a process of its own (data limit 5,296 MiB, sockets denied)`, or
    each limit that did not hold with its reason, `(no data limit (not held on this host), sockets
    denied)`; the service's log holds the same in its line `the file reader started: …`. Until a
    failure is resolved, `files.os_limits: false` runs the reader with neither limit (the self-test
    then is `python -c "from deltasleuth.connectors.tables import duck; duck.selftest(limits=False)"`,
    which ends `…; limits not tried`).

Then the MCP server, when the settings are to have `mcp` (section 3, The MCP server), with a `viewer`
token made for the check, its one line in `~/.config/deltasleuth/mcp-token` (mode 0600) on a
workstation:

```sh
DS=https://deltasleuth.bank.example
LIST='{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
mcp() { printf 'header = "Authorization: Bearer %s"\n' "$(cat "$HOME/.config/deltasleuth/mcp-token")" |
    curl -s -K - -X POST -H 'Content-Type: application/json' -d "$1" $DS/v1/mcp; }
curl -s -X POST -H 'Content-Type: application/json' -d "$LIST" $DS/v1/mcp   # 27: 401 not signed in
curl -s -X POST -H 'Origin: https://evil.example' $DS/v1/mcp                 # 28: 403 cross-origin request refused
mcp "$LIST"                                                                  # 29: ten tools, no compare_key
```

26. Before the block is added to the settings, `mcp "$LIST"` answers 404 `MCP over HTTP is off
    (mcp.http, docs/mvp/MCP.md §4)`: MCP is off by default. The items below are with `http: true` and
    `data_classes: [aggregates, production_tokenised]`.
27. A request without a credential gets 401 `not signed in`.
28. A POST with another origin's `Origin` gets 403 `cross-origin request refused`. On the Docker host,
    `curl -s -X POST -H 'Host: evil.example' http://127.0.0.1:8000/v1/mcp` gets 400 `unexpected Host
    header`.
29. The viewer token lists ten tools and no `compare_key`: the names on the `viewer:` line of `ds mcp
    check`. A signed-in browser session is refused there: a POST with the session's cookie and CSRF token
    answers 403 `MCP takes an API token (Authorization: Bearer dst_…), not a browser session`.
30. Through a wired client ([runbook](RUNBOOK.md#your-own-agent-mcp)), `get_observations` of a key of a
    run shows the `tok:` and `class:` values the Key screen shows for that key, and no raw one.
31. `get_run_summary` of a run whose spec lists a `clear` path answers `run R-… is not served by this
    installation: its spec declares clear values or a business-code key (mcp.data_classes lacks
    production_clear_declared)`. The refusal is MCP's own: the same token still reads that run on the
    other `/v1` routes, as any viewer (section 3, The MCP server).
32. Admin, Audit and the SIEM copy (section 10) hold the agent's rows under its token (`token:T-…`),
    each with `via: mcp:http` in its detail: `key.view` for item 30, and `mcp.read` once the agent has
    read that run's summary, root causes or findings, at most one an hour per token, tool and run. Item
    31's refusal writes none.
33. Revoke the token (Admin, Tokens; or `ds token revoke T-…`): `mcp "$LIST"` then answers 401
    `invalid or expired API token`.

Then one item per client, each run once and recorded with the client's version and the date.
DeltaSleuth's own tests drive the server with the MCP library's client and only parse the runbook's
client blocks: how each client reads its block (a headers helper, a prompted input) is as its own
documentation describes it, was not run for this guide, and is what these items confirm.

34. Claude Code, wired with the runbook's `.mcp.json` and headers helper, lists the tools and reads a
    run.
35. VS Code, wired with the runbook's `.vscode/mcp.json` and given the token at its prompt, lists the
    tools and reads a run.
36. Cursor, in developer mode on a workstation (`ds serve` on loopback, the runbook's `.cursor/mcp.json`
    with `X-CSRF-Token: dev`), lists the tools and reads a run; so does a client that starts `ds mcp`
    over stdio there.

## 6. First run, without touching real endpoints

The `mocks` profile starts both mock pairs in one container, `mocks`. The example `targets.yaml`
already points the `linker-*` and `customer-*` targets at it.

**No licence comes first:** this build needs none (section 3, The licence), so no run below is refused
for one, under SSO either, and every spec name you run is free: no pair is counted, and the mock pairs,
the public pairs and your own all run on one database. **In a vendor build, install the licence first:**
with SSO and no Enterprise licence, the run below is refused (`SSO (auth.oidc) is an Enterprise feature
…`). Every spec name you run is then one of the licence's pairs for its whole period: under unlimited
pairs that costs nothing, but under a licence that counts them (Community, Pro) each smoke-test spec
below holds a seat, so run them on a scratch database instead (a copy of `deltasleuth.yaml` whose
`database_dsn_ref` and `evidence_dir` name another database and directory, used with `ds run --config`,
section 9). There a fresh database is its own unlicensed evaluation: two pairs, so the mock pair and one
public pair.

The commands below call the API with a token of your own. Sign in at `public_url` as an admin, open
Admin, Tokens, and create a token named `first-run` with the role `spec_author` and yourself as the
accountable person. It is shown once: copy it, and paste it when `read` waits, which keeps it out of
the shell history. In tokens mode, create it with `ds token create` instead (section 9).

```sh
docker compose --profile mocks up -d
read -rs TOKEN                                      # paste the token, then Enter
SPEC=$(curl -fsS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/yaml' \
            --data-binary @mocks/customer/spec.yaml http://127.0.0.1:8000/v1/specs \
       | sed -E 's/.*"spec_id": ([0-9]+).*/\1/')
```

With SSO the spec needs a four-eyes approval before it runs: its `security.clear` paths and
`knownDiffs` rules expose values and suppress findings (the upload answers `needs`). Request the
version's approval on the Specs screen, then have two people holding `rule_approver`, neither of them
you, approve it on the Approvals screen (an upload through a token names no author, so it takes two).
In tokens mode skip this step: the run goes ahead, labelled unapproved. Then start the run:

```sh
curl -fsS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d "{\"spec_id\": $SPEC, \"mode\": \"batch\"}" http://127.0.0.1:8000/v1/runs    # returns 202 {run_id}
```

You can do the same in the UI: Specs, then upload and Request approval; Approvals, for the approvers;
Runs, then new run. A version uploaded on the Specs screen while you are signed in needs one approver
other than you.

A run without keys uses the spec's own key list. Watch the run on the Run screen. It ends
`completed` with the 40 keys that `mocks/customer/expected.json` describes:

- 35 MATCH;
- 2 MISMATCH;
- 2 KNOWN_DIFF;
- 1 NOT_FOUND_B.

That file names the keys raw, while the screens show them as tokens. Download `report.pdf` and the
CSVs from the Run screen, or from `GET /v1/runs/<run_id>/reports/<name>`.

When you are done:

1. Run `docker compose --profile mocks stop mocks`, and revoke `first-run` on Admin, Tokens.
2. Delete the mock targets from `targets.yaml`, then run `docker compose restart app`.

Finished runs stay readable until their retention ends (section 10), because each run keeps the
capabilities its targets declared when it was created. A target id is part of that history: give a changed service a new id instead of reusing
one.

If the host can reach the internet, the public pairs in `examples/public/` are a second smoke test on
real APIs:

1. Append the entries of their `targets.yaml` under `targets:` in `deploy/config/targets.yaml`.
2. Run `docker compose restart app`. Targets are read at start.
3. Upload the spec. With SSO, request its approval on the Specs screen and have `rule_approver`s other
   than you approve it (two for a token upload), as above: both specs need one (`unlisted_clear`).
4. Run it with the keys from `keys.txt`.

Each example's README lists the outcome every key should get.

## 7. Point it at your endpoints

**In the product.** A service is declared without the targets file and without a restart: in the
browser on the Targets screen ("Declare an endpoint", then `Change` and `Remove` on its row), or by
`PUT /v1/targets/{id}`, changed by another `PUT` and removed by `DELETE /v1/targets/{id}`
([AUTH.md](AUTH.md) §4.1 says who may: an admin signed in within 15 minutes; in a one-machine trial as
`deploy/up.sh` makes it, a `spec_author` token; in tokens mode behind https no one, and the targets file
below is the only way). The screen lists every target and where it is declared (`targets file` or
`product`) to every role; who may declare also sees what a target of the product's list was declared
with, and the form. Its fields are the body's members, which are what you would give a REST client:

```json
{"base_url": "http://host.docker.internal:8081/api", "allow_private_network": true,
 "allow_insecure_http": true,
 "auth": {"kind": "header", "header": "Cookie", "more": [{"header": "X-CSRF-Token"}]},
 "secret": "JSESSIONID=…", "more_secrets": {"X-CSRF-Token": "…"},
 "rate_per_s": 5, "max_concurrency": 2, "absent_on_404": true}
```

- **The same rules as the targets file.** Each member is judged by the file's schema and address rules
  (step 1), and a problem is a 422 `invalid target` with the file's own issues. Only a service is declared
  this way: a database, a files target and any `capabilities` but `absent_on_404` (a 404 means "no such
  record"; without it a 404 proves nothing and the key reads INCONCLUSIVE) stay in the targets file.
- **Credentials.** `auth` is `none`, `bearer` or `header`; a `header` may name up to three further
  headers in `more`, for an app that wants a session cookie and an anti-forgery header together (the
  targets file takes `more: [{header, secret_ref}]` too). `secret` is the value of the bearer or of the
  first header, `more_secrets` the others' by header. A value is written once (section 3, Secrets) and
  never shown again: the answers, the audit rows (`target.set`, `target.delete`) and the logs hold header
  names only, and where a service answers the request it was sent, `Send` shows
  `[the target's credential]` in place of each string of the answer that holds the whole value. A value not sent is kept, while the base URL, the kind and its header stay as they were;
  change one of those and the value is asked for again, so a stored credential never follows a target to
  another address. A value that holds this installation's own session or sign-in cookie
  (`ds_session_<port>` and `ds_login_<port>` in a one-machine trial, `__Host-ds_session` and
  `__Host-ds_login` behind https) is refused, 422 `secret is refused: it holds DeltaSleuth's own cookie
  <name>, which signs in here and which a browser sends to every port of this host: leave that cookie
  out`. A browser keeps cookies by host, not by port: a request to an app on this machine, copied from
  the browser a trial is open in, carries the trial's cookie beside the app's own.
- **An app on this machine.** Inside the container `localhost` is DeltaSleuth itself, and the route says
  so. Write the host as `host.docker.internal` and keep the port, with `allow_private_network: true`, and
  `allow_insecure_http: true` for a credential over plain http. Docker Desktop resolves the name by
  itself; on Linux the app service's `extra_hosts` line does, and the app you call must listen on the
  Docker bridge's address, not on 127.0.0.1 alone (not observed).
- **Where it lives.** `targets/targets.json` in the `workspace` volume, which the server alone writes:
  never edit it. Every `ds` command and service reads it at its start beside the targets file; for an id
  in both, the targets file wins and the start logs
  `warning: target <id> of the product's list is ignored: the targets file declares it`. A list that does
  not load stops the start, naming the file in each issue. A change holds at once in the process that
  served it (`ds serve`, with its worker). A separate `ds worker` or `ds mcp` reads the list when it
  starts: restart it after a change, because the file of a replaced credential is removed, and its next
  connection to that target fails the run with
  `the stored credential of target <id> cannot be read: enter it again`. `ds spec validate --targets`
  and `ds target check` read the targets file alone.
- **What the list is trusted for.** No more than the product writes, whoever changed the file (a restored
  backup, another container on the volume): a service's members, `absent_on_404`, and each credential
  as a file of its own target under `targets/secrets/`. An entry holding anything else (a database's or
  a files target's members, another capability, a credential reference to any other file) stops the
  start as a list that does not load, and every change answers 422 `invalid target` with the entry's
  issues until the file is put right or removed. So does a symbolic link, checked when the list is read
  (each start and each change): the `targets` folder, its `secrets` folder or a credential an entry
  names that is a link is the issue `link` (`... of the product's list is a symbolic link: remove the
  link`), with or without a list, so a file outside is neither sent to an entry's address nor removed. The list holds what its reader takes, 4 MiB (about
  2,000 targets with the longest base URL): the change that would pass it answers 422
  `the product's list of targets is full (4 MiB): remove one first`, and nothing is written.
- **No credential stays behind.** After each change, and when the server starts, every regular file
  under `targets/secrets/` that the list does not name is removed (a link, a folder or anything else
  there is left alone, and nothing is listed through a linked folder): a replaced or removed credential, and what
  a change that was killed left there. One that cannot be removed is logged by count, never by name
  (`warning: N file(s) no target names could not be removed from the product's secrets folder: the next
  change and the next start try again`). Keep no file of your own in that folder.
- **While runs read it.** A target that unfinished runs read is neither removed nor moved (409
  `target <id> is read by <n> unfinished run(s): stop them first`): only its credentials and its budget
  change under them. A run counts once it is written, which is after its keys were listed: a target
  moved or removed while a run's key listing is still being read is changed, and that run is refused
  instead (422 `target <id> was changed or removed while the run was being created: start it again`; a
  schedule's firing reads it as `not_started` and tries again). The budget holds at once. A credential holds for every run and compare started
  afterwards, and for a run under way once it connects again: a run the old credential's refusal paused
  reads the new one when it is resumed.
- **Try it, then build the comparison.** On the Specs screen "New comparison" opens the comparison
  builder, and a stored spec's name opens it on that version (for a `spec_author`; others read the stored
  spec). It holds a `Sample key`, each service side's `Target`, `Method`, `Path`, `Query`, `Headers` and,
  for a POST, `Body (JSON)` as a form, and the rest of the spec as YAML. `Send` asks that one endpoint
  once for the sample key (`POST /v1/specs/try`): a run's read of the side, under the target's budget,
  the address guard and the spec's timeouts, in one attempt and for an answer of 1 MiB at most. It
  stores nothing but its audit row (`target.try`: who asked which target, with the key as reports show
  it, a stand-in unless the spec declares the key a business code; never the answer).
  A one-machine trial shows the answer as it came. Every other installation shows its structure alone:
  each string and number is a stand-in, equal values get equal stand-ins, and the answer of a 401 or 403
  is not shown at all. A member name stays unless it reads as data (an id, or a name with three digits
  in a row or four in all); a name that is data with fewer digits (`Smith`) shows. `Import curl…` reads a request copied from a browser or a REST client as cURL for
  bash into the form. It leaves out the headers of a browser's cache (`If-None-Match`,
  `If-Modified-Since`, `If-Match`, `If-Unmodified-Since`, `If-Range`: with them a service answers 304
  without a body) and DeltaSleuth's own session and sign-in cookies, by name. A credential the command
  carries never enters the spec: the screen names its header, and "Declare the endpoint…" hands it to
  the Targets form, in memory only. Where the side's target is one of the product's list and the command
  reached it (the same scheme and port, and the same host or two names of this machine, such as the
  command's `localhost` and the target's `host.docker.internal`), the form is `Change <id>`: the target
  keeps its base URL, consents and budget, and only how it authenticates is filled in. Otherwise the form
  is a new endpoint at the command's origin with the base path the import took off the path.
  `Save and compare this key` stores the spec and compares the key. What it stores is a version like any
  other, never deleted and at once the newest of its name, which schedules that follow the name run
  next: build under a scratch name. Under single sign-on a version with `security.clear`, known
  differences or filters is not compared until it is approved, after every edit: build such a spec in a
  trial installation.

**1. Declare each endpoint in `deploy/config/targets.yaml`.** A target holds only what you can
observe from outside:

- **`base_url`.**
- **`allow_private_network: true`** for internal hosts. Private, loopback and link-local addresses are
  refused without it.
- **`auth`:** `none`, `bearer` or `header`, each with a `secret_ref`. Add each credential as a secret
  (section 3, ending with `docker compose up -d`).
- **`rate_per_s` and `max_concurrency`:** the load the service owners agreed to. A budget is held by
  one process. One worker processes every key of every run, and while it is the one inside `ds serve`
  these limits hold across all runs, and the app's one-key compares and key listings, together. From
  the command line, `ds compare` and the key listing of `ds run` use a budget of their own process.
  **With `ds serve --no-worker` beside `ds worker`** (below, for cross-engine scans) the API process
  has a budget of its own too: its one-key compares (`POST /v1/compare`, "compare now", the chat's
  `compare_key`) and the key listings of the runs it creates are not counted against the worker's, so
  a target can see up to twice `rate_per_s` and twice `max_concurrency` while both are busy. Declare
  half of what the owners agreed to if the sum must stay inside it.
- **`capabilities`:**
  - `authoritative_absence`: which statuses (and, via `absence_shapes`, which bodies) truly mean
    "no such key". A GraphQL-style 200 needs a shape. See `examples/public/rickandmorty`.
  - `truncation_indicators`: when the service caps list lengths.

**A database target** is a PostgreSQL database, whose tables or views a spec compares
([TABLES.md](TABLES.md)), or a MySQL or Oracle one (below). A PostgreSQL target holds only a DSN
reference and a budget:

```yaml
  crm-legacy:
    dsn_ref: file:/run/secrets/crm_legacy_dsn
    rate_per_s: 50
    max_concurrency: 2
```

- **The DSN** is a secret of one line: a libpq keyword string or a `postgresql://` URI (in a URI,
  percent-encode the user and the password, `@` as `%40`). Write it, then add it as a secret
  (section 3, steps 2 to 5):

  ```sh
  printf '%s' 'host=crm-db.internal dbname=crm user=ds_reader password=CHANGE-ME sslmode=verify-full sslrootcert=/run/secrets/crm_db_ca' > deploy/secrets/crm_legacy_dsn
  chmod 444 deploy/secrets/crm_legacy_dsn
  ```

  For TLS, `sslmode=verify-full` checks the server's certificate against `sslrootcert`, the
  database's CA certificate, added as a secret the same way. `sslmode=require` encrypts but checks
  nothing. The DSN is read whenever a connection opens and never appears in logs, evidence or errors.
  One libpq cannot parse, or a URI with a raw `@` anywhere but once before its first `/` (an
  unencoded `@` or `/` in the password), is refused as
  "the DSN of target … is not a valid connection string". One that cannot connect refuses the run
  when it is created, with libpq's first line (the host and the user, never the password).
- **A read-only role.** Every DeltaSleuth session is read-only (`default_transaction_read_only`), but
  ask the database owners for a role that can read the compared tables and nothing else:

  ```sql
  CREATE ROLE ds_reader LOGIN PASSWORD 'CHANGE-ME';
  GRANT CONNECT ON DATABASE crm TO ds_reader;
  GRANT USAGE ON SCHEMA crm TO ds_reader;
  GRANT SELECT ON crm.customers TO ds_reader;   -- every table or view a spec names
  ```
- **`rate_per_s` and `max_concurrency`** cap the per-key reads (the keys a scan did not find
  identical, explicit keys, one-key compares): per second, and in flight, across all runs together.
  Between two tables, one read is a group of up to 200 keys in one query. A scan is one long query
  per side, outside both budgets. A run holds one session per side for its per-key reads, and one
  more per side while it scans: four on one database that holds both tables.
- **What a scan costs each database** ([BENCHMARK.md](BENCHMARK.md)): one `REPEATABLE READ` snapshot held
  for the whole scan, which holds back vacuum on that database meanwhile, and one backend that sorts the
  whole population by key on one core (a cursor never gets a parallel plan), side a's before side b's.
  With stock settings the sort spills to temporary files on the database's volume: 61 to 63 bytes a row
  for a `bigint` key, whatever the row's width, held until the scan ends; graph parity's L1 and L2 sorts
  write 201 to 211 bytes a link fact in two phases, L1's files gone before L2's sorts begin, at most 107 to
  121 of them held at once (the benchmark's 4-column rows at 1M and 10M and 24-column rows at 1M). On an
  Apple M3 Max with both databases on one stock PostgreSQL 16 in Docker (synthetic 4-column rows, 101
  differing keys, warm cache, worker poll 0.05 s), both sorts of 10M rows a side took 36.6 s (36.6–36.7) of
  a 68.9 s (68.7–69.1) scan. Keep
  `temp_file_limit`, if you set it, and the free space above that: a sort the server refuses
  (`ConfigurationLimitExceeded`) or cannot spill (`DiskFull`) logs `scan of run R-... paused` and retries
  every `worker.lease_seconds` without end while the run shows `running`. Cancel it (Run screen, or `POST /v1/runs/{id}/cancel`), then
  raise the limit or free space.
- **Filters, key maps, type maps and scheduled re-diffs** need nothing here: they live in the spec
  ([runbook](RUNBOOK.md#compare-two-tables)). A soak over two tables scans both in full every
  cycle: choose an `execution.cycleInterval` the databases can carry.
- **Graph parity** ([runbook](RUNBOOK.md#graph-parity)) reads each side's link facts from a table or
  view, through a database target like any other: no new secret. DeltaSleuth never reads a graph
  store: ask the system's owners for an export into PostgreSQL, one row per (entity, identifier),
  and grant SELECT on it. It needs no index. Its memory holds only the components around the
  identifiers that differ, each capped by the spec's `graph.maxComponent`; the identifiers
  themselves are compared as a stream.
- **On a hot standby,** a replication conflict may cancel a long scan. The worker logs
  `scan of run R-... paused` and resumes the scan from its last checkpoint after
  `worker.lease_seconds`, without counting anything twice.
- A PostgreSQL target takes no `base_url`, `auth`, `capabilities` or network opt-in: its DSN names the
  host, and a missing row is an authoritative absence. Its host is therefore not vetted: no address
  rule applies to a DSN.

**A MySQL or Oracle target** declares its endpoint instead of a DSN ([ENGINES.md](ENGINES.md) is the
contract, [TABLES.md](TABLES.md) §13 its summary). Prerequisites: observed on MySQL 8.4.11 and
Oracle 23.26.3 only (8.0 and 19c are supported, not observed: run the acceptance kit); Oracle over
plain TCP or TCPS, not Native Network Encryption; in an Oracle database that is not AL32UTF8, a run
takes text keys of up to 500 bytes, by a conversion observed on `NVARCHAR2` only; MariaDB and SQL
Server are not supported; `ds target check` reports each of these before a run is attempted. The
acceptance kits are `examples/tables/mysql/` and `examples/tables/oracle/`
([runbook](RUNBOOK.md#across-engines)).

```yaml
  ledger:
    engine: mysql
    host: ledger-db.internal                # a DNS name or an IP literal
    port: 3306                              # the default
    database: ledger
    user: ds_reader
    password_ref: file:/run/secrets/ledger_password
    tls_ca_ref: file:/run/secrets/ledger_db_ca
    allow_private_network: true             # a private address
    rate_per_s: 50
    max_concurrency: 2
  crm-legacy:
    engine: oracle
    host: crm-db.internal
    port: 2484                              # your TCPS listener; the default is 1521
    service: CRMPDB                         # a service name: no SID, alias or connect descriptor
    user: ds_reader
    password_ref: file:/run/secrets/crm_legacy_password
    tls_ca_ref: file:/run/secrets/crm_db_ca
    tls_server_dn: "CN=crm-db.internal,O=Bank"   # the server certificate's subject
    allow_private_network: true
    rate_per_s: 50
    max_concurrency: 2
```

- **Secrets.** `password_ref` is a file holding the password alone, and `tls_ca_ref` the CA certificate
  in PEM; add each as a secret (section 3). Both are read at every connect. The password is a driver
  argument, never part of a connection string, and no environment variable, option file (`~/.my.cnf`),
  `tnsnames.ora`, `sqlnet.ora` or wallet is read. Without `tls_ca_ref` the system trust store verifies
  the chain.
- **TLS has three modes,** and is enforced, not preferred: a MySQL server that offers no TLS is refused
  before the password leaves, and Oracle is dialled as TCPS.
  1. **Chain and name** (the default). A MySQL server's certificate must carry `host` in a
     subjectAltName. An Oracle server's subject must be `tls_server_dn`, which an Oracle target with
     TLS must declare: the session is opened to the address DeltaSleuth vetted, so no host name is
     matched, and DeltaSleuth compares the certificate's subject with the one you declare, whole and
     exactly: every attribute, in the same order, with the same value and case. Another certificate of
     the same CA does not pass, whatever it shares with yours. Write the subject as RFC 4514 does, the
     most specific attribute first and no space after a comma (`openssl x509 -noout -subject -nameopt
     RFC2253` prints it so: `CN=crm-db,CN=Computers,DC=bank,DC=example`); one that does not parse is
     refused when the targets file loads.
  2. **Chain only:** `tls_verify_name: false`, which needs `tls_ca_ref` and accepts any certificate that
     CA issued. It is for a certificate without a subjectAltName, such as the one a MySQL server makes
     for itself: give it that server's own CA (`ca.pem` in its data directory).
  3. **None:** `allow_unencrypted: true`. The password exchange and every compared row cross the network
     in clear; on MySQL the password is sent under the RSA key the server presents, which is trusted as
     it comes.

  An Oracle listener that requires Native Network Encryption cannot be reached (`DPY-3001`): ask for a
  TCPS listener. TLS declared against a plain listener (TLS is the default: port 1521 is usually plain)
  reads `ConnectionResetError: TLS: the handshake failed`.
- **Grants.** Ask the database owners for an account that reads the compared tables and nothing else.
  A MySQL session is read-only and every Oracle read runs in a read-only transaction, but what the
  account cannot do is decided by its grants, not by DeltaSleuth.

  ```sql
  -- MySQL: column grants are honoured, and a column the account may not read is not seen at all
  CREATE USER 'ds_reader'@'%' IDENTIFIED BY 'CHANGE-ME';
  GRANT SELECT ON ledger.customers TO 'ds_reader'@'%';      -- every table or view a spec names

  -- Oracle: READ, not SELECT (it refuses SELECT ... FOR UPDATE); no catalogue role
  CREATE USER ds_reader IDENTIFIED BY "CHANGE-ME";
  GRANT CREATE SESSION TO ds_reader;
  GRANT READ ON app.customers TO ds_reader;                 -- every table or view a spec names
  GRANT EXECUTE ON SYS.UTL_I18N TO ds_reader;               -- only in a database that is not AL32UTF8
  ```

  **The account must read the real values.** Values are compared as the account reads them: under
  Oracle Data Redaction, VPD column masking or a masking view both sides return the same constant and it
  reads as a match. Exempt the account from redaction and masking on the compared columns, and compare
  the base table or an unmasked view. DeltaSleuth cannot see a mask; the certificate says `rows and
  values as the account reads them (redaction, masking and row policies apply)`.
- **Firewall.** New outbound connections of the app's host, and of a `ds worker`'s:

  | To | Port |
  |---|---|
  | each MySQL target's `host` | its `port`: 3306 by default |
  | each Oracle target's `host`, and every address its listener may redirect a session to (each node behind a RAC SCAN, a shared server's dispatcher) | its `port`: 1521 by default, or the TCPS listener's (2484 by convention) |

  At every connect DeltaSleuth resolves `host`, refuses a private or loopback answer without
  `allow_private_network` and a link-local, unspecified, multicast or metadata one always, and opens
  the session to an address it vetted. An IPv6 answer that stands for an IPv4 address is judged as that
  address: behind DNS64 and NAT64 a private database answers `64:ff9b::a01:203`, which needs the opt-in
  as 10.1.2.3 does (the well-known prefix only: a network's own NAT64 prefix is not known to the guard).
  A name with several addresses is tried address by address inside one 10 s budget, each address given
  an equal share of what is left, so one that never answers does not hide the next. One path is not vetted: an Oracle listener may redirect a
  session and the driver follows it. **Bound an Oracle target with an egress rule that lists its
  listener and node addresses.**
- **Sessions.** A run holds one session per side for its per-key reads and one more per side while it
  scans; a key census or a sampling frame takes one; creating a run opens two short ones per side, one
  after the other; closing a MySQL session whose statement still runs opens one more short one to cancel
  it. Set the account's `max_user_connections` (MySQL) or its profile's `SESSIONS_PER_USER` (Oracle) to
  at least three per side for every run that reads the server at once: six when one account serves both
  sides of a pair. A one-key compare is a one-shot run of its own in that count, and it is read by the
  process that was asked: with `ds serve --no-worker` the API process opens its sessions (the two short
  ones of each run it creates, and one per side for each compare in flight) beside those the worker
  holds, and no process waits for the other. Size the limit for both: the worker's runs, plus as many
  compares as operators and chats may ask for at once. A refused login pauses the run and is never retried in a loop, so a stale password
  does not lock an Oracle account through its profile. An account at its limit (MySQL 1226, or 1203 for
  the server's own; ORA-02391) and a database in restricted session (ORA-01035) are waited out: a scan
  pauses a lease period and tries again, without limit, and a per-key read records a connect error.
- **What a scan costs each database.** Each side sorts its whole population by its keys' text, in one
  transaction held for the whole scan. Between two tables of one engine the server sorts the keys and
  one 32-byte hash a row; across engines it sorts every compared column of every row, all of which cross
  the network to the worker, so temporary space and time grow with the row's width.
  - **Oracle:** the scan is one read-only transaction, so undo must cover its duration (`UNDO_RETENTION`,
    the undo tablespace). A pass that ends `ORA-01555` after it saved a checkpoint resumes from it in a
    new snapshot, which the certificate then names; a first pass that saved none is tried again, and the
    third fails the run (`SnapshotLost: ORA-01555`). The sort takes TEMP: `ORA-01652` fails the run.
  - **MySQL:** the scan holds one consistent snapshot of an InnoDB table, which holds back purge
    meanwhile; a view is read in one transaction whose snapshot cannot be observed, and a table of
    another storage engine in none, which the certificate warns of. The sort is a filesort on the key's
    binary text, on the server's temporary directory: a full one fails the run at once, as
    `TargetTempSpace: MySQL 3` (the code 8.4.11 answers, observed; 1, 1021, 1028 and 1114 are classed
    alike).
  - **A code DeltaSleuth does not class** (`TargetRefused: <code>`) is tried again, a lease period
    later when the pass saved no checkpoint, and fails the run at the third such pass in a row.
  - **Stopping a scan.** Cancelling or pausing a run, and stopping the worker, cancel the scan's
    statements on both servers (`KILL QUERY` from a second short session on MySQL, a cancel on Oracle)
    and close its sessions, within seconds. The cancel is best effort: where it cannot be sent, the
    sort runs to its end.
  - **Measured** ([TABLES.md](TABLES.md) §13.9: each run once, on one Mac that was not idle, every
    database a stock container on it, 4-column synthetic rows, 11 differing keys): 1M rows a side took
    27.9 s between PostgreSQL and MySQL, 25.9 s between PostgreSQL and Oracle, 23.0 s between MySQL and
    Oracle, 6.4 s between two MySQL tables and 6.2 s between two Oracle tables, against 6.8 s between two
    PostgreSQL tables. 10M rows a side scanned in 280 s between PostgreSQL and MySQL (3.6 times the
    77.5 s of two PostgreSQL tables in [BENCHMARK.md](BENCHMARK.md), another harness) and in 64 s between
    two MySQL tables; the worker's process peaked at 191 MiB and 180 MiB. 20,000 differing keys of
    100,000 took 38.2 s, 35.9 s and 34.1 s in the three cross-engine pairings. Not measured: 10M rows
    with an Oracle side, MySQL 8.0, Oracle 19c, wider rows, a database on another host, and the
    temporary space a MySQL or an Oracle sort takes.
  - **Run the worker beside the API for cross-engine scans.** The worker parses and hashes every row of
    such a scan in Python, on one core, and inside `ds serve` it shares the interpreter with the API:
    while the 10M-row scan above ran, `GET /v1/runs/{id}` answered in 162 ms at the 95th percentile
    (246 ms at most), against 10 ms (21 ms) with `ds serve --no-worker` and a `ds worker` beside it;
    between two MySQL tables, 65 ms against 10 ms. On the compose stack, in the site file, give `app`
    the command `[ds, serve, --host, 0.0.0.0, --port, "8000", --no-worker]` and add a second service of
    the same image, volumes and secrets with the command `[ds, worker]`, no published port,
    `healthcheck: {disable: true}` (the image's check probes the API port) and `stop_grace_period: 30s`.
    `ds worker` stops on SIGTERM as `ds serve` does: it cancels its scans' statements on their servers
    and hands its leases back, and closing a session whose cancel cannot be sent waits up to 10 s a
    side, more than Compose's default grace of 10 s. Give `app` the same grace where it runs the worker
    itself. A worker killed instead loses nothing (its leases expire and another takes over), but its
    scans' statements run to their end on the databases. One worker per database
    processes keys, so run one. The two processes share no budget and no session: each target's
    `rate_per_s` and `max_concurrency` hold in each of them (step 1), and the API process's sessions
    come on top of the worker's (Sessions, above). The two commands are tested as two processes, and a
    terminated `ds worker` by a test of its own; this compose layout is not exercised by a test.
- **A replica or a standby** is declared as any target. A MySQL replica, an Oracle standby and RAC are
  not observed. A scan compares what that server holds when its snapshot is taken, so replication lag
  shows as differences, which a later cycle no longer finds.
- **Check a target before a run:** `ds target check ID [--table [SCHEMA.]NAME]` (section 9) resolves and
  vets the address, opens a session as a run does and, with `--table`, lists the table's columns with
  their declared types and classes. It reads no row and opens no store.
- **Refused on these sides:** graph parity, `regex` key maps and `ILIKE` ([TABLES.md](TABLES.md) §13).

**Files: CSV and Parquet sides** ([FILES.md](FILES.md) is the contract, [TABLES.md](TABLES.md) §14 its
summary). A table side may be CSV or Parquet files that your export job writes into a directory on the
host. DeltaSleuth reads them in place through DuckDB, which runs in a child process of each `ds`
process, the file reader ([READER.md](READER.md)): no server, no credential, no outbound connection,
and no object storage (sync a bucket into the directory with your own tool).
Observed on DuckDB 1.5.5 with files of DuckDB's own writer; Parquet from Spark, Hive, Impala or Arrow is
not observed: run the acceptance kit of `examples/tables/files/` and then a small export of your own
first ([runbook](RUNBOOK.md#from-files)).

1. **Mount the directory read-only into every service that runs `ds`** (the `app`, and a separate
   worker when you run one), under `/var/lib/deltasleuth/files`, which the image creates. In
   `docker-compose.site.yml`:

   ```yaml
   services:
     app:
       volumes:
         - /srv/exports/legacy:/var/lib/deltasleuth/files/legacy:ro     # SELinux hosts: ":ro,z"
   ```

   The files and their directories must be readable by uid 10001 (the container's user: `chmod -R o+rX`,
   or a group it is given), and a run is refused while the mount is writable for DeltaSleuth, unless the
   target declares `allow_writable: true`, which is for development and is printed on the certificate.
   The mount of each file read is judged, not only the root's: an export mounted below a target's root
   needs `:ro` too.
   **Mount the export before the service starts**, and keep it mounted: a mount point whose mount is
   not there is an empty directory to the container. A new run is then refused (`no file matches …`), a
   schedule waits for it as for a late export, and a run in flight pauses (`scan of run R-… paused:
   Empty` in the worker's log) and goes on when the mount is back. A root that holds other files than
   the run's has changed, and fails it.
   On an SELinux host the mount needs a label the container may read (`:z`, or your policy's). Use a
   mount with stable inodes: DeltaSleuth pins each file by size, both times and inode, so a CIFS mount
   with `noserverino`, or any mount whose inode numbers change between listings, fails runs as
   `FilesChanged ... (replaced)`.
2. **Declare the target** in `targets.yaml`. It holds no secret:

   ```yaml
     legacy-exports:
       engine: files
       root: /var/lib/deltasleuth/files/legacy   # inside the container, its own real path: no trailing /
       settle_s: 60                              # a file modified more recently is not read yet
       rate_per_s: 5                             # per-key statements, each one pass over the files
       max_concurrency: 2
   ```

   A root must lie under `files.roots_under` and outside the evidence, workspace, spill, settings,
   targets and secret directories: otherwise no `ds` command starts (`root of target … must lie under
   …`). A mount that is missing is found when a run is created, not at start, so one absent export does
   not stop the service.
3. **Settings** (optional; the defaults fit an installation whose exports stay under a few GiB):

   ```yaml
   files:
     memory: 2GB          # DuckDB's buffers in each process that runs ds; size the container at this plus 1 GiB (twice this plus 1 GiB covers a discarded instance, below)
     spill: 20GB          # the most a process's sorts may write under spill_dir; the volume needs this plus 1 GiB free
     threads: 2
     # spill_dir: /var/lib/deltasleuth/evidence/spill   # the default: on the evidence volume
     # roots_under: /var/lib/deltasleuth/files
     max_files: 1000      # files a side reads at most
     plan_s: 300          # seconds a run's creation may spend planning its file sides (hashing, counting)
     os_limits: true      # false: the reader's process sets no data limit and no socket filter (Linux)
   ```

   They apply at restart. Creating a run reads every byte of a file side, inside `POST /v1/runs`,
   `POST /v1/compare`, `ds run` and a schedule's firing: set the proxy's read timeout above `plan_s`
   (`proxy_read_timeout 330s;` in the nginx block of section 5), or the proxy answers 504 before the
   run's creation has ended. `plan_s` is the creation's, whatever its file sides (two file sides share
   it), so that timeout holds for a file-to-file pair too. A request closed while its files are planned
   (by the caller, or by a proxy with a shorter timeout) does not stop the creation: the run is still
   created and started, so look at the runs before you send it again. At most 8 creations plan files at
   once in a process; one more waits its turn inside its own `plan_s`. `memory` is one DuckDB
   instance's. A process holds one, but for a while two after a discard ([FILES.md](FILES.md) D14).
   When a read hangs on a dead mount past its deadline, its statement is abandoned and the instance
   dropped, and a new one is built: the scans and plans in flight finish on the old one, which keeps
   its own `memory` until they end. When DuckDB meets an internal error, the instance is dropped and
   built anew too, but DuckDB has then invalidated the old one's database: the later statements of
   sessions still on it fail as `Dropped`, an outage, so their passes are paused and read again on the
   new instance, and a plan under way is refused as not ready (try again). A container sized at
   `memory` plus 1 GiB may be short for as long as a scan's sort on an old instance runs beside reads
   on the new one (sorts stay one at a time a process); twice `memory` plus 1 GiB covers one
   discarded instance at work beside the new one. A mount that hangs can discard up to four, each then
   kept by its hung read alone, which holds little. Not measured.
   **The reader is a process of its own** ([READER.md](READER.md)). The instance spoken of here lives
   in a child of each process that runs `ds` and reads a file side: `docker top` and `ps` show it
   beside its parent, as the same user, `python -I -X utf8 /app/deltasleuth/connectors/tables/reader.py`
   (idle it holds 17 threads, 186 MiB of data, `VmData`, and 60 to 91 MiB resident: seven starts in the
   image, aarch64, each steady from half a second after its start; in that first half second it shows
   about 150 MiB and 60 MiB). A `ds` command that reads a file side has a reader of its own for its
   length (`ds run`, `ds compare`, `ds target check`), and ends it as it exits: run with `docker compose
   exec` it leaves nothing behind, unless the command is killed (`SIGKILL`, or a `SIGTERM` it does not
   handle), when its reader ends by itself and stays a defunct entry of `docker top` until the
   container stops. DuckDB's memory
   is that child's: in the 10M-row scan below the worker's own process peaked at 0.2 GiB and the two
   together at 1.6 GiB. A fault or a memory blow-up of DuckDB's ends the child alone; another is
   started, the run pauses (`paused: Died`) and goes on. A "discarded instance" above is now a retired
   reader: it is killed, and its memory and spill given up, as soon as the last pass in flight on it
   has ended, at once where the hung statement was alone, and no longer waits for a restart.
   **Sizing:** `memory` plus 1 GiB for a `ds` process and its shared reader together, as before. As
   much again may stand for each further reader while it reads: one of a scan pass's own and one of a
   per-key read's or a plan's own, in which files that a reader ended on are read apart (two at most),
   and each retired reader until its passes end. **On Linux the reader limits itself** (unless
   `os_limits: false`): a data limit of `memory` plus 2 GiB plus 600 MiB a thread (5,296 MiB by
   default), no core, no socket, and the kernel's memory killer takes it before its parent; its start
   is logged with what held (`the file reader started: duckdb 1.5.5 in process 4242, data limit 5,296
   MiB, sockets denied`; where one did not, `… no data limit (not held on this host), sockets denied`),
   and `ds target check` prints the same. That line is a warning at every start of a reader, whatever
   held: `ds` configures nothing for the file reader's log, and a warning is the lowest level its
   processes then print (on their error output: `docker compose logs app`; a `ds` command prints it
   on its terminal).
   The data limit lies above a container sized as said, so in such a container the kernel acts first,
   and it takes the reader, not the service: observed in a container of 512 MiB, where the reader was
   killed (`SIGKILL`), its statement was `Died`, and the process that read went on and exited 0. Where
   a statement does meet the data limit, DuckDB does not refuse it: the reader's process ends
   (observed on aarch64 under a limit of 512 MiB: `SIGSEGV`), which is charged like any end (section
   11). Under the default limit a pass over 10M four-column rows a side, Parquet against Parquet, held
   2,579 MiB of data at its peak (2,229 MiB resident; one run, in the image, aarch64). `os_limits:
   false` sets neither the limit nor the filter: for a host on which they stop honest work, and for an
   amd64 image run under emulation on an arm64 host (Docker Desktop on Apple silicon), where a reader
   under its data limit does not start (observed: `the file reader did not start: its process ended as
   it started (SIGTRAP)`, and every file side is refused) and no filter is loaded. The acceptance
   checklist's item 25 (section 5) runs the reader's self-test on your host. **A Parquet file's footer is held
   outside `memory`**: 82 MiB for a footer of 7.7 MiB (100,000 column chunks, 1,000 row groups of 100
   columns), at most about 235 MiB for a footer the reader accepts, and about 2.5 footers a thread of
   `files.threads` at a time in a statement over many files (four at the default 2 threads, 9 at 4,
   19 at 8: about 0.6 GiB a thread at the limit; measured on one laptop; [FILES.md](FILES.md) §3 U31). Exports
   whose files hold a few hundred column chunks each cost nothing to speak of; for files of many
   thousands of row groups or columns add that to the container. Measured figures are in [TABLES.md](TABLES.md) §14.9 (single runs on one laptop,
   macOS, on 2026-10-04 with the reader in its own process, each test in a pytest process of its own on
   a machine that was not idle; the figures of 2026-10-03, with the reader inside the process, are kept
   beside them there): a 1.01 GiB CSV of 18.7M rows was planned in 22.1 s (its counting pass 20.3 s, on
   one thread; a file hashes at about 2 GiB a second), a 175 MiB Parquet file of 10M rows in 0.3 s.
   In the Linux image a counting pass over a CSV whose key is declared by `csv.keys`, as that one's
   is, took about six times a Mac's (point 5): size `plan_s` by a plan made on your host.
4. **Ask the export team for:** the format (Parquet where they can: a per-key read passes over the
   compared columns of every row group, far faster than over a CSV; the order of a file's rows does
   not matter to it, since no row group is skipped by its statistics); a Parquet file of at most
   10,000 row groups and 200,000 column chunks (row groups times columns: DuckDB's own writer fills a
   row group with 122,880 rows); for CSV the delimiter, quoting, line ends (`\n` or `\r\n`, not mixed) and
   encoding (utf-8, utf-16 or latin-1: Windows-1252 and EBCDIC are not read, convert them), with no
   header or trailer record of another shape than the rows; a stable path (the spec names it); files
   **written under another name and renamed into place**, and a small **stamp file written last**
   holding the export's time (`2026-09-30T23:59:00+00:00`), so that an export still being written is
   not read and one that did not arrive is seen. A file renamed into place is also hashed once and
   remembered for a retry; one written in place is hashed again by every plan.
5. **What a run costs.** A CSV file's rows are parsed on one thread. Measured on one laptop
   ([TABLES.md](TABLES.md) §14.9: single runs on 2026-10-04 with the reader in its own process, each test
   in a pytest process of its own on a machine that was not idle; macOS, so without the reader's Linux
   limits): a pass over a 10M-row, 532 MiB CSV took 10.4 s (about 51 MiB a second), and a per-key read
   of 200 keys 10.8 s; from a 10M-row Parquet file 0.3 s, sorted by the key or not. A per-key read is one pass, and a run is refused while one pass takes over half the spec's
   `execution.timeouts.total` (default 60 s): for a CSV side past about 1.5 GiB on that machine raise
   the timeout in the spec, or export Parquet.
   **Those CSV figures are a Mac's, and the image was several times slower where the CSV's key is
   declared by `csv.keys`.** With `csv.keys {userid: {as: integer}}`, as those tests declare it, the
   same 10M-row, 532 MiB CSV took 63.6 and 64.2 s a pass in the image built from this tree
   (linux/arm64, natively, default settings: about six times the 10.4 s above), and one side's sort
   took 145.7 s where the same Mac took 37.4 s; the plan of that side is refused there under the
   default timeout (`one pass over its files took 62.7 s …`), and by these figures so is a CSV side
   so keyed past about 250 MiB. The cause is DuckDB's Linux aarch64 wheel, which casts a key's text to
   the wide decimal that an integer or a decimal key is compared as about seven times slower than its
   macOS wheel (4.65 s against 0.64 s for 3M integers, 4.74 s against 0.64 s for 3M decimals); the
   reader's process and its limits change nothing of it, and a text key cost the same on both. An
   x86_64 host was not measured. **So measure a CSV export's plan in the image, on your host, before
   you size `files.plan_s`, the proxy's read timeout and a spec's `execution.timeouts.total`:** create
   one run of the spec there and read the side's `pass_s` in the run (`GET /v1/runs/{id}`:
   `params.tables.a.pass_s`), or the seconds the refusal names (section 11); `ds target check --count`
   times a pass that builds no key, and shows less. A Parquet key needs no such cast; of Parquet only
   one pass was timed in the image (10M rows a side, both sorts at once: 30 s, [TABLES.md](TABLES.md)
   §14.9), which is no figure of these tests.
   A scan sorts in DuckDB, one sort at a time a process,
   spilling under `spill_dir`: 10M rows a side spilled 176 MiB (Parquet against Parquet, compared in
   53 s) and 600 MiB (CSV against CSV, 183 s); a cross-engine scan of 10M four-column rows took 283 s
   and spilled nothing under the default `memory`, the worker's process and its reader peaking at 1.6
   GiB resident together (0.2 GiB the worker's own). Larger exports were not measured: the plan's
   `sort_bytes` projects the spill, and a scan that would pass `files.spill` is refused when the run is
   created. The projection is the sort as DuckDB 1.5.5 was measured to spill it ([FILES.md](FILES.md) §6
   step 8): three times the key's text (the sort holds it three times), the texts of the columns the
   worker hashes, 24 bytes for each value carried and 16 a row, plus 15%. It errs high: of 37 sorts
   measured by hand on one laptop, and not again since (2M to 20M rows under 128 MiB
   to 1 GiB of memory), none spilled more than 0.98
   of it before the 15%, and short or compressible texts spilled a third to a half (10M rows of a
   17-digit key and three short columns across engines are projected at 2.5 GiB and spilled about 1
   GiB under 256 MiB of memory). So a scan may be refused that would have fitted: raise `files.spill`,
   with the volume. The same figure is asked of the volume's free space before each sort. For large file scans run `ds serve --no-worker` beside `ds worker` (the MySQL and Oracle
   part above describes the layout): the worker's hashing of a cross-engine scan otherwise shares the
   interpreter with the API (the run's page took 130 ms at p95 with the worker inside `ds serve`,
   against 12 ms beside it).
6. **Schedules** ([SCHEDULES.md](SCHEDULES.md)). One run reads one export: for a recurring export use a
   schedule, not a soak (a soak's later cycle fails once the files change). Set the cron after the
   export's latest arrival and `start_within` over its variance: an export that is absent, still
   settling, stale (`file.maxAge`) or unstamped when its time comes is tried again every minute until
   the window closes, then recorded as missed with its code. A firing holds one of the worker's two
   firing tasks while its files are hashed and counted, so stagger large exports.
7. **Check before a run:** `ds target check legacy-exports --table 'customers/current/*.csv' --format
   csv` (the path quoted, so that the shell does not expand it) prints the files (their names, here
   only), and a draft `csv:` and `typeMap:` sniffed from the first file; `--count` reads them whole and
   lists the lines that are not rows; `--manifest` prints `sha256sum` lines and the digest a pack names
   (section 9).

**Table pre-checks and the schema guard** ([PRECHECKS.md](PRECHECKS.md)) need no new target, secret or
setting, and on every engine (a file side is refused: a table pre-check reads databases only):

- **No grant beyond a run's.** A pre-check of a stored spec reads the catalogue, the zero-row probe and
  aggregate statements of the tables the spec names, as the account a run reads them with. One of two
  schemas paired by name lists the tables and views of each schema the account may read (`Engine.relations`:
  on PostgreSQL and MySQL those of which it may select a column, on PostgreSQL in a schema it holds `USAGE`
  on and no partition, whose rows its partitioned table reads; on Oracle its own and those it may read by
  `SELECT ANY TABLE`, `READ ANY TABLE`, or `SELECT` or `READ` granted to it, to `PUBLIC` or to an enabled
  role). The schema guard reads the catalogue alone. No row, key, text or distinct value is fetched:
  counts, lengths and sums come back as aggregates, and so do each number and time column's minimum and
  maximum, protected columns included. The worker compares those minima and maxima, and the sums it does
  not show, in its memory and never stores, shows or exports them (PRECHECKS §6).
- **What each level costs a side** (each statement bounded by `precheck.statement_timeout_s`, 900 s by
  default; one past it has no figures and the pair reads `not read`): `schema`, the catalogue statement
  and the probe; `counts`, one statement over the filtered population; `columns`, the same statement over
  up to 150 columns at a time, one pass in constant memory; `distinct`, then the repeated keys and the
  distinct values 4 columns a statement, which sort or hash. Measured once on an Apple M3 Max with both
  databases on one stock PostgreSQL 16 in Docker (PRECHECKS §12): 10M rows a side, 6 columns, level
  `columns` in 1.7 s; level `distinct` in 18.6 s, the server holding up to 1,940 MiB of temporary files
  at once for both sides' sorts (3,660 MiB written in all).
- **Temporary space.** A side over `precheck.distinct_max_rows` rows (20,000,000 by default) has its
  repeated keys and distinct values not counted, and says so (`duplicate keys and distinct values not
  counted: …`). A sort the server refuses (PostgreSQL
  53400 under `temp_file_limit`, MySQL's full temporary directory, ORA-01652) reads `ran out of temporary
  space` and keeps what was read before it: the pre-check fails no run.
- **One more session a side.** The worker reads a target's pre-check pairs on at most
  `precheck.sessions_per_target` sessions at once (1 by default, at most 4), a pair of one target its two
  sides in turn on one; listing two schemas opens one short session a side in the API's request (30 s a
  statement). Add them to `max_user_connections` and `SESSIONS_PER_USER`. A refused credential is never
  retried: it settles every pending pair of that target at once.
- **A replica for large tables.** Point a target at a replica or a standby, or pre-check a filtered view,
  when a full count of the primary is not wanted during business hours. The two sides are read seconds
  apart, never at one moment.
- **Figures under a filter can be narrowed to one row** (PRECHECKS §6, §16). In tokens and developer mode,
  and for an approved version under SSO, whoever pre-checks a spec with a filter learns, of the rows it
  selects: how many there are (a filter of `id = 7` and a salary of at least 50,000 holds 1 row or none: a
  yes or no about any column it names); how many hold NULL in each compared column, protected and tokenised
  ones included, though the filter names only the key; and, of every other figure, whether the two sides
  agree. Filters that select one row a side (`id = 7` on side a, `id = 8` on side b of one table) so tell
  whether each row's value is NULL, of a number, a moment or a boolean whether the two values are equal,
  and of a text or bytes whether they are as long and whether both or neither is empty. A protected
  column's sum, `true`, `empty` and `distinct` counts and lengths are only ever verdicts under a filter. A
  run of the same spec reads and counts the same rows but compares key with key and shows none of this
  about a protected column. Under SSO an unapproved version with a filter is read at level `schema` only.
  A `regex` key map is no filter and needs no approval of its own, in any mode: its pattern, a predicate
  over raw key values, reaches the target in runs and pre-checks alike, and the rows whose key it does not
  match are counted (a pre-check's null keys, a run's skipped rows). Relation and column names are stored
  and exported.
- **The schema guard** adds one catalogue statement a side to each scan pass, on the pass's own
  sessions, and a short session a side at a key-list cycle's start, a resume and a refused read (30 s a
  statement). Measured on two PostgreSQL tables of 200 columns (PRECHECKS §12): 28 to 81 ms at a scan
  pass, 67 to 71 ms at a cycle's start.

Write a table spec by hand ([runbook](RUNBOOK.md#compare-two-tables)): the profiler drafts service
specs only. `examples/tables/` is a demo pair of databases, with seed SQL and a spec, and
`examples/graph/` a pair of link-fact tables for graph parity.

Then run `docker compose restart app`.

**2. Draft a spec from samples.** On the Specs screen, upload at least 20 pairs of responses for the
same keys to draft a spec (`POST /v1/profile`). The file is either a JSON array of `{key, a, b}`, or
JSON Lines records `{key, side, status, body}` as the runbook describes them. A side whose status is
not 2xx has no document for its key. For a mock pair, `python -m mocks.fixtures <pair> --pairs FILE`
writes the array (the runbook's "Try it on the mock pairs"). Under the form the screen shows the
draft's notes, each side's unmatched paths and the evidence. For request templates and a key pattern,
use `ds profile` (section 9). The profiler proposes:

- the `select` and `map` onto one canonical shape;
- array modes;
- normalisers;
- `notFound`.

**3. Review the draft.** It is only a draft: every suggestion comes with its evidence. The public
examples show a reviewed spec on real APIs:

- one canonical shape;
- links rewritten to ids;
- arrays as sets;
- `notFound` per side;
- `knownDiffs`, each with a reason and a ticket for a difference you accept.

**4. Upload the spec.** Upload validates it and returns 422 with JSON pointers for any problem.

**5. Start small, then grow:**

1. Compare one key: "compare now" on the Key screen, or `POST /v1/compare`. A key of a run is
   compared under that run's spec only.
2. Run a batch.
3. Run a soak over several cycles. The Run screen lists the keys whose outcome changed between
   cycles.

## 8. Point it at the model gateway

The chat sends the run's masked findings, which are tokenised production data
(`production_tokenised`), and the analyst's questions as typed. Once a labelling of the run's newest
ended cycle is labelled, the run's summary (`get_run_summary`) also carries its labelled accuracy as
stored ([ACCURACY.md](ACCURACY.md) §10): the counts, shares and intervals, the truth source's identifier
and date, its first 10 root causes and 10 rules by their ids, and one line with the labelling's, the
run's and the attesting sign-off's ids that says what kind of actor labelled it (`by a person`, `through
a token`, `in developer mode`). No staff name, user id or token id is sent, and no key or note. A profile
can serve the chat only if all of these hold:

- its host is listed as `internal`;
- it lists the `production_tokenised` data class;
- it lists the environment that `llm_environment` names (`prod` in the example).

1. **Declare the gateway in `deploy/config/llm_profiles.yaml`.**
   - Under `hosts`, list its host name as `internal`, and no other host.
   - In the `gateway` profile, set:
     - `base_url`: for OpenAI-compatible gateways it usually ends in `/v1`;
     - `model`;
     - `capabilities`: `tool_calling: true`, which the chat needs; `prompt_caching` (`automatic`
       or `explicit`) if the gateway reports cached tokens; optionally `max_context`, and
       `max_output`, the cap on a reply (2048 tokens by default; raise it for a reasoning model,
       whose reasoning counts against it);
     - `auth_header` and `auth_scheme`, only if the gateway takes the key in another header (the
       example shows them commented out).
2. **Mount the gateway's key and switch the chat on.** Write the key to its file. In the site file,
   name it in `app.secrets` (`llm_key`, beside `oidc_client_secret`) and remove the `# ` of its line
   under `secrets:` (and of the `secrets:` line). In `deltasleuth.yaml`, remove the `# ` of the three
   `llm_` lines, which are off as copied:

   ```sh
   printf '%s' 'YOUR-GATEWAY-KEY' > deploy/secrets/llm_key
   chmod 444 deploy/secrets/llm_key
   ```
3. **Probe the profile.** It sends only small synthetic requests. After a baseline request, it
   checks a tool round trip (`tool_calling`) and cached-token usage (`prompt_caching`), and prints
   pass, fail or unsupported for each:

   ```sh
   docker compose run --rm app ds llm-probe --profile gateway
   ```

   The probe is an onboarding check. It does not change what the chat uses: remove from the
   profile any capability it does not report as pass, then probe again.
4. **Start it and try the chat.** Run `docker compose up -d`, which mounts the key (a later change to
   `llm_profiles.yaml` alone needs only `docker compose restart app`), then open a run and use Chat.
   - Answers cite findings, root causes and keys (`[F-…]`, `[RC-…]`, `[K:…]`).
   - An answer that cites no tool result, cites an id no tool result returned, or states a number
     no tool result shows is repaired once, then withheld. A reply cut off, filtered or refused is
     not an answer.
   - The profile's `limits` (`max_concurrency`, `rps`) hold across all chat questions together.
   - AI text appears in the PDF only in an annex labelled as AI-generated.

**This policy is the built-in chat's.** What a profile may receive, by environment and data class,
governs the calls DeltaSleuth makes to a model. A tool result that a team's own agent reads over MCP
goes to that agent's model host, which DeltaSleuth does not call and this policy does not reach: what
such a result may carry is `mcp.data_classes` (section 3, The MCP server).

**The `claude` and `openai` profiles** point at external hosts. By default they are limited to
`dev`, `test` and `eval`, and to synthetic, golden and schema data, so they only qualify a key or a
model with the probe:

1. Add the key as a secret (section 3).
2. Run `docker compose run --rm app ds llm-probe --profile claude --environment eval`.

**Testing the chat with Claude or OpenAI.** The chat's findings are masked (keys and protected
values are tokens), but they still describe production data, so an external host receives them
only after three deliberate steps, and never in `prod`:

1. In `deltasleuth.yaml`, approve the class for external hosts and pick a test environment:
   `llm_external_data_classes: [production_tokenised]`, `llm_environment: test` (or `dev` or
   `eval`), and `llm_profile: claude` (or `openai`).
2. In that profile in `llm_profiles.yaml`, add `production_tokenised` to `data_classes` and the
   environment to `environments`.
3. Run `docker compose up -d`, then use Chat.

Get your organisation's approval before step 1. In `prod`, only an internal host (your model
gateway) serves the chat.

**AI off** is how the examples start: the three `llm_` lines of `deltasleuth.yaml` are commented, and
the site example mounts no `llm_key`. To switch it off again, put the `# ` back before the three lines,
take `llm_key` out of the site file's `app.secrets` and put the `# ` back before its line under
`secrets:`, then run `docker compose up -d`. Chat then returns 503 "AI disabled" and everything else
works.

## 9. The command line: P0 tools, file-mode runs and administration

The API and the UI cover runs, specs, approvals and chat. The [runbook](RUNBOOK.md)'s other commands run in a
throwaway `app` container, with the app's settings, secrets and volumes. The `p0` volume at
`/var/lib/deltasleuth/p0` carries files in and out, and the running app sees the same files:

```sh
alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
P0=/var/lib/deltasleuth/p0
docker compose cp samples app:$P0/samples             # captured pairs (runbook, route A)
docker compose cp spec.yaml app:$P0/spec.yaml
ds profile --samples $P0/samples --out $P0/profile --name my-parity \
    --target-a legacy-api --target-b new-api --key-name customer_id --targets /etc/deltasleuth/targets.yaml
ds run $P0/spec.yaml --samples $P0/samples           # file mode
ds measure R-... --out $P0/R-...
ds adjudicate R-... --out $P0/R-...
docker compose cp app:$P0/R-.../adjudication.csv ~/  # to the reviewers
docker compose cp ~/adjudication.csv app:$P0/R-.../  # back, filled in
ds score --out $P0/R-...
ds report R-... --out $P0/R-... --kind summary --kind report.pdf
docker compose cp app:$P0/R-... ~/                   # the P0 report and its evidence
```

Files keep their mode when copied in, and the app reads them as another user: run
`chmod -R a+rX samples spec.yaml ~/adjudication.csv` before copying them in.

`ds` reads every path inside its container, never in the folder the command was typed in. A path that
is not there is refused with where the container read it: `error: / [document_unreadable] cannot read
my-specs/customer.yaml: No such file or directory (ds runs in a container, where this path is
/app/my-specs/customer.yaml: a file or folder of the host must first be copied in, with docker compose
cp, or mounted)`. A `--keys` file (`error: No such file or directory: <path> (ds runs …)`) and a
`--samples` directory (`error: sample root '<path>' does not exist (ds runs …)`) say the same. Outside
a container the three refusals end before the bracket. A build of a checkout sees its `mocks/` and
`examples/` at their repository paths (section 2), so `ds run mocks/customer/spec.yaml` needs no copy
there.

One worker per database processes keys, the one inside `ds serve` (or a `ds worker` beside
`ds serve --no-worker`). While the app runs, `ds run`
leaves the run to it, logs that another worker is processing keys, and waits for the run to end.
Samples therefore go in the `p0` volume, where both containers see them.

**Who the CLI acts as.** The CLI reaches the database directly, so it stands outside the role model:
it acts as `local:<DS_OPERATOR>@deltasleuth-app` with every role. With the `auth` settings, each
command that writes audit rows exits 2 without `DS_OPERATOR`, which the alias sets to your host
account. Log in to the host with your own account, never a shared one. The name is asserted, not
verified: its rows carry `asserted` and the container's OS account, and the host's audit of Docker
use (section 10) is the authoritative record of who ran a command. Each command writes a copy of its
audit rows to stderr, so its stdout stays its output.

**Administration.** In oidc mode the Admin screen manages tokens, users and sessions; these commands
are for recovery. In tokens mode they are the only way:

```sh
ds token create --name ci-nightly --owner "Payments QA" --role operator   # prints only the token
ds token list
ds token revoke T-...
ds users                                             # people, with the roles of their last sign-in
ds users disable U-... --revoke-tokens               # a leaver: sessions and tokens end now
ds session revoke --all                              # or --session W-..., --user U-...
ds audit --actor user:U-... --since 2026-09-01       # --json for the rows
ds purge --dry-run                                   # what the next purge deletes (runbook, Retention)
ds purge hold R-... --reason "legal hold LH-7"       # a case number, never personal data: it reaches the SIEM
ds keys status                                       # the KEKs in use, and any the keyring lacks
ds approvals --needed                                # live runs' spec versions that lack an approval
ds approvals revoke A-... --reason "wrong clear path"   # break-glass; people revoke on the Approvals screen
ds licence                                           # that this build needs no licence: state open, what it allows
ds licence --json                                    # the same as GET /v1/licence's body, with the audit head
ds licence withdraw LI-... --reason "wrong tier"     # a vendor build's: only before its first day (section 3, The licence)
ds notify                                            # notifications: channels, counts, mutes, banners
ds notify deliveries --channel ops-hook --state dead # what did not go out, with its closed codes
ds notify retry ops-hook                             # its deliveries that died in the last 72 hours, again
ds notify mute --channel ops-hook --hours 4 --reason "receiver maintenance CHG-12"   # audited; ends early with ds notify unmute M-...
ds notify explain R-... --cycle 12                   # a cycle's digest and each rule's verdict
ds target check crm-legacy --table APP.CUSTOMERS     # a database target's pre-flight; opens no store, reads no row
```

Under the alias, `ds token create` prints the token on its container's stdout, which Docker's logging
driver records as it does every container's: the default `json-file` log goes with the `--rm`
container, but a driver that keeps such containers' lines (journald, section 10) keeps the token in
the host's log and in whatever that log feeds, and the command has no form that hands the token over
another way.

This build refuses `ds licence install` and `ds licence show` (exit 2: it needs no licence and can
verify none), prints no licence warning and refuses no run for a licence. In a vendor build `ds licence
install` reads the file from stdin, so it needs `-T` (section 3, The licence); `ds serve`, `ds worker`,
`ds run` and `ds compare` print each licence warning as `warning: …` on stderr; and a run the licence
refuses exits 2 with its reason and writes no run. The spec file it names is stored first (a new
version, audited `spec.create`), as an upload, which always works.

**`ds target check ID [--table [SCHEMA.]NAME]`** is the pre-flight of a database target, PostgreSQL's
too: it resolves and vets the address, opens a session as a run does (TLS, the session's settings) and
closes it. It opens no store, reads no row and writes no audit row. It alone prints the server's full
version; a plan, a pack and the API carry the major and minor version only. What a server says of
itself (its version, its character set, the names and types of a table) is printed without control
characters, the version and the character set cut at 64 characters.

```
target crm-legacy: oracle 19.21.0.0.0, character set WE8MSWIN1252, TLS yes, session ok
note: this release is observed on Oracle 23.26 and MySQL 8.4 only
note: a run takes text keys of up to 500 bytes in this character set
APP.CUSTOMERS (table, consistency snapshot)
  CUSTOMER_ID  NUMBER(10)          integer    key
  EMAIL        VARCHAR2(120 CHAR)  text       key
  OPENED       DATE                timestamp  key
  GEOM         SDO_GEOMETRY        -          not comparable
```

Each column shows its declared type, its class ([TABLES.md](TABLES.md) §13.4) and `key` when it can be a
key in a pair with a MySQL or Oracle side (between two PostgreSQL tables any column can), `not
comparable` for a type no plan reads, or `not readable` for a column the account is not granted. It
exits 2 with the line a run's creation would refuse with, `target <id>` in place of `side a` (section
11).

**Of a files target** ([FILES.md](FILES.md) §16), `ds target check ID` checks where the root lies, that
it is mounted and read-only, and that the file reader starts; `--table PATH --format csv|parquet` then
reads the files the path names, as a spec's `table.file.path` would name them. It opens no store and
writes no audit row, and **it alone prints file names**: plans, packs, alerts, audit rows and logs name a
file by its position.

```
target legacy-exports: files, read-only, duckdb 1.5.5, parquet and core_functions built in, no extension loads, read in a process of its own (data limit 5,296 MiB, sockets denied)
customers/current/*.csv (csv): 12 file(s), 3,650,722,201 bytes, newest modified 2026-10-01 01:58 UTC, settled
  1  customers/current/customers-0001.csv  304,226,850  2026-10-01 01:41 UTC
  ...
draft (sniffed from the first 20,480 rows of file 1): a run reads only what the approved spec declares
  csv: {delimiter: "|", newline: "\r\n", header: true, columns: [customer_id, email, balance, opened]}
  typeMap: {balance: {as: decimal}, opened: {as: date, format: "%d/%m/%Y"}}
note: customer_id holds only digits and 412 sampled values start with 0: left as text
```

The draft is a starting point for the spec's `csv:` member, never what a run reads: a run reads only
the dialect the approved spec declares. Without `--header` the fields are named `column 1`, `column 2`,
… (a sniffer may take a first row for a header), so no record's text is printed; with it the names come
from the first record. `--delimiter C` replaces the sniffed delimiter. A line of another shape after the
rows (a trailer record) is left out of the draft, which then says `note: drafted from the lines of file 1
that read as rows: --count lists the others`. A column of digits whose values
start with `0` stays text in the draft: declare `csv.keys {customer_id: {as: integer}}` if `00123` and
`123` are one key. For Parquet each column's type and class is printed in place of a draft. `--count`
reads the files whole by the drafted dialect: the rows, a pass's seconds and each line that is not a
row, by position and kind (`file 2 line 81223: columns`). `--manifest` hashes the files and prints
`<sha256>  <path>` lines and `files_sha256`: `sha256sum -c` in the root checks the lines, and `cut
-c1-64 | sha256sum` of them gives `files_sha256`, the digest a run's plan and its evidence pack name, so
the export's owner can confirm which bytes a pack is about. A side that declares `partitions` compares
its directories' names, so its digest is `sha256sum` of the lines whole, which the check prints as a
second `files_sha256` line. The digest is that of the list, never a file's own, of a side of one file
neither: `sha256sum` of that one file gives another value, and the pack's certificate and subject say
how its digest is formed ([runbook](RUNBOOK.md#from-files), step 9, has the commands).

**`ds serve --no-worker`** serves the API and the UI without the run worker: a `ds worker` beside it
then processes the keys (section 7, what a scan costs). The API process keeps its own budget and
sessions for one-key compares, key listings and run creation, beside the worker's: a target's
`rate_per_s` and `max_concurrency` hold in each process, not across the two (section 7).
`ds worker` stops on SIGTERM and on Ctrl-C, its
scans' statements cancelled and its leases handed back, and exits 0 and 130; `ds run` treats a SIGTERM
as Ctrl-C.

**`ds mcp`** serves the MCP tools over its own stdin and stdout to the agent that starts it
([runbook](RUNBOOK.md#your-own-agent-mcp)). It is for a developer's workstation, in developer mode.
With the `auth` settings, as on this host, it is the host-administrator channel like every command
here, and refuses to serve without `--host-channel` (`error: ds mcp under auth is the
host-administrator channel: use /v1/mcp with a named token, or pass --host-channel (docs/mvp/MCP.md
§5.2)`). With the flag it needs `DS_OPERATOR`, warns that its caller is asserted and not signed in,
and serves until its input closes; `--role viewer|operator` (default `viewer`) shapes the tools it
offers and is no control, since whoever runs it holds this host's `docker compose`. Its stdout carries
only the protocol's frames; its log lines and its copy of the audit rows go to stderr, which the
launching client captures and must read: every other logger's record is its name and level alone
there (`psycopg.pool WARNING`), so the reason of a database fault is not on that stream. Under this
wiring it is its container's first process, for which the kernel ignores a signal left at its
default: a SIGTERM after the first, or one that comes once its serving has ended, does nothing there
(observed with a stand-in for `ds`: [MCP.md](MCP.md) §5.2). **Docker's logging driver records both
streams of that container too:** with
the default `json-file` driver they are deleted with the `run --rm` container, and with a driver that
keeps such containers' lines (journald, section 10) the host's log, and the SIEM it feeds, holds the
audit copy and the protocol's frames, tool results included. Production agents call `POST /v1/mcp`
from their own machine with a named token instead. `ds mcp check` reads the settings alone (section 3).

The CLI can neither request nor grant an approval ([runbook](RUNBOOK.md#four-eyes-approvals-and-sign-off)).
In tokens mode, which has no admin session, notification mutes are the CLI's ([runbook](RUNBOOK.md#notifications)).

Purges, holds, erasures and keys have no API or screen: they are the host administrators' (the
[runbook](RUNBOOK.md#retention-purge-and-keys)).

## 10. Operations

| Task | How |
|---|---|
| Logs | `docker compose logs -f app` |
| Upgrade | A signed release: "Upgrading from a release bundle", below. A source build: `git pull && docker compose up -d --build`, with the source override in `.env` (section 2, Build from source). Migrations apply at start. An installation without `.env` moves to the site file first ("Moving from a source checkout", below). Every upgrade ends with the P0 cron on the line of "Volumes, logs and P0 files" below, without `-f` and with `--cap-add DAC_OVERRIDE`, checked once by hand ("Moving from a source checkout", step 6). With a private CA, rebuild the CA layer before `up` (section 11, TLS). From a release before encryption at rest, follow "Upgrading to encryption at rest" below instead. From a release before four-eyes approvals, stop the app and follow the runbook's [upgrade steps](RUNBOOK.md#four-eyes-approvals-and-sign-off) first. Migration 009 (evidence packs) hash-chains every existing audit row as it applies, audit inserts waiting meanwhile ([PACK.md](PACK.md) §10); runs created before it, and sign-offs requested before it, get no pack. From a release before licence keys, follow "Upgrading to licence keys" below instead. Migration 011 (sign-off sampling) changes only the catalogue of `ds.run_keys` (new columns with constant defaults, checks `NOT VALID`): it holds that table's lock about a millisecond, whatever its size, and backfills nothing ([SAMPLING.md](SAMPLING.md) §12). Migration 014 (cut-over readiness) adds one nullable column and seven partial indexes to `ds.cycles` (one row per ended cycle) and marks the cycles of one-shot and captured-sample runs; it rewrites no table and touches no result. After it the worker measures the cycles ended before the upgrade, newest first, 10 full measures every 10 s (20,000 unpurged cycles in under six hours), and writes no `readiness.changed` until they are done, one whose measure failed included (tried again after 1, 2, 4 and 8 hours, then left `unknown`); meanwhile the Readiness screen says how many are left, counted up to 100,000 ([READINESS.md](READINESS.md) §4, §8, §9 Y15). Migration 015 (schedules) reads every row of `ds.runs` once to build an index; run writes wait meanwhile, reads go on ([SCHEDULES.md](SCHEDULES.md) §12). Migration 016 adds one nullable column to ds.runs: no rewrite, no scan ([PRECHECKS.md](PRECHECKS.md) §8); its two new tables start empty. After this upgrade the schema guard can pause running soaks and scheduled runs: a rule that routes run.paused is told (reason `run_paused_schema_drift`; routing `schema.drift` adds its warnings). A run from before 016 is adopted at its first look, which records the shapes it sees then and judges only missing tables and columns. |
| Licence | Nothing to do: this build needs no licence. `ds licence`, or the Licence screen, says so (`state open`), and the worker writes one `licence.state` line, `to: open`, in its first hour. In a vendor build they show the state, what it allows and the counted pairs: alert there on the SIEM's `licence.state` lines (below), which the worker writes when the state changes, 30 days before the end (`ending`), at the end (`grace`) and 30 days later (`expired`), and install each renewal when it arrives (section 3, The licence). |
| Readiness | The Readiness screen, or `ds readiness`: every pair's gates and state, from measures the worker stores as each cycle ends. `ds readiness check` shows the gates as configured; `ds readiness verify` checks each cycle's stored measures against its `readiness.measured` audit row, and each such row against its cycle (exit 2 on measures edited, removed, replaced or measured again, and on measures no row names; not on what was changed before a cycle was measured), and belongs beside `ds audit verify` in the periodic checks. `ds readiness --fail-unless parity_ready --programme <name>` exits 1 while a pair of the programme is below that state, for a pipeline. A stopped worker measures and records nothing: the screens still judge on read, and each pair's `fresh` gate turns red once its evidence cycle is older than the gate allows ([runbook](RUNBOOK.md#cut-over-readiness)). |
| Table pre-checks | The Pre-checks screen, or `ds precheck`: each pre-check's progress and counts by verdict, worst first. Only a worker reads their pairs (`ds serve`, or `ds worker`; `ds precheck run` reads them beside itself unless `--no-wait`), at most 20 unfinished pre-checks at once; a banner names one no worker has taken for 10 minutes or more. They are deleted `retention.standard_days` after they were made, by the hourly purge pass, and are no run, no evidence and in no pack. For a nightly check, the site's scheduler runs `ds precheck run --all-specs --fail-on incomplete` (exit 1 when a pair's verdict is `incomplete` or worse, anything but `schema differs` and `no difference observed`, or the pre-check was cancelled) ([runbook](RUNBOOK.md#triage-with-table-pre-checks)). |
| Schedules | The Schedules screen, or `ds schedules`: each schedule's next due time and newest firing, under the banners. Only a worker fires, and a missed time is reported when a worker next sweeps, so check from outside that one does: a SIEM absence rule (no `schedule.fire` or `schedule.missed` line for `SC-…` within its period), or a monitor polling `GET /v1/schedules?limit=1` with a viewer token for `overdue` (schedules over 10 minutes late and not settled), alarmed once it stands longer than the largest `start_within` (a worker legitimately retrying shows there too). `GET /v1/health` does not cover it, and the app container's healthcheck covers the `ds serve` process, not a separate `ds worker`. Before `ds worker` or `ds serve` starts on a restored or copied database, run `ds schedules pause --all` (Backups and restore, below). After moving to SSO, save each schedule again, signed in; when the licence lapses or the auth mode is about to change, pause the schedules rather than let each due time be recorded as missed ([runbook](RUNBOOK.md#schedules)). |
| The MCP server | `ds mcp check` shows the `mcp` block as judged and what each role is offered. Each tool call leaves one line on the app's stderr (`tool=… actor=… run=… bytes=… ms=… error=0\|1 request_id=…`: never an argument or a result), and the audit log holds an agent's key views and compares and, once an hour per token, tool and run, its other reads of a run (`mcp.read`). What the same token reads on another `/v1` route is not an MCP call: it leaves that route's rows, no `mcp.read` and no such line. With `mcp.http` the app's database pool may open `mcp.concurrency` more connections (4 by default), and each `ds mcp` process holds one idle connection and at most `mcp.concurrency` + 1: count them against the `db` service's `max_connections`. A read that its client cancels, or leaves by disconnecting, has its statement cancelled in the database too, and the pool replaces its connection: until PostgreSQL has ended the cancelled statement its backend stands beside the new connection, so the count can pass that bound for a moment, and a cancel request that does not get through within 5 s leaves the statement running until it ends ([MCP.md](MCP.md) §10). Rate limits and the two audit throttles are per process and forgotten at restart. To stop one agent, revoke its token (`ds token revoke T-…`, or Admin, Tokens): its next request gets 401. To stop MCP, set `http: false` or remove the block and restart the app ([runbook](RUNBOOK.md#your-own-agent-mcp)). |
| Retention | The worker purges expired cycles hourly, from 7 days after migration 006 first applied (time to set holds after an upgrade): `ds purge --dry-run` shows what is due; holds, erasures and on-demand purges are in the [runbook](RUNBOOK.md#retention-purge-and-keys). |
| Keys | `ds keys status` lists each KEK with the data keys it wraps, and any the keyring lacks. Rotate the keyring yearly, or at the site's cryptoperiod ([runbook](RUNBOOK.md#rotating-the-keyring)). |
| Throughput | `worker.concurrency` in `deltasleuth.yaml`: keys in flight at once. Each target's `rate_per_s` and `max_concurrency` still cap the load on it. Between two tables with 1% of keys differing, one worker read 627 (558–696) and 670 (546–794) differing keys a second at 1M rows a side (both flagged noisy) and 749 (747–752) and 752 (752–753) at 10M (warm medians and ranges, every 100th key and 10 blocks), and 454 (443–466) for 24-column rows at 1M, on an Apple M3 Max with both databases and the store on one stock PostgreSQL 16 in Docker (synthetic 4-column rows, warm cache, `worker.concurrency: 4`, `poll_seconds: 0.05`, `execution.concurrency: 8`, budgets lifted; [BENCHMARK.md](BENCHMARK.md) Results): at those rates a million differing keys take about 22 to 27 minutes of reads (an extrapolation, not measured). |
| Sign-off sampling | A draw reads its frame (a table side in one snapshot, a key listing a page at a time) and, when the frame holds a listing or two sources, deduplicates it in a temporary table of its own database session, about 89 bytes a key and as much again for each key both sources give (about 3.4 GB for a union of 20M keys nearly all in both). `sampling.temp_buffers` in `deltasleuth.yaml` (default `2GB`, PostgreSQL's `temp_buffers` for that session only, taken as the table grows) keeps it in the database's memory; beyond it the table spills to temporary files on the database volume. Leave the `db` container that memory, and if you set PostgreSQL's `temp_file_limit`, keep it above the frame table. A 20M-key union draw takes about 90 minutes. `ds samples` lists every draw, or a spec name's ledger, with whether it blocks a later sign-off; closes and sign-offs are made on the Run and Approvals screens ([runbook](RUNBOOK.md#sign-off-sampling), [SAMPLING.md](SAMPLING.md) §3, §16). |
| Audit | `ds.audit_log` records who did what: a person (`user:U-…`), a named token (`token:T-…`), the CLI's asserted operator (`local:…@deltasleuth-app`) or DeltaSleuth itself (`system:…`), each with its name. Admins read it on Admin, Audit; host administrators with `ds audit`. Every row is hash-chained (`ds audit verify`, below). The app connects as the database owner, so the log is append-only against mistakes, not against a compromised app host: the SIEM copy (below) is the independent record. Once a separate migration DSN exists, run the app as a role that may only INSERT and SELECT the audit log. |

### The SIEM copy and host auditing

**Ship the app's stdout to your SIEM, ingested as it is written.** For each audit row, once it
commits, the app writes one JSON line to stdout: `{id, at, actor, actor_name, action, object, detail,
request_id, session_id, client_addr, chain}`, `chain` being the row's `{seq, hash}` in the audit hash
chain ([PACK.md](PACK.md) §4). A row changed later in the database no longer matches its line. The
SIEM is an independent record only if it ingests each line when it is written, with its own
timestamps: the database owner can rewrite the log and its chain consistently, and only the SIEM
shows that (`ds audit verify --siem`, and step 7 of a pack's verification). SIEM retention may be
shorter than a pack's life: archive each `pack.issue` line, or a ticket quoting it, with the pack.
**Alert on `readiness.changed`** lines too (`{pair, from, to, regressed, causes, …}`, by `system:worker`,
within about 30 seconds of what caused them, 15 minutes for a gate that only time changes) unless a
`notify` rule routes them: a pair whose `to` is below its `from`, or whose `regressed` turned true, lost
ready evidence.
**Alert on `schedule.missed`** lines (`{reason, code, due, to, count, …}` on object `SC-…`, by
`system:worker`, or by the person whose save, pause or delete left a time unstarted) unless a `notify`
rule routes them, on a `schedule.pause` line by `system:worker` (`reason: zone`: a schedule whose zone or
cron its build could not load; with `missed: false` no time of it was due, so it has no `schedule.missed`
line, and a `notify` rule for `schedule.missed` sends that pause as its alert), and on the absence of any
`schedule.fire` or `schedule.missed` line for a schedule
within its period: a worker that is down writes neither ([SCHEDULES.md](SCHEDULES.md) §16).
**In a vendor build, alert on `licence.state`** lines (`{from, to, licence_id, ends}`, by
`system:worker`, looked at hourly): `to: ending` 30 days before the licence ends, `grace` after it,
`expired` when new runs and packs stop. This build writes one such line, `to: open` (`from: evaluation`
on a database an older build that enforced the licence ran), and no other while it runs: nothing to
alert on.
**Alert on `notify.dead`** (a delivery that will not be sent, at most one line per channel an hour),
**`notify.skipped`** (audit rows never notified: `stale` after an outage beyond `max_age_hours`, or
`unsafe` and `error` rows) and **`notify.config`** (the channels or rules changed, or notifications were
removed): the chain and the SIEM, not the deliveries, are the record ([ALERTS.md](ALERTS.md) §12). CLI containers write
their copy to stderr. Point Docker's logging driver at your log agent in `/etc/docker/daemon.json`
(for example `{"log-driver": "journald"}`), restart Docker, then run `docker compose up -d
--force-recreate`. That driver also keeps the lines of `docker compose run --rm` containers, which
the default `json-file` driver deletes with the container.

**Audit Docker use on the host.** Anyone who can use Docker here is root on the host and can run any
`ds` command under any `DS_OPERATOR`. Give the `docker` group only to host administrators, each with
their own account. The host's record of `docker compose run` and `exec` is the authoritative record
of who ran a CLI command. Keep it with an auditd rule, for example in
`/etc/audit/rules.d/docker.rules` (use the paths of your installation), then run
`sudo augenrules --load`:

```
-w /usr/bin/docker -p x -k docker
-w /var/run/docker.sock -k docker
```

Or allow Docker only through `sudo`, whose log names the person. The host's audit is also what shows
that nobody emitted forged SIEM lines through an ad-hoc `docker exec`, or signed a pack with the key
file outside the app: a pack whose `pack.issue` line the SIEM did not ingest at issue time is suspect.

**Check the audit chain daily.** `ds audit verify` checks every row against its chain entry, in one
snapshot, and prints the head (`head: row 81234 <hash>`): file that line in a daily ticket, an anchor
later checks start from. With `--siem FILE`, an export of the SIEM's `deltasleuth.audit` lines, it
also compares the rows both ways. A break exits 2 naming the first row: see the
[runbook](RUNBOOK.md#evidence-packs).

```sh
docker compose run --rm app ds audit verify
docker compose cp siem-export.jsonl app:/var/lib/deltasleuth/p0/siem.jsonl
docker compose run --rm app ds audit verify --siem /var/lib/deltasleuth/p0/siem.jsonl
```

**Acceptance: verify a demo pack away from the host.** Before the first real pack, on a signed-off
demo cycle (a batch run over a table pair or a complete key listing, drawn, scored, signed off and
issued on the Run screen): download the pack (Run screen) and the revocation list
(`GET /v1/packs/revocations`, or `ds packs revocations --out DIR`; the Run screen has no control for
it) to a workstation that is not the host, put the register's
`FILED-PROCEDURE.txt` beside the unzipped folder and run it step by step there with `sh`, in the folder:
step 0 prints nothing, step 1 prints the register's fingerprint, step 2 `Verified OK`, steps 3 and 6
nothing, step 5 `decided` twice and a `1` per line, step 7 nothing against the SIEM's export, and step 8
`0` for the pack. Then edit one byte of a listed file and see step 3 fail. `ds packs verify FILE.zip
--fingerprint <register's> --revocations DIR` runs the same checks where DeltaSleuth is installed; it
needs no settings.

### Volumes, logs and P0 files

**Encrypted volumes.** DeltaSleuth encrypts the sealed columns and the evidence blobs, but not what
else the host writes: PostgreSQL's WAL, dead row versions and freed blocks, the P0 files and
temporary files. Put Docker's data root (`/var/lib/docker`, which holds every volume) on an
encrypted disk: LUKS2, unlocked at boot by the TPM2 (`systemd-cryptenroll --tpm2-device=auto`) or a
tang server (clevis), or a cloud disk encrypted with a customer-managed key. Encrypt swap or turn it
off, and turn core dumps off (`Storage=none` in `/etc/systemd/coredump.conf`, or
`kernel.core_pattern=|/bin/false`): a dump would copy the app's memory, keys included, to disk.

**The file reader's spill** ([FILES.md](FILES.md) §13). A scan, a key census or a frame of a file side
sorts in DuckDB, which writes what does not fit `files.memory` under `files.spill_dir` (by default
`spill/` on the evidence volume) **in plaintext**: keys and compared values across engines, keys and
hashes between two file sides, for the length of the statement. DuckDB's own temp-file encryption is not
relied on, so the encrypted disk above is what protects it. The directory of a process that crashed is
removed when the next `ds` command starts, or within the hour under a worker (one left by another
user, which the service may not remove, is logged as `spill directory 2 of 5 not swept` and passed over:
remove it by hand). Size the evidence volume
for `files.spill` plus 1 GiB of free space on top of the evidence (a run is refused while less is
free; what the installation's own sorts hold there at that moment counts as free, so the API beside a
sorting worker, or a `ds` command, starts its reader), or give the spill a volume of its own where
exports are large. `files.spill` is each process's budget: one process sorts
one scan at a time, so size the volume for `files.spill` times the processes that may sort at once (the
worker; a `ds run` beside it), or a sort meets a full volume and its run fails with `TargetTempSpace`. The exported
files themselves are yours: DeltaSleuth mounts them read-only and never copies them.

**A spill volume of its own** is mounted at `/var/lib/deltasleuth/spill`, in every service that runs
`ds`, in `docker-compose.site.yml`:

```yaml
services:
  app:
    volumes:
      - spill:/var/lib/deltasleuth/spill
volumes:
  spill: {}
```

with `files: {spill_dir: /var/lib/deltasleuth/spill}` in the settings. The image creates that directory
for the service's user (uid 10001), and a new named volume takes the owner of the directory it is first
mounted on. Mounted on any other path, a new volume is root's, and the service, which runs as uid 10001
with every capability dropped, cannot write it: no file side is read (`the file reader did not start:
files.spill_dir is not a directory DeltaSleuth (uid 10001) may write`). A host directory mounted there
instead keeps the host's owner: give it to uid 10001 before the first start (`sudo install -d -o 10001
-g 10001 -m 0700 /srv/deltasleuth-spill`). The backup command below does not reach such a volume.

**Logs.** The `db` service runs `postgres -c log_error_verbosity=terse`, so an error such as a unique
violation logs no row values (the `DETAIL` line that would quote a key). The compose file keeps
`db`'s log in Docker's `json-file` driver, rotated at 3 files of 10 MB. The app's log follows
Docker's default driver, which carries the SIEM copy (above): rotate it there (journald by its own
limits; `json-file` with `"log-opts": {"max-size": "10m", "max-file": "5"}` in
`/etc/docker/daemon.json`). Database logs, SIEM lines, downloaded reports and the LLM provider's
copy of the chat are outside DeltaSleuth's purge: the site's policy covers them.

**P0 files.** Everything in the `p0` volume stays in clear, where the purge does not reach it:
samples (raw bodies, named by their raw keys), `--missing-keys` files, `--keys` files, the profiler's
output (its draft spec lists the sampled raw keys), reviewed specs and the `R-…` output folders.
Delete every top-level entry older than `standard_days`, whatever its name, with a daily host cron,
for example `/etc/cron.d/deltasleuth-p0` (with your installation's folder and `standard_days`):

```
30 3 * * * root cd /srv/deltasleuth && docker compose run --rm --no-deps -T -u root --cap-add DAC_OVERRIDE app find /var/lib/deltasleuth/p0 -mindepth 1 -maxdepth 1 -mtime +90 -exec rm -rf {} +
```

A spec or keys file still in use after that time goes too: keep the originals outside the volume,
and copy them in when needed. The job runs as root in the container because `docker compose cp`
copies files in as root, with `DAC_OVERRIDE` because the container drops every capability, without
which root cannot enter the `p0` folder (mode 0700). It runs in the installation's folder, whose `.env`
names the compose files. Replace an earlier line, with `-f` or without `DAC_OVERRIDE`, and check the new
one by hand as step 6 of "Moving from a source checkout" (below) does. `ds purge --dry-run` names the
samples directory of each run whose cycle is due (`outside retention: …`).

### Backups and restore

A backup is a database dump and then a copy of the evidence volume. The keyring and the tokeniser key
are never part of it: they are in the vault (section 4). The dump carries the installation id (and, in
a vendor build, the licences installed), so a restore keeps it; in a vendor build, install again, from
`deploy/licence/`, a licence installed after the backup was taken (section 3, The licence). A dump or
point-in-time recovery from before migration 010 holds no id: 010 applied to it again makes the same id
from the store's first audit row, so a vendor build's licence files install on it. Only a store whose
audit log was empty when 010 applied (a new database) has a random id, which only a backup taken after
010 keeps. Backups go outside the checkout, readable only by you:

```sh
mkdir -p -m 700 ~/deltasleuth-backups
(umask 077; docker compose exec -T db pg_dump -U deltasleuth deltasleuth > ~/deltasleuth-backups/ds-$(date +%F).sql)
(umask 077; docker compose run --rm --no-deps -T app tar -C /var/lib/deltasleuth/evidence --exclude=./spill -cf - . > ~/deltasleuth-backups/evidence-$(date +%F).tar)
```

`--exclude=./spill` leaves out the file reader's spill (above): a backup taken without it while a scan
of a file side ran would carry that sort's raw keys and values for B days. **Existing backup jobs: add
it** when you upgrade to a release that reads files. It names the default `files.spill_dir`: a spill
directory you placed elsewhere inside the evidence volume, or inside any other volume you back up, needs
an `--exclude` of its own.

- **Order.** The dump first, then the evidence copy, finished within 24 h of the dump. A blob is
  deleted 24 h after the last observation naming it, so the copy then holds every blob the dump names.
- **Record the missing count** with each backup: the `N missing` of `docker compose run --rm app ds
  keys status --verify`, run just before the dump. A purge during the backup can only lower the count
  the dump holds, so a restore checks that it is no higher (step 3 below).
- **How long.** Keep backups B days, at most 35, and record B. Purged data remains in backups until
  they age out, and in WAL archives and point-in-time recovery (pgBackRest, Barman), which follow
  the same B. A dump holds masked data and ciphertext: without the keyring it shows no raw key,
  header, chat or body.
- **Backups from before `ds keys migrate`** hold raw keys, headers and chat in clear, and their
  evidence copies hold plaintext bodies: label them plaintext and let them age out.
- **Targets declared in the product** (section 7) are in neither: the list and its credentials are files
  of the `workspace` volume, in its folder `targets`. Declare them again after a restore (each
  `target.set` row of the audit log says with what, never a credential): until then a spec that names
  one does not validate, and its runs fail. Or copy that folder with the backup as the evidence volume
  is copied, with `tar -C /var/lib/deltasleuth/workspace` and `targets` in place of `.`, and unpack it
  into the volume before the app starts (not observed). Such a copy holds those credentials in clear:
  keep it as `deploy/secrets` is kept.

**Restore** in this order, so that nothing purged since the backup is served again:

1. Put the keyring and the tokeniser key from the vault in `deploy/secrets/kek` and
   `deploy/secrets/tokeniser`, `chmod 444` (section 3). The vault's current keyring holds every KEK a
   backup of the last B days needs: a KEK is destroyed only B days after its rotation (the
   [runbook](RUNBOOK.md#rotating-the-keyring)).
2. Load the dump into the empty database, then the evidence copy:

   ```sh
   docker compose up -d db
   docker compose exec -T db psql -U deltasleuth -d deltasleuth -q -v ON_ERROR_STOP=1 -1 < ~/deltasleuth-backups/ds-2026-09-24.sql
   docker compose run --rm --no-deps -T app tar -C /var/lib/deltasleuth/evidence -xf - < ~/deltasleuth-backups/evidence-2026-09-24.tar
   ```

   The load is one transaction that stops at its first error. A database that still holds a store
   refuses it whole (`ERROR:  schema "ds" already exists`, exit 3) and is left as it was: never load a
   dump over a store. Without `-v ON_ERROR_STOP=1 -1` such a load runs on past every error (190 in the
   rehearsal of 2026-10-05): it keeps the store that was there, adds the dump's rows again to a table
   without a primary key (`ds.audit_chain_upgrade`) and sets the store's sequences back to the dump's.
   To restore over a store (a rollback, below), empty the database first, with the app stopped; the
   dump creates the schema again:

   ```sh
   docker compose stop app
   docker compose exec -T db psql -U deltasleuth -d deltasleuth -c 'DROP SCHEMA ds CASCADE'
   ```
3. Check that every blob the database names decrypts: `docker compose run --rm app ds keys status
   --verify` must exit 0 (it exits 2 while a blob is corrupted or needs a KEK the keyring lacks), and
   its missing count (`blobs: … N missing`) must be no higher than the one recorded with the backup.
   Missing blobs are counted, not failed: a body already missing at `ds keys migrate` stays missing
   (its reads answer 410) until its cycle is purged. More missing than recorded means the evidence
   copy is incomplete: restore it again before going on.
4. Purge what expired since the backup: `ds purge` (with the alias of section 9).
5. Replay the erasures made after the backup. For each `retention.expire` line in the SIEM newer than
   the backup whose `reason` starts with `erasure: `, run
   `ds purge --run R-… --cycle N --reason "<the text after erasure: >"`. If the restored run is still
   live (`… is still read by the running soak`, or `… has not ended`), erase the whole run instead:
   `ds purge --run R-… --reason "<the text>"`.
6. Pause the schedules before any worker starts: `ds schedules pause --all` (with the alias of section
   9). A restored or copied database otherwise starts one run of each active schedule whose newest due
   time is still in its window, against the declared targets. Resume, on the installation that is to
   fire them, the ones that should run (`ds schedules resume NAME`): the times of the pause are neither
   due nor missed.
7. Start the app: `docker compose up -d app`. This build has no licence to install again; in a vendor
   build, first install again each licence from `deploy/licence/` installed after the backup (section
   3; a file installed already changes nothing). A database restored into place keeps its installation
   id (one from before migration 010 makes the same id again, above); one rebuilt without a backup is a
   new installation, which in a vendor build needs a reissued licence.

### Upgrading from a release bundle

Every release is a complete bundle ([SECURITY.md](../../SECURITY.md), Fixes). Read its `NOTES.txt`
first, then, with its tar in `~/releases` on the Docker host:

1. **Verify it** (section 2, steps 1 to 4). The procedure's step 5 loads the new tag beside the
   running image, which keeps serving.
2. **Back up** (Backups and restore, above), and keep with the backup a copy of `deploy/config` and of
   the site file as they are now, before you extend them for the new release. The backup, those copies
   and the previous release's folder are the rollback point.
3. **Extract the new kit** over the installation (section 2, step 5). The site file, `.env`,
   `deploy/config`, `deploy/secrets` and a vendor build's `deploy/licence` are in no kit, so they stay.
4. **With a private CA,** build the new version's `-ca` layer (section 11, TLS).
5. **Start it:** `docker compose up -d` recreates the containers from the new tag, and the app applies
   the migrations at start. Then run `docker compose run --rm app ds audit verify`, and check
   acceptance item 21 (section 5).
6. **Check the P0 cron:** `/etc/cron.d/deltasleuth-p0` holds the line of Volumes, logs and P0 files
   (above), without `-f` and with `--cap-add DAC_OVERRIDE`. Replace an older line, and check it by hand,
   as step 6 of "Moving from a source checkout" (below) does.

**Rolling back** to the previous release: extract its kit again (section 2, step 5, in its folder),
rebuild its `-ca` layer if you use one, and run `docker compose up -d`: its image is still loaded (the
procedure's steps 0 and 5, run again in its folder, re-check it). Only when the newer release added a
migration, which its source archive lists under `deltasleuth/store/migrations/` (`tar -tzf
source/*-src.tar.gz` in each folder), restore step 2's backup first (Backups and restore, Restore): an
older release does not undo a migration, and work done since the upgrade is lost. Stop the app,
extract the previous kit, empty the database as Restore's step 2 says, take out of `deploy/config` and
the site file what you added for the newer release (its settings blocks, the targets of its engines,
their secrets and mounts) or put back the copies kept with step 2's backup, then follow Restore with
the previous release's `ds`, and start it last. Nothing but this step stops the previous release on a
migrated store: it applies only the migrations it ships and does not look for newer ones (observed in
the rehearsal of 2026-10-05, where the commands of a build that ships 010 answered on a store
migrated to 016). A patch release adds no migration.

Without a migration too, what you added for the newer release comes out, or the copies go back, before
`docker compose up -d`. The previous release refuses a member of `deploy/config` it does not know, a
settings block such as `files`, `notify`, `readiness`, `precheck` or `mcp`, or a target of a newer engine
(`engine: mysql`, `oracle` or `files`). Each of its commands then exits 2 and names what it does not
know, the settings blocks first, in one line (`error: invalid settings
/etc/deltasleuth/deltasleuth.yaml: files: Unexpected keyword argument`), then the members of the targets
file, and the app restarts in a loop until they are gone.

### Re-scanning a release

A release's `scan/` holds what was known on its release date. The maintainer re-scans every supported
release weekly; a finding there that would block a release (one with a fix that is Critical, High, of
unknown severity or known exploited) starts the clock of SECURITY.md's targets, whose table sets its
advisory (SECURITY.md, Targets and Scanning our releases). To scan it yourselves, air-gapped, fetch a
newer grype database elsewhere (`grype db list`), bring it across, and run in the release's folder:

```sh
grype db import /path/to/vulnerability-db.tar.zst
GRYPE_DB_AUTO_UPDATE=false GRYPE_CHECK_FOR_APP_UPDATE=false GRYPE_DB_VALIDATE_AGE=false grype sbom:sbom/deltasleuth-0.1.0-linux-amd64.cdx.json --vex scan/vex.openvex.json
```

The VEX applies the maintainer's `not_affected` decisions. Expect Debian `wont-fix` findings; a binary
scanner also sees the libraries wheels vendor, whose decisions `sbom/vendored-libraries.txt` lists.
Report a newer finding to the security contact (SECURITY.md, Scanning our releases).

### From a one-machine trial

A trial that `deploy/up.sh` or the guide's first chapter set up is not hardened into an office
deployment by adding the IdP: a bearer token keeps working once single sign-on is on
([AUTH.md](AUTH.md) §1). In this order:

1. **Revoke every token the trial made and every session,** and remove the saved token:

   ```sh
   docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds token list
   docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds token revoke T-…   # each active one
   docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds session revoke --all
   rm -r .deltasleuth/tokens
   ```

   The trial's session secrets travelled in a cookie without `Secure`, which the browser sent to every
   http service on that loopback name, and a new `public_url` ends no session: it changes the cookie's
   name only, and a secret sent under the new name is still its session ([AUTH.md](AUTH.md) §3).
2. **Set the office's sign-in** (section 3): the https `public_url`, the `trusted_proxies` line and the
   `oidc` lines switched on, the client secret mounted (section 5). Then run `ds auth check`
   (section 5).
3. **Take the mock services out:** `docker compose stop mocks`, start without `--profile mocks` from
   now on, and delete the four mock targets (`linker-a`, `linker-b`, `customer-a`, `customer-b`) from
   `deploy/config/targets.yaml`.
4. **Vault `kek` and `tokeniser`** (section 4): the trial's database and evidence open only with them.
   Or start on a new database and new keys, and keep nothing of the trial.
5. **Install the signed release** in place of the source build (section 2): `.env` then names the first
   two files only.
6. Run `docker compose up -d`, and go through the acceptance checklist (section 5), whose item 0 reads
   the mode. Everyone signs in again: step 1 revoked the trial's sessions, and a token's session is no
   session under single sign-on. The new address alone would end none ([AUTH.md](AUTH.md) §3). An admin
   then removes, on the Targets screen, each endpoint the trial declared (`Declared in`: `product`) that
   the office does not want: a trial's `spec_author` token could declare any address the container
   reaches, and from now on only an admin signed in within 15 minutes declares one (section 7).

### Moving from a source checkout

An installation set up before the site file kept its compose changes in the tracked compose file, which
an upgrade now replaces. Once, in the installation's folder, before taking the new version:

1. Save your changes outside the checkout, then take them out of it (an empty diff needs neither):

   ```sh
   git diff deploy/docker-compose.yml > ~/deltasleuth-compose.diff
   git apply -R ~/deltasleuth-compose.diff
   ```
2. Take the new version: verify a release and extract its kit over the checkout (section 2, steps 1 to
   5), or take new source (`git pull`).
3. Copy the site example and write `.env` (the introduction), with the source override for a source
   build (section 2, Build from source). Remove any `export COMPOSE_FILE` from your shell and its
   profile: an exported value overrides `.env`. A file it named besides the base file, such as
   `examples/tables/compose.yaml`, goes into `.env`'s `COMPOSE_FILE` after the site file.
4. Move each change in `~/deltasleuth-compose.diff` into the site file: an added secret goes in its
   `app.secrets` and `secrets:`, and a changed subnet in its `networks` line. The example holds
   `oidc_client_secret` and `llm_key` as commented lines: name the ones you use in `app.secrets` and
   remove the `# ` of their lines under `secrets:`, and of that line itself (section 3). Check the
   result with `docker compose config`.
5. Back up (above), then run `docker compose up -d` (a source build: `docker compose up -d --build`).
   The volumes stay.
6. **Replace the P0 cron.** Its earlier line in `/etc/cron.d/deltasleuth-p0` names a compose file
   with `-f` and lacks `--cap-add DAC_OVERRIDE`. Under the hardened container it fails every night with
   `Permission denied` (a source build: `No such image`) and deletes nothing, and with a moved subnet it
   cuts the app off from `db` (the introduction). Write the line of Volumes, logs and P0 files (above)
   in its place, then run it once by hand with `-print` in place of `-exec rm -rf {} +`. It lists the
   entries due, if any, and prints no `Permission denied`:

   ```sh
   cd /srv/deltasleuth && docker compose run --rm --no-deps -T -u root --cap-add DAC_OVERRIDE app find /var/lib/deltasleuth/p0 -mindepth 1 -maxdepth 1 -mtime +90 -print
   ```

### Upgrading to encryption at rest

Releases before migration 006 stored raw keys, headers and chat in clear, and bodies as plaintext
blobs. The new release refuses to start on such a store (`legacy plaintext remains: stop the app and
run 'ds keys migrate' …`) until `ds keys migrate` has sealed it, offline. Plan a window: migration 006
rewrites `observations`, `results` and `findings`, and `ds keys migrate` seals an estimated 30,000
rows and 2,000 blobs a second.

1. **Back up** (above), and check that the database volume has free space of at least 2.5 to 3 times
   the size of `ds.observations`, `ds.results`, `ds.findings` and `ds.run_keys` (006 rewrites the
   first three, `ds keys migrate` updates every observation and key row again, and VACUUM FULL writes a
   copy of each), plus room for the WAL this writes: `max_wal_size` (1 GB by default), or, with WAL
   archiving or point-in-time recovery on, the archive's space for WAL of about the same size again:

   ```sh
   docker compose exec -T db psql -U deltasleuth -d deltasleuth -c "SELECT pg_size_pretty(sum(pg_total_relation_size(t::regclass))) FROM unnest(array['ds.observations', 'ds.results', 'ds.findings', 'ds.run_keys']) AS t"
   ```
2. **Stop the app** (`docker compose stop app`), **take the new release and move to the site file**
   ("Moving from a source checkout", above, steps 1 to 4), **then create the keyring and move the
   tokeniser key** out of the workspace volume into its secret (the redirect creates the secret file
   before Compose looks for it). The key keeps its value, so every token stays the same:

   ```sh
   (umask 077; openssl rand -hex 32 > deploy/secrets/kek; docker compose run --rm --no-deps -T app cat /var/lib/deltasleuth/workspace/tokeniser.key > deploy/secrets/tokeniser)
   chmod 444 deploy/secrets/kek deploy/secrets/tokeniser
   ```
3. **Add the `encryption` block** of the example to `deltasleuth.yaml`, vault both files (section 4),
   then delete the workspace copy, which would otherwise stop every command (`tokeniser.key was moved
   to encryption.tokeniser_key_ref: …`):

   ```sh
   docker compose run --rm --no-deps app rm /var/lib/deltasleuth/workspace/tokeniser.key
   ```
4. **Seal the store,** then refresh the planner's statistics of the columns sealed in place and
   rewrite the tables whose old row versions still hold plaintext:

   ```sh
   ds keys migrate                                   # the alias of section 9
   docker compose exec -T db psql -U deltasleuth -d deltasleuth -c 'ANALYZE ds.scans, ds.observations' -c 'VACUUM FULL ds.run_keys, ds.observations, ds.results, ds.findings, ds.scans, ds.chat_messages, ds.runs, pg_catalog.pg_statistic'
   ```

   `pg_statistic` holds the planner's samples of column values: raw keys and headers of the dropped
   columns, and, until the `ANALYZE` replaces them, the raw scan points of `ds.scans.state` and the
   legacy blobs' SHA-256 names in `ds.observations.content_hash`, which `ds keys migrate` rewrote in
   place (`VACUUM FULL` never re-analyses, and removes only the replaced rows). `ds.runs` still
   carries, in its rows' dropped `scan` column, the raw resume point of a table scan run under
   migration 003; only a rewrite removes it. Rewriting `pg_statistic` takes a superuser, as the
   compose file's database user is; another user skips it with a warning. Each `-c` runs on its own,
   as `VACUUM` cannot run inside a transaction.

   `ds keys migrate` holds the worker's lock, so it refuses to run beside the app (`stop the app
   first: docker compose stop app`). It also refuses, changing nothing, when observations name legacy
   blobs but `blobs/sha256` in the evidence volume holds none (`observations name legacy blobs, but …
   holds none: check evidence_dir, …`): check the volume and `evidence_dir`. `ds keys migrate
   --accept-missing` records every such blob as missing instead; a later run with the blobs back
   repoints them. It works in batches of one transaction each: interrupted (Ctrl-C exits 130), it
   continues where it stopped when run again. It ends with a line such as `sealed: 9 key(s), 14
   header(s), 4 chat message(s) and 2 scan point(s) under 5 new run data key(s); 6 blob(s)
   re-encrypted, 2 missing; plaintext storage ended (migration 007)`. A body that was already missing
   stays missing (its reads answer 410, and `ds keys status --verify` counts it: record that count
   with the next backup); a corrupted one stops it, naming the file, until it is restored or deleted.
5. **Start the app:** `docker compose up -d`, then replace the P0 cron ("Moving from a source
   checkout", step 6). The worker's first purge comes 7 days after the upgrade: before then, run
   `ds purge --dry-run` and hold the runs that must stay (`ds purge hold`, runbook).
6. **Label the backups taken before step 4 plaintext,** and let them age out.

### Upgrading from the shared API token

1. The app stops at start with `api_token_ref: replaced by named API tokens; …` (section 11).
2. In `deltasleuth.yaml`, replace `api_token_ref` with the `auth` block (section 3). Take the new
   release's kit and image and move to the site file ("Moving from a source checkout", above, steps 1
   to 4), whose example holds the IdP secret as a commented line in place of `api_token` (its step 4
   switches it on); the base file pins the network.
   Then run `docker compose down && docker compose up -d` (a source build: `up -d --build`), and
   replace the P0 cron (its step 6). The volumes stay.
3. Run `ds auth check` (section 5).
4. An admin signs in and creates each CI token on Admin, Tokens, with its owner and accountable
   person, and each CI job switches to its own token. In tokens mode, use `ds token create`
   (section 9). Then delete `deploy/secrets/api_token`.

Audit rows from before the upgrade keep their actor, `api` or `ds`.

### Upgrading to licence keys

**To this build there is no such upgrade:** it needs no licence (section 3, The licence), so upgrade as
"Upgrading from a release bundle" says, with nothing to install. Migration 010 still makes the
installation id. On a database an older build that enforced the licence ran as an unlicensed evaluation
(every build from migration 010 until this one, a source deployment of one among them, or a vendor
build), runs made then keep their "unlicensed evaluation" label and get no evidence pack, new runs carry
no label, a soak that build paused for the licence (`run_paused_licence`) resumes when a person resumes
it on its Run screen, a schedule that build refused for the licence is refused for none at its next due
time (section 11, Schedules, says what it shows until then and what is recorded), and the worker
records `licence.state` `{from: evaluation, to: open}` ([LICENCE.md](LICENCE.md) §19).

**The steps below are for an upgrade to a vendor build.** There, from migration 010, an installation
without a licence is an unlicensed evaluation (section 3, The licence): two pairs and, with SSO, no new
runs, compares or soak cycles (live soaks pause at their next cycle), no sign-off and no evidence packs.
Upgrade so that the licence is installed before the new release serves, with one restart as downtime
([LICENCE.md](LICENCE.md) §15):

1. **Take** the new release while the old one serves: verify it and extract its kit (section 2,
   steps 1 to 5), which leaves the running container alone; or, from source, `git pull && docker
   compose build`.
2. **Print the id:** `docker compose run --rm app ds licence id`. The new image applies migration 010 and
   prints the installation id. The old release keeps serving, restarts included: it applies only the
   migrations it ships. A backup taken before this step restores to the same id: 010 applied to it
   again derives the id from the store's first audit row. Only a store whose audit log was empty gets a
   random id; take a backup after this step, so that a restore keeps it.
3. **Ask for the licence:** send the id to the vendor; keep the licence file it returns in
   `deploy/licence/`.
4. **Install** it with the new image (section 3, The licence, step 3), check it with `ds licence`, then
   start the new release: `docker compose up -d`.

A build that embeds no vendor key is an open build: it refuses every licence file (`this build embeds
no vendor key: it needs no licence and can verify none …`) because it needs none, and an SSO deployment
runs on it without one. Runs made before step 2, by the old release after it, or by an open build carry
no licence record: they have no licence label, and in a vendor build their cycles are packed under the
licence at issue. Under Community or Pro, run in tokens mode: remove `auth.oidc` and the `packs` block
too, which needs SSO; from then on the revocation list of earlier packs is no longer signed.

## 11. Troubleshooting

### Installing a release

Any output section 2 does not expect means: stop, load nothing more, and call the security
contact ([SECURITY.md](../../SECURITY.md)). Never type a fix from the bundle's own files. "Step" below
is the procedure's.

| Symptom | Cause | Fix |
|---|---|---|
| The tar's sha256 differs from the ticket's, or a `grep` of section 2's step 1 prints a line | The tar was changed, or is not the release | Fetch it again through the approved channel |
| Step 0 prints `diff` lines | The bundle's `VERIFY.txt` differs from the procedure your register filed: another format (`deltasleuth-release/2`, filed like a new key) or a forged bundle | Stop; a new format comes with its own filed procedure |
| Step 0 prints `line 2 of VERIFY.txt is malformed` | Line 2 is not `VERSION=… PLATFORM=…` | Stop: never run line 2 |
| Step 1 prints a fingerprint your register does not list, or lists as not in service on step 4's `signed` date | Another key signed it | Stop. A withdrawn key's bundle is genuine only if its list digest is in that key's compromise advisory |
| Step 2 prints `Verification failure` or an error | The key did not sign this `SHA256SUMS`, or `release-key.pem` holds more than one key | Stop |
| Step 3 prints `FAILED`, or `diff` prints a line | A file changed, was added or was removed after signing | Fetch the bundle again |
| Step 4's lines differ from your change record | Not the release the change names | Stop |
| Step 5 prints `0` under Podman | Podman's `.Id` has no `sha256:` prefix | Run `sed -i 's/^/sha256:/' ../id.txt`, then the step's last command again |
| Step 5 prints `0` under Docker | Step 0 did not run in this shell, so `$VERSION` and `$PLATFORM` are empty (step 5 then printed `no such file or directory` and `invalid reference format` first); otherwise, the tag names another image than the one listed | First run step 0, then step 5, in one shell in the release's folder. Only if it still prints `0`: stop, and `docker image rm deltasleuth:<version>` |
| `sha256sum: command not found` (macOS) | No coreutils | Define it as the procedure's `Needs` line says, with `shasum -a 256` |

### Startup, secrets and the database

The app writes one `error:` line for a configuration problem and exits. `docker compose logs app`
shows it.

| Symptom | Cause | Fix |
|---|---|---|
| "encryption: required with auth" | The settings have `auth` but no `encryption` | Add the block and its two secrets (section 3) |
| "encryption.kek_ref: must not be inside evidence_dir or workspace_dir, whose backups would carry it" (or `tokeniser_key_ref`) | A key secret points into a volume | Keep both under `/run/secrets` (section 3) |
| "tokeniser.key was moved to encryption.tokeniser_key_ref: delete it from the workspace" | The workspace still holds the tokeniser key of a release before encryption at rest | Section 10, upgrading to encryption at rest, step 3 |
| "keyring /run/secrets/kek line 3: expected 64 hex", or "keyring … holds no key" | The keyring file is damaged | Restore it from the vault, and check it with `ds keys fingerprint` (section 4) |
| "key file /run/secrets/tokeniser must hold the key as hexadecimal text" (or "holds fewer than 32 bytes") | The tokeniser key file is damaged | Restore it from the vault |
| "legacy plaintext remains: stop the app and run 'ds keys migrate' (docs/mvp/RETENTION.md §7.2)", or "legacy plaintext remains in …/blobs/sha256: …" | The store predates encryption at rest | Section 10, upgrading to encryption at rest |
| "warning: the keyring lacks K-…, which wraps 12 data key(s): restore the line from the vault and restart" | A KEK line is missing. The app starts, but reads that need the KEK answer 503 "evidence unreadable: the keyring lacks K-… …", and runs that need it pause with `run_paused_key_missing` | Restore the line from the vault and restart the app, then resume the paused runs. A KEK lost for good: erase the runs it wraps (runbook, Retention) |
| "a key wrapped by K-… does not unwrap: it was altered", or "data key E-… does not match its id: the row was altered" | A `ds.data_keys` row was changed | Restore the database (section 10) |
| "stop the app first: docker compose stop app", from `ds keys migrate` | The app holds the worker lock | Stop the app, then run it again |
| "retention: Value error, audit_days must be at least standard_days" | `retention.audit_days` is below `standard_days`; each is 7 to 3650 | Fix `deltasleuth.yaml` |
| "serving on 0.0.0.0 needs the auth settings (docs/mvp/AUTH.md §1)" | The settings have no `auth` block | Add it (section 3) |
| "api_token_ref: replaced by named API tokens; …" | The settings predate named API tokens | Section 10, upgrading |
| "auth.public_url: must be https://host[:port] …" | `public_url` is http beyond loopback, or has a path | Give the https URL people open, without a path |
| "auth.allowed_hosts: needs an https public_url" | Extra host names with an http `public_url` | Use https |
| "auth.oidc: … client_auth … needs client_secret_ref" (or `client_key_ref`), or "… is only for …" | The credential does not fit `client_auth` | Section 3 |
| "auth.oidc.client_key_ref: … is not a PEM RSA (>= 2048 bits) or EC P-256 private key" | A wrong, weak or encrypted key | Create it as in section 3 |
| "auth.oidc.client_cert_file: … is not a PEM certificate of client_key_ref's key" | The certificate belongs to another key | Create both together (section 3) |
| "warning: identity provider unreachable at start; sign-in retries on first use" | The app cannot reach the IdP; the next line says why. Sessions and tokens keep working | Allow egress to the issuer's host, or set `auth.oidc.proxy`; then `ds auth check` |
| "set DS_OPERATOR to your user name (docs/mvp/AUTH.md §4.2)" | A CLI command that writes audit rows ran without `DS_OPERATOR` | Use the alias of section 9 |
| "cannot read secret file /run/secrets/…: No such file or directory", at start, as a failed run's error, or as a chat's 503 | The secret is not mounted | Add it to the site file (section 3), then `docker compose up -d` |
| "No such image: deltasleuth:…", from `docker compose up` or `run` | The compose file never builds or pulls: it runs the loaded release, `deltasleuth:source` with the source override, or the `-ca` layer with the CA override | Install the release (section 2; `docker image ls deltasleuth` lists what is loaded), build from source (section 2), or build the CA layer (TLS, below) |
| "Read-only file system", from a command in the container | The root filesystem is read-only: only the volumes and `/tmp` (64 MB) are writable | Write under `/var/lib/deltasleuth/p0` (section 9). Keep `read_only`: never override it in the site file |
| "find: '/var/lib/deltasleuth/p0': Permission denied", from the P0 cron | The container drops every capability, and root needs `DAC_OVERRIDE` to enter the 0700 folder | Replace the line with section 10's, which adds `--cap-add DAC_OVERRIDE` after `-u root` and has no `-f`, and check it by hand (section 10, Moving from a source checkout, step 6) |
| "cannot read secret file /run/secrets/…: Permission denied" | The container user cannot read the file | `chmod 444 deploy/secrets/*` (the directory stays 700) |
| "secret file … is empty or has … surrounding whitespace" | The secret was written with extra whitespace | Rewrite it with `printf '%s'` |
| "the database answered and asked for a password, then refused the connection: check the user, the password and the database name in the file database_dsn_ref names; if they are right, the server, the role or the database is at its connection limit, or a pooler cannot reach its server", from the app's start and from every command that opens the store, after three tries two seconds apart (at once when `db_dsn` holds no password) | The database was reached and refused the sign-in after the password: the `pgdata` volume was created with another password than `deploy/secrets/db_password` holds, or `db_dsn` names another user or database. A database of your own refuses a right password in the same way while the server, the role or the database is at its connection limit, and a pooler in front of it while it cannot reach its server: the driver gives no code that tells these apart, and its own words are never printed. A limit that frees within the three tries is not reported. *Not said by this line:* a wrong password on a server that takes TLS connections only, when `db_dsn` has no `sslmode`: the driver tries again without TLS, and the command ends after 30 s with "the database is unreachable (PoolTimeout); check database_dsn_ref" under the driver's own line. Write `?sslmode=require` (or `verify-full`) at the end of such an address ([FIRSTRUN.md](FIRSTRUN.md) §14) | Put back the `db_password` and `db_dsn` the volume was created under, or correct `db_dsn`. When `db_password` is the volume's and only `db_dsn` is wrong: `rm -f deploy/secrets/db_dsn`, write it again with the `db_dsn` lines of section 3 (Secrets), which read the password from its file, `chmod 444 deploy/secrets/db_dsn`, then `docker compose restart app`. Never set a password on a command line, where the host's process list and the shell history keep it. With a database of your own whose user, password and name are right, ask its administrator for the connection limits and the pooler's state: the server's own message, which DeltaSleuth does not print, is not kept |
| "the file database_dsn_ref names does not hold an address the driver can read: percent-encode special characters in the user and the password", at once, from the app's start and from every command that opens the store | `db_dsn` is no address the PostgreSQL driver can parse, or its user or password holds an unencoded `@` or `/`, which would make the driver take a piece of the password for the host or the database. Nothing was connected, and no piece of the address is printed | Write `db_dsn` as section 3 (Secrets) says: one line, with the special characters of the user and the password percent-encoded |
| "cannot listen on 127.0.0.1:8000: the port is in use; stop the program that holds it, or pass --port" (any other reason: "cannot listen on <host>:<port>: <the system's words>"), from `ds serve` | The address is taken or is not this machine's: on a host, a `ds serve` from a source checkout beside an installation that publishes the same port. Nothing was opened: no `system.start` row, no worker | Stop the other program, or pass `--port`. In the container `ds serve` listens on its own port 8000; the host's port is the site file's `ports` line (section 3) |
| "warning: the web UI is not built (… /web/dist is missing): / answers 404 and only the API under /v1 is served; …", from `ds serve`; the UI shows 404 "the web UI is not built on this server; its API is under /v1" at `/` | The image or the checkout lacks `web/dist`; the API works | A release: run the procedure's steps 0 and 5 again in its folder (section 2), whose step 5 loads the signed image anew; a source build: `docker compose build --no-cache app`; a source checkout: `npm --prefix web ci` and `npm --prefix web run build`, then start `ds serve` again |

### Sign-in

A refused sign-in shows a fixed message and a request id (`Q-…`), never the IdP's text. The app log
has the reason on the same request id: `auth.login_failed reason=<code> ip=<addr> request_id=Q-…:
<the failed check and its values>`, or `auth.login_denied reason=<code> user_id=U-…` for a known
person. `ds auth check` repeats the IdP checks.

| Sign-in page | Cause | Fix |
|---|---|---|
| "The identity provider cannot be reached." (`idp_unavailable`) | The IdP did not answer, or its discovery document failed a check, for example "discovery issuer … differs from auth.oidc.issuer …" | Egress to the issuer's host, a private CA (TLS, below), and `issuer` exactly as the IdP spells it, trailing slash included |
| "The sign-in could not be completed." (`login_failed`) | The code exchange or the ID token failed: a wrong client credential, an algorithm outside `id_token_algs`, clocks apart, or the audience | The log line names it; `ds auth check` shows the keys and the clock offset |
| "The sign-in expired or was started in another tab." (`login_expired`) | The login cookie is missing, altered or over 10 minutes old, or a later sign-in in another tab replaced it | Sign in again, at `public_url` |
| "The identity provider refused the sign-in." / "reported an error." (`access_denied`, `idp_error`) | The IdP stopped it, for example a user not assigned to the application | The IdP's own log |
| "Your account has no DeltaSleuth role." (`no_role`) | None of the user's groups maps to a role. The log lists the claim names the ID token had and how many groups were read | Check `group_claims` against those names and `roles` against the ids (section 5) |
| "Your account is in too many groups …" (`groups_overage`) | The IdP left the groups out (Entra over 200 groups) | Map Entra app roles instead (section 5) |
| "Your DeltaSleuth account is disabled." (`disabled`) | An admin disabled the user | Admin, Users, Enable; or `ds users enable U-…` |
| 400 `unexpected Host header` | The proxy does not pass the `public_url` host | `proxy_set_header Host` (section 5), or `allowed_hosts` for an API client's host name |
| Every audit row shows the same address | `trusted_proxies` does not name the proxy's peer | Section 5 |
| `ds auth check`: "jwks: no key fits auth.oidc.id_token_algs …" | The IdP signs with another algorithm | Set `id_token_algs` to what its keys list |
| "warning: no IdP group maps to rule_approver or above: nothing can be approved", from `ds serve` or `ds auth check` | `auth.oidc.roles` maps no group to `rule_approver`, `pii_reader` or `admin` | Map one (section 5); runs of versions that need an approval wait until then |
| Approve says "Sign in again" (`reauth_required`) | The approver's sign-in is over 15 minutes old, or Entra sends no `auth_time` | Sign in again from the review; on Entra, add the `auth_time` optional claim (section 5) |

### The licence

This build refuses nothing for a licence and warns of none: of the rows below it can show only the
first, the last and, on a database an older build that enforced the licence ran, a soak that build
paused with
`run_paused_licence` and "run R-… was made in an unlicensed evaluation: …". The others are a vendor
build's. A refusal is 409 in the API and one `error:` line with
exit 2 in the CLI, writing nothing but the spec file that `ds run` and `ds compare` store first (a new
version, audited `spec.create`), as an upload, which always works ([LICENCE.md](LICENCE.md) §9 lists
every message).

| Symptom | Cause | Fix |
|---|---|---|
| "this build embeds no vendor key: it needs no licence and can verify none (docs/mvp/LICENCE.md §19)" from `ds licence install` or `ds licence show` | This build is an open build: no licence exists for it | Nothing: it needs none. `ds licence` prints `state open` |
| "SSO (auth.oidc) is an Enterprise feature and licence LI-… is pro: …" (or "… this installation is an unlicensed evaluation: …"), ending "(this installation's server last started in oidc mode)" from a command whose settings lack `auth.oidc` | The installation runs SSO without an Enterprise licence: sign-in, reads and administration work; runs, compares and soak cycles do not | Install an Enterprise licence (section 3), or run in tokens mode, which means removing `auth.oidc` and also `packs`, since `load_settings` refuses `packs` without SSO. With `packs` set, the message says "keep SSO, which evidence packs need" instead of offering tokens mode |
| "spec … would be pair 6 of 5 under licence LI-… …", or "… would be a third pair: an unlicensed evaluation runs 2 pairs (…)" | A new spec name beyond the licence's pairs | Run a seated name (`ds licence` lists them), or install a licence with more pairs |
| "spec … is not one of the 5 pairs licence LI-… … seats …", or a soak paused with `run_paused_licence` | A name counted after the seats filled, for example after a downgrade | A licence that seats it; then resume the paused soak on its Run screen. In this build a soak an older build that enforced the licence paused so resumes with no licence |
| "licence LI-… ended on … and its 30 days of grace on …: …" | The licence and its grace ended: new runs, compares, soak cycles and packs stop | Install the renewal (section 3), then resume the runs paused for the licence |
| "four-eyes sign-off is an Enterprise feature …", "evidence packs are an Enterprise feature …", "run R-… was made in an unlicensed evaluation: no evidence pack …" | In a vendor build sign-off and packs need Enterprise; a run made without a licence by a vendor build, or by an older build that enforced the licence, never gets a pack, in this build either | A new run (in a vendor build, under an Enterprise licence) |
| "… is not a DeltaSleuth licence (deltasleuth-licence/1): line 4: …", "… is signed by key …, which this build does not trust: …", "… does not verify with its key …: …" | The file was edited or truncated, or signed by a key this build does not trust | Install the file as received; `ds licence show - < FILE` checks it without the database |
| "licence LI-… is for installation I-…, not this one (I-…): …" | The licence was issued for another database | Send `ds licence id` and ask for a licence for it |
| "licence LI-… ended on … and its grace on …: install a current licence", "… is installed already with other content: …", "… was withdrawn on …: …" | A past, altered or withdrawn licence | Ask the vendor for the current file |
| "warning: licence LI-… in the store does not count: …", or "… was installed (audit row …) but is missing from the store: …" | A stored licence no longer verifies for this installation (`… which this build does not trust: …`: a later release, which trusts its key, installed it), or was deleted from the database. A licence signed by a key a release removed after a compromise is not warned of: the Licence screen says its reissue replaces it | Install the file again from `deploy/licence/` (or run the later release), and find out who changed the database |
| "warning: the database clock is … behind the newest run …" | The database host's clock went back: licence dates are judged by it. Nothing is refused, and runs made meanwhile record it | Correct the clock (NTP) |
| "licence status unavailable (…): see the server log" | The licence could not be read at start; the process starts anyway | The log names the error, usually the database |

### Targets and specs

| Symptom | Cause | Fix |
|---|---|---|
| "loopback, private and link-local hosts need allow_private_network: true" | An internal target without the network opt-in | Set it on the target |
| "credentials require https unless allow_insecure_http is true" | Plain HTTP with a credential | Use https. Set the opt-in only for a lab target. |
| On the Targets screen or from `PUT /v1/targets/{id}`: "invalid target" with the issue "localhost is DeltaSleuth's own container here, not this machine: write the host as host.docker.internal, keep the port, and set allow_private_network" | The base URL names `localhost` or a loopback address, which inside the container is DeltaSleuth itself | Write the host as `host.docker.internal`, keep the port and tick "Private or local network" (section 7, An app on this machine) |
| "target … is declared in the targets file: change it there" (409) | The id is one of the targets file, which wins | Change the file and restart the app, or declare the endpoint under another id |
| "target … is read by 2 unfinished run(s): stop them first" (409) | A removal, or a change of the address, a consent, the kind of authentication, a header's name or "A 404 means no such record", while unfinished runs read the target (a paused run and a soak between cycles count) | Let the runs end, or cancel them; a credential and the budget change under them |
| "secret is refused: it holds DeltaSleuth's own cookie ds_session_8000, which signs in here and which a browser sends to every port of this host: leave that cookie out" (422) | The credential typed or pasted for an endpoint holds this installation's own session or sign-in cookie: a request to an app on the same host, copied from the browser, carries it | Remove that cookie from the value and save. "Import curl…" leaves it out by itself |
| "secret is required: none is stored for this auth, or the base URL or the auth changed" (422) | No credential is stored for this authentication, or the address or the kind changed: a stored credential never follows a target to another address | Type the credential again and save |
| "the stored credential of target … cannot be read: enter it again", from Send, a compare or a failed run | The credential's file is gone: a separate `ds worker` or `ds mcp` that started before the credential was replaced, or a `workspace` volume restored without its `targets/` folder | Restart that process; otherwise `Change` the endpoint on the Targets screen, type the credential and save |
| "the target could not be written: No space left on device" (500; any OS error) | The `workspace` volume did not take a credential or the list. The list and its credentials are as they were; the `target.set` row of the attempt stays | Fix the volume, then save again |
| `ds serve` and every `ds` command end with an `error:` line that ends "(the product's list, /var/lib/deltasleuth/workspace/targets/targets.json)" | The product's list does not load: it was edited or damaged, or it holds an entry this build's rules refuse | The [runbook](RUNBOOK.md#endpoints-declared-in-the-product): correct the cause, or remove the list and declare the endpoints again |
| Send shows "HTTP 401" with `auth_error` and no answer, or "No answer" with `connect_error` | The endpoint refused the credential (an expired session cookie); or no connection was made: a wrong host or port, a certificate that does not verify (a self-signed one: TLS is always verified), or on Linux an app that listens on 127.0.0.1 alone | `Change` the endpoint and type the credential again; check the address (section 7, An app on this machine) |
| A key one side lacks reads INCONCLUSIVE `absence_unconfirmed` on an endpoint declared in the product | Its 404 is not declared: the endpoint lacks "A 404 means no such record", or the side has no `notFound: {status: [404]}` | Tick it on the endpoint while no unfinished run reads it, and give the side the line |
| Spec upload fails with 422 `not_found_undeclared` (X2) | The side's `notFound` status is not declared by its target | Add the status to the target's `authoritative_absence` |
| Spec upload fails with 422 `truncation_undeclared` (X33) | Truncation is declared nowhere | Declare `truncation` on the side, or `truncation_indicators` on the target |
| Keys come back INCONCLUSIVE `absence_unconfirmed` | A not-found response whose body matches no declared absence shape, often a gateway 404 for a wrong path | Check the request path and the evidence on the Key screen |
| Many keys come back INCONCLUSIVE `timeout`, `rate_limited` or `circuit_open` | The service is slow or pushing back | Lower `rate_per_s`, raise the spec's `execution.timeouts`, check the service |
| Calls time out, yet `curl` through the proxy works | DeltaSleuth ignores proxies: only `auth.oidc.proxy` (the IdP) and `notify.proxy` (notification channels with `proxy: true`) are used | Allow direct egress from the host |
| `ds run` logs "another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops" | `ds serve` processes the run | Nothing: it waits for the run to end. `--no-wait` returns at once. |
| "cannot read …: No such file or directory", "No such file or directory: …" or "sample root '…' does not exist", each ending "(ds runs in a container, where this path is /app/…: a file or folder of the host must first be copied in, with docker compose cp, or mounted)" | `ds` was given a path of the host: it runs in a container and read the path there, a relative one from `/app` | Copy the file or folder into the `p0` volume and give that path (section 9), or mount it in the site file. A build of a checkout reads `mocks/…` and `examples/…` as they are (section 2) |
| "side a: the directory of target … is not there, or DeltaSleuth (uid 10001) may not list it: is it mounted into this container?" (or "did not answer within 30 s") | The export directory is not mounted into the service that created the run, is not readable by uid 10001, or its mount hangs | Add the `:ro` mount to every service that runs `ds` (section 7, Files), fix its permissions or SELinux label, or repair the mount |
| "side a: the directory of target … is mounted writable" (or "file 3 of 12 lies on a mount DeltaSleuth could write to") | The mount lacks `:ro`; the second: an export mounted below the target's root lacks it (each file's own mount is judged) | Mount it read-only; `allow_writable: true` is for development |
| "side a: column 'seen' (an INT96 timestamp) is read to the microsecond: declare typeMap {seen: {as: timestamp}} …" (or "a UTC nanosecond timestamp", "a nanosecond time") | The Parquet column holds nanoseconds, which are read to the microsecond (INT96 is the legacy timestamp of Impala, Hive and Spark) | Declare the typeMap the message names in the spec, or list the columns to compare without it |
| "side a: a directory above the declared partitions (dt) is named name=value …" | With `partitions`, every `name=value` directory of a file's path is read as a column | Rename the directory, or mount the export so that its root and the path's fixed part hold no `=` |
| "side a: file 3 of 12 has a Parquet footer that is not read: …" | The footer is over 16 MiB, its schema holds over 10,000 elements or nests groups over 32 deep, the file holds over 10,000 row groups or 200,000 column chunks (row groups times columns), or it is not written as the reader walks one before DuckDB is handed the file: damaged, or of a writer or a format version whose footer holds what the walk does not know ([FILES.md](FILES.md) §6 step 4) | Export fewer columns or flatter ones, larger row groups, or several files; a damaged file, again; another writer's file that DuckDB itself reads: report it with the writer's name and version |
| "side a: file 3 of 12 is encrypted Parquet, which is not read" | The file's footer is encrypted, or one of its columns is (also one the spec does not compare) | Export it without Parquet encryption; the volume's or the mount's encryption is the place for that |
| "side a: a file changed 12 s ago, inside the 60 s target … waits (settle_s)", "file 3 of 12 changed while the run was planned", "the newest file is 3 d 4 h old, over the 26h the spec allows" | The export is still being written, or did not arrive | Wait, or ask the export team (write then rename, a stamp file last). A schedule tries again until its window closes |
| "side a: over 1,000 lines of its files are not rows of the declared CSV dialect" | The declared delimiter, quote, line ends or encoding do not fit the files | `ds target check ID --table PATH --format csv` drafts a dialect; `--count` lists the lines by position and kind |
| "side a: the file reader did not start: its spill volume has 12 GiB free, and files.spill (20 GiB) plus 1 GiB must fit" (or "files.spill_dir is not a directory DeltaSleuth … may write") | The spill's volume is too small or not writable (the free space said counts what this installation's own sorts hold there) | Lower `files.spill`, grow the volume, or give the spill a volume of its own (section 10: mounted at `/var/lib/deltasleuth/spill`, which the image creates for uid 10001; a volume mounted elsewhere, or a host directory, is root's until you give it to uid 10001) |
| "side a: the file reader did not start: its guard did not hold, or this build's DuckDB lacks the Parquet reader (see the server log)" | A check of DuckDB's lock-down did not hold in this build | The log names it at error level (`the file reader did not start: …`): report it with that line; no file is read until it holds |
| "side a: the file reader takes no work of target …: 4 reads of its mount, which does not answer, are still waiting" (or "the file reader takes no work: 8 reads of mounts that do not answer are still waiting") | The export's mount hangs; the second: two mounts do, and every files target waits | Repair the mount ([runbook](RUNBOOK.md#from-files)); the first refuses that target only |
| "side a: the file reader did not take its files' work (Busy): try again" (or "the file reader plans 8 runs at once, and this one waited the 300 s files.plan_s allows for its turn") | Every thread of the file reader was busy for 30 s, or 8 run creations were planning files; `(Dropped)`: its DuckDB instance was discarded for another read's failure at that moment, and is built anew | Try again; stagger the creation of runs on large exports. A schedule tries again by itself until its window closes |
| "side a: the file reader did not take its files' work (Died): try again" (or `(Alone)`, `(Unstarted)`); the worker logs "scan of run R-… paused: Died" | The file reader's process (the child that runs DuckDB) ended under a read, by the cause the log names; `(Alone)`: another export is read apart first; `(Unstarted)`: the reader's process did not start | Try again: a new reader is started, and scans resume by themselves ([runbook](RUNBOOK.md#from-files)) |
| "side a: the file reader did not start: its process did not start or did not answer (see the server log)" | The reader's process could not be started, ended as it started, did not answer within 20 s or answered outside its protocol; the log's error line says which | Report it with that line, and run the reader's self-test (section 5, item 25). Where the reader's data limit or socket filter is what stops it on a host, `files.os_limits: false` starts it with neither (section 7, Files) |
| "side a: the file reader's process ended 3 times (SIGBUS) while only this side's files were being read: they are read no more until DeltaSleuth restarts" (a run fails with "ReaderDied: …" and the same text) | Three ends of the reader's process, each while this side's files alone were being read. The cause in brackets is the last one's: a fault's signal (DuckDB on these bytes; on Linux also a statement that met the reader's data limit, observed as `SIGSEGV`), `SIGKILL` (the kernel, for memory, or an operator), `SIGABRT` or `exit N` (DuckDB's own abort, or an error of the reader's own code), `killed after its deadline` (a statement that did not end) | By the cause ([runbook](RUNBOOK.md#from-files)): export the files again and report the file; or size the container (section 7, Files). Restart the `ds` process to read those files again |
| "side a: planning its files passed the 300 s files.plan_s allows (7 of 12 files hashed)" | The run's file sides together are larger than `plan_s` allows to hash and count (a second side may say "0 of 12") | Try again (a file renamed into place is not hashed twice); raise `files.plan_s` and the proxy's read timeout |
| "side a: one pass over its files took 48 s and a per-key read is one pass: set the spec's execution.timeouts.total to 96s or more (it is 60s), or export Parquet" | A per-key read of this CSV would time out. In the Linux image a key declared by `csv.keys` as an integer or a decimal was most of such a pass (section 7, Files, point 5: about six times a laptop's figure) | Raise the timeout in the spec, or export Parquet |
| "side a: sorting its 100,000,000 rows needs about 52 GiB of temporary space and files.spill allows 20 GiB" | A scan's sort is projected to pass the spill budget (section 7, Files: the projection errs high) | Raise `files.spill` with the volume, list fewer columns, or filter the rows |
| A pack is refused: "cycle 1 of run R-… is not a census: side a's files hold 3 line(s) that were not rows" | Side a's CSV held lines of another shape (a trailer record, a broken line) | Fix the export, or strip the trailer, and run again: a key on such a line would be in no result |
| A run fails with "FilesChanged: side a: file 3 of 12 is not the file the run was created on (content)" (or `size`, `modified`, `changed`, `replaced`, `link`, or "13 files match now, 12 when the run was created") | The export changed after the run was created | One run reads one export: create a new run on the new files. `replaced` on every run: a mount without stable inodes |
| A run fails with "RowsMoved: side a: 9,999,998 rows were read where the plan counted 10,000,000" | A read of the files met other rows than the plan's count: the reader disagreed with itself | Report it with `ds target check ID --table PATH --format FORMAT --count`'s output; no result of the run is a wrong answer |
| A run fails with "TargetTempSpace: OutOfMemoryException" (or "TargetTempSpace: spill", "TargetTempSpace: MemoryError") | A sort met `files.memory` and `files.spill`, or the spill volume had too little free space (before a sort, or when the reader was built anew while the run read); `MemoryError`: an allocation of the reader's own was refused under its data limit | Free or grow the spill volume, raise `files.spill` or `files.memory` (restart), then create the run again |
| "side a: cannot connect to target …", when a run is created | A database target's DSN, network path or role is wrong; libpq's line says which | Fix the DSN secret (section 7) or the network, then start the run again |
| "the DSN of target … is not a valid connection string" | libpq cannot parse the DSN, or a URI's user or password holds an unencoded `@` | Fix the DSN secret; percent-encode the user and the password (section 7) |
| "side a: the role may not read …: grant it SELECT" when a run is created, or a run fails with `InsufficientPrivilege` | The database role cannot read a compared table or view | Grant SELECT on it (section 7) |
| "side a: table or view … not found or not visible" | A wrong `schema` or `name`, or the role lacks USAGE on the schema | Fix the spec, or grant USAGE (section 7) |
| "side a: cannot connect to target crm-legacy (oracle crm-db.internal:1521/CRMPDB as ds_reader): DPY-6001: the listener does not know the service", when a run is created or from `ds target check` | A MySQL or Oracle target's network path, TLS or credential; the code and its gloss say which: `no server answered` (the firewall, the host or the port), `the host name does not resolve`, `the listener does not know the service`, `the credential was refused`, `the account is locked`, `the password has expired`, `TLS: the server offers none`, `TLS: the server's certificate was not accepted` (also an Oracle certificate whose subject is not exactly `tls_server_dn`), `TLS: the handshake failed` (also TLS declared against a plain Oracle listener), `the answer is not a MySQL server's` (the port is another service's), `the server requires Native Network Encryption: ask for a TCPS listener`, `the account has only a 10G password verifier`, or the guard's `… is a private address: set allow_private_network on the target` | Fix what it names (section 7), then `ds target check ID` until it says `session ok` |
| "side a: table or view APP.BROKEN_V cannot be described: ORA-04063", when a run is created or from `ds target check`; "target crm-legacy: the session's account of its server could not be read: ORA-03113" | The database answered the catalogue's statements, or the statement that proves the grant, with an error after the session was open: a view left invalid by DDL on its table (ORA-04063), or a session the server ended. Only the code is shown | Look the code up with the database's owners (recompile the view), then start the run again |
| "side a: target ledger is MariaDB 11.4: MySQL 8.0.17 or later is supported, MariaDB is not" | The server is MariaDB, or a MySQL before 8.0.17 | Neither is supported |
| "side a: target crm-legacy is Oracle 12.2: 19c or later is supported" | The server is older than Oracle 19c | Not supported |
| "side a: the session could not be set (NLS_SORT is BINARY_CI): a logon trigger overrides it" | A logon trigger sets a session parameter DeltaSleuth sets and reads back | Exempt the DeltaSleuth account from the trigger |
| "side a: key column 'ACCT' is VARCHAR2(1000) in a WE8MSWIN1252 database: a scan orders text keys of up to 500 bytes there (compare a view that shortens it)" | In an Oracle database that is not AL32UTF8, a text key column declared over 500 is refused, for every run | Compare a view that shortens the key |
| "side a: the role may not execute UTL_I18N, which orders text keys in a WE8MSWIN1252 database: grant it EXECUTE" | The same databases order keys through `UTL_I18N` | `GRANT EXECUTE ON SYS.UTL_I18N` to the account (section 7) |
| A run `failed` with `SnapshotLost: ORA-01555` (or `MySQL 1412`) | Three passes in a row lost their snapshot before saving a checkpoint: on Oracle, undo does not cover the first pass | Size undo for the read, read a standby, or narrow the side with a filter; then start the run again |
| A run `failed` with `TargetTempSpace: ORA-01652` (or `MySQL 3`) | The server ran out of temporary space for the scan's sort (on MySQL, its temporary directory is full) | Add temporary space, or compare fewer columns; then start the run again |
| A run `failed` with `TargetDenied: ORA-00942`, or `TargetRefused: <code>` | The table or the grant is gone since the plan; or the server answered three passes in a row with a code DeltaSleuth does not class | Restore the grant, or look the code up with the database's owners; then start the run again |
| A run `failed` with `UnsafeTargetError: 10.1.2.3 is a private address: set allow_private_network on the target` | The target's name now resolves to an address the guard refuses | Fix the name's records, or set the opt-in if the address is the right one |
| A run `failed` with `EngineChanged: side a: target ledger is mysql now; this run was planned for oracle` | The targets file changed a target's engine under a live run | Start a new run |

### Notifications

`ds notify check` prints one line per channel; `ds notify` and the Notifications screen show each
channel's counts and last error, and every screen shows the banners ([ALERTS.md](ALERTS.md) §10, §11).

| Symptom | Cause | Fix |
|---|---|---|
| "error: channel owners-teams: the URL in … is not an https URL for host …" | The URL file holds another host, or http | Paste the URL again; `host` must be its exact host |
| "error: channel owners-mail: smtp.bank.example resolves to 10.1.2.9, a private address: set allow_private_network to allow it" | An internal destination without the opt-in | Set `allow_private_network: true` on the channel (never with `proxy: true`) |
| "error: channel …: host … does not resolve" | DNS, or a typo | Fix the name, or send through `notify.proxy` |
| "error: notify.proxy … resolves to 169.254.…: never a notification destination" | The proxy name points at a link-local or metadata address | Fix the name |
| "error: channel ops-hook: the signing key in … is not whsec_ and base64 of 24 to 64 bytes", or "cannot read secret file …" | A malformed or unmounted secret | Write it as in section 3, mount it, `docker compose up -d` |
| "warning: notify channel …: …; its deliveries retry until it is readable", or the banner "Notifications to … cannot be sent: a secret file cannot be read" | As above, found at start or by a send | Fix the secret; deliveries retry, up to `max_attempts` |
| The banner "Notifications to … are failing: 3 not delivered since …", and `notify.dead` | The receiver refused (`http_4xx`, `smtp_permanent`, `url_mismatch`, `unsafe_destination`) or kept failing (`retries_exhausted`) | `ds notify deliveries --channel … --state dead` shows the codes; fix the receiver, then Send failed again (or `ds notify retry`) |
| The banner "The notifier is behind: audit rows since … are not yet notified; is the worker running?" | No worker runs (the notifier is the worker's), or the database is slow | Start the app; a backlog older than `max_age_hours` is skipped in one `notify.skipped` |
| "error: notifications are off: add notify to deltasleuth.yaml and restart (docs/mvp/ALERTS.md §8)" | The settings have no `notify` | Section 3, Notifications |
| A test answers `http_4xx (HTTP 401)` | The receiver does not verify the signature: another key, or its clock | Give it the key file's value; NTP on both sides |

### Table pre-checks and the schema guard

A pre-check's pair lines, and a run's schema guard lines, are the server's words ([PRECHECKS.md](PRECHECKS.md)
§10); the [runbook](RUNBOOK.md#when-a-table-changes-under-a-run) says what each schema guard line asks of
you.

| Symptom | Cause | Fix |
|---|---|---|
| The banner "no worker has taken this pre-check for 10 min: start ds worker, or ds serve" | No worker runs (one that stopped set the pairs it was reading back to pending; a pair of one that was killed counts as read by none once its claim is three lease periods old), or one is busy reading a long pair of another pre-check of the same target | Start the app or `ds worker`; `ds precheck run` without `--no-wait` reads beside itself |
| "this installation holds 20 unfinished table pre-checks, the most it takes: cancel one or wait" (409) | 20 pre-checks are waiting or being read | Cancel one (`ds precheck cancel PC-…`) or wait |
| A pair reads "not checked: side a's figures timed out after 900 s (raise precheck.statement_timeout_s, or pre-check a filtered view)" | The aggregate statement ran longer than `precheck.statement_timeout_s` | Raise it in `deltasleuth.yaml` and restart, pre-check a filtered view, or read a replica |
| A pair reads "distinct values not counted: side a ran out of temporary space (53400)", or "duplicate keys not counted: …" | The server's temporary space (`temp_file_limit`, its temporary directory, TEMP) is too small for the sort | Pre-check at level `columns`, lower `precheck.distinct_max_rows`, or give the server more temporary space |
| A pair reads "not checked: side a refused the credential (ORA-01017); no other pair of target crm-legacy was tried" | The target's password is stale; every pending pair of that target was settled at once, so the account is not locked by retries | Fix the secret, then make a new pre-check |
| A pair reads "not checked: side a could not be reached (…)" | The target was unreachable twice, a lease period apart | Check the network or the target, then make a new pre-check |
| A pair reads "a run of this spec would be refused: …" | The plan a run would make refuses the pair: the message is the one `POST /v1/runs` would answer | Fix what it names; `ds target check ID --table NAME` shows the columns |
| A pair reads "not checked: spec 12 (…) declares a filter and is not approved: only its schema was read" | Under SSO an unapproved version's filter reaches no target | Have the version approved, then pre-check it again |
| A run is `paused` with `run_paused_schema_drift` | A table the run reads changed: its lines say how, and what to do | The runbook's "When a table changes under a run" |
| Resuming answers 409 "run R-… cannot resume: side b: column 'amount' of public.customers is gone, …" | A change that breaks the run's plan still stands | Undo the change and resume, or cancel the run and start a new one, which plans the table as it is now |
| Resuming answers 409 "run R-… cannot resume: side a's table could not be checked now (…): try again once the target answers" | The schema guard could not look at the table | Resume once the target answers |
| Starting a run answers 422 "spec 12 (…) pauses on schema drift and a run of it would not compare what run R-… compared: …" | Under `schemaDrift: {mode: pause}`, a new run would compare less than the version's last baseline run (while none is one, its newest stored plan) | Upload a new version that compares the tables as they are, or restore the column |

### Schedules

The Schedules screen and `ds schedules` say why a time started no run, in each firing's text
([SCHEDULES.md](SCHEDULES.md) §9).

| Symptom | Cause | Fix |
|---|---|---|
| The banner "schedule … was due at … and is not settled yet: a worker may be retrying or starting it, or none is running (ds worker, or ds serve)" | No worker that fires runs (`ds serve --no-worker` without a `ds worker`; `ds run` or `ds keys migrate` holding the worker lock), or it is retrying a run that could not be created | Start the app or the worker; the time is then started if its window is open, else recorded as missed |
| "not started: its time … passed … before a worker could start it", or "not started: 3 times, … passed while no worker could start them: a schedule never runs a backlog" | The worker or the store was down past the window, or the clock moved forward | Start a run by hand if the firing is needed; widen `start_within` (up to 1440 minutes) if outages of that length are expected |
| "not started: the schedule was saved in tokens mode and this worker runs under SSO: …" | The auth mode changed since the schedule was saved | An operator saves it again (Edit and Save, or `ds schedules set NAME`) in the mode the worker runs in |
| "not started: who saved it is disabled or no longer an operator …" | The person who last saved it left, lost `operator`, or owns the token that saved it and is disabled | Another operator saves the schedule again; `ds schedules --set-by user:U-…` lists a leaver's |
| "not started: spec 12 lacked the four-eyes approval it needs (see Approvals)" | Under SSO the version is unapproved or its approval was revoked | Have it approved: the next due time starts with no other act |
| "not started: the licence refused the run (expired): see Licence" | A vendor build's licence refuses new runs. This build misses no time for a licence: it shows this line, or "not started: 1 more time since … for the same reason", only on a schedule whose times an older build that enforced the licence refused, until that schedule's next due time is settled; and the range of those times that build held is recorded once, as one `schedule.missed` row with `reason: licence`, by the worker then or once the range's first time is 24 h old, or at a person's save, pause or delete of the schedule before that. A `schedule.missed` rule sends that row as a warning | In a vendor build, install the renewal; the next due time runs, with no backlog. In this build nothing: the licence refuses no later time |
| "not started: the run could not be created (…): starting one on the Runs screen shows the cause" | A key listing, a table side or the store could not be read in three tries, five minutes apart, or in as many as the window had room for | Start a run of the spec by hand to see the refusal; fix the target or the spec |
| "not started: its time … passed its 60 min while the worker was still starting other schedules (two at a time)" | The time waited in its window for one of the worker's two firing slots, behind schedules whose key listings outlasted it | Spread the schedules over other minutes, or widen `start_within` |
| "not started, and paused by the worker: the worker's time zone data (…) did not hold '…': …", or the banner "schedule … is paused by the worker: its time zone or cron did not load in the worker's build: …" | The image's tz data no longer holds the zone, or cannot read the cron | Save the schedule with a zone the build holds (the form lists them), then resume it |
| "error: 'Asia/Calcutta' is not a time zone this build uses (tzdata …): use Asia/Kolkata" | A renamed zone | Use the name it gives |
| `ds schedules next` on the host and the screen disagree | The host's `ds` reads the host's tz data | Run it in the image: `docker compose run --rm app ds schedules next …` |

### The MCP server

A tool's refusal is a text the agent reads, in its result's `error`; the route's refusals are JSON
`{"detail": …}` ([MCP.md](MCP.md) §11 lists every message). `ds mcp check` prints what the settings
serve, without the database.

| Symptom | Cause | Fix |
|---|---|---|
| 404 "MCP over HTTP is off (mcp.http, docs/mvp/MCP.md §4)" | The settings have no `mcp` block, or its `http` is false | Section 3, The MCP server; then `docker compose restart app` |
| "error: MCP is off: the settings have no mcp block (docs/mvp/MCP.md §4)", from `ds mcp` or `ds mcp check` | The settings have no `mcp` block | Section 3, The MCP server |
| "error: invalid settings …: mcp.data_classes: Field required" (or "mcp: Value error, work names compare_key, whose results are production_tokenised: add it to data_classes", "mcp: Value error, production_clear_declared widens production_tokenised: add it to data_classes") | The block lacks `data_classes`, which has no default, or lists `compare_key` or `production_clear_declared` without `production_tokenised` | Fix `deltasleuth.yaml` |
| 401 "invalid or expired API token" | The agent's token expired (90 days unless it was created with other `days`) or was revoked. Nothing alerts on it: a failed token is a line in the app's log with the token id, not an audit row | Create a token under the same name and put it in the agent's token file or the editor's secret store; then revoke the old one |
| The client reports an OAuth or authorisation-server error after connecting | The server answered 401 (no token, or one that is expired or revoked) and the client then looked for OAuth metadata under `/.well-known/`, which answers a JSON 404: DeltaSleuth has no OAuth for MCP | Fix the token: the acceptance checks of section 5 (items 27 and 29) show what the server answers it |
| 403 "MCP takes an API token (Authorization: Bearer dst_…), not a browser session" | The request came with a browser's session cookie | Give the client a token of its own |
| 403 "cross-origin request refused" (or "cross-site request refused") | The client sent the `Origin` or `Sec-Fetch-Site` of another site: it is a page in a browser, or sets an `Origin` of its own | Use a client that is not a browser page (Claude Code, VS Code, Cursor), or `ds mcp` over stdio on a workstation in developer mode. No CORS header is sent, by design |
| 400 "unexpected Host header" | The client's URL names the host otherwise than `public_url` does | Use the `public_url` host, or list the other name in `auth.allowed_hosts` and restart |
| 405 "MCP is served by POST only: no stream and no session" | The client opened a stream (GET) or ended a session (DELETE): the server has neither | Nothing, if the client then works over POST alone; one that needs a stream cannot use this server |
| 504 from the proxy during `compare_key` | The compare waited on a target longer than the proxy's read timeout (nginx: 60 s unless `proxy_read_timeout` says otherwise). The compare itself was not cut short: its one-shot run ended, and its `compare_one` and `run.finished` rows are in the audit log | Raise `proxy_read_timeout` (section 5), or see why the target is slow |
| A tool is missing from the agent's list, or a call answers "… is not served by this installation (mcp.data_classes lacks …)" or "(mcp.work)", or "… needs the operator role" | The settings do not serve the tool, or the token's role is below it | `ds mcp check` prints what a viewer and an operator are offered and each tool not served with its reason |
| "run R-… is not served by this installation: its spec declares clear values or a business-code key (mcp.data_classes lacks production_clear_declared)" | With `production_tokenised` alone, only runs whose spec declares nothing clear are served | Read such a run in the UI. `production_clear_declared` serves it over MCP too: the installation's written decision that those values may reach the agent's model host (section 3) |
| "rate limited: at most 60 MCP tool calls a minute per caller" (or "at most 10 compare_key calls a minute per caller", "… get_readiness …"), "busy: 4 MCP tool calls are running; call again" | The caller passed `mcp.calls_per_minute`, or the fixed 10 a minute of `compare_key` and `get_readiness`; or `mcp.concurrency` calls were running for 10 s | The agent calls again later; raise the setting and restart if the load is wanted |
| "the result is over 65536 bytes: ask for fewer rows (limit)" | A list's page does not fit one result | The agent asks with a smaller `limit` |
| "refused: request Q-… names the reason in the server log", or "internal error: request Q-… names it in the server log" | A refusal whose text may name an endpoint, or a fault: neither text is sent to an agent | `docker compose logs app` has the line with that request id |
| A client that starts `ds mcp` through `docker compose run` reports malformed messages or never connects | Compose gave the command a TTY, which corrupts the protocol's framing. Or Compose was not started in this folder, where `.env` names both compose files: with `--project-directory` from another directory Compose looks for the files `.env` names under that directory and ends `stat …: no such file or directory`, and from this folder it ends `bind source path does not exist: …/secrets/db_dsn`, since that flag moves the base of the compose file's relative paths | The runbook's host-channel line as it stands: `sh -c "cd /srv/deltasleuth && exec docker compose run --rm -T …"`, with `-T`, and never `-f`, an exported `COMPOSE_FILE` or `--project-directory` |
| "error: ds mcp under auth is the host-administrator channel: use /v1/mcp with a named token, or pass --host-channel (docs/mvp/MCP.md §5.2)" | `ds mcp` ran where the settings have `auth` | An agent calls `POST /v1/mcp` with a token; `--host-channel` is for a host administrator (section 9) |
| `ds mcp` exits 2 with "error: Propagator b3 not found. It is either misspelled or not installed.", or 3 after a traceback ending `StopIteration`; with `mcp.http`, the app (`ds serve`) ends the same way, before it opens the database: no try leaves an audit row, and it restarts in a loop | The container's environment sets `OTEL_PROPAGATORS` or `OTEL_PYTHON_TRACER_PROVIDER` to a name the image holds nothing for. The MCP library loads the OpenTelemetry API, which reads those names (and `OTEL_PYTHON_CONTEXT`) as it is imported; DeltaSleuth opens no span and ships no exporter | Remove the variable from the site file's `environment` and `docker compose up -d`. The release's compose files set none |
| An agent must be stopped now | | `ds token revoke T-…` (or Admin, Tokens): its next request gets 401. Every agent at once: `http: false`, or remove the `mcp` block, and `docker compose restart app` |

### TLS

**Symptom:** "certificate verify failed" for an internal endpoint or the model gateway, or
"discovery unreachable (ClientConnectorCertificateError)" for the IdP, in a failed sign-in's log line
or from `ds auth check`.

**Cause:** the host uses a private CA. The endpoint, LLM and IdP clients trust the image's system CA
store.

**Fix:** put your CA in `deploy/ca/office-root-ca.crt`. After each new image, build a layer that adds
it, labelled with the id of the image it adds to, then run that layer through the CA override:

```sh
V=X.Y.Z                          # the version the compose file runs (pyproject.toml's)
BASE=deltasleuth:$V              # a source build: <project>:source (deltasleuth:source by default)
docker build -t "deltasleuth:$V-ca" --label org.deltasleuth.base="$(docker image inspect --format '{{.Id}}' "$BASE")" -f- deploy/ca <<EOF
FROM $BASE
USER root
COPY office-root-ca.crt /usr/local/share/ca-certificates/
RUN update-ca-certificates
USER 10001:10001
EOF
printf '%s\n' 'COMPOSE_FILE=deploy/docker-compose.yml:deploy/docker-compose.site.yml:deploy/docker-compose.ca.yml' > .env
docker compose up -d
```

`BASE` keeps its tag and id. A source build leaves the source override out of `.env` while the CA
layer runs: after taking new source, build with `docker build -f deploy/Dockerfile -t <project>:source .`,
then build the layer again. The layer's own tag, `deltasleuth:<version>-ca`, carries no project name: the
installations of one machine share it, so a machine with two builds a layer for one of them only. Where the image is of another platform than the host (the amd64 release
under emulation on Apple silicon), add `--platform linux/amd64` to the `docker build` of the layer:
without it the build looks for a `deltasleuth:X.Y.Z` of the host's platform, finds none loaded, asks
Docker Hub for one and fails (`pull access denied, repository does not exist …`).

### Chat and the LLM

| Symptom | Cause | Fix |
|---|---|---|
| Chat returns 503 "AI disabled" | No `llm_profiles_file` in the settings | Section 8 |
| An LLM call is denied: `environment_not_allowed`, `external_environment`, `external_class` or `class_not_allowed` | The profile's `environments` or `data_classes` forbid the call, or the host is not listed as internal | Fix the profile. List a host as `internal` only if it is your own gateway. To test the chat with Claude or OpenAI, see "Testing the chat with Claude or OpenAI" in section 8; `prod` never allows an external host. |
| Every call to the gateway fails with 400 | The gateway rejects optional OpenAI fields | Add `wire: {max_tokens_param: max_tokens, send_store: false, send_parallel_tool_calls: false}` to the profile, then probe again |
| The probe says a declared capability is `fail` or `unsupported` | The gateway lacks it | Remove it from the profile |
