# Files as table sides (CSV and Parquet): contract

This contract is binding. It builds the files engine on [ENGINES.md](ENGINES.md)'s seam, and §19 lists the
documents it amends; §12 says what it changes in [SCHEDULES.md](SCHEDULES.md) and in the pre-checks with
the schema guard ([PRECHECKS.md](PRECHECKS.md)).

**Goal:** a table side may be CSV or Parquet files in a directory the host administrator mounts read-only
and declares, in any pairing with PostgreSQL, MySQL, Oracle, a service side or another file side.
**Shape:** one more `ENGINES` entry, `files`, read through one in-process, locked-down DuckDB on threads
of its own. The files are pinned by size, times, inode and SHA-256 when the run is created; a change
fails the run. One dependency (`duckdb`), one target form, one spec member (`table.file`), two `typeSpec`
members, one settings block. No migration, route, audit action, licence fence or outbound destination.

**Observed:** each DuckDB behaviour a rule leans on is in §3's table with a named test, on the locked
version (`duckdb==1.5.5`); one that fails stops the release until this contract is revised. §14 point 7
says what a writer of crafted Parquet bytes still can do.

**The reader is a process of its own** ([READER.md](READER.md)): no `ds` process loads DuckDB. Each starts
a file reader, a child process that alone loads it and hands rows back in typed frames, so that a fault or
a memory blow-up of DuckDB's ends that child and no other process. READER.md is the contract of that
process, of its protocol (its §6) and of the accounting of its ends (its §9); this document stays the
contract of what is read and how, amended in place where a passage says "by READER.md", and §3's U32 to
U38 are the behaviours that process rests on. The reader is no sandbox: READER §2 says what is never
claimed of it.

## 1. Decisions (each one seam)

- **D1 One engine, `files`:** `connectors/tables/manifest.py` (guard, expansion, hashes, the thread pool;
  no DuckDB) and `connectors/tables/duck.py` (instance, sessions, statements). `connectors/files.py`
  (captured samples) is unrelated and untouched.
- **D2 Object storage is out, a named seam.** S3 needs `httpfs`, which the wheel does not carry (a
  run-time download, or an unapproved binary); it resolves and dials by itself, so `netguard.vetted`
  could pin no address (ENGINES D12); its credentials travel as `CREATE SECRET` text; and it needs
  external access on, which removes the file guard. A site syncs the bucket into the declared
  directory with its own tool.
- **D3 The directory is the target's, the path the approved spec's:** a relative path of a closed
  grammar under the declared root. No request or run option carries a path, and DuckDB is never handed
  a glob or a URL.
- **D4 One run, one export.** Any difference from the pinned files fails the run, between a soak's
  cycles too. Recurring exports use a schedule (a firing is `create_run`), not a soak.
- **D5 CSV is declared, never sniffed:** names, dialect, line ends and key types are in the four-eyes
  approved spec; every column is text, converted only by `typeMap`. Sniffing only drafts, in
  `ds target check`.
- **D6 Lines that are not rows** are counted on the pinned bytes into the plan, never dropped silently.
  While a side has any, a key not found there is INCONCLUSIVE, never NOT_FOUND; over 1,000 refuses the
  run. **On side a they are no census:** no pack is issued and readiness's population is not met (§11);
  on side b the pack carries the warning. No trailer allowance exists (§20).
- **D7 Hashing** in the worker across engines, in DuckDB for a file-to-file pair (ENGINES D4).
- **D8 The mount must be read-only for DeltaSleuth**, unless the target declares `allow_writable: true`
  (development), which the certificate prints.
- **D9 One DuckDB instance a process**, one budget, one sort at a time; spill is plaintext under the
  evidence volume by default, excluded from backups, swept at every start and hourly (§10, §13). The
  instance is built once whoever asks meanwhile, and one that was discarded makes no cursor more. A
  session that holds one of its cursors
  finishes on it and a per-key reader leaves it at its next read, so a second budget lasts until the
  passes and plans in flight on it have ended and its statements on a thread have returned (§10).
  **By READER.md** the instance is the process's file reader's, a child process started at the first
  file read: one shared reader a `ds` process, for plans and runs alike. Beside it there may stand a
  reader of one scan pass's own and one of a per-key read's or a plan's own, in which a file set that a
  reader ended on is read apart, and retired readers while their last cursors finish: each with its own
  `files.memory`, threads, spill limit and data limit, for as long as those reads last (READER §9, §10).
  Sorts stay one at a time a `ds` process.
- **D10 Refused on a file side:** graph parity, `regex` key maps, `ILIKE`, `table.schema`, nested
  Parquet columns, encodings other than utf-8, utf-16 and latin-1, and table pre-checks.
- **D11** `ds draft` does not exist and `ds profile` drafts service specs only (ENGINES D11): both stay.
- **D12 Names and per-file digests stay inside.** Plans, census records, packs, alerts, audit rows and
  logs name a file by position (`file 3 of 12`, path order); only `ds target check` prints names. The
  per-file list is sealed under the run's data key. Only the aggregate `files_sha256`, unkeyed, is in
  the stored plan, `facts/plan.json`, the pack's manifest (its subject) and the certificate (§13: the one
  exception to "tokens, counts and ids only"). The spec's own `file.path` is spec text, searched by PACK
  §2.3's gate. What is in clear beside it is of the side, not of a file: the count, the bytes of all its
  files and the newest modification time; a side of one file therefore shows that file's size and time,
  and its path where the spec's path names it (§13).
- **D13 Fixed bytes are read once.** A recheck never reads a file side again: its first observation
  stands (§12). As built: a read that failed (an outage, a timeout, temporary space) observed no bytes, so
  nothing of it stands and the recheck reads the side; a result both of whose sides read fixed files is
  final as read (`frozen_cut`).
- **D14 File work never runs on the event loop or asyncio's default executor**, and every call has a
  deadline: a mount that does not answer costs its own runs only (§10). "Its own"
  is its target's: abandoned calls are counted by the target's root, and a files target whose mount
  answers is planned and read while another's stands refused at its four. Only eight abandoned calls of
  all targets together (two dead mounts) stop file work for every target, to keep half the threads.
  **What a discard costs.** A statement abandoned past its
  grace, and an internal or fatal error of DuckDB's, discard the process's one instance, whichever
  target's statement it was (§10). After an abandoned statement no other session's work in flight is
  lost: a scan pass, a census, a frame and a plan finish on the cursor they hold, and a per-key reader
  opens on the new instance at its next read. It costs this: the next cursor asked for builds a new
  instance; until the discarded one's cursors are closed and its hung statement has returned both stand,
  each with its own `files.memory`, threads and spill limit (sorts stay one at a time a process), so the
  process may hold twice `files.memory` for the length of the longest pass in flight; the discarded
  one's spill directory, and what a sort of it has spilled, stay as long, and a statement hung in the
  kernel keeps them until it returns or the process restarts; a session that asks for a cursor in the
  moment between the discard and the new build is `Busy('Dropped')` (P34 at plan); and each of a dead
  mount's abandoned statements, four at most, discards the instance that took it, so up to four
  discarded instances may stand beside the current one, each kept by its hung statement and by what was
  in flight on it. Where an internal or fatal error discarded it, DuckDB has invalidated its database:
  the statements its other cursors send afterwards fail and are `Busy('Dropped')`, an outage, so a pass
  in flight on it is paused and sorted again and a plan under way is P34.
  **By READER.md** a statement given up past its grace keeps no thread and, once its reader's last cursor
  is closed, nothing: the reader is retired (no cursor more; a session that holds one finishes on it)
  and its process killed with its last cursor, at once where the statement was alone, and its memory,
  its threads and its spill directory go with the process, without a restart of `ds`. "A statement hung
  in the kernel keeps them until it returns or the process restarts" holds only of a killed process
  that the kernel does not let end (uninterruptible I/O on a dead mount): it is logged 10 s after its
  kill, and its read stays counted until it ends. On a mount that still answers a stat the kill is
  accounted as an end of the reader (READER §9); on a dead one it is the outage it was.

## 2. What is never claimed (`manifest.NOT`; `test_never_told` asserts each)

That the source system matches (only: no difference observed between the rows of these files, by digest,
and the other side as read); that the export is complete; an export time as an observation (it is
`declared by its stamp file` or `not observed`; modification times are `on the mount`); "snapshot" for a
file side (`fixed files`: the scan state's count of its passes is a member named `snapshots_x` for every
side, §11, a name and no claim); "immutable" or "tamper-proof" (a change restored between two checks is
not seen, and a parent directory swapped and put back is seen by no check at all: §14 point 4); that
single-key reads were hashed (they are checked by size, times and inode); that a recheck
read a file side again (it reads one whose first read failed, which observed no
bytes: §12); "side a observed whole" or "every line compared" while lines were not rows, or
such a line's text; "types verified" for CSV; "keys are unique"; a sniffed type as a file's type; object
storage; BENCHMARK's or ENGINES' figures; that DuckDB is a sandbox or its spill encrypted; "tested on"
Parquet from Spark, Hive, Impala or Arrow (DuckDB's own writer and §18's hand-built fixtures are
observed).

`manifest.NOT` holds this list as ten patterns (`snapshot`; `immutable` or
`tamper`; the source system matching or the export complete; an export time; single keys hashed or a
recheck's read; observed whole or every line compared; types verified or keys unique; object storage;
a sandbox or an encrypted spill; `tested on`), and `test_never_told` searches with each: every text
`manifest.py` and `duck.py` build (their string constants but docstrings), `census.described` and
`census.warning` of a fixed side's scan, table and frame records, and the certificate's own sentence.
The words of the documents are not searched by a test.
The UI's lines for a fixed side are held free of `snapshot` and of a digest by `web/test/packs.test.ts`.
One text built for a sampled cycle does hold the word: a frame of files, whose record's `consistency`
is `fixed`, takes PACK §3.4's `a table in one pass not observed to be a snapshot`, which denies a
snapshot and claims none (SAMPLING §3, §11.3).

By READER.md §2 `manifest.NOT` holds an eleventh pattern (`isolat|confin`) and `test_never_told` searches
`reader.py`'s texts too. Never claimed of the reader's process: that it is a sandbox, isolated or
confined (it runs as the same user, may open what that user may, and may signal its parent); that the
process which holds the keyring parses no hostile byte (the footer walk and the check of compressed
streams still read a file's bytes there); that a crafted file is harmless; that rows of the right form
are the files' rows, should a crafted file run code in the reader (the parent checks an answer's form,
never its truth); that the files of a run failed
with F12 are at fault (F12 says what was observed); a memory limit or denied sockets on macOS, or on any
host whose start line does not print them.

## 3. Release gates

- **Dependency.** `uv add duckdb` under the lock's `exclude-newer` (2026-09-21), pinned exactly
  (architecture Appendix C names 1.5.5, MIT). No pyarrow, pandas or numpy is imported: rows are fetched
  as tuples of `str`, `bytes` and `None`.
- **Extensions.** Needed: `parquet` and `core_functions`, expected compiled into the wheel (U2). CSV,
  gzip and the sort are core. `icu`, `json` and `jemalloc` may be compiled in; no statement calls an ICU
  or JSON function. **Every other extension is refused** (`httpfs`, `aws`, `azure`, `encodings`, `excel`,
  `spatial`, `avro`, `iceberg`, `delta`, the database scanners, community extensions): install and
  autoload are off, no `INSTALL` or `LOAD` is composed, external access is off, the configuration is
  locked. Nothing is downloaded at run time.
- **Confirmed at build on the locked version:**

