# Deploying DeltaSleuth

DeltaSleuth runs under Docker Compose as two containers: `db`, a PostgreSQL 16 database, and `app`, which is `ds serve`: the API, the web UI and the worker in one process. This document covers a trial on one machine, a production deployment and a run from source. Using the product is in the [user guide](user-guide.md); the design is in [architecture.md](architecture.md).

## 1. Requirements

- git, OpenSSL, and Docker with Docker Compose version 2. A port or subnet other than the default needs Compose 2.24.4 or later.
- Network access for the first build. It fetches the images `python:3.13-slim`, `node:24-alpine` and `postgres:16-alpine`, and the Python and npm packages. Without that access, load or mirror them first.
- A checkout of this repository. Run every command of sections 2 and 3 from its folder, where Compose reads `.env`. Never export `COMPOSE_FILE` or `COMPOSE_PROJECT_NAME`: they override `.env`.

## 2. A trial on one machine

One command takes a fresh checkout to a running installation with two pairs of practice services, the mocks.

```bash
sh deploy/up.sh
```

| Option | Sets | Default |
|---|---|---|
| `--project NAME` | The Compose project name: lower-case letters and digits, joined by `-`, `_` or `__` | `deltasleuth` |
| `--port N` | The port on this machine, from 1024 to 65535 | `8000` |
| `--subnet A.B.C.0/24` | The subnet of the installation's Docker network | `172.30.0.0/24` |

The script creates what is missing and deletes no file: run again, it prints `kept` in place of `created`. On an installation whose app still signs in as the database's superuser it changes two files once and prints `changed`: `db_dsn`, written anew for the app's own role, and the settings, given the line `migrate_dsn_ref`. It creates:

- the settings `deploy/config/deltasleuth.yaml`, `targets.yaml` and `llm_profiles.yaml`, copied from their `*.example.yaml` files, the site file `deploy/docker-compose.site.yml`, and `.env`;
- five secrets in `deploy/secrets/`: `db_password`, the database superuser's; `db_dsn` and `db_owner_dsn`, the addresses of the app's own role and of the schema's owner, each with a password of its own; `kek` and `tokeniser`;
- the image `deltasleuth:source`, built from the checkout, the database with its two roles, and the containers `db`, `migrate` (run to its end before each start of the app), `app` and `mocks`;
- a first API token in the file `.deltasleuth/tokens/first-run`, with the role `spec_author`. It is never printed, and it expires at most 90 days after it was made.

Then sign in:

1. Open `http://localhost:8000` in a browser on the same machine, typed exactly so.
2. Copy the token without showing it. On Linux, `xclip -selection clipboard` or `wl-copy` replaces `pbcopy`.
   ```bash
   tr -d '\n' < .deltasleuth/tokens/first-run | pbcopy
   ```
3. Paste it into "API token" and press "Sign in". Then empty the clipboard. On Linux, `xclip -selection clipboard < /dev/null` or `wl-copy --clear` does it.
   ```bash
   pbcopy < /dev/null
   ```
4. Define `ds`, the command line, in each terminal that uses it. It runs in a short-lived container. `DS_OPERATOR` is the name the audit log records, and `ds --help` lists every command.
   ```bash
   alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
   ```

Later, in the same folder:

- Stop. Data, tokens and settings are kept.
  ```bash
  docker compose stop
  ```
- Start again, with the mocks.
  ```bash
  docker compose --profile mocks up -d
  ```
- Update. Fetch the newer source, then run the script again: it rebuilds the image, stops the app, has the `migrate` service apply the new migrations as the schema's owner (first moving an installation whose app signed in as the superuser to two roles), and starts the containers on the new image. Files, data and the token stay.
  ```bash
  git pull
  ```
  ```bash
  sh deploy/up.sh
  ```
- Remove. The first command deletes the containers, the network and all data, and cannot be undone. The second deletes the image. Then delete the checkout folder, with its settings, secrets and token.
  ```bash
  docker compose down -v
  ```
  ```bash
  docker image rm deltasleuth:source
  ```
- Add a second installation on the same machine. It lives in a checkout of its own, with its own project name, port and subnet, and its image is `<project>:source`. When another program merely holds port 8000, `--port N` alone is enough.
  ```bash
  sh deploy/up.sh --project ds2 --port 18000 --subnet 172.31.7.0/24
  ```

## 3. A production deployment

