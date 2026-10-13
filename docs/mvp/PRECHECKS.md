# Table pre-checks and the schema guard: contract

This contract is binding. It builds on readiness (`014_readiness.sql`),
[ENGINES.md](ENGINES.md) (the `Engine` seam) and [SCHEDULES.md](SCHEDULES.md) (`015`), and amends the
documents of §15. It depends on no later feature; §14 names their seams.

**Goal:** before any per-key comparison, say where to look first across many table pairs; and stop a real
run from reading a table that is no longer what its plan, or its version's last run, read. **Shape:** two
halves kept apart. A *table pre-check* is its own object (`ds.prechecks`, `PC-…`), never a run: the worker
reads each side's catalogue and a few aggregate statements inside one `Engine.snapshot`, and a pure module
judges them. The *schema guard* is one nullable column on `ds.runs`, one pure function and five call
sites. Migration `016_prechecks.sql`; one new audit-fed event; no dependency, no licence fence, no outbound
destination but declared database targets through ENGINES' vetted sessions. Every claim stays *observed*.

**Read in the code:**
- `ds draft` does not exist; `ds profile` drafts service specs from JSON samples (ENGINES D11).
- "Precheck" already names the sign-off precheck (`readiness.PRECHECK`, `?precheck=1`, `GET
  /v1/samples/{draw_id}`): every text here says **table pre-check**.
- `schema_drift` (`runner/counters.py`) counts keys for the JSON guard of service sides, which needs `inferredSchema` and
  jsonschema-rs: it stays unbuilt and reads 0. The schema guard here is per relation, for table sides.
- Both `spec.schema.json` copies require `inferredSchema` beside `schemaDrift`, and `rules._gating`
  refuses `schemaDrift/mode: pause`.
- `approvals.plan` hashes every member of `runs.params` but `approval`: the schema guard's record lives
  outside `params`, so `plan_sha256`, `facts/plan.json` and ENGINES D3 are untouched.
- `approvals.needs` puts a table's `where` under the `population_filters` scope: in oidc mode no
  unapproved filter reaches a target today, and §6 keeps that. (A `regex` key map is
  under no scope. Its pattern is a predicate over raw key values that reaches a target unapproved, in a
  run and a pre-check alike: a run counts the rows whose key it does not match as `skipped_a` and
  `skipped_b`, a pre-check as `null_keys`. §6 does not change that.)
- `worker._scan` and `_handle` fail a run on any error but an unreachable store or target (a dropped
  column ends a soak as `failed: UndefinedColumn`), and with `columns` omitted the plan moves a column
  dropped on one side to `only_a` while every key still reads MATCH.
- Scans and censuses take no `TargetGate`; each opens its own snapshot session. With packs or readiness
  configured no key is leased until the cycle's census is recorded (`repo._CENSUS_PENDING`).
- Every pack holds `csv/coverage.csv` (`reports/pack.CSV_KINDS`), and `core/notify._paused` sends
  `run.paused` only for the reasons of `Pause`.

**Observed** on 2026-10-01, on throwaway containers of the local
images (`--pull never`): PostgreSQL 16, MySQL 8.4.11, Oracle 23.26.3; §13's
container tests confirm each form. **Not observed:** MySQL 8.0, Oracle 19c, a non-AL32UTF8
Oracle database, Oracle column collations, 10M rows on MySQL or Oracle.

## 1. Decisions (each one seam)

- **D1** A pre-check is its own object, never a run mode: a run seats a licence pair, is measured by
  readiness (READINESS D13), gated, signed off and packed; a pre-check is none of these.
- **D2** A pre-check writes its two tables and its audit rows only, and is never the schema guard's basis.
- **D3** Four levels, each including the one before: `schema`, `counts`, `columns`, `distinct` (§4).
- **D4** Never sampled, never estimated: every count is the database's own aggregate over the whole
  filtered population; optimiser statistics are never read.
- **D5** Only the worker runs pair items (the holder of `worker.LOCK_ID`), at most
  `precheck.sessions_per_target` sessions a target; a pair whose sides are one target reads them in turn
  on one. No `TargetGate`, as scans take none.
- **D6** No minimum, maximum or distinct value is stored, shown or exported: compared in worker memory,
  stored as a verdict. Text and key values are never fetched.