| | Behaviour | Test |
|---|---|---|
| U1 | cp313 manylinux wheels for x86_64 and aarch64 (`release.MACHINES`), no required dependency, no `*.libs` file (else `[[vendored]]` decisions, RELEASE §8). **As observed** from `uv.lock`: `manylinux_2_28` wheels for both machines, no dependency (pyarrow, pandas, numpy are extras); the installed macOS wheel vendors no `*.libs` file. Neither manylinux wheel holds a `*.libs` file or bundles a shared library (RELEASE §7) | `test_duckdb_wheel` |
| U2 | `duckdb_extensions()` shows `parquet` and `core_functions` loaded, statically linked | `test_needed_extensions_are_built_in` |
| U3 | after `allowed_directories`, `enable_external_access=false`, `lock_configuration=true`: reads inside a root pass; a read outside, a sibling sharing a root's prefix, `http://`, `s3://`, `ATTACH`, `COPY TO` and `EXPORT` outside the allowed directories, `INSTALL`, `LOAD`, `SET`, `RESET` and `PRAGMA` are refused. **As observed:** `ATTACH`, `COPY TO` and `EXPORT` into an allowed directory (a root, or the temporary directory DuckDB allows by itself) run, so a cursor runs nothing but one `SELECT` (§10); `INSTALL` is refused only because the extension directory lies outside every allowed one: with it inside one, `INSTALL httpfs` downloaded the extension with external access off (observed once) | `test_guard_refuses`, `test_guard_reads_inside_a_root`, `test_guard_does_not_refuse_writes_into_an_allowed_directory` |
| U4 | the sort spills to `temp_directory` with external access off and stops at `max_temp_directory_size`; the smallest `memory_limit` under which 200,000 rows spill instead of failing with 2 threads is recorded (RELEASE §5). **As observed:** the limit given in `connect`'s configuration reads back and is not enforced (77 MB spilled under 1 MiB), so it is set by a statement before the lock, and then holds (`OutOfMemoryException`); 200,000 rows of 132 bytes spill from 72 MiB (70 MiB fails, from 90 MiB they fit in memory): a sort spills only near its data's size and otherwise fails with `OutOfMemoryException` | `test_spill_with_external_access_off` |
| U5 | the file list and every reader option bind as parameters | `test_reader_arguments_are_bound` |
| U6 | `execute` then `fetchmany` streams | `test_scan_memory_is_bounded` |
| U7 | `ORDER BY encode(k)` is code-point order | `test_key_order_corpus` |
| U8 | `allow_quoted_nulls=false` tells `""` from an empty field; a UTF-8 BOM is skipped; utf-16, latin-1, gzip and zstd read (spelled `utf-16` and `latin-1`: `utf16`, `latin1` are refused). **As observed:** a utf-16 file holding a character beyond the BMP (a surrogate pair) is refused whole, `InvalidInputException`, `ignore_errors` or not (P29) | `test_csv_nulls_bom_encodings_and_compression` |
| U9 | `CAST(DOUBLE AS VARCHAR)` reads back exactly; `epoch_us` needs no ICU (shown as its result being the same under any session time zone: ICU is compiled in and cannot be unloaded); a `DECIMAL` over 38 digits shows in `parquet_schema` (`duckdb_type` `DOUBLE`) | `test_golden_renderings` |
| U10 | `interrupt()` ends a sort within 5 s | `test_cancel_ends_a_sort` |
| U11 | cursors of one instance share memory, threads and spill limit | `test_one_budget` |
| U12 | `python_enable_replacements=false`: no SQL name resolves to a Python object | `test_no_replacement_scan` |
| U13 | the lines `ignore_errors` skips are the same whatever a statement projects (invalid encoding in an unprojected field, a wrong field count, an over-long line). **As observed:** a wrong field count and an over-long line, yes; invalid encoding is found only in a field a statement reads, so every CSV statement reads every declared column (§7) | `test_rejects_do_not_depend_on_projection` |
| U14 | `new_line` is honoured with `auto_detect=false`: a `\r\n` file leaves no `\r` in a field, and a file of other or mixed line ends gives lines that are not rows or the `\r` §6 step 5 refuses. **As observed:** `new_line` takes the escaped text (`'\n'`, `'\r\n'`); under DuckDB's default strict mode a `\r\n` file reads without `\r`, but an explicit `strict_mode=true` with `\r\n` reads no row and records no reject (§7 leaves it out); a file of other or mixed line ends makes step 5's first-record read raise `InvalidInputException` (P29), while the counting pass alone would read a `\r\n` file declared `\n` as no rows and no rejects | `test_csv_newlines` |
| U15 | `parquet_schema` shows a timestamp's unit and whether it is UTC-adjusted (and DuckDB reads a UTC nanosecond one to the microsecond). Also observed: a zoned value fetched as such needs `pytz`, which is not installed, so statements render it as text (§8) | `test_nanosecond_instants_are_found` |
| U16 | `store_rejects=true` works with `auto_detect=false` on the locked instance, gives file, line and kind, and its two tables belong to their cursor and go when it closes. **As observed:** the tables are made when the statement's rows are read to their end (a count fetched alone, then another statement, stores none); and an aggregate over a column holding invalid UTF-8 (`count(v)`) raises `InternalException` and invalidates the database, where selecting the column records `INVALID ENCODING`: the counting pass aggregates only over a statement that reads every declared column (§6), and a cursor drops an instance an internal or fatal error invalidated (§10) | `test_rejects_belong_to_their_cursor`, `test_an_invalid_encoding_rejected_under_an_aggregate_invalidates_the_database` |
| U17 | `memory_limit` and `max_temp_directory_size` given as `<n>MiB` read back as n × 2^20 bytes (`2048MiB` reads `2.0 GiB`) | `test_limits_are_binary` |
| U18 | **a gzip stream cut short or damaged, and a zstd stream cut short, read as fewer rows**, with no error and at most one reject (half a stream of 100,000 rows read about half of them; a gzip checksum is not checked; a damaged zstd frame raises `IOException`): so the plan checks each compressed stream whole as it hashes the file (§6 step 4) | `test_duckdb_reads_a_damaged_stream_as_fewer_rows`, `test_files_the_reader_cannot_read_are_refused_by_its_class` |
| U19 | in `QUALIFY row_number() OVER (... ORDER BY s DESC)` a name `s` that is both a select alias and a column of the reader is the column: §9's per-key statement writes the filter's expression there, and orders the outer query by its position | `test_per_key_rows_come_one_statement_a_group` (a column named `s`) |
| U20 | **the parallel CSV reader takes a line of a quoted field for a row**: where one of its scans starts (every 8 MiB of a file) inside a quoted field that holds a line end, the field's next line is one more row when it has the declared fields, and no reject; when it has not and the record after the field is not a row either, the field's line is a line that is not a row, and the lines after it are numbered one on; with `parallel=false` the rows and the lines are read as they are, across the 32 MiB buffer's end too: §7's reader is not parallel | `test_the_parallel_reader_takes_a_line_of_a_quoted_field_for_a_row`, `test_a_quoted_field_s_line_ends_are_read_across_a_scan_s_start` |
| U21 | **a file's last record cut inside a quoted field is dropped without a reject** by the reader that is not parallel, and by the parallel one too once a statement reads many files (12 with 2 threads: it then reads one thread a file); the parallel reader of one file records it (`UNQUOTED VALUE`), with one thread too, in a latin-1, utf-16, gzip or zstd file too: the plan reads each file once more with that one (§6 step 6) | `test_the_reader_drops_a_record_cut_inside_a_quoted_field`, `test_a_file_cut_inside_a_quoted_field_counts_one_line_whatever_the_files` |
| U22 | **with `hive_partitioning`, every `name=value` directory of a file's path is a partition**, the root's and the fixed part's too, and its value stands for a file column of that name in every row; a file's own name holding `=` is none: a plan refuses such a directory above the declared partitions (P26) | `test_a_name_value_directory_above_the_partitions_is_refused` |
| U23 | **a string column's row groups are pruned by the deprecated `min` and `max`** of old writers, which are in signed-byte order, by an `IN` or `=` on the column and by a join on the bare column (DuckDB derives a filter of the joined keys); a join on an expression of it prunes nothing: §9's per-key statement joins `encode(<key text>)` and binds no `IN` on any key (U30) | `test_a_text_key_is_not_pruned_by_statistics` |
| U24 | **a bound path that holds `*`, `?` or `[` is a pattern**, expanded at read, hidden directories among its matches: no such path is pinned (P18) or bound (§6 step 2) | `test_the_reader_is_handed_the_pinned_files_alone` |
| U25 | **an INT96 timestamp and a nanosecond time are read to the microsecond**, as `TIMESTAMP` and `TIME`, like the UTC nanosecond timestamp of U15; `parquet_schema` reports a millisecond and a microsecond timestamp as one `duckdb_type`: P23 and P22 read the footer's physical type and unit (§8) | `test_legacy_shapes_are_read`, `test_every_footer_holds_file_one_s_unit` |
| U26 | **a schema's nesting is followed by recursion without a bound**: a file of 20 KB whose groups nest 2,000 deep ended the process with a signal (observed on macOS arm64, in the planning thread; not on Linux). A walk that reads the first schema it meets takes three crafted footers for flat that DuckDB reads thousands of groups deep (the schema written twice, a reader taking the last; the second schema under the id 65538, which a reader cuts to 2; each group's `num_children` followed by field 5 again as an i64, which a reader passes over), and the process that plans or checks them ends the same way (SIGBUS at 3,000 to 20,000 groups): the walk reads the whole footer as the reader does (§6 step 4, U29). No test hands DuckDB such a file in its own process: one hands the plan and `ds target check` each of the three, 9,000 deep, in a process of its own, where they are refused and no statement is sent; that test hands the plan and the check the shapes of U30 and U31 too. **Not proven:** DuckDB's own parser and readers on a file the walk accepts (§14 point 7 says what was tried, and names what still ends the process) | `test_a_parquet_footer_is_walked_before_duckdb_reads_it` (the walk), `test_a_crafted_footer_reaches_duckdb_in_no_process`, `test_a_footer_duckdb_is_not_handed_refuses_the_plan_and_the_check` |
| U27 | **a scan's sort carries the key's text three times**, and what it spills depends on what it holds, not on its texts alone: with `ORDER BY encode(k)` the order key, the selected text and the ordered expression are each in the sorted rows (`SELECT id, c ... ORDER BY encode(id)` of a 44-character key spilled 247 bytes a row where `ORDER BY id` spilled 115), each carried value costs up to 24 bytes beside its text and a row 16; the peak spill of the 37 sorts measured was 0.36 to 0.98 of that sum (short and compressible texts least: DuckDB compresses its temporary files), and it steps up once the sorted rows pass about seven times `memory_limit` (§6 step 8, §10) | `test_a_scan_planned_just_under_the_spill_limit_completes` |
| U28 | **DuckDB keeps a Parquet file's footer between statements** (its external file cache, on by default; the Parquet reader's metadata cache, off by default, does the same when on) and takes a file of the same size whose modification time is the same to the second for the file it read: the new bytes were read by the old footer, a nanosecond column as microseconds in the year 55840, with no error. With `enable_external_file_cache` and `parquet_metadata_cache` false the file is read by its own footer: the instance sets both false, and `enable_http_metadata_cache`, before the lock, and proves them (§10). `parquet_metadata_cache` is no option of `connect`'s configuration (`InvalidInputException`), so the three are set by statements; `enable_object_cache` is a placeholder that does nothing | `test_a_parquet_file_replaced_in_place_is_read_by_its_own_footer`, `test_an_unproved_guard_is_p27` |
| U29 | **Thrift's generated reader, which DuckDB reads a footer with, reads a list's elements as the type parquet.thrift declares for the list's field, whatever element type the list itself names** (a footer in which each of the thirteen lists the walk knows names doubles in turn is read as the one that names them rightly); it takes the last of a field written twice, cuts a field's id to 16 bits, and takes a count of children from an i32 alone (the three footers of U26, 40 deep, are read 40 deep). The rows of a column whose chunk holds `crypto_metadata` (an encrypted column under a plain footer) are an `InternalException`, which invalidates the database. Observed by hand only, with scripts that are not in the repository: a number and a list's size are cut to 32 bits, bytes after the footer's end are ignored, and a field of a type Thrift does not know refuses the file | `test_the_reader_takes_a_list_s_elements_as_parquet_thrift_declares_them` |
| U30 | **DuckDB reads a column chunk's statistics whenever it reads the file, and trusts them; and it has no text for a `TIME` outside a day.** (1) `parquet_metadata` writes each statistic as text and faults there: a `TIME` column whose maximum is 2^60 microseconds ended the process (SIGSEGV; a file of 186 bytes, which the walk accepts), and a negative `TIME`, a NULL-typed column's and an `INTERVAL`'s minimum and maximum were an `InternalException`, which invalidates the database. No statement calls it, nor `parquet_file_metadata`: the plan's rows and sizes are the walk's (§6 steps 4, 6 and 8). (2) `read_parquet` and `parquet_schema` read statistics too (a statistic of another size than its type's refuses every read of the file, also `SELECT *`: `InvalidInputException`, P29) and survived every crafted one tried: 162 files, 19 statement shapes each, each in a process of its own, no process ended and no internal error. (3) They **prune** by them: `=`, `IN` and a join on the bare column skip a row group whose minimum and maximum do not hold the value, for an integer, a date and a decimal column as for a text one (U23). Where the statistics do not hold the group's values (100 to 200 for a group that holds 5; a decimal stored as bytes whose statistics are empty or of another length), the key a scan had read is on no row of a per-key read that binds a typed `IN`: none is bound (§9). (4) They **simplify expressions** by them: a guard `CASE WHEN t BETWEEN TIME '00:00:00' AND TIME '24:00:00' THEN CAST(t AS VARCHAR) ELSE error(…) END` was reduced to the cast where the statistics said that every value lies inside the day. (5) A `TIME` outside a day **in a page** is read as it is, and DuckDB's own text of it (`CAST(t AS VARCHAR)`, a fetch of the value) ended the process from about 10^17 microseconds and was an `InternalException` for a negative one: a time is rendered from `epoch_us`, which converts nothing (§8). Values of the other types that their type cannot hold gave texts or plain errors (79 files) | `test_a_crafted_footer_reaches_duckdb_in_no_process` (the statistics and the pages, in a process of its own), `test_a_parquet_plan_s_figures_are_the_footer_walk_s`, `test_a_typed_key_is_not_pruned_by_statistics`, `test_a_time_is_written_from_its_microseconds` |
| U31 | **Thrift's reader makes a list's elements before it reads them**, so what DuckDB holds of a footer follows the sizes its lists declare, not its bytes, and lies outside `memory_limit`: 704 bytes a column chunk, 96 a row group, 64 a key value, 24 a column order, an encoding statistic or a path's name, 16 a sorting column, 8 or 4 a number, each declared by one byte of footer (a million of each, by the peak resident memory of a process of its own under `memory_limit` 256 MiB; a footer of 1 MiB, a second row group of a million column chunks, took 672 MiB before DuckDB refused the file). A statement over several files held about four footers at a time with two threads. The walk bounds the counts (§6 step 4): the largest footer it accepts (200,000 column chunks and 1.4 million key values) took 235 MiB, four of them in one statement 725 MiB under `read_parquet` and 965 MiB under `parquet_schema`; a footer of DuckDB's own writer of 100,000 column chunks (7.7 MiB) took 82 MiB. And **DuckDB reads some negative numbers of a footer as an internal error**: a schema element's type length, scale or precision when it binds the file, a row group's count of rows under `count(*)`: the walk refuses each. All on macOS arm64 | `test_a_footer_holds_the_schema_s_chunks_and_few_lists`, `test_a_crafted_footer_reaches_duckdb_in_no_process`, `test_a_footer_duckdb_is_not_handed_refuses_the_plan_and_the_check` |
| U32 | (READER.md) **the reader's process starts with nothing of its parent's**: an empty environment, no `deltasleuth` package among its modules, no descriptor beyond its three; it proves its guard and its limits, reads and spills. **As observed** on 2026-10-04 in an image built from the source tree, under the compose hardening with no network, natively on linux/arm64 (kernel 6.12.54): the self-test prints `file reader: duckdb 1.5.5 in a process of its own, replaced when it ended; data limit held, sockets denied`, and no descriptor is inherited. Under emulation (the amd64 image on that arm64 host, by Rosetta) a reader inherits 3 descriptors, the emulator's own, which is warned of and not fatal | image `test_the_self_test_passes`; host `test_the_reader_is_started_with_nothing` |
| U33 | (READER.md) **a fault ends the reader's process alone**: SIGSEGV sent mid-statement is `Died`, the process that read lives and a new reader reads; the third such end while one side's files alone were in flight is F12. **The kept mutant** (§14 point 7's page), as observed: on macOS arm64 each scan of it ends the reader with SIGBUS or SIGSEGV; on linux/arm64, and in the amd64 image under emulation, with SIGSEGV; its plan passes everywhere, the third scan is F12, and a clean file is read afterwards. On macOS, where a process ended by such a signal is a crash report, the host tests (READER §15) make a real fault only with `DS_FAULTS=1` (SIGKILL is sent otherwise, and the kept mutant's tests are skipped); on Linux, and in the image whatever the host, always | host and image `test_a_fault_ends_the_reader_s_process_alone`, `test_the_crafted_page_is_contained` |
| U34 | (READER.md) **no reader outlives its parent**: an exit, SIGTERM, SIGKILL while it is idle and in the middle of a sort; its spill directory goes with the next sweep. A `ds` process that exits kills and reaps its readers first, so a command run beside a container's first process (`docker compose exec`) leaves no zombie entry | host and image `test_no_reader_outlives_its_parent`; host `test_a_process_reaps_its_reader_as_it_exits`; image `test_a_command_leaves_no_reader_behind` |
| U35 | (READER.md) **the data limit holds under DuckDB's allocator, and leaves the default budget room.** A statement that holds about 1 GiB under a data limit of 512 MiB does not complete. **As observed** (linux/arm64): DuckDB does not refuse it, the reader's process ends with SIGSEGV within a second, and its statement is `Died`: a data limit met is an end of the reader, charged like any (READER §9). 200 open cursors add no thread to the reader, and under a data limit of 1 GiB the self-test's sort completes and spills beside them. A pass over 10M rows a side of §17's shape (Parquet against Parquet, both sides' sorts held at once) under the default settings completes under the limit of 5,296 MiB, the reader's `VmData` 2,579 MiB and its resident size 2,229 MiB at their peaks (one run). An idle reader holds 17 threads, 186 MiB of `VmData` and 60 to 91 MiB resident (seven starts, each steady from half a second after its start to the 20 s looked at) | the self-test's step 3; host and image `test_idle_cursors_hold_no_thread`; image `test_the_default_limit_fits_ten_million_rows` |
| U36 | (READER.md) **the socket filter denies sockets and stops nothing the self-test does**, natively on aarch64 (`socket()` in the reader raises `PermissionError`; the self-test's reads, sort and spill pass under it). Its program decides for aarch64 and x86_64 as READER §7 says, by an interpreter in the suite. **Not observed: an x86_64 kernel.** Under Rosetta the filter is not loaded and sockets are not denied; READER R8 excepts this row from the release's stop | the self-test; host `test_the_socket_filter_decides_by_architecture` |
| U37 | (READER.md) **no core, and the kernel takes the reader first.** With DuckDB loaded and a statement running the reader's core limit is 0 and it is not dumpable: SIGSEGV ends it without a core, where a process started beside it without those restrictions left one (`core_pattern` `core`). In a container of 512 MiB a reader that outgrows it (`oom_score_adj` 1000) is killed by the kernel (SIGKILL), its statement is `Died`, and the process that read goes on and exits 0. Both on linux/arm64, and in the amd64 image under emulation | image `test_the_reader_leaves_no_core`, `test_the_kernel_takes_the_reader_first` |
| U38 | (READER.md) **the boundary's cost**: §17's nine tests with the reader in its child. As measured on 2026-10-04 (§17): with nothing read ahead two Parquet-to-Parquet cases took over 1.15 times their figures of 2026-10-03, so one batch is read ahead (READER R9); with it none does | the nine `DS_PERF` tests |

- **Size, SBOM, audit.** About 20 MB a wheel and 60 to 70 MB installed (documentation); the build
  records both in RELEASE §5: from `uv.lock`, 21.5 MB (x86_64) and 19.4 MB (aarch64); installed on macOS
  arm64, 45.9 MB (Linux not observed). `release.lacks` and `_pip_audit` cover `duckdb` from `uv.lock` with no code
  change. RELEASE §7 "Not covered" gains DuckDB's statically compiled third-party code (zstd, snappy,
  brotli, lz4, miniz, mbedTLS, RE2, ICU, jemalloc, Thrift, yyjson; versions from its notice, licences by
  hand in Appendix C): syft, grype and the `*.libs` walk see none of it. **Open:** the wheel carries no
  such notice (one MIT `LICENSE` of seven lines, observed), so the versions and licences need DuckDB's
  source tree: RELEASE §7 records the gap.
- **Image.** Read-only root, uid 10001, `noexec` `/tmp`: DuckDB writes only under its spill directory.
  `deploy/Dockerfile`'s `install -d` line gains `/var/lib/deltasleuth/files`, the mounts' parent. It
  gains `/var/lib/deltasleuth/spill` too, a mount point owned by the service's
  user for a spill volume of its own: a new named volume mounted where the image holds no directory is root's,
  and uid 10001 could not write it (P28; DEPLOY section 10).
- **Smoke.** `release._smoke` gains, under `HARDENED` with the evidence volume, `python -c "from
  deltasleuth.connectors.tables import duck; duck.selftest()"` (`release.DUCK`): it writes a three-row CSV
  and an embedded two-row Parquet constant under `/tmp`, proves U2 and one U3 refusal, reads both, and
  sorts 200,000 generated rows under U4's recorded limit into the evidence volume. As built: it spills in
  `files.spill_dir` of `DS_CONFIG`'s settings (the evidence volume's `spill` by default), sorts at
  `duck.SELFTEST_MIB` (80 MiB, inside U4's band) under a 256 MiB spill limit, and fails unless a watcher
  saw its sort write to the temporary directory. Only the docker stub has run it (RELEASE §13). Failure: `the smoke test failed:
  DuckDB does not load, its guard does not hold, or it cannot read and spill under the compose hardening`.
  **By READER.md §11** the self-test reads through a reader's process and does more: it sends that
  reader SIGSEGV mid-statement and requires that the statement is `Died`, that the process and its spill
  directory are gone and that a new reader reads both files; on Linux it requires that the first reader's
  start saw its data limit hold and, on aarch64 and x86_64, sockets denied, and that a statement holding
  about 1 GiB does not complete under a data limit of 512 MiB; it prints `file reader: duckdb 1.5.5 in a
  process of its own, replaced when it ended; data limit held, sockets denied`. `release._smoke` runs it
  without its limits (`duck.selftest(limits=False)`: `…; limits not tried`) where the image's machine is
  not the build host's, since it then runs under emulation. Failure: `the smoke test failed: DuckDB does
  not load, its guard does not hold, it cannot read and spill, its process is not replaced when it ends,
  or its limits do not hold, under the compose hardening`. **Run for real** on 2026-10-04, not by the
  release tool but by `tests/e2e/test_reader_image.py::test_the_self_test_passes`: the same `python -c`
  under `release.HARDENED` with an evidence volume and no network, in an image built from the source
  tree (`deltasleuth:reader`, linux/arm64, natively: both limits held), and without its limits in the
  amd64 image under emulation. U4's sort at 80 MiB therefore spilled and completed on the Linux
  aarch64 wheel too, and on the x86_64 wheel under emulation. `release._smoke` itself has still run on
  the docker stub only.
- **Image tests** (READER.md §3). `tests/e2e/test_reader_image.py` holds U32 to U37's image tests. The
  image holds no pytest: each test is one `docker run --rm --pull never --network none` of the image
  named by `DS_IMAGE` (skipped without it) under `release.HARDENED`, with a settings directory, an
  evidence volume and a volume at `/var/lib/deltasleuth/files`, running one `python -c` text against the
  product's own `duck` and `reader` and asserting the line it prints. An image of another machine than
  the host's runs under emulation, where its readers start with `files.os_limits` off (below) and the
  test of the default limit is skipped. **Under emulation** (amd64 on an arm64 host, Rosetta) a reader
  with its data limit does not start (`ended as it started (SIGTRAP)`: the limit counts the emulator),
  and its socket filter is not loaded: `files.os_limits: false` is needed there. Emulation is no x86_64
  kernel.
- Not released before §17's measured figures are in TABLES, DEPLOY and README. They
  are (TABLES §14.9; DEPLOY section 7, Files; README), single runs on the macOS arm64 wheel (§17); the
  notice of DuckDB's compiled-in libraries is still open (RELEASE §7).

## 4. Target and settings (`core/spec/targets.py`, `core/types.py`, `config.py`)

```yaml
legacy-exports:
  engine: files
  root: /var/lib/deltasleuth/files/legacy   # inside the container; the host mounts it :ro
  settle_s: 60            # 0 to 86400: a file modified more recently is not read (default 60)
  allow_writable: false
  rate_per_s: 5           # per-key statements, each one pass over the files (default here: 5 and 2)
  max_concurrency: 2
```

- `engine: files` requires `root` and takes only these members (T8). `root` is absolute, at most 1,024
  characters, with no `.` or `..` segment, and not `/` (T9). It is also in its
  normal form, without a trailing or a doubled `/`: such a root is not its own real path, and every plan
  and check would refuse it as a symbolic link (P14's second text). `Target` gains the three members; ENGINES'
  `engine` property answers `files`; capabilities are `DATABASE_CAPABILITIES`. No secret. As built: the
  members are `Target.root`, `settle_s` and `allow_writable`, flat; a files target's `rate_per_s` and
  `max_concurrency` default to 5 and 2 (20 and 4 elsewhere); the schema refuses a files member on another
  target and `_files` words T8 and T9.
- **Allowed roots** (`manifest.forbidden(settings, registry)`; T10). After `realpath`, a root must lie
  under `files.roots_under` (default `/var/lib/deltasleuth/files`, the directory the image creates) and,
  as a second check, must not be, hold or lie inside `evidence_dir`, `workspace_dir`, the spill
  directory, the settings or targets file's directory or a `file:` reference's directory. It needs the
  settings, so it is raised by `Context.open` (a `ContextError`: every command that opens a context
  exits 2 and `ds serve` does not start) and by `ds target check`; `load_targets` and
  `ds spec validate --targets` do not check it. As built: `Settings` does not keep its own file's path,
  so `Context.open` judges the targets file's directory (DEPLOY keeps both files in `/etc/deltasleuth`)
  and `ds target check` the settings file's too; "lie under" includes `files.roots_under` itself. Both
  run it on `manifest.POOL` (`manifest.opened` for `Context.open`, which also sweeps the spill): a root
  that does not answer within `STAT_S` is judged as written, so a dead mount stops no command.
  `ds target check` calls `manifest.opened` too (with its settings file), so one dead mount does not
  hang the check of every files target.
- **A missing mount** is found at run creation and by `ds target check` (P14), never at start: one
  absent export does not stop `ds serve` for every other pair.
- **Settings** (`FilesSettings`, block `files`, optional, `_CLOSED`, applied at restart; X1): `memory`
  (`2GB`) and `spill` (`20GB`), sizes of `^[1-9][0-9]*(MB|GB|TB)$` in binary units as
  `sampling.temp_buffers` reads them, handed to DuckDB as whole `MiB` (U17); `threads` (2; 1 to 64);
  `spill_dir` (default `<evidence_dir>/spill`); `roots_under`; `max_files` (1,000; 1 to 20,000);
  `plan_s` (300; 10 to 86,400: of a run's creation, all its file sides
  together, §6); `os_limits` (`true`; a boolean: whether the reader's process sets itself a data limit
  and denies itself sockets, [READER.md](READER.md) §4 and §7). They are budgets of each process that
  runs `ds`, not of a run. As built:
  any invalid value of the block is X1 whole; a member the block does not take keeps the settings' usual
  `files.<name>: Unexpected keyword argument`; `Settings.spill_dir` answers the default;
  `manifest.size_bytes` reads a size.

## 5. Spec (both `spec.schema.json` copies; `rules._tables`, `_targets`, `_gating`; `assemble._table`)

```yaml
table:
  name: customers             # the label reports show, required as today
  keyColumns: [customer_id]
  columns: [email, balance, opened]
  where: "status = 'A'"
  file:
    path: customers/current/*.csv
    format: csv               # csv | parquet: declared, never guessed from a name
    asOfFile: _AS_OF          # optional stamp file
    maxAge: 26h               # optional
    partitions: [dt]          # optional: key=value directories read as text columns
    binaryAsString: false     # parquet: un-annotated BYTE_ARRAY read as text
    csv:
      columns: [customer_id, email, balance, opened, status]   # required: every field, in file order
      keys: {customer_id: {as: integer}}   # optional: integer | decimal with scale | date; else text
      delimiter: "|"          # required
      newline: "\n"           # "\n" | "\r\n" (default "\n")
      quote: "\""             # one character, or "" for none
      escape: "\""
      header: true
      skip: 0                 # lines before the header
      encoding: utf-8         # utf-8 | utf-16 | latin-1
      "null": ""              # the unquoted text read as NULL
      compression: none       # none | gzip | zstd
      padded: false           # true: columns are class char (trailing spaces stripped)
typeMap:
  balance: {as: decimal, decimalSeparator: ","}
  opened: {as: date, format: "%d/%m/%Y"}
```

- **Path** (at most 512 characters). Segments of letters, digits, space and `. _ - = + @ , ( )`, none
  starting with a dot; no leading `/`, `.` or `..` segment, or backslash. One file; `*` inside one
  segment; or a trailing `/`: every regular file below, at most 8 directories deep (deeper refuses,
  P17), whose name starts with neither `.` nor `_`; a directory whose name starts with either is not
  entered (`_temporary/` of a job still writing, `_SUCCESS`, `.crc` files).
- `file` goes exactly with a files target (L5). `TableConfig` gains `file`. `assemble._member` is
  untouched: a hash sample's population id stays `table:<target>/<name>`, so a path change resets no
  draw ledger (keep one label for one export stream).
- `csv.columns` are `columnName`s; `keyColumns`, `columns`, `where`, `typeMap` and strata name them.
  `partitions` are `columnName`s too, none a `csv.columns` name (L10) or a column of the files (P26).
  With `partitions`, no other `name=value` directory may lie in the target's root or
  in the path's fixed part (P26's third text; §6 step 2).
- **`csv.skip` above 0 is a filter:** it leaves each file's first lines out of
  what is compared, so under SSO the spec needs the approval a row filter needs (`population_filters`,
  APPROVALS §1.1); the plan stores the lines (`lines_skipped`, §6) and the census wording says them (§11).
- **Two `typeSpec` members** (`core/canonical/convert.py`; any table side; L11). `format`, with
  `as: date` or `timestamp`: `%Y %m %d %H %M %S %f %z` and the characters `- / . : T` and space, parsed
  whole by `strptime` in `_instant`; `%y` and `%b` are refused (a pivot year, a locale).
  `decimalSeparator: ","`, with `as: decimal` or `integer`, in `_decimal`, which gains the spec: a text
  holding the other separator does not convert, and group separators are not read.
  `decimal` and `integer` read a text only in a number's ASCII form (a sign, the digits 0 to 9, one point,
  an exponent). Python's `Decimal` also reads an underscore between digits and the digits of every script
  (`1_000` as 1000), which is not the number the export holds: such a text does not convert.
- ENGINES' `emptyAsNull` bridges `''` against NULL.

## 6. Plan (`manifest.py`, `duck.describe`, `tables/plan.py`)

**ENGINES amendments.**
- `Engine.describe` is `describe(session, schema, name)`, called by `plan.described` (at planning and in
  `preflight`) and by the schema guard (`drift._relations`, with the stored plan's `schema` and `name`);
  the files engine needs the side's `file` member there, through a keyword `side` the other engines
  ignore: at planning `side` is the side's `SideConfig` (its `table` member and its letter, by which a file side's
  refusals are worded, as `manifest` words them), afterwards the stored plan with its pins opened into it
  (`pinned`: its letter, files and pins, by `plan.unsealed`), which the scan's V2 look and the guard's
  pass too; a call without it (a table pre-check's) is refused with K1. `Engine.retry_alone`
  (false for `files`): `connector._read` re-reads a timed-out batch key by key only when true.
  `relation(side)` is the name alone without a schema. `consistency` gains `fixed`. `FilesChanged` and
  `RowsMoved` join the `TableError` classes; `NotReady(ValueError)` carries a closed `code` (§12).
  `request_url` is `files://<target_id>/<name>`; `arms = 200`; `unfit = ()` (§9).
- **ENGINES §7's cross-engine check runs whenever a table side's engine is `files`**, a file-to-file
  pair included: families (P9), `other` (P8) and key texts (P6). A PostgreSQL side of such a pair stores
  `classes`, `declared`, `tls` and `consistency: snapshot`, and the plan `rendering: 1`, as beside MySQL
  or Oracle. D3's frozen PostgreSQL pairs are untouched.
- `core.notify.error_type` reads a `TableError`'s class (§12).

At run creation, per file side, in order, inside `files.plan_s` (P30). The run's
creation has one `files.plan_s` for all its file sides, counted from when its planning starts
(`duck.creating`, which `tables.planning` enters), so a file-to-file pair plans within the proxy's read
timeout DEPLOY prescribes. A side planned once the creation's time has passed is P30 (`0 of 12 files hashed`).

1. **Root.** `realpath(root) == root`, a directory DeltaSleuth may list (P14), on a read-only mount
   (`statvfs` `ST_RDONLY`; P15 unless `allow_writable`). The root's mount says
   nothing of a mount below it (exports mounted under a root that lies on the image's own read-only file
   system), so step 4 judges the mount of each file it reads, by `fstatvfs` of the file's descriptor: a
   writable one is P15's second text, and with `allow_writable` it makes the plan's `mount` `writable`.
2. **Expansion** (`os.scandir`, real directories only). A symbolic link, anything not a regular file, a
   name outside the path's alphabet (so no name is a glob, a quote or a URL scheme) or a file the uid
   may not read **refuses the plan**, never skipped (P18). 1 to `files.max_files` files (P16, P17),
   ordered by the relative path's UTF-8 bytes. With `partitions`, each file's directories below the
   path's fixed part are exactly those `name=value` segments, in order (P26).
   - A directory matched under a globbed segment (`dt=*/part.parquet`) is entered only when its name is of
     the alphabet; any other is a candidate that refuses the plan (P18), as below a directory read.
     Entered, a directory named `*` would put `*/part.csv` among the pinned paths, which DuckDB expands
     into files that were never listed, pinned or hashed.
   - `duck._reader` binds a file only when it is one of the side's pinned files and its path holds none of
     `*`, `?` and `[`, which make a path a pattern to DuckDB; else `TargetRefused('Unpinned')`. A root
     whose own path holds one is therefore read by no statement.
   - With `partitions`, no segment of the target's root or of the path's fixed part may hold `=` (P26's
     third text): DuckDB reads every `name=value` directory of a file's whole path as a column, in place
     of the files' own column of that name (observed).
3. **Arrival.** The stamp file (a name in the path's fixed directory that the path does not match) is a
   regular file of at most 4,096 bytes, no link, whose first line is one ISO-8601 instant with an offset
   (P20). No file was modified within `settle_s` of the host's clock; with `maxAge`, the newest is not
   older (P19).
4. **Pin.** Each file is opened `O_RDONLY | O_NOFOLLOW`, `fstat`ed, hashed in 1 MiB reads (a cancel
   flag is read at each: §10) and `fstat`ed again (P19 if it moved). By U18 a CSV side
   declaring `compression` `gzip` or `zstd` has each file's stream checked whole by the same reads (Python's
   `zlib`, the locked `zstandard`: every member to its end and its checksum; what they decompress is
   dropped a slice at a time), else P29 names `BadGzipFile` or `ZstdError`; the hash cache is keyed by the
   stream too. After a gzip member's end the check goes on with what `zlib` left
   unused, never also with the tail its output limit left unread (once a member's text passes 1 MiB both
   hold the next member's bytes). A Parquet file's footer is walked by the same call once the file is read
   (`manifest._footer`, no DuckDB), before any statement names the file: a footer over 16 MiB, a schema of
   over 10,000 elements or nesting groups over 32 deep (the root among them), and a footer that is not
   well formed (an element with a negative count of children, elements that are not exactly the root's
   tree, a length past the file, anything the walk does not read) are P22's fifth text. A file that does
   not end in `PAR1` is left to DuckDB, which reads no footer of it and words it. `ds target check` walks
   each file the same way before it reads one (§16).
   **The walk reads the whole footer as DuckDB's reader takes it**, so that the schema judged is the schema
   DuckDB reads (U26). The reader is Thrift's generated one (U29), and the walk holds
   to what that reader does, field by field of FileMetaData to the footer's declared length:
   - a struct's fields ascend by id, each once (a reader takes the last of a field written twice), and
     an id a reader would cut to 16 bits is refused;
   - the schema is the footer's one field 2, a list of structs; an element's count of children is its
     field 5 of type i32 and of no other, a number a reader would not cut to 32 bits;
   - a list is read only where parquet.thrift declares one in FileMetaData, and only when it names the
     declared element type: the schema, the row groups, a group's column chunks and sorting columns, a
     chunk's encodings, path, key values, encoding statistics, level histograms and geospatial types,
     the file's key values and column orders (`manifest._FOOTER`). A reader reads such a list's
     elements as declared whatever the list names, and passes over a list it does not know by what it
     names, so anywhere else the two would pass over different bytes;
   - a set, a map, a type Thrift does not know, structs nested over 8 deep, a text's length or a list's
     size that a reader would cut to 32 bits or refuse, a footer that ends before its declared length
     and a byte after its end refuse the file.

   Each is P22's fifth text, which ends `or it is not of the form this build reads`. A file that ends
   `PARE` (an encrypted footer) and one a column chunk of which holds `crypto_metadata` (an encrypted
   column under a plain footer, whose rows DuckDB reads as an internal error that invalidates its
   database: U29) are P22's second text, said by the walk: DuckDB is handed neither. A file that ends
   in neither `PAR1` nor `PARE` is still left to DuckDB, which refuses it by its last bytes and reads
   no footer of it (observed: `test_a_crafted_footer_reaches_duckdb_in_no_process`). The walk is
   stricter than the reader: it refuses footers DuckDB would read (§20).
   **The walk also bounds what a footer makes DuckDB's reader
   hold, refuses the numbers DuckDB reads as an internal error, and gives the plan its figures** (U30,
   U31):
   - a row group's column chunks are exactly the schema's leaves (its elements without children, but the
     root): as many, each with its metadata, whose `path_in_schema` is the leaf's path, in the schema's
     order (DuckDB takes a leaf's chunk by its position); a row group without chunks, and a footer whose
     row groups are no list, are refused;
   - at most 10,000 row groups (`FOOTER_GROUPS`), 200,000 column chunks in all of them (`FOOTER_CHUNKS`)
     and 2,000,000 elements in all the footer's lists together (`FOOTER_ITEMS`), each judged from a
     list's declared size before its elements are read. Where they come from: the largest file of §17's
     tests (10M rows of 4 columns, DuckDB's own writer, a row group each 122,880 rows) holds 82 row
     groups, 328 column chunks and 1,075 list elements; that writer spends 81 bytes of footer and 3 list
     elements a column chunk (footers of 39,200 and 100,000 chunks, measured), so the 16 MiB a footer
     may hold are about 200,000 of its column chunks, 10,000 row groups are 1.2 billion of its rows, and
     2,000,000 list elements are ten a column chunk at that limit;
   - no negative number in a schema element's type, type length, repetition, count of children,
     converted type, scale and precision, in a row group's or the file's count of rows, or in a column
     chunk's uncompressed size: DuckDB binds a file with a negative type length, scale or precision, and
     counts the rows of one with a negative row count, as an internal error (U31);
   - it returns the footer's count of rows (`num_rows`) and each top-level column's uncompressed bytes
     over its row groups (`total_uncompressed_size`), which steps 6 and 8 take: no statement reads
     them. A file whose hash the process's cache holds (below) is not read again, but its footer is
     walked again at every plan.

   P22's fifth text names the two counts a legitimate export could pass: `… or nests groups over 32 deep, it
   holds over 10,000 row groups or 200,000 column chunks, or it is not of the form this build reads`. A
   file's pin is `[bytes, mtime_ns,
   ctime_ns, inode, sha256]`. A process cache keyed by device and those four spares `compare_one`, a
   firing's retries and the retry of a plan P30 stopped; it is used only for a file whose change time
   differs from its modification time (one renamed into place, DEPLOY's advice), since equal times do
   not show that the mount keeps a change time; a closing hash (§11) never uses it.
5. **Schema.** Parquet: every planned column has file 1's name and type in every footer (P22, P23); the
   type compared holds a time's or a timestamp's unit and whether it is INT96,
   which DuckDB reads as one type (§8). CSV:
   each file's first record after `skip`, read by the same reader (`header=false LIMIT 1`), has as many
   fields as `csv.columns` (a record that does not come back is P21: U14 found that a file of other line
   ends than declared raises here, or reads as no record, and the counting pass alone would read it as no
   rows and no rejects). The declared reader (`ignore_errors` on) skips a first record of
   another field count and returns the next, so the record is read by the same reader with 64 more
   columns and `null_padding` (`nullstr` a byte no field holds, so a field past the record's last is NULL).
   That reader raising (other line ends) is P29 naming the class; a file with no record is P21 `has 0
   fields`; with `newline: "\n"` its last field does not end in `\r`; with `header`,
   each field equals its declared name after both are folded (lower case; each run of characters outside
   `a-z 0-9 _ $ #` one `_`, trimmed; `_` before a leading digit) (P21). The record is compared, never
   stored, logged or quoted: a headerless file would leak a row.
6. **One counting pass**, on a cursor of its own (U16), timed as `pass_s`.
   - CSV: one statement with `store_rejects=true` (`rejects_limit` 1,001) that reads every declared
     column, so no later statement can reject a line this one accepted (U13). U16's internal error (`count(v)` over a
     line of invalid UTF-8 raises `InternalException` and invalidates the instance) needs a
     statement that reads the column holding invalid UTF-8 without the file's first column (observed: the
     same file read through `strlen(v)` alone raises, through `strlen(id) + strlen(v)` records the reject),
     so the counting pass, which reads every declared column through §7's filter, aggregates (`count(*)`,
     a filtered count, the sum of the texts' bytes) over expressions of them in one row: observed to record
     every reject and raise nothing, a file with invalid UTF-8 in a middle and in its last column among them;
     a cursor still drops an instance an internal error invalidated. Each (file, line) of the pass's rejects
     is one line, of the kind of its first error by column. **The end check** (U21; a side that declares a
     quote): the reader drops a file's last record cut inside a quoted field and records no reject, so each
     file is read once more, alone, by the same statement with `parallel=true`, which records the cut
     record (`UNQUOTED VALUE`). That reader is the one U20 found wrong, so it must have read the pass's
     `rows`, all files together, and the pass's lines that are not rows, each by its file, line and kind,
     and beyond them nothing but a file's last such line, of kind `quote`: that line is counted, and
     anything else refuses the plan (P33), since no file's end is then known. It gives `rows`,
     `selected` (rows the filter selects) and the bytes of the planned columns' texts; `reject_errors`
     gives the distinct (file, line) pairs that are not rows and their kinds (`columns`, `quote`,
     `line_size`, `encoding`, `other`); `csv_line` and `error_message` are never read. `rejected` over
     `REJECT_MAX = 1000` refuses (P24).
   - Parquet: `rows` and the planned columns' uncompressed bytes come from the footers; one statement
     reads the key and filter columns of every row group and counts `selected`: the first read of data
     pages, so a damaged key column shows here. The two figures
     are the plan's own walk's (step 4): no statement calls `parquet_file_metadata` or
     `parquet_metadata` (U30: the second ends the process on a crafted statistic).
   - Any DuckDB failure here or in step 5 is P29, naming the class.
   - The pass also counts, among the rows the filter selects, the keys of a
     `date` key column whose text is not `YYYY-MM-DD` (a Parquet DATE outside the years 0001 to 9999 or
     infinite; a CSV `csv.keys` date of the year 0000, which DuckDB reads as 1 BC): any refuses the plan
     (P6's third file text): its row would be scanned and then answered 404 by its own key. And the
     plan stores `lines_skipped`, the lines `csv.skip` left unread: `skip` times the files.
7. **Flood guard.** The first 1,000 rows of file 1 are converted with the side's `typeMap` and
   `csv.keys`; a column failing in more than half refuses (P25).
8. **Budgets.** A per-key read is one pass: `2 × pass_s` over the spec's `execution.timeouts.total`
   refuses (P31; the default is 60 s). `sort_bytes` is what a scan's sort of the side carries: its key
   texts and, across engines, its compared columns' texts (32 bytes in a file-to-file pair), plus 16
   bytes a row. As built: a file-to-file pair's engine columns (§9) count 32 bytes a row and its worker
   columns their texts, as every compared column does across engines; a CSV side's texts come from the
   counting pass, a Parquet side's key texts from it and its worker columns' from the footers' uncompressed
   sizes (as the walk read them: step 4; the figure is the one DuckDB's
   `parquet_metadata` gives of a file of its own writer, asserted). A table scan whose file sides'
   `sort_bytes` together pass `files.spill` refuses (P32).
   **`sort_bytes` projects what DuckDB's sort spills, as measured (U27):**

   ```
   sort_bytes = 1.15 × ( 3 × key texts' bytes + worker columns' texts' bytes
                         + rows × (24 × values + 32 if an engine column is hashed + 16) )
   values     = 3 × key columns + worker columns + 1 if hashed
   ```

   over the rows the filter selects that hold a key (`duck.SORT_KEYS`, `SORT_VALUE`, `SORT_ROW`,
   `SORT_MARGIN`). The texts and 16 bytes a row alone are about half of what the sort spills, and less for
   a long key. The peak
   spill of the 37 sorts measured (14 shapes of 2M to 20M rows: keys of 8 to 44 characters, 1 to 12
   columns of 1 to 176 characters, digits, hexadecimal and base64 texts, CSV and Parquet, hashed and not,
   under 128 MiB to 1 GiB of memory, 2 threads, on macOS arm64) was 0.36 to 0.98 of the sum in brackets,
   to which the projection adds 15%. The same figure is P32's `needs about N GiB` and the free space
   asked before a sort (§10).
9. **Closing stat.** Every file still has its pin's size, times and inode (P19): the counts describe
   the hashed bytes.

The counts of steps 6 to 8 are cached a process by `files_sha256` and the digest of the side's `table`
member: the same bytes under the same declaration are counted once (as built: the counting pass, its end
check, the flood guard and `sort_bytes`, which `duck._count` gives together; step 5's first records and
footers are read at every plan, and P31 and P32 are judged at every plan from the counts). A side that
declares `partitions` is counted by its paths too, which its `files_sha256` binds (below), so the same bytes
under another directory are counted anew.

**Stored form**, beside ENGINES' `classes`, `declared` and `digest`:

```json
{"schema": null, "name": "customers", "engine": "files", "server": "duckdb 1.5", "consistency": "fixed",
 "file": {"path": "customers/current/*.csv", "format": "csv", "csv": {"delimiter": "|"}},
 "file_count": 12, "bytes": 3650722201, "files_sha256": "<64 hex>", "pinned_at": "2026-10-01T02:14:07Z",
 "as_of": "2026-09-30T23:59:00+00:00", "modified_last": "2026-10-01T01:58:11Z", "mount": "read-only",
 "rows": 10000000, "selected": 9120000, "rejected": 0, "kinds": {}, "pass_s": 21.4,
 "lines_skipped": 0, "sort_bytes": 1012000000}
```

- `files_sha256` is the SHA-256 of each file's hex digest and a newline, in path order: `create_run`'s
  `plan_sha256` and a pack's `facts/plan.json` bind the bytes, the dialect and the counts through it.
  Of a side that declares `partitions`, whose directories' names are compared
  values, it is the SHA-256 of each file's `<hex digest>  <relative path>` and a newline (`sha256sum`'s
  own lines, whole), so the same bytes under another directory are another export, with another digest.
  It is never a file's own SHA-256, of a side of one file neither: wherever a pack shows it, the pack says
  what it is the SHA-256 of (§11, §16).
