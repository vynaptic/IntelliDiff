# 2. Run from source

DeltaSleuth compares what two sources hold and reports the differences key by key. A source can be a REST
service, a database table, or a folder of CSV or Parquet files. This chapter starts it on your own machine
from a copy of its source code, to try it alone or to work on its code. DeltaSleuth itself runs as programs
that you start in terminal windows. Only its PostgreSQL 16 database runs in a Docker container, or on a
server that you already have. It answers on this machine only, without sign-in. If others need it, follow
[chapter 1](01-install-docker.md).

The commands were run on macOS 27.0.1 (Apple silicon, zsh) on 2026-10-05. Outputs are real and shortened.
Not verified here: Linux.

Two readers leave the main path early. If this machine already runs another DeltaSleuth, or the source comes
from a folder on this machine and not from its repository, read "A second installation on the same machine"
before section 2: it changes four commands. If the machine has no Docker, or you would rather use a
PostgreSQL 16 server of your own, section 5 sends you to "With a PostgreSQL server of your own".

## 1. What you need

You need git, uv, Node with npm, Docker and OpenSSL, a browser on this machine, and internet access to PyPI
and the npm registry. Each step prints a version. `command not found` means that the tool is missing: its
own site says how to install it. Not verified here: installing a tool, which this chapter leaves out.
The versions used here were git 2.54.0, uv 0.11.21, Node 24.13.1, npm 11.8.0, Docker 29.1.3 and OpenSSL
3.6.2. Only Node states a lowest version (step 3); older versions of the others were not tried.

1. Check git, which copies the source to your machine.
   ```bash
   git --version
   ```
2. Check uv, which installs the Python packages, runs commands with them, and finds or downloads Python 3.13.
   ```bash
   uv --version
   ```
3. Check Node, which builds the web UI. It must be 20.19 or a later 20.x, or 22.12 or later.
   ```bash
   node --version
   ```
4. Check npm, the package installer that comes with Node.
   ```bash
   npm --version
   ```
5. Check that Docker, which runs the database, is running: Docker Desktop on macOS, the Docker service on
   Linux. Skip this step if you will use a PostgreSQL 16 server of your own (section 5 says how).
   ```bash
   docker info --format '{{.ServerVersion}}'
   ```
   You should see one version number, here `29.1.3`. An error in its place means that Docker is not running.
6. Check OpenSSL, which makes the database password. LibreSSL, which macOS ships under the same name, will do.
   ```bash
   openssl version
   ```

## 2. Get the source

1. Copy DeltaSleuth's git repository, which is named IntelliDiff, into a folder of that name. There is no
   installer. `<repository URL>` is the address of the repository that you were given.
   ```bash
   git clone <repository URL> IntelliDiff
   ```
   You should see `Cloning into 'IntelliDiff'...` first. Not verified here: a clone over a network. The
   source of this chapter's run came from a folder: "A second installation on the same machine" has that
   command, and one more to run right after this section.
2. Go into the folder. It is called the checkout from here on. Every command of this chapter is run in it.
   ```bash
   cd IntelliDiff
   ```
3. Print the full path of the checkout and note it: where a later command says `<checkout>`, type this path.
   ```bash
   pwd
   ```
   You should see one path that ends in `/IntelliDiff`.

## 3. Install the Python part

1. Install DeltaSleuth and its packages. uv fetches them from PyPI, the Python package index, into `.venv`.
   ```bash
   uv sync
   ```
   You should see several lines, among them `Resolved 111 packages` and, before the list of packages,
   `Installed 107 packages in 170ms`, with your own times. Not verified here: uv fetching Python.

## 4. Build the web UI

1. Install the packages that the build needs from the npm registry. `--prefix web` makes npm work in `web`.
   ```bash
   npm --prefix web ci
   ```
   You should see a line that starts `added 24 packages` and, last, `found 0 vulnerabilities`.
2. Build the UI, a set of static files that the server hands to your browser, into the folder `web/dist`.
   ```bash
   npm --prefix web run build
   ```
   You should see `✓ built in 146ms` as the last line, with your own time.

## 5. Start a PostgreSQL 16 database

DeltaSleuth keeps its specs, runs, results and audit log in this database. It is not one of the sources being
compared. Every file of this installation goes into the folder `.deltasleuth` in the checkout. Git ignores it.
Without Docker, do steps 1 and 2 here and then "With a PostgreSQL server of your own", after section 11.

1. Create the folder, closed to the other users of this machine.
   ```bash
   mkdir -p -m 700 .deltasleuth
   ```
2. Write a random password for the database into a file. It never appears on your screen. Do this step
   once: writing the file again later does not change the password of a database that already exists.
   ```bash
   openssl rand -hex 24 > .deltasleuth/db_password
   ```
