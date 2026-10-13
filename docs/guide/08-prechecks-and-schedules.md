# 8. Pre-checks, the schema guard and schedules

This chapter covers three things an operator does around a comparison of two database tables, and one
that works for every kind of comparison:

1. A **table pre-check** counts and adds up two tables before any row is compared (section 1).
2. A pre-check of **two whole schemas**, whose tables are paired by name, and a starter spec drafted
   from a pair (section 2). A schema is a named group of tables in a database; a spec is the file that
   tells DeltaSleuth what to compare.
3. The **schema guard** notices that a table changed since the last run and holds the run until a
   person has decided (section 3).
4. A **schedule** starts a run of a stored spec by itself, for example every night (section 4).

You need the installation of [chapter 1](01-install-docker.md): the three containers, the address
`http://localhost:8000`, the token file `.deltasleuth/tokens/first-run` and the `ds` alias of its
section 5. Run every command from the installation's folder, `IntelliDiff`.

**In a second installation.** If yours is a second installation on its machine (chapter 1, "A second
installation on the same machine"), two names and one number of this chapter are yours to change.
Every `docker compose` command and every `ds` command works unchanged, because the file `.env` in your
folder names your project. The commands that begin `docker run`, `docker exec`, `docker start`,
`docker network` or `docker rm` do not read `.env`: they act on exactly the names you type.

- Write your project name in place of `deltasleuth` in the container name `deltasleuth-practice-db`
  and in the network name `deltasleuth_default`. They stand in section 1, "Before you start", step 3
  (both names) and steps 4 and 5, in the two address files of its steps 6 and 7
  (`@deltasleuth-practice-db:5432`), and in every later `docker exec`, `docker start`, `docker
  network` and `docker rm`.
- Write your port in place of 8000 in every address `http://localhost:8000`.
- Leave the word `deltasleuth` alone everywhere else: in paths such as `/var/lib/deltasleuth/p0`,
  `/etc/deltasleuth/targets.yaml` and `.deltasleuth/tokens/first-run`, and in
  `local:<you>@deltasleuth-app`.

This command prints the network name of the installation your folder holds:
```bash
docker compose ps -a --format '{{.Networks}}' app
```
You should see `deltasleuth_default`, or your project name followed by `_default`. This chapter's own
run was a second installation with the project `dsg08`: the command printed `dsg08_default`, the
practice database was named `dsg08-practice-db`, and with those two names every step worked.

Typed unchanged in a second installation, step 3 either fails, because the first installation has a
container of that name already, or puts the practice database on the first installation's network,
where your app does not find it. Not verified here in that form, because it would touch another
installation. The two symptoms were provoked inside this chapter's own installation instead, a name
that is taken and a practice database that is not on the app's network, and section 1's "If something
goes wrong" quotes them with their fixes.

**How this chapter was checked.** Every command was run on 2026-10-05, on macOS with Docker Desktop, in
an empty second installation (project `dsg08`, port 18400). `git log -1 --oneline` in your folder shows your commit. The outputs are printed with
the default names. Ids and times will differ on your machine, and `<you>` stands for your user name.
Most `ds` commands first print a Docker line (` Container deltasleuth-db-1  Running`) and lines that
start with `{"id":`, copies of DeltaSleuth's audit records; the outputs below leave those lines out.
The web UI steps were written from the pages' source, and the request behind each button was sent with
curl. Not verified here: the clicks themselves in a browser.

A word in angle brackets, such as `<pre-check id>`, stands for a value of your own and is explained
where it first appears.

Three words used throughout:

- A **target** is a database or service that DeltaSleuth may read, declared by name in
  `deploy/config/targets.yaml` (a service also in the product itself: [DEPLOY.md](../mvp/DEPLOY.md),
  section 7).
- **Side a** and **side b** are the two things a spec compares.
- The **worker** is the background part of the app that does the reading and starts scheduled runs.
  In this installation it runs inside the app container, `deltasleuth-app-1`.

## 1. A table pre-check before the first run

### What it is for

A table pre-check answers "where should we look first?" before any row is compared. For each pair of
tables it reads the two tables' descriptions (column names, types, which columns may be empty, the
primary key) and a few totals (row counts, empty values, sums, shortest and longest texts), and lists
the pairs worst first. A team that has many table pairs to migrate uses it to rank them, and an
operator uses it to learn whether a comparison would be refused before starting one.

It never fetches a row or a value, and it is not proof that two tables hold the same data.

### Before you start

The pre-checks and the schema guard read database tables, so they need a database to practise on.
[Chapter 6](06-tables.md) builds one from the example file `examples/tables/seed.sql` and explains
every step. It is a second PostgreSQL container named `deltasleuth-practice-db` on the installation's
own network, without a published port, so only the installation's containers reach it.

**If you did chapter 6** and the practice database still runs, go to step 12. Your two address files
then name that chapter's read-only account, `ds_reader`; the pre-checks of sections 1 and 2 and the
steps of section 3 were run again with that account and gave the same results. The figures printed in
this chapter are those of freshly loaded example data. If chapter 6's
exercises changed your tables and you want the same figures, load the data again with step 5 and then
repeat chapter 6, section 2, step 3, which gives `ds_reader` its rights again: loading the data
recreates both databases, and the rights go with them.

**If you did not**, these are chapter 6's steps in short, with the database's administrator account
in the address files.

1. Create a password for the practice database. The command writes it to a file and shows nothing.
   ```bash
   (umask 077; openssl rand -hex 24 > deploy/secrets/practice_db_password)
   ```
2. Let the containers read the file, like the secret files of chapter 1.
   ```bash
   chmod 444 deploy/secrets/practice_db_password
   ```
3. Start the practice database. `POSTGRES_PASSWORD_FILE` names the file; it does not hold the password.
   In a second installation, first write your project name into the two names after `--name` and
   `--network`, as "In a second installation" above says.
   ```bash
   docker run -d --name deltasleuth-practice-db --network deltasleuth_default -e POSTGRES_PASSWORD_FILE=/run/secrets/practice_db_password -v "$PWD/deploy/secrets/practice_db_password:/run/secrets/practice_db_password:ro" postgres:16-alpine
   ```
   You should see one line of 64 letters and digits, the new container's id.
4. Wait about five seconds, then check that it answers.
   ```bash
   docker exec deltasleuth-practice-db pg_isready -U postgres
   ```
   You should see:
   ```text
   /var/run/postgresql:5432 - accepting connections
   ```
5. Load the example data: two databases, `ds_demo_legacy` (an old customer system, table
   `crm.customers`) and `ds_demo_platform` (its replacement, table `public.users`), with 1,000
   customers and seven planted differences.
   ```bash
   docker exec -i deltasleuth-practice-db psql -q -U postgres -v ON_ERROR_STOP=1 -f - < examples/tables/seed.sql
   ```
   You should see two notices and nothing else:
   ```text
   psql:<stdin>:20: NOTICE:  database "ds_demo_legacy" does not exist, skipping
   psql:<stdin>:21: NOTICE:  database "ds_demo_platform" does not exist, skipping
   ```
6. Write the address of the first database to a secret file. The password goes from one file into the
   other and is never shown. The address names the practice database by its container name, after
   the `@`: in a second installation, write your project name there too, in this step and the next.
   ```bash
   (umask 077; { printf 'postgresql://postgres:'; tr -d '\n' < deploy/secrets/practice_db_password; printf '@deltasleuth-practice-db:5432/ds_demo_legacy'; } > deploy/secrets/crm_legacy_dsn)
   ```
7. Write the address of the second database.
   ```bash
   (umask 077; { printf 'postgresql://postgres:'; tr -d '\n' < deploy/secrets/practice_db_password; printf '@deltasleuth-practice-db:5432/ds_demo_platform'; } > deploy/secrets/platform_users_dsn)
   ```
8. Let the containers read the two files.
   ```bash
   chmod 444 deploy/secrets/crm_legacy_dsn deploy/secrets/platform_users_dsn
   ```
9. Tell Docker Compose to hand the two files to the app. The example file
   `examples/tables/compose.yaml` does that; this command adds it to the list in `.env`. Run it once.
   Chapter 6 reaches the same end by editing the site file; either way works, and an installation with
   both started as well.
   ```bash
   sed -i.bak 's|^COMPOSE_FILE=.*|&:examples/tables/compose.yaml|' .env
   ```
10. Check the line, then delete the copy the command left.
    ```bash
    grep '^COMPOSE_FILE' .env
    ```
    You should see the fourth file at the end of the line:
    ```text
    COMPOSE_FILE=deploy/docker-compose.yml:deploy/docker-compose.site.yml:deploy/docker-compose.source.yml:examples/tables/compose.yaml
    ```
    ```bash
    rm .env.bak
    ```
11. Apply the change. Compose makes the app container anew; the data stays.
    ```bash
    docker compose --profile mocks up -d
    ```
    You should see, among other lines:
    ```text
     Container deltasleuth-app-1  Recreated
     Container deltasleuth-app-1  Started
    ```
12. Check that DeltaSleuth reaches the first table. The targets `crm-legacy` and `platform-users` are
    already declared in `deploy/config/targets.yaml` as chapter 1 copied it.
    ```bash
    ds target check crm-legacy --table crm.customers
    ```
    You should see the server and the table's columns:
    ```text
    target crm-legacy: postgresql 16.14, character set UTF8, TLS no, session ok
    crm.customers (table, consistency snapshot)
      customer_id    integer                      integer    key
      email_address  text                         text       key
      full_name      text                         text       key
      phone          text                         text       key
      status         text                         text       key
      country        character(2)                 char       key
      balance        numeric(12,2)                decimal    key
      created_at     timestamp without time zone  timestamp
      branch_code    text                         text       key
    ```
    The third column is the class, the family of types DeltaSleuth reads the column as. The schema
    guard of section 3 speaks of it. The word `key` in the last column says that a column of this
    kind could serve as a key, the value that tells which row of side a belongs to which row of
    side b. It does not say that the column is a key of this table, and this chapter does not use it.

Nothing needs to be switched on in the settings file: pre-checks work as installed.

### In the web UI

1. Click "Specs" in the bar at the top. In the panel "Upload a spec", press the file chooser and pick
   `examples/tables/spec.yaml` from the `IntelliDiff` folder. Its text appears in the box below.
   If chapter 6 stored this spec already, go to step 3.
2. Press "Save". Beside the button you should see "Saved as spec #1." (the number can differ), and the
   table above gains a row with the name `crm-customers-vs-platform-users` and the version `1.0.0`.
3. In that row, in the column "Table pre-check", click "Pre-check…". The page "Table pre-checks"
   opens. Under its list of pre-checks, in the panel "New table pre-check", the spec is already
   selected in the list "Specs", where it reads `crm-customers-vs-platform-users 1.0.0 (#1)`, with
   the number of step 2. The panel below that one, "New table pre-check of two schemas, paired by
   name", belongs to section 2.
