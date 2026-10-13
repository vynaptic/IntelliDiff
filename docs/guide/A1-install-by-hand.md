# A1. Appendix: install by hand

[Chapter 1](01-install-docker.md) installs DeltaSleuth with one command, `sh deploy/up.sh`. This
appendix is for the reader who wants to know what that script does, do it without the script, run a
second installation on the same machine, create more API tokens, change a setting, or look up a message.

**How this appendix was checked.** Every command was run on 2026-10-05, on macOS 27.0.1 (Apple
silicon) with Docker Desktop (Docker 29.1.3, Compose 2.40.3), curl 8.2.1 and OpenSSL 3.6.2. The outputs
are shortened; ids and dates will differ, and `<you>` stands for your user name. A step that could not
be run, or a message that was not seen in the words printed, is marked "Not verified here:". Not verified
here: anything on Linux.

## 1. What the script does

`deploy/up.sh` first checks, then creates. It creates only what is missing. It never changes,
overwrites or deletes a file, never prints a secret or a token, and runs no `docker pull`, `down` or
`rm`. Run again in a finished installation, it prints `kept` for every file and changes nothing.

Before it creates anything it refuses, with one line that starts `error:`, whatever is not a fresh
one-machine installation of its own folder. "If something goes wrong" lists those lines.

Then it takes six steps:

| Step | What it does | By hand |
|---|---|---|
| 1 | Copies the three settings files, the site file and `.env` from their examples | Sections 2.2 and 2.3 |
| 2 | Creates four secrets | Section 2.4 |
| 3 | Builds the image from the source | Section 2.5 |
| 4 | Creates the workspace and the database's tables | Section 2.6 |
| 5 | Starts the three containers and waits, at most three minutes, until the app is healthy | Section 2.7 |
| 6 | Creates the first API token and saves it in a file | Section 2.8 |

These are the files it creates. Git ignores all of them, so an update never overwrites them.

| File | What it is | Mode |
|---|---|---|
| `.env` | Tells Docker Compose which description files to read, and the project name of a second installation | 644 |
| `deploy/docker-compose.site.yml` | The site file: this installation's own changes to the Compose description | 644 |
| `deploy/config/deltasleuth.yaml` | The settings file: where things are and how people sign in | 644 |
| `deploy/config/targets.yaml` | The targets file: what DeltaSleuth is allowed to read, with the services declared in the product itself ([DEPLOY.md](../mvp/DEPLOY.md), section 7). As copied it holds the four mocks and eight examples | 644 |
| `deploy/config/llm_profiles.yaml` | The AI chat's connection details. AI chat is off as copied | 644 |
| `deploy/secrets/db_password` | The database's password | 444, in a folder of mode 700 |
| `deploy/secrets/db_dsn` | The address the app connects to the database with. It contains the same password | 444 |
| `deploy/secrets/kek` | The keyring: the key that protects every raw value DeltaSleuth stores | 444 |
| `deploy/secrets/tokeniser` | The tokeniser key: every masked value DeltaSleuth shows is derived from it | 444 |
| `.deltasleuth/tokens/first-run` | Your first API token | 600, in folders of mode 700 |

Docker holds the rest. These are the default names:

| Thing | Default |
|---|---|
| Compose project, the name Docker groups the installation under | `deltasleuth` |
| Containers | `deltasleuth-db-1` (PostgreSQL 16), `deltasleuth-app-1` (web pages, API and background worker), `deltasleuth-mocks-1` (the four mock services) |
| Image built from the source | `deltasleuth:source`, about 720 MB |
| Database image | `postgres:16-alpine` |
| Volumes, where Docker keeps the data | `deltasleuth_pgdata` (the database), `deltasleuth_evidence`, `deltasleuth_workspace`, `deltasleuth_p0` |
| Network | `deltasleuth_default`, `172.30.0.0/24`, gateway `172.30.0.1` |
| Web address | `http://localhost:8000`. The app's port 8000 is published on `127.0.0.1` only |
| Database | User `deltasleuth`, database `deltasleuth`, port 5432, reachable only by the containers |
| Mock services | `http://mocks:18081`, `18082`, `18091` and `18092`, reachable only by the containers |
| Inside the app container | Settings in `/etc/deltasleuth`, secrets in `/run/secrets`, data under `/var/lib/deltasleuth` |

## 2. Every step by hand

Do [chapter 1](01-install-docker.md), sections 1 and 2, first: check the tools, clone the source, and
`cd IntelliDiff`. Run every command below from that folder. You also need curl for the checks.

### 2.1 Check that nothing is in the way

1. Check that no installation uses the default project name.

   ```bash
   docker ps -a --filter label=com.docker.compose.project=deltasleuth
   ```

   You should see only the header line:

   ```text
   CONTAINER ID   IMAGE     COMMAND   CREATED   STATUS    PORTS     NAMES
   ```

   If containers are listed, an installation exists: go to section 3.

2. Check that no data is left from an earlier installation of that name.

   ```bash
   docker volume ls --filter label=com.docker.compose.project=deltasleuth
   ```

   You should see only the header line:

   ```text
   DRIVER    VOLUME NAME
   ```

   If volumes are listed and you do not have the `deploy/secrets` folder they were created with, their
   data cannot be opened: go to section 3 and choose another project name.

3. Check that port 8000 is free.

   ```bash
   curl -sS --noproxy '*' --max-time 3 -o /dev/null http://127.0.0.1:8000/
   ```

   A free port answers:

   ```text
   curl: (7) Failed to connect to 127.0.0.1 port 8000 after 0 ms: Couldn't connect to server
   ```

   If the command prints nothing, or another message, a program uses the port: go to section 3. A
   browser may read `localhost` as `127.0.0.1` or as `[::1]`, and the script asks both. Ask the second
   in the same way; `-g` lets curl take the brackets as they are:

   ```bash
   curl -sS -g --noproxy '*' --max-time 3 -o /dev/null 'http://[::1]:8000/'
   ```

   A free port answers `curl: (7) Failed to connect to ::1 port 8000 after 0 ms: Couldn't connect to
   server`.

4. List Docker's networks, each with its subnet, the range of addresses it uses.

   ```bash
   docker network inspect --format '{{.Name}}{{range .IPAM.Config}} {{.Subnet}}{{end}}' $(docker network ls -q)
   ```

   You should see no line with `172.30.0.0/24`. If a network has that subnet, as here on a machine
   that already held an installation, go to section 3:

   ```text
   bridge 172.17.0.0/16
   deltasleuth_default 172.30.0.0/24
   host
   none
   ```

### 2.2 The settings files

DeltaSleuth reads three settings files from `deploy/config/`. Each has an example beside it, and each
example works as copied: people sign in with an API token at `http://localhost:8000`, and AI chat and
every optional feature are off.

1. Copy the settings file.

   ```bash
   cp deploy/config/deltasleuth.example.yaml deploy/config/deltasleuth.yaml
   ```