- **The pins are sealed.** `tables.planning` hands them to `create_run` and `compare_one`, which store them
  as `params.pinned = {"a": {"enc": …}}`, sealed by the run's sealer as `scan.seal_after` seals a scan
  point. `approvals.plan` leaves `pinned` out beside `approval`, so no pack, `plan_sha256` or audit row
  holds a per-file size, time or digest; `deps.shown` leaves out `pinned` and each side's
  `files_sha256`; `repo.list_runs` (the list route, `ds runs`) drops `pinned` from each row it read (about
  200 bytes a file). The worker opens them with the run's data key; they are unreadable once it goes.
- `server` is DuckDB's major and minor (ENGINES D10): an upgrade does not fail a live run.

**Cost.** Run creation reads each file once to hash it and once more to count it (a Parquet file's key
columns only; a CSV file of a side that declares a quote a third time, for its end check), inside `POST /v1/runs`, `POST /v1/compare`, `ds run` and a firing, on the files threads,
for at most `files.plan_s` a run's creation, whatever its file sides: P30 stops it, and the next try goes on
from the cached hashes (step 4). DEPLOY sets the proxy's read timeout above `files.plan_s`, which covers a
file-to-file pair too, and publishes §17's rate. At most `duck.PLANS = 8` creations plan file sides
at once in a process (half the pool's threads: plans alone never fill it); one more waits its turn inside
its own `files.plan_s`, and past it is not ready (P34's second text).

## 7. CSV

```
read_csv($files, auto_detect=false, columns={'customer_id': 'VARCHAR', ...}, header=$h, skip=$s, delim=$d,
  new_line=$l, quote=$q, escape=$e, encoding=$n, compression=$c, nullstr=$z, allow_quoted_nulls=false,
  null_padding=false, union_by_name=false, filename=false, max_line_size=2097152, ignore_errors=true,
  parallel=false)
```

As built after U14 and U13: `strict_mode` is left to DuckDB's default, which is strict (the same rejects
as an explicit `true` for `\n` files), because an explicit `strict_mode=true` with `new_line='\r\n'` reads
no row and records no reject; `strict_mode=false` is never used (it reads `"a"b` as `a` and drops a `\r`).
`$l` is the escaped text `'\n'` or `'\r\n'`. Every statement reads every declared column: one it does not
otherwise read is named in its filter as `("c" IS NULL OR "c" IS NOT NULL)`, since invalid encoding is
found only in a field a statement reads. By U20 and U21 the reader is `parallel=false`
(bound, as every option is), because DuckDB's parallel reader takes a line of a quoted field for a row where
one of its scans starts inside the field, and reads several files one thread a file anyway; a file's rows
are parsed on one thread (the sort and the hashes use `files.threads`). The reader drops a last record cut
inside a quoted field without a reject, which §6 step 6's end check counts.

In both readers `hive_partitioning` is on only with `partitions`, its `hive_types` all `VARCHAR`. DuckDB
then takes every `name=value` directory of a file's path for a partition, the root's and the fixed part's
too, and the directory's value stands for a file column of that name: so the plan refuses one above the
declared partitions (P26; §6 step 2). A file's own name holding `=` is no
partition (observed).

- **Nulls and empties.** An unquoted field equal to `null` is NULL; quoted, it is the string: `a||c`
  holds NULL and `a|""|c` the empty string (PostgreSQL's COPY convention). Against a side holding `''`
  the difference is reported until the spec declares `emptyAsNull`.
- **Types.** Every column is class `text` (`char` with `padded`). Against another family P9 refuses the
  run and names the `typeMap` entry (§6's amendment: also against PostgreSQL and Parquet). A text that
  does not convert is `unconvertible` for its key, naming column and type, never the value. As text
  `00123` differs from `123`; `{as: integer}` reads both as 123, declared and approved.
- **Encoding and line ends.** A UTF-8 byte-order mark is no part of the first field. Windows-1252 is
  not latin-1 (0x80 to 0x9F differ): convert the export. A utf-16 file holding a character beyond the
  BMP is refused whole (U8: P29): convert it to UTF-8. Line ends are declared (`csv.newline`), never
  detected.
- **Lines that are not rows** (a wrong field count, an unterminated quote, invalid encoding, a record
  over 2 MiB, a header or trailer record of another shape, a file cut inside a row). Run-time
  statements skip them, never more than the plan counted (U13). A key with no row on a side whose plan
  has `rejected > 0` is recorded by `TableConnector._observed` as `error_class="not_rows"` (M1), which
  `observe._fetch_failure` maps to `SideState.incomplete("unparseable", <M1>)` with no `typeMap:`
  prefix: INCONCLUSIVE.
- **Rows guard** (`scan.scan`, `census._count`; sides whose plan has `selected`). A read of a side to
  its end must have met exactly the plan's `selected` rows, those without a key among them, else
  `RowsMoved` fails the run (F10): a parser that disagrees with itself is a failed run, never a wrong
  answer. This holds in a key census too: `census.record` raises it, where every
  other failure of a count is recorded, and the worker fails the run.
- **Filters.** A CSV column compares as text in binary order; a number or boolean literal is refused
  (P11). Parquet filters compare typed columns: literals are checked by class (ENGINES P11) and bound.

## 8. Parquet

| Class | Type DuckDB reports | Rendered |
|---|---|---|
| `integer` | `TINYINT` to `BIGINT`, unsigned, `HUGEINT` | `CAST(c AS VARCHAR)` |
| `decimal` | `DECIMAL(p ≤ 38, s)` | `CAST(c AS VARCHAR)`, scale kept |
| `float`, `float32` | `DOUBLE`, `FLOAT` | through `DOUBLE` as text, reparsed, narrowed for 32 bits |
| `text` | `VARCHAR`, `ENUM`, `UUID` | the string |
| `boolean`, `date`, `time` | as named | text; a time's trailing zeros dropped |
| `timestamp` | unzoned timestamps of any unit; INT96 once declared (below) | text, up to 9 digits, trailing zeros dropped |
| `instant` | `TIMESTAMP WITH TIME ZONE` whose footer unit is milli- or microseconds | `epoch_us(c)`, built in Python as `…+00:00`. As built: `replace(CAST(make_timestamp(epoch_us(c)) AS VARCHAR), ' ', 'T')`, core functions, no ICU, `render` adding `+00:00` |
| `bytes` | `BLOB` | the bytes; `binaryAsString` reads them as `text` |
| `json` | `JSON` | text, parsed exactly |
| `other` | `INTERVAL`, `TIME WITH TIME ZONE` | text; never crosses engines (P8) |
| refused (P23) | `LIST`, `STRUCT`, `MAP`, `ARRAY`, `UNION`, `VARIANT`, `GEOMETRY`, `BIT`, any other; a `DECIMAL` over 38 digits (DuckDB hands it over as `DOUBLE`) and a UTC-adjusted nanosecond timestamp (DuckDB reads it to the microsecond), both found in the footer (U9, U15); also an INT96 timestamp and a nanosecond time, which DuckDB reads to the microsecond too, each until the approved spec declares that reading (below); a `DECIMAL` whose scale is over its precision, which only a crafted footer declares (below) | — |

As built a column's class is read from the type `DuckDB` reads file 1's column as (its
statement's description: `TIMESTAMP_MS` comes as `TIMESTAMP`), and its footer (`parquet_schema`, its
top-level columns by walking their children) gives the decimal's precision and the timestamp's unit and
UTC flag; every footer of the files must hold each planned column with file 1's footer type (P22, with its
time unit and INT96 told apart: `TIMESTAMP in nanoseconds`, `TIMESTAMP as
INT96`, so a later file's nanoseconds are not cut to file 1's microseconds unseen). A
partition's column is declared `a partition directory`, class `text`; a CSV column `a CSV field`, or `a
CSV field as integer`, `as decimal(38,s)`, `as date` by `csv.keys`, which P9's and P6's file texts name.

The reader is `read_parquet($files, union_by_name=false, filename=false, binary_as_string=$b)`. A refused
type is refused wherever the plan would read it, discovered columns included; list `columns` to leave it
out. A nanosecond instant is read once the approved spec declares `typeMap {col: {as: timestamp}}` (six
digits kept, ENGINES §9's bridge). So is an INT96 timestamp, which holds the
nanoseconds of its day and which DuckDB reads to the microsecond (the legacy timestamp of Impala, Hive and
Spark: such a column needs the declaration, or is left out); a nanosecond time is read once declared `{as:
string}`, its text to the microsecond. Undeclared, each is P23: two values a nanosecond apart rendered
alike. Encrypted Parquet is P22. Canonical rendering 1 gains this column in ENGINES §9; its version does
not change.

By U30:
- **A `time` is written from its microseconds**, by no conversion of DuckDB's: `printf('%02d:%02d:%02d',
  u // 3600000000, u // 60000000 % 60, u // 1000000 % 60)` for `u = epoch_us(c)` and, where `u % 1000000`
  is not 0, a point and its six digits without trailing zeros. For every time of day that is DuckDB's own
  text, `24:00:00` included (asserted on 51,000 values). A file's page may hold a `TIME` outside a day,
  and DuckDB's own text of one (`CAST(c AS VARCHAR)`) ends the process or is an internal error; a
  guard before the cast is no remedy, since DuckDB removes it where the file's statistics say that
  no value lies outside. Such a value gives a text that is no time of day (`320255973:30:06.846976`),
  which the worker reads as it reads any value that is not of its column's class: `column 't' does not
  read as time`, naming the column and never the value.
- **A `DECIMAL` whose scale is over its precision is refused wherever a plan reads it** (P23's fourth
  text: `column 'k' (DECIMAL(12,30)) cannot be compared: its type is not read; …`). DuckDB reads such a
  footer as `DECIMAL(12,30)` and writes its values without an integer digit (`.000…150`), which is no
  key text (§9): a key the scan read would be answered 404 by its own read. A column's class is `decimal`
  only when DuckDB reports exactly `DECIMAL(p,s)` with s ≤ p; a list of decimals (`DECIMAL(12,2)[]`)
  is the nested column it is.

## 9. Keys, order, statements

**Key text** (ENGINES §8); a NULL text is counted in `skipped_x`:

| Class | Parquet | CSV, by `csv.keys` |
|---|---|---|
| `text` | the value | default: the field's text |
| `integer` | `CAST(k AS VARCHAR)` | `integer`: a text of `-?[0-9]{1,38}` through `DECIMAL(38,0)`, else NULL |
| `decimal` | `CAST(k AS VARCHAR)`; of a `DECIMAL(p,p)`, with its integer digit (`0.5` where DuckDB writes `.5`) | `decimal`, `scale` s: at most s fraction digits, through `DECIMAL(38,s)`, never rounded, else NULL |
| `date` | `CAST(k AS VARCHAR)`; a date outside the years 0001 to 9999 refuses the plan (P6) | `date`: ISO text, else NULL; the year 0000 refuses the plan (P6) |
| `bytes` | `'\x' \|\| lower(hex(k))` | — |

Every other class is refused as a key (P6); ENGINES' one-scale rule applies to the declared class.

- **Order:** `ORDER BY encode(<key text>)` per part, under ENGINES' `KeysOutOfOrder` guard, on which
  soundness rests. **Resume bound:** `(o1 > $a0) OR (o1 = $a0 AND o2 > $a1)`, bound as bytes.
- **Key maps:** `identity` and `prefix_pad` (digits test `regexp_full_match(k, '[0-9]+')`).
- **Duplicates.** A file guarantees no uniqueness. The merge already makes a repeated key a candidate
  and its per-key read answers `identity_ambiguous`. New: `repeated_x`, the keys a side holds in more
  than one row, counted by `scan.scan` and `census._count` for `files` sides only (`new_scan()` and
  PostgreSQL's records stay as they are, ENGINES D3). Nothing is de-duplicated.
- **Statements** are fixed text, identifiers and bound values. Identifiers are `csv.columns` names or a
  footer's, double-quoted with quotes doubled, control characters refused. `extract_statements` finds
  one `SELECT` in each. No `ATTACH`, `COPY`, `EXPORT`, `INSTALL`, `LOAD`, `PRAGMA`, `CREATE SECRET` or
  `SET` is composed once the instance is locked.
- **Per-key read:** one statement a group and side, one pass over the files, not one arm a key (golden
  text in `tests/golden/engines/files_pair.json`):

```sql
SELECT a.i, <rendered columns>, <oversize>, <key texts>, (<where>) IS TRUE
FROM <reader> AS t JOIN (VALUES (0, $v2), (1, $v3)) AS a(i, k0) ON encode(<key text 0>) = a.k0
QUALIFY row_number() OVER (PARTITION BY a.i ORDER BY (<where>) IS TRUE DESC) <= 2
ORDER BY a.i, <its position> DESC
```

  The outer `ORDER BY` is binding: `_observed` reads each key's first row as its selected one, as
  PostgreSQL's `ORDER BY 3 DESC LIMIT 2` gives it. The select item is `(<where>) IS TRUE`, unaliased, the
  window orders by that expression and the outer query by the item's position (U19: a name `s` there
  would be a column `s` of the files where one exists).
  The keys' parts are bound as bytes and the join is `ON encode(<key text 0>) = a.k0`, an expression,
  since of a join on a bare column DuckDB derives a filter for the files' reader, which prunes row groups
  by their statistics as an `IN` does. DuckDB prunes a text column's row groups by the deprecated `min`
  and `max`, which writers before parquet-mr 1.10 computed in signed-byte order: a key the scan had read
  would be answered 404 (observed on a hand-built file whose statistics are `min é`, `max z` for a group
  holding `a`). **No typed `IN` is bound**, and the statement holds no `WHERE`.
  DuckDB prunes an integer, a date and a decimal column's row groups by their statistics as it prunes
  a text one's, and statistics need not hold their group's values (U30): under a minimum and a maximum
  of 100 and 200 for a group that holds 5, and under the empty statistics of a decimal stored as bytes,
  a key a scan had read is on no row of a read that binds one. A per-key read of a Parquet side is
  therefore a pass over the compared columns of every row group, whatever the key's type and the file's
  order (a group of 200 keys of a 10M-row file took 0.3 s with a typed `IN` and takes 0.3 s without it:
  §17). **A spec's filter is the one predicate on a column that reaches the reader**, in the count, the scan
  and the per-key read alike: on a column whose statistics do not hold its values, a row of a row group
  those statistics exclude is taken as not selected by all three (observed: a row of 5 under
  statistics of 100 to 200 and `where: id < 50` is counted as not selected, not scanned, and read by
  its key as not selected). Statistics that hold their values, which every writer's are meant to,
  change no answer.
- **No key is unfit here.** The join compares texts, and a key part that is not of its class's form or
  not in its Parquet type's range is on no row: 404 without a statement; a lone surrogate is
  `KeyRejectedError` without one. No typed `IN` is bound, so no batch
  is ever halved (each half would be a pass). As built: `keys.side_values` rejects a key with no UTF-8 form
  on a file side, so the connector reads it alone and rejects it without a statement; the form of a part is
  the canonical key text's (`0|-?[1-9][0-9]*` for an integer, the decimal's scale exactly).
- **Scan, census, frame, null keys, probe:** ENGINES §10's builders over DuckDB primitives. In a
  file-to-file pair a column is an engine column when both sides have one type, one class and one render
  expression for it (a `padded` CSV column against an unpadded one is a worker column) and no `typeMap`
  names it; `row_hash` is `unhex(sha256(concat(coalesce(sha256(x1), '-'), ...)))`, which keeps
  boundaries and NULLs distinct. Across engines every compared column is a worker column. As built:
  `plan._worker_columns` takes two file sides as it takes two MySQL sides, a column both sides
  declare alike (one declared type and one class, which give one render expression: a CSV field against a
  Parquet column is never alike) and no `typeMap` names; `row_hash` hashes bytes as their hexadecimal
  digits and a `padded` field without its padding (their key texts), so equal hashes are equal canonical
  values and trailing spaces alone make no candidate. A value an engine column renders in two texts of one
  canonical value (a JSON document spaced differently) is a candidate its per-key read then matches.

## 10. The reader (`duck.py`, `manifest.py`)

- **Threads.** Every file-system and DuckDB call (listing, `statvfs`, stat checks, hashing, statements,
  fetches) runs on `manifest.POOL`, 16 daemon threads of the engine's own (a pool of the module's: the
  standard executor's threads are joined at exit). As built a file's hash is one call of its own, which
  opens, reads, `fstat`s and closes in one thread, reads the cancel flag at each MiB and keeps the time of
  its last read, which the caller watches against `STAT_S`: never on the event loop,
  which `ds serve` shares between API and worker, and never on asyncio's default executor, which every
  run's judging and evidence writes use. Each call has a deadline: `STAT_S = 30` for listing, `statvfs`
  and stat checks; a statement's timeout or watch (below); for hashing its caller's budget, the cancel
  flag read each MiB. A caller that is cancelled (a firing's window, a stop; not a closed request: below)
  sets the flag and calls `interrupt()` on its way out, so nothing goes on hashing for nobody.
  - **A deadline starts when its call runs.** The pool's thread stamps the call's start, and `STAT_S`,
    a statement's timeout and a hash's time between reads count from there (`manifest.call`,
    `Cursor._call`): counted from the call's submission, sixteen long calls whose every read answers
    (sixteen runs created on large CSV exports at once) would make a healthy mount look dead.
  - **A busy pool is not a dead mount.** A call no thread took within `STAT_S` of its submission is
    withdrawn (cancelled in the queue: it never runs) and is `manifest.Busy`, a `TargetUnreachable` of
    its own: not counted as abandoned, no instance discarded. At run time it is an outage as any (a
    per-key read is `connect_error` with detail `Busy` and read once more; a scan pass pauses and
    resumes); a stat check of a watched statement that is `Busy` is skipped and made again `WATCH_S`
    later; while a run is planned it is P34 (not ready, code `planning`: a firing tries again). A queued
    call whose caller left (cancelled, or past `files.plan_s`) is withdrawn too.
  - **Few creations plan at once.** `duck.creating`, which `tables.planning` enters for a spec with a file
    side, admits `PLANS = 8` at a time a process, each holding one thread at a time, so plans alone leave
    half the pool to stat checks, per-key statements and the one sort (§6 "Cost").
  - **A closed request does not stop a plan.** The API does not watch for a client's disconnect: when the
    caller (or a proxy's timeout) closes `POST /v1/runs` while its files are planned, the plan goes on to
    its end inside `files.plan_s` and the run is created and started. Look at the runs before sending
    the request again (DEPLOY). A firing's window and a stop do cancel a plan, as written above.
- **A call past its deadline** is `TargetUnreachable` (P14 at plan). A DuckDB call gets `interrupt()` and
  10 s more. A thread that still has not returned (a read hung in the kernel) is **abandoned**, never
  joined: it keeps its pool thread, is counted, and its instance is dropped unclosed and built anew.
  With `STUCK_MAX = 4` abandoned, file work is refused at once (P28 at plan; `TargetUnreachable` at run
  time, paused and resumed as any outage) until one returns or the process restarts; each is logged.
  Daemon threads never hold the process at exit. The abandoned calls are counted
  by the root of the target they read (`manifest.stuck(root)`), and `STUCK_MAX` of one root refuse that
  target's file work alone, before anything is handed to a thread (a plan, a firing, a resumed scan and a
  per-key read spend no thread more on it). **What another files target meets:** nothing, while fewer than
  `STUCK_ALL = 8` abandoned calls wait in all: its runs are created, scanned and read. With two mounts
  dead (four each), or abandoned calls of the spill and of T10's resolving among them, every target's
  file work is refused (P28's third text in its second form), which keeps half the pool's threads. A
  restart frees the threads; the dead target's running runs then abandon four more within a few lease
  periods and stand refused again, the other targets working on: cancel or pause that target's runs and
  schedules to stop the log lines (RUNBOOK).
- **Instance** (lazy: an installation without a files target never imports `duckdb`).
  `duckdb.connect(":memory:", config=…)` with `threads`, `memory_limit`, `max_temp_directory_size`,
  `temp_directory`; `home_directory`, `extension_directory` and `secret_directory` inside the spill
  directory; `autoinstall_known_extensions`, `autoload_known_extensions`, `allow_community_extensions`,
  `allow_unsigned_extensions`, `allow_persistent_secrets` and `python_enable_replacements` false;
  `default_collation` empty. Then `SET allowed_directories` to every files target's root,
  `SET enable_external_access = false`, `SET lock_configuration = true`. As built (U2 to U4): the
  process's spill directory holds `temp/` (the `temp_directory`, which DuckDB allows by itself) and
  `home/`, `extensions/` and `secrets/` beside it, outside every allowed directory, which is what refuses
  `INSTALL`; `max_temp_directory_size` is set by a statement after the connect (in the configuration it is
  not enforced); `duckdb_extensions()` is read before the lock (after it, it needs the extension
  directory); a root is handed over with a trailing `/`. By U28, before the lock,
  `enable_external_file_cache`, `parquet_metadata_cache` and
  `enable_http_metadata_cache` are set false by statements (`duck.CACHES`), so no statement reads a
  file by a footer DuckDB kept of another file at its path. They are
  set after the extensions were read and judged (still before `allowed_directories` and the lock):
  `parquet_metadata_cache` is the Parquet reader's own setting, which a build without that reader does
  not know, so its `SET` would raise DuckDB's `CatalogException` out of a plan where P27 is due (shown by
  simulation, with an extension the wheel lacks and a setting of its own: the locked wheel links the
  reader).
- **It proves itself before first use:** the three settings read back (and the
  three caches' as false: one that does not is P27, logged `… enable_external_file_cache did not read
  back as set`), `read_blob` of the targets file
  raises `PermissionException`, and U2 holds (else P27); the spill directory is writable and its file
  system has `files.spill` plus `SPILL_RESERVE` (1 GiB) free (else P28). A fatal error discards it. As
  built: `duck.instance(settings, roots)` builds and proves it on `manifest.POOL` and raises
  `duck.Unstarted`, which `duck.unstarted` words as P27 or P28 for a side or a target; an
  `InternalException` discards it too (U16), as does a call abandoned past its grace; a new root list or
  budget builds another (one a process in service, many in tests).
  - **One build.** `duck.instance` holds a lock of its loop across the look, the build and the store, so
    tasks that reach the reader before the first instance is stored (two schedules due together, each
    lane and a resumed scan after a restart, an API plan beside the worker) wait for one build.
  - **A discarded instance makes no cursor more** (D14 says
    what a discard costs). `duck.discard` marks it (`Instance.dropped`): a session that asks it for a
    cursor is `Busy('Dropped')`, and the next one opens on the instance built anew. **A session that
    holds one of its cursors finishes on it:** a scan pass, a census, a frame and a plan go on to their
    end, so one target's hung statement costs another target's pass in flight nothing
    (`test_a_scan_in_flight_survives_another_target_s_hung_statement`). A per-key reader, whose read
    needs nothing of an earlier statement, closes such a cursor before its next read and opens on the
    new instance (`_Session.renewed`), so the discarded one's budget ends with the passes and plans in
    flight, not with the runs that read by key. When its last cursor is closed and the last of its
    calls on a thread has returned (`Instance.held` counts both), its spill directory is removed and
    its lock freed, by a call of the pool (`manifest.release`); a statement hung in the kernel keeps
    both. Where an internal or fatal error discarded it, DuckDB has invalidated its database: a result
    already open is still fetched (observed), and a statement one of its cursors sends afterwards
    fails in DuckDB and is `Busy('Dropped')`, an outage, after which the session's owner opens a
    session on the new instance (a scan at its next pass; a plan under way is P34).
  - **Free space, with this installation's own spill.** The start's check counts as free what the spill
    directories under `files.spill_dir` hold on disk (`manifest.spilled`: after the start's sweep, the
    live sorts of this installation's other processes and of a discarded instance): they spill within
    the same budget, so a second `ds` process, a `ds` command and an instance built anew are not refused
    (P28) while the worker's sort uses the volume DEPLOY sizes. An instance that still finds the volume short
    while a run reads is `TargetTempSpace('spill')`, as before a sort: a per-key read waits for it. A discarded
    instance's directory stays, and stays counted, while a session's cursor holds a sort of it, between
    two fetches of a scan too.
  - **P27's log.** Each check of the guard that does not hold is logged at error level by its name
    before the instance is refused, with no text of DuckDB's: `the file reader did not start: this
    build's DuckDB lacks parquet, statically linked`, `… enable_external_access, lock_configuration did
    not read back as set`, `… a read outside the declared roots was not refused`.
- **Sessions.** A cursor with an `asyncio.Lock`; `snapshot()` is `connect()` with §11's checks. As built
  (`duck.Cursor`): it runs a statement only when `extract_statements` finds it one `SELECT` (U3: DuckDB
  runs `ATTACH`, `COPY TO` and `EXPORT` into an allowed directory), else `TargetRefused('not one
  SELECT')`; `execute`, `fetch`, `interrupt` and `close`, each on `manifest.POOL` under its timeout, past
  which it is interrupted and given 10 s more (`GRACE_S`); `close` raises nothing when the pool takes no
  work (`STUCK_MAX`) or the close misses its deadline: a connector closes its session on the way out of an
  outage, and a raise there would make the outage a failed run. A batch
  closes at 10,000 rows or 64 MiB, fetched 64 rows a step when a text, bytes or JSON worker column is
  read; a row over 16 MiB of them is ENGINES' `oversize`. As built: a batch is one call of the
  pool that fetches and shapes (digests, documents) in its thread; the oversize flag covers bytes and JSON
  columns only (a `BLOB`'s octets, a `JSON`'s text bytes): `compose` refuses a strata column that may be
  large (P10), and a frame's strata are text, whose CSV line holds 2 MiB at most.
- **One sort at a time.** A scan pass (both file sides of its pair), a census and a frame hold
  `duck.SORT`, one lock a process, for their length; a pass that waits holds no cursor and logs once,
  its run still `running`. Before a sort the free-space check is made again for the smaller of its
  `sort_bytes` and `files.spill` (else `TargetTempSpace`, before a byte is read; `sort_bytes` as §6 step
  8 projects it, the figure P32 judged). Per-key statements and
  plan passes do not sort and run beside it, at most `max_concurrency` a target. As built:
  `duck.SORT` is taken by `Files.snapshot`, which a scan pass, a census and a frame open, once a task (a
  pass's two file sides share it), an `asyncio.Lock` a loop; the engine does not know the run, so its log
  is `a sort of a file side waits for another`. A scan pass with a file side
  takes `duck.SORT` itself, before either side's snapshot opens (`scan.scan`; `Files.snapshot` then passes
  through), so a database side has no session while the pass waits: its snapshot would sit idle in a
  transaction for as long as another run's sort lasts, and a server that ends such sessions would fail the
  run. `first_at`, which the census prints as a snapshot's time, is stamped when the snapshots opened,
  not after the file side's own sort returned. The pass therefore holds the sort while its database side
  connects and its catalogue is read, and a database side's snapshot is open while the file side sorts,
  as one database's is while the other sorts (ENGINES §10).
- **Beside the worker.** `files.threads` are DuckDB's own, beside `worker.concurrency`'s lanes; the
  worker's canonicalising and hashing of a cross-engine scan shares the interpreter lock with the API
  under `ds serve` (ENGINES §10): run `ds serve --no-worker` beside `ds worker` for large file scans.
- **Timeouts.** Per-key statements `execution.total_timeout_s`; census and frame 3,600 s; a side's plan
  `files.plan_s` in all; scans none. No single read of a hash may take longer than `STAT_S`. A statement
  running over 60 s is watched: every 60 s the manifest is stat-checked under `STAT_S` (which also sees a
  changed file early), and a check that misses its deadline interrupts and abandons the statement, so a
  dead mount never keeps `duck.SORT`. So is `null_keys`, the one more pass over
  the files that a scan, a census and a frame make while they hold `duck.SORT` (a `COUNT(*)` of the rows
  without a key, after the closing hash): its statement and its fetch are watched, and its files are
  stat-checked before and after it, as every read's. A side's plan has what its run's creation has left of
  `files.plan_s` (§6).
- **Spill.** `<spill_dir>/<26-character id>/`, mode 0700, held by `flock` for the process's life. As
  built it is made as `.<id>`, locked, then renamed, so no sweep can remove it in between, and the sweep
  passes over this process's own (a second descriptor's lock is refused, also in one process).
  `manifest.sweep` of the settings' spill directory (no DuckDB: it removes sibling directories whose lock
  is free) runs in
  `Context.open` of every `ds` command, in the worker's hourly purge pass and when an instance starts.
  The spill lies outside `blobs/`, so `EvidenceStore` and the purge's blob pass never see it. The
  sweep is best effort an entry: a directory it cannot open or remove (left by another
  uid: a `ds` command run once as root) is logged by its position, `spill directory 2 of 5 not swept:
  PermissionError`, and passed over, and it never decides whether the reader starts (P28's
  `files.spill_dir is not a directory DeltaSleuth may write` is said of the instance's own directory
  alone). A directory that went between the sweep's listing and its look (its reader
  ended, another process swept it) is not logged. `ds target
  check` sweeps as every command does. A discarded instance's directory is given up as said above, not
  left to the process's end.
- **Sizes (projections; §17 has the measurements).** The projection is the sort's as measured (§6 step 8,
  U27): about 2.5 GiB for 10M rows of a 17-digit key and three short columns across engines (their sort
  spilled 1 GiB), near 100 GiB for 100M rows of 24 short columns (refused by P32 under the default
  `spill`), and for a file-to-file pair three times the key's text, 144 bytes
  and 15% a row. It is an upper estimate: texts DuckDB compresses in its temporary files, and values of
  12 bytes or fewer, which it keeps in their 16-byte slot, spill a third to a half of it.
  `memory_limit` bounds DuckDB's buffers, not every allocation: DEPLOY sizes the container at
  `files.memory` plus 1 GiB.