4. Leave "Level" at `columns` and press "Pre-check". A page headed "Table pre-check" and an id that
   starts with `PC-` opens. Under a sentence that begins "A table pre-check counts and adds up" you
   should see a grey line ending "finished · 1 done" within a few seconds (the page asks again every
   three seconds), the line "1 pair: 1 with differing row counts" in bold, and a table with the columns
   "Pair", "Label", "Verdict", "Differing figures", "Rows a", "Rows b" and "Observed". Its one row
   reads: `1`, `crm-customers-vs-platform-users`, `rows differ`, `4`, `989`, `990`, and "rows differ:
   side a 989, side b 990 (1 fewer on side a)".
5. Click the `1` in the column "Pair". A panel "Pair 1: crm-customers-vs-platform-users" opens under
   the table: the pair's lines, a table "Schema" (columns "Column", "Code", "Side a", "Side b") and a
   table "Columns" (columns "Column", "Classes", "Figures"). The browser's Back button closes it.
6. To keep the result, press "precheck.csv", "precheck-columns.csv" or "precheck.pdf" beside the
   "Verdict" list. The browser saves a file named after the pre-check, for example
   `PC-35T7Q45N3H54D7869X99MTASP7-precheck.csv`.
7. To pre-check every stored spec of two tables at once, go back with "All pre-checks", tick "every
   stored spec of two tables (the newest version of each name)" in "New table pre-check", and press
   "Pre-check".

While a pre-check is not finished, a button "Cancel" stands beside the report buttons and stops it.

The button "Run a comparison" at the bottom of the pair's panel starts a full comparison of that spec
at once, without asking. Section 3 uses a run of this spec.

### On the command line

1. Copy the example spec into the installation. `ds` runs inside a container and does not see the
   files of your folder; `/var/lib/deltasleuth/p0` is the place for files handed to `ds`.
   ```bash
   docker compose cp examples/tables/spec.yaml app:/var/lib/deltasleuth/p0/tables-spec.yaml
   ```
   You should see:
   ```text
    deltasleuth-app-1 copy examples/tables/spec.yaml to deltasleuth-app-1:/var/lib/deltasleuth/p0/tables-spec.yaml Copied
   ```
2. Run the pre-check. The command stores the spec first, then waits until the pair is read.
   ```bash
   ds precheck run /var/lib/deltasleuth/p0/tables-spec.yaml
   ```
   You should see:
   ```text
   A table pre-check counts and adds up; it does not compare rows. Tables whose figures agree can hold different rows, and figures can differ only because two databases store a value differently. It says where to look first. It is observed, not evidence, readiness or a sign-off, and no evidence pack contains it: run a comparison to compare rows.
   PC-35T7Q45N3H54D7869X99MTASP7  specs  level columns  by local:<you>@deltasleuth-app 2026-10-05T21:51:58+00:00  finished
   1 pair: 1 with differing row counts
   1  crm-customers-vs-platform-users  rows differ: side a 989, side b 990 (1 fewer on side a)
   ```
   Another line says `another worker is processing keys (a ds serve, ds worker or ds run on this
   database): it does the work, and this one takes over only if it stops`. That is normal here: the
   app's worker read the pair, and the command only waited for it.
3. Open the pair. `<pre-check id>` is the id at the start of the second line above.
   ```bash
   ds precheck show <pre-check id> --pair 1
   ```
   You should see the sentence again, then (some column lines left out):
   ```text
   pair 1 of PC-35T7Q45N3H54D7869X99MTASP7: crm-customers-vs-platform-users (crm-legacy crm.customers, platform-users users): rows differ
   rows differ: side a 989, side b 990 (1 fewer on side a)
   2 of 7 compared columns differ in a figure: phone (nulls), balance (sum, min)
   schema: 5 differences: country is character(2) on side a and text on side b; created_at is timestamp without time zone on side a and timestamp with time zone on side b; branch_code is on side a only; marketing_opt_in is on side b only; updated_at is on side b only
   not applied: this spec's normalisers, tolerances, known-difference rules and compare.include or exclude; a comparison applies them
   side a: postgresql 16.14, crm.customers, in 1 snapshot at 2026-10-05 21:51:59 UTC; side b: postgresql 16.14, public.users, in 1 snapshot, 0 s later: not read at one moment
     balance: nulls: side a 0, side b 0 (same)
     balance: sum: differs (figures withheld: side a declares a filter)
     balance: min: differs; max: same (values are never shown)
     country: length: side a 2 to 2, side b 2 to 2 characters (same)
     created_at: not compared: typeMap converts this column per row
     email: empty: same (figures withheld: side a declares a filter and $.email is protected by the spec's security policy)
     phone: nulls: side a 0, side b 1 (differs)
     status: length: side a 6 to 9, side b 6 to 9 characters (same)
   ```
4. Ask for the deepest level. The spec is stored now, so name it instead of the file.
   ```bash
   ds precheck run --spec crm-customers-vs-platform-users --level distinct
   ```
   You should see the same lines with `level distinct`, and a new id: every `ds precheck run` makes a
   new pre-check.
   ```text
   PC-35T7Q4ECBSFTCD0R6PRH0V9ESW  specs  level distinct  by local:<you>@deltasleuth-app 2026-10-05T21:52:03+00:00  finished
   1 pair: 1 with differing row counts
   1  crm-customers-vs-platform-users  rows differ: side a 989, side b 990 (1 fewer on side a)
   ```
5. Open the pair of this new pre-check. Here `<pre-check id>` is the id that step 4 printed, not the
   one of step 2: with the older id you get the output of step 3 again.
   ```bash
   ds precheck show <pre-check id> --pair 1
   ```
   You should see more columns named in the third line, and one more line for each column, ending
   `distinct: …` (the other lines are those of step 3 and are left out here):
   ```text
   pair 1 of PC-35T7Q4ECBSFTCD0R6PRH0V9ESW: crm-customers-vs-platform-users (crm-legacy crm.customers, platform-users users): rows differ
   rows differ: side a 989, side b 990 (1 fewer on side a)
   4 of 7 compared columns differ in a figure: email (distinct), name (distinct), phone (nulls, distinct), balance (sum, min)
     balance: distinct: side a 989, side b 989 (same)
     country: distinct: side a 2, side b 2 (same)
     email: distinct: differs (figures withheld: side a declares a filter and $.email is protected by the spec's security policy)
     status: distinct: side a 2, side b 2 (same)
   ```
6. Write the reports into the same shared place. Either id will do; the output below is that of the
   pre-check of step 2.
   ```bash
   ds precheck report <pre-check id> --out /var/lib/deltasleuth/p0/precheck
   ```
   You should see three paths:
   ```text
   /var/lib/deltasleuth/p0/precheck/PC-35T7Q45N3H54D7869X99MTASP7-precheck.csv
   /var/lib/deltasleuth/p0/precheck/PC-35T7Q45N3H54D7869X99MTASP7-precheck-columns.csv
   /var/lib/deltasleuth/p0/precheck/PC-35T7Q45N3H54D7869X99MTASP7-precheck.pdf
   ```
7. Copy them to your folder.
   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/precheck ./precheck-report
   ```
   You should see a line that ends `to ./precheck-report Copied`. `precheck-report` then holds the
   three files. `…-precheck.csv` has one row for each pair, and its last column, `claim`, reads
   `observed; not parity`. `…-precheck-columns.csv` has one row for each pair, column and figure. Each
   file starts with the sentence of step 2.
8. List every pre-check, newest first.
   ```bash
   ds precheck
   ```
   You should see the sentence, then two lines for each pre-check:
   ```text
   PC-35T7Q4ECBSFTCD0R6PRH0V9ESW  specs  level distinct  by local:<you>@deltasleuth-app 2026-10-05T21:52:03+00:00  finished
     1 pair: 1 with differing row counts
   PC-35T7Q45N3H54D7869X99MTASP7  specs  level columns  by local:<you>@deltasleuth-app 2026-10-05T21:51:58+00:00  finished
     1 pair: 1 with differing row counts
   ```
9. For a nightly job outside DeltaSleuth, add `--fail-on`. The command then ends with status 1 when a
   pair is as bad as the word you give, or worse.
   ```bash
   ds precheck run --spec crm-customers-vs-platform-users --fail-on differs
   ```
   You should see the usual lines. Then ask the shell for the status the command ended with:
   ```bash
   echo $?
   ```
   You should see `1`, because the row counts differ. `--fail-on` takes `refused`, `differs` or
   `incomplete`.
10. Pre-check the newest version of every stored spec whose two sides are tables, in one go.
    ```bash
    ds precheck run --all-specs
    ```
    You should see one line for each such spec. With only this section's spec stored:
    ```text
    PC-35T7Q65ZVR7XDWPRSDHPFMFWW9  specs  level columns  by local:<you>@deltasleuth-app 2026-10-05T21:52:32+00:00  finished
    1 pair: 1 with differing row counts
    1  crm-customers-vs-platform-users  rows differ: side a 989, side b 990 (1 fewer on side a)
    ```

A pre-check that is not finished can be stopped. A pre-check of the practice tables finishes within a
second or two, so you will seldom be in time; the command is this:
```bash
ds precheck cancel <pre-check id>
```
Tried here on a pre-check that waited for a stopped practice database, it answered:
```text
cancelled PC-35T7Q6RV7EVJ8NDFVYS8JCGXQK
```
The pair's line then read `not checked: the pre-check was cancelled`. Sent to a pre-check that has
finished, the command answers with the `has ended` line quoted under "If something goes wrong".

### How to read the result

**The levels.** Each level includes the one before it.

| Level | What it reads from each side |
|---|---|
| `schema` | The table's description only, no row: column names, declared types, whether a column may be empty, the primary key. It also says whether a run of the spec would be refused |
| `counts` | Also the number of rows, and the rows that have no key |
| `columns` (the default) | Also, for each compared column: how many values are empty (`nulls`); for texts the empty strings and the shortest and longest length; for numbers the sum, minimum and maximum; for dates the minimum and maximum |
| `distinct` | Also the keys that several rows share, and the number of different values in each column |

At level `schema` this pair read `1 pair: 1 with schema differences only`; at `counts`, `columns` and
`distinct` it read `1 pair: 1 with differing row counts`.

**The verdict of a pair.** Each pair gets the first verdict of this list that applies, and pairs are
listed worst first. The words are the ones the screen and `ds precheck` print. This run met
`unpaired`, `refused`, `not read`, `rows differ` and `schema differs`; the others are described as the
runbook describes them.

| Verdict | In plain words | Look next at |
|---|---|---|
| `unpaired` | A table exists on one side only (section 2) | The table's name |
| `refused` | A comparison of this spec would be refused. The line quotes the reason | What the reason names |
| `not read` | A side could not be read, for example the database did not answer | The database, then pre-check again |
| `rows differ` | The two tables hold different numbers of rows | A comparison, which says which keys |
| `keys null or repeated` | A side has rows without a key, or several rows with one key | The key column |
| `columns not compared` | A comparison would leave a column out: it exists on one side only, or its two types are of different families | The columns the line lists |
| `values differ` | A figure of a compared column differs | The column the line names |
| `incomplete` | A check of the level you asked for was skipped by a limit | A narrower pre-check |
| `schema differs` | Only the descriptions differ: declared types, sizes, empty or not, keys | Whether that matters to you |
| `no difference observed in the checks made (not parity)` | None of the above | A comparison, still |

The totals line counts pairs by verdict (`1 pair: 1 with differing row counts`). It is never a
percentage or a score.

**The lines of a pair**, with the example of step 3:

- `rows differ: side a 989, side b 990 (1 fewer on side a)`. The spec filters side a to open accounts,
  so 989 of its 1,000 rows count.
- `2 of 7 compared columns differ in a figure: phone (nulls), balance (sum, min)` names the columns to
  look at and which figure of each differs. The number in "Differing figures" on the screen, 4, counts
  these three figures and the row counts.
- `schema: 5 differences: …` lists what differs in the two descriptions.
- `not applied: …` reminds you that the spec's rules for ignoring harmless differences are used by a
  comparison only, never by a pre-check.
- `side a: postgresql 16.14, … not read at one moment` says when each side was read. The two sides are
  read seconds apart, so a table that changes meanwhile can differ for that reason alone.
- A column line such as `phone: nulls: side a 0, side b 1 (differs)` gives both figures.
  `sum: differs (figures withheld: side a declares a filter)` gives only the verdict: a figure that
  could reveal something about protected or filtered data is compared and not shown. Minimum and
  maximum are never shown (`values are never shown`).
- `created_at: not compared: typeMap converts this column per row` means the spec converts this column
  row by row, which a pre-check does not do.

**What `same` does not mean.** `same` says that two figures were equal. It does not say the rows are
equal: two swapped values change no count, length, minimum, maximum or sum. A differing figure is not
a defect either: here `phone (nulls)` differs because one customer exists only on side b. Run a
comparison to compare rows.

### Limits

- A pre-check reads database tables only (PostgreSQL, MySQL, Oracle). A spec that compares services
  is refused, as "If something goes wrong" shows. By the contract a spec with a CSV or Parquet file on
  one side is refused too; that was not tried here.
- By the contract, one pre-check takes at most 500 pairs, an installation holds at most 20 unfinished
  pre-checks, and a pre-check is deleted 90 days after it was made (`retention.standard_days` in the
  settings file). None of the three was reached here.
- A pre-check is not evidence, and no evidence pack contains it.
- Only the worker reads the tables. While the app is stopped, `ds precheck run` reads them itself; with
  `--no-wait` it only queues the pre-check and prints `read by 'ds worker' or 'ds serve'`.
- No schedule runs pre-checks (section 4 schedules runs only). For a nightly pre-check, let your own
  scheduler run the command of step 9.
- Three bounds can be changed in `deploy/config/deltasleuth.yaml` with this line, which shows the
  defaults; remove the `# ` and restart the app as chapter 1's appendix, section 5.1, describes. Not
  verified here: changing them.
  ```text
  # precheck: {sessions_per_target: 1, statement_timeout_s: 900, distinct_max_rows: 20000000}
  ```
  A statement that runs longer than `statement_timeout_s` seconds is given up, and a table with more
  rows than `distinct_max_rows` gets no repeated-key or distinct counts.