2. Copy the targets file.

   ```bash
   cp deploy/config/targets.example.yaml deploy/config/targets.yaml
   ```

3. Copy the AI chat's file.

   ```bash
   cp deploy/config/llm_profiles.example.yaml deploy/config/llm_profiles.yaml
   ```

No password ever goes in these files: a line such as `file:/run/secrets/db_dsn` only names the file
that holds one. In the settings file, a line that starts with `# ` is a setting switched off, and a
line that starts with `##` is an explanation.

### 2.3 The site file and `.env`

Docker Compose builds its description of the installation from three files: the product's base file,
your site file, and the product's source override, which says "build the image from this folder". The
file `.env` names the three.

1. Copy the site file. As copied it changes nothing.

   ```bash
   cp deploy/docker-compose.site.example.yml deploy/docker-compose.site.yml
   ```

2. Copy `.env`.

   ```bash
   cp deploy/env.source.example .env
   ```

   For a second installation, go to section 3.3 now: it makes four edits, runs the check of step 3
   with you, and sends you on to section 2.4.

3. Ask Compose how it reads the files. This command starts nothing.

   ```bash
   docker compose --profile mocks config | grep -e '^name:' -e '^    image:' -e 'published:' -e 'subnet:' -e 'gateway:'
   ```

   You should see the default names:

   ```text
   name: deltasleuth
       image: deltasleuth:source
           published: "8000"
       image: postgres:16-alpine
       image: deltasleuth:source
           - subnet: 172.30.0.0/24
             gateway: 172.30.0.1
   ```

Never set the variables `COMPOSE_FILE` or `COMPOSE_PROJECT_NAME` in your shell: they would replace the
lines of `.env`.

### 2.4 The secrets

A secret is a password or a key. DeltaSleuth reads each secret from a file of its own that holds
exactly one value. The files live in `deploy/secrets/`, and Docker shows each one to the containers as
`/run/secrets/<name>`. None of the commands below prints a secret. In each, `umask 077` creates the
file readable by you alone.

1. Create the folder.

   ```bash
   mkdir -p deploy/secrets
   ```

2. Close the folder to every other account on this machine.

   ```bash
   chmod 700 deploy/secrets
   ```

3. Create the database password.

   ```bash
   (umask 077; openssl rand -hex 24 > deploy/secrets/db_password)
   ```

4. Create the database address from it. The password goes from one file into the other.

   ```bash
   (umask 077; { printf 'postgresql://deltasleuth:'; tr -d '\n' < deploy/secrets/db_password; printf '@db:5432/deltasleuth'; } > deploy/secrets/db_dsn)
   ```

5. Create the keyring.

   ```bash
   (umask 077; openssl rand -hex 32 > deploy/secrets/kek)
   ```

6. Create the tokeniser key.

   ```bash
   (umask 077; openssl rand -hex 32 > deploy/secrets/tokeniser)
   ```

7. Let the containers read the four files. The containers run under other user ids than yours, so
   each file must be readable by everyone; the folder's own mode keeps them private on the machine.

   ```bash
   chmod 444 deploy/secrets/db_password deploy/secrets/db_dsn deploy/secrets/kek deploy/secrets/tokeniser
   ```

8. Check the result.

   ```bash
   ls -l deploy/secrets
   ```

   You should see four files with these modes and sizes:

   ```text
   -r--r--r--@ 1 <you>  staff  93 Oct  5 15:53 db_dsn
   -r--r--r--@ 1 <you>  staff  49 Oct  5 15:53 db_password
   -r--r--r--@ 1 <you>  staff  65 Oct  5 15:53 kek
   -r--r--r--@ 1 <you>  staff  65 Oct  5 15:53 tokeniser
   ```

After the first start, create none of them again, with one exception. The database keeps the password
it was created with, and what `kek` and `tokeniser` protected opens only with them. The exception is
`db_dsn`: it holds nothing of its own, and "If something goes wrong" shows how to make it again. If
the data will matter, keep a copy of `kek` and `tokeniser` in a password manager.

### 2.5 Build the image

1. Build the app's image from the source. Docker downloads what the machine does not hold yet.

   ```bash
   docker compose build
   ```

   You should see many build lines, ending with:

   ```text
    deltasleuth:source  Built
   ```

### 2.6 The workspace and the database

`ds` is DeltaSleuth's command line. `docker compose run --rm app ds ...` starts a short-lived copy of
the app container, runs the command in it, and removes it again.

1. Create the workspace. This is the first test of the settings file: a mistake in it shows as one
   line that starts with `error:`.

   ```bash
   docker compose run --rm app ds init
   ```

   You should see Docker create the network, the four volumes and the database container, then:

   ```text
    Network deltasleuth_default  Created
    Volume deltasleuth_evidence  Created
    Volume deltasleuth_workspace  Created
    Volume deltasleuth_p0  Created
    Volume deltasleuth_pgdata  Created
    Container deltasleuth-db-1  Started
   workspace ready: /var/lib/deltasleuth/workspace (keys: the encryption settings' secrets)
   ```

2. Create the tables in the database. This is the first test of `db_dsn` and of the database password.

   ```bash
   docker compose run --rm app ds migrate
   ```

   You should see:

   ```text
    Container deltasleuth-db-1  Running
   {"id": 1, "at": "2026-10-05T15:53:19.639524+00:00", "actor": "system:migration", ... "action": "installation.created", ...}
   {"id": 2, "at": "2026-10-05T15:53:19.639524+00:00", "actor": "system:migration", ... "action": "system.migrate", ...}
   migrations applied: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17
   ```

   The lines that start with `{"id":` are copies of rows DeltaSleuth wrote to its audit log, the
   record of who did what. Many `ds` commands print such lines before their result.

The app also creates missing tables by itself when it starts. The two steps are kept because they
report a mistake on your terminal instead of in a log.

### 2.7 Start

1. Start the database, the app and the mocks. `--profile mocks` adds the mock services; `-d` returns
   your terminal once the containers are started.

   ```bash
   docker compose --profile mocks up -d
   ```

   You should see:

   ```text
    Container deltasleuth-db-1  Running
    Container deltasleuth-mocks-1  Created
    Container deltasleuth-app-1  Created
    Container deltasleuth-mocks-1  Started
    Container deltasleuth-db-1  Healthy
    Container deltasleuth-app-1  Started
   ```

2. Wait about ten seconds, then list the containers.

   ```bash
   docker compose ps
   ```

   You should see three containers, `app` and `db` marked `(healthy)`:

   ```text
   NAME                  IMAGE                COMMAND                  SERVICE   CREATED          STATUS                    PORTS
   deltasleuth-app-1     deltasleuth:source   "ds serve --host 0.0…"   app       8 seconds ago    Up 8 seconds (healthy)    127.0.0.1:8000->8000/tcp
   deltasleuth-db-1      postgres:16-alpine   "docker-entrypoint.s…"   db        17 seconds ago   Up 17 seconds (healthy)   5432/tcp
   deltasleuth-mocks-1   deltasleuth:source   "sh -c 'python -m mo…"   mocks     8 seconds ago    Up 8 seconds              8000/tcp
   ```

   The mocks have no health check, so their line never says `(healthy)`. Their line shows `8000/tcp`
   because they run the app's image; the mock services themselves listen on 18081, 18082, 18091 and
   18092, and only the other containers can reach them.

