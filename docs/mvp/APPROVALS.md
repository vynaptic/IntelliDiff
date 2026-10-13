# Four-eyes approvals and sign-off: contract

This contract is binding. It builds four-eyes approvals on `docs/architecture.md` §13.1, §13.2
and §15 item 1, implements [AUTH.md](AUTH.md) §11 and feeds evidence packs (§8). [SAMPLING.md](SAMPLING.md) amends
§1, §3, §6 and §7 for sampled sign-offs; [ACCURACY.md](ACCURACY.md) amends §1.2, §3, §6 and §8 for the
labelling a sign-off attests.

**Goal:** under SSO, a spec version that exposes values, suppresses findings or filters what is
compared runs only once a non-author approved exactly its content, and a cycle is cut-over evidence only
once someone who neither wrote its spec, started its run nor requested the sign-off accepted a hash of
its facts, class by class. **Shape:** one `ds.approvals` row per request, of kind `spec` (one
content hash) or `signoff` (one ended cycle), guarded by SQL that every writer meets. Tokens and
developer modes have no persons: their runs go ahead labelled *unapproved* and are never signed off,
whatever approval they later resume or attach under.

## 1. What is approved

### 1.1 Spec versions

An approval covers the whole content hash: the approver reviews tolerances, normalisers, key sources,
`select` and `map` in its diff. `approvals.needs(spec)` only decides which versions gate runs (§4);
every sign-off needs an approval of its content hash (§1.2).