- **D7** Counts are numbers. A sum is a number only for a column the spec clears, on sides without a
  filter (§6's four conditions); else a verdict. Under a filter a protected column's counts beside its
  NULLs and its lengths are verdicts too (§6).
- **D8** Inputs: stored spec versions, or two declared database targets' schemas paired by name (§3).
- **D9** The schema guard's record is `ds.runs.guard`. Its reference is the run's own plan and, at
  creation, the version's newest earlier baseline run (while none is one, the newest stored plan, §7).
  DeltaSleuth holds no observation from when a spec was drafted.
- **D10** A change that alters what the plan's statements read or how a key renders (*breaking*) always
  pauses. A new run that would not compare what its predecessor compared (*narrowing*) is created
  paused, or refused under `schemaDrift: {mode: pause}`. Any other change warns, or pauses under that
  mode. The schema guard fails no run.
- **D11** A schema-guard pause ends only by a person's audited resume, never while a breaking change stands.
- **D12** One new event, `schema.drift`. A schema-guard pause is also a `run.paused`, as every pause with
  a reason: one delivery a channel (§9). A pre-check sends nothing.
- **D13** No licence fence (LICENCE O2). An open build, which
  has no licence, labels none: `evaluation` is false (LICENCE §19).
- **D14** Service sides are not guarded. This contract adds no `RunOptions` field (SCHEDULES §13).
- **D15** Under SSO a version that declares a filter and that the approval gate does not admit is
  pre-checked at level `schema` only: no unapproved filter reaches a target (§6). An author's `regex` key
  map is no filter and needs no approval of its own: its pattern, a predicate over raw key values, reaches the target
  unapproved in runs and pre-checks alike (§6, §16).

## 2. What readers are never told

`precheck.NOT` heads the screen, `ds precheck`, every export and each API answer but a download (its `not`
member; a draft, a starter spec, carries none: §3), verbatim; a CSV's first record is that sentence (§11):

> "A table pre-check counts and adds up; it does not compare rows. Tables whose figures agree can hold
> different rows, and figures can differ only because two databases store a value differently. It says
> where to look first. It is observed, not evidence, readiness or a sign-off, and no evidence pack
> contains it: run a comparison to compare rows."

`test_never_told` asserts on every surface:
- **N1** None of the whole words `match`, `matches`, `matched`, `identical`, `passed`, `verified`,
  `validated`, `reconciled` (any case), no tick, no green. The best verdict reads `no difference observed
  in the checks made (not parity)`. The one exception is a refused plan's own message, quoted after `a run
  of this spec would be refused:` (N7): TABLES' LIKE refusal says `cannot be matched`.
- **N2** No percentage, score or "n of m tables OK": totals are counts by verdict, worst first.
- **N3** A check not made is `not checked: <reason>` or `not compared: <reason>`, never agreeing.
- **N4** No value (D6): `same` for a minimum says the two were equal, never what they were.
- **N5** The sides were not read at one moment: both read times and the gap accompany every pair; "in 1
  snapshot" only where ENGINES' `consistency` is `snapshot`.
- **N6** A sum is never `same` unless neither engine can have rounded it (§4).
- **N7** A pre-check never says a run will succeed; it quotes the plan when one would be refused.
- **N8** Catalogue pairs are `paired by name`.
- **N9** The schema guard never says "schema unchanged": `no change observed`, with when it looked. It
  reads names, declared types, classes, nullability, the primary key and the relation's kind; not a
  view's own tables, constraints, defaults, indexes, a change of meaning, or one reverted between checks.
- **N10** Rows and values are as the account reads them (ENGINES §14 point 8).

## 3. Inputs

`POST /v1/prechecks` (`PrecheckIn(Payload)`) takes `level` (default `columns`) and exactly one of:

| Form | Pairs |
|---|---|
| `spec_ids` (1 to 500, none twice) | each stored version; both sides are tables (K3) |
| `spec_names` (1 to 500) | each name's newest version (`repo.newest_spec`) |
| `all_specs: true` | the newest version of every name whose sides are both tables; the others are counted in `skipped` |

`POST /v1/prechecks/catalogue` takes `{a: {target, schema?}, b: {target, schema?}, include?, exclude?,
level?}`: two declared database targets, no spec; level `schema`, `counts` (default) or `columns`.

**Catalogue pairs.** One short session a side (in turn when both are one target), inside the request as
`create_run` plans (30 s a statement), calls `Engine.relations`. A `schema` left out is ENGINES §7's.
Relations pair by lower-cased name; `include` and `exclude` are up to 20 `fnmatchcase` globs over it. A
name on one side only, or two names of a side alike ignoring case, is an item with verdict `unpaired`.
At most 500 items (K7) and 5,000 relations a side (K9).

**The implied plan** (`precheck.implied(rel_a, rel_b)`): the key is the primary key when both catalogues
show one over the same lower-cased columns, else none (`keys: not checked: no common primary key`); the
columns are the common names of one family and no refused type; no filter, no `typeMap`; every value
protected. It is a plan, not a spec: nothing is compiled, stored or counted as a pair. (As built: it
takes each side's engine too, and its columns hold the names of two families as well; see below.)

**The draft.** `profiler/tables.table_draft(result)` (about 80 lines over `profiler.draft.render_yaml`)
renders a starter spec for a catalogue pair: both `table` blocks in the catalogue's spelling;
`keyColumns` from the common primary key, else `TODO_key_column` and a header note; `columns` listing
the common comparable columns; `security: {unlisted: tokenize}`; a comment per left-out column with
ENGINES §9's bridge, never applied (and above `key`, a key whose texts never meet). It holds names and types only, comes with `validate_spec`'s issues,
and is never stored: uploading it is `POST /v1/specs`. A catalogue name or type reaches the body only as
a dumped YAML scalar and a comment only as `json.dumps(name)` (ASCII, so control characters, U+0085,
U+2028 and U+2029 are escaped): YAML reads those three as line breaks and `draft._comment` folds ASCII
whitespace only, so a hostile column name could otherwise end the comment and add spec content.

**As built** (`deltasleuth/prechecks.py` `catalogue`, `draft`; `core/precheck.py` `implied`,
`alone`; `profiler/tables.py`; `runner/precheck.py`):
- A blank `schema` is left out. Each side is listed on a short session of its own (`Engine.connect(target,
  timeout_s=30)`), at once on two targets and in turn on one. A connect or a statement that fails is K9's
  first message with the failure's code (`connectors.tables.code`: an engine's code, else the error's
  class, never a driver's text). "Holds no table or view the account can read" is judged on the listing,
  before the globs; the 5,000 relations are counted after them, so `include` narrows them (K9's third
  message); globs that leave neither side a name are refused (§10). `include` and `exclude` each take up
  to 20 patterns (K8), matched with `fnmatchcase` over the lower-cased name, every name without an
  `include`.
- The pairs are numbered from 1 in the order of their labels' bytes. A pair's `relation_a` and
  `relation_b` are the catalogue's names alone (the schema is the request's, which `claim_pair` now
  answers with the pair); a side with no name, or with two alike, has none. A name without a pair is
  stored settled (`precheck.alone(level, line)`: its result's only line is `unpaired`'s); its line names
  a lone relation as `<schema>.<name>` with the schema as asked (the name alone when none was), and
  names alike by their names alone. When every name is without a pair, the pre-check ends as it is made,
  `precheck.finished` audited as `system:worker` in the transaction that made it.
- What a schema lists (`Engine.relations`, sorted by the names' bytes): PostgreSQL's readable tables and views (§5),
  but no partition (`NOT relispartition`: each would be listed beside its partitioned table,
  whose rows it is, so a pair would read them twice and, against a MySQL or Oracle schema, which list a
  partitioned table once, every partition would be `unpaired`);
  MySQL's the `BASE TABLE`s and `VIEW`s of the schema as spelled, else of the one equal ignoring case, of
  which the account may select a column (`COLUMNS.PRIVILEGES`); Oracle's the tables (`ALL_TABLES`, not
  dropped, nested, secondary or an IOT's overflow) and views (`ALL_VIEWS`) of the schema as spelled, else
  upper-cased, that are the account's own or that it may read: `SELECT ANY TABLE` or `READ ANY TABLE`
  among `SESSION_PRIVS`, or `SELECT` or `READ` in `ALL_TAB_PRIVS` granted to it, to `PUBLIC` or to a
  role in `SESSION_ROLES` (`ALL_TABLES` alone lists a table the account may only write).
- **The implied plan** is `precheck.implied(rel_a, rel_b, engines)`: the pure module names no engine, so
  each side's engine name comes as data (P8, `other` across engines, needs it). A key column must be
  readable and of a class on both sides, else there is no key; side b's key is put in side a's order, so
  that key parts align. Its `columns` hold every common name the account reads on both sides; those of
  two families or of a type no plan reads get no figure (as `wanted` reads a spec's) and are reported
  by `schema_diff` (`columns_not_compared`), which is what a comparison would leave out. A column the
  account may not read on one side is on the other side only (N10). Each side carries its `engine`,
  `classes` and `declared`, so every engine's statements read it as a spec pair's. Columns a side reads
  under one lower-cased name (`"Email"` and `email`) are none of its columns and on neither side only: the
  plan lists them in `alike_a` or `alike_b`, and `schema_diff` reports each under that code
  (`columns_not_compared`), as a spec of them is refused (`both compare as`).
- The worker reads a pair of two relations paired by name as a spec pair: describes each by the
  request's schema and the stored name, plans with `implied`, sends no probe (there is no filter or key
  map) and judges with `Pair(None, label)`. A target no longer declared as a database refuses the pair
  with no session opened, and a relation gone since the schemas were listed is refused with `described`'s
  message: such a pair's line reads `a run of these tables would be refused: …` (§10).
- **The draft** is `table_draft(pair, targets=)`: it takes the stored pair (its pre-check, number, label,
  targets and result) and the registry its issues are judged against, and reads the result's new `names`
  member (§8), so a pair is drafted only once both catalogues were read (§10's K12). `columns` are listed
  by name (a stored result keeps no order); with none comparable it is `[]`, which its issues say;
  `metadata.name` is the label in kebab-case (`table-draft` when nothing is left), version `0.1.0`, owner
  `unassigned`; `key` is named after the key's columns, `class: identifier` (`type: composite` with its
  `parts` for several) and without a `pattern`, which no catalogue gives, so its issues hold the warning
  `key_pattern_empty` ([PACK.md](PACK.md) §2.3) until its reader declares one; `recheck` is the profiler's. The header names the pre-check and pair and the
  label through `json.dumps`; each column left out is a comment `json.dumps(precheck.clause(entry, …))`,
  the pre-check's own clause with its bridge (a `key_text_differs` entry, a comment above
  `key` rather than among the columns); each issue a comment `json.dumps("<severity>: <pointer>:
  <message>")`. The issues are `validate_spec`'s of the draft as parsed from its own text. A draft is a
  starter spec, not an export of the pre-check: it carries neither `NOT` nor the evaluation label.

## 4. Checks (`core/precheck.py`, pure; it names no engine)

Population: the rows the side's `where` selects, rows without a key among them, as a scan's `rows_a` and
the census's `rows`; duplicate keys are counted over the rows that have one. A spec pair is judged first
by `tables.issues` and the zero-row probe (§5): the refusals a run would meet are listed (10 at most; §5
says which) and the pair is `refused`.

| Level | Read | Statements a side |
|---|---|---|
| `schema` | catalogue only, no row | the catalogue read; a spec pair's probe |
| `counts` | `rows`; `null_keys` (the key is NULL, or its key map gives none) | 1 |
| `columns` | per compared column, below | the same statement, 150 columns each, 200 columns at most |
| `distinct` | `duplicate_keys`, `rows_in_duplicates`; `distinct` per column | 1, then 1 per 4 columns, 40 columns at most |

**Schema codes** (per canonical column), in two groups:
- *Not compared* (verdict `columns_not_compared`): `family_differs` (ENGINES P9, with its bridge);
  `uncomparable` (P7, P8); `only_a`, `only_b` where the plan takes the common columns (a spec that lists
  no `columns`, and every catalogue pair): a comparison would silently leave the column out. Also:
  `alike_a`, `alike_b` (a catalogue pair's column named alike ignoring case by another of its side,
  §3); and `key_text_differs`, a key column whose two sides' key texts are never equal, so that a
  comparison would find no key of one side on the other: of two families neither of which is text (a
  `date` against a `timestamp`), or two numbers whose declared scales differ (an `integer`'s is 0:
  `numeric(10,2)` reads 1.00 where `integer` reads 1; P6 refuses it across engines, not between two
  PostgreSQL sides nor in a catalogue pair). It has its own line rather than a place among the columns
  left out.
- *Schema only* (verdict `schema_differs`): `class_differs` (one family); `size_differs` (both declare
  `(n)` or `(p,s)` in one unit and the numbers differ); `nullable_differs` (two base tables); `only_a`,
  `only_b` for a column the spec's `columns` does not list; `key_differs`. Also:
  `key_class_differs`, `key_size_differs`, `key_nullable_differs`, a key column's two sides judged as a
  compared column's (an `integer` against a `varchar(10)`, whose texts may meet, is `key_class_differs`).
- Each key column is judged by side a's name, part by part in the key's order; a part a key map reads
  (`prefix_pad`) is judged by its NULLs alone, the map making its key text.
- `key_differs` is judged only between two base tables that both have a primary key: the two keys
  differ, or `keyColumns` is not a side's. Otherwise the pair prints `key: not checked: side b is a view
  (no primary key)` or `: side b has no primary key`, which lowers no verdict.

**Columns**, by class on both sides:

| Class | Figures |
|---|---|
| every class | `nulls` |
| `text`, `char` | `empty`; `length` range in code points (`char` without trailing spaces) |
| `integer`, `decimal` | `sum`, `min`, `max` |
| `date`, `timestamp`, `instant`, `time` | `min`, `max`, in canonical rendering 1 |
| `boolean` | `true` count |
| `bytes` | `length` range in octets |
| `float`, `float32`, `json`, `other`, a LOB column | `nulls` only |

**Not compared**, each with its reason: families differ; a column either side's `typeMap` names (`nulls`
only: the worker converts it per row; with `emptyAsNull`, that side's `nulls + empty`); floats; a sum
that is not exact;
over the column or row limits. Normalisers, tolerances, known-difference rules and `compare.include` or
`exclude` are not applied; the pair says so when the spec has any. A class with no figure of its own
(`json`, `other`) gives its `nulls` and no reason; a column whose two sides are of two families, or of a
type no plan reads, gives no figure at all and its schema code's text as the reason.

**Statement forms** (observed):
- Text distinct counts and key grouping are binary on the canonical text: PostgreSQL `COLLATE "C"`,
  MySQL `CAST(CONVERT(c USING utf8mb4) AS BINARY)` (its default collation counted `A@x.example` and
  `a@x.example` once), Oracle under the session's `NLS_COMP = BINARY`, `RTRIM` for `char`.
- Lengths: `char_length`, `CHAR_LENGTH`, `LENGTH4` (`LENGTH` gave 2 for an emoji in `NVARCHAR2`).
- MySQL empties are `CHAR_LENGTH(c) = 0`: under a PAD SPACE collation `c = ''` matched spaces.
- Oracle NULLs are `SUM(CASE WHEN c IS NULL THEN 1 ELSE 0 END)`: `COUNT(clob)` raises ORA-22849. (As
  built, every count of a MySQL or Oracle side is `COUNT(CASE WHEN … THEN 1 END)`, which reads a CLOB
  too and is 0, never NULL, over no row.)
- Duplicate keys are `GROUP BY <key texts> HAVING COUNT(*) > 1` on every engine (Oracle refuses
  `COUNT(DISTINCT a, b)`): what the scan's merge calls a repeated key.
- Sums are composed for `integer` and `decimal` only (MySQL sums text to 0); PostgreSQL sums `c::numeric`.
- As built, PostgreSQL: every figure is `(<aggregate>)::text`; a text is `c::text COLLATE "C"`
  (the cast drops a `character(n)`'s padding, so a blank one is empty and its length 0; a `uuid` and an
  enum are their texts); `bytes` lengths are `octet_length`; a `date`, `timestamp`, `instant` or `time`
  minimum and maximum are `to_jsonb(min(c)) #>> '{}'`, a row's own rendering (`0044-03-15 BC`,
  `2024-05-01T10:00:00+00:00`); `nulls`, `empty`, `true` and `null_keys` are `count(*) FILTER (WHERE …)`.
  Observed on the local image: §13's `test_postgres.py`.
- As built, MySQL and Oracle (`compose.figures`, `compose.duplicates`, `compose.figured`; observed
  on the local images: §13's `test_mysql.py`, `test_oracle.py`). `nulls`, `empty`, `true` and
  `null_keys` are `COUNT(CASE WHEN … THEN 1 END)`; an empty text is one whose `length` is 0; `shortest`
  and `longest` are `MIN` and `MAX` of the `length`; a `min` and a `max` is the engine's own rendering of
  the aggregate, as it renders the column, and a `sum` as it renders a decimal (`rendered`:
  MySQL's sum of a `bit`, a `DECIMAL`, would otherwise go through the column's `CAST(… AS UNSIGNED)`, which clips a sum
  past 2^64 - 1 that `exact` calls exact): `CAST(SUM(c) AS CHAR)`,
  `TO_CHAR(SUM(c), 'TM9')` (Oracle writes 1E-20 and 1E+30 out in full there), `DATE_FORMAT(MIN(c), …)`,
  `TO_CHAR(SYS_EXTRACT_UTC(MAX(c)), …)`; a date's or a time's minimum and maximum is then read in
  canonical rendering 1 in the worker (`render.value`: MySQL's six digits and Oracle's signed year read as
  PostgreSQL's `to_jsonb` writes them), so that two engines' are compared as one, and Oracle's numbers
  are fetched as `Decimal`.
  - MySQL: a length is `CHAR_LENGTH(c)` (`LENGTH(c)`, octets, for bytes; the server strips a `CHAR`'s
    padding, so a blank one is empty and 0 long, as PostgreSQL's); a text's distinct values are those of
    its key text's order expression, `CAST(CONVERT(c USING utf8mb4) AS BINARY)`.
  - Oracle: a length is `LENGTH4(c)`, a `char`'s `NVL2(c, NVL(LENGTH4(RTRIM(c, ' ')), 0), NULL)` (a blank
    one trims to NULL: it is empty and 0 long, as PostgreSQL's), bytes' `LENGTH(RAWTOHEX(c)) / 2`
    (`LENGTH` of a `RAW` counts its hexadecimal digits, observed); distinct values are `COUNT(DISTINCT c)`
    under the session's `NLS_COMP = BINARY`, a `CHAR` as stored (a column pads every value alike, and
    `RTRIM` would make a blank one NULL: `RTRIM` is the key grouping's). A column that declares a
    collation other than `BINARY` (only a database whose `MAX_STRING_SIZE` is `EXTENDED` takes one: the
    local image refuses it, ORA-43929, not observed) gets ` COLLATE <its collation>` in its declared
    type and is counted as `c COLLATE BINARY`: compared, rather than left out as this section first said.
  - Duplicate keys group the population by the key texts' order expressions, `HAVING COUNT(*) > 1`, as
    PostgreSQL's (`RTRIM` for an Oracle `char` key, the binary cast for MySQL's texts).

**Sums: exact, or not compared** (`precheck.exact(n, m, scale, digits)`). Oracle rounds silently: `SUM`
of 1E30, 1E-20, 5 and 1.5 printed 32 digits without the 1E-20. `digits` is the side's
`Engine.sum_digits`: None for PostgreSQL (exact always), 65 for MySQL, 38 for Oracle. With n the
non-NULL count, m the larger of |min| and |max| and s the declared scale (an `integer` class is 0), a
bounded side's sum is exact when the column declares a scale and `digits(m) + max(s, 0) + digits(n) ≤
digits`; a column without one (an unconstrained Oracle `NUMBER`, `FLOAT(n)`) never is. A rendered `min`,
`max` or `sum` that is not a finite number (PostgreSQL `numeric` holds `NaN` and `Infinity`) compares as
its text, and that column's sum is not compared. Exact sums compare as `Decimal` (`3.50` equals `3.5`).

**Oracle's `''`** is NULL (ENGINES D7). `nulls` is compared as counted, as a comparison would report it;
when exactly one side is Oracle and the gap equals the other side's `empty`, the text adds the
`emptyAsNull` bridge and stays `differs`.

**Verdict:** the first that holds (`rank`); pairs sort by rank, then by the number of differing figures
(each figure whose verdict is `differs`, and the row counts when they differ), then label.

| | Verdict | When |
|---|---|---|
| 1 | `unpaired` | no counterpart by name, or an ambiguous name |
| 2 | `refused` | a run of this spec would be refused |
| 3 | `not_read` | a side's read failed with a code of §5; what was read is still printed |
| 4 | `rows_differ` | row counts differ |
| 5 | `keys_null_or_repeated` | a side holds rows without a key, or a key several rows hold |
| 6 | `columns_not_compared` | a schema code of the first group |
| 7 | `values_differ` | a compared figure differs |
| 8 | `incomplete` | a check of the asked level was skipped by a limit (200 or 40 columns, `distinct_max_rows`) or by D15 |
| 9 | `schema_differs` | only schema codes of the second group |
| 10 | `no_difference_observed` | nothing above |

## 5. Reading (`runner/precheck.py`; additions to ENGINES §5 and §7, built first on PostgreSQL)

- `Relation.columns[*]` gains `nullable` (None for a PostgreSQL view's or materialized view's column)
  and `size`, the `(n)` or `(p,s)` the type declares after their unit: `("digits", p, s)` for a decimal
  (s is 0 when it declares none beside p), `("characters", n)` for a text, `("fraction", n)` for a time;
  None when it declares none. `Relation.facts` gains `key` (the primary key's columns in the key's order,
  or None) beside the `kind` of ENGINES §5 (`table`, `view`, and on PostgreSQL `materialized view`
  and `foreign table`), from `pg_attribute.attnotnull` and `pg_index`; `COLUMNS.IS_NULLABLE` and
  `STATISTICS`; `ALL_TAB_COLUMNS.NULLABLE` and `ALL_CONSTRAINTS`: all visible to a read-only account. The
  PostgreSQL catalogue query is the adapter's `_RELATION` (`connectors/postgres/engine.py`), which now
  also groups by the relation's oid (ENGINES D3's one changed statement, changed again).
- `Engine.describe(session, schema, name, *, side=None)`: the keyword is the files engine's ([FILES.md](FILES.md)
  §6), read by the files engine alone (the side's `SideConfig` at planning, the stored plan afterwards);
  a pre-check passes none, which that engine refuses.
- `Engine.sum_digits: int | None` and `Engine.empty_null: bool` (it stores `''` as NULL, ENGINES D7: the
  pure module names no engine, so the fact comes with the side); `Engine.built(column, cls, declared, key)
  -> str | None`, what its statements read a planned column of that class and declared type as (a key
  column's `key_text`, any other's `render`; None on PostgreSQL, whose statements are built of no declared
  type), handed to §7's `changes` as data;
  `Engine.relations(session, schema) ->
  list[tuple[str, str]]` (each readable table's and view's name and kind, by name; PostgreSQL's schema as
  spelled, else folded, else the session's current schema); `Engine.figures(session, side, wanted) ->
  list[str | None]` (one statement, one row of rendered texts; `wanted` is `(figure, column)` pairs, the
  column None for a figure of the rows, the figures `rows`, `null_keys`, `nulls`, `empty`, `shortest`,
  `longest`, `sum`, `min`, `max`, `true`, `distinct`; `side` carries its `classes`);
  `Engine.duplicates(session, side) -> tuple[int, int]`. The
  two attributes are 65 and false on MySQL, 38 and true on Oracle; the last item has MySQL's and Oracle's
  methods.
- `compose.py` gains `figures` and `duplicates` (its builders carry no `_sql`) over the existing
  primitives plus `length`, `distinct` and `rendered` (below); PostgreSQL's builders join `connectors/postgres/queries.py`,
  composed with `psycopg.sql`: `figures_sql` over `_filter` alone (§4's population; `_population` would
  drop the rows without a key), `duplicates_sql` over `_population` and `_canonical_keys`. Identifiers
  come from the catalogue and literals only from the plan's filter: a pre-check adds no value to any
  statement. PostgreSQL sends a statement without parameters when it binds none, so a plan made of a
  catalogue alone may name a column that holds a `%`.
- `tables/plan.py`: `issues(targets, spec, relations)` lists a pair's refusals in the order `plan`
  raises them; `resolved(targets, spec, relations)` is pure and raises the first (`planned` is ENGINES'
  `engine.planned`, and each side's engine comes from its target); `planning(targets, spec) ->
  Planned(plan, relations)` connects, describes, plans and probes; `plan()` returns its `.plan`, byte for
  byte today's (`test_postgres_pair_plan_unchanged`). The plan is made in steps (each side's names and
  grants; the pair's counterparts; what each side compares; with a MySQL or Oracle side the classes
  read, then the pair's keys and families): `issues` lists every refusal of the first step that refuses,
  since a later step is judged on what the earlier ones gave. `planning` describes every table side
  before it plans, so a side that cannot be described is refused before another side's column.
  `described(engine, session, who, schema, name)` and `probed(engine, session, name, side)` word a
  refused relation and a refused probe on a session the caller holds.
- `tables/figures.py` is what a pre-check reads of one side inside its snapshot: `classed(side,
  relation)` gives a PostgreSQL pair's plan side the classes its plan does not store; `read(engine,
  session, side, wanted, duplicates=, distinct_max_rows=, timeout_s=)` sends `wanted` 150 columns a
  statement (the figures of the rows with the first), then the duplicate keys, then the distinct counts
  4 columns a statement, each bounded by the timeout the snapshot was opened with; `failure(exc,
  timeout_s)` is a failed read's code of the table below and what a line shows beside it. A read that
  fails ends the side's: its check (`figures`, `duplicates`, `distinct`) is recorded and what was read
  before it stays. The duplicate keys sort or hash too, so they are skipped with the distinct counts.
- As built, MySQL and Oracle (`connectors/tables/mysql.py`, `oracle.py`, `compose.py`):
  - The catalogue statement (`_RELATION`) also reads `COLUMNS.IS_NULLABLE` and the primary key's columns by
    their place in it (MySQL: one `JSON_OBJECTAGG` over `STATISTICS`, which lists a key column the
    account may not read too; Oracle: each column's `position` in `ALL_CONS_COLUMNS` of its `P`
    constraint, `ALL_TAB_COLUMNS.NULLABLE`): one statement still. A view's column says no `nullable` on
    every engine (MySQL and Oracle report a guess). `size` is `engine.sized(cls, declared)`, shared with
    PostgreSQL's adapter, on MySQL's `COLUMN_TYPE`; Oracle's comes from its catalogue: `("digits", p, s)`
    (`NUMBER(*,s)` is 38 digits), `("characters", n)` for a `CHAR`-semantics or national text,
    `("octets", n)` for one declared in bytes (a fourth unit: 100 octets and 100 characters are not one
    size), `("fraction", n)` for a `TIMESTAMP`; an unconstrained `NUMBER` and a `FLOAT(n)` declare none.
  - `compose.Dialect` gains `rendered(value, cls, declared)` (what `render` writes of any expression of
    the column's type: `render` is it over the column), `length(column, cls, declared)` and
    `distinct(column, cls, declared)`; an empty is `length = 0` and a sum's exact text is `rendered` over
    `SUM(c)`, so neither `is_empty` nor a sum primitive was needed. `compose.figured(side, wanted)` reads
    the one row (§4's canonical moments).
  - `relations`, `figures` and `duplicates` on both engines (§3 says what each lists); Oracle's
    `_Session.query` takes `decimals` to fetch numbers as `Decimal`.
  - `figures.failure(exc, timeout_s, elapsed_s)`: `figures.read` times each statement, and a MySQL 1317
    once the statement ran its timeout is `timeout` (observed when a `KILL QUERY` ends one; the server's
    own limit answered 3024 for every statement here).
  - MySQL 1054 (a column that is gone) joins the denied class, `TargetDenied` (§7's V5; ENGINES §11).

**One item.** Each side in one `engine.snapshot(target, timeout_s=precheck.statement_timeout_s)`:
describe, `issues` and `engine.probe` on that session, then `figures`, `duplicates` and the distinct
batches. Two targets are read at once; a pair whose sides are one target reads side a, then side b, on
one session slot (two snapshots in turn; `apart_s` is their gap). No row crosses the wire. Each side
records `at`, `engine`, `server`, `consistency`. The result is saved in one statement. As built
(`runner/precheck.py`):
- The plan needs both relations before either side is read, so a pair of one target describes and
  probes both sides on side a's session, reads side a's figures there, and opens side b's snapshot only
  for side b's figures (at level `schema`, or when a run would be refused, it opens none: side b's `at`
  is then side a's). When side a's session did not open, side b is not tried: its failure is side a's.
- A refusal ends the read on its session: PostgreSQL ends a snapshot's transaction with the refused
  statement, so the session is asked nothing more. Two targets are described and probed at once, and
  each one's refusal is listed, side a's first; on one target the first refusal is the only one, as
  it is for `planning`. Figures are read only once neither probe refused.
- A stored version that no longer compiles against today's targets (one of them is no longer declared)
  is `refused` with the loader's messages, and no session is opened.
- A fault of the read's own code is the pair's failure, `error (<its class>)` on both sides, and the
  pair is not read again; a read whose result the store never took is tried `TRIES` (3) times, then
  settled `not checked: side a's read failed (NotSettled)` without asking a target again.

**Worker.** `_sweep_once` reads `repo.prechecks_due(conn)` in its first connection block and starts
`self.checks[(precheck_id, n)]`, as `self.draws`; a pair starts when each target it names has a free
pre-check session, and holds one a target. `_check` catches everything but its cancellation, logs
`failure(exc)` and never calls `fail_run`. The first sweep after taking the lock sets `running` items
back to `pending` (`repo.requeue_prechecks`); an item's result is replaced whole. The sweep cancels a
cancelled pre-check's tasks, which send the engine's cancel. `ds precheck run` polls beside an
in-process `run_worker(ctx, stop=stop, schedules=False)`, as `ds run`. As built: every sweep
sets back to `pending` the `running` pairs no task of this worker reads (on the first sweep after the
lock, all of them; later, one whose task ended without settling it, the store being unreachable),
through `repo.requeue_prechecks(conn, pairs)`; a task whose pair is no longer due is cancelled; a fault
that escapes a task is logged by its class and the pair waits `worker.lease_seconds` before it is read
again; and a fault reading the due pairs is logged and leaves the sweep to its runs. An unreachable
side's second read is made by the pair's own task, a lease period later, the pair still `running`
(`tries` counts claims, not reads). `claim_pair` sets the pair's `claimed_at`, and its
task renews it every third of `worker.lease_seconds` while it reads the pair (`repo.hold_pair`, as a slot
renews its leases); a task that is cancelled (the worker stops: `ds precheck run` interrupted, `ds worker`
stopped) sets its pair back to `pending` if it is still `running` (`requeue_prechecks`), so that no pair
stays `running` with no reader. A killed worker renews no claim: §10's banner counts its pair as read by
none once the claim is `prechecks.HELD` (3) lease periods old.

**Failures** (codes only, never a driver's text; what was read stays; the pair is `not_read`):

| Code | When | Retry |
|---|---|---|
| `timeout` | `StatementTimeout`, PostgreSQL 57014, or MySQL 1317 once the limit has passed | never |
| `unreachable` | ENGINES' `UNREACHABLE` | once after `worker.lease_seconds`; then every pending pair of the pre-check naming that target is settled in one statement |
| `auth_refused` | `TargetAuthRefused` (MySQL, Oracle; a PostgreSQL refusal is an `OperationalError` today and reads `unreachable`) | never. One statement (`repo.settle_target`) settles the pending pairs of every unfinished pre-check naming that target, so a stale password is tried by at most `sessions_per_target` sessions before a person acts (Oracle's default profile locks an account, the one live runs use, after 10); a pre-check made later tries again |
| `temp_space` | 53400, `TargetTempSpace` | never |
| `snapshot_lost`, `denied`, `error` | `SnapshotLost`; `TargetDenied`; any other class, by name | never |

**Large tables.** `counts` and `columns` are one pass in constant memory; a statement past its timeout
has no figures: raise the timeout, or pre-check a filtered view or a replica. Distinct counts sort or
hash (observed on PostgreSQL at 10M rows: 259 MB of temporary files for the keys, about 1.4 GB for six
columns in one statement, hence batches of 4) and are skipped, with the duplicate keys, when the side's
`rows` is over `precheck.distinct_max_rows`; the pair says both (`duplicate keys and distinct values not
counted: …`).

A failure of any check with code `unreachable` (a session lost during the figures too) is read again
once; with a refused credential among a pair's failures nothing is read again. The transaction that
settles a pair settles its target's other pairs first, the refused credential's before the unreachable
one's: that statement locks the pre-checks' rows in the order of their ids, so two tasks never wait for
each other in a circle.

**Settings** (`Settings.precheck: PrecheckSettings`, `_CLOSED`, all defaulted, no secret):

| Setting | Default | Range |
|---|---|---|
| `sessions_per_target` | 1 | 1 to 4 |
| `statement_timeout_s` | 900 | 60 to 86400 |
| `distinct_max_rows` | 20000000 | 0 (never) to 10^12 |

Build constants: 500 pairs, 20 unfinished pre-checks (K10), 200 and 40 columns, names cut at 128 characters.

## 6. Privacy

- **Never fetched:** a key value, a text value, a distinct value, a row.
- **In worker memory only:** each number and time column's rendered `min` and `max`, sums not shown, and
  the counts and lengths a filter withholds (below).
- **Stored and exported:** counts, length ranges, verdicts, closed codes, relation and column names,
  declared types and classes, and sums that are shown; under a filter, of a protected column, only its
  NULL count beside the verdicts (below).
- **No unapproved filter (D15).** A count under an author's filter answers yes or no about any
  column, tokenised ones included (`where id = 7 and salary >= 50000` has 1 row or none). So in oidc
  mode, when a side declares `where` and `approvals.gate`, read unlocked, does not admit the version,
  the worker reads that pair at level `schema` and sends no probe: the filter reaches no target, and
  the pair is `incomplete` with the reason. Listed `columns`, the other table member of APPROVALS'
  `population_filters`, select no rows and gate nothing here. Tokens and developer mode have no gate,
  as for their runs (§16). A pre-check asked at level `schema` leaves only the probe out for such a
  version: its level was read, so the pair keeps its schema verdict.
- **A `regex` key map is not gated.** `approvals.needs` counts it under no scope,
  so under SSO a version that holds nothing else needing approval is admitted without one, its runs start
  and the rule above, which looks at `where` alone, reads it at its level. Its pattern is a predicate over
  raw key values that reaches the target in every statement that reads the key text (PostgreSQL's `CASE
  WHEN key ~ <pattern> THEN … END`; a MySQL or Oracle side refuses a `regex` key map): a pre-check's
  `null_keys`, as a run's `skipped_a` and `skipped_b`, counts the rows whose key it does not match,
  protected keys included. So successive versions can narrow down a key's raw values, in runs and
  pre-checks alike (§16). A `prefix_pad` key map's test is fixed: whether the key text is digits.
- **A sum is shown as a number** (`precheck.shown`) only when all hold, else as its verdict with the
  reason: both sides' sums are exact; neither side declares `where` (two filters one row apart would
  give that row's value); `canonical_policy(spec.security).resolve` gives `$.<column>` no class (listed
  under `clear`, or unlisted under `unlisted: clear`; never the default `unlisted: tokenize`); and in
  oidc mode the gate admits the version. A catalogue pair has no policy: verdicts only. So a number
  leaves only where findings already show the column's values in clear.
- **Under a filter, a protected column's counts are verdicts** (`precheck.filtered`):
  when a side declares `where` and the spec's policy gives the column a class (or the spec has none), its
  `true`, `empty` and `distinct` counts and its length range are kept as their verdicts with `figures
  withheld: side a declares a filter and $.<column> is protected by the spec's security policy`, as its
  sum is: as numbers, a filter that names only the key would tell one row's boolean, emptiness and
  length, and two rows' equality through `distinct`. A cleared column's stay numbers (its sum is withheld
  under any filter, above).
- **What figures under a filter still tell.** The rows and every compared column's NULL count stay
  numbers, and every other figure a verdict that says whether the two sides agree. Of the rows a filter
  selects, whoever reads the pre-check so learns how many there are (yes or no about any column the
  filter names); how many hold NULL in each compared column, protected and tokenised ones included,
  though the filter names only the key; and whether the two sides agree in each other figure. Filters of
  one row a side (one table on both sides, `id = 7` on side a and `id = 8` on side b) tell whether each
  row's value is NULL, of a number, a moment or a boolean whether the two values are equal (their sums,
  minima, maxima or `true` counts agree), and of a text or bytes whether they are as long and whether
  both or neither is empty. A run of the same spec reads and counts the same rows (`rows_a`) but compares
  key with key: it shows none of these facts of a protected column. §16, APPROVALS' threat row and DEPLOY
  §7 say the same.
- A pre-check is otherwise not gated by approvals: authors triage drafts.
- Catalogue names are untrusted strings: CSV cells go through `reports/csv.escape_cell`; drafts, §3.
- No agent tool reads a pair, a verdict's line or a figure: the chat's agent has no pre-check tool, and
  the MCP server's `list_prechecks` gives of each pre-check its id, level, source and times and its
  pairs counted by status and by verdict ([MCP.md](MCP.md) §7). Of a pre-check of one pair that count is the
  pair's verdict, which under one-row filters says whether two protected values agree (above; MCP's
  N9). `precheck.create` names target ids and schemas; no audit detail, log or notification carries a
  relation name, column name, declared type or value.
- The target's own logs hold the aggregate statements, MySQL filter literals as hex (ENGINES §14 point 5).

## 7. The schema guard (`runner/drift.py`; pure parts in `core/precheck.py`)

"The guard" in ENGINES is its key-order guard and its G1, G2 are SSRF messages: this one is always *the
schema guard*, its check sites V1 to V5, its texts E, U and W (§10).

**A side read from files** ([FILES.md](FILES.md) §12). Its relation is its plan's: `kind: files`,
`key` null, each planned column with its declared type and class, described after a stat check of the
pinned files (`Engine.describe(…, side=<the stored plan>)`); at creation the guard records the relation
the plan was made from. Only other files could change that shape, and a changed file fails the run
(`FilesChanged`) before any look judges it, so looks V2 to V5 find no change on a fixed side; V1 judges a
new export's shape against the basis run's, as for a database.

**Shape.** `shape(relation)` is `{kind, key, columns: {name: [declared, cls, nullable]}}`; its `sha256`
is over `jsonio.dumps(…, sort_keys=True)`. Column order, defaults, comments, indexes, grants and the
server's version are not in it. As built, a column the account may not read is not in it
either (N10): a column whose grant was revoked reads as one dropped on every engine, as it already did
where the catalogue lists only the granted ones, and E1 says "is gone, or the role may no longer read
it". A relation that is gone, or none of whose columns the account reads, has no shape (`null`, whose
hash is that of `null`).

**`ds.runs.guard`** (format 1): `{format, mode, baseline, basis: {run_id, predates, changes: {a, b}} |
null, a: {relation, roles, common, planned, sha256, seen, seen_sha256, changes}, b, adopted, since:
{cycle, at, where}, accepted: {by, name, at, cycle, where, a, b}, checked: {cycle, at, where,
error_type, side}, unchecked: [{cycle, where}]}`. `roles` maps each planned column to `key`, `compared`, `filter` or `strata`
(`precheck.roles(side)`, of several the first). Names and declared types live here only: `deps.shown`
leaves `guard` out of every run row it shows (run lists and pages included), and `GET /v1/runs/{id}`
prints lines built from it. As built:
- A side's `changes` are those that stand against the run's own plan; those against the basis at its
  creation are `basis.changes`, kept apart so that a later look does not replace them. `basis.predates`
  says the basis is no baseline and was judged by its plan alone (it predates 016, the guard adopted it,
  or its look at its creation failed). A change is `{kind, breaking, column, was, now}`,
  `was` and `now` a column's `[declared, cls, nullable]` (a key's columns, a relation's kind).
- `seen` holds the shape last seen only when it is not the planned one (`seen_sha256` tells a relation
  that is gone from one that is as planned): a wide table's shape is held once a side, not twice.
- `common`: the side lists no `columns`, so a run takes the columns both sides have (W's "a new run
  would"). `adopted`: the cycle a run from before 016 was first looked at in, else null. `since`: when
  the changes that stand were first seen. `accepted` also holds who by name, the cycle the change was
  seen in and `where` (`creation` for a run created paused). `checked.side`: the side a look could not
  reach. `unchecked` lists each look that could not be made, its cycle and `where`,
  kept across the looks after it (`checked` holds the last look only), so that no line claims a look that
  was not made (absent: none).

**Changes** (`precheck.changes(before, after, roles, *, basis=None, built=None)`, closed kinds
`precheck.BREAKING` and `OTHER`; `basis`: the basis run's roles, at a creation; `built`: the side's
`Engine.built`, compared for the old and the new declared type; without `before` only what is missing is
judged; the breaking ones come first):

| Against | Breaking | Other |
|---|---|---|
| the run's plan | `relation_missing`; `column_missing` (a planned column is gone or unreadable; MySQL cannot tell which); `class_changed` (a planned column); `key_render_changed` (a key column whose key text would render differently: a number's scale, which every engine's text of a value holds, or a key text the side's engine builds otherwise of the new declared type, an Oracle `NUMBER(p,s)`'s mask of its precision or MySQL's cast of a `bit`: ENGINES §8); `render_changed` (a compared or strata column whose declared type changed within its class into one the side's engine renders otherwise, Oracle's `DATE` and `TIMESTAMP(n)` either way, MySQL's `tinyint` and `bit(n)`: ENGINES §9) | `column_added`, `column_removed` (unplanned), `type_changed` (a declared type within its class that the side's engine reads alike, a key column's included: a widened `varchar(20)`, `NUMBER(10)` to `NUMBER(12)`, MySQL's `int(11)` respelled `int`, Oracle's `NUMBER(*,2)` for `NUMBER(38,2)`, any on PostgreSQL but a key's scale), `nullable_changed`, `key_changed`, `kind_changed` |
| the basis, at creation (*narrowing*) | `compared_lost` (a column the basis read is not read now); `class_changed`; `key_render_changed` | the same (a type the engine renders otherwise is `type_changed` there: the new run's plan reads it as it is) |

**When it looks:**

| | Where | Against | Cost |
|---|---|---|---|
| V1 | `create_run` (batch, soak), on the relations `planning` described | the basis | one store read |
| V2 | `scan._snapshot`, on each pass's own session before its cursor opens: every cycle and resume | the plan | one catalogue statement a side |
| V3 | the worker's `self.guards[run_id]`, for a running key-list run with a table side whose cycle fell due (`cycle.due`) with `guard.checked.cycle` below it; its census and draw wait for it, two tries at most (Faults) | the plan | one short session a side |
| V4 | `resume_run` of a run paused by the schema guard | the plan | the same |
| V5 | `worker._scan`, `_handle` and `_draw`, before `fail_run`, when a table read or a draw's frame raised `UndefinedColumn`, `UndefinedTable`, `InsufficientPrivilege` or `TargetDenied` (ENGINES §11's denied class, to which §15 adds MySQL 1054, a dropped column) | the plan | the same |

**The basis** is the newest earlier run of the `spec_id` with `guard.baseline` (`runs_spec_idx`). When no
earlier run of it is a baseline, it is the newest one's stored plan (`params.tables.*.columns`), against
which `compared_lost` alone is judged, but never that of a run the guard created paused and no person
accepted: so a run from before 016 (no `guard`), one the guard adopted, or one whose look at its creation
failed. With
neither it is null.
`compare_one` judges its fresh plan against the basis and returns the lines as `warnings`; it never
pauses, records no guard and is no basis.

**What happens** (`precheck.verdict(mode, found, creation=False)`, pure: `paused`, `warned`, `refused` or
None; the pure parts are `core/precheck.py`'s, as §13 tests them there):

| Change | `finding` (the schema's default) | `pause` |
|---|---|---|
| breaking, V2 to V5 | paused; resume refused while it stands (U1) | the same |
| narrowing, V1 | created paused, before any table row is read; a resume accepts | refused at creation (E6, a `ValueError`: 422, nothing written, no pair seated; a schedule records `not_started`): a run that could only be cancelled would hold its schedule in `overlap` |
| any other | recorded, one warning event, the run goes on with its plan | paused (at V1, created paused); a resume accepts |

- **A trip** is one transaction: `repo.held_guard` (the run's row lock, under which the cycle is
  compared and the look judged), `repo.set_guard`,
  `set_run_status('paused', when=('running',), stop_reason=SCHEMA_PAUSE)`, then the audit rows `paused`
  and, last, `schema.drift`, which names the `paused` row's chain seq (`paused_seq`: the two
  share a dedup key, §9). V2 raises `drift.Tripped`, which `_scan` catches before its failure branch;
  at V5 `_handle` hands its keys back first, as for a refused credential. `SCHEMA_PAUSE =
  "run_paused_schema_drift"` joins `resume_run`'s `cleared`. In `create_run` the order is `create_run`,
  `licence.pair`, SCHEDULES' `schedule.fire`, `paused`, `schema.drift`.
- **Once per state.** A row is written only when a side's observed shape differs from the one last
  recorded (`seen_sha256`): a standing change is one row and one event, however many cycles follow.
- **Resume** describes again. Accepting sets `guard.accepted` and `baseline: true`, audited in the
  `running` row; an accepted shape never trips again. A run capped at creation gets
  `stop_condition:maxKeys` back when `runs.keys_cut` is set.
- **Baseline.** `guard.baseline` is true unless the run was created paused by the schema guard and not
  accepted, so a cancelled paused run never hides a change from the next run.
- **Faults.** A side unreachable: V2 resumes as the scan does; V4 answers U2; V3 tries again once after
  `lease_seconds`, then records `checked: {cycle, error_type: TargetUnreachable}`, the look among
  `unchecked`, and one `schema.drift {checked: false}` row (a warning), and the census and the cycle go on: the per-key reads then meet the
  target as today (`connect_error`, the stop conditions, their alert), and V5 still catches a breaking
  change. A refused login pauses as `AUTH_PAUSE` (ENGINES §11). Any other fault of the schema guard's
  own code fails open the same way, with its class in `error_type`: never silent, never a stalled
  lease. A V5 that finds nothing breaking falls back to `fail_run`.
- **Before 016.** A run without `guard` is adopted at its first check: the shape seen then is recorded
  (`basis: null`, `baseline: false`) and only missing relations and columns are judged. No baseline, it is
  still a basis by its plan (above) while no run of the version is one.
- `purge.abandon` cancels a run paused by the schema guard past its retention days, as any paused run.
  Under SCHEDULES it is live: later firings are `overlap` until a person resumes or cancels it.

**Invariant.** Every baseline run of one `spec_id` read the columns its predecessor read, as the same
classes, unless a person accepted the change: READINESS's streak never crosses an unaccepted narrowing.

**As built** (`runner/drift.py`: `created`, `compared`, `check`, `cycle_start`, `failed`,
`resuming`, `unchecked`, `lines`, `told`, `warnings`, `sentence`):
- **V1.** `create_run` plans through `tables.planning`, reads `repo.guard_basis` inside its transaction
  and calls `drift.created`, which returns the guard and the `schema.drift` detail or raises E6 before
  the run is inserted. A run created paused is inserted `paused` (its `started_at` is set by its
  resume), so no scan, census, draw or lease touches a target. The basis is judged by its `planned`
  shape and its roles: a change the basis run met and accepted during its cycles is told again at the
  next creation (a warning, or a run created paused under `pause`), since accepting it there meant
  going on with that run's plan, not a new baseline. V1 is the look at cycle 1's start: `checked.cycle`
  is 1, so V3 first looks at cycle 2.
- **V2.** `scan._snapshot` describes the relation on the pass's session; `_Stream.open` sends the scan's
  statement only after both sides were described and `drift.check` judged them. `where` is `scan` for a
  pass from the cycle's start and `resume` for one that resumes after a checkpoint.
- **V3.** The sweep finds the running key-list runs with a table side whose `checked.cycle` is below
  their cycle (`drift.due`; a run without a guard too) and starts `self.guards[run_id]` once the cycle
  fell due (`cycle.due`, computed once a run and cycle); until the look is recorded, that cycle's census
  and draw are not started, due or not. The second try is made by the task itself, a lease period later.
- **V4.** `drift.resuming` looks before the resume's transaction, which records the guard, audits
  `running` and, when the look saw a new state, `schema.drift` after it. A resume accepts when the run
  was created paused, or when the spec pauses on schema drift and changes stand; under `finding` a run
  paused for a breaking change resumes, once that change is undone, without accepting anything. Under
  `pause` a change that breaks nothing, recorded beside a breaking one, is accepted by the resume made
  once the breaking one is undone: §11's `accepts` says so from the pause on.
- **V5.** `drift.DENIED` is `UndefinedColumn`, `UndefinedTable`, `InsufficientPrivilege` and
  `TargetDenied` (MySQL 1054 and ORA-00904 are of the denied class). `_handle` releases its leases, then
  looks; a run the guard paused there reads on a fresh session once resumed (`halted`). V5 pauses only
  for a breaking change: under `pause` too, another change found there is recorded and the run fails.
  `_draw` looks too: a hash sample's frame that meets a dropped strata or key column (MySQL
  1054, `UndefinedColumn`, ORA-00904) pauses the run, its draw still due
  (`sample._Frame` turns no denied code into a `DrawError`).
- **A look is judged under the run's row lock** (`repo.held_guard`, then `repo.set_guard`): of two looks
  at once (two slots meeting one dropped column) the second is judged on what the first recorded, so a
  state is recorded once. The cycle is compared under that lock; `set_guard`'s own `cycle` is unused.
- **Once per state, exactly.** A `schema.drift` row is written when a side's shape is not the one last
  recorded and the new state holds a change the last one did not; its counts are the changes the state
  adds, so that rows add up (§11's report). A shape that goes back to the plan's updates the guard and
  writes nothing. A run that is running again without V4 while a pausing change stands (its reason was
  replaced, say) is paused again by a `paused` row alone.
- **`where`** is `creation`, `scan`, `cycle`, `resume` or `read`; a line says `before any table row was
  read`, `at the start of cycle N` (scan, cycle) or `in cycle N` (resume, read).
- **Faults.** A look that cannot be made is `drift.unchecked`: `error_type` is `TargetUnreachable` for
  every unreachable class (PostgreSQL's `OperationalError` among them), else the exception's class; a
  spec that no longer compiles against today's targets is recorded the same way at V3. At V2 a fault of
  the describe is the pass's own (it resumes or fails as the scan does); at V5 any fault leaves the
  failure to `fail_run`. A fault of the guard's own code fails open at V1 and V4 too: the run is created
  with nothing held of its tables (its next look adopts it), a resume goes on, each with its
  `schema.drift {checked: false}` row. Every look recorded as not made joins `unchecked`.
- **`drift.Refused(status, message)`** is a `ValueError`: 422 for E6, 409 for U1 and U2. The API's
  `_runner_errors` lets it through to its `_HANDLERS` row.

**Spec.** Both schema copies drop the rule requiring `inferredSchema` beside `schemaDrift` and reword
its description. `rules._gating` accepts `schemaDrift/mode: pause` when a side is a table and refuses it
otherwise (`pausing a run on schema drift between two service sides`); `_IGNORED["inferredSchema"]`
reads `the inferred-schema guard for service sides is not built; schemaDrift acts on table sides only`.
The mode is spec content, in the diff an approver reviews; it adds no approval scope.

## 8. Storage: migration `016_prechecks.sql`

```sql
-- Table pre-checks and the schema guard (docs/mvp/PRECHECKS.md). A pre-check is no run and no evidence:
-- its rows go retention.standard_days after it was made; its audit rows stay. ds.runs.guard holds what
-- the schema guard recorded of a run's tables: added without a default or a check, so ds.runs is neither
-- rewritten nor scanned.
CREATE TABLE ds.prechecks (
    precheck_id  text PRIMARY KEY CHECK (precheck_id ~ '^PC-[0-9A-Z]{26}$'),
    source       text NOT NULL CHECK (source IN ('specs', 'catalogue')),
    level        text NOT NULL CHECK (level IN ('schema', 'counts', 'columns', 'distinct')),
    request      jsonb NOT NULL CHECK (jsonb_typeof(request) = 'object'),  -- ids, target ids, schemas, globs
    pairs        integer NOT NULL CHECK (pairs BETWEEN 1 AND 500),
    skipped      integer NOT NULL DEFAULT 0,
    evaluation   boolean NOT NULL DEFAULT false,     -- made in an unlicensed evaluation (LICENCE O1's label)
    created_by   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    cancelled_by text,
    cancelled_at timestamptz,
    finished_at  timestamptz,
    CHECK ((cancelled_at IS NULL) = (cancelled_by IS NULL))
);
CREATE INDEX prechecks_open_idx ON ds.prechecks (precheck_id) WHERE finished_at IS NULL;
CREATE INDEX prechecks_created_idx ON ds.prechecks (created_at);
CREATE TABLE ds.precheck_pairs (
    precheck_id text NOT NULL REFERENCES ds.prechecks ON DELETE CASCADE,
    n           integer NOT NULL CHECK (n >= 1),
    spec_id     bigint REFERENCES ds.specs,          -- NULL: paired by name
    label       text NOT NULL,                       -- the spec's name, or the relation's lower-cased name
    target_a    text NOT NULL, relation_a text,
    target_b    text NOT NULL, relation_b text,
    status      text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'done', 'cancelled')),
    tries       smallint NOT NULL DEFAULT 0,
    claimed_at  timestamptz,                         -- when a worker took it, renewed while it reads it
    verdict     text CHECK (verdict IN ('unpaired', 'refused', 'not_read', 'rows_differ',
                                        'keys_null_or_repeated', 'columns_not_compared', 'values_differ',
                                        'incomplete', 'schema_differs', 'no_difference_observed')),
    rank        smallint,
    differing   integer,
    rows_a      bigint,                              -- what the list prints, so that it never reads result
    rows_b      bigint,
    line        text,                                -- the verdict's line (precheck.line)
    result      jsonb CHECK (jsonb_typeof(result) = 'object'),   -- format 1: counts, verdicts, names
    finished_at timestamptz,
    PRIMARY KEY (precheck_id, n),
    CHECK ((status = 'done') = (verdict IS NOT NULL AND rank IS NOT NULL AND line IS NOT NULL
                                AND result IS NOT NULL))
);
CREATE INDEX precheck_pairs_due_idx ON ds.precheck_pairs (precheck_id, n) WHERE status IN ('pending', 'running');
CREATE INDEX precheck_pairs_worst_idx ON ds.precheck_pairs (precheck_id, rank, differing DESC, label, n);
ALTER TABLE ds.runs ADD COLUMN guard jsonb;
```

No trigger, no function, no `store/db.py` constant; `control-plane.sql` gains both. `store/repo.py`
gains a "pre-checks" section of plain SQL (`new_precheck_id` by `_sortable_id("PC-")`, `insert_precheck`,
`get_precheck`, `list_prechecks`, `count_open_prechecks`, `precheck_pairs`, `precheck_pair`,
`prechecks_due`, `claim_pair`, `save_pair` (it writes `rows_a`, `rows_b` and `line` beside `result`),
`settle_target`, `requeue_prechecks`, `cancel_precheck`, `finish_precheck`, `prune_prechecks`) and
`set_guard`, `guard_basis` and `held_guard(conn, run_id)` (the run's `cycle` and
`guard`, its row locked until the caller's transaction ends). `get_run` and `list_runs` select `r.*`, so `guard` reaches the sweep without
a query, and `deps.shown` leaves it out from the day the column exists. `precheck_pairs` selects every
column but `result` (and `claimed_at`). As built:
- `insert_precheck(conn, precheck, pairs)` stores the pairs pending; one that comes with a `verdict` (a
  name without a pair) is stored settled. `get_precheck` and `list_prechecks` give each pre-check with
  its pairs counted by status (`progress`) and by verdict (`verdicts`), reading no `result`.
- `save_pair` and `settle_target` take the pre-check's row lock in their one statement, so that
  `finish_precheck`, called next in the same transaction, sees every pair another task settled: two
  tasks that settle the last two pairs at once would otherwise each see the other's still open.
  `settle_target(conn, target, verdict, rank, a, b, precheck_id=None)` takes the line and result of a
  pair whose side a is on the target, and those of any other.
- `claim_pair` counts a try and answers the pair with its pre-check's `source` and `level`;
  `cancel_precheck(conn, precheck_id, by, line)` ends the pre-check and writes `line` on each pair it
  cancels (the CHECK lets a cancelled pair hold a line, never a verdict); `finish_precheck` answers what
  `precheck.finished` records, `failed` counting the pairs each code was met in.
- `precheck_pairs`' cursor is a position in the worst-first order, which a pair settled between two
  pages moves; `set_guard(conn, run_id, guard, cycle=None)` compares the run's cycle; `guard_basis(conn,
  spec_id, before)` is §7's basis, never a one-shot run.
- Also: `precheck_specs(conn, spec_ids=, names=)` gives what a pre-check pairs of stored
  specs (each version's id, name, version, two targets and two `table` members) from their bodies,
  compiling none: the versions named, else the newest version of each name, else of every name;
  `requeue_prechecks(conn, pairs=None)` sets back the running pairs named (all without `pairs`);
  `precheck_pairs(…, results=True)` also selects `result`, for the reports alone; `get_precheck` and
  `list_prechecks` also give `settled_at`, when a pair of the pre-check was last settled, which the
  banner of §10 is judged by.
- Also: the column `claimed_at` (set by `claim_pair`, renewed by `hold_pair(conn,
  precheck_id, n)` while a worker reads the pair) and, in `get_precheck` and `list_prechecks`,
  `claimed_at`, the newest claim of the pre-check's running pairs, which the banner reads too (§10).

**Result, format 1:** `{format, level` (the level read), `refused: [text], sides: {a: {engine, server,
consistency, at, relation, kind, sha256, failed: {check: code}}, b}, apart_s, schema: [{code, column,
group` (`not_compared` or `schema`, §4's two groups)`, a: {declared, cls, nullable}, b}], keys: {a: {rows,
null_keys, duplicate_keys, rows_in_duplicates, primary}, b}, columns: {<canonical>: {classes, figures:
{<figure>: {a, b, verdict, reason}}, reason` (why no other figure is compared)`}}, skipped: [code]`
(`unapproved_filter`, `columns`, `distinct_columns`, `distinct_rows`)`, lines}`. A `min` or `max` holds
its verdict only; a `sum` holds `a` and `b` only when shown, as exact texts; a `length` holds `[shortest,
longest]` a side (null: no value), and it, `empty`, `true` and `distinct` hold `a` and `b` unless
`precheck.filtered` withholds them (§6); a figure's verdict is `same`, `differs`, `not compared` or `not
checked`. A key column's schema entry (`key_…`) names side a's column. `lines` holds the pair's lines worst first, the verdict's first (`precheck.line(result)`);
`precheck.column_lines` words a column's figures. A stored result keeps no order of its members (jsonb
orders them by length, then by name): `column_lines` and the exports list a column's figures in
`precheck.ORDER` (`nulls`, `empty`, `length`, `sum`, `min`, `max`, `true`, `distinct`), and every surface
lists a pair's compared columns by name. Once a plan was made of both catalogues the
result holds `names: {a: {schema, name, keys, columns: {<canonical>: <column>}}, b}`, each side's relation,
key and compared columns as its catalogue spells them, which a draft is written from; `claim_pair` also
answers its pre-check's `request`.

**Retention.** `runner/purge.purge` ends by deleting pre-checks made more than
`retention.standard_days` ago (cascade; 1,000 a batch), counted as `Summary.prechecks`, which joins
`finished()` and `_finished`'s condition: a pass that deleted only pre-checks is audited
`retention.finished` too. Audit rows and `ds.runs.guard` stay. No hold or erasure applies: a pre-check
holds no key and no value. `ds purge` prints `deleted N table pre-check(s) past their retention`.

## 9. Roles, audit, alerts, licence, readiness, packs

**Roles.** `viewer` reads. `operator` pre-checks stored specs and cancels, as `POST /v1/runs`.
`spec_author` pre-checks a catalogue and reads a draft (it is authoring, as `POST /v1/profile`). No
step-up, no four-eyes object; §6's two rules are what approvals decide here.

**Audit** (added to AUTH §6 and RETENTION §8):

| Action | Actor | Object and detail |
|---|---|---|
| `precheck.create` | the caller | `PC-…` `{source, level, pairs, skipped, spec_ids` (50 at most, else their count) or `a: {target, schema}, b}` |
| `precheck.finished` | `system:worker` | `PC-…` `{pairs, verdicts: {<verdict>: n}, failed: {<code>: n}, seconds}` |
| `precheck.cancel` | the caller | `PC-…` `{pending, running}` |
| `report.download` | the caller | `PC-…` `{name}` |
| `schema.drift` | `system:worker`; the caller at V1 and V4 | `R-…` `{cycle, where, mode, checked, paused, paused_seq, basis, sides: {a: {breaking, other, kinds: {<kind>: n}, from, to}, b}, error_type}`: counts, closed kinds, ids, the seq of the `paused` row of its trip (null: it paused nothing) and 64-hex hashes only |
| `paused` (amended) | as today | reason `run_paused_schema_drift`, with `schema: {a, b}`, the two `to` hashes |
| `running` (amended) | the resumer | gains `schema: {accepted: {a, b}}` |

**Alerts** (ALERTS §2.4's recipe; `core/notify.py` only).
- `schema.drift`: the `EVENTS` row `"schema.drift": (_drift, frozenset({"schema.drift"}))`, the `Kind`
  member, the model `SchemaDrift` (`extra="forbid"`; the detail's members as integers, booleans,
  `Literal`s and `^[0-9a-f]{64}$`; no name), and one case each in `dedup` (`schema.drift|R-…|<to a>|<to
  b>`; a pause's, `schema.drift|R-…|<paused_seq>`), `severity` (`alert` when paused, else
  `warning`), `_run_text` and `_route` (`#/runs/R-…`).
- `run.paused`: rules name their events, and ALERTS' own example routes `run.paused`, not an event that
  did not exist. So `run_paused_schema_drift` joins `Pause` (hence `PAUSES`, `Reason`, a rule's
  `reasons` and the daily summary's named reasons) and `_PAUSED`; `_paused` projects it as any pause,
  severity `alert`; `RunPaused` gains the optional `schema` hashes. `dedup` gives a `run.paused` that
  holds them its `schema.drift`'s key, so a channel routed both sends one (the `run.paused`, written
  first) and holds the other back as a duplicate; with `dedup_hours: 0` both are sent. The
  key is the pause's own seq (`schema.drift|R-…|<seq>`, which its `schema.drift` names as
  `paused_seq`), not the shapes it saw: each pause is its own event (ALERTS §2.1), so a second pause of
  the same tables after a person resumed the run is told within `dedup_hours` too.
- So after the upgrade a rule that routes `run.paused` carries every schema-guard pause unchanged;
  routing `schema.drift` adds the warnings and the counts.
- As built: the model's kinds are `core/notify.Drift` (a test holds it equal to
  `precheck.BREAKING + OTHER`); `sides` maps `a` and `b` to `{breaking, other, kinds, from, to}`, the
  two hashes null where no shape was held or seen; `error_type` is set exactly when `checked` is false.
  The projector copies the row's own members and no other. `schema` is null in every other pause's
  `run.paused` facts (a side that is no table is null inside it). `concerned` takes a `schema.drift`
  row's cycle from its detail. A look that could not be made has the dedup key `schema.drift|R-…|None|None`:
  an outage that lasts for cycles is one notification a channel within `dedup_hours`.

**Licence.** Nothing is refused: no route or command calls `licence.admit` or `need`, and a pre-check
counts no pair. The one read is `licence.status`, which never raises, to record `evaluation`: true only
for one made in an unlicensed evaluation, a vendor build's or an older build's that enforced the
licence, and false for every one an open build makes, which carries no label (LICENCE §19; one an older
build that enforced the licence made keeps its own). Such a
pre-check is labelled everywhere (O1): each API item carries `evaluation: true`; the screen and `ds
precheck` print `Unlicensed evaluation` under `NOT`; its exports are named
`PC-…-unlicensed-evaluation-<name>` and headed the same (a CSV's second record, the PDF's line under
`NOT`). The routes join `test_nothing_locks_out`.

**Readiness, approvals, packs.** No module of them reads `ds.prechecks` or `ds.runs.guard`, and no pack
file changes: `coverage.csv`, which every pack carries, gains no column
(`test_precheck_is_never_evidence` asserts the import graph and that readiness, pairs, cycles and pack
bytes are unchanged by a pre-check and by a guard record). `guard` is one mutable record of the run's
latest state, so nothing issued for a cycle is built from it. What a pack holds of the schema guard is
the run's `schema.drift` and `paused` audit rows in its segment (PACK §4.3's rule): append-only, each
with its `cycle`, passing the raw-value gate by construction. No `PC-` row is in a segment and no `PC-`
id is in a run's rows, so `rawvalues._GENERATED` is unchanged. READINESS Y7 already prints a paused
run's reason.

## 10. Messages (binding; each asserted)

The API answers 422 unless a status is shown; the CLI prints `error: <message>` and exits 2.

- **K1** `give spec_ids, spec_names or all_specs, and only one of them`
- **A file side** ([FILES.md](FILES.md) §15 calls it its K1) `not supported: a table pre-check with a
  file side; its run counts rows, lines that are not rows and repeated keys` (`core.precheck.FILE_SIDE`):
  422 for a pre-check of two schemas where either target is a files target; for a pre-check of stored
  specs the pair is read as refused, its line this text alone and its item `a run would refuse`.
- **K2** 404 `spec 12 not found`; 404 `no spec is named customer-profile`; `spec 12 is listed twice`
- **K3** `spec 12 (customer-profile 1.4.0) is not a pair of tables: a table pre-check reads two table sides`
- **K4** `no stored spec is a pair of tables`
- **K5** `level must be schema, counts, columns or distinct`; `two schemas paired by name take level schema, counts or columns: distinct counts need a stored spec`
- **K6** 404 `target crm-old is not declared`; `target crm-api is a service: a table pre-check reads database targets`
- **K7** `spec_ids takes 1 to 500 ids and 812 were given`; `spec_names takes 1 to 500 names and 812 were given`; `812 stored specs are pairs of tables and a table pre-check takes up to 500: name them with spec_names or spec_ids`; `the two schemas give 812 pairs and a table pre-check takes up to 500: narrow them with include or exclude`
- **K8** `include and exclude take up to 20 patterns of at most 128 characters`; `include and exclude leave no table or view of either schema`
- **K9** `side a: cannot list schema APP of target crm-legacy (ORA-12541)`; `side a: schema APP of target crm-legacy holds no table or view the account can read`; `side a: schema APP of target crm-legacy holds over 5,000 relations: name fewer with include`. As built: a schema left out reads `the default schema` (`side b: the default schema of target crm-new holds …`)
- **K10** 409 `this installation holds 20 unfinished table pre-checks, the most it takes: cancel one or wait`
- **K11** 404 `table pre-check PC-… not found (pre-checks are deleted 90 days after they are made)`, the days being `retention.standard_days`; 404 `pair 7 of table pre-check PC-… not found`; 409 `table pre-check PC-… has ended`
- **K12** 409 `pair 7 is a stored spec (customer-profile 1.4.0): it needs no draft`; 409 `pair 7 has no table on side b: there is nothing to draft` (also for a side of two names alike); 409 `pair 7 was not read: there is nothing to draft yet` (pending, not read, or refused)
- **K13** (CLI) `--fail-on waits for the pre-check's end: leave out --no-wait`; `--a and --b come together, and without a spec`; `--include and --exclude take --a and --b`; `give --pair N or --all, and only one of them`; `no pair of PC-… can be drafted: none is two tables paired by name and read`

**Pair texts** (built by `precheck.compare` into a result's `lines`, and by `precheck.column_lines`,
`unpaired`, `totals` and `stalled`: no surface builds one; `precheck.line(result)` is the verdict's):
- `no table on side b is named customers (side a: APP.CUSTOMERS; paired by name)`; `two tables of side a are named alike ignoring case (Customers, CUSTOMERS): compare them in a spec`
- `a run of this spec would be refused: <the plan's message>` (`and 3 more`); of two relations paired by name, `a run of these tables would be refused: side a: table or view crm.gone not found or not visible`, or `…: target crm-old is not declared as a database`
- Not read, one per code: `not checked: side a's figures timed out after 900 s (raise precheck.statement_timeout_s, or pre-check a filtered view)`; `not checked: side a could not be reached (ORA-12541)`; `not checked: side a refused the credential (ORA-01017); no other pair of target crm-legacy was tried`; `not checked: side a's account may not read APP.CUSTOMERS or a column of it (ORA-00942)`; `not checked: side a's snapshot was lost (ORA-01555)`; `distinct values not counted: side a ran out of temporary space (53400)`; `not checked: side a's read failed (TargetRefused: MySQL 1064)`, or the class alone, `(TypeError)`
- `rows differ: side a 1,204,331, side b 1,204,298 (33 fewer on side b)`
- `keys: 12 row(s) of side b have no key (NULL, or its key map gives none): a scan does not compare them`; `keys: 3 key(s) of side a are held by more than one row (7 rows): per key they read identity_ambiguous`; `keys: not checked: no common primary key`
- `2 columns would not be compared: legacy_flag is on side a only; opened is a timestamp (DATE) on side a and a date on side b: <ENGINES P9's bridge>`; `: geom (SDO_GEOMETRY) cannot be compared: compare a view that casts it`; `: Email and another column of side a both compare as email: list each in a spec under a name of its own`
- A line of its own (rank 6): `key column id is numeric(10,2) on side a and integer on side b: a number key's text holds its scale, so no key of one side is found on the other: compare a view that casts it`, or for two families `…: the key text of a date is never that of a timestamp, so …`; a key column's other schema codes are clauses of the `schema:` line, `key column id is integer on side a and varchar(10) on side b`, `key column id is NOT NULL on side a and nullable on side b`
- `4 of 23 compared columns differ in a figure: amount (sum), email (nulls), opened (max), note (length)`
  (a line lists ten names, then `and 13 more`; a name is cut at 128 characters with `…`)
- `sum: side a 4999950000.00, side b 4999949987.50 (side b is 12.50 lower)`; `sum: differs (figures withheld: $.iban is protected by the spec's security policy)`, or `(…: side a declares a filter)`, `(…: spec 12 is not approved)`, `(…: paired by name, no security policy)`; under a filter, `true: differs (figures withheld: side a declares a filter and $.vip is protected by the spec's security policy)`, and so `empty`, `length` and `distinct`
- `sum: not compared (side a's sum could pass the 38 digits an oracle sum holds)`; `(side b's sum could pass the 65 digits a mysql sum holds)`; `(side a's NUMBER declares no scale)`; `(side a holds NaN or Infinity)`
- `min: differs; max: same (values are never shown)`; a shown sum that agrees ends `(same)`; a side
  without a value reads `no value`; `true`, `empty` and `distinct` read as `nulls` does
- `nulls: side a 120, side b 0 (differs); side b holds 120 empty string(s), which Oracle (side a) stores as NULL: declare typeMap {email: {as: string, emptyAsNull: true}} on side b to compare them as equal`
  (under a filter, of a column the spec's policy protects: `Oracle (side a) stores as NULL any empty string side b holds: declare typeMap {email: {as: string, emptyAsNull: true}} on side b to compare them as equal`, whatever side b holds; and a folded side's reason names no count, `side b's empty string(s) count as NULL (emptyAsNull)`, since the column's `empty` count is a verdict there, §6)
- `length: side a 6 to 40, side b 6 to 38 characters (differs)` (`octets` for bytes)
- `not compared: typeMap converts this column per row`; `: a float sum depends on the order rows are added in`; `: a large-object column`; `: level counts`
- `not applied: this spec's normalisers, tolerances, known-difference rules and compare.include or exclude; a comparison applies them`
- A check other than the figures names itself: `not checked: side a's distinct values timed out after 900 s (…)`; `duplicate keys not counted: side a ran out of temporary space (ORA-01652)`; a figure of a side that was not read is `distinct: not checked (<that side's line>)`
- Skipped: `not compared: 214 columns are compared and the first 200 were read`; `distinct values not counted for 12 of 52 compared columns: the first 40 were read`; `duplicate keys and distinct values not counted: 61,204,118 rows on side a is over precheck.distinct_max_rows (20,000,000)` (`duplicate keys not counted: …` or `distinct values not counted: …` where the pair has no key, or no column whose distinct values it counts); `not checked: spec 12 (customer-profile 1.4.0) declares a filter and is not approved: only its schema was read`
- `schema: 2 differences: amount is NUMBER(10,2) on side a and numeric(12,4) on side b; opened is NOT NULL on side a and nullable on side b`; `key: the primary key is (id) on side a and (id, region) on side b`; `key: keyColumns (customer_id) is not side a's primary key (id)`; `key: not checked: side b is a view (no primary key)`; `key: not checked: side b has no primary key`
- `no difference observed in the checks made (level columns): not parity; run a comparison to compare rows`
- `not checked: the pre-check was cancelled`
- Every pair: `side a: oracle 19.21, APP.CUSTOMERS, in 1 snapshot at 2026-10-07 10:02:11 UTC; side b: mysql 8.4, ledger.customers, in 1 transaction, a snapshot not observable (a MySQL view), 3 s later: not read at one moment` (`3 s earlier`; a side that was not read is `side a: not read`)
- Totals, a phrase a verdict in rank order, zero counts left out: `12 pairs: 1 unpaired, 1 a run would refuse, 1 not read, 2 with differing row counts, 1 with null or repeated keys, 1 with columns not compared, 1 with differing figures, 1 not fully checked, 1 with schema differences only, 2 with no difference observed (not parity)`; an unfinished or cancelled pre-check ends `; 3 pending, 2 cancelled`
- Banner: `no worker has taken this pre-check for 10 min: start ds worker, or ds serve`; `Unlicensed evaluation` (of one made without a licence, by a vendor build or by an older build that enforced the licence).
  The banner stands for an unfinished pre-check none of whose pairs is being read and none was settled
  (or, with none settled, that was made) 10 minutes ago or longer, with the whole minutes since; the list
  shows the one of the pre-check longest so. A pair is being read while it is
  `running` and a worker took or renewed its claim within `prechecks.HELD` (3) lease periods (§5): a pair
  a killed worker held no longer keeps the banner away, and one a stopped worker held is `pending` again.
- A verdict as words (`precheck.worded`): its code's words (`rows differ`, `not read`, `keys null or
  repeated`), and the best one `no difference observed in the checks made (not parity)` (N1). A pair no
  read of which was settled reads `not checked: side a's read failed (NotSettled)` (§5).

**Schema guard texts** (`drift.lines`, the only builder; the Run screen, `ds runs`, `ds run`):
- **E1** `Paused by the schema guard at the start of cycle 7: side b: column 'email' of public.customers is gone, or the role may no longer read it. Put it back and resume the run, or cancel it and start a new run, which plans the table as it is now.`
- **E2** `…: side b: column 'amount' of public.customers was numeric(12,2) (decimal) when this run was planned and is text (text) now: its values would no longer render as planned. Cancel the run and start a new one, which plans the table as it is now, or change the column back and resume.` It also words `render_changed`, a type within its class that the engine renders otherwise: `…: side a: column 'AT' of APP.EVENTS was DATE (timestamp) when this run was planned and is TIMESTAMP(6) (timestamp) now: its values would no longer render as planned. …`
- **E3** `…: side a: table or view APP.CUSTOMERS is gone or no longer visible. Put it back and resume the run, or cancel it.`
- **E4** `…: side a: key column 'ID' of APP.CUSTOMERS was NUMBER(10,2) when this run was planned and is NUMBER(10,4) now: its key text would render differently. Cancel the run and start a new one, which plans the table as it is now, or change the column back and resume.`
- **E5** `Paused by the schema guard before any table row was read: <narrowing>. Resume to accept the tables as they are, or cancel.` The narrowing line is one of: `column 'loyalty_tier' was compared by run R-… of this version and is not compared now`; `column 'amount' was numeric(12,2) (decimal) in run R-… of this version and is text (text) now`; `key column 'ID' was NUMBER(10,2) in run R-… of this version and is NUMBER(10,4) now`.
- **E6** 422 `spec 12 (customer-profile 1.4.0) pauses on schema drift and a run of it would not compare what run R-… compared: <narrowing>. Upload a new version to compare the tables as they are.`
- **E7** `Paused by the schema guard at the start of cycle 7: side a: column 'EMAIL' was VARCHAR2(120 CHAR) and is VARCHAR2(254 CHAR) now (still text). This spec pauses on schema drift: resume to go on with the run's plan, or cancel.` At creation: `Paused by the schema guard before any table row was read: <the W line>. This spec pauses on schema drift: resume to accept the tables as they are, or cancel.`
- **W** (warnings) `side b gained column 'loyalty_tier' since this run was planned: this run does not compare it; a new run would` (at creation `since run R-…: it is compared from this run on`, or `: this version does not compare it`); `side b: column 'legacy_flag', which this run does not read, is gone`; `side a: column 'EMAIL' was VARCHAR2(120 CHAR) and is VARCHAR2(254 CHAR) now (still text)`; `side b: column 'opened' was NOT NULL and is nullable now`; `side b: the primary key of public.customers changed`; `side a: APP.CUSTOMERS was a table and is a view now`
- **U1** 409 `run R-… cannot resume: <the first breaking line>`; **U2** 409 `run R-… cannot resume: side a's table could not be checked now (ORA-12541): try again once the target answers`
- `the change of side a observed at cycle 7 was accepted by jdoe on 2026-10-07; the run goes on with its plan`
- `no change observed against the run's plan at its creation and each cycle's start (table sides only; a view's own tables, constraints and defaults are not read)`; once a look could not be made `no change observed against the run's plan where the schema guard looked (table sides only; a view's own tables, constraints and defaults are not read); it could not look at the start of cycle 7` (`(and 2 more)` after the first of several)
- `no earlier run of this version recorded its tables' shapes: this run records them` (no earlier run stored a plan the basis could be); `run R-… of this version recorded no shape of its tables at its creation: only its compared columns were checked against this run` (for every basis that is no baseline: a run from before 016, one the guard adopted, one whose look at its creation failed); `planned before the schema guard existed: its tables' shapes were first recorded at cycle 12`
- `the schema guard could not check side a at the start of cycle 7 (TargetUnreachable): the cycle went on unchecked`
- `report.pdf`: `Schema guard (observed): through cycle 7 it recorded 1 change that paused the run and 2 others on side b, and could not check 1 cycle start.` (`… and 1 other look` for a look not made at a resume or after a failed read)
- Log (`deltasleuth.guard`): `schema guard: run R-… paused at cycle 7 (side b: 1 breaking, 2 other)`; `schema guard: run R-… not checked: TargetUnreachable`.
- As built (`drift.lines`; each asserted):
  - A pause's line names the first change that stops the run (the breaking ones first, then by column);
    each other change that stands follows as a line of its own: a breaking one as its clause alone
    (`side b: column 'amount' of public.customers is gone, or the role may no longer read it`), any other
    as its W line. E6, one message, ends its first narrowing line `(and 1 more)`.
  - A look made mid-cycle (a resumed scan pass, a failed read, a resume) says `in cycle 7` for `at the
    start of cycle 7`.
  - W: `; a new run would` ends a gained column's line only where it holds: the side lists no `columns`
    and the other side holds the column too (or is no table). A type that changed class in a column the
    run does not read ends `(this run does not read it)` for `(still text)`. At a creation the other W
    lines read as they do during a run, the basis being named by the block's `basis`.
  - A look that reached no side (a fault of the guard's own code) says `could not check the run's
    tables`. A run planned before 016 says its `planned before the schema guard existed` line in place
    of `no change observed`, which claims a look at its creation. A run any of
    whose looks could not be made (`guard.unchecked`, kept across the looks after it) says the `where
    the schema guard looked` line in its place, naming the looks not made: it never claims each cycle's
    start, beside the `could not check` line or after a later look.
  - `report.pdf`'s sentence counts a change as one `that paused the run` when its row paused and the
    change pauses one (under `finding` the breaking ones, under `pause` every
    one, read from the row's `mode`) and as one of the `others` otherwise; a look not made at a creation, a
    scan pass or a cycle's start counts as a `cycle start`, at a resume or after a failed read as an
    `other look`; with nothing recorded it reads `through cycle 3 it could not check 1 cycle start.`, and
    without a row of the cycle or an earlier one there is no sentence.
  - Notifications: titles `<spec>: run R-… was paused by the schema guard`, `<spec>: a table of run R-…
    changed`, `<spec>: the schema guard could not check run R-…`; both sides read `side a's table changed
    (…) and side b's table changed (…)`; a warning at a creation reads `…: side b's table changed since
    run R-… of its version (2 changes); this run reads the tables as they are now.`; after a failed read
    (V5) that found nothing breaking, `…: side b's table changed since the run
    was planned (1 change); the run fails on the read that failed.` (the run is failed next: it never says
    the run goes on); under `pause` a pause for changes that break nothing counts them as `(2 changes)`.
- Notifications. `schema.drift`: `[prod] run R-… of customer-profile 1.4.0 (soak) was paused by the schema guard at cycle 7: side b's table changed (1 change that stops the run, 2 others). Observed by DeltaSleuth (installation I-…); not evidence and not a sign-off. Audit row 81234.` Warning: `…: side b's table changed since the run was planned (2 changes); the run goes on reading by its plan.` Unchecked: `…: the schema guard could not check cycle 7 (TargetUnreachable); the cycle went on unchecked.` `run.paused` (`_PAUSED`): `the schema guard found that a table it reads changed. A person reads the run's schema lines, then resumes or cancels it.`

## 11. Interfaces

**API** (`api/routes/prechecks.py`, in `app.py`'s tuple before `runs`; logic in
`deltasleuth/prechecks.py`, shaped like `notifications.py`; its `Refused(status, message)` and
`drift.Refused` are one `_HANDLERS` row each; the eight routes join `test_roles.ROUTES`):

| Route | Role | Answer; audit |
|---|---|---|
| `POST /v1/prechecks` | operator | 202 `{not, precheck_id, pairs, skipped, evaluation}`; `precheck.create` |
| `POST /v1/prechecks/catalogue` | spec_author | the same |
| `GET /v1/prechecks?limit=&cursor=` | viewer | `{not, banners, items, next_cursor}`: level, source, who, when, progress, counts by verdict, `evaluation` |
| `GET /v1/prechecks/{precheck_id}?verdict=&limit=&cursor=` | viewer | the item, `totals`, and pairs worst first: `{n, label, spec_id, status, verdict, differing, rows: [a, b], text}`, read from `rows_a`, `rows_b` and `line` |
| `GET /v1/prechecks/{precheck_id}/pairs/{n}` | viewer | the pair's `result` |
| `POST /v1/prechecks/{precheck_id}/cancel` | operator | 200 `{not, precheck_id, cancelled_at}`; `precheck.cancel` |
| `GET /v1/prechecks/{precheck_id}/reports/{name}` | viewer | `precheck.csv`, `precheck-columns.csv`, `precheck.pdf`; `report.download`, before the bytes |
| `GET /v1/prechecks/{precheck_id}/pairs/{n}/draft.yaml` | spec_author | the draft, its issues as header comments; unaudited, as `POST /v1/profile` |

`GET /v1/runs/{id}` gains `schema: {guarded, mode, basis, checked, lines}`, built before `shown`; `POST
/v1/runs` and `POST /v1/compare` answers gain `warnings`. As built (`drift.told`,
`drift.warnings`): the block also holds `paused` (the guard holds the run paused) and `accepts` (its
resume accepts changes: the run was created paused, or its spec pauses on schema drift and a change that
breaks nothing stands, whether or not a breaking one stands beside it, which must
be undone first, U1: the resume made then accepts the other in the person's name); `basis` is the basis run's id; `checked` is the last look
(`{cycle, at, where, error_type, side}`); a run without a table side answers `guarded: false` and no
line. `warnings` are the lines of the tables' changes since the basis, the pause's line first for a run
created paused; a one-shot compare's come from `compare_one`'s `attrs["warnings"]`. E6 answers 422 and
U1, U2 409 through `drift.Refused`.

As built (the six routes of spec inputs):
- Every answer but a download carries `not`. An item is `{precheck_id, source, level, pairs` (how
  many)`, skipped, evaluation, created_by, created_by_name, created_at, cancelled_by, cancelled_by_name,
  cancelled_at, finished_at, progress: {<status>: n}, verdicts: {<verdict>: n}, totals` (the text)`}`.
- `GET /v1/prechecks/{id}` answers the item with `banners`, `worded: {<verdict>: its words}`, its pairs
  as `items` (the item's `pairs` is their count) and `next_cursor`; a pair not settled comes last, its
  `verdict` and `text` null (a cancelled pair's `text` says so). `verdict=` takes a verdict's code,
  another value answering FastAPI's 422 `invalid request`; a cursor it did not issue answers 400
  `malformed cursor`; `limit` is 1 to 500.
- `GET …/pairs/{n}` answers `{not, precheck_id, source, level, evaluation, n, label, spec_id, status,
  verdict, said` (the verdict as words)`, differing, rows, text, targets: [a, b], relations: [a, b],
  finished_at, result, columns: [{column, classes, lines}]}`: `columns` holds each compared column's
  figures as `precheck.column_lines` words them, so that no screen builds one.
- `POST /v1/prechecks` judges K1, K5 and K7's counts before it reads the store (`prechecks.refuse`),
  then K2 to K4 and K7's `all_specs` limit, then K10 under the pre-checks lock. A name given twice is
  K2's `spec 12 is listed twice`, 12 being its newest version. `precheck.create` records `{source,
  level, pairs, skipped, spec_ids}`.
- An unknown report name answers 404 `report 'x.csv' not found`, as a run's.

As built (the eighth and ninth rows of `test_roles.ROUTES`; both in `test_nothing_locks_out`):
- `POST /v1/prechecks/catalogue` takes `CatalogueIn` (`a`, `b` as `{target, schema}`, `include`,
  `exclude`, `level`; a member not of these is FastAPI's 422 `invalid request`), judges K5 and K8 before
  any target (`prechecks.refuse_catalogue`), then K6, the listing (K9), the globs, K7, then K10 under the
  pre-checks lock, and answers 202 as `POST /v1/prechecks`. `precheck.create` records `{source:
  catalogue, level, pairs, skipped: 0, a: {target, schema}, b}`. Its pairs are listed as any pre-check's,
  `spec_id` null.
- `GET …/pairs/{n}/draft.yaml` answers the draft as `application/yaml`, its issues heading it as
  comments; K11 and K12 refuse; unaudited.

**CLI** (`precheck_app` in `cli/main.py`, as `notify_app`; AUTH §4.2 rows):

| Command | Audit | `DS_OPERATOR` |
|---|---|---|
| `ds precheck [--json]`; `ds precheck show PC-… [--pair N] [--verdict V] [--json]` | none | no |
| `ds precheck run [SPEC.yaml…] [--spec-id N]… [--spec NAME]… [--all-specs] [--a TARGET[:SCHEMA] --b TARGET[:SCHEMA] [--include GLOB]… [--exclude GLOB]…] [--level L] [--no-wait] [--fail-on refused\|differs\|incomplete] [--json]` (a file is stored first, as `ds run` stores it) | `spec.create`, `precheck.create` | yes |
| `ds precheck cancel PC-…` | `precheck.cancel` | yes |
| `ds precheck report PC-… --out DIR` | `report.download` per file | yes |
| `ds precheck draft PC-… (--pair N \| --all) --out DIR` | none | no |

As built: `run` refuses K1,
K5, K7 and K13 before it stores a file, and stores a file as `ds run` does but for the approval gate
(§6: a pre-check is not gated; under SSO a file whose content was never uploaded and needs an approval
is still refused, as an upload of it by the CLI would be). `--no-wait` prints the pending pairs and
`read by 'ds worker' or 'ds serve'`; `run --json` and `show --json` print the API's body with every
pair. `show --pair N` prints the pair's lines, then each compared column's as `  <column>: <line>`.

Output: `NOT`, the evaluation label, the totals, then each pair's line worst first. Exit 0; 1 under
`--fail-on` when a pair ranks at or above it (`refused`: ranks 1 to 2; `differs`: 1 to 7, so a pair that
was not read fails it too: a control must not pass on no data; `incomplete`: 1 to 8) or was cancelled; 2
with §10's message; 3 for a bug. `ds run` prints each creation warning as `warning: …`, and a run
created paused as `paused (run_paused_schema_drift)` with its schema guard lines, exit 2. As built: so does `--no-wait` for a run created paused (no worker would take it), and a run the guard
pauses while `ds run` waits; `ds runs` prints the pause's line under such a run; `ds compare` prints its
warnings on stderr.

As built: `run --a TARGET[:SCHEMA] --b TARGET[:SCHEMA]` (a blank schema is the target's
default) calls `prechecks.catalogue`, its level `counts` unless asked (`columns` otherwise), and is read
and printed as any pre-check; `--a` without `--b`, either with a spec, and `--include` or `--exclude`
without them are refused (§10's K13 additions), as are K5 and K8 before any target. `draft PC-…
--pair N` writes `PC-…-pair-N.draft.yaml` (a file name of no catalogue name) and prints its path, then
each issue as `ds profile` prints one; `--all` writes every pair that can be drafted and skips the rest
(a stored spec, a name without a pair, a pair not read), refusing only when none can be; K11 and K12 exit
2.

**UI** (`web/src/Prechecks.tsx`, pure `prechecking.ts`, types in `api.ts`: `prechecks.ts` would be
`Prechecks.tsx` on a file system that ignores case, as READINESS's `ready.ts` and SCHEDULES'
`scheduling.ts` avoid; the list item's type is `TablePrecheck`, `Precheck` being the sign-off
precheck's): a nav link `Pre-checks` after
Schedules for every role, never on the Readiness screen. `#/prechecks` shows `NOT`, the list and, by
role, a form (specs, or two targets with schemas and globs; the level). `#/prechecks/PC-…` shows
progress, the totals, then pairs worst first with a verdict filter and the downloads; a pair opens its
refusals, schema table, column table with each `not compared` reason, both read lines, `Run a
comparison` (operators) and `Draft a spec` (catalogue pairs, spec authors). Verdicts are words, never a
tick or green; no screen builds a text. `Specs.tsx` gains `Pre-check…`. `Run.tsx` prints the schema
guard's lines, a banner while paused by it, and `Resume (accept the changes)` where a resume accepts.
As built: the form takes the stored versions chosen, or every stored spec of two tables
(`spec_names` is the API's and the CLI's); `Pre-check…` opens it with that version chosen
(`#/prechecks?spec_id=N`); a pair opens at `#/prechecks/PC-…/pairs/N`; `Run a comparison` starts a batch
run of the pair's spec.
As built: `Run.tsx` prints `schema.lines` as the API words them, the first as a banner while
`schema.paused`, and names its resume by `schema.accepts`; `prechecking.ts` holds the three pure helpers
(`guardBanner`, `guardLines`, `controlLabel`) and `api.ts` the type `RunSchema`.
As built: spec authors get a second form, two database targets (`GET /v1/targets`, those with an
engine) each with a schema (blank: its default), `include` and `exclude` (separated by spaces or commas)
and the level (`schema`, `counts`, `columns`); `Draft a spec` stands on a pair of two relations paired by
name once both catalogues were read (its result holds `names`) and saves the draft as the CLI names it.
`prechecking.ts` gains `Schemas`, `blankSchemas`, `CATALOGUE_LEVELS`, `catalogueOf`, `draftable` and
`draftFile`; `api.ts` `CatalogueIn`, `createCatalogue`, `precheckDraft` and the result's `names`.

**Reports** (`reports/precheck.py`, over `render.Document` and `render_csv`): `precheck.csv` (a row a
pair, with `claim` = `observed; not parity`); `precheck-columns.csv` (a row a pair, column and figure:
`a`, `b`, `verdict`, `reason`; withheld cells empty); `precheck.pdf` (`NOT`, who and when, totals, pairs
worst first, a section for each of the first 100 pairs with a difference, "What this report is not"). As
built: each CSV's first record is `NOT` in one cell and, in an unlicensed evaluation, its
second `Unlicensed evaluation`; then come the header and the rows. `precheck.csv` holds `precheck_id, n,
label, spec_id, target_a, relation_a, target_b, relation_b, status, verdict, differing, rows_a, rows_b,
text, claim`; `precheck-columns.csv` holds `precheck_id, n, label, column, class_a, class_b, figure, a,
b, verdict, reason`, a length as two rows (`shortest`, `longest`) and a column's own reason as a row
without a figure. The PDF's title is `Table pre-check (observed)`, its first line `NOT`; its last
section holds `reports.precheck.NOT`, seven sentences in §2's and §16's words. A
run's `report.pdf`, which no pack carries, gains §10's one sentence after `reports/data.tables_line`
when the run has `schema.drift` audit rows of that cycle or an earlier one: it adds up those rows, so a
cycle's report reads the same whenever it is made; with none it says nothing. No CSV of a run changes.
No run report carries a pre-check.

## 12. Performance

`DS_PERF=1`, `tests/e2e/test_precheck.py`; this document publishes measured times before release, not
these thresholds (below). Indicative, observed before the build: the `columns` statement on
10M PostgreSQL rows of 6 columns took 1.0 to 1.4 s; at 1M rows 0.1 to 0.6 s on all three engines.

- `test_columns_a_million_rows`: 1M rows a side, 6 columns, level `columns`: under 10 s for
  PostgreSQL-PostgreSQL, 20 s with a MySQL or Oracle side.
- `test_columns_ten_million_rows` (PostgreSQL): under 30 s; `test_distinct_ten_million_rows`: under
  120 s, the server's peak temporary bytes published.
- `test_three_hundred_pairs_schema`: 300 PostgreSQL pairs at `schema` under 60 s, never more than one
  session a target (`pg_stat_activity` sampled).
- `test_guard_costs_a_catalogue_read`: V2 and V3 under 1 s; the existing `DS_PERF` table tests must not move.
- `GET /v1/prechecks/{id}` for 500 pairs under 300 ms: it selects no `result`.

As built: the schema guard's test is in `tests/e2e/test_drift.py`, beside the soak it times, and
the last bound is `tests/e2e/test_precheck.py::test_get_five_hundred_pairs`.

**Measured** (`DS_PERF=1`, each test run once, on 2026-10-03; single runs, not medians). One Mac (Apple M3
Max, 16 CPUs) that was not idle (load average 2 to 3); Docker Desktop (16 CPUs, 17 GB); every database a
stock container of a local image on that host (`postgres:16-alpine` 16.14, `mysql:8.4` 8.4.11,
`gvenzl/oracle-free:23-slim-faststart` 23.26.3, which Oracle caps at two CPU threads), the store on the
PostgreSQL container, both sides of a PostgreSQL pair on that one server; the settings' defaults (one session
a target, 900 s a statement, 20,000,000 rows); worker concurrency 4, poll 0.05 s. A pre-check's time runs
from its creation until the worker, started with it, ended it. The rows are synthetic: a `bigint` key (an
Oracle `NUMBER(19)`) and six compared columns, two texts, a `char(4)`, a `numeric(12,2)`, an integer and a
timestamp, identical on both sides; every pair below read `no difference observed`.

| Test | Measured | Bound |
|---|---|---|
| `test_columns_a_million_rows`, 1M rows a side, level `columns` | PostgreSQL-PostgreSQL 0.2 s; PostgreSQL-MySQL 0.8 s; PostgreSQL-Oracle 1.5 s; MySQL-Oracle 0.9 s | 10 s; 20 s |
| `test_columns_ten_million_rows`, 10M rows a side, PostgreSQL, level `columns` | 1.7 s | 30 s |
| `test_distinct_ten_million_rows`, the same tables, level `distinct` (the duplicate keys, then six distinct counts in two statements, a side) | 18.6 s; the server's temporary files at most 1,940 MiB at once for both sides' sorts (the ordinary files `pg_ls_tmpdir()` lists, sampled every 0.1 s: a shorter peak, and a parallel query's shared file sets, would be missed), 3,660 MiB written in all (`pg_stat_database.temp_bytes`) | 120 s |
| `test_three_hundred_pairs_schema`, 300 stored specs of two 3-column tables on two PostgreSQL targets, level `schema` | 16.9 s; at most one session on each target in 659 samples of `pg_stat_activity`, 20 ms apart | 60 s; one |
| `test_get_five_hundred_pairs`: `GET /v1/prechecks/{id}?limit=500` of two schemas' 500 pairs of 20-column tables read at level `columns`, asked 20 times through the in-process client | median 7 ms, max 9 ms (the worker read the 500 pairs in 28.2 s). A first run, before the test asserted the verdicts, gave 8 ms and 11 ms | 300 ms |
| `test_guard_costs_a_catalogue_read`, two PostgreSQL tables of 200 columns: V2 is both sides' catalogue statements on their passes' sessions and `drift.check`; V3 is `drift.cycle_start` (a short session a side, its catalogue statement, the judgement recorded) | V2 81, 28 and 29 ms at a table soak's three passes; V3 67 and 71 ms at a key-list soak's two later cycle starts | 1 s |
| TABLES' `DS_PERF` table tests, after this feature | `test_a_million_rows_a_side_finish_in_a_minute` 7.1 s (TABLES §7: 6.8 s); `test_many_differing_rows_are_read_in_batches` 42.2 s (TABLES §12: 43 s, 42.1 s re-measured) | 60 s each |

The table tests are single runs on a busy host, so whether 7.1 s against 6.8 s is a move or noise is not
established; V2 adds one catalogue statement a side to each scan pass (28 to 81 ms above), and the 1M-row
test's scan is one pass.

**Not measured:** MySQL or Oracle at 10M rows; level `distinct` on MySQL or Oracle at any size (the
temporary space their sorts take included); two schemas paired by name across engines at scale; the schema
guard on MySQL or Oracle; a database on another host than the worker's; a cold cache; a repetition of any
figure above. The indicative figures above are earlier observations, not these tests'.

## 13. Tests (local images only, `--pull never`; ENGINES §16's fixtures; nothing pulled)

| File | Tests |
|---|---|
| `unit/core/test_precheck.py` | `schema_diff` per code and group; `test_missing_column_outranks_a_differing_figure`; `test_key_not_checked_over_a_view_keeps_the_best_verdict`; figures per class; `test_sum_exact_bounds` (Oracle's lost 1E-20 is `not compared`; `sum_digits` 65 and None; no scale; a negative scale counts 0); `test_nan_and_infinity_sums_not_compared` (two equal sides do not differ); `test_oracle_empty_stays_a_difference`, and folded under `emptyAsNull`; `test_typemap_gives_nulls_only`; `test_sum_shown_only_under_four_conditions` (an unlisted column under `unlisted: tokenize` and a side with `where` are withheld); `test_verdict_ladder`; `test_module_names_no_engine`; `implied` with and without a common primary key; `shape` ignores column order; `changes` per kind, with `test_widened_key_is_not_breaking`, `test_respelled_integer_is_not_breaking`, `test_key_scale_and_oracle_precision_are_breaking`; `verdict` per mode; Hypothesis: `changes(s, s)` is empty, an unplanned column is never breaking, `compare` is symmetric; `test_messages_golden` (every text of §10: each kind, code and limit) |
| `unit/connectors/test_tables_figures.py`, `test_tables_plan.py` | golden statement text per engine and level; figures count rows without a key, duplicates do not; chunks of 150 and 4; `test_statements_hold_only_grammar` (hostile names and filters); `issues` lists every refusal of a step and `plan` raises the first; `test_postgres_pair_plan_unchanged`; a failed read's code, and what was read before it stays |
| `unit/connectors/test_postgres.py`, `test_mysql.py`, `test_oracle.py` (containers) | each form of §4: binary distinct against a case-insensitive column; PAD SPACE empties; `char` lengths; `LENGTH4`; CLOB null counts; sum rounding; `NaN` in a PostgreSQL `numeric`; duplicate keys; `relations` and `key` with a read-only account; a timeout is `timeout` under 3024 and 1317; **a column dropped under per-key reads pauses the run at V5 on each engine** (`UndefinedColumn`, MySQL 1054, ORA-00904) |
| `unit/runner/test_precheck_run.py` (a fake `Engine`) | one session a target at once; `test_same_target_pair_runs_under_one_session` (side a, then b, `apart_s` set, cap 1); each side in one snapshot; a timeout keeps the rest; unreachable twice settles its target's pairs; `test_refused_login_settles_every_open_precheck_of_the_target` (never retried; at most `sessions_per_target` logins); over `distinct_max_rows` skipped; cancel sends the engine's cancel; a killed worker's items finish once; `test_fault_never_touches_a_run_or_the_sweep`; `test_writes_only_its_tables_and_audit`; `test_planted_value_reaches_no_row_export_log_or_delivery` |
| `unit/runner/test_drift.py` | V1 to V5; an added column warns once across three cycles; a dropped compared column pauses before the cursor opens, resume refused, restored, resumes; a class change pauses; `pause` mode pauses on any other change and a resume accepts; narrowing created paused and accepted under `finding`, **refused at creation under `pause`** (422, no run row, no pair seated); `test_cancelled_paused_run_is_no_basis`; a first run has no basis; `test_pre_016_basis_judges_compared_lost`; a run from before 016 is adopted; a misclassified error at V5 still fails; a schema guard fault fails open with its row; **`test_unreachable_at_cycle_start_does_not_stall_leases`** (two tries, the unchecked row, the census counted, keys leased, `maxConsecutiveErrors` stops the run); the `maxKeys` reason returns; audit rows last and in order; `test_one_event_per_state`; `test_plan_sha256_and_pack_unchanged` |
| `unit/test_evidence_boundaries.py`, `unit/store/test_migration_016.py`, `unit/runner/test_purge.py` | `test_precheck_is_never_evidence` (also: `coverage.csv` under `write_csv(keyed=False)` is byte for byte the same with and without a guard record, and two packs of one cycle issued before and after a later change are equal); 016 applies over 001 to 015 with runs; each CHECK; `ds.runs` not rewritten; the cascade; pruning; a pass that deletes only pre-checks audits `retention.finished` |
| `unit/api/test_prechecks_api.py`, `test_roles.py`, `test_licence_api.py`, `test_runs.py` | roles in each mode; K1 to K12; sums withheld under SSO without the approval; **`test_unapproved_filter_reads_schema_only`** (oidc: no figure stored, shown or exported, no probe sent, the pair `incomplete`; approved, it reads its level; tokens mode reads it); `test_never_told` (N1 to N10, whole words); the evaluation label on the item, the list, the screen text and the exports; `test_nothing_locks_out`; the list selects no `result`; run lists and pages show no `guard` |
| `unit/core/test_notify_events.py`, `test_notify_rules.py` | the projection; the model against planted names and type texts; dedup; severity; golden wording; **`test_guard_pause_reaches_a_run_paused_rule`** (ALERTS' example rules, unchanged, deliver it); a channel routed both events sends one; the daily names the reason |
| `unit/reports/test_precheck_reports.py`, `test_pdf.py`, `test_pack_contents.py` | CSV escaping of a column named `=cmd\|…`; withheld cells; the PDF; a cycle's `report.pdf` sentence is the same before and after a later cycle's change; `test_drift_rows_pass_the_pack_gate` (an integer key pattern); no `PC-` row in a segment |
| `unit/spec/test_schema_copies.py`, `test_validate_negative.py`, `unit/profiler/test_table_draft.py`, `unit/cli/test_cli.py`, `web/test/prechecks.test.ts` | `schemaDrift` without `inferredSchema`; `pause` accepted with a table side; drafts with and without a key; **a column named with U+2028, U+0085 and a newline followed by `security: {unlisted: clear}` leaves the draft's parsed content unchanged**; commands and exits; `--fail-on differs` exits 1 when a side was unreachable or refused its credential and when the pre-check was cancelled, `--fail-on incomplete` on a limit skip; K13; no screen builds a text, no tick |
| `e2e/test_precheck.py` | TABLES §7's dataset with a duplicate key, NULL keys, a filter, `''` against NULL, `CHAR` padding, a float and a `typeMap` column, as PostgreSQL-PostgreSQL, PostgreSQL-MySQL, PostgreSQL-Oracle, MySQL-Oracle, MySQL-MySQL and Oracle-Oracle: figures and verdicts exact; a pair of two schemas of one database; **`test_agreeing_figures_are_not_parity`** (two rows with swapped balances read `no difference observed` while the batch run reports 2 MISMATCH); two schemas with an unpaired and a case-ambiguous table, then a draft that validates; `statement_timeout` and `temp_file_limit` on a role |
| `e2e/test_drift.py` | a table soak with `ALTER TABLE` between cycles on each engine: a column added (warned), a compared column dropped (paused, not failed), a retyped column (paused, resume refused, a new run plans), a key widened (warned, the soak goes on) |

As built: `unit/runner/test_drift.py` also holds the schema guard's `test_messages_golden`
(its texts are `drift.lines`'s, the pure module's golden test holding a pair's), the report's sentence,
N9 (`test_never_told_that_a_schema_is_unchanged`) and a schedule's firing that the guard creates paused
or refuses. `test_plan_sha256_and_pack_unchanged` is two tests: the plan's hash in the first run's test
there, the pack's bytes in `unit/reports/test_pack_contents.py::test_drift_rows_pass_the_pack_gate`,
which also holds what the table lists under `test_precheck_is_never_evidence` for a guard record
(`coverage.csv` byte for byte, and two packs of one cycle, which differ only in the manifest, the
segment, the certificate, `VERIFY.txt` and the checksums that cite them): the packs' fixtures live
there. `test_evidence_boundaries.py` adds `runner/drift.py` and `repo.held_guard` to what no evidence
module imports or reads.

As built:
- The container tests' forms of §4 are in `unit/connectors/test_mysql.py` and `test_oracle.py` (each
  figure of each class with known values, binary distinct against the default case-insensitive collation
  and a PAD SPACE trailing space, blank `CHAR`s, `LENGTH4`, a `CLOB`'s NULLs, Oracle's rounded sum,
  duplicate and null keys with a filter, `relations` and `key` with a read-only account, a timeout
  under 3024 and a 1317 that a `KILL QUERY` sent once the timeout had passed; DPY-4024 on Oracle), the
  catalogue's `nullable`, `size` and `key` in their describe tests. The V5 tests of MySQL (1054) and
  Oracle (ORA-00904) are in `unit/runner/test_drift.py`, beside PostgreSQL's, as one test over both.
- `unit/connectors/test_tables_figures.py` pins MySQL's and Oracle's statements in
  `tests/golden/engines/mysql_figures.json` and `oracle_figures.json` (the plain and mapped sides of
  their `_pair.json`), holds that a MySQL statement holds only grammar and an Oracle one no value
  (Hypothesis), the canonical bounds of a moment and the 1317 rule. (The PostgreSQL grammar test's
  hostile name `.` was its relation's own text, `"s"."t"`: excluded as `s` and `t` were, and `C`
  too, the name of the collation a text is read in, `COLLATE "C"`.)
- `implied`, `alone` and the refused line of two tables are in `unit/core/test_precheck.py`; the draft
  in `unit/profiler/test_table_draft.py`; the catalogue route, its refusals (K5 to K10), the draft
  route (K11, K12), its role and N8 (the unpaired lines say `paired by name`, no N1 word) in
  `unit/api/test_prechecks_api.py`; the catalogue read by the worker in `unit/runner/test_precheck_run.py`
  (implied plan, no probe, one target's sessions, a table gone since the listing); `--a`, `--b` and
  `draft` in `unit/cli/test_cli.py`; the form, the draft's name and the calls in `web/test/prechecks.test.ts`.
- `e2e/test_precheck.py`: the dataset (a repeated key, a row without a key, a filter, `''` against NULL,
  `CHAR` padding, a float and a `typeMap` column) as PostgreSQL-PostgreSQL, PostgreSQL-MySQL,
  MySQL-MySQL, PostgreSQL-Oracle, MySQL-Oracle and Oracle-Oracle, every figure and verdict exact; two
  schemas of one database read as one target; two schemas paired by name with a name of one side alone
  and two alike, then a draft that validates against the targets, is stored and runs to `completed`; a
  role's own `statement_timeout`, which the pre-check's session replaces, and its `temp_file_limit`,
  which ends the duplicate keys' sort (53400). `test_agreeing_figures_are_not_parity` is in
  `unit/runner/test_precheck_run.py`.
- `e2e/test_drift.py` runs the soak on MySQL and Oracle too; there the dropped column comes back as an
  integer rather than a text, since between MySQL or Oracle sides a number and a text are two families,
  which the new run's plan refuses (P9) where a PostgreSQL pair plans them.

As built: §12's `DS_PERF` tests are `e2e/test_precheck.py`'s `test_columns_a_million_rows` (four
pairings), `test_columns_ten_million_rows` and `test_distinct_ten_million_rows` (one module fixture seeds the
two 10M-row databases at once), `test_three_hundred_pairs_schema` (a thread samples `pg_stat_activity`) and
`test_get_five_hundred_pairs`, and `e2e/test_drift.py`'s `test_guard_costs_a_catalogue_read`, which wraps the
PostgreSQL adapter's `describe`, `drift.check` and `drift.cycle_start` to time them inside real soaks. Each
also asserts what it read (row counts, verdicts, sessions or looks), so a fast wrong answer fails it.

Also (the schema guard): `unit/core/test_precheck.py`'s
`test_a_type_its_engine_reads_otherwise_is_breaking` (Oracle `DATE` and `TIMESTAMP` either way, MySQL `bit`,
compared, strata and key columns; respellings each engine reads alike, a filter column and a creation's basis
warn), Oracle's `NUMBER(*,2)` key in `test_key_scale_and_oracle_precision_are_breaking`;
`unit/runner/test_drift.py`'s `test_a_type_its_engine_reads_otherwise_pauses_the_run_on_each_engine`
(containers: Oracle `DATE` widened to `TIMESTAMP(6)` with fractions on both sides and `TIMESTAMP(6)` narrowed
to `DATE`, MySQL `TINYINT` to `BIT(8)`, each paused at V2; Oracle's `NUMBER(38,2)` key respelled
`NUMBER(*,2)`, a warning and a completed run), `test_an_adopted_run_s_plan_is_the_basis_while_no_run_is_a_baseline`
(and a first run whose look at its creation failed), `test_a_cancelled_paused_run_after_runs_from_before_016_hides_nothing`,
`test_a_resume_that_accepts_is_named_so_while_a_breaking_change_stands`,
`test_a_cycle_start_not_checked_is_never_claimed` and the report's sentence under `finding` and for other
looks; `unit/runner/test_sample_draw.py`'s `test_a_frame_that_meets_a_dropped_column_has_the_schema_guard_look_first`
(PostgreSQL, MySQL, Oracle); `unit/runner/test_notify_feed.py`'s
`test_a_guard_pause_resume_pause_of_one_state_gives_two_deliveries` (the real guard and notifier); and in
`unit/core/test_notify_events.py` the pause's key by its seq and the V5 warning's text.

Also (figures, privacy and operations): `unit/core/test_precheck.py`'s
`test_key_parts_are_judged_as_columns_and_one_whose_texts_never_meet_is_not_compared` (PostgreSQL's
`numeric(10,2)` key against an `integer`, two scales, a date against a timestamp, class, size and NULLs, a key
map; across engines, an Oracle `NUMBER(10,2)` key paired by name with a PostgreSQL `integer`, and a
PostgreSQL `integer` with a MySQL `varchar(10)`), `test_columns_named_alike_ignoring_case_on_a_side_are_reported_never_dropped`,
`test_under_a_filter_a_protected_column_s_counts_are_verdicts` and the skipped lines in `test_messages_golden`;
`unit/runner/test_precheck_run.py`'s `test_keys_whose_texts_never_meet_are_not_read_as_no_difference` (a
stored spec on PostgreSQL; the batch run of it reads 2 `NOT_FOUND_A`) and
`test_a_worker_that_stops_mid_pair_sets_it_back_after_renewing_its_claim`; `unit/api/test_prechecks_api.py`'s
`test_a_pre_check_no_worker_takes_says_so` (a claim renewed 5 minutes ago keeps the banner away, one 7 minutes
old does not); `unit/connectors/test_mysql.py`'s `test_a_bit_column_s_sum_is_its_exact_decimal` (a
`BIT(64)` sum of 3 x 2^63 against 2^64 - 1, `differs`); `unit/connectors/test_postgres.py`'s
`test_a_partitioned_table_is_listed_once_and_reads_every_partition`; `unit/profiler/test_table_draft.py`'s
`test_a_key_whose_texts_never_meet_is_said_at_the_key_not_among_the_columns`; and `e2e/test_precheck.py`'s
catalogue test, whose schemas gained a table with `"Email"` and `email`. `mysql_figures.json` pins a `bit`
column's sum without the `UNSIGNED` cast. `unit/runner/test_schedule_fire.py::test_rate_sample_free` runs a
worker until its second run ends (`harness.finish`).

Also (messages): `unit/api/test_prechecks_api.py`
asserts K12's `pair 1 was not read: there is nothing to draft yet` for a pending pair of two schemas (and
that the cancel answer carries `not`); `unit/runner/test_precheck_run.py` the same refusal for a pair read
as `refused`, and `test_a_pair_by_name_whose_target_is_no_longer_declared_is_refused_unread` (`a run of
these tables would be refused: target db-b is not declared as a database`, no session opened);
`unit/cli/test_cli.py` K13's `no pair of PC-… can be drafted: none is two tables paired by name and read`
for `ds precheck draft --all` of a pre-check of stored specs. `unit/core/test_precheck.py` also asserts the
two-family key line (`the key text of a date is never that of a timestamp`).

## 14. Seams

- **Files** (built: [FILES.md](FILES.md) §12): one more `ENGINES` entry, `files`. It implements
  `describe` alone: `relations`, `figures` and `duplicates` refuse, so **a table pre-check that names a
  file side is refused** (§10), and pre-check figures over pinned bytes are a seam there.
  `core/precheck.py` still names no engine.
- **`schemaDrift.expect`:** shape hashes pinned in the approved spec, so "since it was approved" is
  exact; a pre-check already prints each side's `sha256`.
- **Later:** a schedule kind for pre-checks (until then a site's scheduler calls `ds precheck run
  --all-specs --fail-on incomplete`); a rename map for catalogue pairs; a readiness gate over `guard`;
  a second person for an accept; the JSON guard for service sides, which fills `SCHEMA_DRIFT`;
  bucketed counts to localise a difference.

## 15. Documents this contract amends

**ENGINES** §5, §7 (§5 here), §11 (the denied class gains MySQL 1054; its
"fails the run" becomes "the schema guard looks first, V5: a breaking change pauses, else the run
fails"), §12 (F4, the same), §13, §16 (the 1054 code to its class). **TABLES** §3 (the shape beside the
plan), §5 (V2, V5), §6, §8. **CONTRACTS** §1, §2 (`precheck`), §3 (016), §4, §5, §7, §8, §10, §11.
**AUTH** §4.1 (eight routes), §4.2, §6. **APPROVALS** §4 (a pre-check is not gated; an unapproved
filter reads at level `schema`; sums need `clear` approved), §10. **LICENCE** §1, §2. **RETENTION**
§4.2, §4.3 (`run_paused_schema_drift`), §5 (`prechecks` in `retention.finished`), §8. **ALERTS** §2.1
(`schema.drift`; `run.paused` gains the reason), §2.3, §2.4, §3 (`reasons`), §6.2, §8 (the example's
`run-health` rule now also carries schema-guard pauses), §14. **READINESS** §12, §15 (the invariant).
**PACK** §1.3 (the census of a key-list cycle now waits for the cycle to fall due and for V3, two tries
at most), §2.1 (unchanged, said so: no guard figure in any pack file), §2.3, §4.3. **SAMPLING** §5 (a
draw waits for V3). **SCHEDULES** §5 (audit order; a run created paused is live; E6 is `not_started`),
§13. **DEPLOY** §3, §7 (no grant beyond a run's; what each level costs; temporary space; one more
session a side, under `sessions_per_target`; a replica for large tables; figures under a filter can be
narrowed to one row, §6 and §16), §10 (`Migration 016 adds one nullable column to ds.runs: no rewrite, no
scan`; `After this upgrade the schema guard can pause running soaks and scheduled runs: a rule that
routes run.paused is told`), §11. **RUNBOOK:** new "Triage with table pre-checks" (levels, reading the
list, what `same` does not mean, catalogue pairs and drafts) and "When a table changes under a run"
(each E text, accepting, a schedule held in `overlap`); and `Before a downgrade, resume or cancel every
run paused with run_paused_schema_drift: the older release knows neither the reason nor its
notification`. **PENTEST** §2. **BENCHMARK** §2. **PLAN.** **README.** `docs/architecture.md` §4.3,
§4.4, §4.11, §6, §7.2 (the schema guard as built for tables; `SCHEMA_DRIFT` stays its per-key code),
`control-plane.sql`. Both `spec.schema.json` copies. The docstrings of
`runner/worker.py` (the census waits for V3; V5 before `fail_run`), `runs.py`, `scan.py`, `purge.py`.

## 16. Limits

**Honest limits.**
- Agreeing figures are not parity: offsetting differences cancel in a sum and swapped values change
  nothing counted. Differing figures are not a defect: bridges and rules are not applied, and the sides
  are read seconds apart.
- An unconstrained Oracle `NUMBER` never has its sum compared; `typeMap`, float and LOB columns get null
  counts only.
- **Figures under a filter are an oracle.** In tokens and developer mode, and for an approved version
  under SSO, whoever pre-checks a spec with a filter learns of the rows it selects how many there are (yes
  or no about any column the filter names), how many hold NULL in each compared column, protected and
  tokenised ones included, and whether the two sides agree in every other figure: filters of one row a
  side tell whether each row's value is NULL, of a number, a moment or a boolean whether the two values
  are equal, and of a text or bytes whether they are as long and whether both or neither is empty (§6).
  A protected column's sum, `true`, `empty` and `distinct` counts and lengths are only verdicts there. A
  run of the same spec reads and counts the same rows but compares key with key and shows none of this of
  a protected column: the pre-check tells more. DEPLOY §7 and APPROVALS' threat row say so. Column and relation
  names are stored and exported. An agent's token reads the verdict counts over MCP's `list_prechecks`
  where the settings serve `aggregates`, so of a pre-check of one pair it reads that pair's verdict
  ([MCP.md](MCP.md) N9); it reads no line, figure or name.
- **So is a `regex` key map, in every mode:** it needs no approval of its own, under
  SSO too, and its pattern reaches the target as a predicate over raw key values; the rows whose key it
  does not match are counted, by a pre-check as `null_keys` and by a run as `skipped_a` and `skipped_b`,
  so successive versions can narrow down a protected key's raw values (§6). D15 does not cover it; a run
  of the same version tells the same count.
- Listing two schemas holds the request while both catalogues answer. Pre-checks wait while no worker
  runs. A cancelled sort may run on until its timeout. A PostgreSQL schema lists no partition: a partition
  is read through its partitioned table. A pair a killed worker was reading counts as being read until its
  claim is three lease periods old (6 minutes by default): the banner waits for that too.
- The schema guard looks at creation, each scan pass, a key-list cycle's start, a resume and a failed
  read. Without packs or readiness configured, a key-list run may read some keys before V3's pause
  lands. A cycle whose start could not be checked goes on unchecked, with a warning. A change reverted
  between checks, a redefined view and a change of meaning are unseen.
- The reference at creation is the version's previous baseline run (while none is one, the newest
  stored plan), not the moment of approval; a first run on an already narrowed schema becomes the
  baseline.
- A server upgrade that respells a type reads as `type_changed` (a warning, or a pause under `pause`)
  where the engine reads both spellings alike, and pauses the run where it does not (`render_changed`,
  `key_render_changed`);
  on MySQL a revoked column grant reads as a dropped column; one-shot compares only warn.
- A run created paused (`finding`) seats its pair, has read its key source, and holds its schedule in
  `overlap` until a person resumes or cancels it or `purge.abandon` does.
- With `dedup_hours: 0` a channel routed both `run.paused` and `schema.drift` gets both for one pause.
- A change that a run met and accepted during its cycles is told again when the version's next run is
  created (a warning, or under `pause` a run created paused): the basis is judged by what it planned.
- Under `pause`, a failed read (V5) that finds only changes that break nothing records them and the run
  fails, as it would have without the guard.
- A look that could not be made is deduplicated a channel as one state: repeated outages at cycle
  starts within `dedup_hours` send one notification.
- The constants (500, 20, 200, 40, 150, 4, 900 s, 20M) were chosen without measurement.
- An empty `CLOB` or `BLOB` is counted as a value by `nulls`, though a comparison
  reads it as NULL (§6 of ENGINES); a column whose declared collation is not binary is counted `COLLATE
  BINARY`, not observed (the local image takes no such column); Oracle's `relations` reads `READ` and
  `SELECT` grants of the account, `PUBLIC` and its enabled roles, so a table read through some other
  path is not listed; a column the account may not read on one side of two relations paired by name
  reads as on the other side only; a draft of a pair with no comparable column lists `columns: []`, which
  its issues refuse.