`deploy/up.sh` sets up a trial only. A production deployment is set up by hand, in a fresh checkout on the host: people reach it at an `https` address through a TLS reverse proxy, and no mock service runs. Proxy variables of the environment are ignored, so the host must reach every target and the model endpoint directly.

**A signed release in place of a build.** No signed release exists yet, so the steps below build the image from the checkout. Once the maintainer publishes signed bundles: verify the bundle as [SECURITY.md](../SECURITY.md) section 7 says, load its image with `docker load`, name only the first two Compose files in `.env` (`COMPOSE_FILE=deploy/docker-compose.yml:deploy/docker-compose.site.yml`), and leave out `docker compose build` in step 7. Compose then runs the loaded image `deltasleuth:<version>` and never pulls one.

1. **Create the working files.** Git ignores the four copies, so an update never overwrites them. The site file holds this installation's Compose changes: never edit `deploy/docker-compose.yml`, which an update replaces. `.env` names the Compose files, among them the one that builds the image from the checkout. Paste this block whole.
   ```bash
   cp deploy/config/deltasleuth.example.yaml deploy/config/deltasleuth.yaml
   cp deploy/config/targets.example.yaml deploy/config/targets.yaml
   cp deploy/docker-compose.site.example.yml deploy/docker-compose.site.yml
   cp deploy/env.source.example .env
   ```
2. **Edit the settings**, `deploy/config/deltasleuth.yaml`. A line that starts `# ` is a setting switched off: remove those two characters to switch it on. A mistyped setting is an error, never ignored. The other lines work as copied.
   - `auth.public_url`: the address people type, exactly: `https://<host name>`, without a path.
   - `auth.trusted_proxies`: switch the line on. It names the gateway of the Compose network, `172.30.0.1/32`, through which a proxy on the host connects.
   - Sign-in with OIDC: switch on the `auth.oidc` lines and fill in `issuer`, `client_id`, the claims and `roles`. `client_secret_ref` names the secret `oidc_client_secret` (step 3).
   - Sign-in with tokens: leave `auth.oidc` off. Everyone then signs in with an API token (step 8). No token can hold `admin`, so administration is done with `ds` on the host.
   - `worker.judge_processes`: keys are judged in that many processes beside the app, each about 50 MB and holding no key (at most the host's CPUs; 0 judges in the app's own process). Unset, it is 2 on a host of four CPUs or more, 1 on three and 0 on fewer. It pays from answers of a few kB and costs nothing on small ones: measured on one 16-core host, 2.8 kB answers went from 128 to 218 keys a second with 2, and 28 kB answers from 23 to 60 with 4.
   - More workers: the app sweeps (cycle ends, scans, schedules) and processes keys; each `ds worker --slots-only` beside it, with the app's settings, secrets and volumes (one evidence folder for all), processes keys too, which pays where a run's per-key reads keep the app's CPU busy: measured on one 16-core host, a run of 50,000 differing keys between two tables of 5 million rows read 1,000 keys a second with one beside the app against 710 without, and took 94 s against 118 s. In the site file, add a service `worker` with the app's image, volumes and secrets, `command: [ds, worker, --slots-only]`, no ports and `healthcheck: {disable: true}`; then `docker compose up -d --scale worker=2`. The workers share each target's `rate_per_s` and `max_concurrency`, its Retry-After pauses and open breakers, and every run's stop conditions; `execution.concurrency` holds per worker. A killed one costs its keys in flight and drops out after 10 s.
   - Roles, lowest first, each holding the ones below it: `viewer` (read), `operator` (start runs), `spec_author` (upload specs), `rule_approver` (approve), `pii_reader` and `admin`. `auth.oidc.roles` maps each role to group ids of the identity provider, matched exactly. A token holds one of the first three only.