3. Ask the app. This address needs no sign-in.

   ```bash
   curl -fsS http://localhost:8000/v1/health
   ```

   You should see:

   ```text
   {"status": "ok", "ai": false, "auth": "tokens"}
   ```

   `"ai": false` says AI chat is off. `"auth": "tokens"` says people sign in with an API token.

4. Read what the script reads while it waits: Docker's health word for the app, and how often Docker
   had to start the app again.

   ```bash
   docker inspect --format '{{.State.Health.Status}} {{.RestartCount}}' "$(docker compose ps -aq app)"
   ```

   You should see:

   ```text
   healthy 0
   ```

   `starting` in place of `healthy` means the app is still starting. A number that keeps rising means
   it stops as soon as it starts: see "Starting by hand" under "If something goes wrong". Run this
   command while no `ds` command runs in another terminal. Not verified here: `docker compose ps -aq
   app` then lists that command's short-lived container too, and `docker inspect` refuses two
   containers in one word; the script finds the app's own container by Docker's labels for that reason.

### 2.8 The first token, and signing in

The web pages cannot create a token, so the first one comes from the command line. A token is shown
once. The command below puts it straight into a file, so it never appears on your screen.

1. Create a folder for your tokens.

   ```bash
   mkdir -p .deltasleuth/tokens
   ```

2. Close it to every other account.

   ```bash
   chmod 700 .deltasleuth .deltasleuth/tokens
   ```

3. Create a token named `first-run` with the role `spec_author`. `-T` keeps the output clean for the
   file. `-e DS_OPERATOR="$(id -un)"` tells DeltaSleuth who runs the command; `$(id -un)` is your user
   name on this machine. `set -C` makes the shell refuse to write over a file that exists, so a second
   run cannot destroy the token you saved.

   ```bash
   (umask 077; set -C; docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds token create --name first-run --owner "$(id -un)" --role spec_author > .deltasleuth/tokens/first-run)
   ```

   You should see the token's id, role and expiry, but not the token:

   ```text
    Container deltasleuth-db-1  Running
   {"id": 8, "at": "2026-10-05T16:52:01.120841+00:00", "actor": "local:<you>@deltasleuth-app", ... "action": "token.create", "object": "T-ab996823e2778210", ...}
   token T-ab996823e2778210 (first-run), spec_author, expires 2027-01-03T16:52:01+00:00; it is shown only now
   ```

   The words "it is shown only now" are `ds`'s own, for a token printed on a screen. Here nothing was
   shown: the token is in the file. Section 4 says what a second run of this command does.

4. Check that the file holds one token, without printing it.

   ```bash
   grep -c '^dst_' .deltasleuth/tokens/first-run
   ```

   You should see:

   ```text
   1
   ```

5. Check that the app accepts the token. The token goes from its file through a pipe to curl, so it
   appears neither on the screen nor in a list of running programs.

   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - http://localhost:8000/v1/auth/me
   ```

   You should see the token's name and its roles:

   ```text
   {"actor": "token:T-ab996823e2778210", "name": "first-run", "display_name": null, "kind": "token", "roles": ["viewer", "operator", "spec_author"], "csrf": null, "mode": "tokens", "idle_minutes": 30, "expires_at": "2027-01-03T16:52:01.120841+00:00"}
   ```

6. Sign in to the web pages as [chapter 1](01-install-docker.md), section 4, says.

7. If you cannot sign in, send the request the "Sign in" button sends, without a browser.

   ```bash
   { printf '{"token": "'; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"}'; } | curl -sS -H 'Origin: http://localhost:8000' -H 'Content-Type: application/json' --data-binary @- -o /dev/null -w '%{http_code}\n' http://localhost:8000/v1/auth/token-login
   ```

   You should see:

   ```text
   200
   ```

   Then the app and the token are in order, and the trouble is in the browser.

**The clipboard commands on Linux.** Chapter 1, section 4, prints the macOS pair. With X11 (install
`xclip` first), copy the token:

```bash
tr -d '\n' < .deltasleuth/tokens/first-run | xclip -selection clipboard
```

and empty the clipboard after signing in:

```bash
xclip -selection clipboard < /dev/null
```

With Wayland (install `wl-clipboard` first), copy the token:

```bash
tr -d '\n' < .deltasleuth/tokens/first-run | wl-copy
```

and empty the clipboard after signing in:

```bash
wl-copy --clear
```

Not verified here: these four commands, for lack of a Linux machine; they are the ones the script
prints there. The macOS pair was run with a stand-in value in place of the token, and on a side
pasteboard (`pbcopy -pboard ruler`): the value arrived without its line end, and `pbcopy < /dev/null`
left the pasteboard empty.

**What was seen of the sign-in.** The app answers that request with a session cookie, a small value a
browser sends back with every later request. At `http://localhost:8000` the cookie is named
`ds_session_8000`, after the port, and is marked `HttpOnly; Path=/; SameSite=strict`. Here curl
kept the cookie, and with it the app answered as the signed-in token; without it the app answered 401
`not signed in`. The session it opened ended 8 hours later, and `idle_minutes` above says it also ends
after 30 minutes without use. The sign-in page was opened in Chromium 152 and showed the "Sign in"
panel with its sentence about where a token comes from. Not verified here: pressing "Sign in" in a
browser; what the first signed-in page shows is known from the page's source only (chapter 1,
section 4). Safari and Firefox were not tried.

**Why only that address works.** The app knows its own address from `public_url` in the settings file.
It accepts a sign-in, and every other request that changes something, only from a page loaded from
exactly that address: the same `http`, the same name, the same port. Sent as from
`http://127.0.0.1:8000`, the request of step 7 was answered 403 `cross-origin request refused`. From
another machine nothing opens at all, because the port is published on this machine's own address
`127.0.0.1` only.

## 3. A second installation on the same machine

Two installations cannot share a project name, a port or a subnet. The default ones are `deltasleuth`,
8000 and `172.30.0.0/24`. A second installation lives in its own folder, a second clone of the source,
and takes its own three values. The image is named after the project, so the two share no image.

### 3.1 Find free values

1. List the project names in use. Choose a short name in lower-case letters and digits that is not in
   the list, for example `ds2`. It is written `<project>` below.

   ```bash
   docker compose ls -a
   ```

   You should see one line for each installation:

   ```text
   NAME                STATUS              CONFIG FILES
   deltasleuth         running(3)          <folder>/IntelliDiff/deploy/docker-compose.yml,...
   ```

