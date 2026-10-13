# Alerts and notifications: contract

This contract is binding. It builds alerts and notifications on [AUTH.md](AUTH.md), [PACK.md](PACK.md) §4,
[APPROVALS.md](APPROVALS.md), [LICENCE.md](LICENCE.md) (O2), [SAMPLING.md](SAMPLING.md) and architecture §4.3
and D.2, and amends the documents in §15. [READINESS.md](READINESS.md) §8 amends §2.1, §2.3, §2.4, §3, §6.2, §6.4,
§10, §12 and §14 for the event `readiness.changed`. [SCHEDULES.md](SCHEDULES.md) §8 amends §2.1, §2.3, §2.4, §3,
§6.2, §10, §12 and §14 for the event `schedule.missed`.

**Goal:** during a parallel run, owners hear on a signed HTTPS webhook, SMTP e-mail, Slack or Teams when a cycle
observes new gaps or crosses a threshold, a run stops, pauses or fails, a draw is refused or a sample judged, an
approval waits or is revoked, or the licence changes state; a failed channel never touches a run, and nothing leaves but
ids, counts, closed codes, a spec name and a link. **Shape:** each such fact is already an audit row written last in
the transaction that makes it true (AUTH §6), and 009 chains every audit row under the DSAU lock in commit order: **the
chain is the outbox.** A worker task follows it from a durable cursor, turns chosen rows into typed events, matches
rules declared in settings and, in the transaction that moves the cursor, stores one rendered delivery per event and
channel; a task per channel sends them in order, with retries, on threads of the notifier's own. No run path changes; no
new dependency (aiohttp, pydantic, stdlib `smtplib`, `email`, `hmac`, `concurrent.futures`); every claim stays
*observed*.

## 1. Decisions (each one seam)

- **D1** The chain is the outbox: no `ds.outbox`, no call from `runner/cycle.py` or any run
  path; architecture §4.3's generic outbox stays deferred.
- **D2** Channels and rules live only in settings (config change control, applied at restart), never in an immutable,
  approved spec; no API creates a destination. Tests, retries and mutes are the only runtime acts, each audited.
- **D3** Events concern a cycle, run, draw, approval, licence or channel, never a key; `one_shot` runs notify only
  when a rule names them; a person's own pause never does.
- **D4** Payloads carry closed typed facts that fail closed (§2.3): never RC ids (unkeyed 48-bit hashes of masked
  parts), keys, tokens, path patterns, stratum labels, free text, person names or addresses.
- **D5** At least once, in order per channel; a URL holding a secret is itself a `file:` secret bound to a declared
  host; an explicit `notify.proxy`, declared as `auth.oidc.proxy` (AUTH §2.1) but guarded (§7), the environment
  never read.
- **D6** No licence fence (O2), as SAMPLING D10; a fence would be one line in `Standing.refusal`. An open
  build, which enforces no licence at all (LICENCE §19), adds `open` to the states of `licence.state`.
- **D7** Viewers read; operators test and retry; mutes need an admin signed in within 15 minutes (tokens mode: the
  CLI, AUTH §1).
- **D8** Severity `alert`, `warning`, `info`: alerts bypass the rate limit, never a mute or de-duplication; rules
  route pause-class and investigate-class events to different receivers (D.2).
- **D9** A notification problem never stops a process or reaches the sweep (§9): an unreadable secret warns and
  retries.

## 2. Events (`core/notify.EVENTS`: audit action → projector)

### 2.1 Catalogue

Run events carry `run {run_id, spec_id, spec_name*, spec_version, mode, now: {cycle, status, licence, approval}}`:
`now` is the run as the feed finds it, which may be later than the event (`licence` the run record's `state`,
`approval` from `approvals.run_approval`); the event's own cycle is `facts.cycle`, taken from its audit row: the cycle
a `run.cycle` ended (`detail.cycle − 1`), a refused draw's or a labelling's `detail.cycle`, else, for a row without
one (`paused`, `cancelling`, `run.finished`), the cycle its run's latest earlier `run.cycle` row started, 1 without one
(`repo.cycle_at`, on `audit_chain_object_idx`). A labelling's row (`accuracy.label`, object `V-…`) names its run in
`detail.run_id`: the feed reads the run from there. `*` is a label (§6.4). In the dedup keys, `s` is the event's audit seq.

