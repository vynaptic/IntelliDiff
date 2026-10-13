# Table comparison (PostgreSQL, MySQL, Oracle and files): contract

This contract is binding. It follows [PLAN.md](PLAN.md): minimal, black-box, generic.
§1 to §12 say how a PostgreSQL table side is read; §13 says what a side on MySQL or Oracle changes, and
[ENGINES.md](ENGINES.md) is that part's contract, with what was observed and what was not; §14 says what a
side read from CSV or Parquet files changes, and [FILES.md](FILES.md) is that part's contract.

**Goal:** compare two tables or views, each in a PostgreSQL, MySQL or Oracle database or exported as CSV
or Parquet files into a mounted directory, key by key and column by column. It must scale to millions of rows. A table side is one more connector: the
engine (observe, compare, classify, root causes, masking), the reports, the UI and the AI chat stay
as they are.

## 1. Targets

A target with `dsn_ref` is a PostgreSQL database. A target with `base_url` is a service, and the
two are exclusive. A target with `engine` is a MySQL or Oracle database, which declares its endpoint
instead of a DSN (§13.1) and excludes both; `engine: files` declares a directory of exported files
(§14.1).

```yaml
targets:
  users-legacy:
    dsn_ref: file:/run/secrets/users_legacy_dsn  # a libpq keyword string or a postgresql:// URI
    rate_per_s: 200                              # per-key reads; a scan is one query per side
    max_concurrency: 2
```

**Rules for a PostgreSQL database target** (§13.1 has a MySQL or Oracle target's):
- It takes only `dsn_ref`, `rate_per_s` and `max_concurrency`.
- A relative `dsn_ref` resolves against the targets file, as `secret_ref` does. The DSN is read
  with `read_secret` when a connection opens. It never enters logs, evidence or errors.
- Some DSNs raise `SecretError("the DSN of target <id> is not a valid connection string")`
  before connecting, because libpq's own messages would quote part of them:
  - one libpq cannot parse;
  - a URI with more than one raw `@` before its first `/`, or any raw `@` after it. An unencoded
    `@` or `/` in the password turns its tail into a host, port or database name that connection
    errors name.

  Percent-encode `@` everywhere else.
- Its capabilities are fixed: `authoritative_absence: {status: [404]}`. The connector records a
  missing row as status 404 (§4).

**Every PostgreSQL connection runs `SET` for these settings first** (§13.3 has the other engines'):

| Setting | Value |
|---|---|
| `default_transaction_read_only` | `on` |
| `TimeZone` | `UTC` |
| `DateStyle` | `ISO, YMD` |
| `IntervalStyle` | `iso_8601` |
| `extra_float_digits` | `1` |
| `bytea_output` | `hex` |
| `application_name` | `deltasleuth` |

With these, both databases render the same value as the same text.

## 2. Spec

A table side replaces `request`/`select`/`map` with `table`:

```yaml
sides:
  a:
    role: legacy
    target: users-legacy
    table:
      schema: public            # optional: without it the search_path finds the name
      name: users               # a table, view, materialized view or foreign table
      keyColumns: [userid]
      columns: [email, {name: full_name, as: name}]   # optional: omitted = every non-key column
      where: "status = 'active'"                       # optional: a row filter (§9)
```

Key maps, type maps (§10) and `graph` ([GRAPH.md](GRAPH.md)) extend this form. A side on a files target
names its files in `table.file` (§14.2) and takes no `schema`.

**Schema changes** (`deltasleuth/core/spec/spec.schema.json` and its copy
`docs/architecture/spec.schema.json` stay identical):
- `tableSide.session` is no longer required.
- `table.columns` is no longer required (`minItems: 1` when present).

**Loader rules:**
- **Rejected with `unsupported`, each with a one-line reason:**
  - `template`;
  - `session`;
  - `collation`;
  - in `keyColumns`, a `part`, or a `keyMap` of kind `lookup` (§10);
  - `key.normalize` when a side is a table (keys are the tables' values as text);
  - a `compare.valueFilter` that reads the CEL variable `side`, between two table sides (the scan
    counts identical rows as matched, and a filter by side could tell them apart, §5).
- **Accepted with an `ignored` warning:** `compare.strategy` (two tables are always scanned by row
  hash, then read key by key).
- **A key listing** (`paginated_endpoint`) whose target is a database is a `target_kind` error.
- **Key columns:** both table sides list the same number of `keyColumns`. That number is 1, or
  `len(key.parts)` when `key.type` is `composite`.
- **Composite keys.** A composite key's text is its parts joined by `|`; inside a part, `\` and `|`
  are escaped as `\\` and `\|`. Between two tables it is judged whole. Beside a service side it is a
  multi-part key ([CONTRACTS.md](CONTRACTS.md) §4): each part is matched against `key.pattern` and goes
  into the service's request as `{{ keyParts.<name> }}`, and the table side lists one key column a part.
- **Mixed pairs** (one table side, one service side) are allowed. Their keys come from
  `key.source` or from the run.
- **A key table** is a `table` key source with a `target` and a `table` of its own, in place of the
  `side` a frame member names (CONTRACTS §11). Its `table` is judged as a side's, under
  `/key/source/table`, but takes no `columns` (`not supported: columns in a key table; it gives keys
  only`); its target is a database or a files target (`a key table needs a database target (dsn_ref or
  engine)`); between two table sides it is refused (`not supported: a key table between two table
  sides; their scan compares every row`).
- **Compiled form:** a table side compiles to `SideConfig.table` (schema, name, key columns,
  columns as `(column, canonical)` pairs or `None` when omitted, and the side's `where`, key maps
  and type maps). It gets
  `not_found = NotFoundRule((404,))` and `success_status = (200,)`, and no select or map.

## 3. Column plan

The plan is resolved by `create_run` and `compare_one`, and stored as
`runs.params["tables"]`. A run always reads with its plan, even if a table changes later.

**The shape beside the plan** ([PRECHECKS.md](PRECHECKS.md) §7). The schema guard records, in
`ds.runs.guard` and never in `params`, each table side's shape as the plan was made of it (the relation's
kind, its primary key, and each readable column's declared type, class and nullability), what the plan
reads each column as (`key`, `compared`, `filter`, `strata`) and the shape it last saw where that is
another. So `plan_sha256`, the stored form below and every statement are unchanged. A table that is no
longer what the plan reads (a planned column gone or unreadable, another class, a type within its class
that the side's engine reads otherwise, a key column whose key text would render otherwise, the relation
gone) pauses the run (`run_paused_schema_drift`) rather than being read by a stale
plan; any other change warns, or pauses under `schemaDrift: {mode: pause}`. `compare_one` records no
shape: it only warns.

**Per side:**
- **The relation:** `to_regclass` of the quoted name as spelled, else as PostgreSQL folds it
  (lower case). Its columns come from `pg_attribute` in `attnum` order, with `attnum > 0` and not
  dropped. If the relation is missing, or the role lacks USAGE on its schema, raise
  `ValueError("side a: table or view public.users not found or not visible")`.
- **The privilege:** if the role may read no column of the relation, raise
  `ValueError("side a: the role may not read public.users: grant it SELECT")`. If a key or compared
  column is not granted (`has_column_privilege`, for column-level grants), raise
  `ValueError("side a: the role may not read column 'secret' of public.users: grant it SELECT")`.
- **The connection:** if it fails, raise
  `ValueError("side a: cannot connect to target …: <libpq's first line>")`. libpq's line names the
  host and user, never the password (a DSN that could show it is refused first, §1), so a wrong DSN
  shows when the run is created.
- **Name matching:** a spec name matches a column exactly. Otherwise it matches the one column
  with the same lower-cased name (PostgreSQL folds unquoted names).
- **Key columns:** they must exist.
- **The side's map (canonical → column):**
  - `columns` given: its entries. A bare name gets its lower-cased name as the canonical name.
  - `columns` omitted: every non-key column under its lower-cased name. Two columns that
    lower-case to the same name are an error.

**Per pair (both sides are tables):**
- **Compared columns:** the canonical names present on both sides.
- **An explicit column without a counterpart is an error:**
  `side a: column 'email' has no counterpart on side b`.
- **Discovered one-sided columns** are not compared. They are listed as `only_a` / `only_b`.

**No compared column** on a table side (after the pair's intersection) is an error, because two
rows holding no value would be MATCH to the scan and INCONCLUSIVE per key:
`side a: no column to compare: list the columns to compare; a key column listed alone compares which keys exist`.
To compare only which keys exist, list the key column as the only column, e.g. `columns: [userid]`
on side a and `columns: [{name: user_id, as: userid}]` on side b.

**Stored form:**

```json
{"a": {"schema": "public", "name": "users", "keys": ["userid"], "key_maps": [null],
       "columns": {"email": "email", "name": "full_name"}, "where": null, "types": {}, "digest": []},
 "b": {"schema": "public", "name": "users_v", "keys": ["user_id"], "key_maps": [null],
       "columns": {"email": "email", "name": "name"}, "where": null, "types": {}, "digest": []},
 "only_a": ["legacy_flag"], "only_b": []}
```

`where`, `key_maps`, `types` and `digest` are the filter and maps of §9 and §10.

A pair with a table side on MySQL or Oracle stores more (§13.4): each table side's `engine`, `server`,
`consistency`, `tls`, and the `classes` and `declared` types of the columns it reads, and `rendering: 1`
beside `only_a` and `only_b`; there `digest` is every compared column the worker hashes. A pair of
PostgreSQL tables, and a PostgreSQL table with a service side, store the form above and nothing else,
so their `plan_sha256` is what it was.

`schema` and `name` are the catalogue's spelling. All SQL is composed with `psycopg.sql.Identifier`,
so no name is ever spliced into SQL text. On a PostgreSQL side a schema, relation, key, filter or
compared column name holding `%` is refused (the driver would read it as a placeholder): compare a view
that renames it.
A column on one side only is never read, so it is only listed, whatever its name or grant.

A mixed pair stores only its table side, with empty `only_a` / `only_b`.

A hash sample's `table` frame member ([SAMPLING.md](SAMPLING.md) §3) adds `strata` to that side's plan
only: each `strataColumns` name as written → its column, resolved and granted as filter columns are, so no
other run's stored plan changes.

## 4. Per-key read (`deltasleuth/connectors/tables/`)

`TableConnector` implements the `Connector` protocol, through its side's engine ([ENGINES.md](ENGINES.md)
§5): PostgreSQL's is `connectors/postgres/engine.py`, over the session and statements of this contract.

**Connection and politeness:**
- One connection per connector, opened lazily. Reads go through it one at a time.
- The target's `TargetGate` applies (rate, in-flight cap, and its breaker for the spec's
  `execution.circuitBreaker`), as for REST. The breaker admits
  a read through `connectors/base.py` `guarded()`, shared with `RestConnector`. A read then takes the
  connection's lock before the target-wide in-flight slot, so a read queued on the connection holds
  no slot.
- A per-key read sets `statement_timeout` to `execution.total_timeout_s`.

**The query** (`queries.rows_sql`), one arm per key; §12 joins a group's arms with `UNION ALL`:

```sql
(SELECT 0, to_jsonb(r.*)::text, coalesce((<where>), false) FROM "public"."users" AS t
 CROSS JOIN LATERAL (SELECT t."full_name" AS "name", t."email" AS "email") AS r
 WHERE t."userid" IN (%(k0_0_0)s) AND (t."userid"::text COLLATE "C") = %(c0_0)s
 ORDER BY 3 DESC LIMIT 2)
```

`r.*` is the whole row even when a column is named `r`. Key parts are bound as untyped text, so the
server casts them to the column's type and uses its index (verified with psycopg 3). The `IN` list
holds the side values a key map allows (§10); without a map it is the key part alone. Each key
part's canonical expression (§10) must also equal the key's part: the cast finds `7` for `'007'`,
whose text is `7`, a different key, so that row is not the key's (the scan never pairs them either,
§5). The third column is whether the row filter selects the row (`true` without a filter, §9).

**Row outcomes (each an `Observation`):**

| Result | Observation |
|---|---|
| 0 rows | `http_status=404`, `body=None` |
| 1 row | `http_status=200`, `body` = the UTF-8 JSON |
| 2 rows | the first when only it is selected (§9); else `error_class="identity_ambiguous"`, a new `ERROR_CLASSES` member; `observe` maps it to `INCOMPLETE(identity_ambiguous)` |
| A row over 64 MiB (`jsonio.DEFAULT_MAX_BYTES`) | `error_class="resource_limit"`, `body=None`, as for REST |

Every observation has `request_url = "postgresql://<target_id>/<relation>"`, with no credentials
(`mysql://` or `oracle://` on those engines, whose statement is §13.7's and whose errors are §13.8's).

**Errors:**
- **A key the column type or the database encoding cannot hold** (`psycopg.DataError`, a type's
  input `SyntaxError` such as ltree's, or `UnicodeEncodeError`, whose messages quote the key and are
  never recorded; `session.UNFIT`): raise
  `KeyRejectedError("the key does not fit the key column's type or encoding")`, so the key is
  INCONCLUSIVE(skipped).
- **`QueryCanceled`:** `"timeout"`.
- **Any other `OperationalError`:** `"connect_error"`. The connection is dropped and reopened on
  the next read. psycopg reports a failed login with no SQLSTATE, so it cannot be told from an
  outage. A wrong DSN shows when the run is created instead (§3).
- **Other errors** (a privilege revoked after the plan, a dropped table) propagate: the run fails
  with the error's type.
- A failed read is not retried, and its key lands in the retry list. Inside a batch (§12), a key
  that timed out is first read once more on its own, and a batch on a session the server ended is
  read once more on a new one.
- **On MySQL and Oracle** the server's codes say which of these a failure is (§13.8): a key the column
  cannot hold by its code, a refused login as `auth_error`, which pauses the run, and any other code as
  `connect_error` with the code as its detail, never the server's text.

## 5. Table scan (the key source for two table sides)

**When a run scans:** a batch or soak run (§11) over two table sides with neither keys passed with
the run nor `key.source`. Explicit keys, a `key.source`, `compare_one` and mixed pairs read per key.

**Store:** migration `003_table_scan.sql` added `runs.scan jsonb`; migration `004` moves it to one
`ds.scans` row per cycle (§11). `create_run` inserts the run with no keys, sets `status=running`,
and sets the first cycle's scan state to:

```json
{"done": false, "after": null, "first_at": null, "last_at": null, "rows_a": 0, "rows_b": 0, "skipped_a": 0, "skipped_b": 0, "matched": 0, "candidates": 0, "checkpoint": 0, "snapshots_a": 0, "snapshots_b": 0}
```

- `rows_a` / `rows_b` count every row read on the side, NULL-key rows included; `skipped_a` /
  `skipped_b` are the rows among them not compared (a NULL, empty or over-1024-byte key).
- `matched` and `candidates` count keys, not rows.
- `after` is the key the scan resumes after, sealed under the run's data key: `{"enc": "<base64>"}`,
  the JSON list of the key's parts sealed with label `scan_after` in the scan's cycle
  ([RETENTION.md](RETENTION.md) §3.1). It is null before the first checkpoint and once the scan is
  done, which resumes nowhere. It stays in the store: the API strips it.
- `checkpoint` counts the checkpoints saved; the compare-and-set below is on it.
- `snapshots_a` / `snapshots_b` count the snapshots each side's saved rows were read in, opened
  between `first_at` and `last_at` (UTC, when the scanner opened them), saved with the rows read in
  them, so a pass that saved nothing counts none. A graph spec's L2 adds `graph_at`, when it took its
  own snapshots. A cycle's census and an evidence pack's certificate state them (`read to its end in
  1 snapshot`, or a warning for several: [PACK.md](PACK.md) §1.2).

**Scanner (`deltasleuth/runner/scan.py`):** the worker's sweep starts one scanner task per running
run whose scan is not done and has no task in this process.

**Per side:** one `REPEATABLE READ READ ONLY` transaction (a consistent snapshot), with a named
server-side cursor fetched in batches of 10,000:

```sql
SELECT <k>, sha256(convert_to(to_jsonb(r.*)::text, 'UTF8')) [, to_jsonb(m.*)::text]
FROM "public"."users" AS t CROSS JOIN LATERAL (SELECT t."email" AS "email", ...) AS r
     [CROSS JOIN LATERAL (SELECT <digest columns>) AS m]
WHERE <k> IS NOT NULL [AND (<where>)] [AND (convert_to(<k>, 'UTF8')) > (%(a0)s)]
ORDER BY convert_to(<k>, 'UTF8')
```

`<k>` is each key column's canonical text: `t."userid"::text`, or its key map's expression (§10).
The `m` lateral holds the typeMap's digest columns (§10); the filter is §9's.

**The schema guard first (V2).** Each pass describes both relations on its own two sessions, inside
their snapshots, before either cursor opens, and `drift.check` judges them against the run's plan: a
change that breaks the plan pauses the run there (`drift.Tripped`), and the pass ends without failing it
or reading a row. A pass from the cycle's start records `where: scan`, one that resumes after a checkpoint
`where: resume`. Its cost is one catalogue statement a side ([PRECHECKS.md](PRECHECKS.md) §12 measures
it).

A MySQL or Oracle side streams the same rows, its key texts and one digest, from §13.7's statement, in
one session of its engine. In a pair with such a side a PostgreSQL side keeps the statement above, every
compared column a digest column, and its rows take rendering 1's form before the worker hashes them.

Both sides sort by their key texts' UTF-8 bytes. UTF-8 bytes sort as code points, which is how the
merge compares keys, so the order holds in every database encoding. A "C" collation would sort a
WIN1252 or EUC_JP database's own bytes, and those disagree with code points.

**Keys are compared as text.** Key columns must render the same text on both sides. An `integer`
key against a `numeric(10,2)` key (`1` against `1.00`), or a `uuid` against upper-case text, never
pairs up: every such row is one-sided on both sides. A per-key read on the other side agrees
with 404 when the key text casts to that column's type (`1` against numeric: its text is `1.00`),
and is INCONCLUSIVE(skipped) when it does not (`1.00` against integer, §4). The only fix is to
compare views that cast the key.

**Merge:** a synchronous loop over the fetched batches, awaiting only to refill a batch. There is
never a task or a `gather` per row: that costs 16× (measured 47 s against 2.9 s for 1M rows a
side). Keys are compared as tuples of text, in code-point order, which is the order both sides
return them in.
- **Matched:** the key is on both sides with equal hashes. It is counted only.
- **Candidate:** the hashes differ, the key is on one side only, or a side repeats the key. It is
  inserted into `run_keys` (pending, due now, `seq` = candidates so far, masked key), so the
  worker's slots read it per key (§4) while the scan goes on.
- **Skipped:** a key text that is empty or over 1024 bytes (composite keys after encoding). Its
  rows are counted in `rows_x` and `skipped_x` and not compared.
- **Out of order:** a side whose key comes below the one before it, or whose first key is not after
  the resume point, fails the run (`KeysOutOfOrder`, [ENGINES.md](ENGINES.md) §8): a mis-sorted side
  never mis-pairs a key.

**Checkpoint:** after every 10,000 merged keys and at the end, one store transaction does both of:
- updates `scan` (counters, `after` = the last merged key's parts, sealed, or null at the end, and
  `checkpoint + 1`) `WHERE status = 'running'` and `checkpoint` is still the one this scanner built on
  (compare-and-set on the number: `repo.save_scan(..., saved_checkpoint)`, `saved` being the state's
  `checkpoint` when the scanner read it; a re-sealed `after` never compares equal, having a new nonce);
- inserts the batch's candidates, their raw keys sealed.

If the run is no longer running, or another scanner saved since (a worker lock handover), nothing
is saved and this scanner stops quietly. A resume merges that batch again.

**Resume:** the scan opens `after` and restarts after it with fresh snapshots, so nothing is counted
twice; each pass adds one to `snapshots_a` and `snapshots_b`. Rows added behind the resume point
meanwhile are not read, which is why a pack's census names the snapshots.
This covers a worker restart, pause then resume, and a lost target connection. The bound compares
the same expression the sides sort by with `after`'s parts bound as UTF-8 bytes. So it holds in
every database encoding, even after a key the other database cannot encode.

**End:**
- Rows of the filtered population with a NULL key part, or a value its key map does not match, are
  counted per side (`count(*) ... WHERE (<k1> IS NULL OR ...) [AND (<where>)]`) into `rows_x` and
  `skipped_x`.
- `done` becomes `true`.

**Stop conditions:**
- `stop.maxKeys` caps candidates. On reaching it the scan saves its state as done, the run's stop
  reason becomes `stop_condition:maxKeys`, and the run goes on reading the keys found.
- `stop.until` and `stop.maxDuration` are judged after each checkpoint. When one is reached,
  `scan()` returns its reason and the worker stops the run (`stop_run`: `cancelling`, then
  `completed_with_issues`, stop reason `stop_condition:until` or `stop_condition:maxDuration`). The
  scan stays not done.

**Failures:**
- **`OperationalError` from a target:** log it, wait `worker.lease_seconds`, and end. The sweep starts
  the scan again, resuming from `after`. That includes a sort the server refuses (`temp_file_limit`:
  `ConfigurationLimitExceeded`) or cannot spill (a full disk: `DiskFull`), which every resume meets
  again: such a scan retries without end while its run shows `running` ([BENCHMARK.md](BENCHMARK.md)
  §16). With stock settings each side's sort spills 61 to 63 bytes a row for a `bigint` key (its texts
  and the row hash, whatever the row's width), held until the scan ends, and a link fact's L1 and L2
  sorts write about 201 to 211 bytes in two phases, at most 107 to 121 of them held at once: L1's files go
  when its snapshots close, before L2's sorts begin ([BENCHMARK.md](BENCHMARK.md) Results: one stock
  PostgreSQL 16 in Docker on an Apple M3 Max, 4-column rows at 1M and 10M a side and 24-column rows at 1M).