3. **Create the secrets.** A secret is one file in `deploy/secrets/` that holds one value on one line. Compose mounts it read-only at `/run/secrets/<name>`. The settings name a secret only as a `file:` reference: no credential is ever written in a settings file. Paste this block whole.
   ```bash
   mkdir -p deploy/secrets && chmod 700 deploy/secrets
   (umask 077
    openssl rand -hex 24 > deploy/secrets/db_password
    { printf 'postgresql://deltasleuth_owner:'; openssl rand -hex 24 | tr -d '\n'; printf '@db:5432/deltasleuth'; } > deploy/secrets/db_owner_dsn
    { printf 'postgresql://deltasleuth_app:'; openssl rand -hex 24 | tr -d '\n'; printf '@db:5432/deltasleuth'; } > deploy/secrets/db_dsn
    openssl rand -hex 32 > deploy/secrets/kek
    openssl rand -hex 32 > deploy/secrets/tokeniser)
   chmod 444 deploy/secrets/*
   ```
   Keep a copy of `kek` and `tokeniser` in a vault, never beside a backup: they open the stored data, and a lost key cannot be made again. A further secret, such as the OIDC client secret or a target's credential, takes four actions. Write `deploy/secrets/<name>` and `chmod 444` it. In the site file, add `<name>` to `services.app.secrets` and the line `<name>: {file: ./secrets/<name>}` under the top-level `secrets:`. Reference it as `file:/run/secrets/<name>`. Apply it with `docker compose up -d`.
4. **The database** needs no further setting. The `db` service is PostgreSQL 16, with a database `deltasleuth` whose superuser `deltasleuth` takes `db_password` when the volume is first created; the app never uses it. Before each start of the app the one-shot `migrate` service, which alone mounts `db_password` and `db_owner_dsn`, runs `ds db setup`: it makes the roles `deltasleuth_owner` (the schema's owner) and `deltasleuth_app` (the app's), each with its address's password, applies pending migrations as the owner, and grants the app's role only the data rights `ds db grants` prints, so whoever holds `db_dsn` cannot switch the audit log's triggers off; the app's start log and `ds audit verify` say `roles: separate`. With migrations pending it refuses while the app runs. Any other command stops with `the database needs migrations: run ds migrate` until then. The database takes 512 MB of shared buffers and 32 MB of work memory per sort, sized for a host of 4 GB or more; a site file's `db` `command` can change them.
5. **Declare the targets**, `deploy/config/targets.yaml`. It is the allow-list: DeltaSleuth calls only the targets declared there or in the product. Replace the example entries with real ones, and delete the four mock targets. [The example](../deploy/config/targets.example.yaml) shows each kind.
   - A service: `base_url`, `auth` (`kind: none`, `bearer` or `header`, with a `secret_ref`), and the agreed load as `rate_per_s` and `max_concurrency`.
   - A database: `dsn_ref` for PostgreSQL; `engine: mysql` or `engine: oracle` with `host`, `port`, `user`, `password_ref` and `tls_ca_ref`.
   - The private-network opt-in: loopback and private addresses require `allow_private_network: true`. Link-local and metadata addresses are always refused. A service on the host itself is `host.docker.internal`.
   - In the product: on the Targets screen, "Declare an endpoint" adds a service without a restart. Under OIDC, an `admin` who signed in within the last 15 minutes may do so; in a trial, a `spec_author` token may; with tokens at an `https` address nobody may. For an id declared in both places, the file wins.
6. **Optional: name the model endpoint for AI chat.** Without it AI is off and everything else works. Copy `deploy/config/llm_profiles.example.yaml` to `deploy/config/llm_profiles.yaml`. Under `hosts`, list the endpoint's host as `internal`: only such a host serves the chat. In the profile `gateway`, set `base_url` and `model`; its `auth_ref` names the secret `llm_key` (step 3). In the settings, switch on `llm_profiles_file`, `llm_profile` and `llm_environment`. For runs with clear values, business-code keys or historical member-name masking, a suitably approved endpoint requires `production_clear_declared` to the profile's `data_classes`. Approve `analyst_text` separately to allow questions typed by staff. After the build of step 7, this command probes the profile.
   ```bash
   docker compose run --rm app ds llm-probe --profile gateway
   ```
7. **Build, initialise and start**, with these three commands in order; `up -d` runs the `migrate` service before the app. With OIDC, run `docker compose run --rm --no-deps app ds auth check` before the last one: it checks the settings against the identity provider and prints the two redirect URIs to register there.
   ```bash
   docker compose build
   ```
   ```bash
   docker compose run --rm app ds init
   ```
   ```bash
   docker compose up -d
   ```
8. **Create an API token** for each script and, with token sign-in, for each person. `<role>` is `viewer`, `operator` or `spec_author`. The command prints the token once, as its only line of standard output, so the file is its one copy. The `ds` alias of section 2 works here too.
   ```bash
   (umask 077; docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds token create --name <name> --owner <owner> --role <role> > <file>)
   ```
