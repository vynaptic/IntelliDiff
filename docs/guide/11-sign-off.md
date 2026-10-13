# 11. Signing off a migration

A comparison tells you where two systems differ. A migration ends when named people accept, in writing,
what was compared and what was found. This chapter covers the features that lead there: a sample nobody
chooses for populations too large to compare whole, labels that say which side was right, a count of the
old side, the readiness gates, the approval of a spec by a second person, the sign-off of a run, and the
signed evidence pack an auditor checks years later.

**What needs single sign-on.** [Chapter 1](01-install-docker.md) set up sign-in with an API token. A token
is a named password, not a person, and half of this chapter needs people: single sign-on (SSO), where each
person signs in through your organisation's identity provider. Sections 2 to 6 work with the token of
chapter 1. Sections 8 to 10 need single sign-on and at least two different people.

**If you have no identity provider to use, do sections 2 to 6 and read the rest.** DeltaSleuth ships no
practice identity provider, so nothing in sections 8 to 10 can be done on the installation of chapter 1
alone. Every answer printed there is real: "How this chapter was checked" says where it came from.

| Feature | With the token of chapter 1 | With single sign-on |
|---|---|---|
| Sampled runs: the draw, the strata, the judgement, the list of draws | Yes. The run is labelled unapproved | Yes, once the spec is approved |
| Closing a draw that did not pass | No | An operator signed in as a person |
| Labelled accuracy | Yes. Shown, never attested by a sign-off | Yes. Attested when a person uploaded the labels |
| The count of side a (the census) | Yes | Yes |
| The reviewer sheet kept by DeltaSleuth | No | An operator |
| Cut-over readiness | Yes: the five parity gates, at most `parity ready` | Yes: the governance and evidence gates are judged too |
| Approval of a spec version | No | A spec author asks, a rule approver decides |
| Sign-off of a run's cycle | No | An operator asks, a rule approver decides |
| Evidence pack | No | An operator signed in as a person issues it |
| Checking a pack | Anywhere, without the installation | Anywhere, without the installation |

**How this chapter was checked.** Every command was run on 2026-10-05 on one installation made as chapter
1 says, the numbered steps in the order printed, on macOS with Docker Desktop, with the practice database
of chapter 6 beside it. Run ids, approval ids, hashes of signed files and times will
differ on your machine.

For sections 7 to 10 that same installation was switched to single sign-on with the settings lines of
section 7, against a stand-in provider: the one the product's own tests use
(`tests/unit/auth/fake_idp.py`), listening on a loopback port of the machine, which a small forwarding
program made reachable for the app's container. Five stand-in people signed in through it, and the requests
that the screens' buttons send were sent over HTTP as those people. So every answer in sections 7 to 10 is
real and comes from a Docker installation like yours, but no step there was done with a real identity
provider. No screen was opened in a browser: each "In the web UI" part is written from the page's source.
Not verified here: anything on Linux.

## 1. What a sign-off claims, and what it does not

A sign-off is a recorded decision by a person. It says three things, and DeltaSleuth words all three the
same way everywhere:

- **Observed.** DeltaSleuth read both sides and stored what it saw. Every report, screen and pack says
  "observed". Nothing says "certified" or "proven".
- **On these keys.** A key is the identifier of one record, for example a customer number. The claim covers
  the keys one cycle compared: every row of two tables, the keys of a list, or a sample. It says nothing of
  keys outside them.
- **At this time.** Each read has a time. The claim is about the data as it was then.

A cycle is one pass of a run over its keys. A sign-off accepts one ended cycle, never a whole run. The
person who approves it must not be the person who wrote the spec, started the run or asked for the
sign-off. Approving decides each difference class the cycle found: the list is on the screen they approve.

A sign-off does not claim that the two systems are equal, that a difference was fixed, that the data is
still the same today, or that the truth is on either side. Three more words recur in this chapter:

- **Side a** is the old system, the baseline. **Side b** is the new one.
- A **census** means that side a was counted whole in the cycle, so no key of side a can be missing from
  the comparison without showing. A **key list** that someone wrote is not a census. A **sample** is a part
  of the population drawn by a rule.
- **Readiness** is a set of named gates that shows what is still missing. It is not evidence and decides
  nothing: a pair that reads `not ready` can still be signed off, and section 9 does exactly that.

The pieces build on each other in this order:

| Piece | What it adds | Who acts |
|---|---|---|
| A cycle of a run | Counts, differences and root causes, observed | An operator starts the run |
| Labelled accuracy (section 4) | For the differences: which side the truth favoured | People label a sheet, an operator uploads it |
| Reviewer sheet (section 5) | Whether people agree with the engine's verdicts | Reviewers fill a sheet, an operator uploads it |
| Spec approval (section 8) | A second person accepted the spec's content | A rule approver |
| Sign-off (section 9) | A person outside the work accepted the cycle's facts | A rule approver |
| Evidence pack (section 10) | One signed file of all of the above, checkable offline | An operator issues it, an auditor checks it |

## 2. Before you start: two shortcuts and the practice data

This chapter needs the installation of chapter 1 with its mock services running, and the practice database
of [chapter 6](06-tables.md): do its sections 1 and 2 first. They start a PostgreSQL container named
`deltasleuth-practice-db`, load the example's two tables into it, create the read-only account `ds_reader`,
and hand the app the two address files that the targets `crm-legacy` and `platform-users` read. The tables
are a legacy CRM's `crm.customers` (side a) and a new platform's `public.users` (side b): 1,000 customers
with seven planted differences. Run every command from the folder `IntelliDiff`.

In a second installation (chapter 1), write your project name in place of `deltasleuth` in
`deltasleuth-practice-db`, as in chapter 6, and your port in place of 8000.

1. Define `ds`, as in chapter 1, section 5.
   ```bash
   alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
   ```
   It prints nothing.
2. Define `dsapi`, a short form for requests to the API with the token of chapter 1. The token goes from
   its file through a pipe to curl, so it appears neither on the screen nor in a list of running programs.
   ```bash
   dsapi() { { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - "$@"; }
   ```
   It prints nothing.
3. Check it. `dsapi` prints the token's name and roles.
   ```bash
   dsapi http://localhost:8000/v1/auth/me
   ```
   You should see:
   ```text
   {"actor": "token:T-8951f1431e174745", "name": "first-run", "display_name": null, "kind": "token", "roles": ["viewer", "operator", "spec_author"], "csrf": null, "mode": "tokens", "idle_minutes": 30, "expires_at": "2027-01-03T20:36:47.930742+00:00"}
   ```

The two shortcuts last until the terminal closes. Zsh, the default shell on macOS, prints a `%` after an
answer that has no line end; it is not part of the answer.

4. Load the example data again. The script deletes both databases and makes them anew. That puts the
   planted differences back and removes what earlier chapters added to the two databases, among it the
   table `crm.profiles` of chapter 6, section 9. Section 3 makes a table of that name with other rows.
   ```bash
   docker exec -i deltasleuth-practice-db psql -q -U postgres -v ON_ERROR_STOP=1 -f - < examples/tables/seed.sql
   ```
   It prints nothing when the databases existed.
5. Let the account `ds_reader` read the new databases. Repeat this step after every run of step 4. It
   prints nothing.
   ```bash
   docker exec -i deltasleuth-practice-db psql -q -U postgres -v ON_ERROR_STOP=1 -f - <<'SQL'
   GRANT CONNECT ON DATABASE ds_demo_legacy, ds_demo_platform TO ds_reader;
   \connect ds_demo_legacy
   GRANT USAGE ON SCHEMA crm TO ds_reader;
   GRANT SELECT ON ALL TABLES IN SCHEMA crm TO ds_reader;
   \connect ds_demo_platform
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO ds_reader;
   SQL
   ```
6. Upload the example's spec. A spec is the file that describes one comparison. If an earlier chapter
   uploaded the same file, the answer names the spec stored then.
   ```bash
   dsapi -X POST -H 'Content-Type: application/yaml' --data-binary @examples/tables/spec.yaml http://localhost:8000/v1/specs
   ```
   You should see the spec's number, written `<spec id>` below, and what its runs would need approved
   under single sign-on:
   ```text
   {"spec_id": 1, "name": "crm-customers-vs-platform-users", "version": "1.0.0", "content_hash": "584feccfb13a2f024a73f70329891ece1a6dd58ee3b1360e4c200e96f24f54e0", "needs": ["clear", "population_filters"], "approval_id": null}
   ```
7. Start a run. A batch run of a table pair without keys reads both tables to their end.
   ```bash
   dsapi -X POST -H 'Content-Type: application/json' --data '{"spec_id": <spec id>, "mode": "batch"}' http://localhost:8000/v1/runs
   ```
   You should see the run's id, written `<run id>` below:
   ```text
   {"run_id": "R-35T7EWQY686K2Y75EZJJP5TF65", "warnings": []}
   ```
8. Wait ten seconds, then list the runs.
   ```bash
   ds runs
   ```
   You should see the run completed, with 6 keys different of 991 and a label that says nobody approved
   its spec:
   ```text
    Container deltasleuth-db-1  Running
   R-35T7EWQY686K2Y75EZJJP5TF65  crm-customers-vs-platform-users 1.0.0  batch  completed  cycle 1  different 6 of 991
     Unapproved spec version: clear, population_filters were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   ```

Every `ds` command first prints Docker's line ` Container deltasleuth-db-1  Running`, as here, and some
print lines that start `{"id":`, which are copies of audit records. From here on the outputs leave both
out.

The outputs of this chapter come from an installation that held nothing else. If earlier chapters ran this
spec, `ds runs` and the readiness lines of section 6 show their runs and cycles too, and the numbers of
your specs are higher than the 1, 2 and 3 printed here.

Sections 4 to 6 use this run. In the web UI the same two steps are the Specs screen ("Upload a spec",
"Save") and the Runs screen ("New run", "Start").

## 3. Sampled runs

### What it is for

Some populations are too large to compare key by key: 20 million keys take 11.6 days at 20 requests a
second. A sampled run compares a part of them, and the part is drawn by a rule that nobody can steer. It is
for operators and cut-over leads of very large pairs.

Four ideas carry it:

- The **frame** is the complete list of keys the sample is drawn from: here, every row of side a's table,
  read in one go.
- **A sample nobody chooses.** The spec names a label, the `salt`. For each draw the installation adds a
  random number of its own, the nonce, and keeps the keys whose SHA-256 hash with that salt is smallest.
  It writes a hash of the salt to the audit log in the same step, before any key is compared. Nobody can
  pick keys, and nobody can draw again quietly until a sample looks good.
- **Strata** are buckets the spec writes down, for example one per customer segment. Each bucket has a
  size, `targetSize`, and a tier: `standard` allows at most 0.1% discrepant keys, `high` 0.01%.
- **The judgement.** When the cycle ends, each stratum is judged once: `passed`, `failed` or `unfinished`.
  A stratum with no more keys than its size is compared whole and judged on its exact count. A sampled
  stratum is judged on a one-sided 95% upper bound of its share of discrepant keys.

### Before you start

The practice pair is a small one: a table of 40 customer profiles against the profile service of the
customer mock pair. Forty keys cannot pass a sample. They show a draw, the strata and a judgement.

1. Create the table on side a's database. Its 40 rows hold the segment and the status that the mock
   service's old variant holds for its 40 customers.
   ```bash
   docker exec -i deltasleuth-practice-db psql -U postgres -d ds_demo_legacy -v ON_ERROR_STOP=1 -f - <<'SQL'
   CREATE TABLE crm.profiles (customer_id text PRIMARY KEY, segment text NOT NULL, status text NOT NULL);
   INSERT INTO crm.profiles
   SELECT 'C' || lpad((12000 + n)::text, 8, '0'),
          CASE WHEN n IN (5, 6, 16, 19, 32, 34, 40) THEN 'SME' WHEN n = 31 THEN 'PRIVATE' ELSE 'RETAIL' END,
          CASE WHEN n IN (4, 12, 21, 24, 37) THEN 'SUSPENDED' ELSE 'ACTIVE' END
   FROM generate_series(1, 40) AS n;
   GRANT SELECT ON crm.profiles TO ds_reader;
   SQL
   ```
   You should see:
   ```text
   CREATE TABLE
   INSERT 0 40
   GRANT
   ```
2. Write the spec into the folder `.deltasleuth`, which git ignores. Its key source is a `hash_sample`
   over side a's table, with one stratum dimension, the segment, and three buckets of 10 keys.
   ```bash
   cat > .deltasleuth/profiles-sample.yaml <<'EOF'
   apiVersion: deltasleuth/v1
   kind: ComparisonSpec
   metadata:
     name: profiles-sample
     version: "1.0.0"
     owner: guide
     description: Segment and status of 40 customers, a table against a service, compared on a hash sample.
   normalizerVersion: "1.1.0"
   key:
     name: customer_id
     type: string
     pattern: "^C[0-9]{8}$"
     class: identifier
     source:
       kind: hash_sample
       method: bottom_k
       salt: practice-q4
       resolve: {times: 1, delay: 5s}
       strata:
         - name: segment
           column: segment
           buckets:
             - {label: SME, values: ["SME"], targetSize: 10}
             - {label: RETAIL, values: ["RETAIL"], targetSize: 10}
             - {label: other, targetSize: 10}
       source: {kind: table, side: a, strataColumns: [segment]}
   sides:
     a:
       role: legacy
       target: crm-legacy
       table:
         schema: crm
         name: profiles
         keyColumns: [customer_id]
         columns: [segment, status]
     b:
       role: new
       target: customer-b
       request: {method: GET, path: "/v2/customers/{{ key }}"}
       select: "$.data"
       map: '{"segment": segment, "status": lifecycleStatus}'
       notFound: {status: [404]}
       truncation: {countPath: "$.data.productCount", itemsPath: "$.data.products", cap: 100}
   compare: {}
   recheck: {delay: 1s, times: 1}
   security:
     unlisted: tokenize
     clear: ["$.segment", "$.status"]
   EOF
   ```

`method: bottom_k` is the method for a sample that will be signed off. The other method, `rate`, only
monitors: its draws are never signed off.

### In the web UI

1. Click "Specs". The panel "Upload a spec" has a file field and, under it, a box that holds the spec's
   text. The folder `.deltasleuth` is hidden, and a file dialog does not list hidden folders, so paste the
   text instead: copy it to the clipboard with the command below (macOS), click into the box and paste.
   ```bash
   pbcopy < .deltasleuth/profiles-sample.yaml
   ```
   It prints nothing. Not verified here: the paste and the file dialog, in a browser. The command was
   tried on a side pasteboard (`pbcopy -pboard ruler`), and that pasteboard then held the file's text. By
   the page's source, a file chosen in the dialog puts its text into the same box; on macOS, Cmd+Shift+.
   (full stop) in the dialog shows hidden folders.
