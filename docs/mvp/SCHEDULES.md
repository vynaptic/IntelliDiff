# Scheduling: contract

This contract is binding. It builds cron schedules and a schedule screen after cut-over readiness
(`014_readiness.sql`) and amends the documents of §14; §13 names its seams with other features.

**Goal:** a one-off proof becomes a standing control: a stored spec runs at declared wall-clock times
without a person, and every time that did not start a run is recorded with a closed reason, and can alert,
once a worker sweeps (N7: a schedule is not a dead-man's switch; §16 gives the outside check).
**Shape:** a row of `ds.schedules` holds a spec (a pinned version, or a name followed), what `POST /v1/runs`
takes but keys, a 5-field cron expression, an IANA time zone and a start window. The worker of `ds worker`
or `ds serve` reads the due rows each sweep and calls the existing `runner.runs.create_run` as
`system:worker`; the run, the schedule's advance and their audit rows commit in one transaction, and a
unique index on `ds.runs` refuses a second run of one schedule and due time from any writer. No setting, no
dependency (stdlib `zoneinfo`, as `core/spec/rules.py` and `core/canonical/convert.py` use it), no outbound
call, no key or value stored. Every claim stays *observed*.

## 1. Decisions (each one seam)

- **D1** A firing is `create_run`, nothing more: the four-eyes gate, the licence, sampling's refusals, the
  column plan and the census are the run's own, and a schedule re-implements none.
- **D2** A schedule holds no key list (it would sit outside RETENTION §3's sealing and clock): its runs read
  the spec's `key.source` or scan two tables.
- **D3** Pinned version (`spec_id`) by default; `spec_name` alone follows the name's newest stored version
  (`repo.newest_spec`) at each firing and never falls back to an older approved one.
- **D4** A firing's actor, and `runs.created_by`, is `system:worker` (AUTH §4.3: "the worker passes
  `WORKER`"): no person is recorded as doing what no session did, and 005's actor CHECK is unchanged.
- **D5** Overlap is fixed: at most one live run (queued, running, paused, cancelling) per schedule.
- **D6** Only the newest due time is ever started, and only inside its *window*, which closes
  `start_within` minutes after it or at the next due time, whichever is first; the window is enforced at
  the commit (§5). Older times are recorded as missed. Never a burst, never a backfill.
- **D7** One clock, the database's `now()` (LICENCE D7).
- **D8** The firing history is the audit chain (ALERTS D1's pattern): one row per settled time or counted
  range of times, no firings table. A standing refusal chains its first time at once and its later times
  as one range row at most 24 h after the first of them (§5 step 7).
- **D9** No licence fence (LICENCE O2); one would be one line in `Standing.refusal`.
  In an open build, which enforces no licence, no firing is refused or missed for one (LICENCE §19).
- **D10** Writes need `operator`, as `POST /v1/runs`; no step-up; schedules are not four-eyes approved.
- **D11** A schedule fires only from a worker in the auth mode it was last saved in, and only while the
  person accountable for its last save is enabled and still an operator (§6). A token's expiry or revoke
  stops nothing: rotation is routine (AUTH §5).
- **D12** Firings are at least 15 minutes apart on the wall clock (a soak's `execution.cycleInterval`
  repeats faster); an installation holds at most 200 schedules.
- **D13** Rows and audit details hold ids, times, cron text, zone names, slugs and closed codes. A refusal's
  message is never stored, logged by this feature or sent (`cycle.failure`'s rule: it may hold keys).
- **D14** Only the worker of `ds worker`, or of `ds serve` not started `--no-worker`, fires: `ds run`'s
  in-process worker never does (§5).

## 2. What readers are never told

`schedules.NOT` (one sentence for each of N1 to N7) heads the screen, `ds schedules` and the list answer;
`test_never_told` asserts each on every surface. **N1** A firing is not a completed run: texts say `started run R-…`; the run's status says
how it ended. **N2** Next due is `a schedule, not a forecast`. **N3** No person is named as a firing's
actor. **N4** A missed time is never shown as run; a late start says how late; a range is counted; times a
person's save, pause or delete left unstarted are missed (`superseded`), never dropped. **N5** No
percentage, uptime or success rate: a schedule is not evidence, readiness or sign-off. **N6** No key and
no value. **N7** `no alert is sent while no worker runs: a missed time is reported when a worker next sweeps`.

## 3. What a schedule is

Body of `POST` and `PUT` (`ScheduleIn(Payload)`):

| Field | Rule |
|---|---|
| `name` | slug of at most 63 characters, unique among schedules not deleted; never `zones` or `preview`, the API's own routes under `/v1/schedules` (`GET /v1/schedules/zones` would never read a schedule so named) |
| `spec_id` xor `spec_name` | the pinned version, or the name followed |
| `run` | `{mode: batch\|soak, soak?: {rolling}}`: `RunOptions`, which `routes/runs.RunIn` is split into (`RunIn(RunOptions)` adds `spec_id` and `keys`) |
| `cron` | §4; stored trimmed and single-spaced; a body's text is at most 1,000 characters (`Payload`: `invalid request`), and C9 judges it once trimmed |
| `time_zone` | required: `UTC`, or a name with a `/` that `cron.zones()` holds and `cron.RENAMED` does not (§4 T5); at most 64 characters in a body, as in the row |
| `start_within` | 15 to 1440 minutes, default 60 |
| `ends_at` | optional instant with an offset, before the last day of the year 9999 in UTC (the store, read in a zone ahead of UTC, holds no later one); no due time exists after it, and a run for the last time before it may still start within that time's window |

Saving checks what a firing would meet: the spec has a key source or two table sides (S5: the spec schema
requires a key source of every spec with a service side, so only a version an earlier build stored can
meet it); `create_run`'s mode rules; `sample.refuse(mode, None, None, soak)` (SAMPLING R2); a soak's spec has `execution.stop.maxCycles`,
`maxDuration` or `until` (`StopConditions.from_spec`). The gate is read unlocked and answered as a
warning, never a refusal; the licence is not read (the header's licence warnings already say its state).

**States:** `active`, `paused` (by a person, or by the worker: T6), `ended` (no due time before `ends_at`),
`deleted` (a tombstone: the name is freed, its runs still name it). **Invariant:** `next_due =
cron.after(cron, time_zone, anchor_at)`, NULL while paused, deleted or past `ends_at`; `anchor_at` is the
settled due time after each firing or miss.

| Write | When | Effect (one transaction, the row locked) |
|---|---|---|
| create | — | `active`; `anchor_at = now()`; creates take the advisory lock `DSSC` before they count the schedules, so the 200 of D12 hold however many arrive together |
| `PUT` | not deleted | *settles what is due*; `anchor_at = greatest(now(), anchor_at)`; a paused schedule stays paused (`next_due` NULL); an ended one is active again if the body leaves a time |
| pause | active | *settles what is due*; `next_due` NULL. Ended: 409 `has ended`; paused: 409 `is paused already` |
| resume | paused | cron and zone are validated again (C1–C9, Z1); `anchor_at = greatest(now(), anchor_at)`; with no time before `ends_at` the state is `ended`, with S9's second text as a warning |
| delete | not deleted | *settles what is due*; the tombstone |

*Settles what is due* (`schedules.settle_due`, before the caller's own audit row): a pending `held` range
(§5 step 7) is chained, then every due time at or before `now()` is one `schedule.missed {reason:
superseded, code: update|pause|delete, due, to, count}` row by the caller (`to` and `count` null when the
stored cron or zone does not load). A pause's own times are neither due nor missed: its pause and resume rows
bracket them. `greatest` keeps a time at or before a settled one from ever being due again, whatever the
clock did. Every write by a person adds 1 to `revision`.

## 4. Cron and time (`deltasleuth/core/cron.py`, pure, about 250 lines)

`parse`, `zone`, `zones`, `tzdata`, `after(cron, tz, t, until=None)`, `latest`, `between`, `upcoming`,
`notes(cron, tz, t, days=370)`, `utc` (an instant as schedules write one, `2026-10-07T01:00Z`),
`CronError(ValueError)`; `SPACING_MIN = 15`, `HORIZON_YEARS = 8`, `RENAMED`. `parse` raises C1 to C9 from
the expression alone (C7 and C8 over a leap cycle of dates: no clock is read). It trims and single-spaces
its text and judges C9 before anything is remembered: the cache (`_parse`, 512 entries) is keyed by that
text of at most 100 characters, never by what a caller sent.

- **Syntax.** `minute hour day-of-month month day-of-week`; each `*`, a number, `a-b`, a list, `*/n` or
  `a-b/n` (a step of a single value, `5/15`, is refused: C3); `JAN`–`DEC`, `SUN`–`SAT` in any case;
  day-of-week 0 and 7 are Sunday. Letters and digits are ASCII: `ſun`, which upper-cases to `SUN`, and a
  digit of another script are C3, as 015's CHECK refuses them. No seconds, macros, `?`, `L`, `W` or `#`. Restricting both day fields is refused. Presets only write cron text.
- **T1** Times are wall-clock times in the zone, stored and compared as instants: each matching time of a
  day is read with `fold=0`, and a day's instants are de-duplicated and sorted.
- **T2** A time the clock skips fires once, later by the size of the jump: `30 1 * * *` in Europe/London
  fires at 02:30 BST (01:30 UTC) on 2027-03-28; `30 2 * * *` in America/New_York at 03:30 EDT on
  2027-03-14; `15 2 * * *` in Australia/Lord_Howe at 02:45 on 2026-10-04. Two times on one instant fire once.
- **T3** A time the clock repeats fires at its first occurrence only (01:30 Europe/London on 2026-10-25 is
  00:30 UTC), so an hourly local schedule has one two-hour gap that night. **T4** `UTC` has neither.
- **T5** Zone rules are the image's tz data; `tzdata()` reads its version from the first `tzdata.zi` on
  `zoneinfo.TZPATH` (`unknown` without one). `zones()` is `UTC` plus the names of
  `zoneinfo.available_timezones()` that hold a `/`: `localtime` (the container's own), `Factory`,
  `posixrules` and the bare abbreviations are never zones. `RENAMED` maps 28 legacy names (those
  browsers following CLDR report, and the `US/*` links) to their IANA primaries, for example
  `Asia/Calcutta` to `Asia/Kolkata` and `Europe/Kiev` to `Europe/Kyiv`: a renamed name is refused with its
  primary even where a host's data holds it. Observed 2026-10-01: the local `deltasleuth:source` image has
  `2026c`, 486 zones and no legacy link; a macOS host has 598, `US/Eastern` and `Asia/Calcutta` among
  them, so DEPLOY says to run `ds schedules` in the release image, as the API and the worker.
- **T6** A stored cron or zone the worker's build cannot load (data older than the saver's, a name a later
  tz release dropped) never raises into the sweep: in one transaction the worker pauses the schedule as
  `system:worker` (`schedule.pause {reason: zone, code: zone|cron, tzdata, missed, spec_name, mode}`) and,
  when its next time is due, chains `schedule.missed {reason: zone, code, due, tzdata}` (an alert) before
  it (the pause's `missed: true`); a range the schedule holds (§5 step 7) is chained first, a time due or
  not. A time not yet due is no missed time (N4: it may still start once the schedule is saved and
  resumed), so a schedule paused before its time has the pause's row alone (`missed: false`), which the
  notifier sends as a `schedule.missed` event of reason `zone` without a time (an alert: §8, §16). A `PUT`
  that validates, then a resume, clears it.
- **Rezone.** The first sweep after a firing worker takes the lock recomputes each active `next_due` from
  `anchor_at` (`runner.schedule.rezone`, each schedule guarded: T6, with the missed row only if its time
  is due at the database's `now()`, or logged and left; a sweep that cannot read the schedules leaves it
  to the next) and audits a change
  as `schedule.rezone`, so an upgrade's rules apply at the restart. `fire` does the same for the one
  schedule whose `next_due` is not its cron's first time after `anchor_at` (saved by a process with other
  tz data), and judges it at the next sweep.
- **Clock.** Moved forward: the times in between are missed (D6). Moved back: nothing fires twice and
  nothing fires until the clock passes `next_due`. `next_due` moves back only by a `PUT` or a rezone, and
  never to or before a settled time.

## 5. Firing (`deltasleuth/runner/schedule.py`, `runner/worker.py`, `runner/runs.py`)

**Worker.** `run_worker(ctx, *, stop=None, schedules=True)`; `ds run` passes `schedules=False`, so its
in-process worker neither rezones nor fires with that invocation's config and targets (ALERTS §9 guarantee
5 is unchanged: it still feeds notifications). `ds serve --no-worker` (ENGINES §13) runs no worker, so it
fires nothing: the `ds worker` beside it does. With `schedules`, `_sweep_once` reads
`repo.schedules_due(conn)` (each with its `next_due`: those with `next_due <= now()`, on the partial index,
and those holding a range whose first time is 24 h old, step 7) in a savepoint of its first connection
block, so a fault reading them is logged and the sweep goes on (the store unreachable ends that pass, as
it would at its next statement: the next sweep reads again), and starts `self.firing[schedule_id] =
create_task(self._fire(id, queued))`, as `self.draws` does: never two
for one schedule, at most `FIRING = 2` at once, none before the schedule's time in `self.waiting`; a due
schedule left without a slot is remembered in `self.queued` with its due time and the database's time at
that sweep, while that time stays its `next_due`.
`_fire` catches everything
but its cancellation: whatever escapes `fire` (the store unreachable, a bug) is logged with `failure(exc)`
and puts the schedule in `self.waiting` for `RETRY_SLOW_S` (its count of tries stands), so nothing loops
at the poll rate, and it never calls `fail_run`; `close()` cancels the tasks, as a SIGTERM to `ds worker` does (ENGINES §13): a
firing cancelled before its commit leaves its time due. A firing holds a pooled connection for its reads and
its insert only, never while a listing is read (`create_run` resolves keys outside any store connection),
so the pool is unchanged. Only the holder of `LOCK_ID` fires.

**`fire(ctx, schedule_id, *, now=None, queued=None, tried=0)`** (`now`: the database's; tests inject it;
`queued`: `(due, at)`, the time this worker left without a slot and the database's time when it did;
`tried`: the attempts to create the run of the schedule's next due time that failed. The worker keeps
that count in `self.tried` as `(due, tried)`, from the `Retry`, and each sweep forgets one whose `due` is
no longer the schedule's `next_due`: the time was settled, as a person's write settles it, so the next
time has its own three tries; `fire` counts from none for a newest time it reaches past older ones). It
answers `Retry(seconds, tried, due)` when the time `due` stays due, to be tried again, else None:

1. Read the row; return unless due (a held range 24 h old is chained first, step 7). A cron or zone that
   does not load: T6. `decide(row, now)` (pure) gives
   `newest`, the latest due time at or before `now`, the older due times, and whether `newest`'s window
   has closed.
2. The older times, and `newest` once its window has closed, are settled in one transaction as `late`:
   counted ranges, `schedule.missed {reason: late, code: down, due, to, count}`, but the time this worker
   held in `queued`, if it was queued while its window was open (`at` before the window's end:
   `start_within` minutes after the time, or the next due time if sooner). That one is its own row, code
   `busy`, between the ranges before and after it. So a time whose window had closed before the worker
   first saw it (an outage) is `down`, however many schedules then waited for a slot, and an hourly time
   closed by the next one is `busy`, though it is an older time by then. Each row's `next` is the time
   after its last. A closed window ends here.
3. Pre-checks, the first that holds deciding: `mode` (`auth_mode` is not `ctx.settings.auth_mode`);
   `setter` (`repo.setter_live`, §6; read again at the commit: step 5); `overlap` (the run `last_run_id` is not in `repo.TERMINAL`);
   `sample`, code `unclosed` (a `bottom_k` spec whose population's `repo.ledger` holds a draw
   `sample.blocking` flags: SAMPLING D7 for unattended firings).
4. `create_run(ctx, spec_id, mode, soak=…, who=WORKER, schedule=Firing(…))` under `asyncio.timeout` to the
   window's end, `spec_id` the pin or the name's newest: no listing outlives its window.
5. A refusal becomes a closed reason. `ApprovalRequired`: `approval`. `SetterGone` (who saved it was disabled or demoted after step 3, as the insert
   transaction read: §6): `setter`. `LicenceRefused`: `licence`, code
   `expired`, `sso` or `pairs`, read from `licence.standing` (`state`, `sso_fenced`), never from the
   message. `SampleRefused`: `sample`, code `live` (R1) or `refused`. `ValueError`, `LookupError`,
   `SpecError`, `ConnectorError`, `TableError` (a table side's code on any engine; a connect it refused is
   `Unconnected`, a `ValueError`: ENGINES §12), `psycopg.OperationalError` (a PostgreSQL side or the store:
   the two cannot be told apart) and `TimeoutError`: `not_started {error_type}`. `NotReady`, a file
   side's export that is not ready when its plan is made ([FILES.md](FILES.md) §12), is caught before
   `ValueError`: `not_started` with its code, `export_absent`, `unsettled`, `stale`, `unstamped` or
   `planning`, and `error_type: NotReady`. `UniqueViolation` on
   `runs_schedule_due_idx` (a run of this time exists: a forged or hand-made row): `late`, code
   `fired_already`. `KeyError`, `IndexError` and anything else (bugs, as `routes/runs._runner_errors`
   treats them): `error {error_type}`. After `ScheduleMoved`, or the timeout at the window's end, the miss
   is settled under the same compare-and-set as every miss (`next_due`, `revision`, not paused, not
   deleted), which is the second read: a schedule advanced or changed takes nothing; one unchanged can only
   have lost its window, and the time is `not_started {TimeoutError}` at once, whatever its tries.
6. **Waiting.** `overlap`, `sample` `live` and a `not_started` with a code (an export not ready yet,
   which counts no try: an export that lands inside `start_within` starts its run) are tried again every
   `RETRY_S = 60` until the window closes; any other
   `not_started` every `RETRY_SLOW_S = 300`, at most `TRIES = 3` attempts (a listing is re-read under its
   target's rate budget; the count is `Retry.tried`, handed back at the next try). Whether another try
   fits is judged after the attempt, on the clock as it then stands (the time read, plus what the attempt
   took by `time.monotonic`): the wait and one sweep (`worker.poll_seconds`) must end before the window
   does. A try that waits writes nothing; a try after which no other fits settles the time at once with
   its reason, never as `late`; every other reason is settled at once. If the store cannot take the miss row,
   nothing moved and the time is judged again when it can (`late` once its window has closed).
   `self.waiting`, `self.queued` and `self.tried` are memory only: a restart tries sooner, never later.
   `self.waiting` is the schedule's, whatever time is due: a time that comes due inside a wait (300 s at
   most) is first tried when the wait ends.
7. **Standing reasons** (`mode`, `setter`, `approval`, `licence`, `sample` `unclosed`). The first due time
   of a `(reason, code)` chains its row. Each later consecutive one (the schedule's `last` is a miss of the
   same reason and code at the same `revision`) is settled into the schedule's
   `held` (the range's row as it will be chained: §11's detail, `due` its first time, `to` its last,
   `count`) with no audit row, and `held` is chained as one counted
   `schedule.missed` row, in the same transaction, by whichever comes first: a settlement with another
   outcome, a person's write, or the first sweep 24 h after `held.due` (which `repo.schedules_due` lists
   though no time is due; a time due then starts the next range).

**`create_run`** gains `schedule: Firing | None = None` (`Firing`, `ScheduleMoved` and `SetterGone` live in
`runner/runs.py`; about ten lines):
- `params["schedule"] = {schedule_id, due, revision, time_zone}`, `due` as `%Y-%m-%dT%H:%MZ` in UTC, the
  canonical text 015's CHECK and index rely on; `approvals.plan` keeps it, so `plan_sha256` binds it.
- In the insert transaction, before any audit row, `repo.settle_schedule` moves `anchor_at`, `next_due` and
  `last*` under `WHERE next_due = due AND revision = seen AND paused_at IS NULL AND deleted_at IS NULL AND
  clock_timestamp() < window end` (the statement's own time: `now()` is the transaction's start, and a
  transaction that waited for a lock commits later); zero rows raises `ScheduleMoved` and rolls the run
  back. With the row so locked, `repo.setter_live` is read again for `Firing.set_by` (the compare-and-set
  on `revision` leaves no other save in between): false raises `SetterGone` and rolls the run back, so
  who was disabled or demoted while a listing was read gets no run, and the time is `setter`.
- Audit rows last, in order: a flushed `held` row if any, `create_run` (detail gains `schedule`, as
  `params`), `licence.pair` if any, `schedule.fire`. Without `schedule`, params and detail are
  byte-for-byte today's. The schema guard's rows of a run's creation come after them
  ([PRECHECKS.md](PRECHECKS.md) §7, V1): `paused` (`run_paused_schema_drift`) for a run created paused,
  then `schema.drift`, as `system:worker`. A run the guard created paused is live (`paused`), so the
  schedule's later times are `overlap` until a person resumes or cancels it or `purge.abandon` does; its
  refusal at creation (E6, a narrowing under `schemaDrift: {mode: pause}`) is a `ValueError`, so the time
  is `not_started {error_type: Refused}` and nothing is written.
- Lock order: approval `FOR SHARE`, DSLC, DSSM, the schedule row, DSAU; a person's write takes the row,
  then DSAU; a create takes DSSC (§3), then DSAU.

**Guarantees.** (1) Exactly once: run, advance and audit rows commit together or not at all; a kill, a
timeout or a lost race before the commit leaves the time due, judged again under D6;
`runs_schedule_due_idx` refuses a second writer, so two lock holders racing (several workers) start one run. (2) A
`PUT`, pause, resume or delete during a firing wins the compare-and-set or waits for it. (3) No fault of
firing or rezoning fails a run, ends a sweep or retries faster than `RETRY_S`. (4) A miss is one
transaction whose last statements are its audit rows; if it fails, nothing moved. (5) No run commits
outside its window.

## 6. Actors, roles, four-eyes

- **Roles.** `viewer` reads. `operator` creates, replaces, pauses, resumes and deletes: a person, a token
  (`TOKEN_ROLES` holds operator), `dev:loopback`, or the CLI as `local:`.
- **Per-user audit.** Each write is the caller's own row. `set_by` is the creator, then each `PUT`'s caller;
  a firing's row carries the `revision` that person's row made.
- **Gate.** Under oidc `approvals.gate` judges every firing: an unapproved or revoked version never runs
  (`approval`); the next due time is judged anew, so a grant lets it start with no other act. Saving a
  schedule of an unapproved version is allowed and warned. In tokens and developer modes runs go ahead
  labelled unapproved (`params.approval.mode`), as every run there.
- **Sign-off.** `ds.attributed('system:worker')` is false and `ds.accountable` gives nobody, so APPROVALS
  §3's unknown authorship applies with no DDL: a scheduled run's sign-off needs two approvers. The setter is
  not excluded (§16); for a one-approver sign-off, start the candidate run by hand. ACCURACY's `started_run`
  is false for a scheduled run.
- **Mode.** Create and `PUT` stamp `auth_mode`; a worker in another mode settles each due time as `mode`:
  a `--config` without `auth.oidc` starts no ungated run of an SSO schedule, and a move from tokens to SSO
  shows until each schedule is saved again.
- **New version.** Pinned: nothing changes; the item's `newer` and a banner name the newer version; moving
  is a `PUT`. Following: the next firing runs the newest version if the gate admits it. READINESS's streak
  restarts (it never crosses versions).
- **Setter** (`repo.setter_live(conn, set_by)`, one statement). `user:`: its `ds.users` row is enabled and
  its stored roles hold `operator` (the row `ds.tg_approvals` already trusts; roles refresh at the person's
  next sign-in). `token:`: its `owner_user_id`, if it has one, is not disabled; the token's own expiry and
  revoke are not read, so rotation and the 90-day default stop nothing. `local:` and `dev:loopback`: live.
  It is read before a firing creates its run and again in the transaction that inserts it (§5), so a
  disable or a demotion stored while a firing reads its key listing stops that firing too.
  `user.disable` therefore stops the schedules a leaver saved, in person or through tokens they own, until
  another operator saves them; `--set-by` lists them for access reviews. In tokens mode no person exists,
  so only a pause or a delete stops a schedule (§16).

## 7. Licence, retention, sampling, readiness, packs

- **Licence.** Schedule routes and `ds schedules` never call `licence` (LICENCE §2 gains the row). An
  open build (LICENCE §19) admits every firing, counts no pair and misses no time for a licence. On a
  store where an older build that enforced the licence refused a schedule's times, the range of them
  that build held is chained once under the open build, as `schedule.missed {reason: licence}`, by
  whichever of §5 step 7's three comes first: the worker settling the schedule's next due time, which
  the licence no longer refuses, a person's write, or the first sweep once the range's first time is
  24 h old. Until that next time is settled, the schedule's newest firing reads as that build left it
  (`not started: the licence refused the run (pairs): see Licence`, or `not started: 1 more time since …
  for the same reason`). The rest of this item is a vendor build's.
  `licence.admit` judges each firing: a new name is counted (`licence.pair` by `system:worker`); M1–M5
  give `licence`. Firings in `ending` and `grace` run, labelled by `licence.run_label`; after the grace
  every due time is missed (step 7: one row, then one a day) until a renewal, whose next due time runs with
  no backlog (D9 there concerns paused runs; a firing is a new run). A scheduled soak paused at a boundary
  for the licence is live: later firings are `overlap`.
- **Retention.** `ds.schedules` rows and their audit rows never expire. Scheduled runs are ordinary runs
  to retention; a paused one that `purge.abandon` cancels ends the overlap.
- **Sampling.** Each scheduled `hash_sample` run is created undrawn and drawn by the worker with a new
  nonce (SAMPLING D1); a schedule holds no salt, nonce or selection. R2 is checked at save and by each
  firing; R1 and the unclosed-draw rule at each firing; a rate sample never blocks. A later scheduled draw
  that does not pass spends an earlier pass (SAMPLING D6): pause the schedule before requesting sign-off.
- **Readiness.** Nothing is built: every batch and soak run is measured (READINESS D13), so scheduled batch
  runs of one version are evidence points, build the streak across runs and keep `fresh`.
- **Packs.** `security/rawvalues._GENERATED` gains `SC` beside `LI`, so `facts/plan.json` and the
  `create_run` row pass the raw-value gate (a region's zone name is letters, `_`, `-` and `/`: the pack
  test holds one; a zone named by its offset, `Etc/GMT+5`, holds a number a numeric key pattern may match,
  and its runs then get no pack: §16; a save warns of such a name and a preview notes it, §9). `SC-` rows are in no pack's segment (PACK §4.3 unchanged); they stay in the installation's
  chain (`ds audit --object SC-…`, `ds audit verify`).

## 8. Alerts (ALERTS §2.4's seam; `core/notify.py` only)

One event, `schedule.missed`, from the audit action of that name, whoever wrote it, and from the worker's
`schedule.pause` row that no missed row accompanies (T6 before a time is due: `reason: zone`,
`missed: false`): the `EVENTS` rows `"schedule.missed"` and `"schedule.pause"`, each
`(_missed, frozenset({"schedule.missed"}))`, the `Kind` member, `SC` in `Id`'s pattern,
the model `ScheduleMissed` (`schedule_id`; `reason` and `code` as `Literal`s; `due` and `to`, ISO-8601 UTC;
`count`, `run_id`, `draw_id`, `error_type`; `extra="forbid"`; no zone name; a code that is not one of its
reason's fails the model, by `core/notify.MISSED`, which holds §8's table, as does an `error_type` beside any
reason but `not_started` and `error`, or none beside those two, or a null `due` beside any reason but
`zone`), and one case each in `dedup`
(`schedule.missed|SC-…|reason`), `severity`, `_other_text` and `_route` (`#/schedules/SC-…`).

| `reason` | `code` | Severity |
|---|---|---|
| `late` | `down`, `busy`, `fired_already` | alert |
| `superseded` | `update`, `pause`, `delete` | alert |
| `zone` | `zone`, `cron` | alert |
| `mode`, `setter` | — | alert |
| `not_started`, `error` | — (`error_type`); `not_started` also `export_absent`, `unsettled`, `stale`, `unstamped`, `planning` (a file side's export, [FILES.md](FILES.md) §12) | alert |
| `overlap`, `approval` | — | warning |
| `licence` (a vendor build's; in an open build only the range an older build held, §7) | `expired`, `sso`, `pairs` | warning |
| `sample` | `live`, `unclosed`, `refused` | warning |

`runner/notify.py` is untouched: `_seen` already returns `Seen()` for an `SC-` object, and the projector
sets the event's `spec` and `mode` from the row's `detail.spec_name` and `detail.mode`, which rules match
and no payload carries. `config._FILTERS` already refuses `reasons` beside this event, so `fires` never
reads its `reason`. A started firing sends nothing, nor does a person's `schedule.pause`, nor the worker's
beside its missed row (`missed: true`: that row is the notification). The worker's pause of a schedule
before its time is due (T6 at a rezone: `missed: false`) is this event with reason `zone`, the pause's
code, and `due`, `to`, `count`, `run_id`, `draw_id` and `error_type` null: rules match the `spec_name` and
`mode` its row holds, and its dedup key (`schedule.missed|SC-…|zone`) and severity (alert) are the
reason's. It reads `[prod] schedule SC-… is paused by the worker before a time of it came due: the worker's
time zone data did not hold its zone. A person saves the schedule with a zone the build holds, then resumes
it. Observed by …` (`core/notify._ZONE`, a closed clause for each code), never `did not start a run` (N4).
Wording of a missed time, one closed clause per reason and code (`core/notify._MISSED`): `[prod] schedule
SC-… did not start a run for
2026-10-07T01:00Z: run R-… of it was still live. Observed by DeltaSleuth (installation I-…); not evidence
and not a sign-off. Audit row 81234.` A counted range reads `for 3 times, 2026-10-05T01:00Z to
2026-10-07T01:00Z: …`, a standing refusal's held range `for 14 more times since 2026-10-08T01:00Z, for the
same reason: …`. The `mode` clause names no mode: the event carries none.

The worker is the scheduler and the notifier (ALERTS §9 guarantee 5), so an outage alerts only after it
(N7); §16 gives the outside check. A nightly batch schedule re-reports every standing gap as new, because
the digest baselines inside one run (ALERTS §2.2): RUNBOOK routes scheduled pairs to `readiness.changed`,
`run.failed` and `schedule.missed`.

## 9. Messages (binding; each asserted)

The API answers 422 unless a status is shown; the CLI prints `error: <message>` and exits 2.

- **S1** `name must be a slug of 1 to 63 characters (lower-case letters, digits and single hyphens, starting with a letter)`; `name must not be zones: /v1/schedules/zones is a route of the API's own` (`preview` likewise)
- **S2** 409 `a schedule named nightly-customers exists already (SC-…)`
- **S3** `give spec_id (a pinned version) or spec_name (the newest version at each firing), not both`
- **S4** 404 `spec 12 not found`; 404 `no spec is named customer-profile`
- **S5** `spec 12 (customer-profile 1.4.0) has no key source and is not a pair of tables: a schedule's runs need one of them`
- **S6** `create_run`'s own: `mode must be batch or soak, and only a soak run takes soak settings`; `a soak run needs the spec's execution.cycleInterval`; SAMPLING R2. `keys` is no member: `Payload` answers `invalid request` at `/body/run/keys`.
- **S7** `a scheduled soak must end by itself: spec 12 (customer-profile 1.4.0) needs execution.stop.maxCycles, maxDuration or until, or every later firing is skipped while it runs`
- **S8** `start_within must be 15 to 1440 minutes`
- **S9** `ends_at must be an ISO-8601 time with a UTC offset, later than now` (also for an instant in the last day of the year 9999 in UTC or after it, `9999-12-31T23:59:59-12:00` among them: no row holds it); `the schedule has no time before its ends_at (2026-10-01T00:00Z)`
- **S10** 409 `this installation holds 200 schedules, the most it takes: delete one first`
- **S11** 404 `schedule SC-… not found`; 409 `schedule SC-… changed since you loaded it (revision 4): reload and edit again`; 409 `schedule SC-… is deleted`; 409 `schedule SC-… is paused already`; 409 `schedule SC-… is not paused`; 409 `schedule SC-… has ended`
- **C1** `cron must have 5 fields (minute hour day-of-month month day-of-week), not 6`
- **C2** `cron field minute: '61' is outside 0-59` (hour 0-23, day-of-month 1-31, month 1-12 or JAN-DEC, day-of-week 0-7 or SUN-SAT)
- **C3** `cron field hour: '2-' is not *, a number, a range a-b, a list a,b or a step */n or a-b/n` (a letter or digit beyond ASCII too: `'ſun'`)
- **C4** `cron field day-of-week: range '5-1' runs backwards`; **C5** `cron field minute: step '0' must be at least 1`
- **C6** `cron sets both day-of-month and day-of-week: set one and leave the other * (cron dialects disagree on what both mean)`
- **C7** `cron names no time in the next 8 years`
- **C8** `cron fires at 02:00 and 02:05: a schedule fires at most every 15 minutes (a soak's execution.cycleInterval repeats faster)`
- **C9** `cron must be at most 100 characters`
- **Z1** `'Asia/Calcutta' is not a time zone this build uses (tzdata 2026c): use Asia/Kolkata` (a `RENAMED` name); else `'Mars/Olympus' is not a time zone this build knows (tzdata 2026c): use an IANA region name such as Europe/London, or UTC`
- **Z2** `time_zone must be UTC or an IANA region name such as Europe/London: 'localtime' is not one` (any name without a `/`)

**Warnings** (in the answer; the CLI's `warning: …` on stderr): the gate's message plus `: its firings are
not started until it is approved`; `tokens mode has no approvers: its runs are labelled unapproved`
(`developer mode` likewise); S9's second text at a resume; `the name of 'Etc/GMT+5' holds a number: under a
key pattern that matches a short number, the runs of a schedule in it get no evidence pack: use a region's
zone, or UTC` (any zone whose name holds a digit: §7, §16; it is also the first of a preview's `notes`, and
`ds schedules next` prints it); each note.

**Notes** (`cron.notes`, at most 10): `2027-03-28: 01:30 does not exist in Europe/London (clocks go
forward): that firing happens at 02:30 (01:30 UTC)`; `2026-10-25: 01:30 happens twice in Europe/London
(clocks go back): the schedule fires at the first (00:30 UTC)`. A skipped time that fires on the next
day of the zone names that day: `2027-03-27: 23:30 does not exist in America/Nuuk (clocks go forward): that
firing happens at 2027-03-28 00:30 (01:30 UTC)`.

**Firing texts** (`schedules.line(outcome, detail, at=, held=)`, the only builder; the API, UI and CLI print
it; pure in the row's detail and time, so a row reads the same whatever was saved since and whatever
build, with whatever tz data, reads it). A length of time
reads `4 s`, `12 min` or `7 h 30 min`; a time's *60 min* is its window: the row's `start_within`, or the
minutes from its `due` to its `next` if fewer.
- `started run R-… at its time` (under a minute late); `started run R-… 12 min after its time`
- on a run: `started by schedule nightly-customers (SC-…) for 2026-10-07 02:00 Europe/London (2026-10-07T01:00Z), 4 s after its time` (the zone in force at the firing; UTC alone when this build cannot load it)
- `not started: its time 2026-10-07T01:00Z passed 7 h 30 min before a worker could start it, over the 60 min the schedule allows (no worker that fires schedules ran, the store was down, or the clock moved)`
- `not started: 3 times, 2026-10-05T01:00Z to 2026-10-07T01:00Z, passed while no worker could start them: a schedule never runs a backlog`
- `not started: its time 2026-10-07T01:00Z passed its 60 min while the worker was still starting other schedules (two at a time)`
- `not started: a run for 2026-10-07T01:00Z exists already`
- `not started: its time 2026-10-07T01:00Z was due and not started when the schedule was saved again` (`was paused`, `was deleted`; a range reads `3 times, … to …, were due and not started when …`)
- `not started, and paused by the worker: the worker's time zone data (2026c) did not hold 'Europe/Kyiv': save the schedule with a zone the build holds, then resume it` (the version is the row's `tzdata`, the worker's when it paused the schedule; a row without one reads `the worker's time zone data did not hold …`; code `cron`: `the worker's build could not read its cron: save it again, then resume it`)
- `not started: run R-… of this schedule was still live when its window closed`
- `not started: the schedule was saved in tokens mode and this worker runs under SSO: save it again, signed in, to run it` (the row's `auth_mode` and `worker_mode`, read `SSO`, `tokens mode`, `developer mode`; without SSO the tail is `save it again to run it`)
- `not started: who saved it is disabled or no longer an operator (for a token, its accountable person is disabled): another operator saves the schedule again to run it`
- `not started: spec 12 lacked the four-eyes approval it needs (see Approvals)`
- `not started: the licence refused the run (expired): see Licence`
- `not started: draw D-… of its population did not pass and is not closed: close it with the fix, then the next firing draws`; `not started: a live sampled run samples its population`; `not started: the sample's rules refused the run`
- `not started: the run could not be created (ValueError): starting one on the Runs screen shows the cause` (`TimeoutError`: `… was not created before its window closed (TimeoutError): …`)
- `not started: the export a file side reads was not ready before its window closed (export_absent): ds target check shows its state` (F11 of [FILES.md](FILES.md) §15; for `unsettled` too), and for the codes whose cause that check cannot show (it reads no stamp file, takes no `maxAge` and plans nothing: `stale`, `unstamped`, `planning`) `not started: the export a file side reads was not ready before its window closed (unstamped): starting a run on the Runs screen shows the cause`
- `not started: a fault in DeltaSleuth (KeyError): see the server log`
- `not started: 14 more times since 2026-10-08T01:00Z for the same reason` (a range row, `1 more time` for one; while still `held`, the item's `last.text` adds `, chained as one row within 24 h`)
- `its times moved with the time zone data (2026c): next due 2026-10-08T01:00Z, was 2026-10-08T00:00Z`

**Banners** (`schedules.banners`, over the page's items): `schedule nightly-customers was due at
2026-10-07T01:00Z and is not settled yet: a worker may be retrying or starting it, or none is running (ds
worker, or ds serve)` (`OVERDUE_MIN = 10` minutes late); `schedule nightly-customers is paused by the
worker: its time zone or cron did not load in the worker's build: save it with a zone and a cron the build
takes, then resume it`; `schedule
nightly-customers is pinned to 1.4.0 (spec 12); 1.5.0 (spec 14) is stored`; `the database clock is behind
schedule nightly-customers' last firing: nothing fires until it passes 2026-10-08T01:00Z`.

**Log** (`deltasleuth.schedules`): `schedule SC-… did not start a run for 2026-10-07T01:00Z (not_started:
ValueError)`: codes and types only.

## 10. Interfaces

**API** (`api/routes/schedules.py`, in `app.py`'s tuple before `runs`; logic in `deltasleuth/schedules.py`,
shaped like `notifications.py`, its `Refused(status, message)` one `_HANDLERS` row):

| Route | Role | Answer |
|---|---|---|
| `GET /v1/schedules?spec_name=&state=&set_by=&limit=&cursor=` | viewer | `{not, tzdata, overdue, banners, items, next_cursor}`; `overdue` counts every schedule over `OVERDUE_MIN` late (one aggregate on `schedules_due_idx`), for monitors |
| `GET /v1/schedules/zones` | viewer | `{tzdata, zones, renamed}`: `cron.zones()` sorted, without the names of `cron.RENAMED` a host's data may hold (§3 refuses them), and `cron.RENAMED` |
| `GET /v1/schedules/{schedule_id}` (an id, or a live name; a tombstone by its id) | viewer | the item with `runs.approval` (the gate, read here only: `{needs, mode, state, approval_id, refusal}`, `state` being `approved`, `not_needed` or `unapproved` and `refusal` the gate's message a firing would meet), `upcoming` (5 × `{utc, local}`: the times after the database's `now()`, or after `anchor_at` while the clock is behind it, so a time due and not settled is the item's `next_due` and never one of them; none unless active), `firings` (newest 20) |
| `GET /v1/schedules/{schedule_id}/firings?limit=&cursor=` | viewer | the `schedule.fire`, `.missed` and `.rezone` rows as `{seq, at, outcome, due, to, count, late_s, run_id, reason, code, next, text}` (`outcome` is `fire`, `missed` or `rezone`; a rezone has no `due`, and its new time is `next`): no actor, address or request data, so no `audit.read` (as READINESS's transitions); 400 `malformed cursor` |
| `POST /v1/schedules/preview` `{cron, time_zone}` | operator | `{upcoming: [10 × {utc, local}], notes, tzdata}` after the database's `now()`, `notes` being §9's warning of a zone whose name holds a digit, then the clock changes' notes; writes nothing |
| `POST /v1/schedules` | operator | 201 the item, `warnings` |
| `PUT /v1/schedules/{schedule_id}` | operator | the body plus `revision`; 200 the item, `warnings` |
| `POST /v1/schedules/{schedule_id}/pause`, `/resume` | operator | 200 the item (`warnings` at a resume) |
| `DELETE /v1/schedules/{schedule_id}` | operator | 200 `{schedule_id, deleted_at}` |

**Item:** `{schedule_id, name, spec_name, spec_id, runs: {spec_id, version}, newer, run, cron, time_zone,
start_within, ends_at, state, auth_mode, revision, created_by(_name), created_at, set_by(_name), set_at,
paused_by(_name), paused_at, next_due: {utc, local}, overdue, clock_behind, last: {at, run_id, run_status,
text}, held: {due, to, count}}`; `runs` is what a firing would run now and `newer` the stored version past a
pin, both from the list's one SQL join: a list compiles no spec. `local` is the zone's wall clock
(`2026-10-02 02:00`; null when this build cannot load the zone); `clock_behind` is `now() < anchor_at`
while a time is due next; `last.run_status` is the status of the run `last` names, when it names the
newest the schedule started. `state=` is `active`, `paused`, `ended` or `deleted` (else `invalid request`;
without it, all but the deleted). `GET /v1/runs` gains `schedule_id=` (the index serves
it); `GET /v1/runs/{id}` gains `schedule: {schedule_id, name, due, time_zone, late_s, text}` or null, from
`params.schedule`, `created_at` and the schedule's row (`text` by `schedules.line`). The ten routes join
`test_roles.ROUTES`.

**CLI** (`schedules_app` in `cli/main.py`, as `notify_app`; AUTH §4.2 rows):

| Command | Audit | `DS_OPERATOR` |
|---|---|---|
| `ds schedules [--spec NAME] [--state S] [--set-by ACTOR] [--json]`; `ds schedules show NAME\|SC-… [--json]` | — | no |
| `ds schedules next CRON --tz ZONE [--count N]` (no store, no settings) | — | no |
| `ds schedules add NAME (--spec-id N \| --spec NAME) [--soak [--rolling]] --cron TEXT --tz ZONE [--start-within MIN] [--ends TIME]`; `ds schedules set NAME\|SC-… …` | `schedule.create`; `schedule.update` (after `schedule.missed` if a time was due) | yes |
| `ds schedules pause (NAME\|SC-…\|--all)`; `resume NAME\|SC-…`; `delete NAME\|SC-…` | as the API's | yes |

Output: `added SC-… (nightly-customers): next 2026-10-02 02:00 Europe/London (2026-10-02T01:00Z)`; `saved
SC-…: next …` (`saved SC-…: paused`); `paused SC-…`; `paused 14 schedules` (`paused 1 schedule`); `no active
schedules`; `resumed SC-…: next …` (`resumed SC-…: ended` with S9's warning); `deleted SC-…`; `no schedules`.
Exit 0; 2 with §9's message; 3 for a bug.
- `ds schedules` prints `schedules.NOT`, the banners, then each schedule in one line (id, name, `pinned
  1.4.0 (spec 12)` or `follows customer-profile: 1.4.0 (spec 12) now`, mode, cron and zone, `next …` or its
  state, and `last: <text>` with its run's status); `--json` prints the list answer with every schedule in
  it; `--state` other than the four states is `error: --state is one of active, paused, ended, deleted`.
  `show` adds the revision, who saved it, the gate's state, the next five times and the firings' texts.
- `ds schedules next` prints one `2026-10-02 02:00 Europe/London (2026-10-02T01:00Z)` line a time (10 by
  default, 100 at most) after the host's clock, and each note as `warning: …` on stderr.
- `ds schedules set` reads the schedule, replaces what its options name and keeps the rest, and saves it at
  the revision it read (S11's 409 if it changed meanwhile). `--spec-id` pins and `--spec` follows;
  `--soak/--batch` and `--rolling` rebuild the run options (`--rolling` alone keeps the mode). No option
  clears `ends_at` or renames: a `PUT` does. `--ends`' help reads `No time is due after this ISO-8601 time
  with a UTC offset; a run for the last time before it may still start within its window.`
- `ds schedules pause` with neither a schedule nor `--all`, or with both, is `error: name a schedule, or
  pass --all`. `--all` pauses each active schedule in a transaction of its own (`schedules.pause_all`).

**UI** (`web/src/Schedules.tsx`; `web/src/scheduling.ts` holds presets and labels, pure, named so that
`./Schedules` resolves to one file on a case-insensitive file system, as `ready.ts` is beside
`Readiness.tsx`; `api.ts` the types). The screens poll every 60 s:
- `#/schedules`, in the nav after Specs for every role: `schedules.NOT`, banners, then a table of name,
  spec and version (`pinned 1.4.0 (spec 12)` or `follows customer-profile: 1.4.0 (spec 12) now`), mode, cron
  and zone, next due in the zone and in UTC with `(a schedule, not a forecast)`, the last firing's server
  text with its run's status, and state (`(a schedule, not a forecast)` heads the due-time column).
  Operators get a form: name, spec (a stored version), follow switch, mode (a soak's rolling switch),
  presets (daily, weekdays, weekly, monthly, every N hours) or custom cron, zone, start within, end, and
  Preview (the next ten times and the notes). A preset only writes cron text, shown beside it
  (`cronOf`): daily `M H * * *`, weekdays `M H * * 1-5`, weekly `M H * * D`, monthly `M H D * *` on a
  day 1 to 28, every N hours `0 */N * * *` for an N that divides 24; custom starts from the text the
  preset wrote, and a stored schedule is edited as custom. The end is typed as an ISO-8601 time with an
  offset (S9 judges it). The zone list is the server's (`/zones`); its default is the browser's zone
  when listed, else the browser's zone through `renamed`, else `UTC` with `your browser's time zone
  (Foo/Bar) is not in this build's zone data: UTC is selected` (`scheduling.ts defaultZone`, pure). A
  stored schedule's zone that is neither listed nor renamed (T6) is never replaced for its editor: the
  select is required and starts at `choose a zone`, under `this build's zone data does not hold
  Europe/Kyiv, the zone this schedule was saved with: choose its time zone`. A save shows the answer's
  warnings.
- `#/schedules/SC-…`: definition, revision, who saved it, the gate's state, next five, firings paged; for
  operators Edit, Pause, Resume, Delete (confirmed) and Move to the newer version. Edit saves at the
  revision the schedule had when Edit was pressed, and Move is a `PUT` of the stored definition with the
  newer `spec_id`; a tombstone offers none of them.
- `Run.tsx` prints the run's `schedule.text`, linking to the schedule. `Specs.tsx` reads the list once (at
  most four pages), groups it by `spec_name` and shows each name's schedules with their next due on
  each of the name's versions, and for operators `Schedule…`, which opens the form with that version
  (`#/schedules?spec_id=12`).

## 11. Audit actions (added to AUTH §6 and RETENTION §8)

| Action | Actor | Detail (object `SC-…`) |
|---|---|---|
| `schedule.create`, `schedule.update` | the caller | `{name, spec_name, spec_id, run, cron, time_zone, start_within, ends_at, auth_mode, revision, next_due}`; update adds `changed: [field]` |
| `schedule.pause`, `schedule.resume`, `schedule.delete` | the caller; `schedule.pause` also `system:worker` (T6) | `{revision, next_due}`; the worker's adds `reason: zone`, `code` (`zone` or `cron`), `tzdata` (its tz data version), `missed` (whether a `schedule.missed` row of reason `zone` is chained before it in the same transaction: false when no time was due, and the pause's row is then what the notifier sends, §8) and, for rules to match, `spec_name` and `mode` |
| `schedule.fire` | `system:worker` | `{due, run_id, spec_id, spec_name, mode, revision, time_zone, late_s, next}` |
| `schedule.missed` | `system:worker`; the caller for `superseded` and a `held` range its write flushes | `{reason, code, due, to, count, spec_name, mode, spec_id, run_id, draw_id, error_type, revision, time_zone, start_within, auth_mode, worker_mode, tzdata, next}`, every member in every row (`schedules.missed` builds it); `to` and `count` are set on a counted range only (`late` `down`, `superseded`, a held range), null for a time settled by itself; `start_within` and `auth_mode` are the schedule's as saved at that `revision` and `worker_mode` the firing worker's auth mode (reason `mode` only, else null) and `tzdata` its tz data version (reason `zone` only, else null): what the row's text is built from (§9); `next` is the time due after the row's last: for the earlier rows of a `late` settlement the next of its times, and for a settlement's last row what the schedule's `next_due` becomes (null once it is paused, deleted or ended) |
| `schedule.rezone` | `system:worker` | `{from, to, tzdata}` |
| `create_run` (amended; object `R-…`) | as today | gains `schedule: {schedule_id, due, revision, time_zone}` |

## 12. Migration `015_schedules.sql`

```sql
-- Schedules (docs/mvp/SCHEDULES.md): a standing request to start a run of a stored spec at cron times in
-- a time zone. Rows are never deleted: a run names its schedule. The index on ds.runs is built first,
-- under SHARE (reads go on, run writes wait while it reads every row once); the check is NOT VALID and
-- last, so its ACCESS EXCLUSIVE lock scans nothing and is held only to the end of the migration.
CREATE TABLE ds.schedules (
    schedule_id  text PRIMARY KEY CHECK (schedule_id ~ '^SC-[0-9A-Z]{26}$'),
    name         text NOT NULL CHECK (name ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(name) <= 63),
    spec_name    text NOT NULL,
    spec_id      bigint REFERENCES ds.specs,       -- pinned; NULL: the name's newest version at each firing
    run          jsonb NOT NULL CHECK (jsonb_typeof(run) = 'object' AND run->>'mode' IN ('batch', 'soak')
                                       AND NOT run ?| ARRAY['keys', 'spec_id']),
    cron         text NOT NULL CHECK (length(cron) <= 100 AND cron ~ '^[0-9A-Za-z*,/ -]+$'),
    time_zone    text NOT NULL CHECK (length(time_zone) <= 64 AND (time_zone = 'UTC'
                                       OR time_zone ~ '^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)+$')),
    start_within integer NOT NULL DEFAULT 60 CHECK (start_within BETWEEN 15 AND 1440),
    ends_at      timestamptz,
    auth_mode    text NOT NULL CHECK (auth_mode IN ('none', 'tokens', 'oidc')),
    revision     integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    set_by       text NOT NULL,                    -- its creator, then each PUT's caller
    set_at       timestamptz NOT NULL DEFAULT now(),
    paused_by    text,                             -- a person, or system:worker (a zone or cron it cannot load)
    paused_at    timestamptz,
    deleted_by   text,
    deleted_at   timestamptz,
    anchor_at    timestamptz NOT NULL DEFAULT now(),  -- next_due is the first cron time after it
    next_due     timestamptz,                         -- NULL: paused, ended or deleted
    last         jsonb CHECK (jsonb_typeof(last) = 'object'),  -- its newest settlement's detail and outcome
    last_at      timestamptz,
    last_run_id  text REFERENCES ds.runs,             -- the newest run it started; a miss keeps it
    held         jsonb CHECK (jsonb_typeof(held) = 'object'),  -- a standing refusal's range not yet chained
    CHECK ((paused_at IS NULL) = (paused_by IS NULL)),
    CHECK ((deleted_at IS NULL) = (deleted_by IS NULL)),
    CHECK (next_due IS NULL OR (paused_at IS NULL AND deleted_at IS NULL AND next_due > anchor_at)),
    CHECK ((last IS NULL) = (last_at IS NULL))
);
CREATE UNIQUE INDEX schedules_name_idx ON ds.schedules (name) WHERE deleted_at IS NULL;
CREATE INDEX schedules_due_idx ON ds.schedules (next_due) WHERE next_due IS NOT NULL;
CREATE INDEX schedules_spec_idx ON ds.schedules (spec_name) WHERE deleted_at IS NULL;
-- One run per schedule and due time, for every writer:
CREATE UNIQUE INDEX runs_schedule_due_idx ON ds.runs
    ((params->'schedule'->>'schedule_id'), (params->'schedule'->>'due')) WHERE params ? 'schedule';
ALTER TABLE ds.runs ADD CONSTRAINT runs_schedule CHECK (NOT params ? 'schedule' OR (
    params->'schedule'->>'schedule_id' ~ '^SC-[0-9A-Z]{26}$'
    AND params->'schedule'->>'due' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z$')) NOT VALID;
```

No trigger, no function, no change to the actor CHECK or to `ds.approval_authors`, and no `store/db.py`
constant (015 needs no apply-time handling; `migrate` applies all pending files in one transaction).
`docs/architecture/control-plane.sql` gains the table. `store/repo.py` gains a "schedules" section of
plain SQL: `new_schedule_id` (`_sortable_id("SC-")`), `insert_schedule`, `get_schedule` (by id or live
name), `list_schedules` (joining each row's spec version and the name's newest), `count_schedules` (`lock=`
takes the advisory lock `DSSC`, 0x44535343, to the transaction's end: a create's count),
`overdue_schedules`, `replace_schedule` (compare-and-set on `revision`), `pause_schedule`,
`resume_schedule`, `delete_schedule`, `schedules_due` (each row with the database's `now`),
`settle_schedule`, `release_held` (a range 24 h
old, chained while nothing is due), `rezone_schedule`, `setter_live`, `schedule_firings`
(on `audit_log_object_idx`) and `list_runs(schedule_id=)`. `repo.create_run` is unchanged.

## 13. Seams

- **Table pre-checks** ([PRECHECKS.md](PRECHECKS.md) D14): built, they add no `RunOptions` field, so
  nothing new is scheduled; a schedule kind for table pre-checks is a later seam (until then a site's
  scheduler calls `ds precheck run --all-specs --fail-on incomplete`). **Engines and
  files:** a firing is `create_run`, so a side that cannot be read is `not_started`. A firing that plans
  a file side holds one of the worker's two firing tasks while the export is hashed and counted, at most
  `files.plan_s` a firing (its file sides together); schedules queued behind two such firings past their
  window are `late`, code
  `busy`. Set the cron after the export's latest arrival, `start_within` over its variance, and stagger
  exports ([DEPLOY.md](DEPLOY.md), section 7).
- **READINESS Y7**: `schedules.upcoming(conn, spec_name)` lets its tail read
  `; schedule nightly-customers is next due 2026-10-08T01:00Z (a schedule, not a forecast): each batch run
  of it that ends complete adds one` in place of `; no live soak of … will add cycles`. Not built here.
- **Cross-run digest baseline** for alerts: `params.schedule.schedule_id` names a run's predecessor.
- **Later:** the setter in `ds.approval_authors` (through `params.schedule`); step-up
  (`require_fresh("operator")`) for saves; a four-eyes kind for schedules; retry policy, blackout windows
  and a start date as columns; a heartbeat a monitor can read while no worker runs. Several workers
  are safe already (the compare-and-set and the index; `test_two_firers_race_one_run`).

## 14. Documents to amend

New `docs/mvp/SCHEDULES.md`. **CONTRACTS** §1 (the four new modules), §3 (015), §4 (`create_run`'s
`schedule`; the worker's firing tasks; `run_worker`'s `schedules`), §7, §8, §10. **AUTH** §4.1 (ten routes;
firings as a projection), §4.2 (the CLI rows; the row of `ds worker` gains `schedule.fire`, `.missed`,
`.rezone` and `.pause`, and says `ds run`'s in-process worker fires and rezones nothing), §4.3, §5 (a
token's expiry or revoke does not stop the schedules it saved), §6. **APPROVALS** §3 (a scheduled run is
`system:`-started), §4 (the gate at each firing), §10. **LICENCE** §1, §2, §7, §8. **RETENTION** §4.2, §4.3,
§8. **SAMPLING** §5, §9, §15. **READINESS** §6, §15 (the Y7 seam). **ALERTS** §2.1, §2.3 (`SC`), §2.4,
§3, §6.2, §10, §12, §14. **PACK** §2.3. **ACCURACY** §4 (`started_run`). **TABLES** §11 (a soak's `cycleInterval`
against a cron schedule). **DEPLOY** §3 (nothing to configure; NTP; tz data ships in the image, so run
`ds schedules` with `docker compose run --rm app`, the image the API and the worker run), §10 (the Upgrade
row: `Migration 015 (schedules) reads every row of ds.runs once to build an index; run writes wait
meanwhile, reads go on`; restore: `ds schedules pause --all` before `ds worker` or `ds serve` starts on a
restored or copied database; the outside check of §16; moving to SSO means saving each schedule again),
§11. **RUNBOOK**: a new "Schedules" section (declare, preview, pin or follow, DST, read firings, the
reasons and the tries, a schedule the worker paused, pause, stagger shared minutes, the leaver step,
sampled schedules, which alerts to route, the outside check, and pausing schedules when a licence lapses
or the mode changes). **RELEASE** §9 step 5 (the smoke test also
runs `ds schedules next '0 2 * * *' --tz Europe/London --count 1` in the built image: `the smoke test
failed: the image's time zone data does not load Europe/London`), §13. **PENTEST** §2.
`docs/architecture.md` §4.3, §4.4, §4.11, §6 and `control-plane.sql`. README; PLAN; the docstrings of `runner/worker.py` and `runner/runs.py`.

## 15. Tests

Fake clock: `core/cron` takes instants, `decide` is pure, `fire(now=)` is injected. Zone tests never depend
on the host's tz data: they use names every tz release holds, `Mars/Olympus` for an unknown one, `RENAMED`
for the alias case and a monkeypatched `cron.tzdata`. The suite's local `postgres:16-alpine`; nothing is
pulled.

| File | Tests |
|---|---|
| `unit/core/test_cron.py` | `test_messages` (C1–C9, letters and digits beyond ASCII among C3's, Z1 both forms, Z2); `test_only_the_trimmed_text_is_remembered`; `test_localtime_factory_and_bare_names_refused`; `test_gap_fires_once_later` (London, New York, Lord Howe); `test_repeat_fires_first`; `test_hourly_through_both_nights`; `test_leap_day`; `test_spacing_across_midnight`; `test_notes_name_each_shift` (a gap crossing midnight in America/Nuuk names the next day); Hypothesis: `after` strictly increases, `latest(after(t)) == after(t)`, `between` equals iterated `after`, each result matches the fields in local time or is a shifted gap time |
| `unit/runner/test_schedule_fire.py` (helpers in `runner/scheduling.py`) | `test_what_saving_a_schedule_checks` (S1 to S10, the two route names, C and Z through a save, the warnings, a zone named by its offset among them, the 200 limit: at the functions the API and the CLI call); `test_the_limit_holds_under_concurrent_creates` (four creates at 199: one stored); `test_scheduled_runs_are_evidence_points`; `test_fires_once`; `test_two_firers_race_one_run` (two connections, one due time); `test_forged_second_run_is_fired_already`; `test_noncanonical_due_refused`; `test_kill_before_commit_refires_in_window`; `test_put_pause_delete_during_firing_leave_nothing`; `test_put_pause_delete_settle_due_times`; `test_put_on_paused_stays_paused`; `test_resume_past_ends_is_ended`; `test_overlap_waits_then_starts`; `test_overlap_settled_when_window_closes`; `test_abandoned_run_ends_overlap`; `test_thirty_days_down_one_range_row_no_run`; `test_wide_window_starts_newest_only`; `test_clock_forward_and_back`; `test_save_after_clock_back_refires_nothing`; `test_ends_at`; `test_resume_without_catch_up`; `test_other_mode_missed`; `test_disabled_or_demoted_setter`; `test_setter_gone_during_the_listing_starts_no_run` (disabled, and demoted, while `resolve` runs); `test_token_rotation_keeps_schedules` (expired, revoked, then its owner disabled); `test_unapproved_then_granted_then_revoked`; `test_licence_expired_sso_pairs_then_renewal_no_backlog`; `test_standing_refusal_one_row_then_one_a_day`; `test_new_nonce_each_firing`; `test_r1_waits`; `test_unclosed_draw_blocks`; `test_rate_sample_free`; `test_follow_takes_new_version`; `test_pinned_keeps_version`; `test_not_started_three_tries` (an `OperationalError` among them); `test_a_try_is_judged_on_the_clock_after_the_attempt` (a monkeypatched `schedule.monotonic`: the attempt, the wait and a sweep past the window settle the reason at once); `test_a_newer_time_counts_its_own_tries`; `test_listing_past_window_times_out`; `test_commit_after_window_not_started`; `test_queue_past_window_is_busy` (queued in its window; queued after it: `down`); `test_queued_time_closed_by_the_next_is_busy` (an hourly time, and one between two `down` ranges); `test_bug_is_error`; `test_audit_rows_last_in_order`; `test_planted_key_reaches_no_row_log_or_delivery`; `test_unscheduled_run_unchanged`; `test_rezone_audited`; `test_unknown_zone_pauses_never_stops_sweep` (in `rezone`, with a missed row only for a time that is due and none beside the later firing of one that was not, and in `fire`; the pause row's `missed`, `spec_name` and `mode`); `test_a_pause_before_a_time_is_due_is_notified` (a real feed pass: one delivery for the pause before its time, by its pause row, one for the pause at a due time, by its missed row, none for a person's pause) |
| `unit/core/test_notify_events.py` | also `test_planted_texts_never_reach_a_body` over a `schedule.missed` row and a worker's `schedule.pause` row |
| `unit/runner/test_worker.py` | `test_two_firings_at_most`; `test_times_lost_to_an_outage_are_down_never_busy` (five schedules, two slots, a real sweep); `test_a_count_of_tries_is_its_due_time_s_own` (two failed tries, a person's save, then the next time's first of three); `test_firing_fault_never_fails_a_run_or_the_sweep`; `test_escaped_fault_waits_no_poll_loop`; `test_no_lock_no_firing`; `test_ds_run_worker_fires_nothing` |
| `unit/api/test_schedules_api.py`, `test_roles.py`, `test_licence_api.py` | roles in each mode; S1–S11, C1–C9, Z1 and Z2 with their statuses, through a save and a preview (a cron name beyond ASCII and an `ends_at` no row holds among them: never a 500); a body's `cron` over 1,000 characters and `time_zone` over 64, and padded previews remembered as one text; the 200 limit; warnings; `test_a_zone_named_by_its_offset_is_warned_of`; `test_upcoming_counts_from_now_while_a_time_is_overdue`; a held range; preview writes nothing; `zones`; the list compiles no spec; `overdue` and each banner; the firings projection without actors; the run's `schedule.text` in the firing's zone after a `PUT` changed it; the list filter; `test_firing_texts` (every §9 firing text, golden; a zone miss reads the same under another reader's tz data); `test_never_told` (N1–N7, with a no-key canary: a refusal's message holding a key reaches no answer); `test_nothing_locks_out` past the grace |
| `unit/test_approvals.py` | `test_scheduled_run_needs_two_approvers`; unscheduled runs judge as before |
| `unit/store/test_migration_015.py` | applies over 001–014 with runs; each CHECK (`localtime` and `GMT` refused); both unique indexes; the index statement precedes the check; no rewrite of `ds.runs` |
| `unit/core/test_notify_events.py`, `test_notify_rules.py`, `unit/test_config.py` | projection per reason and code; the model against planted names and unsafe values; dedup; severity (§8's table); golden text; `specs` globs and `modes` matched from the detail; `reasons` beside `schedule.missed` refused; `test_the_worker_s_pause_before_a_time_is_due` (each code: facts, alert, dedup key, golden title and text; a pause beside its missed row and a person's pause project nothing; a null `due` beside another reason fails the model) |
| `unit/reports` pack tests | `test_scheduled_run_pack_passes_gate` (an integer key pattern; a zone name in `plan.json`; a two-step sign-off; `Etc/GMT+5` refused under a pattern a digit matches); no `SC-` row in its segment |
| `unit/cli/test_cli.py`, `unit/test_release_tool.py` | commands, outputs, exit codes (2 with §9's message, 3 for a bug), `DS_OPERATOR` before any work, `next` offline (a zone named by its offset warned of, a cron name beyond ASCII refused), `--ends`' help, an `ends_at` no row holds and the name `zones` exit 2, `show`'s next times while overdue, `pause --all` with none active, the missed row before a `set`'s own; the smoke step's argv and message (docker stub) |
| `web/test/schedules.test.ts` (of `web/src/scheduling.ts`) | presets to cron; the form's body (pinned or followed, never a key) and a move's; no screen builds a firing text, a banner or a share (the sources of `scheduling.ts`, `Schedules.tsx`, `Run.tsx` and `Specs.tsx` hold none of §9's wordings); `defaultZone`: `Asia/Calcutta` gives `Asia/Kolkata`, an unlisted zone gives `UTC` and the note, a stored schedule's unlisted zone gives none, the note naming it and a required select; the labels of `ds schedules`; the list read over its pages; the requests each control sends |
| `e2e/test_schedule.py` | on a clean table pair (a scan is a census; a mock pair lists its keys inline, a `keys` basis no streak counts, and seeds discrepancies), three rounds: each sets `cron` to a daily time at a different minute just past and back-dates `anchor_at` and `next_due` to it by SQL (`next_due > anchor_at`), and the real worker starts one batch run; the three runs meet the convergence gate. On the mock pair: a `ds worker` killed after it started the due time's run loses and duplicates nothing (one run, one `schedule.fire`, every key once) |

## 16. Limits, deferred, size

- **Honest limits.**
  - Not a dead-man's switch (N7): while no firing worker runs, nothing is recorded or sent, and the miss
    is reported as one `late` range on recovery. The outside check: a SIEM absence rule (no `schedule.fire`
    or `schedule.missed` for `SC-…` within its period), or a monitor polling `GET /v1/schedules?limit=1`
    with a viewer token for `overdue`, alarmed once it stands longer than the largest `start_within` (a
    worker legitimately retrying shows there too). `GET /v1/health` does not cover it (LICENCE D10); the
    app container's healthcheck covers the `ds serve` process, not a separate `ds worker`.
  - A source down past three tries (or past the tries its window has room for: each waits 300 s and a
    sweep), or a worker down past the window, costs that firing: the alert and a hand-started run are the
    recovery. A weekly schedule down for over a day loses its week.
  - A firing whose spec has a file side ([FILES.md](FILES.md) §12) plans inside `create_run`: it holds
    one of the worker's two firing tasks while the export is hashed and counted, at most `files.plan_s` a
    firing (its file sides together), so two such firings at once make every other schedule wait, and one
    past its window meanwhile is `late`, code `busy`. A firing the file reader was too busy to take
    (`planning`) is waited for as an export not ready is. An export not ready is waited for inside the window only: past it the time is
    missed with its code, and the next due time tries the export as it then is.
  - While `ds run`'s worker, or `ds keys migrate`, holds the lock, nothing fires; the times are judged
    under D6 afterwards.
  - Two firings at once: table scans start within seconds, but specs with a listed key source start two
    at a time for as long as their listings take (each at most its window), and schedules queued behind
    them past their window are `late`, code `busy`. Stagger shared minutes; scans are not capped.
  - A standing refusal chains two rows on its first day and one a day after, per schedule (at most 200 a
    day); up to 24 h of its times sit in `held`, unchained. Other reasons chain one row per due time (at
    most 96 a day per schedule); notifications de-duplicate by `notify.dedup_hours` only.
  - The setter is not excluded from a scheduled run's sign-off and may be one of its two approvers. The
    setter check reads stored roles, which change only at the person's next sign-in, never IdP groups; it
    is read as a firing begins and again in the transaction that inserts its run, which does not lock the
    person's row: a disable or a demotion stored after that second read does not stop that run. A revoked
    or expired token's schedules go on firing, and in tokens mode no setter check can stop one.
  - A following schedule runs whatever version was stored last; in tokens mode nothing gates it. A
    `ds worker` in the schedule's own mode under another config still fires it (AUTH §4.2's bypass).
  - A restored or copied database starts one run of each active schedule whose newest due time is still in
    its window, against the declared targets, unless paused first.
  - On a clock-change day two firings can fall closer than 15 minutes: C8 measures the wall clock, and a
    time the clock skips fires later by the jump, which can be a minute before the cron's next time
    (`0,59 1,3 * * *` in Europe/London fires at 01:59Z and 02:00Z on 2027-03-28; `14,45 2 * * *` in
    Australia/Lord_Howe at 15:44Z and 15:45Z on 2026-10-03). The first time's window closes at the second,
    and the notes name the shift, not the closeness. A clock set back stalls schedules,
    shown only by `clock_behind`. Zone rules change only with a release; a zone a release drops pauses its
    schedules (T6). One paused so before its time is due (at a restart's rezone) writes no missed row:
    its `schedule.pause` row (`code`, `tzdata`, `missed: false`) is sent as a `schedule.missed` alert
    without a time (§8) where a rule routes that event for its spec and mode, and the list's banner and
    its paused state say it besides; its times while paused are neither due nor missed (§3). The alert
    has the dedup key of a `zone` miss of that schedule: within `notify.dedup_hours` of either, the other
    is held back as a duplicate.
  - `ends_at` bounds the due times, not the starts: a run for the last time before it may start up to
    `start_within` minutes after that time, a wait for a slot or for a live run included.
  - A schedule in a zone named by its offset (`Etc/GMT+5`) under a numeric key pattern that a one- or
    two-digit number matches gets no evidence pack for its runs: the raw-value gate reads the zone's
    number in `plan.json` and the `create_run` row as a key. Use a region's zone, or `UTC`. A save warns
    of every zone whose name holds a digit and a preview notes it (§9); neither reads the spec's key
    pattern, and the zone list still offers the names.
  - `not_started` and `error` record the type alone; a PostgreSQL side's and the store's
    `OperationalError` read the same. The notifier takes a row's `error_type` by its pattern, as
    `run.failed`'s: a forged row whose `error_type` is an identifier-shaped text would send it. The constants (15 minutes, 200, two tasks, three tries, 24 h) were chosen without
    measurement.
- **Deferred:** §13's seams; a run-now button; per-schedule retry settings.
- **Size.** About 2,300 Python product lines (1,460 in the
  four new modules, 300 in `repo.py`, 240 in the CLI, 140 in `runner/worker.py` and `runner/runs.py`, 110
  in `core/notify.py`), 46 of SQL, 580 of TypeScript and 4,250 of tests.
