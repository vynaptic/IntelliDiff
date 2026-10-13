# 1. Install with Docker

DeltaSleuth compares what two sources hold and reports the differences key by key. A source can be a
REST service, a database table, or a folder of CSV or Parquet files. This chapter installs it on one
machine with Docker, from the source, with one command. At the end you have three containers (a
database, the app with its web pages, and practice services called the mocks), the web address
`http://localhost:8000`, and an API token, a named password to sign in with, in a file. Checked on macOS
with Docker Desktop on 2026-10-05; not checked on Linux.

## 1. What you need

You need git, which fetches the source; Docker with Docker Compose version 2; and OpenSSL, a tool that
makes random keys. The first installation downloads what the machine does not hold yet: the images
`python:3.13-slim`, `node:24-alpine` and `postgres:16-alpine` from Docker Hub (about 600 MB as Docker
lists them), and Python and npm packages. Without that internet access the build fails. The image it
builds takes about 720 MB more. Not verified here: how long that first download takes; with everything
already on the machine the installation took 19 seconds.

1. Check Docker Compose. It must be version 2, here `Docker Compose version v2.40.3-desktop.1`.
   ```bash
   docker compose version
   ```
2. Check that Docker runs (macOS: start Docker Desktop first; Linux: your account must be allowed to
   run `docker`). It prints the server's version, here `29.1.3`, or an error line when it does not.
   ```bash
   docker info --format '{{.ServerVersion}}'
   ```
3. Check OpenSSL. It prints its version, here `OpenSSL 3.6.2`. LibreSSL, which macOS ships under the
   same name, makes the same keys.
   ```bash
   openssl version
   ```

## 2. Get the source

1. Clone the repository. `<repository URL>` is the repository's address. The folder is called
   `IntelliDiff`; the product in it is DeltaSleuth.
   ```bash
   git clone <repository URL> IntelliDiff
   ```
2. Go into the folder. Run every later command from it: Docker Compose reads its settings there.
   ```bash
   cd IntelliDiff
   ```

## 3. Install and start

1. Run the installation script.
   ```bash
   sh deploy/up.sh
   ```
   You should see six steps and a closing block; each `...` stands for lines left out here. Docker's
   own progress and lines that start with `{"id":`, copies of DeltaSleuth's audit records, are normal.
   ```text
   == 1/6 Settings, site file and .env
   created  deploy/config/deltasleuth.yaml
   ...
   == 4/6 Workspace and database
   ...
   migrations applied: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17
   ...
   == 6/6 First token
   ...
   token T-f66c5874ffb089ed (first-run), spec_author, expires 2027-01-03T16:47:46+00:00; it is shown only now
   created  .deltasleuth/tokens/first-run

   DeltaSleuth is running: Compose project "deltasleuth", an unsigned build of this checkout, for a trial.

     Open            http://localhost:8000   in a browser on this machine, typed exactly so
     Copy the token  tr -d '\n' < .deltasleuth/tokens/first-run | pbcopy
     Sign in         paste it into "API token", press "Sign in", then empty the clipboard: pbcopy < /dev/null
     ...
     Every step      docs/guide/A1-install-by-hand.md        The office deployment: docs/mvp/DEPLOY.md
   ```

The line `token T-... (first-run) ... it is shown only now` names the token's id, role and expiry, not
the token, which went straight into the file `.deltasleuth/tokens/first-run`: the script never prints it
or a secret. The last line names the [appendix](A1-install-by-hand.md): the same steps, by hand.

The script creates only what is missing and never changes or deletes a file: run again, it prints
`kept` in place of `created`. The closing block comes only when every step passed. If a step fails,
the script stops there with a line that starts `error:`. That line says what to correct; where it
reads `... failed (the lines above say why)`, the reason is above it, in Docker's lines or in one more
line that starts `error:`. Correct it and run the script again. A line that starts `warning:` just
above the closing block says that the saved token is no longer valid, and how to get a new one.

From here on you need the address `http://localhost:8000`, which only this machine can reach, and the
token's file. Docker groups the containers, the data and the network under one name, the Compose
project, here `deltasleuth`. The appendix, section 1, lists every file and every name that was made.

## 4. Sign in

1. Open this address in a browser on the same machine, typed exactly like this:
   ```text
   http://localhost:8000
   ```
   You should see a panel headed "Sign in" with one field, "API token", a "Sign in" button, and a
   sentence that begins "An API token comes from the command line".
2. Copy the token to the clipboard without showing it. On macOS:
   ```bash
   tr -d '\n' < .deltasleuth/tokens/first-run | pbcopy
   ```
   On Linux, the first command with X11 (it needs `xclip`), the second with Wayland (`wl-clipboard`):
   ```bash
   tr -d '\n' < .deltasleuth/tokens/first-run | xclip -selection clipboard
   ```
   ```bash
   tr -d '\n' < .deltasleuth/tokens/first-run | wl-copy
   ```
3. Click the field, paste, and press "Sign in". The field hides what you paste.
4. Empty the clipboard. On macOS:
   ```bash
   pbcopy < /dev/null
   ```
   On Linux, the first command with X11, the second with Wayland:
   ```bash
   xclip -selection clipboard < /dev/null
   ```
   ```bash
   wl-copy --clear
   ```

The script's closing block prints the pair for your desktop. Not verified here: the four Linux
commands, and the page after sign-in in a browser: by its source it shows a bar (DeltaSleuth, Runs,
Readiness, Specs, Schedules, Pre-checks, Licence, `first-run`, "Sign out") above a page headed "Runs".
If the panel stays and a line appears under the button, see "If something goes wrong".