- **Anything else** (privileges, a dropped table, a bug), and `ProgramLimitExceeded` (data past a
  server limit, which every resume would meet again): `fail_run`, which shows the error's type
  only. A read refused with `UndefinedColumn`, `UndefinedTable`, `InsufficientPrivilege` or
  `TargetDenied` (a column dropped while the pass read) first has the schema guard look at the run's
  tables (V5, [PRECHECKS.md](PRECHECKS.md) §7): a change that breaks the plan pauses the run instead;
  otherwise, or when the look cannot be made, `fail_run` as before.
- **A MySQL or Oracle side's failure** is one of §13.8's classes, by the server's code: an unreachable
  server is waited out as above, without limit; a lost snapshot or any other code resumes, at the next
  sweep when the pass saved a checkpoint and a lease period later when it saved none (a stalled pass:
  the worker counts them in memory), and the third stalled pass in a row fails the run; a refused login pauses the run; temporary space exhausted, a denied or missing
  relation and a target whose engine changed fail it, showing `<class>: <code>`.

**Cycle end:**
- `repo.cycle_done` is also false while the run is running with a scan not done.
- `summarise` counts the cycle a page of results at a time (`counters.Tally`, which `count_cycle` is over
  a list) with `total=keys + scan.matched, bulk_match=scan.matched`. The scan's matched keys
  count in `total`, `match`, `by_outcome.MATCH` and `by_confirmation.bulk_exhaustive`, and every
  identity still holds.

**A hash sample's frame** ([SAMPLING.md](SAMPLING.md) §3) is not a scan: a table side that a
`hash_sample`'s `table` member reads is streamed once, by the draw, in one `pg.snapshot` with a named
cursor, through `queries.frame_sql(side)`: the key census's canonical key parts, key maps, filter and
order, then the plan's `strata` columns as one JSON document (`to_jsonb` of a lateral `SELECT`, `'{}'`
without strata), which breaks ties in `COLLATE "C"` so a key's first row is always the same one.
`null_keys_sql` counts `skipped`. A hash sample between two table sides is refused: their scan compares
every row.

**Soundness:** equal hashes mean equal JSON text of the same canonical columns. Both sides are then
canonicalised identically: table sides have no per-side select or map, normalisers are shared, and
a `valueFilter` that reads `side` is refused (§2). The compared set is never empty (§3). So the
per-key engine would answer MATCH. The only exception is a row whose identical document fails
canonicalisation on both sides (a parse limit), which counts as MATCH here.

## 6. Reads, reports, UI

- **Reader:** `LiveReader(ctx, spec, params["tables"])` builds a `TableConnector` for a database
  target and a `RestConnector` otherwise (`runner.compare.connect`). The worker passes the run's
  plan.
- **Captured samples** (file mode) replay service sides only: a run with a table side and a samples
  directory is refused.
- **Coverage CSV:**
  - `selected_fields` lists the compared columns of a table run.
  - New columns: `rows_a`, `rows_b`, `rows_skipped_a`, `rows_skipped_b`, `columns_only_a` and
    `columns_only_b`. They are empty for service runs.
  - `engine_a` and `engine_b` (§13): each table side's engine, `postgresql` for a plan that names
    none; empty for a service side.
  - `rows_rejected_a`, `rows_rejected_b`, `keys_repeated_a` and `keys_repeated_b` (§14): a file side's
    lines that were not rows and, once scanned, its keys held by more than one row; empty for every
    other side.
- **PDF summary:** one "Tables" line with the same facts; a side whose plan names its engine reads
  `side a ledger.users (mysql)`, a file side `side a customers (files: csv, 12 file(s), 3 line(s) not
  rows, 5 repeated key(s))` (the last once it was scanned).
- **UI run page:** while a scan runs, and after it, one line gives the rows read per side, the
  identical keys, the keys to compare and the rows not compared. It also lists the columns only on
  one side and, where the plan names them, each side's engine, server version and consistency
  (`Side A: oracle 19.21 (snapshot).`); a file side reads `Side A: files (csv), 12 files, 3,650,722,201
  bytes, fixed.`, then `3 lines not rows.` and `5 repeated keys.` when there are any.
- **The schema guard** ([PRECHECKS.md](PRECHECKS.md) §10, §11): `GET /v1/runs/{id}` answers a
  `schema` block whose `lines` are the server's words (`no change observed against the run's plan at its
  creation and each cycle's start (table sides only; …)`, or where it looked once a look could not be
  made, each change, a pause and how to end it); the
  Run screen prints them, the first as a banner while the guard holds the run paused, and names a resume
  that would accept the changes `Resume (accept the changes)`; `ds runs` prints the pause's line under
  such a run. `report.pdf` adds one sentence counting the run's `schema.drift` rows up to its cycle. No
  CSV of a run changes, and no pack file.
- **Table pre-checks** ([PRECHECKS.md](PRECHECKS.md)): before any run, `POST /v1/prechecks`, `ds
  precheck run` and the Pre-checks screen read each stored table pair's (or two schemas paired by name)
  catalogues and a few aggregate statements in one snapshot a side, and say where to look first. A
  pre-check is no run: it compares no row, is no evidence, and no pack holds it.

## 7. Tests (throwaway PostgreSQL, `tests/conftest.py`)