2. List Docker's networks with the command of section 2.1, step 4. Choose a subnet that is not in the
   list, of the form `a.b.c.0/24`, for example `172.31.7.0/24`. It is written `<subnet>` below. Its
   gateway, written `<gateway>`, is the same address ending in `.1`, for example `172.31.7.1`.

3. Check that the port you want is free, with the two commands of section 2.1, step 3, and your port
   in place of 8000. Choose a number from 1024 to 65535, for example 18000. It is written `<port>` below.

### 3.2 With the script

1. Clone the source a second time, into a folder of its own. `<another folder>` is a name of your
   choice that does not exist yet.

   ```bash
   git clone <repository URL> <another folder>
   ```

2. Go into it.

   ```bash
   cd <another folder>
   ```

3. Run the script with your three values.

   ```bash
   sh deploy/up.sh --project <project> --port <port> --subnet <subnet>
   ```

   You should see the same six steps as in chapter 1, and a closing block that names your project and
   your address. Here, with `--project dsguide --port 18000 --subnet 172.31.10.0/24`:

   ```text
   == 3/6 Build the image dsguide:source
   ...
   DeltaSleuth is running: Compose project "dsguide", an unsigned build of this checkout, for a trial.

     Open            http://localhost:18000   in a browser on this machine, typed exactly so
   ```

The script wrote the three values into three files. `.env` got one more line,
`COMPOSE_PROJECT_NAME=dsguide`. The `public_url` line of `deploy/config/deltasleuth.yaml` got the port.
And the site file was written like this:

```yaml
# This installation's changes to docker-compose.yml, written by deploy/up.sh for its own port and subnet;
# deploy/docker-compose.site.example.yml shows what else goes here.
services:
  app:
    secrets: []
    ports: !override ["127.0.0.1:18000:8000"]
networks: {default: {ipam: {config: !override [{subnet: 172.31.10.0/24, gateway: 172.31.10.1}]}}}
```

Later runs of the script in that folder need no options: `sh deploy/up.sh` reads the three files.

### 3.3 By hand

The steps are those of section 2 from 2.2 on, done in the second folder, with four small edits in
three files. The order, once: section 2.2; section 2.3, steps 1 and 2; the five steps below; then
sections 2.4 to 2.8. Section 2.1 is not repeated: section 3.1 took its place. Make the edits in a text
editor. They need Compose 2.24.4 or later, for the word `!override`.

1. In `.env`, change the last line: remove the `# ` at its start and write your project name.

   ```text
   COMPOSE_PROJECT_NAME=<project>
   ```

2. In `deploy/docker-compose.site.yml`, change the line that starts with `    # ports:`: remove the
   `# `, keep the four spaces before `ports`, and write your port in place of 8001.

   ```text
       ports: !override ["127.0.0.1:<port>:8000"]   # !override: Compose 2.24.4 or later
   ```

3. In the same file, change the last line: remove the `# ` and write your subnet and its gateway.

   ```text
   networks: {default: {ipam: {config: !override [{subnet: <subnet>, gateway: <gateway>}]}}}
   ```

   The comment above that line mentions `auth.trusted_proxies`. Leave that setting switched off in the
   settings file: it matters only behind a reverse proxy, and this installation ran without it.

4. In `deploy/config/deltasleuth.yaml`, write your port in the `public_url` line.

   ```text
     public_url: http://localhost:<port>              # the address people type, exactly; an office: https://<name>, through its TLS proxy
   ```

5. Run the check of section 2.3, step 3. You should see your own values. Here, with the project
   `dsguideh`, port 18005 and subnet `172.31.12.0/24`:

   ```text
   name: dsguideh
       image: dsguideh:source
           published: "18005"
       image: postgres:16-alpine
       image: dsguideh:source
           - subnet: 172.31.12.0/24
             gateway: 172.31.12.1
   ```

   If the first line says `name: deltasleuth`, stop: the `.env` line of step 1 is missing, and the
   commands that follow would act on the first installation.

Then go on with sections 2.4 to 2.8. Use your port wherever a command says `localhost:8000`.

### 3.4 What changes, and what stays

Every `docker compose` command of this guide works unchanged in the second folder, because `.env`
there names the project: the script runs its own `docker compose` commands that way. Docker's names
begin with your project name: the containers (`<project>-app-1`), the volumes (`<project>_pgdata`),
the network (`<project>_default`) and the image (`<project>:source`).

To see which installation the folder you are in acts on, ask Compose. Do it before any command that
stops or deletes.

```bash
docker compose config | grep '^name:'
```

You should see the project's name, here:

```text
name: dsguideh
```

To name the project yourself for one command, write `-p <project>` after `docker compose`.

```bash
docker compose -p <project> ps
```

With `-p` a command acts on that project from any folder: `ps` answered from a folder without an
installation, and so did `down -v`, which deleted the project's data. Read the name after `-p` twice.

Five commands of this guide are `docker` commands without the word `compose`. They name the project
themselves, so type your name in them. Typed with `deltasleuth`, they look at the first installation
or act on it.

```bash
docker ps -a --filter label=com.docker.compose.project=<project>
```

```bash
docker volume ls --filter label=com.docker.compose.project=<project>
```

```bash
docker image rm <project>:source
```

```bash
docker image ls --filter dangling=true --filter label=com.docker.compose.project=<project>
```

```bash
docker image prune --force --filter label=com.docker.compose.project=<project>
```

The word `deltasleuth` stays wherever it is not the project's name:

- `local:<you>@deltasleuth-app` in the lines of `ds token create` and `ds token list`.
  `deltasleuth-app` is the host name of the app container, which the product's base file fixes, so two
  installations on one machine record the same host name for their `ds` commands.
- Paths inside the containers, such as `/var/lib/deltasleuth/workspace` and
  `/etc/deltasleuth/deltasleuth.yaml`.
- The database's user and its name, both `deltasleuth`, in the command of section 2.4, step 4.
- The settings file's name, `deploy/config/deltasleuth.yaml`.

Each installation's session cookie carries its port in its name. Here one curl cookie store signed in
to two installations, at ports 18000 and 18005, held `ds_session_18000` and
`ds_session_18005`, and stayed signed in to the second when it signed out of the first. Not verified
here: the same in a browser.

The script protects the first installation. Run in the second folder without options, or with a value
the first one holds, it stops before it creates anything. "If something goes wrong" quotes those
lines.

## 4. More tokens and their roles

An API token is a named credential. People paste one into the sign-in page, and scripts send one with
each request. A token has one of three roles:

| Role | May |
|---|---|
| `viewer` | Read runs and results |
| `operator` | Also start runs |
| `spec_author` | Also upload comparison specs. This is the highest role a token can hold |

No token can be an administrator. Administration is done with `ds`, which works on the database
directly: whoever can run it in the installation's folder can do everything. A `ds` command that
changes something must be told who runs it, in the variable `DS_OPERATOR`; the name is taken on trust
and written to the audit log.