You are then signed in as the token `first-run`. Its role, `spec_author`, may read results, start runs
and upload comparison specs, and it expires after 90 days. What you do is recorded under its name.

## 5. Run `ds` commands

`ds` is DeltaSleuth's command line. Here it runs in a short-lived container that Docker then removes.

1. Define a short form for this terminal. `$(id -un)` is your user name, which `ds` records.
   ```bash
   alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
   ```
2. List the API tokens. The list never shows a token itself.
   ```bash
   ds token list
   ```
   You should see one line; `<you>` is your user name, and `last used` shows a time once you signed in:
   ```text
    Container deltasleuth-db-1  Running
   T-f66c5874ffb089ed (first-run)  spec_author  active  expires 2027-01-03T16:47:46+00:00  owner '<you>'  created by local:<you>@deltasleuth-app  last used -
   ```

The alias lasts until the terminal closes and works only in this folder. `ds --help` lists every command.

## 6. Stop and start

1. Stop the three containers. Everything is kept: the data, the tokens and the settings.
   ```bash
   docker compose stop
   ```
2. Start them again. `--profile mocks` includes the mocks; `-d` gives the terminal back.
   ```bash
   docker compose --profile mocks up -d
   ```
3. Read the app's newest log lines. Add `-f` to follow the log, and leave with Ctrl-C.
   ```bash
   docker compose logs --tail 5 app
   ```

## 7. Update

1. Fetch the newer source. It prints `Already up to date.` when there is none.
   ```bash
   git pull
   ```
2. Run the script again. It builds the new source and restarts the app and the mocks on it; your
   files, your data and your token stay.
   ```bash
   sh deploy/up.sh
   ```

Not verified here: a `git pull` that brings new commits. The update was tried by changing one source
file and running the script, which printed `Recreated` for the app and the mocks and kept the token.

## 8. Remove the installation

Do this only when you no longer want this installation. It deletes the data, the settings, the secrets
and the token, and it cannot be undone.

1. Check which installation this folder acts on. It prints the Compose project: `name: deltasleuth`.
   ```bash
   docker compose config | grep '^name:'
   ```
2. Delete the containers, the network and the data.
   ```bash
   docker compose down -v
   ```
3. Delete the image.
   ```bash
   docker image rm deltasleuth:source
   ```
4. Leave the folder.
   ```bash
   cd ..
   ```
5. Delete the folder, with your settings, secrets and token.
   ```bash
   rm -rf IntelliDiff
   ```

## A second installation on the same machine

The script takes over no other folder's installation. A second one lives in a clone of its own, with
its own project name, port and network, which three options give; the [appendix](A1-install-by-hand.md),
section 3.1, finds free values. They need Docker Compose 2.24.4 or later. This chapter's run was one:
```bash
sh deploy/up.sh --project dsguide --port 18000 --subnet 172.31.10.0/24
```
It printed `Compose project "dsguide"` and `http://localhost:18000`; the outputs above show the default
names in their place. Every command of this chapter works unchanged in such a folder but one, which
names the image: `docker image rm dsguide:source`. Before a command that stops or deletes, run the
check of section 8, step 1: it prints the project the folder you are in acts on, here `name: dsguide`.

## If something goes wrong

**`error: Docker is not running, or this account may not use it`**
Start Docker Desktop (macOS) or the Docker service (Linux, with an account that may run `docker`).

**`error: Compose project "deltasleuth" on this machine was started from <folder>/deploy: this script
takes over no other installation. For a second one here: sh deploy/up.sh --project <name> --port <port>
--subnet <a.b.c.0/24>`**
Another folder already runs an installation under this name. Go on there, or make this folder a second
installation with the three options above.

**`error: port 8000 of this machine answers already: choose another with --port N (for a first
installation on this machine that option alone is enough)`**, or the same with `is published by the
container <name>` in place of `answers already`
A program or a container holds the port. Give `--port` a free one, for example 18000: alone, or with
the other two options beside another DeltaSleuth installation. Not verified here: `--port` alone.

**`error: the subnet 172.30.0.0/24 is the Docker network <name>'s: choose another with --subnet
a.b.c.0/24`**
Another Docker network holds the range: give `--subnet` a free one (the appendix, section 3.1).

**`cross-origin request refused`** under the "Sign in" button
The page was opened at another address than `http://localhost:8000`, for example
`http://127.0.0.1:8000`. **`invalid or expired API token`** means the paste was not the token.

**`sh: deploy/up.sh: No such file or directory`** (macOS; Linux words it differently), or **`error:
deploy/docker-compose.yml is not in this folder: cd to the DeltaSleuth checkout, then sh
deploy/up.sh`**, or **`no configuration file provided: not found`**, or **`command not found: ds`**
You are not in the installation's folder, or the alias of section 5 is not defined in this terminal.
`cd` into `IntelliDiff`, and define the alias again.

## Reference

- [A1-install-by-hand.md](A1-install-by-hand.md): every step by hand, more tokens, and every message.
- [FIRSTRUN.md](../mvp/FIRSTRUN.md): what the script and the sign-in page promise.
- [DEPLOY.md](../mvp/DEPLOY.md), an office installation; [AUTH.md](../mvp/AUTH.md), roles and tokens.

Next: [chapter 3, your first comparison](03-first-comparison.md), on the mocks that are now running.