- Roles: a token of role `operator` may pre-check stored specs and cancel a pre-check; `viewer` may
  only read and download, and the forms are not shown to it. Chapter 1's token, `spec_author`, may do
  everything in this chapter. Checked here with one token of each role.
- Not verified here, because this installation has no single sign-on: under single sign-on, a spec
  version that declares a filter and is not yet approved is read at level `schema` only.

### If something goes wrong

**`docker: Error response from daemon: Conflict. The container name "/deltasleuth-practice-db" is
already in use by container "<id>". You have to remove (or rename) that container to be able to reuse
that name.`**, from step 3 of "Before you start"
A container of that name exists already, and nothing was started. If it is your own practice
database, from chapter 6 or from an earlier try, go on with step 4. If step 4 then answers `Error
response from daemon: container <id> is not running`, the practice database is stopped: start it
with the `docker start` command further down this list, and repeat step 4. If yours is a second
installation and you typed the command unchanged, the name belongs to the first installation: leave
that container alone and run step 3 again with your project name in both names. Provoked here by
running step 3 a second time, and step 4 on the stopped container.

**`error: target crm-legacy: cannot connect to target crm-legacy: failed to resolve host
'deltasleuth-practice-db': [Errno -2] Name or service not known`**, from step 12, while the practice
database runs
The practice database is not on your installation's network, or the address files name it by another
name than the container's. Ask which network it is on:
```bash
docker inspect --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' deltasleuth-practice-db
```
You should see the name that the command under "In a second installation" printed, here
`deltasleuth_default`. If you see another name or an empty line, connect the container to your
network and repeat step 12. Provoked here by taking the container off its network; after this
command, step 12 answered as printed there.
```bash
docker network connect deltasleuth_default deltasleuth-practice-db
```
If the network is right, the two address files name the practice database by another name than the
container's, for example `deltasleuth-practice-db` in a second installation. Write them again. Delete
them first, because they are read-only and the shell would answer `permission denied:
deploy/secrets/crm_legacy_dsn`:
```bash
rm -f deploy/secrets/crm_legacy_dsn deploy/secrets/platform_users_dsn
```
Repeat steps 6 to 8 with the right name, then restart the app, which still holds the deleted files:
```bash
docker compose restart app
```
Tried here with a wrong name and then the right one. Until the restart, a pre-check read by the app
answered `not checked: side a's read failed (SecretError)`; after it, the pre-check passed.

**`error: / [document_unreadable] cannot read examples/tables/spec.yaml: No such file or directory`**
You gave `ds` a path of your own folder. Copy the file in first (step 1) and give the path under
`/var/lib/deltasleuth/p0`.

**`error: spec 6 (mock-customer-v1-vs-v2 1.0.0) is not a pair of tables: a table pre-check reads two
table sides`**
The spec compares services. Pre-check a spec whose two sides are database tables.

**`error: no spec is named nope`**
No stored spec has that name. The "Specs" page lists the names.

**`error: give spec_ids, spec_names or all_specs, and only one of them`**
`ds precheck run` was given nothing to pre-check. The message uses the API's words: give a file path,
`--spec <name>`, `--spec-id <number>` or `--all-specs`, and only one kind.

**`error: level must be schema, counts, columns or distinct`**
`--level` was given another word.

**`error: Read-only file system: /app/out`**
`--out` named a folder the container may not write to. Write under `/var/lib/deltasleuth/p0`.

**`1  crm-customers-vs-platform-users  not checked: side a could not be reached (OperationalError)`**,
after a wait of about two minutes, and **`error: target crm-legacy: cannot connect to target
crm-legacy: failed to resolve host 'deltasleuth-practice-db': [Errno -2] Name or service not known`**
from `ds target check`
The practice database is not running, for example after the machine restarted. Start it, then make a
new pre-check:
```bash
docker start deltasleuth-practice-db
```
You should see its name. If the container was running all along, the cause is the second entry of
this list.

