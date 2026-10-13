# 7. MySQL, Oracle and files

So far a side of a comparison was a REST service or a PostgreSQL table. This chapter adds three more
kinds of side: a MySQL table, an Oracle table, and a folder of CSV or Parquet files that an export job
wrote. Any of them can be compared with any other, with a PostgreSQL table or with a service.

The source brings a practice set for each of the three, called an acceptance kit: data with planted
differences, and a file that lists the result to expect. They are in `examples/tables/mysql`,
`examples/tables/oracle` and `examples/tables/files`. This chapter starts a practice MySQL server and a
practice Oracle server in Docker, loads the kits, and compares them with each other and with a
PostgreSQL table. Nothing here touches a real system.

**How this chapter was checked.** Every command was run on 2026-10-05, on macOS
with Docker Desktop, against MySQL 8.4.11, Oracle 23.26.3 and DuckDB 1.5.5. Run ids, spec numbers, times
and masked values will differ on your machine; counts, outcomes, keys and file hashes will not. Not
verified here: the pages in a browser (their steps are written from the pages' source), and Linux.

**Words used here.** A *target* is one entry of the targets file `deploy/config/targets.yaml`: a system
DeltaSleuth is allowed to read. A *spec* is the YAML file that describes one comparison; its two
*sides* are called `a` and `b`, and each names a target. The *key* is the column whose value says which
row of side a belongs to which row of side b. The *site file* is `deploy/docker-compose.site.yml`, this
installation's own changes to the Docker description. A *secret* is a password or a key: it lives in a
file of its own in `deploy/secrets/`, and no command of this chapter shows one.

**Conventions.**

- Run every command from the installation's folder, `IntelliDiff`. Commands of several lines, and
  lines to type into a file, stand at the left edge of the page so that they can be copied whole.
- The outputs leave out Docker's own progress lines, which differ with the terminal, and the lines
  that start with `{"id":`, copies of audit records as in chapter 1. A command without a "You should
  see" block prints nothing when it succeeds, or only such lines.
- `<run id>` stands for the id a run printed in its line `run R-... started`.
- "The engines document", "the files document", "the reader document", "the deployment document"
  and "the runbook" are the product's own descriptions, listed under Reference at the end. What is
  taken from one of them was not tried here unless the text says so.
- **In a second installation** (chapter 1), change three names, which Docker Compose does not fill
  in: the network `deltasleuth_default` and the containers `deltasleuth-kit-mysql` and
  `deltasleuth-kit-oracle`. Write your project name in place of `deltasleuth` wherever they stand:
  in the `docker` commands of sections 2, 3, 4 and 6, and in the `host:` lines of the two target
  entries. With the project `ds2` they read `ds2_default`, `ds2-kit-mysql` and `ds2-kit-oracle`.
  Everything else works unchanged. This chapter's own run was such an installation, and its outputs
  are printed with the default names.

## 1. Prepare

This section is not a feature. Steps 1 to 4 are needed by every later section, also when you do only
one of them. Steps 5 to 14 make a PostgreSQL table to compare with, and are needed only for the
comparisons with PostgreSQL: section 2 from step 4 of "On the command line", section 3 from step 5,
section 4, and section 5 from step 6.

1. Open a terminal in the installation's folder, `IntelliDiff`, and define the short form `ds`, as in
   [chapter 1](01-install-docker.md), section 5. It lasts until the terminal closes.
   ```bash
   alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
   ```
2. Ask which installation this folder acts on.
   ```bash
   docker compose config | grep '^name:'
   ```
   You should see the Compose project's name:
   ```text
   name: deltasleuth
   ```
   Another name means a second installation: use it as "In a second installation" above says.
3. Make a folder for the files you write in this chapter. Git lists it as untracked, which is harmless.
   ```bash
   mkdir -p practice
   ```
4. Keep a copy of the two files this chapter changes, so that section 6 can put them back. Each
   command copies only when there is no copy yet, so a second run cannot replace the copy by a file
   that already holds this chapter's changes.
   ```bash
   [ -e practice/targets.yaml.before ] || cp deploy/config/targets.yaml practice/targets.yaml.before
   ```
   ```bash
   [ -e practice/site.yml.before ] || cp deploy/docker-compose.site.yml practice/site.yml.before
   ```

The remaining steps make the PostgreSQL side: a database `ds_kit` on the installation's own database
server, with a copy of the kits' accounts in it. A real installation never keeps compared data beside
its own; a practice one may.

5. Create the database and its two tables. `accounts` holds the same 1,010 accounts as the first
   table of the MySQL kit and of the Oracle kit. `file_accounts` holds the 1,000 accounts of the files
   kit, with one balance changed and one account removed. The command prints nothing when it succeeds.

```bash
docker compose exec -T db psql -q -U deltasleuth -d deltasleuth -v ON_ERROR_STOP=1 <<'SQL'
CREATE DATABASE ds_kit;
\connect ds_kit
CREATE TABLE accounts (
    code    text,
    email   text,
    status  character(8),
    balance numeric(12, 2),
    visits  bigint,
    flag    boolean,
    born    date,
    opened  timestamp(6),
    seen    timestamptz(3),
    note    text
);
INSERT INTO accounts
SELECT 'K' || lpad(n::text, 4, '0'), 'customer' || n || '@example.com',
       (ARRAY['active', 'dormant', 'closed'])[1 + n % 3], n * 1.25, n * 1000, n % 2 = 1,
       DATE '1970-01-01' + n, TIMESTAMP '2020-01-01 00:00:00' + n * INTERVAL '1.000001 second',
       TIMESTAMPTZ '2021-06-01 12:00:00.250+00' + n * INTERVAL '3601 second', repeat('note ', n % 20)
FROM generate_series(1, 1000) AS n;
INSERT INTO accounts (code, email, balance) VALUES
    ('B', 'order', 1), ('Z', 'order', 2), ('a', 'order', 3), ('a ', 'order', 4), ('e', 'order', 5),
    (U&'\00E9', 'order', 6), (U&'\+01F600', 'order', 7), ('E', 'order', 8), ('x1', 'order', 9),
    ('X1', 'order', 10);
CREATE TABLE file_accounts AS
SELECT n AS account_id,
       CASE WHEN n % 100 = 0 THEN 'Holder ' || n || '; Ltd' ELSE 'Holder ' || n END AS holder,
       (n * 1.25)::numeric(12, 2) AS balance, DATE '2020-01-01' + n AS opened, 'A'::text AS status
FROM generate_series(1, 1000) AS n;
UPDATE file_accounts SET balance = balance + 1 WHERE account_id = 7;
DELETE FROM file_accounts WHERE account_id = 9;
SQL
```

6. Create a password for the account DeltaSleuth will read with. It goes straight into a file.
   ```bash
   (umask 077; openssl rand -hex 24 > deploy/secrets/kit_pg_password)
   ```
7. Create that account, `ds_kit_reader`. The password goes from its file through a pipe into the
   database, so it appears neither on the screen nor in a list of running programs.
   ```bash
   { printf "CREATE ROLE ds_kit_reader LOGIN PASSWORD '"; tr -d '\n' < deploy/secrets/kit_pg_password; printf "';\n"; } | docker compose exec -T db psql -q -U deltasleuth -d deltasleuth -v ON_ERROR_STOP=1
   ```
8. Let the account read the two tables, and nothing else.
   ```bash
   docker compose exec -T db psql -q -U deltasleuth -d ds_kit -v ON_ERROR_STOP=1 -c 'GRANT SELECT ON accounts, file_accounts TO ds_kit_reader'
   ```
9. Write the address DeltaSleuth connects with, from the password's file into a second file.
   ```bash
   (umask 077; { printf 'postgresql://ds_kit_reader:'; tr -d '\n' < deploy/secrets/kit_pg_password; printf '@db:5432/ds_kit'; } > deploy/secrets/kit_pg_dsn)
   ```
10. Let the app's container read that file.
    ```bash
    chmod 444 deploy/secrets/kit_pg_dsn
    ```
11. Tell Docker to show the file to the app. Open `deploy/docker-compose.site.yml` in a text editor
    and make two changes. First, under `app:`, find the line that starts with `    secrets:`. It
    holds a list between square brackets, empty so far. Write the secret's name into the brackets,
    and leave the rest of the line as it is. The line then starts like this, with four spaces:

```text
    secrets: [kit_pg_dsn]
```

Second, add these two lines at the end of the file. The first starts at the left edge, the second
with two spaces:

```text
secrets:
  kit_pg_dsn: {file: ./secrets/kit_pg_dsn}
```

Sections 2 and 3 add more secrets the same way: a name in the brackets, after a comma, and one more
line at the end of the file. The line `secrets:` at the left edge is written once only. If your file
already has one that does not start with `#`, add only the indented line under it: Docker refuses a
file with two (`mapping key "secrets" already defined`).

12. Declare the target. The command adds an empty line and four lines to the end of the targets file.
    The two spaces before `kit-postgres` matter: they put the entry under the file's `targets:` line.

```bash
cat >> deploy/config/targets.yaml <<'YAML'

  kit-postgres:
    dsn_ref: file:/run/secrets/kit_pg_dsn
    rate_per_s: 50
    max_concurrency: 2
YAML
```

13. Check the target before the app reads the file. `ds target check` connects as a run would, reads
    no row, and with `--table` lists the columns.
    ```bash
    ds target check kit-postgres --table accounts
    ```
    You should see (the version is your database image's):
    ```text
    target kit-postgres: postgresql 16.14, character set UTF8, TLS no, session ok
    public.accounts (table, consistency snapshot)
      code     text                            text       key
      email    text                            text       key
      status   character(8)                    char       key
      balance  numeric(12,2)                   decimal    key
      visits   bigint                          integer    key
      flag     boolean                         boolean
      born     date                            date       key
      opened   timestamp(6) without time zone  timestamp
      seen     timestamp(3) with time zone     instant
      note     text                            text       key
    ```
14. Apply the two files. A change to the site file needs this command, not a restart
    ([appendix](A1-install-by-hand.md), section 5.2). The app is made anew and reads the targets file.
    ```bash
    docker compose --profile mocks up -d
    ```
    Among Docker's progress lines you should see that the app's container was made anew:
    ```text
     Container deltasleuth-app-1  Recreated
     Container deltasleuth-app-1  Started
    ```

### If something goes wrong

**`ERROR:  database "ds_kit" already exists`**, from step 5, or **`ERROR:  role "ds_kit_reader" already
exists`**, from step 7
The step ran before in this installation. Leave it at that if the earlier pass went through. To
start over, run step 4 of section 6, which removes the database and the account, delete the two
files with the next command, and go on from step 5.
```bash
rm -f deploy/secrets/kit_pg_password deploy/secrets/kit_pg_dsn
```

**`permission denied: deploy/secrets/kit_pg_dsn`** (zsh; bash says `deploy/secrets/kit_pg_dsn:
Permission denied`), from step 9
The file is there from an earlier pass, and step 10 made it read-only. The step is done; to start
over, see the entry above.

## 2. A MySQL table as one side

### What it is for

Many legacy systems keep their data in MySQL while the new system uses another database or offers
only an API. With a MySQL target a side of a comparison is a MySQL table or view, read where it is,
so no export and no copy is needed. A database administrator declares the target and creates the
account; whoever writes specs then names the target like any other.

### Before you start

You need a MySQL server, version 8.0.17 or later, an account on it that may read the compared tables,
and a target entry. The steps below make all three for practice. The practice server took about 450
MiB of memory here.

Do steps 1 to 4 of section 1 first: every step below needs them. Steps 5 to 14 of section 1 are
needed only for the comparison with PostgreSQL, from step 4 of "On the command line".

1. Fetch the image, 812 MB as Docker lists it. Not verified here: this command, because the image
   was already on the machine.
   ```bash
   docker pull mysql:8.4
   ```
2. Create the practice server's administrator password, in a file.
   ```bash
   (umask 077; openssl rand -hex 24 > deploy/secrets/kit_mysql_root)
   ```
3. Write a small options file from it, which the `mysql` program inside the container will read. This
   keeps the password off every command line.
   ```bash
   (umask 077; { printf '[client]\nuser=root\npassword='; cat deploy/secrets/kit_mysql_root; } > deploy/secrets/kit_mysql_admin.cnf)
   ```
4. Start the server, in the installation's Docker network so that the app can reach it by its name.
   Nothing is published on the machine's own ports. In a second installation, write your project
   name in `deltasleuth-kit-mysql` and `deltasleuth_default`, here and in every later step.
   ```bash
   docker run -d --name deltasleuth-kit-mysql --network deltasleuth_default -e MYSQL_ROOT_PASSWORD_FILE=/run/secrets/kit_mysql_root -v "$PWD/deploy/secrets/kit_mysql_root:/run/secrets/kit_mysql_root:ro" -v "$PWD/deploy/secrets/kit_mysql_admin.cnf:/etc/kit_mysql_admin.cnf:ro" mysql:8.4
   ```
   You should see one line, the new container's id, and nothing after it.
5. Wait about fifteen seconds, then check that the server is ready. It starts twice, the first time
   to set itself up; only the second start listens on port 3306.
   ```bash
   docker logs deltasleuth-kit-mysql 2>&1 | grep "port: 3306 "
   ```
   You should see one line. If you see none, wait and ask again.
   ```text
   2026-10-05T20:24:41.641043Z 0 [System] [MY-010931] [Server] /usr/sbin/mysqld: ready for connections. Version: '8.4.11'  socket: '/var/run/mysqld/mysqld.sock'  port: 3306  MySQL Community Server - GPL.
   ```
6. Load the kit: the database `ds_kit` with the tables `accounts_a` and `accounts_b`. The command
   prints nothing when it succeeds.
   ```bash
   docker exec -i deltasleuth-kit-mysql mysql --defaults-extra-file=/etc/kit_mysql_admin.cnf --default-character-set=utf8mb4 < examples/tables/mysql/seed.sql
   ```
7. Create the password of the account DeltaSleuth reads with.
   ```bash
   (umask 077; openssl rand -hex 24 > deploy/secrets/kit_mysql_password)
   ```
8. Create that account, `ds_reader`, and grant it `SELECT` on the kit's database. That is all a
   MySQL reader account needs: `SELECT` on each table or view a spec names.
   ```bash
   { printf "CREATE USER 'ds_reader'@'%%' IDENTIFIED BY '"; tr -d '\n' < deploy/secrets/kit_mysql_password; printf "';\nGRANT SELECT ON ds_kit.* TO 'ds_reader'@'%%';\n"; } | docker exec -i deltasleuth-kit-mysql mysql --defaults-extra-file=/etc/kit_mysql_admin.cnf
   ```
9. Copy the certificate of the authority that signed the server's TLS certificate. A MySQL server
   makes both for itself at its first start. A certificate is not a secret, but it is kept with them.
   ```bash
   docker cp deltasleuth-kit-mysql:/var/lib/mysql/ca.pem deploy/secrets/kit_mysql_ca
   ```
10. Let the app's container read the two files it needs.
    ```bash
    chmod 444 deploy/secrets/kit_mysql_password deploy/secrets/kit_mysql_ca
    ```
11. Show the two files to the app. In `deploy/docker-compose.site.yml`, write the two names into
    the brackets of the `secrets:` line under `app:`, and add one line for each at the end of the
    file, under the line `secrets:` at the left edge. If you did all of section 1, the two places
    then read:

```text
    secrets: [kit_pg_dsn, kit_mysql_password, kit_mysql_ca]
```

```text
secrets:
  kit_pg_dsn: {file: ./secrets/kit_pg_dsn}
  kit_mysql_password: {file: ./secrets/kit_mysql_password}
  kit_mysql_ca: {file: ./secrets/kit_mysql_ca}
```

If you did only steps 1 to 4 of section 1, your file has no `kit_pg_dsn` in either place and no
line `secrets:` at the left edge yet: write that line at the end of the file first, then the two
lines under it.

12. Declare the target. In a second installation, write your container's name in the `host:` line.

```bash
cat >> deploy/config/targets.yaml <<'YAML'

  kit-mysql:
    engine: mysql
    host: deltasleuth-kit-mysql
    port: 3306
    database: ds_kit
    user: ds_reader
    password_ref: file:/run/secrets/kit_mysql_password
    tls_ca_ref: file:/run/secrets/kit_mysql_ca
    tls_verify_name: false
    allow_private_network: true
    rate_per_s: 50
    max_concurrency: 2
YAML
```

13. Check the target.
    ```bash
    ds target check kit-mysql
    ```
    You should see:
    ```text
    target kit-mysql: mysql 8.4.11, character set utf8mb4, TLS yes, session ok
    ```
14. Apply the two files.
    ```bash
    docker compose --profile mocks up -d
    ```

What the entry's lines mean:

| Line | Meaning |
|---|---|
| `engine: mysql` | The kind of database. A PostgreSQL target has `dsn_ref` instead |
| `host`, `port` | The server's name or address, and its port (3306 when left out) |
| `database` | The MySQL database. A spec's tables are looked for in it |
| `user`, `password_ref` | The reader account, and the file that holds its password and nothing else |
| `tls_ca_ref` | The authority's certificate. Without this line the system's own list of authorities is used |
| `tls_verify_name: false` | Accept any certificate that authority signed. Needed here because the certificate a MySQL server makes for itself carries no server name. Leave the line out for a server with a proper certificate: its name is then checked against `host` |
| `allow_private_network: true` | Needed for a server at a private or loopback address, as every container's is |
| `rate_per_s`, `max_concurrency` | The most reads of single keys per second, and at once |

The connection is always encrypted unless the entry says `allow_unencrypted: true` in place of the two
`tls_` lines. Such an entry was tried here and answered `TLS no, session ok`. The password exchange
and every compared row then cross the network unprotected.

### In the web UI

The web UI lists the targets on its "Targets" screen, each with its engine under "Kind", and declares
services only ([chapter 4](04-specs.md), section 1). A database or files target is declared and checked
by the file edit and the `ds target check` of this section. Once the target exists, a comparison is stored and started like any other
([chapter 3](03-first-comparison.md)).

1. Click "Specs" in the bar. In the panel "Upload a spec", press the file button and choose
   `examples/tables/mysql/spec.yaml`. The file's text appears in the box.
2. Press "Validate". Beside the buttons you read "No issues.".
3. Press "Save". You read "Saved as spec #1."; your number will differ. The table at the top gains
   the row `acceptance-kit-mysql`, version `1.0.0`, hash `2059f5f5bf2d`.
4. Click "Runs" in the bar. In the panel "New run", choose `acceptance-kit-mysql 1.0.0 (#1)` under
   "Spec", leave "Mode" at `batch`, and leave the box "Keys, one per line (none: the spec's key
   source; a table pair without one scans both tables)" empty.
5. Press "Start". The page changes to the run. After a few seconds its status reads `completed`, and
   this line stands above the progress bar:
   ```text
   Table scan (done): 1,010 rows on A, 1,012 on B; 991 identical; 20 to compare; 1 not compared (NULL, empty or over-long key). Side A: mysql 8.4 (snapshot). Side B: mysql 8.4 (snapshot).
   ```
   Under it you read `20/20 keys · done 20` and a count for each outcome, in no fixed order:
   INCONCLUSIVE 1, MISMATCH 17, NOT_FOUND_A 1, NOT_FOUND_B 1.

The last two sentences of that line are all the page says about the engine: its name, the server's
version as far as the second number, and how the table was read. `snapshot` means every row was read
as of one moment. Tried here: for a view made for the purpose, `ds target check` printed `(view,
consistency unverified)`, because MySQL gives no way to observe that a view was read as of one
moment.

The "Pre-checks" screen is the one other place that names an engine. Its form "New table pre-check of
two schemas, paired by name" lists, under "Side a" and "Side b", every target that has an engine, each
as its name and the engine in brackets: `kit-mysql (mysql)`, the examples the targets file came with,
such as `billing-mysql (mysql)`, and after section 5 also `kit-files (files)`. A files target is not
a database: choose it there and press "Pre-check", and the form answers `not supported: a table
pre-check with a file side; its run counts rows, lines that are not rows and repeated keys`.
[Chapter 8](08-prechecks-and-schedules.md) describes the form.

### On the command line

1. Look at a table before comparing it.
   ```bash
   ds target check kit-mysql --table accounts_a
   ```
   You should see:
   ```text
   target kit-mysql: mysql 8.4.11, character set utf8mb4, TLS yes, session ok
   ds_kit.accounts_a (table, consistency snapshot)
     code     varchar(20)    text       key
     email    varchar(100)   text       key
     status   char(8)        char       key
     balance  decimal(12,2)  decimal    key
     amount   decimal(12,2)  decimal    key
     ratio    double         float
     score    float          float32
     visits   int unsigned   integer    key
     flag     tinyint(1)     integer    key
     born     date           date       key
     opened   datetime(6)    timestamp
     seen     timestamp(3)   instant
     at       time(3)        time
     tag      varbinary(16)  bytes      key
     kind     enum           text       key
     doc      json           json
     note     text           text
   ```
2. Copy the kit's spec to where `ds` can read it, the app's hand-over folder (chapter 3).
   ```bash
   docker compose cp examples/tables/mysql/spec.yaml app:/var/lib/deltasleuth/p0/kit-mysql.yaml
   ```
   You should see Docker's own line or two; the last ends in `Copied`. Every later `docker compose
   cp` answers the same way.
3. Run the kit: the table `accounts_a` against the table `accounts_b`, both on the MySQL server.
   ```bash
   ds run /var/lib/deltasleuth/p0/kit-mysql.yaml
   ```
   You should see, after a few seconds:
   ```text
   run R-35T7DCZGSZB8HQPA14JJXJFKNR started
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 1011, compared 1010, match 991, different 19, indeterminate 1, cancelled 0, outstanding 0
     INCONCLUSIVE 1, MATCH 991, MISMATCH 17, NOT_FOUND_A 1, NOT_FOUND_B 1
   ```
   The last two lines are exactly those of `examples/tables/mysql/expected.json`.
4. Now compare across engines: the PostgreSQL table of section 1 against the MySQL table
   `accounts_b`. Write the spec.

```bash
cat > practice/pg-vs-mysql.yaml <<'YAML'
apiVersion: deltasleuth/v1
kind: ComparisonSpec

metadata:
  name: kit-postgres-vs-mysql
  version: "1.0.0"
  owner: guide
  description: The practice PostgreSQL table against the MySQL kit's accounts_b.

normalizerVersion: "1.1.0"

key:
  name: code
  type: string
  class: business_code

sides:
  a:
    role: legacy
    target: kit-postgres
    table: {name: accounts, keyColumns: [code]}
  b:
    role: new
    target: kit-mysql
    table: {name: accounts_b, keyColumns: [code]}
    typeMap:
      flag: {as: boolean}

compare: {}

recheck:
  delay: 0s
  times: 1
  outcomes: [MISMATCH, NOT_FOUND_A, NOT_FOUND_B]

execution:
  concurrency: 2
  timeouts: {total: 30s}

security:
  unlisted: tokenize
YAML
```

Each side names its target, its table and its key column. Neither side lists `columns`, so every
column that both tables have is compared. The two lines under `typeMap` say that the MySQL column
`flag`, a number, is to be read as true or false: section 4 shows what happens without them.

5. Copy the spec to the hand-over folder.
   ```bash
   docker compose cp practice/pg-vs-mysql.yaml app:/var/lib/deltasleuth/p0/pg-vs-mysql.yaml
   ```
6. Run it.
   ```bash
   ds run /var/lib/deltasleuth/p0/pg-vs-mysql.yaml
   ```
   You should see:
   ```text
   run R-35T7DF1X5X2VE43M6Z9MR25E22 started
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 1011, compared 1010, match 998, different 12, indeterminate 1, cancelled 0, outstanding 0
     INCONCLUSIVE 1, MATCH 998, MISMATCH 10, NOT_FOUND_A 1, NOT_FOUND_B 1
   ```
7. Write the list of differences, and the list of keys without a verdict, as files. `<run id>` is the
   id in the line `run R-... started`.
   ```bash
   ds report <run id> --out /var/lib/deltasleuth/p0/pg-vs-mysql --kind discrepancies.csv --kind retry.csv
   ```
   You should see the two files' names:
   ```text
   /var/lib/deltasleuth/p0/pg-vs-mysql/R-35T7DF1X5X2VE43M6Z9MR25E22-discrepancies.csv
   /var/lib/deltasleuth/p0/pg-vs-mysql/R-35T7DF1X5X2VE43M6Z9MR25E22-retry.csv
   ```
8. Copy them out of the container.
   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/pg-vs-mysql practice/
   ```
9. Print the key, the outcome and the column of each difference.
   ```bash
   cut -d, -f3,4,9 practice/pg-vs-mysql/*-discrepancies.csv
   ```
   You should see:
   ```text
   key,outcome,path_pattern
   K0005,MISMATCH,$.email
   K0010,MISMATCH,$.status
   K0015,MISMATCH,$.balance
   K0035,MISMATCH,$.visits
   K0040,MISMATCH,$.flag
   K0045,MISMATCH,$.born
   K0050,MISMATCH,$.opened
   K0055,MISMATCH,$.seen
   K0080,MISMATCH,$.note
   K0090,NOT_FOUND_B,$
   K1001,NOT_FOUND_A,$
   é,MISMATCH,$.email
   ```

### How to read the result

**The check.** The first line of `ds target check` says that the address, the TLS session, the login
and the session's settings are in order. It is the only place that prints the server's full version.
Each further line is one column: its name, its type as the database declares it, the class
DeltaSleuth reads it as, and the word `key` when the column may be a key column in a pair across
engines. A column without that word can still be compared.

**The run.** A run of two tables without a key list scans both tables once, in the order of their
keys. Rows that are identical on both sides are only counted. Every other key is then read again by
itself and reported. In the kit that is 20 keys: 17 rows that differ in one column each (`MISMATCH`),
`K0090`, which only side a holds (`NOT_FOUND_B`), `K1001`, which only side b holds (`NOT_FOUND_A`),
and `K0100`, which side b holds twice, so that no row can be paired (`INCONCLUSIVE`). One row of side b
has no key at all and is counted as not compared.

`ds run` prints the run's counters but not the scan's row counts. Those are on the Run screen, in the
line quoted above, and in the report `coverage.csv` (columns `rows_a`, `rows_b`, `engine_a`,
`engine_b`).

**Across engines.** The second run reads one side from PostgreSQL and the other from MySQL, and finds
the planted differences in the columns both tables have. The Run screen's line for it, worked out from
the run's data:

```text
Table scan (done): 1,010 rows on A, 1,012 on B; 998 identical; 13 to compare; 1 not compared (NULL, empty or over-long key). Columns only on B: amount, ratio, score, at, tag, kind, doc. Side A: postgresql 16.14 (snapshot). Side B: mysql 8.4 (snapshot).
```

"Columns only on B" lists what the MySQL table has and the PostgreSQL table has not: such columns are
named and not compared. The keys in the reports are readable because the spec gives its key the class
`business_code`; the values are not, because `unlisted: tokenize` masks them.

### Limits

- Observed on MySQL 8.4.11 only. The engines document says that MySQL 8.0.17 or later is accepted,
  and that MariaDB and older MySQL servers are refused when DeltaSleuth connects. On another version,
  run this section's kit first.
- Supported on a MySQL side, as the engines document lists it: scans, runs over a key list,
  comparing one key, a table against a service, key columns of several parts, filters with `LIKE`,
  and type maps. Run here: a scan, one key (section 4) and a type map.
- Not supported on a MySQL side: `ILIKE` in a filter, key maps that use a regular expression, and
  graph parity. They are refused when the spec is checked against its targets. Tried here with the
  filter `where: "email ILIKE 'customer%'"` on side a of the kit's spec: the request of the
  "Validate" button, and `ds spec validate <spec> --targets /etc/deltasleuth/targets.yaml`, answered
  `not supported: ILIKE on a MySQL or Oracle side; use LIKE`. Without `--targets`, `ds spec validate`
  reads the spec alone and answered `ok`.
- Across engines a scan is slower than between two tables of one engine, where the database itself
  hashes most columns: every compared column of every row travels to DeltaSleuth and is hashed
  there. The deployment document measured about four times the duration for a million rows.
- Values are compared as the reader account reads them. Choose an account that sees the real values:
  if the database masks a column for it, both sides can show the same mask and read as a match.
- The practice server does not start again by itself after the machine restarts. `docker start
  deltasleuth-kit-mysql` brought it back with its data here, after a `docker stop`.

### If something goes wrong

Each message below was provoked here; those about a target, with a second
target entry that was wrong on purpose. Those entries had names of their own, such as `bad-pw`; the
messages are printed here with `kit-mysql` in that place. `ds target check` and `ds run` word them
alike: a run says `side a:` where the check says `target kit-mysql:`. In the web UI the same text
appears under the "Start" button of "New run".

**`docker: Error response from daemon: failed to set up container networking: network
deltasleuth_default not found`**, from step 4
This folder is a second installation, and its network carries its project name: see "In a second
installation" at the top. Docker printed an id before the message and left a container that never
started. Remove it with the next command, then run step 4 with your names. `-v` also removes the
storage Docker had made for it.
```bash
docker rm -v deltasleuth-kit-mysql
```

**`docker: Error response from daemon: Conflict. The container name "/deltasleuth-kit-mysql" is
already in use by container "<id>". You have to remove (or rename) that container to be able to reuse
that name.`**, from step 4
A container of that name exists. If a failed step 4 left it, the command above removes it and
prints its name. If the command answers `Error response from daemon: cannot remove container
"deltasleuth-kit-mysql": container is running: stop the container before removing or force remove`,
it is a practice server that works: yours from an earlier pass through this section, or that of
another installation on this machine that followed this chapter. Leave it, and in the second case
use your own project's name in its place.

**`no such file or directory: practice/pg-vs-mysql.yaml`** (zsh; bash says `practice/pg-vs-mysql.yaml:
No such file or directory`)
The folder `practice` is not there: do steps 1 to 4 of section 1.

**`error: target kit-mysql: cannot connect to target kit-mysql (mysql deltasleuth-kit-mysql:3306/ds_kit
as ds_reader): MySQL 1045: the credential was refused`**
The file that `password_ref` names does not hold the account's password. Provoked by naming another
password file. Check that the entry names the file of step 7 and that step 8 ended without an error.

**`... gaierror: the host name does not resolve`**
The name in the `host` line is not that of a container in the installation's network. It must be
the name after `--name` in step 4, and the name after `--network` there must be this installation's
network. Provoked here in a second installation by leaving `deltasleuth-kit-mysql` in the `host:`
line. Not verified here, because it would have touched another installation: step 4 typed with
`deltasleuth_default` while a first installation runs on the machine puts the server into that
installation's network, where this installation's app cannot find it either. Remove such a server
with `docker rm -f -v` and its name, and start it again with your names.

**`... ConnectionRefusedError: no server answered`**
Nothing listens at that address and port: the server is stopped or still starting, or the `port` line
is wrong.

**`... 172.30.0.5 is a private address: set allow_private_network on the target`**
The entry lacks `allow_private_network: true`. The address is the server's and will differ.

**`... SSLCertVerificationError: TLS: the server's certificate was not accepted`**
The entry lacks `tls_verify_name: false` or `tls_ca_ref`, so the self-made certificate was checked
against the server's name or against the system's authorities.

**`... error: the answer is not a MySQL server's`**
The port belongs to another service. Provoked with port 33060, which MySQL uses for another protocol.

**`error: cannot read secret file /run/secrets/kit_mysql_passwd: No such file or directory`**
The file is not shown to the container: its name is misspelt in the target entry, as here, or missing
from the site file (step 11).

**`error: /targets/kit-mysql [invalid_database_target] engine mysql needs database and takes no
service`**
A mistake in the target entry, here `service:` in place of `database:`. While the targets file has a
mistake, every `ds` command stops with such a line, and the app stops at its next start
([appendix](A1-install-by-hand.md), "Starting by hand"). That is why step 13 checks before step 14
applies.

**`error: target kit-mysql: table or view nope not found or not visible`**
The table does not exist in the target's database. The engines document says that the same line
appears when the account has no `SELECT` on it; not verified here.

## 3. An Oracle table as one side

### What it is for

The same as section 2, for an Oracle database, version 19c or later. Oracle differs from the other
engines in ways that show in a comparison: it stores an empty text as "no value", its `DATE` holds a
time of day, and it has no true-or-false type before version 23. This section meets all three.

### Before you start

You need an Oracle server, an account that may read the compared tables, and a target entry. The
practice server took about 2 GiB of memory here (between 1.9 and 2.4 GiB), and its image is 4.8 GB
as Docker lists it.

Do steps 1 to 4 of section 1 first: every step below needs them. Steps 5 to 14 of section 1 are
needed only for the comparison with PostgreSQL, from step 5 of "On the command line". Section 2 is
not needed.

1. Fetch the image. Not verified here: this command, for the same reason as in section 2.
   ```bash
   docker pull gvenzl/oracle-free:23-slim-faststart
   ```
2. Create the practice server's administrator password, in a file.
   ```bash
   (umask 077; openssl rand -hex 12 > deploy/secrets/kit_oracle_system)
   ```
3. Let the server's container read it.
   ```bash
   chmod 444 deploy/secrets/kit_oracle_system
   ```
4. Start the server in the installation's network. In a second installation, write your project
   name in `deltasleuth-kit-oracle` and `deltasleuth_default`, here and in every later step.
   ```bash
   docker run -d --name deltasleuth-kit-oracle --network deltasleuth_default -e ORACLE_PASSWORD_FILE=/run/secrets/kit_oracle_system -v "$PWD/deploy/secrets/kit_oracle_system:/run/secrets/kit_oracle_system:ro" gvenzl/oracle-free:23-slim-faststart
   ```
5. Wait about fifteen seconds, then check that the database is ready. Here it was ready within ten
   seconds of the start. If you see no line, wait and ask again.
   ```bash
   docker logs deltasleuth-kit-oracle 2>&1 | grep 'DATABASE IS READY TO USE'
   ```
   You should see:
   ```text
   DATABASE IS READY TO USE!
   ```
6. Create the account that will own the kit's tables, `KIT_OWNER`. It has no password: nobody logs
   in as it. `sqlplus / as sysdba` is the administrator's door inside the container and asks for no
   password. The command prints nothing when it succeeds.
   ```bash
   printf 'WHENEVER SQLERROR EXIT FAILURE\nSET FEEDBACK OFF\nALTER SESSION SET CONTAINER = FREEPDB1;\nCREATE USER kit_owner NO AUTHENTICATION QUOTA UNLIMITED ON users;\n' | docker exec -i deltasleuth-kit-oracle sqlplus -s / as sysdba
   ```
7. Load the kit into that account: the tables `DS_KIT_A` and `DS_KIT_B`. This prints nothing either.
   ```bash
   { printf 'WHENEVER SQLERROR EXIT FAILURE\nSET FEEDBACK OFF\nALTER SESSION SET CONTAINER = FREEPDB1;\nALTER SESSION SET CURRENT_SCHEMA = kit_owner;\n'; cat examples/tables/oracle/seed.sql; } | docker exec -i deltasleuth-kit-oracle sqlplus -s / as sysdba
   ```
8. Create the password of the account DeltaSleuth reads with.
   ```bash
   (umask 077; openssl rand -hex 12 > deploy/secrets/kit_oracle_password)
   ```
9. Create that account, `DS_READER`, with its grants. An Oracle reader account needs `CREATE SESSION`,
   and `READ` on each table or view a spec names. `READ` is weaker than `SELECT`: it does not allow
   locking rows.
   ```bash
   { printf 'WHENEVER SQLERROR EXIT FAILURE\nSET FEEDBACK OFF\nALTER SESSION SET CONTAINER = FREEPDB1;\nCREATE USER ds_reader IDENTIFIED BY "'; tr -d '\n' < deploy/secrets/kit_oracle_password; printf '";\nGRANT CREATE SESSION TO ds_reader;\nGRANT READ ON kit_owner.ds_kit_a TO ds_reader;\nGRANT READ ON kit_owner.ds_kit_b TO ds_reader;\n'; } | docker exec -i deltasleuth-kit-oracle sqlplus -s / as sysdba
   ```
10. Let the app's container read the password file.
    ```bash
    chmod 444 deploy/secrets/kit_oracle_password
    ```
11. Show the file to the app. In `deploy/docker-compose.site.yml`, write the name into the brackets
    of the `secrets:` line under `app:`, and add one line at the end of the file, under the line
    `secrets:` at the left edge. If you did all of sections 1 and 2, the two places then read:

```text
    secrets: [kit_pg_dsn, kit_mysql_password, kit_mysql_ca, kit_oracle_password]
```

```text
secrets:
  kit_pg_dsn: {file: ./secrets/kit_pg_dsn}
  kit_mysql_password: {file: ./secrets/kit_mysql_password}
  kit_mysql_ca: {file: ./secrets/kit_mysql_ca}
  kit_oracle_password: {file: ./secrets/kit_oracle_password}
```

If you skipped some of those steps, your file has fewer names. With only steps 1 to 4 of section 1
done, as tried here, the first place reads `secrets: [kit_oracle_password]`, and the end of the file
gets the line `secrets:` and under it the one line that starts with `kit_oracle_password`.

12. Declare the target. In a second installation, write your container's name in the `host:` line.

```bash
cat >> deploy/config/targets.yaml <<'YAML'

  kit-oracle:
    engine: oracle
    host: deltasleuth-kit-oracle
    port: 1521
    service: FREEPDB1
    user: ds_reader
    password_ref: file:/run/secrets/kit_oracle_password
    allow_unencrypted: true
    allow_private_network: true
    rate_per_s: 50
    max_concurrency: 2
YAML
```

13. Check the target.
    ```bash
    ds target check kit-oracle
    ```
    You should see:
    ```text
    target kit-oracle: oracle 23.26.3.0.0, character set AL32UTF8, TLS no, session ok
    ```
14. Apply the two files.
    ```bash
    docker compose --profile mocks up -d
    ```

The entry differs from a MySQL one in two lines. `service` is the name of the database service, here
the image's `FREEPDB1`; a real one comes from the database administrator. `allow_unencrypted: true`
is there because the practice server offers no encrypted listener. A real Oracle target leaves that
line out and declares `tls_ca_ref` and `tls_server_dn`, the exact subject of the server's
certificate, as `deploy/config/targets.example.yaml` shows under `ledger-oracle`. Not verified here:
an encrypted Oracle connection.

### In the web UI

As in section 2: no screen declares or checks a database target. The kit's spec must first get the name of the
tables' owner, which step 2 of "On the command line" does; it writes `practice/kit-oracle.yaml`.

1. Click "Specs". In "Upload a spec", choose `practice/kit-oracle.yaml`, press "Validate" ("No
   issues."), then "Save".
2. Click "Runs". In "New run", choose `acceptance-kit-oracle 1.0.0` under "Spec", leave the rest, and
   press "Start".
3. When the status reads `completed`, the line above the progress bar reads:
   ```text
   Table scan (done): 1,010 rows on A, 1,012 on B; 991 identical; 20 to compare; 1 not compared (NULL, empty or over-long key). Side A: oracle 23.26 (snapshot). Side B: oracle 23.26 (snapshot).
   ```

### On the command line

1. Look at a table. The account that reads is not the account that owns the table, so the owner's
   name goes before the table's name.
   ```bash
   ds target check kit-oracle --table KIT_OWNER.DS_KIT_A
   ```
   You should see:
   ```text
   target kit-oracle: oracle 23.26.3.0.0, character set AL32UTF8, TLS no, session ok
   KIT_OWNER.DS_KIT_A (table, consistency snapshot)
     CODE     VARCHAR2(20)                  text       key
     EMAIL    VARCHAR2(100)                 text       key
     STATUS   CHAR(8)                       char       key
     BALANCE  NUMBER(12,2)                  decimal    key
     AMOUNT   NUMBER(12,2)                  decimal    key
     RATIO    BINARY_DOUBLE                 float
     SCORE    BINARY_FLOAT                  float32
     VISITS   NUMBER(10)                    integer    key
     PLAIN    NUMBER                        decimal    key
     FLAG     NUMBER(1)                     integer    key
     BORN     DATE                          timestamp  key
     OPENED   TIMESTAMP(6)                  timestamp
     SEEN     TIMESTAMP(3) WITH TIME ZONE   instant
     HELD     INTERVAL DAY(2) TO SECOND(3)  other
     TAG      RAW(16)                       bytes      key
     DOC      CLOB                          text
     NOTE     NVARCHAR2(100)                text
   ```
2. Make a copy of the kit's spec that names the tables' owner, as `schema`.
   ```bash
   sed 's/table: {name: ds_kit_/table: {schema: KIT_OWNER, name: ds_kit_/' examples/tables/oracle/spec.yaml > practice/kit-oracle.yaml
   ```
3. Copy it to the hand-over folder.
   ```bash
   docker compose cp practice/kit-oracle.yaml app:/var/lib/deltasleuth/p0/kit-oracle.yaml
   ```
4. Run it.
   ```bash
   ds run /var/lib/deltasleuth/p0/kit-oracle.yaml
   ```
   You should see the two lines of `examples/tables/oracle/expected.json`:
   ```text
   run R-35T7DJ6X1J3K37EP283ZA0Q9AE started
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 1011, compared 1010, match 991, different 19, indeterminate 1, cancelled 0, outstanding 0
     INCONCLUSIVE 1, MATCH 991, MISMATCH 17, NOT_FOUND_A 1, NOT_FOUND_B 1
   ```
5. Compare across engines: the PostgreSQL table of section 1 against the Oracle table `DS_KIT_B`.
   Write the spec.

```bash
cat > practice/pg-vs-oracle.yaml <<'YAML'
apiVersion: deltasleuth/v1
kind: ComparisonSpec

metadata:
  name: kit-postgres-vs-oracle
  version: "1.0.0"
  owner: guide
  description: The practice PostgreSQL table against the Oracle kit's DS_KIT_B.

normalizerVersion: "1.1.0"

key:
  name: code
  type: string
  class: business_code

sides:
  a:
    role: legacy
    target: kit-postgres
    table:
      name: accounts
      keyColumns: [code]
      columns: [email, status, balance, visits, flag, born, opened, note]
    typeMap:
      note: {as: string, emptyAsNull: true}
  b:
    role: new
    target: kit-oracle
    table: {schema: KIT_OWNER, name: ds_kit_b, keyColumns: [code]}
    typeMap:
      FLAG: {as: boolean}
      BORN: {as: date}

compare: {}

recheck:
  delay: 0s
  times: 1
  outcomes: [MISMATCH, NOT_FOUND_A, NOT_FOUND_B]

execution:
  concurrency: 2
  timeouts: {total: 30s}

security:
  unlisted: tokenize
YAML
```

Side a lists the columns to compare, because the Oracle kit wrote the column `seen` at another time
of day than section 1 did, and it would differ in every row. The three lines under `typeMap` bridge
the three Oracle differences named above; section 4 takes each away in turn.

6. Copy it to the hand-over folder.
   ```bash
   docker compose cp practice/pg-vs-oracle.yaml app:/var/lib/deltasleuth/p0/pg-vs-oracle.yaml
   ```
7. Run it.
   ```bash
   ds run /var/lib/deltasleuth/p0/pg-vs-oracle.yaml
   ```
   You should see:
   ```text
   run R-35T7DJTHZ3M2DES70FMKKZ4MNB started
   warning: spec 5 (kit-postgres-vs-oracle 1.0.0) needs a four-eyes approval of population_filters; tokens mode has no approvers, so run R-35T7DJTHZ3M2DES70FMKKZ4MNB is labelled unapproved
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 1011, compared 1010, match 1000, different 10, indeterminate 1, cancelled 0, outstanding 0
     INCONCLUSIVE 1, MATCH 1000, MISMATCH 8, NOT_FOUND_A 1, NOT_FOUND_B 1
   ```

### How to read the result

Oracle writes names in capital letters. A spec may write them in small letters: `ds_kit_b` found
`DS_KIT_B`, and Oracle's column `EMAIL` was paired with PostgreSQL's `email`. In the `typeMap` above
the columns are written as the refusal messages of section 4 write them, in capitals.

The class column of the check shows the three differences before any run. `FLAG` is `NUMBER(1)`, an
integer, where PostgreSQL has a boolean. `BORN` is a `DATE` and its class is `timestamp`, because an
Oracle `DATE` holds hours, minutes and seconds. `NOTE` shows nothing unusual: that Oracle keeps an
empty text as "no value" is seen only in the rows. `HELD` is of class `other`: the engines
document says that such a column is compared between two Oracle tables, as in the kit, and never
across engines.

The warning in step 7 appears because the spec lists `columns`. Choosing which columns are compared
changes what a result means, so an installation with single sign-on wants a second person to approve
such a spec first. With token sign-in there is nobody to approve: the run goes ahead, and the Run
screen says `Unapproved spec version: population_filters was not approved by a second person (tokens
mode has no approvers). Not sign-off evidence.`

The cross-engine run finds 8 rows that differ, where the kit's own run found 17: this spec compares
8 columns, and the kit plants several of its differences in columns this spec does not compare. One
planted difference is hidden on purpose: `K0045` differs by one second in `BORN`, and
`BORN: {as: date}` compares the day only. The Run screen's line for this run:

```text
Table scan (done): 1,010 rows on A, 1,012 on B; 1,000 identical; 11 to compare; 1 not compared (NULL, empty or over-long key). Columns only on B: amount, ratio, score, plain, seen, held, tag, doc. Side A: postgresql 16.14 (snapshot). Side B: oracle 23.26 (snapshot).
```

### Limits

- Observed on Oracle 23.26.3 only, in a database whose character set is AL32UTF8 (Unicode). The
  engines document says that Oracle 19c or later is accepted. On another version or character set,
  run this section's kit first.
- A real Oracle target is reached over TLS, declared with `tls_ca_ref` and `tls_server_dn`. The
  engines document lists what is not supported: Oracle's own Native Network Encryption, a SID in
  place of a service name, connect descriptors and wallets.
- The same features as on a MySQL side are supported and refused (section 2).
- The practice server does not start again by itself after the machine restarts. `docker start
  deltasleuth-kit-oracle` brought it back with its data here, after a `docker stop`.

### If something goes wrong

Provoked here, each with a second target entry that was wrong on purpose,
printed with the name `kit-oracle`, or with a grant taken away. The messages of step 4, when the
network is not found or the container's name is taken, and of a missing folder `practice`, are those
of section 2 with this section's names.

**`error: target kit-oracle: cannot connect to target kit-oracle (oracle
deltasleuth-kit-oracle:1521/FREEPDB1 as ds_reader): ORA-01017: the credential was refused`**
The file that `password_ref` names does not hold the account's password.

**`... DPY-6001: the listener does not know the service`**
The `service` line names a service the server does not have.

**`... ConnectionResetError: TLS: the handshake failed`**
The entry asks for TLS and the listener is a plain one. The practice server needs
`allow_unencrypted: true` and no `tls_` line.

**`error: /targets/kit-oracle [invalid_database_target] engine oracle with TLS needs tls_server_dn (the
server certificate's subject), or tls_verify_name: false`**
The entry has neither `allow_unencrypted: true` nor the lines an encrypted Oracle connection needs.
Like every mistake in the targets file, it stops every `ds` command until it is corrected. For the
practice server, write `allow_unencrypted: true`. Do not follow the message's last words alone: an
entry given only `tls_verify_name: false` is refused again, with **`error: /targets/kit-oracle
[invalid_database_target] tls_verify_name: false needs tls_ca_ref and takes no tls_server_dn`**. As
tried here, the targets file accepts `tls_verify_name: false` only together with `tls_ca_ref` and
without `tls_server_dn`.

**`error: target kit-oracle: table or view DS_KIT_A not found or not visible`**, or from a run
**`error: side a: table or view ds_kit_a not found or not visible`**
The table belongs to another account and the name lacks its owner: write `KIT_OWNER.DS_KIT_A` in the
check and `schema: KIT_OWNER` in the spec. The same line, with the owner's name in it, appears when
the account has no `READ` on the table; that was provoked here by taking the grant away.

## 4. Traps between engines

### What it is for

Two databases can hold the same facts and still not hold the same values: one has no true-or-false
type, one keeps a time inside a date, one takes `e` and `E` for the same text. DeltaSleuth never
bridges such a difference silently: it refuses the run and names the line to add to the spec, or it
reports the difference. This section shows each trap once, with its message, for whoever writes specs.

### Before you start

All of section 1, and sections 2 and 3 to their ends: the three practice targets, and the specs
`practice/pg-vs-mysql.yaml` and `practice/pg-vs-oracle.yaml`.

Each step below writes a changed copy of a spec straight into the app's hand-over folder and runs it.
The copies get names of their own, because `ds run` stores every spec it is given, also one it then
refuses.

### In the web UI

A run that is refused when it is created never becomes a run. The page stays on "Runs", and the
message, without the word `error:`, appears under the "Start" button of the "New run" panel. A
difference that is reported, as in step 3 below, shows on the Run screen like any other.

### On the command line

1. **A type one engine lacks.** Take the `typeMap` away from the MySQL comparison of section 2.
   ```bash
   sed -e 's/name: kit-postgres-vs-mysql/name: trap-boolean/' -e '/typeMap:/d' -e '/flag: {as: boolean}/d' practice/pg-vs-mysql.yaml | docker compose exec -T app sh -c 'cat > /var/lib/deltasleuth/p0/trap.yaml'
   ```
   ```bash
   ds run /var/lib/deltasleuth/p0/trap.yaml
   ```
   You should see no run, and this line:
   ```text
   error: side a: column 'flag' is a boolean (boolean) and side b's a number: declare typeMap {flag: {as: boolean}} on side b
   ```
2. **A date that holds a time.** Take only the `BORN` line away from the Oracle comparison of
   section 3.
   ```bash
   sed -e 's/name: kit-postgres-vs-oracle/name: trap-date/' -e '/BORN: {as: date}/d' practice/pg-vs-oracle.yaml | docker compose exec -T app sh -c 'cat > /var/lib/deltasleuth/p0/trap.yaml'
   ```
   ```bash
   ds run /var/lib/deltasleuth/p0/trap.yaml
   ```
   You should see:
   ```text
   error: side a: column 'born' is a date (date) and side b's a timestamp: declare typeMap {BORN: {as: date}} on side b, or {as: timestamp} on both
   ```
3. **Empty text and no value.** Oracle stores an empty text as NULL, "no value". Take the `note`
   line away, with the `typeMap:` line above it.
   ```bash
   sed -e 's/name: kit-postgres-vs-oracle/name: trap-empty/' -e '/^    typeMap:$/{N;/emptyAsNull/d;}' practice/pg-vs-oracle.yaml | docker compose exec -T app sh -c 'cat > /var/lib/deltasleuth/p0/trap.yaml'
   ```
   ```bash
   ds run /var/lib/deltasleuth/p0/trap.yaml
   ```
   This time the run is not refused. You should see 47 more rows that differ than in section 3:
   ```text
   run R-35T7DKA25HBTXQ5YMPDXWQ0RYD started
   warning: spec 8 (trap-empty 1.0.0) needs a four-eyes approval of population_filters; ...
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 1011, compared 1010, match 953, different 57, indeterminate 1, cancelled 0, outstanding 0
     INCONCLUSIVE 1, MATCH 953, MISMATCH 55, NOT_FOUND_A 1, NOT_FOUND_B 1
   ```
4. **Capital letters and accents in a key.** MySQL's usual way of comparing text takes `e`, `E` and
   `é` for the same value. Ask MySQL itself for the row with the key `e`.
   ```bash
   printf "SELECT code, email FROM ds_kit.accounts_b WHERE code = 'e';\n" | docker exec -i deltasleuth-kit-mysql mysql --defaults-extra-file=/etc/kit_mysql_admin.cnf --default-character-set=utf8mb4
   ```
   You should see three rows:
   ```text
   code	email
   e	order
   é	changed
   E	order
   ```
   Now let DeltaSleuth compare the key `e` between the two MySQL tables of the kit.
   ```bash
   ds compare e --spec /var/lib/deltasleuth/p0/kit-mysql.yaml
   ```
   You should see one row found, and equal:
   ```text
   e: MATCH (MATCH, provisional)
   run R-35T7DKHR096H4V5VP39X00FAC7; observations a O-d38399e3103d50d8 b O-c91cf5b7281fdcc4
   ```
   Then the key `é`.
   ```bash
   ds compare é --spec /var/lib/deltasleuth/p0/kit-mysql.yaml
   ```
   You should see the planted difference, with both values masked:
   ```text
   é: MISMATCH (DIFFERENT, rechecked)
     MEDIUM VALUE_MISMATCH value_changed /email
         A: tok:free_text:v1:5T5U6UWXDYDFFSQ2CL7DWOZHQF
         B: tok:free_text:v1:3GWLAT2M6HBE5KLMGYJOYKEJ2Q
   run R-35T7DKNF77NMQ4K1EWT3DDXD2A; observations a O-a53c600f193a0278 b O-9652a72923ead222
   ```
5. **A time with a zone against a time without one.** Pair PostgreSQL's `opened`, which has no time
   zone, with MySQL's `seen`, which has one.
   ```bash
   sed -e 's/name: kit-postgres-vs-mysql/name: trap-zone/' -e 's/table: {name: accounts, keyColumns: \[code\]}/table: {name: accounts, keyColumns: [code], columns: [{name: opened, as: seen}]}/' -e 's/table: {name: accounts_b, keyColumns: \[code\]}/table: {name: accounts_b, keyColumns: [code], columns: [seen]}/' -e '/typeMap:/d' -e '/flag: {as: boolean}/d' practice/pg-vs-mysql.yaml | docker compose exec -T app sh -c 'cat > /var/lib/deltasleuth/p0/trap.yaml'
   ```
   ```bash
   ds run /var/lib/deltasleuth/p0/trap.yaml
   ```
   You should see:
   ```text
   error: side a: column 'seen' is a timestamp (timestamp(6) without time zone) and side b's a zoned timestamp: declare typeMap {as: timestamp, naiveTimezone: ...} on both
   ```
6. **A number key with two scales.** The MySQL kit declares `amount` with two decimal places in one
   table and four in the other. Make it the key.
   ```bash
   sed -e 's/name: acceptance-kit-mysql/name: trap-scale/' -e 's/keyColumns: \[code\]/keyColumns: [amount]/' examples/tables/mysql/spec.yaml | docker compose exec -T app sh -c 'cat > /var/lib/deltasleuth/p0/trap.yaml'
   ```
   ```bash
   ds run /var/lib/deltasleuth/p0/trap.yaml
   ```
   You should see:
   ```text
   error: key column 'amount' is decimal(12,2) on side a and decimal(14,4) on side b: a number key needs one scale on both sides (compare a view that casts it)
   ```

### How to read the result

| Trap | What DeltaSleuth does | What to declare |
|---|---|---|
| MySQL `TINYINT(1)` or Oracle `NUMBER(1)` against a boolean | Refuses the run (step 1) | `{as: boolean}` on the number's side |
| An Oracle `DATE`, which holds a time, against a date | Refuses the run (step 2) | `{as: date}` on the Oracle side, which drops the time, or `{as: timestamp}` on both sides |
| An empty text against Oracle's NULL | Reports each such row as a difference (step 3) | `{as: string, emptyAsNull: true}` on the side that is not Oracle |
| Keys that differ only in capitals, in accents or by a space at the end | Treats them as different keys (step 4) | Nothing: this is what you want. A key is found by its bytes, whatever the database's own way of comparing text is |
| A time with a zone against one without | Refuses the run (step 5) | `{as: timestamp, naiveTimezone: <zone>}` on both sides. `<zone>` is the zone the values without one were written in, for example `Europe/London` |
| Two times with a zone | Compares the moment, not the digits | Nothing. `12:00` at `+02:00` and `12:00` at `+00:00` are two moments. The Oracle kit and the PostgreSQL table of section 1 hold `seen` so, and a run of that column alone found all 1,000 rows different |
| A number key declared with two scales | Refuses the run (step 6) | A view on one side that casts the key: `1` never pairs with `1.00` |
| A key column that is a floating-point number, or a time with fractions of a second | Refuses the run | A view that casts the key |
| A date key against an Oracle `DATE` key | Refuses the run | A view that casts one side's key, so that both read alike |

The last two rows were provoked here with specs of their own. They answered `side a: key column
'ratio' (double) cannot be a key: compare a view that casts it`, the same for `'opened'
(datetime(6))`, and `key column 'born' is date on side a and DATE on side b: the key text of a date is
never that of a timestamp (compare a view that casts it)`. Not verified here: the views themselves,
and a run with `naiveTimezone`.

In step 3 the 47 extra rows are those whose `note` is empty in PostgreSQL. The report
`discrepancies.csv` of such a run gave them the change type `type_changed`: one side holds a text and
the other no value. A text that differs from another text reads `value_changed`.

In step 4, `(MATCH, provisional)` and `(DIFFERENT, rechecked)` are the verdict and how far it was
confirmed, as chapter 3 explains. The point is the first word: DeltaSleuth read one row for `e` and
found it equal on both sides, although MySQL's own comparison finds three rows.

The engines document says how zones are read: a MySQL `TIMESTAMP` with the session's time zone set
to UTC, and an Oracle time with a zone converted to UTC, so the zone a value was stored with is
never compared. A MySQL `DATETIME` and an Oracle `TIMESTAMP` without a zone are compared as written.

### Limits

- A refusal names one column at a time. A spec with two such columns is refused twice, as the Oracle
  comparison of section 3 was here: first for `flag`, then for `born`.
- A `typeMap` entry is part of the spec, so under single sign-on it is approved with the spec.
- What no declaration bridges needs a view in the database. The engines document names MySQL's
  zero dates and a `uuid` against Oracle's `RAW(16)`, beside the key cases of the table above. Not
  verified here.

### If something goes wrong

**`error: side a: typeMap column 'note' is not compared`**
A `typeMap` names a column that the side's `columns` list leaves out. Remove the entry, or list the
column. Met here when a changed copy of a spec dropped a column and kept its `typeMap` line.

**Every row differs in one column of times.**
The two sides hold the same digits in different zones, or one side holds a time of day inside a date.
Look at the column's class on both sides with `ds target check`, and at one key with `ds compare`, as
in step 4.

**The same spec name appears several times on the "Specs" screen and under "Spec" in "New run".**
Every changed content is stored as a new spec, also under an unchanged name and version, and also
when its run was refused. The number in brackets tells them apart: the highest is the newest.

## 5. CSV and Parquet files as one side

### What it is for

Some systems cannot be queried at all: they write an export every night, as CSV or Parquet files,
into a folder. With a files target a side of a comparison is such a folder, read in place, for teams
whose legacy side is a nightly export and for checking an export against the table it was taken from.
DeltaSleuth compares the rows of exactly these files and says so: it does not know the system they
came from, nor whether the export is complete.

### Before you start

Three things are needed: the folder shown to the app's container read-only, a target entry, and files
that the container's user, number 10001, may read. The settings file needs no change.

Do steps 1 to 4 of section 1 first: every step below needs them. Steps 5 to 14 of section 1 are
needed only for the comparison with a table, from step 6 of "On the command line". Sections 2 and 3
are not needed.

1. Make a folder for the practice export. A real export lies where its job writes it; this one is
   kept with the chapter's other working files, so that it belongs to this installation alone and
   section 6 removes it with them.
   ```bash
   mkdir -p practice/exports/kit
   ```
2. Let the container's user, number 10001, write into it for the next command, as the kit's own
   instructions ask. On macOS with Docker Desktop the next command also worked without this step.
   Not verified here: Linux, for which the instructions ask for it.
   ```bash
   chmod 777 practice/exports/kit
   ```
3. Write the kit's two exports with the kit's generator, run by the Python of the app's image: a
   CSV file as a legacy system writes one, and a Parquet file with planted differences. The command
   shows the folder to a short-lived container as `/export`, writable. It prints nothing when it
   succeeds.
   ```bash
   docker compose run --rm --no-deps -v "$PWD/examples/tables/files:/kit:ro" -v "$PWD/practice/exports/kit:/export" app python /kit/generate.py /export
   ```
4. Close the folder again.
   ```bash
   chmod 755 practice/exports/kit
   ```
5. Look at what was written.
   ```bash
   head -3 practice/exports/kit/accounts_a/accounts.csv
   ```
   You should see a header line and two rows, with semicolons between the fields, a comma as the
   decimal sign, and dates as day, month, year:
   ```text
   account_id;holder;balance;opened;status
   000001;Holder 1;1,25;02/01/2020;A
   000002;Holder 2;2,50;03/01/2020;A
   ```
6. In `deploy/docker-compose.site.yml`, add this line under `app:`, for example after its `secrets:`
   line. It starts with four spaces. The path before the first colon is the folder on the machine:
   a path that starts with `..` is read from the folder `deploy`, where the site file lies, so this
   one is the folder of step 1. `:ro` at the line's end makes the folder read-only for the app.

```text
    volumes: ["../practice/exports/kit:/var/lib/deltasleuth/files/kit:ro"]
```

7. Declare the target. `root` is the folder as the container sees it. A files target holds no secret.

```bash
cat >> deploy/config/targets.yaml <<'YAML'

  kit-files:
    engine: files
    root: /var/lib/deltasleuth/files/kit
    settle_s: 0
YAML
```

8. Check the target.
   ```bash
   ds target check kit-files
   ```
   You should see two lines. The first comes each time a command starts the file reader, and is not
   an error:
   ```text
   the file reader started: duckdb 1.5.5 in process 53, data limit 5,296 MiB, sockets denied
   target kit-files: files, read-only, duckdb 1.5.5, parquet and core_functions built in, no extension loads, read in a process of its own (data limit 5,296 MiB, sockets denied)
   ```
9. Apply the two files.
   ```bash
   docker compose --profile mocks up -d
   ```

`settle_s` is how many seconds a file must have been left alone before it is read. The default is 60,
so that an export still being written is not read; `0` is for this practice folder, which you have
just written. A real export keeps the default.

The folder inside the container must lie under `/var/lib/deltasleuth/files`. By the deployment
document a real export is mounted the same way, with the folder's full path on the machine, for
example `/srv/exports/legacy:/var/lib/deltasleuth/files/legacy:ro`, and its files and folders must
be readable by user 10001 (`chmod -R o+rX`, or a group).

**The `files` block of the settings file** is optional, and everything in it has a default. It was
left switched off for every run of this section. To change a limit, open
`deploy/config/deltasleuth.yaml`, remove the `# ` in front of `files:` and in front of the line you
change, check with `ds target check kit-files`, and restart the app (`docker compose restart app`).
For example, to give the file reader 1 GB in place of 2 GB, the two lines read:

```text
files:
  memory: 1GB                                    # DuckDB's buffers a process; size the container at this plus 1 GiB
```

Tried here: with these two lines the check said `data limit 4,272 MiB` in place of `5,296 MiB`.
After the restart the app answered at its health address, a run of the files kit completed with
the kit's counts, and the app's log (`docker compose logs app`) held the line `the file reader
started: duckdb 1.5.5 in process 67, data limit 4,272 MiB, sockets denied`. Putting the two `# `
back and restarting once more returned the limit to 5,296 MiB. With `os_limits: false` in the block,
the check said `no data limit (files.os_limits is false), sockets not denied (files.os_limits is
false)`.

### In the web UI

No screen lists an export's files, drafts a CSV layout or prints the files' hashes: those are `ds
target check`, below. A comparison is stored and started as in section 2.

1. Click "Specs". In "Upload a spec", choose `examples/tables/files/spec.yaml`, press "Validate" ("No
   issues."), then "Save".
2. Click "Runs". In "New run", choose `acceptance-kit-files 1.0.0` under "Spec", leave the rest, and
   press "Start". By the deployment document, creating the run reads the files whole, so with large
   files the page waits here.
3. When the status reads `completed`, the line above the progress bar reads:
   ```text
   Table scan (done): 1,000 rows on A, 1,002 on B; 995 identical; 6 to compare; 1 not compared (NULL, empty or over-long key). Side A: files (csv), 1 files, 38,118 bytes, fixed. Side B: files (parquet), 1 files, 19,098 bytes, fixed. 1 repeated keys.
   ```

For a file side the line gives the format, the number of files and their size, and the word `fixed`:
the files were pinned when the run was created. It adds `lines not rows` and `repeated keys` when
there are any.

### On the command line

1. List the CSV export, draft its layout and count its rows. The path after `--table` is relative to
   the target's `root`.
   ```bash
   ds target check kit-files --table accounts_a/accounts.csv --format csv --header --count
   ```
   You should see, after the two lines of the check above:
   ```text
   accounts_a/accounts.csv (csv): 1 file(s), 38,118 bytes, newest modified 2026-10-05 20:27 UTC, settled
     1  accounts_a/accounts.csv  38,118  2026-10-05 20:27 UTC
   draft (sniffed from the first 20,480 rows of file 1): a run reads only what the approved spec declares
     csv: {delimiter: ";", newline: "\n", header: true, columns: [account_id, holder, balance, opened, status]}
     typeMap: {opened: {as: date, format: "%d/%m/%Y"}}
   note: account_id holds only digits and 1,000 sampled values start with 0: left as text
   count (as drafted): 1,000 rows, a pass 0.0 s, 0 line(s) not rows
   ```
2. Do the same for the Parquet export. A path that ends in `/` means every file in that folder.
   ```bash
   ds target check kit-files --table accounts_b/ --format parquet --count
   ```
   You should see the file and its columns, which carry their types:
   ```text
   accounts_b/ (parquet): 1 file(s), 19,098 bytes, newest modified 2026-10-05 20:27 UTC, settled
     1  accounts_b/accounts.parquet  19,098  2026-10-05 20:27 UTC
     account_id  BIGINT         integer
     holder      VARCHAR        text
     balance     DECIMAL(12,2)  decimal
     opened      DATE           date
     status      VARCHAR        text
   count: 1,002 rows by the footers, a pass over the first column 0.0 s
   ```
3. Print the hashes of the files, as a run pins them.
   ```bash
   ds target check kit-files --table accounts_a/accounts.csv --format csv --manifest
   ```
   You should see, at the end:
   ```text
   f2868383482ab7db7a46e7ea789130531376c12c7e3e8cc93e08e179d6d37ea5  accounts_a/accounts.csv
   files_sha256 776f2358623e93dced5f50da758a673709d55f43d32edbb2214d5177cff1fdc5
   files_sha256 058fda81f896179e07f3e652ce18db640c08addc5065e8cd5bb3925ff1f82012 where the spec declares partitions (the lines above, whole)
   ```
4. Check the first of those lines on the machine itself, where the file is. On Linux the program is
   `sha256sum`; not verified here.
   ```bash
   shasum -a 256 practice/exports/kit/accounts_a/accounts.csv
   ```
   You should see the same 64 characters, from `f2868383` to `d6d37ea5`, then the file's path.
5. Copy the kit's spec to the hand-over folder and run it: the CSV export against the Parquet export.
   ```bash
   docker compose cp examples/tables/files/spec.yaml app:/var/lib/deltasleuth/p0/kit-files.yaml
   ```
   ```bash
   ds run /var/lib/deltasleuth/p0/kit-files.yaml
   ```
   You should see the two lines of `examples/tables/files/expected.json`:
   ```text
   the file reader started: duckdb 1.5.5 in process 56, data limit 5,296 MiB, sockets denied
   run R-35T7DN70PRM9NW6SWPBAX8ZZR9 started
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 1001, compared 1000, match 995, different 5, indeterminate 1, cancelled 0, outstanding 0
     INCONCLUSIVE 1, MATCH 995, MISMATCH 3, NOT_FOUND_A 1, NOT_FOUND_B 1
   ```
6. Now compare the CSV export with a database table: `file_accounts` of section 1. Write the spec.

```bash
cat > practice/csv-vs-postgres.yaml <<'YAML'
apiVersion: deltasleuth/v1
kind: ComparisonSpec

metadata:
  name: kit-csv-vs-postgres
  version: "1.0.0"
  owner: guide
  description: The files kit's CSV export against a PostgreSQL table.

normalizerVersion: "1.1.0"

key:
  name: account_id
  type: string
  class: business_code

sides:
  a:
    role: legacy
    target: kit-files
    table:
      name: accounts_csv
      keyColumns: [account_id]
      file:
        path: accounts_a/accounts.csv
        format: csv
        asOfFile: _AS_OF
        csv:
          columns: [account_id, holder, balance, opened, status]
          keys: {account_id: {as: integer}}
          delimiter: ";"
          header: true
    typeMap:
      balance: {as: decimal, decimalSeparator: ","}
      opened: {as: date, format: "%d/%m/%Y"}
  b:
    role: new
    target: kit-postgres
    table: {name: file_accounts, keyColumns: [account_id]}

compare: {}

recheck:
  delay: 0s
  times: 1
  outcomes: [MISMATCH, NOT_FOUND_A, NOT_FOUND_B]

execution:
  concurrency: 2

security:
  unlisted: tokenize
YAML
```

7. Copy it to the hand-over folder.
   ```bash
   docker compose cp practice/csv-vs-postgres.yaml app:/var/lib/deltasleuth/p0/csv-vs-postgres.yaml
   ```
8. Run it.
   ```bash
   ds run /var/lib/deltasleuth/p0/csv-vs-postgres.yaml
   ```
   You should see the two differences that section 1 planted in the table, account 7's balance and
   the missing account 9:
   ```text
   the file reader started: duckdb 1.5.5 in process 56, data limit 5,296 MiB, sockets denied
   run R-35T7DNZZXQ4BJZ52NQYCF3HKVH started
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 1000, compared 1000, match 998, different 2, indeterminate 0, cancelled 0, outstanding 0
     MATCH 998, MISMATCH 1, NOT_FOUND_B 1
   ```

### How to read the result

**The check's first line** says how the folder is mounted (`read-only`), which DuckDB reads the
files (DuckDB is the program built into the app that reads CSV and Parquet), and what limits its
process runs under.

**The layout of a CSV is declared, never guessed.** The draft of step 1 is a starting point to copy
into a spec, under `file:` and `typeMap:`, and to correct. A run reads only what the spec declares:

| In the spec, under `csv:` | Meaning |
|---|---|
| `columns` | Every field of a line, in the file's order. Required |
| `delimiter` | The character between fields. Required |
| `header: true` | The first line names the fields. It must name them as `columns` does |
| `encoding` | `utf-8` (the default), `utf-16` or `latin-1`. Other encodings, such as Windows-1252, must be converted first, the files document says |
| `newline` | `"\n"` (the default) or `"\r\n"` |
| `"null"` | The text, written without quotes in the file, that means "no value". The default is nothing between two delimiters, while `;"";` is an empty text |
| `keys` | How a key field reads: `{as: integer}` makes `000005` the key 5. Without it a key is text, and `000005` is not `5` |

Every field of a CSV file is text. Where the other side holds a number or a date, the side's
`typeMap` must say how the text reads, as for `balance` and `opened` above. The draft found the date
format. It cannot find a decimal comma, which the spec declares with `decimalSeparator: ","`.

Checked here with small files, against a table of three rows: a field left empty matched
a database NULL, and `""` matched an empty text; with `"null": "NULL"` the word `NULL` matched a
database NULL; a file in Latin-1 matched once the spec said `encoding: latin-1`, and without that
line its row with an accented letter was counted as a line that is not a row (`file 1 line 4:
encoding`).

**Pinning.** When a run is created, every file of both sides is hashed, and the run then reads only
those bytes. `--manifest` prints the same hashes: one line for each file, which the export's owner can
compare where the files are, as step 4 does, and `files_sha256`, one hash over all those lines, which
the files document makes the name of the export in an evidence pack. If a file changes before the
run ends, the run fails: it never reads half an old and half a new export. The stamp file `_AS_OF`,
which the spec names with `asOfFile`, is a small file that the export job writes last, holding the
export's time. A spec that names one is refused while the file is not there, so an export that is
not complete is not read.

**The kit's result.** Key 20 is missing in the Parquet file, key 1001 is only there, three rows
differ, and key 30 stands twice in the Parquet file: a file promises no unique key, so that key is
`INCONCLUSIVE`, and the Run screen's line says `1 repeated keys`. One Parquet row has no key and is
not compared.

**Against a table.** The Run screen's line for the last run names a file side and a database side:

```text
Table scan (done): 1,000 rows on A, 999 on B; 998 identical; 2 to compare; 0 not compared (NULL, empty or over-long key). Side A: files (csv), 1 files, 38,118 bytes, fixed. Side B: postgresql 16.14 (snapshot).
```

**The file reader.** DuckDB runs in a process of its own beside each process that reads files, so
that a damaged file ends that process and not the app. You see it in the line `the file reader
started: ...`. When it ends, a new one starts by itself. Tried here by killing the app's reader
between two runs: the app's log said `the file reader's process ended (SIGKILL) with no statement in
flight`, and the next run completed with the kit's counts. The reader document names what you see
when it ends in the middle of a read: a run asked for at that moment is refused with a line that
ends in `try again`, and files it ended on three times fail their run with a line that starts
`ReaderDied:` and are read again only after the app restarts. Not verified here: those two.
[READER.md](../mvp/READER.md) has the rest.

### Limits

- Observed with DuckDB 1.5.5 and files that DuckDB itself wrote. For Parquet from another writer
  (Spark, Hive, Impala, Arrow), run the kit and then a small export of your own, as the kit's
  instructions say. Files in object storage are not read: copy them into the folder first.
- Memory. The file reader may use 2 GB for its buffers (`files.memory`), in each process that reads
  files. The deployment document sizes the app's container at that plus 1 GiB.
- Disk. A scan sorts each side by key and writes what does not fit in memory to the app's data
  volume, at most 20 GB (`files.spill`). The files document says that a run whose sort would need
  more is refused when it is created. Not verified here: the kit is far too small.
- File sizes and time, from the files document and not tried here: one side reads at most 1,000
  files (`files.max_files`), and one pass over a side's files must take less than half the spec's
  `execution.timeouts.total`, which is 60 seconds unless the spec says otherwise. A large CSV needs
  a higher timeout there, or Parquet.
- One run reads one export. For a nightly export use a schedule, not a soak run: by the runbook a
  soak's later cycle fails once the files are replaced.
- Lines that are not rows, such as a trailer record, are counted and never compared. While side a
  has any, a key missing there is `INCONCLUSIVE`, not `NOT_FOUND_A`. Tried here with a copy of the
  CSV that ends in a line of two fields: the run printed `side a: 1 line(s) of its files are not
  rows: file 1 line 1002: columns`, the Run screen's line said `1 lines not rows`, and key 1001 became
  `INCONCLUSIVE`.
- Not supported on a file side: table pre-checks, which answer `not supported: a table pre-check
  with a file side; its run counts rows, lines that are not rows and repeated keys`, and, by the
  files document, `ILIKE`, key maps with a regular expression, graph parity, and `schema` in the
  spec's `table`.

### If something goes wrong

Each message was provoked here, but for one entry that says otherwise.

**`error: target kit-files: the directory of target kit-files is not there, or DeltaSleuth (uid 10001)
may not list it: is it mounted into this container?`**
The `volumes:` line is missing from the site file. A missing folder stops nothing else: with the
target declared and the folder not mounted, other `ds` commands ran, and only this target's check
said so.

**`error: target kit-files: the directory of target kit-files is mounted writable: mount it read-only
(:ro), or declare allow_writable: true on the target`**
The `volumes:` line lacks `:ro` at its end.

**`error: root of target bad-root must lie under /var/lib/deltasleuth/files (files.roots_under) and
outside the directories DeltaSleuth uses (evidence, workspace, spill, settings, targets, a secret):
mount exports in a directory of their own`**
The target's `root` names a folder elsewhere in the container, here `/srv/exports/kit`. Unlike a
missing folder, this stops every `ds` command until the entry is corrected. A `root` that ends in `/`
is refused too: `root must be an absolute path without . or .. segments or a trailing or doubled /,
and not /`.

**`error: side a: a file changed 3 s ago, inside the 60 s target kit-files waits (settle_s)`**
A file was written less than `settle_s` seconds ago. Wait, and start the run again. Provoked with
the default of 60 seconds and a file touched just before. The check's list then says `not settled
(settle_s 60)` in place of `settled`.

**`error: target kit-files: --table of a files target is a path and needs --format csv or parquet`**,
and **`error: target kit-files: no file matches nope/*.csv under target kit-files`**
The check needs `--format`, and a path that exists under the target's `root`. Put a path with `*`
in quotes, so that the shell leaves it alone.

**`error: target kit-files: no file matches accounts_a/accounts.csv under target kit-files`**, although
step 5 showed the file
The `volumes:` line of the site file names another folder than the one you wrote the export into.
Provoked here with `./practice/exports/kit` in place of `../practice/exports/kit`: Docker then made
an empty folder `deploy/practice/exports/kit` and showed that one to the app. Correct the line,
delete the empty folder `deploy/practice`, and run the check again.

**`error: side a: the first record of file 1 of 1 has 1 fields, not the 5 of csv.columns`**
The `delimiter` of the spec is not the file's. **`error: side a: the header of file 1 of 1 does not
name field 2 as csv.columns does`** means that a name in `columns` is not the one in the file's first
line.

**`error: side a: column 'balance' is text (a CSV field) and side b's a number (numeric(12,2)):
declare typeMap {balance: {as: decimal}} on side a`**
The CSV side lacks a `typeMap` entry for a column that is a number on the other side. For a key the
line reads **`side a: key column 'account_id' is text (a CSV field) and side b's a number: declare
csv.keys {account_id: {as: integer}}`**.

**`error: side a: column 'balance' is declared decimal and 1,000 of the first 1,000 rows do not read as
one: declare the format the export holds (typeMap format), or string`**
The values do not read as the declared type. For a number written with a decimal comma, as here, the
remedy is `decimalSeparator: ","` in the entry, which the message does not name; `format` is for
dates and times.

**`error: side a: the stamp file is not there: the export is not complete, or the spec names another
file`**
The file named by `asOfFile` is not in the export's folder.

**A run ends `failed` with `FilesChanged: side a: file 1 of 1 is not the file the run was created on
(size)`**
A file changed after the run was created. Start a new run on the new export. Provoked here by
creating a run while the app was stopped, adding a line to the file, and starting the app. `ds runs`
showed only the word `failed`. The reason was in the app's log (`docker compose logs app`), as `scan
of run R-... failed: FilesChanged: ...`, and in the run's data, from which the Run screen shows it
under the run's heading.

**`ds target check` prints `note: drafted from the lines of file 1 that read as rows: --count lists the
others`, or answers `error: target kit-files: no CSV dialect could be drafted from file 1: write its csv
block by hand (delimiter, header, quote, escape, newline, encoding)`**
The note says that the file holds a line of another shape, such as a trailer record: the check drafted
from the lines that are rows, also when `--delimiter ';'` is given, and `--count` lists the others. On
the kit's CSV with a trailer line it counts `1 line(s) not rows` and names `file 1 line 1002: columns`,
as a run does. The error says that the check found no layout at all, as for an export in another
encoding or one that holds no text: write the `csv:` block by hand from a look at the file (`head -3`).
Not provoked on this chapter's installation: both texts are from the product's tests, which draft the
kit's CSV with a trailer line and a small export in Latin-1.

**`error: invalid settings /etc/deltasleuth/deltasleuth.yaml: files.memory and files.spill are sizes
such as 2GB (MB, GB or TB); threads is 1 to 64, max_files 1 to 20,000, plan_s 10 to 86,400; spill_dir
and roots_under are absolute paths; os_limits is true or false`**
A value in the `files` block is not of its form. Provoked with `memory: 2G`.

## 6. Remove the practice servers and data

Do this when you are done with the chapter. The stored specs and the runs stay in DeltaSleuth and can
still be read. The steps also work when you did only some of the sections.

1. Put back the two files as they were before the chapter. If you changed either file for another
   reason since section 1, remove this chapter's lines in a text editor instead.
   ```bash
   cp practice/targets.yaml.before deploy/config/targets.yaml
   ```
   ```bash
   cp practice/site.yml.before deploy/docker-compose.site.yml
   ```
   If `cp` answers `No such file or directory`, the copies of section 1, step 4, were never made.
   Then remove this chapter's lines from the two files in a text editor before you go on: the
   targets whose names start with `kit-`, the names that start with `kit_` in the two `secrets:`
   places, and the `volumes:` line. Step 6 deletes the secret files that those lines name, and
   "If something goes wrong" below shows what Docker says when a line is left.
2. Apply them.
   ```bash
   docker compose --profile mocks up -d
   ```
3. Remove the two practice servers with their data. `-v` also removes the storage Docker made for the
   MySQL server. In a second installation, write your project name in the two names.
   ```bash
   docker rm -f -v deltasleuth-kit-mysql deltasleuth-kit-oracle
   ```
   You should see the two names. For a server you never started, Docker says `Error response from
   daemon: No such container: deltasleuth-kit-oracle` and removes the other.
4. Remove the practice database and its account from the installation's database server. The command
   prints nothing when it succeeds. If you never made them, it prints two lines that start with
   `NOTICE:` and end in `does not exist, skipping`.
   ```bash
   docker compose exec -T db psql -q -U deltasleuth -d deltasleuth -v ON_ERROR_STOP=1 -c 'DROP DATABASE IF EXISTS ds_kit WITH (FORCE)' -c 'DROP ROLE IF EXISTS ds_kit_reader'
   ```
5. Delete the copies of this chapter's specs and reports in the app's hand-over folder. They are
   small and harmless, and they lie in the Docker volume `deltasleuth_p0`, not in the installation's
   folder, so step 7 does not reach them. The command prints nothing, also when a file it names is
   not there.
   ```bash
   docker compose exec -T app sh -c 'cd /var/lib/deltasleuth/p0 && rm -rf kit-mysql.yaml pg-vs-mysql.yaml pg-vs-mysql kit-oracle.yaml pg-vs-oracle.yaml trap.yaml kit-files.yaml csv-vs-postgres.yaml'
   ```
6. Delete this chapter's secret files.
   ```bash
   rm -f deploy/secrets/kit_*
   ```
7. Delete its working files, with the practice export. Not verified here: on Linux, where the
   export's files belong to user 10001 and may need `sudo`.
   ```bash
   rm -rf practice
   ```

After this, a spec of this chapter cannot be run again, because its targets are gone. Tried here on
the command line: with the MySQL kit's spec copied to the hand-over folder once more, `ds run`
answered with one line for each side.

```text
error: /sides/a/target [unknown_target] target 'kit-mysql' is not in the registry
error: /sides/b/target [unknown_target] target 'kit-mysql' is not in the registry
```

In the web UI the specs of this chapter stay in the table of the "Specs" screen and under "Spec" in
the "New run" panel. Pressing "Start" on one of them shows only the words `invalid spec` under the
button. The app sends the two reasons above with those words, but the panel does not show them.

Do step 3 before you ever remove the whole installation (chapter 1, section 8). While a practice
server is attached to the installation's network, `docker compose down -v` removes everything else
and leaves the network, with the line ` Network deltasleuth_default  Resource is still in use`. After
step 3 the same command removed the network too.

### If something goes wrong

**`cp: practice/targets.yaml.before: No such file or directory`**, from step 1
The copies of section 1, step 4, are not there. Do what step 1 says for that case before you delete
anything.

**`Error response from daemon: invalid mount config for type "bind": bind source path does not exist:
/host_mnt/<folder>/IntelliDiff/deploy/secrets/kit_oracle_password`**, from every `ds` command, after a
line that says `secret file deltasleuth_kit_oracle_password does not exist`
The site file still names a secret whose file step 6 deleted. `<folder>` is where your installation
lies. Remove that name from both `secrets:` places of the site file, and its target from the targets
file, then run step 2. Provoked here by moving one secret file away. The running app went on
working, but `docker compose restart app` then failed with a long line that ends in
`deploy/secrets/kit_oracle_password: no such file or directory` and left the app stopped. Here the
file was put back, and step 2 started the app again. The `/host_mnt` at the start of the path is
Docker Desktop's on macOS.

## Reference

- [ENGINES.md](../mvp/ENGINES.md): MySQL and Oracle sides in full: the target entry, TLS, sessions
  and grants, how each type is read, and every message.
- [FILES.md](../mvp/FILES.md): file sides in full: the target, the spec's `file` block, CSV and
  Parquet types, pinning, and every message.
- [READER.md](../mvp/READER.md): the file reader's process, its limits, and what happens when it ends.
- [TABLES.md](../mvp/TABLES.md): comparing two tables: key columns, the scan, filters and type maps.
- [DEPLOY.md](../mvp/DEPLOY.md), section 7: a real MySQL or Oracle target and a real export folder:
  secrets, TLS modes, grants, firewall rules, what a scan costs the database, and measured times.
- [RUNBOOK.md](../mvp/RUNBOOK.md), "Across engines" and "From files": the operator's steps and the
  full tables of messages.
- The kits' own instructions: `examples/tables/mysql/README.md`, `examples/tables/oracle/README.md`
  and `examples/tables/files/README.md` in the source.
