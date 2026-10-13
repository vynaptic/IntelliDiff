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

The script creates what is missing. It changes, overwrites and deletes no existing file: run again, it prints `kept` in place of `created`. It creates:

- the settings `deploy/config/deltasleuth.yaml`, `targets.yaml` and `llm_profiles.yaml`, copied from their `*.example.yaml` files, the site file `deploy/docker-compose.site.yml`, and `.env`;
- four secrets in `deploy/secrets/`: `db_password`, `db_dsn`, `kek` and `tokeniser`;
- the image `deltasleuth:source`, built from the checkout, the database, and the containers `db`, `app` and `mocks`;
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
- Update. Fetch the newer source, then run the script again: it rebuilds the image and restarts the containers on it. Files, data and the token stay.
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
   - Roles, lowest first, each holding the ones below it: `viewer` (read), `operator` (start runs), `spec_author` (upload specs), `rule_approver` (approve), `pii_reader` and `admin`. `auth.oidc.roles` maps each role to group ids of the identity provider, matched exactly. A token holds one of the first three only.
3. **Create the secrets.** A secret is one file in `deploy/secrets/` that holds one value on one line. Compose mounts it read-only at `/run/secrets/<name>`. The settings name a secret only as a `file:` reference: no credential is ever written in a settings file. Paste this block whole.
   ```bash
   mkdir -p deploy/secrets && chmod 700 deploy/secrets
   (umask 077
    openssl rand -hex 24 > deploy/secrets/db_password
    { printf 'postgresql://deltasleuth:'; tr -d '\n' < deploy/secrets/db_password; printf '@db:5432/deltasleuth'; } > deploy/secrets/db_dsn
    openssl rand -hex 32 > deploy/secrets/kek
    openssl rand -hex 32 > deploy/secrets/tokeniser)
   chmod 444 deploy/secrets/*
   ```
   Keep a copy of `kek` and `tokeniser` in a vault, never beside a backup: they open the stored data, and a lost key cannot be made again. A further secret, such as the OIDC client secret or a target's credential, takes four actions. Write `deploy/secrets/<name>` and `chmod 444` it. In the site file, add `<name>` to `services.app.secrets` and the line `<name>: {file: ./secrets/<name>}` under the top-level `secrets:`. Reference it as `file:/run/secrets/<name>`. Apply it with `docker compose up -d`.
4. **The database** needs no further setting. The `db` service is PostgreSQL 16, with a user and a database both named `deltasleuth`. It takes `db_password` when its volume is first created, and the app connects with `db_dsn`. `ds migrate` creates the schema `ds`, and every later start applies pending migrations by itself.
5. **Declare the targets**, `deploy/config/targets.yaml`. It is the allow-list: DeltaSleuth calls only the targets declared there or in the product. Replace the example entries with real ones, and delete the four mock targets. [The example](../deploy/config/targets.example.yaml) shows each kind.
   - A service: `base_url`, `auth` (`kind: none`, `bearer` or `header`, with a `secret_ref`), and the agreed load as `rate_per_s` and `max_concurrency`.
   - A database: `dsn_ref` for PostgreSQL; `engine: mysql` or `engine: oracle` with `host`, `port`, `user`, `password_ref` and `tls_ca_ref`.
   - The private-network opt-in: a loopback, private or link-local address is refused unless the target sets `allow_private_network: true`. A service on the host itself is `host.docker.internal`.
   - In the product: on the Targets screen, "Declare an endpoint" adds a service without a restart. Under OIDC, an `admin` who signed in within the last 15 minutes may do so; in a trial, a `spec_author` token may; with tokens at an `https` address nobody may. For an id declared in both places, the file wins.
6. **Optional: name the model endpoint for AI chat.** Without it AI is off and everything else works. Copy `deploy/config/llm_profiles.example.yaml` to `deploy/config/llm_profiles.yaml`. Under `hosts`, list the endpoint's host as `internal`: only such a host serves the chat. In the profile `gateway`, set `base_url` and `model`; its `auth_ref` names the secret `llm_key` (step 3). In the settings, switch on `llm_profiles_file`, `llm_profile` and `llm_environment`. After the build of step 7, this command probes the profile.
   ```bash
   docker compose run --rm app ds llm-probe --profile gateway
   ```