**`1 pair: 1 a run would refuse`**, and the pair's line **`a run of this spec would be refused: side
a: table or view crm.customers not found or not visible (and 1 more)`**
The account in the address file cannot see the table. Met here after the example data was loaded
again while the address files named chapter 6's read-only account: loading recreates the databases,
and the account's rights go with them. Repeat chapter 6, section 2, step 3, then pre-check again.

**`error: table pre-check PC-NOPE not found (pre-checks are deleted 90 days after they are made)`**
The id is mistyped, or the pre-check is older than 90 days.

**`error: table pre-check PC-35T7Q45N3H54D7869X99MTASP7 has ended`**
`ds precheck cancel` was given a pre-check that had finished. Only an unfinished one can be cancelled.

**`error: set DS_OPERATOR to your user name (docs/mvp/AUTH.md §4.2)`**
The `ds` alias of chapter 1, section 5, is not defined in this terminal. Define it again.

**`{"detail": "needs the operator role"}`**, from the API
The token's role is `viewer`. Making or cancelling a pre-check needs `operator`.

**`no worker has taken this pre-check for 12 min: start ds worker, or ds serve`**, above the list of
`ds precheck`, and a pair that stays `pending`
The pre-check was queued with `--no-wait` and nothing reads it, because the app is stopped. Start the
app:
```bash
docker compose --profile mocks up -d
```
A `ds precheck run` without `--no-wait` also helps: it read the waiting pre-check together with its
own. Provoked here by stopping the app for 19 minutes.

## 2. Two whole schemas paired by name, and a draft spec

### What it is for

Before anyone has written a spec, this form of the pre-check takes two schemas, pairs their tables by
name, and pre-checks each pair. It tells a migration team which tables exist on one side only and
which pairs differ at first sight. For a pair it can also write a starter spec, so that the person who
writes specs (the spec author) does not begin from an empty file.

### Before you start

The practice database of section 1. Its two tables have different names (`crm.customers` and
`public.users`), so step 1 below shows what "no partner" looks like, and step 2 adds a table that has
one.

### In the web UI

1. Give side b a table that has a partner: run the command of step 2 of "On the command line" below
   once. The web UI cannot change the practice database.
2. Click "Pre-checks" in the bar. At the bottom, find the panel "New table pre-check of two schemas,
   paired by name". It is shown to tokens of role `spec_author` only.
3. In "Side a" choose `crm-legacy (postgresql)` and type `crm` in the field "Schema" beside it. In
   "Side b" choose `platform-users (postgresql)` and type `public` in its "Schema" field. Leave
   "Include" and "Exclude" empty.
4. Set "Level" to `columns` and press "Pre-check". The pre-check's page opens with one row for each
   table name. You should see the totals "2 pairs: 1 unpaired, 1 with differing row counts" and two
   rows: `users` with the verdict `unpaired` first, then `customers` with `rows differ`.
5. Click the number in the column "Pair" of the row `customers`. In the panel that opens, press "Draft
   a spec". The browser saves a file named like `PC-35T74Z71ZWFYRSTVCJ4YGYZZ3M-pair-1.draft.yaml`. The
   button is shown only for a pair of two tables that were both read.
6. To store the draft once you have reviewed it, use "Upload a spec" on the "Specs" page, as in
   section 1.

"Include" and "Exclude" take table-name patterns such as `customer*`, separated by spaces or commas;
they narrow which tables are paired.

### On the command line

1. Pre-check the two schemas as they are. A side is written `<target>:<schema>`.
   ```bash
   ds precheck run --a crm-legacy:crm --b platform-users:public
   ```
   You should see, after the sentence:
   ```text
   PC-35T74YJW3KWF99HXWW7QD7PKJP  catalogue  level counts  by local:<you>@deltasleuth-app 2026-10-05T19:09:21+00:00  finished
   2 pairs: 2 unpaired
   1  customers  no table on side b is named customers (side a: crm.customers; paired by name)
   2  users  no table on side a is named users (side b: public.users; paired by name)
   ```
2. Give side b a table of the same name as side a's: a copy of `public.users` under the name
   `customers`, with side a's column names. The third statement lets every account of the practice
   database read the new table, the read-only account of chapter 6 included.
   ```bash
   docker exec deltasleuth-practice-db psql -U postgres -d ds_demo_platform -c 'CREATE TABLE public.customers AS SELECT user_id AS customer_id, email AS email_address, name AS full_name, phone, status, country, balance, created_at FROM public.users' -c 'ALTER TABLE public.customers ADD PRIMARY KEY (customer_id)' -c 'GRANT SELECT ON public.customers TO PUBLIC'
   ```
   You should see:
   ```text
   SELECT 990
   ALTER TABLE
   GRANT
   ```
3. Pre-check again, one level deeper.
   ```bash
   ds precheck run --a crm-legacy:crm --b platform-users:public --level columns
   ```
   You should see:
   ```text
   PC-35T74Z71ZWFYRSTVCJ4YGYZZ3M  catalogue  level columns  by local:<you>@deltasleuth-app 2026-10-05T19:09:32+00:00  finished
   2 pairs: 1 unpaired, 1 with differing row counts
   2  users  no table on side a is named users (side b: public.users; paired by name)
   1  customers  rows differ: side a 1,000, side b 990 (10 fewer on side b)
   ```
4. Open the pair that has two tables.
   ```bash
   ds precheck show <pre-check id> --pair 1
   ```
   You should see (the column lines left out):
   ```text
   pair 1 of PC-35T74Z71ZWFYRSTVCJ4YGYZZ3M: customers (crm-legacy customers, platform-users customers): rows differ
   rows differ: side a 1,000, side b 990 (10 fewer on side b)
   2 columns would not be compared: created_at is a timestamp (timestamp without time zone) on side a and a zoned timestamp on side b: declare typeMap {as: timestamp, naiveTimezone: ...} on both; branch_code is on side a only
   2 of 6 compared columns differ in a figure: phone (nulls), balance (sum, min)
   schema: 7 differences: email_address is NOT NULL on side a and nullable on side b; full_name is NOT NULL on side a and nullable on side b; status is NOT NULL on side a and nullable on side b; country is character(2) on side a and text on side b; country is NOT NULL on side a and nullable on side b; balance is NOT NULL on side a and nullable on side b; created_at is NOT NULL on side a and nullable on side b
   side a: postgresql 16.14, crm.customers, in 1 snapshot at 2026-10-05 19:09:32 UTC; side b: postgresql 16.14, public.customers, in 1 snapshot, 0 s later: not read at one moment
   ```
5. Write a starter spec for that pair.
   ```bash
   ds precheck draft <pre-check id> --pair 1 --out /var/lib/deltasleuth/p0/drafts
   ```
   You should see the file's path:
   ```text
   /var/lib/deltasleuth/p0/drafts/PC-35T74Z71ZWFYRSTVCJ4YGYZZ3M-pair-1.draft.yaml
   ```
6. Copy it to your folder.
   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/drafts ./drafts
   ```
   The file in `drafts` holds, shortened here:
   ```yaml
   # DeltaSleuth ComparisonSpec DRAFT, from table pre-check PC-35T74Z71ZWFYRSTVCJ4YGYZZ3M pair 1:
   #     "customers", paired by name.
   # A starter, not an approved spec: it holds names and declared types only. Review, edit, then
   #     upload.
   metadata:
     name: customers
     version: 0.1.0
     owner: unassigned
   key:
     name: customer_id
     type: string
     class: identifier
   # Columns left out, each as a pre-check said it (a bridge to declare is never applied):
   #   "created_at is a timestamp (timestamp without time zone) on side a and a zoned timestamp on
   #     side b: declare typeMap {as: timestamp, naiveTimezone: ...} on both"
   #   "branch_code is on side a only"
   sides:
     a:
       role: baseline
       target: crm-legacy
       table:
         schema: crm
         name: customers
         keyColumns: [customer_id]
         columns: [balance, country, email_address, full_name, phone, status]
   # Every column is tokenised in results until the spec lists it under clear.
   security:
     unlisted: tokenize
   ```
7. Check the draft against the declared targets.
   ```bash
   ds spec validate /var/lib/deltasleuth/p0/drafts/<pre-check id>-pair-1.draft.yaml --targets /etc/deltasleuth/targets.yaml
   ```
   You should see:
   ```text
   ok: PC-35T74Z71ZWFYRSTVCJ4YGYZZ3M-pair-1.draft.yaml is valid (0 warning(s))
   ```

A draft is never stored by itself. Review it, give it a name, an owner and a version of your own, then
upload it on the "Specs" page.

### How to read the result

The lines and verdicts are those of section 1, with three differences.

- A table without a partner reads `no table on side b is named customers (side a: crm.customers;
  paired by name)`, with the verdict `unpaired`, the worst one, so it is listed first.
- No spec exists yet, so DeltaSleuth makes its own plan for each pair: the key is the primary key when
  both tables have the same one, and the compared columns are those both tables have under the same
  name. The line `2 columns would not be compared: …` lists what such a comparison would leave out,
  and why. Here `created_at` is stored without a time zone on one side and with one on the other; the
  line names the spec setting (`typeMap`) that bridges the two. [Chapter 6](06-tables.md) explains it.
- Every value counts as protected, because no spec says which columns may be shown. So a sum is never
  shown: `balance: sum: differs (figures withheld: paired by name, no security policy)`.

The `schema:` line of step 4 is long because the copy made in step 2 did not take over the rule that
these columns may not be empty (`NOT NULL`). A pre-check of two schemas exists to find differences of
that kind.

### Limits

- The levels are `schema`, `counts` (the default here) and `columns`. `distinct` needs a stored spec.
- Tables are paired by their lower-cased names only. A renamed table (`customers` and `users`) is two
  unpaired tables; compare those with a spec.
- By the contract: at most 500 pairs, and 5,000 tables or views a side; "Include" and "Exclude"
  (`--include`, `--exclude` on the command line) take up to 20 patterns each. Not verified here: the
  patterns and these bounds.
- Only a token of role `spec_author` may pre-check two schemas and draft a spec. An `operator` token
  was answered `{"detail": "needs the spec_author role"}` for both.
- A draft holds names and types only. It applies no bridge it mentions, and it lists every value as
  protected until you say otherwise.

### If something goes wrong

**`error: side a: the default schema of target crm-legacy holds no table or view the account can
read`**
The schema was left out (`--a crm-legacy`, or an empty "Schema" field), and the database's default
schema, `public`, is empty on that side. Name the schema: `--a crm-legacy:crm`.

**`error: side a: schema nope of target crm-legacy holds no table or view the account can read`**
The schema's name is mistyped.

**`error: two schemas paired by name take level schema, counts or columns: distinct counts need a
stored spec`**
`--level distinct` was given with `--a` and `--b`.

**`error: --a and --b come together, and without a spec`**
Only one side was given, or a spec was named as well.

**`error: target crm-old is not declared`**, or **`error: target customer-a is a service: a table
pre-check reads database targets`**
The name before the colon is not a database target of `deploy/config/targets.yaml`.

**`error: side a: cannot list the default schema of target crm-links (SecretError)`**
The target is declared, but the secret file that holds its address does not exist. The example
targets `crm-links` and `platform-links` are such placeholders.

**`error: pair 2 has no table on side a: there is nothing to draft`**, or **`error: pair 1 is a
stored spec (crm-customers-vs-platform-users 1.0.0): it needs no draft`**
A draft can be written only for a pair of two tables paired by name.

**The pre-check still reads `2 pairs: 2 unpaired` after step 2**
The account in your address files may not read the new table, so the pre-check does not see it: a
schema lists only what the account can read. Met here with chapter 6's read-only account and a table
made without the third statement of step 2. Give the right, then pre-check again:
```bash
docker exec deltasleuth-practice-db psql -U postgres -d ds_demo_platform -c 'GRANT SELECT ON public.customers TO PUBLIC'
```

## 3. The schema guard

### What it is for

Tables change: someone adds a column, widens a type, drops a field. The schema guard keeps a run from
quietly comparing something other than what was planned, or less than the last run compared. When a
run is created it remembers the shape of each table the run reads; when it finds the shape changed in
a way that matters, it pauses the run and waits for an operator to accept the change or cancel.

### Before you start

The practice database of section 1 and the spec file copied in section 1, step 1 of the command line.
Nothing is switched on: every run that reads a table is guarded.

### In the web UI

1. Click "Runs" in the bar. In the panel "New run", above the list of runs, choose
   `crm-customers-vs-platform-users 1.0.0 (#1)` in "Spec" (the number can differ),
   leave "Mode" at `batch` and the box "Keys, one per line" empty, and press "Start". The run's page
   opens. When its status reads `completed`, a grey line near the top reads "no change observed
   against the run's plan at its creation and each cycle's start (table sides only; a view's own
   tables, constraints and defaults are not read)". On the first run of a spec version a second grey
   line follows it: "no earlier run of this version recorded its tables' shapes: this run records
   them".
2. Change a table in the practice database with step 2 of the command line below. The web UI cannot
   do that.
3. The run's page has no "New run" panel, so click "Runs" in the bar again, then press "Start" in
   "New run" with the same choices. The run completes, and a grey line reads "side b gained column
   'loyalty_tier' since run R-…: this version does not compare it".
4. Make the second change, step 4 of the command line below, click "Runs" and press "Start" once
   more. This time the status reads `paused`, and a highlighted line reads "Paused by the
   schema guard before any table row was read: column 'country' was character(2) (char) in run R-… of
   this version and is text (text) now. Resume to accept the tables as they are, or cancel."
5. To accept the change, press "Resume (accept the changes)". The run goes on and completes, and two
   grey lines say what was accepted and by whom: the line about `country` again, and "the change of
   side a observed at cycle 1 was accepted by first-run on 2026-10-05; the run goes on with its plan".
6. To refuse it instead, press "cancel". The run ends as `cancelled`. Neither button asks for a
   confirmation.

On a run the guard paused for a change that must be undone first, the button reads "resume", and
pressing it shows the refusal quoted under "If something goes wrong".

### On the command line

1. Start a run of the spec. With no earlier run, it becomes the reference.
   ```bash
   ds run /var/lib/deltasleuth/p0/tables-spec.yaml
   ```
   You should see:
   ```text
   run R-35T752CTDK9JRB6TG0Z59S7QFZ started
   completed
     total 991, compared 991, match 985, different 6, indeterminate 0, cancelled 0, outstanding 0
     MATCH 985, MISMATCH 4, NOT_FOUND_A 1, NOT_FOUND_B 1
   ```
   A line that starts `warning: spec 1 (crm-customers-vs-platform-users 1.0.0) needs a four-eyes
   approval` is normal with token sign-in; [chapter 6](06-tables.md) explains the counts.
2. Make a harmless change: add a column to side b's table.
   ```bash
   docker exec deltasleuth-practice-db psql -U postgres -d ds_demo_platform -c 'ALTER TABLE public.users ADD COLUMN loyalty_tier text'
   ```
   You should see `ALTER TABLE`.