1. Create a token. `<name>` is 3 to 64 characters: lower-case letters, digits, `.`, `_` and `-`,
   starting with a letter or a digit; choose one that has no file in `.deltasleuth/tokens/` yet.
   `<role>` is one of the three roles. `<days>` is how long it lives, from 1 to 90; without `--days`
   it lives 90 days. `set -C` makes the shell refuse to write over a file that exists.

   ```bash
   (umask 077; set -C; docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds token create --name <name> --owner "$(id -un)" --role <role> --days <days> > .deltasleuth/tokens/<name>)
   ```

   You should see, here for the name `nightly-check`, the role `operator` and 30 days:

   ```text
    Container deltasleuth-db-1  Running
   {"id": 12, "at": "2026-10-05T16:52:40.607166+00:00", "actor": "local:<you>@deltasleuth-app", ... "action": "token.create", "object": "T-6cdfd691380498bb", ...}
   token T-6cdfd691380498bb (nightly-check), operator, expires 2026-11-04T16:52:40+00:00; it is shown only now
   ```

   If `ds` answers with a line that starts `error:` instead, no token was made, but the shell has
   already created the file, and it is empty. Delete it with the command of step 4 before you correct
   the command and run it again; otherwise the shell answers `file exists`.

2. List the tokens, newest first, with the alias of [chapter 1](01-install-docker.md), section 5. The
   list never shows a token itself.

   ```bash
   ds token list
   ```

   You should see:

   ```text
    Container deltasleuth-db-1  Running
   T-6cdfd691380498bb (nightly-check)  operator  active  expires 2026-11-04T16:52:40+00:00  owner '<you>'  created by local:<you>@deltasleuth-app  last used -
   T-ab996823e2778210 (first-run)  spec_author  active  expires 2027-01-03T16:52:01+00:00  owner '<you>'  created by local:<you>@deltasleuth-app  last used 2026-10-05T16:52:01+00:00
   ```

3. Take a token away. `<id>` is the id at the start of its line, for example `T-6cdfd691380498bb`.

   ```bash
   ds token revoke <id>
   ```

   You should see an audit line, then:

   ```text
   revoked token T-6cdfd691380498bb (nightly-check)
   ```

   A revoked token stays in the list, marked `revoked`, and the app answers it with 401
   `invalid or expired API token`.

4. Delete the file of a token you revoked, or the empty file a refused command left. `<name>` is the
   token's name.

   ```bash
   rm .deltasleuth/tokens/<name>
   ```

**One name, one file.** Keep `set -C` in the command of step 1. With it, a second run under a name
whose file exists is refused by the shell before `ds` starts: zsh says `file exists:
.deltasleuth/tokens/<name>`, bash says `.deltasleuth/tokens/<name>: cannot overwrite existing file`,
and the file and its token stay as they were. Without it, the shell empties the file first. Tried here:
a command that `ds` then refused left the file of an existing token at 0 bytes, so the token stayed
valid while its value was lost. If that happens, revoke the token (step 3) and create a new one.

Token names are not unique in the database. If you delete a token's file and create a token of the
same name again, the list shows two lines with that name: the lower one is the older.

To use a token in a script, send it as the command of section 2.8, step 5, does.

## 5. Change a setting

The app reads the files in `deploy/config/` and the secrets only when it starts.

### 5.1 A settings file

The example ends an idle browser session after 60 minutes instead of 30.

1. Open `deploy/config/deltasleuth.yaml` in a text editor and add the last line shown here under
   `auth:`, indented by two spaces like `public_url`.

   ```text
   auth:                                            # sign-in and roles (docs/mvp/AUTH.md §8)
     public_url: http://localhost:8000              # the address people type, exactly; an office: https://<name>, through its TLS proxy
     session_idle_minutes: 60
   ```

2. Check the change before it takes effect. This command opens what a start opens: the files of
   `deploy/config`, the secrets and the database.

   ```bash
   docker compose run --rm app ds licence
   ```

   When all is well you should see:

   ```text
    Container deltasleuth-db-1  Running
   installation I-199293194dabf0b4
   licence none: this build needs no licence
   state open
   allows: runs yes, sso yes, signoff yes, packs yes
   ```

   A mistake shows instead as the one line a start would log, for example:

   ```text
   error: invalid settings /etc/deltasleuth/deltasleuth.yaml: auth.sesion_idle_minutes: Unexpected keyword argument
   ```

3. Restart the app so that it reads the file.

   ```bash
   docker compose restart app
   ```

4. Wait five seconds, then check that it came back. The app took about three seconds to answer again;
   asked sooner, curl says `curl: (52) Empty reply from server`.

   ```bash
   curl -fsS http://localhost:8000/v1/health
   ```

   You should see the line of section 2.7, step 3. The answer of section 2.8, step 5, then said
   `"idle_minutes": 60`.

Unknown names are refused, never ignored. An app restarted with a mistake in a file does not stay up:
"If something goes wrong" describes what you then see.

### 5.2 The site file

A change to `deploy/docker-compose.site.yml` is different: `restart` does not apply it. Later chapters
bring such changes, for example a folder of files to compare.

1. After changing the site file, apply it. Compose makes anew each container whose description
   changed; the data stays.

   ```bash
   docker compose --profile mocks up -d
   ```

   After a change under `app:` you should see the app made anew and the other two left alone:

   ```text
    Container deltasleuth-mocks-1  Running
    Container deltasleuth-db-1  Running
    Container deltasleuth-app-1  Recreated
    Container deltasleuth-db-1  Healthy
    Container deltasleuth-app-1  Started
   ```

   When nothing changed you should see every container `Running` and none `Recreated`.

The change made here was a `volumes:` line under `app:`, of the kind the site file
carries as a comment. It showed an empty folder of the machine to the app, read-only. After
`docker compose restart app` the app did not have the folder; after the command of this step it did.

### 5.3 Stop and start, in more detail

`docker compose stop` stops all three containers and `docker compose start` starts all three.
`docker compose ps -a` also lists stopped containers; there the mocks show `Exited (137)`, which is how
they always stop. `docker compose up -d` without `--profile mocks` starts the database and the app and
leaves the mocks as they are. `docker compose down` without `-v` removes the containers and the
network and keeps the data: after it, the command of section 2.7, step 1, brings the installation
back, and the tokens still work.

When only the database is stopped, the health address answers 503
`{"detail": "the database is not answering, or the pool gave no connection in 3 s"}` (not verified
here in these words). The app answered `ok` again by itself
within 15 seconds after the database was started again.

Not verified here: what happens after the machine restarts. The containers are declared to start
again with Docker unless you stopped them.

## 6. Update by hand, and tidy up

1. Fetch the newer source. It prints `Already up to date.` when there is none.

   ```bash
   git pull
   ```