9. **Put a TLS reverse proxy in front.** The app's port is published on the host's loopback only, `127.0.0.1:8000`. A proxy on the host, such as nginx, terminates TLS for the host name of `public_url` and passes every path to that port: DeltaSleuth cannot run under a path prefix. It must pass the `Host` header and set `X-Forwarded-For`, which the app reads only from `trusted_proxies`. In nginx, the `location /` block holds `proxy_pass http://127.0.0.1:8000;`, `proxy_set_header Host $host;` and `proxy_set_header X-Forwarded-For $remote_addr;`.
10. **Back up.** Size a fixed-key soak by keys per cycle × cycles per day × retained days. Measure actual database and evidence growth across several cycles with rechecks. Table scans count equal rows in bulk. `retention.standard_days` defaults to 90 and can be reduced to 7. Holds extend retention. State lives in four Docker volumes: `pgdata` (the database), `evidence` (response bodies, encrypted), `workspace` (working files, and the targets declared in the product with their credentials) and `p0` (samples and specs for `ds` commands). A backup is a database dump, then a copy of the evidence volume, with a copy of `deploy/config`, the site file and `.env`. Keep `kek` and `tokeniser` in the site vault. Stop any separate worker. While the app is stopped, have the site's private backup capture the workspace volume's `targets/` directory, if present, including `targets.json` and its owner-only `secrets/`. Restart writers only after the dump, evidence and target backups succeed.
    ```bash
    (set -eC; umask 077; day=$(date +%F); mkdir -p -m 700 ~/deltasleuth-backups
     test ! -e ~/deltasleuth-backups/ds-"$day".sql && test ! -e ~/deltasleuth-backups/evidence-"$day".tar || exit 1
     docker compose stop app
     docker compose exec -T db pg_dump -U deltasleuth_owner deltasleuth > ~/deltasleuth-backups/ds-"$day".sql
     docker compose run --rm --no-deps -T app tar -C /var/lib/deltasleuth/evidence --exclude=./spill -cf - . > ~/deltasleuth-backups/evidence-"$day".tar
    )
    ```
    After confirming all backups, run `docker compose start app` and restart any separate worker.
11. **Restore** into an empty database, with the app stopped. On a new host, first put back the copies of `deploy/config`, the site file and `.env`, create the secrets as in step 3 but with the backup's `kek` and `tokeniser` in place of new ones, and build the image. `<date>` is the day in the backup's file names. The six commands, in order: start the database and make its two roles, without a schema; load the dump as the schema's owner, whole or not at all, its grants with it; put the evidence back; check that every stored body opens: the `blobs:` line reads `0 missing, 0 corrupted, 0 unreadable` and no line starts with `error:`; pause every schedule, so that no run starts by surprise; start the app. Before any service that reads targets starts, restore the vault's `targets/` subtree to the workspace, owned by UID 10001, with directories mode 0700 and files mode 0600. A dump taken before the roles were apart loads with `-U deltasleuth`; `migrate` then hands its schema to the owner. On the host the backup came from, first stop the app, empty the database and move the audit anchor aside, which names rows the backup may lack:
    ```bash
    docker compose stop app && docker compose exec -T db dropdb -U deltasleuth deltasleuth && docker compose exec -T db createdb -U deltasleuth deltasleuth && docker compose run --rm --no-deps app sh -c 'cd /var/lib/deltasleuth/workspace && [ ! -e audit-anchor.json ] || mv audit-anchor.json audit-anchor.before-restore.json'
    ```
    ```bash
    docker compose run --rm migrate --roles-only
    ```
    ```bash
    docker compose exec -T db psql -U deltasleuth_owner -d deltasleuth -q -v ON_ERROR_STOP=1 -1 < ~/deltasleuth-backups/ds-<date>.sql
    ```
    ```bash
    docker compose run --rm --no-deps -T app tar -C /var/lib/deltasleuth/evidence -xf - < ~/deltasleuth-backups/evidence-<date>.tar
    ```
    ```bash
    docker compose run --rm app ds keys status --verify
    ```
    ```bash
    docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds schedules pause --all
    ```
    ```bash
    docker compose up -d
    ```