3. Run again with the command of step 1. You should see the same result, and one more line:
   ```text
   warning: side b gained column 'loyalty_tier' since run R-35T752CTDK9JRB6TG0Z59S7QFZ: this version does not compare it
   ```
   The guard warned and let the run go on.
4. Make a change that matters: turn side a's `country` column from `character(2)` into `text`.
   ```bash
   docker exec deltasleuth-practice-db psql -U postgres -d ds_demo_legacy -c 'ALTER TABLE crm.customers ALTER COLUMN country TYPE text'
   ```
   You should see `ALTER TABLE`.
5. Run again with the command of step 1. You should see:
   ```text
   run R-35T755C7HZBNTDNA5G5HGSZV4C started
   paused (run_paused_schema_drift)
     Paused by the schema guard before any table row was read: column 'country' was character(2) (char) in run R-35T753RD2JGE7VVGCFRJQS339E of this version and is text (text) now. Resume to accept the tables as they are, or cancel.
   error: the run is paused
   ```
   The command ends with status 2. The run exists and waits.
6. List the runs. The paused one carries the guard's line.
   ```bash
   ds runs
   ```
   You should see, at the top:
   ```text
   R-35T755C7HZBNTDNA5G5HGSZV4C  crm-customers-vs-platform-users 1.0.0  batch  paused  cycle 1
     Unapproved spec version: clear, population_filters were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
     Paused by the schema guard before any table row was read: column 'country' was character(2) (char) in run R-35T753RD2JGE7VVGCFRJQS339E of this version and is text (text) now. Resume to accept the tables as they are, or cancel.
   ```
7. Accept the change. No `ds` command resumes a run, so send the request the button sends. The token
   goes from its file through a pipe to curl and is never shown. `<run id>` is the paused run's id.
   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -X POST http://localhost:8000/v1/runs/<run id>/resume
   ```
   You should see:
   ```text
   {"run_id": "R-35T755C7HZBNTDNA5G5HGSZV4C", "status": "running"}
   ```
8. About fifteen seconds later, read what the guard recorded.
   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - http://localhost:8000/v1/runs/<run id> | grep -o '"lines": \[[^]]*\]'
   ```
   You should see:
   ```text
   "lines": ["column 'country' was character(2) (char) in run R-35T753RD2JGE7VVGCFRJQS339E of this version and is text (text) now", "the change of side a observed at cycle 1 was accepted by first-run on 2026-10-05; the run goes on with its plan"]
   ```
   `ds runs` now lists the run as `completed`. It is the reference for the next run.

To refuse a change instead of accepting it, send the request of step 7 with `cancel` in place of
`resume`:
```bash
{ printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -X POST http://localhost:8000/v1/runs/<run id>/cancel
```
Sent here to another run that the guard had paused, it answered:
```text
{"run_id": "R-35T80JS23N7WHBKHNE6F7V3DA5", "status": "cancelled"}
```
`ds runs` then lists that run as `cancelled  cycle 1  different 0 of 0`.

**A change while a run is going.** A run that repeats, called a soak run, is checked again at the
start of every round (cycle). This needs a copy of the spec that repeats every minute. Read steps 4
to 6 before you begin: the change of step 6 has to fall between the first cycle, which takes a few
seconds, and the second, which starts one minute after the run did.

1. Copy the example spec.
   ```bash
   cp examples/tables/spec.yaml tables-soak.yaml
   ```
2. Open `tables-soak.yaml` in a text editor. Change the line `  name: crm-customers-vs-platform-users`
   to `  name: crm-customers-soak`. Under the line `  concurrency: 2`, add these two lines, indented
   by two spaces like it:
   ```text
     cycleInterval: 1m
     stop: {maxCycles: 4}
   ```
3. Check your edit against the example.
   ```bash
   diff examples/tables/spec.yaml tables-soak.yaml
   ```
   You should see exactly these differences:
   ```text
   18c18
   <   name: crm-customers-vs-platform-users
   ---
   >   name: crm-customers-soak
   76a77,78
   >   cycleInterval: 1m
   >   stop: {maxCycles: 4}
   ```
4. Copy the file in.
   ```bash
   docker compose cp tables-soak.yaml app:/var/lib/deltasleuth/p0/tables-soak.yaml
   ```
   You should see a line that ends `/var/lib/deltasleuth/p0/tables-soak.yaml Copied`.
5. Start it as a soak run, then list the runs at once. `--no-wait` gives the terminal back.
   ```bash
   ds run /var/lib/deltasleuth/p0/tables-soak.yaml --soak --no-wait
   ```
   You should see:
   ```text
   run R-35T7QQA1CDNRKHTK0B52NDNEB2 started
   processed by 'ds worker' or 'ds serve'
   ```
   ```bash
   ds runs
   ```
   You should see, at the top, the run with its first cycle done and waiting for its second:
   ```text
   R-35T7QQA1CDNRKHTK0B52NDNEB2  crm-customers-soak 1.0.0  soak  running  cycle 2  different 6 of 991
   ```
   Here the line read so three seconds after the run was started. If yours does not yet say `cycle 2
   different 6 of 991`, run `ds runs` again before you go on.
6. Now, and before the first minute is over, rename a column the run reads.
   ```bash
   docker exec deltasleuth-practice-db psql -U postgres -d ds_demo_platform -c 'ALTER TABLE public.users RENAME COLUMN phone TO phone_number'
   ```
   You should see `ALTER TABLE`.
7. A minute after the start, list the runs again.
   ```bash
   ds runs
   ```
   You should see:
   ```text
   R-35T7QQA1CDNRKHTK0B52NDNEB2  crm-customers-soak 1.0.0  soak  paused  cycle 2  different 6 of 991
     Unapproved spec version: clear, population_filters were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
     Paused by the schema guard at the start of cycle 2: side b: column 'phone' of public.users is gone, or the role may no longer read it. Put it back and resume the run, or cancel it and start a new run, which plans the table as it is now.
   ```
   If you were slower than a minute, the line names a later cycle. If it says `cycle 1`, see "If
   something goes wrong".
8. Try to resume it with the resume command of step 7 of the first list. While the column is missing
   you should see:
   ```text
   {"detail": "run R-35T7QQA1CDNRKHTK0B52NDNEB2 cannot resume: side b: column 'phone' of public.users is gone, or the role may no longer read it"}
   ```
9. Put the column back.
   ```bash
   docker exec deltasleuth-practice-db psql -U postgres -d ds_demo_platform -c 'ALTER TABLE public.users RENAME COLUMN phone_number TO phone'
   ```
   You should see `ALTER TABLE`.
10. Resume again with the same command. You should see `"status": "running"`. Within two minutes
    `ds runs` lists the run as `completed  cycle 4  different 6 of 991`; here it completed 1 minute
    50 seconds after the resume.

After such a pause is undone and the run resumed, the run's lines read "no change observed against the
run's plan at its creation and each cycle's start" again and no longer mention the pause. The pause
stays in the audit log, the record of who did what. `<run id>` is the soak run's id:
```bash
ds audit --object <run id>
```
You should see the run's rows, newest first. Two of them, shortened here, record the pause:
```text
66  2026-10-05T21:58:20+00:00  system:worker (worker)  schema.drift  R-35T7QQA1CDNRKHTK0B52NDNEB2  {"basis": "R-35T7QBFA9S3DHR5F2HJQBMME55", "checked": true, "cycle": 2, … "paused": true, …}
65  2026-10-05T21:58:20+00:00  system:worker (worker)  paused  R-35T7QQA1CDNRKHTK0B52NDNEB2  {"reason": "run_paused_schema_drift", …}
```

### How to read the result

**What the guard remembers.** For each table a run reads: every column's name, its declared type, its
class, whether it may be empty, the primary key, and whether the thing is a table or a view. It reads
them from the database's own description of the table, never from rows. It does not see a change of
meaning, a constraint, a default, an index, what a view is built from, or a change that was undone
again before the guard looked.

**When it looks.** When a run is created, at the start of each cycle of a soak run, when a paused run
is resumed, and after a read that the database refused.

**What it does**, in three cases:

| The change | What happens | What you do |
|---|---|---|
| A new run would not compare what the last run of the same spec version compared: a column of another class, or a column no longer compared | The run is created paused, "before any table row was read" | Read the line. Resume to accept the tables as they are, or cancel |
| The table no longer fits a run that is going: a column it reads is gone or of another class, or the table is gone | The run pauses "at the start of cycle N", or "in cycle N" when the change came while a cycle was reading. A resume is refused while the change stands | Undo the change and resume, or cancel and start a new run |
| Anything else: a column added, a column the run does not read removed, a wider type of the same class | A grey line on the run and a `warning:` line from `ds run`. The run goes on | Nothing |

After you accept, the accepted run is the reference. The next run is compared with it and is not
paused for the same change. If you change the table back, the next run is paused once more, this time
for the change back: here it read `column 'country' was text (text) in run R-… of this version and is
character(2) (char) now`.

A run that saw nothing says "no change observed", never "unchanged": the guard only knows what it saw
at the moments it looked.

**A stricter mode.** A spec can ask the guard to pause on every change, harmless ones included, with
one line at the top level of the spec file:
```text
schemaDrift: {mode: pause}
```
With that line, a copy of the example spec was created paused for an added column: `Paused by the
schema guard before any table row was read: side b gained column 'vip' since run R-…: this version
does not compare it. This spec pauses on schema drift: resume to accept the tables as they are, or
cancel.` Not verified here: in this mode the first case of the table is refused outright instead of
created paused, by the runbook, and the answer is to upload a new spec version.

### Limits

- The guard covers database tables. A service side is not guarded.
- It never fails a run and never repairs anything. It pauses and waits for a person.
- No `ds` command resumes or cancels a run. Use the run's page, or the requests of step 7.
- A paused run stays paused until someone acts. A schedule whose run is paused starts no further run
  meanwhile (section 4, "Overlap with a run still going").
- If the guard cannot look, for example because the database does not answer at that moment, it
  records that and lets the cycle go on. Not verified here.

### If something goes wrong

**The soak run reads `paused  cycle 1`, without `different 6 of 991`, and its line begins `Paused by
the schema guard in cycle 1: side b: column 'phone' of public.users is gone`** (or `at the start of
cycle 1` in the same place)
The column was renamed before the first cycle had finished: the rename of step 6 came within a second
or two of the start. Provoked here by renaming in the same second. Nothing is lost: steps 8 to 10
work the same from here, and the run completed 2 minutes 42 seconds after the resume. To see the
pause at the start of cycle 2, wait until this run has completed, start another with the `ds run`
command of step 5, and list the runs before you rename.

**The soak run reads `paused  cycle 3  different 6 of 991`, and its line says `at the start of cycle
3`**
The rename came after the second cycle had started, more than a minute after the run did. Go on with
step 8; only the cycle's number differs. Provoked here by renaming 75 seconds after the start.