7. **Build, initialise and start**, with these four commands in order. With OIDC, run `docker compose run --rm --no-deps app ds auth check` before the last one: it checks the settings against the identity provider and prints the two redirect URIs to register there.
   ```bash
   docker compose build
   ```
   ```bash
   docker compose run --rm app ds init
   ```
   ```bash
   docker compose run --rm app ds migrate
   ```
   ```bash
   docker compose up -d
   ```
8. **Create an API token** for each script and, with token sign-in, for each person. `<role>` is `viewer`, `operator` or `spec_author`. The command prints the token once, as its only line of standard output, so the file is its one copy. The `ds` alias of section 2 works here too.
   ```bash
   (umask 077; docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds token create --name <name> --owner <owner> --role <role> > <file>)
   ```
9. **Put a TLS reverse proxy in front.** The app's port is published on the host's loopback only, `127.0.0.1:8000`. A proxy on the host, such as nginx, terminates TLS for the host name of `public_url` and passes every path to that port: DeltaSleuth cannot run under a path prefix. It must pass the `Host` header and set `X-Forwarded-For`, which the app reads only from `trusted_proxies`. In nginx, the `location /` block holds `proxy_pass http://127.0.0.1:8000;`, `proxy_set_header Host $host;` and `proxy_set_header X-Forwarded-For $remote_addr;`.
10. **Back up.** State lives in four Docker volumes: `pgdata` (the database), `evidence` (response bodies, encrypted), `workspace` (working files, and the targets declared in the product with their credentials) and `p0` (samples and specs for `ds` commands). A backup is a database dump, then a copy of the evidence volume, with a copy of `deploy/config`, the site file and `.env`. The keys `kek` and `tokeniser` never go into it. The app keeps running. Paste this block whole.
    ```bash
    mkdir -p -m 700 ~/deltasleuth-backups
    (umask 077; docker compose exec -T db pg_dump -U deltasleuth deltasleuth > ~/deltasleuth-backups/ds-$(date +%F).sql)
    (umask 077; docker compose run --rm --no-deps -T app tar -C /var/lib/deltasleuth/evidence --exclude=./spill -cf - . > ~/deltasleuth-backups/evidence-$(date +%F).tar)
    ```
11. **Restore** into an empty database, with the app stopped. On a new host, first put back the copies of `deploy/config`, the site file and `.env`, create the secrets as in step 3 but with the backup's `kek` and `tokeniser` in place of new ones, and build the image. `<date>` is the day in the backup's file names. The six commands, in order: start the database alone; load the dump, whole or not at all; put the evidence back; check that every stored body opens, which prints no `error:` line; pause every schedule, so that no run starts by surprise; start the app. Targets declared in the product are not in the backup: declare them again.
    ```bash
    docker compose up -d db
    ```
    ```bash
    docker compose exec -T db psql -U deltasleuth -d deltasleuth -q -v ON_ERROR_STOP=1 -1 < ~/deltasleuth-backups/ds-<date>.sql
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
12. **Update.** Take a backup first: it is the way back. Then fetch the newer source and rebuild. The app applies pending database migrations as it starts.
    ```bash
    git pull
    ```
    ```bash
    docker compose up -d --build
    ```
13. **Check health and read the logs.** The first command answers with the status, whether AI is on and the sign-in mode. The second shows the app's newest lines: a configuration problem stops the app with one line that starts `error:`. After a change to a settings file or to a secret's content, run `docker compose restart app`; after a change to the site file, run `docker compose up -d`.
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
| `cannot read secret file /run/secrets/<name>` | The secret is not mounted, or the container's user cannot read it | Name it in the site file and `chmod 444` it (section 3, step 3), then run `docker compose up -d` |