| Event | From | Facts | Severity | Dedup key |
|---|---|---|---|---|
| `cycle.ended` | `run.cycle` (ended cycle `detail.cycle − 1`); `run.finished` with status `completed` or `completed_with_issues`; never with `ds.cycles.counters` `{}` | `cycle`, digest (§2.2) | warning if a condition held or crossed, or `different` or `indeterminate` > 0; else info | `cycle.ended\|R\|c` |
| `sample.judged` | `run.cycle`, and `run.finished` of any status (`fail_run` judges first), when `repo.get_draw(run, c)` exists | `{cycle, draw_id, method, status, strata: {passed, failed, unfinished}}`; `unjudged` when `ds.cycles.sample` is NULL | passed info; else warning | `sample.judged\|R\|c` |
| `run.finished` | `run.finished`, status not `failed` | `{status, reason, cycle}` | `completed_with_issues` warning; else info | `run.end\|R` |
| `run.failed` | `run.finished`, status `failed` | `{cycle, error_type}` | alert | `run.end\|R` |
| `run.stopped` | `cancelling` with a `STOP_PREFIX` reason; `run.finished` with `stop_condition:until` or `:maxDuration` for a run without a `cancelling` row (`at_boundary`) | `{cycle, condition}` | alert for `maxErrorRate`, `maxInconclusiveRate`, `maxConsecutiveErrors`, `maxUnexplainedCritical`; else warning | `run.stopped\|R` |
| `run.paused` | `paused` with a reason, but `run_paused_draw` and a revoke's `run_paused_approval` (one with an `approval_id`) | `{cycle, reason, approval_id?, draw_id?, licence_refusal?, schema?}` (`schema`: the schema guard's pause, each side's shape as it was seen, by its hash: PRECHECKS §9) | alert for `run_paused_auth_error`, `run_paused_key_missing`, `run_paused_schema_drift`; else warning | `run.paused\|R\|s`; the schema guard's pause: `schema.drift\|R\|s`, its `schema.drift`'s key |
| `sample.draw_failed` | `sample.draw_failed` | `{cycle}` | warning | `sample.draw_failed\|R\|s` |
| `accuracy.measured` | `accuracy.label` (ACCURACY §10): a labelling of an ended cycle was labelled | `{cycle, labelling_id, labelled, undecidable, not_reviewed}` (counts; never a label, a share or a hash) | info | `accuracy.measured\|V` |
| `approval.waiting` | `approval.request`; `approval.grant` with `step < of`; while still `requested` | `{approval_id, kind, spec_id, run_id?, cycle?, approvals_left}` | info | `approval.waiting\|A\|approvals_left` |
| `approval.revoked` | `approval.revoke`, `cascade_of` null | `{approval_id, kind, paused, stopped}` (counts) | alert | `approval.revoked\|A` |
| `licence.state` | `licence.state` | `{installation_id, from, to, licence_id, ends}` | `expired` alert; `ending`, `grace` warning; else info, `open` too: the one row of a build that enforces no licence, titled `no licence is needed` (`Installation I-…: no licence is needed (was <from, or unrecorded>); this build enforces none.`; LICENCE §19) | `licence.state\|to\|licence_id` |
| `readiness.changed` | `readiness.changed` (READINESS §8; object `readiness:<pair>`): a pair's state, `regressed` or a required gate's status changed | `{pair*, programme*, entity*, from, to, was_regressed, regressed, score: {passed, required}, causes, gates: {<gate>: <status>}, changed: [{gate, from, to}], evidence, candidate, signoff_id, pack_id, config_sha256}` (§2.3; never a gate's counts or its text) | info for a `baseline`; alert when `regressed` turned true, or a changed gate went `not_met` or `unknown` from `met`, or went so as the state fell; warning when a gate went `unavailable` or the state fell otherwise; else info (a gate that became judged, from `unavailable`, `not_applicable` or not required, without the state falling: a licence renewed) | `readiness.changed\|<pair>\|<to>\|<mask>` |
| `schema.drift` | `schema.drift` (PRECHECKS §7, §9; object `R-…`): what the schema guard recorded of a run's tables at its creation, a scan pass, a key-list cycle's start, a resume or a failed read, once a state; or a look it could not make (`checked: false`) | `{cycle, where, mode, checked, paused, paused_seq, basis, sides: {a, b: {breaking, other, kinds: {<kind>: n}, from, to}}, error_type}` (§2.3; counts, closed kinds, the basis run's id, hashes and the seq of the `paused` row it was written with: never a relation's or a column's name, nor a declared type) | alert when the run was paused; else warning | the pause's `schema.drift\|R\|<paused_seq>`; else `schema.drift\|R\|<to a>\|<to b>` |
| `schedule.missed` | `schedule.missed` (SCHEDULES §8; object `SC-…`), whoever wrote it: a schedule's due time, or a counted range of them, that started no run; `schedule.pause` by `system:worker` with `missed: false`: a schedule the worker paused before a time of it came due (`reason: zone`, `due` null) | `{schedule_id, reason, code, due, to, count, run_id, draw_id, error_type}` (§2.3; never the zone, the spec's name or a refusal's message) | alert for `late`, `superseded`, `zone`, `mode`, `setter`, `not_started`, `error`; warning for `overlap`, `approval`, `licence`, `sample` | `schedule.missed\|SC\|reason` |
| `notify.dead` | `notify.dead` (§5) | `{channel, delivery_id, event, attempts, error, dead_since_last}` | alert | `notify.dead\|channel` |
| `notify.skipped` | `notify.skipped` (§4) | `{reason, from_seq?, to_seq?, rows?, seqs?}` | warning | `notify.skipped\|s` |
| `notify.daily` | housekeeping, once a UTC day from `daily_hour`; the heartbeat whose absence shows the pipeline down | live runs by status, paused ones by reason (`manual` without one, `other` for any other), cycles ended and runs finished in 24 h, approvals waiting, licence state (the newest `licence.state` row's `to`: the notifier never calls `licence`), per-channel counts and the age of each pending head, the feed's lag (age of the oldest unread feed row, 0 when none), active mutes | info | `notify.daily\|date` |
| `notify.test` | test sends only (§11) | `{channel}` | info | `notify.test\|N-…` |

- Each pause and refused draw is its own event: a person resumes a paused run, so two pauses cannot flap unattended.
- A schema guard's pause is a `paused` row with reason `run_paused_schema_drift` and the two shapes' hashes
  (`schema`), written before its `schema.drift` row in one transaction: both are projected, the pause as any
  `run.paused`, so a rule that routes `run.paused` carries it unchanged. The pause and its `schema.drift` (which
  names the pause's seq, `paused_seq`) share a dedup key, the pause's seq: a channel routed both sends the
  `run.paused` and holds the other back as its duplicate (`dedup_hours: 0` sends both), and a later pause of the same
  tables, after a person resumed the run, is told again. A change that pauses nothing has its `schema.drift` alone, a
  warning, keyed by the shapes it went to (once a state); its cycle is the row's own (`concerned`), the one the guard
  looked in.
- `maxKeys` (a creation cap every capped run carries to its end) and `maxCycles` (the planned end) are `run.finished`
  reasons only. A run enters `cancelling` once at most, so `repo.last_audit(conn, "cancelling", obj=run_id)` (new
  `obj`, served by `audit_log_object_idx`) tells a boundary stop.
- A refused draw's `paused {run_paused_draw}` and `sample.draw_failed` share a transaction: only the second notifies,
  saying the run paused. `approval.revoked` counts a revoke's `paused` and `cancelling` rows.
- `readiness.changed` has no run block: rules match its pair as a spec (§3). `<mask>` is the required gates `not_met`
  or `unknown` as a 3-digit hex bitmask in `core/readiness.GATES` order, the first gate the highest bit (at most 102
  characters). Of a pair's readiness only the channel's newest pending or sent delivery makes a duplicate (§3): the
  same state and unmet gates recorded twice in a row within `dedup_hours` are one notification, and a fall that
  follows a delivered recovery is sent.
- `schedule.missed` has no run block either: rules match the spec's name and the mode its row's detail names
  (`detail.spec_name`, `detail.mode`), which no payload carries. Its reasons and their codes are closed
  (`core/notify.MISSED`): `late` (`down`, `busy`, `fired_already`), `superseded` (`update`, `pause`, `delete`),
  `zone` (`zone`, `cron`), `mode`, `setter`, `not_started` and `error` (with `error_type`; `not_started` also
  with a code where a file side's export was not ready, `export_absent`, `unsettled`, `stale`, `unstamped` or
  `planning`, its `error_type` `NotReady`: [FILES.md](FILES.md) §12), `overlap` (with
  `run_id`), `approval`, `licence` (`expired`, `sso`, `pairs`) and `sample` (`live`, `unclosed` with `draw_id`,
  `refused`). Of `late`'s codes, `busy` is a time that waited in its open window for one of the worker's two
  firing slots, and `down` any other time a worker finds past its window, an outage's among them (SCHEDULES §5
  step 2). `to` and `count` are set on a counted range only (a `late` `down` or `superseded` row, a standing
  refusal's later times: SCHEDULES §5 step 7). A started firing sends nothing, nor does a person's
  `schedule.pause`, nor the worker's when a `schedule.missed` row is chained before it (`missed: true`: that row
  is the notification). A schedule the worker pauses before its next time is due (a zone or cron its build
  cannot load, met at a restart) has its `schedule.pause` row alone (`missed: false`), which is projected to this
  event: reason `zone`, the pause's code, `due` and every other fact but `schedule_id` null, matched by the
  `spec_name` and `mode` the pause's detail names, with the reason's severity and dedup key (SCHEDULES §4 T6, §8).
- `reason` is sent only as `cancelled`, `erasure`, `abandoned`, `approval_revoked` or `stop_condition:<name>`, else
  null. `error_type` is the reason (`failure(exc)`) when it matches `^[A-Za-z_][A-Za-z0-9_]{0,63}$`, or what stands
  before its first `: ` when that does: a table side's failure is `<class>: <code>` and the SSRF guard's
  `UnsafeTargetError: <its text>` ([ENGINES.md](ENGINES.md) §12 F2 to F8), sent as `TargetTempSpace` or
  `UnsafeTargetError`, never the code or the address. A file side's failures are of that form too
  ([FILES.md](FILES.md) §15): `FilesChanged`, `RowsMoved`, `TargetTempSpace`, `TargetDenied` or `TargetRefused`
  is sent, never the position of a file or a count; and of a file set that ended its reader's process three
  times ([READER.md](READER.md) §12, F12) `ReaderDied`, never the cause. Otherwise `SecretError` when it holds
  a space or `/` (of the reasons left only a `SecretError`'s text does, and it names a host path, never sent);
  else `Error`.
  Revoke reasons, notes, draw errors and a grant's `authors` are never read.

### 2.2 The cycle digest (`notifications.digest`)

Computed at feed time for a `cycle.ended` a rule routes (the notifier's `_digest` calls it), and by `ds notify
explain` (§11), which is why it lives outside the notifier (§9, guarantee 1). Each query runs in a savepoint, under the
feed's 30 s statement timeout at feed time (explain sets none); a `QueryCanceled` makes its fact null, listed in
`unavailable`, as when its cycle is purging. The facts:
`cycle`, `counters`, `levels {mismatch_rate, indeterminate_rate, coverage, unexplained_critical}` (a counter's
`unavailable` is null), `previous {<level>: {cycle, value} | null}`, the four change facts, `baseline_from`,
`between_samples` and `unavailable` (change facts and `unexplained_critical`; a level whose baseline read timed out, which
is then not judged).

- `counters` (`total, compared, match, different, indeterminate, cancelled, outstanding, mismatch_rate, coverage,
  absent_both`), from the `ds.cycles` tombstones. A level fact's **baseline** is its value in the latest earlier
  cycle where it is not null (`repo.previous_fact`, backwards on the `ds.cycles` key; `unexplained_critical`: the
  latest earlier cycle whose purge has not started); `previous {cycle, value}` per fact. None: not held, as at c = 1.
- `new_root_causes` and `gone_root_causes` (counts, `repo.root_cause_changes`): RC ids of c held by no retained earlier
  cycle (`baseline_from`, the earliest), and of c−1 absent from c.
- `newly_discrepant` and `no_longer_discrepant`, **over the keys read in both cycles**: `repo.discrepant_changes` joins
  each cycle's discrepant subset (MISMATCH, NOT_FOUND_A, NOT_FOUND_B; `results_outcome_idx`) to the other cycle's
  results on `key_masked`, O(discrepant keys). Newly: discrepant in c, read in c−1 and not discrepant there; no longer:
  discrepant in c−1, read in c as MATCH, NOISE_ONLY or KNOWN_DIFF. So a rolling soak's keys entering or leaving the
  listing are not counted. A table scan's cycle whose scan ran to its end (`ds.scans` state `done`) uncut by maxKeys
  (`ds.cycles.keys_cut` null) read every key and re-read the keys that differed (TABLES §11), so a key without a
  result there was read identical; a cycle whose scan stopped at a checkpoint or was cut (TABLES §5) counts only keys
  with a result in both cycles, as a key list does. Other moves show in `indeterminate` and on the Changes tab; a
  KNOWN_DIFF under a `bug` rule counts in `different` only.
- **Baselines of the change facts.** At c = 1 they count against an empty baseline: every root cause and discrepant key
  is new (`observed in the first cycle`), the gone facts 0; so a batch run's gaps notify. Null with c−1 purged (root
  causes: with no earlier cycle retained). In a `hash_sample` run (`params.keys_source`) each cycle is a fresh sample:
  from c = 2 the key-change facts are null and root-cause changes are worded `between samples`.
- `unexplained_critical` of c: `repo.unexplained_criticals(conn, run, c, approvals.bug_rules(spec))` (`_bug_rules`
  made public).

### 2.3 Facts that may leave

Each kind has a pydantic model, `extra="forbid"`, every string a `Literal` or matching its pattern:
`^(R|A|D|LI|N|P|SC|V)-[0-9A-Z]{26}$` (`P` for `readiness.changed`, whose event names a pack; `SC` for schedules), `^I-[0-9a-f]{16}$`,
slugs, semver, ISO-8601 UTC (a schedule's due time to the minute, `^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z$`), `^[0-9]+(\.[0-9]+)?$`, `^[0-9a-f]{64}$` (readiness' `config_sha256`), `error_type`'s pattern or `unavailable`. A
projection failing its model is never sent: it is counted in the pass's `notify.skipped {reason: unsafe}` and logged
with its seq and kind. A bug can drop a notification, never leak one.

`ReadinessChanged` (READINESS §8): `pair`, `programme`, `entity` slugs (`programme` and `entity` or null); `from` a
readiness state `Literal` or null (the baseline), `to` a state; `was_regressed`, `regressed` bool; `score` two integers
0–12; `causes` a list of cause `Literal`s; `gates` a map of gate `Literal`s to status `Literal`s (the required gates:
the dedup mask is taken from it); `changed` at most 12 `{gate, from, to}` of `Literal`s (a status, or null for a gate
that was, or is, not required); `evidence`, `candidate` `{run_id, cycle}` or null; `signoff_id`, `pack_id` ids or
null; `config_sha256`. The worker writes spec names and the settings' slugs only, so no such row is skipped as unsafe.

`SchemaDrift` (PRECHECKS §9): `cycle` an integer from 1; `where` one of `creation`, `scan`, `cycle`, `resume`,
`read`; `mode` `finding` or `pause`; `checked`, `paused` booleans; `paused_seq` a count, the chain seq of the
`paused` row of its trip, or null when it paused nothing; `basis` a run's id or null; `sides` a map of
`a` and `b` to `{breaking, other` (counts)`, kinds` (a map of the closed kinds `core/notify.Drift`, which are
`core/precheck`'s, to counts)`, from, to` (64-hex hashes or null)`}`; `error_type` in `run.failed`'s pattern exactly
when `checked` is false. The projector copies these members and no other, so a name planted in a row is never
read; one planted in a member fails the model. `RunPaused` gains `schema`: a map of `a` and `b` to a hash or
null, null for every other pause.

`ScheduleMissed` (SCHEDULES §8): `schedule_id` an id (the row's object); `reason` and `code` `Literal`s, the code one of
its reason's or null; `due` and `to` (or null) ISO-8601 UTC to the minute, `due` null only beside reason `zone` (the
worker's pause before a time is due: a null `due` beside another reason fails the model); `count` an integer from 1 or null; `run_id`,
`draw_id` ids or null; `error_type` in `run.failed`'s pattern for `not_started` and `error`, null for every other
reason (a type beside another reason, or none beside these two, fails the model). No zone name, no spec name, no
revision and no tz data version (a `zone` row's `tzdata`). As for `run.failed`, a text of that pattern in a `not_started` or `error` row's `error_type` is sent as a
type: the worker writes only `type(exc).__name__` there.

### 2.4 Seams

A later feature adds an event by auditing a row last in its transaction, plus one `EVENTS` row, fact model and
adversarial test. `core/notify.RESERVED` names an event refused in rules until its feature is built; none is reserved
now. Readiness built `readiness.changed` (§2.1; object `readiness:<pair>`, rules by programme, pair, `to`, gate and cause,
READINESS §8): its reservation is lifted. Labelled accuracy built its event as `accuracy.measured` (§2.1; ACCURACY §10, §14):
`accuracy.label`, the placeholder this contract had reserved for it, is the name of the audit action it is projected
from, never an event. Schedules built `schedule.missed` (§2.1; SCHEDULES §8) by this seam alone: its audit row, one `EVENTS` row, the model and
the adversarial test; `runner/notify.py` is untouched (`_seen` gives an `SC-` object nothing to read). A second
`EVENTS` row, `schedule.pause`, has the same projector, which answers nothing for a pause but the worker's with
`missed: false` (SCHEDULES §8). Table pre-checks built `schema.drift` (§2.1; PRECHECKS §9) by this seam: its audit row last in its transaction, one `EVENTS` row,
the model `SchemaDrift` and the adversarial test, with one case each in `dedup`, `severity`, `_run_text` and
`concerned`; and it added `run_paused_schema_drift` to `Pause` (hence `PAUSES`, `Reason`, a rule's `reasons` and the
daily summary's named reasons) and `_PAUSED`. `runner/notify.py` is untouched. Readiness extended
`repo.discrepant_changes` as `repo.cycle_changes`.

## 3. Rules and noise

```yaml
rules:
  - name: new-gaps                   # slug, unique
    events: [cycle.ended]
    channels: [owners-mail]          # declared, 1..5
    specs: ["cards-*"]               # fnmatchcase globs; absent: every spec
    modes: [batch, soak]             # the default; one_shot only when named
    when:                            # cycle.ended only; any condition fires; absent: every cycle
      new_root_causes_at_least: 1        # change
      newly_discrepant_at_least: 10      # change
      mismatch_rate_above: "0.001"       # level
      indeterminate_rate_above: "0.02"   # level: indeterminate / total
      coverage_below: "0.99"             # level
      unexplained_critical_at_least: 1   # level
    reasons: [stop_condition:maxErrorRate]   # run.paused (run_paused_schema_drift too), run.stopped, run.finished
    states: [ending, grace, expired]         # licence.state (to; an open build's is open); readiness.changed (to)
    statuses: [failed, incomplete, unjudged] # sample.judged
  - name: cutover-falls                # readiness.changed only (READINESS §8), with specs and states
    events: [readiness.changed]
    channels: [owners-mail]
    causes: [cycle, time, signoff, pack]     # required: a listed cause is among the row's
    programmes: ["cutover-*"]                # fnmatchcase globs
    states: [not_ready, unknown]
    gates: [targets, fresh]                  # a listed gate is among those changed
```

**Matching** (`core/notify.fires`, pure): the kind is in `events`; a run or approval event's spec matches a glob and a
run event's mode is in `modes` (licence and `notify.*` events match only rules without `specs`); the kind's filter and
`when` hold. A `readiness.changed` rule (it names no other event) matches the pair with its `specs` globs, the `to`
state with `states`, and checks each of its own keys that is present: `programmes` (a pair in no programme matches
only a rule without it), `gates` (a listed gate is in `changed`) and `causes`, which it must name: the rule fires
when any cause it lists is among the row's. A `baseline` row, or one that names `config` alone, so fires only a rule
that lists it, and no row is dropped for the causes it names besides (READINESS §8 says what each cause is, told from
the pair's own changed gates). A `schedule.missed` event is matched by `specs` and `modes` from its row's detail and
takes no filter: `reasons` is for the run events only, so a rule never reads a missed time's reason. A
`schema.drift` event is a run's (its spec and mode are the run's) and takes no filter either; its pause is matched
as a `run.paused`, whose `reasons` may name `run_paused_schema_drift`.
A change condition holds each cycle its fact reaches the bound (at c = 1 too, §2.2). A level condition is
*crossed* when it holds at c and not at its baseline (§2.2), *recovered* when the reverse, never fired while it stays.
A null fact at c never holds, crosses or recovers; comparisons are `Decimal` on the counters' strings. Rules firing
for one event and channel make one delivery listing every rule, crossed and recovered (`UNIQUE (event_id, channel)`,
the kind inside `event_id`, so one row may yield `cycle.ended` and `sample.judged`).

**Suppression** (at feed time; the sender re-checks mutes), in order: *muted* (an active mute of the channel or the
event's run); *duplicate* (a pending or sent delivery of the same `(channel, dedup_key)` within `dedup_hours`; for
`readiness.changed`, only while no pending or sent delivery of the pair with another key followed it:
`core/notify.superseded`, `repo.held_back(among=)`); *rate
limited* (a non-alert event once the channel holds `rate_per_hour` pending or sent deliveries from the last hour).
Suppressed rows are stored, never sent; the next pending one carries `suppressed_since_last`: `(4 notifications to
this channel were held back since the last one: see Notifications)`. Tests bypass all three, and a test send never
ends the count: it runs outside the queue.

## 4. The feed (`runner/notify.Notifier.feed`)

One `repo.audited` transaction per pass, `SET LOCAL statement_timeout = '30s'` and `lock_timeout = '5s'`: lock the
cursor `FOR UPDATE` (the first configured pass inserts it at `coalesce(max(seq), 0)`), read `head = max(seq)`, then up
to 200 rows `seq > cursor AND seq <= head AND action = ANY(FEED_ACTIONS)` in seq order (`repo.audit_chain` gains
`actions` and `upto`), of the `FEED_ACTIONS` whose kinds a rule routes: a row no rule can route yields nothing.

- **Backlog.** When the first unread row is older than `max_age_hours` by its audit `at` (an outage, or `notify`
  re-enabled), the pass bisects seq between the cursor and `head` for the last row older than that
  (`repo.chain_before`: about 20 primary-key lookups; `at` follows seq to within a transaction's length), moves the
  cursor there in one step, writes one `notify.skipped {reason: stale, from_seq, to_seq}` and reads on.
- **Rows.** Each row runs in a savepoint: its events are projected, matched and inserted (`ON CONFLICT DO NOTHING`).
  A projection failing its model is `unsafe`; any other error in the savepoint but a lost connection is `error`. Both
  skip the row and are counted in the pass's `notify.skipped {reason, rows, seqs}` (the first 20 seqs). A `notify.*`
  row is never counted in a `notify.skipped`, only logged, so no skip begets another.
- **Faults outside a row.** A lost connection (`conn.broken`, SQLSTATE class 08, or no SQLSTATE) anywhere, or a failed
  or cancelled cursor, head or chain read, rolls the pass back: nothing moves and the next pass retries.
- The cursor moves to the last row read after 200 rows or 60 s, else to `head`; the pass's `notify.skipped` rows (one
  per reason) close the transaction.

**Why exactly once.** `ds.chain_append` holds `pg_advisory_xact_lock(DSAU)` to commit and numbers `max(seq) + 1`, so
seq order is commit order and a snapshot seeing seq s sees all below (writers run READ COMMITTED, a stated invariant;
a stale number would fail the seq primary key, not duplicate it). `head` is read first; deliveries and cursor commit
together; a crash recomputes the same rows under the unique key. Of the `notify.*` actions only `notify.dead` and
`notify.skipped` are fed, under §5's loop guard.

## 5. Delivery (`runner/notify.Notifier.scan` and `_send`, a task per channel)

**Claim.** A channel's head is its oldest pending delivery by `delivery_id`, due when its `due_at` has passed or its
`queued_at` is older than `max_age_hours`; a head backing off holds its channel, so order holds and a dead endpoint
gets one probe per interval. The head dies `expired` when too old, `channel_removed` when undeclared and
`channel_changed` when it was rendered for another kind, `labels` or `links` than the channel now has (its `render`,
`kind:labels:links`; a restart applies a changed channel), is `suppressed` under an active mute, else is claimed
(`attempts + 1`, `due_at = now() + timeout_seconds + 15 s`, a lease: every send ends by `timeout_seconds`, §7), sent
holding no pooled connection (`connectors/notify.send`) and recorded in a second short transaction.

**Outcomes** (`last_error` a closed code, `last_status` the HTTP or SMTP code; bodies read to 4 KiB, discarded):

| Outcome | Codes |
|---|---|
| sent | 2xx (Teams Workflows: 202); SMTP 250 (`recipients_refused` when some were) |
| retried, up to `max_attempts` | `http_5xx` (and a status outside 100–599, its `last_status` null), `http_429`, `http_408`, `http_425`, `timeout` (an SMTP reply cut short at the deadline too), `connect_error`, `dns_error`, `tls_error`, `smtp_temporary`, `secret_unreadable` |
| dead | `retries_exhausted` (a retried code at `max_attempts`, so one poisoned delivery cannot hold its channel); at once: `http_3xx` (never followed), `http_4xx` (Slack's `invalid_token`, `no_service`, `channel_is_archived`), `unsafe_destination`, `url_mismatch`, `smtp_permanent` (smtplib's own refusals too: no AUTH, no AUTH mechanism in common, no SMTPUTF8), `smtp_no_starttls` (only `starttls` finding no STARTTLS), `payload_too_large`, `channel_removed`, `channel_changed`, `expired` |

Retries wait `connectors/retry.retry_delay(attempts, RetryPolicy(initial_s=60, multiplier=3, max_s=3600),
random.Random(f"{delivery_id}:{attempts}"), parse_retry_after(…))`: full jitter, `Retry-After` a floor; 8 attempts span
about two hours.

**Dead letters.** Going dead audits `notify.dead` as `system:worker` unless the newest such row of
`notify:<channel>` is under an hour old (`dead_since_last` counts the deaths since). A `notify.dead` event is never
routed to the channel it names; a dead `notify.dead` or `notify.skipped` delivery is only logged.

**Idempotency.** `delivery_id` (`N-…`, `repo._sortable_id("N-")`) and the stored body are fixed at insert, so every
attempt sends the same bytes (`body_sha256` stored), as `webhook-id` and in the e-mail `Message-ID`, only while the
channel renders as it did (`render`). A crash between a 2xx and its record re-sends after the lease.

**Housekeeping** (hourly) writes `notify.daily` when a rule routes it, and deletes deliveries created, and mutes
ended or lapsed, before `now() − retention.standard_days`, 10,000 a batch.

## 6. Payloads, channels, wording

### 6.1 `deltasleuth-notify/1` (the webhook body; the other kinds render from it)

```json
{"format": "deltasleuth-notify/1", "id": "N-01K…", "event": "cycle.ended", "event_id": "seq:81234:cycle.ended",
 "severity": "warning", "occurred_at": "2026-10-03T04:00:12Z", "installation": {"id": "I-3f…", "label": "prod"},
 "audit": {"seq": 81234, "hash": "…"}, "rules": ["new-gaps"], "crossed": ["mismatch_rate_above"], "recovered": [],
 "run": {…}, "facts": {…, "unavailable": []}, "suppressed_since_last": 0, "withheld": {}, "claim": "observed",
 "link": "https://ds.bank.example/#/runs/R-…?cycle=12", "title": "…", "text": "…"}
```

`occurred_at` is the audit row's `at`; `link` is `auth.public_url` plus a hash route of ids where the channel's `links`
is on (null in developer mode); bodies are capped at 64 KiB. `withheld` maps each withheld field to why: `labels_off`,
`no_key_pattern`, `key_shaped` (§6.4), `links_off`.

### 6.2 Wording (`core/notify.text`: closed templates, UTC)

- `cycle.ended`: `[prod] customer-profile: cycle 12 of run R-… ended: 12 of 1,000,000 keys different, 3 indeterminate,
  0 not read (observed). 2 root causes observed for the first time since cycle 4; 1 no longer observed. Of the keys
  read in both cycles 11 and 12, 41 were observed discrepant in 12 and not in 11; 17 no longer. Mismatch rate 0.0021
  (was 0.0009 in cycle 11; above 0.001).` A timed-out fact reads `not counted`; c = 1 reads `2 root causes and 41
  discrepant keys observed in the first cycle`; a hash sample reads `… (between samples); key changes are not counted
  between samples`.
- `run.paused`: `customer-profile: run R-… paused: a side refused its credential. A person fixes it and resumes the
  run.` (one clause per reason); `run.stopped`: `stop condition maxErrorRate met: keys not started are cancelled; the
  cycle ends when keys in flight finish.`
- `schema.drift` (PRECHECKS §10): `[prod] run R-… of customer-profile 1.4.0 (soak) was paused by the schema guard at
  cycle 7: side b's table changed (1 change that stops the run, 2 others).` A warning reads `…: side b's table
  changed since the run was planned (2 changes); the run goes on reading by its plan.` (at a creation `since run R-…
  of its version (2 changes); this run reads the tables as they are now.`; after a failed read, which then fails the
  run, `…; the run fails on the read that failed.`), a look that could not be made `…: the
  schema guard could not check cycle 7 (TargetUnreachable); the cycle went on unchecked.` Counts only: no table and
  no column is named. Its pause's `run.paused` reads `… paused: the schema guard found that a table it reads
  changed. A person reads the run's schema lines, then resumes or cancels it.`
- `accuracy.measured`: `customer-profile: labelling V-… of cycle 2 of run R-… was labelled: 150 items labelled, 4
  undecidable, 1 of them not reviewed (observed).` It says that labels were counted, never what they found: the
  shares and their intervals are read on the Run screen and in the reports, with their limits (ACCURACY §7).
- `readiness.changed`: `[prod] cutover-q4 / customer-profile: readiness not ready -> parity ready (5 of 5 gates;
  targets met, convergence met; cycle 14 of run R-…). Observed; readiness is not a sign-off.` The parenthesis holds
  the score, each changed gate with its status now (`not required` for one no longer required), `regressed` when the
  pair is, and the evidence cycle. A baseline reads `readiness first recorded as parity ready`, a row whose state
  stayed `readiness still not ready`; a pair in no programme is named alone, a withheld one `a pair` (§6.4). The
  footer follows, as on every notification.
- `schedule.missed`: `[prod] schedule SC-… did not start a run for 2026-10-07T01:00Z: run R-… of it was still live.`
  One closed clause per reason and code (`core/notify._MISSED`; `zone`'s two say what `the worker's` build did
  not hold or could not read, whichever build sends them); a counted range reads `for 3 times, 2026-10-05T01:00Z
  to 2026-10-07T01:00Z: no worker could start a run in time (…); a schedule never runs a backlog.`, a standing
  refusal's later times `for 14 more times since 2026-10-08T01:00Z, for the same reason: …`. `not_started` and
  `error` name an exception's type, never its message. The worker's pause before a time is due (`due` null) is
  titled `schedule SC-… is paused by the worker` and reads `[prod] schedule SC-… is paused by the worker before a
  time of it came due: the worker's time zone data did not hold its zone. A person saves the schedule with a zone
  the build holds, then resumes it.` (`core/notify._ZONE`, a closed clause for each code), never `did not start a
  run`. The footer follows.
- **Footer:** `Observed by DeltaSleuth (installation I-…); not evidence and not a sign-off. Audit row <seq>.`, plus
  `approvals.NOT_EVIDENCE` when the run's approval state is `unapproved` or `revoked`; a run made in evaluation starts
  `[unlicensed evaluation]` (LICENCE O1: a vendor build's run; one an open build made records no licence and starts
  with nothing, LICENCE §19).
- Never `resolved`, `fixed`, `parity`, `all clear` or `no discrepancies`, nor "new" without its baseline. The one
  exception is the name of readiness's state `parity ready` (READINESS §5), which names a rung, never a finding.

### 6.3 Channel shapes

- **Webhook:** `POST` JSON, `User-Agent: deltasleuth-notify/<version>`, `webhook-id: N-…`, `webhook-timestamp: <unix s
  of this attempt>`, `webhook-signature: v1,<base64 HMAC-SHA256(key, id "." ts "." body)>` (two, space-separated, with
  two keys). Each `signing_key_refs` file holds `whsec_<base64>` of 24–64 bytes, read at every send; a malformed one
  is `secret_unreadable`.
- **Slack:** `{"text", "blocks": [section, fields ≤ 8, context "run R-… · audit row s · N-… · observed"],
  "unfurl_links": false, "unfurl_media": false}`, `&`, `<`, `>` escaped.
- **Teams** (Workflows webhook): `{"type": "message", "attachments": [{"contentType":
  "application/vnd.microsoft.card.adaptive", "contentUrl": null, "content": <AdaptiveCard 1.4: title, text, FactSet ≤ 10,
  Action.OpenUrl when linked>}]}`, under 28 KB; never MessageCard.
- **E-mail:** `EmailMessage`, policy `SMTP` (CR/LF in headers refused), plain text; `Subject: [DeltaSleuth prod]
  <Severity>: <title>`, `Date` (`created_at`), `Message-ID: <N-…@I-….deltasleuth.invalid>`, `Auto-Submitted:
  auto-generated`, `X-DeltaSleuth-Event`; no Reply-To. Stored without `From` and `To`, which each send adds, so no
  delivery row holds an address and `body_sha256` covers the stored bytes.

### 6.4 Labels and links

`labels` and `links` default to true for `webhook` and `email`, false for `slack` and `teams`. `spec_name` is null,
with its reason in `withheld`, when `labels` is off; when `rawvalues.searcher(spec)` raises `EmptyPattern` (no key
pattern, or one matching the empty string, cannot tell a key: fail closed); or when `rawvalues.key_shaped(spec, name)`
flags it. `business_code` keys (`searcher` None) keep it. A `readiness.changed` event's labels are its facts `pair`,
`programme` and `entity`: all three are null, each `labels_off` in `withheld`, where `labels` is off; `pair` alone is
null (`no_key_pattern`, `key_shaped`) when its newest stored version says so, as a `spec_name` is; a name only the
settings list is kept. Its link is `#/readiness/<pair>`, `#/readiness` when the pair is withheld.

## 7. Network and secrets (`connectors/notify.py`)

- **Threads.** The notifier's sender owns two single-thread executors: `ds-notify-dns` resolves names
  (`socket.getaddrinfo`), `ds-notify-mail` sends mail, so a hung lookup never holds the mail thread. A lookup or mail
  waits its turn in its own executor; one whose send ended first never starts. Nothing of it runs on the loop's
  default executor, which key judging (`compare.evaluate_many`), sample judgements, purge and target DNS
  (`netguard.system_resolve`, on every new target connection) share. `close()` shuts both down with
  `cancel_futures=True`, not waiting.
- **Addresses** (`_allowed(address, allow_private)`; an IPv6 address that stands for an IPv4 one, IPv4-mapped,
  IPv4-translated, the well-known NAT64 prefix or IPv4-compatible, is judged as that one: `core.spec.targets.address_rule`,
  [ENGINES.md](ENGINES.md) §4; `core/notify.refusal` words why, as
  settings judge a literal): link-local (169.254.169.254 among
  them), unspecified, multicast and `fd00:ec2::254` (a ULA metadata address) are always refused; any other non-global
  address only with `allow_private_network`. It vets every answer of `_Resolver(resolve, allow_private)` (an aiohttp
  `AbstractResolver` on the resolve thread), an IP-literal URL host or `smtp_host` before its call (aiohttp never
  resolves a literal), and a proxied channel's declared `host`. `netguard.check_literal_host` is not used: it ignores
  `allow_private_network`.
- **HTTP:** a session per send, as `connectors/http.py` builds one: `trust_env=False` (`HTTP(S)_PROXY` ignored),
  `DummyCookieJar`, `allow_redirects=False`, `use_dns_cache=False`, `ClientTimeout(total=timeout_seconds)`, resolver
  `_Resolver(resolve, allow_private_network)`.
- **Proxy:** `notify.proxy` is declared by the admin, as `auth.oidc.proxy`; a webhook, slack or teams channel with
  `proxy: true` sends through it (aiohttp `proxy=`), https only, with resolver
  `_Resolver(resolve, allow_private=True)`: the proxy may be internal, never link-local, unspecified, multicast or
  metadata. The proxy resolves the destination, checked only as its declared `host` (§16).
- **URLs:** `url` (webhook) is https (http to loopback only, never with `proxy: true`) without userinfo, query or
  fragment. `url_ref` is read by `read_secret` at every send and must be https (loopback http, never through the
  proxy) to exactly `host`, without userinfo or fragment (`url_mismatch`); it is never logged, stored, audited or
  shown.
- **SMTP:** `smtp_host` is resolved and vetted by `_Resolver` first; then `smtplib` runs on the mail thread against
  the vetted address: `_GuardedSMTP`/`_GuardedSMTPSSL` (`local_hostname="deltasleuth.invalid"`, so no
  `socket.getfqdn()`) override `_get_socket` to connect to it, TLS verifying `smtp_host`. One deadline,
  `timeout_seconds`, covers resolve, the wait for the mail thread (a mail not started by the deadline never starts)
  and send (so the lease holds): before each command the socket timeout is set to the budget left, and at the deadline
  the loop shuts the socket down (`socket.shutdown`), ending a slow-drip reply, so the thread is free by then; a
  cancelled send (`close()`) shuts it down at once, as the loop may end first. The deadline bounds when the mail
  job starts, not when it ends: once started it is bounded by those timeouts and the shutdown, so a mail the relay
  accepted (250 to the end of DATA) is `sent` even when QUIT is answered after the deadline, never re-sent. `starttls` (default) refuses a server
  without it; `implicit` is SMTPS; `none` sends no credentials and refuses a global address. No XOAUTH2.
- **Custody:** `_key_custody` adds every notify ref.

## 8. Settings (`Settings.notify: NotifySettings | None = None`; one `ChannelSettings` per channel; every class `_CLOSED`)

```yaml
notify:                                  # absent: no notifier runs
  label: prod                            # ^[a-z0-9][a-z0-9-]{0,31}$
  proxy: http://proxy.bank.example:8080  # config._proxy; used by channels with proxy: true
  timeout_seconds: 10                    # 1..60: the whole send
  max_attempts: 8                        # 2..20
  max_age_hours: 24                      # 1..72
  rate_per_hour: 30                      # per channel, 1..600
  dedup_hours: 6                         # 0..168
  daily_hour: 7                          # UTC, 0..23
  channels:                              # slugs, at most 20; labels and links: §6.4
    ops-hook:     {kind: webhook, url: https://events.bank.example/deltasleuth,
                   signing_key_refs: [file:/srv/deltasleuth/secrets/hook.key], allow_private_network: true}
    owners-teams: {kind: teams, url_ref: file:/srv/deltasleuth/secrets/teams.url,
                   host: prod-00.eastus.logic.azure.com, proxy: true}
    owners-mail:  {kind: email, smtp_host: smtp.bank.example, smtp_port: 587, tls: starttls, username: ds-alerts,
                   password_ref: file:/srv/deltasleuth/secrets/smtp.pw, sender: deltasleuth@bank.example,
                   recipients: [parity-owners@bank.example], allow_private_network: true}
  rules:                                 # pause and fail to operators, gaps to owners (D.2)
    - {name: new-gaps, events: [cycle.ended], channels: [owners-mail, owners-teams], when: {new_root_causes_at_least: 1}}
    - {name: run-health, events: [run.paused, run.stopped, run.failed, notify.dead], channels: [ops-hook]}
```

Recipients 1–20; `host` a lower-case DNS name or IP literal; a literal host (in `host`, `url` or `smtp_host`) is
judged by §7's rule at load, as is a literal `notify.proxy` (private allowed); at most 50 rules. `smtp_port` defaults to
587, 465 with `tls: implicit`, 25 with `none`; `tls` (e-mail only) defaults to `starttls`. `new-gaps` fires from the
first cycle and on batch runs (§2.2). `run-health`, unchanged, also carries the schema guard's pauses
(`run_paused_schema_drift`, an alert: PRECHECKS §9); adding `schema.drift` to its events adds the guard's warnings
and the looks it could not make.

## 9. Worker integration

`_sweep_once`, after its `finish_cycle` loop, starts `self.notifying = asyncio.create_task(notifier.run(ctx))` when
none runs (with `notify` absent, once, to record a removal): no query and nothing awaited, so no notifier fault can
keep cycles from ending. `notifier.run` first calls `record_config` (auditing `notify.config` when the routing token
differs from the newest row's, `licence.watch`'s pattern; `{routing: null}` once `notify` is removed), then every
`poll_seconds` runs a feed pass, the due-channel scan (a `_send(channel)` task per due channel without one) and,
hourly, housekeeping; each step catches everything but its own cancellation (psycopg may end a cancelled savepoint
with `OutOfOrderTransactionNesting`: while the task is cancelling, any error ends it), logs `failure(exc)` and goes on.
An ended task is restarted by the next sweep; `close()` cancels it, then its send tasks, waiting 10 s at most for each
(what still runs then is logged), so a stop never hangs.
Guarantees:
1. Only `runner/worker.py` imports the notifier; it writes only its three tables and its audit rows.
2. A send ends by `timeout_seconds` (§7). A hung receiver holds its channel only, a hung relay other e-mail channels
   at most that long per send; the run path's threads, connections and DNS are never used.
3. At most two pooled connections (`asyncio.Semaphore(2)`), none during a send; the pool is `concurrency + 4 + 2`
   with `notify`.
4. Its audit rows are the last statements of their transactions.
5. Only the worker-lock holder (`LOCK_ID`: `ds worker`, `ds serve`'s or `ds run`'s worker) feeds and sends queued
   deliveries; test sends (§11) are the exception.

## 10. Messages (binding; each asserted)

**Settings** (`error: invalid settings <file>: <message>`, exit 2; cross-field messages carry pydantic's `Value error, `
prefix, as test_config's `CROSS` does):
- `notify.channels.ops-slack: Value error, kind slack needs url_ref and host (its webhook URL is a secret)` (teams likewise)
- `notify.channels.ops-hook.url: must be https://host[:port]/path without userinfo, query or fragment (http only for localhost, 127.0.0.1 or [::1]); a URL carrying a secret goes in url_ref with host`
- `notify.channels.ops-hook: Value error, kind webhook needs signing_key_refs (one or two file: references)`
- `notify.channels.owners-mail: Value error, tls none needs allow_private_network and no username`; `…: Value error, password_ref needs username`; `…: Value error, proxy is only for webhook, slack and teams`
- `notify.channels.owners-teams: Value error, allow_private_network with proxy: true is meaningless: the proxy resolves the destination`; `notify.channels.ops-hook: Value error, proxy: true needs an https url (http is for loopback, never through a proxy)`
- `notify.channels.ops-hook: Value error, 10.1.4.2 is a private address: set allow_private_network to allow it`; `…: Value error, 169.254.169.254 is never a notification destination (link-local, unspecified, multicast or metadata)`
- `notify: Value error, channels.owners-teams: proxy: true needs notify.proxy`; `notify: Value error, rules.1.channels: unknown channel ops-slak`; `notify: Value error, rules.2.name: duplicate rule sign-off`
- `notify.rules.0.events.0: notify.test is sent only by a test` (an event reserved for a feature not built would read `…: <event> is reserved for <feature> and not built`: none is)
- `notify.rules.0: Value error, when is only for cycle.ended` (`reasons`, `statuses` likewise, `reasons` beside `schedule.missed` too; `states is only for licence.state and readiness.changed`); `notify.rules.3: Value error, licence.state, notify.dead, notify.skipped and notify.daily match no spec`
- `notify.rules.0: Value error, readiness.changed needs causes (baseline and config are never implied)`; `notify.rules.0: Value error, programmes, gates and causes are only for readiness.changed`; `notify.rules.0.states.1: expected a licence state or a readiness state`
- `notify.channels.ops-hook: Value error, kind webhook needs url, or url_ref and host`; `notify.channels.owners-mail: Value error, kind email needs smtp_host, sender and recipients (1 to 20)`; `…: Value error, url is only for webhook` (`signing_key_refs` likewise; `url_ref` and `host` for webhook, slack and teams; the SMTP settings, `tls` among them, for email: `…: Value error, tls is only for email`)
- `notify: Value error, proxy: 169.254.169.254 is never a notification destination (link-local, unspecified, multicast or metadata)`
- `notify.rules.0.when: Value error, when names no condition`; `notify.rules.0: Value error, events names no event`; an unknown event: `notify.rules.0.events.0: Input should be 'cycle.ended', … or 'notify.daily'` (the events a rule takes)
- ranges as pydantic words them (`notify.daily_hour: Input should be less than or equal to 23`)

**API:** 409 `notifications are off: add notify to deltasleuth.yaml and restart (docs/mvp/ALERTS.md §8)` (every
POST); 404 `channel ops-slak is not declared`; 404 `run R-… not found`; 404 `mute M-… not found`; 409 `mute M-… ended
already`; 409 `mute M-… lapsed at <UTC>`; 429 `a test of channel ops-hook was sent less than 10 s ago`
(`Throttle(10)`); 422 `hours must be 1 to 168`; 422 `since must be an ISO-8601 time with a zone, not in the future`;
422 `target must be a declared channel for scope channel, or R-… for scope run`; 422 `reason must be 1 to 1000
characters without control characters`; 422 `the reason holds a key-shaped value: write it without customer keys` (a
run mute, against its spec); AUTH's 403 `needs the admin role` and `reauth_required`. The order: AUTH's answers,
then 409 (before the schema judges a body, so before its 422 `invalid request`; only a body that is not JSON at all is
refused 422 before everything), then the schema's 422, then 404 (a run mute's unknown run before its hours and
reason), then 429 (a test of a declared channel only) or 422; `notifications.Refused` carries each status.

**CLI** (exit 2 on `error:`):
- off: `error: notifications are off: add notify to deltasleuth.yaml and restart (docs/mvp/ALERTS.md §8)`, from
  every `ds notify` command but the listing and `deliveries`, which read; the listing prints the sentence
  without `error: ` and exits 0
- check: `error: channel owners-teams: the URL in <file> is not an https URL for host prod-00.eastus.logic.azure.com`
  (the URL found is never printed); `error: channel owners-mail: smtp.bank.example resolves to 10.1.2.9, a private
  address: set allow_private_network to allow it`; `error: channel owners-teams: host … does not resolve`; `error:
  notify.proxy proxy.bank.example resolves to 169.254.10.1: never a notification destination`; `error: channel
  ops-hook: <SecretError text>`; `error: channel ops-hook: the signing key in <file> is not whsec_ and base64 of 24
  to 64 bytes`; `ok: channel ops-hook (webhook, events.bank.example): resolves to 10.1.4.2 (private: allowed); 1
  signing key readable`; `ok: channel owners-teams (teams, prod-00.eastus.logic.azure.com): via proxy
  proxy.bank.example:8080 at 10.2.0.5; the URL file names the host`. Also, as a send judges them: `error:
  notify.proxy proxy.bank.example does not resolve`; `error: channel ops-hook: events.bank.example resolves
  to 169.254.10.1: never a notification destination`; `error: channel owners-mail: smtp.bank.example
  resolves to 93.184.216.34, a global address: tls none sends to a private relay only`; an `ok:` line lists
  every address and adds `the password readable`; a proxied channel whose proxy failed prints no line after
  its secrets are read (the proxy's line says why). Lines follow the settings' order, errors on stderr
- test: `sent to ops-hook: HTTP 204 in 212 ms (N-…)`; `error: ops-hook: http_4xx (HTTP 401) (N-…)` (e-mail:
  `SMTP 250`, and `sent to owners-mail: SMTP 250, recipients_refused in 812 ms (N-…)`; no status: `error:
  ops-hook: timeout (N-…)`)
- retry, mute, unmute: `1 dead deliveries of ops-hook since 2026-10-01T09:00:00+00:00 are pending again`;
  `muted ops-hook until 2026-10-04 09:00 UTC (M-…)` (`muted run R-… until …`); `error: give --channel or
  --run`; `ended mute M-…`; the API's refusals as `error:` lines
- explain: `error: run R-… not found`; `error: run R-… has no cycle 40`; `error: cycle 12 of run R-… has not ended`;
  `error: cycle 12 of run R-… ended without counters: it is never notified` (an erasure's); a purged cycle
  prints `cycle 3 is purged: root causes, key changes and criticals are unavailable` and the rest
- `serve`, `worker` and `run` (with or without `--no-wait`) at start, never exiting: `warning: notify channel
  ops-hook: <SecretError text>; its deliveries retry until it is readable` (`connectors/notify.unreadable`)

**Banners** (every screen, viewers included): `Notifications to ops-hook are muted until 2026-10-04 09:00 UTC by
<actor> (M-…)` (`Notifications for run R-… are muted …`; `<actor>` the mute's `created_by`); `Notifications to
ops-hook are failing: 3 not delivered since <UTC>` (its deliveries that died, tests aside, since its latest sent one,
from the first of them); `Notifications to ops-hook cannot be sent: a secret file cannot be read` (its head last failed
`secret_unreadable`); `The notifier is behind: audit rows since <UTC> are not yet notified; is the worker running?` (an
unread feed row a rule routes, or a due pending head, over 10 minutes old; `<UTC>` the older of the row's `at` and the
head's `due_at`). `<UTC>` is written `2026-10-04 09:00 UTC`; the banners come in that order.

## 11. Interfaces

| Route | Role | Answer; audit |
|---|---|---|
| `GET /v1/notifications` | viewer | `{enabled, label, cursor: {seq, head, behind}, channels: [{name, kind, host, proxy, allow_private_network, labels, links, recipients (a count), pending, sent_24h, dead_24h, suppressed_24h, last_error, last_status}], rules, mutes, banners}`; never a URL, path, address or secret. `seq` is null before the first pass, `behind` the `at` of the oldest unread row a rule routes (null when none); a channel's `last_error` and `last_status` are its newest attempt's (its head's once tried, else its latest sent or dead delivery's); `mutes` the active ones. While `notify` is absent: `{enabled: false, label: null, cursor: null}` and empty lists |
| `GET /v1/notifications/deliveries?channel&state&event&run_id&limit&cursor` | viewer | newest first (`limit` 1–500, 50): each row as stored, its body (an e-mail's without addresses), `body_sha256`, seq, attempts, `last_error` among them |
| `POST /v1/notifications/channels/{channel}/test` | operator | one attempt now, outside the queue: inserted after it as `sent` or `dead`, never `pending` (so no sender claims it), `event_id` `test:N-…`, `dedup_key` `notify.test\|N-…`; 200 `{delivery_id, state, status, error, latency_ms}`; `notify.test` |
| `POST /v1/notifications/channels/{channel}/retry` `{since?}` | operator | dead deliveries since `since` (default 72 h), tests and those rendered for other channel settings (`render`, §5) excluded, to pending: due now, `attempts = 0`, `queued_at = now()`; `{count}`; `notify.retry` |
| `POST /v1/notifications/mutes` `{scope: channel\|run, target, hours, reason}` | `require_fresh("admin")` | 201 `{mute_id, until}`; `notify.mute` |
| `POST /v1/notifications/mutes/{mute_id}/end` | admin | `{mute_id}`; `notify.unmute` |

The routes (`api/routes/notifications.py`) and `notify_app` share `deltasleuth/notifications.py`: the state and
banners, the test, retry and mutes, the digest (§2.2) and `explain`; `ds notify check` and the warnings at start are
`connectors/notify.check` and `unreadable`. Neither imports the notifier (§9). A test uses a `Sender` of its own for
its one attempt, closed after it.

**CLI** (`notify_app`, like `licence_app`): `ds notify [--json]`; `ds notify check` (no database; resolves and reads,
sends nothing); `ds notify explain R-… [--cycle N] [--json]` (the digest and each rule's verdict: `fires: … held|crossed|
recovered`, `does not fire`, `does not take this run (specs or modes)`, `takes no cycle.ended`; its latest ended cycle by
default; writes nothing); `ds notify deliveries [--channel] [--state] [--run] [--limit N] [--json]` (the newest 50 by
default; `--json` adds the stored bodies); `ds notify test CHANNEL`; `ds notify retry CHANNEL [--since TIME]`;
`ds notify mute (--channel X | --run R-…) --hours N --reason TEXT`; `ds notify unmute M-…`; the last four audited and
needing `DS_OPERATOR` under `auth` (AUTH §4.2).

**UI:** `web/src/Notifications.tsx` at `#/notifications` (words in `notify.ts`), in the nav when enabled: cursor
state, channels (Send test; Send failed again), read-only rules (`Declared in deltasleuth.yaml (notify.rules); a change
applies at the next restart.`), mutes (a Mute form for admins), deliveries; SaaS channels badged `external`. `main.tsx`
shows `banners` as it shows the licence warnings.

## 12. Audit actions (added to AUTH §6 and RETENTION §8)

| Action | Object | Detail | By |
|---|---|---|---|
| `notify.config` | `notify` | `{label, channels: {name: {kind, host, proxy, allow_private_network, labels, links, recipients}}, rules, routing}` or `{routing: null}`; `routing` is `ctx.tokeniser.token("notify_routing", <canonical routing>)`, keyed, so a changed address shows and none can be guessed back. The canonical routing is the `notify` block as loaded, a rule's `programmes`, `gates` and `causes` (the keys readiness added) left out while unset, as they are in `rules`: an upgrade to a build that has them leaves the token of unchanged settings alone, and writes no row | `system:worker` |
| `notify.dead` | `notify:<channel>` | `{delivery_id, event, attempts, error, status, body_sha256, dead_since_last}` | `system:worker` |
| `notify.skipped` | `notify` | `{reason: stale, from_seq, to_seq}` or `{reason: unsafe\|error, rows, seqs}` | `system:worker` |
| `notify.test`, `notify.retry` | `notify:<channel>` | `{delivery_id, state, status, error}`, `{since, count}` | the caller |
| `notify.mute`, `notify.unmute` | `M-…` | `{scope, target, until, reason}`, `{}` | the caller |

No detail holds a URL, path, address, secret or message text; pack segments select by object, so none enters a pack.
`rawvalues._GENERATED` gains `M` and `N`, with labelled accuracy `V` and with schedules `SC` (`(?:[ADJMNPRV]|LI|SC)-[0-9A-Z]{26}`).

## 13. Migration `012_notify.sql`

```sql
-- Alerts and notifications (docs/mvp/ALERTS.md): the audit hash chain is the outbox. The cursor is the last chain row
-- turned into deliveries, inserted at the head by the first pass with notify configured. Ids, counts and codes only.
CREATE TABLE ds.notify_cursor (
    one      boolean PRIMARY KEY DEFAULT true CHECK (one),
    seq      bigint NOT NULL CHECK (seq >= 0),
    moved_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE ds.notify_deliveries (
    delivery_id text PRIMARY KEY CHECK (delivery_id ~ '^N-[0-9A-Z]{26}$'),
    event_id    text NOT NULL CHECK (event_id ~ ('^(seq:[0-9]+:[a-z]+\.[a-z_]+|daily:[0-9]{4}-[0-9]{2}-[0-9]{2}'
                                                 '|test:N-[0-9A-Z]{26})$')),
    seq         bigint CHECK (seq > 0),  -- a ds.audit_chain seq (no FK: chain rows are never deleted); NULL: daily, test
    event       text NOT NULL CHECK (event ~ '^[a-z]+\.[a-z_]+$'),
    severity    text NOT NULL CHECK (severity IN ('info', 'warning', 'alert')),
    channel     text NOT NULL CHECK (channel ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(channel) <= 63),
    rules       text[] NOT NULL DEFAULT '{}',
    dedup_key   text NOT NULL CHECK (length(dedup_key) <= 200),
    run_id      text CHECK (run_id ~ '^R-[0-9A-Z]{26}$'),
    body        text NOT NULL CHECK (octet_length(body) <= 65536),  -- rendered once; e-mail without From and To
    body_sha256 text NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    render      text NOT NULL CHECK (render ~ '^(webhook|slack|teams|email):[01]:[01]$'),  -- kind:labels:links rendered for
    state       text NOT NULL CHECK (state IN ('pending', 'sent', 'dead', 'suppressed')),
    why         text CHECK (why IN ('muted', 'duplicate', 'rate_limited')),
    mute_id     text,
    attempts    smallint NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    queued_at   timestamptz NOT NULL DEFAULT now(),  -- expiry counts from here; a retry resets it
    due_at      timestamptz,
    last_error  text CHECK (last_error ~ '^[a-z][a-z0-9_]{0,31}$'),
    last_status integer CHECK (last_status BETWEEN 100 AND 599),
    created_at  timestamptz NOT NULL DEFAULT now(),
    done_at     timestamptz,
    UNIQUE (event_id, channel),
    CHECK ((state = 'suppressed') = (why IS NOT NULL)),
    CHECK ((why IS NOT DISTINCT FROM 'muted') = (mute_id IS NOT NULL)),
    CHECK ((state = 'pending') = (due_at IS NOT NULL)),
    CHECK ((state IN ('sent', 'dead')) = (done_at IS NOT NULL))
);
CREATE INDEX notify_pending_idx ON ds.notify_deliveries (channel, delivery_id) WHERE state = 'pending';
CREATE INDEX notify_dedup_idx ON ds.notify_deliveries (channel, dedup_key, created_at);
CREATE INDEX notify_channel_idx ON ds.notify_deliveries (channel, created_at);
CREATE INDEX notify_run_idx ON ds.notify_deliveries (run_id, delivery_id) WHERE run_id IS NOT NULL;
CREATE INDEX notify_created_idx ON ds.notify_deliveries (created_at);
CREATE TABLE ds.notify_mutes (
    mute_id    text PRIMARY KEY CHECK (mute_id ~ '^M-[0-9A-Z]{26}$'),
    scope      text NOT NULL CHECK (scope IN ('channel', 'run')),
    target     text NOT NULL CHECK (target ~ '^([a-z][a-z0-9]*(-[a-z0-9]+)*|R-[0-9A-Z]{26})$'),
    until      timestamptz NOT NULL,
    reason     ds.note NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    ended_by   text,
    ended_at   timestamptz,
    CHECK ((scope = 'run') = (target ~ '^R-')),
    CHECK (until > created_at AND until <= created_at + interval '7 days'),
    CHECK ((ended_by IS NULL) = (ended_at IS NULL))
);
CREATE INDEX notify_mutes_active_idx ON ds.notify_mutes (until) WHERE ended_at IS NULL;
CREATE FUNCTION ds.tg_notify_mutes() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN  -- ended once, as of now(), nothing else changed (ds.tg_licences' pattern); the prune deletes
    IF OLD.ended_at IS NOT NULL OR NEW.ended_at IS DISTINCT FROM now()
       OR (to_jsonb(NEW) - ARRAY['ended_by', 'ended_at']) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['ended_by', 'ended_at']) THEN
        RAISE EXCEPTION 'ds.notify_mutes: mute % is only ever ended, once, as of now()', OLD.mute_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER notify_mutes_end_only BEFORE UPDATE ON ds.notify_mutes
    FOR EACH ROW EXECUTE FUNCTION ds.tg_notify_mutes();
```

012 touches no existing table (no foreign key, so no lock or trigger on `ds.audit_chain`), applies in milliseconds and
backfills nothing. The API and the CLI check a mute's `target` against its `scope` before the insert (§10). The repo
gains a `# -- notifications` section (cursor, deliveries, mutes, `chain_before`, `cycle_at`, `previous_fact`,
`root_cause_changes`, `discrepant_changes`, `daily_counts`; for §11 `notify_state`, `channel_states`, `list_deliveries`,
`insert_tested`, `retry_dead`, `insert_mute`, `get_mute`, `end_mute`, `active_mutes`); `audit_chain` gains `actions` and
`upto`, `last_audit` gains `obj`.
**Retention** (RETENTION §4.2): deliveries go `standard_days` after they were made and mutes after they ended or
lapsed; the cursor stays; none is erased with a run. **Licence:** the notifier never calls `licence`; LICENCE §2's
"always works" gains notifications. Packs neither cite nor include notifications.

## 14. Tests (local receivers; a fixture fails any non-loopback resolve or connect; only the local `postgres:16-alpine`)

Receivers in `mocks/notify.py`: an aiohttp app on 127.0.0.1 (`/webhook` verifying signatures, `/slack`, `/teams`,
`/__received`, `/__fail?status=&times=&retry_after=`, a hang), an asyncio SMTP sink (STARTTLS with an in-memory CA from
`cryptography`, AUTH PLAIN, scripted 4yz/5yz, a hang and a slow drip) and a recording HTTP proxy.

| File (under `tests/unit/` but the last two) | Behaviours |
|---|---|
| `core/test_notify_events.py` | each §2.1 row, `readiness.changed` among them (its facts, severity, a renewed licence's row no alert, dedup mask, labels withheld, names that are no slugs unsafe: READINESS §17); `test_boundary_stop`, `test_maxkeys_not_stopped`, `test_draw_refusal_once`, `test_revoke_pause_once`, `test_error_type` (`_LeaseLost` kept, the class of a `<class>: <code>` reason and of the guard's text, a SecretError text `SecretError`, else `Error`), `test_failed_run_sample_judged`, `test_unsafe_skipped`, `test_now_block` (a late `cycle.ended` keeps its cycle); `schedule.missed` (SCHEDULES §15): its facts, each reason and code with its severity and clause, the counted ranges, unsafe values never sent, and the worker's `schedule.pause` before a time is due (its facts, title and text for each code; a pause beside its missed row and a person's pause send nothing); `schema.drift` (PRECHECKS §13): its facts, severity, dedup key and golden wording for a pause, a warning, a creation, a failed read and a look not made, its model against planted names and type texts, and the guard's pause as a `run.paused` that shares its key, a later pause of the same tables keyed apart; Hypothesis plants key-shaped strings, labels, notes and failure texts in every detail field, none reaching a body, audit row or log |
| `core/test_notify_rules.py` | a `schema.drift` matched as a run's event and taking no filter, its pause matched by `reasons`; a missed time matched by the spec and mode of its row; a readiness rule's `causes` (required; any listed cause fires it), `programmes`, `states` and `gates`; change vs level, crossing, recovery; c = 1 fires `new_root_causes_at_least` on a batch run; unavailable, above, above: one crossing; null never fires (one delivery for several rules and alerts bypassing the rate limit only need the store: `runner/test_notify_feed.py`) |
| `core/test_notify_payloads.py` | the Standard Webhooks test vector and an independent HMAC; Slack escaping; Teams ≤ 28 KB; e-mail headers, stored without addresses; footer, `NOT_EVIDENCE`, evaluation label; `withheld` for labels off, a spec without a key pattern and a key-shaped name; links off for slack and teams; `test_banned_words`; no RC id |
| `connectors/test_notify_send.py` | every §5 code; a private literal allowed with the flag and refused without; 169.254.169.254, `fd00:ec2::254` and a link-local literal refused in both modes, as a proxied `host` and as the proxy's answer; rebinding; `HTTPS_PROXY` ignored; `url_mismatch` never printing the path; a malformed or non-ASCII `whsec_` key; SMTP modes; smtplib's own refusals `smtp_permanent`; a status outside 100–599; http never through the proxy; a slow-drip relay (a cut reply too) ends `timeout` within `timeout_seconds` + 1 s; two hung lookups never delay a mail to a literal relay; a cancelled mail frees its thread at once; a failing default executor and a raising `socket.getfqdn` never reached; canary strings in secret files absent from logs, audit, API, CLI and deliveries |
| `runner/test_notify_feed.py`, `test_notify_deliver.py` (db) | exactly once beside DSAU writers; crash, no duplicate; cursor at head on first configured pass; a poison row skipped with its seq (`error`) while later rows notify; a lost connection moves nothing; a 100k-row stale backlog gives one `notify.skipped` and one delivery; pause, resume, pause gives two deliveries; `test_guard_pause_reaches_a_run_paused_rule` (§8's `run-health` rule, unchanged, delivers the schema guard's pause, and the daily counts name its reason); a channel routed both `run.paused` and `schema.drift` sends one, both with `dedup_hours: 0`; the schema guard's pause, a resume and a pause of the same tables give two deliveries; a pair's renewed fall is sent once its recovery was, the same fall twice in a row once; `QueryCanceled` gives `unavailable`; digest on key-list, rolling, table-scan (whole, cut and stopped) and `hash_sample` soaks; a test send keeps the held-back count; a cancel inside a digest savepoint ends the notifier; a head rendered before a restart changed its channel dies `channel_changed`; after a 30-hour outage heads die `expired` and new rows arrive (`test_after_a_30h_outage`); a head always answered 500 (or 999) dies `retries_exhausted` and the next alert arrives; hourly `notify.dead`; loop guard; head-of-line (A 503, B delivers) |
| `runner/test_worker.py` | a raising notifier never fails a run; a stop inside a digest savepoint ends the worker; with the notify tables locked or failing, cycles end on time; two connections at most; no other runner import; `notify.config` on change and removal, and none for a token computed before a rule's readiness keys existed |
| `api/test_notify.py`, `api/test_roles.py` (`ROUTES`), `cli/test_cli.py`, `test_config.py`, `store/test_migration_012.py` | roles, fresh sign-in, 429, §10's messages and their order, `DS_OPERATOR`, constraints; the canary absent from every API answer (a test send's, the state, the deliveries, the audit log) and from `ds notify check`'s output |
| `test_integration_contracts.py` | each `FEED_ACTIONS` action still carries the detail keys its projector reads |
| `tests/e2e/test_notify.py` | a three-cycle soak on the customer mock pair: signed `cycle.ended` whose counts equal `ds.cycles.counters`, `body_sha256` matching; nothing for unchanged gaps; a 503 and a hanging webhook, then (in a run of its own: `notify` holds at most 20 channels) 20 hanging SMTP channels, leave each cycle's counters and the status equal to a run without `notify`, and its time within twice the baseline's plus 10 s; kill -9 mid-send delivers at most twice under one `webhook-id` (the second after the lease); `ds audit verify` passes |
| `web/test/notify.test.ts` | texts, banners, the Mute form for admins only |

[FILES.md](FILES.md) §12 adds to `core/test_notify_events.py`: `test_error_type` holds a file side's
run-time failures (`FilesChanged`, `RowsMoved`, `TargetTempSpace`, `TargetDenied`), each sent as its class,
and `test_each_reason_and_code_of_a_missed_time` the five codes of an export that was not ready, whose
clause ends `ds target check shows its state` for `export_absent` and `unsettled` and `starting a run on
the Runs screen shows the cause` for `stale`, `unstamped` and `planning`, which that check cannot show
(FILES §15, F11). The file reader in a process of its own ([READER.md](READER.md) §12) adds `ReaderDied` (F12),
`TargetTempSpace: MemoryError` and `TargetRefused: Oversize` and `Unframed` to `test_error_type`.

## 15. Documents to amend

AUTH §4.1, §4.2 (the `ds notify` rows; `ds worker`'s actions), §6. RETENTION §4.2, §8. DEPLOY intro (outbound calls
also to *the notification channels you declare*), §3 (a Notifications subsection: settings, the Secrets table, the key
as `printf 'whsec_%s\n' "$(openssl rand -base64 32)" > hook.key`, a firewall table of hosts and the proxy, a Standard
Webhooks verifier, NTP), §5 Acceptance checklist (`check`, `test`, the next `notify.daily`), §10 The SIEM copy (alert
on `notify.dead`, `notify.skipped`, `notify.config`), §11 Troubleshooting (a Notifications table; the "ignores
proxies" row names `notify.proxy`). RUNBOOK: a Notifications section after "Stopping early" (what each event asks,
first-cycle and between-samples wording, `explain`, mutes, recovery, naming specs after systems); "Security and
privacy" (the SSRF bullet and the outbound destinations gain the channels and the proxy). CONTRACTS §1, §2, §3, §4
Runner (the worker's notifier task), §7, §8. `runner/worker.py`'s module docstring. LICENCE §1, §2. APPROVALS §12
drops "notifications". PENTEST §2 and SECURITY.md: new destinations, SSRF, the canary. architecture.md §4.3
(Outbox row), D.2; control-plane.sql's `ds.outbox` comment.
README (features, layout) and PLAN (built, not built). `deploy/config/deltasleuth.example.yaml` holds a commented
`notify` block; DEPLOY §3 shows the block, and its key file is
`deploy/secrets/notify_hook_key`, since DEPLOY's commands write only under `deploy/secrets/` (a test checks).

## 16. Limits, deferred, size

**Limits.** At least once: Slack, Teams and mail readers may see a duplicate. "Delivered" is a 2xx or a 250, never a
reader. Thresholds are judged at cycle ends. No worker, no notifier: `notify.daily` and the SIEM watch. Rows older than
`max_age_hours` are never notified, one `notify.skipped` saying so. A poisoned delivery dies after `max_attempts`; in
a long outage heads die one by one and `retry` sends them again, out of order. Behind a proxy the guard checks the
declared host only. "New" forgets purged cycles; a hash sample's key changes are not counted. The Teams envelope is
checked against the mock only. The chain and the SIEM, not the deliveries table, are the record. No
proxy authentication or XOAUTH2 (use an internal relay).

**Deferred.** Ticketing; approval reminders; rule and spec mutes; routing by `metadata.owner`; quiet hours;
mid-cycle thresholds; `resumed` events (`accuracy.measured` and `readiness.changed` are built: ACCURACY
§10, READINESS §8).

**Size.** About 1.5k product lines, 300 UI, 280 mocks, 1.5k tests; no run path changes.