2. Build it and replace the running app and mocks. The database container is left running.

   ```bash
   docker compose --profile mocks up -d --build
   ```

   You should see the build lines, then:

   ```text
    deltasleuth:source  Built
    Container deltasleuth-db-1  Running
    Container deltasleuth-app-1  Recreated
    Container deltasleuth-mocks-1  Recreated
    Container deltasleuth-mocks-1  Started
    Container deltasleuth-db-1  Healthy
    Container deltasleuth-app-1  Started
   ```

3. Each rebuild leaves the build it replaced on the machine, without a name. List those builds.

   ```bash
   docker image ls --filter dangling=true --filter label=com.docker.compose.project=deltasleuth
   ```

   After two rebuilds you should see two:

   ```text
   IMAGE        ID             DISK USAGE   CONTENT SIZE   EXTRA
   <untagged>   b2042b7f6098        720MB             0B
   <untagged>   bacbb989fd33        720MB             0B
   ```

4. Remove them. The filter limits the command to builds of this project.

   ```bash
   docker image prune --force --filter label=com.docker.compose.project=deltasleuth
   ```

   You should see the builds it deleted:

   ```text
   Deleted Images:
   deleted: sha256:b2042b7f609875fc35670f156efe033ad791e023bd555261419691405d4fff4e
   deleted: sha256:bacbb989fd33384e46967330434a1aaaf073e4ec8c8905fb8bdec92c3c80953d

   Total reclaimed space: 0B
   ```

When the newer source needs new tables, the app creates them as it starts. If your data matters, take
a backup first; the deployment document describes it under "Backups and restore". New optional
settings appear only in the example files, never in your copies.

Not verified here: a `git pull` that brings new commits, and an update that needs new tables. The
update was tried by changing one source file. The build ran once, both containers were made anew on
the new image, and the image it replaced stayed without a name until step 4.

After removing an installation ([chapter 1](01-install-docker.md), section 8), these two commands
print only their header line when nothing is left:

```bash
docker ps -a --filter label=com.docker.compose.project=deltasleuth
```

```bash
docker volume ls --filter label=com.docker.compose.project=deltasleuth
```

Not verified here: removing what the build downloaded. The base images (`postgres:16-alpine`,
`python:3.13-slim`, `node:24-alpine`) and Docker's build cache stay on the machine.

## If something goes wrong

Each message below was met or provoked here, unless it says otherwise.

### Getting the source

**`fatal: destination path 'IntelliDiff' already exists and is not an empty directory.`**
`git clone` found a folder of that name where you are. If it holds an installation you want to keep,
clone under another name (section 3.2).

### The script refuses before it creates anything

Each of these is one line on the terminal, the script ends with status 2, and nothing was created or
changed.

**`error: Compose project "deltasleuth" on this machine was started from <folder>/deploy: this script
takes over no other installation. For a second one here: sh deploy/up.sh --project <name> --port <port>
--subnet <a.b.c.0/24>`**
Another folder already runs an installation under this project name. Go on in that folder, or make
this one a second installation (section 3.2).

**`error: port 8000 of this machine is published by the container deltasleuth-app-1: choose another
with --port N (for a first installation on this machine that option alone is enough)`**, or **`error:
port 8000 of this machine answers already: choose another with --port N (for a first installation on
this machine that option alone is enough)`**
A container, or a program outside Docker, uses the port; the script asks the machine's two own
addresses, `127.0.0.1` and `[::1]`. Stop it, or choose a free port (section 3.1, step 3). Where no
other DeltaSleuth installation exists on the machine, `--port <port>` alone is enough: the project name
and the network stay the default ones. Beside another installation, give the three options of
section 3.2: here the default project name and network were taken as well, and with `--port` alone
the script answered with the project line above. Not verified here: `--port` alone going through, and
the words after `--port`.

**`error: the subnet 172.30.0.0/24 is the Docker network deltasleuth_default's: choose another with
--subnet a.b.c.0/24`**
Another Docker network uses that range. Give `--subnet` one that section 3.1 shows as free.

**`error: Docker volumes of project "<name>" exist, but deploy/secrets holds no db_password, kek and
tokeniser: this folder does not hold the keys of that data, which DeltaSleuth cannot open without
them. Use another project name, or put the secret files back. If the data is no longer wanted, docker
volume ls --filter label=com.docker.compose.project=<name> lists its volumes, for you to remove with
docker volume rm; this script deletes nothing`**
An earlier installation of that name was removed without `-v`, or its folder was deleted, and its data
is still in Docker. Put its `deploy/secrets` folder back into this folder, or use another project
name. Provoked here by `docker compose down` in one folder and the script in a fresh clone. Not
verified here: the line's words about the two `docker volume` commands. If that data is no longer
wanted, this command, which was tried here, deletes it by the
project's name, `<project>` being the name in the message. This cannot be undone, so read the name
twice.

```bash
docker compose -p <project> down -v
```

Run in the fresh clone, it removed the four volumes, and a later run of the script under that name
went through.

**`error: Docker is not running, or this account may not use it`**
Start Docker Desktop (macOS) or the Docker service (Linux, with an account that may run `docker`).
Provoked here by pointing the `docker` command at a socket that does not exist.

**`error: docker is not installed (docs/guide/01-install-docker.md, section 1)`**
Install Docker. Provoked here with a search path that did not hold `docker`. The script says the same
of `openssl`; that line was not provoked.

**`error: deploy/docker-compose.yml is not in this folder: cd to the DeltaSleuth checkout, then sh
deploy/up.sh`**
Run the script from the folder of the clone, `IntelliDiff`.

**`error: unset COMPOSE_FILE and COMPOSE_PROJECT_NAME in this shell: they override .env`**
One of the two variables is set in your terminal. Remove both, then run the script again:

```bash
unset COMPOSE_FILE COMPOSE_PROJECT_NAME
```

**`error: unknown option --projekt (--project NAME, --port N, --subnet A.B.C.0/24)`**, **`error:
--port needs a value (--project NAME, --port N, --subnet A.B.C.0/24)`**, **`error: --project: a
project name is lower-case letters and digits, joined by -, _ or __ (for example ds2)`**, **`error:
--port: a number from 1024 to 65535`**, **`error: --subnet: a.b.c.0/24, for example 172.31.7.0/24`**
An option is misspelt or its value has the wrong form. The line says which form is accepted.

**`error: .env makes this folder project "dsguideh", not "other": this script changes no existing
file (leave --project out, or edit .env yourself)`**, **`error: deploy/config/deltasleuth.yaml exists
and does not say port 18006 in public_url: this script changes no existing file (edit it yourself, or
give the option that matches it)`**, or **`error: deploy/docker-compose.site.yml exists and does not
say subnet 172.31.99.0/24: this script changes no existing file (edit it yourself, or give the option
that matches it)`**
The folder already holds files with other values than the options you gave. Leave the options out:
the script reads the values from the files.