- **Unit:**
  - plan resolution: explicit, omitted, rename, folding, missing relation, missing key column, no
    counterpart, collision, no column to compare, no SELECT, no USAGE, a DSN libpq would quote;
  - composite key encoding round trip;
  - connector: row, 404, ambiguous, rejected key, lost connection;
  - merge: matched, changed, one-sided, repeated, skipped, and an EUC_JP database in code-point
    order;
  - `count_cycle` with `bulk_match`;
  - loader gating and schema changes.
- **E2E:** two databases on the test server, tables and a view, seeded with:
  - identical rows;
  - a changed value;
  - a scale-only numeric difference (per-key MATCH);
  - rows missing in B and extra in B;
  - NULL keys;
  - a view repeating a key;
  - a one-sided column.

  The scan run's counters and findings must be exactly those. Also cover: a kill and resume
  mid-scan with no double count, pause and resume, `compare_one`, explicit keys, a composite key,
  the key column listed alone, a column named `r`, a time limit, and a resume past a key the other
  database's encoding cannot hold.
- **Engines** (throwaway MySQL and Oracle, markers `mysql` and `oracle`; [ENGINES.md](ENGINES.md) §16):
  the same dataset in every pairing of the three engines, in one engine with the engine and with the
  worker hashing, and against a service; a golden corpus of one column per type with edge values
  (`tests/golden/engines/`); golden statements per engine; every server code to its class; and the
  acceptance kits of `examples/tables/{mysql,oracle}/` against their expected results.
- **Performance** (opt-in, `DS_PERF=1`,
  `tests/e2e/test_tables.py::test_a_million_rows_a_side_finish_in_a_minute`): 1M rows a side finish
  scanning in under 60 s on a laptop. The measured time is 6.8 s for the whole run, per-key reads
  included. That 6.8 s stays this test's measurement: [BENCHMARK.md](BENCHMARK.md)'s 1M `sparse`
  (6.96 s, 6.95–6.96, on an Apple M3 Max: 4 columns, 11 differing keys, warm cache, worker poll 0.05 s) is
  another profile, not a re-measurement (its §7 notes list the differences).

## 8. Docs

- **PRECHECKS:** table pre-checks and the schema guard, built on this contract's plan and scan.
- **RUNBOOK:** a "Compare two tables" section, with "Triage with table pre-checks" and "When a table
  changes under a run".
- **DEPLOY:** a database target, its DSN secret, and a read-only role.
- **CONTRACTS:** this summary.
- **PLAN:** PostgreSQL table sides move from deferred to scope.
- **`examples/tables/`:** a spec, target entries and seed SQL; `examples/tables/mysql/` and
  `examples/tables/oracle/` are the acceptance kits (§13).

## 9. Row filters (`table.where`)

A table side may restrict its population with `where`, a predicate over that side's columns.
Other sides are unaffected.

```yaml
table: {name: customers, keyColumns: [customer_id], where: "status IN ('active', 'dormant') AND country = 'GB'"}
```