12. **Update.** Take a backup first: it is the way back. Then fetch the newer source, rebuild, stop the app and start it: the `migrate` service applies pending migrations as the schema's owner before the app starts, and the app refuses a database a newer build migrated or whose applied migrations this build changed beyond comments and layout. To go back, check out the older source, rebuild its image, and restore the backup as in step 11. Under single sign-on, a stored spec whose side has a `map`, or a `select` of part of the answer, needs a new approval that covers `transforms` before its next run or resume.
    ```bash
    git pull
    ```
    ```bash
    docker compose build && docker compose stop app && docker compose up -d
    ```
13. **Check health and read the logs.** The first command answers with the status, whether AI is on, the sign-in mode and the worker's last sweep (its time and age in seconds); a server that runs its own worker answers 503 once no sweep has run for 30 s, or three polls if longer. The second shows the app's newest lines: a configuration problem stops the app with one line that starts `error:`. Each container's log rotates at 10 MB, three files kept, and a stop gives the app 40 s to hand back its work. An API read whose statement takes longer than `api.read_timeout_s` (default 30) is cancelled and answers 503, so it never holds a connection the worker needs. Each log line has its time, level and logger; audit rows are JSON lines on standard output; a line `audit anchor {…}` gives the audit chain's head, at most once a minute: keep a copy off the host for `ds audit verify --anchor <file>`. A failed run's error names its type and where in the code it was raised, never its message, and a 500's log line names the request id its response carried. A line `<job> job failed: <type> at <file:line>` names a worker job that met a fault; the sweep and the slots go on. After a change to a settings file or to a secret's content, run `docker compose restart app`; after a change to the site file, run `docker compose up -d`.
    ```bash
    curl -fsS http://127.0.0.1:8000/v1/health
    ```
    ```bash
    docker compose logs --tail 40 app
    ```

## 4. Run from source, without Docker

This is developer mode, for one person's machine: there is no sign-in, and whoever opens the address acts as `admin`. `ds serve` then listens on `127.0.0.1` only; never put a proxy or a port forward in front of it. It needs uv, Node 22.12 or later with npm, and an empty PostgreSQL 16 database that its user owns.

1. Install the Python part, build the web UI and copy the developer settings. Paste this block whole.
   ```bash
   uv sync
   npm --prefix web ci
   npm --prefix web run build
   mkdir -p -m 700 .deltasleuth
   cp deploy/config/deltasleuth.dev.example.yaml .deltasleuth/deltasleuth.yaml
   ```
2. Write the database's address, as one line, into the file `.deltasleuth/db_dsn`: `postgresql://deltasleuth:<password>@127.0.0.1:5432/deltasleuth`.
3. Name the settings file, in every terminal that runs `ds`.
   ```bash
   export DS_CONFIG="$PWD/.deltasleuth/deltasleuth.yaml"
   ```
4. Create the workspace with its keys, create the schema, and start the server. It stays in the foreground: open `http://127.0.0.1:8000`. In a second terminal, `uv run python -m mocks.run all` starts the practice services. Paste this block whole.
   ```bash
   uv run ds init
   uv run ds migrate
   uv run ds serve
   ```

## 5. If something goes wrong

| Message | Cause | What to do |
|---|---|---|
| `error: Docker is not running, or this account may not use it` | Docker is stopped, or the account may not run `docker` | Start Docker, or use an account that may run it |
| `error: port 8000 of this machine answers already`, or `is published by the container <name>` | Another program or container holds the port | Run the script with `--port N` and a free port. For `the subnet <subnet> is the Docker network <name>'s`, give `--subnet` a free one |
| `error: the app did not become healthy` | The app stopped as it started | The line above it is the app's last `error:` line. Correct what it names and run the script again |
| `cross-origin request refused` under "Sign in" | The page was opened at another address than `public_url`, such as `http://127.0.0.1:8000` | Open `public_url` exactly. `invalid or expired API token` means the paste was not an active token |
| `unexpected Host header` (status 400) | The proxy does not pass the host name of `public_url` | Pass the `Host` header (section 3, step 9) |
| `the database needs migrations: run ds migrate` | This build has migrations the database lacks; only `ds serve`, `ds worker` and `ds migrate` apply them | Run `docker compose run --rm migrate`, or start the app |
| `migrations are pending and the application's role deltasleuth_app has 1 session(s) open` | `migrate` ran while the older app still ran | Run `docker compose stop app`, then `docker compose up -d` |
| `cannot read secret file /run/secrets/<name>` | The secret is not mounted, or the container's user cannot read it | Name it in the site file and `chmod 444` it (section 3, step 3), then run `docker compose up -d` |
