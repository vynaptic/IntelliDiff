# The file reader in a process of its own: contract

This contract is binding. It hardens the files engine as built ([FILES.md](FILES.md)), builds
FILES §20's seam "a subprocess reader", and amends the documents of §16. It weakens no guarantee FILES
gives; where a FILES rule changes, the section says how. It is filed as `docs/mvp/READER.md`: its name
claims nothing §2 denies. FILES.md, amended in place, stays the one contract of the file reader and
names this one for the protocol (§6) and the accounting (§9).

**Goal:** a fault or a memory blow-up in DuckDB ends a child process only. The worker and the API
survive, a new child starts, the run pauses or fails with a closed code (no DuckDB text, no file name),
and files that keep ending the reader, or that it must be killed for, are read no more.
**Shape:** one new module, `deltasleuth/connectors/tables/reader.py` (standard library and `duckdb`
alone: the frame codec, the child's restrictions, today's `_connect`, `_prove` and `_select` moved, the
child's loop). `duck.py` keeps `Cursor`, `_Session`, `Files`, `check` and `selftest`; its `Instance`
becomes the handle of a child. One error class (`engine.ReaderDied`), one setting (`files.os_limits`).
`scan.py`, `worker.py` and `connector.py` do not change. No migration, route, role, audit action, UI
change, dependency, licence fence or outbound destination.
**New guarantee:** no `ds` process imports `duckdb`; only its reader does.

**Observed** (2026-10-04, by scripts that are not in the repository):
- macOS arm64, DuckDB 1.5.5. The kept mutant (`PAGE` of `tests/support/crafted.py`, 320 bytes, four
  rows of `id BIGINT, v VARCHAR`) passes `parquet_schema`, `count(*)`, `LIMIT 0` and `SELECT id`; `SELECT
  v` and `SELECT *` end a process of their own with SIGBUS or SIGSEGV. A child started as §5 starts and
  connects in 75 ms, imports no `deltasleuth` package and inherits no descriptor. `setrlimit` is
  refused for `RLIMIT_AS`, `RLIMIT_DATA` and `RLIMIT_RSS`.
- Linux aarch64 (kernel 6.12), the local `deltasleuth:source` image under the compose hardening
  (read-only root, uid 10001, `cap_drop ALL`, `no-new-privileges`, noexec `/tmp`; no network, nothing
  pulled), **Python only**:
  - a child starts in 11 ms; SIGSEGV sent to it reads back as -11; a child blocked on its input is gone
    once its parent is SIGKILLed; its listing of `/proc/self/fd` shows 0, 1, 2 and the listing's own;
  - `RLIMIT_DATA`: the interpreter starts with 5 MiB of data. With the soft limit at 64 MiB a private
    anonymous mapping of 128 MiB is refused (ENOMEM) and a shared one is made; with the soft limit
    raised to 5,296 MiB the private one is made and one of the limit plus a page refused; the hard
    limit cannot be raised again. Under a limit of 1 GiB allocations end in `MemoryError` at 852 MiB
    beside 20 threads, whose stacks it counts: 24 threads add 195 MiB. A thread is started and joined
    in 56 µs. `RLIMIT_AS` refused the fifteenth thread: not used;
  - `oom_score_adj` takes 1000 before `PR_SET_DUMPABLE 0`, not after. In a container of 256 MiB
    (`docker run --memory`) a child at 1000 that outgrows it is killed (SIGKILL) and its parent, holding
    150 MiB, lives; without the adjustment the parent is taken and the container exits 137;
  - §7's filter answers `socket` and `socketpair` with EPERM, in later threads and started processes
    too, while files and pipes work.
- §7's filter program, interpreted in Python over 20 pairs of architecture and call number, decides as
  §7 says for aarch64, x86_64 (x32 numbers too) and two other architectures. No kernel was asked.
- **Not observed:** DuckDB on Linux in any form (that image predates it), an x86_64 kernel, emulation: §3.
- **Observed since** (2026-10-04): DuckDB on linux/arm64, natively, in an image built
  from the working tree, by §3's image tests, all of which pass; the same image tests in an amd64 image
  under emulation (Rosetta), with what emulation changes; §14's figures; the fuzz. **Still not observed:
  an x86_64 kernel.**

## 1. Decisions (each one seam)

- **R1 One shared reader a `ds` process**, started at the first file read, for plans and runs alike
  (D9). No child a statement: eight plans at once would be eight budgets, and a start is a quarter of a
  0.3 s per-key group.
- **R2 Started by `subprocess.Popen`**, never `fork` without `exec` (the parent holds threads, an event
  loop, pool connections and the keyring) and never `multiprocessing` (pickle).
- **R3 A dumb child:** one `SELECT`, rows back. Shaping (`compose.found`, `digested`, `stratified`)
  stays in the parent's pool thread.
- **R4 Typed frames, no pickle, no marshal**, every message bounded and every reply checked (§6).
- **R5 A hung statement retires its process, which is killed with its last cursor** (§8): FILES §10's
  "a session that holds one of its cursors finishes on it" stands. On a mount that answers, the kill is
  accounted as a death (§9).
- **R6 An end is charged to a file set only when it alone was in flight.** Sets in flight together are
  read apart next, in a reader of their task's own; three charges condemn a set until restart (§9).
- **R7 Linux: `RLIMIT_DATA`** of `files.memory` + 2 GiB + 600 MiB × `files.threads` (5,296 MiB by
  default: U31's footers, DEPLOY's 1 GiB, 24 request stacks, one batch), **proved at every start**
  (§7); `oom_score_adj` 1000; no core dump.
- **R8 Linux: sockets denied by a seccomp filter**, built last, proved at every
  start. Its x86_64 arm is judged by an interpreter in the suite (U36) and first loaded by a kernel on
  a site's host. A machine whose gate fails leaves `reader.FILTERED`, and the documents say so.
- **R9 One batch read ahead in the child, built only if U38 fails** (§14); never in a reader of a
  task's own. **Built:** the first case passed its line without it (§14).
- **R10 No confidentiality claim** (§2): the child runs as the same user.
- **R11 The child runs each request on a thread started for it:** an idle cursor holds none (§5).

## 2. What is never claimed

`manifest.NOT` gains `isolat|confin`; `test_never_told` searches `reader.py` too. Never claimed:
- that the reader's process is a sandbox, isolated or confined: it runs as uid 10001, can open what
  that user can (secret files, settings, the evidence's ciphertext, other roots) and signal its parent;
- that the process holding the keyring parses no hostile byte: the Parquet footer walk and the zlib and
  zstandard stream checks (`manifest._Stream`) still read a file's bytes there;
- that a crafted file is harmless: it can end the reader, cost other runs a pass, fail its own run, and
  have a filtered read take other rows (FILES §14 point 7);
- that rows of the right form are the files' rows, should a crafted file run code in the reader: the
  parent checks an answer's form and bounds what it builds of it (§6), never its truth;
- that the files of a run failed with F12 are at fault: F12 says what was observed (§12);
- that the directory-swap gap (FILES §14 point 4) is closed, or DuckDB's decoders audited;
- a memory limit or denied sockets on macOS, or on any host whose start line does not print them: the
  line prints only what that start saw hold (§7, §11).

## 3. Release gates (FILES §3 gains U32 to U38; one that fails stops the release until this contract is revised, U36 excepted by R8)

"Image": a test of `tests/e2e/test_reader_image.py`, in the image under the compose hardening, on
Linux natively (the harness below). "Host": a pytest test where the suite runs.

| | Behaviour | Test |
|---|---|---|
| U32 | the child starts with an empty environment, proves its guard and its limits, reads and spills | image `test_the_self_test_passes`; host `test_the_reader_is_started_with_nothing` |
| U33 | SIGSEGV sent mid-statement is `Died`, the parent lives, a new reader reads (on Linux, and in the image whatever the host; on macOS, where a fault is a crash report, the host test sends it only with `DS_FAULTS=1`, and SIGKILL otherwise: §15). What the kept mutant does on Linux (a death, a closed refusal or a read) is recorded in FILES §14: each passes | host and image `test_a_fault_ends_the_reader_s_process_alone`; image `test_the_crafted_page_is_contained` |
| U34 | no reader outlives its parent, SIGKILL mid-sort included; a `ds` process that exits reaps its readers first, so a command run beside a container's first process leaves no zombie entry | host and image `test_no_reader_outlives_its_parent`; host `test_a_process_reaps_its_reader_as_it_exits`; image `test_a_command_leaves_no_reader_behind` |
| U35 | the data limit under DuckDB's allocator: a statement holding twice a small limit does not complete; 200 idle cursors add no thread to the child (every platform), and under a data limit of 1 GiB, which 200 stacks alone would pass, the self-test's sort completes beside them; a sort of 10M rows of §17's shape under the default settings completes (the reader's peak `VmData` recorded) | the self-test's step 3; host and image `test_idle_cursors_hold_no_thread`; image `test_the_default_limit_fits_ten_million_rows` |
| U36 | the filter denies sockets and stops nothing the self-test does, natively on aarch64; its program decides for aarch64 and x86_64 as §7 says | the self-test; host `test_the_socket_filter_decides_by_architecture` |
| U37 | no core with DuckDB loaded; in a container of 512 MiB the kernel takes the reader, the statement is `Died (SIGKILL)` and the parent exits 0 | image `test_the_reader_leaves_no_core`, `test_the_kernel_takes_the_reader_first` |
| U38 | the boundary's cost (§14), on the build's Mac | the nine `DS_PERF` tests |

**The harness.** The image holds no pytest, no tests and no pip, and nothing is installed in it. The
image tests run on the host, only with `DS_IMAGE=<image>` set (skipped otherwise). Each is one `docker
run --rm --pull never --network none` of that image with `release.HARDENED`, a settings directory and
an evidence volume as `release._smoke` mounts them, and a volume at `/var/lib/deltasleuth/files` for
files a test writes; it runs one `python -c` text against the product's own `duck` and `reader` (the
mutant travels in the text) and prints one line, which the test asserts. The mutant's line is
recorded, never judged. `test_the_kernel_takes_the_reader_first` adds `--memory 512m --memory-swap
512m`: a cgroup's limit can only be set from outside the container.

