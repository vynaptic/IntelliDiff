# 6. Comparing database tables

DeltaSleuth compares two database tables, or views, row by row. It pairs the rows by a key, such as a
customer number, compares the columns both tables have, and reports every key whose rows differ or that
only one table holds. Teams use it to show that a table holds the same data before and after a
migration.

This chapter starts a practice database beside your installation, declares it to DeltaSleuth, and goes
through each table feature on the differences planted in its data. It needs the installation of
[chapter 1](01-install-docker.md): Docker, the token `first-run` and the mock services. Nothing here
touches a real system.

**How this chapter was checked.** Every command was run on 2026-10-05, on macOS with Docker Desktop, from an empty installation. The machine already ran a
DeltaSleuth installation, so the run was a second installation, as chapter 1 describes one, and the
outputs are printed here with the default names. Run ids, the numbers of stored specs, times and
masked values will differ on your machine; counts, outcomes and keys will not. The steps under "In the
web UI" were written from the pages' source, and the numbers and lines they quote were worked out from
the answers of the requests the pages send. Not verified here: the pages in a browser, and anything on
Linux.

**Words used here.** A *target* is one entry of the targets file `deploy/config/targets.yaml`:
something DeltaSleuth is allowed to read. A *spec* is the YAML file that describes one comparison; its
two *sides* are called `a` and `b`, and each names a target. The *key* is the value that says which row
of side a belongs to which row of side b. A *run* is one execution of a spec, with an id that starts
`R-`. A *scan* is one pass over both tables from the first key to the last. A *secret* is a password or
a connection string that holds one: it lives in a file of its own in `deploy/secrets/`, and no command
of this chapter shows one. The *site file* is `deploy/docker-compose.site.yml`, this installation's own
changes to the Docker description. [Chapter 3](03-first-comparison.md) explains outcomes such as
`MISMATCH` and `NOT_FOUND_B`, findings and root causes.

**Conventions.**

- Run every command from the installation's folder, `IntelliDiff`, in a terminal where the short form
  `ds` of chapter 1, section 5, is defined.
- Every `ds` command first prints Docker's line ` Container deltasleuth-db-1  Running`, and many print
  lines that start `{"id":`, which are copies of audit records. The outputs below leave both out.
- Every `ds run` and `ds compare` of this chapter also prints one line that starts `warning: spec`
  and ends `is labelled unapproved`. It is normal with token sign-in; section 3 shows it once and
  explains it, and the outputs after that leave it out.
- `ds run` prints three lines before its result: `run R-... started`, that warning, and a line that
  starts `another worker is processing keys`. Their order changes from run to run. The outputs below
  show `run R-... started` and the result.
- `<run id>` stands for the id a run printed, such as `R-35T77G09RZ5DWP066CE61V9J3T`.
- In a second installation (chapter 1), write your project name in place of `deltasleuth` in
  `deltasleuth-practice-db` and `deltasleuth_default`, and your port in place of 8000. Do this with
  care: these are `docker` commands without the word `compose`, so they act on exactly the name you
  type, and typed with `deltasleuth` they act on the first installation.

| Section | Feature | Spec file |
|---|---|---|
| 1 | The practice database | |
| 2 | Declare a database target | |
| 3 | The smallest table spec and its first run | `practice/customers.yaml` |
| 4 | Row filters | `practice/customers-open.yaml` |
| 5 | Cap a scan | `practice/customers-capped.yaml` |
| 6 | Key maps | `practice/customers-coded.yaml` |
| 7 | Type maps | `practice/customers-dated.yaml`, `examples/tables/spec.yaml` |
| 8 | Chosen keys, and one key | `practice/customers.yaml` |
| 9 | A table against a service | `practice/profiles-vs-service.yaml` |
| 10 | Repeat a comparison inside one run | `practice/customers-soak.yaml` |
| 11 | Graph parity | `examples/graph/spec.yaml` |
| 12 | Tidy up | |

Sections 1 to 3 are the base and the shortest path to a first result. Sections 4 to 10 each need only
section 3, in any order. Section 11 needs sections 1 and 2.

## 1. The practice database

This section is not a feature. It makes the two tables the chapter compares: a PostgreSQL 16 server in
a container of its own, on the installation's Docker network so that the app reaches it by name, with a
password that a command makes and keeps in a file.

**If you come from chapter 8.** [Chapter 8](08-prechecks-and-schedules.md) makes the same container,
`deltasleuth-practice-db`, and then changes its tables: one more column, one column of another type and
one more table. Do not make the container again. Skip steps 1 to 5 and run step 6 only: it loads the
example data anew, so that your outputs are the ones printed here. Three things then differ for you,
and each is said where it happens: section 2, step 6 (the two address files exist already), section 2,
step 12 (the app is not made anew), and section 7, step 4 (one run is paused once).

1. Create the password of the database's administrator account. `umask 077` makes the file readable by
   you alone. The command prints nothing.

```bash
(umask 077; openssl rand -hex 24 > deploy/secrets/practice_db_password)
```

2. Let the database container read the file. It runs under another user id than yours; the folder's
   own mode, 700, keeps the file private on this machine.

```bash
chmod 444 deploy/secrets/practice_db_password
```

3. Look up the Docker network of the installation this folder holds. The command asks Compose, which
   reads the folder's own settings, so it needs no name from you.

```bash
docker compose ps -a --format '{{.Networks}}' app
```

You should see one name: the Compose project's name followed by `_default`.

```text
deltasleuth_default
```

   In a second installation the name starts with your project name, for example `ds2_default`. That
   is the name to write after `--network` in step 4. A container started on another installation's
   network cannot be reached by this installation's app.

4. Start the practice database. `POSTGRES_PASSWORD_FILE` names the file that holds the password; it
   does not hold the password.

```bash
docker run -d --name deltasleuth-practice-db --network deltasleuth_default -e POSTGRES_PASSWORD_FILE=/run/secrets/practice_db_password -v "$PWD/deploy/secrets/practice_db_password:/run/secrets/practice_db_password:ro" postgres:16-alpine
```

You should see one line of 64 letters and digits, the new container's id:

```text
43e9ee893f65d504db828baa2ad16aba76a4040dff23c8ab5d426f6c1a6bdfd6
```

5. Wait five seconds, then ask whether it answers.

```bash
docker exec deltasleuth-practice-db pg_isready -U postgres
```

You should see:

```text
/var/run/postgresql:5432 - accepting connections
```

   Asked at once, it said `/var/run/postgresql:5432 - no response` here, and step 6 then ended with
   `psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed`. Wait a
   few seconds and ask again.

6. Load the product's example data.

```bash
docker exec -i deltasleuth-practice-db psql -q -U postgres -v ON_ERROR_STOP=1 -f - < examples/tables/seed.sql
```

You should see two notices and nothing else:

```text
psql:<stdin>:20: NOTICE:  database "ds_demo_legacy" does not exist, skipping
psql:<stdin>:21: NOTICE:  database "ds_demo_platform" does not exist, skipping
```

   Run a second time, or after chapter 8, the command prints nothing: the file deletes the two
   databases it finds and makes them anew.

Started this way, only the installation's containers reach the practice database. To look at its tables
with a database client of your own, add `-p 127.0.0.1:<db port>:5432` before `postgres:16-alpine` in
step 4, where `<db port>` is a free port of this machine from 1024 to 65535. Checked here with port
18361, which then answered on this machine.

**What the data is.** The server now holds two databases that describe the same customers:

- `ds_demo_legacy`, an old customer system. Its table `crm.customers` has 1,000 rows with the columns
  `customer_id`, `email_address`, `full_name`, `phone`, `status`, `country`, `balance`, `created_at` and
  `branch_code`. Eleven of its accounts have the status `closed`.
- `ds_demo_platform`, its replacement. Its table `public.users` has 990 rows with the columns
  `user_id`, `email`, `name`, `phone`, `status`, `country`, `balance`, `created_at`, `marketing_opt_in`
  and `updated_at`.

The 989 customers both tables hold agree, except for these planted differences on the platform:

| Customer | What is planted |
|---|---|
| 100003 | The e-mail address in other case, with a space at its end |
| 100005 | Another phone number |
| 100007 | Status `suspended` in place of `active` |
| 100009 | Balance 21.25 in place of 11.25 |
| 100011 | Missing on the platform |
| 100013 | `active` on the platform, `closed` in the old system |
| 100050, 100150, and so on to 100950 | Ten closed accounts that were never migrated: missing on the platform |
| 101001 | Only on the platform |

The two tables also store `created_at` differently: the old system as local time in London without a
time zone, the platform as a time with its zone. Section 7 is about that.

## 2. Declare a database target

### What it is for