3. Start PostgreSQL in a container named `deltasleuth-devdb`. It listens on port 5432 of this machine only,
   has a user and a database both named `deltasleuth`, and reads the password from the file of step 2.
   ```bash
   docker run -d --name deltasleuth-devdb -p 127.0.0.1:5432:5432 -e POSTGRES_USER=deltasleuth -e POSTGRES_DB=deltasleuth -e POSTGRES_PASSWORD_FILE=/run/secrets/db_password -v "$PWD/.deltasleuth/db_password:/run/secrets/db_password:ro" postgres:16-alpine
   ```
   You should see one line of 64 letters and digits, the id of the new container, and nothing after it. If an
   error comes after the id or in its place, look it up under "If something goes wrong" before you go on:
   beside another installation from source, the name and the port are taken ("A second installation on the
   same machine" has the command with other values).
   Not verified here: a machine without the image `postgres:16-alpine`, where Docker downloads it first.
4. Check that the database accepts connections. If it answers `no response`, wait a few seconds and repeat.
   ```bash
   docker exec deltasleuth-devdb pg_isready -h 127.0.0.1 -U deltasleuth -d deltasleuth
   ```
   You should see `127.0.0.1:5432 - accepting connections`.
5. Write the address of the database into a file. The address contains the password, so DeltaSleuth reads it
   from this file. The command joins the text before the password, the password and the text after it.
   ```bash
   { printf 'postgresql://deltasleuth:'; tr -d '\n' < .deltasleuth/db_password; printf '@127.0.0.1:5432/deltasleuth'; } > .deltasleuth/db_dsn
   ```

## 6. Write the settings

1. Copy the developer settings example, which ships with the source. Every `ds` command and the server read
   this one settings file. It is used as copied, and its comments explain each line.
   ```bash
   cp deploy/config/deltasleuth.dev.example.yaml .deltasleuth/deltasleuth.yaml
   ```
2. Tell the commands in this terminal where the settings file is. `DS_CONFIG` holds a path, not a secret.
   It lasts until you close the terminal: every new terminal that runs a `ds` command needs this step.
   ```bash
   export DS_CONFIG="$PWD/.deltasleuth/deltasleuth.yaml"
   ```

## 7. Prepare the workspace and the database

1. Create the workspace and its two keys. `ds` is DeltaSleuth's command-line program, and `uv run` runs it
   with the Python environment of the checkout. The first `ds` command can stay silent for several seconds.
   ```bash
   uv run ds init
   ```
   You should see one line. For a try-out, leave the two key files where they are.
   ```text
   workspace ready: tokeniser key <checkout>/.deltasleuth/workspace/tokeniser.key, keyring <checkout>/.deltasleuth/workspace/kek; keep a copy of both somewhere safe: a lost key cannot be made again
   ```
2. Create DeltaSleuth's tables in the database.
   ```bash
   uv run ds migrate
   ```
   You should see two long lines that start with `{`, then `migrations applied: 1, 2, 3, 4, 5, 6, 7, 8, 9,
   10, 11, 12, 13, 14, 15, 16, 17`. A line that starts with `{` is an audit record of a change. You can ignore
   such lines in this chapter.

## 8. Start the mock services

1. Open a second terminal window and go to the checkout. For `<checkout>`, type the path of section 2, step 3.
   ```bash
   cd "<checkout>"
   ```
2. Start the four practice services that ship with the source: the pairs `linker` and `customer`, each with
   an old variant, `a`, and a new one, `b`, which disagree on purpose. They stay in the foreground. Their
   four ports are fixed: beside another installation from source, see "A second installation on the same
   machine".
   ```bash
   uv run python -m mocks.run all
   ```
   You should see:
   ```text
   linker a: http://127.0.0.1:18081
   linker b: http://127.0.0.1:18082
   customer a: http://127.0.0.1:18091
   customer b: http://127.0.0.1:18092
   Ctrl-C stops all four; POST /__reset on each forgets its call counts.
   ```

## 9. Start the server and open the UI

1. Open a third terminal window and go to the checkout, as in step 1 of section 8.
   ```bash
   cd "<checkout>"
   ```
2. Tell this terminal where the settings file is.
   ```bash
   export DS_CONFIG="$PWD/.deltasleuth/deltasleuth.yaml"
   ```
3. Start the server: one process that holds the API, the web UI and the worker that carries out comparisons.
   It listens on `127.0.0.1`, port 8000, stays in the foreground and writes its log to this terminal. If
   chapter 1's installation runs on this machine, it holds that port: add `--port 18001`, or another free
   port, and open the UI at that port in step 4.
   ```bash
   uv run ds serve
   ```
   You should see these two lines, with `INFO` lines and audit records between and after them:
   ```text
   warning: developer mode (no auth settings): every local user and process acts as admin; never expose this port, directly or through a reverse proxy or port forward
   INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
   ```
4. Open `http://127.0.0.1:8000/` in a browser on this machine.
   You should see the Runs screen. A yellow notice at the top starts "Developer mode: no sign-in". Below it
   is a row of links (Runs, Readiness, Specs, Schedules, Pre-checks, Admin, Licence) that ends with your
   account, your host name and `(dev:loopback)`. Then come a form titled "New run" and the word "None.".

**What developer mode means.** The settings have no `auth` block, so there is no sign-in: whoever opens the
address is the administrator. In return the server listens on `127.0.0.1` only, refuses to start on an
address that other machines can reach, and refuses a request that came through a proxy. Every account and
program on this machine can still change everything here, so never make the port reachable from elsewhere.

## 10. Run a first comparison

A spec is a file that describes one comparison: which two targets, which keys, and how to compare the answers.
`mocks/customer/spec.yaml` ships with the source and compares 40 customer records between two mock services.

1. In the first terminal, start the comparison and wait for it to end. It took 5 seconds here.
   ```bash
   uv run ds run mocks/customer/spec.yaml
   ```
   You should see two audit records, then:
   ```text
   run R-35T6XJJMTR7RN2JETFHVJ0X3VT started
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 40, compared 40, match 36, different 4, indeterminate 0, cancelled 0, outstanding 0
     KNOWN_DIFF 2, MATCH 35, MISMATCH 2, NOT_FOUND_B 1
   ```
   Your run id differs. The second line says who does the work: the running server carries out the run, and
   this command waits. The 4 differing keys are expected: the two variants disagree on purpose.
2. In the browser, reload the page. A table with one row for the run now shows below the form.
3. Click the run id in that row.
   You should see the page of the run: "Run", the run id and `completed`, a line that starts "Unapproved
   spec version", `40/40 keys`, the counts of step 1, and the tables Root causes, Results and Findings. A
   key is shown as a token such as `tok:identifier:v1:...`, never as the customer number, and so are
   account and phone numbers. Other values, for example a currency, are shown as they are.

## 11. Stop, start again, start over, remove

**Stop everything.**
1. In the third terminal, press Ctrl-C to stop the server. Its last line holds `Finished server process`.
2. In the second terminal, press Ctrl-C to stop the four mock services. They print nothing more.
3. Stop the database. Its data is kept.
   ```bash
   docker stop deltasleuth-devdb
   ```

**Start again later.**
1. Start the database. Docker must be running (step 5 of section 1).
   ```bash
   docker start deltasleuth-devdb
   ```
2. Start the mock services (section 8) and the server (section 9). Section 7 is not repeated.
3. In a new first terminal, first go to the checkout (section 8, step 1) and do step 2 of section 6 again.

**Start over with an empty database.** This deletes every run and result. The keys in `.deltasleuth/workspace`
and the database belong together, so both are replaced. Run no `ds` command between the steps.
1. Stop the server with Ctrl-C in the third terminal.
2. Remove the database container together with its data.
   ```bash
   docker rm -f -v deltasleuth-devdb
   ```
3. Delete the stored answers and the keys: these two folders only, never the whole of `.deltasleuth`.
   ```bash
   rm -rf .deltasleuth/evidence .deltasleuth/workspace
   ```
4. Do steps 3 to 5 of section 5 again, then section 7, then start the server. The other files stay.

**Remove this installation.** If the folder also holds chapter 1's installation, remove that one first.
1. Stop the server and the mock services with Ctrl-C, and close the second and the third terminal.
2. In the first terminal, remove the database container together with its data.
   ```bash
   docker rm -f -v deltasleuth-devdb
   ```
3. Leave the checkout.
   ```bash
   cd ..
   ```
4. Delete the checkout, the folder `IntelliDiff` of section 2, with every file of this installation in it.
   ```bash
   rm -rf IntelliDiff
   ```
What stays does no harm: the image `postgres:16-alpine`, the caches of uv and npm, a Python that uv fetched.

## With a PostgreSQL server of your own

Use this section in place of steps 3 to 5 of section 5 when the machine has no Docker or you would rather
use a PostgreSQL 16 server that you already run. You need `psql`, PostgreSQL's command-line client, on this
machine and an account on the server that may create users and databases. DeltaSleuth needs a user that may
log in with a password and an empty database that this user owns, and no other right: it creates one
schema, named `ds`, in that database and installs no extension.

1. Do steps 1 and 2 of section 5: the folder and the password file.
2. In the checkout, start `psql` connected to your server. `<host>` and `<port>` are where the server
   listens (5432 unless that was changed), and `<administrator>` is the account. psql may ask for that
   account's password, which it does not show.
   ```bash
   psql -h <host> -p <port> -U <administrator> -d postgres
   ```
   You should see a line that starts `psql (16.` and, last, the prompt `postgres=#`. Steps 3 to 7 are typed
   at that prompt.
3. Create the user.
   ```sql
   CREATE ROLE deltasleuth LOGIN;
   ```
   You should see `CREATE ROLE`.
4. Read the password file into a psql variable. psql answers nothing and does not show the password.
   ```sql
   \set pw `cat .deltasleuth/db_password`
   ```
5. Give the user that password.
   ```sql
   ALTER ROLE deltasleuth PASSWORD :'pw';
   ```
   You should see `ALTER ROLE`.
6. Create the empty database.
   ```sql
   CREATE DATABASE deltasleuth OWNER deltasleuth;
   ```
   You should see `CREATE DATABASE`.
7. Leave psql.
   ```sql
   \q
   ```
8. Write the address file, with your server's host and port in place of `<host>` and `<port>`.
   ```bash
   { printf 'postgresql://deltasleuth:'; tr -d '\n' < .deltasleuth/db_password; printf '@<host>:<port>/deltasleuth'; } > .deltasleuth/db_dsn
   ```
9. Go on with section 6.

**If someone else made the user and the database for you**, do step 1 of section 5 only, skip the steps above
and create the file `.deltasleuth/db_dsn` in a text editor, with this one line:

```text
postgresql://<user>:<password>@<host>:<port>/<database>
```

Write seven characters of the password in another form: `%25` for `%`, `%40` for `@`, `%2F` for `/`, `%20`
for a space, `%23` for `#`, `%3F` for `?` and `%3A` for `:`. One line break at the end of the file is allowed.

**Section 11 with a server of your own.** There is no container to stop, start or remove. To start over,
stop the DeltaSleuth server, then replace the database in place of step 2 of "Start over": start psql as in
step 2 above and type these two lines. Then do step 3 of "Start over" and section 7.

```sql
DROP DATABASE deltasleuth;
```
```sql
CREATE DATABASE deltasleuth OWNER deltasleuth;
```

While the DeltaSleuth server still runs, the first line is refused after about five seconds with
`ERROR:  database "deltasleuth" is being accessed by other users` and a line that starts `DETAIL:` and counts
the other sessions.

Not verified here: a PostgreSQL installed directly on a machine, and a psql installed on this one, which has
none. Everything above was run with the psql 16.14 of a PostgreSQL container that stood in for the server.
psql was started in a folder that held `.deltasleuth`, with `127.0.0.1` and `5432` as host and port and the
server's own administrator, `postgres`, and it asked for no password. The user it made had no special right.

## A second installation on the same machine

The values in this chapter are the defaults. They clash with another DeltaSleuth on the same machine: the
Docker installation of chapter 1 holds port 8000, and another installation from source holds the container
name, port 5432 and the four mock ports. This chapter's commands were run on a machine that already ran chapter 1's
installation, so they used the values below. The outputs above are shown with the defaults put back and with
the folder's path written as `<checkout>`. Nothing else was changed.

| What | Default | Value used | Where it is typed |
|---|---|---|---|
| Source | the repository URL | the path of a checkout already on the machine | `git clone` |
| Database container | `deltasleuth-devdb` | `dsguide-devdb` | every `docker` command |
| Database port | 5432 | 15432 | `docker run` and the address file |
| Server port | 8000 | 18001 | `ds serve` and the address of the UI |
| Mock ports | 18081, 18082, 18091, 18092 | the defaults, which were free | nowhere |

The four commands that changed are below. `<path of the existing checkout>` is the folder that already
holds the source. `--pull never` makes Docker use the image that is already on the machine. The port inside
the container stays 5432, so `pg_isready` names 5432 whatever port you chose. The UI was opened at
`http://127.0.0.1:18001/`.

```bash
git clone <path of the existing checkout> IntelliDiff
```
```bash
docker run -d --pull never --name dsguide-devdb -p 127.0.0.1:15432:5432 -e POSTGRES_USER=deltasleuth -e POSTGRES_DB=deltasleuth -e POSTGRES_PASSWORD_FILE=/run/secrets/db_password -v "$PWD/.deltasleuth/db_password:/run/secrets/db_password:ro" postgres:16-alpine
```
```bash
{ printf 'postgresql://deltasleuth:'; tr -d '\n' < .deltasleuth/db_password; printf '@127.0.0.1:15432/deltasleuth'; } > .deltasleuth/db_dsn
```
```bash
uv run ds serve --port 18001
```

**A clone from a folder copies committed files only.** It prints `done.` as its second and last line. Changes
in that folder that are not committed, and files that git does not track yet, are not in the clone. To bring
the changed files over, run this command in the new checkout right after section 2. It was run here and
printed nothing. When the existing checkout holds no such changes, it answers
`error: No valid patches in input (allow with "--allow-empty")`, and nothing is missing.

```bash
git -C <path of the existing checkout> diff HEAD | git apply
```

**Other ports for the mock services.** `python -m mocks.run all` takes no other ports than its own four, so
two installations from source cannot both run it. One mock service alone takes any port. These steps move
the customer pair, which section 10 uses, to ports 28091 and 28092. They were run here as well.

1. In the second terminal, in place of step 2 of section 8, start variant `a`.
   ```bash
   uv run python -m mocks.run customer --variant a --port 28091
   ```
   You should see `======== Running on http://127.0.0.1:28091 ========` and `(Press CTRL+C to quit)`.
2. In one more terminal, go to the checkout (section 8, step 1) and start variant `b`.
   ```bash
   uv run python -m mocks.run customer --variant b --port 28092
   ```
3. In the first terminal, save a copy of the targets file, which with the product's own list
   ([DEPLOY.md](../mvp/DEPLOY.md), section 7) is what DeltaSleuth may call, with the two ports changed.
   ```bash
   sed -e 's|127.0.0.1:18091|127.0.0.1:28091|' -e 's|127.0.0.1:18092|127.0.0.1:28092|' mocks/targets.yaml > .deltasleuth/targets.yaml
   ```
4. Look at the addresses in the copy.
   ```bash
   grep base_url .deltasleuth/targets.yaml
   ```
   You should see the linker pair as it was and the customer pair on the new ports:
   ```text
       base_url: http://127.0.0.1:18081
       base_url: http://127.0.0.1:18082
       base_url: http://127.0.0.1:28091
       base_url: http://127.0.0.1:28092
   ```
5. Write the settings file again from the example, with one line changed: `targets_file: ../mocks/targets.yaml`
   becomes `targets_file: targets.yaml`, which names the copy.
   ```bash
   sed -e 's|^targets_file: .*|targets_file: targets.yaml|' deploy/config/deltasleuth.dev.example.yaml > .deltasleuth/deltasleuth.yaml
   ```
6. If the server is running, stop it with Ctrl-C in the third terminal and start it again. It reads the
   settings and the targets only when it starts.
   ```bash
   uv run ds serve --port 18001
   ```
7. Run the comparison of section 10. It gave the result shown there. Before step 6 it ended with
   `INCONCLUSIVE 40`, because the running server still called the old ports.

## How this differs from chapter 1

Here you start and stop two programs and one database container yourself, `ds` runs the code in your
checkout, and there is no sign-in. [Chapter 1](01-install-docker.md) builds an image, keeps three containers
running and has people sign in with an API token: use it when others need DeltaSleuth or it reads real data.

## For people who change the code: lint and tests

This section needs a git checkout: a release kit holds no `tests/` and no `tools/`. `ds` runs the code in the
checkout: an edit under `deltasleuth/` takes effect when you stop the server and start it again, and one
under `web/src` after step 2 of section 4 and a page reload.

1. Run the lint, which checks the Python code for mistakes and style. You should see `All checks passed!`.
   ```bash
   uv run ruff check deltasleuth mocks tests examples tools
   ```
2. Run the tests of the web UI. You should see a line for each test, then `ℹ pass 92` and `ℹ fail 0`.
   ```bash
   npm --prefix web test
   ```
3. Run the Python tests.
   ```bash
   uv run pytest -q
   ```
   Not verified here: the full suite takes about 26 minutes and was run separately. The README says that its
   database tests start containers of their own and are skipped without Docker or the images.

## If something goes wrong

Each message below was met or provoked on this build, except where an entry says otherwise. Paths are shown
with `<checkout>`, and ports and names with the defaults. Several lines in one block are separate messages
unless the text says that they come together.

**A tool is missing.**
```text
zsh: command not found: docker
```
The tool named at the end is not installed (section 1). bash words it `bash: docker: command not found`.
Without Docker, use "With a PostgreSQL server of your own".

**Docker is installed but not running.**
```text
failed to connect to the docker API at unix://<socket>; check if the path is correct and if the daemon is running: dial unix <socket>: connect: no such file or directory
```
`<socket>` is a path on your machine. Start Docker Desktop (macOS) or the Docker service (Linux), wait until
step 5 of section 1 prints a version, and repeat the command. Not verified here: Docker itself stopped. The
message was provoked with a socket that does not exist, so the words after the last colon may differ.

**A command was pasted with its placeholder.**
```text
cd: no such file or directory: <checkout>
zsh: no such file or directory: repository
```
A word in angle brackets stands for a value of your own. In `cd "<checkout>"`, type the path that step 3 of
section 2 printed between the quotes. In `git clone <repository URL> IntelliDiff`, type the address of the
repository. bash words the two `bash: cd: <checkout>: No such file or directory` and
`bash: repository: No such file or directory`.

**Node is too old.**
```text
SyntaxError: The requested module 'node:util' does not provide an export named 'styleText'
node: bad option: --experimental-transform-types
```
Install a newer Node, then do section 4 again. The build asks for Node 20.19 or a later 20.x, or 22.12 or
later (`web/package.json`), and the README asks for 22.12 or later for the tests. Not verified here: this
entry, because no Node older than 24.13.1 was on this machine. The two messages were recorded earlier
with Node 20.11.1: the first near the end of `npm --prefix web run build`, the second at the
end of `npm --prefix web test`, after `npm --prefix web ci` had printed lines that start `npm WARN EBADENGINE`.

**`docker run` says the port is taken.**
```text
docker: Error response from daemon: failed to set up container networking: driver failed programming external connectivity on endpoint deltasleuth-devdb (0e1b65f510521867664572a8020bc43a794020d15ee2879c201cf9cc57ea7b7e): Bind for 127.0.0.1:5432 failed: port is already allocated
docker: Error response from daemon: ports are not available: exposing port TCP 127.0.0.1:5432 -> 127.0.0.1:0: listen tcp4 127.0.0.1:5432: bind: address already in use
```
Docker prints the container's id first, then one of these. The first means that another container holds
the port, the second that a program outside Docker holds it, for example a PostgreSQL installed on the
machine. After the second, Docker adds `Run 'docker run --help' for more information`, which you can ignore.
The container now exists but does not run, and `docker start` would start it without the port.
Not verified here: Docker Engine on Linux, which may word the messages differently.
1. Remove the container that was left behind.
   ```bash
   docker rm -f -v deltasleuth-devdb
   ```
2. Do step 3 of section 5 again with another port, for example 15432, in place of the first `5432` in
   `-p 127.0.0.1:5432:5432`. The second `5432` is the port inside the container and stays.
3. Do steps 4 and 5 of section 5, with the same port in place of `5432` in step 5.

**`docker run` says the name is in use.**
```text
docker: Error response from daemon: Conflict. The container name "/deltasleuth-devdb" is already in use by container "4481a64c7c461d67f438318b8d25fde3ab6d16aeeb6192b6dbc0c76d7fd3a14b". You have to remove (or rename) that container to be able to reuse that name.
```
A container of that name exists, and no new one was made. Docker adds `Run 'docker run --help' for more
information`, which you can ignore. Look at the container:
```bash
docker ps -a --filter name=deltasleuth-devdb
```
Under `STATUS`, `Up` with `127.0.0.1:5432->5432/tcp` under `PORTS` means that the database runs, and there
is nothing to do. `Created`, or `Up` with only `5432/tcp` under `PORTS`, means that a failed `docker run`
left the container without its port: follow "`docker run` says the port is taken". `Exited` means that the
database was stopped. Start it:
```bash
docker start deltasleuth-devdb
```

**A `docker exec` command says the container is not running.**
```text
Error response from daemon: container a82c4dc36267614ed101c0717677c58e78e5933dee6c1b7abc15448901f141e0 is not running
```
Look at the container as in the entry above and do what it says for the state you see.

**A `ds` command says there is no settings file, or cannot read it.**
```text
error: no settings file: pass --config or set DS_CONFIG
error: invalid settings <checkout>/web/.deltasleuth/deltasleuth.yaml: cannot read <checkout>/web/.deltasleuth/deltasleuth.yaml: No such file or directory
```
The first means that `DS_CONFIG` is not set in this terminal, for example because the terminal is new. The
second names the path that `DS_CONFIG` holds: the variable was set from another folder, here `web`, or step 1
of section 6 was skipped. In both cases go to the checkout and do section 6 again.

**`uv run` cannot find `ds`.**
```text
error: Failed to spawn: `ds`
  Caused by: No such file or directory (os error 2)
```
The two lines come together. You are not in the checkout, or `uv sync` has not been run in it.

**The settings file is refused.**
```text
error: invalid settings <checkout>/.deltasleuth/deltasleuth.yaml: targets_file: Field required; target_file: Unexpected keyword argument
```
A name in the file is mistyped, here `target_file` for `targets_file`. Copy the example again (section 6).

**A command names a path under `/var/lib` or `/run/secrets`, or asks for `DS_OPERATOR`.**
```text
error: Permission denied: /var/lib/deltasleuth
error: cannot read secret file /run/secrets/db_dsn: No such file or directory
error: cannot read secret file /run/secrets/kek: No such file or directory
error: set DS_OPERATOR to your user name (docs/mvp/AUTH.md §4.2)
```
The settings file is a copy of `deploy/config/deltasleuth.example.yaml`, which is the Docker installation's.
`ds init`, `ds migrate`, `ds runs` and `ds run` answered with these four, in that order. Copy
`deploy/config/deltasleuth.dev.example.yaml` instead (section 6).

**The targets file is not found.**
```text
error: / [document_unreadable] cannot read <folder>/../mocks/targets.yaml: No such file or directory
```
`<folder>` is the folder that holds the settings file, and it is not `.deltasleuth` inside the checkout. The
paths in the settings are looked up from the file's own folder. Do section 6 again. `ds init` does not read
the targets file: if it ran with the misplaced settings file, it made a folder `workspace` with two keys
beside that file. Delete that folder, which nothing uses.

**The address file is missing or refused.**
```text
error: cannot read secret file <checkout>/.deltasleuth/db_dsn: No such file or directory
error: secret file <checkout>/.deltasleuth/db_dsn is empty or has a BOM or surrounding whitespace
```
The first means that step 5 of section 5 was skipped. The second means that the file is empty or has a blank
line or a space before or after the address. Do step 5 of section 5 again.

**A command says the keys are missing.**
```text
error: tokeniser key <checkout>/.deltasleuth/workspace/tokeniser.key not found; run 'ds init' first
```
Step 1 of section 7 was skipped. Run it:
```bash
uv run ds init
```

**The database cannot be reached.**
```text
error connecting in 'pool-1': connection failed: connection to server at "127.0.0.1", port 5432 failed: could not receive data from server: Connection refused
error connecting in 'pool-1': connection failed: connection to server at "127.0.0.1", port 5432 failed: Connection refused
	Is the server running on that host and accepting TCP/IP connections?
error: the database is unreachable (PoolTimeout); check database_dsn_ref
```
The database is not running. The command is not stuck: it tries for about 30 seconds, prints one or both of
the `error connecting` lines and ends with the last line. Start the database and repeat the command:
```bash
docker start deltasleuth-devdb
```
A server that was already running when the database stopped fills its log with such lines, and its health
address, `http://127.0.0.1:8000/v1/health`, answers
`{"detail": "the database is not answering, or the pool gave no connection in 3 s"}`.
When the database is back, the server works again without a new start.

**The database refuses the user or the password.**
```text
error: the database answered and asked for a password, then refused the connection: check the user, the password and the database name in the file database_dsn_ref names; if they are right, the server, the role or the database is at its connection limit, or a pooler cannot reach its server
```
A PostgreSQL answered, but it does not accept the user, the password or the database name in
`.deltasleuth/db_dsn`. DeltaSleuth tries three times, two seconds apart, before it says so: here the message
came about six seconds after the command was typed. With a server of your own, correct the address file.
With the container:
1. Do step 5 of section 5 again, and repeat the command that failed. The address file then holds the
   password that is in the password file now.
2. If the message stays, ask Docker for the port of the container.
   ```bash
   docker port deltasleuth-devdb
   ```
   If it prints `5432/tcp -> 127.0.0.1:5432`, the password file was written again after the container was
   created, and the container still holds the older password. Follow "Start over with an empty database"
   (section 11), which deletes the runs made so far. If it prints nothing, the container has no port on
   this machine, and another PostgreSQL answered. Follow "`docker run` says the port is taken".

**An address file typed by hand holds a password with special characters.**
```text
error: the file database_dsn_ref names does not hold an address the driver can read: percent-encode special characters in the user and the password
```
The password holds a `%`, an `@`, a `/` or a space, typed as it is. Write it in its other form, as "With a
PostgreSQL server of your own" says. The message comes at once and shows nothing of the address.

**The mock services say a port is in use.**
```text
error: port 18081 is in use (linker a): nothing was started
```
They are already running in another terminal, or another program holds the port named. None of the four was
started. "A second installation on the same machine" shows how to use other ports.

**The server warns that the web UI is not built, and the browser shows a 404 message.**
```text
warning: the web UI is not built (<checkout>/web/dist is missing): / answers 404 and only the API under /v1 is served; run 'npm --prefix web ci' and 'npm --prefix web run build' in the checkout, then start ds serve again
{"detail": "the web UI is not built on this server; its API is under /v1"}
```
The first is among the server's start-up lines, the second is what the browser shows. Do section 4, then
stop the server and start it again.

**The server says its port is in use.**
```text
error: cannot listen on 127.0.0.1:8000: the port is in use; stop the program that holds it, or pass --port
```
Another program holds port 8000, for example the Docker installation of chapter 1, or a server you started
before. Start this one on a free port of your choice, `<port>`, and open the UI at that port:
```bash
uv run ds serve --port <port>
```
A port is a number up to 65535. A larger one is refused at once, in a box under two lines that start
`Usage:` and `Try`: `Invalid value for '--port': 70000 is not in the range 0<=x<=65535.`

**Developer mode refuses an address or a request.**
```text
error: serving on 0.0.0.0 needs the auth settings (docs/mvp/AUTH.md §1)
{"detail": "developer mode refuses proxied requests"}
{"detail": "missing or invalid CSRF token"}
```
The first ends `ds serve --host` with an address that other machines can reach: developer mode serves this
machine only, and the Docker installation of chapter 1 is the way to serve others. The second answers a
request that came through a proxy. The third answers a script's request that changes something (POST or
DELETE) without the header `X-CSRF-Token: dev`. The browser UI sends that header by itself.