**The soak run of step 5 ends `paused (run_paused_schema_drift)` with a line `Paused by the schema
guard before any table row was read: column 'country' was text (text) in run R-… of this version and
is character(2) (char) now`**
An earlier soak run of this copy exists, and a table was changed since it ran, here the `country`
column. The guard holds the new run as in step 5 of the first list. Accept it with the resume command
of step 7 there; the run then begins its first cycle, and you go on with `ds runs` in step 5.

**`error: side a: column 'phone' has no counterpart on side b`**, from `ds run`
This is not the guard. The spec lists the column `phone` for side a, and side b no longer has it, so
no run can be planned at all. You meet it when you start a run, a second soak run for example, while
the column is still renamed: put it back with step 9 of the second list, then start the run again.
For tables that really changed, upload a spec version that fits them.

**`{"detail": "run R-… cannot resume: side b: column 'phone' of public.users is gone, or the role may
no longer read it"}`**
The change that paused the run still stands. Undo it, then resume; or cancel the run.

**`error: side a: cannot connect to target crm-legacy: failed to resolve host
'deltasleuth-practice-db': [Errno -2] Name or service not known`**, from `ds run`
The practice database is stopped. Start it:
```bash
docker start deltasleuth-practice-db
```
You should see its name. If it runs already, see the entry with the same message in section 1.

**`paused (run_paused_schema_drift)` and `error: the run is paused`, status 2**
Not an error of the command: the guard holds the run. Read its line, then accept or cancel (step 7).

**After the practice database is loaded again from `seed.sql`, the next run is paused**
The seed puts every column back, which is a change against the run you accepted. Accept it the same
way. Here the paused run read `column 'country' was text (text) in run R-… of this version and
is character(2) (char) now`, with a second line `side b: column 'loyalty_tier', which this run does
not read, is gone`.

**`error: side a: table or view crm.customers not found or not visible`**, from `ds run`
The account in the address file cannot see the table. Met here after the example data was loaded
again while the address files named chapter 6's read-only account. Repeat chapter 6, section 2,
step 3, which gives the account its rights again.

## 4. Schedules

### What it is for

A schedule starts a run of a stored spec by itself at set times: every night at two, every weekday
morning, every six hours. It turns a comparison someone starts by hand into a standing check, for the
operators who watch two systems over weeks. Every time that was due and did not start a run is recorded
with its reason, so a gap is visible afterwards.

### Before you start

1. A stored spec. The steps below use the bundled mock pair, `mock-customer-v1-vs-v2`. If
   [chapter 3](03-first-comparison.md) stored it, it is there already. Otherwise store it: on the
   "Specs" page with "Upload a spec" and the file `mocks/customer/spec.yaml`, or with this command,
   which sends the file with your token and never shows the token.
   ```bash
   { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -H 'Content-Type: application/yaml' --data-binary @mocks/customer/spec.yaml http://localhost:8000/v1/specs
   ```
   You should see the spec's number and name:
   ```text
   {"spec_id": 7, "name": "mock-customer-v1-vs-v2", "version": "1.0.0", "content_hash": "dadc627b…", "needs": ["clear", "known_diffs"], "approval_id": null}
   ```
   Your `spec_id` can be another number. Storing the same file a second time changes nothing: the
   answer names the same `spec_id`.
2. The app must be running, because its worker starts the runs. Check it.
   ```bash
   docker compose ps
   ```
   You should see the line of `deltasleuth-app-1` with the status `Up … (healthy)`. While the app is
   stopped, the list has no line for it.

Nothing is switched on in the settings file: schedules work as installed.

### In the web UI

1. Click "Schedules" in the bar. The page shows seven grey lines on what a schedule never tells (the
   first reads "A firing starts a run and is not a completed run: the run's status says how it
   ended."), a table of schedules, and at the bottom the panel "New schedule".
2. In "Name" type `nightly-customers`. In "Spec" choose `mock-customer-v1-vs-v2 1.0.0 (#7)`; the
   number in brackets is the spec's id and can differ. Leave "Mode" at `batch`.
3. Leave "Runs" at `daily`. In the field "at", set a time two minutes from now by your own clock. The
   cron text the form will save appears beside it, for example `16 21 * * *`. "Time zone" already
   shows your browser's zone; leave it.
4. Press "Preview". Under the form you should see "Its next 10 times by tz data 2026c (a schedule,
   not a forecast):" and ten lines, the first one two minutes away, each with the time in your zone and
   in UTC.
5. Press "Add". Above the form you should see "Added SC-… (nightly-customers)." and the line "tokens
   mode has no approvers: its runs are labelled unapproved". The table has a new row: under "Spec and
   version" it reads `pinned 1.0.0 (spec 7)`, with your spec's id, under "Next due (a schedule, not a
   forecast)" the time you set, under "State" `active`.
6. Wait until the time has passed and reload the page (it also refreshes by itself every minute).
   Under "Last firing" you should see "started run R-… at its time", the run's status `completed`, and
   a link "run" that opens the run.
7. Click the name `nightly-customers`. The page "Schedule nightly-customers" shows what it runs, who
   saved it, "Four-eyes approval: unapproved", the buttons "Edit", "Pause" and "Delete", the list "Next
   due (a schedule, not a forecast)" with five times, and the table "Firings" with the columns
   "Recorded", "What happened" and "Audit row".
8. To change it into a run at two every night, press "Edit". The panel "Edit nightly-customers" shows
   the timing as "custom cron" with the cron text in the field "Cron". Replace it with `0 2 * * *`,
   and press "Save". "Next due" changes to the next 02:00.
9. Press "Pause": the state becomes `paused`, the button becomes "Resume", and nothing is due. Press
   "Resume" to start again from now.
10. To end a schedule on a date, press "Edit", type a time such as `2026-12-31T00:00Z` in "Ends", and
    press "Save".
11. Press "Delete" and confirm the question "Delete schedule nightly-customers? A time due now is
    recorded as missed; its runs and firings still name it." You stay on the schedule's page: the
    state beside the heading now reads `deleted`, the buttons are gone, "Next due" reads "None." and
    the firings are still listed. Click "All schedules": the list no longer shows it.

Two more places lead here. On the "Specs" page each row has the link "Schedule…", which opens "New
schedule" with that spec chosen. On a run's page a grey line such as "started by schedule
nightly-customers (SC-…) for 2026-10-05 22:03 UTC (2026-10-05T22:03Z), 0 s after its time" links back
to the schedule.

The tick box "follow its name: the newest stored version at each firing" decides what "Pin or follow"
below explains. "Runs" also offers `on weekdays`, `weekly`, `monthly`, `every N hours` and `custom
cron`.

### On the command line

1. Look at the times a cron text gives before you save anything. This command needs no database.
   ```bash
   ds schedules next '0 2 * * *' --tz Europe/London --count 3
   ```
   You should see each time in the zone and in UTC:
   ```text
   2026-10-06 02:00 Europe/London (2026-10-06T01:00Z)
   2026-10-07 02:00 Europe/London (2026-10-07T01:00Z)
   2026-10-08 02:00 Europe/London (2026-10-08T01:00Z)
   ```
2. A schedule cannot be run "now": its first run starts at its next time. So for a first try, make one
   that is due in two minutes. Read the time in UTC.
   ```bash
   date -u '+%H:%M'
   ```
   You should see the hour and minute, here `22:01`.
3. Add the schedule. `<minute>` is the minute you just read plus two (minus 60 if that passes 59),
   here `3`. The cron text `<minute> * * * *` means "at that minute of every hour".
   ```bash
   ds schedules add nightly-customers --spec mock-customer-v1-vs-v2 --cron '<minute> * * * *' --tz UTC
   ```
   You should see:
   ```text
   warning: tokens mode has no approvers: its runs are labelled unapproved
   added SC-35T7R7NFBGJZ09JPY8012RKE5D (nightly-customers): next 2026-10-05 22:03 UTC (2026-10-05T22:03Z)
   ```
   If the time after `next` is an hour away, the minute had passed already: see "If something goes
   wrong".
4. List the schedules.
   ```bash
   ds schedules
   ```
   You should see seven lines on what a schedule never tells, then one line for each schedule:
   ```text
   A firing starts a run and is not a completed run: the run's status says how it ended.
   Next due is a schedule, not a forecast.
   No person is named as a firing's actor: the worker starts a schedule's runs.
   A missed time is never shown as run: a late start says how late, a range of missed times is counted, and the times a save, pause or delete left unstarted are missed, never dropped.
   A schedule has no percentage, uptime or success rate: it is not evidence, readiness or a sign-off.
   It holds no key and no value.
   It is not a dead-man's switch: no alert is sent while no worker runs: a missed time is reported when a worker next sweeps.
   SC-35T7R7NFBGJZ09JPY8012RKE5D  nightly-customers  follows mock-customer-v1-vs-v2: 1.0.0 (spec 7) now  batch  3 * * * * UTC  next 2026-10-05 22:03 UTC (2026-10-05T22:03Z)
   ```
5. When the minute has passed, show the schedule.
   ```bash
   ds schedules show nightly-customers
   ```
   You should see its line, who saved it, the next five times and, last, its firings:
   ```text
   SC-35T7R7NFBGJZ09JPY8012RKE5D  nightly-customers  follows mock-customer-v1-vs-v2: 1.0.0 (spec 7) now  batch  3 * * * * UTC  next 2026-10-05 23:03 UTC (2026-10-05T23:03Z)  last: started run R-35T7RBMBWGWG3SPYR1WPTPXYP6 at its time (completed)
   revision 1, saved by local:<you>@deltasleuth-app 2026-10-05T22:01:54+00:00 (tokens); starts within 60 min
   approval: unapproved
   due 2026-10-05 23:03 UTC (2026-10-05T23:03Z)
   due 2026-10-06 00:03 UTC (2026-10-06T00:03Z)
   due 2026-10-06 01:03 UTC (2026-10-06T01:03Z)
   due 2026-10-06 02:03 UTC (2026-10-06T02:03Z)
   due 2026-10-06 03:03 UTC (2026-10-06T03:03Z)
   2026-10-05T22:03:00+00:00  started run R-35T7RBMBWGWG3SPYR1WPTPXYP6 at its time
   ```
   Typed before the minute, the command shows no firing line yet and `next` still names that minute:
   wait and type it again.
6. The run is an ordinary run. List the runs.
   ```bash
   ds runs
   ```
   You should see it at the top:
   ```text
   R-35T7RBMBWGWG3SPYR1WPTPXYP6  mock-customer-v1-vs-v2 1.0.0  batch  completed  cycle 1  different 4 of 40
     Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   ```
7. Turn the schedule into the nightly one. `set` replaces what its options name and keeps the rest.
   ```bash
   ds schedules set nightly-customers --cron '0 2 * * *' --tz Europe/London
   ```
   You should see the warning of step 3 again, then:
   ```text
   saved SC-35T7R7NFBGJZ09JPY8012RKE5D: next 2026-10-06 02:00 Europe/London (2026-10-06T01:00Z)
   ```