**As built:** each text also gets `--platform linux/<the image's architecture>`; an image
of another machine than the host's runs under emulation, where its settings turn `files.os_limits` off,
the self-test runs without its limits and `test_the_default_limit_fits_ten_million_rows` is skipped.

**The rebuild.** The image tests need an image built from the working tree. That build
downloads: every Linux wheel `uv.lock` pins for the machine (DuckDB's manylinux wheel among them, run
for the first time), `uv` through pip (`deploy/uv-requirements.txt`), the npm packages when that layer
is not cached, and a base image where its digest is not local. The image is tagged
`deltasleuth:reader`, never `deltasleuth:source` or `deltasleuth:latest`, which a running stack
may use; the image tests take it as `DS_IMAGE=deltasleuth:reader`. An amd64 image is built with
`--build-arg PYTHON=python@sha256:<release.BASES>`, as `deltasleuth:reader-amd64`.

## 4. Setting (`config.FilesSettings`)

`files.os_limits: true` (boolean). False: the reader sets no data limit and no socket filter, for a
host where one stops it; the start line and `ds target check` say so. X1 becomes (`SettingsError`, exit
2): `files.memory and files.spill are sizes such as 2GB (MB, GB or TB); threads is 1 to 64, max_files 1
to 20,000, plan_s 10 to 86,400; spill_dir and roots_under are absolute paths; os_limits is true or
false`. `deploy/config/deltasleuth.example.yaml` holds the commented line.

## 5. Process model (`duck.Instance`, `duck._started`, `reader.main`)

**The parent's `Instance`** holds `proc`, `spill`, `version`, `held` (its open cursors), `dropped`, the
limit and socket states, the pending requests (id to reply slot and file-set key) and three daemon
threads: a writer draining a queue, so nothing of the parent blocks writing to a stopped child; a
reader that completes slots; and one that waits for the child's process, reaps it (`ds serve` is PID 1
in the image), kills what is left of its process group, lets the reader read what the child wrote before
its end (`DRAIN_S = 1`), then fails what is pending, removes its spill directory and does §9's
accounting. **The end is the process's, not the end of its output:** a process
the child started holds that output open. A kill is of the child's process group, which it leads. A `ds`
process that exits kills and reaps the readers it still has (`atexit`).