**`ds run` prints a line about another worker.**
```text
another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
```
This is not an error. The running server carries out the run. Without a running server, `ds run` carries out
the run itself and does not print this line.

**The comparison ends, but nothing was compared.**
```text
completed
  total 40, compared 0, match 0, different 0, indeterminate 40, cancelled 0, outstanding 0
  INCONCLUSIVE 40
```
The three lines come together. Nothing answered at the addresses in the targets file. Either the mock
services are not running: start them (section 8) and run the comparison again. Or the settings or the
targets file changed while the server ran: stop the server and start it again. To ask one mock service
whether it is up:
```bash
curl -fsS http://127.0.0.1:18081/__health
```
You should see `{"status":"ok","pair":"linker","variant":"a"}`. zsh, the default shell on macOS, prints a `%`
after such an answer, which is not part of it. When nothing listens on the port, the answer is
`curl: (7) Failed to connect to 127.0.0.1 port 18081 after 0 ms: Couldn't connect to server`.

**A run pauses because the keyring lacks a key.**
```text
warning: the keyring lacks K-7053c962926dbd39, which wraps 3 data key(s): restore the line from the vault and restart
run R-35T6W9W6WB91DXEVDQ5EXJK2SR started
run R-35T6W9W6WB91DXEVDQ5EXJK2SR paused: the evidence address key needs K-7053c962926dbd39, which the keyring lacks
paused (run_paused_key_missing)
error: the run is paused
```
The lines come together, the third three times. The folder `.deltasleuth/workspace` was deleted and created
anew, but the database was set up with the old keys. `ds runs` and `ds serve` print the warning too. For a
try-out, follow "Start over with an empty database" (section 11), which replaces both.