8. Pause it. Nothing is due while it is paused.
   ```bash
   ds schedules pause nightly-customers
   ```
   You should see `paused SC-35T7R7NFBGJZ09JPY8012RKE5D`. In the list of step 4 its line now carries
   the word `paused` where the next time was.
9. Resume it. It starts again from now; the times of the pause are neither run nor counted as missed.
   ```bash
   ds schedules resume nightly-customers
   ```
   You should see:
   ```text
   resumed SC-35T7R7NFBGJZ09JPY8012RKE5D: next 2026-10-06 02:00 Europe/London (2026-10-06T01:00Z)
   ```
10. Give it an end. The time is written with `Z` or another offset from UTC; no time is due after it.
    ```bash
    ds schedules set nightly-customers --ends 2026-12-31T00:00Z
    ```
    You should see `saved SC-…: next …` again. The command of step 5 now prints this second line:
    ```text
    revision 5, saved by local:<you>@deltasleuth-app 2026-10-05T22:03:30+00:00 (tokens); starts within 60 min; ends 2026-12-31T00:00:00+00:00
    ```
11. Delete it. The name is free again; its run and its firings stay.
    ```bash
    ds schedules delete nightly-customers
    ```
    You should see `deleted SC-35T7R7NFBGJZ09JPY8012RKE5D`. The list of step 4 no longer shows it.
12. List the deleted schedules.
    ```bash
    ds schedules --state deleted
    ```
    You should see the seven lines, then:
    ```text
    SC-35T7R7NFBGJZ09JPY8012RKE5D  nightly-customers  follows mock-customer-v1-vs-v2: 1.0.0 (spec 7) now  batch  0 2 * * * Europe/London  deleted  last: started run R-35T7RBMBWGWG3SPYR1WPTPXYP6 at its time (completed)
    ```
13. A deleted schedule answers to its id only. `<schedule id>` is the id that starts with `SC-`.
    ```bash
    ds schedules show <schedule id>
    ```
    You should see its line, who saved it and its firings; no time is due any more:
    ```text
    SC-35T7R7NFBGJZ09JPY8012RKE5D  nightly-customers  follows mock-customer-v1-vs-v2: 1.0.0 (spec 7) now  batch  0 2 * * * Europe/London  deleted  last: started run R-35T7RBMBWGWG3SPYR1WPTPXYP6 at its time (completed)
    revision 8, saved by local:<you>@deltasleuth-app 2026-10-05T22:03:30+00:00 (tokens); starts within 60 min; ends 2026-12-31T00:00:00+00:00
    approval: unapproved
    2026-10-05T22:03:00+00:00  started run R-35T7RBMBWGWG3SPYR1WPTPXYP6 at its time
    ```
    Yours says `revision 6`. The run printed here paused and resumed the schedule once more before
    deleting it, for the command below; repeated in the order of these steps, it gave `revision 6`.

One more command pauses every active schedule of the installation at once. Type it only when you
mean that: each schedule it pauses has to be resumed by name afterwards.
```bash
ds schedules pause --all
```
With no schedule left after step 11, you should see `no active schedules`. Sent here while two
schedules were active, it answered `paused 2 schedules`. The deployment document asks for it before an
app is started on a restored or copied database, which would otherwise start a run of each schedule.

### How to read the result

**Cron text.** A cron text has five fields: minute, hour, day of the month, month, day of the week.
`*` means "every". `0 2 * * *` is 02:00 every day, `0 2 * * 1-5` is 02:00 on Monday to Friday, `30 6 1
* *` is 06:30 on the first of each month, `0 */6 * * *` is every six hours. Numbers, ranges (`1-5`),
lists (`0,30`) and steps (`*/6`) are allowed, and the names `JAN` to `DEC` and `SUN` to `SAT`. The
form's choices only write such a text:

| "Runs" | Cron text it writes |
|---|---|
| `daily` at 02:00 | `0 2 * * *` |
| `on weekdays` at 02:00 | `0 2 * * 1-5` |
| `weekly` on Monday at 02:00 | `0 2 * * 1` |
| `monthly` on day 1 at 02:00 | `0 2 1 * *` |
| `every N hours`, N = 6 | `0 */6 * * *` |
| `custom cron` | what you type |

**Time zones and daylight saving.** The zone is required: `UTC`, or a region name such as
`Europe/London`. Times are clock times in that zone, so a 02:00 schedule stays at 02:00 local time
when the clocks change, and its UTC time moves. Two nights a year need care, and the preview warns of
both. This command asks for 01:30 in London:
```bash
ds schedules next '30 1 * * *' --tz Europe/London --count 22
```
Besides the times, it printed:
```text
warning: 2026-10-25: 01:30 happens twice in Europe/London (clocks go back): the schedule fires at the first (00:30 UTC)
warning: 2027-03-28: 01:30 does not exist in Europe/London (clocks go forward): that firing happens at 02:30 (01:30 UTC)
```
A time the clock repeats fires once, at its first occurrence; a time the clock skips fires once, later
by the size of the jump. `UTC` has neither.