**`error: .env has no line COMPOSE_FILE=… that names deploy/docker-compose.source.yml (once,
unquoted): this script runs an unsigned build of this checkout for a trial; a loaded release is run as
docs/mvp/DEPLOY.md says`**
`.env` was written by hand: it names only two files, or its line begins with the word `export`.
Replace it with a copy of `deploy/env.source.example` (section 2.3, step 2). A quoted project name gets
**`error: .env's COMPOSE_PROJECT_NAME (read unquoted and once): a project name is lower-case letters
and digits, joined by -, _ or __ (for example ds2)`**: remove the quotes.

**`error: deploy/config/deltasleuth.yaml is not a token sign-in trial at a loopback http address
(public_url is https://ds.example.com): this script sets up a trial only; docs/mvp/DEPLOY.md is the
office deployment`**, or the same with `(auth.oidc is on)`
The settings file describes an office installation: an `https` address, or single sign-on switched
on. The script does not start such an installation; the deployment document does.

**`error: deploy/secrets holds some of db_password, db_dsn, kek and tokeniser but not all: put the
missing ones back; new ones cannot open what the old ones protected`**
A secret file is missing. Put it back from your copy.

**`error: deploy/secrets/kek is empty: remove it yourself, then run this script again`**, or
**`error: .env.new is left by an interrupted run: remove it yourself, then run this script again`**
The file named is empty, or is left over from a run that was interrupted while it wrote. Remove that
file with `rm`, then run the script again.

**`error: deploy/secrets is open to other accounts: chmod 700 it yourself, then run this script
again`**
The folder `deploy/secrets` lets the group or every account of the machine in. The secrets in it are
readable by everyone, for the containers' sake, so only the folder's mode keeps them private. Close it
with the command of section 2.4, step 2.

**`error: deploy/secrets/db_password holds characters an address cannot carry as written: write
deploy/secrets/db_dsn yourself (postgresql://deltasleuth:<the password, percent-encoded>@db:5432/deltasleuth),
then run this script again`**
`db_dsn` is missing, and `db_password` holds a password of your own with a character other than a
letter, a digit, `.`, `_`, `~` or `-`. Section 2.4 makes no such password. Write `db_dsn` in a text
editor as one line of the form shown, with each such character in its percent form, for example `%40`
for `@`, `%2F` for `/`, `%25` for `%` and `%20` for a space, and give it mode 444 like the others.

**`error: deploy/secrets/kek is a link to a file that is not there: put that file back, or remove the
link yourself; this script changes no existing file`**
One of the ten files is a symbolic link, and what it points to is missing, for example a folder of
secrets that is not mounted now. Put the file back, or remove the link.

Not verified here: the three lines above, and three more refusals of the script. Those concern Docker
Compose version 1, a Compose older than 2.24.4 together with `--port` or `--subnet`, and a signed
release image already loaded on the machine.

### The script stops after it has begun

What exists is kept. Correct the cause and run the script again.

**`error: ds init failed (the lines above say why): correct it and run this script again; it keeps
what exists`**, or the same with `the build`, `ds migrate`, `docker compose up` or `ds token list`
The line above it is the reason. Under `ds init` it was a misspelt setting (`error: invalid settings
...`, below). Under `ds migrate` it was a wrong database password (`error: the database answered and
asked for a password ...`, below). `the build` was provoked by making the build command fail.
`ds token list` is the last step of a run that finds the token's file already there. It opens what a
start of the app opens, so it reports a mistake that the running app has not read yet, for example
one made in `deploy/config/targets.yaml` since the app started; the app itself keeps running on what
it read. Correct the file and check it with the command of section 5.1, step 2. Not verified here:
the lines with `docker compose up` and with `ds token list`.

**`error: the app did not become healthy; the line above, if any, is its last error (all of it:
docker compose logs --tail 40 app)`**
The app stopped as soon as it started. The line above is the last error of its log. Here it was
a mistake in the targets file: `error: /targets/orders-v1/rate_per_sec [schema.additionalProperties]
unexpected member 'rate_per_sec'`.

**`warning: the token in .deltasleuth/tokens/first-run (T-ab996823e2778210) is not an active token of
this database (revoked, expired, or another database's) and will be refused: remove the file and run
this script again for a new one`**
The token file is older than the database, for example after a removal with `-v` that kept the folder.
Do as the line says:

```bash
rm .deltasleuth/tokens/first-run
```

```bash
sh deploy/up.sh
```

### Starting by hand

**`Error response from daemon: ports are not available: exposing port TCP 127.0.0.1:8000 ->
127.0.0.1:0: listen tcp4 127.0.0.1:8000: bind: address already in use`**
A program outside Docker already listens on the port. The database and the mocks started; the app did
not. Stop that program, then run the command of section 2.7, step 1, again.

**`docker compose ps` never shows the app as `(healthy)`: its status reads `Up Less than a second
(health: starting)`, `Up 1 second (health: starting)` or `Restarting (2) Less than a second ago`**
The app stops as soon as it starts, and Docker starts it again, in a loop. `docker compose up -d` and
`docker compose restart app` still report `Started`. Meanwhile curl answers `curl: (7) Failed to
connect to localhost port 8000` or `curl: (52) Empty reply from server`. Which of the three status
lines you catch is chance. The reason is in the app's log, as one line that starts with `error:` and
repeats:

```bash
docker compose logs --tail 5 app
```

Here it was:

```text
app-1  | error: invalid settings /etc/deltasleuth/deltasleuth.yaml: auth.sesion_idle_minutes: Unexpected keyword argument
app-1  | error: invalid settings /etc/deltasleuth/deltasleuth.yaml: auth.sesion_idle_minutes: Unexpected keyword argument
```

Look that line up below. After the correction, `docker compose restart app` ends the loop.

### Settings, secrets and the database

Each of these is the one `error:` line of a `ds` command, or the repeating line of the app's log.

**`error: invalid settings /etc/deltasleuth/deltasleuth.yaml: auth.sesion_idle_minutes: Unexpected
keyword argument`**
A setting's name is misspelt (here `sesion`). Correct the file.

**`error: /targets/orders-v1/rate_per_sec [schema.additionalProperties] unexpected member
'rate_per_sec'`**
A name in `deploy/config/targets.yaml` is misspelt (here `rate_per_sec` for `rate_per_s`). `ds init`
does not read that file and passes; the check of section 5.1, step 2, and the app's start report it.

**`error: invalid settings ...: auth.public_url: must be https://host[:port] (http only for localhost,
127.0.0.1 or [::1]), without a path, query or fragment`**
`public_url` uses plain `http` with another name than those three, or has something after the port.
Use `http://localhost:8000`.

**`error: the database answered and asked for a password, then refused the connection: check the
user, the password and the database name in the file database_dsn_ref names; if they are right, the
server, the role or the database is at its connection limit, or a pooler cannot reach its server`**
`db_dsn` holds another password than the one the database was created with. The answer comes after
three tries, about four seconds. Not verified here: the three tries and the words after `names`. If
`deploy/secrets/db_password` is still the file the database was created with, make `db_dsn` again from
it, then restart the app:

```bash
rm -f deploy/secrets/db_dsn
```

```bash
(umask 077; { printf 'postgresql://deltasleuth:'; tr -d '\n' < deploy/secrets/db_password; printf '@db:5432/deltasleuth'; } > deploy/secrets/db_dsn)
```

```bash
chmod 444 deploy/secrets/db_dsn
```

```bash
docker compose restart app
```

If `db_password` itself was created again after the first start, the database no longer knows it. On
an installation without data yet, remove it ([chapter 1](01-install-docker.md), section 8, step 2) and
start again from section 2.6. With data to keep, see "Startup, secrets and the database" in the
deployment document.

**`error: the file database_dsn_ref names does not hold an address the driver can read:
percent-encode special characters in the user and the password`**
`db_dsn` was written by hand, and its password holds a `%`, an `@`, a `/` or a space typed as it is.
Write each in its percent form (`%25`, `%40`, `%2F`, `%20`). If `db_password` is one that section 2.4
made, make `db_dsn` again with the four commands above instead. Not verified here on Docker; run from
the source, as in chapter 2, the line came at once.

**`error: secret file /run/secrets/db_dsn is empty or has a BOM or surrounding whitespace`**
The file named holds more than its one value, here a blank line at its end. For `db_dsn`, make it
again with the four commands above. For another secret, before the first start: remove the file and
create it again (section 2.4). After the first start, put back the copy you kept.

**`error: keyring /run/secrets/kek line 1: expected 64 hex`**
The file `kek` does not hold a key as section 2.4, step 5, makes it. Before the first start, remove
the file and create it again. After the first start, put back the copy you kept. Provoked with a `kek`
file that held the words `not-a-key`, through the check of section 5.1, step 2.

**`permission denied: deploy/secrets/kek`** (zsh) or **`bash: deploy/secrets/kek: Permission
denied`** (bash), with any of the four file names
You ran a command of section 2.4, steps 3 to 6, a second time. The file exists and is read-only, and
the command left it unchanged. That is the protection you want after the first start.

**`error connecting in 'pool-1': failed to resolve host 'db': [Errno -2] Name or service not known`,
then, 30 seconds later, `error: the database is unreachable (PoolTimeout); check database_dsn_ref`**
The database container is not running. Provoked by stopping it and running a `ds` command that did not
start it again. Start the installation (section 2.7, step 1).

**`{"detail": "the database is not answering, or the pool gave no connection in 3 s"}`** from the
health address
The app runs and the database does not answer. Start the database container with the command of
section 2.7, step 1.

Not verified here: on Linux, a secret or settings file that the container's user cannot read. The
settings files need mode 644 and the secrets mode 444, as sections 2.2 and 2.4 leave them.

### `ds` and tokens

**`error: set DS_OPERATOR to your user name (docs/mvp/AUTH.md §4.2)`**
A `ds` command that changes something ran without `DS_OPERATOR`. Use the alias of
[chapter 1](01-install-docker.md), section 5, or add `-e DS_OPERATOR="$(id -un)"` after `--rm`.

**`error: role must be one of operator, spec_author, viewer`**
You asked for a token with another role, for example `admin`.

**`error: name must match ^[a-z0-9][a-z0-9._-]{2,63}$`**
The token's name has a capital letter, a space, or fewer than three characters.

**`error: days must be from 1 to 90`**
A token lives 90 days at most.

After any of the four lines above, the command of section 4, step 1, has left an empty file
`.deltasleuth/tokens/<name>`. Delete it (section 4, step 4), then run the corrected command.

**`file exists: .deltasleuth/tokens/<name>`** (zsh) or **`.deltasleuth/tokens/<name>: cannot overwrite
existing file`** (bash)
A file of that name is there already, and the shell left it alone; no token was created. If it is the
empty file of a refused command, delete it. If it holds a token you use, choose another name.

**`command not found: ds`** (zsh) or **`ds: command not found`** (bash)
The alias is not defined in this terminal. Define it again.

**`no configuration file provided: not found`**
You ran `docker compose` outside the installation's folder. `cd` into it and run the command again.

**`error: cannot pull with rebase: You have unstaged changes.`**, then `error: Please commit or stash
them.`, from `git pull`
A file that belongs to the source was changed by hand; here it was a source file changed to try an
update. Git words it so where its `pull.rebase` setting is on, as here; not verified here: its words
without that setting. Your own settings, secrets and tokens never cause this, because git ignores
them. Undo the change to the file git names in `git status`, then pull again.

### Signing in

**`{"detail": "not signed in"}`**
A request to the API carried no token. Send one as in section 2.8, step 5.

**`invalid or expired API token`** (under the "Sign in" button, or from curl)
The token is mistyped, revoked, or past its expiry. Check it with section 2.8, step 5, or create
another one (section 4).

**`cross-origin request refused`** (under the "Sign in" button, or from curl)
The browser's address is not exactly `public_url`: for example `http://127.0.0.1:8000` in place of
`http://localhost:8000`. Open the address the settings file names. Not verified here: the page then
adds a hint that names the address to try; the hint is in the page's source and was not seen in a
browser.

**The "Sign in" panel comes back after you press "Sign in"**
Not verified here. The page's source holds a message for a browser that does not send the session
cookie back: "The token was accepted, but this browser did not send the session cookie back, so you
are not signed in. Allow cookies for this address, or use another browser." Send the request of
section 2.8, step 7, to see whether the app and the token are in order.

### Removing

**`Error response from daemon: conflict: unable to remove repository reference "deltasleuth:source"
(must force) - container <id> is using its referenced image <id>`**
`docker image rm` ran while containers of that image still exist. Run `docker compose down -v` first
([chapter 1](01-install-docker.md), section 8, step 2), then remove the image. Do not add `--force`.

## Reference

- [docs/mvp/FIRSTRUN.md](../mvp/FIRSTRUN.md): the script, the example files and the sign-in page, in
  full: every refusal of the script and why it is there.
- [docs/mvp/DEPLOY.md](../mvp/DEPLOY.md): the deployment document, written for an office installation
  with an identity provider and a TLS reverse proxy. Its troubleshooting section lists more messages.
- [docs/mvp/AUTH.md](../mvp/AUTH.md): sign-in modes, roles, tokens, sessions, cookies and the audit
  log.
- [docs/mvp/RUNBOOK.md](../mvp/RUNBOOK.md): every `ds` command, the roles in a table, and the mock
  services.
- [docs/mvp/RETENTION.md](../mvp/RETENTION.md): the keyring, the tokeniser key and how long data is
  kept.
- [docs/mvp/LICENCE.md](../mvp/LICENCE.md): why this build needs no licence (its section 19).
- [docs/mvp/RELEASE.md](../mvp/RELEASE.md): signed releases, the other way to get the image once a
  release exists.

Back to [chapter 1](01-install-docker.md).