2. Press "Validate". A table with the columns Pointer, Severity, Code and Message shows three rows of
   severity `warning` and code `undersized`. Press "Save". Beside the buttons you should see `Saved as spec
   #2.`, with your own number.
3. Click "Runs". In the panel "New run", choose `profiles-sample 1.0.0 (#2)` under "Spec", leave "Mode" at
   `batch`, leave the box "Keys, one per line" empty, and press "Start". The Run screen opens.
4. On the Run screen, find the heading "Sample". Under it you should see, once the run has ended:
   - a line `sample D-...`, the id of the draw;
   - a line that starts `Draw D-... at 2026-10-05 20:38 UTC, by bottom_k: salt practice-q4 with a nonce
     the installation drew; selection SHA-256`;
   - the line `Frame: 40 distinct keys (a only 40; b only 0; both 0); 0 outside key.pattern.`;
   - a table with the columns Stratum, Tier, Basis, Frame keys, Drawn, Done, Compared, Discrepant,
     Outstanding, Unresolved, Absent both, Truncated, Retrying, Bound, Worst case and Verdict, one row for
     each of `segment=SME`, `segment=RETAIL` and `segment=other`;
   - the heading "Sign-off precheck", with one line for each stratum that did not pass;
   - the heading "Draws of this population", with a table of the columns Draw, Spec, Run, Drawn, Status
     and "Draw and close". The status of this draw reads `failed blocking`.
5. With single sign-on, an operator who is signed in as a person also sees the button "Record the fix and
   close failed draws" under that table. Press it, type what was fixed into the box that opens, and
   confirm. You should see `Closed D-....` and the draw's row now ends with who closed it and the note. With
   the token of chapter 1 the button is not shown.

### On the command line

1. Ask how large a stratum must be. The command needs no settings.
   ```bash
   ds samples size --tier standard
   ```
   You should see:
   ```text
   standard (≤ 0.1%, one-sided 95%): 2,995 cleanly compared keys pass with at most 0 discrepant; draw 3,011 (0.5% margin); fix the size before any key is compared, never extend it
   ```
2. Ask the same for the high tier, for a stratum that may hold one discrepant key and still pass.
   ```bash
   ds samples size --tier high --failures 1
   ```
   You should see:
   ```text
   high (≤ 0.01%, one-sided 95%): 47,437 cleanly compared keys pass with at most 1 discrepant; draw 47,676 (0.5% margin); fix the size before any key is compared, never extend it
   ```
3. Upload the practice spec.
   ```bash
   dsapi -X POST -H 'Content-Type: application/yaml' --data-binary @.deltasleuth/profiles-sample.yaml http://localhost:8000/v1/specs
   ```
   You should see, with your own `spec_id`:
   ```text
   {"spec_id": 2, "name": "profiles-sample", "version": "1.0.0", "content_hash": "0dca65dd65b96ad4e3c38cccd7202cdd40c73c6fc3da0a4fc1b274e47099729e", "needs": ["clear", "population_filters", "sampling"], "approval_id": null}
   ```
4. Start a batch run, without keys: the keys come from the draw.
   ```bash
   dsapi -X POST -H 'Content-Type: application/json' --data '{"spec_id": <spec id>, "mode": "batch"}' http://localhost:8000/v1/runs
   ```
   You should see a run id, as in section 2.
5. Wait fifteen seconds, then list the runs.
   ```bash
   ds runs
   ```
   You should see the new run first, with a third line that names its draw, and the run of section 2
   under it:
   ```text
   R-35T7EYCC5XZ8066AZPSTJTEQWX  profiles-sample 1.0.0  batch  completed  cycle 1  different 1 of 18
     Unapproved spec version: clear, population_filters, sampling were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
     sample D-35T7EYCJRV8KW3SBQK05M9W959: 3 strata, 18 keys
   ...
   ```
6. List every draw of the installation, oldest first.
   ```bash
   ds samples
   ```
   You should see:
   ```text
   D-35T7EYCJRV8KW3SBQK05M9W959  spec profiles-sample  run R-35T7EYCC5XZ8066AZPSTJTEQWX cycle 1  bottom_k  18 keys  drawn 2026-10-05T20:38:46+00:00  failed (segment=RETAIL, segment=other)  blocking: close it with the fix
   ```
7. Print each stratum's counts and verdict from the run's coverage report. `cut` keeps ten of its columns.
   ```bash
   dsapi http://localhost:8000/v1/runs/<run id>/reports/coverage.csv | cut -d, -f1,10,12,82-86,89,92
   ```
   You should see:
   ```text
   stratum,compared,mismatched,tier,target,basis,frame_keys,drawn,ub95_one_sided,verdict
   *,18,1,,,,,,,
   segment=SME,7,0,standard,0.001,census,7,7,,passed
   segment=RETAIL,10,0,standard,0.001,sample,32,10,0.2589,failed
   segment=other,1,1,standard,0.001,census,1,1,,failed
   (contrast),0,0,,,,,,,
   ```

### How to read the result

- **18 keys of 40.** The frame held 7 SME customers, 32 RETAIL and 1 other. Each bucket takes at most 10.
- **`segment=SME`, basis `census`, passed.** The bucket's size covers all 7 keys, so the stratum was
  compared whole and judged on its exact count: no key discrepant.
- **`segment=RETAIL`, basis `sample`, failed.** Ten of 32 keys were drawn, and all ten matched. It still
  fails: after ten clean keys the 95% upper bound of the discrepant share is 25.89%, far above the 0.1% the
  tier allows. This is why step 1 says 2,995: only that many clean keys bring the bound under 0.1%. Your
  ten keys are other keys than this run's, and one of them may be the customer that side b does not know:
  the stratum then shows one discrepant key.
- **`segment=other`, failed.** Its one key was compared whole and differs: side a says `PRIVATE`, side b
  `WEALTH`. A stratum compared whole allows 0.1% of its keys, which for one key is none.
- **`(contrast)`** counts keys that were compared but belong to no sign-off stratum. Here there are none.
- **`blocking: close it with the fix`.** A draw that did not pass is spent. It is never tested again and
  never extended. It stays in the list of draws of its population, the ledger, and blocks the sign-off of
  every later draw of that table, under whatever spec name, until a person closes it with a note that says
  what was fixed. The approver of a later sign-off reads that note. Later, under
  single sign-on, the stand-in operator of section 7 closed this draw. The request of the button of step 5
  answered `{"closed": ["D-35T7EYCJRV8KW3SBQK05M9W959"]}`, and the draw's line in `ds samples` then ended
  `closed by user:U-93f7be9d5a7410e1 (omar) 2026-10-05T20:52:20+00:00: 'Segment of the one PRIVATE customer
  corrected on the platform'` in place of `blocking: close it with the fix`.

**What a sampled result supports.** A passed sample says: parity was observed on a sample, stratum by
stratum. Had a stratum's true discrepant share been above its target, the procedure would have passed it
with at most a 5% chance. That 5% is a property of the procedure. It is not a probability that the two
systems agree. No figure is given for the strata taken together, and nothing is said of keys outside the
frame, such as records created after it was read.

### Limits

- With the token of chapter 1 the run goes ahead labelled unapproved, and it can never be signed off.
  Under single sign-on a spec with a hash sample needs the approval scope `sampling` before any run draws
  (section 8).
- Closing a draw needs single sign-on and an operator signed in as a person. `ds` closes nothing.
- A hash sample needs a frame the product can read whole: a table side, a service's complete key listing,
  or one of each. Between two table sides it is refused, because a table scan already compares every row.
- A sample holds at most 1,000,000 keys. Only one `bottom_k` run per population is live at a time.
- The nonce is shown to nobody while the cycle is live, and afterwards only to a rule approver.
- Not verified here: a sample large enough to pass, a soak run that draws a new sample each cycle, and a
  frame read from a service's key listing. The mock services list no keys and know 40 customers.

### If something goes wrong

**Three warnings of code `undersized` when the spec is validated, for example `bucket segment=RETAIL
(standard, 0.1%) draws at most 10 keys; a zero-failure pass needs 2,995 cleanly compared keys (draw 3,011):
it passes only as a census`**
They are true of the practice spec: a bucket this small passes only when the frame holds no more keys than
its size. A real spec takes its sizes from `ds samples size`. Seen here in the answer to the request that
the "Validate" button sends.

**An error of code `truncation_undeclared`: `neither this side nor target 'customer-b' declares truncation
indicators (X33), so truncation would go undetected`**
A service side must say how a cut-off answer shows. Met here, with the same request, on a copy of the spec
without the `truncation` line of side b. Add the line.

**`{"detail": "a hash sample draws its keys from the frame: run it without --keys"}`**
The run was started with keys. Leave "Keys" empty, or leave `keys` out of the request.

**`{"detail": "a soak over a hash sample draws a new sample each cycle: start it rolling (ds run --soak
--rolling)"}`**
A soak run of a sampled spec must read its key source again each cycle. On the Runs screen, tick "resolve
the keys again each cycle".

**`{"detail": "closing draws needs SSO (auth.oidc); this deployment runs in tokens mode"}`**, or under
single sign-on with a token **`{"detail": "closing draws needs a signed-in person"}`**
A draw is closed by a person. Sign in through single sign-on as an operator and use the button of step 5.

**`{"detail": "the population of profiles-sample has no draw to close (every ended bottom_k draw passed or
is closed)"}`**
There is nothing left to close.