| Scope | `CompiledSpec` members | Why |
|---|---|---|
| `clear` | `security.clear` | Values reach findings, exports, the UI and the LLM untokenised. |
| `unlisted_clear` | `security.unlisted == "clear"` | Every path without a tokenize rule is clear. |
| `known_diffs` | `known_diffs` (active rules) | A match suppresses the finding (KNOWN_DIFF). |
| `population_filters` | `sides.*.table.where`, `sides.*.table.columns`, `sides.*.table.file.csv.skip` above 0 ([FILES.md](FILES.md) §5: each file's first lines are not read), `compare.include`, `compare.exclude`, `compare.value_filter` | They narrow what is compared; so do key sources, `select` and `map`, which gate nothing but are in the diff (sign-off shows `keys_source`). |
| `sampling` (011) | `sample` (a `hash_sample` key source) | It pre-registers the frame, strata, sizes, salt label and `resolve` before any run draws; the sign-off cites it ([SAMPLING.md](SAMPLING.md) §2.1). |

`raw_clear` is reserved; a build that honours a new scope refuses approvals lacking it (message 3). A
`sampling` version's review lists `/key/source` as its needed item and adds `ledger`, every draw of its
population under any spec name (`order` null; SAMPLING §9).

### 1.2 Run sign-off

A sign-off accepts one ended cycle as parity evidence. In a vendor build its request and each approval
step also need an
Enterprise licence, whatever the cycle (M6, §6.2; [LICENCE.md](LICENCE.md) §6, §8); rejecting and
revoking do not. An open build, which this one is, asks for no licence and never raises M6
([LICENCE.md](LICENCE.md) §19). `approvals.signoff_problems` judges the cycle, never the run's current status, at
request and at decision:

1. **A live run made under 008 and SSO:** `a one-shot run is not signed off`; `run R-… read captured
   samples: only a live run is signed off`; `run R-… started before approvals (008): only later runs are
   signed off` (`runs.created_at` against 008's `applied_at`); `run R-… started in tokens mode: only a
   run started under SSO is signed off` (`in developer mode` likewise), from `params.approval.mode`,
   which only its creation writes.
2. **Ended, with its own counters:** `cycle 3 of run R-… has not ended`; 410 once its purge starts;
   `cycle 3 of run R-… has no counters of its own (failed, erased or abandoned)` unless
   `ds.cycles.counters->>'cycle'` is the cycle (so `'{}'` fails).
3. **Complete:** `cycle 2 of run R-… is partial: 12 key(s) cancelled, 3 owed a recheck`; `cycle 2 of
   run R-… does not reconcile: <identity>` unless every `counters.reconciliation` entry holds (none
   fails); `cycle 2 of run R-… compared no key`; `cycle 2 of run R-… scanned only part of its tables
   (rows_a 10, rows_b 10 read; the scan stopped before the end)` when its `ds.scans` state is not
   `done` (`until`, `maxDuration` or a cancel stopped it; a `maxKeys` stop is item 4's).
4. **Uncut:** `cycle 2 of run R-… was cut at 10000 keys (maxKeys)`, from `ds.cycles.keys_cut`,
   never from `stop_reason`, which a pause overwrites.
5. **No unexplained CRITICAL:** `cycle 2 of run R-… has 3 key(s) with unexplained CRITICAL findings
   (bug rules count as unexplained)`.
6. **Approved content:** the cycle's `ds.cycles.approval_id`, still `approved`, or, when that is null
   and `needs` is empty, the version's active approval at request (either way
   `attests.spec_approval_id`): `cycle 2 of run R-… ran without a spec approval`; `cycle 2 of run R-…
   ran under spec approval A-…, since revoked`.

`approvals.attests` builds `{run_id, cycle, mode, auth_mode, keys_source, scan, spec_id, content_hash,
spec_approval_id, created_by, ended_at, counters, unexplained_critical, rules, root_causes, claim:
"observed"}`: `auth_mode`, the mode the run started in; `scan`, a table scan's `{done, rows_a, rows_b,
skipped_a, skipped_b, matched, candidates}` (null for a key list); `rules`, each active known-diff rule's
fields and key count; `root_causes`, each
unsuppressed discrepancy class (`repo.list_root_causes`: `root_cause_id`, `taxonomy_code`,
`max_severity`, `change_type`, `path_pattern`, `affected_keys`). The decision rebuilds
`attests_sha256` (`sha256_hex` of `jsonio.dumps(attests, sort_keys=True)`) and refuses a difference.
**Approving a sign-off decides each listed rule and root cause.** From 009 a request records **attests
format 1** ([PACK.md](PACK.md) §1.2): `format: 1`, the cycle's `census` and `adjudication`, the newest
adjudication of the cycle a person scored (`{adjudication_id, sheet_sha256, score_sha256, adjudicated,
agreed, passed}`, or null), with each rule's fields named (`_RULE_FIELDS`). A decision rebuilds a
sign-off by its recorded format (`approvals.rebuilt`): format 0 as 008 built it, so a request made
before 009 is still decided; an unknown format is refused as changed. From 011 every request records
**attests format 2** ([SAMPLING.md](SAMPLING.md) §10): format 1 plus `sample`, null unless the run's
`keys_source` is `hash_sample`, else the cycle's draw (salt label, nonce, salt SHA-256, its `sample.draw`
chain row `draw_seq`, population, frame, strata, selection SHA-256), the build's targets, confidence,
margin and `resolve`, the ledger draws before it as R-a judged them (a close shown only if its chain row
precedes the draw's) and the cycle's stored judgement. From 013 every request records **attests format
3** ([ACCURACY.md](ACCURACY.md) §8): format 2 plus `labellings` (`{drawn, labelled}`: the cycle's
labellings counted, whether or not one is attested, so an approver also attests one that no person
labelled) and `accuracy`, null unless a person (`user:`) labelled a labelling of the cycle, else the
newest such by `labelling_id` (`repo.attested_labelling`): `{labelling_id, salt, truth: {source, as_of,
names_side}, unlocatable, selection_sha256, labels_sha256, score_sha256, drawn_by, labelled_by,
labelled_at, labeller: {started_run, spec_author}, discrepant, accepted, agreed}`. `labeller` says whether
its labeller started the run or wrote the cited spec approval; `discrepant` is `{items, uniform, census,
labels, reasons}` (the uniform items' exact label and undecidable-reason counts), `accepted` and `agreed`
`{keys, drawn, labels}`, each null for a population not drawn. No share or interval is attested as such:
`score_sha256` binds them. `rebuilt` rebuilds format 3 from the labelling it names (`repo.cycle_labelling`)
and still rebuilds formats 0 to 2; `signoff_problems` judges nothing of a labelling (ACCURACY D6). A
sampled cycle's problems follow item 6: `sample.problems` gives SAMPLING §13's G1–G11 from the stored
draw, judgement and ledger, never recomputed (a pack's `later=False` leaves out G10, which it reads from
the final grant), and R-b's for a judgement neither `passed` nor `failed` (a cycle that ended unjudged, or
incomplete with a member not final) and for a ledger draw no committed `sample.draw` row names.

| Architecture §15 | Checked | The approver's judgement |
|---|---|---|
| 1; counters reconcile | items 3, 5 | — |
| 2 per-stratum bound (sampled cycles) | G1–G11 and R-b: its own `bottom_k` draw, its keys re-hashing to it, no unfit frame key, its judgement `passed` (every key final, every sign-off stratum within its exact one-sided 95% rule), the ledger's earlier draws passed or closed before it, no later draw failed | the fixes the close notes state |
| 3 coverage | cancelled, outstanding, nothing compared; for sampled cycles also each stratum's unresolved (after retries), `absent_both` and `api_uncomparable` gates | `indeterminate`, `coverage`, `by_inconclusive_reason`, `absent_both`, `api_uncomparable` |
| 4, 5 | — | all (a census stratum approximates item 4); every claim is `observed` |

Adjudication is in the store (PACK.md §5): the request fixes the attested one, a draw or score is
refused (409) while the cycle has a requested or approved sign-off, and its scorer may not approve the
sign-off (§3). A labelling likewise (ACCURACY §3, §5): the request fixes the attested one, a draw or an
upload of labels is refused (409) while the cycle has a requested or approved sign-off, and its labeller
may not approve the sign-off. **Readiness is no precondition** ([READINESS.md](READINESS.md) D10): `signoff_problems` never reads a
pair's readiness, its gates or `ds.cycles.readiness`, so a cycle whose pair reads `not ready` is signed
off when items 1 to 6 hold, and readiness may be stricter than a sign-off (a signed pair can read
`regressed`). Readiness reads sign-offs, never the reverse: its `signoff` gate is met by an approved,
unrevoked sign-off of the pair's candidate cycle, and its precheck (`GET
/v1/readiness/pairs/{name}?precheck=1`) calls `signoff_problems` unchanged, labelled `computed now; the
sign-off decides`; where packs are configured it lists after them the one line that refuses every pack
of the spec, a key pattern that cannot tell a key ([PACK.md](PACK.md) §2.3), which is no problem of a
sign-off's: `signoff_problems` does not judge it. **Divergences from `control-plane.sql`:** `ds.approvals` replaces
`ds.spec_approvals`; §3's lineage rule replaces "no author of any version"; the runner, not
`tg_runs_spec_approval`, gates runs.

## 2. Lifecycle

| Transition | Who | Effect | Audit |
|---|---|---|---|
| → `requested` | `spec_author` (spec) or `operator` (sign-off), tokens included | Stores `scopes` or `attests`; one open request per version or cycle. | `approval.request` |
| approval step | a fresh `rule_approver` person (§5) | The trigger stores `authors`, `unattributed`; the first of two sets `first_by`. | `approval.grant`; a sign-off's last, `retention.hold` |
| → `rejected` | a `rule_approver` person, the first approver too | Note required. | `approval.reject` |
| `approved` → `revoked` | a `rule_approver` person, `ds approvals revoke`, or a purge of the cycle no issued pack cites | Below. | `approval.revoke`, `paused`, `retention.release`, `pack.revoke` |

**Revoke** is one transaction, in order: (1) `UPDATE ds.approvals SET status = 'revoked', … WHERE
approval_id = %s AND status = 'approved'` (row lock; no row: 409); (2) a spec approval's `running` runs
pause (`APPROVAL_PAUSE`) and its `queued` one-shot runs stop (`approval_revoked`); (3) approved
sign-offs citing it are revoked (`spec approval A-… was revoked`), open ones rejected by a person
revoker or refused at decision; (4) a run left without an approved sign-off loses a `signed off: …`
hold; (5) each revoked sign-off's evidence packs are revoked with it, copying who, when and why
(`ds.tg_signoff_packs`, for every writer, SQL included), and audited `pack.revoke` `{reason,
cascade_of}` by the revoker ([PACK.md](PACK.md) §6). Creation, resume, the boundary attach and the
sign-off decision read the spec approval `FOR SHARE` before locking the run or sign-off, so each commits
first and is caught by steps 2–3, or waits and finds `revoked`; the trigger re-reads it `FOR SHARE` for
every writer.

**Holds:** a sign-off's last step calls `repo.set_hold(run, 'signed off: A-…', approver)` unless the run
is held, keeping what `attests` is rebuilt from. A purge of a signed cycle (after `ds purge release`)
revokes its sign-off (`cycle 2 purged`, by the purger) in `_purge_cycle`'s opening transaction, unless
an issued evidence pack cites it: the sign-off then stays approved, and the purge deletes the pack's
stored ZIP and keeps its record ([PACK.md](PACK.md) §6).

- Nothing expires or is superseded; rows are never deleted; ids come from `repo._sortable_id`.
- **A run records** `runs.approval_id` (the approval it started, resumed or attached under) and
  `params["approval"] = {needs, mode}`, which only its creation writes (`mode`, the one it started
  in), plus `from_cycle` when a resume attached its first approval mid-cycle; each ended cycle,
  `ds.cycles.approval_id` (null for a cycle before `from_cycle`). `approvals.run_state` is `approved`,
  `revoked`, `unapproved` (`needs` but no approval; `mode` says why, null before 008) or `not_needed`.

## 3. Four-eyes

| Actor | Excludes (`ds.accountable`) | Attributes authorship (`ds.attributed`) |
|---|---|---|
| `user:U-…` | itself | yes |
| `token:T-…` | its creator if a `user:`, and `user:<owner_user_id>` if set | no: it shows who holds it, not who wrote the content |
| `local:`, `dev:loopback`, `system:`, `api`/`ds`, NULL | nobody | no |

**The exclusion set** (`ds.approval_authors`), recomputed at every step, the union stored:
- **Always the requester:** the maker never checks.
- **Spec** (`ds.spec_accountable`): every uploader (`created_by` and each `spec.create` actor) of the
  name's versions after its newest approved version before this one, up to this one. A colleague's bump
  of Bob's unapproved rules leaves Bob excluded; an approval resets the lineage, so two people taking
  turns can approve each other.
- **Sign-off:** the stored `authors` of the spec approval it cites, the run's creator and, from 009,
  whoever scored the adjudication it attests ([PACK.md](PACK.md) §5); from 011, whoever closed a draw of
  the ledger it attests (`attests.sample.ledger[].closed_by`, [SAMPLING.md](SAMPLING.md) §10); from 013,
  whoever labelled the labelling it attests (`attests.accuracy.labelling_id`'s `labelled_by`,
  [ACCURACY.md](ACCURACY.md) §8).

**Unknown authorship:** if any actor behind the set does not attribute (a token, CLI, developer-mode or
pre-005 upload in the lineage; a token- or CLI-started run; a token requester; a cited approval that
needed two), two distinct `rule_approver`s outside the set must approve, so the hidden author is at
most one of them. CI uploads and their runs' sign-offs so need two approvers. So does a scheduled run's
sign-off ([SCHEDULES.md](SCHEDULES.md) §6): the worker starts it as `system:worker`, which does not
attribute and excludes nobody, with no DDL. Whoever saved the schedule is not in the set and may be one
of the two; for a one-approver sign-off, a person starts the candidate run by hand.

**Enforced three times:** `require("rule_approver")` refuses non-persons; the route reads
`ds.approval_authors` for its 409; `ds.tg_approvals` checks every writer, and that the decider is an
enabled `ds.users` row whose stored roles hold `rule_approver`.

| Bypass | Why it fails |
|---|---|
| The author approves, or uploaded through a token they own or created | In the set |
| The author uploads through a shared or colleague's token, or the CLI | Unattributed: two approvers |
| A token, the CLI or developer mode approves | `require`; no CLI grant; the CHECKs; the trigger |
| Re-upload the same content, or a colleague bumps the version | Every `spec.create` row in the lineage counts |
| Edit rules after approval | `specs_immutable`; the gate re-hashes the body (message 4) |
| Approve other content than shown | The decision echoes the reviewed hash and scopes |
| Act while a revoke commits | The revoke order and `FOR SHARE` reads (§2) |
| Sign off a cycle that ran unapproved | `ds.cycles.approval_id` is fixed when it ends; a resume that attaches a run's first approval leaves its cycle without one (`from_cycle`), unless a soak's boundary paused that cycle before it read any key |
| Start ungated (tokens mode, a `--config` without `auth.oidc`), then resume under SSO | The start mode, which a resume or attach keeps, refuses every sign-off |

## 4. Enforcement

`approvals.gate(ctx, conn, spec_id, spec, *, lock=False)` returns `(approval_id, params_approval)`. In
oidc mode it refuses a stored body whose `assemble.content_hash` differs from its `content_hash`
(message 4). It returns the active approval (`FOR SHARE` when `lock`) covering `needs`; else `None`
when `needs` is empty or the mode is not oidc; else it raises `ApprovalRequired`
(messages 1–3): 409 via `_HANDLERS`, exit 2 via `_EXPECTED`, a `ToolError` in `_compare_key`.

| Where | Change |
|---|---|
| `runner.runs.create_run`, `compare_one` | Gate before resolving keys, then with `lock=True` in the insert transaction; `repo.create_run` takes `approval_id` and `keys_cut`; audit details gain `approval_id` and `approval` (`{needs, mode}`, as `params["approval"]`: `create_run`'s `mode` is the run's, and only these two write it). `compare_one` raises message 2, not its result, when its completion finds the run stopped by a revoke. |
| `resume_run` | One transaction, in order: `gate(lock=True)`, for a run paused for the licence `licence.need` ([LICENCE.md](LICENCE.md) §8), `set_run_status` also clearing `APPROVAL_PAUSE` and `LICENCE_PAUSE`, `repo.set_run_approval` (the approval only). A run's first approval gets `from_cycle = cycle + 1`: its current cycle read keys without one, unless a soak's boundary paused it, for want of one or for the licence, before it read any. A revoked approval replaced by a new one of the same content covers the current cycle. |
| `finish_cycle` | `repo.end_cycle` copies `runs.approval_id` (null before `from_cycle`) and `keys_cut` into `ds.cycles`. Then, at a soak boundary in oidc mode, a run with `needs` and no approval attaches the active one for its next cycle (`gate(lock=True)`) or starts that cycle paused (`APPROVAL_PAUSE`, by `system:worker`). In any mode, a boundary the licence refuses as of when that cycle falls due (`licence.stopped`: M3, M4 or M5) starts that cycle paused with `LICENCE_PAUSE = "run_paused_licence"` instead, still attaching an approval found; that reason wins. |
| `runs.keys_cut` | `{resolved, taken}` when `maxKeys` cut the key set: set by `create_run`, replaced by a rolling cycle (`_rolled` returns the cut), cleared by a rescan, `{taken}` from the scan's `maxKeys` stop. |
| `ds run`, `ds compare` in oidc | Find the file by content hash (`repo.spec_by_hash`) and gate before writing: a refusal writes nothing, and a stored version is not saved again (no `spec.create` row). |
| a schedule's firing (`runner/schedule.fire`) | It is `create_run`, so the gate judges each firing as it judges any run: in oidc mode an unapproved or revoked version starts nothing, and the time is recorded as `schedule.missed {reason: approval}` without the gate's message. The next due time is judged anew, so a grant lets it start with no other act. Saving a schedule of such a version is allowed and answered with the gate's message as a warning; the schedule's detail shows the gate's state, read unlocked ([SCHEDULES.md](SCHEDULES.md) §6, §10). A following schedule runs the name's newest stored version, never an older approved one. |
| a table pre-check ([PRECHECKS.md](PRECHECKS.md) §6) | Not gated: a pre-check is no run, and authors triage drafts, so it reads unapproved versions too. Two rules keep what approvals decide: in oidc mode a version that declares a `where` and that `approvals.gate`, read unlocked, does not admit is read at level `schema` only, with no probe, so no unapproved filter reaches a target (the pair reads `incomplete`, `not checked: spec 12 (…) declares a filter and is not approved: only its schema was read`); and a sum is shown as a number only when the column is `clear` by the version's security policy, neither side declares a filter, both sums are exact and, in oidc mode, the gate admits the version (else its verdict with the reason, `figures withheld: spec 12 is not approved`); under a filter a protected column's `true`, `empty` and `distinct` counts and its lengths are verdicts too (§10's threat row says what figures under a filter still tell). Listed `columns` select no rows and gate nothing here. A `regex` key map is no filter: `needs` counts it under no scope, so its pattern, a predicate over raw key values, reaches the target unapproved in a run and a pre-check alike, and the rows whose key it does not match are counted (a run's `skipped_a` and `skipped_b`, a pre-check's `null_keys`; PRECHECKS §6, §16). `ds precheck run` stores a file without the gate, as a pre-check needs none; under SSO a file whose content was never uploaded and needs an approval is still refused, as an upload would be. |

**Tokens and developer modes:** runs, compares and resumes go ahead, labelled; requests answer 409
`approvals need SSO (auth.oidc); this deployment runs in tokens mode` (or `in developer mode`);
decisions and revokes answer 403 `needs the rule_approver role` (developer: `the rule_approver role
needs a signed-in person`). The CLI warns `spec 12 (customer-profile 1.4.0) needs a four-eyes approval
of clear, known_diffs; tokens mode has no approvers, so run R-… is labelled unapproved`; `ds serve` and
`ds auth check` warn `no IdP group maps to rule_approver or above: nothing can be approved`.

**Labels:** `GET /v1/runs/{id}` gains `approval` (`state`, `approval_id`, `needs`, `mode`, `deciders`
`[{actor, name, at}]`, `revoked_by`, `revoked_by_name`, `revoked_at`, `reason`) and `signoffs`, every
sign-off request of its cycles (revoked ones included) as items without `attests`. The Run screen,
`report.pdf`, the P0 summary and `ds runs` say (`approvals.label`; `web/src/four-eyes.ts` mirrors it)
"Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has no
approvers). Not sign-off evidence." (`was` for one scope; `developer mode has no approvers`, or `the run
has no approval` in oidc; before 008 "Unapproved spec version: the run started before approvals (008).
Not sign-off evidence."), "Spec approval A-… was revoked on 2026-09-27 by jdoe (wrong clear path). Not
sign-off evidence.", who approved, when: "Spec approval A-… was granted by bob on 2026-09-27." (two:
"by amy on 2026-09-26 and bob on 2026-09-27"; a run started outside SSO adds " Not sign-off evidence: the
run started in tokens mode." or "… in developer mode."), or "The spec version needs no four-eyes approval."
Days are UTC. `report.pdf` and the P0 summary label the reported cycle by the approval it ran under
(`approvals.run_approval(conn, run, cycle)`: an ended cycle's `ds.cycles.approval_id`, a live one's the
run's, none before `from_cycle`); the Run screen and `ds runs` label the run. A run created before 008's
`applied_at` keeps the "(008)" label whatever it attaches or resumes under: its `approval` has `state`
`unapproved` and null `needs` and `mode`. `report.pdf`'s header and the P0 summary's "Four-eyes approval"
section add the cycle's sign-off likewise (`approvals.signoff_label`: its approved request, else its newest revoked one; "Sign-off
A-… was granted by …", "Sign-off A-… was revoked on … by … (…). Not sign-off evidence."); `ds runs`
prints the label under each run.

## 5. Step-up

Each approval step needs `auth_time` within 15 minutes, else 403 `reauth_required`. `FRESH` and `fresh`
move from `api/routes/auth.py` to `api/deps.py` as `require_fresh(role)` (token creation's dependency)
and `fresh(who)`, which the decision route applies to `approve` only, as its body decides. Rejecting and
revoking only restrict and need no step-up. The UI sends the approver to
`/v1/auth/login?reauth=1&next=#/approvals/A-…`.

## 6. Interfaces

### 6.1 API (`api/routes/approvals.py`)

| Route | Role | Body | Answer |
|---|---|---|---|
| `GET /v1/approvals?status=&kind=&spec_id=&run_id=&limit=&cursor=` | viewer | — | `{items, next_cursor}` |
| `GET /v1/approvals/{approval_id}` | viewer | — | the item, `attests`, `review`, `you: {can_decide, reason}` |
| `POST /v1/specs/{spec_id}/approvals` | spec_author | `{note?}` | 201 `{approval_id, status, scopes, approvers_needed}` |
| `POST /v1/runs/{run_id}/cycles/{cycle}/signoff` | operator; in a vendor build, an Enterprise licence | `{note?}` | 201 `{approval_id, status, attests_sha256, approvers_needed}` |
| `POST /v1/approvals/{approval_id}/decision` | rule_approver; approving a sign-off in a vendor build, an Enterprise licence | `{decision: approve\|reject, note?, content_hash?, scopes?, attests_sha256?}` | 200 the item |
| `POST /v1/approvals/{approval_id}/revoke` | rule_approver | `{reason}` | 200 `{approval_id, status, paused, stopped, revoked}` |

- **Items** add the spec's name, version and owner, `approvers_needed` and actor names; lists leave out
  `attests`. **`review`:** the needed items (a spec's `{scope, pointer, value}` per member each scope
  covers; a sign-off's `{rule}` and `{root_cause}` rows from `attests`), the exclusion set (`persons`
  with names, `unattributed`), `history` (each earlier request of the version or cycle: status,
  decider, `decision_note`, `revoke_reason`) and, for a spec, the `content_hash` a decision echoes and
  `previous`, the name's newest approved version before this one, both bodies masked. `you` answers
  for an approval step (rejecting has no four-eyes check). A sampled sign-off's `review` adds `ledger`,
  every draw of its population in commit order with its status, close and `drawn_at`, each row's `order`
  `before`, `this` or `after` the signed draw; a `sampling` spec version's adds its population's ledger
  (`order` null; [SAMPLING.md](SAMPLING.md) §10). A format-3 sign-off's `review` adds `lines`, what the
  labelling it attests says (`accuracy.lines`, naming the sign-off while the request is open or approved;
  none when it attests no labelling), and `labellings`, every labelling of its cycle, newest first
  (`{labelling_id, drawn_by, drawn_at, item_count, labelled_by, labelled_at, attested}`;
  [ACCURACY.md](ACCURACY.md) §8); `attests.accuracy` is in `attests`.
- `POST /v1/specs` gains `needs` and `approval_id` (its active approval); `GET /v1/specs` items gain
  `approval`, the version's newest request (`approval_id`, `status`, `scopes`) or null.
- The trigger's `check_violation` (23514, `ds.approvals:`) maps to 409 with the texts below, never 500
  (a refusal the route cannot explain says the trigger's text without its prefix). A request or
  decision refused for several §1.2 problems names them all, joined by `; `.

### 6.2 Messages (binding; each asserted)

**Gate** (409; the CLI exits 2):
1. `spec 12 (customer-profile 1.4.0) needs a four-eyes approval of clear, known_diffs before it runs`
2. `spec 12 (customer-profile 1.4.0) needs a new four-eyes approval: A-… was revoked`
3. `spec 12 (customer-profile 1.4.0) needs a new four-eyes approval: A-… covers clear; this build also needs raw_clear`
4. `spec 12's stored body does not re-hash to its content hash (edited, or not storable as uploaded): upload a changed version (a new version)` (the same content maps to the same row)
5. CLI: `spec customer-profile 1.4.0 has no approved upload: upload it on the Specs screen, signed in, and have it approved (a CLI or token upload needs two approvers)`

**Upload** (422, storing nothing; `POST /v1/specs` and `ds run`/`ds compare`, which exit 2): `spec body
holds NUL at /…, which the store cannot keep`; `spec body holds -0.0 at /…, which the store keeps as 0:
write 0` (`approvals.unstorable`, before storing); once stored, in the same transaction, the stored body
is re-read and re-hashed (`approvals.unstored`): `spec body holds 1e+16 at /…, which the store keeps as
10000000000000000: write 10000000000000000` (any number whose text jsonb changes), else `spec 12's stored
body differs from this upload at /… (edited, or not storable as uploaded): upload a changed version (a
new version)`.

**Request** (409 unless noted): 404 `spec 12 not found`, `run R-… not found`; `spec 12 already has an
open request A-…`; `spec 12 is already approved: A-…`; `cycle 2 of run R-… already has an open
sign-off request A-…`; `cycle 2 of run R-… is already signed off: A-…`; §1.2's.

**Licence** (409; [LICENCE.md](LICENCE.md) §9 M6; a vendor build's, never an open build's, §19): a
sign-off's request, first in its transaction
(after the SSO 409, so before its 404), and its approval step, after the approver's own refusals, answer
`four-eyes sign-off is an Enterprise feature and licence LI-… is pro (docs/mvp/LICENCE.md §6)`, or in an
unlicensed evaluation `four-eyes sign-off is an Enterprise feature and this installation is an
unlicensed evaluation: install an Enterprise licence for installation I-…`.

**Decision** (409 unless noted): 404 `approval A-… not found`; `approval A-… is already <status>`; `approval
needs an approver other than you: <why>`, `<why>` being `you requested it`, `you uploaded a version of
customer-profile since spec 9` (without `since …` when no version before is approved) or `you started run
R-…`, each plus ` through token ci-bot (T-…)` when that actor was a token, or, for a sign-off, `you scored
its adjudication J-…`, then `you labelled its labelling V-…` (013), then `you closed draw D-… of its
ledger` (011) (else, for a sign-off, `you are an
author of spec approval A-…`; for a spec version, from the authors its step 1 stored, `you were an author of
it when step 1 of A-… was taken`); `you approved step 1 of A-…; another rule_approver must approve it`;
`step 1 of A-… was taken by amy, who is now an author of it; reject it and request it again` (its first
approver joined the exclusion set since: a revoke moved the lineage's base back, or they uploaded it again;
`you` says it too); `your stored roles lack rule_approver, or your account is disabled; sign in again`; `the
reviewed content hash or scopes are not spec 12's; reload and review again`; `spec 12's needs changed since
the request; request it again`; `cycle 2 of run R-… changed since the sign-off was requested; request it
again` (also for an `attests_sha256` other than the request's); §1.2's, a sampled cycle's G1–G11 among them
([SAMPLING.md](SAMPLING.md) §13); 422 `content_hash and scopes are required to approve a spec version`,
`attests_sha256 is required to approve a sign-off`, `note is required to reject`; 403 `reauth_required`.

**Revoke:** 404; 409 `approval A-… is <status>, not approved`. **Payloads:** 422 `note must be 1 to 1000
characters without control characters` (reasons: 200), also for text with no UTF-8 form (a lone
surrogate); `spec_id` on `GET /v1/approvals` is 1 to 2^63-1 (else 422). **Loader:** `unapproved_clear`
says `under SSO a run needs a four-eyes approval of unlisted_clear`.

### 6.3 CLI and UI

| Command | Audit | `DS_OPERATOR` |
|---|---|---|
| `ds approvals [--status S] [--kind spec\|signoff] [--needed] [--limit N] [--json]` | — | no |
| `ds approvals revoke A-… --reason TEXT` (break-glass) | as the API's | yes |

- The CLI cannot request or grant. `--needed` lists the versions live (`queued`, `running` or `paused`)
  runs use that need an approval and have none covering it, with those runs.
- **`Approvals.tsx`** (oidc only): one list, a status filter, a "Two approvers needed" marker. A review
  shows `history` above Approve and Reject (disabled with `you.reason`; Reject only for a caller who is
  not a `rule_approver` person), then a spec's needed items, exclusion set and `DocDiff` against
  `previous`, or a sign-off's counters, checklist (the start mode, and a table scan's counts),
  `keys_source` (flagging explicit key lists: `explicit` and `list`), rules and root causes, one per row;
  a format-3 sign-off's also what the labelling it attests says and may not be read as (`review.lines`
  and that labelling's `limits`, the server's text), or `No labelling a person labelled is attested.`,
  and each other labelling of the cycle in its one-line `summary` ([ACCURACY.md](ACCURACY.md) §10).
  An approved one shows Revoke, with a reason, to a `rule_approver` person. Once revoked there, the
  review shows the revoke's answer under "Revoked" in the panel's place (`Runs paused: …. Runs stopped:
  …. Sign-offs revoked: ….`, `none` for an empty list): it is the review's own state, so it outlives the
  reload that brings the status `revoked`, and is gone once the review is left (the audit log keeps
  it: `paused` and `stopped` on the approval's `approval.revoke` row, and a row with `cascade_of` for
  each sign-off revoked with it). A 403 `reauth_required` offers the §5 sign-in.
- **`Specs.tsx`** gains an Approval column and "Request approval" (a version without a request, or whose
  newest was rejected or revoked), **`Run.tsx`** the label and a sign-off panel per ended cycle
  ("Request sign-off" for a cycle without an open or approved request, not purged).

### 6.4 Audit actions (added to AUTH §6 and RETENTION §8)

`approval.request` (`kind`, `spec_id`, and `content_hash`, `scopes` or `run_id`, `cycle`,
`attests_sha256`); `approval.grant` (plus `step`, `of`, `authors`, `auth_time`; a sampled sign-off's
final step also `later_draws: [{draw_id, status}]`, the ledger draws after the signed one as R-c judged
them then, which a pack reads: [SAMPLING.md](SAMPLING.md) §9); `approval.reject`;
`approval.revoke` (`reason`, `paused`, `stopped`, `cascade_of`; by the revoker or purger);
`retention.hold` and `retention.release` (`signed off: A-…`; by the approver or revoker); `paused`,
`cancelling` and `running` (`reason`: `run_paused_approval`, `approval_revoked` or null; `approval_id`);
`spec.rehash` (object `specs`; `spec_ids`, by `system:migration`). Notes never enter details; revoke
reasons do.

## 7. Migration `008_approvals.sql`

Checked on a local `postgres:16-alpine` over 001–007, token cases and revoke races included.

```sql
-- Four-eyes approvals (docs/mvp/APPROVALS.md §3).
CREATE FUNCTION ds.accountable(actor text) RETURNS text[] LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN actor ~ '^user:U-[0-9a-f]{16}$' THEN ARRAY[actor]
        WHEN actor ~ '^token:T-[0-9a-f]{16}$' THEN coalesce((
            SELECT array_remove(ARRAY[substring(t.created_by FROM '^user:U-[0-9a-f]{16}$'),
                                      'user:' || t.owner_user_id], NULL)
            FROM ds.api_tokens t WHERE t.token_id = split_part(actor, ':', 2)), '{}')
        ELSE '{}' END
$$;
CREATE FUNCTION ds.attributed(actor text) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(actor ~ '^user:U-[0-9a-f]{16}$', false)
$$;

ALTER TABLE ds.runs ADD UNIQUE (run_id, spec_id);
CREATE DOMAIN ds.note AS text CHECK (VALUE <> '' AND length(VALUE) <= 1000 AND VALUE !~ '[\x01-\x1f\x7f-\x9f]');

CREATE TABLE ds.approvals (
    approval_id    text PRIMARY KEY CHECK (approval_id ~ '^A-[0-9A-Z]{26}$'),
    kind           text NOT NULL CHECK (kind IN ('spec', 'signoff')),
    spec_id        bigint NOT NULL REFERENCES ds.specs,
    run_id         text,
    cycle          integer,
    scopes         text[] NOT NULL DEFAULT '{}'
                   CHECK (scopes <@ '{clear,unlisted_clear,known_diffs,population_filters,raw_clear}'),
    attests        jsonb CHECK (jsonb_typeof(attests) = 'object'),
    attests_sha256 text CHECK (attests_sha256 ~ '^[0-9a-f]{64}$'),
    status         text NOT NULL DEFAULT 'requested'
                   CHECK (status IN ('requested', 'approved', 'rejected', 'revoked')),
    requested_by   text NOT NULL CHECK (requested_by ~ '^(user:U|token:T)-[0-9a-f]{16}$'),
    requested_at   timestamptz NOT NULL DEFAULT now(),
    request_note   ds.note,
    authors        text[],     -- set by the trigger (§3)
    unattributed   boolean,
    first_by       text CHECK (first_by ~ '^user:U-[0-9a-f]{16}$'),
    first_at       timestamptz,
    decided_by     text CHECK (decided_by ~ '^user:U-[0-9a-f]{16}$'),
    decided_at     timestamptz,
    decision_note  ds.note,
    revoked_by     text,
    revoked_at     timestamptz,
    revoke_reason  ds.note,
    UNIQUE (approval_id, spec_id, kind),
    FOREIGN KEY (run_id, spec_id) REFERENCES ds.runs (run_id, spec_id),
    FOREIGN KEY (run_id, cycle) REFERENCES ds.cycles (run_id, cycle),
    CHECK ((kind = 'signoff') = (run_id IS NOT NULL) AND (run_id IS NULL) = (cycle IS NULL)
           AND (run_id IS NULL) = (attests IS NULL) AND (attests IS NULL) = (attests_sha256 IS NULL)),
    CHECK (kind = 'spec' OR scopes = '{}'),
    CHECK ((status = 'requested') = (decided_by IS NULL) AND (decided_by IS NULL) = (decided_at IS NULL)),
    CHECK ((first_by IS NULL) = (first_at IS NULL) AND (status <> 'approved' OR first_by <> decided_by)),
    CHECK (status <> 'approved' OR (authors IS NOT NULL AND (NOT unattributed OR first_by IS NOT NULL))),
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL) AND (revoked_at IS NULL) = (revoked_by IS NULL)
           AND (revoked_at IS NULL) = (revoke_reason IS NULL))
);
CREATE UNIQUE INDEX approvals_open_spec ON ds.approvals (spec_id) WHERE kind = 'spec' AND status = 'requested';
CREATE UNIQUE INDEX approvals_active_spec ON ds.approvals (spec_id) WHERE kind = 'spec' AND status = 'approved';
CREATE UNIQUE INDEX approvals_open_signoff ON ds.approvals (run_id, cycle) WHERE kind = 'signoff' AND status = 'requested';
CREATE UNIQUE INDEX approvals_signed_cycle ON ds.approvals (run_id, cycle) WHERE kind = 'signoff' AND status = 'approved';

CREATE FUNCTION ds.spec_accountable(spec bigint, OUT persons text[], OUT unattributed boolean)
LANGUAGE sql STABLE AS $$
    WITH this AS (SELECT spec_id, name FROM ds.specs WHERE spec_id = spec),
    base AS (
        SELECT coalesce(max(s.spec_id), 0) AS id FROM ds.specs s JOIN this ON s.name = this.name
        WHERE s.spec_id < this.spec_id AND EXISTS (
            SELECT 1 FROM ds.approvals a WHERE a.spec_id = s.spec_id AND a.kind = 'spec' AND a.status = 'approved')),
    versions AS (
        SELECT s.spec_id, s.created_by FROM ds.specs s JOIN this ON s.name = this.name, base
        WHERE s.spec_id > base.id AND s.spec_id <= this.spec_id),
    uploads AS (
        SELECT v.created_by AS actor FROM versions v
        UNION ALL
        SELECT l.actor FROM versions v
        JOIN ds.audit_log l ON l.object = v.spec_id::text AND l.action = 'spec.create')
    SELECT (SELECT coalesce(array_agg(DISTINCT x ORDER BY x), '{}') FROM uploads, unnest(ds.accountable(actor)) AS x),
           coalesce(bool_or(NOT ds.attributed(actor)), true)
    FROM uploads
$$;

CREATE FUNCTION ds.approval_authors(a ds.approvals, OUT persons text[], OUT unattributed boolean)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF a.kind = 'spec' THEN
        SELECT s.persons, s.unattributed INTO persons, unattributed FROM ds.spec_accountable(a.spec_id) s;
    ELSE
        SELECT coalesce(p.authors, '{}') || ds.accountable(r.created_by),
               p.unattributed IS NOT FALSE OR NOT ds.attributed(r.created_by)
          INTO persons, unattributed
          FROM ds.runs r LEFT JOIN ds.approvals p ON p.approval_id = a.attests->>'spec_approval_id'
         WHERE r.run_id = a.run_id;
    END IF;
    persons := ARRAY(SELECT DISTINCT x FROM unnest(persons || ds.accountable(a.requested_by)) AS x ORDER BY x);
    unattributed := unattributed IS NOT FALSE OR NOT ds.attributed(a.requested_by);
END $$;

CREATE FUNCTION ds.tg_approvals() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_mutable text[] := ARRAY['status', 'authors', 'unattributed', 'first_by', 'first_at', 'decided_by',
                              'decided_at', 'decision_note', 'revoked_by', 'revoked_at', 'revoke_reason'];
    v_step boolean;
    v_by text;
    v record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ds.approvals: approval % cannot be deleted', OLD.approval_id
            USING ERRCODE = 'restrict_violation';
    ELSIF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'requested' OR num_nonnulls(NEW.first_by, NEW.authors, NEW.unattributed) > 0 THEN
            RAISE EXCEPTION 'ds.approvals: approval % must start as an undecided request', NEW.approval_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF (OLD.status, NEW.status) NOT IN (('requested', 'requested'), ('requested', 'approved'),
                                        ('requested', 'rejected'), ('approved', 'revoked'))
       OR (to_jsonb(NEW) - v_mutable) IS DISTINCT FROM (to_jsonb(OLD) - v_mutable)
       OR (OLD.first_by IS NOT NULL AND (NEW.first_by, NEW.first_at) IS DISTINCT FROM (OLD.first_by, OLD.first_at))
       OR (OLD.status <> 'requested'
           AND (NEW.authors, NEW.unattributed, NEW.decided_by, NEW.decided_at, NEW.decision_note)
               IS DISTINCT FROM (OLD.authors, OLD.unattributed, OLD.decided_by, OLD.decided_at, OLD.decision_note))
    THEN
        RAISE EXCEPTION 'ds.approvals: approval % cannot go from % to % or change what it covers',
                OLD.approval_id, OLD.status, NEW.status
            USING ERRCODE = 'restrict_violation';
    END IF;
    v_step := NEW.status = 'approved' OR (OLD.first_by IS NULL AND NEW.first_by IS NOT NULL);
    v_by := CASE WHEN OLD.status = 'requested' AND NEW.status <> 'requested' THEN NEW.decided_by
                 WHEN v_step THEN NEW.first_by END;
    IF v_by IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM ds.users u WHERE 'user:' || u.user_id = v_by AND u.disabled_at IS NULL
                                   AND 'rule_approver' = ANY (u.roles)) THEN
        RAISE EXCEPTION 'ds.approvals: % is not an enabled rule_approver', v_by USING ERRCODE = 'check_violation';
    END IF;
    IF v_step THEN
        IF NEW.kind = 'signoff' THEN  -- a revoke waits (§2)
            PERFORM 1 FROM ds.approvals p WHERE p.approval_id = NEW.attests->>'spec_approval_id'
                AND p.kind = 'spec' AND p.spec_id = NEW.spec_id AND p.status = 'approved' FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'ds.approvals: the spec approval that % cites is not approved', NEW.approval_id
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        SELECT * INTO v FROM ds.approval_authors(NEW);
        NEW.authors := ARRAY(SELECT DISTINCT x FROM unnest(coalesce(OLD.authors, '{}') || v.persons) AS x ORDER BY x);
        NEW.unattributed := coalesce(OLD.unattributed, false) OR v.unattributed;
        IF v_by = ANY (NEW.authors) OR NEW.first_by = ANY (NEW.authors) THEN
            RAISE EXCEPTION 'ds.approvals: % may not approve % (four-eyes)', v_by, NEW.approval_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF (NEW.status = 'approved') <> (NOT NEW.unattributed OR NEW.first_by IS NOT NULL AND NEW.first_by <> v_by) THEN
            RAISE EXCEPTION 'ds.approvals: approval % needs % approver(s)', NEW.approval_id,
                    CASE WHEN NEW.unattributed THEN 2 ELSE 1 END
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER approvals_lifecycle BEFORE INSERT OR UPDATE OR DELETE ON ds.approvals
    FOR EACH ROW EXECUTE FUNCTION ds.tg_approvals();
CREATE TRIGGER approvals_no_truncate BEFORE TRUNCATE ON ds.approvals
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();

ALTER TABLE ds.runs ADD COLUMN approval_id text,
    ADD COLUMN approval_kind text NOT NULL DEFAULT 'spec' CHECK (approval_kind = 'spec'),
    ADD COLUMN keys_cut jsonb,
    ADD FOREIGN KEY (approval_id, spec_id, approval_kind) REFERENCES ds.approvals (approval_id, spec_id, kind);
ALTER TABLE ds.cycles ADD COLUMN approval_id text REFERENCES ds.approvals, ADD COLUMN keys_cut jsonb;
CREATE INDEX runs_approval_idx ON ds.runs (approval_id) WHERE approval_id IS NOT NULL;
```

- 008 also adds `specs_immutable`, a `BEFORE UPDATE OR DELETE` row trigger on `ds.specs` raising
  `restrict_violation` on a DELETE or `NEW IS DISTINCT FROM OLD` (`save_spec`'s no-op upsert passes),
  and `specs_no_truncate` (`ds.append_only`).
- The circular foreign keys are safe: runs, cycles and specs are never deleted. Nothing is backfilled.
- **Re-hash:** when 008 applies, `db.migrate` re-hashes every stored body in Python in the same
  transaction, and logs and audits (`spec.rehash`) those that differ.
- **011** ([SAMPLING.md](SAMPLING.md) §12) rebuilds `approvals_scopes_check` with `sampling` (before the
  reserved `raw_clear`), keeping every earlier row valid, and replaces `ds.approval_authors` as 009 did,
  adding `ds.accountable(closed_by)` of each draw in `attests.sample.ledger`, the scorer kept.

**Code:** new `deltasleuth/approvals.py` (`gate`, `needs`, `attests`, `signoff_problems`, `run_state`,
`run_approval`, `revoke`, `unstorable`, `unstored`, `APPROVAL_PAUSE = "run_paused_approval"`) and `api/routes/approvals.py`; in `store/repo.py`,
an approvals section and a new `unexplained_criticals(conn, run_id, cycle, bug_rules)`: distinct keys
with a CRITICAL finding of that cycle, unsuppressed or suppressed by a `classification: bug` rule (the
worker's `unexplained_critical` stays).

## 8. What an evidence pack consumes

Evidence packs are built ([PACK.md](PACK.md)); a pack of a signed cycle consumes:

1. **The sign-off** of the cycle: `pack.problems` rebuilds `attests` by its recorded format and refuses
   a changed hash (`the sign-off A-… of run R-… cycle 2 is stale (rules changed since it was signed
   under attests format 1): revoke it and request the sign-off again`; a format-0 sign-off `was signed
   before evidence packs (attests format 0)`), and needs a census of side a (PACK.md §1.2), not a
   census `keys_source`; from 011 it takes formats 1 and 2, a sampled cycle's frame record of side a
   standing for its census ([SAMPLING.md](SAMPLING.md) §11); from 013 formats 1, 2 and 3, a format-3
   sign-off's pack carrying the labelling it attests without keys ([ACCURACY.md](ACCURACY.md) §9).
2. **The spec approval** it cites, still `approved`, with its authors and deciders (the manifest's
   `decisions`).
3. **A decision per discrepancy class:** `attests.rules` (key counts) and `attests.root_causes`, each
   accepted, and no unexplained CRITICAL key (the certificate's "Classes decided at sign-off").
4. **Evidence:** the `approval.*` audit rows with `session_id` and `auth_time`, in `audit/chain.tsv`.
5. **Adjudication:** the reviewer sheet in the store (`ds.adjudications`), bound to the signed cycle by
   `attests.adjudication` (PACK.md §5).
6. **Labelled accuracy** (013): the labelling in the store (`ds.labellings`), bound to the signed cycle
   by `attests.accuracy` (its `labels_sha256` and `score_sha256`; ACCURACY §9), or none; and the cycle's
   labellings counted (`attests.labellings`), which the pack lists in `accuracy/attempts.json`.

## 9. Tests

No image is pulled. The SSO flow lives in `tests/unit/api`, beside AUTH §10's fake IdP; existing oidc
tests that start runs call `approved(client, spec_id)`. Every §6.2 message is asserted.

| File | Behaviours |
|---|---|
| `tests/unit/store/test_approvals_sql.py`, `test_migration_008.py` | `ds.accountable`, `ds.attributed` per actor; the lineage; bob uploading through a token fay created and carol owns: bob alone refused, bob then dave approved; every trigger refusal; the first approver rejecting; `runs.approval_id` refusing a sign-off; `specs_immutable`; `spec.rehash` |
| `tests/unit/store/test_approvals_races.py` | create, resume, boundary attach and decision against a revoke, both lock orders, two connections (the second waits on `Lock` in `pg_stat_activity` until the first commits): never a running run or approved sign-off under a revoked approval |
| `tests/unit/test_approvals.py` | each `signoff_problems` refusal (tokens and developer starts among them, a needs-empty version's too; a partial scan, attested); a cancelled soak's completed cycle passes, its last refused; capped run → revoke → approve → resume → refused; a capped rolling cycle refused; stable `attests`; a cycle labelled by its own approval; a pre-008 run unapproved after an attach |
| `tests/unit/api/…` | every route and refusal; both 23514 paths as 409; two-step approvals of CLI and token uploads; a first approver who became an author; `reauth_required` at + 16 min, not + 14; revoke pausing, stopping a one-shot, cascading, releasing the hold; the run, `report.pdf` and P0 summary after a revoke, per cycle and before 008; a signed cycle surviving a purge pass, and revoked by one after release; uploads the store would not keep, lone surrogates and an out-of-range `spec_id` |
| `tests/unit/{runner,cli}/` | the gate per entry point and mode; a boundary attaching or pausing; a tokens-started run resumed under SSO (its cycle stays unapproved, never signed off); a scan stopped by `until`, `maxDuration` or a cancel; `compare_one` after a revoke; `keys_cut`; `ds approvals --needed`; `ds run` writing nothing when refused or unstorable, and no `spec.create` row for approved content |
| `tests/e2e/test_api.py`, `web/test/four-eyes.test.ts` | tokens mode: runs labelled, requests refused; the labels, the review's decision body and requests, and the reauth URL |

## 10. Honest limits

| Who | Can | How it shows |
|---|---|---|
| An admin (holds `rule_approver`) | approve what they did not upload, request or start | audited; ABAC deferred |
| One person with two IdP accounts, or colluders | approve their own content | IdP hygiene; names |
| The CLI operator | revoke; lift a sign-off hold (a purge then revokes it); run ungated with a `--config` lacking `auth.oidc` against the same DSN (no `system.start` row) or in tokens mode; edit targets and settings | `params.approval.mode` (a resume or attach keeps it), the `create_run` detail's `approval` (`{needs, mode}`), the host audit (DEPLOY §10); such runs are never signed off |
| The database owner | write approval rows (in a vendor build, a sign-off without an Enterprise licence too: M6 is the route's check, not the trigger's; [LICENCE.md](LICENCE.md) §18), disable triggers, edit bodies | no fresh-session `approval.grant` SIEM line; message 4 |
| An operator who pre-checks a version, in tokens or developer mode, or of an approved version under SSO | learn from the figures under its filters, of the rows they select: how many there are (`where id = 7 and salary >= 50000` holds 1 row or none, a yes or no about any column the filter names); how many hold NULL in each compared column, protected and tokenised ones included, though the filter names only the key (a one-row filter tells whether that row's value is NULL); and, of every other figure of every compared column, whether the two sides agree (filters that select one row each, `id = 7` on side a and `id = 8` on side b of one table, tell of a number, a moment or a boolean whether the two rows' values are equal, and of a text or bytes whether they are as long and whether both or neither is empty). A protected column's sum, `true`, `empty` and `distinct` counts and lengths are never numbers under a filter, only verdicts | a run of the same version reads and counts the same rows (`rows_a`) but compares key with key, and shows none of these facts of a protected column: the pre-check tells more; `precheck.create` names the spec ids ([PRECHECKS.md](PRECHECKS.md) §6, §16; DEPLOY §7) |
| An operator who saves a schedule | have the worker start runs of a version with no person as their creator; be one of the two approvers of such a run's sign-off; in tokens or developer mode, have a following schedule run whatever version was stored last, ungated | `runs.created_by` is `system:worker` and `params.schedule` names the schedule; `schedule.create` and `schedule.update` name who saved it, and each `schedule.fire` the `revision` that save made ([SCHEDULES.md](SCHEDULES.md) §16) |

Keys in flight at a revoke finish (at most one lease). A sign-off approved before a vendor build's
licence stopped allowing sign-off stays approved, and its hold keeps its cycle for a pack after a renewal.

## 11. Upgrade

1. 008 refuses nothing; it reports bodies that do not re-hash, whose runs oidc mode refuses: upload a
   changed version (a new version) and have it approved; runs of the old one cannot resume.
2. In oidc mode, new runs of versions with `needs` wait for an approval; batches finish; live soaks
   attach one at their next boundary or pause there (RETENTION §4.3 cancels one paused past its
   retention days). RUNBOOK: `ds approvals --needed` before and after upgrading.
3. Existing versions are requested as they are; a CLI, token (so CI) or pre-005 upload in the lineage
   needs two approvers (a token-started run's sign-offs need two approvers, not its spec approval).
4. Runs before 008 keep working, labelled `unapproved` whatever they attach or resume under (the Run
   screen, `report.pdf`, the P0 summary and `ds runs`), and are never signed off.

## 12. Deferred, documents, size

**Deferred:** expiry; carrying approvals across versions; per-rule approvals; noise masks and cluster
labels; `raw_clear`; exposure-path diffing; tabs and an inbox; withdrawing
requests; ABAC; quorum; a CLI grant (never). Notifications are built ([ALERTS.md](ALERTS.md)): a request
waiting and a revoke are events (`approval.waiting`, `approval.revoked`); reminders stay deferred.

**Documents:** AUTH §4, §6, §11; RETENTION §4.3, §8; PLAN; CONTRACTS; architecture §13; RUNBOOK;
DEPLOY's checklist gains §3's bypasses. Adjudication and evidence packs: [PACK.md](PACK.md). Labelled
accuracy: [ACCURACY.md](ACCURACY.md). Cut-over readiness: [READINESS.md](READINESS.md), which amends §1.2.
Schedules: [SCHEDULES.md](SCHEDULES.md), which amends §3, §4 and §10. Table pre-checks:
[PRECHECKS.md](PRECHECKS.md), which amends §4 and §10.

**Size:** about 1,400 product lines and 1,300 test lines.