- **Errors.** No DuckDB text leaves the module (it quotes paths and values): each becomes ENGINES'
  `TableError(code)` raised `from None`, the code an exception's class name or this contract's text. As
  built, `duck._error` maps by class alone; an `IOException` is `TargetRefused` there (or
  `TargetTempSpace` with the spill under 64 MiB free), and the Parquet and CSV readers stat-check the
  side's pins before they judge one, as the table says. The spill limit is met as
  `OutOfMemoryException` (observed: `failed to offload data block`), so `TargetTempSpace` either way.

| Cause | Class | Per key | Scan | Census; frame |
|---|---|---|---|---|
| the root cannot be listed, a stat check raises `OSError` (as built: `manifest.verify` maps an `OSError` of its listing, a stale handle or EIO, to `TargetUnreachable('OSError')`; while a run is planned it is P14's first text), a deadline is missed, `STUCK_MAX` abandoned reads of the target's root wait, or `STUCK_ALL` of all (`Stuck`). Also: no thread took the call within `STAT_S`, or its instance was discarded as it asked for a cursor, or invalidated by another statement's internal error (`Busy`: code `Busy` or `Dropped`); the closing hash of a file that did not change is answered with an error (EIO, a stale handle: code `OSError`; a missing file, a link or other content are `FilesChanged` and fail the run); after the plan the root holds no entry at all (code `Empty`: a mount point whose mount is not there, as when the service started before the export was mounted; a root that holds other entries has changed, and while a run is planned it stays P16) | `TargetUnreachable` | `connect_error`, re-read once | paused (F1), resumed without limit | `{failed}`; postponed |
| `IOException` or `OSError` from a statement: the manifest is stat-checked at once. Changed: `FilesChanged`. Not listable: the row above. Unchanged (a damaged page or compressed stream, a bad block) | `TargetRefused` | `connect_error` naming the class | a stalled pass, checkpoint or not: the same bytes fail the same way, so the third fails the run (F4). As built: the session marks the error (`TargetRefused.fixed`) once its stat check found the files unchanged, and `worker._stalled` counts such a pass whether or not it saved a checkpoint; three in a row fail the run | `{failed}`; F7 |
| `InterruptException` after DeltaSleuth's interrupt | `StatementTimeout` | `timeout`, not re-read | — | `{failed}` |
| `OutOfMemoryException`; `IOException` with the spill at its limit or under 64 MiB free; the check before a sort; an instance built while a run reads that finds the spill volume short of `files.spill` plus 1 GiB | `TargetTempSpace` | `connect_error` (it met a sort's budget): retried by the spec's retries, never fails the run | fails the run (F3) | `{failed}`; fails the run |
| `PermissionError` (a closing hash's too), `PermissionException` | `TargetDenied` | fails the run | fails the run (F4) | fails the run |
| the files are not the plan's | `FilesChanged` | fails the run | fails the run (F9) | fails the run (as built: the census records `{failed: FilesChanged}`, as it records every failure, and the next per-key read's stat check fails the run) |
| any other `duckdb.Error` | `TargetRefused` | `connect_error` naming the class | ENGINES' stalled-pass rule | `{failed}`; F7 |
| (READER.md) the reader's process ended under the statement, or before it was sent (code `Died`); another task's reader apart did not end within `STAT_S` (`Alone`); the reader's process did not start while a run reads (`Unstarted`) | `Busy`, a `TargetUnreachable` | `connect_error` with the code, the group read once more | paused (F1), resumed | `{failed}`; postponed |
| (READER.md) the third end of a reader's process while one side's files alone were being read | `ReaderDied` | fails the run (F12) | fails the run (F12) | `{failed: ReaderDied}`, and the next per-key read fails the run; a draw fails the run |

**By READER.md: the reader in a process of its own.** What the paragraphs above say of the instance,
its threads, its sessions and its spill stands with these changes (READER §5 to §10 are binding):

- **Threads.** File-system calls run on `manifest.POOL` as before. A DuckDB call is a pool job that
  writes a request to the reader's process, waits for its answer and shapes it (digests, documents);
  its deadline counts from when a thread took it, and `Busy` after `STAT_S` without a thread stands.
  Past its timeout a statement is interrupted and given `GRACE_S`; past that the parent answers the
  waiting job itself (`TargetUnreachable('Deadline')`), so **no pool thread stays with a hung
  statement**, closes the statement's cursor and retires its reader. The read is counted for its
  target's root (`STUCK_MAX`, `STUCK_ALL`) until it answers or its process ends. A watched statement's
  reader is also asked for a sign of life at every watch (`ping`): none within `STAT_S`, and the
  process is killed, so a frozen reader keeps `duck.SORT` no longer than a dead mount does. The
  reader makes an interrupt stick, also one that reaches it before
  DuckDB began the statement, and a kill is of the reader's process group.
- **Instance.** No `ds` process imports `duckdb`. `duck.Instance` is the handle of a child process:
  this interpreter run on `reader.py` by its path (`python -I -X utf8`), started by `subprocess.Popen`
  with an empty environment, its spill directory to work in, a session of its own and its error output
  discarded (DuckDB's text quotes paths and values). The connection, its configuration, the caches, the
  lock and the guard's proof are the child's (`reader.connect`, `reader.prove`: the statements above,
  moved), as is the rule of one `SELECT` a call (`reader.select`); the spill directory, its lock and the
  free-space check are the parent's. A start that fails (the process cannot be started, ends first,
  does not answer within 20 s, answers outside the protocol, or is not ready within 50 s with its file
  work) is P27's second text, logged by its cause, and leaves no process and no directory. Before it
  loads DuckDB the child restricts itself (READER §7): no core on every platform; on Linux the kernel's
  memory killer takes it first, it is not dumpable, and, unless `files.os_limits` is false, it sets
  itself a data limit of `files.memory` plus 2 GiB plus 600 MiB a thread (5,296 MiB by default), which
  it proves, and loads a filter that denies it sockets. Its first answer reports what that start saw
  hold, and the start's log line (a warning, so that every `ds` process prints it), `ds target check`
  and the self-test print exactly that.
- **A retired reader** (a statement of it was given up, a fatal error, or it is the process's reader
  no more because the roots or the budget changed) makes no cursor more; a session that holds one
  finishes on it; with its last cursor, at once when it has none, its process is killed and its spill
  directory removed, by the thread that waits for its process, at the process's end (a
  reader's end is its process's, not the end of its output, which a process it started
  may hold open; READER §5). `Instance.held` counts its open cursors alone, and `discard` starts no
  pool job. A `ds` process that exits kills and reaps the readers it still has.
- **An end is accounted** (READER §9). A reader's end that this process did not cause, and a kill for a
  deadline, is charged to the one file set that was in flight, or makes the sets in flight together
  suspect; a charged or suspect set is read apart next, in a reader of its task's own, and a set
  charged three ends is read no more until `ds` restarts (`ReaderDied`: F12, P35). The key of a set is
  the SHA-256 of its side's pinned files and pins, kept in memory only.
- **Sessions.** `duck.Cursor` is a number its reader knows a cursor by. `execute`, `fetch` and `columns`
  are requests (READER §6); rows cross by column in a frame of at most 128 MiB (a larger batch in
  several frames; a row that alone passes one is `Oversize`), and only None, booleans, integers, text
  and bytes cross (`Unframed` for any other value: no statement DeltaSleuth composes selects one). Every
  answer is checked, and one outside the protocol ends the reader (cause `reply`). By READER §6 a batch holds
  2^20 cells (rows times columns) at most, so a statement of over 104
  columns crosses in batches of fewer than 10,000 rows, and an answer that names more is no answer; a
  statement's rows are of one width; the rows a plan keeps or words (its counts, the rejected lines,
  the footers' schema) cross under the tags their statement must answer; and a column's type is kept
  by a grammar of the parent's, a nested or unread one by its head alone (`LIST`, `STRUCT`, `ENUM`).
  `close` is two frames, an interrupt and a close, with no pool call and no wait: it fails no read, and
  a statement still unanswered `GRACE_S` after its cursor's close is given up as above. After a batch
  of 10,000 rows or more (10,048 where a text, bytes or JSON column is read, 64 rows a step) of a scan,
  a census or a frame the reader reads the next batch ahead while the worker shapes the one it has
  (READER R9), except in a reader of a task's own.
- **Spill.** The parent makes and flocks the directory as before, one a reader, and the child works
  in it. When a reader's process ends, its directory is removed once the process is reaped, before the
  next start counts free space. When the parent ends, SIGKILL included, the kernel frees the lock, the
  child exits at the end of its input, and the next sweep removes the directory.
- **Errors.** The child answers a failure by its class's name (one of `reader.NAMES`; `Error` for any
  other) and a kind, by the class raised and never its text; `duck._error` maps the kind as the table
  maps the class. `MemoryError` (an allocation of the child's own under the data limit) is
  `TargetTempSpace`, like `OutOfMemoryException`; `Oversize` and `Unframed` are `TargetRefused`. A fatal
  or internal error discards the reader as it discarded the instance.

## 11. Change, consistency and the claim

**Observed**, for a file side, means the bytes of these files as DeltaSleuth read them between
`pinned_at` and `verified_at`, named by digest: never the system they were exported from.
`manifest.verify(target, path, side, files, pins, full=False)` expands again and compares the count and
each position's name, type, size, modification time, change time and inode with the pins; `full=True`
also hashes, uncached. It compares files: no directory above them is pinned (§14 point 4).

| Reader | Before | After |
|---|---|---|
| Plan (first records, footers, count, flood guard) | the pin (§6 step 4) | stat after its last statement (step 9) |
| Scan pass | stat | stat at each checkpoint; full when the pass's context exits (as built: stat after each fetched batch of 10,000 rows, `scan.BATCH`'s checkpoint, and full when its rows end, `verified_x` its time) |
| Census, frame | stat | full |
| Per-key statement; a recheck's reused observation (§12) | stat | stat |