DeltaSleuth reads only what is declared as a target, and a database is declared in the targets file
alone (the product's own list holds services only). For a PostgreSQL database a target holds two
things: the name of a secret file with the connection string, and how hard DeltaSleuth may read. The
person who administers the installation writes it, with an account that the database's owners provide.

### Before you start

The practice database of section 1 runs. Nothing is switched on in the settings file.

### In the web UI

The web UI does not offer this. Its "Targets" screen lists every target and declares services only
([chapter 4](04-specs.md), section 1); no screen declares or checks a database. A database is declared
in the targets file, which is changed only on the installation's machine.

Two forms show what is declared. On the "Pre-checks" screen, the form "New table pre-check of two
schemas, paired by name" lists the database targets under "Side a" and "Side b", each with its engine,
such as `crm-legacy (postgresql)`. On the "Specs" screen, the form "Draft a spec from sample pairs"
suggests target ids as you type.

### On the command line

First the account. Every DeltaSleuth session is read-only, but give it an account that can read the
compared tables and nothing more.

1. Create the password of that account. The command prints nothing.

```bash
(umask 077; openssl rand -hex 24 > deploy/secrets/practice_reader_password)
```

2. Create the account `ds_reader` in the practice database. The password goes from its file through a
   pipe into the database, so it appears neither on the screen nor in a command.

```bash
{ printf "CREATE ROLE ds_reader LOGIN PASSWORD '"; tr -d '\n' < deploy/secrets/practice_reader_password; printf "';\n"; } | docker exec -i deltasleuth-practice-db psql -q -U postgres -v ON_ERROR_STOP=1
```

3. Give the account what a comparison needs: the right to connect to each database, the right to use
   the schema (the folder of tables) `crm`, and the right to read the two tables. The command prints
   nothing when it succeeds.

```bash
docker exec -i deltasleuth-practice-db psql -q -U postgres -v ON_ERROR_STOP=1 <<'SQL'
GRANT CONNECT ON DATABASE ds_demo_legacy, ds_demo_platform TO ds_reader;
\connect ds_demo_legacy
GRANT USAGE ON SCHEMA crm TO ds_reader;
GRANT SELECT ON crm.customers TO ds_reader;
\connect ds_demo_platform
GRANT SELECT ON public.users TO ds_reader;
SQL
```

4. Check that the account reads.

```bash
docker exec deltasleuth-practice-db psql -U ds_reader -d ds_demo_legacy -c 'SELECT count(*) FROM crm.customers'
```

You should see:

```text
 count 
-------
  1000
(1 row)
```

5. Check that it cannot write.

```bash
docker exec deltasleuth-practice-db psql -U ds_reader -d ds_demo_legacy -c "UPDATE crm.customers SET status = 'closed' WHERE customer_id = 100001"
```

You should see a refusal:

```text
ERROR:  permission denied for table customers
```

Next the connection string, called the DSN: one line with the account, its password, the server and
the database. The app runs in a container on Docker's network, so the DSN names the practice database
by its container name, `deltasleuth-practice-db`, and by the port 5432 that PostgreSQL uses inside
that network. `localhost` would mean the app's own container.

6. Write the DSN of the old system to a secret file. The password goes from one file into the other.

   Only if you come from chapter 8, run this command first; everyone else skips it. That chapter's
   files `crm_legacy_dsn` and `platform_users_dsn` exist already, are read-only and name the
   administrator account. The command makes them writable, so that this step and the next replace
   their content. It prints nothing.

```bash
chmod u+w deploy/secrets/crm_legacy_dsn deploy/secrets/platform_users_dsn
```

   Then, for everyone, the DSN file. The command prints nothing.

```bash
(umask 077; { printf 'postgresql://ds_reader:'; tr -d '\n' < deploy/secrets/practice_reader_password; printf '@deltasleuth-practice-db:5432/ds_demo_legacy'; } > deploy/secrets/crm_legacy_dsn)
```

7. Write the DSN of the platform. The command prints nothing.

```bash
(umask 077; { printf 'postgresql://ds_reader:'; tr -d '\n' < deploy/secrets/practice_reader_password; printf '@deltasleuth-practice-db:5432/ds_demo_platform'; } > deploy/secrets/platform_users_dsn)
```

8. Let the app's container read the two files. The command prints nothing.

```bash
chmod 444 deploy/secrets/crm_legacy_dsn deploy/secrets/platform_users_dsn
```

9. Make a folder `practice` for the files of this chapter, and keep a copy of the site file in it as
   it is now. Section 12 puts the copy back. Neither command prints anything. Git lists the folder as
   untracked, which is harmless.

```bash
mkdir -p practice
```

```bash
cp deploy/docker-compose.site.yml practice/site.yml.before-tables
```

10. Tell Docker to show the two files to the app. Open `deploy/docker-compose.site.yml` in a text
   editor and make two changes. First, find the line under `app:` that starts with `secrets: []` and
   replace the whole line, with any comment on it, by this one. Keep its four leading spaces.

```yaml
    secrets: [crm_legacy_dsn, platform_users_dsn]
```

   Second, add these three lines at the very end of the file. The first starts in the first column.

```yaml
secrets:
  crm_legacy_dsn: {file: ./secrets/crm_legacy_dsn}
  platform_users_dsn: {file: ./secrets/platform_users_dsn}
```

   If the file already has a line `secrets:` in the first column that does not start with `#`, as
   after [chapter 7](07-engines-and-files.md), add only the two indented lines under that one, and add
   the two names to the list of the first change.

11. Ask Compose whether it can read the file. This command starts nothing.

```bash
docker compose config | grep -c crm_legacy_dsn
```

You should see a number, here `5`: the lines of Compose's description that name the secret. An error
in place of the number names the mistake in the file.

12. Apply the change. Compose makes the app's container anew; the data stays. A restart would not do:
    the site file is read only by this command.

```bash
docker compose --profile mocks up -d
```

You should see:

```text
 Container deltasleuth-mocks-1  Running
 Container deltasleuth-db-1  Running
 Container deltasleuth-app-1  Recreate
 Container deltasleuth-app-1  Recreated
 Container deltasleuth-db-1  Waiting
 Container deltasleuth-db-1  Healthy
 Container deltasleuth-app-1  Starting
 Container deltasleuth-app-1  Started
```

   After chapter 8 the third line reads ` Container deltasleuth-app-1  Running` and no line says
   `Recreate`: that chapter's line in `.env` already shows the two files to the app, so nothing
   changed for it. Both ways of naming the files can stay side by side.

Now the targets file. As chapter 1 copied it, it already declares both practice databases, so there is
nothing to add.

13. Read the entry of the old system.

```bash
grep -A 3 'crm-legacy:' deploy/config/targets.yaml
```

You should see:

```text
  crm-legacy:
    dsn_ref: file:/run/secrets/crm_legacy_dsn
    rate_per_s: 50                             # per-key reads per second (a group of up to 200 keys
    max_concurrency: 2                         # between two tables); in flight, across all runs
```

`crm-legacy` is the target's id, the name a spec uses. `dsn_ref` names the secret file as the app's
container sees it: Docker shows `deploy/secrets/<name>` to the app as `/run/secrets/<name>`.
`rate_per_s` and `max_concurrency` are the budget: at most 50 reads of single keys a second and two at
once, over all runs together. A scan is one long query a side and stands outside that budget. The
entry `platform-users` beside it reads the same, with `platform_users_dsn`.

14. Check that DeltaSleuth reaches the old system and may read the table. This check reads no row.

```bash
ds target check crm-legacy --table crm.customers
```

You should see:

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

15. Check the platform. Without a schema before the table's name, the database's own search order
    finds the table, here in the schema `public`.

```bash
ds target check platform-users --table users
```

You should see:

```text
target platform-users: postgresql 16.14, character set UTF8, TLS no, session ok
public.users (table, consistency snapshot)
  user_id           bigint                    integer  key
  email             text                      text     key
  name              text                      text     key
  phone             text                      text     key
  status            text                      text     key
  country           text                      text     key
  balance           numeric(12,2)             decimal  key
  created_at        timestamp with time zone  instant
  marketing_opt_in  boolean                   boolean
  updated_at        timestamp with time zone  instant
```

The practice targets are now declared, and section 3 goes on from here.

**A database of your own.** Skip this part while you follow the practice data: the walk-through never
changes the targets file. For a database of your own the steps above are the same: an account that may
only read; its DSN in a file of `deploy/secrets/` with mode 444; and the file's name in the site file,
applied as in steps 10 to 12. Four more steps declare it. `<target id>` is the name specs will use:
lower-case letters, digits and `-`, starting with a letter. `<secret name>` is the name of the DSN
file.

1. Open `deploy/config/targets.yaml` in a text editor and add an entry of four lines under
   `targets:`, indented like the entries around it.

```yaml
  <target id>:
    dsn_ref: file:/run/secrets/<secret name>
    rate_per_s: 20
    max_concurrency: 2
```

2. Check the file before the app reads it. The command opens what a start of the app opens.

```bash
ds licence
```

You should see four lines, the first beginning `installation I-`, or in their place the one `error:`
line a start would log:

```text
installation I-bc919bf66daad82a
licence none: this build needs no licence
state open
allows: runs yes, sso yes, signoff yes, packs yes
```

3. Restart the app, which reads the targets file only when it starts.

```bash
docker compose restart app
```

You should see:

```text
 Container deltasleuth-app-1  Restarting
 Container deltasleuth-app-1  Started
```

4. Check the new target.

```bash
ds target check <target id>
```

You should see the first line of step 14 with your target's id. Here, for an entry `sales-archive`
whose `dsn_ref` named the file `crm_legacy_dsn`:

```text
target sales-archive: postgresql 16.14, character set UTF8, TLS no, session ok
```

### How to read the result

The first line of `ds target check` says what answered: the engine and its full version, the
database's character set, whether the connection is encrypted, and `session ok` when DeltaSleuth could
open a session as a run opens one. `TLS no` is right for the practice database, whose DSN asks for no
encryption.

The second line names the table as the database spells it, says whether it is a table or a view, and
`consistency snapshot`: a scan will read it as it stood at one moment.

Each further line is one column: its name, the type the database declares, the class DeltaSleuth reads
it as, and the word `key` when the column can serve as a key column. `created_at` cannot: a time is
not a key.

### Limits

- A PostgreSQL target takes `dsn_ref`, `rate_per_s` and `max_concurrency`, and no other line.
- A MySQL or an Oracle database, and a folder of CSV or Parquet files, are declared with other lines:
  see [chapter 7](07-engines-and-files.md).
- The check is a command only, and it is not written to the audit log.
- The DSN of a real database should ask for an encrypted connection and name the certificate of the
  database's certificate authority. [DEPLOY.md](../mvp/DEPLOY.md), section 7, gives the form
  `host=... dbname=... user=... password=... sslmode=verify-full sslrootcert=/run/secrets/<name>`. Not
  verified here: a DSN with TLS, because the practice database has no certificate.
- In a DSN written as `postgresql://...`, the characters `@` and `/` inside the password must be
  written `%40` and `%2F`. The passwords this chapter makes hold only digits and the letters a to f.

### If something goes wrong

**`error: cannot read secret file /run/secrets/crm_legacy_dsn: No such file or directory`**
The app's container does not see the DSN file. The site file does not name it, or step 12 was not run.

**`permission denied: deploy/secrets/crm_legacy_dsn`** (zsh) or **`bash:
deploy/secrets/crm_legacy_dsn: Permission denied`** (bash), from step 6 or 7
The file exists and is read-only: you ran the step a second time, or chapter 8 made the file. Make
it writable with the `chmod u+w` command under step 6, run steps 6 and 7 again, then step 8.

**`error: target crm-legacy: cannot connect to target crm-legacy: failed to resolve host
'deltasleuth-practice-db': [Errno -2] Name or service not known`**
The app does not find the practice database on its network. Either it is not running, for example
after the machine restarted, or the DSN misspells its name, or it was started on another network
than this installation's. Start it:

```bash
docker start deltasleuth-practice-db
```

If the line stays, print the network the container is on:

```bash
docker inspect --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' deltasleuth-practice-db
```

You should see the name that section 1, step 3, printed, here `deltasleuth_default`.

If it prints the network of another installation, the container was started with the wrong name
after `--network`, which happens in a second installation. Remove it (section 12, step 3) and make
it again: section 1, steps 4 to 6, then section 2, steps 2 to 5.

If it prints nothing, join the container to the installation's network. Write the name of section 1,
step 3, after `connect`. The command prints nothing.

```bash
docker network connect deltasleuth_default deltasleuth-practice-db
```

Provoked here by taking the container off the network: the check failed with the line above while
`docker ps` listed the container as running, and answered `session ok` again after this command.

**`error: target crm-legacy: cannot connect to target crm-legacy: connection failed: connection to
server at "172.30.0.5", port 5432 failed: FATAL:  password authentication failed for user
"ds_reader"`** (the address differs)
The password in the DSN file is not the account's. Provoked here by changing the account's password.
Set it again from the file:

```bash
{ printf "ALTER ROLE ds_reader PASSWORD '"; tr -d '\n' < deploy/secrets/practice_reader_password; printf "';\n"; } | docker exec -i deltasleuth-practice-db psql -q -U postgres -v ON_ERROR_STOP=1
```

If you ran step 1 a second time, the password file is new and the DSN files still hold the old
password. Then also write the DSN files again: `chmod u+w` on each, steps 6 to 8, and the same for
the two files of section 11 if you made them. Met here with the files of section 11.

**`error: target crm-legacy: cannot connect to target crm-legacy: connection failed: connection to
server at "127.0.0.1", port 18361 failed: Connection refused`**
The DSN says `localhost` and a port of your machine. Inside the app's container `localhost` is that
container. Name the database's container and port 5432, as in step 6.

**`error: the DSN of target crm-legacy is not a valid connection string`**
The file does not hold a connection string the driver can read. Provoked here with a password that
held a plain `@`.

**`error: target crm-legacy: the role may not read crm.customers: grant it SELECT`**
The account lacks the right to read the table. Provoked here with `REVOKE SELECT`. Run step 3 again.

**`error: target platform-users: table or view public.nosuch not found or not visible`**
The table's name or schema is wrong, or the account lacks `USAGE` on the schema: after `REVOKE USAGE
ON SCHEMA crm`, `ds run` said the same of `crm.customers`.

**After `examples/tables/seed.sql` was loaded again**
The file deletes both databases and makes them anew, so the rights of step 3 are gone, and so are the
view of section 6 and the table of section 9. Here the check of step 14 then said `table or view
crm.customers not found or not visible`, and the check of step 15 said `the role may not read
public.users: grant it SELECT`. Run step 3 again, and the commands under "Before you start" of
sections 6 and 9 if you need them.

**`error: target linker-a is not a database target (dsn_ref or engine)`**
The id names a service. `ds target check` is for databases and file folders.

**`error: target 'nosuch' is not in the registry`**
No entry of the targets file has that id.

**`error: /targets/sales-archive/allow_private_network [schema.not] member 'allow_private_network' is
not allowed here`**, from the check under "A database of your own", step 2
The entry of a PostgreSQL target holds a line it does not take. Remove the line.

**`docker: Error response from daemon: Conflict. The container name "/deltasleuth-practice-db" is
already in use by container "..."`**, from section 1, step 4
The practice database exists already. Go on with section 1, step 5.

To change a DSN file later, make it writable with `chmod u+w`, write it again, and run `chmod 444`
again. The app reads the file whenever it opens a connection: here a DSN rewritten that way took
effect on the next run, without a restart.

## 3. The smallest table spec and its first run

### What it is for

A table spec says which two tables are compared, which columns identify a row, and which columns are
compared. A run of it without a list of keys scans both tables once: identical rows are counted, and
every other key is then read again on both sides and reported. Whoever owns a migration writes the
spec; an operator runs it.

### Before you start

Section 2 is done. You need the role `spec_author` to store a spec and `operator` to start a run; the
token of chapter 1 has both.

Write the spec file into the folder `practice` that section 2, step 9, made.

```bash
cat > practice/customers.yaml <<'EOF'
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata:
  name: practice-customers
  version: "1.0.0"
  owner: practice
normalizerVersion: "1.1.0"
key:
  name: customer_id
  type: string
sides:
  a:
    role: legacy
    target: crm-legacy
    table:
      schema: crm
      name: customers
      keyColumns: [customer_id]
      columns: [phone, status, country, balance]
  b:
    role: new
    target: platform-users
    table:
      name: users
      keyColumns: [user_id]
      columns: [phone, status, country, balance]
compare: {}
recheck:
  delay: 5s
  times: 1
security:
  clear: ["$.status", "$.country", "$.balance"]
EOF
```

What the lines say:

| Lines | Meaning |
|---|---|
| `metadata` | The spec's name and version, and who owns it. A changed spec gets a new version. |
| `key` | What the key is called. Between two tables a key is the key column's value as text. |
| `sides.a.target`, `sides.b.target` | The target id of each side's database (section 2). |
| `table.schema`, `table.name` | The table or view. Side b leaves `schema` out. |
| `table.keyColumns` | The column that identifies a row: `customer_id` on side a, `user_id` on side b. |
| `table.columns` | The columns to compare. Both sides name the same four. |
| `compare: {}` | No special comparison rule. |
| `recheck` | When a key that differs is read a second time. A scan does not need it (section 8 does). |
| `security.clear` | The three values the screens may show as they are. Every other value is shown masked, as a token such as `tok:free_text:v1:EOAH54HD...`. |

The last two lines are not needed for a comparison. Without them the screens show every value masked.

### In the web UI

1. Click "Specs" in the bar at the top. You see the heading "Specs", the stored specs ("None." while
   there are none), and a panel "Upload a spec".
2. In "Upload a spec", press the file button (the browser names it, for example "Choose File") and
   choose `practice/customers.yaml`. The file's text appears in the large box.
3. Press "Validate". Beside the buttons you read "No issues.".
4. Press "Save". You read "Saved as spec #1." (the number can differ), and the list at the top gains a
   row: Name `practice-customers`, Version `1.0.0`, Hash `dc73baa30341`, and the links "Schedule…" and
   "Pre-check…".
5. Click "Runs" in the bar. In the panel "New run", choose `practice-customers 1.0.0 (#1)` under
   "Spec", leave "Mode" at `batch`, leave the box "Keys, one per line (none: the spec's key source; a
   table pair without one scans both tables)" empty, and press "Start".
6. The run opens under the heading "Run R-…" with a status badge. The page asks again every two
   seconds; after a few seconds the badge reads `completed`. Under the heading you see, in this order:
   - a grey line `practice-customers 1.0.0 · batch · cycle 1 · created …`, with who started the run
     and when it finished;
   - `Evidence retained until 2027-01-03 (retention standard)`, with your own date;
   - a warning: `Unapproved spec version: clear, population_filters were not approved by a second
     person (tokens mode has no approvers). Not sign-off evidence.` That is normal with token sign-in;
   - one or two grey lines of the schema guard ([chapter 8](08-prechecks-and-schedules.md)). The
     first begins `no change observed against the run's plan`. The second, `no earlier run of this
     version recorded its tables' shapes: this run records them`, shows only on the first run of a
     spec version: not on a second "Start", and not when `ds run` of the command line came first;
   - a link `Pair readiness: not ready (2 of 5 gates)`;
   - nine report buttons, from `report.pdf` to `readiness.csv`;
   - the scan line: `Table scan (done): 1,000 rows on A, 990 on B; 985 identical; 16 to compare; 0 not
     compared (NULL, empty or over-long key).`;
   - a progress bar with `16/16 keys · done 16`;
   - three badges with counts: `MISMATCH 4`, `NOT_FOUND_A 1` and `NOT_FOUND_B 11`. Their order on
     the page changes from run to run, here and in every later section;
   - a fold "Counters".
7. Below a bar of filters come three tables. "Root causes" has five rows, the first `MISSING_DOCUMENT`
   with 11 under "Keys". "Results" has 16 rows, for example Key `100011`, Outcome `NOT_FOUND_B`,
   Confirmation `rechecked`, A `COMPLETE`, B `ABSENT`. "Findings" has 16 rows, for example Key
   `100007`, Pointer `/status`, Change `value_changed`, Taxonomy `VALUE_MISMATCH`, A `active`, B
   `suspended`.
8. Further down, under "Census, adjudication and evidence packs", the panel "Cycle 1" reads `Census:
   side a counted, 1,000 rows of crm.customers read to the end in 1 snapshot, 2026-10-05; side b
   counted, 990 rows of public.users read to the end in 1 snapshot, 2026-10-05`. This is the one
   place on the screen that names the tables.
9. Click the key `100005` in "Results". The page "Key 100005" shows the two rows side by side under
   "Canonical A and B (masked)", with `phone` marked and a different token on each side, then
   "Findings" and "Observations". In "Observations" the column "HTTP" reads `200` for a row that was
   found and `404` for a row that is not there; no HTTP call is made to a database.

### On the command line

`ds` runs in a container, which does not see the files of your folder. The folder
`/var/lib/deltasleuth/p0` of the app carries files in and out, and every `ds` command sees it.

1. Copy the folder `practice` into it. Repeat this command whenever a file in `practice` is new or
   changed.

```bash
docker compose cp practice/. app:/var/lib/deltasleuth/p0/practice
```

You should see:

```text
 deltasleuth-app-1 copy practice/. to deltasleuth-app-1:/var/lib/deltasleuth/p0/practice Copied
```

2. Check the spec. `--targets` names the targets file as the container sees it.

```bash
ds spec validate /var/lib/deltasleuth/p0/practice/customers.yaml --targets /etc/deltasleuth/targets.yaml
```

You should see:

```text
ok: customers.yaml is valid (0 warning(s))
```

3. Run it. The command stores the spec, starts the run and waits for its end.

```bash
ds run /var/lib/deltasleuth/p0/practice/customers.yaml
```

You should see:

```text
run R-35T77G09RZ5DWP066CE61V9J3T started
warning: spec 1 (practice-customers 1.0.0) needs a four-eyes approval of clear, population_filters; tokens mode has no approvers, so run R-35T77G09RZ5DWP066CE61V9J3T is labelled unapproved
another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
completed
  total 1001, compared 1001, match 985, different 16, indeterminate 0, cancelled 0, outstanding 0
  MATCH 985, MISMATCH 4, NOT_FOUND_A 1, NOT_FOUND_B 11
```

The first three lines can come in another order; the Conventions at the top say why the later outputs
show only the first. The warning is normal with token sign-in: this spec lists `columns` and
`security.clear`, which a second person would approve under single sign-on ("Limits" below). The line
`another worker is processing keys` says that the running app does the work, and this command only
waits.

4. Read the scan's own counters. `ds run` does not print them; the application's API does. The token
   goes from its file through a pipe to curl.

```bash
{ printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - http://localhost:8000/v1/runs/<run id> | grep -o '"scan": {[^}]*}'
```

You should see:

```text
"scan": {"done": true, "rows_a": 1000, "rows_b": 990, "last_at": "2026-10-05T19:32:07.823923Z", "matched": 985, "first_at": "2026-10-05T19:32:07.823923Z", "skipped_a": 0, "skipped_b": 0, "candidates": 16, "checkpoint": 1, "snapshots_a": 1, "snapshots_b": 1}
```

5. List the newest run.

```bash
ds runs --limit 1
```

You should see:

```text
R-35T77G09RZ5DWP066CE61V9J3T  practice-customers 1.0.0  batch  completed  cycle 1  different 16 of 1001
  Unapproved spec version: clear, population_filters were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
```

6. Write the list of differing keys, a report named `discrepancies.csv`, into the app's folder.

```bash
ds report <run id> --out /var/lib/deltasleuth/p0/reports --kind discrepancies.csv
```

You should see the file's name:

```text
/var/lib/deltasleuth/p0/reports/R-35T77G09RZ5DWP066CE61V9J3T-discrepancies.csv
```

7. Copy the reports to a folder of your machine. Git ignores the name `p0-out`.

```bash
docker compose cp app:/var/lib/deltasleuth/p0/reports/. p0-out
```

You should see:

```text
 deltasleuth-app-1 copy deltasleuth-app-1:/var/lib/deltasleuth/p0/reports/. to p0-out Copied
```

8. Print six of the report's columns.

```bash
cut -d, -f3,4,6,9,13,14 p0-out/<run id>-discrepancies.csv
```

You should see 16 keys:

```text
key,outcome,confirmation,path_pattern,value_a_tok,value_b_tok
100005,MISMATCH,rechecked,$.phone,tok:free_text:v1:EOAH54HDCEF5IL7OQD6RSISXWH,tok:free_text:v1:NZZK44UGCU647P5AOQUGMEOAMP
100007,MISMATCH,rechecked,$.status,active,suspended
100009,MISMATCH,rechecked,$.balance,11.25,21.25
100011,NOT_FOUND_B,rechecked,$,,
100013,MISMATCH,rechecked,$.status,closed,active
100050,NOT_FOUND_B,rechecked,$,,
...
100950,NOT_FOUND_B,rechecked,$,,
101001,NOT_FOUND_A,rechecked,$,,
```

### How to read the result

**The scan.** `rows_a` and `rows_b`, on the screen "1,000 rows on A, 990 on B", are the rows the scan
read on each side. `matched`, "985 identical", are the keys whose rows are the same in all four
compared columns. `candidates`, "16 to compare", are all other keys: a row that differs, or a key on
one side only.

**Identical rows.** They are counted and never read again. They have no row in "Results" and no key
page. They still count in the run's counters: `match 985`, as outcome `MATCH`. The run report
`coverage.csv` counts them in its column `confirmation_bulk_exhaustive`.

**Differing keys.** Each of the 16 was read again on both sides, by its key, and judged like any key
of [chapter 3](03-first-comparison.md). That second read is their confirmation, so each says
`rechecked`. By [TABLES.md](../mvp/TABLES.md) these reads go in groups of up to 200 keys, one query
a side for a group; nothing switches that on or off. Against the planted differences:

| Keys | Outcome | Why |
|---|---|---|
| 100005, 100007, 100009 | `MISMATCH` | Phone, status and balance differ |
| 100013 | `MISMATCH` | Status `closed` against `active` |
| 100011 and the ten closed accounts | `NOT_FOUND_B` | No row on the platform |
| 101001 | `NOT_FOUND_A` | No row in the old system |
| 100003 | counted as identical | Its difference is in the e-mail address, which this spec does not compare |

The counters reconcile: `total 1001` is 985 identical keys and 16 keys read again, and `different 16`
is 4 and 11 and 1.

**Skipped rows.** A row whose key is empty, missing (NULL) or longer than 1,024 bytes cannot be
paired. The scan counts it under "not compared", per side as `skipped_a` and `skipped_b`, and in no
other counter. The practice tables have none. Checked here with a view that adds one row with a NULL
key to side a: `"rows_a": 1001`, `"skipped_a": 1`, on the screen "1 not compared", and the run's
counters as above.

**Values.** Status, country and balance show as they are, because the spec lists them under
`security.clear`. The phone numbers show as tokens: the same number gives the same token, so two
different tokens mean two different numbers. Your tokens differ from the ones printed here.

**Columns with other names, and all columns.** Side a calls the e-mail column `email_address` and
side b `email`, so the spec above cannot list it on both sides. An entry `{name: email_address, as:
email}` on side a compares it under side b's name; the bundled spec of section 7 does that. A side
that leaves `columns` out compares every column both sides have, and the scan line then lists the
others, for example `Columns only on B: marketing_opt_in, updated_at.`

### Limits

- Keys pair by their text. A customer written `100005` in one table and `0100005` or `C-00100005` in
  the other never pairs: section 6 has the remedy.
- A key the scan found identical cannot be opened on a screen. Only the keys read again have a page.
- The Run screen does not say which columns a run compared. They are in the report `coverage.csv`
  (column `selected_fields`) and in the line "Tables" of `report.pdf`.
- A stored spec opens from its name on the "Specs" screen as the store holds it, without the file's
  comments ([chapter 4](04-specs.md), section 4). Keep the file.
- The store accepts a changed file under an unchanged name and version, as a second spec with the same
  label. Here a copy of `practice/customers.yaml` with `delay: 6s` in place of `delay: 5s` was stored
  beside the first, and "New run" then offered two entries `practice-customers 1.0.0` that differ only
  in the number in brackets. Raise `metadata.version` with every change.
- With single sign-on, a spec that lists `columns` or `security.clear` needs the approval of a second
  person before it runs ([APPROVALS.md](../mvp/APPROVALS.md)). With token sign-in it runs, labelled
  unapproved, and cannot be signed off.
- On large tables, read [DEPLOY.md](../mvp/DEPLOY.md), section 7, first: a scan sorts each table on its
  database server and holds one view of it open until the scan ends.

### If something goes wrong

`ds spec validate` and "Validate" read the spec's text and the targets file. They do not open the
database, so a wrong table or column name passes them and shows when the run is created: as one
`error:` line of `ds run`, or in red under the "Start" button. The spec is stored all the same.

**`error: /sides/a/target [unknown_target] target 'crm-legcy' is not in the registry`**
The spec names a target id the targets file does not have.

**`error: side b: table or view userz not found or not visible`**
The table's name is wrong, or the account may not use its schema.

**`error: side a: column 'mobile' is not in crm.customers`**, or **`error: side b: column 'userid' is
not in public.users`**
A compared column or a key column is misspelt.

**`error: side a: column 'branch_code' has no counterpart on side b`**
A column is listed on one side and has no column of that name on the other. List it on both sides,
rename it with `as`, or leave it out.

**`error: side a: the role may not read crm.customers: grant it SELECT`**, or **`error: side a: cannot
connect to target crm-legacy: ...`**
See section 2, "If something goes wrong".

**`error: / [document_unreadable] cannot read /var/lib/deltasleuth/p0/practice/nosuch.yaml: No such
file or directory`**
The file is not in the app's folder. Run step 1 again, and give the path inside the container: the
path `practice/customers.yaml` of your own folder gets the same line.

**`paused (run_paused_schema_drift)`**, then a line that begins `Paused by the schema guard before
any table row was read: column 'country' was character(2) (char) in run R-... of this version and is
text (text) now`, then `error: the run is paused`
A table changed since the last run of this spec. The schema guard of
[chapter 8](08-prechecks-and-schedules.md) then creates the run paused and waits for you. Met here
with `practice/customers.yaml`, after that chapter's `ALTER TABLE` commands had been run on the
practice tables. Either accept the tables as they are: the button "Resume (accept the changes)" on the
run's page, or the request under "If something goes wrong" of section 7. Or end the paused run with
its button "cancel" and put the data back (section 1, step 6, then section 2, step 3).

## 4. Row filters

### What it is for

A row filter narrows one side to the rows that should be compared, for example the open accounts,
because the others were never migrated. Only those rows are scanned and counted. Whoever writes the
spec sets it, and everyone who reads the result must know it is there.

### Before you start

Section 3 is done. Write the spec file: it is the one of section 3 with another name and one more
line, the filter on side a.

```bash
cat > practice/customers-open.yaml <<'EOF'
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata:
  name: practice-customers-open
  version: "1.0.0"
  owner: practice
normalizerVersion: "1.1.0"
key:
  name: customer_id
  type: string
sides:
  a:
    role: legacy
    target: crm-legacy
    table:
      schema: crm
      name: customers
      keyColumns: [customer_id]
      columns: [phone, status, country, balance]
      where: "status <> 'closed'"              # the row filter: side a compares open accounts only
  b:
    role: new
    target: platform-users
    table:
      name: users
      keyColumns: [user_id]
      columns: [phone, status, country, balance]
compare: {}
recheck:
  delay: 5s
  times: 1
security:
  clear: ["$.status", "$.country", "$.balance"]
EOF
```

### In the web UI

1. On "Specs", upload `practice/customers-open.yaml` and press "Save", as in section 3.
2. On "Runs", start a `batch` run of `practice-customers-open 1.0.0` with no keys.
3. The scan line now ends with the filter: `Table scan (done): 989 rows on A, 990 on B; 985 identical;
   6 to compare; 0 not compared (NULL, empty or over-long key). Filter on A: "status" <> 'closed'.`
4. The badges read `MISMATCH 4`, `NOT_FOUND_A 1` and `NOT_FOUND_B 1`.
5. In the bar of filters above the tables, type `FILTER_MEMBERSHIP` into the box `taxonomy_code` and
   press "Filter". "Findings" keeps one row: Key `100013`, Pointer `/`, Taxonomy `FILTER_MEMBERSHIP`,
   A `false`, B `true`. The link "clear" removes the filter of the page again.

### On the command line

1. Copy the folder `practice` again. It prints the `Copied` line of section 3, step 1.

```bash
docker compose cp practice/. app:/var/lib/deltasleuth/p0/practice
```

2. Check the spec.

```bash
ds spec validate /var/lib/deltasleuth/p0/practice/customers-open.yaml --targets /etc/deltasleuth/targets.yaml
```

You should see:

```text
ok: customers-open.yaml is valid (0 warning(s))
```

3. Run it.

```bash
ds run /var/lib/deltasleuth/p0/practice/customers-open.yaml
```

You should see:

```text
run R-35T75DQ7RBZ9NH0JSEADDZ60R6 started
completed
  total 991, compared 991, match 985, different 6, indeterminate 0, cancelled 0, outstanding 0
  MATCH 985, MISMATCH 4, NOT_FOUND_A 1, NOT_FOUND_B 1
```

4. Compare the one customer who is closed on one side and active on the other.

```bash
ds compare 100013 --spec /var/lib/deltasleuth/p0/practice/customers-open.yaml
```

You should see:

```text
100013: MISMATCH (DIFFERENT, rechecked)
  MEDIUM VALUE_MISMATCH value_changed /status
      A: closed
      B: active
  MEDIUM FILTER_MEMBERSHIP value_changed /
      A: False
      B: True
run R-35T75EGKRZ487XWYQ71C5KPW0T; observations a O-62bae85accf3109a b O-1c863f731ecef341
```

### How to read the result

The scan read 989 rows on side a, not 1,000: the eleven closed accounts are outside the filter and
were neither compared nor counted. `NOT_FOUND_B` fell from 11 to 1. The ten accounts that were never
migrated are no longer reported as missing, which is what the filter is for.

Customer 100013 is still reported, and not as missing. The key was found on side b only, so it was
read again on both sides. A read by key does not hide a row outside the filter: it reads the row and
notes whether the filter selects it. The row exists on both sides, inside side b's population and
outside side a's, so the key is a `MISMATCH` with a finding `FILTER_MEMBERSHIP` (A `false`, B `true`)
beside the status that differs.

The Run screen prints the filter as the run keeps it, with the column name in quotes. The reports
print it as the spec writes it: `coverage.csv` in its columns `filter_a` and `filter_b`.

### Limits

- The filter language is small: a column on the left and fixed values on the right, with `=`, `<>`,
  `!=`, `<`, `<=`, `>`, `>=`, `IS NULL`, `IS NOT NULL`, `IN (...)`, `BETWEEN ... AND ...`, `LIKE`
  and `ILIKE`, joined by `AND`, `OR`, `NOT` and brackets. It has no functions, no casts and no
  comparison of two columns. For those, compare a view that does the work. Run here: `<>`, and one
  filter that joined `IN (...)`, `BETWEEN`, `LIKE`, `IS NOT NULL` and `NOT (... ILIKE ...)` with `AND`,
  which a run accepted. [TABLES.md](../mvp/TABLES.md), section 9, has the whole grammar.
- The filter narrows the scan. A comparison of one key (section 8) still reads a row outside it: here
  `ds compare 100050` under this spec answered `NOT_FOUND_B`.
- A run that does not scan (sections 8 and 9) has no scan line, so its filter is not on the Run
  screen; it is in `coverage.csv` and `report.pdf`.
- With single sign-on a `where` needs the approval of a second person
  ([APPROVALS.md](../mvp/APPROVALS.md)).

### If something goes wrong

**`error: /sides/a/table/where [invalid_where] unexpected '(' at position 5`**, from `ds spec
validate`; on the "Specs" screen a table with Pointer `/sides/a/table/where`, Severity `error`, Code
`invalid_where`
The filter is outside the language. Provoked here with `lower(status) = 'closed'`.

**`error: side a: the database refuses the filter or key map: invalid input syntax for type numeric:
"abc"`**, when the run is created
A fixed value does not fit its column. Provoked here with `balance > 'abc'`.

**`error: side a: column 'stattus' is not in crm.customers`**, when the run is created
The filter names a column the table does not have.

## 5. Cap a scan

### What it is for

A new spec is best tried on a small slice before it reads two large tables. A cap stops the scan after
a number of keys to compare, and a time limit stops the run at a moment or after a duration. Whoever
writes the spec sets them; a result cut this way says nothing about the rows that were not read.

### Before you start

Section 3 is done. Write the spec file: the one of section 3 with another name and a block
`execution.stop`.

```bash
cat > practice/customers-capped.yaml <<'EOF'
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata:
  name: practice-customers-capped
  version: "1.0.0"
  owner: practice
normalizerVersion: "1.1.0"
key:
  name: customer_id
  type: string
sides:
  a:
    role: legacy
    target: crm-legacy
    table:
      schema: crm
      name: customers
      keyColumns: [customer_id]
      columns: [phone, status, country, balance]
  b:
    role: new
    target: platform-users
    table:
      name: users
      keyColumns: [user_id]
      columns: [phone, status, country, balance]
compare: {}
recheck:
  delay: 5s
  times: 1
execution:
  stop:
    maxKeys: 5                                 # the cap: stop the scan at the fifth key to compare
security:
  clear: ["$.status", "$.country", "$.balance"]
EOF
```

### In the web UI

1. On "Specs", upload `practice/customers-capped.yaml` and press "Save".
2. On "Runs", start a `batch` run of `practice-customers-capped 1.0.0` with no keys.
3. The grey line under the heading now ends `· stopped: stop_condition:maxKeys`, and the badge reads
   `completed`.
4. The scan line reads `Table scan (done): 13 rows on A, 12 on B; 8 identical; 5 to compare; 0 not
   compared (NULL, empty or over-long key).`
5. Under "Census, adjudication and evidence packs", the panel "Cycle 1" reads `Census: side a not
   covered; side b not covered`.

### On the command line

1. Copy the folder `practice` again. It prints the `Copied` line of section 3, step 1.

```bash
docker compose cp practice/. app:/var/lib/deltasleuth/p0/practice
```

2. Run the spec.

```bash
ds run /var/lib/deltasleuth/p0/practice/customers-capped.yaml
```

You should see:

```text
run R-35T75JGK3915HMV6CNBC7B6Z11 started
completed (stop_condition:maxKeys)
  total 13, compared 13, match 8, different 5, indeterminate 0, cancelled 0, outstanding 0
  MATCH 8, MISMATCH 4, NOT_FOUND_B 1
```

3. Read the scan's counters, with the command of section 3, step 4.

```bash
{ printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - http://localhost:8000/v1/runs/<run id> | grep -o '"scan": {[^}]*}'
```

You should see:

```text
"scan": {"done": true, "rows_a": 13, "rows_b": 12, "last_at": "2026-10-05T19:14:56.471746Z", "matched": 8, "first_at": "2026-10-05T19:14:56.471746Z", "skipped_a": 0, "skipped_b": 0, "candidates": 5, "checkpoint": 1, "snapshots_a": 1, "snapshots_b": 1}
```

### How to read the result

The scan goes through the keys in the order of their text. It stopped at the fifth key to compare,
customer 100013, after 13 rows of side a. The five keys were still read and judged: 100005, 100007,
100009, 100011 and 100013. `total 13` is all the run knows about; the other 987 customers were never
looked at.

Three things tell a cut scan from a whole one. The run's line ends `stopped: stop_condition:maxKeys`.
The row counts are small. And the census, the record of what was read to the end, says `not covered`
for both sides, where the run of section 3 named both tables. The words `Table scan (done)` and
`"done": true` do not tell them apart: here they only say that the scan is over.

**A time limit** is written in the same block, in place of the `maxKeys` line or beside it: `until`
with a moment and its time zone, or `maxDuration` with a duration counted from the run's start. A
scan looks at the clock after every 10,000 keys and at its end, and the reads by key look at it as
they go. To see a limit take hold, give a copy of the spec another name and one of these two lines in
place of `maxKeys: 5`:

```yaml
    until: "2026-01-01T00:00:00Z"              # a moment already past
```

```yaml
    maxDuration: 1s                            # one second from the run's start
```

Run here on the practice tables, both ended `completed_with_issues`, with `(stop_condition:until)`
and `(stop_condition:maxDuration)`. The scan still read both tables to the end, because they have
fewer than 10,000 keys, and its line read `Table scan (done): 1,000 rows on A, 990 on B`. How many
of the 16 keys are read again before the limit takes hold is a matter of timing. Three runs with
`until` gave `compared 1001, different 16, cancelled 0` twice and `compared 986, different 1,
cancelled 15` once. A cancelled key was not compared.

On larger tables the limit cuts the scan itself. Checked here on two tables of 30,000 rows: `total
10000`, and the scan line began `Table scan (completed_with_issues): 10,000 rows on A, 10,000 on B`.

### Limits

- The limits are part of the spec. No option of `ds run` and no field of "New run" sets them.
- `maxKeys` caps the keys to compare, not the rows read: with few differences a capped scan still
  reads far.
- A run cut by `maxKeys` ends `completed`, not `completed_with_issues`.
- A cut run is not evidence for the tables: a sign-off refuses a cycle whose scan was cut or stopped
  before its end ([APPROVALS.md](../mvp/APPROVALS.md)).
- For graph parity (section 11), the structure is not worked out for a cut scan.

### If something goes wrong

**The numbers are far smaller than the tables**
Look for `stopped: stop_condition:` in the run's line, or in brackets in the output of `ds run`. A
spec that was tried with a cap is sometimes run for real with the cap still in it.

## 6. Key maps

### What it is for

Two systems often write the same key differently: `C-00100005` in one, the number 100005 in the
other. Read as text, those never pair, and every row looks missing on the other side. A key map tells
a side how its values become the shared key, so that the rows pair. Whoever writes the spec sets it.

### Before you start

Section 3 is done. The practice tables write their keys alike, so first make something that does not:
a view of the old system's customers whose key is a code such as `C-00100005`. The command prints
nothing when it succeeds.

```bash
docker exec -i deltasleuth-practice-db psql -q -U postgres -d ds_demo_legacy -v ON_ERROR_STOP=1 <<'SQL'
CREATE VIEW crm.customers_coded AS
  SELECT 'C-' || lpad(customer_id::text, 8, '0') AS customer_code, phone, status, country, balance
  FROM crm.customers;
GRANT SELECT ON crm.customers_coded TO ds_reader;
SQL
```

Write the spec file. Side a reads the view, whose key column already holds the shared key. Side b
declares the map: its number, padded with zeros to eight digits, behind the prefix `C-`.

```bash
cat > practice/customers-coded.yaml <<'EOF'
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata:
  name: practice-customers-coded
  version: "1.0.0"
  owner: practice
normalizerVersion: "1.1.0"
key:
  name: customer_code
  type: string
sides:
  a:
    role: legacy
    target: crm-legacy
    table:
      schema: crm
      name: customers_coded
      keyColumns: [customer_code]              # text such as C-00100005: already the shared key
      columns: [phone, status, country, balance]
  b:
    role: new
    target: platform-users
    table:
      name: users
      keyColumns:
        - name: user_id                        # the number 100005
          keyMap: {kind: prefix_pad, prefix: "C-", width: 8, sideType: integer}   # the key map
      columns: [phone, status, country, balance]
compare: {}
recheck:
  delay: 5s
  times: 1
security:
  clear: ["$.status", "$.country", "$.balance"]
EOF
```

### In the web UI

1. On "Specs", upload `practice/customers-coded.yaml` and press "Save".
2. On "Runs", start a `batch` run of `practice-customers-coded 1.0.0` with no keys.
3. The scan line reads as in section 3: `Table scan (done): 1,000 rows on A, 990 on B; 985 identical;
   16 to compare; ...`.
4. In "Results" and "Findings" the keys are the shared ones: `C-00100005`, `C-00100007`, and so on.
   No line of the screen says that a key map was used, or which.

### On the command line

1. Look at the view as DeltaSleuth sees it.

```bash
ds target check crm-legacy --table crm.customers_coded
```

You should see:

```text
target crm-legacy: postgresql 16.14, character set UTF8, TLS no, session ok
crm.customers_coded (view, consistency snapshot)
  customer_code  text           text     key
  phone          text           text     key
  status         text           text     key
  country        character(2)   char     key
  balance        numeric(12,2)  decimal  key
```

2. Copy the folder `practice` again. It prints the `Copied` line of section 3, step 1.

```bash
docker compose cp practice/. app:/var/lib/deltasleuth/p0/practice
```

3. Run the spec.

```bash
ds run /var/lib/deltasleuth/p0/practice/customers-coded.yaml
```

You should see:

```text
run R-35T75QZNEYNTGF42CNTYXPSH8H started
completed
  total 1001, compared 1001, match 985, different 16, indeterminate 0, cancelled 0, outstanding 0
  MATCH 985, MISMATCH 4, NOT_FOUND_A 1, NOT_FOUND_B 11
```

4. Compare one key. Write it as the shared key.

```bash
ds compare C-00100005 --spec /var/lib/deltasleuth/p0/practice/customers-coded.yaml
```

You should see:

```text
C-00100005: MISMATCH (DIFFERENT, rechecked)
  MEDIUM VALUE_MISMATCH value_changed /phone
      A: tok:free_text:v1:EOAH54HDCEF5IL7OQD6RSISXWH
      B: tok:free_text:v1:NZZK44UGCU647P5AOQUGMEOAMP
run R-35T75R7TT5E0Y6Y59KZWK99SMT; observations a O-82009e0212e25073 b O-a2c40f7d92f2e503
```

### How to read the result

The counters are those of section 3: the same rows paired, under another spelling of the key. The key
a run stores and shows is always the shared one, here `C-00100005`, whatever a side writes.

`prefix_pad` is one of two kinds of map. The other, `regex`, rewrites a value with a pattern in each
direction. Run here in place of the `prefix_pad` line, this map gave the same counters:

```yaml
          keyMap:
            kind: regex
            toCanonical: {pattern: "(\\d+)", replacement: "C-00$1"}
            toSide: {pattern: "C-00(\\d+)", replacement: "$1"}
            sideType: integer
```

`toCanonical` makes the shared key from the side's value, and `toSide` makes the side's value from
the shared key. Each pattern must match the whole value.

### Limits

- By [TABLES.md](../mvp/TABLES.md), a value its map does not fit has no shared key, and its row
  counts as not compared (section 3). Not verified here: the practice data has no such value.
- By the same document, a `regex` map works on a PostgreSQL side only, and a map that looks keys up
  in a third table is not supported: compare a view that joins it.
- No screen shows a run's key map. It is in the spec file and in the run's record,
  `GET /v1/runs/<run id>`, under `params.tables`.
- A key map is for the key. A compared column stored in two types is the matter of section 7.

### If something goes wrong

**No key is identical, and the run has about as many keys as both tables have rows**
The keys do not pair. Run here without the map, the same spec gave `total 1990, compared 990, match
0`, with `INCONCLUSIVE 1000, NOT_FOUND_A 990`: each side's keys were looked for on the other side
under the wrong spelling.

**`100005: INCONCLUSIVE (INDETERMINATE, rechecked) reason absent_both`**, from `ds compare 100005`
The key was written the way side b stores it. Under a key map, give the shared key: `C-00100005`.

## 7. Type maps

### What it is for

Two tables can hold the same value in different types: a local time without a zone against a time
with its zone, the letter `Y` against a true-or-false column. Compared as they are, every row
differs. A type map tells each side what the column is, so that both give the same value. Whoever
writes the spec sets it, on both sides.

### Before you start

Section 3 is done. Write the spec file: the one of section 3 with another name, the column
`created_at` on both sides, and a `typeMap` on both sides.

```bash
cat > practice/customers-dated.yaml <<'EOF'
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata:
  name: practice-customers-dated
  version: "1.0.0"
  owner: practice
normalizerVersion: "1.1.0"
key:
  name: customer_id
  type: string
sides:
  a:
    role: legacy
    target: crm-legacy
    table:
      schema: crm
      name: customers
      keyColumns: [customer_id]
      columns: [phone, status, country, balance, created_at]
    typeMap:                                   # the type map of side a
      created_at: {as: timestamp, naiveTimezone: Europe/London}   # local time without a zone
  b:
    role: new
    target: platform-users
    table:
      name: users
      keyColumns: [user_id]
      columns: [phone, status, country, balance, created_at]
    typeMap:                                   # the type map of side b: map the column on both sides
      created_at: {as: timestamp}              # a time with its zone
compare: {}
recheck:
  delay: 5s
  times: 1
security:
  clear: ["$.status", "$.country", "$.balance", "$.created_at"]
EOF
```

### In the web UI

1. On "Specs", upload `practice/customers-dated.yaml` and press "Save".
2. On "Runs", start a `batch` run of `practice-customers-dated 1.0.0` with no keys.
3. The scan line reads `Table scan (done): 1,000 rows on A, 990 on B; 985 identical; 16 to compare;
   ...`: five compared columns now, and the same 985 identical keys as with four.
4. Click the key `100005` in "Results". Under "Canonical A and B (masked)" the line `created_at` reads
   `"2020-01-06T03:05:00.000000Z"` on both sides. No line of the Run screen says which columns were
   converted.

### On the command line

1. Copy the folder `practice` again. It prints the `Copied` line of section 3, step 1.

```bash
docker compose cp practice/. app:/var/lib/deltasleuth/p0/practice
```

2. Run the spec.

```bash
ds run /var/lib/deltasleuth/p0/practice/customers-dated.yaml
```

You should see:

```text
run R-35T760S7HBX739AFVAZ862G2C3 started
completed
  total 1001, compared 1001, match 985, different 16, indeterminate 0, cancelled 0, outstanding 0
  MATCH 985, MISMATCH 4, NOT_FOUND_A 1, NOT_FOUND_B 11
```

**Everything together: the bundled example.** The source brings a spec for these two tables,
`examples/tables/spec.yaml`. It has the filter of section 4 and the type maps of this section, renames
the e-mail and name columns, ignores case and spaces in e-mail addresses, masks the customer number,
and leaves `columns` out on side b.

3. Copy the examples into the app's folder.

```bash
docker compose cp examples/. app:/var/lib/deltasleuth/p0/examples
```

You should see:

```text
 deltasleuth-app-1 copy examples/. to deltasleuth-app-1:/var/lib/deltasleuth/p0/examples Copied
```

4. Run the bundled spec.

```bash
ds run /var/lib/deltasleuth/p0/examples/tables/spec.yaml
```

You should see:

```text
run R-35T75Z937SK9FF7V6BC20F21Y0 started
completed
  total 991, compared 991, match 985, different 6, indeterminate 0, cancelled 0, outstanding 0
  MATCH 985, MISMATCH 4, NOT_FOUND_A 1, NOT_FOUND_B 1
```

   After chapter 8, which ran this same spec on its changed tables, you see `paused
   (run_paused_schema_drift)` in place of `completed`, once. "If something goes wrong" below has the
   line and the request that accepts it; the run then ends with the counters above.

On the Run screen its scan line reads `Table scan (done): 989 rows on A, 990 on B; 984 identical; 7 to
compare; 0 not compared (NULL, empty or over-long key). Columns only on B: marketing_opt_in,
updated_at. Filter on A: "status" <> 'closed'.`

### How to read the result

Customer 100200 was created at one moment. The old system stores it as `2020-07-24 04:20:00`, the
clock in London in summer; the platform stores `2020-07-24 03:20:00+00`. With the two maps both sides
give `2020-07-24T03:20:00.000000Z`, and the scan counts the row as identical.

`as: timestamp` says the column is a moment in time. `naiveTimezone: Europe/London` says in which
zone a value without a zone was written. Other types are `string`, `integer`, `decimal`, `boolean`,
`date`, `json` and `bytes`; [TABLES.md](../mvp/TABLES.md), section 10, lists what each accepts.

In the bundled example the scan found 984 identical keys and 7 to compare, but the run says `match
985`. The seventh key is customer 100003, whose e-mail address differs only in case and a space. The
scan compares rows exactly, so it handed the key on; the read by key applied the spec's e-mail rule
and judged it `MATCH`. Its keys show as tokens such as `tok:identifier:v1:OAEBQPRKYGWM...`, because
that spec declares the customer number as personal data: `ds compare 100005 --spec
/var/lib/deltasleuth/p0/examples/tables/spec.yaml` prints the token to look for.

### Limits

- Map a converted column on both sides. A side without a map keeps its database's own form.
- A type map is for compared columns, named as that side spells them. Keys are the matter of
  section 6.
- No screen shows which columns a run converted. They are in the spec file and in the run's record
  under `params.tables`.

### If something goes wrong

**No key is identical, and one column differs in every row**
The column needs a type map, or has one on one side only. Run here without any map, the spec gave
`match 0` and `MISMATCH 989`, each with a finding such as A `2020-07-24T04:20:00`, B
`2020-07-24T03:20:00+00:00`. With the map on side a only, the same: A `2020-07-24T03:20:00.000000Z`, B
`2020-07-24T03:20:00+00:00`.

**About two thirds of the rows differ by one hour**
Side a's map lacks `naiveTimezone`, so its local times are read as UTC. Run here that way: `match
370`, `MISMATCH 619`, with A `2020-07-24T04:20:00.000000Z`, B `2020-07-24T03:20:00.000000Z`.

**`error: /sides/a/typeMap/created_at/naiveTimezone [invalid_type_map] 'Europe/Londn' is not an IANA
time zone`**
The zone's name is misspelt.

**`error: side b: typeMap column 'updated_at' is not compared`**, when the run is created
A type map names a column the pair does not compare.

**Every key `INCONCLUSIVE`, with `INCOMPLETE: unparseable` in the columns A and B of "Results"**
A value does not convert to the declared type. Provoked here by declaring `status` as `integer`:
`total 1001, compared 0`, `INCONCLUSIVE 1001`. The column "Why" read `recheck_failed`; under a spec
whose `recheck.outcomes` leaves `INCONCLUSIVE` out, as the bundled one, it reads `unparseable`.

**`paused (run_paused_schema_drift)`**, from step 4 after chapter 8, with these lines:

```text
run R-35T7ECWQNDX4WT1N3ZE4EKP66K started
paused (run_paused_schema_drift)
  Paused by the schema guard before any table row was read: column 'country' was text (text) in run R-35T7E7J3H1J5WHZTZ621C41WBH of this version and is character(2) (char) now. Resume to accept the tables as they are, or cancel.
  side b: column 'loyalty_tier', which this run does not read, is gone
error: the run is paused
```

The last run of this spec, in chapter 8, read the tables as that chapter had changed them. Section 1,
step 6, put the example data back, and the schema guard of
[chapter 8](08-prechecks-and-schedules.md) asks whether that is meant. The run exists and waits.
Accept the tables as they are: on the run's page press "Resume (accept the changes)", or send the
request that button sends. The token goes from its file through a pipe to curl.

```bash
{ printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - -X POST http://localhost:8000/v1/runs/<run id>/resume
```

You should see:

```text
{"run_id": "R-35T7ECWQNDX4WT1N3ZE4EKP66K", "status": "running"}
```

About fifteen seconds later `ds runs --limit 1` lists the run as `completed`, with `different 6 of
991`, and the next run of the spec is not paused. Provoked here by doing chapter 8's changes first.

## 8. Chosen keys, and one key

### What it is for

Not every question needs a scan. A handful of keys can be compared again after a fix, and a new spec
can be tried on one customer whose state is known. Such a run reads only the keys it is given.
Operators use it day to day.

### Before you start

Section 3 is done, and `practice/customers.yaml` is in the app's folder. Write a file of keys, one a
line.

```bash
printf '%s\n' 100001 100005 100011 101001 > practice/keys.txt
```

### In the web UI

1. On "Runs", in "New run", choose `practice-customers 1.0.0`, leave "Mode" at `batch`, type the four
   keys `100001`, `100005`, `100011` and `101001` into "Keys, one per line ...", one a line, and press
   "Start".
2. The run opens. It has no scan line. Within about ten seconds the progress reads `4/4 keys · done
   4`, and the badges `MATCH 1`, `MISMATCH 1`, `NOT_FOUND_A 1` and `NOT_FOUND_B 1`.
3. "Results" has four rows. Key `100001` reads Outcome `MATCH`, Confirmation `provisional`; the other
   three read `rechecked`.
4. To compare one key of a run again, click the key, for example `100005`, and press "Compare now" on
   its page. A panel "Compared now (run R-…)" appears above the old result, with the new outcome and a
   link to the small run that holds it.

### On the command line

1. Copy the folder `practice` again. It prints the `Copied` line of section 3, step 1.

```bash
docker compose cp practice/. app:/var/lib/deltasleuth/p0/practice
```

2. Run the spec for the keys of the file.

```bash
ds run /var/lib/deltasleuth/p0/practice/customers.yaml --keys /var/lib/deltasleuth/p0/practice/keys.txt
```

You should see, after about eight seconds:

```text
run R-35T763A17QJ9ESH4E9H4SSP41Z started
completed
  total 4, compared 4, match 1, different 3, indeterminate 0, cancelled 0, outstanding 0
  MATCH 1, MISMATCH 1, NOT_FOUND_A 1, NOT_FOUND_B 1
```

3. Compare one key now.

```bash
ds compare 100005 --spec /var/lib/deltasleuth/p0/practice/customers.yaml
```

You should see:

```text
100005: MISMATCH (DIFFERENT, rechecked)
  MEDIUM VALUE_MISMATCH value_changed /phone
      A: tok:free_text:v1:EOAH54HDCEF5IL7OQD6RSISXWH
      B: tok:free_text:v1:NZZK44UGCU647P5AOQUGMEOAMP
run R-35T763T62JJP9NCQG395PD5VJ3; observations a O-405939e7235de779 b O-c156ae6276d9a146
```

### How to read the result

Each key is read on both sides. A key that differs is read a second time after the spec's
`recheck.delay`, five seconds here, to see whether the difference is still there; then it says
`rechecked`. A key that agrees is read once and says `provisional`. That is why this run of four keys
took longer than the scan of a thousand rows, whose keys need no second pass.

`total 4` counts only the keys given. Nothing is said about any other row, and the census of such a
run reads `side a not covered; side b not covered`.

`ds compare` prints the outcome, each finding with its two values, and the id of a run of its own. In
the list of runs it shows with the mode `one_shot`, and so does every "Compare now".

### Limits

- The web UI compares a key again only from the page of a run that holds it. A key that is in no run
  is compared with `ds compare`.
- A run that does not scan has no scan line, so nothing on the Run screen names its tables or its
  filter.
- A key that the key column cannot hold is not looked up: `ds compare abc` under this spec answered
  `abc: INCONCLUSIVE (INDETERMINATE, provisional) reason skipped`.
- A key is matched by its text. `0100005` is not customer 100005: `ds compare 0100005` answered
  `INCONCLUSIVE (INDETERMINATE, rechecked) reason absent_both`.

### If something goes wrong

**`ds compare` ends with exit code 1**
You added `--fail-on-diff`, and the key is different: here `ds compare 100011 ... --fail-on-diff`
printed `100011: NOT_FOUND_B (DIFFERENT, rechecked)` and ended with 1. Without the option the code
is 0 whatever the outcome.

**A key you know is in the table answers `INCONCLUSIVE ... reason absent_both`**
It is written differently from the stored key, or the spec has a key map (section 6).

## 9. A table against a service

### What it is for

Sometimes the old data is in a database and the new system only offers a service, or the reverse. A
spec can pair a table with a service. Such a pair cannot be scanned, because a service has no "all
rows": it is compared for a list of keys, each read from the table and asked of the service.

### Before you start

Section 2 is done and the mock services run (`docker compose ps` lists `deltasleuth-mocks-1`).

Make the table: six customer profiles that mirror the mock customer service, with one status changed
and one customer the service does not know. The command prints nothing when it succeeds.

```bash
docker exec -i deltasleuth-practice-db psql -q -U postgres -d ds_demo_legacy -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE crm.profiles (customer_id text PRIMARY KEY, segment text NOT NULL, status text NOT NULL);
INSERT INTO crm.profiles VALUES
  ('C00012001', 'RETAIL', 'ACTIVE'),
  ('C00012002', 'RETAIL', 'ACTIVE'),
  ('C00012003', 'RETAIL', 'ACTIVE'),
  ('C00012004', 'RETAIL', 'ACTIVE'),
  ('C00012005', 'SME', 'ACTIVE'),
  ('C00099001', 'RETAIL', 'ACTIVE');
GRANT SELECT ON crm.profiles TO ds_reader;
SQL
```

Write the spec file. Side a is the table. Side b is version 2 of the mock customer service, with the
request that fetches one customer and a `map` that brings its answer into the table's shape: the two
fields `segment` and `status`.

```bash
cat > practice/profiles-vs-service.yaml <<'EOF'
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata:
  name: practice-profiles-vs-service
  version: "1.0.0"
  owner: practice
normalizerVersion: "1.1.0"
key:
  name: customer_id
  type: string
  pattern: "^C[0-9]{8}$"                       # required as soon as one side is a service
  source:                                      # the keys: such a pair never scans
    kind: list
    keys: [C00012001, C00012002, C00012003, C00012004, C00012005, C00012006, C00099001]
sides:
  a:                                           # the table side
    role: legacy
    target: crm-legacy
    table:
      schema: crm
      name: profiles
      keyColumns: [customer_id]
      columns: [segment, status]
  b:                                           # the service side: the mock customer service, version 2
    role: new
    target: customer-b
    request:
      method: GET
      path: "/v2/customers/{{ key }}"
      query:
        include: [products]
    select: "$.data"
    map: |
      {"segment": segment, "status": lifecycleStatus}
    notFound:
      status: [404]
    truncation:                                # a service side must say how a cut list would show
      countPath: "$.data.productCount"
      itemsPath: "$.data.products"
      cap: 100
compare: {}
recheck:
  delay: 5s
  times: 1
security:
  clear: ["$.segment", "$.status"]
EOF
```

[Chapter 4](04-specs.md) explains the lines of a service side.

### In the web UI

1. On "Specs", upload `practice/profiles-vs-service.yaml` and press "Save".
2. On "Runs", start a `batch` run of `practice-profiles-vs-service 1.0.0`. Leave the keys empty: the
   spec brings its own list.
3. The run has no scan line. The progress ends at `7/7 keys · done 7`, with the badges `MATCH 4`,
   `MISMATCH 1`, `NOT_FOUND_A 1` and `NOT_FOUND_B 1`.
4. "Findings" has three rows: Key `C00012004`, Pointer `/status`, A `ACTIVE`, B `SUSPENDED`; Key
   `C00012006`, Taxonomy `EXTRA_DOCUMENT`; Key `C00099001`, Taxonomy `MISSING_DOCUMENT`.
5. Nothing on the screen says that side a is a table.

### On the command line

1. Copy the folder `practice` again. It prints the `Copied` line of section 3, step 1.

```bash
docker compose cp practice/. app:/var/lib/deltasleuth/p0/practice
```

2. Check the spec.

```bash
ds spec validate /var/lib/deltasleuth/p0/practice/profiles-vs-service.yaml --targets /etc/deltasleuth/targets.yaml
```

You should see:

```text
ok: profiles-vs-service.yaml is valid (0 warning(s))
```

3. Run it.

```bash
ds run /var/lib/deltasleuth/p0/practice/profiles-vs-service.yaml
```

You should see:

```text
run R-35T768PJTS2GMJK9GKPQ97XFEG started
completed
  total 7, compared 7, match 4, different 3, indeterminate 0, cancelled 0, outstanding 0
  MATCH 4, MISMATCH 1, NOT_FOUND_A 1, NOT_FOUND_B 1
```

### How to read the result

| Key | Outcome | Why |
|---|---|---|
| C00012001, C00012002, C00012003, C00012005 | `MATCH` | The row and the service's answer agree |
| C00012004 | `MISMATCH` | The table says `ACTIVE`, the service `SUSPENDED` |
| C00012006 | `NOT_FOUND_A` | The service has the customer, the table has no row |
| C00099001 | `NOT_FOUND_B` | The table has the row, the service answers "not found" |

The run is one of chosen keys, as in section 8: keys that differ are read twice, and nothing is said
about rows of the table whose keys were not asked for. The run's record says so: its `scan` is `null`.

### Limits

- A table and a service never scan. The keys come from the spec's `key.source`, from `--keys`, or from
  the box "Keys, one per line".
- By [TABLES.md](../mvp/TABLES.md), a key of several columns (section 11 has one) is allowed between
  two tables only, and the service is read one key at a time, at the rate its target allows.
- The Run screen shows no table name, filter or column of such a run. They are in `coverage.csv`, in
  `report.pdf` and in the run's record under `params.tables`.

### If something goes wrong

**`error: /sides/b [truncation_undeclared] neither this side nor target 'customer-b' declares
truncation indicators (X33), so truncation would go undetected`**
The service side lacks the block `truncation`. A service that returns a list must say how a shortened
list would show, even when, as here, the list is not compared.

## 10. Repeat a comparison inside one run: a soak over a scan

### What it is for

While a migration is being repaired, the question is not only "what differs" but "what changed since
the last look". A soak is a run that repeats itself: over two tables it scans both again at a fixed
interval, each pass being one cycle, and lists the keys whose outcome changed from one cycle to the
next. Operators use it to watch two tables come together over an afternoon.

### Before you start

Section 3 is done. Write the spec file: the one of section 3 with another name, an interval, and an
end.

```bash
cat > practice/customers-soak.yaml <<'EOF'
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata:
  name: practice-customers-soak
  version: "1.0.0"
  owner: practice
normalizerVersion: "1.1.0"
key:
  name: customer_id
  type: string
sides:
  a:
    role: legacy
    target: crm-legacy
    table:
      schema: crm
      name: customers
      keyColumns: [customer_id]
      columns: [phone, status, country, balance]
  b:
    role: new
    target: platform-users
    table:
      name: users
      keyColumns: [user_id]
      columns: [phone, status, country, balance]
compare: {}
recheck:
  delay: 5s
  times: 1
execution:
  cycleInterval: 1m                            # a new scan every minute, counted from the run's start
  stop:
    maxCycles: 2                               # the soak ends by itself after its second scan
security:
  clear: ["$.status", "$.country", "$.balance"]
EOF
```

Have the command of step 3 below at hand: you repair one planted difference between the two scans, and
there is one minute for it.

### In the web UI

1. On "Specs", upload `practice/customers-soak.yaml` and press "Save".
2. On "Runs", in "New run", choose `practice-customers-soak 1.0.0`, set "Mode" to `soak`, leave the
   new box "resolve the keys again each cycle" unticked and the keys empty, and press "Start".
3. The run opens. Its first scan ends within seconds, and the grey line then reads `cycle 2`. Until
   the minute is over, the scan line reads `Table scan (running): 0 rows on A, 0 on B; 0 identical; 0
   to compare; ...` and the tables are empty: the screen shows the second cycle, which has not begun.
   To see the first, choose `1` under "Cycle" in the bar of filters.
4. Repair the difference now, with step 3 of the command line.
5. When the minute is over the second scan runs, and the badge turns `completed`. The grey line ends
   `· stopped: stop_condition:maxCycles`, and the outcome badges of cycle 2 read `MATCH 1`,
   `MISMATCH 3`, `NOT_FOUND_A 1` and `NOT_FOUND_B 11`.
6. The table "Outcome changes across cycles", which only a soak has, shows one row: Cycle `2`, Key
   `100009`, From `MISMATCH`, To `MATCH`.
7. The report buttons download the reports of the cycle chosen under "Cycle".

### On the command line

1. Copy the folder `practice` again. It prints the `Copied` line of section 3, step 1.

```bash
docker compose cp practice/. app:/var/lib/deltasleuth/p0/practice
```

2. Start the soak. `--soak` makes the run a soak, and `--no-wait` gives the terminal back at once: the
   app does the work.

```bash
ds run /var/lib/deltasleuth/p0/practice/customers-soak.yaml --soak --no-wait
```

You should see:

```text
run R-35T76AM07DYMCKHG673MFHAJXH started
processed by 'ds worker' or 'ds serve'
```

3. Within a minute, repair one planted difference: give customer 100009 the old system's balance on
   the platform. The administrator account does it, because the reader account cannot write.

```bash
docker exec deltasleuth-practice-db psql -U postgres -d ds_demo_platform -c "UPDATE public.users SET balance = 11.25 WHERE user_id = 100009"
```

You should see:

```text
UPDATE 1
```

4. Wait until a minute has passed since step 2, then list the newest run.

```bash
ds runs --limit 1
```

You should see the second cycle, completed, with one difference fewer:

```text
R-35T76AM07DYMCKHG673MFHAJXH  practice-customers-soak 1.0.0  soak  completed  cycle 2  different 15 of 1001
  Unapproved spec version: clear, population_filters were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
```

While the second cycle waits, the same command says `running  cycle 2  different 16 of 1001`.

5. Read the keys whose outcome changed.

```bash
{ printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - http://localhost:8000/v1/runs/<run id>/changes
```

You should see:

```text
{"items": [{"cycle": 2, "key_masked": "100009", "from_outcome": "MISMATCH", "to_outcome": "MATCH"}]}
```

6. Put the planted difference back, so that the other sections keep giving the numbers printed here.

```bash
docker exec deltasleuth-practice-db psql -U postgres -d ds_demo_platform -c "UPDATE public.users SET balance = 21.25 WHERE user_id = 100009"
```

You should see:

```text
UPDATE 1
```

### How to read the result

Each cycle is a whole scan with its own counters. The first found 16 keys to compare and judged 4 of
them `MISMATCH`. The second started one minute after the run's start, not after the first cycle's end.

A key that differed in the last cycle is read again in the next one even when its rows are now the
same. That is how the repair shows: customer 100009 was read again in cycle 2, judged `MATCH`, and
listed as a change from `MISMATCH` to `MATCH`. The second scan's own counters therefore still said
`985 identical; 16 to compare`, while the run's counters said `match 986`, `different 15`.

A key that was the same in the last cycle and differs now has no earlier result. It is a difference of
the new cycle, in its "Results", and is not listed as a change.

The run ended by itself after the second cycle, with the reason `stop_condition:maxCycles`.

### Limits

- A soak needs `execution.cycleInterval` in its spec, and it should have an end: `maxCycles`, `until`
  or `maxDuration` under `execution.stop`.
- Every cycle reads both tables in full. Choose an interval the databases can carry.
- By [TABLES.md](../mvp/TABLES.md), the option "resolve the keys again each cycle", `--rolling` on
  the command line, changes nothing for a scan. It was left off here.
- The Run screen's scan line is the newest cycle's. An older cycle's row counts are in its panel under
  "Census, adjudication and evidence packs", and in the reports of that cycle.
- A soak repeats at an interval counted from its start. To compare two tables at a time of day, for
  example every night at two, use a schedule: [chapter 8](08-prechecks-and-schedules.md).

### If something goes wrong

**`error: a soak run needs the spec's execution.cycleInterval`**, from `ds run ... --soak`; the same
words in red under "Start"
The spec has no interval. Provoked here by starting the spec of section 3 as a soak.

**"Outcome changes across cycles" says "None." after the second cycle**
The repair came after the second scan had started, or it changed another customer. Start the soak
again and repeat step 3 within the minute.

## 11. Graph parity

### What it is for

Some tables do not describe things but links between things: one row says "this company holds this
VAT number". When such a table is migrated, a link that is missing or added changes more than one row:
it can join two groups of companies that were separate, or separate a group that belonged together.
Graph parity compares the links and then says what the differing links do to those groups. Teams that
migrate entity-resolution or master data use it.

In plain words, with the names the spec and the reports use:

- A **link fact** is one row: an entity and one identifier it holds, for example company `ORG-1001` and
  the VAT number `GB900001001`. The whole row is the key.
- A **member** is an entity. A **group** is one identifier together with all entities that hold it.
- A **component** is a set of entities joined through identifiers they share, directly or through a
  chain of other entities.

### Before you start

Sections 1 and 2 are done. The example lives in `examples/graph`: two databases of 441 links each.

1. Load the example's data.

```bash
docker exec -i deltasleuth-practice-db psql -q -U postgres -v ON_ERROR_STOP=1 -f - < examples/graph/seed.sql
```

You should see two notices:

```text
psql:<stdin>:13: NOTICE:  database "ds_graph_legacy" does not exist, skipping
psql:<stdin>:14: NOTICE:  database "ds_graph_platform" does not exist, skipping
```

2. Let the reader account read the two tables. The command prints nothing when it succeeds.

```bash
docker exec -i deltasleuth-practice-db psql -q -U postgres -v ON_ERROR_STOP=1 <<'SQL'
GRANT CONNECT ON DATABASE ds_graph_legacy, ds_graph_platform TO ds_reader;
\connect ds_graph_legacy
GRANT USAGE ON SCHEMA crm TO ds_reader;
GRANT SELECT ON crm.entity_identifiers TO ds_reader;
\connect ds_graph_platform
GRANT SELECT ON public.links_export TO ds_reader;
SQL
```

3. Write the DSN of the old system's links. The command prints nothing.

```bash
(umask 077; { printf 'postgresql://ds_reader:'; tr -d '\n' < deploy/secrets/practice_reader_password; printf '@deltasleuth-practice-db:5432/ds_graph_legacy'; } > deploy/secrets/crm_links_dsn)
```

4. Write the DSN of the platform's links. The command prints nothing.

```bash
(umask 077; { printf 'postgresql://ds_reader:'; tr -d '\n' < deploy/secrets/practice_reader_password; printf '@deltasleuth-practice-db:5432/ds_graph_platform'; } > deploy/secrets/platform_links_dsn)
```

5. Let the app's container read the two files. The command prints nothing.

```bash
chmod 444 deploy/secrets/crm_links_dsn deploy/secrets/platform_links_dsn
```

6. In `deploy/docker-compose.site.yml`, add the two names to the line of section 2, step 10:

```yaml
    secrets: [crm_legacy_dsn, platform_users_dsn, crm_links_dsn, platform_links_dsn]
```

   and add two lines at the end of the file, under the lines added there:

```yaml
  crm_links_dsn: {file: ./secrets/crm_links_dsn}
  platform_links_dsn: {file: ./secrets/platform_links_dsn}
```

7. Apply the change.

```bash
docker compose --profile mocks up -d
```

You should see the app made anew, as in section 2, step 12.

8. Check one of the two targets. The targets file as chapter 1 copied it declares both, `crm-links`
   and `platform-links`.

```bash
ds target check crm-links --table crm.entity_identifiers
```

You should see:

```text
target crm-links: postgresql 16.14, character set UTF8, TLS no, session ok
crm.entity_identifiers (table, consistency snapshot)
  entity_code  text  text  key
  id_type      text  text  key
  id_value     text  text  key
```

9. Copy the examples into the app's folder, if section 7 has not done it.

```bash
docker compose cp examples/. app:/var/lib/deltasleuth/p0/examples
```

You should see:

```text
 deltasleuth-app-1 copy examples/. to deltasleuth-app-1:/var/lib/deltasleuth/p0/examples Copied
```

The spec is `examples/graph/spec.yaml`. Three parts of it make the run a graph run. The key has three
parts, one for each key column, so a link is written `ORG-1002|vat|GB900001001`:

```yaml
key:
  name: link
  type: composite                              # one key part per key column, on both sides
  parts: [entity_code, id_type, id_value]
  class: business_code                         # company codes and numbers, not personal data: shown clear
```

Each side lists its three key columns, and the one compared column is a key column again, which means
"compare which links exist":

```yaml
    table:
      schema: crm
      name: entity_identifiers
      keyColumns: [entity_code, id_type, id_value]
      columns: [entity_code]                   # a key column listed alone: compare which facts exist
```

And a block `graph` says which key parts name the entity and which the identifier:

```yaml
graph:
  members: [entity_code]                       # the key parts naming an entity
  groups: [id_type, id_value]                  # the key parts naming an identifier
  maxComponent: 20                             # small for the demo; the default is 10,000
```

### In the web UI

1. On "Specs", upload `examples/graph/spec.yaml` and press "Save".
2. On "Runs", start a `batch` run of `entity-links-crm-vs-platform 1.0.0` with no keys.
3. The scan line reads `Table scan (done): 441 rows on A, 441 on B; 437 identical; 8 to compare; 0 not
   compared (NULL, empty or over-long key).`
4. Under it a second line appears: `Graph: 409 groups on A, 409 on B; 6 differ; 1 split, 1 merge, 1
   rearranged, 2 too large to expand.`
5. The badges read `NOT_FOUND_A 4` and `NOT_FOUND_B 4`, and "Results" lists the eight links, for
   example Key `ORG-1002|vat|GB900001001`, Outcome `NOT_FOUND_A`.
6. Press the button `graph.csv`. The browser saves `<run id>-graph.csv`, the eleven rows shown below.
   The screen itself does not list them; `report.pdf` does, in its section "Graph parity".

### On the command line

1. Run the spec.

```bash
ds run /var/lib/deltasleuth/p0/examples/graph/spec.yaml
```

You should see:

```text
run R-35T76H0EXBV2Q8S165XN5ZENCE started
completed
  total 445, compared 445, match 437, different 8, indeterminate 0, cancelled 0, outstanding 0
  MATCH 437, NOT_FOUND_A 4, NOT_FOUND_B 4
```

2. Read the structure's counters.

```bash
{ printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - http://localhost:8000/v1/runs/<run id> | grep -o '"graph": {[^}]*}'
```

You should see:

```text
"graph": {"merge": 1, "split": 1, "groups_a": 409, "groups_b": 409, "oversized": 2, "rearrange": 1, "groups_equal": 403, "groups_different": 6}
```

3. Write the structure report into the app's folder.

```bash
ds report <run id> --out /var/lib/deltasleuth/p0/reports --kind graph.csv --kind report.pdf
```

You should see the two files' names:

```text
/var/lib/deltasleuth/p0/reports/R-35T76H0EXBV2Q8S165XN5ZENCE-graph.csv
/var/lib/deltasleuth/p0/reports/R-35T76H0EXBV2Q8S165XN5ZENCE-report.pdf
```

4. Copy the reports to your machine.

```bash
docker compose cp app:/var/lib/deltasleuth/p0/reports/. p0-out
```

You should see:

```text
 deltasleuth-app-1 copy deltasleuth-app-1:/var/lib/deltasleuth/p0/reports/. to p0-out Copied
```

5. Print the structure report.

```bash
cat p0-out/<run id>-graph.csv
```

You should see:

```text
seq,code,label,count_a,count_b,only_a,only_b,detail
0,GROUP_MEMBERS_DIFFER,duns|900002001,2,1,1,0,"{""only_a"":[""ORG-2002""],""only_b"":[]}"
1,GROUP_MEMBERS_DIFFER,duns|900003001,2,2,1,1,"{""only_a"":[""ORG-3002""],""only_b"":[""ORG-3003""]}"
2,GROUP_MEMBERS_DIFFER,duns|900003003,2,2,1,1,"{""only_a"":[""ORG-3003""],""only_b"":[""ORG-3002""]}"
3,GROUP_MEMBERS_DIFFER,lei|LEI00000000000004001,1,2,0,1,"{""only_a"":[],""only_b"":[""ORG-4031""]}"
4,GROUP_MEMBERS_DIFFER,vat|GB000000000,30,29,0,0,"{""oversized"":true}"
5,GROUP_MEMBERS_DIFFER,vat|GB900001001,1,2,0,1,"{""only_a"":[],""only_b"":[""ORG-1002""]}"
6,CLUSTER_OVERSIZED,ORG-4001,1,0,0,0,"{""side"":""a""}"
7,CLUSTER_OVERSIZED,ORG-4001,0,2,0,0,"{""side"":""b""}"
8,CLUSTER_MERGE,ORG-1001,2,1,0,0,"{""members"":2,""sample"":[""ORG-1001"",""ORG-1002""]}"
9,CLUSTER_SPLIT,ORG-2001,1,2,0,0,"{""members"":2,""sample"":[""ORG-2001"",""ORG-2002""]}"
10,CLUSTER_REARRANGE,ORG-3001,2,2,0,0,"{""members"":4,""sample"":[""ORG-3001"",""ORG-3002"",""ORG-3003"",""ORG-3004""]}"
```

### How to read the result

The result has two layers.

**The links.** The scan compared the 441 links of each side as rows: 437 are on both sides, and eight
are on one side only. Those eight are the run's results and findings, like the missing rows of
section 3: a link only on the platform is `NOT_FOUND_A`, a link only in the old system `NOT_FOUND_B`.

**The structure.** It adds no result and changes no counter. It explains the eight links. A row of
`graph.csv` has a code, a label and counts:

| Code | The label is | `count_a`, `count_b` | Meaning |
|---|---|---|---|
| `GROUP_MEMBERS_DIFFER` | An identifier, such as `vat\|GB900001001` | The entities that hold it on each side | The identifier links other entities on the two sides. `only_a` and `only_b` count the entities on one side only, and `detail` names up to five of them. |
| `CLUSTER_MERGE` | One entity of the component | Components on each side | Several components of side a are one on side b. |
| `CLUSTER_SPLIT` | One entity of the component | Components on each side | One component of side a is several on side b. |
| `CLUSTER_REARRANGE` | One entity of the component | Components on each side | Both at once: entities moved between components. |
| `CLUSTER_OVERSIZED` | The component's first entity | Entities reached on the side `detail` names | The component is too large to work out, and is reported by size only. |

The example plants four stories, and the rows tell them:

- **A merge.** The platform also links `ORG-1002` to the VAT number of `ORG-1001`. Row 5: that number
  has one entity on side a and two on side b. Row 8: two components of side a are one on side b.
- **A split.** The platform no longer links `ORG-2002` to the DUNS number it shared with `ORG-2001`.
  Row 0: two entities on side a, one on side b. Row 9: one component of side a is two on side b.
- **A rearrangement.** Two DUNS numbers swapped `ORG-3002` and `ORG-3003`. Rows 1 and 2: each number
  lost one entity and gained another. Row 10: two components on each side, with their four entities
  grouped differently.
- **An identifier that is too common.** Thirty entities share the placeholder VAT number
  `GB000000000`, 29 on the platform. That is more than `maxComponent: 20` allows, so row 4 compares it
  by size only, and rows 6 and 7 say that the component around `ORG-4001` reaches it and was not worked
  out. Row 3 is a real difference near it: the platform also links `ORG-4031` to the LEI number of
  `ORG-4001`.

The line on the Run screen sums this up: 409 identifiers on each side, 6 of them with other entities,
one split, one merge, one rearrangement, and two components too large to expand. `groups_equal 403`,
the identifiers that link the same entities on both sides, is in the counters of step 2 and in the PDF
but not on the screen.

### Limits

- By [GRAPH.md](../mvp/GRAPH.md), both sides must be PostgreSQL tables or views. A MySQL, Oracle or
  file side is refused: load the links into PostgreSQL first.
- The structure is worked out only when a scan reaches its end. A scan cut by `maxKeys` or a time
  limit (section 5) has no "Graph" line, and its `graph.csv` holds the header line only: so it was
  here for the example with `maxKeys: 3`, which ended `completed (stop_condition:maxKeys)`.
- The rows of the structure are in `graph.csv`, in `report.pdf` and in the API
  (`GET /v1/runs/<run id>/graph`), not on a screen.
- An identifier that links more entities than `maxComponent` is compared by size only. Clean such
  placeholders, or leave them out on both sides with a row filter (section 4).
- Labels are masked like keys. This example shows them as they are, because its key is declared a
  `business_code`.

### If something goes wrong

**`error: /graph [invalid_graph] members and groups must split key.parts between them`**
Every part of the key must be named once, in `members` or in `groups`. Provoked here by leaving
`id_value` out of `groups`.

**`error: cannot read secret file /run/secrets/crm_links_dsn: No such file or directory`**
Steps 6 and 7 of "Before you start" are missing.

**Many rows `CLUSTER_OVERSIZED`**
One identifier links very many entities. See the fourth story above.

## 12. Tidy up

The practice database is a container of its own. `docker compose stop` and `docker compose down` do
not touch it. Steps 1 and 2 pause it, step 3 removes it, and steps 4 to 8 take its secrets out of the
installation again. If [chapter 8](08-prechecks-and-schedules.md) is still to be practised, do steps
1 and 2 only: that chapter uses the same container and the same two DSN files.

1. Stop it, when you do not need it for a while. Its data stays.

```bash
docker stop deltasleuth-practice-db
```

2. Start it again. Do this also after the machine restarted.

```bash
docker start deltasleuth-practice-db
```

Both print the container's name:

```text
deltasleuth-practice-db
```

3. Remove it for good, with its data. This cannot be undone. `-v` also deletes the storage Docker
   made for it.

```bash
docker rm -f -v deltasleuth-practice-db
```

You should see:

```text
deltasleuth-practice-db
```

Now the secrets, in this order: the site file first, the files last.

4. Put the site file back as it was before section 2, step 10, from the copy that step 9 made. The
   command prints nothing.

```bash
cp practice/site.yml.before-tables deploy/docker-compose.site.yml
```

   If you changed the site file for another chapter since then, the copy lacks that change. Then
   open `deploy/docker-compose.site.yml` in a text editor instead and take out what sections 2 and
   11 added: their names in the `secrets:` line under `app:` (with none left the line reads
   `secrets: []`), and their lines at the end of the file, together with the line `secrets:` above
   them when nothing is left under it. Done here both ways, with the same file as result.

   Only if you did chapter 8: that chapter named the two DSN files a second time, in `.env`. Open
   `.env` in a text editor and delete `:examples/tables/compose.yaml` from the end of the line that
   starts `COMPOSE_FILE=`. With that left in, step 5 prints `5` and step 7 breaks every `docker
   compose` command, as shown below.

5. Ask Compose whether the secrets are gone from its description.

```bash
docker compose config | grep -c crm_legacy_dsn
```

You should see:

```text
0
```

6. Apply the change.

```bash
docker compose --profile mocks up -d
```

You should see the app made anew, as in section 2, step 12.

7. Delete the six secret files of this chapter. `-f` is needed because they are read-only, and it
   passes over a file that section 11 never made. The command prints nothing.

```bash
rm -f deploy/secrets/practice_db_password deploy/secrets/practice_reader_password deploy/secrets/crm_legacy_dsn deploy/secrets/platform_users_dsn deploy/secrets/crm_links_dsn deploy/secrets/platform_links_dsn
```

8. Check that `ds` still works.

```bash
ds token list
```

You should see the line of chapter 1, section 5, which begins with the token's id, such as
`T-f66c5874ffb089ed (first-run)`.

Step 7 must come after step 6. With a DSN file gone while the site file still named it, every `ds`
command here stopped with these two lines, before it did anything:

```text
time="2026-10-05T20:29:38Z" level=warning msg="secret file deltasleuth_crm_links_dsn does not exist"
Error response from daemon: invalid mount config for type "bind": bind source path does not exist: /host_mnt<folder>/IntelliDiff/deploy/secrets/crm_links_dsn
```

`<folder>` is where your `IntelliDiff` folder lies; Docker Desktop on macOS puts `/host_mnt` before
it. Put back, the file was read again by the next command. The same two lines came for
`crm_legacy_dsn` when that file was gone while chapter 8's line in `.env` still named
`examples/tables/compose.yaml`.

**What stays, on purpose.** The folder `practice` with your spec files and the copy of the site file,
and the folder `p0-out` with the reports; their copies in the app's folder `/var/lib/deltasleuth/p0`;
the stored specs and the runs, which stay listed on the "Specs" and "Runs" screens; and the four
practice targets in the targets file, which came with chapter 1. A check of one of them now answers
`error: cannot read secret file /run/secrets/crm_legacy_dsn: No such file or directory`.

**Before you remove the installation** (chapter 1, section 8), do step 3. The practice database
holds on to the installation's network. Run here with the practice database still there, `docker
compose --profile mocks down -v` removed the three containers and the four volumes with the data,
and printed among its lines:

```text
 Network deltasleuth_default  Resource is still in use
```

After step 3, the same command removed what was left:

```text
 Network deltasleuth_default  Removing
 Network deltasleuth_default  Removed
```

**To start the practice data over**, do step 3, then section 1, steps 4 to 6, and section 2, steps 2
to 5. The password files, the DSN files and the site file stay as they are; do not run section 2,
step 1, again, because a new password would no longer match the DSN files. Done here: the check of
section 2, step 14, answered `session ok`, and the run of section 3 ended `completed` with the same
counters. The view of section 6, the table of section 9 and the two databases of section 11 are gone
with the old container. If you need them, repeat the commands under "Before you start" of sections 6
and 9, and steps 1 and 2 of section 11's.

## Reference

- [TABLES.md](../mvp/TABLES.md): the contract of table comparison: targets, the spec, the scan, row
  filters, key maps, type maps, repeated scans and reads in groups.
- [GRAPH.md](../mvp/GRAPH.md): the contract of graph parity.
- [RUNBOOK.md](../mvp/RUNBOOK.md): "Compare two tables" and "Graph parity", with every message.
- [DEPLOY.md](../mvp/DEPLOY.md): section 7, a database target, its secret, the read-only account, and
  what a scan costs a database.
- [APPROVALS.md](../mvp/APPROVALS.md): which parts of a spec need a second person with single sign-on.
- [ENGINES.md](../mvp/ENGINES.md) and [FILES.md](../mvp/FILES.md): MySQL, Oracle and file sides, the
  subject of [chapter 7](07-engines-and-files.md).
- [PRECHECKS.md](../mvp/PRECHECKS.md) and [SCHEDULES.md](../mvp/SCHEDULES.md): table pre-checks, the
  schema guard and schedules, the subject of [chapter 8](08-prechecks-and-schedules.md).
- `examples/tables/README.md` and `examples/graph/README.md` in the source: the two bundled examples.