**The start window.** A run may still start up to 60 minutes after its time (`--start-within`, "Start
within … min of its time" on the form; 15 to 1440). The window never reaches past the next due time.

**Pin or follow.** A schedule either runs one stored version of a spec for ever (pinned), or the
newest stored version of a spec name at each firing (it follows the name). `--spec <name>` follows;
`--spec-id <number>` pins, the number being the one in the "Id" column of the "Specs" page. The form
pins unless you tick "follow its name". When a newer version of a pinned spec is stored, the list
says so in a line above the schedules. This was tried here with a schedule made for the purpose,
named `pinned-demo` and pinned with `--spec-id 7`. After a version 1.0.1 of the mock spec was
stored, as spec 10, the list began `schedule pinned-demo is pinned to 1.0.0 (spec 7); 1.0.1 (spec 10)
is stored`. The command `ds schedules set pinned-demo --spec-id 10` moved it, and the line went away.
The schedule's page has a button "Move to the newer version: 1.0.1 (spec 10)" for the same. With
token sign-in nothing reviews a new version before a following schedule runs it, so pin what must not
change by an upload.

**What starts the runs.** Only the worker does, and in this installation the worker lives in the app
container. A firing is the worker starting a run for a due time. The run is created by
`system:worker`, not by a person.

**Overlap with a run still going.** A schedule has at most one run going. While a run it started is
still live (running or paused), its later times start nothing. The worker tries again every minute
while the time's window is open. When the window closes with the run still live, the time is recorded
as not started.

You need not repeat what follows. It is this chapter's own trial, which took 45 minutes of waiting,
and it is printed so that you know the lines when you meet them. A schedule named
`tables-quarter-hour` ran the tables spec of section 1 every 15 minutes (cron text `*/15 * * * *`,
with `--start-within 15`). Its run of 22:15 was created paused by the schema guard of section 3, and
nobody acted for half an hour. Afterwards the schedule's firings read, newest first:
```text
2026-10-05T22:46:00+00:00  started run R-35T7X5D02K7WC9QQ96RRC18VVE 1 min after its time
2026-10-05T22:44:08+00:00  not started: run R-35T7SPHVZPK8EXAJ6Q0REN08BE of this schedule was still live when its window closed
2026-10-05T22:15:00+00:00  started run R-35T7SPHVZPK8EXAJ6Q0REN08BE at its time
```
The middle line is the 22:30 time: it found the paused run and was given up when its window closed.
The run was accepted at 22:45:35 (section 3, step 7), and the 22:45 time started its run within the
next minute.

While the schedule waited, its line in `ds schedules` ended `last: started run
R-35T7SPHVZPK8EXAJ6Q0REN08BE at its time (paused)`. The word in brackets is the status of the run the
line names. A status other than `completed` there, long after the time, is the sign that later times
are being held back. From ten minutes after the 22:30 time until its window closed, the list also
began with the line `schedule tables-quarter-hour was due at 2026-10-05T22:30Z and is not settled
yet: …`, although the app was running. "If something goes wrong" quotes that line in full.

**While no worker runs.** Nothing starts and nothing is recorded, and the seventh line of `ds
schedules` says that no alert is sent either. When the worker is back it settles each schedule once:
the newest due time starts its run if its window is still open, and every older time is recorded as
not started. A schedule never runs a backlog.

This too is the chapter's own trial and not a step for you, because it stops the app for half an
hour. The app was stopped at 22:47 with `docker compose stop app` and started again at 23:16 with
`docker compose --profile mocks up -d`. Meanwhile `ds schedules` still answered, because the command
reads the database itself, and from 23:10 on, ten minutes after a due time, its list began with the
"not settled yet" line again, this time for the 23:00 time. Two seconds after the start the worker
recorded two firings of the schedule `tables-quarter-hour` described above:
```text
2026-10-05T23:16:03+00:00  started run R-35T80GTRH344M9PG5ZB35MT42H 1 min after its time
2026-10-05T23:16:03+00:00  not started: its time 2026-10-05T23:00Z passed 16 min before a worker could start it, over the 15 min the schedule allows (no worker that fires schedules ran, the store was down, or the clock moved)
```
The 23:00 time was missed, because its 15 minutes were over. The 23:15 time was still inside its
window and started late.

A second schedule, due every full hour, was paused at 23:02 while the app was stopped. A pause, a save
and a delete each record a time that is due and not started, so that it is not lost:
```text
2026-10-05T23:02:11+00:00  not started: its time 2026-10-05T23:00Z was due and not started when the schedule was paused
```

**The firings.** Each line of "Firings" (the last lines of `ds schedules show`) is one of two kinds.
`started run R-… at its time`, or `… 1 min after its time`, says a run was started; it does not say
how the run ended, which the run's own status does. `not started: …` gives the reason in words. The
table lists the reasons under the short names the runbook and the API use; this run met the first
three, and the others are described as the runbook describes them:

| Reason | In plain words | What to do |
|---|---|---|
| `late` | The window closed before a worker could start the run: no worker ran, or it was busy starting other schedules | Start a run by hand if you need it. Keep the app running |
| `overlap` | A run of this schedule was still live when the window closed | Let the run end, or cancel it; or schedule it less often |
| `superseded` | You saved, paused or deleted the schedule while a time was due and not yet started | Nothing: it is recorded so that it is not lost |
| `approval` | Under single sign-on, the spec version lacked the approval it needs | Have the version approved |
| `mode`, `setter` | The sign-in mode changed since the schedule was saved, or the person who saved it was disabled | An operator saves the schedule again |
| `sample` | A sampled run of the same data is live, or an earlier sample did not pass and is not closed | Close the sample (the sign-off chapter) |
| `not_started`, `error` | The run could not be created, for example a database did not answer | Start a run by hand to see the refusal |
| `zone` | The app's time zone data no longer knows the schedule's zone; the worker paused the schedule | Save it with a zone the build knows, then resume |

### Limits

- Firings are at least 15 minutes apart. A cron text that fires more often is refused.
- There is no "run now". For a run now, start one on the "Runs" page.
- Only the newest due time is ever started. After an outage the older times are recorded as missed,
  never run one after the other.
- A schedule is not an alarm for a stopped app: while no worker runs, nothing is recorded and nothing
  is sent. The health address of chapter 1 does not cover it either. Watch from outside if that
  matters. `ds schedules --json` and the API's list of schedules carry a count named `overdue`, the
  schedules more than ten minutes late. It read 1 during the outage described above, and also while
  the 22:30 time waited behind the paused run.
- An installation holds at most 200 schedules (by the contract; not reached here).
- A schedule starts comparisons. It does not start pre-checks (section 1, step 9, shows the way).
- A soak run can be scheduled (`--soak`, or "Mode" `soak`) only for a spec that sets a cycle interval
  and an end. The mock spec has none, and `--soak` on it was refused (below).
- `ds schedules set` cannot rename a schedule or remove its end; the "Edit" form can. The "Schedules"
  page never lists deleted schedules; `ds schedules --state deleted` does.
- With token sign-in every scheduled run is labelled unapproved, as every run is. Not verified here,
  because this installation has no single sign-on: there a version without its approval starts
  nothing, and each such time is recorded with the reason `approval`.
- Roles: `operator` may add, change, pause, resume, delete and preview; `viewer` may only read, and
  was answered `{"detail": "needs the operator role"}` for a pause and for a preview.

### If something goes wrong

**`added SC-… (nightly-customers): next …` names a time almost an hour away**
The minute you gave in step 3 had passed already when the command ran. Read the time again (step 2)
and move the schedule to a new minute:
```bash
ds schedules set nightly-customers --cron '<minute> * * * *'
```
Provoked here with a schedule added for a minute that had just passed: `next` named the next hour.
The command above then answered `saved SC-…: next 2026-10-05 22:13 UTC (2026-10-05T22:13Z)`, two
minutes ahead, and the schedule started its run at 22:13:01.

**`error: cron fires at 00:00 and 00:05: a schedule fires at most every 15 minutes (a soak's
execution.cycleInterval repeats faster)`**
The cron text (here `*/5 * * * *`) fires too often. Use a step of 15 minutes or more.

**`error: cron must have 5 fields (minute hour day-of-month month day-of-week), not 6`** (or `not 1`)
The text has a seconds field, or is a word such as `@daily`. Write the five fields.

**`error: cron sets both day-of-month and day-of-week: set one and leave the other * (cron dialects
disagree on what both mean)`**
For example `0 2 1 * 1`. Keep one of the two day fields as `*`.

**`error: cron field day-of-month: 'L' is not *, a number, a range a-b, a list a,b or a step */n or
a-b/n`**
Special letters such as `L`, `W`, `?` and `#` are not supported.

**`error: 'Asia/Calcutta' is not a time zone this build uses (tzdata 2026c): use Asia/Kolkata`**
The zone has a newer name. Use the one the message gives.

**`error: time_zone must be UTC or an IANA region name such as Europe/London: 'London' is not one`**
Write the region and the city, with the slash.

**`Missing option '--tz'.`**
`ds schedules next` and `ds schedules add` always need a zone.

**`warning: the name of 'Etc/GMT+5' holds a number: under a key pattern that matches a short number,
the runs of a schedule in it get no evidence pack: use a region's zone, or UTC`**
Not an error, but take the advice: choose a region's zone or `UTC`.

**`error: a schedule named nightly-customers exists already (SC-…)`**
Choose another name, or change the existing one with `ds schedules set`.

**`error: name must be a slug of 1 to 63 characters (lower-case letters, digits and single hyphens,
starting with a letter)`**
The name had a capital letter or an underscore (`Nightly_Customers`).

**`error: no spec is named no-such-spec`**
`--spec` takes the name of a stored spec. Store it first ("Before you start").

**`error: give spec_id (a pinned version) or spec_name (the newest version at each firing), not
both`**
`ds schedules add` was given neither `--spec` nor `--spec-id`. The message uses the API's words: give
one of the two options.

**`error: start_within must be 15 to 1440 minutes`**, or **`error: ends_at must be an ISO-8601 time
with a UTC offset, later than now`**
`--start-within` was below 15, or `--ends` was in the past or had no offset (`2026-12-31` alone).
Write the end as `2026-12-31T00:00Z`.

**`error: a soak run needs the spec's execution.cycleInterval`**
`--soak` was given for a spec that does not repeat. Leave it out, or schedule a spec that sets the
interval and an end (section 3 made one).

**`error: schedule SC-… is paused already`**, or **`… is not paused`**
`pause` was sent twice, or `resume` to a schedule that runs.

**`error: schedule nightly-customers not found`**
The name is mistyped, or the schedule was deleted. A deleted one answers only to its id (`SC-…`).

**`{"detail": "schedule SC-… changed since you loaded it (revision 2): reload and edit again"}`**
Someone else saved the schedule while you had "Edit" open. Reload the page and edit again.

**`schedule tables-quarter-hour was due at 2026-10-05T22:30Z and is not settled yet: a worker may be
retrying or starting it, or none is running (ds worker, or ds serve)`**, above the list of `ds
schedules` and of the "Schedules" page
A time of the schedule it names is more than ten minutes late. The line has two causes, and this
command tells them apart:
```bash
docker compose ps
```
- The line of `deltasleuth-app-1` is missing: the app is stopped, so no worker runs. Start it with
  the command of [chapter 1](01-install-docker.md), section 6, step 2. "While no worker runs" above
  shows what the worker then records.
- The app is `(healthy)`: look at the end of that schedule's own line in the list. If it ends
  `(paused)` or `(running)`, the schedule's last run is still live and holds the next one back, as
  "Overlap with a run still going" describes. Open the run the line names. If the schema guard paused
  it, accept or cancel it (section 3, step 7); if it is running, let it end. Met here with the app
  healthy and a run that the guard had paused 25 minutes earlier; the line went away when the time's
  window closed, and the time was then recorded as not started.

**A firing reads `not started: its time … passed 16 min before a worker could start it, over the 15
min the schedule allows (no worker that fires schedules ran, the store was down, or the clock
moved)`**
The app was stopped over that time. If you need that run, start one on the "Runs" page.

**A firing reads `not started: run R-… of this schedule was still live when its window closed`**
Open the run it names. If the schema guard paused it, accept or cancel it (section 3). If it is still
running, let it end, or give the schedule more time between runs.

## When you are done

The schedule of section 4 is deleted by its step 11. A schedule you added besides it keeps starting
runs until you delete it, so look at the list once more:
```bash
ds schedules
```
You should see the seven lines and, under them, `no schedules`. If a schedule is listed that you do
not want to keep, delete it. `<name>` is the second word of its line.
```bash
ds schedules delete <name>
```
You should see `deleted SC-…`.

**What stays, and does no harm.** The stored specs (`crm-customers-vs-platform-users`,
`crm-customers-soak` and `mock-customer-v1-vs-v2`), the pre-checks and the runs stay listed on their
screens. A stored spec cannot be deleted: neither `ds` nor the web UI has a command for it. The
folders `precheck-report` and `drafts` and the file `tables-soak.yaml` in your folder are copies; keep
or delete them. The files this chapter copied into the app's folder `/var/lib/deltasleuth/p0` stay
there too.

**If chapter 6 is still to come, or you did it, stop here.** [Chapter 6](06-tables.md) uses the same
practice database and the same two address files, and its section 12 tidies up for both chapters.

**Otherwise, take the practice database out again**, in this order: the container first, the line in
`.env` next, the secret files last.

1. Remove the practice database, with its data. This cannot be undone.
   ```bash
   docker rm -f -v deltasleuth-practice-db
   ```
   You should see the container's name, `deltasleuth-practice-db`.
2. Take the example file out of the list in `.env` again. This undoes step 9 of section 1's "Before
   you start".
   ```bash
   sed -i.bak 's|:examples/tables/compose.yaml$||' .env
   ```
3. Check the line, then delete the copy the command left.
   ```bash
   grep '^COMPOSE_FILE' .env
   ```
   You should see the three files of chapter 1:
   ```text
   COMPOSE_FILE=deploy/docker-compose.yml:deploy/docker-compose.site.yml:deploy/docker-compose.source.yml
   ```
   ```bash
   rm .env.bak
   ```
4. Apply the change. Compose makes the app container anew, without the two address files.
   ```bash
   docker compose --profile mocks up -d
   ```
   You should see ` Container deltasleuth-app-1  Recreated` among the lines.
5. Only now delete the three secret files. `-f` is needed because they are read-only. The command
   prints nothing.
   ```bash
   rm -f deploy/secrets/practice_db_password deploy/secrets/crm_legacy_dsn deploy/secrets/platform_users_dsn
   ```
6. Check that `ds` still works.
   ```bash
   ds token list
   ```
   You should see the line of chapter 1, section 5, which begins with the token's id.

Step 5 must come after step 4. As long as the line in `.env` names `examples/tables/compose.yaml`,
the two address files must stay, even when the practice database is gone. Tried here with
`crm_legacy_dsn` taken away while the line was still there, every `ds` command stopped with these two
lines before it did anything:
```text
time="2026-10-05T23:17:03Z" level=warning msg="secret file deltasleuth_crm_legacy_dsn does not exist"
Error response from daemon: invalid mount config for type "bind": bind source path does not exist: /host_mnt<folder>/IntelliDiff/deploy/secrets/crm_legacy_dsn
```
`<folder>` is where your `IntelliDiff` folder lies; Docker Desktop on macOS puts `/host_mnt` before
it. With the file put back, the next command worked again.

After these steps the targets `crm-legacy` and `platform-users` are still declared in the targets
file, and the two table specs are still stored. A check, a run or a pre-check of them now answers
`error: cannot read secret file /run/secrets/crm_legacy_dsn: No such file or directory` (a pre-check
says `not checked: side a's read failed (SecretError)`). Everything else works as before.

If you leave the practice database in place for now, remove it before you ever remove the whole
installation ([chapter 1](01-install-docker.md), section 8). Tried here in the other order, the
removal (typed as `docker compose --profile mocks down -v`) removed the containers and the data but
kept the network, with the line ` Network deltasleuth_default  Resource is still in use`. Run again
after step 1 above, it removed the network too.

## Reference

- [PRECHECKS.md](../mvp/PRECHECKS.md): table pre-checks and the schema guard in full, with every
  message.
- [SCHEDULES.md](../mvp/SCHEDULES.md): schedules in full: cron, time zones, firing and every reason.
- [RUNBOOK.md](../mvp/RUNBOOK.md): the operator's sections "Triage with table pre-checks", "When a
  table changes under a run" and "Schedules".
- [DEPLOY.md](../mvp/DEPLOY.md): what a pre-check costs a database, the `precheck` settings, and the
  troubleshooting rows for pre-checks and schedules.
- [TABLES.md](../mvp/TABLES.md) and [chapter 6](06-tables.md): comparing two tables.
- [ALERTS.md](../mvp/ALERTS.md): notifications for a paused run and for a missed schedule time.