- Any difference raises `FilesChanged` and fails the run; no pack covers it. A scan stores
  `verified_x`, when a fixed side's closing hash agreed. A resumed pass reads the same bytes: the
  several-snapshots warning never prints for a fixed side, and its census record holds no `snapshots`.
  The scan's state does count each pass of every side, a fixed one too
  (`snapshots_a`, `snapshots_b`: `scan.scan` adds one a pass whatever the engine, and `GET /v1/runs/{id}`
  answers the scan's state). `census.fixed`
  leaves the member out of the record, so no wording, warning, `run.census` row, certificate or screen
  says it of a file side: it is the count of the passes that read the same pinned bytes.
- A key-list run without packs or readiness has no census: its files are hashed at creation and
  stat-checked at each read, no more; so is `compare_one`.
- **Census record** of a file side adds `engine`, `consistency`, `files` (the count), `bytes`,
  `pinned_at`, `verified_at`, `as_of`, `modified_last`, `mount`, `rejected`, `repeated` and `lines_skipped`,
  and holds no `snapshots` and no digest. `census._detail` (the `run.census` audit row) gives a fixed side
  `consistency: fixed`, `files`, `bytes`, `rejected` and `repeated` in place of `snapshots: 1`.
- **Side a with lines that are not rows is no census** (PACK §1.1(5), §1.2). `pack._uncounted` answers,
  for a side-a record of any basis with `rejected > 0`, `side a's files hold 3 line(s) that were not
  rows`: a key on such a line that side b lacks is in no result, the case the baseline's census exists
  to exclude. No pack is issued (refusal C1). `runner/readiness._measured` asks `census_problem` for a
  whole scan too and `core.readiness.measured` reads a problem before the scan branch, so the basis is
  `keys`, the population gate not met and Y2 prints C1; a `parity_ready` pair never rests on it.
- **Wording** (`census.described`; readiness's `basis_text`, the UI and the certificate print it).
  Where `in 1 snapshot at <t>` stands: `from 12 fixed file(s), 3,650,722,201 bytes, pinned 2026-10-01
  02:14 UTC and hashed the same when the read ended at 2026-10-01 02:20 UTC; an export, not the system
  it came from: declared as of 2026-09-30 23:59 UTC by its stamp file, not observed; newest file
  modified 2026-10-01 01:58 UTC on the mount`. Without a stamp the middle reads `its own time is not
  observed`. It holds no digest: `ds.cycles.readiness` keeps it for ever (RETENTION §4.2). Where
  `csv.skip` left lines unread the bytes are followed by ` (24 line(s) at the files'
  starts skipped as the spec declares, not read)`: `read to its end` is not said of them.
- **The certificate alone** (`pack._census_lines`, from the plan) adds `; sha256 <64 hex>; reads of
  single keys before and after it were checked by size, times and inode, not hashed again`. PACK §1.3's
  churn sentence holds for `snapshot` only. The digest is followed by what it is
  the SHA-256 of and the command that prints it from the files (`manifest.SHA256_OF`): `; sha256 <64 hex>
  of the list of its files' SHA-256 values, a line each, in the byte order of their paths under the
  target's root, not a file's own (sha256sum FILE... | cut -c1-64 | sha256sum); reads of single keys …`;
  of a side that declares `partitions` (`manifest.SHA256_OF_PATHS`), `… of the list of its files'
  sha256sum lines, each with its path under the target's root, in the byte order of those paths, not a
  file's own (sha256sum PATH... | sha256sum, run in that root); …`: the holder of a side's one file who
  runs `sha256sum` on it gets another value, and must not take the pack to be about other bytes.
- **Warnings** (`census.warning` returns a list, which `pack._census_lines` extends; as built it returns
  them as it returned ENGINES' ones, one text of a line each, None without any):
  - W1 `SIDE B: 3 LINE(S) OF ITS FILES WERE NOT ROWS and were not compared: a key on such a line is not
    compared, and a key not found on this side is inconclusive` (on side a it is C1, never a warning)
  - W2 `SIDE A: 5 KEY(S) ARE HELD BY MORE THAN ONE ROW: each is inconclusive, never paired`
  - W3 `SIDE A WAS READ FROM A DIRECTORY DELTASLEUTH COULD WRITE TO`

## 12. Features

**In:** scans, soak re-diff (the same pinned bytes each cycle, useful against a live other side),
explicit keys, `key.source`, `compare_one`, mixed pairs, batched reads, the key census, hash-sample
frames (a mixed pair only: `rules._gating` refuses a hash sample between two tables), composite keys,
`prefix_pad`, row filters, `typeMap`, schedules, a key file (below).
**Out:** graph parity (each L2 level would be a pass over the files), `regex` key maps, `ILIKE`,
captured samples (service-only, as today), object stores, trailer records.

- **A key file** is a key table on a files target ([CONTRACTS.md](CONTRACTS.md) §11): `key.source` with
  `kind: table`, the target and a `table` with `file`, its `keyColumns` the key's parts. Each read (a
  run's creation, each roll of a rolling soak) lists, pins, hashes and counts the files as a file side's
  plan does, then reads them in one pass under the rows guard (`the key source could not be read (6 rows
  were read where the plan counted 99)`). A CSV key file's lines that are not rows (D6) are counted into
  that plan as a side's are, and any refuses the read by their count (`the key source holds N line(s)
  that are not rows of its declared CSV dialect: fix the export or its csv declaration`): each would be
  a key left out of the list unsaid. The reader's log line says where they are, and names the key
  source. No plan and no pin of it is stored, so an export replaced between two cycles is the next
  cycle's keys and fails no run. An export that is not ready raises `NotReady` with its code, which a
  schedule's firing waits for.

- **Mixed pairs read files in groups.** `LiveReader.batches` is true when every side is a table (today)
  or a table side is `fixed`: the worker leases `READ_BATCH` keys, `prefetch` (which already reads table
  sides only) reads the file side in one pass, and the service side reads them one by one in the same
  slot, at its target's rate. A sample of 3,000 keys is 15 passes, not 3,000. As built: a group holds one
  lane and `execution.delay` follows the group (TABLES §12), so the service side is paced by its target's
  `rate_per_s`, not by the delay; and the group's reads end at the first refused credential (401 or 403),
  which pauses the run and hands every key back, so a stale secret is tried once, not 200 times
  (`evaluate_many`). Beside a side whose rows may be large (a compared LOB
  column of MySQL or Oracle: ENGINES §6) the group stays `READ_BATCH` keys. The worker reads the file
  side for the lease's first passes in one pass (`LiveReader.ahead`), then reads the other side, judges
  and saves `LOB_BATCH` (4) keys at a time, so no more large rows are held than such a side's own group
  holds. Leased 4 keys a group, each a pass over the files, the pair would take 50 times the passes.
- **Rechecks never read a fixed side.** On a pass after the first, `evaluate_many` takes the key's
  stored first observation of that side (`compare.side_state`: its row and its evidence) after one stat
  check for the group, and `prefetch` skips that side; only the live side is read again, and the
  recheck's result names that same observation. `RecheckPolicy.due(result, fixed)` is false when both
  sides are fixed or the unusable side (M1, `identity_ambiguous`) is a fixed one: those bytes cannot
  answer differently. As built:
  - `LiveReader.standing(firsts)` gives, for a group's recheck keys, the fixed sides each keeps: the
    stored first result's observation of that side (its id and the state `rederive_all` observed from its
    row and its evidence), after one stat check of the side's files (`TableConnector.unchanged`: the
    engine's `describe` of the stored plan). `prefetch` and `evaluate_many` take it (`held`): the side is
    not read, the pass saves no observation of it, and its result's `obs_a_id` or `obs_b_id` is the first
    one's. A changed file fails the run (F9); files that do not answer the check keep nothing, so the keys
    are read as any, which records the outage as each key's failed read of that pass.
  - **A read that failed observed no bytes.** A first read of the fixed side that ended in an error state
    (an outage, a timeout, temporary space: `connect_error`, `timeout`) is not an observation: the recheck
    reads the side, in one statement for the group, and `due` counts such a side as not yet read. Without
    this a mount's blip would leave its keys inconclusive for good.
  - `RecheckPolicy.due(result, fixed)` (`fixed`: the sides that read fixed files) is false when both
    sides read them, or when a side that read them is INCOMPLETE (M1, `identity_ambiguous`, a value that
    does not convert, a row past a limit); such a result keeps its own reason instead of `recheck_failed`.
  - **A result both of whose sides read fixed files is `frozen_cut`** (`recheck.freeze`; architecture
    §8.3: a frozen cut, where re-reading is a no-op, stands in for the recheck). Left `provisional`,
    a file-to-file pair's every discrepancy would count as outstanding (owed a recheck that is never
    due) and its runs would end `completed_with_issues`; a scanned pair's per-key results are `frozen_cut`
    too, where two databases' are `rechecked`.
  - `compare_one` (`runs._read_now`) does the same for its one key.
- **Alerts.** A failed run alerts as `run.failed`; no new event. `cycle.failure` shows a `TableError`
  as `<class>: <text>`. `core.notify.error_type` sends the text
  before the first `: ` when it has a name's form (`_ERROR_TYPE`), so the alert says `FilesChanged`,
  `RowsMoved` or `TargetTempSpace`, and any other reason with a space or `/` stays `SecretError`: no
  `notify.TABLE_ERRORS` is needed (`test_error_type` holds `FilesChanged` and `TargetTempSpace`).
- **Schedules.** A firing plans inside `create_run`: one of the worker's two
  firing tasks is held while an export is hashed and counted, at most `files.plan_s`, and schedules
  queued behind two such firings past their window are `late`, code `busy`. An export that is not ready
  raises `NotReady`: `export_absent` (P16), `unsettled` (P19's first two texts), `stale` (P19's third),
  `unstamped` (P20) or `planning` (P30; also P34, the reader busy). SCHEDULES §5 catches it before
  `ValueError`: `not_started`
  with that `code`, tried every `RETRY_S` until the window closes, as `overlap` is, not three times; §8's
  table and `ScheduleMissed.code` gain the five codes (alert) and §9 the text F11. DEPLOY: set the cron
  after the export's latest arrival, `start_within` over its variance, and stagger exports. As built:
  `schedule._reason` answers `not_started` with the code and `error_type:
  NotReady` (the model requires an `error_type` beside `not_started`), `_passing` takes it, so it waits
  `RETRY_S` and counts no try; `core/notify.MISSED` and `ScheduleMissed.code` hold the codes, `_MISSED` a
  clause for each, and `schedules.line` gives F11. F11 and the alert's
  clause end by the code (§15): `ds target check` shows an absent and an unsettled export, and nothing of
  a stamp file, an age against the spec's `maxAge` or a plan's time, so for `unstamped`, `stale` and
  `planning` they name a run's creation, which says the cause.
- **Table pre-checks** ([PRECHECKS.md](PRECHECKS.md)). `describe`'s two callers (`plan.described` and
  `drift._relations`) pass the schema
  and name, and a keyword `side` carries what a file side needs (§6); `describe` answers a file side's
  `Relation` (`kind: files`, `key`, `nullable` and `size` null) from the plan's `declared` and `classes`
  after a stat check (the plan's own relation is the one the guard records at creation, so V1 to V5 see
  one shape). The guard's looks are V1 to V5 (PRECHECKS §7), and its kinds the `notify.Drift` codes, which
  a fixed side meets only as `relation_missing` or `column_missing` through a stat check's `FilesChanged`
  (F9 fails the run first). `relations`, `figures` and `duplicates` are not implemented. A pre-check of
  two schemas naming a files target (`prechecks._declared`) is refused with K1, 422 (this K1 is FILES'
  text; PRECHECKS' own K1 is another refusal), and a pre-check of stored specs naming a file side is
  refused at its look: its line is K1's text (`core.precheck.FILE_SIDE`, which `prechecks` and `duck`
  share), not `a run of this spec would be refused`, the item `a run would refuse`.

## 13. Privacy and retention

- Files are raw customer data read in place. The store gains nothing a table side does not store today
  (sealed candidate keys and scan point, encrypted evidence of the keys read per key, masked findings)
  but sizes, times, counts, the spec's path, the sealed pins and one digest.
- **The one digest that leaves** is `files_sha256`, unkeyed by design so the export's owner can
  reproduce it (`ds target check --manifest`; of a side that declares `partitions` it binds the files'
  relative paths too: §6). It is in `ds.runs.params` (never purged), `facts/plan.json`, the pack's
  manifest (`subject.sides.<x>.files.sha256`, §16) and the certificate; never in an API answer, audit
  detail, alert, readiness row or log. RETENTION §11's
  table gains, for a database dump and for a downloaded pack: *can confirm that files it holds or
  guesses are byte for byte the export a run compared; a small, guessable export by guessing*. Body
  addresses stay keyed (`hk1:`). Per-file digests, sizes and times are sealed and go with the run's data
  key (RETENTION §4.2).
- **What a side of one file discloses.** In clear, in the stored plan and so in
  `facts/plan.json`, `plan_sha256`'s input and `GET /v1/runs/{id}`, are the spec's own `file.path`,
  `file_count`, `bytes` (of all the side's files) and `modified_last` (the newest file's); the census
  record and the certificate hold the count, the bytes and the times. Of a side of several files those
  are sums and one file's time; of a side of one file (the acceptance kit's shape, any single daily
  export) they are that file's exact size and modification time, with its relative path where the spec's
  path names it, and with a stamp file the time it declares. Its own SHA-256 is not among them
  (`files_sha256` is the SHA-256 of a line holding it), and the pins stay sealed: what stays sealed is each matched
  file's name, size, times, inode and digest as the pins hold them.
- **The one copy is the spill:** plaintext sort runs (keys and compared values across engines; keys and
  hashes file-to-file) for the length of a statement. DuckDB's temp-file encryption is not relied on. A
  crashed process's spill stays until the next sweep (§10: a `ds` command's start, or within the hour
  under a worker), and no purge, erasure or crypto-erasure reaches it before: RETENTION §5.5 and §11 say
  so. DEPLOY's backup command gains `--exclude=./spill`, the upgrade note tells existing backup jobs,
  and RETENTION §11 says a backup taken without it during a scan carries raw values. RETENTION §9, which
  states that command, has the exclusion too, and both say that it names the
  default `files.spill_dir`: a spill directory placed elsewhere inside a volume that is backed up needs
  an exclusion of its own.
- Logs hold codes, positions and counts. Retention is unchanged: the plan lives with its run's row.

## 14. Security review points

1. No new outbound destination: no network extension is in the wheel, external access is off, nothing
   installs or loads. `test_no_socket_is_opened` sees Python's sockets only; U3 covers DuckDB's own. As
   observed (U3), external access off does not stop `INSTALL` from downloading: what refuses it is the
   extension directory lying outside every allowed directory, and no statement but one `SELECT` is run.
   By READER.md §7, on Linux the process that runs DuckDB also denies itself sockets before DuckDB
   loads, by a seccomp filter (`socket`, `socketpair` and `io_uring_setup` answer EPERM), unless
   `files.os_limits` is false: held natively on aarch64 (U36), not observed on an x86_64 kernel, not
   there on macOS. Its start line says whether it held. It is one more layer and no claim: the reader
   runs as the same user as its parent (READER §2).
2. DuckDB's file access is the declared roots and its spill, locked, proved at each start and in the
   smoke test. Its check is a path prefix: hence DeltaSleuth's own listing and refused links. It lets a
   statement write into an allowed directory (U3): the roots are read-only mounts (P15) and the cursor
   runs nothing but one `SELECT`.
3. No `ATTACH`; spec text reaches a statement only as `columnName` identifiers and bound values.
4. **Accepted gap.** DuckDB opens by path, and a check compares the files, never the directories above
   them: whoever can write the export directory on the host can make a read take other bytes than the
   pinned ones.
   - **A file swapped** for a link, or for another file, between a check and a read makes uid 10001 read
     another file into encrypted evidence. The next check fails the run: the file's inode and change time
     are compared, and a file renamed away and back has another change time (observed on macOS and Linux;
     a mount that keeps no change time widens this, §20).
   - **A parent directory swapped and put back is seen by no check.** With a spec path `cur/c.csv`, `mv cur
     .orig; mv .alt cur` after a check and the two renames back before the next make no link and change
     no file's inode, size or times: the stat checks pass, and the closing hash reads the genuine file
     again and agrees. The read in between took the other directory's bytes (observed: a per-key read
     answered the other file's row, and `verify(full=True)` passed afterwards; on macOS, and on Linux the
     files' inodes and times stayed as they were). The scan then stores `verified_x`, and the certificate
     says `hashed the same when the read ended` under a digest that names bytes DuckDB did not read. The
     window is one statement (a scan's sort reads every file in one, checked every 60 s at most; a
     per-key read between its two stat checks) and stands while a run is planned too (the hash, then the
     counting pass, then the closing stat).
   - Only reads pinned by descriptor close either (a seam, §20). Pinning each directory's inode and change
     time would not: a directory's change time moves whenever an entry is added to it, so any other file
     landing beside the export would fail its runs.
5. **Accepted gap.** A crafted file is parsed by native code in the process that holds the keyring.
   `ds serve --no-worker` beside `ds worker` keeps scans out of the API process; plans and `compare_one`
   still parse there. A subprocess reader is a seam.
   **By READER.md the seam is built, and the gap is narrower, not closed.** DuckDB's native code runs
   in the reader's process, a child of each `ds` process, and in no other: a fault there (U33) or a
   memory blow-up (U35, U37) ends that child, the worker and the API go on, plans and `compare_one`
   included, and files that keep ending it are read no more (F12). **What still parses a file's bytes
   in the process that holds the keyring** is DeltaSleuth's own Python: the walk of each Parquet footer
   (`manifest._footer`, `footed`) and the check of a `gzip` or `zstd` stream as it is hashed
   (`manifest._Stream`, through zlib and zstandard, which are native libraries). And the reader is no
   sandbox: it runs as uid 10001 like its parent, can open what that user can (the settings, a `file:`
   secret, the evidence's ciphertext, other roots) and can signal its parent; code run inside it by a
   crafted file would have all of that. On Linux it has no socket of its own (point 1) and a data limit;
   on macOS neither.
6. Roots lie under one declared parent and outside evidence, workspace, spill and secrets, so a
   mis-declared target reads none of them as "CSV"; the read-only mount means a statement could not
   write even if one were composed.
7. A huge or hostile file is bounded by `files.memory`, `files.spill` (checked against free space
   before a sort), `files.plan_s`, the 2 MiB line, the 16 MiB row and `files.max_files`; gzip and zstd
   expansion is bounded in memory, not time (`stop.maxDuration`). A Parquet
   footer is bounded by 16 MiB, 10,000 schema elements and 32 nested groups, walked in Python before
   DuckDB reads the file (§6 step 4): DuckDB follows a schema's nesting by recursion without a bound, and
   a file of 20 KB whose groups nest 2,000 deep ended the process that planned it (observed on 1.5.5,
   macOS arm64; the depth a Linux thread's stack takes is not measured). A dead mount is bounded by §10's
   deadlines and `STUCK_MAX`.
   **The bound that holds is narrower than "walked before DuckDB reads".** Thrift's reader, which DuckDB
   reads the footer with, takes the last schema, also under an id it cuts to 16 bits, and a count of
   children from an i32 alone (U26). DuckDB is handed a Parquet file only when the walk read its whole footer,
   field by field to its declared length, as Thrift's generated reader reads it (§6 step 4, U29), and
   found one schema, a tree of at most 10,000 elements nested at most 32 groups deep, in structs nested
   at most 8 deep, no encrypted footer and no encrypted column. **What is not proven:**
   - that the walk and DuckDB's reader take the same schema rests on Thrift's generated code as
     observed on the locked version (U29: each list the walk knows, the crafted footers, and three
     behaviours seen by hand), not on DuckDB's source: a difference
     nobody thought of would be a file the walk passes and DuckDB reads otherwise;
   - nothing here bounds what DuckDB's own parser and readers do with a footer the walk accepts, or
     with the rest of a file (pages, statistics, page indexes, bloom filters; a CSV's bytes). An
     internal error there discards the instance (D14 says what that costs: U16's and U29's are two
     that a file can cause, the second refused by the walk); a fault there ends the process
     (point 5);
   - the depth of 32 is a choice, not a measurement of what a thread's stack takes on Linux.

   The walk is Python in the pool's thread: a footer of 6.1 MiB (78,400 column chunks, DuckDB's own
   writer) took 0.7 s on §17's Mac, so the 16 MiB a footer may hold is about 2 s there.

   **The threat is a writer of the export directory who can craft Parquet bytes**, and this is what such
   a writer can and cannot do to the process that reads them. Each observed: `parquet_metadata` ends the
   process on one `TIME` statistic of a file of 186 bytes that the walk accepts (U30); a footer of 1 MiB
   makes the reader hold 672 MiB outside `files.memory` (U31); and the same conversion, of a `TIME` value
   outside a day in a page, ends the process at the first scan that compares the column, whatever a
   statement does to guard it (U30). **What holds, of a file the walk accepts:**
   - **No statement reads a footer's statistics as text.** DuckDB is asked two things of a Parquet
     file, `read_parquet` and `parquet_schema`; the plan's rows and sizes are the walk's own.
   - **What the reader holds of a footer is bounded by counts**, not by `files.memory`, which does not
     cover it: 10,000 schema elements, 10,000 row groups, each row group's column chunks exactly the
     schema's leaves, 200,000 column chunks, 2,000,000 list elements in all, 16 MiB. Measured (U31,
     macOS arm64): 235 MiB for the largest footer the walk accepts and, since a statement over many
     files holds about 2.5 footers a thread of `files.threads` at a time (4, 9 and 19 at 2, 4 and 8
     threads), up to 965 MiB at 2 threads, 2.2 GiB at 4 and 4.7 GiB at 8; a footer of DuckDB's
     own writer costs as much for its size (82 MiB at 100,000 column chunks). DEPLOY's `files.memory`
     plus 1 GiB a container does not cover a side whose footers are that large, crafted or not: it
     says so.
   - **A time's text is written without DuckDB's conversion** (§8), and **the per-key read holds no
     predicate that statistics prune** (§9).
   - **The numbers DuckDB reads as an internal error are refused by the walk**: a negative type length,
     scale or precision of a schema element, a negative count of rows (step 4), an encrypted column.

   **Tried, which is no proof.** Of 250,000 footers made by changing one to five bytes of 331 files (18
   of DuckDB's own writer and of the tests' hand-built kind: flat and nested columns, GEOMETRY, VARIANT,
   decimals, times and timestamps, several row groups, version 2 pages; and 313 with crafted
   statistics and values), the walk accepted 40,566; each went through `ds target
   check` with its count, a plan, a scan, a census with its count of rows without a key and a per-key
   read of the keys the scan gave, in processes whose memory was capped at 3 GiB: **no process ended, no
   internal error, and no key that a scan gave was missing from its own read**; 23,412 were read to the
   end by all five, the others refused by a plain error. `parquet_schema`, `parquet_file_metadata`,
   `parquet_kv_metadata` and `LIMIT 0` alone, on 18,418 accepted footers of 110,000 more: no process
   ended and no internal error. A second script (a plan, a scan and a per-key read of 21,682
   accepted footers of 120,000): none. Measured by hand; the scripts are not shipped.

   **What such a writer still can do**, each stated, none closed here:
   - **End the process with a crafted page.** Pages are not walked, and are mostly compressed. Of
     35,907 files whose pages (the bytes before the footer) had one to five bytes changed, one ended
     the process, and the same one in an earlier 35,899: a `DELTA_BINARY_PACKED` header whose block
     size is 0, in a page of a `DELTA_LENGTH_BYTE_ARRAY` column of a file DuckDB wrote with data pages
     of version 2 (one changed byte; SIGBUS or SIGSEGV in the first statement that reads the column,
     each time; the plan and `ds target check` pass it, a scan of the column dies). Nothing here bounds
     what DuckDB's decoders do with a page; a fault there ends the process that holds the keyring (point 5), the API
     process too where it plans. Only a reader in a process of its own would contain it (§20's seam).
     **By READER.md that reader is built, and this page ends the reader's process, not the one that
     holds the keyring** (U33). Observed on 2026-10-04 with the kept file (`tests/support/crafted.py`,
     320 bytes): its plan passes (4 rows); each scan of it ends the reader, with SIGBUS or SIGSEGV on
     macOS arm64 and with SIGSEGV on linux/arm64 (natively, and in the amd64 image under emulation);
     the first two are `Died` (the second read apart), the third fails the run with F12, and every
     later read of the file is refused with no statement sent; the process that read lived through
     each, `ds serve` among them (its `/v1/health` answered throughout, and a clean run completed after
     it). What the writer of such a file still gets: an end of the shared reader costs every statement
     in flight in it its pass (they are read again), its own run fails, and nothing bounds such files
     one after another (READER §17).
   - **Raise an internal error in DuckDB**, which discards the process's instance and costs what D14
     says (other targets' passes in flight are read again). None was met in the footers above; that there is
     none is not shown, and pages can cause one (U16's is a CSV's).
   - **Have a read take other rows than the pages hold, under a spec's filter**: DuckDB trusts the
     statistics, so a row group they exclude is not selected (§9), in every statement alike. No other
     effect of crafted statistics on an answer was found (values are read from the pages; a sort of
     values wider than their statistics said gave the same rows).
   - **Have the reader hold memory up to the bound above**, and refuse or fail a run: any file DuckDB
     refuses is P22 or P29 when the run is created, or a failed pass afterwards (F4).
   - What points 4 and 5 say: other bytes than the pinned ones through a swapped directory, and
     whatever native code does with them.

   **What is not proven** is therefore everything DuckDB's native code does with a file the walk
   accepts beyond what was tried: the bounds above are counts and measurements on DuckDB 1.5.5's macOS
   arm64 wheel, the conversions that fault were found by trying values, not by reading DuckDB's source,
   and Linux was not tried.

   **Tried again through the reader's process** (READER.md §15; 2026-10-04, macOS arm64, DuckDB 1.5.5).
   The fuzz is in the repository (`tests/support/fuzzed.py`: the same mutation; its seed files made anew,
   13 by DuckDB's writer and 2 hand-built, so the two runs are not the same files). 40,000 mutants of footers
   that the walk
   accepted (of 221,097 made; 181,097 refused) and 40,000 of pages, one to five bytes changed in each,
   went through `ds target check` with its count, a plan, a scan, a census with its rows without a key
   and a per-key read, in the pytest process that ran them: **that process did not end, no reader's
   process ended on any of the 80,000, every outcome was a closed code (a class's name, never DuckDB's
   text or the file's name), no scanned key was missing from its own read, and nothing timed out.**
   27,379 of the footer mutants and 7,826 of the page mutants were read to the end by all five; the
   others were refused by a plain error at the plan or at a statement. The kept page, run last, ended
   its reader twice (the scan and the per-key read; SIGBUS or SIGSEGV), as above. The default suite runs 100
   mutants of each kind and the kept page (on macOS the kept page only with `DS_FAULTS=1`: READER §15;
   `test_fuzzed_files_end_no_process_but_a_reader`;
   `DS_FUZZ=<n>` for n of each). A test, not a proof; Linux was not fuzzed.
8. Error texts and `reject_errors` lines are never read, logged or stored; DuckDB's compiled-in
   libraries are outside the SBOM's reach (§3).
9. An unkeyed digest of customer files leaves in packs (§13); per-file digests do not.

## 15. Messages (binding; each asserted; none carries a value, a file name or DuckDB's text)

**Settings** (`SettingsError`, exit 2, after `invalid settings <path>: `):
- X1 `files.memory and files.spill are sizes such as 2GB (MB, GB or TB); threads is 1 to 64, max_files 1
  to 20,000, plan_s 10 to 86,400; spill_dir and roots_under are absolute paths; os_limits is true or
  false` (as amended by [READER.md](READER.md) §4)

**Targets file** (`TargetsError`: `ds serve` and every `ds` command exit 2; T10 as §4 says):
- T8 `invalid_files_target` `engine files needs root and takes only root, settle_s (0 to 86400),
  allow_writable, rate_per_s and max_concurrency`
- T9 `invalid_root` `root must be an absolute path without . or .. segments or a trailing or doubled /,
  and not /`
- T10 `forbidden_root` `root of target legacy-exports must lie under /var/lib/deltasleuth/files
  (files.roots_under) and outside the directories DeltaSleuth uses (evidence, workspace, spill, settings,
  targets, a secret): mount exports in a directory of their own`

**Spec load** (422 on upload; `ds spec validate --targets` exit 2):
- L5 `target_kind` `table.file is for a files target (engine: files)`; `a files target needs table.file
  (path, format)`
- L6 `not supported: graph parity with a file side; load the link facts into PostgreSQL`
- L7 `not supported: ILIKE on a file side; use LIKE`
- L8 `not supported: a regex key map on a file side; declare csv.keys or a prefix_pad map`
- L9 `not supported: table.schema on a file side`
- L10 `invalid_file`, one of:
  - `file.path is a relative path under the target's root: letters, digits, space and . _ - = + @ , ( )
    in each segment, none starting with a dot; * matches inside one segment; a trailing / reads the whole
    directory` (also `ds target check --table`, exit 2)
  - `format csv needs csv (columns, delimiter); format parquet takes none`
  - `csv.columns names every field once, in file order`
  - `csv.delimiter, quote and escape are one character each, and the delimiter is not the quote`
  - `csv.newline must be "\n" or "\r\n"`
  - `csv.encoding must be utf-8, utf-16 or latin-1: convert the export for any other`
  - `csv.keys names key columns: {as: integer}, {as: decimal, scale: n} or {as: date}`
  - `file.asOfFile is a file name in the path's directory that the path does not match`
  - `file.maxAge is a duration such as 26h or 2d`
  - `file.partitions names the directories' keys: column names, none of them in csv.columns`
- L11 `invalid_type_map` `format goes with as: date or as: timestamp and holds only %Y %m %d %H %M %S %f
  %z and the characters - / . : T and space`; `decimalSeparator goes with as: decimal or as: integer and
  is "." or ","`

**Run creation** (`ValueError`: 422 from `POST /v1/runs` and `POST /v1/compare`; `ds run` and
`ds compare` exit 2; `ds target check` says `target <id>` for `side a` and exits 2; `uid 10001` is the
process's own):
- P14 `side a: the directory of target legacy-exports is not there, or DeltaSleuth (uid 10001) may not
  list it: is it mounted into this container?`; `side a: the directory of target legacy-exports is, or
  is reached through, a symbolic link: declare its real path as root`; `side a: the directory of target
  legacy-exports did not answer within 30 s: is its mount alive?`
- P15 `side a: the directory of target legacy-exports is mounted writable: mount it read-only (:ro), or
  declare allow_writable: true on the target`; `side a: file 3 of 12 lies on a mount DeltaSleuth
  could write to: mount it read-only (:ro), or declare allow_writable: true on the target`
- P16 `side a: no file matches customers/current/*.csv under target legacy-exports`
- P17 `side a: the path matches 1,204 files: a side reads at most 1,000 (files.max_files)`; `side a: the
  path holds directories more than 8 deep`
- P18 `side a: file 3 of 12 (in path order) is a symbolic link or is reached through one: links are never
  followed`; or the tails `is not a regular file`, `may not be read by DeltaSleuth (uid 10001)`, `has a
  name outside letters, digits, space and . _ - = + @ , ( ): rename it` (a directory matched
  under a globbed segment too, which is then counted as a file of the path)
- P19 `side a: file 3 of 12 changed while the run was planned: the export is still being written`; `side
  a: a file changed 12 s ago, inside the 60 s target legacy-exports waits (settle_s)`; `side a: the
  newest file is 3 d 4 h old, over the 26h the spec allows (file.maxAge): the export did not arrive`
- P20 `side a: the stamp file is not there: the export is not complete, or the spec names another file`;
  `side a: the stamp file must hold one ISO-8601 time with a UTC offset`; `side a: the stamp file is a
  symbolic link, may not be read or is over 4,096 bytes`
- P21 `side a: the first record of file 3 of 12 has 7 fields, not the 6 of csv.columns`; `side a: the
  header of file 3 of 12 does not name field 4 as csv.columns does`; `side a: file 3 of 12 ends its
  lines with \r\n where csv.newline declares \n` (a file with no record has `0 fields`)
- P22 `side a: file 3 of 12 is not a Parquet file this build reads (InvalidInputException)`; `side a:
  file 3 of 12 is encrypted Parquet, which is not read`; `side a: file 3 of 12 has another type for column
  'amount' than file 1 (DOUBLE, not DECIMAL(12,2))`; `side a: file 3 of 12 lacks column 'amount'`;
  a time's or a timestamp's type is named with its unit and an INT96 as such, `(TIMESTAMP WITH
  TIME ZONE in nanoseconds, not TIMESTAMP WITH TIME ZONE in microseconds)`, `(TIMESTAMP as INT96, not
  TIMESTAMP in microseconds)`; and `side a: file 3 of 12 has a Parquet footer that is not read: it is over
  16 MiB, its schema holds over 10,000 elements or nests groups over 32 deep, it holds over 10,000 row
  groups or 200,000 column chunks, or it is not of the form this build reads` (the
  second text is said by the plan's walk, of an encrypted footer and of an encrypted column under a plain
  one; the two counts: §6 step 4)
- P23 `side a: column 'tags' (LIST) cannot be compared: nested columns are not read; list the columns to
  compare without it`; `side a: column 'amount' (DECIMAL(76,10)) cannot be compared: a decimal over 38
  digits is not read exactly`; `side a: column 'seen' (a UTC nanosecond timestamp) is read to the
  microsecond: declare typeMap {seen: {as: timestamp}} to compare microseconds, or leave it out`;
  a type that is neither nested nor read (`BIT`, `VARIANT`, any other): `side a: column 'flags'
  (BIT) cannot be compared: its type is not read; list the columns to compare without it`; the
  nanosecond text also for `column 'seen' (an INT96 timestamp)`, and `side a: column 'at' (a nanosecond
  time) is read to the microsecond: declare typeMap {at: {as: string}} to compare its text, or leave it
  out`; the fourth text also for a decimal whose scale is over its precision, `column 'k'
  (DECIMAL(12,30))`
- P24 `side a: over 1,000 lines of its files are not rows of the declared CSV dialect: the dialect does
  not fit (ds target check drafts one)`
- P25 `side a: column 'opened' is declared date and 812 of the first 1,000 rows do not read as one:
  declare the format the export holds (typeMap format), or string`
- P26 `side a: file 3 of 12 does not lie under the declared partitions (dt)`; `side a: partition 'dt' is
  also a column of the files`; `side a: a directory above the declared partitions (dt) is named
  name=value, in the path or in the root of target legacy-exports: it would be read as a column of every
  row; rename it`
- P27 `side a: the file reader did not start: its guard did not hold, or this build's DuckDB lacks the
  Parquet reader (see the server log)`; (READER.md) `side a: the file reader did not start: its process
  did not start or did not answer (see the server log)`
- P28 `side a: the file reader did not start: files.spill_dir is not a directory DeltaSleuth (uid 10001)
  may write`; `side a: the file reader did not start: its spill volume has 12 GiB free, and files.spill
  (20 GiB) plus 1 GiB must fit: lower files.spill or grow the volume` (the free space said counts
  what this installation's live sorts spill there, §10); and a third text of two forms, by whose reads
  wait: `side a: the file reader takes no work of target legacy-exports: 4 reads of its mount, which does not
  answer, are still waiting; it resumes when they return or DeltaSleuth restarts` (its own target's:
  other files targets are planned), and `side a: the file reader takes no work: 8 reads of mounts that do
  not answer are still waiting; it resumes when they return or DeltaSleuth restarts` (of all targets)
- P29 `side a: its files could not be read while the run was planned (IOException)` (any DuckDB class: a
  wrong `compression` or `encoding`, a damaged file, `OutOfMemoryException`; `BadGzipFile` or
  `ZstdError` for a compressed stream cut short or damaged, U18)
- P30 `side a: planning its files passed the 300 s files.plan_s allows (7 of 12 files hashed): try
  again (a file renamed into place is not hashed twice), or raise files.plan_s and the proxy's read
  timeout` (the 300 s are the run's creation's, so a second file side may read `0 of 12 files
  hashed`)
- P31 `side a: one pass over its files took 48 s and a per-key read is one pass: set the spec's
  execution.timeouts.total to 96s or more (it is 60s), or export Parquet` (a per-key read skips no
  row groups, so the order of a file's rows is no help: §9)
- P32 `side a: sorting its 100,000,000 rows needs about 52 GiB of temporary space and files.spill allows
  20 GiB: raise files.spill with the volume under files.spill_dir, list fewer columns, or filter the rows`
- P33 `side a: its files hold line ends inside quoted fields, and whether a file ends inside one
  could not be checked: export them without line ends inside fields, or as Parquet`
- P34 (`NotReady`, code `planning`: a firing tries again) `side a: the file reader did not take
  its files' work (Busy): try again` (no thread was free within 30 s; `Dropped`: its DuckDB instance was
  discarded for another read's failure and is built anew: only a plan that asked
  for a cursor at that moment, or whose instance an internal error invalidated, meets it); `the file reader plans 8 runs at once, and
  this one waited the 300 s files.plan_s allows for its turn: try again`. By READER.md also the codes
  `Died` (the reader's process ended under the plan's statement), `Alone` (another task's reader apart
  did not end within 30 s) and `Unstarted` (its process did not start): `side a: the file reader did not
  take its files' work (Died): try again`
- P35 (READER.md) `side a: the file reader's process ended 3 times (SIGBUS) while only this side's files
  were being read: they are read no more until DeltaSleuth restarts`; `ds target check`, at its first
  such end: `target legacy-exports: the file reader's process ended (SIGBUS) while it read these files`.
  The cause is a signal's name (`signal 35` for one without), `exit 1`, `reply` or `killed after its
  deadline`
- P6 (file) `side a: key column 'opened' (TIMESTAMP) cannot be a key in a file: export it as text, a whole
  number, a decimal or a date`; `side a: key column 'customer_id' is text (a CSV field) and side b's a
  number: declare csv.keys {customer_id: {as: integer}}`; `side a: key column 'opened' holds 2
  date(s) outside the years 0001 to 9999, which have no key text: filter those rows out, or export the key
  as text`
- P9 (file) `side a: column 'balance' is text (a CSV field) and side b's a number (numeric(12,2)): declare
  typeMap {balance: {as: decimal}} on side a`, and likewise for each family
- P11 (file) `side a: the filter compares column 'balance' (a CSV field, which is text) with a number:
  quote the literal to compare text`
- K1 (with PRECHECKS) `not supported: a table pre-check with a file side; its run counts rows, lines that are
  not rows and repeated keys`

**`ds target check` of a files target** (exit 2; beside the run-creation texts above worded
for `target <id>`):
- `target legacy-exports: --table of a files target is a path and needs --format csv or parquet`;
  `--format is csv or parquet`
- `target legacy-exports: file 1 ends its lines with neither \n nor \r\n: convert the export` (a draft's
  first file whose lines end with a lone carriage return)
- `target legacy-exports: no CSV dialect could be drafted from file 1: write its csv block by hand
  (delimiter, header, quote, escape, newline, encoding)` (a draft's first file that DuckDB's sniffer
  refuses: another encoding, no text, or no dialect under the options given; §16)

**Run time** (the run's `error`, the Run screen; `ds run` exits 2; an alert's `error_type` is the class):
- F9 `FilesChanged: side a: file 3 of 12 is not the file the run was created on (content)` (or `size`,
  `modified`, `changed`, `replaced`, `link`); `FilesChanged: side a: 13 files match now, 12 when the run
  was created`
- F10 `RowsMoved: side a: 9,999,998 rows were read where the plan counted 10,000,000`
- F3 `TargetTempSpace: OutOfMemoryException`; `TargetTempSpace: spill`; (READER.md) `TargetTempSpace:
  MemoryError`
- F4 `TargetDenied: PermissionError`; `TargetRefused: IOException` (the third stalled pass); (READER.md)
  `TargetRefused: Oversize`, `TargetRefused: Unframed`
- F12 (READER.md; alert `run.failed`, `error_type` `ReaderDied`) `ReaderDied: side a: the file reader's
  process ended 3 times (SIGBUS) while only this side's files were being read: they are read no more
  until DeltaSleuth restarts`
- F1 log `scan of run R-… paused: OSError` (`Deadline` for a missed one); log `file read abandoned after
  its deadline (3 waiting)`; log `scan of run R-… waits for another sort` (as built: `a sort of a file
  side waits for another`: the engine does not know the run). The pause's code
  is also `Stuck` (its target's mount holds four abandoned reads), `Busy` or `Dropped` (the reader did
  not take the work) and `Empty` (the root holds no entry: is the export mounted?); `(3 waiting)` counts
  every target's; logs `spill directory 2 of 5 not swept: PermissionError` and, at error level, `the file
  reader did not start: a read outside the declared roots was not refused` (P27's cause, §10). By
  READER.md §12 the pause's code is also `Died`, `Alone` or `Unstarted`, and the logger
  `deltasleuth.files` gains: `the file reader started: duckdb 1.5.5 in process 4242, data limit 5,296
  MiB, sockets denied` (a warning whatever held, each half else as `no data limit (not held on this
  host)`: no `ds` process prints a lower level); `the file reader inherited 1 descriptor(s)` (warning); `the
  file reader did
  not start: its process could not be started (PermissionError)`, `… ended as it started (exit 1)`, `…
  did not answer within 20 s`, `… was not ready within 50 s`, `… answered outside the protocol`
  (errors); `the file reader's process ended (SIGKILL) with no statement in flight` (warning); `the
  file reader's process ended (SIGBUS) while only one side's files were being read: 2 of the 3 after
  which they are read no more`, `… (killed after its deadline) while only one side's files were being
  read: 1 of the 3 …`, `… (SIGBUS) with 3 statements in flight: each one's files are read apart next`,
  `… (exit 1: KeyError at reader.py line 212) …` (errors); `the file reader's process 4242 did not end
  within 10 s of being killed: its reads stay counted` (warning)
- F11 (with SCHEDULES) `not started: the export a file side reads was not ready before its window closed
  (export_absent): ds target check shows its state`, for `unsettled` too. For
  `unstamped`, `stale` and `planning`: `not started: the export a file side reads was not ready before its
  window closed (unstamped): starting a run on the Runs screen shows the cause`. The check reads no stamp
  file (one named `_AS_OF` is not even listed), takes no `maxAge` and plans nothing: of an unstamped
  export it prints the files and `settled` and exits 0.
- M1 (side state detail) `no row read holds the key, and 3 line(s) of this side's files were not rows`
- C1 (PACK §7.2's census refusal, READINESS Y2) `cycle 1 of run R-… is not a census: side a's files hold
  3 line(s) that were not rows`

## 16. Interfaces

- **API.** No new route. `GET /v1/targets` (viewer) adds `engine: "files"`, never `root`.
  `GET /v1/runs/{id}` (viewer) carries the plan's members but `files_sha256`, never `pinned`, and the
  scan's `repeated_x` and `verified_x`. As built: `deps.shown` leaves `files_sha256` out of
  every table side of a run it shows, whether or not the row still holds `pinned` (`repo.list_runs` drops
  the pins before the list route shows its rows).
- **CLI.** No new command. ENGINES' `ds target check ID` gains `--table PATH --format csv|parquet
  [--delimiter C] [--header] [--count] [--manifest]` (no store, not audited, exit 0 or 2). `PATH` passes
  `file.path`'s grammar and §6's steps 1 and 2, as a spec's does. The first line comes after
  T10 (with the settings file's directory), P14, P15 and the instance's proof (P27, P28), each exiting 2
  worded for `target <id>`:

  By READER.md §11 the first line ends `…, no extension loads, read in a process of its own (data limit
  5,296 MiB, sockets denied)`, each of the two phrases as the reader's start reported it: `no data limit
  (<why>)` and `sockets not denied (<why>)` else, `<why>` one of `files.os_limits is false`, `not on
  this platform` and `not held on this host`.

  ```
  target legacy-exports: files, read-only, duckdb 1.5.5, parquet and core_functions built in, no extension loads
  customers/current/*.csv (csv): 12 file(s), 3,650,722,201 bytes, newest modified 2026-10-01 01:58 UTC, settled
    1  customers-0001.csv  304,226,850  2026-10-01 01:41 UTC
  draft (sniffed from the first 20,480 rows of file 1): a run reads only what the approved spec declares
    csv: {delimiter: "|", newline: "\r\n", columns: [customer_id, email, balance, opened]}
    typeMap: {balance: {as: decimal}, opened: {as: date}}
  note: customer_id sniffs as a number and 412 sampled values start with 0: left as text
  ```
  As built (`duck.check`, called by `cli.main._files_check`; the options are `--table`,
  `--format`, `--delimiter`, `--header`, `--count`, `--manifest`):
  - A file's line names its path relative to the root (`customers/current/customers-0001.csv`), and the
    second line says `settled` or `not settled (settle_s 60)`: the check does not refuse an export still
    being written, except with `--manifest`, which pins the files as a plan does (P19); so does
    `--count` of a gzip or zstd export (below).
  - The draft comes from DuckDB's `sniff_csv` of file 1, one `SELECT` on the locked instance. Its `csv:`
    member says `header: true` whenever the sniffer took the first record for a header, with `--header`
    or without, and without it adds `note: the first record of file 1 reads as a header: --header names
    its fields from it`; a quote or escape other than `"` is printed, `"` is not (the reader's default).
    An escape that is not the quote is printed beside the default quote too
    (`escape: "\\"` for an export that writes `\"` inside a quoted field, as MySQL's `OUTFILE` does):
    without it the drafted dialect reads none of such an export's rows.
    A drafted date carries the sniffed `format` when it is not ISO (`{as: date, format: "%d/%m/%Y"}`);
    `BIGINT`, `DOUBLE`, `DATE`, `TIMESTAMP` and `BOOLEAN` are drafted as `integer`, `decimal`, `date`,
    `timestamp` and `boolean`, anything else is left text. A decimal comma is not sniffed.
  - **A line of another shape.** The sniffer wants every sampled line a row, so one trailer record after
    the rows defeats its draft (a delimiter the file does not hold and one column, or the trailer's own
    fields). File 1 is therefore sniffed a second time with `ignore_errors`, from the lines that
    are rows, as a run reads a file, under the same `--delimiter` and `--header`. Where the two readings
    differ, the draft is the one whose dialect reads more fields of the first 20,480 rows: the rows it
    reads of them times its columns, counted by the `SELECT` that finds the digits (below), one a
    reading; of equals, the sniffer's own. A draft of the second reading adds `note: drafted from the
    lines of file 1 that read as rows: --count lists the others`. `--count` then lists the trailer by
    position and kind (`file 1 line 1002: columns`), as a run of the dialect declared by hand counts it.
    The second reading is made only of a file the sniffer reads as text at all: one it refuses under the
    options given and without them (another encoding, no text) is no draft, and neither is a file for
    which no reading finds a dialect under the options given: `target <id>: no CSV dialect could be
    drafted from file 1: write its csv block by hand (delimiter, header, quote, escape, newline,
    encoding)`.
  - **The note differs.** DuckDB 1.5.5's sniffer itself answers `VARCHAR` for a column of digits with
    leading zeros, so no such column "sniffs as a number": the note is `note: customer_id holds only
    digits and 412 sampled values start with 0: left as text`, from one more `SELECT` over the first
    20,480 rows read by the drafted dialect.
  - `--count` reads the files whole by the drafted dialect, as a plan's counting pass and end check do
    (P24 and P33 can refuse it): `count (as drafted): 1,000 rows, a pass 0.3 s, 2 line(s) not rows`, then
    each line. For Parquet: `count: 1,002 rows by the footers, a pass over the first column 0.0 s` (no
    key is known to the check, so its pass reads the first column of every row group; the
    rows are the figure the check's own walk of each footer read, §6 step 4, not a
    statement's).
  - A Parquet column no plan reads prints `not comparable` in place of a class; one that
    is read to the microsecond (§8) prints what a plan needs of it, `read to the microsecond: a
    plan needs typeMap {seen: {as: timestamp}}`. By READER.md §6 a nested or unread type is printed by
    its head alone (`tags  LIST  not comparable`, where DuckDB says `INTEGER[]`): what follows the head
    holds a file's text. A partitioned layout is checked as plain files: the
    check takes no `partitions`, and `--manifest` prints the digest of either (below).
  - For Parquet the check walks each file's footer before it reads one, as a
    plan's pin does (§6 step 4; P22's fifth text for `target <id>`, and its second
    for an encrypted footer or column); and `--manifest` prints, after
    `files_sha256 <hex>`, the line `files_sha256 <hex> where the spec declares partitions (the lines
    above, whole)`: `sha256sum` of the `<sha256>  <path>` lines themselves, which is a partitioned
    side's digest (§6).
  - A first file named `.gz` or `.zst` drafts `compression: "gzip"` or `"zstd"` and is read by it. The
    draft reads the file as utf-8: the check takes no encoding, so a utf-16 or latin-1 export is drafted
    after converting a sample, or declared by hand.
  - Refusals: a path outside the grammar is L10's path text alone; without `--format`, `target <id>:
    --table of a files target is a path and needs --format csv or parquet`; another format, `--format is
    csv or parquet`; a failing statement is P29's text with the class, but the sniffer's own refusal of
    file 1, which is the draft's text above; a first file whose lines end with
    a lone carriage return, `target <id>: file 1 ends its lines with neither \n nor \r\n: convert the
    export`; a directory that does not answer within `STAT_S`, P14's third text (the first line's check
    too, through `duck.mount`), and P28's while abandoned reads wait.
  - T10 is judged on the files threads under `STAT_S` (§4), so a mount that does
    not answer gives P14's third text, or nothing when it is another target's.
    `--count` of a compressed CSV export (a first file named `.gz` or `.zst`) and `--manifest` pin the
    files as a plan pins them, each compressed stream read to its end (U18), a Parquet footer walked: an
    export cut short in transfer is refused with P29's `BadGzipFile` or `ZstdError`. And a printed line is never
    cut, however many columns the draft names.

  Without `--header` the draft names fields `column 1 …` (a sniffer may take a first row for a header),
  so no value is printed; with it, names come from the first record. Parquet prints each column's type
  and class instead of a draft. `--count` prints rows, `pass_s` and the lines that are not rows by
  position and kind (`file 2 line 81223: columns`). `--manifest` prints `<sha256>  <path>` lines and
  `files_sha256`: `sha256sum -c` in the root checks them and `cut -c1-64 | sha256sum` gives
  `files_sha256`, so the export's owner can reproduce a pack's digest. File names appear on this
  terminal only.
- **UI.** `Run.tsx` `scanLine` appends ` Side A: files (csv), 12 files, 3,650,722,201 bytes, fixed.`,
  then ` 3 lines not rows.` and ` 5 repeated keys.` when not zero; `api.ts` `TablePlan` gains the
  members and `schema: string | null`; `packs.ts` mirrors §11. As built: the line's text is
  `packs.fileSide` (a module the Node tests reach), which `scanLine` calls for a side whose engine is
  `files`; `packs.sideLine` reads a fixed side `… read to the end from 12 fixed file(s), 3,650,722,201
  bytes (an export, not the system it came from)`, shorter than the certificate's wording and without a
  digest or `as the account reads them`; `packs.censusWarnings` gives W1 to W3 in the server's words
  and, for side a's lines that were not rows, `side a's files hold 3 line(s) that were not rows: this
  cycle is not a census, and no pack is issued`.
- **Reports.** `reports/data.tables_line` prints `side a customers (files)` for a side without a
  schema. The coverage CSV gains `rows_rejected_a`, `rows_rejected_b`, `keys_repeated_a` and
  `keys_repeated_b`; the PDF "Tables" line names format, files and both counts. As built
  the two sentences are one text: `side a customers (files: csv, 12 file(s), 3 line(s) not rows, 5
  repeated key(s))`, the last once the run scanned; the four columns are the last of `coverage.csv`,
  empty for every side that is no file side, from the plan's `rejected` and the scan's `repeated_x`.
  A scanned file side that holds no repeated key says so, `0 repeated key(s)` and a
  cell of `0`: the scan stores `repeated_x = 0` when its pass opens, so that "scanned, none repeated" does not read
  as "not scanned".
- **Packs.** `pack._subject` adds `files: {count, bytes, sha256, as_of}` per side from the plan;
  `pack_verify` reads it with `.get`; the format stays `deltasleuth-pack/1`. As built: step 4
  of `ds packs verify` shows a file side's `12 file(s) sha256 <64 hex>` after its engine and server, and
  reads a pack without the member, or with one that is no object, as before; the certificate's subject
  reads `Side a (legacy): target legacy-exports, customers (files duckdb 1.5)`. The
  subject's `files` also holds `sha256_of`, §11's text of what the digest is the SHA-256 of with the
  command that prints it (`manifest.SHA256_OF`, or `SHA256_OF_PATHS` of a side that declares
  `partitions`), so that the pack says it wherever it shows the digest: step 4 of `VERIFY.txt`, which
  prints the manifest's subject; step 4 of `ds packs verify`, `12 file(s) sha256 <64 hex> of the list of
  its files' SHA-256 values, …`; and the certificate's census line (§11). `VERIFY.txt`'s own text is not
  changed: from its line 3 it is the procedure filed with the key register (PACK §3.3), which step 0
  compares, so a line added to it would fail every pack issued before. The member is fixed text, which
  the raw-value gate does not search (`pack._FIXED`: its `64` would read as a key under a numeric
  pattern). A pack without the member is shown as before.
- **Audit.** No new action; `run.census` as §11. **Licence.** No fence (O2).
  **Migration.** None: plan, pins, scan state and census are JSON.

## 17. Performance (`DS_PERF=1`, `tests/e2e/test_files.py`; TABLES publishes the measurements)

- `test_a_million_rows_of_files`: 1M rows a side, 4 columns, 11 differing keys. Parquet and CSV against
  PostgreSQL under 120 s each, Parquet-Parquet under 30 s, CSV-CSV under 60 s (PostgreSQL alone: 6.8 s).
- `test_ten_million_parquet_against_postgres`: 10M rows a side, 101 differing keys, under `ds serve`
  with default `files` settings, under 600 s. It records plan seconds and the hash rate, the scan's
  seconds and its multiple of PostgreSQL's 77.5 s, peak resident memory (asserted under `files.memory`
  plus 1 GiB), peak spill bytes (under 4 GiB, and against the plan's `sort_bytes`) and the p95 of
  `GET /v1/runs/{id}` asked once a second, alone and with `--no-worker`.
- `test_ten_million_rows_of_files`: Parquet-Parquet under 180 s, CSV-CSV under 300 s; spill bytes a row.
- `test_a_group_against_ten_million_rows`: one group of 200 keys: Parquet sorted by key under 5 s, CSV
  under 30 s (so under the default 60 s timeout with P31's margin); unsorted Parquet and each `pass_s`
  recorded (the order of a Parquet file's rows does not matter to a
  per-key read, §9: the two Parquet cases measure one thing). TABLES publishes the seconds of a pass; DEPLOY sizes per-key work by them.
- `test_batches_on_files`: 20,000 differing keys of 100,000: Parquet under 120 s, CSV under 240 s;
  rechecks issue no statement on a file side (asserted).
- `test_a_mixed_pair_on_files`: 20,000 keys of a 1M-row CSV against a mock service under 300 s, in 100
  file statements (asserted).
- `test_a_frame_of_a_file`: a `bottom_k` sample of 3,000 keys framed by a 10M-row Parquet side against
  a mock service: frame seconds recorded, at most 15 file statements for its reads.
- `test_plan_of_a_gigabyte`: hashing and counting a 1 GiB CSV under 30 s, the p95 of
  `GET /v1/runs/{id}` during that `POST /v1/runs` under 1 s.
- `test_two_scans_share_one_budget`: two 1M-row cross-engine runs started together both complete,
  their sorts one after the other, memory under `files.memory` plus 1 GiB.
- The existing `DS_PERF` tests must not move.

**As measured** (on 2026-10-03 between 23:11 and 23:37; TABLES §14.9 has every figure and what was not
measured). Method: `DS_PERF=1`, each
of the nine tests once, each in a pytest process of its own, one after the other, on one Mac that was
not idle (load average 2.8 to 5.6: other work ran on it), with DuckDB's file caches off as the instance
sets them (U28). One test was run a second time at once, because its figures stood out
(`test_batches_on_files`): both runs are given. No test was skipped, and every bound above held:

| Test | Bound | Measured |
|---|---|---|
| `test_a_million_rows_of_files` | 120 s, 120 s, 30 s, 60 s | 26.7 s, 30.9 s, 5.4 s, 14.8 s (plan and run) |
| `test_ten_million_parquet_against_postgres` | scan 600 s; memory 3 GiB; spill 4 GiB | scan 277 s inside `ds serve` (3.6 times 77.5 s) and 274 s beside it (3.5 times); 1,601 and 1,588 MiB; no spill seen (`sort_bytes` 2,538 MiB, step 8's projection: the sort fitted `files.memory`); plan 0.3 s; the file hashed at 1,985 MiB/s; p95 182 ms inside `ds serve`, 12 ms beside it |
| `test_ten_million_rows_of_files` | 180 s, 300 s | 54.0 s (spill 17 bytes a row), 181.3 s (63 bytes a row); `sort_bytes` 189 bytes a row for both |
| `test_a_group_against_ten_million_rows` | 5 s, 30 s | 0.3 s sorted Parquet, 10.6 s CSV; 0.3 s unsorted Parquet; `pass_s` 0.1, 10.0 and 0.1 s |
| `test_batches_on_files` | 120 s, 240 s | 44.3 s, 60.8 s; 100 statements each. Run again at once: 37.9 s, 34.6 s |
| `test_a_mixed_pair_on_files` | 300 s; 100 statements | 101.1 s; 100 statements |
| `test_a_frame_of_a_file` | 15 statements | frame 97 s; 15 statements |
| `test_plan_of_a_gigabyte` | 30 s; p95 1 s | 21.6 s (pass 19.8 s); p95 16 ms |
| `test_two_scans_share_one_budget` | both complete; one sort at a time; 3 GiB | 55 s; the second's sort waited; 388 MiB |

The 77.5 s of PostgreSQL's own scan is BENCHMARK's, not measured again.

As built the tests differ from the list in these ways. The 1M and 10M bounds are judged on plan plus
run. `test_batches_on_files` asserts that the per-key statements asked the files for each key once
(20,000 keys in all): its keys come from a scan, which confirms them without a recheck (TABLES §12), so
the recheck's reuse of a fixed side is asserted by `test_a_mixed_pair_on_files` instead, 200 of whose
keys differ and are rechecked without a statement on the files; that test allows 104 statements (a
lane's first group of a sweep may be smaller) and met 100. `test_a_group_against_ten_million_rows`
runs with one worker lane, so that the 200 keys are one group. `test_plan_of_a_gigabyte` asks the page
of another run ten times a second, not once, and sets its spec's `execution.timeouts.total` to 600 s so
that P31 cannot hide the figure. `test_two_scans_share_one_budget` judges "one after the other" by the
worker's log line `a sort of a file side waits for another`. The spill is looked at once or twice a
second: a shorter peak is not in the figures. The existing `DS_PERF` tests of other modules were not
run again.

Each `sort_bytes` printed above is §6 step 8's projection, seen beside a
sort: 189 bytes a row for a file-to-file pair whose sort spilled 17 (Parquet) and 63 (CSV) bytes a row,
and 2,538 MiB for the cross-engine sort of 10M rows, which fitted `files.memory` and spilled nothing the
look saw. The projection is of a sort that spills, and errs high (§20).

**The table's figures were measured with a typed `IN` in the per-key statement of a Parquet side and with
two metadata statements in a plan, which are sent no more (§9, §6 step 4); no `DS_PERF` test was run again
without them.** No plan figure changes: a Parquet side's `rows` and `sort_bytes` are computed from the walk's
figures, which are
the ones DuckDB's own metadata functions give (asserted on files of DuckDB's writer:
`test_a_parquet_plan_s_figures_are_the_footer_walk_s`), and the 10M-row file's `sort_bytes` is still 189
bytes a row. Timed apart, by
hand, with a script that is not in the repository, on the same Mac on 2026-10-04 (load average 2.6 to
3.4), the tests' own files (`write_people`: 10M rows, 175 MiB sorted by the key, 255 MiB not), through
`tables.planning` and `Files.rows` with the default `files` settings: a plan 0.19 s and 0.23 s (`pass_s`
0.1 s); one group of 200 keys 0.27 s sorted by the key (five reads, 0.266 to 0.276 s) and 0.30 s not
sorted (0.297 to 0.307 s), where the table above has 0.3 s for both; and 20 ms for a group of 200 keys
of a 100,000-row file (32 to 34 ms with the `IN`). No published figure moves by
these; the tests' own bounds were not judged again.

**With the reader in a process of its own** ([READER.md](READER.md) §14, U38; measured on 2026-10-04
between 15:05 and 16:17). Method: as above, `DS_PERF=1`, each of the nine tests once, each in a pytest
process of its own, one after the other, on the same Mac, which was not idle (load average 4.8 to 8.4 as
the tests started); DuckDB 1.5.5's macOS arm64 wheel in the reader's process, which on macOS has no data
limit and no socket filter. The two memory assertions sum the server's (or the worker's) process and
its readers, looked at once a second, against the same `files.memory` plus 1 GiB. No test was skipped.
Three sets of figures are given beside the table above (the in-process reader, 2026-10-03):

1. **the reader in its child, nothing read ahead** (15:05 to 15:28). Every bound held, but two cases
   took over 1.15 times their figure of 2026-10-03, both Parquet against Parquet, where DuckDB hashes
   and the rows only cross: 6.4 s for 5.4 s at 1M rows and 63.4 s for 54.0 s at 10M. READER §14 had
   projected 13% to 18% there;
2. **the reader in its child with one batch read ahead** (READER R9, built for that reason; 15:52 to
   16:15): the reader as built. Every bound holds and no figure is over 1.15 times its own of
   2026-10-03 in the table above; the largest is 1.06 (1M rows, Parquet against PostgreSQL);
3. **the in-process reader** (before the reader had a process of its own), in the same sitting, for the
   two file-to-file tests and the batches only (15:46 to 15:52, 16:15 to 16:17), since single runs a day
   apart on a machine that is not idle differ by themselves.

| Test | In process, 2026-10-03 | Child, nothing read ahead | Child, one batch read ahead (as built) | In process, same sitting |
|---|---|---|---|---|
| `test_a_million_rows_of_files` (plan and run) | 26.7 s, 30.9 s, 5.4 s, 14.8 s | 29.3 s, 33.0 s, 6.4 s, 15.3 s | 28.2 s, 32.4 s, 5.6 s, 15.1 s | 28.4 s, 34.3 s, 5.7 s, 15.2 s |
| `test_ten_million_parquet_against_postgres` | scan 277 s inside `ds serve`, 274 s beside it; 1,601 and 1,588 MiB, one process | 291 s, 283 s; 1,665 and 1,648 MiB, the process and its reader together (206 and 191 MiB the process's own peak) | 283 s (3.7 times 77.5 s), 283 s; 1,663 and 1,646 MiB together (208 and 192 MiB the process's own); no spill seen; plan 0.3 s; p95 130 ms inside `ds serve`, 12 ms beside it | not run |
| `test_ten_million_rows_of_files` (plan and run) | 54.0 s, 181.3 s | 63.4 s, 184.1 s | 53.2 s (spill 18 bytes a row), 182.6 s (63 bytes a row) | 56.2 s, 185.2 s |
| `test_a_group_against_ten_million_rows` | 0.3 s, 10.6 s, 0.3 s | 0.3 s, 11.0 s, 0.3 s | 0.3 s, 10.8 s, 0.3 s; `pass_s` 0.1, 10.4 and 0.1 s | not run |
| `test_batches_on_files` | 44.3 s, 60.8 s; again at once 37.9 s, 34.6 s | 47.2 s, 54.7 s | 44.3 s, 54.0 s; 100 statements each | 45.3 s, 55.8 s |
| `test_a_mixed_pair_on_files` | 101.1 s | 100.5 s | 99.6 s; 100 statements | not run |
| `test_a_frame_of_a_file` | frame 97 s | frame 110 s | frame 102 s; 15 statements | not run |
| `test_plan_of_a_gigabyte` | 21.6 s (pass 19.8 s); p95 16 ms | 22.6 s (pass 20.8 s); p95 17 ms | 22.1 s (pass 20.3 s); p95 16 ms | not run |
| `test_two_scans_share_one_budget` | 55 s; 388 MiB, one process | 60 s; 477 MiB together (206 MiB the server's own) | 57 s; the second's sort waited; 450 MiB together (207 MiB the server's own) | not run |

What these do and do not show. Against the batches' second run of 2026-10-03 (37.9 s and 34.6 s, "run
again at once") the 44.3 s and 54.0 s of 2026-10-04 are 1.17 and 1.56 times; the in-process reader took 45.3 s
and 55.8
s in the same sitting, so that difference is the machine's between the two days and not the reader's
(the test's 100 statements on the files are about 3 s of either figure). READER §14 asked for each test
twice on both builds, the faster taken: each ran once, and the in-process reader ran for three tests only. The
memory figures are no longer one process's peak (`ru_maxrss`) but the sum of
two processes' resident sizes at one-second looks: a peak between two looks is not in them. With the
reader apart the process that holds the keyring and serves the API peaked at about 0.2 GiB where it had
peaked at 1.6 GiB. The figures are macOS's: no data limit is under them. On Linux (aarch64, natively, in
the image built from this tree, `tests/e2e/test_reader_image.py`) a pass over 10M rows a side, Parquet
against Parquet, both sides' sorts held at once under the default settings and their data limit of 5,296
MiB, completed in 30 s with the reader's `VmData` at 2,579 MiB at its peak and its resident size at 2,229
MiB: an observation of the limit's room, not a `DS_PERF` figure.

**What was read ahead when these were measured** (READER §14). A batch was then read ahead only after one of
exactly 10,000 rows, which a statement fetched 64 rows a step never answers (its batches hold 10,048):
the scans across engines, whose worker reads the text columns (`test_ten_million_parquet_against_postgres`,
the two cases of `test_a_million_rows_of_files` against PostgreSQL, `test_two_scans_share_one_budget`),
and the frame's read nothing ahead in either run, so for them the table's third and fourth
columns ran the same code and differ by the day's spread. A batch of 10,000 rows or more is read
ahead after. The nine tests were not run again; one pass timed by hand (2M rows of this shape, the three
compared columns rendered for the worker, three rounds) took 15.9 to 16.4 s to fetch and shape with
nothing read ahead and 14.1 to 14.7 s with it.

**On Linux a CSV side keyed by `csv.keys` is several times slower than the figures above** (measured on
2026-10-04). The nine tests' CSV sides
declare `csv.keys {<key>: {as: integer}}`, whose key text is `CAST(TRY_CAST(field AS DECIMAL(38,0)) AS
VARCHAR)` (§9; a decimal key's is the same cast to `DECIMAL(38,scale)`). DuckDB 1.5.5's Linux aarch64
wheel takes about seven times as long for that cast as the macOS arm64 wheel the figures above were
measured on: 4.65 s against 0.64 s for 3M integer texts and 4.74 s against 0.64 s for 3M decimal ones
(DuckDB alone, 4 threads; a cast to `BIGINT` or to `DECIMAL(18,scale)` takes 0.01 to 0.02 s on both). In
the image built from this tree (linux/arm64 natively, the compose hardening, default settings) the
10M-row, 532 MiB CSV of this section took 63.6 and 64.2 s for a side's counting pass (`pass_s`) where
the table above has 10.0 to 10.4 s, and 145.7 s for one side's sort where the same Mac took 37.4 s; its
fetch took 6.0 s for 5.3 s. With `files.os_limits` off, and with the in-process reader, the image
gave the same (64.7 to 67.1 s a pass), so neither the reader's process nor its limits cause it; with a
text key a 3M-row pass took 0.4 s on both machines, against 18.6 s and 3.0 s keyed as an integer. **What
follows for a site:** under the default `execution.timeouts.total` of 60 s such a plan is refused in the
image from about 4.7M rows of this shape, some 250 MiB (P31, by the figures above; observed at 10M: `one
pass over its files took 62.7 s …`), where TABLES §14.9 and DEPLOY speak of a CSV side past about 1.5
GiB on the Mac; a per-key group costs that pass each time, and a scan's sort took about four times the
Mac's. No bound of this section was judged on Linux, no x86_64 wheel was
measured, and a date key's text was not timed. **Measure a CSV export's plan in the image it will run
in before `files.plan_s` and a spec's timeout are sized** (the stored plan's `pass_s`, or P31's text;
`ds target check --count` times a pass that builds no key text). A cheaper key text where the digits
fit is a seam (§20).

## 18. Tests (no external call)

Files are generated by an unrestricted, test-only DuckDB connection and by Python.
`tests/support/parquet_tiny.py` (a test-only Thrift compact encoder, about 150 lines, no dependency)
writes one-row-group, PLAIN-encoded files of a few hundred bytes for the shapes DuckDB's writer does not
produce: a 76-digit `DECIMAL`, timestamps of any unit adjusted to UTC or not,
`INT96` and an un-annotated `BYTE_ARRAY`, read by DuckDB in `test_duck_parquet.py`; a time's logical type,
nested groups, a count of children as written, the deprecated `min` and
`max` statistics and `created_by`; a file's parts (`laid`, `footed`), a
list that names another element type, an encrypted column and the crafted footers of U26. Fixture targets declare `settle_s: 0` and `allow_writable: true` under a temporary
`files.roots_under`; the read-only check is tested with `os.statvfs` patched, and each file's own mount
with `os.fstatvfs`.

| File | Behaviours |
|---|---|
| `unit/spec/test_targets.py`, `test_tables.py`, `test_schema_copies.py`, `unit/test_config.py` | T8 to T10 (a root outside `roots_under`, and each forbidden place; `ds spec validate --targets` does not raise T10); L5 to L11, each L10 text; the path grammar (Hypothesis: no accepted path leaves the root); X1 and the size units |
| `unit/canonical/test_convert.py` | `format` (each directive; `%y`, `%b` refused); `decimalSeparator`; leading zeros |
| `unit/connectors/test_manifest.py` | one file, a glob, a directory; hidden names left out, `_temporary/` not entered, depth 9 refused; a link at the root, a directory and a file; a FIFO; a name outside the alphabet; `max_files`; path order; a file growing while hashed; `test_file_changed_during_plan` (step 9); the stamp (missing, malformed, a link, oversized); `settle_s` and `maxAge` with an injected clock; each `NotReady` code; P15; partitions; `files_sha256` equals the `sha256sum` recipe; `verify` for each F9 code (`changed` and `replaced` among them); `test_cache_only_when_change_time_differs`, a closing hash never cached; P30 stops the hash within one chunk and the next plan goes on; `test_stuck_mount_stalls_nothing` (a stub whose `scandir` and `read` block: `GET /v1/health` answers, a run without a file side judges its keys, the deadline gives P14, the fifth call P28, the process exits); `test_stale_spill_swept_at_open_and_hourly` |
| `unit/connectors/test_duck_guard.py` | U2 to U4, U12, U17; `test_no_socket_is_opened`; P27 with a stub; P28 for an unwritable spill and for too little free space; a fatal error rebuilds the instance; a live process's spill is kept; nothing is written outside the spill |
| `unit/connectors/test_tables_compose.py` | golden statement text for `files` (the per-key `ORDER BY` in it); `test_files_statements_are_one_select_and_run`; `test_duck_statements_hold_only_grammar` (Hypothesis: hostile names, keys, literals, dialect characters) |
| `unit/connectors/test_tables_plan.py` | ENGINES §7's check for PostgreSQL-CSV (P9 for text against `numeric`, passing with `typeMap`), CSV-Parquet and Parquet-Parquet of differing types; P6 (file); the PostgreSQL side stores `classes`; P31, P32 and the count cache (`test_a_pass_must_fit_the_timeout_and_a_scan_s_sort_the_spill`); `test_postgres_pair_plan_unchanged` |
| `unit/connectors/test_duck_csv.py` | each dialect member; U13, U14, U16; CRLF files; quoted newlines across a buffer boundary; BOM; utf-16, latin-1; gzip, zstd; `null` forms; `padded`; P21 (a headerless file is refused, its row quoted nowhere; the `\r` text); each kind counted; a file cut inside a row counts one line; 1,001 refuse; P25; P29 for a wrong `compression` and a gzip with a damaged tail; `csv.keys` texts; a canary value reaches no message, log or stored row |
| `unit/connectors/test_duck_parquet.py`, `tests/golden/engines/` | each row of §8 against `expected.json`; `test_portable_values_digest_alike` on four engines, a `padded` CSV column against Parquet `VARCHAR` among them; refused types; P22; U15; INT96, un-annotated `BYTE_ARRAY`, a 76-digit `DECIMAL` and a UTC nanosecond timestamp from `parquet_tiny` files (observed, not stubbed); two instants a nanosecond apart never digest equal without the declared `typeMap` |
| `unit/connectors/test_duck.py` | per-key: row, 404, ambiguous duplicate, `test_per_key_rows_come_one_statement_a_group` (a key's two rows, one selected, which comes first; a key not of its class and a lone surrogate answered without a statement), M1 (the side state's detail, without `typeMap:`: `test_a_key_missing_from_a_side_with_lines_that_are_not_rows_is_inconclusive`); `test_timed_out_group_is_not_reread_by_key` (temporary space met by a group's sort is read once more, in it too); `test_one_sort_at_a_time`; `test_a_sort_is_refused_without_free_space`; U5 to U7, U10, U11; every class of §10; no DuckDB text in `str(exc)` |
| `unit/runner/test_files.py` (as built: the runs with a file side), `test_scan.py`, `test_census.py`, `test_recheck.py` | `test_files_changed_fails_the_run` (size, time, change time, inode, same-size-same-times content at the pass's end, an added file, a link; mid-scan, at resume, in a soak's second cycle); `test_rows_moved_fails` (with a filter too); `test_io_error_on_unchanged_bytes_fails_at_the_third_pass` (a Parquet file with a damaged compared-column page, and a stubbed EIO); `test_an_unmounted_directory_pauses_and_a_changed_file_fails_at_resume`; `repeated`; the wording without a digest, W1 to W3; the `run.census` detail of a fixed side has no `snapshots`; `test_mixed_pair_reads_the_file_side_in_groups`; a recheck that reuses a fixed side's first observation (in `test_mixed_pair_reads_the_file_side_in_groups`), `test_no_recheck_is_due_where_fixed_bytes_alone_would_answer` (between two fixed sides, after M1), `test_a_recheck_reads_a_fixed_side_whose_first_read_failed`; PostgreSQL records unchanged |
| `unit/reports/test_pack_contents.py`, `unit/runner/test_readiness_measure.py`, `unit/core/test_notify_events.py` | **`test_side_a_with_lines_that_were_not_rows_gets_no_pack`**, with `test_a_file_side_s_record_says_what_it_was_read_from` in `unit/runner/test_census.py` (the pack is refused with C1; the scan's basis is `keys`, population not met, Y2 prints C1; the same lines on side b give a pack with W1); `test_per_file_digests_are_sealed` (no size, time or digest of one file in `params.tables`, `plan.json`, an audit row or an API answer; `files_sha256` in the plan and certificate only; none in `basis_text`); subject; no matched name in any pack file (a canary); a dated path trips the gate at `sides.a.table.file.path`; older packs verify; `test_error_type` gains every F message here and of ENGINES §12 (its class) and a `SecretError` text (`SecretError`) |
| `unit/cli`, `unit/api`, `unit/reports/test_csv.py`, `web/test/packs.test.ts`, `unit/test_release_tool.py` | coverage columns; `tables_line` without a schema; `ds target check` output and exits, a refused `--table` path, positions without `--header`; `/v1/targets` without `root`; the list route without `pinned`; `test_never_told`; the smoke step's argv and message |
| `unit/runner/test_schedule_fire.py`, `unit/runner/test_precheck_run.py`, `unit/connectors/test_duck.py` (as built) | `test_a_late_export_starts_in_its_window` (a real firing against a real files target: the export absent, then unstamped, the stamp landing 30 minutes after the due time under `start_within: 60`: one run, no miss row; the next day's stamp never lands: missed as `unstamped`); `test_an_absent_export_is_missed_with_its_code`; `test_precheck_with_a_file_side_refused` (K1); `test_a_fixed_side_is_described_from_its_plan` |
| `e2e/test_files.py` | TABLES §7's dataset as CSV-CSV, Parquet-Parquet, CSV-Parquet, each against PostgreSQL, and CSV against a mock service (MySQL and Oracle under their markers): counters and findings exact. Also a glob of three files; partitions; a composite key; `prefix_pad`; `emptyAsNull`; a decimal-comma, `DD/MM/YYYY` CSV against `numeric` and `date`; a zero-padded CSV key against `integer`; kill and resume; two soak cycles, then the file replaced; the key census; a frame with strata |

The readers and the plan: `unit/spec/test_tables.py` (L5 both ways, each L10 text at its member,
L6 to L9, L11), `unit/canonical/test_convert.py` (`format`, `decimalSeparator`, leading zeros),
`unit/connectors/test_duck_csv.py` (every dialect member, P21, other line ends, each kind of line that is no
row counted once, U20 and a quoted field's line ends across a scan's start and the buffer's end, U21 and a
file cut inside a quoted field among twelve, P33, a CSV side's partitions, P11, P24, P25, P29 and U18),
`test_duck_parquet.py` (the golden corpus `files.sql` and
`files.json` beside ENGINES' one, `portable.json` read from Parquet and a padded CSV field and digested as
the other engines' rows, nested, wide decimal and UTC nanosecond refusals from `parquet_tiny`, INT96 and
`BYTE_ARRAY`, P22, nanoseconds and their bridge), `test_duck.py` (per-key reads in one statement, a column
named `s`, keys answered without a statement, M1, no re-read by key, temporary space retried, `SORT`, the
free-space check, §10's stat check of an error, the 60 s watch, a file changed between a scan's batches, a
fixed side described from its plan), `test_tables_compose.py` (golden
statements in `files_pair.json`, each one `SELECT` run on the locked instance, Hypothesis: hostile keys,
literals, prefixes and delimiters change no statement), `test_tables_plan.py` (P6 and P9 for a CSV field
against PostgreSQL and against a Parquet side, two Parquet sides of two families, P6 of a Parquet key, P31,
P32, the count cache, the sealed pins left out of `plan.json` and answers),
`runner/test_census.py` (a fixed side's record, wording, W1 to W3, `run.census` detail, C1, readiness's
basis; the key census of a CSV side against PostgreSQL: its repeated key, its line that was no row, its
closing hash, and `{failed: FilesChanged}` once its file changed), `runner/test_precheck_run.py`
(`test_precheck_with_a_file_side_refused`), `core/test_notify_events.py` (F3, F4, F10) and `e2e/test_files.py` (the dataset as CSV and Parquet
against PostgreSQL, MySQL and Oracle, CSV against Parquet, CSV against a service with a line that is no
row, a declared dialect against `numeric` and `date`, side a's lines that are not rows, a soak whose file is
replaced in its third cycle, the rows guard, a directory taken away and put back, a Parquet file with a
damaged page of a compared column failing its run at the third stalled pass, three partitioned
Parquet files with a composite key and a `prefix_pad` map, a hash sample framed by a CSV side). The
key census runs against the store in
`runner/test_census.py`, each reader's statement in `test_files_statements_are_one_select_and_run`.

Runs: a module of its own, `unit/runner/test_files.py`, holds the runs with a
file side, each against the store with files of its own: `test_mixed_pair_reads_the_file_side_in_groups` (250
keys, two
passes, the recheck's result naming the first observation), no recheck where fixed bytes alone would answer
(M1, a repeated key), a recheck that reads a fixed side whose first read failed, the one stat check of what
stands (changed: F9; not answering: nothing stands), a mixed pair's soak (a pass a cycle), `compare_one`,
a refused credential ending a group's reads, two file sides hashed in DuckDB and `frozen_cut`, a padded field and a `typeMap` column as worker
columns, `test_files_changed_fails_the_run` (size, modified, changed, replaced, a link, an added file), a
per-key read's changed file and refused permission failing its run, the closing hash on a mount that keeps no change time (content), `test_rows_moved_fails` (with a filter
too), F3 and F4 as a run's error and its `error_type`, the sort refused without free space,
`test_io_error_on_unchanged_bytes_fails_at_the_third_pass` (a stub at one row, the first pass having saved
a checkpoint), an unmounted directory pausing (F1's log) and a changed file failing at resume, a mount
that does not answer (four abandoned reads, then `Stuck`, a run of two services judged meanwhile) and
`test_per_file_digests_are_sealed` (no name, size, time, inode or digest of one file in the stored plan,
`plan.json`, an audit row, the cycle's row, an answer or a log; `files_sha256` in the plan and `plan.json`
alone). Beside it: `runner/test_recheck.py` (`due` and `freeze` with fixed sides), `api/test_runs.py` (a
files target without its root, a run's answers without the digest or the pins on both routes, P16 as a
422), `connectors/test_tables_compose.py` (the hashed scan's golden text in `files_pair.json` and
`row_hash` on the locked instance: a NULL, an empty text, a moved boundary, bytes, padding),
`test_tables_plan.py` (two Parquet sides alike: no worker column, 32 bytes a row), `test_duck.py` (§15's
plan refusals verbatim through `_planning`, `SORT`'s log, a per-key read refused while abandoned reads wait:
an outage), `test_duck_parquet.py` (P26's second text, P23's
fourth, a row over 16 MiB), `test_duck_csv.py` (a value that does not convert reaches no message, log or
plan), `test_manifest.py` (a mount answering with an error: P14 at plan, an outage afterwards; F1's
abandoned-read log), `spec/test_tables.py` (L10's path text verbatim) and `e2e/test_files.py` (the dataset
as two CSV sides and as two Parquet sides, `emptyAsNull`, a scan stopped mid-pass and resumed after its
checkpoint, a frame's drawn keys read in one pass). C1 is asserted on `pack.census_problem` and readiness's
basis (`runner/test_census.py`). The stuck mount is
tested on the manifest and on a run; `GET /v1/health` is not asked.

Packs, reports, the CLI and the UI: `unit/reports/test_pack_contents.py` (the subject's `files` and step 4 of
`ds packs verify`, a pack without the member; `test_a_pack_of_a_file_side_names_its_digest_and_no_file`:
a pack issued from a store-level signed cycle whose plan is a file side's, its certificate's census line
verbatim with the digest and the single-keys sentence, W1 to W3 for side b, `files_sha256` in
`facts/plan.json` and the subject, and no sealed pin, canary file name or per-file digest in any pack
file, the PDF's streams among them, the pack verifying; `test_side_a_with_lines_that_were_not_rows_gets_no_pack`
(C1 from `pack.problems` and `pack.issue`); `test_a_dated_path_trips_the_raw_value_gate` (the place
`sides.a.table.file.path`)), `unit/reports/test_csv.py` (the four coverage columns and the "Tables"
line, scanned and not), `unit/cli/test_cli.py` (`--table` for CSV and for Parquet: the draft without and
with `--header`, the two notes, `--count`'s line by position and kind, `--manifest` against Python's own
digests and §6's `files_sha256`, three refused paths, a refused format, P16, P29 by its class; a gzip
export drafted and read by its compression, a lone carriage return refused, a directory that does not
answer refused with P14's third text and its abandoned call counted until it returns),
`unit/connectors/test_manifest.py` (`test_never_told`, one case a pattern), `web/test/packs.test.ts` (a
fixed side's lines and warnings, `fileSide`), `e2e/test_files.py`
(`test_the_files_kit_gives_its_expected_result`, the acceptance kit of `examples/tables/files/` as
shipped, and §17's nine `DS_PERF` tests). The signed cycle of the pack tests is written to the store as
the other pack tests write theirs, with a file side's plan and sealed pins; it is not a run of real
files followed by a sign-off, so "no matched name in any pack file" rests on that test for the pack's
builder and on `test_per_file_digests_are_sealed` for what a real run stores. Not tested: a
pack issued from a real run of files end to end; `ds target check --table` while abandoned reads wait
(P28's text for it).

Wrong answers and security, each rule with a test that fails without it:
`unit/connectors/test_manifest.py` (a directory named `*`, `**`, `?`, `[a]` or with a quote under a globbed
segment: P18, and `FilesChanged` once one is made later; gzip exports of two and three members, each over
1 MiB of text, in one read and across reads, and a stream cut short or with a tail that is no member;
`name=value` directories in the fixed part and in the root; each file's mount by `fstatvfs`; a partitioned
side's digest; the footer walk: 2,000 groups deep, 32 deep, a negative count, a leaf that claims children,
10,001 elements, a length past the file, no schema, the size limit), `test_duck.py` (the reader binds
pinned files only, none a pattern), `test_duck_parquet.py` (INT96 and a nanosecond time refused until
declared; a later file's unit or INT96 as P22; a `DECIMAL(9,9)` key scanned and read by its own key, DATE
keys outside four-digit years refused; a text key under signed-byte statistics, with DuckDB's own pruning
observed; a nested and a negative-count footer refused by a plan and by `ds target check`;
`region=eu` and `k=zzz` directories), `test_duck_csv.py` (`lines_skipped`; a partitioned side counted by
its paths; a CSV date key of the year 0000; a `v=eu` directory), `test_tables_compose.py` and
`files_pair.json` (the join on `encode`, no `IN` on a text key), `unit/canonical/test_convert.py`
(underscores, other scripts' digits), `unit/test_approvals.py` (`csv.skip`), `unit/cli/test_cli.py` (the
second digest line; an INT96 column's line), `unit/runner/test_scan.py`
(`test_a_pass_takes_the_one_sort_before_a_database_snapshot_opens`: no session of the database while the
pass waits, a row committed meanwhile read, `first_at` before the file side's sort, with the database on
either side), `unit/runner/test_census.py` (`RowsMoved` in a key census fails the run; the wording of
skipped lines) and `unit/runner/test_files.py` (a group of 40 keys beside a MySQL `TEXT` column: one pass
over the files, the MySQL side read 4 keys at a time; `csv.skip` approved, stored and said).

Reliability is tested the same way, each scenario in the module that holds its
subject. `unit/connectors/test_manifest.py`: `test_a_busy_pool_is_no_dead_mount` (sixteen held threads: a call that
waited and then ran is answered, one no thread took is `Busy`, P34 at plan, never run afterwards),
`test_stuck_mount_stalls_nothing` (a second files target planned while the dead one stands at its four,
P28's two forms), `test_a_read_error_of_the_closing_hash_is_no_change` (EIO, ESTALE, a refused permission,
a missing file, a link), `test_an_emptied_root_is_an_outage_after_the_plan`. `test_duck.py`:
`test_a_statement_no_thread_takes_is_withdrawn_and_never_run`,
`test_null_keys_on_a_mount_that_stops_answering_releases_the_sort`,
`test_a_run_s_file_sides_share_one_plan_s`, `test_few_runs_plan_their_files_at_once`,
`test_a_short_spill_at_run_time_is_temporary_space`, a root's own count in
`test_file_work_refused_while_reads_wait_is_an_outage`, P34's wording. `test_duck_guard.py`:
`test_concurrent_first_uses_build_one_instance`, `test_one_instance_a_process_built_anew_when_dropped` (no
statement more, the spill directory given up with the last one),
`test_a_stale_spill_directory_that_cannot_be_swept_stops_no_start`, the start's free space with a live
sibling's spill (`test_an_unwritable_or_small_spill_is_p28`), the guard's log lines
(`test_an_unproved_guard_is_p27`). `test_tables_plan.py`:
`test_a_scan_planned_just_under_the_spill_limit_completes` (two million rows of a 44-character key: a
limit that the texts and 16 bytes a row alone would fit is refused, and the side planned at its projection is
scanned to its
end under it). `unit/spec/test_targets.py` (T9: a trailing, a doubled and two leading slashes).
`unit/cli/test_cli.py`: `test_target_check_of_a_root_that_does_not_resolve_never_hangs`,
`test_target_check_reads_a_compressed_export_s_streams_whole` (a gzip and a zstd export cut short, with
`--count`, `--manifest` and both), `test_target_check_drafts_an_escape_and_a_wide_file_whole`,
`test_target_check_drafts_an_export_with_a_trailer_from_its_rows` (the acceptance kit's CSV with a trailer
record, with and without `--delimiter`, `--header` and `--count`; a latin-1 export refused in the
draft's own words).
`unit/runner/test_files.py`: a run of another files target created and read while one mount hangs at its
cap (`test_a_mount_that_does_not_answer_costs_its_own_runs_only`), `repeated_x` 0 for a scanned side.
`unit/reports/test_csv.py` and the acceptance kit's `expected.json`: the zero in the cell and on the
line. `unit/runner/test_schedule_fire.py`: `test_a_late_export_starts_in_its_window`. Not tested: a closed
request (§10
says what happens). U27's measurements were made by hand, with scripts that are not in the repository;
their figures are in §6 step 8.

The footer walk, DuckDB's caches and a discarded instance are tested where their subjects are.
`unit/connectors/test_manifest.py`,
`test_a_parquet_footer_is_walked_before_duckdb_reads_it`: three crafted footers (the
schema written twice, the second also under the id 65538, `num_children` again as an i64), 5,000 groups
deep; a count of children a reader cuts to 32 bits; the schema as a
struct, as a list that names numbers, after the rows; row groups that name numbers; a list parquet.thrift
has not, a set, a map, a type Thrift has not; structs nine deep; a byte after the footer's end; a length
that starts inside the footer, and one that starts before it; an encrypted footer and an encrypted column
(P22's second text); and what still passes: eight structs deep, statistics and `created_by`, an unknown
field without a list, a file that does not end as Parquet does. `tests/support/parquet_tiny.py` holds
`laid`, `footed`, `crafted`, a list that names another element type and an encrypted column.
`unit/connectors/test_duck_parquet.py`: `test_a_footer_duckdb_is_not_handed_refuses_the_plan_and_the_check`
(the three footers 40 deep and an encrypted column through `tables.planning` and `duck.check`: the
contract's words, no statement sent); `test_a_crafted_footer_reaches_duckdb_in_no_process` (the three,
9,000 deep, planned and checked in a process of its own:
it exits 0, each refused, no statement sent; and the same footer under last bytes that are not Parquet's
is handed to DuckDB, which refuses it without reading it);
`test_the_reader_takes_a_list_s_elements_as_parquet_thrift_declares_them` (U29);
`test_a_parquet_file_replaced_in_place_is_read_by_its_own_footer` (U28: by the locked instance the new
footer; with DuckDB's defaults, and with the Parquet metadata cache on, the old one, as observed).
`unit/connectors/test_duck_guard.py`: `test_an_unproved_guard_is_p27` (a cache left on is P27, logged by
its name); `test_one_instance_a_process_built_anew_when_dropped` (no cursor more; a held cursor's
statement runs; one that meets the invalidated database is `Busy('Dropped')`; the spill directory stays
while a cursor is open and goes with the last, a second close counting nothing).
`unit/connectors/test_duck.py`: `test_a_scan_in_flight_survives_another_target_s_hung_statement` (a scan of
one files target has fetched its first batch when a statement of
another hangs on a FIFO, is abandoned and discards the instance; the scan reads on to its last row, its
closing hash and `null_keys`; a new session opens on a new instance; the old spill directory goes once the
hung statement returned) and `test_a_per_key_reader_leaves_a_discarded_instance`. Not tested: a real
internal error under a pass in flight (the invalidated database is a stub's `FatalException`; that an
open result is still fetched was seen by hand), and a discard between two
fetches of a spilling sort, which was run by hand (the spill stayed counted, and the
volume's space came back when the session closed).

What a crafted Parquet file can do (§14 point 7) is tested where its subjects are.
`unit/connectors/test_manifest.py`:
`test_a_footer_holds_the_schema_s_chunks_and_few_lists` (a footer with a second row group of
100,000 column chunks; a chunk more and a chunk fewer than the leaves, another path, a chunk without
its metadata, a row group without chunks; 10,001 row groups, 200,004 column chunks and 2,000,001 list
elements, and what still passes: 10,000 row groups and 2,000,000 list elements; a negative count of
rows of a row group and of the file, a negative size, a negative type length, scale and precision; the
figures the walk returns, a nested leaf giving none and a file that does not end as Parquet does None;
a side's sum of them, also where each file's hash stands for reading it, with no read of a file).
`unit/connectors/test_duck_parquet.py`: `test_a_crafted_footer_reaches_duckdb_in_no_process` also
hands the plan and the check, in its process of its own, that footer (refused, no statement sent), two
`TIME` statistics and a NULL-typed column's minimum and maximum (each planned, scanned,
read by its key and checked with its count: `parquet_metadata` ends the process on the first and
invalidates the database on the others), a `TIME` of 2^60 and of -2^60 microseconds in a page under
statistics that say it lies inside a day (each scanned and read as a value that does not read as a
time: DuckDB's own text of it ends the process, or is an internal error), and counts the statements
that name a metadata function: none; `test_a_parquet_plan_s_figures_are_the_footer_walk_s` (a side of
two files and three row groups against a CSV side: the walk's rows and sizes are those
`parquet_file_metadata` and `parquet_metadata` give, the plan's `rows` and `sort_bytes` are computed
from them, the figures are no part of the sealed pins, and a plan, a scan, a per-key read and `ds
target check --count` send no statement that names a metadata function);
`test_a_typed_key_is_not_pruned_by_statistics` (an integer and a date key under statistics of 100 to
200 for a group that holds 5, a decimal stored as bytes under empty ones: DuckDB's own `IN` and its join
on the bare column find no row, as observed; the scan's key is found by its own read, and the
statement holds no `IN` and no `WHERE`); `test_a_time_is_written_from_its_microseconds` (51,000 times
of day, `24:00:00` and NULL among them: the text is DuckDB's own, and the expression names no cast to
text); `test_a_partition_and_a_type_no_plan_reads_are_refused` (a decimal whose scale is over its
precision, as a key and as a compared column: P23's fourth text);
`test_a_footer_duckdb_is_not_handed_refuses_the_plan_and_the_check` (a row group of more chunks than
leaves and a negative type length among its footers: the contract's words, no statement sent).
`unit/connectors/test_tables_compose.py` and `files_pair.json`: the per-key statement without its typed
`IN`. `unit/connectors/test_duck_guard.py`: `test_an_unproved_guard_is_p27` (a build that lacks an
extension, simulated by httpfs and a setting of its own among the caches: P27,
logged by the extension's name).
`unit/connectors/test_tables_plan.py`: P31's text. `tests/support/parquet_tiny.py` holds a date and a
NULL logical type and `swollen`. Not tested in the repository: the crafted statistics and values of
U30 beyond those named (162 and 151 files, each statement in a process of its own), the memory of U31
and the fuzz of §14 point 7, which were run by hand with scripts that are not in the repository; and
nothing hands DuckDB the page that ends the process (§14 point 7).

What a pack says of its digest, F11's wording and the spill volume's mount point are tested where their
subject is. `unit/reports/test_pack_contents.py`: `test_a_pack_says_what_its_files_digest_is_and_how_to_reproduce_it`
(a side of one real file, whose own SHA-256 is not the pack's digest: the subject's `sha256_of`, the
certificate's census line and step 4 of `ds packs verify` say what the digest is, and the command they
give, run by `sh` on the files, prints it, a partitioned side's from its paths too; the gate does not
search the member, whose `64` a numeric key pattern reads as a key), with the subject's and the
certificate's texts verbatim in the two tests beside it, the second of which also runs step 4 of the
issued pack's own `VERIFY.txt` with `sh` and reads the member in what it prints.
`unit/runner/test_schedule_fire.py`, in
`test_a_late_export_starts_in_its_window`: the missed `unstamped` time's line names a run's creation,
`duck.check` of that export prints the file and `settled` and nothing of the stamp, and creating the run
says the cause. `unit/core/test_notify_events.py`: the alert's clause for each of the five codes.
`unit/deploy/test_config_examples.py`: `test_the_image_holds_a_mount_point_for_a_spill_volume` (the
Dockerfile's `install -d` line, and DEPLOY's volume and `files.spill_dir` on that path). What the
documents say is held by no test (§2): the directory swap of §14 point 4 was run by hand against the real
`duck._verify`, `Files.rows` and `manifest.verify` with a script that is not in the
repository, and the ownership of a new volume with a local image under the compose hardening.

**By READER.md** (its §15 names each test). The tests of U1 to U31 still observe DuckDB itself, on
`ducked.locked`, a connection of the test process built and proved by `reader.connect` and
`reader.prove`; everything the product reads goes through a reader (`ducked.started`, or the process's
own), and a fixture of `unit/connectors/conftest.py` asserts after each module that no reader is left
but the current one. `unit/connectors/test_reader.py` holds the reader's own tests: the frames (a
property test of rows as fetched, what no frame carries, DuckDB's error names), an answer and a start
outside the protocol, a batch over a frame and an answer over its limit, every composed statement's
values through the frames, `test_the_parent_never_loads_duckdb` (a plan, a scan, a per-key read and a
check in a process of its own), the start with nothing of its parent's, starts that fail (P27's second
text), a replaced and a killed reader, a closed cursor's statement, idle cursors' threads, a batch read
ahead, the socket filter interpreted over both architectures, `files.os_limits` and the data limit's
proof, the deadlines (a hung statement's process killed, charged where the mount answers and not where
it does not, an abandoned statement that answers, a process that gives no sign of life, a killed one
that does not end), the accounting of ends (one set charged, several suspect and read apart, a pair
sharing one reader, a per-key read that waits for no pass, an innocent run, a condemned set), the kept
mutant (`tests/support/crafted.py`), the fuzz (`tests/support/fuzzed.py`: 100 footer mutants, 100 page
mutants and the kept one in the default suite, `DS_FUZZ=<n>` of each kind), and that no reader outlives
its parent. Also: a frame of too many cells and a wide statement's
shorter batches, a reader's text that reaches no message, an interrupt before a statement began, a
reader's end learnt from its process, a pass whose reader ended before its sort, a batch fetched in
steps read ahead, a process that reaps its reader as it exits; and `tests/support/faults.py`, by which
a test's reader is ended on macOS by a signal that is no crash report there, the kept mutant's tests
running there with `DS_FAULTS=1` alone. **On a Mac the default suite makes no real fault:** every
process macOS sees end by SIGSEGV, SIGBUS, SIGABRT or SIGTRAP is a crash to it, with a report under
`~/Library/Logs/DiagnosticReports` and a `Python quit unexpectedly` dialog on the screen. `DS_FAULTS=1
uv run pytest tests/unit/connectors/test_reader.py tests/unit/connectors/test_duck_guard.py
tests/e2e/test_files.py` makes them there (SIGSEGV in the tests of a fault itself and of the self-test,
which send SIGKILL otherwise, and the three tests of the kept mutant), seventeen such dialogs a run; on Linux
they are made always, and `DS_IMAGE=<image>` runs them in the image
whatever the host. `unit/runner/test_files.py`: a scan and a key-list run fail with F12 after three charged
ends while the worker and another run go on, and the three pauses. `e2e/test_files.py::
test_a_crafted_page_fails_its_run_and_no_other` (under `ds serve`, whose process stands and whose
`/v1/health` answers throughout) and `::test_the_server_s_log_says_how_its_reader_started`,
`e2e/test_kill_resume.py::test_a_worker_killed_mid_scan_leaves_no_reader`.
`e2e/test_reader_image.py`: U32 to U37 in a Linux image, skipped unless `DS_IMAGE` names one (§3).
`test_never_told` searches `reader.py` too; `test_the_self_test_and_its_imports` asserts the self-test's
printed line with and without its limits and that DuckDB is not among its process's modules.

## 19. Documents this contract amends

- **TABLES:** goal, §1 to §3, §4 (the file statement, M1), §5 (rows guard, fixed sides, `repeated_x`),
  §9, §10 (`format`, `decimalSeparator`), §12 (mixed pairs in groups, rechecks of a fixed side), §13
  "Engines" with §17's figures and the seconds of a pass.
- **ENGINES:** D8, D9; §5 (`describe`'s keyword `side`, `retry_alone`, the classes, `cycle.failure`);
  §7 (when the cross-engine check runs; a DuckDB column); §9; §11 (the stalled pass of a fixed side);
  §18 (the files seam is built). **CONTRACTS:** §1, §2 (`files`), §4, §7, §11.
- **ALERTS:** §2.1 (`error_type` reads a `TableError`'s class), §14 (`test_error_type`).
- **READINESS:** §3 (a scan with a census problem is basis `keys`; `basis_text` of a fixed side, without
  a digest), §9 Y1 and Y2, §12, §17. **PACK:** §1.1(5), §1.2 (the fixed record; side a with lines that
  are not rows), §1.3, §2.2, §2.3 (`file.path` is searched; `pinned` is in no pack), §7.2 (C1).
- **RETENTION:** §4.2 (the sealed pins go with the data key; `files_sha256` stays with the run row),
  §5.5 (the spill and its sweep), §9 (the backup command, with `--exclude=./spill`), §11 (the two new
  "can" entries; a crashed process's spill; a backup without `--exclude`).
- **SCHEDULES:** §5 steps 5 and 6 (`NotReady`), §8 (the codes), §9 (F11), §16 (a firing
  that plans files holds a firing task). **PRECHECKS:** §5 (`describe`'s argument), §7 (a
  fixed side), §10 (K1), §14.
- **DEPLOY:** §3 Settings (`files`, the proxy's read timeout over `files.plan_s`). §7 "Files": the `:ro`
  mount under `files.roots_under` in `docker-compose.site.yml` for every service that runs `ds`,
  readable by uid 10001, with its SELinux label; what to ask the export's owner (format, delimiter, line
  ends, encoding, a stable path, write then rename, a stamp file written last, no header or trailer
  records of another shape); mounts with stable inodes (not CIFS `noserverino`); D2's bucket sync; the
  seconds of a pass and `execution.timeouts.total`; schedules after the export's latest arrival,
  staggered; `--no-worker`; §10's sizing. §10: spill under "Encrypted volumes", a volume of its own
  where exports are large, `tar --exclude=./spill`, the evidence volume sized by `files.spill`. §11: rows
  for P14, P15, P19, P24, P28, P30 to P32, C1, F9, F10 and F3.
- **RUNBOOK:** "Compare two tables" gains "From files": draft with `ds target check`, declare dialect and
  types, the `sha256sum` recipe, leading zeros, dates, lines that are not rows (side a: fix the export
  before a pack), duplicates, a schedule per export, Parquet for per-key work (whatever its order, §9), recovery
  from F3 and from a dead mount, that this is not Route A's captured samples; before a downgrade, finish
  or cancel runs with a file side and remove files targets (the older schema refuses them).
- **RELEASE** §5, §7, §9 step 5, §13, §15. **SAMPLING** §3. **GRAPH** §1. **LICENCE** §1. **BENCHMARK**
  §2. **PENTEST** §2, §8. `SECURITY.md`, README, PLAN.
- **`docs/architecture.md`:** §4.9, §8.5, Appendix C, Appendix D's DuckDB row.
- `deploy/config/targets.example.yaml`, `deploy/docker-compose.site.example.yml`, `examples/tables/files/`
  (a generator, a target, a spec, the expected result: the acceptance kit).

TABLES holds the feature as a section of its own, §14, with pointers from its goal, §1, §2, §6, §10 and
§12, as §13 holds the engines; its figures are §14.9, not §13. ENGINES §9's table gained no DuckDB column:
FILES §8 is the table of a file side's classes and renderings, and ENGINES §7 points to it. The acceptance
kit's generator writes side a's CSV with Python and side b's Parquet with the installed `duckdb`, and its
expected result is held by a test.

## 20. Limits and seams

**Limits.** Run creation reads every byte, a
CSV twice (three times when it declares a quote: U21's end check), within `files.plan_s`; a CSV file's rows
are parsed on one thread (U20); a CSV side whose quoted fields hold line ends is refused (P33), though its
rows are read exactly, where the end check's parallel reader starts a scan (every 8 MiB of a file) inside
such a field and the field's next line has the declared number of fields, or the record after the field
is not a row; an export written in place (its change time equal to its modification
time) is hashed again by every plan, `compare_one`'s too. A per-key group costs one pass over the files: a large CSV with many
differences takes hours, and one whose pass is over half the spec's total timeout is refused; prefer
Parquet (the order of its rows is no help to a per-key read, §9). A CSV key declared by `csv.keys` as an
integer or a decimal costs each pass and each sort its
key text's cast, which DuckDB's Linux aarch64 wheel was measured to make about seven times slower than
the macOS wheel §17's figures are of (a pass of 64 s for one of 10 s over 10M rows): a CSV side is
measured in the image before it is sized (§17). A changed export fails the run. One line that is not a row makes that side's
one-sided keys inconclusive and, on side a, costs the pack and readiness's population: an export with a
trailer record of another field count must be stripped, and a file cut exactly between two rows is not
seen (the stamp file is the control). A change that keeps size, both times and inode between two checks
is seen only by a closing hash, which per-key reads and a key-list run without a census never take;
coarse timestamps and mounts that keep no change time (some SMB and FUSE) widen that, and a mount
without stable inodes fails runs as F9 `replaced`. The closing hash reads the
bytes as they are when it runs, so it sees a change that stands and not one put back; and no pin holds a
directory, so a parent directory renamed away, another put in its place and both renamed back between
two checks changes nothing a check compares: the read in between took other bytes, the scan stores
`verified_x`, and the digest names bytes that were not read (§14 point 4). A recheck's kept observation
follows a stat check, as a
per-key read does, not a hash. In a mixed pair the service side is read key by key inside a group of 200,
paced by its target's rate: `execution.delay` follows the group, and a stop condition or a refused
credential is acted on once the group ends (the credential: at its first refusal). `settle_s` and `maxAge`
compare the mount's times with the host's clock. An instance that cannot start while a run reads (after a
restart) because its guard does not hold, or its spill directory cannot be made, fails that run by
the class's name, `Unstarted`: §15 words it only for a plan (P27, P28). A spill volume short of
`files.spill` plus 1 GiB is `TargetTempSpace: spill` then, which a per-key read waits for and a scan
fails with as before its sort (F3). The spill limit is checked, not
reserved: evidence writes on the same volume can still meet a full disk during a sort, and so can two
`ds` processes that sort at once on a volume sized for one. P32's projection is DuckDB's sort
as measured on 37 sorts of one version on one machine (§6 step 8, U27), with 15% added: a shape not
measured may spill more, and then fails with F3; texts that compress, and short ones, spill
half of it or less, so P32 refuses scans that would have fitted until `files.spill` is raised. A Parquet
side's worker columns are projected by their footers' uncompressed sizes, which are those of every row
whatever its filter selects, and are small for a dictionary-encoded column of few long values. Abandoned
reads are counted, not killed. Refused:
Windows-1252 and EBCDIC, group separators, implied decimals, fixed-width files, names outside the
alphabet, and Parquet columns whose names are not `columnName`s as keys, filters or listed columns.
Spill is plaintext. Parquet from other writers is a site's first test. **DuckDB keeps a Parquet file's
footer between statements** (its external file cache, on by default): a file rewritten in place at the
same size within the same second, or with its modification time put back, is read by the next statement
of the same process with the first file's footer and no error (observed on 1.5.5: a nanosecond column
read as microseconds), though a run created on it hashes the new bytes. The instance switches that cache off,
and the Parquet
reader's metadata cache, and proves both (U28, §10): a file is read by its own footer. Every Parquet
statement then reads its files' footers again; §17 has the figures measured with it off. What is left:
the pins compare size, times and inode, so the same replaced file still passes every stat check, and
only a closing hash sees its content (above). The constants (1,000 lines,
2 MiB, 64 rows, 30 s, 4 abandoned reads, 16 threads, the factor 2 of P31) are not measured; nor the 8
abandoned reads of all targets, the 8 creations that plan at once and the 30
s a call waits for a thread. A closed request does not cancel its plan (§10). A plan under way when the
instance is discarded finishes on its cursor, unless it asks for its first cursor at that moment or an
internal error invalidated the instance (P34).
While a discarded instance's sessions finish, the process holds two budgets (D14): DEPLOY's `files.memory`
plus 1 GiB does not cover that. `ds target check --count` of a compressed
export hashes every file, and refuses one still being written (P19). A footer's limits (16 MiB,
10,000 schema elements, 32 nested groups) are not measured either: a file past one is refused,
though DuckDB would read it. The walk refuses more that DuckDB would read: a footer whose fields do not ascend
by id, structs nested over 8 deep, bytes after the
footer's end, a file with an encrypted column where the plan reads only the others, and a list that
parquet.thrift as DuckDB 1.5.5 knows it does not declare, so a file of a later format version that adds
one (an upgrade of DuckDB must be followed by `manifest._FOOTER`). No footer of another writer than
DuckDB's and the tests' own has been walked. An INT96 timestamp needs `typeMap {as: timestamp}` or is left
out, and the files of a side must agree in each timestamp's unit. No key gets pruning, since statistics that
do not hold their group's values would lose a
key its own row (U30, §9): every per-key read of a Parquet side is a pass over the compared columns of
every row group, 0.3 s for a 10M-row file of four columns on §17's Mac, and more with the file's
size. A spec's filter is still pruned by statistics, in every
statement alike (§9). **A footer's counts are limits too**: a file of over 10,000 row groups or 200,000
column chunks (past 1.2 billion rows of DuckDB's default row groups; past 2,000 columns in 100 row groups)
is refused though DuckDB would read it, as is one whose row groups hold other column chunks than its
schema's leaves, in another order, or a group of no column at all; a footer the walk accepts may still
make the reader hold some hundred MiB outside `files.memory`, about 2.5 footers a thread of
`files.threads` at a time (U31). **A crafted
page still ends the process** (§14 point 7 names one): nothing but a reader in a process of its own
would contain what DuckDB's decoders do. **By READER.md that reader is built:** such a page ends the
reader's process and no other, its run pauses and, at the third such end while its files alone were
being read, fails with F12; READER §17 lists what the reader's process itself leaves (no sandbox; no memory
limit and no denied sockets on macOS; an end costs every statement in flight in that reader its pass;
charges last until `ds` restarts and are made for the kernel's memory killer, the data limit and an
operator's kill as for a fault; readers apart and retired ones hold budgets of their own beside the
shared one's). A `TIME` outside a day reads as a value that is no time, a
`DECIMAL` whose scale is over its precision is refused, and P31 advises no file sorted by the
key. A database side's
snapshot is open while the file side of its scan sorts. The UI's line of a fixed side does not say the
lines `csv.skip` left unread; the census wording and the certificate do.

**Seams.** Object storage as a guarded target DeltaSleuth's own client fetches; `csv.trailer` with the
trailer's byte position observed against the file's end; a pack for side a that says `side a NOT
observed whole`; plans started through the worker; descriptor-pinned reads; a subprocess reader (built:
[READER.md](READER.md), whose §17 names its own seams); a
sorted key cache for per-key reads; flattened nested columns; a stale-export label (the same bytes as an
earlier run); a file as a mixed pair's key source (built: §12's key file); pre-check figures over pinned
bytes (§12); a cheaper
key text for a `csv.keys` integer or decimal whose digits fit a narrower type (`TRY_CAST` to `BIGINT`
or `DECIMAL(18,scale)` first, `DECIMAL(38,scale)` only for longer values), to be shown to give the same
key texts (§17's Linux figures).