## Reference

- [README.md](../../README.md): what DeltaSleuth does and the layout of the source.
- [docs/mvp/RUNBOOK.md](../mvp/RUNBOOK.md): every command from a first spec to a report.
  [Setup](../mvp/RUNBOOK.md#setup) covers the settings file and the two keys,
  [Commands, from start to report](../mvp/RUNBOOK.md#commands-from-start-to-report) the commands,
  [Outcomes, verdicts and counters](../mvp/RUNBOOK.md#outcomes-verdicts-and-counters) the words in a result,
  and [Troubleshooting](../mvp/RUNBOOK.md#troubleshooting) the full table of messages.
- [Try it on the mock pairs](../mvp/RUNBOOK.md#try-it-on-the-mock-pairs) in the same document: more to do
  with the mock services, among it the second spec that ships, `mocks/linker/spec.yaml`. Its two services
  answer some keys differently the first time they are asked, so a second run gives other counts than the
  first until each has been reset, as that section describes. Here the first run ended with `DRIFT 1,
  INCONCLUSIVE 3, MATCH 52, MISMATCH 4, NOT_FOUND_B 1` and the second with `INCONCLUSIVE 3, MATCH 53,
  MISMATCH 4, NOT_FOUND_B 1`.
- [docs/mvp/FIRSTRUN.md](../mvp/FIRSTRUN.md): the developer settings example, the one command for the mock
  services, and the exact wording of the messages a first run can meet, with the reasons behind them.
- [docs/mvp/AUTH.md](../mvp/AUTH.md): developer mode, the two sign-in modes, roles and the audit log.
- [docs/mvp/RETENTION.md](../mvp/RETENTION.md): the keyring, the tokeniser key and how long raw data is kept.
- [docs/mvp/LICENCE.md](../mvp/LICENCE.md): why this build needs no licence.
- [Chapter 1](01-install-docker.md) and [docs/mvp/DEPLOY.md](../mvp/DEPLOY.md): the Docker installation,
  step by step and in full detail.
- [deploy/config/deltasleuth.dev.example.yaml](../../deploy/config/deltasleuth.dev.example.yaml) and
  [mocks/targets.yaml](../../mocks/targets.yaml): the settings file and the targets file this chapter uses.
- [mocks/customer/expected.json](../../mocks/customer/expected.json) and
  [mocks/linker/expected.json](../../mocks/linker/expected.json): the outcome each mock key is meant to have.