**A sign-off of the sampled cycle is refused with `stratum segment=RETAIL of cycle 1 of run R-... is over
its 0.1% target: its one-sided 95% upper bound is 25.89% (0 discrepant of 10 cleanly compared keys);
stratum segment=other of cycle 1 of run R-... is over its 0.1% target: compared whole, its 1 keys allow 0
discrepant or unresolved key(s); it has 1`**
A failed sample is not signed off. The same lines stand under "Sign-off precheck" on the Run screen. Met
here under single sign-on, with runs of the spec that the stand-in operator started after its approval
(section 8). A draw made while an earlier failed draw is still open adds one more line to both: `draw
D-... (spec profiles-sample, run R-... cycle 1) failed before draw D-... was made and was not closed
before it: close it with the fix, then sign off a draw made after the close`. For the run of step 4 itself
the request's answer begins `run R-... started in tokens mode: only a run started under SSO is signed
off`.

## 4. Labelled accuracy

### What it is for

A comparison finds that two sides differ. It cannot know which side is right. Labelled accuracy lets people
answer that for the differences one cycle found: DeltaSleuth draws the items, people look each one up in a
trusted source and write a label, and DeltaSleuth counts the labels. A cut-over board uses the counts to
see which side the differences speak against. DeltaSleuth never decides which side is right, and a label
changes no result.

### Before you start

Nothing to switch on. You need an ended cycle with differences, here the run of section 2, and the name of
a truth source: an authoritative system other than both compared sides. This chapter uses the made-up name
`REF.customer_master`.

### In the web UI

1. Click "Runs", then the id of the run of section 2. Scroll to the heading "Labelled accuracy". Under it
   is a panel "Cycle 1".
2. In its form, leave "Discrepant items" at 100, "Per root cause" at 2, "Accepted keys" and "Agreed keys"
   at 0. Type `REF.customer_master` into "Truth source". Under "As of", choose today's date as it is in
   UTC, the time DeltaSleuth keeps: `date -u +%F` in a terminal prints it. East of UTC your own calendar
   is a day ahead of it for the first hours after midnight, and that day is refused. Press "Draw". You
   should see `Drew V-...: download its sheet, label every item, then upload it.` and, under it, the drawn
   keys as links to the Key screen. A key is shown masked, as a token that starts `tok:`.
3. The table above the form now has a row with the columns "Labelling" and "What it says". Press "Download
   sheet" in that row. The browser saves `V-....csv`.
4. Fill the columns `label`, `basis` and `reason` of every row (see "How to read the result"), and save the
   file as CSV in UTF-8.
5. In the same row, choose the filled file under "Upload labels". The panel then prints what the labelling
   says, line by line, and a long paragraph that begins `Observed.` and says what the figures may not be
   read as.

### On the command line

The CLI lists labellings. Drawing and uploading go through the API.

1. Print today's date in UTC, the time DeltaSleuth keeps. It is written `<utc date>` below.
   ```bash
   date -u +%F
   ```
   You should see one date, here:
   ```text
   2026-10-05
   ```
2. Draw. `<run id>` is the run of section 2.
   ```bash
   dsapi -X POST -H 'Content-Type: application/json' --data '{"size":100,"per_cause":2,"accepted":0,"agreed":0,"truth":{"source":"REF.customer_master","as_of":"<utc date>"}}' http://localhost:8000/v1/runs/<run id>/cycles/1/labellings
   ```
   You should see the labelling's id, written `<labelling id>` below, and seven items:
   ```text
   {"labelling_id": "V-35T7EZT61NBV6H0M78D9D4YKE7", "salt": "...", "items": [{"item": 1, "key": "tok:identifier:v1:YDTQQBKAOBLATRQARCZPXX7Y4N", "population": "discrepant", "root_cause_id": "RC-b8f1b2eb9a37"}, ...], "warnings": []}
   ```
3. Download the sheet.
   ```bash
   dsapi -o .deltasleuth/sheet.csv http://localhost:8000/v1/labellings/<labelling id>/sheet.csv
   ```
4. Look at five of its sixteen columns.
   ```bash
   cut -d, -f1,3,5,6,8 .deltasleuth/sheet.csv
   ```
   You should see the seven items, in an order of your own:
   ```text
   item,population,path_pattern,change_type,engine_outcome
   1,discrepant,$.status,value_changed,MISMATCH
   2,discrepant,$.status,value_changed,MISMATCH
   3,discrepant,$.balance,value_changed,MISMATCH
   4,discrepant,$,value_changed,MISMATCH
   5,discrepant,$,missing_in_b,NOT_FOUND_B
   6,discrepant,$,extra_in_b,NOT_FOUND_A
   7,discrepant,$.phone,value_changed,MISMATCH
   ```
5. Label every row. In real work people do this in a spreadsheet, one record at a time. For this practice,
   one command writes labels by the kind of difference: the status and the membership differences favour
   side b, the balance and the two missing records favour side a, and the phone number cannot be decided.
   The sheet's lines end with a carriage return, which `\r` matches.
   ```bash
   sed -e '/,\$\.status,/s/,,,,\r$/,B_RIGHT,SYSTEM_OF_RECORD,,/' -e '/,\$,value_changed,/s/,,,,\r$/,B_RIGHT,SYSTEM_OF_RECORD,,/' -e '/,\$\.balance,/s/,,,,\r$/,A_RIGHT,SOURCE_DOCUMENT,,/' -e '/,\$,extra_in_b,/s/,,,,\r$/,A_RIGHT,OWNER_CONFIRMED,,/' -e '/,\$,missing_in_b,/s/,,,,\r$/,A_RIGHT,SYSTEM_OF_RECORD,,/' -e '/,\$\.phone,/s/,,,,\r$/,UNDECIDABLE,,NO_TRUTH,/' .deltasleuth/sheet.csv > .deltasleuth/sheet-labelled.csv
   ```
6. Upload the labelled sheet.
   ```bash
   dsapi -X POST -H 'Content-Type: text/csv' --data-binary @.deltasleuth/sheet-labelled.csv http://localhost:8000/v1/labellings/<labelling id>/labels
   ```
   You should see a long answer. Its `lines` are what every screen and report prints.
7. List the labellings, one line each.
   ```bash
   ds labellings
   ```
   You should see:
   ```text
   V-35T7EZT61NBV6H0M78D9D4YKE7 run R-35T7EWQY686K2Y75EZJJP5TF65 cycle 1: 7 of 7 discrepancies labelled 2026-10-05 through token first-run (T-8951f1431e174745) against REF.customer_master as of 2026-10-05 (no sign-off attests it): b (new) alone right 3, a (legacy) alone right 3, neither 0, did not matter 0, undecidable 1; b (new) wrong on 3 of 6 decided (every item labelled: exact; 42.85% to 57.15% counting the undecidable either way)
   ```

### How to read the result

**The sheet.** An item is one key's difference in one root cause, so a key with two kinds of difference has
two rows. For each row, fill:

| `label` | Means |
|---|---|
| `A_RIGHT` | The truth holds side a's values. For a record only one side holds: a is right to hold it, or right not to |
| `B_RIGHT` | The same for side b |
| `BOTH_WRONG` | The truth holds neither side's values |
| `BOTH_RIGHT` | The difference does not matter |
| `UNDECIDABLE` | The truth, as it stood when the item was observed, cannot be established |

A decided label needs a `basis`: `SYSTEM_OF_RECORD`, `SOURCE_DOCUMENT`, `BUSINESS_RULE` or
`OWNER_CONFIRMED`. `UNDECIDABLE` needs a `reason` instead: `NOT_LOCATABLE`, `TOKENISED`, `NO_TRUTH`,
`TRUTH_CONFLICTS`, `CHANGED_SINCE` or `NOT_REVIEWED`. The column `notes` is for the labellers and is never
stored. Do not change the other columns.

**The lines.** After the upload of step 6 the answer's first lines read:

```text
Labelling V-35T7EZT61NBV6H0M78D9D4YKE7 drawn 2026-10-05 by first-run, labelled 2026-10-05 through token first-run (T-8951f1431e174745) against REF.customer_master as of 2026-10-05, not by a signed-in person: no sign-off attests it.
Discrepancies: 7 of the 7 that cycle 1 of run R-35T7EWQY686K2Y75EZJJP5TF65 observed (one key's difference in one root cause; 6 keys, 6 root causes), drawn uniformly; side a is legacy, side b is new; each label is the truth when its item was observed.
Undecidable: 1 of 7 (1 NO_TRUTH); the ranges below count them either way.
Truth favoured b (new) alone: 3 of 7 (42.86%, every item labelled: exact; 42.85% to 57.15% counting the undecidable either way: 3 to 4 of the 7).
Truth favoured a (legacy) alone: 3 of 7 (42.86%, every item labelled: exact; 42.85% to 57.15% counting the undecidable either way: 3 to 4 of the 7).
...
b (new) wrong (truth favoured a, or neither): 3 of 6 decided (50.00%, every item labelled: exact; 42.85% to 57.15% counting the undecidable either way).
```

- The first line says who drew, who labelled and against what. `not by a signed-in person` is the token:
  no sign-off will attest this labelling.
- Each share is a count of labels. Here every difference of the cycle was drawn, so the counts are exact.
  When a cycle has more differences than "Discrepant items", the items are a random sample and each share
  comes with a 95% interval: 100 items give an interval about ±10 points wide at worst, 400 about ±5.
- The range `counting the undecidable either way` shows how far the share could move if the undecidable
  items had been decided.
- One line follows for each root cause. Its figures come from its own few items and describe it only.

The shares say which side the truth favoured on the differences this cycle found. They do not say whether
either system is correct overall.

### Limits

- A labelling is labelled once. To replace it, draw a new one; every labelling of the cycle stays listed.
- Only a labelling that a person signed in through single sign-on uploaded is attested by a sign-off and
  carried in an evidence pack. Upload the labels before the sign-off is asked for: afterwards the cycle
  takes no draw and no labels.
- Whoever uploaded the labels may not approve the cycle's sign-off.
- Keys the cycle did not compare, and rows a table scan matched without reading them key by key, are never
  drawn.
- Drawing and uploading need the role `operator`. Reading and downloading need `viewer`.
- If the key is tokenised and the spec shows no value in clear, labellers cannot find the records. The
  draw is then refused until `acknowledge_unlocatable` is set; the Run screen offers a tick box of that
  name once the server asks. Not verified here: that case. The tables example shows four values in clear.

### If something goes wrong

**`{"detail": "truth.source must be a system identifier: 1 to 64 letters, digits and _ . : / -, starting
with a letter"}`**
The truth source is a system's name, never free text. Provoked with `crm customers!`.

**`{"detail": "truth.as_of must be a date (YYYY-MM-DD) no later than today"}`**
"As of" is the day of the source's state the labellers use. It cannot lie in the future, and "today" is
the date in UTC, which `date -u +%F` prints, not the date on your own calendar. Met here at 21:19 UTC on
2026-10-05 with `2026-10-06`, which was already the date in New Zealand (`TZ=Pacific/Auckland date +%F`
printed it). East of UTC, write the UTC date for the first hours after your midnight.

**`{"detail": "cycle 1 of run R-... has no discrepant item to label: set size and per_cause to 0"}`**
The cycle found no difference, as the clean runs of section 6 did. Label a cycle that has differences. Or
set "Discrepant items" and "Per root cause" to 0 and ask for agreed keys only: with "Agreed keys" at 2,
the draw here gave one key and the warning `989 rows matched by row hash cannot be sampled: agreed keys
are the 1 read per key`.

**`{"detail": "size must be 0 to 1,000, per_cause 0 to 20, accepted and agreed 0 to 500"}`**
One of the four sizes is out of range.

**`{"detail": "every drawn item needs a label: mark UNDECIDABLE with a reason where the truth cannot be
established, with reason NOT_REVIEWED where nobody reviewed it (items 1, 3, 5)"}`**
A partly filled sheet is refused and changes nothing. Label the items named, then upload again.

**`{"detail": "labelling V-... is already labelled: draw a new sheet"}`**
The labels of this labelling are in. Draw again only if the first sheet was wrong.

**A warning in the draw's answer: `truth.source crm-legacy.customers names side a (legacy): labels resting
on a compared side are circular and measure nothing; name an authoritative system other than both sides
(docs/mvp/ACCURACY.md §2.3)`**
The source's name is one of the compared sides. The labelling is drawn, and every output repeats the
warning.

**`{"detail": "needs the operator role"}`**
A viewer tried to draw.

## 5. The census of side a and the reviewer sheet

### What it is for

These are two facts a sign-off records and an evidence pack needs. The census says whether side a, the old
system, was counted whole in the cycle. Without it nobody can say that no record of the old system was
left out. The reviewer sheet says whether people agree with the engine: an operator draws a set of per-key
results, reviewers write their own verdict beside each, and the uploaded sheet is scored once.

### Before you start

The census needs nothing: every run of two tables without keys is a scan that reads side a to its end. The
reviewer sheet kept by DeltaSleuth needs single sign-on (section 7) and the role `operator`.

### In the web UI

1. Open the run of section 2 and scroll to the heading "Census, adjudication and evidence packs". Under
   "Cycle 1" you should see one line:
   `Census: side a counted, 989 rows of crm.customers read to the end in 1 snapshot, 2026-10-05; side b
   counted, 990 rows of public.users read to the end in 1 snapshot, 2026-10-05`.
   Under "Adjudications" and "Evidence packs" you should see `None.`. With the token of chapter 1 the
   paragraph above ends `Evidence packs need SSO (auth.oidc); this deployment runs in tokens mode.` and
   there are no buttons.
2. With single sign-on, as an operator: press "Draw" in the cycle's panel. A box asks `Draw a reviewer
   sheet of cycle 1: how many per-key results (1 to 500)?` and proposes 50. Confirm. You should see `Drew
   J-...: 5 key(s) to review. Download the sheet, fill in reviewer_verdict, then upload it.`
3. The table "Adjudications" now has a row with the columns Adjudication, Drawn, Sample and Score. "Sample"
   reads `up to 50 of 5 per-key results` and "Score" reads `not scored`. Press "Download sheet". The
   browser saves `J-....csv`.
4. Give the sheet to the reviewers. For each row they open the key on the Key screen, compare the two
   documents, and write `MATCH`, `DIFFERENT` or `INDETERMINATE` into the column `reviewer_verdict`.
5. In the same row, choose the reviewed file under "Upload reviewed sheet". You should see `Scored J-...: 5
   adjudicated, 5 agreed with the engine: did not pass (5 key(s) adjudicated; the exit needs at least
   50).`

### On the command line

1. Read the census of each ended cycle from the run. This works with the token of chapter 1. The run's
   answer is one line of several thousand characters, so a short Python program picks the census out of
   its part `"cycles"`.
   ```bash
   dsapi http://localhost:8000/v1/runs/<run id> | python3 -c 'import json,sys; [print(json.dumps(c["census"])) for c in json.load(sys.stdin)["cycles"]]'
   ```
   You should see one line for each ended cycle, here one:
   ```text
   {"cycle": 1, "at": "2026-10-05T20:38:19.739357Z", "a": {"basis": "scan", "target": "crm-legacy", "relation": "crm.customers", "rows": 989, "skipped": 0, "snapshots": 1, "first_at": "2026-10-05T20:38:19.739357Z", "last_at": "2026-10-05T20:38:19.739357Z"}, "b": {"basis": "scan", "target": "platform-users", "relation": "public.users", "rows": 990, "skipped": 0, "snapshots": 1, "first_at": "2026-10-05T20:38:19.739357Z", "last_at": "2026-10-05T20:38:19.739357Z"}}
   ```
   Without the program, do not stop at the first `"census"` in the answer. It reads `"census": null`: that
   one is of the cycle a run is working on, and a completed run has none. The ended cycles' follow after
   `"cycles"`.
2. With the token of chapter 1, a draw of a reviewer sheet is refused.
   ```bash
   dsapi -X POST http://localhost:8000/v1/runs/<run id>/cycles/1/adjudications
   ```
   You should see:
   ```text
   {"detail": "evidence packs need SSO (auth.oidc); this deployment runs in tokens mode"}
   ```

`ds` neither draws nor scores a stored reviewer sheet. Under single sign-on the stand-in operator's draw
(section 9, step 5) answered with the id of the sheet, written `J-...`, and five masked keys. The sheet he
downloaded held these columns, shown here for one row:

```text
key,engine_outcome,engine_verdict,confirmation,reason,top_findings,root_cause_id,selected_for,obs_a_id,obs_b_id,reviewer_verdict,reviewer_notes
tok:identifier:v1:4COXDO3GXGZQDBIOO5IHPHZ5T6,MISMATCH,DIFFERENT,rechecked,,HIGH VALUE_MISMATCH value_changed $.balance,RC-b56a0b39b5ab,root cause RC-b56a0b39b5ab,O-c1516ec7f4a68058,O-49215115db809866,,
```

With `reviewer_verdict` filled in every row, the upload's answer held `"score": {"exit": {"passed": false,
"minimum": 50, "reasons": ["5 key(s) adjudicated; the exit needs at least 50"]}, "rows": 5, ...,
"agreed": 5, ...}`.

### How to read the result

- **`basis: scan`, `rows: 989`, `snapshots: 1`** says side a's table was read to its end in one snapshot:
  one consistent view of the table. Side a is counted. The table holds 1,000 customers; the spec leaves out
  the 11 closed accounts, so 989 rows were read.
- A run of a key list between two services counts no side. A run of the mock customer pair
  (`mocks/customer/spec.yaml`, with the keys of `mocks/customer/keys.txt`) recorded a census with no side
  in it: the command of step 1 printed `{"cycle": 1, "at": null, "a": null, "b": null}`. The screen prints
  that as `side a not covered; side b not covered`, and no pack can cover such a cycle. A key list checked
  against a table side is counted by the worker in one more read of that table, but only when the settings
  hold a `readiness` or a `packs` block.
- **The score.** `5 adjudicated, 5 agreed with the engine` is the reviewers' verdicts against the engine's.
  A sheet passes only from 50 reviewed keys. The scan of the example read 5 keys one by one, so its sheet
  can never pass, and it does not have to: a sign-off needs a passed sheet only when the cycle has 50 or
  more per-key results.

### Limits

- A sheet is scored once. Only the verdicts are kept from an upload, never the notes.
- Draw and score before the sign-off is asked for. The sign-off attests the newest sheet a person scored;
  afterwards the cycle takes no draw and no score. Whoever scored the sheet may not approve the sign-off.
- Every draw is recorded and listed in the evidence pack, so drawing until a sheet passes shows.
- Outside single sign-on only the file tools `ds adjudicate` and `ds score` exist. They write and score a
  sheet in a folder and create nothing a sign-off can attest.

### If something goes wrong

**`{"detail": "adjudication J-... is already scored: draw a new sheet"}`**
Met here in an awkward way. A sheet was uploaded as it was downloaded, with no verdict filled in.
DeltaSleuth did not refuse it: it scored it, with `0 key(s) adjudicated; the exit needs at least 50`, and
the sheet was spent. Check the file before you choose it. If it happens, draw a new sheet; the pack will
list both draws.

**`{"detail": "the adjudication limit must be 1 to 500"}`**
The number typed into the box of step 2 was out of range.

**`{"detail": "cycle 1 of run R-... has a sign-off (A-...): revoke it before a new adjudication"}`**
The cycle has a sign-off that is requested or approved, and its reviewer sheet is fixed. The message says
"revoke" in both cases, but a requested sign-off cannot be revoked. If it is still requested, a rule
approver rejects it with a note. If it is approved, a rule approver revokes it. Either opens the cycle
again. Section 9, "If something goes wrong", shows both.

## 6. Cut-over readiness

### What it is for

Readiness shows, for each pair, which named gates hold on the stored evidence and where the pair stands on
a fixed ladder of states. A pair is a spec name that has a batch or soak run. Programme leads use it to see
what is still missing before cut-over, pair by pair. Its only figure is "k of m gates". It is never a
percentage or a forecast, and it is not sign-off evidence.

### Before you start

Nothing: it works with the token of chapter 1 and without any setting. The app must be running, because
its background worker measures each ended cycle; a new cycle shows within about a minute.

An optional `readiness` block in the settings file groups pairs into programmes and sets the gates. Steps
4 to 6 of the command line add one.

### In the web UI

1. Click "Readiness". You should see the heading "Readiness", a paragraph that begins `Readiness is a set
   of named gates`, the line `Readiness settings` with twelve characters of a hash, and a row of filters:
   "Programme", "Entity", "State", "Gate not met", "Pairs" and "Name". Beside them are the buttons
   "dashboard.pdf", "dashboard.csv" and "trends.csv".
2. Below, each programme has a heading such as `(no programme): 0 of 2 pairs ready (parity ready or better;
   worst: not ready)` and a table with the columns Pair, State, Gates, "Last evidence cycles", "Evidence
   cycle", Since, Sign-off, Pack and Deviations. "Gates" shows a figure such as `2 of 5` and twelve small
   shapes, one for each gate whether it is counted in the figure or not: `■` is met, `✕` not met, `?`
   unknown, `–` not applicable and `□` unavailable. With the token of chapter 1 four gates of the tables
   pair show `–` and three show `□`, and the sampled pair has one `?`, its gate `sample_ledger`. Hold the
   mouse over a shape to read the gate's name, status and text.
3. Click the pair's name. You should see "Readiness of crm-customers-vs-platform-users" with its state, the
   link "All pairs", the ladder of seven states with the current one marked, a line such as `2 of 5 gates`,
   and the button "Check sign-off now". Below are the tables "Parity gates", "Governance gates" and
   "Evidence gates", each with the columns Gate, Required, Status and Observed, then "Convergence", "Trend"
   with a table of the measured cycles, "Live soaks of this version", "Versions" and "Transitions".
4. Press "Check sign-off now". With the token of chapter 1 you should see `Nothing to check: the precheck
   is of an unsigned candidate cycle, under single sign-on.` Under single sign-on it lists what a sign-off
   request would be refused for, or `A sign-off request would find no problem now.`
5. On the Run screen, the header of a batch or soak run shows the same state as a link, for example `Pair
   readiness: not ready (2 of 5 gates)`.

### On the command line

1. Print every pair.
   ```bash
   ds readiness
   ```
   You should see the definition, the hash of the settings, then each pair with the gates it does not
   meet. Here for the run of section 2:
   ```text
   Readiness is a set of named gates, each met or not on observed evidence, and a place on a fixed ladder. It is not a probability that the two systems are at parity, not a forecast, and not sign-off evidence: ...
   readiness settings 60dc82b06781dad52631bf124887f87712b314eb28692831df9d70b97a998004
   (no programme): 0 of 2 pairs parity-ready or better (worst: not ready)
   crm-customers-vs-platform-users 1.0.0 (spec 1)  -  not ready  2 of 5 gates  cycle 1 of R-35T7EWQY686K2Y75EZJJP5TF65 (2026-10-05)
     critical not met: cycle 1 of run R-35T7EWQY686K2Y75EZJJP5TF65 has 2 key(s) with unexplained CRITICAL findings (bug rules count as unexplained); the gate allows 0
     targets not met: cycle 1 of run R-35T7EWQY686K2Y75EZJJP5TF65 has 6 discrepant or unresolved key(s) of 991 observed whole; its pooled 0.1% target allows 0: 6 over; 0 key(s) suppressed (known differences or noise only)
     convergence not met: 0 of the 3 evidence cycles in a row that 1.0.0 (spec 1) needs meet every per-cycle gate (cycle 1 of run R-35T7EWQY686K2Y75EZJJP5TF65 failed critical, targets); no live soak of 1.0.0 (spec 1) will add cycles
   profiles-sample 1.0.0 (spec 2)  -  not ready  3 of 6 gates  cycle 1 of R-35T7EYCC5XZ8066AZPSTJTEQWX (2026-10-05)
   ...
   ```
2. Print one pair with every gate.
   ```bash
   ds readiness pair crm-customers-vs-platform-users
   ```
   You should see the definition paragraph of step 1 again, left out here as `...`, then the pair, one
   line a gate, and the measured cycles:
   ```text
   ...
   crm-customers-vs-platform-users 1.0.0 (spec 1)  -  not ready  2 of 5 gates  cycle 1 of R-35T7EWQY686K2Y75EZJJP5TF65 (2026-10-05)
   parity  population  met: cycle 1 of run R-35T7EWQY686K2Y75EZJJP5TF65 observed side a whole (read to its end in 1 snapshot at 2026-10-05 20:38 UTC)
   parity  critical  not met: ...
   parity  targets  not met: ...
   parity  convergence  not met: ...
   parity  fresh  met: the evidence cycle (1 of run R-35T7EWQY686K2Y75EZJJP5TF65) ended 0 hours ago, within the gate's 7 days
   parity  graph (not required)  not applicable: 1.0.0 (spec 1) declares no graph
   governance  spec_approval  unavailable: run R-35T7EWQY686K2Y75EZJJP5TF65 started in tokens mode: only a run started under SSO is signed off
   governance  adjudication  not applicable: cycle 1 of run R-35T7EWQY686K2Y75EZJJP5TF65 has 7 per-key results, under the 50 an adjudication needs
   governance  sample_ledger  not applicable: cycle 1 of run R-35T7EWQY686K2Y75EZJJP5TF65 is not sampled
   governance  accuracy (not required)  not applicable: no accuracy gate is configured
   evidence  signoff  unavailable: sign-off needs SSO (auth.oidc); this deployment runs in tokens mode
   evidence  pack  unavailable: evidence packs are not configured (packs)
   convergence: 0 of 3 evidence cycles in a row
   2026-10-05T20:38:21+00:00  cycle 1 of R-35T7EWQY686K2Y75EZJJP5TF65  spec 1  census  6 different, 0 unresolved, 0 suppressed of 991  fails: critical, targets
   ```
3. Use it in a script. `--fail-unless` makes the command end with status 1 while a listed pair is below
   the state named.
   ```bash
   ds readiness --fail-unless parity_ready
   ```
   It prints the same list. Typed on the next line, `echo $?` printed `1`.
4. Add a `readiness` block to the end of the settings file. This one keeps the default gates and puts the
   pair into a programme named `cutover-q4`, as an entity of type `customer`.
   ```bash
   cat >> deploy/config/deltasleuth.yaml <<'EOF'
   readiness:
     gates:
       tier: standard
       convergence: {cycles: 3}
       fresh: {max_age: 7d}
     programmes:
       - name: cutover-q4
         pairs:
           - {spec: crm-customers-vs-platform-users, entity: customer}
   EOF
   ```
5. Check the block before it takes effect. The command prints each pair's gates and where each value
   comes from.
   ```bash
   ds readiness check
   ```
   You should see:
   ```text
   crm-customers-vs-platform-users (programme cutover-q4, entity customer): tier standard (installation), convergence 3 cycles (installation), fresh 7d (installation), graph not required (default), accuracy not required (default)
   profiles-sample: tier standard (installation), convergence 3 cycles (installation), fresh 7d (installation), graph not required (default), accuracy not required (default)
   readiness settings e5dd05d667cf2ef835cf44b0c2c19e03a7a5875bb7c0979350e72d345a412177
   ```
6. Restart the app so that it reads the file, and wait about forty seconds.
   ```bash
   docker compose restart app
   ```
   `ds readiness` then prints the new hash and the programme in place of `(no programme)`:
   ```text
   ...
   readiness settings e5dd05d667cf2ef835cf44b0c2c19e03a7a5875bb7c0979350e72d345a412177
   cutover-q4: 0 of 1 pairs parity-ready or better (worst: not ready)
   crm-customers-vs-platform-users 1.0.0 (spec 1)  customer  not ready  2 of 5 gates  cycle 1 of R-35T7EWQY686K2Y75EZJJP5TF65 (2026-10-05)
   ...
   ```
7. Now let the pair climb. Correct the six differences in the practice data, as the labels of section 4
   decided them: two on side a, four on side b.
   ```bash
   docker exec -i deltasleuth-practice-db psql -U postgres -v ON_ERROR_STOP=1 -f - <<'SQL'
   \connect ds_demo_legacy
   UPDATE crm.customers SET status = 'suspended' WHERE customer_id = 100007;
   UPDATE crm.customers SET status = 'active' WHERE customer_id = 100013;
   \connect ds_demo_platform
   UPDATE public.users SET balance = 11.25 WHERE user_id = 100009;
   UPDATE public.users SET phone = '+447700900005' WHERE user_id = 100005;
   DELETE FROM public.users WHERE user_id = 101001;
   INSERT INTO public.users (user_id, email, name, phone, status, country, balance, created_at)
   VALUES (100011, 'customer11@example.com', 'Customer 11', '+447700900011', 'active', 'GB', 13.75,
           timestamptz '2020-01-01 00:00:00+00' + 11 * interval '1 day 37 minutes');
   SQL
   ```
   You should see `UPDATE 1` four times, `DELETE 1` and `INSERT 0 1`, with a line `You are now connected
   to database ...` before each group.
8. Start a run as in section 2, step 7, wait forty seconds, and print the programme.
   ```bash
   ds readiness --programme cutover-q4
   ```
   You should see, after the definition paragraph and the `readiness settings` line, four gates met and
   the convergence gate counting:
   ```text
   ...
   cutover-q4: 0 of 1 pairs parity-ready or better (worst: not ready)
   crm-customers-vs-platform-users 1.0.0 (spec 1)  customer  not ready  4 of 5 gates  cycle 1 of R-35T7F5BH2S5X1YA00SCX882GHW (2026-10-05)
     convergence not met: 1 of the 3 evidence cycles in a row that 1.0.0 (spec 1) needs meet every per-cycle gate (cycle 1 of run R-35T7EWQY686K2Y75EZJJP5TF65 failed critical, targets); no live soak of 1.0.0 (spec 1) will add cycles
   ```
9. Repeat step 8 twice. After the second clean run the convergence line reads `2 of the 3`. After the
   third you should see:
   ```text
   ...
   cutover-q4: 1 of 1 pairs parity-ready or better (worst: parity ready)
   crm-customers-vs-platform-users 1.0.0 (spec 1)  customer  parity ready  5 of 5 gates  cycle 1 of R-35T7FAQ8FHXTF0TBG92G41W8B9 (2026-10-05)
   ```
   `ds readiness --programme cutover-q4 --fail-unless parity_ready` now ends with status 0. Without
   `--programme` it still ends with 1, because the sampled pair of section 3 is not ready.
10. Check that the stored measures still match the audit rows that recorded them.
    ```bash
    ds readiness verify
    ```
    You should see five cycles: the first run, the sampled run of section 3 and the three clean runs.
    ```text
    readiness: 5 measured cycle(s) match their readiness.measured rows; 0 excluded, 0 not measured
    ```
11. Save the reports into `.deltasleuth`, which git ignores. They are the buttons of the Readiness screen:
    `dashboard.csv` has a row for each pair and gate, `trends.csv` a row for each pair and measured cycle,
    `dashboard.pdf` up to 100 pairs.
    ```bash
    dsapi -o .deltasleuth/readiness-dashboard.csv 'http://localhost:8000/v1/readiness/reports/dashboard.csv?programme=cutover-q4'
    ```
    ```bash
    dsapi -o .deltasleuth/readiness-dashboard.pdf http://localhost:8000/v1/readiness/reports/dashboard.pdf
    ```
    Both print nothing. The CSV starts with the line `programme,entity,pair,version,spec_id,state,
    regressed,gates_passed,gates_required,gate,required,status,value,allowed,over,suppressed,run_id,cycle,
    ended_at,basis,deviations,config_sha256,text`; the PDF had five pages here.

### How to read the result

**The states**, lowest first:

| State | It means |
|---|---|
| `no evidence` | No complete cycle of the pair counts as evidence yet |
| `not ready` | A parity gate is not met |
| `unknown` | No parity gate is unmet, but one could not be judged |
| `parity ready` | Every parity gate is met |
| `sign-off ready` | Every governance gate is met too: ask for the sign-off |
| `signed off` | The cycle holds an approved sign-off |
| `evidenced` | And an issued evidence pack |

**The gates.** Each is `met`, `not met`, `unknown`, `not applicable` or `unavailable`.

| Group | Gate | Met when |
|---|---|---|
| Parity | `population` | The cycle observed side a whole: a table scan read to its end, a counted key list, or a sample whose frame holds side a. A key list between two services never meets it |
| Parity | `critical` | No key has an unexplained finding of severity CRITICAL, such as a record missing on one side |
| Parity | `targets` | Discrepant and unresolved keys are within 0.1% of the pair, or 0.01% for the `high` tier; a sampled cycle: its judgement passed |
| Parity | `convergence` | The newest 3 evidence cycles in a row each met the gates above |
| Parity | `fresh` | The evidence cycle ended at most 7 days ago |
| Parity | `graph` | Only when the settings require it |
| Governance | `spec_approval`, `adjudication`, `sample_ledger`, `accuracy` | What a sign-off needs: an approved spec, a passed reviewer sheet from 50 results, a sampled cycle's draws in order, and, only when configured, labels within bounds |
| Evidence | `signoff`, `pack` | An approved sign-off of the cycle, and an issued pack of it |

With the token of chapter 1 the governance and evidence gates read `unavailable` or `not applicable`, five
gates are counted, and a pair stops at `parity ready`. Under single sign-on with evidence packs configured,
eight were counted here: the five, `spec_approval`, `signoff` and `pack`. The gate
`adjudication` joins them for a cycle of 50 or more per-key results.

Two things in the outputs above deserve a second look:

- **`its pooled 0.1% target allows 0`.** The allowance is 0.1% of the keys, rounded down. For 991 keys
  that is none, so a pair of this size meets the `targets` gate only without any discrepant key.
- **Readiness is stricter than a sign-off, and separate from it.** A sign-off is refused for an unexplained
  CRITICAL finding, but not for a `targets` or `convergence` gate. Section 9 signs off a cycle with four
  open differences, and readiness then reads `not ready ... regressed`: the pair holds a sign-off and is
  below `signed off`.

### Limits

- Readiness judges batch and soak runs. One-shot compares and runs from captured samples are not assessed.
- The settings apply at a restart, and they are a file on the host: no screen changes them. A programme or
  pair may only tighten the tier. Fewer convergence cycles or older evidence are allowed, and are then
  named as a deviation on every surface.
- What the worker had recorded at a past time is CLI only: `ds readiness --as-of <time>`.
- Not verified here: `--as-of`, a pair closed with `closed:`, the accuracy and graph gates, and
  `ds readiness report --out`, which writes the report files into a folder inside the container.

### If something goes wrong

**The pair is missing, or still shows the cycle before the newest**
The worker measures a cycle after it ends and judges the pairs about every thirty seconds. Wait a minute.
If the app is stopped, nothing is measured.

**`ds readiness pair <name> --precheck` prints no precheck**
With the token of chapter 1 there is nothing to check, and the command says nothing about it. The screen's
button says `Nothing to check: the precheck is of an unsigned candidate cycle, under single sign-on.`
Under single sign-on the command ends with `precheck of cycle 1 of run R-... (computed now; the sign-off
decides):` and, when all is well, `nothing refuses its sign-off`.

**`sample_ledger unknown: the draw discipline of draw D-... of cycle 1 of run R-... is read once every
parity gate is met`**
This line of the sampled pair is not an error. The gate is judged only once the parity gates hold.

## 7. Single sign-on for the rest of this chapter

Sections 8 to 10 need people. **Without an identity provider that you can register DeltaSleuth with, stop
typing here and read on:** the token of chapter 1 can do none of the steps of sections 8 to 10, and
DeltaSleuth ships no practice provider. This section says what must be in place, and what this chapter
used in its stead.

**The roles.** Under single sign-on each person gets a role from the groups the identity provider reports.
Each role holds the ones before it: `viewer`, `operator`, `spec_author`, `rule_approver`, `pii_reader`,
`admin`. A token can hold at most `spec_author`. Only a signed-in person can be a `rule_approver`. The
stand-in people of this chapter were:

| Person | Role | Does |
|---|---|---|
| alice | `spec_author` | Uploads the spec, asks for its approval |
| omar | `operator` | Starts runs, draws and uploads sheets, asks for the sign-off, issues the pack |
| amir | `rule_approver` | Approves, rejects, revokes |
| bea | `rule_approver` | The second approver, where two are needed |
| vera | `viewer` | Reads and downloads |

### Switching it on

Single sign-on is three things: the client secret as a file, ten lines of the settings file, and the
secret's name in the site file `deploy/docker-compose.site.yml`. The administrator of your identity
provider registers DeltaSleuth there as a client and gives you the provider's address (the issuer), a
client id and a client secret. Step 6 prints the two addresses they must register. The
[deployment document](../mvp/DEPLOY.md), section 5, has a table of values for Entra ID, ADFS, Okta, Ping
and Keycloak.

1. Put the client secret into a file without showing it. Copy it to the clipboard where it was issued,
   then, on macOS:
   ```bash
   (umask 077; pbpaste | tr -d '\n' > deploy/secrets/oidc_client_secret)
   ```
   It prints nothing. `umask 077` creates the file readable by you alone, and `tr` removes a line end.
   Empty the clipboard afterwards with `pbcopy < /dev/null`, as in chapter 1.
2. Let the app's container read the file, as for the secrets of chapter 1.
   ```bash
   chmod 444 deploy/secrets/oidc_client_secret
   ```
3. Open `deploy/config/deltasleuth.yaml` in a text editor and find the block `auth:` near its top. It
   holds the line `public_url` and, a few lines below, ten lines switched off: the first reads
   `#   oidc:` and the last ends `admin: [DS.Admin]}`. Remove the first two characters, `# `, of each of
   the ten, and write your provider's values in place of the ones printed there. Edit this block where it
   stands. Do not add a second `auth:` block, and do not append these lines to the end of the file as
   sections 6 and 10 do with theirs. With the comments left out, the block used here then read:
   ```yaml
   auth:
     public_url: http://localhost:8000
     oidc:
       issuer: http://127.0.0.1:18463
       client_id: deltasleuth
       client_secret_ref: file:/run/secrets/oidc_client_secret
       subject_claim: sub
       username_claim: preferred_username
       group_claims: [groups]
       roles:
         {viewer: [G-view], operator: [G-op], spec_author: [G-author],
          rule_approver: [G-approver], pii_reader: [G-pii], admin: [G-admin]}
   ```
   The values are the stand-in provider's. `issuer` is the provider's address, `subject_claim` the field
   of its answer that identifies a person for good, `username_claim` the field with the name to show, and
   `group_claims` the field with the person's groups. The names in square brackets under `roles` are
   groups as the provider reports them. Map a group to `rule_approver`, or nothing can be approved.
4. Open `deploy/docker-compose.site.yml` and make two edits, as in chapter 6, section 2, step 9. First,
   add the name `oidc_client_secret` to the list in the line under `app:` that starts `secrets:`. After
   chapter 6 the line then reads:
   ```yaml
       secrets: [crm_legacy_dsn, platform_users_dsn, oidc_client_secret]
   ```
   Second, add one line at the very end of the file, under the lines of the block `secrets:` that chapter
   6 put there. It starts with two spaces.
   ```yaml
     oidc_client_secret: {file: ./secrets/oidc_client_secret}
   ```
5. Ask Compose whether it can read the site file. This command starts nothing.
   ```bash
   docker compose config | grep -c oidc_client_secret
   ```
   You should see a number, here `5`. An error in its place names the mistake in the file.
6. Check the settings against the identity provider before you start the app.
   ```bash
   ds auth check
   ```
   You should see the provider answer, its signing keys, and the two addresses to register with it. Against
   the stand-in provider this printed:
   ```text
   client credential: ok (client_secret_basic)
   discovery: ok (http://127.0.0.1:18463)
   signing key 'rsa-9ff22a54': RS256
   signing key 'ec-62060664': none of auth.oidc.id_token_algs (kty 'EC', alg 'ES256')
   clock offset: +0 s (the IdP's clock minus this host's)
   redirect URI to register: http://localhost:8000/v1/auth/callback
   post-logout redirect URI to register: http://localhost:8000/
   ```
7. Apply the change. The site file changed, so a restart is not enough.
   ```bash
   docker compose --profile mocks up -d
   ```
   You should see Docker's lines end with ` Container deltasleuth-app-1  Started`.
8. Wait ten seconds, then ask the app for its mode.
   ```bash
   curl -fsS http://localhost:8000/v1/health
   ```
   You should see `"auth": "oidc"`:
   ```text
   {"status": "ok", "ai": false, "auth": "oidc"}
   ```

**How the stand-in was reached.** The stand-in provider listened on the machine's own loopback address,
`127.0.0.1:18463`, which a container cannot reach: inside a container that address is the container
itself. So the output of step 6 was not printed by the `ds` of chapter 1, which runs in a container of its
own and answered `error: discovery unreachable (ClientConnectorError)`, and the app logged `warning:
identity provider unreachable at start; sign-in retries on first use` when step 7 started it. A small
forwarding program was then put into the app's container network, and `ds auth check`, typed inside the
app's container, printed the lines of step 6. A real provider has an address that containers reach, and
needs none of this. Not verified here: steps 1 and 6 with a real provider, and a sign-in through one. The
stand-in's secret is a fixed test value that a script wrote into the file, the `pbpaste` line was tried
with a made-up value on a side pasteboard, and the edits of steps 3 and 4 were made by a script that wrote
those lines.

**Signing in.** The sign-in page then shows one button, "Sign in with single sign-on". After the sign-in
the bar at the top has one more link, "Approvals". To leave the trial behind for good, follow "From a
one-machine trial" in the deployment document.

**The token under single sign-on.** The token of chapter 1 stays valid, and `dsapi` and scripts go on
reading with it: `dsapi http://localhost:8000/v1/auth/me` now answers with `"mode": "oidc"`. But the token
no longer starts a run of a spec version that lacks its approval. The run request of section 2, step 7,
now answers:

```text
{"detail": "spec 1 (crm-customers-vs-platform-users 1.0.0) needs a four-eyes approval of clear, population_filters before it runs"}
```

### Start clean

Two rules make the work of sections 2 to 6 unusable for a sign-off:

- A run started with a token is never signed off, whatever happens later. The sign-off request answers
  `run R-... started in tokens mode: only a run started under SSO is signed off`.
- A spec that a token uploaded has no author who can be named, so every version of that spec name needs
  two approvers. Asked through the token, the approval request of section 2's spec answered
  `"approvers_needed": 2`.

So the next sections use a copy of the tables spec under a new name, uploaded by a person. The tables
spec has a key pattern, which an evidence pack needs (section 10 says why), and the copy keeps it.

1. Make the copy.
   ```bash
   awk '/^  name: crm-customers-vs-platform-users$/ {print "  name: customers-cutover"; next} {print}' examples/tables/spec.yaml > .deltasleuth/customers-cutover.yaml
   ```
   It prints nothing.
2. Look at what changed.
   ```bash
   diff examples/tables/spec.yaml .deltasleuth/customers-cutover.yaml
   ```
   You should see:
   ```text
   18c18
   <   name: crm-customers-vs-platform-users
   ---
   >   name: customers-cutover
   ```
3. Put the planted differences back: run steps 4 and 5 of section 2 again. Loading the example data also
   removes the table `crm.profiles` of section 3. If you want to run the sampled spec again later, as the
   two approvers of section 8 let it, run step 1 of section 3, "Before you start", again after this step.
   Without the table its run answers `{"detail": "side a: table or view crm.profiles not found or not
   visible"}`.

### If something goes wrong

**`error: invalid settings /etc/deltasleuth/deltasleuth.yaml: duplicate key 'auth' (line 103, column 1)`**
The settings file holds two `auth:` blocks. Provoked here by adding the lines of "Switching it on", step
3, at the end of the file, and seen from `ds licence`, which opens the settings as a start of the app does.
Delete the added block and edit the one near the top. The line number in the message is where the second
block starts.

**`error: ds auth check needs the auth.oidc settings: these settings give tokens mode`**
The ten lines of "Switching it on", step 3, are still switched off, or the file was not saved.

**`error: discovery unreachable (ClientConnectorError)`** from `ds auth check`, and in the app's log
`warning: identity provider unreachable at start; sign-in retries on first use`
The containers cannot reach the address in `issuer`. Met here with the stand-in provider, whose address
is the machine's own loopback. Check the address, and that the machine's firewall and proxy let the
containers reach it.

## 8. Four-eyes approval of a spec version

### What it is for

A spec decides what is compared and what is shown. Under single sign-on a spec version that shows values in
clear, hides findings behind known-difference rules, filters what is compared or declares a sample may run
only after a second person approved exactly that content. "Four eyes" means two people: whoever wrote it
cannot approve it. Spec authors and approvers use it, and so does anyone who wonders why a run is refused.

### Before you start

Single sign-on (section 7), one person with the role `spec_author` and another with `rule_approver`. No
setting beyond those of section 7.

What needs approval is decided by the spec's content. The upload's answer names the scopes:

| Scope | In the spec |
|---|---|
| `clear` | `security.clear` paths: values stored and shown as they are |
| `unlisted_clear` | `security.unlisted: clear` |
| `known_diffs` | Active `knownDiffs` rules, which hide findings |
| `population_filters` | A table side's `where` or `columns`, and the compare filters |
| `sampling` | A `hash_sample` key source |

### In the web UI

1. As the spec author: click "Specs". In "Upload a spec", paste the text of
   `.deltasleuth/customers-cutover.yaml` into the box, as in section 3 (`pbcopy <
   .deltasleuth/customers-cutover.yaml` copies it), and press "Save". You should see `Saved as spec #3. Its
   runs need a four-eyes approval of clear, population_filters: request one in the list above.`
2. In the table, the spec's row has the button "Request approval". Press it. A box asks `Ask for a
   four-eyes approval of customers-cutover 1.0.0 (spec #3). A note for the approver (optional):`. Type a
   note and confirm. The review of the new request opens, headed "Approval A-..." with the status
   `requested`.
3. As the approver: click "Approvals". You should see a list with the columns Approval, Status, Kind,
   Spec, Covers, Approvers, "Requested by" and Requested, and a "Status" filter above it. A request that
   needs two approvers carries the mark "Two approvers needed". Click the approval's id.
4. Read the review. From the top it shows:
   - a summary: `The whole content of customers-cutover 1.0.0 (spec #3), owner examples; its runs need
     clear, population_filters. Content hash ...`, and who asked, when, with their note;
   - "Earlier requests": the requests of this version that came before, with who decided and their note;
   - the panel "Decision", with a box "Note: required to reject, and not kept for the first of two
     approvers" and the buttons "Approve" and "Reject". When you may not approve, "Approve" is grey and
     the reason stands beside it;
   - "What its runs need approved": a table with the columns Scope, Pointer and Value, one row for each
     thing a scope covers. Here: the four clear paths, side a's filter `status <> 'closed'` and its column
     list;
   - "Who may not approve it": here `alice`;
   - the spec's whole text, headed `The whole content: no earlier version of this spec is approved`. For a
     later version the heading reads `Changes since spec #... (...), approved as A-...` and the changes
     are marked, with "A: the version approved before; B: this version."
5. To reject: type a note and press "Reject". The status becomes `rejected`, and the spec's row on the
   Specs screen shows "Request approval" again.
6. To approve: press "Approve". The status becomes `approved`. If your sign-in is more than 15 minutes
   old you should see `An approval needs a sign-in in the last 15 minutes. Sign in again, then approve.`
   and the button "Sign in again", which returns to this review.
7. To revoke an approved request, open its review. A rule approver sees the panel "Revoke" with the field
   "Reason" and the button "Revoke". The panel is there only while the status is `approved`: a request
   that is still `requested` is rejected, not revoked. After a confirmation that names what rests on the
   approval, you should see `Runs paused: none. Runs stopped: none. Sign-offs revoked: none.`, or the ids
   of what was hit. Leave this until the end of section 10.

### On the command line

`ds` lists approvals and can revoke one. It can neither ask for one nor grant one: people do. The steps
below show the `ds` commands, and what the stand-in people's requests answered in between.

1. Alice uploaded the copy of section 7. The answer names the spec's number and what its runs need
   approved:
   ```text
   {"spec_id": 3, "name": "customers-cutover", "version": "1.0.0", "content_hash": "8831bbe3b9c475c3562ff6480fe50b390a96d0dcd29a771caf7ff9d6930d820a", "needs": ["clear", "population_filters"], "approval_id": null}
   ```
2. Before the approval, Omar's run of the new spec was refused. The request of section 2, step 7,
   answered:
   ```text
   {"detail": "spec 3 (customers-cutover 1.0.0) needs a four-eyes approval of clear, population_filters before it runs"}
   ```
3. Alice asked for the approval, with a note. The answer:
   ```text
   {"approval_id": "A-35T7FVARQ1D1TRY6963W1ZJB0P", "status": "requested", "scopes": ["clear", "population_filters"], "approvers_needed": 1}
   ```
4. List what is waiting.
   ```bash
   ds approvals --status requested
   ```
   You should see:
   ```text
   A-35T7FVARQ1D1TRY6963W1ZJB0P  spec  requested  spec 3 (customers-cutover 1.0.0)  clear, population_filters  requested by user:U-d9c2b2dd728a6902 (alice) 2026-10-05T20:46:52+00:00
   ```
5. Amir rejected that request with the note `Say why the closed accounts are left out`. Alice asked
   again, with the answer in her note, and Amir approved the second request. List everything, newest
   first.
   ```bash
   ds approvals
   ```
   You should see:
   ```text
   A-35T7FW8NM2TB647MT0NRRAK2KA  spec  approved  spec 3 (customers-cutover 1.0.0)  clear, population_filters  requested by user:U-d9c2b2dd728a6902 (alice) 2026-10-05T20:47:08+00:00  decided by user:U-4cd86241d6e37476 (amir) 2026-10-05T20:47:24+00:00
   A-35T7FVARQ1D1TRY6963W1ZJB0P  spec  rejected  spec 3 (customers-cutover 1.0.0)  clear, population_filters  requested by user:U-d9c2b2dd728a6902 (alice) 2026-10-05T20:46:52+00:00  decided by user:U-4cd86241d6e37476 (amir) 2026-10-05T20:47:03+00:00
   ```
6. Ask which live runs use a version that lacks its approval.
   ```bash
   ds approvals --needed
   ```
   You should see:
   ```text
   every live run's spec version has the approval it needs
   ```
7. Revoke an approval from the host. This is the emergency way, for an administrator of the machine; the
   reason is kept for ever. `<approval id>` is the approved id of step 5. Leave this until the end of
   section 10: here it was run last of all.
   ```bash
   ds approvals revoke <approval id> --reason "the filter on side a is under review"
   ```
   You should see:
   ```text
   revoked A-35T7FW8NM2TB647MT0NRRAK2KA; runs paused: -; runs stopped: -; sign-offs revoked: -
   ```

### How to read the result

- **The approval covers the whole content**, named by its content hash. One changed character is a new
  version, which needs a new approval. Nothing expires.
- **Who may not approve**: whoever asked, and everyone who uploaded a version of that spec name since its
  last approved version. A colleague who only bumps your version number does not free you.
- **Two approvers** are needed when an author cannot be named: an upload through a token or `ds`. The
  practice spec of section 3, uploaded and requested through the token, needed two: the request's answer
  held `"approvers_needed": 2`. Amir took step 1. His second try answered `you approved step 1 of A-...;
  another rule_approver must approve it`. Bea took step 2. Omar then started a run of it, after the table
  `crm.profiles` was made again (section 7, "Start clean", step 3), and `ds runs` labelled that run `Spec
  approval A-35T7GF0926Y5D88GSR1FBT7X4D was granted by amir on 2026-10-05 and bea on 2026-10-05.`
- **Rejecting needs a note.** Rejecting and revoking need no fresh sign-in; approving does. With a sign-in
  15 minutes and 15 seconds old, Amir rejected one request and revoked one approval, and his approval of a
  third was refused until he signed in again.
- **Revoking a spec approval** pauses its running runs, stops its queued one-shot runs and revokes the
  sign-offs that rest on it. A new run then answers `spec 3 (customers-cutover 1.0.0) needs a new
  four-eyes approval: A-... was revoked`, and `ds runs` labels the old ones `Spec approval A-... was revoked
  on 2026-10-05 by local:<you>@deltasleuth-app (the filter on side a is under review). Not sign-off
  evidence.` When Amir revoked the approval of another spec whose cycle held an approved sign-off, the
  answer named that sign-off: `{"approval_id": "A-35T7HHX6J381ASQBYNMV7GG1WF", "status": "revoked",
  "paused": [], "stopped": [], "revoked": ["A-35T7HJVD9XRG6ZYDETJY89MDP4"]}`.

### Limits

- Approvals exist under single sign-on only. With a token or in developer mode, runs go ahead labelled
  unapproved and are never signed off.
- A token can ask for an approval, never decide one.
- `ds approvals --needed` has no screen. The screen's list filters by status only; `ds approvals` also
  takes `--kind spec` or `--kind signoff`.
- With no approval stored, `ds approvals` prints nothing at all, not even a line that says so.

### If something goes wrong

**`{"detail": "approvals need SSO (auth.oidc); this deployment runs in tokens mode"}`**
The installation has no single sign-on. See section 7.

**`{"detail": "spec 3 (customers-cutover 1.0.0) needs a four-eyes approval of clear, population_filters
before it runs"}`**
Nobody approved this spec version yet. Under single sign-on this also meets the token of chapter 1 and the
specs of sections 2 and 3. Ask for the approval and have it decided.

**`{"detail": "needs the rule_approver role"}`**
The spec author tried to approve her own request. On the screen "Approve" is grey, with the same words.

**`{"detail": "note is required to reject"}`**
Type a note first.

**`{"detail": "reauth_required"}`**, on the screen `An approval needs a sign-in in the last 15 minutes.
Sign in again, then approve.`
Met with a sign-in that was 15 minutes and 25 seconds old. Sign in again and approve.

**`{"detail": "you approved step 1 of A-...; another rule_approver must approve it"}`**
The request needs two approvers and you were the first. A second rule approver decides it.

**`{"detail": "approval A-... is already approved"}`**
Someone decided it meanwhile. Reload the review.

**`{"detail": "approval A-... is requested, not approved"}`**
Only an approved request is revoked. Reject this one with a note.

**`{"detail": "the reason holds a key-shaped value: write it without customer keys"}`**
A revoke reason held a customer number. Reasons are kept for ever and go into evidence packs: write them
without keys and without personal data.

**`{"detail": "reason must be 1 to 200 characters without control characters"}`**
The reason was empty.

**`error: approval A-... is revoked, not approved`**
`ds approvals revoke` was run a second time.

## 9. Signing off a run's cycle

### What it is for

A sign-off turns one ended cycle into evidence of parity that a person stands behind. An operator asks for
it. DeltaSleuth refuses the request, naming each problem, unless the cycle is complete and ran under an
approved spec. A rule approver who had no part in the work then reviews the cycle's facts and approves.

### Before you start

Single sign-on, an approved spec version (section 8), and a batch or soak run that a signed-in operator
started after the approval. Before you ask: upload the labels (section 4) and score the reviewer sheet
(section 5). The request fixes them, and afterwards the cycle takes no more.

### In the web UI

1. As the operator, open the run and scroll to the heading "Sign-off". You should see the sentence `A
   sign-off accepts one ended cycle as parity evidence, once someone who neither wrote its spec, started
   this run nor asked for it approved its facts.` and a table with the columns Cycle, Requests and
   Sign-off. The cycle reads `Not signed off.` and has the button "Request sign-off".
2. Press "Request sign-off". A box asks `Ask for a sign-off of cycle 1. A note for the approver
   (optional):`. Confirm. The review opens. If the request is refused, the reason appears in red under
   the table instead.
3. As the approver, open the review from "Approvals". It shows:
   - a summary: `The sign-off of cycle 1 of run R-... (customers-cutover 1.0.0 (spec #3)): its facts, hash
     ...`;
   - the panel "Decision", with the sentence `Approving decides each rule and root cause listed below.`;
   - "Checklist": a table with the columns Check, Attested and Note. Its rows are "A live run made under
     approvals", "Ended, with its own counters", "Complete", "Scanned to the end", "Counters reconcile",
     "Uncut", "No unexplained CRITICAL", "Approved content", "Key source" and "Coverage". The last two say
     `Your judgement.`;
   - "Census and adjudication": the census line of section 5, and `Adjudication J-...: 5 adjudicated, 5
     agreed with the engine: did not pass.`;
   - "Labelled accuracy (observed)": the lines of the labelling the sign-off attests;
   - "Counters of cycle 1";
   - "Who may not approve it": here `omar` and `alice`;
   - "Known-diff rules", under `Approving accepts each rule and the keys it suppressed.`;
   - "Root causes", under `Approving accepts each unsuppressed discrepancy class; every claim is
     observed.`: a table with the columns "Root cause", Severity, Taxonomy, Change, Path and Keys.
4. Type a note and press "Approve". The status becomes `approved`. To send the request back instead,
   type a note and press "Reject": the cycle is then open again for a new labelling, a new reviewer sheet
   and a new request.
5. Back on the Run screen, the cycle's row reads `Sign-off A-... was granted by amir on 2026-10-05.`, and
   the header reads `Evidence held until released (retention standard): held by user:U-... since
   2026-10-05, signed off: A-...`.

### On the command line

`ds` lists sign-offs. The steps below show the `ds` commands, and what the stand-in people's requests
answered in between.

1. Omar started a run of the approved spec. It found the six planted differences.
   ```bash
   ds runs
   ```
   You should see, first in the list:
   ```text
   R-35T7FXSVCH5FCKAZ61339GRV9M  customers-cutover 1.0.0  batch  completed  cycle 1  different 6 of 991
     Spec approval A-35T7FW8NM2TB647MT0NRRAK2KA was granted by amir on 2026-10-05.
   ```
2. His sign-off request for that cycle was refused:
   ```text
   {"detail": "cycle 1 of run R-35T7FXSVCH5FCKAZ61339GRV9M has 2 key(s) with unexplained CRITICAL findings (bug rules count as unexplained)"}
   ```
   The two keys are the customer missing on the platform and the user missing in the CRM.
3. The two records were corrected on side b.
   ```bash
   docker exec -i deltasleuth-practice-db psql -U postgres -d ds_demo_platform -v ON_ERROR_STOP=1 -f - <<'SQL'
   INSERT INTO public.users (user_id, email, name, phone, status, country, balance, created_at)
   VALUES (100011, 'customer11@example.com', 'Customer 11', '+447700900011', 'active', 'GB', 13.75,
           timestamptz '2020-01-01 00:00:00+00' + 11 * interval '1 day 37 minutes');
   DELETE FROM public.users WHERE user_id = 101001;
   SQL
   ```
   You should see `INSERT 0 1` and `DELETE 1`.
4. Omar started a second run. It found four differences, none of them critical. `ds runs` listed it
   first:
   ```text
   R-35T7FZ13CM2294HMFXZZNSX1T8  customers-cutover 1.0.0  batch  completed  cycle 1  different 4 of 990
     Spec approval A-35T7FW8NM2TB647MT0NRRAK2KA was granted by amir on 2026-10-05.
   ```
5. He drew and scored a reviewer sheet of that cycle (section 5), then drew, labelled and uploaded a
   labelling (section 4). Its first line now names a person: `Labelling V-35T7G158K6HNDFPF36JCV7CHF0 drawn
   2026-10-05 by omar, labelled 2026-10-05 by omar against REF.customer_master as of 2026-10-05; no
   sign-off attests it.` Its second line is a warning, because the same person started the run: `Warning:
   the labeller started run R-35T7FZ13CM2294HMFXZZNSX1T8: the labels are not independent of the run.`
6. Before asking, he ran the same checks a request runs.
   ```bash
   ds readiness pair customers-cutover --precheck
   ```
   Its last lines:
   ```text
   precheck of cycle 1 of run R-35T7FZ13CM2294HMFXZZNSX1T8 (computed now; the sign-off decides):
     nothing refuses its sign-off
   ```
7. His sign-off request was accepted:
   ```text
   {"approval_id": "A-35T7G1RA9RTXRDQC16WAZVD952", "status": "requested", "attests_sha256": "6f8c95e7750181ca4a36b0b6dab01fbeb2895360741a379a5323ab6b9484d2e9", "approvers_needed": 1}
   ```
8. List the sign-offs.
   ```bash
   ds approvals --kind signoff
   ```
   You should see:
   ```text
   A-35T7G1RA9RTXRDQC16WAZVD952  signoff  requested  spec 3 (customers-cutover 1.0.0)  cycle 1 of run R-35T7FZ13CM2294HMFXZZNSX1T8  requested by user:U-93f7be9d5a7410e1 (omar) 2026-10-05T20:48:40+00:00
   ```
9. Amir's review named who may not approve, `omar` and `alice`, and said that he could. He approved, with
   the note `Four residual differences accepted; see the labelling`. The run was then held. Print the hold:
   ```bash
   dsapi http://localhost:8000/v1/runs/<run id> | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["retention"]["hold"]))'
   ```
   You should see:
   ```text
   {"reason": "signed off: A-35T7G1RA9RTXRDQC16WAZVD952", "by": "user:U-4cd86241d6e37476", "at": "2026-10-05T20:48:54.082639+00:00"}
   ```

### How to read the result

- **What the request checks**: the run is a live batch or soak run started under single sign-on; the cycle
  has ended, is complete, was not cut short, and its counters add up; no key has an unexplained CRITICAL
  finding; and the cycle ran under a spec approval that still stands.
- **What it fixes**: a hash of the cycle's facts, the `attests_sha256` of step 7. The facts are the
  counters, each known-difference rule with the keys it hid, each root cause, the census, the reviewer
  sheet and the labelling. A cycle whose facts changed since the request cannot be approved: ask again.
- **What approving means**: the approver accepted each root cause in the list. Here Amir accepted four
  open differences, one of them of severity HIGH, with the labelling beside them that said which side the
  truth favoured. That is a decision of a person. DeltaSleuth records it and does not judge it.
- **Who may not approve**: the authors of the spec, whoever started the run, whoever asked, whoever scored
  the reviewer sheet and whoever uploaded the labels. A run that a token, `ds` or a schedule started needs
  two approvers.
- **The hold.** The last approval step puts a hold on the run, so the evidence the sign-off rests on is not
  deleted when its retention time ends.
- **Readiness is not asked.** After this sign-off the pair still read `not ready`, with the word
  `regressed` (section 10 shows the lines). That is readiness speaking, not the sign-off. See section 6.
- **Requested, approved, rejected, revoked.** A requested sign-off is approved or rejected. Only an
  approved one can be revoked. Rejecting and revoking both open the cycle again: it takes a new labelling,
  a new reviewer sheet and a new request.

### Limits

- A sign-off is of one cycle. A one-shot compare, a run from captured samples and a run started outside
  single sign-on are never signed off.
- Only people decide. `ds` can neither ask nor approve.
- The precheck of step 6 runs the sign-off's checks. Of what an evidence pack needs on top (section 10),
  the readiness document says it lists one thing, once packs are switched on: a spec whose key pattern
  cannot tell a key. Not verified here.

### If something goes wrong

**`{"detail": "cycle 1 of run R-... has 2 key(s) with unexplained CRITICAL findings (bug rules count as
unexplained)"}`**
Correct the data and run again, or explain the findings with a known-difference rule in a new spec
version, which needs its own approval.

**`{"detail": "run R-... started in tokens mode: only a run started under SSO is signed off; ...; cycle 1
of run R-... ran without a spec approval"}`**
The run is from before single sign-on. Start a new one as a signed-in operator. Met here with the run of
section 2, whose answer also named its two CRITICAL keys.

**`{"detail": "cycle 1 of run R-... already has an open sign-off request A-..."}`**
One request per cycle is open at a time.

**`{"detail": "cycle 1 of run R-... has a sign-off (A-...): revoke it before a new labelling"}`**
The request fixed the cycle's labelling and reviewer sheet. The message says "revoke" whether the sign-off
is approved or only requested, and you meet it most often while it is only requested. Then revoking does
not work: Amir's revoke answered `{"detail": "approval A-... is requested, not approved"}`, and the review
shows no "Revoke" panel. What to do depends on the status, which `ds approvals --kind signoff` prints:

- `requested`: a rule approver rejects the request, with a note. Here Amir rejected with the note `A new
  labelling is wanted first`, and Omar's next draw of a labelling was accepted.
- `approved`: a rule approver revokes the sign-off, with a reason (section 10, step 11). That also
  revokes its evidence pack.

Either opens the cycle again. Ask for the sign-off anew when the new labels are in.

**`{"detail": "needs the rule_approver role"}`**
The operator tried to approve his own request.

## 10. Evidence packs

### What it is for

An evidence pack is one signed ZIP file for one signed-off cycle. An auditor can check it years later,
without DeltaSleuth's database, with OpenSSL and one entry in the organisation's key register: that this
installation issued the files unchanged, who decided what, and over which population parity was observed.
An operator issues it; auditors and cut-over boards receive it.

### Before you start

Single sign-on, a signed-off cycle whose side a was counted (section 5), and a signing key. The key is a
file, like every secret of chapter 1.

1. Create the signing key. It appears on no screen.
   ```bash
   (umask 077; openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out deploy/secrets/pack_signing_key)
   ```
   It prints nothing.
2. Let the containers read it.
   ```bash
   chmod 444 deploy/secrets/pack_signing_key
   ```
3. In `deploy/docker-compose.site.yml`, add the name `pack_signing_key` to the app's `secrets:` list, after
   the names that are there. After chapter 6 and section 7 the line then reads:
   ```yaml
       secrets: [crm_legacy_dsn, platform_users_dsn, oidc_client_secret, pack_signing_key]
   ```
   And add one line at the very end of the file, under the other lines of the block `secrets:`:
   ```yaml
     pack_signing_key: {file: ./secrets/pack_signing_key}
   ```
4. Add the `packs` block to the end of the settings file. `installation` is the name your key register
   will list for this installation: 1 to 63 lower-case letters, digits, `.` and `-`.
   ```bash
   cat >> deploy/config/deltasleuth.yaml <<'EOF'
   packs:
     installation: guide-1
     signing_key_ref: file:/run/secrets/pack_signing_key
   EOF
   ```
5. Apply both changes. The site file changed, so `restart` is not enough.
   ```bash
   docker compose --profile mocks up -d
   ```
   You should see Docker's lines end with ` Container deltasleuth-app-1  Started`.
6. Write the key's register entry. With the `ds` of chapter 1 the folder is inside the container, in its
   volume for files.
   ```bash
   ds packs key --out /var/lib/deltasleuth/p0/pack-key
   ```
   You should see:
   ```text
   wrote fingerprint.txt and procedure.txt to /var/lib/deltasleuth/p0/pack-key: file both in the key register, dated (installation guide-1, fingerprint 60c58d19f5fcbe57f58a9292d6169ecdd620228fad4f2dc7e27ee91dcac35fb8)
   ```
7. Copy the two files out, into `.deltasleuth/pack-key`. Type the `/.` at the end of the first path: it
   copies what is in the folder. Without it, a second run of this command puts a folder `pack-key` inside
   the first one and leaves the old files where they were.
   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/pack-key/. .deltasleuth/pack-key
   ```
   You should see a line that ends `Copied`.
8. Look at the entry.
   ```bash
   cat .deltasleuth/pack-key/fingerprint.txt
   ```
   You should see two lines, with your own fingerprint, written `<fingerprint>` below:
   ```text
   installation: guide-1
   fingerprint: 60c58d19f5fcbe57f58a9292d6169ecdd620228fad4f2dc7e27ee91dcac35fb8
   ```
9. File the two files of `.deltasleuth/pack-key` in your key register, with the date, before the first
   pack is issued. `procedure.txt` is the verification procedure. The register is the auditor's only
   anchor: they compare every pack with the register's copies, never with what the pack says of itself.

The edit of step 3 was made here by a script that wrote those lines. Keep one key per installation, and
never copy a production key to a test installation: its packs would pass for production's.

Everything this section writes on your machine goes into `.deltasleuth`, which git ignores: the register
entry, the pack, the revocation list and the unzipped pack. Delete them there when you are done.

### In the web UI

1. As the operator, signed in as a person, open the signed-off run and scroll to "Census, adjudication and
   evidence packs". The cycle's panel now has the button "Issue evidence pack".
2. Press it. A box asks `Issue the evidence pack of cycle 1, signed with this installation's key?`.
   Confirm. You should see `Issued P-..., signed by the key with fingerprint`, then the fingerprint of
   step 8 in full.
3. The table "Evidence packs" has a row with the columns Pack, Status, Issued, Key and Revoked. Status reads
   `issued`. Press "Download" in that row. The browser saves `P-....zip`. Any viewer can download.
4. A pack of a revoked sign-off shows the status `revoked` and, under "Revoked", who revoked it, when and
   why. It can still be downloaded.

No screen lists the packs of all runs, shows a pack's contents, checks a pack or hands out the revocation
list. Checking is done away from the installation on purpose.

### On the command line

1. Omar issued the pack of the cycle signed off in section 9. The answer:
   ```text
   {"pack_id": "P-35T7G61C0HCMWPERYK4WQNX0EJ", "sums_sha256": "13bab9f4307f3d998f5ae9ed9cc4a34271143ed08a9dd02d758d7cd78b61adce", "key_fingerprint": "60c58d19f5fcbe57f58a9292d6169ecdd620228fad4f2dc7e27ee91dcac35fb8"}
   ```
2. List the packs, newest first.
   ```bash
   ds packs
   ```
   You should see:
   ```text
   P-35T7G61C0HCMWPERYK4WQNX0EJ  issued  cycle 1 of run R-35T7FZ13CM2294HMFXZZNSX1T8  sign-off A-35T7G1RA9RTXRDQC16WAZVD952  issued by user:U-93f7be9d5a7410e1 (omar) 2026-10-05T20:49:52+00:00  key 60c58d19f5fcbe57f58a9292d6169ecdd620228fad4f2dc7e27ee91dcac35fb8  sums 13bab9f4307f3d998f5ae9ed9cc4a34271143ed08a9dd02d758d7cd78b61adce
   ```
3. Download the pack with the token. `<pack id>` is the id of step 2. Each download is recorded.
   ```bash
   dsapi -o .deltasleuth/<pack id>.zip http://localhost:8000/v1/packs/<pack id>/download
   ```
   It prints nothing. The file was 30,728 bytes here, the same bytes as a viewer's download.

   `ds` downloads too. With the `ds` of chapter 1 it writes inside the container, so a second command
   copies the file out. The two commands gave the same bytes as the one above:
   ```bash
   ds packs download <pack id> --out /var/lib/deltasleuth/p0/packs
   ```
   You should see:
   ```text
   wrote /var/lib/deltasleuth/p0/packs/P-35T7G61C0HCMWPERYK4WQNX0EJ.zip: check it with 'ds packs verify P-35T7G61C0HCMWPERYK4WQNX0EJ.zip --fingerprint <the register's>'
   ```
   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/packs/<pack id>.zip .deltasleuth/<pack id>.zip
   ```
4. Look inside.
   ```bash
   unzip -l .deltasleuth/<pack id>.zip
   ```
   You should see twenty files in one folder:
   ```text
     Length      Date    Time    Name
   ---------  ---------- -----   ----
        1440  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/SHA256SUMS
          72  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/SHA256SUMS.sig
        4899  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/VERIFY.txt
        1504  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/accuracy/attempts.json
         567  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/accuracy/labels.csv
        5015  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/accuracy/score.json
         808  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/adjudication/attempts.json
         647  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/adjudication/score.json
        1215  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/adjudication/sheet.csv
          61  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/annex/ai_answers.json
        9218  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/audit/chain.tsv
       15773  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/certificate.pdf
        1829  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/csv/coverage.csv
          47  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/csv/graph.csv
         563  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/csv/root_causes.csv
        4797  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/facts/attests.json
        1335  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/facts/plan.json
        1374  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/facts/spec.json
        9101  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/manifest.json
         178  10-05-2026 20:49   P-35T7G61C0HCMWPERYK4WQNX0EJ/signing-key.pem
   ---------                     -------
       60443                     20 files
   ```
5. The check runs without the installation: it needs a copy of the source with its Python part
   installed, and no settings file, no database and no sign-in. It needs the tool uv
   ([chapter 2](02-run-from-source.md), section 1, step 2). Install the Python part once, in the folder
   `IntelliDiff` (chapter 2, section 3). On a machine of the auditor's, this folder is a fresh copy of the
   source.
   ```bash
   uv sync
   ```
   You should see several lines, the last ones naming installed packages, such as ` + zstandard==0.25.0`.
6. Check the pack. `<fingerprint>` is the one your key register lists (step 8 of "Before you start"),
   never the one the pack names.
   ```bash
   uv run ds packs verify .deltasleuth/<pack id>.zip --fingerprint <fingerprint>
   ```
   You should see one line a step, and status 0:
   ```text
   step 0 procedure: ok
   step 1 key: ok (fingerprint 60c58d19f5fcbe57f58a9292d6169ecdd620228fad4f2dc7e27ee91dcac35fb8)
   step 2 signature: ok
   step 3 files: ok (17 listed file(s) unchanged, none added)
   step 4 subject: run R-35T7FZ13CM2294HMFXZZNSX1T8 cycle 1; basis census; spec customers-cutover 1.0.0 (content hash 8831bbe3b9c475c3562ff6480fe50b390a96d0dcd29a771caf7ff9d6930d820a); side a legacy crm-legacy crm.customers; side b new platform-users public.users: match it with your change record
   step 5 decisions: ok (sign-off A-35T7G1RA9RTXRDQC16WAZVD952 and spec approval A-35T7FW8NM2TB647MT0NRRAK2KA decided)
   step 5 labelled accuracy: ok (row 62 is labelling V-35T7G158K6HNDFPF36JCV7CHF0's accuracy.label row and holds the SHA-256 of accuracy/labels.csv and accuracy/score.json, as facts/attests.json does)
   step 6 audit rows: ok (13 row(s) consistent)
   step 7 SIEM: compare with your SIEM's deltasleuth.audit lines up to row 72 of 3, A-35T7FW8NM2TB647MT0NRRAK2KA, ...
   step 8 revocation: not checked: pass --revocations DIR
   ```
   This was typed in the installation's own folder, on the file of step 3. It read nothing of the
   installation. Not verified here: the same on a second machine.
7. Write the signed revocation list, the list of packs that are no longer valid.
   ```bash
   ds packs revocations --out /var/lib/deltasleuth/p0/revocations
   ```
   You should see:
   ```text
   wrote revoked.txt (0 revoked pack(s)), revoked.txt.sig and signing-key.pem to /var/lib/deltasleuth/p0/revocations
   ```
8. Copy the list out. As in step 7 of "Before you start", type the `/.`: you will run this command again
   in step 12, and without it the second copy lands in a folder inside the first and the old list stays.
   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/revocations/. .deltasleuth/revocations
   ```
   The same three files come as one ZIP from the API: `dsapi -o .deltasleuth/revocations.zip
   http://localhost:8000/v1/packs/revocations`.
9. Check the pack again, now with the list.
   ```bash
   uv run ds packs verify .deltasleuth/<pack id>.zip --fingerprint <fingerprint> --revocations .deltasleuth/revocations
   ```
   The last line becomes:
   ```text
   step 8 revocation: ok (not listed as of 2026-10-05T20:50:33Z)
   ```
   The time is when the list was written. Compare it with the time you ran step 7: an older time means
   the check read an older list.
10. Without DeltaSleuth at all, an auditor runs the filed procedure by hand, in `sh`, with OpenSSL. Its
    first four steps were run here. Unzip the pack:
    ```bash
    unzip -q -d .deltasleuth .deltasleuth/<pack id>.zip
    ```
    Put the register's copy of the procedure beside the pack's folder, under the name the procedure
    expects:
    ```bash
    cp .deltasleuth/pack-key/procedure.txt .deltasleuth/FILED-PROCEDURE.txt
    ```
    Go into the pack's folder:
    ```bash
    cd .deltasleuth/<pack id>
    ```
    The three commands print nothing. Now the procedure's steps. Its step 0 compares the pack's own
    procedure text with the filed one:
    ```bash
    sed -n '3,$p' VERIFY.txt | diff - ../FILED-PROCEDURE.txt
    ```
    It prints nothing: the two are the same. Step 1 prints the fingerprint of the pack's key, to compare
    with the register's:
    ```bash
    openssl pkey -pubin -in signing-key.pem -outform DER | sha256sum
    ```
    ```text
    60c58d19f5fcbe57f58a9292d6169ecdd620228fad4f2dc7e27ee91dcac35fb8  -
    ```
    Step 2 checks the signature of the list of hashes:
    ```bash
    openssl dgst -sha256 -verify signing-key.pem -signature SHA256SUMS.sig SHA256SUMS
    ```
    ```text
    Verified OK
    ```
    Step 3 checks every file against that list:
    ```bash
    sha256sum -c SHA256SUMS
    ```
    It prints one line a file, seventeen here, each ending `OK`, from `VERIFY.txt: OK` to `manifest.json:
    OK`. Where `sha256sum` is missing, the procedure's second line says how to define it from `shasum -a
    256`; that definition gave the same answers here. Go back to the folder `IntelliDiff`:
    ```bash
    cd ../..
    ```
11. A pack is revoked only by revoking its sign-off. Amir revoked the sign-off on its review, with the
    reason `the balance difference was accepted by mistake`. The answer:
    ```text
    {"approval_id": "A-35T7G1RA9RTXRDQC16WAZVD952", "status": "revoked", "paused": [], "stopped": [], "revoked": []}
    ```
    `ds packs` then showed the pack `revoked`, ending `revoked by user:U-4cd86241d6e37476 (amir)
    2026-10-05T20:51:10+00:00: 'the balance difference was accepted by mistake'`. The hold on the run was
    gone: the command of section 9, step 9, printed `null`. Readiness read `signoff  not met: sign-off
    A-35T7G1RA9RTXRDQC16WAZVD952 was revoked on 2026-10-05 (see Approvals)`.
12. Write the revocation list again and copy it out again. These are the commands of steps 7 and 8.
    ```bash
    ds packs revocations --out /var/lib/deltasleuth/p0/revocations
    ```
    You should see one pack counted now:
    ```text
    wrote revoked.txt (1 revoked pack(s)), revoked.txt.sig and signing-key.pem to /var/lib/deltasleuth/p0/revocations
    ```
    ```bash
    docker compose cp app:/var/lib/deltasleuth/p0/revocations/. .deltasleuth/revocations
    ```
13. Read the list you copied.
    ```bash
    cat .deltasleuth/revocations/revoked.txt
    ```
    You should see a later time in the first line than the one of step 9, and the pack:
    ```text
    # DeltaSleuth pack revocations, key 60c58d19f5fcbe57f58a9292d6169ecdd620228fad4f2dc7e27ee91dcac35fb8, as of 2026-10-05T20:51:23Z
    P-35T7G61C0HCMWPERYK4WQNX0EJ 13bab9f4307f3d998f5ae9ed9cc4a34271143ed08a9dd02d758d7cd78b61adce 2026-10-05T20:51:10Z 60c58d19f5fcbe57f58a9292d6169ecdd620228fad4f2dc7e27ee91dcac35fb8
    # end
    ```
    If the time is still that of step 9 and no pack is listed, the copy went wrong: see "If something goes
    wrong".
14. Check the pack against the new list. This is the command of step 9.
    ```bash
    uv run ds packs verify .deltasleuth/<pack id>.zip --fingerprint <fingerprint> --revocations .deltasleuth/revocations
    ```
    It now ends with status 1, and its last line names the time of step 13:
    ```text
    step 8 revocation: FAILED (the pack is revoked (list as of 2026-10-05T20:51:23Z))
    ```

To try the check without uv, the `ds` of chapter 1 runs it on the copies inside the container, after the
`ds packs download` of step 3. Typed after step 14, `ds packs verify /var/lib/deltasleuth/p0/packs/<pack
id>.zip --fingerprint <fingerprint> --revocations /var/lib/deltasleuth/p0/revocations` printed the lines of
step 6 for steps 0 to 7 and the line of step 14 for step 8. That shows the check. It is not an auditor's
check, which is made away from the installation.

### How to read the result

**What is inside.**

| File | Holds |
|---|---|
| `certificate.pdf` | The pack as a document: claim, subject, census, results, the classes decided at sign-off, the decisions, the reviewer sheet, labelled accuracy, the audit trail, retention, what the signature proves, and how to verify |
| `manifest.json` | The same facts for programs: who issued it, the key's fingerprint, the decisions with each approver's session and sign-in time |
| `facts/attests.json`, `facts/spec.json`, `facts/plan.json` | The facts the sign-off fixed, the approved spec, and the run's plan |
| `csv/root_causes.csv`, `csv/coverage.csv`, `csv/graph.csv` | The run's aggregate reports, without key tokens |
| `adjudication/` | The attested reviewer sheet with masked keys and no notes, its score, and every draw of the cycle |
| `accuracy/` | The attested labelling's labels without keys, its score, and every labelling of the cycle |
| `audit/chain.tsv` | The audit rows of the run and its decisions, each linked to the one before by a hash |
| `annex/ai_answers.json` | Which AI answers existed for the cycle, marked `AI-generated, not evidence` |
| `VERIFY.txt` | The verification procedure, to be compared with the register's copy |
| `SHA256SUMS`, `SHA256SUMS.sig`, `signing-key.pem` | The list of the files' hashes, its signature, and the public key |

A pack never holds a raw key, a response body, a note or chat text.

**What the certificate says.** Its first lines, for this pack:

```text
DeltaSleuth evidence pack P-35T7G61C0HCMWPERYK4WQNX0EJ
Claim level: observed
Installation guide-1; issued 2026-10-05 20:49 UTC by omar
Run R-35T7FZ13CM2294HMFXZZNSX1T8, cycle 1; spec customers-cutover 1.0.0
THE LABELLER STARTED RUN R-35T7FZ13CM2294HMFXZZNSX1T8: THE LABELS ARE NOT INDEPENDENT OF THE RUN
1 OF 5 SAMPLED DISCREPANCIES ARE UNDECIDABLE, 0 OF THEM NOT REVIEWED
```

The lines in capitals are warnings. Section 5 of the certificate lists the four root causes under
`Approving the sign-off decided each rule and root cause listed here.` Section 6 names each decision: `step
1 of 1 granted by amir (user:U-4cd86241d6e37476, issuer http://127.0.0.1:18463) on 2026-10-05 20:48 UTC`.
The issuer is the identity provider; here it is the stand-in's address. Section 7 says `5 adjudicated, 5
agreed with the engine: did not pass`, which is the small sheet of section 5 of this chapter.

**What the signature proves, and what it does not.** The certificate says it in these words: `The signature
proves that the holder of the key with fingerprint ... issued these files unchanged.` And: `Nothing here
proves the comparison right beyond the recorded facts (adjudication sampled); agreement outside the
compared population, filters, columns and time window, or beyond side a's census as of its times; true
times (the installation's clock); that the key was not stolen or copied, or history not rewritten before
issue by the database owner; that the pack is still valid (check revocation); or a certified claim, which
needs a frozen cut.`

**What readiness said.** Between steps 2 and 11, with the sign-off approved and the pack issued, the pair's
gates read as follows. The sign-off and the pack count, and the pair is still `not ready`, because four
keys differ. Readiness calls that `regressed`.

```bash
ds readiness pair customers-cutover
```

```text
...
customers-cutover 1.0.0 (spec 3)  -  not ready  6 of 8 gates  cycle 1 of R-35T7FZ13CM2294HMFXZZNSX1T8 (2026-10-05)  regressed
regressed: 1.0.0 (spec 3) was signed off (A-35T7G1RA9RTXRDQC16WAZVD952, cycle 1) and the pair is now below signed off
parity  population  met: cycle 1 of run R-35T7FZ13CM2294HMFXZZNSX1T8 observed side a whole (read to its end in 1 snapshot at 2026-10-05 20:47 UTC)
parity  critical  met: cycle 1 of run R-35T7FZ13CM2294HMFXZZNSX1T8 has no key with unexplained CRITICAL findings
parity  targets  not met: cycle 1 of run R-35T7FZ13CM2294HMFXZZNSX1T8 has 4 discrepant or unresolved key(s) of 990 observed whole; its pooled 0.1% target allows 0: 4 over; 0 key(s) suppressed (known differences or noise only)
parity  convergence  not met: 0 of the 3 evidence cycles in a row that 1.0.0 (spec 3) needs meet every per-cycle gate (cycle 1 of run R-35T7FZ13CM2294HMFXZZNSX1T8 failed targets); no live soak of 1.0.0 (spec 3) will add cycles
...
governance  spec_approval  met: cycle 1 of run R-35T7FZ13CM2294HMFXZZNSX1T8 ran under spec approval A-35T7FW8NM2TB647MT0NRRAK2KA, approved
governance  adjudication  not applicable: cycle 1 of run R-35T7FZ13CM2294HMFXZZNSX1T8 has 5 per-key results, under the 50 an adjudication needs
...
evidence  signoff  met: sign-off A-35T7G1RA9RTXRDQC16WAZVD952 of cycle 1 approved on 2026-10-05
evidence  pack  met: pack P-35T7G61C0HCMWPERYK4WQNX0EJ issued 2026-10-05 for sign-off A-35T7G1RA9RTXRDQC16WAZVD952
...
```

After the revoke of step 11 the first line read `not ready  4 of 8 gates`, without `regressed`.

**The steps of the check.** Step 1 compares the key with the register. Steps 2 and 3 show that no file was
changed or added. Step 4 prints the subject, to match with your change record by eye. Step 5 shows who
decided. Step 6 shows that the audit rows hang together. Step 7 is done by hand: compare the rows with the
copy of the audit log that your log system, the SIEM, received as the rows were written. Step 8 checks the
revocation list. A failed step prints `FAILED` with the reason and the command ends with status 1.

### Limits

- Packs exist under single sign-on only, and only a signed-in person issues one. A token cannot.
- The cycle's side a must have been counted. A key list between two services gets no pack.
- From 50 per-key results the sign-off must attest a passed reviewer sheet.
- The stored copy of a pack is deleted when its cycle's evidence is purged. Archive the downloaded file.
- After the signing key is replaced, `--revocations` accepts only a list signed by the pack's own key:
  check step 8 of an older pack by hand.
- Give an auditor a revocation list dated after their decision date.
- Not verified here: a pack of a sampled cycle, a second pack of the same cycle, a replaced key, a
  download after a purge, and steps 4 to 8 of the filed procedure by hand.

### If something goes wrong

**`{"detail": "spec 4's key.pattern matches the empty string: a raw key cannot be told from other text, so
no pack"}`**
Met here with a copy of the tables example without its `key.pattern`.
DeltaSleuth searches every text of a pack for raw keys and needs the pattern to know what a key looks
like. The upload, the approval and the sign-off all pass, and the refusal comes when the pack is asked
for. Validation warns of it from the start: `ds spec validate` of that copy prints `warning:
/key/pattern [key_pattern_empty] key.pattern is missing: a raw key cannot be told from other text, so no
evidence pack is issued for a run of this spec (docs/mvp/PACK.md §2.3); declare a pattern that fits its
keys only, or class keys that are not personal business_code`, and the Specs screen lists the same
warning after "Validate". Act on the warning before the first run, because afterwards the way out is
long: revoke the sign-off, upload a new spec version with a pattern, have it approved, run again, and
sign off again. That is why the tables example has a pattern. Choose a pattern that fits your keys
only: one that also fits ordinary words or short numbers makes the same search refuse the pack for other
texts.

**`step 8 revocation: ok (not listed as of ...)` for a pack you know is revoked**
The check read an old list. The time in the line is when that list was written: compare it with the first
line of `ds packs revocations`' newest `revoked.txt`. Met here by copying the list out a second time
without the `/.` of step 8: Docker then put a folder `revocations` inside `.deltasleuth/revocations`, the
old `revoked.txt` stayed in place, and the check of a revoked pack passed with status 0. Delete the folder
and copy again:
```bash
rm -r .deltasleuth/revocations
```
then the command of step 8. The same happens to `.deltasleuth/pack-key` (step 7 of "Before you start").

**`replace .deltasleuth/P-.../SHA256SUMS? [y]es, [n]o, [A]ll, [N]one, [r]ename:`**
The pack was unzipped before (step 10). Type `A` and Enter to write the files again.

**`{"detail": "issuing a pack needs a signed-in person"}`**
A token asked. A viewer gets `{"detail": "needs the operator role"}`.

**`{"detail": "evidence packs need SSO (auth.oidc); this deployment runs in tokens mode"}`**
See section 7.

**`error: evidence packs are not configured: set packs.signing_key_ref (docs/mvp/PACK.md §3.1)`**
`ds packs key` was run before the `packs` block was added.

**`error: --fingerprint is required: the fingerprint your key register lists for the installation`**
`ds packs verify` never trusts the pack's own key without it.

**`step 1 key: FAILED (signing-key.pem has fingerprint 60c5..., not the register's)`**
The pack was not signed by the key your register lists. Provoked with a fingerprint of 64 zeros. Do not
accept the pack.

**`step 8 revocation: FAILED (the pack is revoked (list as of 2026-10-05T20:51:23Z))`**
The pack's sign-off was revoked. The pack is no longer evidence.

## 11. What an auditor is handed at the end

When a pair is signed off, the hand-over is small:

1. **The pack**, `P-....zip`, one for each signed-off cycle. It holds the certificate, the facts, the
   decisions and the audit rows.
2. **The key register entry**, filed when the key entered service: `fingerprint.txt` and `procedure.txt`
   with their date. The auditor takes these from the register, not from you and not from the pack.
3. **The revocation list**, `revoked.txt`, `revoked.txt.sig` and `signing-key.pem`, written after the
   auditor's decision date.
4. **Access to the SIEM's copy of the audit log**, for step 7 of the check. The line of the action
   `pack.issue` there names the pack and its `sums_sha256`, here `13bab9f4...`.

With these the auditor can establish, without asking the team that ran the migration: which installation
issued the pack; that its files are unchanged; which spec version was compared, on which run and cycle;
that side a was counted, with how many rows and when; how many keys matched and differed; which difference
classes a named approver accepted, when, and after signing in how; who else was barred from approving;
whether reviewers agreed with the engine, and on how many keys; which side the truth favoured on the
differences, by whose labels; and that the pack has not been revoked.

What stays a matter of judgement is written in the pack itself: the claim is observed, on the compared
population, at the times recorded. Whether four open differences were acceptable is the approver's
decision, and the pack shows it as theirs.

## Reference

- [APPROVALS.md](../mvp/APPROVALS.md): spec approvals and sign-offs, who may not approve, every message.
- [SAMPLING.md](../mvp/SAMPLING.md): the frame, the draw, the strata, the judgement, the ledger and closes.
- [ACCURACY.md](../mvp/ACCURACY.md): labelled accuracy, the labels, the figures and what they claim.
- [PACK.md](../mvp/PACK.md): the census, the reviewer sheet, the pack's files, signing and verification.
- [READINESS.md](../mvp/READINESS.md): the gates, the states, the settings block and the reports.
- [RUNBOOK.md](../mvp/RUNBOOK.md): the operator's view of all five, in the sections "Four-eyes approvals
  and sign-off", "Evidence packs", "Sign-off sampling", "Cut-over readiness" and "Labelling against the
  truth".
- [DEPLOY.md](../mvp/DEPLOY.md): single sign-on with each identity provider, the pack signing key, and
  "From a one-machine trial".
- [AUTH.md](../mvp/AUTH.md): the roles, the sign-in modes and the audit log.
- [examples/tables/README.md](../../examples/tables/README.md): the practice data of this chapter.