**`duck._started(settings, roots)`**, on `manifest.POOL`, one build whoever asks, under a deadline of
`START_S + STAT_S` (50 s: the start and its file work):
1. `manifest.sweep` (best effort), then `manifest.held(spill_dir)`: the parent makes and flocks the
   directory (P28's first text).
2. `Popen([sys.executable, "-I", "-X", "utf8", reader.__file__], stdin=PIPE, stdout=PIPE,
   stderr=DEVNULL, env={}, close_fds=True, start_new_session=True, cwd=<spill>)`. Run by path, the
   child imports no `deltasleuth` package; stderr is discarded because DuckDB's text quotes paths and
   values; a session of its own keeps a terminal's Ctrl-C from ending it as a counted death.
3. The first frame: roots, spill, memory, spill limit, threads, the data limit and whether to filter
   (null and false with `os_limits` off), `FRAME_MAX`, the probe path.
4. `ready` or `unstarted` within `START_S = 20`; then the free-space check (today's tail of `_prove`,
   P28's second text) in the parent.
- A process that cannot be started, ends first, does not answer, answers outside the protocol or
  misses the deadline is killed and is `Unstarted('process')`: P27's second text, never P14's. **A
  failed start releases the spill directory it made** (today it stays held until the process ends).
- **A reader no longer current is retired** (§8: no cursor more, killed when its last cursor closes, at
  once when it has none): one replaced because the roots or the budget changed (tests do it at every
  `ducked.registry`; an in-process connection was collected, a process is not), and one whose start
  ended after its caller had given up.

**The child:**
1. counts the descriptors it holds beyond 0, 1, 2 and its listing's own; duplicates 0 and 1 for the
   frames, then points 0 at `/dev/null` and 1 at 2, so a stray native write forges no frame;
2. reads the first frame; `restrict()` (§7); only then `import duckdb`;
3. `connect()` and `prove()`, moved statement for statement; answers `{ready, version, limit, sockets,
   inherited}` or `{unstarted, names}` (codes: the parent words P27's log from its own constants). An
   inherited descriptor is logged as a warning and asserted absent natively (U32); it is not fatal,
   the count being unobserved under emulation, where the emulator may hold its own;
4. its main thread only reads frames: `ping` is answered at once, and every other request runs on a
   thread started for it, its cursor found by number (R11); `close` closes a cursor once its request in
   flight has returned. **`interrupt` stops the request it was sent for:** the
   cursor's request that runs is interrupted every 10 ms, by a thread of its own, until it has
   returned (DuckDB forgets an interrupt that reaches it before it began the statement), and a request
   read before the interrupt that has not begun is answered `InterruptException` without being run.
   Threads follow what is in flight, not what is open: at most 16 requests (the parent's pool) beside
   `STUCK_ALL = 8` given up, 24 stacks, and one more for each of them that is interrupted and has not
   returned. The end of input is `os._exit(0)`, mid-statement too;
5. an uncaught exception of its own Python ends it with exit 1 after a last frame `{fault: <class>,
   line: <n>}`.

**Each parent has its own reader, suspects and charges:** `ds serve`, `ds worker`, `ds serve
--no-worker` (plans and `compare_one` there), `ds run`, `ds compare`, `ds target check`, the
self-test. `files.*` stay budgets of each process that runs `ds`.

## 6. Protocol (`reader.pack`, `unpack`, `pack_rows`, `unpack_rows`)

**Frame:** a u32 length, one kind byte, the body; integers little-endian.

**Parent to child**, kind `J` (a JSON object), at most `REQUEST_MAX = 64 MiB`:
- `{id, op: "execute", cursor, statement, binds}`; a bind is `[tag, value]`: `n` null, `s` text, `b`
  bytes in hexadecimal, `i` integer, `o` boolean, `d` a Decimal's text, `l` a list of texts, `m` a map
  of text to text: every type `_reader`, `_bind`, `_keyed` and `_drafted` bind today.
- `{id, op: "fetch", cursor, rows, step, bytes}`: today's batch loop, `step` rows a `fetchmany` until
  `rows` rows or `bytes` of their texts and bytes. `Cursor.fetch(size)` sends `rows` and `step` as
  `size`. `duck.BATCH`, `BATCH_BYTES` and `STEP` travel here, so tests that patch them still work. With
  R9 `Cursor.batch` adds `ahead`, true in a reader that reads ahead: the child then reads the
  next batch once this one was answered with `rows` rows or more.
- `{id, op: "columns", cursor}`, `{id, op: "ping"}`; `{op: "interrupt", cursor}` and `{op: "close",
  cursor}` take no reply. A cursor is a number the parent allots.

**Child to parent:**
- `J`, at most `ANSWER_MAX = 16 MiB`: `{id, ok}`, `{id, columns: [[name, type]]}`, `{id, error: <name>,
  kind}`, kind one of `memory`, `interrupt`, `denied`, `io`, `fatal`, `select`, `other`. A name is one
  of `reader.NAMES` (the locked DuckDB's 32 exception classes, `MemoryError`, `OSError`,
  `PermissionError`, `Unframed`, `Oversize`); the child sends `Error` for any other. **An answer that
  would pass `ANSWER_MAX`** (the columns of a footer whose names are that long) is answered `Oversize`
  instead: a refusal (P29 at plan), no death.
- `R`, at most `FRAME_MAX = 128 MiB`: `<u32 id><u32 rows><u16 columns>`, then each column: a tag, and
  for `n` (all NULL) nothing; for `o` (boolean) a byte a row (0, 1, 2 for NULL); for `s` (text), `b`
  (bytes) and `i` (an integer's decimal text) `<u32 blob bytes>`, an i32 length a row (-1 for NULL; of
  `s` and `i` in characters), the values joined (UTF-8).

**A batch is never refused for its size, only a row:** a batch whose frame would pass `FRAME_MAX`
crosses as its longest prefix of whole rows that fits, and the child keeps the rest for that cursor's
next `fetch`. A row that alone passes `FRAME_MAX` is `Oversize`. A frame of no rows is the end.
**Nor for its width:** a batch holds `reader.CELLS = 2^20` cells (rows times
columns) at most, so the child closes one there as it closes one at `rows` or `bytes`, a step's rows
past the cells kept for the next `fetch`: a statement of over 104 columns crosses in batches of fewer
than 10,000 rows, one row at least, and nothing of it is read ahead.

**Refused in the child:** a value that is not None, a boolean, an integer, text or bytes (`Unframed`).
No statement DeltaSleuth composes selects one: `duck._SNIFF` selects `unnest(Columns, recursive :=
true)` (observed: scalar rows) and `_draft` rebuilds the list.

**The parent checks** each reply: length, kind, a pending id of the right request, each member's type,
no more rows than the `rows + step` asked, known tags, lengths of -1 or more that sum to the blob,
strict UTF-8, no byte left over, a name of `NAMES`. **And, before a row is built:**
no more than `CELLS` cells, whatever the frame's bytes say (a column that is all NULL is one
byte: 400 rows of 65,535 such columns, a frame of 65 KiB, were 408 MiB of lists and tuples in the
process that holds the keyring, as observed, and the 20,000 rows a fetch allows about 20 GiB); rows of
the statement's width, which is its first frame's or its `columns`' count; and, for a statement whose
values a plan keeps or words (its counts, the rejected lines, the footers' schema, `ds target check`'s
sniff), each column under the tag the statement must answer (`Cursor.fetch(tags=…)`), or all NULL.

**Free text** is taken from the child by a grammar of the parent's own, and in no other way: a
`fault`'s class only as `[A-Za-z_][A-Za-z0-9_]{0,63}` with an integer line; a `ready`'s version only as
digits and dots (32 at most); **a column's type** (`columns`, and `parquet_schema`'s `duckdb_type`) as
one the parent reads (a name of `duck._CLASSES`, a `DECIMAL(p,s)`), else as `LIST` or `ARRAY` for one
that ends in `]`, else by its head alone, an upper-case word of 32 characters at most
(`"?[A-Z][A-Z0-9_ ]{0,31}`, then nothing or its parameters: `STRUCT`, `MAP`, `UNION`, `ENUM`): what
follows the head holds a file's text (a struct's field names, an enum's values) and is neither kept nor
worded. **A column's name** is a file's text and has no grammar: wherever a message names one it is
quoted (`repr`; in a typeMap a refusal suggests, as JSON unless it is a plain word), and `ds target
check`, which alone prints a file's names, prints their printable characters; so it prints its draft's
sniffed dialect (a delimiter, a quote, an escape, a date's format: a file's text too, written as JSON
on the command's own output and nowhere else). A rejected line's file is
one of the side's pinned files, by its number. Anything else, and any exception raised while a reply is
decoded (`RecursionError` among them), is the child's death with cause `reply`: it is killed. A reply to
a request the parent gave up completes its slot (§8) and is dropped.

**`duck._error(kind, name, spill)`** maps as today, by kind: `memory` `TargetTempSpace`; `interrupt`
`StatementTimeout`; `denied` `TargetDenied`; `io` `TargetTempSpace` when `_free(spill) < LOW_SPACE`,
else `TargetRefused`; `fatal` discards the instance; `select` is `TargetRefused('not one SELECT')`. No
DuckDB text crosses the pipe.

## 7. What the child may do, and what stays in the parent

**The same instance as FILES §10:** one `SELECT` a call by `extract_statements`, `allowed_directories`,
external access off, the configuration locked, the three caches off, nothing installs or loads, the
guard proved before `ready`.

**`reader.restrict`, before DuckDB loads; each step best effort, and reported in `ready` as held only
when this start saw it hold:**
1. `RLIMIT_CORE` 0.
2. Linux: `oom_score_adj` 1000, then `prctl(PR_SET_DUMPABLE, 0)`, in that order. A core would hold
   rows in clear; a piped `core_pattern` ignores the core limit, not this.
3. Linux, `os_limits`: `RLIMIT_DATA`, the hard limit at R7's figure and the soft one first at `PROBE =
   64 MiB`: a private anonymous mapping of twice `PROBE` (`MAP_PRIVATE | MAP_ANONYMOUS`; Python's
   `mmap` is shared by default, which the limit does not count), never touched, must be refused with
   ENOMEM. Then the soft limit is raised to the hard one and the same mapping must be made. Both seen:
   held. Else `no data limit` (a kernel before 4.7 counts `brk` alone; one booted with
   `ignore_rlimit_data` only warns). Proved at 64 MiB and not at the limit: a mapping of the limit
   itself may be refused for the host's own memory, with nothing limited.
4. Linux, `os_limits`, a machine of `reader.FILTERED` (`aarch64`, `x86_64`): `PR_SET_NO_NEW_PRIVS`,
   then one seccomp program of 15 instructions for both architectures. EPERM for a call of aarch64
   numbered 198, 199 or 425 (`socket`, `socketpair`, `io_uring_setup`), for one of x86_64 numbered 41,
   53 or 425 or at or over 0x40000000 (x32), and for any call of another architecture; every other
   call is allowed. Held when `socket.socket()` raises `PermissionError`. One program for both, so a
   translated process is judged by the architecture the kernel sees.

None of it needs a capability, a writable root or an executable `/tmp`: the compose hardening stands.
On macOS (development) only step 1 holds: a fault and the parent's end still end the child alone, but
there is no data limit and sockets are not denied.

**In the parent, before the child is handed a file:** T10 and the path grammar, expansion and refused
links (`manifest.expand`, `_walk`), pins, hashes and the compressed streams' check (`pin`, `_digest`),
the footer walk (`_footer`, `footed`), every stat and closing check (`verify`), and `duck._reader`'s
refusal of an unpinned path or a pattern. They stay on `manifest.POOL` with `STAT_S`, `STUCK_MAX` and
`STUCK_ALL`: kernel calls in the parent can still hang.

## 8. Deadlines and cancellation (`duck.Cursor._call`)

**Kept:** a call is a pool job whose deadline counts from when a thread took it (the job writes the
request, waits for its slot, then decodes and shapes); `Busy('Busy')` after `STAT_S` without a thread;
`PLANS = 8`; the watch every `WATCH_S`; `duck.SORT`; interrupt at the timeout (U10) and `GRACE_S =
10`; `_Session.renewed`; an internal or fatal error discards the instance, whose other cursors'
statements are `Busy('Dropped')`.

**Changed:**
- Past the grace the parent answers the waiting job itself (`TargetUnreachable('Deadline')`), closes the
  statement's cursor and calls `discard`. The process is **retired** (no cursor more; sessions holding
  its cursors finish) and **killed when its last cursor closes**: at once where the statement was alone.
  §9 accounts the kill; where it was the set's third charge, the set's next statement is `ReaderDied`,
  before anything is sent.
- The statement given up is counted for its target's root, as today, **until it answers or its process
  ends, whichever comes first**: `manifest._abandoned` is handed the request's slot in place of the
  pool's call, and that slot is completed at the late reply, by the parent's thread that reads the
  child's answers, or at the child's end, by the thread that waits for its process (§5). A statement slow to honour its interrupt, on a retired reader that another
  session's cursor keeps for hours, frees its count when it returns, as FILES D14 has it; P28's third
  text stays true. A killed process not reaped within `REAP_S = 10` (uninterruptible I/O on a dead
  mount) is logged, and its statements stay counted until it is: `STUCK_MAX` and `STUCK_ALL` bound
  such readers as they bounded hung threads.
- A watch also asks the process for a sign of life (`ping`). None within `STAT_S`: it answers nobody
  and is killed at once (§9); a scan has no timeout, and a frozen child would otherwise keep
  `duck.SORT`.
- A cursor closed while its statement had not answered sends `interrupt`, then `close`; only when no
  answer comes within `GRACE_S` is its process retired and the read counted. The child makes the
  interrupt stick (§5 step 4): one that reaches it while DuckDB still prepares the statement, or
  before the request's thread began, ends the statement all the same.
- **A kill is the process group's** (`os.killpg`: the child leads a session of its own). `Instance.end`
  kills only a process this parent has not reaped: its number is then its own. The thread that reaps
  it kills what is left of the group right after the reaping (§5): the number stays the group's while
  one of it lives, and otherwise the call finds nothing, unless the kernel handed the number out again
  between the two calls, which nothing guards against.

**Removed as needless:**
- the pool thread a hung statement kept for ever (its slot is answered);
- `_taken` and `_returned` around every call (`held` counts cursors);
- `Cursor.close`'s pool call and deadline (a frame, no wait), and the pool call that made a cursor;
- `discard`'s pool job for `manifest.release` (the thread that makes a reader's end releases its spill:
  the one that waits for its process);
- the four `import duckdb` of `duck.py`;
- D14's "a statement hung in the kernel keeps them until it returns or the process restarts": a
  retired reader, its memory, threads and spill end with its last cursor, without a restart.

## 9. Ends and their accounting (`duck._ended`, `duck._Session._opened(side)`, `Files.rows`)

**The key** of a session is the SHA-256 of its side's pinned files and pins, in memory only (`ds
target check`'s pins, which hold no digest, key too).

**Accounted:** a child's end the parent did not cause, and a kill for a deadline (§8: a statement
given up past its grace, whose reader's other statements finish; a `ping` unanswered). K is the keys
with a request pending in that reader then (with R9, or a batch being read ahead); if none, the keys
of its cursors whose statement is not read to its end; if none, nobody.
- **K of one key: it is charged.** A kill is charged only when a stat of that key's root answers
  within `STAT_S`: a dead mount stays an outage and is never a charge.
- **K of several: each is suspect.**
- Where the process ended, every request pending in it, or sent to it later, is
  `manifest.Busy('Died')`. The request whose key reached `DEATHS = 3` charges is `ReaderDied`.
- Every cause counts alike: a signal, an exit, `reply`, a kill for a deadline.

**A suspect or charged key is read apart:** its cursor opens in a reader of its task's own, started as
§5 and killed when that task's cursors in it are closed. Every cursor the task opens meanwhile opens
there too, so the file sides of one pass or one plan, both suspect or both reading the same files,
share it. One task has one request pending at a time and nothing is read ahead there, so an end
during a request has a K of one key and is charged.
- **A pass whose reader ended before its sort was sent** (between its cursor's opening and its check
  of the spill's room, which reads the reader's own directory) is `Died`, not `TargetTempSpace`: the
  directory went with the process.
- **A task that holds `duck.SORT`** (a scan pass, a census, a frame) has the one such reader `SORT`
  allows a process and waits for nothing more. A scan pass learns of a suspect or charged side when
  its sides' catalogues are read (`Files.describe`, V2), before either cursor opens: both its file
  sides then read apart, and the pair needs no second budget of its own.
- **Any other task** (a per-key read, a plan, `ds target check`) takes `duck.ALONE`, one a loop, held by
  task as `SORT` is, until its cursors there are closed; a wait over `STAT_S` is `Busy('Alone')`. A
  per-key reader closes a cursor it opened apart after each read. It never waits for a pass.
- **Cleared:** when a task's cursors there close with that reader alive, its keys are suspect no more.
  A charged key is read apart until the process restarts.

**A condemned key** (three charges) raises `ReaderDied` before anything is sent, until the process
restarts. Both maps hold `manifest.CACHE_MAX` keys.

**What it costs, and whom:**
- A file set is charged three times at most, each time with it alone in flight; then no statement of
  it is sent.
- A run whose statement was in flight beside others when the shared reader ended loses that pass (a
  scan sorts again from its checkpoint; a per-key group is read once more) and is read apart once,
  after at most one `STAT_S` wait for `ALONE`. Nothing of it lasts.
- A set that ended the shared reader beside others reads apart next, so it ends the shared reader
  again only after a clean read apart (a new run's plan between two of its passes). Nothing bounds
  those, as nothing bounds new file sets (§17).
- A key that was alone in flight when the reader ended for another cause (the kernel, an operator, the
  data limit) is charged: it reads apart until restart (a start a per-key group, about 0.1 s; a budget
  of its own beside the shared reader's), and three such ends fail its run with F12, which names the
  cause and blames no file.
- A file-to-file pass whose reader apart is ended between two requests, each time, is paused each time
  and never charged (K is then its two keys): no statement ran, the cause lies outside the files, the
  log names it, and nothing bounds it.

| Reader | `Died`, `Alone`, `Unstarted` | `ReaderDied` |
|---|---|---|
| Scan (`Worker._scan`) | paused, resumed | `fail_run` |
| Per key (`TableConnector._query`) | `connect_error` with the code, a group read once more | propagates: `fail_run` |
| Census | `{failed: Busy}` | `{failed: ReaderDied}`; the next per-key read fails the run |
| Draw | postponed | `fail_run` |
| Plan (`duck._planning`) | P34 | P35 |
| `ds target check` | its own text (§12) | — |

- `ReaderDied` is a `TableError` in none of `UNREACHABLE`, `STALLS` and `drift.DENIED`.
- `Files.probe`'s first statement words a `TargetUnreachable` or `ReaderDied` through `_planning`
  (today `plan.probed` says `the database refuses the filter or key map`).
- A reader that cannot start while a run reads (`Unstarted('process')`) is `Busy('Unstarted')`: an
  outage with a code of its own, not `Died`. A guard that does not hold still fails the run as
  `Unstarted` (FILES §20).

## 10. Budget, sort, spill

- Cursors of a reader share its memory, threads and spill limit (U11). `duck.SORT` is the parent's:
  one sort at a time across readers.
- **D9 and D14 as amended:** beside the shared reader there may stand one pass's own (`SORT`), one of
  a per-key read or a plan (`ALONE`) and retired ones (four by one root's hung statements, as today),
  each with its own `files.memory`, threads, spill limit and data limit, for as long as those reads
  last. DEPLOY sizes a container for the shared reader and says what each further one may add.
- **Spill:** the parent's flocked directory. A reader that ends: removed once it is reaped, before the
  next start counts free space. A parent that ends, SIGKILL too: the kernel frees the lock, the child
  exits at the end of its input, and the next sweep removes it, as today.

## 11. `ds target check`, the self-test, the release

**Two phrases, independent, used by every line that reports the limits:** `data limit 5,296 MiB` or `no
data limit (<why>)`; `sockets denied` or `sockets not denied (<why>)`. `<why>` is one of `files.os_limits
is false`, `not on this platform` (macOS; for sockets, a machine outside `FILTERED`) and `not held on
this host` (tried, and this start did not see it hold).

**`ds target check`** prints `target legacy-exports: files, read-only, duckdb 1.5.5, parquet and
core_functions built in, no extension loads, read in a process of its own (data limit 5,296 MiB,
sockets denied)`, each phrase as its reader's start reported it.

**`duck.selftest(settings=None, *, limits=True, fault=signal.SIGSEGV)`**, under `HARDENED` as today;
without `limits` its readers start with `os_limits` off:
1. starts a reader, proves it, reads both files, sees the sort spill (the parent watches `temp/`);
2. sends it `fault`, SIGSEGV (the release and DEPLOY's item pass none; a test on macOS, where a fault
   is a crash report, passes SIGKILL): the statement is `Died`, a new reader reads again, the first
   one's process and directory are gone (no session, no key: nothing is charged);
3. with `limits`, on Linux: a reader with 2 GiB of memory and a data limit of 512 MiB is sent a
   statement that holds about 1 GiB, which must not complete; step 1's `ready` must report the limit
   held and, on a machine of `FILTERED`, sockets denied, else the failure names which did not and why,
   by §11's two phrases: `the reader's limits did not hold: no data limit (not held on this host),
   sockets denied`;
4. prints `file reader: duckdb 1.5.5 in a process of its own, replaced when it ended; data limit held,
   sockets denied`, each half else as its phrase above; without `limits`, `…; limits not tried`.

`release.DUCK` passes `limits=False` when the built platform's machine (`release.MACHINES`) is not the
build host's (`os.uname().machine`, `arm64` read as `aarch64`): amd64 is smoke-tested under emulation
(RELEASE §15), where a limit would count the emulator and a filter judge it. Failure text: `the smoke
test failed: DuckDB does not load, its guard does not hold, it cannot read and spill, its process is
not replaced when it ends, or its limits do not hold, under the compose hardening`. The image and the
compose files do not change.

**On a site's host** the self-test, run once with `docker compose run --rm app python -c "<DUCK>"`,
is DEPLOY's acceptance item: an x86_64 kernel first loads the filter there, and DEPLOY says so.

## 12. Messages (binding; each asserted; none holds a value, a file name or DuckDB's text)

FILES §15's other texts stand, P27's first and P28's among them. A cause is `SIGBUS` (a signal's name;
`signal 35` for one without), `exit 1`, `reply` or `killed after its deadline`: the last accounted
end's. The texts say what was seen, an end while only that side's files were being read, and no more.

**Run creation** (422 from `POST /v1/runs` and `POST /v1/compare`; `ds run`, `ds compare` and `ds
target check`, which says `target <id>`, exit 2):
- P27, second text: `side a: the file reader did not start: its process did not start or did not
  answer (see the server log)`.
- P34 gains the codes `Died`, `Alone` and `Unstarted` (`NotReady`, `planning`: a firing tries again):
  `side a: the file reader did not take its files' work (Died): try again`.
- P35: `side a: the file reader's process ended 3 times (SIGBUS) while only this side's files were
  being read: they are read no more until DeltaSleuth restarts`.
- `ds target check`, at its first end: `target legacy-exports: the file reader's process ended (SIGBUS)
  while it read these files`.

**Run time** (the run's `error`; `ds run` exits 2; alert `run.failed`, `error_type` `ReaderDied`):
- F12: `ReaderDied: side a: the file reader's process ended 3 times (SIGBUS) while only this side's
  files were being read: they are read no more until DeltaSleuth restarts`.
- F1 gains `scan of run R-… paused: Died` (and `Alone`, `Unstarted`); F3 `TargetTempSpace:
  MemoryError`; F4 `TargetRefused: Oversize` and `TargetRefused: Unframed`.

**Logs** (`deltasleuth.files`):
- warning, whatever held, for the process's reader (no `ds` process configures
  this log, so each prints what the interpreter's last resort does, warnings and errors, on its error
  output; at info the line was in no log where both limits held); for a reader apart (§9), info where
  both limits held and a warning where one did not, since each per-key group of a charged or suspect
  file set starts one: `the file reader started: duckdb 1.5.5 in process 4242, data
  limit 5,296 MiB, sockets denied`; `… in process 4242, no data limit (not held on this host), sockets
  denied`.
- warning: `the file reader inherited 1 descriptor(s)`.
- error: `the file reader did not start: its process could not be started (PermissionError)`; `… ended
  as it started (exit 1)`; `… did not answer within 20 s`; `… was not ready within 50 s`; `… answered
  outside the protocol`.
- warning: `the file reader's process ended (SIGKILL) with no statement in flight`.
- error: `the file reader's process ended (SIGBUS) while only one side's files were being read: 2 of
  the 3 after which they are read no more`; `… (killed after its deadline) while only one side's files
  were being read: 1 of the 3 …`; `… (SIGBUS) with 3 statements in flight: each one's files are read
  apart next`; `… (exit 1: KeyError at reader.py line 212)`.
- warning: `the file reader's process 4242 did not end within 10 s of being killed: its reads stay
  counted`. `file read abandoned after its deadline (3 waiting)` stays.

## 13. Interfaces

No route, role, audit action or UI change: the Run screen shows F12 as it shows every run's `error`.
No CLI command; `ds target check`'s line grows (§11). Migration: none.

## 14. Performance

**Observed** (this Mac, load 2.8 to 3.4; a prototype of §6's frames; 2M rows in batches of 10,000,
best of three): +0.49 µs a row for a key and a 32-byte digest (1.16 s in process, 2.14 s through the
pipe), +0.52 µs for a key and three texts. Encode is 0.12 s and decode 0.17 s a million rows; the rest
is the pipe and waiting in turn. A thread a request
(R11) is 56 µs on Linux: 0.06 s of a 10M-row scan's 1,000 batches.

**Projected on FILES §17:**

| Case | Published | Projected |
|---|---|---|
| Parquet-Parquet, 10M rows a side | 54.0 s | +7 to +10 s (13% to 18%) |
| CSV-CSV, 10M rows a side | 181.3 s | +4% to +5% |
| cross-engine, 10M rows | 277 s | +1% to +2% |
| a group of 200 keys | 0.27 s | three round trips, under 1 ms |

A plan's counts cross, no row; the first read after a start takes about 0.1 s more. Batches stay
10,000 rows or 64 MiB, 64 rows a step when wide.

**U38, binding.** The nine `DS_PERF` tests of `tests/e2e/test_files.py`, each twice in a pytest
process of its own, the faster taken, on the in-process reader, before the reader had a process of its
own, and on the feature, in one
sitting on one machine (§17's published figures were single runs whose spread passed 15%). Every §17
bound holds, and no test takes over 1.15 times its own figure before. The first case is at the line:
if a test fails, **R9** is built and measured again: after a full batch the request's thread fetches
and packs the next and holds it for the next `fetch` of the same `rows`, `step` and `bytes`; an error
met there answers that fetch; `interrupt` and `close` drop it. The two memory assertions sum the
server's process and its readers, sampled once a second (`files.memory` plus 1 GiB, as before). Both
sets of figures go into §17 and TABLES §14.9. They are macOS figures: no data limit is under them, and
U35's image test is what shows the default limit beside a 10M-row sort.

**As measured** (2026-10-04; each test once, not twice, against FILES §17's and TABLES §14.9's figures of
2026-10-03, and the in-process reader, before the reader had a process of its own, in the same sitting for
three tests only; one Mac, not idle). With nothing
read ahead two cases passed the line, both Parquet against Parquet: 6.4 s for 5.4 s at 1M rows and 63.4
s for 54.0 s at 10M (+17%, where the table above projected 13% to 18%); the CSV pair took +2% and the
cross-engine scan +3% to +5%. R9 was built and the nine run again: 5.6 s and 53.2 s for those two, and
no figure over 1.06 times its own of 2026-10-03. FILES §17 and TABLES §14.9 hold every figure of both
runs, and of the in-process reader in the same sitting for three tests.

**A full batch.** When those runs were made, R9 read ahead only after a batch of exactly `rows` rows, which a
statement fetched 64 rows a step never answers (its batches close at 10,048): every scan across
engines with a text, bytes or JSON column, and a census or a frame with such a key, read nothing
ahead, in both of the runs above. A full batch is now one of `rows` rows or more. One pass timed by
hand, not a `DS_PERF` test (2M rows of §17's four columns, the three compared ones rendered for the
worker, this Mac, three rounds): 15.9 to 16.4 s to fetch and shape with nothing read ahead, 14.1 to
14.7 s with it. §17's cross-engine figures were not measured again.

## 15. Tests (no external call)

**New `tests/unit/connectors/test_reader.py`.** `ducked.locked` keeps an in-process connection built
by `reader.connect` and `reader.prove` for the U1 to U31 tests; `ducked.started` yields a reader. A
module-scoped fixture of `tests/unit/connectors/conftest.py` asserts after each files test module
that this process has at most one reader child, the current one.
- `test_frames_carry_rows_as_fetched` (property: NUL, beyond the BMP, empty, NULLs, 39 digits; every
  bind tag);
- `test_a_reply_not_of_the_protocol_ends_the_reader` (a stand-in child: an over-long frame, lengths
  that do not sum, too many rows, invalid UTF-8, an unknown id, a name outside `NAMES`, a member of
  another type, a `fault` class or a version of other characters, a nesting that raises
  `RecursionError`; rows of another width than their statement's and a column's
  type that is no type's text: each `reply`, none in a log line);
- `test_a_batch_over_a_frame_crosses_in_several` (one row over it is `Oversize`);
  `test_an_answer_over_its_limit_is_refused` (columns past `ANSWER_MAX`: `Oversize`, P29, no death);
- `test_every_composed_statement_fetches_framed_values`; `test_the_error_names_are_duckdb_s`;
- `test_the_parent_never_loads_duckdb` (a process of its own: plan, scan, per-key, check);
- `test_the_reader_is_started_with_nothing` (argv, environment, modules, no descriptor; a planted one
  is counted and logged);
- `test_a_process_that_does_not_start_is_p27` (a start past its deadline too: P27's second text, not
  P14's, and what it started is killed); `test_a_failed_start_releases_its_spill`;
  `test_a_replaced_reader_is_ended` (at once without a cursor, with its last cursor otherwise);
- `test_a_fault_ends_the_reader_s_process_alone` (SIGSEGV sent mid-statement, three times: `Died`,
  `Died` read apart, then `ReaderDied` with F12's text; the test process lives, a new reader reads);
- `test_the_crafted_page_is_contained`: the mutant as a constant in `tests/support`; the plan passes;
  the scan ends as F12, a closed refusal or a read, and the test process lives. On macOS arm64 with
  1.5.5 it asserts F12 with SIGBUS or SIGSEGV, as observed: another outcome there amends FILES §14
  point 7;
- **the signals:** `tests/support/faults.py` is the one switch. A test that
  proves the accounting, a charge, a message or a restart ends its reader by `faults.QUIET` (SIGKILL)
  on every platform and asserts the cause by that name. A test of a fault itself (the test above, the
  self-test's) sends `faults.FAULT`: SIGSEGV on Linux, and on macOS, where a process ended by SIGSEGV,
  SIGBUS, SIGABRT or SIGTRAP is a crash report and a dialog, only with `DS_FAULTS=1` (`QUIET`
  otherwise). The tests in which DuckDB itself faults on the kept mutant (the test above of the crafted
  page, the e2e test below, the kept one of the fuzz) are skipped on macOS without `DS_FAULTS=1`, and
  run on Linux and, whatever the host, in the image tests. **To make the real faults on a Mac:**
  `DS_FAULTS=1 uv run pytest tests/unit/connectors/test_reader.py tests/unit/connectors/test_duck_guard.py
  tests/e2e/test_files.py`, at a desk whose screen may fill: each reader that ends so is a report in
  `~/Library/Logs/DiagnosticReports` and a dialog (`Python quit unexpectedly`), seventeen in one such
  run, counted from these tests (3 and 3 by the two tests above, 2 by the fuzz's kept
  mutant, 2 and 4 by the self-test's two tests, 3 by the e2e test; not run so again since);
- `test_a_killed_reader_is_replaced` (idle and mid-statement; process and spill gone);
  `test_idle_cursors_hold_no_thread` (200 open cursors: the child's thread count, read from the
  process table, is what it was); with R9, `test_a_batch_is_read_ahead` (the child's own functions) and
  `test_a_batch_read_ahead_is_in_flight` (the accounting; none in a reader apart);
- `test_a_hung_statement_s_process_is_killed` (a FIFO; `manifest.stuck()` returns to 0);
  `test_a_statement_killed_after_its_deadline_is_charged` (a FIFO under a root that answers, three
  times: F12 with `killed after its deadline`); `test_a_dead_mount_s_kill_is_no_charge` (the root's
  stat does not answer: an outage, nothing charged);
- `test_a_scan_in_flight_survives_another_target_s_hung_statement` (amended: the retired process is
  killed when the scan's cursor closes);
  `test_an_abandoned_statement_that_answers_frees_its_count` (on a retired reader another cursor
  keeps: the late reply brings `manifest.stuck(root)` to 0 with the process alive);
- `test_a_child_that_stops_answering_is_killed` (SIGSTOP; a `ping` is answered while a sort runs);
  `test_a_killed_process_that_does_not_end_stays_counted`;
- `test_ends_are_charged_when_one_set_was_in_flight`; `test_suspects_are_read_apart_and_cleared`;
  `test_a_pair_of_suspects_shares_one_reader` (a scan pass and a plan whose two sides are suspect, a
  pass whose side b alone is charged, and a pair reading the same files: each completes when its
  reads are clean and fails with F12 when its reader ends three times; none is `Alone`);
  `test_a_per_key_read_does_not_wait_for_a_pass`;
  `test_an_innocent_run_is_not_failed_by_another_s_files`;
  `test_a_condemned_set_is_refused_with_no_death` (a new plan is P35);
- `test_no_reader_outlives_its_parent` (exit, SIGTERM, SIGKILL idle and mid-sort; then the sweep);
- `test_the_socket_filter_decides_by_architecture` (every platform, no kernel: the program's 15
  instructions interpreted over a table: x86_64 `socket`, `socketpair`, `io_uring_setup` and an x32
  number EPERM, `read`, `mmap` and `clone3` allowed; the same for aarch64; another architecture EPERM);
- `test_the_reader_s_memory_is_limited` (Linux: the proof at the start; a mapping that is not refused
  reports `no data limit (not held on this host)`, in the start line too);
  `test_os_limits_off_sets_none`; on macOS both phrases say `not on this platform`.

**Elsewhere:**
- `tests/e2e/test_reader_image.py`: §3's image tests, skipped without `DS_IMAGE`.
- `test_never_told` (`reader.py`, the new patterns); `test_error_type` holds `ReaderDied`;
  `test_the_self_test_and_its_imports` (the printed line, with and without `limits`; `duckdb` not in
  the parent).
- `tests/unit/runner/test_files.py::test_a_run_whose_reader_dies_three_times_fails_with_f12`: a scan
  and a key-list run; the alert's `error_type`; the worker alive; another run completing.
- e2e `test_files.py::test_a_crafted_page_fails_its_run_and_no_other`, under `ds serve`: `/v1/health`
  answers throughout, the server's process stands, a clean run completes. `test_kill_resume.py`: a
  worker SIGKILLed mid-scan leaves no reader, and its spill is swept.
- **Fuzz.** `tests/support/fuzzed.py` changes one to five bytes of a Parquet footer or of the pages
  before it, over the tests' own files, and sends each mutant the footer walk accepts through `ds target
  check`, plan, scan, census and per-key read in the test process: no parent's end, every outcome a
  closed code. The default suite runs 100 footer mutants, 100 page mutants and the kept one (the kept
  one where real faults are made: the signals, above); `DS_FUZZ=<n>` runs n of each. A run of 40,000 of
  each (2026-10-04, macOS arm64) ended no parent and no reader but the kept one's (FILES §14 point 7).
- `test_release_tool` and `releases.py`: the new failure text; `limits` by platform, `arm64` and
  `aarch64` one machine.

## 16. Documents this contract amends

- **FILES:** the head (it names READER.md); D9, D14; §2; §3 (U32 to U38, Image, Smoke); §4
  (`os_limits`, X1); §10 (Threads, Instance, Sessions, Spill, the errors' table); §14 points 1, 5 and 7
  (a crafted page ends the reader, not the process that holds the keyring; what still parses there);
  §15 to §18; §20 (the seam is built; §17's limits).
- **CONTRACTS:** the module map (`reader.py`; `duck.py` no longer "the one locked DuckDB"), the files
  settings line (`os_limits`), "Without a files target DuckDB is never imported" (no `ds` process
  imports it; a reader starts at the first file read), the Files paragraph ("through one DuckDB a
  process"), and READER.md linked where FILES.md is.
- **PENTEST** §2 and §8, **`SECURITY.md`** §10: what the reader's process contains, by platform, and
  "what a site is told, what is never claimed" from §2.
- **DEPLOY** section 7, Files (the reader in `ps`; sizing: `files.memory` plus 1 GiB for the app and
  its shared reader, as much again for each reader apart, two at most, and each retired one while it
  reads; the data limit lies above a container so sized, where the kernel acts first and takes the
  reader; `os_limits`; the acceptance item, the first x86_64 kernel to load the filter) and section 11
  (P27's second text, `Died`, `Alone`, `Unstarted`, P35 and F12, `MemoryError`).
- **RUNBOOK**, From files, Security and privacy, Troubleshooting:
  - `paused: Died`: the reader's process ended, by the cause in the log; reads resume.
  - `paused: Unstarted`: the reader did not start (the log says how); reads resume when it does.
  - `paused: Alone`: another export is read apart first.
  - `ReaderDied … (SIGSEGV, SIGBUS, SIGILL, SIGFPE)`: a fault in DuckDB: export the files again and
    report the file with the DuckDB version. **As observed,** a statement that meets the data
    limit also ends the reader with SIGSEGV (linux/arm64): RUNBOOK says so, and how to tell the two.
  - `ReaderDied … (SIGKILL)`: the kernel, for memory (DEPLOY section 7), or an operator.
  - `ReaderDied … (SIGABRT)`, `(exit N)`: the data limit (`files.os_limits`, DEPLOY) or DuckDB's own
    abort. `(reply)`: report it. `(killed after its deadline)`: a statement that did not end on a mount
    that answers: report the file.
  - each: restart the `ds` process to read the files again.
  - the dead-mount row: no reader is left behind once the mount answers.
- **RELEASE** §5, §9 step 5, §13, §15. **ALERTS** §2.1, §14. **ENGINES** §5, §11. **TABLES** §14,
  §14.9. `docs/architecture.md` §4.9, §8.5. **PLAN**, README: READER.md beside
  FILES.md.

## 17. Limits and seams

**Limits.**
- The reader is no sandbox (§2). On macOS it has no memory limit and may open sockets.
- DuckDB on Linux, the data limit under its allocator and an x86_64 kernel are unobserved until U32 to
  U37 run and a site's self-test has; the limit's constant is a choice, and `os_limits: false`, the
  only way around a limit or a filter that stops honest work, removes both. **As observed:**
  U32 to U37 ran on linux/arm64; an x86_64 kernel is still unobserved. A statement that meets the data
  limit is not refused by DuckDB: the reader's process ends (SIGSEGV), an end charged like any. Under
  emulation (an amd64 image on an arm64 host) a reader with the data limit does not start (SIGTRAP: P27's
  second text for every file side) and the filter is not loaded: `os_limits: false` is needed there.
- With R9 the reader holds one batch more a scanning cursor, its rows and their frame (for rows of long
  texts up to twice `BATCH_BYTES`), outside DuckDB's `memory_limit` and inside the data limit.
- A row whose values alone pass 128 MiB is refused (a Parquet text cell; FILES §14 point 7's 16 MiB
  row covers bytes and JSON only). The parent holds a frame of up to 128 MiB a request in flight, and
  the rows it builds of it, `CELLS` cells at most (§6).
- An end costs every statement in flight in that reader its pass; nothing bounds ends across
  different file sets, nor one set's ends beside others after clean reads apart (§9).
- Charges never reset before a restart, which interrupts other runs. The kernel's memory killer,
  which takes a reader first, the data limit and an operator's kill are charged like a fault; so is a
  statement that does not end within its grace on a slow mount that still answers a stat.
- With readers apart or retired a process may hold several budgets (§10); a container sized for one
  is then short, and the reader the kernel takes is charged.
- A killed reader in uninterruptible I/O stays, with its spill, until the kernel lets go. A `ds`
  command that exits reaps its reader first; one that is killed (SIGKILL, or SIGTERM where it handles
  none) under `docker compose exec` leaves its reader, which ends at the end of its input, as a zombie
  entry until the container stops.
- A process a reader started (no honest reader starts one) is killed with it, by its process group,
  and is then a zombie entry of the container's first process, which waits for none. One that left the
  group lives on and holds the reader's output open: the reader's end is made all the same (§5), and
  one thread and one descriptor of the parent stay with that output until it ends.
- A fault leaves a signal's name and no more: diagnosing DuckDB needs the file.

**Seams.** A reader user of its own; Landlock; `execve` and `ptrace` denied; the stream check moved
into the child; a command that clears a condemned set; a fatal inherited descriptor once the count is
observed under emulation; a switch for the filter apart from the limit.