**Grammar** (`deltasleuth/core/spec/where.py`, the loader's `invalid_where` error). Keywords are
case-insensitive.

```
predicate  := disjunct ("OR" disjunct)*
disjunct   := conjunct ("AND" conjunct)*
conjunct   := "NOT" conjunct | "(" predicate ")" | comparison
comparison := column (op value | "IS" ["NOT"] "NULL" | ["NOT"] "IN" "(" value ("," value)* ")"
                      | ["NOT"] "BETWEEN" value "AND" value | ["NOT"] ("LIKE" | "ILIKE") string)
            | column                                  -- a boolean column
op         := "=" | "<>" | "!=" | "<" | "<=" | ">" | ">="
value      := string | number | TRUE | FALSE
column     := identifier | "quoted identifier"
```

A column is always on the left and literals on the right. There are no functions, subqueries,
casts or column-to-column comparisons. Parentheses and `NOT` nest at most 100 deep, and a `LIKE`
or `ILIKE` pattern may not end in an escape (`\`), which PostgreSQL refuses only on reaching a row.
The plan also requires a `LIKE` or `ILIKE` column to be text in a deterministic collation (before
PostgreSQL 18, LIKE refuses a nondeterministic one, again only on reaching a row).

**Plan.** Each column resolves as a `columns` entry does (as spelled, else folded) and must be
readable, else a `ValueError` names the side and the column. The plan stores `where` re-written
with the resolved, quoted column names; reports print the filter as the spec writes it.

The plan also runs a probe: each side's filter and key maps planned and bound with `LIMIT 0`, so it
reads no row and no data value can reach its message, plus one `regexp_replace('', …)` per regex key
map to compile its pattern. A bad pattern or a literal of the wrong type is then refused when the
run is created, as `side a: the database refuses the filter or key map: …`, even for an empty table.

**SQL.** The predicate is composed with `psycopg.sql`: columns become `t.<Identifier>`, and every
literal becomes a named placeholder bound as text. The server converts the text to the column's
type, so `amount > 10` compares numbers when `amount` is numeric.

**On MySQL and Oracle** a literal is typed by the class of the column it is compared with, in Python,
because neither engine's zero-row probe refuses a literal of the wrong type: a number, a date, a
timestamp, a time or bytes (`\x` and hexadecimal digits) that is not of that form is refused when the
run is created, as `side a: the filter compares column 'amount' (NUMBER(10,2)) with a literal that is
not a number`; `TRUE`, `FALSE` and a bare column need a boolean column (Oracle 23's `BOOLEAN`; `side a:
the filter tests column 'flag' (tinyint(1)) as a boolean: compare it with 1 or 0`). On MySQL a literal
is written into the statement as a number or as the hexadecimal digits of its UTF-8 bytes; on Oracle it
is a bind variable. A literal compared with an Oracle `CHAR` column is bound inside `CAST(:w AS
CHAR(2000))` (`NCHAR(1000)` for an `NCHAR`): a plain bind selects no row there, and a cast to the
column's own length would cut a longer literal; one over 2000 bytes is refused. Each database compares
as it does for any client: MySQL by the column's collation (`status = 'active'` also selects `ACTIVE`
on a case-insensitive column), Oracle by bytes, and an empty literal is NULL to Oracle, so its
comparison selects no row. `ILIKE` is refused on these sides when the spec is loaded (`not supported:
ILIKE on a MySQL or Oracle side; use LIKE`); `LIKE` keeps each server's own semantics, with `\` as its
escape.

**Where the filter applies:**
- **The scan:** `WHERE <keys not null> AND (<where>)`, and the same for the null-key count and
  graph queries. The population is the rows the filter selects.
- **Per-key reads:** a per-key read does not exclude rows outside the filter. It reads the
  row by key and also returns `coalesce((<where>), false) AS selected`; of two rows with the key it
  reads the one the filter selects, and only two rows alike are `identity_ambiguous` (§4). A row outside the filter is recorded with
  `headers = {"selected": "false"}`, and `observe` sets `SideState.selected = False`.
- **`compare_pair`:** when both sides are COMPLETE and exactly one is selected, the comparison
  runs as usual and adds one finding. It has code `FILTER_MEMBERSHIP` (MEDIUM), pointer `""`,
  path `$`, change type `value_changed`, and the two `selected` values. A key that left one side's
  population therefore reads as `FILTER_MEMBERSHIP` plus its value differences, never as
  `NOT_FOUND`. A missing row is still `NOT_FOUND`.
- **Recheck:** a side's `selected` is part of what the recheck compares. A entering or leaving its
  filter is DRIFT(a_changed) even with unchanged values; B doing so is b_converged, or
  intermittent_b while something still differs.
- **Reports:** the coverage CSV gains `filter_a` and `filter_b`, the predicates as written. The
  PDF "Tables" line also shows them. The Run screen's scan line shows the plan's form (from
  `runs.params`).

## 10. Key and type mapping

### Key maps (`keyColumns: [{name, keyMap}]`)

A key column may map its values to the canonical key; `part` stays unsupported. The kinds are
those of the schema, except `lookup`, which stays `unsupported` because it needs SQL templates.

| Kind | Canonical from side (SQL: scan, resume and ordering) | Side from canonical (Python: per-key reads) |
|---|---|---|
| `identity` | `t.k::text` | the key part as is |
| `prefix_pad` `{prefix, width, sideType}` | `prefix \|\| lpad(t.k::text, greatest(width, length(t.k::text)), '0')` when `t.k::text ~ '^[0-9]+$'` | prefix removed; the digits must be ASCII and at least `width` long. Exactly `width` digits: every value `lpad` restores to them, i.e. the digits with any number of their leading zeros removed (`"0"` for all zeros). Wider: only the digits as they are. `sideType: integer` keeps the values an integer can render (no leading zero). A text key stored more than one way selects two rows and is `identity_ambiguous`. |
| `regex` `{toCanonical, toSide, sideType}` | `regexp_replace` of the whole value when it matches `^(?:pattern)$` (`$n` becomes `\n`; `\` is doubled, `&` stays literal) | `re.fullmatch(toSide.pattern)`, then expand `$n`; with `sideType: integer`, only a value an integer renders (`0`, or no leading zero) |

**On MySQL and Oracle** the kinds are `identity` and `prefix_pad`; a `regex` key map is refused when the
spec is loaded (`not supported: a regex key map on a MySQL or Oracle side; compare a view that derives
the key`). A column's key text is the text PostgreSQL's `::text` gives the same value (§13.5), and
`prefix_pad`'s digits test is written without `$`: MySQL `k <> '' AND k NOT REGEXP '[^0-9]'`, Oracle
`LTRIM(k, '0123456789') IS NULL AND k IS NOT NULL`.

**Rules:**
- **Constants** (prefix, width, patterns, replacements) are bound as named parameters
  (`m<j>_prefix`, `m<j>_width`, `m<j>_pattern`, `m<j>_replacement`), never spliced: a `%` in a
  constant would read as a placeholder.
- **Regex replacements** may reference only `$1` to `$9` (PostgreSQL has no more), and only groups
  the pattern has (`invalid_key_map` at load).
- **An unmatched value:** a side value its map does not match has no canonical key (NULL). It is
  excluded from the scan and counted in `skipped_x` with the NULL-key rows.
- **An unmappable key:** a canonical key a side cannot map back (the pattern does not match, or
  `sideType: integer` and the mapped value is not how an integer renders) cannot exist on that side. The read
  returns 404 without a query.
- **Exact text:** every canonical expression is in the `"C"` collation, so keys compare, group and
  match exactly as the Python merge does, even on a column whose collation is nondeterministic
  (case-insensitive `X1` and `x1` stay two keys). An `IN` list on the raw column still uses its
  index.
- **The canonical key** is the key the run stores and shows. The scan orders and resumes on the
  canonical expression's UTF-8 bytes, so both sides still merge in one order.
- **Regex dialects:** patterns are checked with Python's `re` at load (`invalid_key_map`).
  `toCanonical` runs in PostgreSQL (ARE) and `toSide` in Python, so keep to their common syntax:
  anchors are implied, plus groups, classes, quantifiers and `\d \w \s`.

### Type maps (`sides.<x>.typeMap: {<column as spelled on this side>: typeSpec}`)

A mapped column is converted to its canonical type in Python (`deltasleuth/core/canonical/convert.py`),
the same way for the scan's row digests and for per-key rows. NULL stays null for every type.

| `as` | Accepts | Produces |
|---|---|---|
| `string` | anything | a string: numbers as exact decimal text, booleans as `true`/`false`, objects and arrays as canonical JSON text; with `emptyAsNull: true`, null for an empty string |
| `integer` | an integral number, or text holding one (surrounding spaces allowed) | a number |
| `decimal` | a number, or numeric text; `scale` rounds half to even | a number |
| `boolean` | a boolean; text or numbers found in `trueValues` / `falseValues` (compared as text). Without the lists: PostgreSQL's boolean words (`t`, `true`, `y`, `yes`, `on`, `1` and their false forms), any case. | a boolean |
| `date` | ISO date or timestamp text. An instant with a zone gives its date in `naiveTimezone` (default UTC). | `YYYY-MM-DD` |
| `timestamp` | ISO date or timestamp text (`T` or a space; `Z` or an offset). A naive value is read in `naiveTimezone` (default UTC); a date is midnight there. The value is truncated to `precision` digits (default 6). | `YYYY-MM-DDTHH:MM:SS[.fff…]Z` in UTC |
| `json` | JSON text (exact decimals), or any JSON value as is | the JSON value |
| `bytes` | PostgreSQL's `\x…` hex text, any case | lower-case `\x…` |

`emptyAsNull` is valid with `as: string` only (`invalid_type_map` otherwise). It bridges Oracle, where
an empty string is NULL: declared on the other side's column, `''` there and NULL on Oracle match, in
the scan's digests and in per-key rows alike ([ENGINES.md](ENGINES.md) §9). It bridges a CSV side the
same way, whose unquoted empty field is NULL (§14.2).

Two members, on any table side ([FILES.md](FILES.md) §5), read text as an export writes it. `format`,
with `as: date` or `as: timestamp`, is parsed whole by `strptime`: the directives `%Y %m %d %H %M %S %f
%z` and the characters `- / . : T` and space, nothing else (`%y` would need a pivot year, `%b` a
locale). `decimalSeparator: ","`, with `as: decimal` or `as: integer`, reads `1,50` as 1.50; a text
holding the other separator does not convert, and group separators are not read; `decimal` and
`integer` read a number's ASCII form only (no underscore, no digit but 0 to 9). Either with another
`as` is `invalid_type_map`.

**Validation and plan:**
- The loader checks the schema, and that `naiveTimezone` names an IANA zone (`invalid_type_map`).
  A typeMap column must be one of that side's compared columns
  (plan: `side a: typeMap column 'x' is not compared`).
- The plan stores each side's maps by canonical column name.

**Per-key reads.** The connector parses the row's JSON, converts the mapped columns, and records
the converted row, re-serialised exactly (`jsonio.dumps`), as the observation's body. A value
that does not convert gives `error_class = "unconvertible"` (a new `ERROR_CLASSES` member) with
no body. Its `error_detail` names the column and the type, never the value. `observe` maps it to
`INCOMPLETE(unparseable)`.

**The scan.** A column mapped on either side is a digest column on both sides, and the side
without a map converts it with the identity. Each side selects:
- the key texts;
- `sha256` of the unmapped columns' `to_jsonb` text;
- `to_jsonb` of the digest columns.

The row digest is `sha256(sql_hash + canonical_json(converted digest columns))`, with
`jsonio.dumps` and sorted keys. Without digest columns, the digest is the SQL hash, as before.

A conversion failure gives that side's row a digest that never matches, so the key is read per
key and ends INCONCLUSIVE(unparseable).

Soundness still holds: equal digests mean identical unmapped JSON and identical converted values.

## 11. Scheduled table re-diffs (a soak over a scan)

A soak over two table sides, with neither keys nor `key.source`, scans both tables anew every
cycle. `execution.cycleInterval` is required, as for any soak, and `soak.rolling` has no effect.

**Store** (migration `004`):
- `ds.scans (run_id, cycle, state jsonb, PRIMARY KEY (run_id, cycle))` holds each cycle's scan
  state, and replaces `runs.scan`; existing values move to the run's current cycle.
- `repo.get_run` / `list_runs` still return `scan`, the current cycle's.
- `save_scan` and `cycle_done` work on the cycle's row.
- Reports of an ended cycle recount with that cycle's `matched` (`bulk_match`).

**Cycle end.** For a soak scan, `finish_cycle` starts the next cycle with no keys. It inserts that
cycle's scan state `new_scan() | {"due": <next start, ISO UTC>}`. The sweep starts a scan only
once `due` has passed.

**Keys that differed last cycle.** At scan start the scanner loads the keys whose result in the
previous cycle is not MATCH (`repo.unmatched_keys`). When one of them is identical this cycle, it
is queued anyway, so its return to MATCH is read, stored, and listed by `outcome_changes`. A key
that returned to MATCH is not queued again the cycle after, so the re-read set does not grow with
every key that ever differed. A key that was identical last cycle and
differs now has no earlier result. It shows as a discrepancy of this cycle, not as an outcome
change.

**Stop conditions** at cycle boundaries are unchanged (`maxCycles`, `until`, `maxDuration`).

**Against a cron schedule** ([SCHEDULES.md](SCHEDULES.md)). A soak's `execution.cycleInterval` repeats
one run's cycles at a fixed interval from when the run started, as often as the interval allows. A
schedule starts a new run at wall-clock times in a time zone, at most every 15 minutes of that clock,
each a batch scan or a soak of its own. A table pair re-diffed nightly at 02:00 local time is a schedule
of batch runs; one re-diffed every five minutes for an afternoon is a soak. A scheduled soak must end by
itself (`execution.stop.maxCycles`, `maxDuration` or `until`): while it is live the schedule starts no
other run.

## 12. Batched per-key reads

**Groups.** A worker slot processes a group of keys of one run. A run whose live sides all read
tables takes up to `READ_BATCH = 200` keys; any other run takes one, exactly as before, but a mixed pair
whose table side reads files (§14.6): its group's keys are read from the files in one pass and from the
service one by one.
`repo.lease_due` gains an optional `run_id` filter. The slot leases one due key, then up to
`READ_BATCH - 1` more of that run, all under the slot's lease owner, so renewal and release cover
the group.

**Processing a group:**
1. **Earlier reads:** the recheck keys' first results and observations are read in one query per
   table, then re-derived.
2. **Prefetch:** `reader.prefetch(group)`. `TableConnector.prefetch` reads every key of the group
   with one statement per side. That statement is a `UNION ALL` of one indexed lookup per key
   (`WHERE t.k IN (<the key's possible side values>) LIMIT 2`, bound untyped), each tagged with the
   key's position and selecting the row and `selected`. When one key's value does not fit its
   column, the batch is halved until that key stands alone, and `fetch` then rejects it; every
   other key is still served from a batch. When the batch runs past the per-key timeout, each key
   that timed out is read again on its own, each read admitted by the breaker and costing a
   rate-budget token: a slow key costs two timeouts, never a halving cascade. It caches one `Observation` per key, built as
   `fetch` builds it, and `fetch` takes a cached one first. Each group's prefetch first drops what an
   earlier group left unread, so a stale read is never served. Keys that cannot map back are 404
   without a query. Recheck keys read side B first, as now.
3. **Evaluation:** `evaluate_many`. The group's reads come from the cache, then the evidence and
   the judgement of all its keys happen in one thread call. Auth failure, stop
   conditions and recheck scheduling are per key, as now. A refused credential hands back the
   whole group.
4. **Saving:** one store transaction per group, with the same SQL batched: `executemany`, which
   psycopg sends as one pipeline. It holds the observations, the results with their findings, and
   each key's move to done or to a recheck. If any key's lease was lost, the whole group rolls back
   and its keys are read again later.
5. **Politeness:** the group holds one lane (`execution.concurrency`). `execution.delay` applies
   after each group. A group costs one statement and one rate-budget token per side for its first
   reads, and one more for its recheck keys; each halving step, each timed-out key's solo re-read,
   a re-read after a lost session and a rejected key's solo read cost one more each.

**A scanned key is confirmed by its per-key read** (architecture §8.3: bulk runs need no recheck).
The scan read the key once; the per-key read reads it again, later. So when a run's keys come from
a scan, a first-pass result the recheck policy would re-read is final, with confirmation
`rechecked`, and no further pass is scheduled. An unusable read (INCONCLUSIVE) confirms nothing:
it is rechecked as the policy says. A key that stopped differing in between reads as
it is now. Runs with keys, or with a `key.source`, recheck as before.

**`jsonio.dumps`** compiles one pattern at import. The pattern matches any placeholder nonce, and
a match is replaced only when its nonce is this call's. Before, each call compiled a new regex.

**Measured** with the opt-in benchmark in `tests/e2e/test_tables.py` (`DS_PERF=1`): 20,000
differing keys of 100,000 rows. Run it on its own (`-k batches`): right after the million-row benchmark, the
shared test server is still busy and it takes about twice as long.

| Stage | Time |
|---|---|
| Before these changes (with a recheck pass each) | 205 s |
| After (1.3 s scan, 35 s per-key reads, 6.3 s cycle end) | 43 s, 4.7× faster (42.1 s re-measured) |

The remaining cost per key is spread across:
- the evidence (two blobs, each written, synced and verified);
- the engine (observe, compare);
- the save.

The cycle end re-derives the discrepant results for root causes.

**At scale** ([BENCHMARK.md](BENCHMARK.md) Results: an Apple M3 Max, one stock PostgreSQL 16 in Docker holding
both sources and the store, synthetic 4-column rows, warm cache, worker concurrency 4 and poll 0.05 s,
`execution.concurrency: 8`, budgets lifted), with 1% of keys differing (every 100th, or 10 blocks), per-key
reads ran at 627 (558–696) and 670 (546–794) keys/s at 1M rows a side (both sizes flagged noisy) and 749
(747–752) and 752 (752–753) at 10M, warm medians with their ranges; once the scan had ended, 731 (616–846), 767
(565–970), 919 (918–920) and 930 (928–932). 24-column rows read 454 (443–466) keys/s at 1M. 10M rows a side
with every 100th key differing (100,000 keys) took 211.9 s (211.4–212.3): 77.5 s (77.4–77.6) of scan, 93.1 s
(92.7–93.6) of reads after it and 41.2 s (41.1–41.3) of cycle end. That pattern's cycle end took 0.31 s
(0.31–0.31), 2.68 s (2.55–2.81) and 41.2 s (41.1–41.3) at 1,000, 10,000 and 100,000 differing keys, after the
fix to the cycle end on a store whose tables had no statistics yet ([BENCHMARK.md](BENCHMARK.md) §16). The
store's tables were analysed before the cycle end at the two smaller sizes and, by autovacuum, during it at
100,000 (the report's statistics state `analysed during the cycle end`), so the three are not one curve.

## 13. Engines: MySQL and Oracle sides

A table side may be a MySQL or Oracle table or view, in any pairing with each other, with PostgreSQL and
with a service side. [ENGINES.md](ENGINES.md) is the binding contract of this section, with every fact
it rests on marked observed or not; this section states what a run does. **Observed on MySQL 8.4.11 and
Oracle 23.26.3 only:** MySQL 8.0.17 and later and Oracle 19c and later are accepted and not observed
(the acceptance kits of `examples/tables/mysql/` and `examples/tables/oracle/` are what a site runs on
its own version); MariaDB and a MySQL before 8.0.17 are refused at every connect, an Oracle before 19
too; SQL Server is not built. Graph parity ([GRAPH.md](GRAPH.md)), `regex` key maps and `ILIKE` are
refused on these sides when the spec is loaded; captured samples stay service-only. Everything else of
§1 to §12 holds: scans, soak re-diffs, explicit keys, `key.source`, `compare_one`, mixed pairs, batched
reads, the key census, hash-sample frames and composite keys.

### 13.1 Targets

```yaml
crm-legacy:
  engine: oracle                    # mysql | oracle
  host: crm-db.bank.example         # a DNS name or an IP literal
  port: 1521                        # default 3306 (mysql), 1521 (oracle)
  service: CRMPDB                   # oracle: a service name. mysql: `database: ledger`
  user: ds_reader
  password_ref: file:/run/secrets/crm_legacy_password
  tls_ca_ref: file:/run/secrets/crm_db_ca          # optional: else the system trust store
  tls_server_dn: "CN=crm-db.bank.example,O=Bank"   # oracle with a verified name: required
  allow_private_network: true
  rate_per_s: 50
  max_concurrency: 2
```

- **Members.** `engine` requires `host`, `user`, `password_ref`, and `database` (mysql) or `service`
  (oracle); it excludes `dsn_ref`, `base_url`, `auth`, `capabilities` and `allow_insecure_http`.
  `service`, `database` and `user` match `^[A-Za-z0-9_.$#@-]{1,128}$`, so no connect descriptor, SID or
  alias can be written; `host` is an IP literal or a DNS name. Its capabilities are a database target's
  (§1). The targets file's errors are `invalid_database_target`, `private_network` and `forbidden_host`
  ([ENGINES.md](ENGINES.md) §12, T1 to T8).
- **TLS is on unless `allow_unencrypted: true`,** and enforced: a MySQL server that offers no TLS is
  refused before any authentication packet, Oracle is dialled as TCPS, and after the login the session's
  own report is checked again. By default the chain and the name are verified, MySQL's against `host`,
  Oracle's against `tls_server_dn`, which the server certificate's subject must equal attribute for
  attribute, in order (DeltaSleuth compares it in the handshake; the driver's own match is not exact
  and is not used). `tls_verify_name: false` verifies
  the chain only, against `tls_ca_ref`, which it requires: the certificate a MySQL server makes for
  itself carries no name and passes only this mode. Oracle's encryption is TCPS or none: the driver has
  no Native Network Encryption.
- **Secrets** are `password_ref` and `tls_ca_ref` only, `file:` references read at every connect. The
  password is a driver argument, never part of a connection string. No environment variable,
  `~/.my.cnf`, `tnsnames.ora`, `sqlnet.ora` or wallet is read.
- **Every session goes to an address DeltaSleuth vetted.** A literal loopback or private `host` needs
  `allow_private_network`, and a link-local, unspecified, multicast or metadata address is never a
  target; at every connect the name is resolved, every answer is held to the same rules
  (`netguard.vetted`), and the session is opened to one of those addresses: DeltaSleuth dials MySQL's
  socket itself, and hands Oracle's driver the address, never the name. An IPv6 address that stands for
  an IPv4 one (IPv4-mapped, IPv4-translated, the well-known NAT64 prefix `64:ff9b::/96`,
  IPv4-compatible) is judged as that one. The answers are tried in order inside one 10 s budget, each
  with an equal share of what is left. One path is not vetted: an
  Oracle listener may redirect a session (RAC SCAN, shared server) and the driver follows it. A
  PostgreSQL `dsn_ref` target is not vetted at all: its host is inside the secret DSN.

### 13.2 The seam (`deltasleuth/connectors/tables/`)

The plan, the per-key connector and the runner ask an `Engine` (`engine.py`) for sessions, the
catalogue, the probe, per-key rows, the scan, the census and the frame. PostgreSQL's adapter
(`connectors/postgres/engine.py`) keeps §1 to §12's sessions and statements. MySQL (`mysql.py`, over
PyMySQL) and Oracle (`oracle.py`, over python-oracledb in thin mode: no Oracle client library) share one
statement composer (`compose.py`) over a set of primitives each, and one canonicaliser (`render.py`).
No driver exception leaves an engine: each becomes a `TableError` holding only a code (`ORA-01555`,
`MySQL 1142`, `DPY-4024`, or a class name such as `gaierror`), because the servers' messages quote
values; that includes whatever PyMySQL raises on an answer no MySQL server sends. A run that fails on
one shows `<class>: <code>` (`TargetTempSpace: ORA-01652`) and its `run.failed` notification the class
alone ([ALERTS.md](ALERTS.md) §2.1). A refused connect names the endpoint its target declares to
whoever creates the run, never the password; the chat's `compare_key` tool tells the model only the
side and the target id.

### 13.3 Sessions

| | MySQL | Oracle |
|---|---|---|
| Every session | `SET NAMES utf8mb4`; `sql_mode = ''`, `time_zone = '+00:00'`, long `wait_timeout` and network timeouts, `max_sort_length = 8388608`, `max_execution_time`; no value converters | `NLS_SORT` and `NLS_COMP` `BINARY`, `NLS_CALENDAR` `GREGORIAN`, `TIME_ZONE` `UTC`, `NLS_NUMERIC_CHARACTERS` `.,`, each read back: a logon trigger that overrides one refuses the run |
| Read-only | `SET SESSION TRANSACTION READ ONLY` | no session switch exists: every read runs inside `SET TRANSACTION READ ONLY` ... `COMMIT` |
| Grants | `SELECT` on each relation | `CREATE SESSION`, and `READ` on each relation |
| A pass's snapshot | `REPEATABLE READ`, `START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY` | one `SET TRANSACTION READ ONLY` |
| `consistency` | `snapshot` for an InnoDB base table; `unverified` for a view; `none` for another storage engine | `snapshot` |

- **`consistency`** is stored in the plan and in the side's census record, and the certificate says it
  (§13.8): a MySQL view is read in one transaction whose snapshot cannot be observed, and a table outside
  InnoDB in no snapshot at all.
- **Timeouts:** per-key reads `execution.total_timeout_s`; a census and a frame 3600 s a statement; a
  scan none, relying on its socket's keepalive probes (60 s idle, then 6 probes 10 s apart, set on both
  engines' sockets). A connect, with its handshake and the session's settings, has 10 s in all.
- **A scan ends with its run and with its worker:** cancelling or pausing a run, and stopping the
  worker (`ds serve`, `ds worker` and `ds run` on SIGTERM or Ctrl-C), cancel the scan's statements on
  both servers and close its sessions.
- **Large values.** A row whose LOB values (`TEXT`, `BLOB` and `JSON` types on MySQL; `CLOB`, `NCLOB`,
  `BLOB` and `JSON` on Oracle) total more than 16 MiB is not fetched: it digests as a row that never
  matches and reads per key as `resource_limit`. A MySQL fetch closes at 10,000 rows or 64 MiB; an
  Oracle fetch holds what 64 MiB take of rows of the declared width, 4 rows or fewer with a LOB column.
  The PostgreSQL side of a pair with either fetches 100 rows, then as many as 64 MiB hold at the width
  of the widest row of the fetch before (it has no bound on one row). Keys are read per key 200 a group
  (§12), or 4 when a compared column of a MySQL or Oracle side is a LOB column.
- **Sessions per run:** one per side for per-key reads, one more per side while it scans, one for a
  census or a frame, two short ones per side when the run is created. No pool.

### 13.4 Plan

- **The relation** resolves as spelled, else by the one name equal ignoring case (MySQL) or upper-cased
  (Oracle); a column the same way. Without `schema`: the target's `database` (MySQL), the session's
  current schema (Oracle). Oracle follows one synonym, the schema's own before a public one, and stores
  the real owner and name; a synonym over a database link is refused. Canonical names stay lower-cased,
  so Oracle `EMAIL` pairs with PostgreSQL `email`. A relation the database answers with an error for
  once the session is open (an Oracle view left invalid, a session the server ended) is refused when
  the run is created, by the code alone: `side a: table or view APP.BROKEN_V cannot be described:
  ORA-04063`, on PostgreSQL by the error's class.
- **Classes.** Each column the plan reads has a class by its declared type: `integer`, `decimal`,
  `float`, `float32`, `text`, `char`, `boolean`, `date`, `timestamp`, `instant`, `time`, `bytes`, `json`
  or `other` ([ENGINES.md](ENGINES.md) §7 lists the types of each engine). A type with no class (a
  spatial type or `VECTOR` on MySQL; `LONG`, `LONG RAW`, `BFILE`, `XMLTYPE`, object and collection types
  on Oracle) is refused wherever the plan would read it, never dropped.
- **Across the pair,** when both sides are tables and one is MySQL or Oracle: a compared column's two
  sides must be of one family after their typeMaps (text and char; the number classes; every other class
  alone), else the run is refused naming the `typeMap` entry that bridges them; a column of class `other`
  never crosses engines; and a key column's two sides must render one text (§13.5). Nothing is coerced
  silently.
- **Stored form.** Each table side adds `engine`, `server` (the server's major and minor version, never
  its patch level), `consistency`, `tls`, and `classes` and `declared` for its key, compared, filter and
  strata columns; the top level adds `rendering: 1`; `digest` is every compared column the worker hashes.
  A run whose target's engine is no longer its plan's fails (`EngineChanged`).

### 13.5 Keys and one order

- **A key text is PostgreSQL's `::text` of the same value** on every engine: an integer's digits, a
  decimal's at its declared scale (`1.50`), a text as it is, a `CHAR` without its padding, a date or a
  timestamp without fractional seconds in ISO form, bytes as `\x` and lower-case hexadecimal digits.
  Every other class, a timestamp with fractional seconds, an Oracle `FLOAT`, `NCHAR`, `NVARCHAR2` or
  LOB, a MySQL `ZEROFILL` decimal and an Oracle column in a collation of its own cannot be a key in such
  a pair: compare a view that casts it. A number key's two sides must declare one scale (`1` never pairs
  with `1.00`); an unconstrained Oracle `NUMBER` counts as scale 0, and a row whose key is not a whole
  number there is counted in `skipped_x`, as is an Oracle `DATE` key before year 1. Two key classes
  whose texts are never equal are refused the same way: a date against a timestamp (an Oracle `DATE`, a
  MySQL `DATETIME`), and bytes, a date or a timestamp against a number or each other. A text key pairs
  with any class.
- **Order.** Every side sorts by the key text's code points: PostgreSQL by `convert_to(k, 'UTF8')`,
  MySQL by `CAST(<key text> AS BINARY)` whatever the column's collation, Oracle by the key text under
  `NLS_SORT = BINARY` in an AL32UTF8 database and by `UTL_I18N.STRING_TO_RAW(<key text>, 'AL32UTF8')` in
  any other. That function returns at most 2000 bytes, so outside AL32UTF8 a text key column declared
  over 500 is refused for every run, and the account needs `EXECUTE` on `UTL_I18N`.
- **The guard** (§5, Merge) holds on every engine: soundness rests on it, not on trust in a collation.
- **Lookups** are an index-friendly `t.k IN (<typed values>)` plus equality of the key text's order
  expression with the key's part, and the connector keeps a row only when the key texts it fetched are
  the key's. A key part that is no value of the column's class (a number no decimal holds among them)
  cannot be on that side: 404 without a query. `X1` and `x1` stay two keys on every engine. Oracle's `''` is NULL: such a row has no key.
- **A key the column cannot hold** is §4's on every engine, by the server's code: the batch is halved
  until the key stands alone, which is then INCONCLUSIVE(skipped), and the other keys are served.

### 13.6 Values: canonical rendering 1

The engine renders every value as text or bytes, the driver carries nothing else, and one Python
canonicaliser (`render.py`) turns each into the JSON value PostgreSQL's `to_jsonb` gives the same value:
exact numbers, the shortest float text that reads back, `YYYY-MM-DD`, timestamps as
`YYYY-MM-DDTHH:MM:SS[.f]` with trailing zeros dropped and instants in UTC with `+00:00`, bytes as `\x`
hex, JSON parsed exactly. In a pair with another engine a PostgreSQL `char` loses its trailing spaces,
its `time` its trailing zeros, and its floats take the shortest text (its own is a digit longer for some
whole floats from 2^25 and 2^54), as the other engines' do. A rendering expression gives one text in
every statement: MySQL floats are rendered through `JSON`, because a cast to `CHAR` is cut to 22
characters in a batch and in a temporary table ([ENGINES.md](ENGINES.md) §9). A rendered text that is
not of its class's form is `unconvertible`, never passed on. The rendering is `1` in the plan and pinned by the golden
corpus.

Reported as differences until the approved spec bridges them ([ENGINES.md](ENGINES.md) §9): Oracle's
NULL against `''` (`emptyAsNull`, §10); an Oracle `DATE`, which holds a time, against a date (`{as:
date}` on the Oracle side, or `{as: timestamp}` on both); `TINYINT(1)` or `NUMBER(1)` against a boolean
(`{as: boolean}`); an unzoned against a zoned timestamp; nanoseconds against microseconds; MySQL's zero
dates, `BINARY` padding and a `uuid` against `RAW(16)` need a view.

### 13.7 Statements and digests

- **Injection-free by construction.** A statement is fixed text, identifiers and values. Identifiers
  come only from the catalogue's spelling, quoted. An Oracle value is a bind variable typed by its
  column's class. No permissively licensed MySQL driver binds on the server, so a MySQL value is exactly
  one of a number matching `-?[0-9]+(\.[0-9]+)?`, `_utf8mb4 X'<hex of its UTF-8>'` and `X'<hex>'`:
  nothing is escaped, and key values, filter literals and resume bounds appear in MySQL statement text
  as hexadecimal digits.
- **Per-key reads** are one arm a key, as §4 and §12: 200 keys a statement on MySQL; on Oracle a group
  of 200 is read in statements of at most 50 keys, each one rate-budget token. A group is 4 keys where a
  compared column is a LOB column (§13.3); beside a file side it stays 200, the files read in one pass
  and the LOB side 4 keys at a time ([FILES.md](FILES.md) §12).
- **The scan** selects the key texts, the engine's row hash where it hashes, the worker's columns as
  rendered, in key order. **Across engines** the worker hashes every compared column:
  `sha256(EMPTY + jsonio.dumps(convert_row(document, types), sort_keys=True))`, so every compared column
  of every row crosses the network to the worker and each engine sorts the population with its compared
  columns. A MySQL or Oracle side, and the PostgreSQL side of a pair with either, hash each fetched
  batch off the event loop; a pair of PostgreSQL tables hashes its typeMap columns on it, as it did
  before the engines. **In one engine** (two MySQL sides, or two Oracle sides whose databases share both character
  sets) the engine hashes the columns both sides declare with the same type text, that no `typeMap`
  names and that are no LOB (`UNHEX(SHA2(JSON_ARRAY(...), 256))`; nested `STANDARD_HASH` on Oracle), and
  the worker the others, as a PostgreSQL pair's digest columns (§10).
- **Soundness** is §5's: equal digests mean identical converted values of the same canonical columns,
  and per-key rows are built by the same canonicaliser.

### 13.8 Failures, the census and the certificate

| Class | Per key | Scan | Key census; frame |
|---|---|---|---|
| a key the column cannot hold | the batch halved; alone, skipped | resumed; stalled when the pass saved no checkpoint | `{failed}`; the run pauses |
| unreachable (no server code, or an outage's: a full account or server among them) | `connect_error`, re-read once on a new session | paused, resumed after `worker.lease_seconds`, without limit | `{failed}`; drawn again at the next sweep |
| timeout | `timeout`, then each key alone | none set | `{failed}`; the run pauses |
| snapshot lost (1412; ORA-01555, ORA-08176, ORA-01466) | `connect_error` | resumed; stalled when the pass saved no checkpoint | `{failed}`; the run pauses |
| login refused | `auth_error`: the run pauses | the run pauses | `{failed}`; the run pauses |
| temporary space exhausted | fails the run | fails the run | `{failed}`; fails the run |
| denied, or the relation gone | the schema guard looks first ([PRECHECKS.md](PRECHECKS.md) §7, V5): a change that breaks the plan pauses the run, else it fails | the same | `{failed}`; the same |
| any other server code | `connect_error` naming the code | resumed; stalled when the pass saved no checkpoint | `{failed}`; the run pauses |
| an address the guard refuses | `connect_error` | fails the run | `{failed}`; the run pauses |

A failed key census is recorded as `{failed: <error class>}` and the cycle goes on ([PACK.md](PACK.md)
§1.3); a frame that cannot be read pauses its run with the reason, naming the code.

The third stalled pass in a row fails the run: an outage is waited out without limit, and a code every
resume would meet again cannot loop for ever. A stalled pass is followed by `worker.lease_seconds`
before the next, so the three span minutes. A stale password is never retried in a loop: an Oracle
profile would lock the account. An Oracle first pass that cannot finish inside the undo retention
(ORA-01555 before its first checkpoint) therefore fails after three tries.

A census record of a MySQL or Oracle side adds `engine` and `consistency`, and its `run.census` audit
row too; every table side's record of such a pair adds `server`, the version its read met, which the
pack's subject and certificate name, with a warning where it is not the plan's
([ENGINES.md](ENGINES.md) §13). The certificate ([PACK.md](PACK.md) §1.2) reads `in 1 transaction, a snapshot not observable (a
MySQL view)` or `in 1 pass, not a snapshot (a MySQL table outside InnoDB)` where `in 1 snapshot` stands
for a pinned side, warns `SIDE A NOT READ IN A SNAPSHOT (...)` for either, and says of every MySQL or
Oracle side `rows and values as the account reads them (redaction, masking and row policies apply)`:
under Oracle Data Redaction, VPD column masking or a masking view both sides return the same constant
and it reads as a match. `ds target check ID [--table [SCHEMA.]NAME]` prints what a spec's author
needs before a run: the server's full version, its character set (both without control characters and
cut at 64), whether the session is encrypted, a
note for an unobserved version or a character set that limits keys, and a table's columns with their
declared types and classes.

### 13.9 Measured

`DS_PERF=1`, `tests/e2e/test_engines.py`, each test in a pytest process of its own, **each run once, on
2026-10-02**: single runs, not medians, and not [BENCHMARK.md](BENCHMARK.md)'s harness, whose results are
PostgreSQL's alone. One Mac (Apple M3 Max, 16 CPUs) that was not idle (load average 2.6 to 5.8, other work
running); Docker Desktop (16 CPUs, 17 GB); every database a stock container of a local image on that
host (`postgres:16-alpine` 16.14, `mysql:8.4` 8.4.11, `gvenzl/oracle-free:23-slim-faststart` 23.26.3,
which Oracle caps at two CPU threads), the store on the PostgreSQL container; synthetic 4-column rows (an
integer key, two texts and a decimal of scale 2); worker concurrency 4, poll 0.05 s.

| Test | Pair | Whole run |
|---|---|---|
| `test_a_million_rows_across_engines`: 1M rows a side, 11 differing keys, the worker hashing every column | PostgreSQL against MySQL | 27.9 s |
| | PostgreSQL against Oracle | 25.9 s |
| | MySQL against Oracle | 23.0 s |
| `test_a_million_rows_in_one_engine`: the same, the engine hashing every column | MySQL against MySQL | 6.4 s |
| | Oracle against Oracle | 6.2 s |
| `test_batches_across_engines`: 20,000 differing keys of 100,000, `execution.concurrency: 8` | PostgreSQL against MySQL | 38.2 s |
| | PostgreSQL against Oracle | 35.9 s |
| | MySQL against Oracle | 34.1 s |

Two PostgreSQL tables take 6.8 s for the first (§7) and 43 s for the last (§12), by their own tests.
The figures above were taken before three later changes to this feature (the float rendering, the
PostgreSQL side's fetch sized by bytes, the pause between stalled passes); the same eight cases were run
once more after them and passed, their whole tests, seeding included, taking 11 s to 38 s each.

`test_ten_million_rows`: 10M rows a side, 11 differing keys, under `ds serve` in developer mode. `GET
/v1/runs/{id}` was asked once a second while the scan ran, and the scan's seconds are that poll's, so to
the second. The memory is the peak resident size of the process the worker ran in.

| Pair | The worker | Scan | Times 77.5 s | Peak memory | `GET /v1/runs/{id}` p95 (max) |
|---|---|---|---|---|---|
| PostgreSQL against MySQL (the worker hashes) | inside `ds serve` | 280 s | 3.6 | 191 MiB | 162 ms (246 ms) |
| | `ds worker` beside `ds serve --no-worker` | 278 s | 3.6 | 131 MiB | 10 ms (21 ms) |
| MySQL against MySQL (the engine hashes) | inside `ds serve` | 64 s | 0.8 | 180 MiB | 65 ms (88 ms) |
| | `ds worker` beside `ds serve --no-worker` | 63 s | 0.8 | 120 MiB | 10 ms (15 ms) |

77.5 s is BENCHMARK's scan of 10M rows a side between two PostgreSQL tables with every 100th key
differing, from another harness on another day: the multiple compares the two figures and is no paired
measurement. While a cross-engine scan ran, the worker's process was seen at about 110% of one CPU each
time it was looked at: the driver's row parsing and the digests hold the interpreter, which the API
shares when the worker runs inside `ds serve`, hence 162 ms against 10 ms. A site with cross-engine
scans runs `ds serve --no-worker` beside `ds worker` ([DEPLOY.md](DEPLOY.md), section 7).

**Not measured:** 10M rows a side with an Oracle side; any size on MySQL 8.0 or Oracle 19c; rows wider
than 4 columns; a database on another host (every one was on the worker's); the temporary space a MySQL
or an Oracle sort takes; a cold cache; a repetition of any figure above. The figures were taken before
later changes ([ENGINES.md](ENGINES.md) §3, §9, §15) and not again after them; the rows measured
hold no float and no large value, whose reading those changes touch. A pair of PostgreSQL tables with
typeMap columns is measured by no test here: figures for it are in ENGINES §15.

## 14. Files: CSV and Parquet sides

A table side may be CSV or Parquet files in a directory the host administrator mounts and declares, in
any pairing with PostgreSQL, MySQL, Oracle, a service side or another file side. [FILES.md](FILES.md) is
the binding contract of this section, with each DuckDB behaviour it rests on marked as observed; this
section states what a run does. The files are read in place by one DuckDB (`duckdb==1.5.5`), locked down
so that it reads the declared directories and writes only its spill. It runs in a child process of each
`ds` process, the file reader, which alone loads it ([READER.md](READER.md)): a fault or a memory
blow-up of DuckDB's ends that child, a new one is started, and the worker and the API go on. **Observed
on DuckDB 1.5.5's macOS arm64 wheel, with files of DuckDB's own writer and the tests' hand-built Parquet
files only**, and since the reader's hardening its Linux aarch64 wheel too, but only for the reader's own
gates (READER §3: its start, its limits, its ends) in an image built from the source tree: the x86_64
wheel was run under emulation alone, no table of §14.9 holds a Linux figure (what was timed there is at
its end: a CSV side keyed by `csv.keys` took about six times the Mac's pass), and Parquet of other writers (Spark,
Hive, Impala, Arrow) is the site's first test (the acceptance kit of `examples/tables/files/`). Graph
parity ([GRAPH.md](GRAPH.md)), `regex` key maps, `ILIKE`, `table.schema` and table pre-checks are refused
on a file side; captured samples stay service-only; object storage is not read (sync the bucket into the
declared directory with your own tool). Everything else of §1 to §12 holds: scans, soak re-diffs,
explicit keys, `key.source`, `compare_one`, mixed pairs, batched reads, the key census, hash-sample frames
(a mixed pair's), composite keys, `prefix_pad`, row filters, `typeMap` and schedules.

**What a file side is, and is not.** A file side is the bytes of these files as DeltaSleuth read them
between its pin and its last check, named by one digest: fixed files, never the system they were
exported from, and never said to be complete. An export's own time is what its stamp file declares, or
not observed.

### 14.1 Target and settings

```yaml
legacy-exports:
  engine: files
  root: /var/lib/deltasleuth/files/legacy   # inside the container; the host mounts it :ro
  settle_s: 60            # 0 to 86400: a file modified more recently is not read (default 60)
  allow_writable: false   # true (development): a mount DeltaSleuth could write to, which the certificate prints
  rate_per_s: 5           # per-key statements, each one pass over the files (default here: 5 and 2)
  max_concurrency: 2
```

- **Members.** `engine: files` requires `root` and takes only these (`invalid_files_target`); `root` is
  an absolute path of at most 1,024 characters with no `.` or `..` segment and no trailing or doubled
  `/`, and not `/` (`invalid_root`). It holds no secret.
- **Where a root may lie** (`forbidden_root`). After `realpath`, under the settings' `files.roots_under`
  (default `/var/lib/deltasleuth/files`, which the image creates), and never at, over or inside the
  evidence directory, the workspace, the spill directory, the directory of the settings or targets file
  or of a `file:` reference. Every command that opens the store exits 2 on it and `ds serve` does not
  start; a root that does not answer within 30 s is judged as written, so a dead mount stops no command
  (`ds target check` judges it the same way).
- **A missing or writable mount** is found when a run is created and by `ds target check`, never at
  start: the directory must be its own real path, listable, and mounted read-only for DeltaSleuth unless
  the target declares `allow_writable: true`.
- **Settings** (block `files` of the settings file, optional, applied at restart): `memory` (`2GB`) and
  `spill` (`20GB`), sizes in binary units; `threads` (2); `spill_dir` (default `<evidence_dir>/spill`);
  `roots_under`; `max_files` (1,000 a side); `plan_s` (300 s for the planning of a run's file sides, all
  together); `os_limits` (`true`: on Linux the reader's process sets itself a data limit of `memory`
  plus 2 GiB plus 600 MiB a thread and denies itself sockets; `false` sets neither). They are budgets
  of each process that runs `ds`, not of a run.

### 14.2 Spec

```yaml
table:
  name: customers             # the label reports show
  keyColumns: [customer_id]
  file:
    path: customers/current/*.csv
    format: csv               # csv | parquet: declared, never guessed from a name
    asOfFile: _AS_OF          # optional stamp file: its first line is one ISO-8601 time with an offset
    maxAge: 26h               # optional: the newest file is not older
    partitions: [dt]          # optional: key=value directories read as text columns
    binaryAsString: false     # parquet: un-annotated BYTE_ARRAY read as text
    csv:
      columns: [customer_id, email, balance, opened, status]   # every field, in file order
      keys: {customer_id: {as: integer}}   # optional: integer | decimal with scale | date; else text
      delimiter: "|"
      newline: "\n"           # "\n" | "\r\n"
      quote: "\""             # one character, or "" for none
      escape: "\""
      header: true
      skip: 0
      encoding: utf-8         # utf-8 | utf-16 | latin-1
      "null": ""              # the unquoted text read as NULL
      compression: none       # none | gzip | zstd
      padded: false           # true: trailing spaces are no part of a value
typeMap:
  balance: {as: decimal, decimalSeparator: ","}
  opened: {as: date, format: "%d/%m/%Y"}
```

- **The path** is relative to the target's root, of a closed grammar: segments of letters, digits, space
  and `. _ - = + @ , ( )`, none starting with a dot; one file, `*` inside one segment, or a trailing `/`
  for every regular file below (at most 8 directories deep; names starting with `.` or `_`, and
  directories so named, are left out). No request or run option carries a path.
- **CSV is declared, never sniffed.** Names, dialect, line ends and key types are in the four-eyes
  approved spec, and every column is text until a `typeMap` or `csv.keys` entry says otherwise: as text
  `00123` differs from `123`, and `{as: integer}` reads both as 123. An unquoted field equal to `null`
  is NULL, a quoted one the string. `ds target check` drafts a dialect by sniffing (§14.8); a run never
  does.
- **Parquet** columns carry their types: integers, decimals of up to 38 digits, floats, text, boolean,
  date, time, timestamps, bytes and JSON are read; a nested column, a decimal over 38 digits, one whose
  scale is over its precision and what
  DuckDB reads to the microsecond (a UTC nanosecond timestamp, an INT96 timestamp, a nanosecond time) are
  refused wherever a plan would read them (list `columns` without it; a nanosecond instant and an INT96
  timestamp are read to the microsecond once the spec declares `{as: timestamp}`, a nanosecond time
  once it declares `{as: string}`). The files of a side agree in each column's type, a timestamp's unit
  included. Encrypted Parquet is refused (an encrypted footer, or an encrypted column under a plain
  one), as is a footer over 16 MiB, of over 10,000 schema elements, nested over 32 groups deep, of over
  10,000 row groups or 200,000 column chunks (row groups times columns), or not
  of the form the plan's own walk of it reads: the walk reads the whole footer as DuckDB's reader takes
  it before DuckDB is handed the file, and refuses some footers DuckDB would read (a list of a later
  format version, fields out of the order of their ids, a row group whose column chunks are not the
  schema's columns in their order, a negative length or count: [FILES.md](FILES.md) §6 step 4, §20).
  The walk bounds what a footer costs the reader's memory and keeps a crafted footer from ending the
  process; a crafted page still can (FILES §14 point 7). A time outside a day, which only a crafted
  file holds, reads as a value that is not a time.
- **Loader rules** ([FILES.md](FILES.md) §15, L5 to L11): `table.file` goes exactly with a files target;
  graph parity, `ILIKE`, a `regex` key map and `table.schema` are refused on a file side.

### 14.3 Plan

When a run is created (`POST /v1/runs`, `POST /v1/compare`, `ds run`, `ds compare`, a schedule's
firing), each file side is planned inside `files.plan_s`, which the run's creation has once for all its
file sides, on threads of the file reader's own (at most 8 creations plan files at once in a process; a
call no thread takes within 30 s is refused as busy, to be tried again, never as a dead mount):

1. The root is checked (§14.1), and the path expanded by DeltaSleuth's own listing, never DuckDB's: a
   symbolic link, anything that is not a regular file, a name outside the alphabet or a file the process
   may not read refuses the plan and is never skipped; 1 to `files.max_files` files, ordered by path.
2. No file was modified within `settle_s`; with `maxAge` the newest is not older; with `asOfFile` the
   stamp file is there and holds one time.
3. **Each file is hashed** (SHA-256, opened without following a link) and pinned by its size, both
   times and inode. A `gzip` or `zstd` CSV's stream is checked whole as it is hashed.
4. A Parquet side's planned columns have file 1's name and type in every footer; a CSV side's first
   record of each file has the declared number of fields, and with `header` its declared names. A
   Parquet side's `rows` and the sizes its `sort_bytes` is projected from are its footers', as the
   plan's own walk of each read them (no statement of DuckDB's reads a footer's figures or statistics).
5. **One counting pass** reads the files: the rows, the rows the filter selects, and for CSV **the lines
   that are not rows of the declared dialect** (a wrong field count, an unterminated quote, invalid
   encoding, a line over 2 MiB), each by file, line and kind, never its text. Over 1,000 such lines
   refuse the run. A CSV side that declares a quote is read once more, each file alone, to find a last
   record cut inside a quoted field.
6. The first 1,000 rows of file 1 are converted with the side's `typeMap` and `csv.keys`: a column
   failing in more than half refuses the run, naming the column and the type.
7. A per-key read is one pass over the files, so a pass over half the spec's
   `execution.timeouts.total` (default 60 s) refuses the run; a scan whose sort would pass
   `files.spill` refuses it. The sort is projected as DuckDB was measured to spill it (`sort_bytes`:
   three times the key's text, the worker's columns' texts, 24 bytes a carried value and 16 a row, plus
   15%; FILES §6 step 8), an upper estimate: sorts measured spilled 0.36 to 0.98 of it before the 15%.
8. Every file still has its pin's size, times and inode.

The stored plan adds, beside §13.4's members (`engine: files`, `server: duckdb 1.5`, `consistency:
fixed`, `classes`, `declared`): `file` (the spec's member), `file_count`, `bytes`, `files_sha256` (the
SHA-256 of each file's hex digest and a newline, in path order; of a side that declares `partitions`, of
each file's `sha256sum` line whole, its path included), `pinned_at`, `as_of`, `modified_last`,
`mount`, `rows`, `selected`, `rejected`, `kinds`, `pass_s`, `lines_skipped` (the lines `csv.skip` left
unread) and `sort_bytes`; `schema` is null. **No
matched file's name, inode or own digest is in it, and no one file's size or times as such:** each
file's pin is sealed under the run's data key (`params.pinned`), in no pack, audit row, API answer or
log; the API answers leave `files_sha256` out too. What the plan holds in clear is of the side: the
spec's own path, the count, the bytes of all its files and the newest modification time. Of a side of one
file those are that file's size and time, and its path where the spec's path names it
([FILES.md](FILES.md) §13). Run creation therefore reads every byte: each file once to hash it and once
more to count it (a Parquet file's key and filter columns only; a CSV with a quote a third time).

The cross-engine checks of §13.4 run whenever a side reads files, a file-to-file pair included: a CSV
field is text, so against a number, a date or a typed Parquet column the run is refused naming the
`typeMap` entry (or `csv.keys` entry, for a key) that bridges it.

### 14.4 Keys, per-key reads and the scan

- **Key texts** are §13.5's: a Parquet integer, decimal, date or text key as PostgreSQL's `::text`
  writes it, bytes as `\x` hex; a CSV key is its field's text unless `csv.keys` declares `integer`,
  `decimal` with a `scale`, or `date`. Every other class is refused as a key. Every side sorts by the key
  text's bytes (`ORDER BY encode(...)`), under §5's guard.
- **A per-key read is one statement for the group and one pass over the files** (§12), not one arm a
  key; the files are checked by size, times and inode before and after it, not hashed again. Its
  observation's `request_url` is `files://<target_id>/<name>`. A group that timed out is not re-read key
  by key. A key part that is not of its column's class is on no row: 404 without a statement. On a
  Parquet side the pass reads the compared columns of every row group, whatever the key's type and the
  file's order: the statement holds no predicate on a key column, since DuckDB would skip row groups
  by their statistics, which need not hold the group's values (FILES §9). A spec's `where` is such a
  predicate, in the count, the scan and the per-key read alike.
- **A key with no row on a side whose files held lines that were not rows is INCONCLUSIVE
  (`unparseable`), never NOT_FOUND:** the key may be on such a line.
- **A file promises no unique key.** A key several rows hold reads `identity_ambiguous` per key, and
  the scan counts such keys of a file side as `repeated_a` or `repeated_b`. Nothing is de-duplicated.
- **The scan** is §13.7's: the worker hashes every compared column across engines; between two file
  sides DuckDB hashes the columns both sides declare alike that no `typeMap` names (two CSV fields, or
  two Parquet columns of one type; a CSV field against a Parquet column is the worker's), bytes and a
  `padded` field as their key texts.
- **Rows guard.** A read of a file side to its end (a scan, a key census) must meet exactly the rows
  its plan counted, else the run fails (`RowsMoved`).
- **One sort at a time.** A scan pass, a key census and a frame sort in DuckDB, one a process; another
  waits, its run still `running`. The sort spills under `files.spill_dir`, plaintext, in a directory of
  the process's own that is swept when a `ds` command starts and hourly under a worker; before it the
  spill volume's free space is checked.

### 14.5 Change and what is claimed

- **Any difference from the pinned files fails the run** (`FilesChanged`: by size, modification time,
  change time, inode, a link, a file added or taken away, or content), between a soak's cycles too: one
  run reads one export, and a recurring export is a schedule (§11), not a soak. The files are
  stat-checked before and after each per-key statement and after each fetched batch of a scan, a census
  and a frame, and **hashed again when such a read ends**; a scan stores when (`verified_a`,
  `verified_b`). A change that keeps size, both times and inode between two checks is seen only by that
  closing hash, which per-key reads and a key-list run without a census never take. The checks compare
  the files, not the directories above them: a parent directory swapped for another and put back between
  two checks is seen by none of them, the closing hash included, and the digest then names bytes that
  were not read. That needs a writer of the export directory on the host, and is an accepted gap
  ([FILES.md](FILES.md) §14 point 4).
- **The census record** of a file side ([PACK.md](PACK.md) §1.2) holds no snapshots and no digest, but
  `consistency: fixed`, `files`, `bytes`, `pinned_at`, `verified_at`, `as_of`, `modified_last`,
  `mount`, `rejected`, `lines_skipped` and `repeated`. Where `in 1 snapshot at <t>` stands for a database it reads `from
  12 fixed file(s), 3,650,722,201 bytes, pinned 2026-10-01 02:14 UTC and hashed the same when the read
  ended at 2026-10-01 02:20 UTC; an export, not the system it came from: declared as of 2026-09-30 23:59
  UTC by its stamp file, not observed; newest file modified 2026-10-01 01:58 UTC on the mount` (without a
  stamp: `its own time is not observed`; where `csv.skip` left lines unread, the bytes are followed by
  how many: ` (24 line(s) at the files' starts skipped as the spec declares, not read)`). The certificate
  alone adds the digest, what it is the SHA-256 of and the command that prints it from the files, `; sha256
  <64 hex> of the list of its files' SHA-256 values, a line each, in the byte order of their paths under
  the target's root, not a file's own (sha256sum FILE... | cut -c1-64 | sha256sum); reads of single keys
  before and after it were checked by size, times and inode, not hashed again` (of a side that declares
  `partitions`: `of the list of its files' sha256sum lines, each with its path …`). The digest is never a
  file's own SHA-256, of a side of one file neither.
- **Side a with lines that were not rows is no census:** a key on such a line that side b lacks is in
  no result, so no pack is issued (`cycle 1 of run R-… is not a census: side a's files hold 3 line(s)
  that were not rows`) and readiness's population gate is not met. On side b the pack is issued with the
  warning `SIDE B: 3 LINE(S) OF ITS FILES WERE NOT ROWS and were not compared: ...`; a repeated key and
  a mount DeltaSleuth could write to are warned of on either side.

### 14.6 Mixed pairs and rechecks

- **A mixed pair reads its file side in groups** of 200 keys: one pass over the files for the group, the
  service side key by key in the same slot at its target's rate. `execution.delay` follows the group; a
  refused credential (401, 403) ends the group's service reads at once.
- **A recheck never reads fixed files again.** The key's first observation of the file side stands,
  after one stat check for the group, and only the other side is read again. A first read that ended in
  an outage, a timeout or temporary space observed no bytes, and is read by the recheck. No recheck is
  due when both sides read fixed files or the unusable side is the fixed one: a result both of whose
  sides read fixed files is final as read, confirmation `frozen_cut`, where two databases' scanned keys
  are `rechecked` (§12).

### 14.7 Failures

| Cause | Per key | Scan | Key census; frame |
|---|---|---|---|
| the root cannot be listed or, after the plan, holds no entry at all (its mount is not there); a check misses its 30 s deadline, counted from when a thread took it; 4 abandoned reads of the target's own mount still wait (8 of all targets' stop every files target); no thread took the call within 30 s (`Busy`); the closing hash of an unchanged file meets a read error | `connect_error`, re-read once | paused, resumed without limit | `{failed}`; postponed |
| the files are not the plan's (`FilesChanged`) | fails the run | fails the run | `{failed: FilesChanged}`; the next per-key read fails the run |
| a read to its end met other rows than the plan counted (`RowsMoved`) | — | fails the run | a key census fails the run (a frame has no rows guard) |
| a statement fails on unchanged files (a damaged page or stream) | `connect_error` naming the class | a stalled pass; the third in a row fails the run | `{failed}` |
| memory or the spill at its limit, too little free space before a sort or when the reader is built anew while a run reads (`TargetTempSpace`) | `connect_error`, retried | fails the run | `{failed}`; fails the run |
| a file the process may not read (`TargetDenied`) | fails the run | fails the run | fails the run |
| a statement past its timeout | `timeout`, not re-read key by key | none set | `{failed}` |
| the file reader's process ended under the statement (`Died`), another export is read apart first (`Alone`), or the reader's process did not start (`Unstarted`) | `connect_error` with the code, the group read once more | paused, resumed | `{failed}`; postponed |
| the reader's process ended three times while this side's files alone were being read (`ReaderDied`) | fails the run | fails the run | `{failed: ReaderDied}`; the next per-key read fails the run |

No DuckDB message leaves the reader (it quotes paths and values): a failure is its class's name. A run
that fails shows `<class>: <text>` (`FilesChanged: side a: file 3 of 12 is not the file the run was
created on (content)`) and its `run.failed` notification the class alone. [FILES.md](FILES.md) §15 lists
every refusal's text; each names a file by its position in path order, never by its name.

### 14.8 `ds target check`

`ds target check ID` of a files target prints its mount, DuckDB's version, that the Parquet reader is
built in and that the files are read in a process of its own, with the data limit and the denied sockets
its start saw hold, or why not; with `--table PATH --format csv|parquet` the files the path names (the one place file names
are printed), and for CSV a draft of `csv:` and `typeMap:` sniffed from the first 20,480 rows of file 1,
for Parquet each column's type and class (a nested column's type, or one no plan reads, by its head
alone: `LIST`, `STRUCT`). `--header` names a draft's fields from the first record
(without it they are `column 1 ...`, so no record's text is printed); `--delimiter C` replaces the
sniffed one; `--count` reads the files whole (rows, a pass's seconds, each line that is not a row as
`file 2 line 81223: columns`); `--manifest` hashes them and prints `sha256sum`'s lines and
`files_sha256`, so the export's owner can reproduce a pack's digest (`sha256sum -c` in the root checks
the lines; `cut -c1-64 | sha256sum` of them gives `files_sha256`, and `sha256sum` of the lines whole
that of a side that declares `partitions`, printed as a second line). The draft is a starting point: a run
reads only what the approved spec declares. Its lines are printed whole, and an escape that is not the
quote is drafted. `--manifest`, and `--count` of a `.gz` or `.zst` export, pin the files as a run's
creation does, each compressed stream read to its end: an export cut short is refused, not counted
short. A mount that does not answer, the checked target's or another's, hangs no check (30 s at most).

### 14.9 Measured

`DS_PERF=1`, `tests/e2e/test_files.py`, **each test run once, on 2026-10-03 between 23:11 and 23:37**,
after that day's changes, among them the footer walk of each Parquet file in Python, the per-key
statement's join on `encode`, deadlines counted from a call's start, the scan pass taking the sort
before its snapshots, and DuckDB's file caches switched off. Single runs, not medians, and not [BENCHMARK.md](BENCHMARK.md)'s harness, whose results
are PostgreSQL's alone. Method: each of the nine tests in a pytest process of its own, one after the
other. One Mac (Apple M3 Max, 16 CPUs, 64 GB) that was not idle (load average 2.8 to 5.6: other work ran
on it); DuckDB 1.5.5's macOS arm64 wheel inside the worker's process, with the default `files` settings
(`memory: 2GB`, `spill: 20GB`, `threads: 2`); each file side one file on the Mac's own disk, written by
DuckDB just before it was read, so the page cache was warm; PostgreSQL sides and the store on one stock
`postgres:16-alpine` container (Docker Desktop, 16 CPUs, 17 GB); synthetic 4-column rows (an integer
key, two texts and a decimal of scale 2); worker concurrency 4, poll 0.05 s; the files targets at their
default `rate_per_s: 5` and `max_concurrency: 2`. One test was run a second time, at once, because its
figures stood out (the batches, below): both runs are given.

**The plan** is what creating the run took (`create_run`: each file hashed, a Parquet footer walked, its
first record or footers read, its rows counted, a CSV's end checked); **the run** is from then to the
run's end, the scan, the per-key reads of what differed and the cycle's end.

| Test | Case | Plan | Run |
|---|---|---|---|
| `test_a_million_rows_of_files`: 1M rows a side, 11 differing keys | Parquet against PostgreSQL (the worker hashes every column) | 0.1 s | 26.6 s |
| | CSV against PostgreSQL | 1.1 s | 29.8 s |
| | Parquet against Parquet (DuckDB hashes every column) | 0.1 s | 5.3 s |
| | CSV against CSV | 2.0 s | 12.8 s |
| `test_ten_million_rows_of_files`: 10M rows a side, 101 differing keys | Parquet against Parquet (175 MiB a side) | 0.4 s | 53.6 s; the spill peaked at 167 MiB, 17 bytes a row (the plan's `sort_bytes` projected 189 a row) |
| | CSV against CSV (532 MiB a side) | 22.0 s (a side's counting pass 10.0 s) | 159.3 s; the spill peaked at 598 MiB, 63 bytes a row (projected: 189) |
| `test_batches_on_files`: 20,000 differing keys of 100,000, `execution.concurrency: 8` | Parquet against PostgreSQL | 0.1 s | 44.2 s, in 100 per-key statements on the files; run again at once: 37.8 s |
| | CSV against PostgreSQL | 0.2 s | 60.6 s, in 100 statements; run again at once: 34.4 s |
| `test_a_mixed_pair_on_files`: 20,000 keys of a 1M-row CSV against a service on loopback, 200 of them differing and rechecked | CSV against a service | 1.4 s (the counting pass 0.9 s) | 99.7 s, in 100 statements on the files: none for the rechecks |
| `test_a_frame_of_a_file`: a `bottom_k` sample of 3,000 keys framed by a 10M-row Parquet side (two strata), against a service on loopback | Parquet against a service | | the frame was read and drawn 97 s after the run's creation; the run ended at 102 s, its keys read in 15 statements on the file |
| `test_two_scans_share_one_budget`: two runs of 1M rows a side, Parquet against PostgreSQL, started together under one `ds serve` | | | both completed 55 s after the second was created; the second's sort waited for the first's (its log line); that process peaked at 388 MiB resident |

The batches' first run took 44.2 s and 60.6 s where a run earlier that day, before those changes, had
taken 35.9 s and 34.6 s, so the test was run again at once: 37.8 s and 34.4 s. Its 100 statements on the files are about 3 s
of either figure (a group of 200 keys on a 100,000-row file took 33 ms, CSV or Parquet, timed apart);
the rest is PostgreSQL's reads and the store's writes of 20,000 results, on a machine other work was
using. No other figure was repeated.
Two PostgreSQL tables take 6.8 s for 1M rows a side (§7) and 43 s for the batches (§12), by their own
tests, which were not run again here. The spill bytes were looked at twice a second: a peak between two
looks is not in them.

**One group of 200 keys read from 10M rows** (`test_a_group_against_ten_million_rows`; the statement
with its two stat checks; the keys spread evenly over the rows):

| Files | One group | The plan's counting pass |
|---|---|---|
| Parquet, sorted by the key | 0.3 s | 0.1 s |
| Parquet, not sorted | 0.3 s | 0.1 s |
| CSV (532 MiB) | 10.6 s | 10.0 s |

A CSV pass ran at about 53 MiB a second (532 MiB in 10.0 s; 1.01 GiB in 19.8 s, below): a file's rows
are parsed on one thread. A per-key read of a CSV side is one such pass for each group of up to 200
keys, and a plan whose pass is over half the spec's `execution.timeouts.total` (60 s by default) is
refused: on this machine that is a CSV side of about 1.5 GiB (in the Linux image, with the key these
tests declare by `csv.keys`, about 250 MiB: "On Linux", below).

**10M rows a side under `ds serve`** (`test_ten_million_parquet_against_postgres`: a 175 MiB Parquet
file against PostgreSQL, 101 differing keys, developer mode). `GET /v1/runs/{id}` was asked once a
second while the scan ran, and the scan's seconds are that poll's, so to the second. The memory is the
peak resident size of the process the worker ran in. The file hashed at 1,985 MiB a second
(`manifest`'s own hashing of it, measured apart from the plan).

| The worker | Plan | Scan | Times 77.5 s | Peak memory | Spill peak | `GET /v1/runs/{id}` p95 (max) |
|---|---|---|---|---|---|---|
| inside `ds serve` | 0.3 s | 277 s | 3.6 | 1,601 MiB | 0 (the plan's `sort_bytes`, §14.3's projection: 2,538 MiB) | 182 ms (224 ms) |
| `ds worker` beside `ds serve --no-worker` | 0.3 s | 274 s | 3.5 | 1,588 MiB | 0 | 12 ms (25 ms) |

77.5 s is BENCHMARK's scan of 10M rows a side between two PostgreSQL tables with every 100th key
differing, from another harness on another day, not measured again here: the multiple compares the two
figures and is no paired measurement (§13.9 says the same of MySQL's 3.6). The sort of 10M four-column
rows stayed inside `files.memory` and wrote no spill that the once-a-second look saw, where the plan
projected 2,538 MiB: the projection is of a sort that spills, and it errs high (§14.3). With the worker
inside `ds serve` the API shares the interpreter with the worker's hashing, hence 182 ms against 12 ms:
a site with large cross-engine file scans runs `ds serve --no-worker` beside `ds worker`
([DEPLOY.md](DEPLOY.md), section 7).

**The plan of a gigabyte** (`test_plan_of_a_gigabyte`: one CSV of 1.01 GiB and 18.7M rows, planned
inside `POST /v1/runs` under `ds serve --no-worker`): 21.6 s, of which its counting pass 19.8 s; the
page of another run, asked ten times a second meanwhile, answered in 16 ms at p95 (23 ms at most, 187
asked).

**Against a run before those changes** (the same tests, the same machine, earlier that day): no figure
moved by more than a single run's spread there, but the first run of the batches (above), the
cross-engine scan (277 s and 274 s; 268 s in both modes before) and the two scans' peak memory (388 MiB;
508 before). What switching DuckDB's file caches off costs a per-key read was timed apart, by hand: a
group of 200 keys on a 100,000-row file took 31 to 33 ms with the caches as DuckDB starts them and 32 to
34 ms with them off.

**After a later change** these figures stand, and no
test was run again: no plan figure changed (a Parquet side's `rows` and `sort_bytes` come from the plan's
own walk of each footer, the figures DuckDB's metadata functions gave: 189 bytes a row for the 10M-row
file, as before). The per-key statement of a Parquet side lost its typed `IN`, so no row group is
skipped by its statistics any more. Timed apart, by hand, on the same Mac on 2026-10-04, with the tests'
own 10M-row files and the default `files` settings: a plan 0.19 s (sorted by the key) and 0.23 s (not
sorted), one group of 200 keys 0.27 s and 0.30 s (five reads each, within 0.01 s), against the 0.3 s of
the table above for both; a group of 200 keys of a 100,000-row file 20 ms. On a file whose row groups
the `IN` skipped, a per-key read now costs a pass; at these sizes that is not seen.

**With the file reader in a process of its own** ([READER.md](READER.md); measured on 2026-10-04 between
15:05 and 16:17). Since the reader's hardening DuckDB runs in a child process of each `ds` process, and
rows cross a pipe in typed frames. The same nine tests were run again, each once, each in a pytest
process of its own, one after the other, on the same Mac (not idle: load average 4.8 to 8.4 as the tests
started), with the default `files` settings and DuckDB 1.5.5's macOS arm64 wheel, now in the reader's
process (on macOS that process has no data limit and no socket filter). **These are the figures of the
reader as built**, with one batch read ahead in the child (READER R9); the figures of 2026-10-03 above
are the in-process reader's and stay for comparison. As built the child read ahead only after a batch
of exactly 10,000 rows, which a statement fetched 64 rows a step never answers (its batches hold
10,048): the scans across engines, whose worker reads the text columns (the 10M-row scan under `ds
serve` below, the 1M-row cases against PostgreSQL, the two scans that share a budget), and the frame
read nothing ahead in these figures. Since
a later change a batch of 10,000 rows or more is read ahead after; the nine tests were not run
again (one pass was timed by hand, below):

| Test | Case | Plan | Run | 2026-10-03, in process (plan and run) |
|---|---|---|---|---|
| `test_a_million_rows_of_files` | Parquet against PostgreSQL | 0.2 s | 28.0 s | 26.7 s |
| | CSV against PostgreSQL | 1.2 s | 31.2 s | 30.9 s |
| | Parquet against Parquet | 0.2 s | 5.4 s | 5.4 s |
| | CSV against CSV | 2.2 s | 12.9 s | 14.8 s |
| `test_ten_million_rows_of_files` | Parquet against Parquet (175 MiB a side) | 0.5 s | 52.7 s; the spill peaked at 176 MiB, 18 bytes a row | 54.0 s |
| | CSV against CSV (532 MiB a side) | 22.4 s (a side's counting pass 10.1 s) | 160.2 s; the spill peaked at 600 MiB, 63 bytes a row | 181.3 s |
| `test_batches_on_files` | Parquet against PostgreSQL | 0.1 s | 44.2 s, in 100 per-key statements on the files | 44.3 s; again at once 37.9 s |
| | CSV against PostgreSQL | 0.3 s | 53.7 s, in 100 statements | 60.8 s; again at once 34.6 s |
| `test_a_mixed_pair_on_files` | CSV against a service | 1.5 s (the counting pass 0.9 s) | 98.1 s, in 100 statements on the files | 101.1 s |
| `test_a_frame_of_a_file` | Parquet against a service | | the frame was read and drawn 102 s after the run's creation; the run ended at 108 s, its keys read in 15 statements | 97 s |
| `test_two_scans_share_one_budget` | | | both completed 57 s after the second was created; the second's sort waited; the server's process and its reader together peaked at 450 MiB resident, the server's own at 207 MiB | 55 s; 388 MiB, one process |

One group of 200 keys read from 10M rows: 0.3 s (Parquet sorted by the key), 0.3 s (not sorted), 10.8 s
(CSV; its plan's counting pass 10.4 s). The plan of the 1.01 GiB CSV: 22.1 s (its counting pass 20.3 s),
the other run's page at 16 ms p95 (50 ms at most, 191 asked).

10M rows a side under `ds serve` (a Parquet file against PostgreSQL). The memory is now the most seen,
at one-second looks, of the resident sizes of the process the worker ran in and of its reader together;
the worker's own peak (`ru_maxrss`) is given beside it:

| The worker | Plan | Scan | Times 77.5 s | Peak memory, with its reader (its own) | Spill peak | `GET /v1/runs/{id}` p95 (max) |
|---|---|---|---|---|---|---|
| inside `ds serve` | 0.3 s | 283 s | 3.7 | 1,663 MiB (208 MiB) | 0 | 130 ms (155 ms) |
| `ds worker` beside `ds serve --no-worker` | 0.3 s | 283 s | 3.7 | 1,646 MiB (192 MiB) | 0 | 12 ms (34 ms) |

**What the boundary costs, and what was read ahead for.** The nine tests were first run with the reader
in its child and nothing read ahead (15:05 to 15:28): 29.3 s, 33.0 s, 6.4 s and 15.3 s for the four
cases of 1M rows (plan and run), 63.4 s and 184.1 s for 10M rows a side of files, scans of 291 s and 283
s across engines, 47.2 s and 54.7 s for the batches, 100.5 s for the mixed pair, a frame at 110 s, the
gigabyte's plan in 22.6 s and the two scans in 60 s. Two cases took over 1.15 times their figure of
2026-10-03, both Parquet against Parquet, where DuckDB hashes and the worker only merges, so the pipe
is most of what is left: 6.4 s for 5.4 s and 63.4 s for 54.0 s (READER §14 had projected 13% to 18%
there). For that the child reads one batch ahead while the worker shapes the one it has, and the nine
were run again (15:52 to 16:15): the table above, in which no figure is over 1.06 times its own of
2026-10-03. The in-process reader, before the reader had a process of its own, was measured in the same
sitting for three tests (15:46 to 15:52 and 16:15 to 16:17): 28.4 s, 34.3 s, 5.7 s and 15.2 s for 1M rows,
56.2 s and 185.2 s for 10M rows a side of files, 45.3 s and 55.8 s for the batches. So against the same
day's in-process reader the reader as built is no slower on those; the batches' second run of
2026-10-03 (37.9 s, 34.6 s) was not met by either on 2026-10-04, by the machine's state and not the
reader's. Single runs, as before; READER §14 asked for each twice on both builds, which was not done.

**After that change to the read-ahead** the nine tests were not run again. What changed for them is the
read-ahead of a batch fetched 64 rows a step (above); one such pass timed by hand (2M rows of the tests'
four columns, the three compared ones rendered for the worker, the same Mac, three rounds) took 15.9 to
16.4 s to fetch and shape with nothing read ahead and 14.1 to 14.7 s with it.

**On Linux** none of the tables above was measured, and two things were, both in the image built from
this tree (aarch64, natively, under the compose hardening):
- a pass over 10M rows a side, Parquet against Parquet (`tests/e2e/test_reader_image.py`), completed
  in 30 s under the default settings and the reader's data limit of 5,296 MiB, its `VmData` 2,579 MiB
  and its resident size 2,229 MiB at their peaks: one observation, of the limit's room;
- **a CSV side keyed by `csv.keys` as an integer was about six times slower than on the Mac**
  (measured by scripts that are not in the repository; [FILES.md](FILES.md) §17 has the figures).
  The tests' 10M-row, 532 MiB CSV took 63.6 and 64.2 s for a side's counting
  pass where the tables above have 10.0 to 10.4 s, and 145.7 s for one side's sort where the same Mac
  took 37.4 s. The cause is DuckDB's own: the key text of an integer or a decimal key casts the field
  to `DECIMAL(38,scale)`, which its Linux aarch64 wheel does about seven times slower than its macOS
  wheel (3M texts: 4.65 s against 0.64 s as integers, 4.74 s against 0.64 s as decimals); the reader's
  process and its limits change nothing of it, the in-process reader, before the reader had a process of
  its own, is as slow there, and
  with a text key a pass took the same on both. Under the default `execution.timeouts.total` (60 s)
  the plan of that 10M-row side is refused in the image (`one pass over its files took 62.7 s …`), and
  by these figures so is one from about 4.7M such rows, some 250 MiB, where the Mac's figure above is a
  CSV side of about 1.5 GiB. **Measure a CSV export's plan in the image before `files.plan_s` and a
  spec's timeout are sized.** An x86_64 Linux wheel was not measured.

**Not measured:** Parquet written by anything but DuckDB; the manylinux wheels beyond the two
observations above (every figure of the tables is the macOS arm64 wheel's); files on a network mount (NFS, SMB) or on any disk but the Mac's own; a cold
cache; rows wider than 4 columns; a side of more than one file; a compressed CSV; a CSV over 1.01 GiB
or any side over 18.7M rows (100M rows were not run); a sort that reaches `files.spill`; the time a
hung mount costs; a discarded instance beside a new one; a Parquet file with a large footer (the
tests' files hold a few hundred column chunks; a footer of 6.1 MiB, 78,400 chunks, was walked in 0.7 s,
timed apart, and later one of 7.7 MiB, 100,000 chunks, in 0.9 s, which DuckDB's reader
then held in 82 MiB outside `files.memory`); a database or a service on another host; a repetition of any figure but the batches'. [FILES.md](FILES.md) §10's sizes for a 100M-row sort (near 100 GiB for rows of 24 short columns,
as §14.3 projects it) are projections, not measurements.
