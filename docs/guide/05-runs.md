# 5. Runs in depth

A run is one execution of a comparison. DeltaSleuth takes a set of keys, reads each key from both
sides, and stores one result for each key. A key is the identifier of one record, for example a
customer number.

This chapter covers everything about runs: comparing one key, comparing a whole set, pausing and
cancelling, comparing captured files in place of live services, repeating a comparison in cycles,
reading the outcomes, the causes, the view of one key and the reports, and checking the engine's
verdicts against people's.

It uses the two practice pairs of services that [chapter 1](01-install-docker.md) started, the mocks.
The linker pair has 61 keys such as `ORG-000007`, which every screen shows as they are. The customer pair
has 40 keys such as `C00012003`, which every screen shows only as a token (section 11 says why). Each
pair has a comparison spec, the file that names the two services, says how to ask them and what counts
as equal: `mocks/linker/spec.yaml` and `mocks/customer/spec.yaml`. What each key should give is written
down in `mocks/linker/expected.json` and `mocks/customer/expected.json`.

**How this chapter was checked.** Every command was run on 2026-10-05, on macOS with Docker Desktop, in
the order of this chapter, in a fresh installation made by chapter 1's script. The machine already held another installation, so this one was a second
installation and the outputs are printed here with the default names. Run ids (`R-...`), observation
ids (`O-...`), finding ids (`F-...`), tokens (`tok:...`), the numbers of audit records and times differ
on your machine. Outcomes, root cause ids (`RC-...`) and counts should be the same, except where a step
says that they vary. The web UI steps are written from the pages' source in `web/src`; they were not
clicked in a browser. A step that could not be run is marked "Not verified here:".

Each section gives the web UI steps first and the command-line steps second. They are two ways to the
same end and neither needs the other. Where both call the mocks, a note says what to do between them.

Two placeholders appear in the commands. `<run id>` is the id a run prints when it starts, for example
`R-35T7CT44ZW54C0C6CVH5H63JQ1`. `<masked key>` is a key as the screens show it, for example `ORG-000007`
or `tok:identifier:v1:FRQO6URTWXKUCG2KMKKG274GYF`.

## 1. Prepare

Do this once. Every later section builds on it.

### 1.1 Two short forms

1. Go into the installation's folder, `IntelliDiff`, and define the short form of
   [chapter 1](01-install-docker.md), section 5, if this terminal does not have it yet.

   ```bash
   alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
   ```

2. Define a second short form, for calling the API. A few things in this chapter have no `ds` command
   and are done with a request to the app instead. The short form reads your token from its file at each
   call and hands it to curl through a pipe, so the token appears neither on the screen, nor on a command
   line, nor in a variable.

   ```bash
   dsapi() { { printf 'header = "Authorization: Bearer '; tr -d '\n' < .deltasleuth/tokens/first-run; printf '"\n'; } | curl -sS -K - "$@"; }
   ```

   Neither definition prints anything.

3. Check it.

   ```bash
   dsapi http://localhost:8000/v1/auth/me
   ```

   You should see the token's name and its roles:

   ```text
   {"actor": "token:T-64b6250b01a0f50f", "name": "first-run", "display_name": null, "kind": "token", "roles": ["viewer", "operator", "spec_author"], "csrf": null, "mode": "tokens", "idle_minutes": 30, "expires_at": "2027-01-03T20:15:25.472286+00:00"}
   ```

Both short forms last until the terminal closes and work only in this folder. Put an address that holds
a `?` in single quotes, as the commands below do: the shell would otherwise read the `?` itself.

**If yours is a second installation on the machine** (chapter 1 describes one), the alias and every
`docker compose` command of this chapter work unchanged, because the folder's file `.env` names the
project. The address does not follow: in every `dsapi` command, and in the browser, write your own
address in place of `http://localhost:8000`. It is the one the installation script printed after
"Open". Typed as printed here, a request goes to the first installation, which does not know your
token. An installation that does not know a token answers `{"detail": "invalid or expired API token"}`.

### 1.2 The files this chapter uses

`ds` runs in a container, which sees of your folder only `mocks` and `examples`, read-only. It reads
every other file from a storage area of its own, the volume `p0`, which the container shows as
`/var/lib/deltasleuth/p0`. The running app sees the same volume. So each file is prepared in a folder on
your machine and then copied in. The folder is called `p0-out` here, a name git ignores. Steps 1 to 9
print nothing.

1. Create the folder.

   ```bash
   mkdir -p p0-out
   ```

2. Copy the linker pair's spec into it.

   ```bash
   cp mocks/linker/spec.yaml p0-out/linker.yaml
   ```

3. Copy the customer pair's spec.

   ```bash
   cp mocks/customer/spec.yaml p0-out/customer.yaml
   ```

4. Write a list of three keys, one per line.

   ```bash
   printf 'ORG-000007\nORG-000029\nORG-000041\n' > p0-out/three-keys.txt
   ```

5. Make a copy of the linker spec that takes only the first ten keys.

   ```bash
   awk '{ sub(/name: mock-linker-legacy-vs-new/, "name: mock-linker-first10"); print } /^  concurrency: 2$/ { print "  stop: {maxKeys: 10}" }' mocks/linker/spec.yaml > p0-out/linker-first10.yaml
   ```

6. Make a slow copy: one key at a time, half a second apart.

   ```bash
   sed -e 's/name: mock-linker-legacy-vs-new/name: mock-linker-slow/' -e 's/^  concurrency: 2$/  concurrency: 1/' -e 's/^  delay: 10ms$/  delay: 500ms/' mocks/linker/spec.yaml > p0-out/linker-slow.yaml
   ```

7. Make a slow copy that also stops by itself after five seconds.

   ```bash
   awk '{ sub(/name: mock-linker-legacy-vs-new/, "name: mock-linker-5s"); sub(/^  concurrency: 2$/, "  concurrency: 1"); sub(/^  delay: 10ms$/, "  delay: 500ms"); print } /^  concurrency: 1$/ { print "  stop: {maxDuration: 5s}" }' mocks/linker/spec.yaml > p0-out/linker-5s.yaml
   ```

8. Make a copy that repeats every 30 seconds and ends after two rounds.

   ```bash
   awk '{ sub(/name: mock-linker-legacy-vs-new/, "name: mock-linker-soak"); print } /^  concurrency: 2$/ { print "  cycleInterval: 30s"; print "  stop: {maxCycles: 2}" }' mocks/linker/spec.yaml > p0-out/linker-soak.yaml
   ```

9. Make a copy with a mistake built in: it no longer translates side b's words for the kind of an
   entity into side a's.

   ```bash
   sed -e 's/name: mock-linker-legacy-vs-new/name: mock-linker-unmapped-kind/' -e 's/"kind": \$lookup(\$types, entityType),/"kind": entityType,/' mocks/linker/spec.yaml > p0-out/linker-unmapped-kind.yaml
   ```

10. Look at what one of the copies changed.

    ```bash
    diff mocks/linker/spec.yaml p0-out/linker-soak.yaml
    ```

    You should see the new name and the two added lines:

    ```text
    29c29
    <   name: mock-linker-legacy-vs-new
    ---
    >   name: mock-linker-soak
    212a213,214
    >   cycleInterval: 30s
    >   stop: {maxCycles: 2}
    ```

11. Copy the folder's content into the volume.

    ```bash
    docker compose cp p0-out/. app:/var/lib/deltasleuth/p0/
    ```

    You should see:

    ```text
     deltasleuth-app-1 copy p0-out/. to deltasleuth-app-1:/var/lib/deltasleuth/p0/ Copying
     deltasleuth-app-1 copy p0-out/. to deltasleuth-app-1:/var/lib/deltasleuth/p0/ Copied
    ```

12. List the volume.

    ```bash
    docker compose run --rm app ls /var/lib/deltasleuth/p0
    ```

    You should see the eight files:

    ```text
     Container deltasleuth-db-1  Running
    customer.yaml
    linker-5s.yaml
    linker-first10.yaml
    linker-slow.yaml
    linker-soak.yaml
    linker-unmapped-kind.yaml
    linker.yaml
    three-keys.txt
    ```

Each copy has its own name, so its runs do not mix with the linker pair's own:

| File | What it changes | Used in |
|---|---|---|
| `linker.yaml` | Nothing: the linker pair's spec | Sections 2, 3, 5 and 7; its run of section 3 again in sections 9, 10, 12 and 13 |
| `customer.yaml` | Nothing: the customer pair's spec | Sections 7 and 11 |
| `three-keys.txt` | A key list: a mismatch, the drift key and a one-sided key | Section 3 |
| `linker-first10.yaml` | `stop: {maxKeys: 10}` | Section 3 |
| `linker-slow.yaml` | `concurrency: 1` and `delay: 500ms`, so a run takes about 40 seconds | Section 4 |
| `linker-5s.yaml` | The same, and `stop: {maxDuration: 5s}` | Section 3 |
| `linker-soak.yaml` | `cycleInterval: 30s` and `stop: {maxCycles: 2}` | Section 6 |
| `linker-unmapped-kind.yaml` | A mapping mistake that touches every key | Section 8 |

A file you copy in keeps its mode, and the container reads it as another user. If your shell creates
files that only you can read, see "If something goes wrong" in section 3.

### 1.3 Make the mocks forget

The mocks count the calls they get for each key, and two keys answer differently the first time. For
`ORG-000029`, side b gives an out-of-date answer to the first call and the current one after that. For
`ORG-000052`, side b answers with an error once. A second run therefore differs from the first, and
`mocks/linker/expected.json` describes the first. To get first-time answers again, restart the mocks:

```bash
docker compose --profile mocks restart mocks
```

You should see, a second or two later:

```text
 Container deltasleuth-mocks-1  Restarting
 Container deltasleuth-mocks-1  Started
```

Do it now. The sections that need it again have it as a step.

### 1.4 What every `ds` command prints first

A `ds` command prints three kinds of lines before its result. They are normal, and the "You should see"
blocks below leave them out after the first example in section 2.

- Docker's own line, ` Container deltasleuth-db-1  Running`.
- Lines that start with `{"id":`. They are copies of the rows the command wrote to the audit log, the
  record of who did what. One of them, with `"action": "spec.create"`, comes with every command that
  reads a spec file, also when that spec has been stored for a long time. It does not mean that a new
  spec was made.
- For a command that starts a run, a line `warning: spec 1 (mock-linker-legacy-vs-new 1.1.0) needs a
  four-eyes approval of clear; tokens mode has no approvers, so run R-... is labelled unapproved`. The
  mock specs show some values unmasked (`clear`), and the customer spec also sets some differences
  aside (`known_diffs`). Where people sign in through single sign-on, a second person has to approve
  such a spec before it runs. With token sign-in nobody can approve, so the run goes ahead and carries
  the label. The comparison is the same. The label matters only for sign-off, which
  [chapter 11](11-sign-off.md) covers. This line and the line `run R-... started` can come in either
  order.

### 1.5 Store the specs

A run is always a run of a stored spec. `ds run` and `ds compare` store the spec file they are given,
but the web UI can only start a run of a spec that is stored already, and in a fresh installation its
list of specs is empty. Store the seven specs now, so that every section's web UI steps find theirs.

1. Send the seven files to the app, in this order.

   ```bash
   for f in linker customer linker-first10 linker-slow linker-5s linker-soak linker-unmapped-kind; do dsapi -X POST -H 'Content-Type: application/yaml' --data-binary @p0-out/$f.yaml http://localhost:8000/v1/specs; echo; done
   ```

   You should see seven lines, each with the number the spec got (each `...` stands for a long hash):

   ```text
   {"spec_id": 1, "name": "mock-linker-legacy-vs-new", "version": "1.1.0", "content_hash": "9485...", "needs": ["clear"], "approval_id": null}
   {"spec_id": 2, "name": "mock-customer-v1-vs-v2", "version": "1.0.0", "content_hash": "dadc...", "needs": ["clear", "known_diffs"], "approval_id": null}
   {"spec_id": 3, "name": "mock-linker-first10", "version": "1.1.0", "content_hash": "1968...", "needs": ["clear"], "approval_id": null}
   {"spec_id": 4, "name": "mock-linker-slow", "version": "1.1.0", "content_hash": "7912...", "needs": ["clear"], "approval_id": null}
   {"spec_id": 5, "name": "mock-linker-5s", "version": "1.1.0", "content_hash": "016b...", "needs": ["clear"], "approval_id": null}
   {"spec_id": 6, "name": "mock-linker-soak", "version": "1.1.0", "content_hash": "036c...", "needs": ["clear"], "approval_id": null}
   {"spec_id": 7, "name": "mock-linker-unmapped-kind", "version": "1.1.0", "content_hash": "eca4...", "needs": ["clear"], "approval_id": null}
   ```

The web UI does the same, one file at a time: click "Specs" in the bar at the top, choose a file of
`p0-out` under "Upload a spec" (`linker.yaml` first, to get the same numbers), and press "Save". The
line "Saved as spec #1." appears beside the button, and the spec is added to the table above.

**About the numbers.** This chapter names a spec with its number, as the web UI does: `(#1)` to `(#7)`.
Yours are the same if you came here straight from chapter 1 and stored the files in this order. A spec
is stored once: sent again, the same file gets the number it already has. So if an earlier chapter
stored a mock spec, your numbers differ; use the ones your seven lines show. Numbers also skip. Every
command that reads a spec file uses one up, even when it stores nothing: after all the commands of this
chapter, the next new spec got the number 36 here, not 8.

## 2. Comparing one key now

### What it is for

It reads one key from both sides right now and gives a short verdict: the outcome, the fields that
differ with their masked values, and the ids of what was read. If the first read finds a difference,
the key is read once more straight away to confirm it. An operator uses it to check one record, for
example after a fix.

### Before you start

Section 1. Both services must be reachable: this is always a live read, never one from captured files.

The web UI steps also need a run that holds the key. A fresh installation has no run, so step 2 makes
one. The command-line steps need no run.

### In the web UI

The web UI compares a key only if the key is already in a run.

1. Click "Runs" in the bar at the top. The page "Runs" has a panel headed "New run" and, under it, the
   table of runs, newest first. While the installation has no run, the word "None." stands in place of
   the table.
2. Make a run that holds the key. In the panel "New run", pick `mock-linker-legacy-vs-new 1.1.0 (#1)`
   in "Spec"; the list shows the newest spec first, so this one is at its end. Leave "Mode" at `batch`,
   type `ORG-000007` into the box "Keys, one per line (none: the spec's key source; a table pair without
   one scans both tables)", and press "Start". A page headed "Run R-..." opens, and within a few
   seconds the badge beside the heading reads `completed`. If you already have a run that holds the
   key, click its id in the column "Run" of the table instead.
3. In the table under "Results", click the key `ORG-000007` in the column "Key". A page headed
   "Key ORG-000007" opens, with the key's outcome, `MISMATCH`, beside the heading.
4. Press "Compare now". The button reads "Comparing…" while the two sides are read. Then a panel headed
   "Compared now (run R-...)" appears above the stored result, with the new outcome beside its heading
   and the same blocks as the page below it (section 10 describes them). The link "run R-..." in the
   heading opens the key in the new run.

In the panel "Compared now" the fact `pass` reads 1, although the key was read twice and the table
"Observations" in the same panel lists reads of pass 1 and pass 2. Section 10 says how to read it.

The web UI has one form for comparing a key that is in no run: "Save and compare this key", on the
page "New comparison" and on a stored spec's page of the "Specs" screen, for the role `spec_author`
([chapter 4](04-specs.md), section 2). It stores the spec it holds as a version first, and it refuses
a stored spec whose key list is shown as stand-ins. The "New run" panel takes a spec, a mode and keys,
and "Compare now" sends only a key of the run it shows; a batch run with the one key in the box "Keys,
one per line", as in step 2, works for every spec. The command below and the API (`POST /v1/compare`)
take any key.

### On the command line

1. Compare the key `ORG-000007` under the linker spec.

   ```bash
   ds compare ORG-000007 --spec /var/lib/deltasleuth/p0/linker.yaml
   ```

   You should see, here once with the lines section 1.4 describes (each `...` stands for text left out):

   ```text
    Container deltasleuth-db-1  Running
   {"id": 16, "at": "2026-10-05T20:19:24.923994+00:00", "actor": "local:<you>@deltasleuth-app", ... "action": "spec.create", "object": "1", ...}
   {"id": 17, ... "action": "compare_one", "object": "R-35T7CS4E3DRMS4MCN2A8QQ73J5", ...}
   {"id": 18, ... "action": "run.finished", "object": "R-35T7CS4E3DRMS4MCN2A8QQ73J5", ...}
   warning: spec 1 (mock-linker-legacy-vs-new 1.1.0) needs a four-eyes approval of clear; tokens mode has no approvers, so run R-35T7CS4E3DRMS4MCN2A8QQ73J5 is labelled unapproved
   ORG-000007: MISMATCH (DIFFERENT, rechecked)
     HIGH FIELD_MISSING missing_in_b /links/ORG-000080
         A: {"code": "ORG-000080", "flagged": true, "kind": "PERSON", "…
         B: <absent>
   run R-35T7CS4E3DRMS4MCN2A8QQ73J5; observations a O-469b135d96a16b83 b O-3e7c201763997c1e
   ```

   The record with `spec.create` is printed although section 1.5 stored this spec already. It comes
   with every command that reads a spec file (section 1.4).

2. Compare it again and let the command fail when the key is different. This is the form for a script.

   ```bash
   ds compare ORG-000007 --spec /var/lib/deltasleuth/p0/linker.yaml --fail-on-diff
   ```

   You should see the same four result lines.

3. Print the status the command ended with.

   ```bash
   echo $?
   ```

   You should see:

   ```text
   1
   ```

4. Get the result as JSON, here for a key whose difference is a flag.

   ```bash
   ds compare ORG-000023 --spec /var/lib/deltasleuth/p0/linker.yaml --json
   ```

   You should see one finding and the result (shortened):

   ```text
   {
     "findings": [
       {
         "change_type": "value_changed",
         ...
         "json_pointer": "/links/ORG-000104/flagged",
         "key_masked": "ORG-000023",
         ...
         "path_pattern": "$.links[*].flagged",
         "root_cause_id": "RC-e0bcd1375262",
         ...
         "severity": "CRITICAL",
         ...
         "taxonomy_code": "VALUE_MISMATCH",
         "value_a": true,
         "value_b": false,
         "value_class": "other"
       }
     ],
     "key_masked": "ORG-000023",
     "result": {
       ...
       "confirmation": "rechecked",
       ...
       "first_outcome": "MISMATCH",
       ...
       "outcome": "MISMATCH",
       "revision": 2,
       ...
     },
     "run_id": "R-35T7CSD0K5K2NGYSPVJFRQ7P0D"
   }
   ```

5. Compare a key on which the two sides agree.

   ```bash
   ds compare ORG-000001 --spec /var/lib/deltasleuth/p0/linker.yaml
   ```

   You should see:

   ```text
   ORG-000001: MATCH (MATCH, provisional)
   run R-35T7CSH4RH9QX8TGEVQTSRNHNC; observations a O-2f0c6e2e2b6ae29f b O-80d211469242d70a
   ```

### How to read the result

The first line holds four things. `ORG-000007` is the key as every screen shows it; for the customer
pair it is a token. `MISMATCH` is the outcome (section 7 lists them all). `DIFFERENT` is the verdict,
one of three words that every outcome folds into: `MATCH`, `DIFFERENT` or `INDETERMINATE`.
`rechecked` says the key was read a second time and the difference was still there; `provisional`
says it was read once. A key that could not be compared adds `reason` and a word that says why.

Each difference follows as three lines. The first gives the severity (`HIGH`), the kind of difference
(`FIELD_MISSING`), the direction of the change (`missing_in_b`: side a has it, side b does not) and the
place in the record (`/links/ORG-000080`: the link to the entity `ORG-000080`). The lines `A:` and `B:`
give each side's value, masked and cut at 60 characters; `<absent>` means the side has nothing there.

The last line names the run that holds the result. Every compare is stored as a run of its own, of the
mode `one_shot`, and appears in the list of runs. The two `O-...` ids name the reads, one a side.

The exit status is 0, or 1 with `--fail-on-diff` when the verdict is `DIFFERENT`.

### Limits

- It compares under the spec file you name. In the web UI, a key of a run is always compared under
  that run's spec.
- Every compare adds a run to the list of runs. The list has no filter to hide them.
- `--fail-on-diff` reacts to `DIFFERENT` only. A key that could not be compared (`INCONCLUSIVE`) ends
  with status 0.
- A value in the `A:` and `B:` lines is printed in Python's spelling: a JSON `true` shows as `True`.

### If something goes wrong

**`error: / [document_unreadable] cannot read p0-out/linker.yaml: No such file or directory (ds runs in a
container, where this path is /app/p0-out/linker.yaml: a file or folder of the host must first be copied
in, with docker compose cp, or mounted)`**
You gave a path of your machine that the container does not see. Copy the file into the volume (section
1.2, step 11) and give the path under `/var/lib/deltasleuth/p0`.

**`error: 1 key(s) do not match key.pattern after key.normalize, or are over 1024 bytes or hold NUL
(positions 1)`**
The key does not have the form the spec demands, here `ORG-` and six digits. Provoked with the key
`org-7`. The message never repeats a key, because a key can be personal data.

**`Missing option '--spec'.`**
`ds compare` needs the spec file. Add `--spec` and its path.

**`error: set DS_OPERATOR to your user name (docs/mvp/AUTH.md §4.2)`**
The command ran without the alias. Define the alias again (section 1.1).

**`ORG-000001: INCONCLUSIVE (INDETERMINATE, provisional) reason both_error`**
Neither service answered. Provoked by stopping the mocks. Start them:
`docker compose --profile mocks up -d`.

## 3. Batch runs

### What it is for

A batch run compares every key of a set once and stores a result for each. A key whose first read
shows a difference is read a second time after a short wait, so that a record that was just changing is
not reported as different. An operator starts a batch run; anyone who may read results can watch it.

### Before you start

Section 1. Restart the mocks (section 1.3) before the web UI steps, so that the counts come out as
printed; the command-line steps start with the same restart.

Five things decide how a batch run goes. Only the first, which keys, can be given in the web UI, in
the box "Keys, one per line". The other four are lines of the spec or of the installation's settings
files.

| What | Where it is set | The linker spec's value |
|---|---|---|
| Which keys | The keys you give with the run; without any, the spec's `key.source`: a list written in the spec, or an address of a service that lists them | A list of 61 keys in the spec |
| How many keys are in flight at once | `execution.concurrency` in the spec, 1 to 64, 1 when left out | 2 |
| The pause before a worker's next key | `execution.delay` in the spec, plus `execution.jitter` | 10 ms |
| How hard one service may be hit | `rate_per_s` (20 when left out) and `max_concurrency` (4 when left out) of the target in `deploy/config/targets.yaml` | Left out: 20 a second, 4 at once |
| How many keys the installation works on at once | `worker.concurrency` in `deploy/config/deltasleuth.yaml` | 4 |

The smallest limit wins. The second read of a key comes no sooner than `recheck.delay` in the spec, one
second for the mock pairs. It can come much later: in the slow copies of this chapter, which read one
key at a time, the second reads came only after every first read.

**Who does the work.** Keys are processed by a worker, a loop that takes keys that are due, reads both
sides and saves the result. One worker processes keys for one database. The app container holds one,
inside `ds serve`. `ds run` holds one too, but while the app is running `ds run` only creates the run,
says `another worker is processing keys (a ds serve, ds worker or ds run on this database): it does
the work, and this one takes over only if it stops`, and waits for the app's worker to finish. With the
app container stopped, the same `ds run` printed no such line and processed its three keys itself.

### In the web UI

1. Click "Runs". Above the table of runs is a panel headed "New run".
2. In "Spec", pick `mock-linker-legacy-vs-new 1.1.0 (#1)`. The list holds the stored specs, the newest
   first, so the entry it shows at first is another one: after section 1.5 it is
   `mock-linker-unmapped-kind 1.1.0 (#7)`. If the list is empty, do section 1.5 and load the page again.
3. Leave "Mode" at `batch`.
4. Leave the box "Keys, one per line (none: the spec's key source; a table pair without one scans both
   tables)" empty to take the spec's 61 keys, or type keys into it, one per line.
5. Press "Start". The run's own page opens, headed "Run R-..." with a badge that reads `running`. A
   refusal, for example of a key that does not have the form the spec demands, appears in red under
   the button instead.
6. Watch the page: it asks the app again every two seconds until the run ends. Under the buttons a bar
   fills, with a line such as `61/61 keys · done 61`, and under it one badge for each outcome with its
   count: `MATCH 52`, `MISMATCH 4`, `INCONCLUSIVE 3`, `DRIFT 1` and `NOT_FOUND_B 1`. The badge beside
   the heading ends at `completed`.

The states in the line beside the bar, and the outcome badges, come in no fixed order. The order can
differ from run to run and can change while you watch.

The line under the heading reads, for example, `mock-linker-legacy-vs-new 1.1.0 · batch · cycle 1 ·
created ... by first-run (T-...) · finished ...`. Two lines follow it on every run of this guide:
`Evidence retained until 2027-01-03 (retention standard)`, the day until which the run's stored answers
are kept, and `Unapproved spec version: clear was not approved by a second person (tokens mode has no
approvers). Not sign-off evidence.`, which section 1.4 explains. A batch or soak run also has a line
such as `Pair readiness: not ready (1 of 5 gates)`, a link to the pair's readiness, which
[chapter 11](11-sign-off.md) covers.

### On the command line

1. Restart the mocks, so that they answer as if for the first time (section 1.3). Do it also if you
   did so before the web UI steps.

   ```bash
   docker compose --profile mocks restart mocks
   ```

   You should see:

   ```text
    Container deltasleuth-mocks-1  Restarting
    Container deltasleuth-mocks-1  Started
   ```

2. Run the linker pair with the spec's own 61 keys.

   ```bash
   ds run /var/lib/deltasleuth/p0/linker.yaml
   ```

   You should see, after about eight seconds:

   ```text
   run R-35T7CT44ZW54C0C6CVH5H63JQ1 started
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 61, compared 57, match 52, different 5, indeterminate 4, cancelled 0, outstanding 0
     DRIFT 1, INCONCLUSIVE 3, MATCH 52, MISMATCH 4, NOT_FOUND_B 1
   ```

   Note the run id: sections 7, 9, 10, 12 and 13 use this run.

3. Run only the keys of a file. The file holds one key per line; blank lines and lines that start with
   `#` are skipped.

   ```bash
   ds run /var/lib/deltasleuth/p0/linker.yaml --keys /var/lib/deltasleuth/p0/three-keys.txt
   ```

   You should see:

   ```text
   run R-35T7CTHCCV05VC5MYP0BEYD23Q started
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 3, compared 3, match 1, different 2, indeterminate 0, cancelled 0, outstanding 0
     MATCH 1, MISMATCH 1, NOT_FOUND_B 1
   ```

   The key `ORG-000029` was `DRIFT` in step 2 and is `MATCH` here: the mock had already given its
   out-of-date answer (section 1.3).

4. Run the copy that takes only the first ten keys.

   ```bash
   ds run /var/lib/deltasleuth/p0/linker-first10.yaml
   ```

   You should see the stop condition named beside the status:

   ```text
   run R-35T7CTRQP2NT6BTA1CD95Y3NGH started
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed (stop_condition:maxKeys)
     total 10, compared 10, match 9, different 1, indeterminate 0, cancelled 0, outstanding 0
     MATCH 9, MISMATCH 1
   ```

5. Run the slow copy that stops itself after five seconds.

   ```bash
   ds run /var/lib/deltasleuth/p0/linker-5s.yaml
   ```

   You should see another status, and keys that were never read:

   ```text
   run R-35T7CV299NF2P1NTWJJT37CWYD started
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed_with_issues (stop_condition:maxDuration)
     total 61, compared 8, match 8, different 0, indeterminate 0, cancelled 52, outstanding 1
     MATCH 8, MISMATCH 1
   ```

   Your numbers for `compared`, `match` and `cancelled` can differ by a key or two, because the five
   seconds are measured on the clock: six runs here compared 8, 9 or 10 keys and cancelled 52, 51 or
   50. The status, the stop reason, `total 61` and `outstanding 1` were the same every time.

6. Start a run and get the terminal back at once.

   ```bash
   ds run /var/lib/deltasleuth/p0/linker.yaml --keys /var/lib/deltasleuth/p0/three-keys.txt --no-wait
   ```

   You should see:

   ```text
   run R-35T7CVHF23BEAZMYZKYMEDAFQJ started
   processed by 'ds worker' or 'ds serve'
   ```

7. Look at the two newest runs a few seconds later.

   ```bash
   ds runs --limit 2
   ```

   You should see:

   ```text
   R-35T7CVHF23BEAZMYZKYMEDAFQJ  mock-linker-legacy-vs-new 1.1.0  batch  completed  cycle 1  different 2 of 3
     Unapproved spec version: clear was not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   R-35T7CV299NF2P1NTWJJT37CWYD  mock-linker-5s 1.1.0  batch  completed_with_issues  cycle 1  different 0 of 61
     Unapproved spec version: clear was not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   ```

`ds run` ends with status 0. With `--fail-on-diff` it ends with 1 when a key is different or still
owed its second read: run again with that option, the copy of step 5 ended with 1, because of its
`outstanding 1`.

### How to read the result

The first result line is the run's status, with the reason it stopped in brackets when it stopped
early.

| Status | Meaning |
|---|---|
| `queued` | Created and not started. Only a one-key compare is ever in this state |
| `running` | The worker is reading keys |
| `paused` | No new key is started until someone resumes it (section 4) |
| `cancelling` | A cancel or a stop condition was taken; keys in flight are finishing |
| `completed` | Every key got a result. It does not say that the two sides agree |
| `completed_with_issues` | It ended, but keys were cancelled or a difference is still owed its second read |
| `cancelled` | Someone cancelled it |
| `failed` | It could not go on, and the run carries the reason as its error. Not verified here: the runbook's example is a credential file that cannot be read |

The second line holds the counters. They always add up: `total` is `match` plus `different` plus
`indeterminate` plus `cancelled` plus `outstanding`.

| Counter | Meaning |
|---|---|
| `total` | Keys in the run |
| `compared` | Keys both sides answered for in full: `match` plus `different` |
| `match` | Keys whose verdict is `MATCH` |
| `different` | Keys whose verdict is `DIFFERENT` |
| `indeterminate` | Keys that could not be judged: `INCONCLUSIVE` and `DRIFT` |
| `cancelled` | Keys that never got a result because the run stopped first |
| `outstanding` | Keys whose first read showed a difference and whose confirming second read did not happen |

The third line counts the keys by outcome. Section 7 explains each.

**Stop conditions.** A spec can end its runs early under `execution.stop`. The run then says
`stop_condition:` and the name.

| Name | What it does | Seen here |
|---|---|---|
| `maxKeys` | The run takes only the first so many keys of its set. Nothing else is cut short, so it ends `completed` | Step 4 |
| `maxDuration` | Ends the run so long after its start. Keys not started are cancelled and it ends `completed_with_issues` | Step 5 |
| `until` | The same at a fixed time, written as a date and time with its offset, such as `"2026-10-05T20:27:18Z"` | A slow copy with a time 25 seconds ahead ended one second after that time, with `(stop_condition:until)` beside its status |
| `maxUnexplainedCritical` | Ends the run once so many differences of the highest severity exist that no rule explains | With `maxUnexplainedCritical: 1` the linker run ended at its first such difference, with 21 keys compared and 37 cancelled |
| `maxConsecutiveErrors` | Ends the run after so many failed calls in a row | With `maxConsecutiveErrors: 3` and the mocks stopped, the run ended after 3 keys |
| `maxErrorRate`, `maxInconclusiveRate` | End the run when the share of failed calls, or of `INCONCLUSIVE` keys, among the last so many passes a limit. Written as `{rate: 0.5, window: 10}`; the window is at least 10 | With these values and the mocks stopped, the runs ended after 6 and after 11 keys |
| `maxCycles` | Ends a soak run after so many cycles | Section 6 |

Every run that one of the conditions from `maxDuration` to `maxInconclusiveRate` ended had the status
`completed_with_issues`.

A file `retry.csv` lists every key that did not get a final result (section 12). Start a new run with
those keys to finish the job.

### Limits

- The web UI does not show where a run's keys came from, nor how many keys `maxKeys` left out. In step 4
  the run says `total 10`; that the spec listed 61 is recorded with the run, and only the API returns
  it: the answer to `dsapi http://localhost:8000/v1/runs/<run id>` holds
  `"keys_cut": {"taken": 10, "resolved": 61}`.
- Keys are typed or pasted in the web UI. It takes no file.
- A service that refuses the credential (HTTP 401 or 403) pauses the run with the reason
  `run_paused_auth_error`. Not verified here: the mocks ask for no credential.
- A target's limits hold for each process. A `ds compare` from the command line has a budget of its
  own beside the app's.
- `ds serve --no-worker` together with a separate `ds worker` splits the app and the worker into two
  processes. Not verified here: chapter 1's installation runs both in one.

### If something goes wrong

**`error: 2 key(s) do not match key.pattern after key.normalize, or are over 1024 bytes or hold NUL
(positions 2, 4)`**
Keys of the list do not have the form the spec demands. The positions count keys, not lines: blank
lines and comment lines are not counted. Provoked with a file whose bad keys stood on lines 2 and 6.

**`Invalid value for '--keys': Path '/var/lib/deltasleuth/p0/private-keys.txt' is not readable.`**
The file was copied in with a mode that lets only its owner read it, and the container is another
user. The same message names `--spec` for a spec file. Make the folder readable, then copy it again
(section 1.2, step 11):

```bash
chmod -R a+rX p0-out
```

It prints nothing.

**`error: No such file or directory: /var/lib/deltasleuth/p0/none.txt`**
The key file is not in the volume. Copy it in.

**`error: the key set is empty`**
The key file holds no key.

**Every key is `INCONCLUSIVE`, and the run still says `completed`**
The services did not answer. Provoked by stopping the mocks: three keys gave `total 3, compared 0,
match 0, different 0, indeterminate 3`, the run was `completed`, and `--fail-on-diff` ended with
status 0. Read the counters, not the status. Start the mocks with `docker compose --profile mocks up
-d` and run again.

**The counts differ from the ones above**
The mocks remembered earlier calls. Repeat the steps from step 1, which restarts them.

## 4. Pausing, resuming and cancelling

### What it is for

Pausing stops a run from starting new keys without losing what it has; resuming lets it go on;
cancelling ends it for good. An operator uses them when a service under comparison needs relief, or
when a run was started by mistake.

### Before you start

Section 1. A run must last long enough to be caught: the copy `linker-slow.yaml` takes about 40
seconds.

### In the web UI

1. Click "Runs". In the panel "New run", pick `mock-linker-slow 1.1.0 (#4)` in "Spec", leave the rest
   as it is, and press "Start". The run's page opens with the badge `running`.
2. Look at the row of buttons under the lines of text. It starts with the run's controls, whose labels
   are lower-case words, followed by nine buttons for the reports. A running run has "pause" and
   "cancel".
3. Press "pause". The badge reads `paused`, the controls become "resume" and "cancel", and the line
   beside the bar stops moving, for example at `8/61 keys · done 8 · pending 52 · recheck 1`. The three
   states can stand in another order.
4. Press "resume". The badge reads `running` again and the bar moves on.
5. Press "cancel". Nothing asks you to confirm. The badge reads `cancelled`, the line under the heading
   ends with `· stopped: cancelled`, and no control is left.

### On the command line

`ds` has no command that pauses, resumes or cancels a run. The three are requests to the app.

1. Start the slow run without waiting for it.

   ```bash
   ds run /var/lib/deltasleuth/p0/linker-slow.yaml --no-wait
   ```

   You should see:

   ```text
   run R-35T7CW4AZ0N914AQP00VQFFGB4 started
   processed by 'ds worker' or 'ds serve'
   ```

2. Pause it.

   ```bash
   dsapi -X POST http://localhost:8000/v1/runs/<run id>/pause
   ```

   You should see:

   ```text
   {"run_id": "R-35T7CW4AZ0N914AQP00VQFFGB4", "status": "paused"}
   ```

3. Look at it in the list.

   ```bash
   ds runs --limit 1
   ```

   You should see:

   ```text
   R-35T7CW4AZ0N914AQP00VQFFGB4  mock-linker-slow 1.1.0  batch  paused  cycle 1
     Unapproved spec version: clear was not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   ```

4. Resume it.

   ```bash
   dsapi -X POST http://localhost:8000/v1/runs/<run id>/resume
   ```

   You should see:

   ```text
   {"run_id": "R-35T7CW4AZ0N914AQP00VQFFGB4", "status": "running"}
   ```

5. Cancel it.

   ```bash
   dsapi -X POST http://localhost:8000/v1/runs/<run id>/cancel
   ```

   You should see:

   ```text
   {"run_id": "R-35T7CW4AZ0N914AQP00VQFFGB4", "status": "cancelled"}
   ```

6. Look at it in the list again.

   ```bash
   ds runs --limit 1
   ```

   You should see:

   ```text
   R-35T7CW4AZ0N914AQP00VQFFGB4  mock-linker-slow 1.1.0  batch  cancelled  cycle 1  different 0 of 61
     Unapproved spec version: clear was not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   ```

### How to read the result

A paused run starts no new key. A key that is being read when the pause arrives is finished first, and a
key that waits for its second read keeps waiting: the web UI counts it under `recheck`.

A cancelled run keeps every result it had. The keys that were never started count as `cancelled`. In
this guide's run that was 60 of 61, with 1 compared; your numbers depend on how fast you typed. A key
whose first read showed a difference, and whose second read the cancel cut off, counts as `outstanding`
and is listed in `retry.csv` with the reason `recheck_owed`: a slow run that was cancelled later had
30 keys compared, 28 cancelled and 3 outstanding.

Stopping the command is not stopping the run. Ctrl-C in a `ds run` that is waiting prints `interrupted`
and ends with status 130; the run stays `running` and the app finishes it.

### Limits

- Only the web UI and the API pause, resume and cancel. A `ds run` that is waiting for a run someone
  pauses prints `paused` and `error: the run is paused`, and ends with status 2.
- A cancelled run cannot be resumed. Start a new run with the keys `retry.csv` lists.
- "cancel" in the web UI acts at once and stands beside "pause".
- It takes the role `operator`. A token of the role `viewer` got `{"detail": "needs the operator
  role"}` for each of the three.
- Not verified here: a worker that is killed. The runbook says it loses at most the keys it was reading,
  and that another worker reads them again.

### If something goes wrong

**`{"detail": "cannot resume a run that is cancelled"}`**, or **`{"detail": "cannot pause a run that
is completed"}`**
The run is not in a state the request can change. A run that ended stays as it ended.

**`{"detail": "no run R-NOPE"}`**
The run id is mistyped. `ds runs` lists the ids.

**`no matches found: http://localhost:8000/v1/runs/...`** (zsh)
The address held a `?` and was not in single quotes. Quote it.

**`curl: (7) Failed to connect to localhost port 8000 after 0 ms: Couldn't connect to server`**
Nothing answers at that address. The app container is stopped, or the address is not your
installation's (section 1.1). Start the containers with `docker compose --profile mocks up -d`.

**`dsapi: no such file or directory: .deltasleuth/tokens/first-run`** (zsh) or **`bash:
.deltasleuth/tokens/first-run: No such file or directory`** (bash), followed by **`{"detail": "not
signed in"}`**
The short form found no token file, because the terminal is not in the installation's folder. `cd`
into `IntelliDiff`.

**`{"detail": "invalid or expired API token"}`**
The address is another installation's, or the token has expired. Check the address (section 1.1).

## 5. Runs from captured files (file mode)

### What it is for

A file-mode run compares answers that were captured earlier and saved as files. It calls no service. A
team uses it to try a spec before any address or credential is available, or to compare the very same
answers again after changing the spec.

### Before you start

Section 1, and a directory of samples in the volume. Step 1 below writes one for the linker pair. The
layout is one sub-directory for each side, `a` and `b`, and in each one file for each key:

```text
linker-samples/
  a/ORG-000001.json          the answer's body, exactly as the service sent it
  a/ORG-000001.meta.json     optional: the HTTP status, the time it took and when it was captured
  b/ORG-000001.json
  b/ORG-000001.meta.json
```

An answer without a `.meta.json` file counts as HTTP 200. An answer that said "not found" therefore
belongs in the samples with its real status in the `.meta.json` file: the mocks' sample for
`ORG-000041`, which side b does not have, holds the body `{"error":"NOT_FOUND"}` and `"status": 404`.
Run on a copy of the samples without the `.meta.json` files, that key ended `INCONCLUSIVE` where it
should be `NOT_FOUND_B`.

### In the web UI

The web UI cannot start a file-mode run, and neither can the API. The runbook names file-mode runs
among the things only the command line does and gives no reason. Once the run exists, it is listed on
the Runs screen and opens like any other run. Nothing on its page says that it read files.

### On the command line

1. Write the linker pair's samples into the volume. The command runs the mocks' own sample writer in a
   container of the app; `-v` lets that container see the folder `mocks` of your machine.

   ```bash
   docker compose run --rm -v "$PWD/mocks:/app/mocks:ro" app python -m mocks.fixtures linker --out /var/lib/deltasleuth/p0/linker-samples
   ```

   You should see:

   ```text
    Container deltasleuth-db-1  Running
   wrote 122 samples for linker to /var/lib/deltasleuth/p0/linker-samples
   ```

2. Run the linker spec on the samples.

   ```bash
   ds run /var/lib/deltasleuth/p0/linker.yaml --samples /var/lib/deltasleuth/p0/linker-samples
   ```

   You should see:

   ```text
   run R-35T7CWHHAZR656X93NJMB4CK8F started
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 61, compared 58, match 52, different 6, indeterminate 3, cancelled 0, outstanding 0
     INCONCLUSIVE 3, MATCH 52, MISMATCH 5, NOT_FOUND_B 1
   ```

### How to read the result

A captured answer cannot be read a second time, so nothing is confirmed: every result of the run stays
`provisional`. A difference still counts as different (`different 6`), never as `outstanding`.

`DRIFT` cannot appear. The key `ORG-000029`, which a live run reports as `DRIFT`, is a `MISMATCH` here:
its sample holds side b's out-of-date answer, and nothing shows that the service caught up. That is why
this run has `MISMATCH 5` where the live run of section 3 had `MISMATCH 4` and `DRIFT 1`.

The result does not depend on the mocks: the run gave the same counts with them stopped or restarted.

### Limits

- Batch only, and only for sides that are services. A table side is always read live.
- The samples must be in the volume. The app's worker processes the run, and it sees only the volume.
- `--keys` narrows the run to some of the sampled keys: with `three-keys.txt` the run had `total 3`.
- The spec's targets must still be declared in `deploy/config/targets.yaml`, as the mocks' are.
- Not verified here: a sample of an answer that comes in several pages. The runbook says it must hold
  the whole walk, merged, or the key is `INCONCLUSIVE` with the reason `truncated`.

### If something goes wrong

**`error: sample root '/var/lib/deltasleuth/p0/no-such-samples' does not exist`**
The directory is not in the volume. Write or copy the samples there.

**`error: a soak run reads the targets: captured samples do not change between cycles`**
`--samples` was given together with `--soak`. A soak run is always live.

## 6. Soak runs

### What it is for

A soak run repeats the same comparison at a fixed interval, as numbered rounds called cycles. It watches
two live systems over hours or days, for example while both are in use before a cut-over. It ends by
itself on the conditions the spec sets, and it lists every key whose outcome changed from one cycle to
the next.

### Before you start

Section 1. The spec must say how far apart the cycles start, in `execution.cycleInterval`. To end by
itself it also needs a condition under `execution.stop`. The copy `linker-soak.yaml` holds these two
lines, which neither the web UI nor a command can set:

```text
  cycleInterval: 30s
  stop: {maxCycles: 2}
```

Restart the mocks (section 1.3) before the web UI steps, so that one key changes between the two
cycles; the command-line steps start with the same restart.

### In the web UI

1. Click "Runs". In the panel "New run", pick `mock-linker-soak 1.1.0 (#6)` in "Spec".
2. Set "Mode" to `soak`. A checkbox "resolve the keys again each cycle" appears. Leave it empty: every
   cycle then reads the same keys. Ticked, each cycle reads the spec's key source again.
3. Press "Start". The run's page opens. The line under the heading reads `mock-linker-soak 1.1.0 · soak
   · cycle 1 · ...`, and `cycle 2` once the first cycle has ended.
4. Wait about 40 seconds from the start. The badge reads `completed`, and the line under the heading
   ends with `· stopped: stop_condition:maxCycles`.
5. Find the drop-down "Cycle" at the left of the filter bar. Only a soak run has it. Its choices are
   `any`, `1` and `2`. The choice `any` does not show all cycles together: it shows the current cycle,
   which is the last one, cycle 2, once the run has ended.
6. Choose `1`. The tables "Root causes", "Results" and "Findings" now show cycle 1, and the report
   buttons download cycle 1's reports. The key `ORG-000029` reads `DRIFT` in "Results" of cycle 1 and
   `MATCH` under `any`. The bar, the outcome badges and "Counters" above keep showing the current
   cycle.
7. Scroll to "Outcome changes across cycles". Its table has the columns "Cycle", "Key", "From" and "To",
   and one row: `2`, `ORG-000029`, `DRIFT`, `MATCH`.

### On the command line

1. Restart the mocks (section 1.3), also if you did so before the web UI steps.

   ```bash
   docker compose --profile mocks restart mocks
   ```

   You should see:

   ```text
    Container deltasleuth-mocks-1  Restarting
    Container deltasleuth-mocks-1  Started
   ```

2. Start the soak run without waiting for it.

   ```bash
   ds run /var/lib/deltasleuth/p0/linker-soak.yaml --soak --no-wait
   ```

   You should see:

   ```text
   run R-35T7CX368F6X6KADJQ5WWWVDT9 started
   processed by 'ds worker' or 'ds serve'
   ```

3. Wait ten seconds, then look at it.

   ```bash
   ds runs --limit 1
   ```

   You should see the run in its second cycle, which has not read a key yet; the count beside it is the
   first cycle's:

   ```text
   R-35T7CX368F6X6KADJQ5WWWVDT9  mock-linker-soak 1.1.0  soak  running  cycle 2  different 5 of 61
     Unapproved spec version: clear was not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   ```

4. Write an interim report: two reports of the first cycle, while the run goes on.

   ```bash
   ds report <run id> --out /var/lib/deltasleuth/p0/<run id>-cycle1 --cycle 1 --kind coverage.csv --kind retry.csv
   ```

   You should see the two files it wrote:

   ```text
   /var/lib/deltasleuth/p0/R-35T7CX368F6X6KADJQ5WWWVDT9-cycle1/R-35T7CX368F6X6KADJQ5WWWVDT9-coverage.csv
   /var/lib/deltasleuth/p0/R-35T7CX368F6X6KADJQ5WWWVDT9-cycle1/R-35T7CX368F6X6KADJQ5WWWVDT9-retry.csv
   ```

5. Wait until 45 seconds have passed since step 2, then look again.

   ```bash
   ds runs --limit 1
   ```

   You should see:

   ```text
   R-35T7CX368F6X6KADJQ5WWWVDT9  mock-linker-soak 1.1.0  soak  completed  cycle 2  different 5 of 61
     Unapproved spec version: clear was not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   ```

6. Ask for the keys whose outcome changed between cycles. `ds` has no command for this.

   ```bash
   dsapi http://localhost:8000/v1/runs/<run id>/changes
   ```

   You should see one key:

   ```text
   {"items": [{"cycle": 2, "key_masked": "ORG-000029", "from_outcome": "DRIFT", "to_outcome": "MATCH"}]}
   ```

7. Write the final report: without `--cycle`, a report is the current cycle's, here the last one.

   ```bash
   ds report <run id> --out /var/lib/deltasleuth/p0/<run id> --kind report.pdf --kind retry.csv
   ```

   You should see:

   ```text
   /var/lib/deltasleuth/p0/R-35T7CX368F6X6KADJQ5WWWVDT9/R-35T7CX368F6X6KADJQ5WWWVDT9-report.pdf
   /var/lib/deltasleuth/p0/R-35T7CX368F6X6KADJQ5WWWVDT9/R-35T7CX368F6X6KADJQ5WWWVDT9-retry.csv
   ```

8. Copy the reports out of the volume, to read them on your machine.

   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/<run id> p0-out/
   ```

   You should see:

   ```text
    deltasleuth-app-1 copy deltasleuth-app-1:/var/lib/deltasleuth/p0/R-35T7CX368F6X6KADJQ5WWWVDT9 to p0-out/ Copying
    deltasleuth-app-1 copy deltasleuth-app-1:/var/lib/deltasleuth/p0/R-35T7CX368F6X6KADJQ5WWWVDT9 to p0-out/ Copied
   ```

Started without `--no-wait`, the command returns when the soak ends, here after 37 seconds, and prints
the last cycle:

```text
completed (stop_condition:maxCycles)
  total 61, compared 58, match 53, different 5, indeterminate 3, cancelled 0, outstanding 0
  INCONCLUSIVE 3, MATCH 53, MISMATCH 4, NOT_FOUND_B 1
```

`--rolling` is the command's form of the checkbox "resolve the keys again each cycle":

```bash
ds run /var/lib/deltasleuth/p0/linker-soak.yaml --soak --rolling --no-wait
```

It printed the same two lines as step 2, and the run went through its two cycles as the first did.

### How to read the result

**Cycles and the interval.** A cycle is due one interval after the start of the cycle before it. Here
cycle 1 took 5 seconds, cycle 2 started 30 seconds after the run's start, and the run ended 36 seconds
after its start.

**Fixed and rolling keys.** A fixed soak, the default, reads the same keys in every cycle. A rolling
soak reads the spec's key source again at each start of a cycle, so it follows a set that grows or
shrinks. With a list written in the spec the two do the same, as here. Keys given with the run, in a
`--keys` file or the box "Keys, one per line", are never read again. Not verified here: the runbook says
that a rolling soak whose key listing fails keeps the keys of the cycle before and logs `run R-...
keeps its key set`.

**Outcome changes.** A change is a key that was read in two cycles in a row and got another outcome in
the second. Here it is `ORG-000029`: `DRIFT` in cycle 1, because side b was still catching up, and
`MATCH` in cycle 2. A key that changes back and forth is an unstable record or an unstable service; a
key that goes from `MISMATCH` to `MATCH` was fixed.

**Interim and final reports.** A report is built from the database at the moment you ask. Asked with
`--cycle` for a cycle that has ended, while the soak goes on, it is an interim report. Asked after the
end without `--cycle`, it is the final one. The PDF's first lines say `soak · status completed · cycle 2
of 2` and `stop reason: stop_condition:maxCycles`, and its section "Outcome changes across soak cycles"
holds the same row as step 6. `retry.csv` of cycle 1 lists `ORG-000029` as `DRIFT`; the final one does
not.

**Stop conditions.** `maxCycles` ends the soak after that many cycles, with the status `completed`.
`maxDuration` and `until` end it at the start of the first cycle that would begin after the limit, also
`completed`: a copy with `stop: {maxDuration: 20s}` and the same interval ended after one cycle with
`completed (stop_condition:maxDuration)`. The other conditions of section 3 stop a cycle in progress.
Not verified here: a soak whose spec has no stop condition. By the code it runs until someone cancels
it (section 4), and a cycle that takes longer than the interval is followed by the next at once.

### Limits

- The interval and the stop conditions are lines of the spec. To change them, save a changed spec.
- After the start, the web UI does not show whether the soak is rolling. The API does:
  `dsapi http://localhost:8000/v1/runs/<run id>` holds `"soak": {"rolling": true}`.
- The top of the run's page always shows the current cycle. An earlier cycle's counters are in that
  cycle's `coverage.csv` and `report.pdf`.
- Between two cycles, `ds runs` prints the new cycle's number beside the last cycle's counts.
- A report asked for a cycle that has not read a key yet, or that does not exist, is written with its
  header and no rows. Nothing warns: `--cycle 3` of this two-cycle run wrote such a file.
- A soak run never reads samples (section 5).

### If something goes wrong

**`error: a soak run needs the spec's execution.cycleInterval`**
The spec has no interval. Provoked with `linker.yaml` and `--soak`. Use a spec that sets one.

**`{"items": []}` from step 6**
No key changed its outcome. With the mocks that means they were not restarted just before the run: the
key `ORG-000029` had already given its out-of-date answer. Repeat the steps from step 1.

**The run never ends**
Its spec has no stop condition. Cancel it (section 4).

## 7. Outcomes and reasons

### What it is for

Every key ends with one outcome. It says whether the two sides agree, differ, or could not be
compared, and where needed a reason says why. Anyone who reads a run needs these words, because
neither the web UI nor the reports explain them.

### Before you start

Section 1. The web UI steps need the batch run of the linker pair's 61 keys that section 3 made. The
command-line steps need no run; they start with a restart of the mocks.

### In the web UI

Outcomes show in four places on a run's page, and nowhere with an explanation.

1. Open the batch run of section 3: click "Runs", then the id of the run whose "Spec" reads
   `mock-linker-legacy-vs-new 1.1.0` and whose "Mode" reads `batch`. If several runs fit, the right one
   shows `61/61 keys` beside its bar.
2. Look at the row of badges under the bar: one for each outcome that occurred, with its count, for
   example `MISMATCH 4`. Their order is not fixed.
3. Click the badge `MISMATCH`. The table "Results" now lists the four keys, and the drop-down "Outcome"
   in the filter bar reads `MISMATCH`.
4. Read the table's columns: "Key", "Outcome", "Why" (the reason of an `INCONCLUSIVE` or `DRIFT` key),
   "Confirmation", and "A" and "B", the state of each side.
5. Click "clear" in the filter bar to see every key again.
6. Click "Counters" above the filter bar. A list unfolds with every counter of the run, the counts for
   each outcome and reason among them, with names such as `by_outcome.MATCH` and
   `by_inconclusive_reason.truncated`.

The page of one key shows its outcome beside the heading (section 10).

### On the command line

Each step after the first compares one key that the mocks answer in a special way.

1. Restart the mocks (section 1.3): the next step needs the first-time answer of one key.

   ```bash
   docker compose --profile mocks restart mocks
   ```

   You should see:

   ```text
    Container deltasleuth-mocks-1  Restarting
    Container deltasleuth-mocks-1  Started
   ```

2. Compare the key whose side b gives an out-of-date answer to the first call.

   ```bash
   ds compare ORG-000029 --spec /var/lib/deltasleuth/p0/linker.yaml
   ```

   You should see:

   ```text
   ORG-000029: DRIFT (INDETERMINATE, rechecked) reason b_converged
   run R-35T7D0NX8CE76AD1ESMMBRE61W; observations a O-6418118f6a48b44a b O-a74bc43377d598d1
   ```

3. Compare the same key again. Side b now answers with the current record.

   ```bash
   ds compare ORG-000029 --spec /var/lib/deltasleuth/p0/linker.yaml
   ```

   You should see:

   ```text
   ORG-000029: MATCH (MATCH, provisional)
   run R-35T7D0T91N0NF1Q4W4R9R8VA4P; observations a O-c648221d3ee1e65f b O-6f42ef0c65459928
   ```

4. Compare a key that side b does not have.

   ```bash
   ds compare ORG-000041 --spec /var/lib/deltasleuth/p0/linker.yaml
   ```

   You should see:

   ```text
   ORG-000041: NOT_FOUND_B (DIFFERENT, rechecked)
     CRITICAL MISSING_DOCUMENT missing_in_b /
         A: <absent>
         B: <absent>
   run R-35T7D0Y1J1VA8T1ATTY4QP21H8; observations a O-7fe66466ded69b17 b O-0c0e64538a82e1c1
   ```

   The line `A: <absent>` is misleading here: side a has the record. A difference that concerns the
   whole record prints no value for either side.

5. Compare a key for which side b returns only part of a list.

   ```bash
   ds compare ORG-000034 --spec /var/lib/deltasleuth/p0/linker.yaml
   ```

   You should see:

   ```text
   ORG-000034: INCONCLUSIVE (INDETERMINATE, provisional) reason truncated
   run R-35T7D1297MM9WV203NASFK6KAR; observations a O-8557756ad2442b1d b O-2aa0d72c7e9d030d
   ```

6. Compare a key for which side a says "not found" in a form nobody declared.

   ```bash
   ds compare ORG-000046 --spec /var/lib/deltasleuth/p0/linker.yaml
   ```

   You should see:

   ```text
   ORG-000046: INCONCLUSIVE (INDETERMINATE, provisional) reason absence_unconfirmed
   run R-35T7D15TR7934EHDKHCMTKXADQ; observations a O-418423fd51f4100f b O-3246b53e98b61332
   ```

7. Compare a key that neither side has.

   ```bash
   ds compare ORG-000999 --spec /var/lib/deltasleuth/p0/linker.yaml
   ```

   You should see:

   ```text
   ORG-000999: INCONCLUSIVE (INDETERMINATE, provisional) reason absent_both
   run R-35T7D19B005S9VZVRCHEZ3SYTG; observations a O-382009184c0c0364 b O-b0001fd7bb4969fe
   ```

8. Compare a customer whose one difference the spec declares as intended.

   ```bash
   ds compare C00012031 --spec /var/lib/deltasleuth/p0/customer.yaml
   ```

   You should see the difference, the rule that covers it, and the verdict `MATCH`:

   ```text
   tok:identifier:v1:2C4GS3V4D2FDCOWXVX6PBNQUAI: KNOWN_DIFF (MATCH, provisional)
     MEDIUM VALUE_MISMATCH value_changed /segment suppressed by KD-CP-0001
         A: PRIVATE
         B: WEALTH
   run R-35T7D1CTXNV5060TEZMTQ0XEH6; observations a O-f68833d8b4cc6827 b O-347d62c0af29c655
   ```

9. Compare a customer whose one difference the spec declares as a known bug.

   ```bash
   ds compare C00012035 --spec /var/lib/deltasleuth/p0/customer.yaml
   ```

   You should see the same outcome with the verdict `DIFFERENT`:

   ```text
   tok:identifier:v1:RKVR2R4A26MDHC6Y7DH3U4TFP5: KNOWN_DIFF (DIFFERENT, provisional)
     MEDIUM FIELD_MISSING missing_in_b /preferences/marketing/sms suppressed by KD-CP-0002
         A: False
         B: <absent>
   run R-35T7D1GD5BSWFQM7443G4K891J; observations a O-279ce489a8d2b251 b O-2d83b0085356a3ae
   ```

10. Freeze the mocks, so that they accept a call and never answer it.

    ```bash
    docker compose --profile mocks pause mocks
    ```

    You should see:

    ```text
     Container deltasleuth-mocks-1  Paused
    ```

11. Compare any key. The command takes about 20 seconds: each side is tried twice, five seconds each.

    ```bash
    ds compare ORG-000001 --spec /var/lib/deltasleuth/p0/linker.yaml
    ```

    You should see:

    ```text
    ORG-000001: INCONCLUSIVE (INDETERMINATE, provisional) reason both_error
    run R-35T7D1MBAP6M8ADBBMES252BSP; observations a O-e535d125e2afcef1 b O-9a05b550e1d7fd13
    ```

12. Let the mocks go on.

    ```bash
    docker compose --profile mocks unpause mocks
    ```

    You should see:

    ```text
     Container deltasleuth-mocks-1  Unpaused
    ```

The key of step 11 is stored with the state `ERROR` and the reason `timeout` for each side, and two
attempts for each. `both_error` is what a key gets when both sides fail. With only side b frozen, one
of the four mock services halted by hand, the key's reason was `timeout`.

One more key is worth knowing. For `ORG-000052`, side b answers the first call with an error (HTTP 503).
The spec retries such calls, so the key is a plain `MATCH`; only the list of reads on the key's page
shows it, with 2 under "Attempts" for side b.

### How to read the result

| Outcome | In plain words | Verdict | What to do |
|---|---|---|---|
| `MATCH` | Both sides answered in full and nothing that counts differs. A difference of format only, which the spec tells DeltaSleuth to ignore, does not count; such differences are listed in `normalization.csv` | `MATCH` | Nothing. Read `normalization.csv` once and agree that these are the same value |
| `MISMATCH` | Both sides answered in full and at least one difference is left that no rule explains | `DIFFERENT` | Open the key (section 10), find the cause (section 8), and have the service or the spec corrected |
| `KNOWN_DIFF` | Every difference of the key is covered by a known-difference rule of the spec, a line that says "this difference is expected" | `MATCH`; `DIFFERENT` when the rule is classed `bug` | For a `bug` rule, follow the fix the rule names. Otherwise nothing |
| `NOT_FOUND_A`, `NOT_FOUND_B` | The record certainly does not exist on that side and exists on the other. These are two outcomes | `DIFFERENT` | Find out why the record is missing: usually it was not migrated, or was deleted on one side |
| `DRIFT` | The data changed between the first and the second read, so the first difference proves nothing. `b_converged`: side b caught up and the difference is gone. `a_changed`: side a changed | `INDETERMINATE` | Compare the key again later. Many `b_converged` keys mean that side b receives changes late |
| `INCONCLUSIVE` | A side could not be read completely or reliably. The reason says why. It is never counted as a match | `INDETERMINATE` | Depends on the reason: see the next table |

The drop-down "Outcome" offers one more, `NOISE_ONLY`. No key can get it in this build: it would need a
part of a spec that this build refuses to load.

Not verified here: `NOT_FOUND_A` and the drift reason `a_changed`. The mocks have no key that only side
a lacks, and side a never changes its answer.

The reasons of `INCONCLUSIVE`:

| Reason | In plain words | What to do | Seen here |
|---|---|---|---|
| `truncated` | A side says it has more items than it returned. Side b returns 50 of the 57 links of `ORG-000034` | Such a key cannot be compared through this address. Compare it through a paged request, or by other means | Step 5 |
| `absence_unconfirmed` | A side answered "not found", but not in the form its target declares as a real "not found". It could be a gateway's error page | Find out what the service returns for a missing record and declare that form on the target | Step 6 |
| `absent_both` | Neither side has the record. The key list is out of date | Remove the key from the list | Step 7 |
| `both_error` | Both sides failed. The columns "A" and "B" give each side's own reason | Check that the services are up, then run the keys again | Step 11 |
| `timeout` | One side did not answer in time, after the retries the spec allows | Run the key again. If it happens often, lower the target's `rate_per_s` or raise the spec's `execution.timeouts` | With one mock service halted |
| `error_a`, `error_b` | That side could not be reached or kept answering with a server error | Check that service | As the sides' reasons of `both_error`, with the mocks stopped |
| `recheck_failed` | The second read of a key failed, so its first difference was never confirmed | Run the key again | By freezing the mocks between the two reads of `ORG-000007` in a batch run: first outcome `MISMATCH`, then `INCONCLUSIVE` with `ERROR: timeout` for both sides |
| `circuit_open` | The spec's circuit breaker, a line `execution.circuitBreaker` that stops calls to a service that keeps failing, was open, so no call was made | Wait for the service, then run the keys again | As both sides' reason under `both_error`: with the mocks stopped, a run whose spec sets a breaker failed on 11 keys and was refused for the other 50. See "Limits" |
| `rate_limited` | A side kept answering "too many requests" (HTTP 429) | Lower the target's `rate_per_s` and `max_concurrency` | Not verified here: the mocks never answer 429 |
| `unparseable` | An answer could not be read as JSON, or the spec selected nothing from either side | Look at the answer and at the spec's `select` | Not verified here: the mocks give no such answer |

`coverage.csv` has a column for each reason, a few more than this table: the others belong to table
and file sides, which [chapter 6](06-tables.md) and [chapter 7](07-engines-and-files.md) cover.

**Confirmation.** `rechecked` means the key was read a second time and judged again. `provisional`
means it was read once. A key that matched at once is `provisional` for good, since nothing needed
confirming: of the 61 keys of the run of section 3, 55 are `provisional` and 6 `rechecked`. Which first
outcomes get a second read is the spec's choice (`recheck.outcomes`); the mock specs name `MISMATCH`,
`NOT_FOUND_A` and `NOT_FOUND_B`, so an `INCONCLUSIVE` key is not read again.

### Limits

- The web UI has no legend for outcomes or reasons.
- The web UI does not show a key's verdict. It is in `ds compare`, in the counters and in the reports.
- For a `KNOWN_DIFF` key the web UI shows the rule's id and `(suppressed)`, not the rule's class,
  which decides whether the key counts as different. The class is in `suppressed.csv` (section 12).
- A circuit breaker belongs to the spec that declares it. The app keeps one for each service and each
  `circuitBreaker` line declared for it, so a spec's own line governs its runs, whichever spec called
  the service first, and a spec without the line has no breaker. An open breaker also refuses the runs
  and compares of other specs that declare the same breaker (the same rate, window and cooldown) on the
  same services, until its cooldown is over. It refuses no call of a spec that declares another
  breaker, or none. If you meet `circuit_open` on a healthy service, wait for the cooldown: nothing
  needs a restart. Not verified here: this bullet is the runbook's account.

### If something goes wrong

**Step 2 prints `MATCH`, not `DRIFT`**
The mocks had been asked for that key before. Restart them (step 1) and repeat the step.

**Every compare ends in `both_error` after about 20 seconds**
The mocks are still frozen. Run step 12.

## 8. Root causes and signatures

### What it is for

A run can find thousands of differences that are one mistake seen thousands of times. DeltaSleuth
groups the differences of a run into probable causes, called root causes, and lists them with the most
severe and the most widespread first. A team then corrects causes, not single records.

### Before you start

Section 1. The causes are worked out when a cycle ends, so a run shows none while it is still going.

### In the web UI

1. Click "Runs". In the panel "New run", pick `mock-linker-unmapped-kind 1.1.0 (#7)` in "Spec", leave
   the rest as it is, press "Start" and wait for the badge `completed`. The outcome badges read
   `MISMATCH 57`, `NOT_FOUND_B 1` and `INCONCLUSIVE 3`, in any order.
2. Read the first table, "Root causes". Its columns are "Root cause", "Severity", "Taxonomy" (the kind
   of difference), "Path" (where in the record), "Change" (what changed, and in brackets the
   direction), "Keys", "Findings" and "Sample keys".
3. Find the row whose "Path" is `$.kind`. It reads 57 under "Keys" and 57 under "Findings". The row
   `$.links[*].kind` reads 48 and 188.
4. Click the id of the first of the two, `RC-2f791e4669b6`. The table "Findings" further down now holds
   only that cause's differences, and the box `root_cause_id` in the filter bar holds the id.
5. Click one of the "Sample keys" to open that key's page (section 10).

### On the command line

1. Run the copy with the built-in mistake.

   ```bash
   ds run /var/lib/deltasleuth/p0/linker-unmapped-kind.yaml
   ```

   You should see every comparable key reported as different:

   ```text
   run R-35T7D321V6YJCYNX2CPXZXFWKN started
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 61, compared 58, match 0, different 58, indeterminate 3, cancelled 0, outstanding 0
     INCONCLUSIVE 3, MISMATCH 57, NOT_FOUND_B 1
   ```

2. Write the run's root causes as a file.

   ```bash
   ds report <run id> --out /var/lib/deltasleuth/p0/<run id> --kind root_causes.csv
   ```

   You should see the file's path:

   ```text
   /var/lib/deltasleuth/p0/R-35T7D321V6YJCYNX2CPXZXFWKN/R-35T7D321V6YJCYNX2CPXZXFWKN-root_causes.csv
   ```

3. Copy it out of the volume.

   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/<run id> p0-out/
   ```

   You should see the two copy lines:

   ```text
    deltasleuth-app-1 copy deltasleuth-app-1:/var/lib/deltasleuth/p0/R-35T7D321V6YJCYNX2CPXZXFWKN to p0-out/ Copying
    deltasleuth-app-1 copy deltasleuth-app-1:/var/lib/deltasleuth/p0/R-35T7D321V6YJCYNX2CPXZXFWKN to p0-out/ Copied
   ```

4. Print it.

   ```bash
   cat p0-out/<run id>/<run id>-root_causes.csv
   ```

   You should see eight causes:

   ```text
   root_cause_id,kind,taxonomy_code,max_severity,path_pattern,change_type,direction,affected_keys,finding_count,member_key_tok,value_class,signature_hash,sample_keys,bug_rule_id
   RC-cd785916a3ea,signature,MISSING_DOCUMENT,CRITICAL,$,missing_in_b,a_only,1,1,,other,a08433237de818ad,ORG-000041,
   RC-e0bcd1375262,signature,VALUE_MISMATCH,CRITICAL,$.links[*].flagged,value_changed,both,1,1,,other,d27c92bda89a6ad2,ORG-000023,
   RC-4dc4f6d9717a,signature,FIELD_EXTRA,HIGH,$.links[*],extra_in_b,b_only,1,1,,other,3cd57e0a6f90ec4d,ORG-000012,
   RC-e2ac602b3290,signature,FIELD_MISSING,HIGH,$.links[*],missing_in_b,a_only,1,1,,other,7dfd378ad62f59da,ORG-000007,
   RC-2f791e4669b6,signature,VALUE_MISMATCH,MEDIUM,$.kind,value_changed,both,57,57,,other,9a213312372d8f1c,ORG-000001;ORG-000002;ORG-000003;ORG-000004;ORG-000005,
   RC-73c5b13f386d,signature,VALUE_MISMATCH,MEDIUM,$.links[*].kind,value_changed,both,48,188,,other,5f6b003f7442078f,ORG-000001;ORG-000003;ORG-000004;ORG-000006;ORG-000007,
   RC-ee9e9dbeaead,signature,FIELD_MISSING,MEDIUM,$.links[*].sharedIdentifiers.devices[*],missing_in_b,a_only,1,1,,other,0d036c166ab14675,ORG-000057,
   RC-f8bb5e09390a,signature,FIELD_EXTRA,MEDIUM,$.links[*].sharedIdentifiers.devices[*],extra_in_b,b_only,1,1,,other,c85bc8f70ff6acf4,ORG-000057,
   ```

### How to read the result

The run reports 57 keys as `MISMATCH`, yet two lines explain nearly all of it. `RC-2f791e4669b6` says:
at `$.kind`, the value differs (`value_changed`) and both sides have one (`both`), for 57 keys. That is
the look of one systematic cause. Here it is the mistake built into the spec: side b's words for the
kind of an entity are no longer translated. The six other causes are the ones the linker pair always
has, with one key each. The order is by severity first, then by the number of keys.

Each line holds:

| Column | Meaning |
|---|---|
| `root_cause_id` | The cause's id. It is made from where and how the difference occurs, never from a key or a value, so the same cause has the same id in every run. `RC-e0bcd1375262` is the same in section 2's output, in the run of section 3 and here |
| `kind` | How the cause was found. `signature`: differences that share a signature. `member_key`: the same member of a list, missing or extra under two or more keys, for example one linked entity that is missing from several records |
| `taxonomy_code`, `max_severity` | The kind of difference, and the highest severity among the cause's differences |
| `path_pattern` | Where in the record, with `[*]` for "any member of the list" |
| `change_type` | `missing_in_b`, `extra_in_b`, `value_changed`, `type_changed` or `order_changed` |
| `direction` | Which side holds the value: `a_only`, `b_only`, or `both` when the two differ |
| `affected_keys`, `finding_count` | How many keys the cause explains, and how many single differences |
| `member_key_tok` | For the kind `member_key`, the member concerned, masked |
| `value_class` | A finer class of the difference, such as `case_only` for a difference of upper and lower case, or `other` |
| `signature_hash` | The signature: 16 characters made from the place, the change, the value class and the kind of difference |
| `sample_keys` | Up to five of the keys, masked |
| `bug_rule_id` | The known-difference rule of class `bug` that covers the cause, if one does |

Not verified here: a cause of the kind `member_key`. The mocks hold no list member that is missing
under two keys, so every cause in this guide's runs is of the kind `signature`.

### Limits

- Only differences that make a key different are grouped: those above the severity `INFO` that no rule
  covers, and those a `bug` rule covers.
- The web UI's table does not show the kind, the signature or the masked member. They are in
  `root_causes.csv`.
- The table "Findings" has no column for the cause. To see a cause's differences, click the cause.

### If something goes wrong

**"None." under "Root causes"**
The run is still going, or it found no difference that counts. The same word shows for a moment while
the page loads.

## 9. Filtering results and findings on the Run screen

### What it is for

A run's page lists every key's result and every single difference. The filter bar narrows the two
lists, so that a reader finds, say, all differences of the highest severity or all keys one side lacks.

### Before you start

The batch run of the linker pair's 61 keys that section 3 made: the rows named below are that run's.
In the commands, `<run id>` is its id. If you did not note it, `ds runs` lists the runs (section 14);
it is the line with `mock-linker-legacy-vs-new 1.1.0  batch` that ends with `different 5 of 61`.

### In the web UI

The filter bar is the panel between the run's counters and the tables.

1. Open the run: click "Runs", then the run's id (the first web UI step of section 7 says how to tell
   it).
2. Choose `CRITICAL` in the drop-down "Severity". The choice applies at once. The table "Findings" now
   holds two rows: for the key `ORG-000023` the pointer `/links/ORG-000104/flagged` with `true` under
   "A" and `false` under "B", and for `ORG-000041` the pointer `/` with the change `missing_in_b`.
3. Click "clear" at the right of the bar. Every filter is removed.
4. Type `$.links[*]` into the box labelled `path_pattern` and press "Filter". "Findings" now holds two
   rows, for `ORG-000007` and `ORG-000012`. The text must be the whole pattern: the row at
   `$.links[*].flagged` is not among them.
5. Click "clear", then choose `NOT_FOUND_B` in the drop-down "Outcome". The table "Results" holds one
   row, `ORG-000041`, with `COMPLETE` under "A" and `ABSENT` under "B".
6. Look at the browser's address bar. The filters are part of the address, for example
   `#/runs/R-...?outcome=NOT_FOUND_B`, so a filtered view can be kept as a bookmark or sent to someone.
7. Press "More" under a table, where it shows, to load the next rows. "Results" and "Findings" load 100
   rows at a time.

Each filter narrows only some of the tables, and the bar does not say which:

| Filter | Narrows |
|---|---|
| "Cycle" (soak runs only) | "Root causes", "Results" and "Findings", and the report buttons. `any` is the current cycle, not all cycles |
| "Outcome" | "Results" only |
| "Severity", `key`, `taxonomy_code`, `path_pattern`, `root_cause_id` | "Findings" only |

The four boxes carry the names the API uses. `key` takes a key as the page shows it. `taxonomy_code`
takes the word in the column "Taxonomy". `root_cause_id` is filled when you click a cause (section 8).
Several filters together all apply.

### On the command line

The same filters are parameters of two addresses of the API.

1. List the keys with one outcome.

   ```bash
   dsapi 'http://localhost:8000/v1/runs/<run id>/results?outcome=NOT_FOUND_B'
   ```

   You should see one key:

   ```text
   {"items": [{"seq": 40, "run_id": "R-35T7CT44ZW54C0C6CVH5H63JQ1", "cycle": 1, "outcome": "NOT_FOUND_B", "side_a_state": "COMPLETE", "side_a_reason": null, "side_b_state": "ABSENT", "side_b_reason": null, "inconclusive_reason": null, "drift_reason": null, "confirmation": "rechecked", "first_outcome": "NOT_FOUND_B", "revision": 2, "suppressed_count": 0, "obs_a_id": "O-8d8f6fe67c31796a", "obs_b_id": "O-c4d9c724a460c417", "attrs": {}, "updated_at": "2026-10-05T20:19:45.684231+00:00", "key_masked": "ORG-000041"}], "next_cursor": null}
   ```

2. List the differences of the highest severity.

   ```bash
   dsapi 'http://localhost:8000/v1/runs/<run id>/findings?severity=CRITICAL'
   ```

   You should see two differences (shortened):

   ```text
   {"items": [{"key_seq": 22, "outcome": "MISMATCH", "confirmation": "rechecked", ... "finding_id": "F-41ed85da4876c1e1", "taxonomy_code": "VALUE_MISMATCH", "severity": "CRITICAL", "change_type": "value_changed", "direction": "both", "path_pattern": "$.links[*].flagged", "json_pointer": "/links/ORG-000104/flagged", "value_a": true, "value_b": false, ... "root_cause_id": "RC-e0bcd1375262", "key_masked": "ORG-000023"}, {"key_seq": 40, "outcome": "NOT_FOUND_B", ... "taxonomy_code": "MISSING_DOCUMENT", "severity": "CRITICAL", "change_type": "missing_in_b", ... "key_masked": "ORG-000041"}], "next_cursor": null}
   ```

3. List the differences at one place. The pattern holds characters an address cannot carry as they
   are, so curl encodes it.

   ```bash
   dsapi -G --data-urlencode 'path_pattern=$.links[*]' http://localhost:8000/v1/runs/<run id>/findings
   ```

   You should see two differences, for `ORG-000007` and `ORG-000012` (shortened):

   ```text
   {"items": [{"key_seq": 6, "outcome": "MISMATCH", ... "taxonomy_code": "FIELD_MISSING", "severity": "HIGH", "change_type": "missing_in_b", "direction": "a_only", "path_pattern": "$.links[*]", "json_pointer": "/links/ORG-000080", ... "key_masked": "ORG-000007"}, {"key_seq": 11, ... "taxonomy_code": "FIELD_EXTRA", ... "change_type": "extra_in_b", ... "json_pointer": "/links/ORG-000070", ... "key_masked": "ORG-000012"}], "next_cursor": null}
   ```

The address `/results` takes `outcome`, `cycle` and `limit`. The address `/findings` takes `severity`,
`key`, `taxonomy_code`, `path_pattern`, `root_cause_id`, `cycle` and `limit`. When `next_cursor` is not
`null`, there are more rows: ask again with `cursor=` and that value.

### How to read the result

A row of "Results" is one key. "A" and "B" give the state of each side: `COMPLETE` (it answered in
full), `ABSENT` (it certainly does not have the record), `INCOMPLETE` or `ERROR` followed by the reason.

A row of "Findings" is one difference:

| Column | Meaning |
|---|---|
| "Key" | The key the difference belongs to; a link to its page |
| "Finding" | The difference's id, `F-...` |
| "Pointer" | The place in the record, as a path: `/links/ORG-000080` is the member `ORG-000080` of the list `links`. `/` is the whole record |
| "Change" | `missing_in_b`, `extra_in_b`, `value_changed`, `type_changed` or `order_changed` |
| "Severity" | `CRITICAL`, `HIGH`, `MEDIUM`, `LOW` or `INFO`. An `INFO` difference never makes a key different |
| "Taxonomy" | The kind of difference. With the mocks: `FIELD_MISSING`, `FIELD_EXTRA`, `VALUE_MISMATCH`, `MISSING_DOCUMENT` (the whole record is missing on side b) and `NORMALIZATION_ONLY` (the raw values differ, and are equal once the spec's clean-up is applied) |
| "A", "B" | Each side's value, masked (section 11). Empty when the side has none |
| "Class" | A finer class of the difference, for example `case_only` or `type_only`; `other` when none fits |
| "Rule" | The id of the known-difference rule that covers the difference, followed by `(suppressed)` |

### Limits

- The API returns more about a difference than the table shows: its direction, its root cause, its
  signature. They are in `discrepancies.csv` (section 12).
- The filter `key` takes only the key as shown. For a pair whose keys are tokens, section 11 shows how
  to find the token of a key you know.
- A filter cannot say "everything except".

### If something goes wrong

**`{"detail": "invalid request", "issues": [{"pointer": "/query/outcome", "message": "Input should be
'MATCH', 'MISMATCH', 'KNOWN_DIFF', 'NOISE_ONLY', 'DRIFT', 'NOT_FOUND_A', 'NOT_FOUND_B' or
'INCONCLUSIVE'", "code": "enum"}]}`**
The value is not an outcome. Provoked with `outcome=DIFFERENT`, which is a verdict. The severities must
be written in capitals too.

**`{"detail": "run R-NOPE not found"}`**
The run id is mistyped.

**`no matches found: http://localhost:8000/v1/runs/...`** (zsh)
The address held a `?` and was not in single quotes.

## 10. The Key screen

### What it is for

The page of one key shows the two sides' records next to each other, field by field, with the fields
that differ marked. Below it are the key's differences and a log of every read made for it. A reviewer
uses it to decide whether a reported difference is real.

### Before you start

The batch run of the linker pair's 61 keys that section 3 made. Every time someone opens a key's page,
the audit log records it (`key.view`).

### In the web UI

1. Open the run as in the first web UI step of section 7, and in the table "Results" click the key
   `ORG-000007`. The page opens with a link "Run R-..." back to the run, the heading "Key ORG-000007"
   with the badge `MISMATCH`, and the button "Compare now" (section 2).
2. Read the facts under the button:

   | Fact | For this key | Meaning |
   |---|---|---|
   | `cycle` | 1 | The cycle the result belongs to |
   | `state` | `done` | Where the key stands in the run: `pending`, `leased` (being read), `recheck` (waiting for its second read), `done` or `cancelled` |
   | `pass` | 2 | The number of the last read the run planned for the key: 2 when a batch or soak run read it a second time. A one-key compare always shows 1 here, also when it read the key twice |
   | `first outcome` | `MISMATCH` | What the first read gave |
   | `confirmation` | `rechecked` | Section 7 |
   | `revision` | 2 | How many times the result was written: 2 after a second read, in every kind of run |
   | `side A`, `side B` | `COMPLETE` | Each side's state, with its reason |
   | `why` | empty | The reason of an `INCONCLUSIVE` or `DRIFT` outcome |
   | `suppressed` | 0 | How many of its differences a rule covers |

   A key whose read failed has more facts, for example `side_b_detail` with `fetch failed: timeout`.
3. Read the block "Canonical A and B (masked)". It has three columns headed `path`, `A` and `B`, and one
   row for each field, indented by its depth. The first row, `$`, is the whole record. A row for a
   group of fields shows the number of fields in braces, such as `{4}`; a row for a list shows the
   number of members in square brackets. A list whose members are matched by a key, such as `links`,
   is drawn with one row for each member's key, here the code of each linked entity.
4. Find the coloured rows. A row that differs has the colour of its kind of change, and the five kinds
   are named in a row of coloured words beside the checkbox: `missing_in_b`, `extra_in_b`,
   `value_changed`, `type_changed`, `order_changed`. For this key the row `ORG-000080` under `links`,
   and the rows below it, are filled under `A` and empty under `B`.
5. Tick "differences only". Only the coloured rows and the rows that lead to them remain.
6. Read the table "Findings". It has the columns of section 9 without "Key", and one row for this key:
   the pointer `/links/ORG-000080`, the change `missing_in_b`, the severity `HIGH`, the kind
   `FIELD_MISSING`, the member under "A" and nothing under "B".
7. Read the table "Observations", the log of reads. It has the columns "Observation" (the read's id),
   "Side", "Pass" (1 for the first read, 2 for the second), "HTTP" (the status of the answer),
   "Latency ms", "Attempts" (more than 1 when a call was retried), "Pages", "Error" and "Observed". This
   key has four rows: both sides in pass 1 and both in pass 2.
8. Look at the bottom right corner of the window. With AI chat off, as chapter 1 leaves it, the words
   "AI is off" stand there, on this page and on a run's page, however far the page is scrolled.
   [Chapter 10](10-chat-and-agents.md) switches the chat on.

For a soak run, the key opens in the cycle that the drop-down "Cycle" of the run's page shows. With a
number chosen there, the key page's address carries it (`?cycle=1`), and the link "Run R-..." returns
to that cycle. With "Cycle" at `any`, the address carries no cycle and the page shows the key in the
current cycle. A key opened from the table "Outcome changes across cycles" always carries that row's
cycle.

### On the command line

`ds` has no command that shows a stored key. The API returns what the page shows.

1. Ask for the key.

   ```bash
   dsapi http://localhost:8000/v1/runs/<run id>/keys/ORG-000007
   ```

   You should see the result, the differences, the four reads and the two records (shortened):

   ```text
   {"key_masked": "ORG-000007", "cycle": 1, "seq": 6, "state": "done", "pass_no": 2, "run_id": "R-35T7CT44ZW54C0C6CVH5H63JQ1", "result": {... "outcome": "MISMATCH", "side_a_state": "COMPLETE", ... "side_b_state": "COMPLETE", ... "confirmation": "rechecked", "first_outcome": "MISMATCH", "revision": 2, ...}, "findings": [{... "finding_id": "F-1732fdf97e2359cb", "taxonomy_code": "FIELD_MISSING", "severity": "HIGH", "change_type": "missing_in_b", ... "json_pointer": "/links/ORG-000080", ...}], "observations": [{... "side": "a", "pass_no": 1, "http_status": 200, ... "attempts": 1, ...}, ...], "canonical": {"a": {"code": "ORG-000007", "name": "tok:name:v1:2KEVZVBAC2LIAAFOPZXJYA4DSZ", "kind": "PERSON", "links": {...}}, "b": {...}}}
   ```

### How to read the result

The two records are not the services' raw answers. They are what the spec makes of them: the part the
spec selects, with side b's field names translated into side a's, so that the same field stands in the
same row. The raw answers are stored encrypted and no page, command or address shows them.

The colours are a reading aid. The table "Findings" is what the run judged.

A side that was not read in full has no record, and its column is empty. That is so for side b of
`ORG-000041`, which has no such record, and for side b of `ORG-000034`, whose answer was cut short.

`first outcome` and the badge differ when the second read changed the judgement. For the key
`ORG-000029` of the run of section 3 the first outcome is `MISMATCH` and the badge reads `DRIFT`.

Do not read `pass` as the number of reads. For this key in the batch run it is 2. For the same key in
a one-key compare, in the panel "Compared now" or on the key's page of the `one_shot` run, it is 1,
while `confirmation` reads `rechecked`, `revision` reads 2 and "Observations" lists four reads, two of
them of pass 2. The table "Observations" is the one to trust.

### Limits

- A key's page is addressed by the key as shown. Asked for with a raw customer number, the customer
  run of section 11 answered `{"detail": "key C00012003 not found"}`.
- The page is built from the stored answers each time. Not verified here: once a cycle's stored answers
  are deleted, on the day the line "Evidence retained until ..." names, the page can no longer be built.
- The API returns two facts about a read that the page does not show: whether a paged answer was read
  to its end, and whether the pages changed during the read.

### If something goes wrong

**`{"detail": "key ORG-000404 not found"}`**
The run does not hold that key. Check the key against the table "Results".

## 11. Masking

### What it is for

A comparison of real systems touches names, phone numbers and account numbers. DeltaSleuth shows such
a value as a token, a stand-in made from the value. The same value always gives the same token, on both
sides and in every run, so equal values still look equal and different values look different. Everyone
who reads results works with tokens, and nobody needs to handle the personal data behind them.

### Before you start

Section 1 for the steps below. Nothing has to be switched on: masking is always there. The spec decides
what is masked:

| Line of the spec | Effect | Linker spec | Customer spec |
|---|---|---|---|
| `key.class` | `identifier`: keys are shown as tokens. `business_code`: keys are shown as they are | `business_code` | `identifier` |
| `security.tokenize` | The fields that are personal data, each with its class, such as `name` or `phone` | 5 fields | 9 fields |
| `security.clear` | The fields that are shown as they are | 6 fields | 18 fields |
| `security.unlisted` | What happens to a field named in neither list | `tokenize` | `tokenize` |

The tokens are made with the tokeniser key, one of the secrets chapter 1 created.

### In the web UI

There is nothing to click: no page has a control that shows a raw value. What you can see is where
masking applies.

1. Click "Runs". In the panel "New run", pick `mock-customer-v1-vs-v2 1.0.0 (#2)` in "Spec", leave the
   rest as it is, press "Start" and wait for the badge `completed`.
2. Look at the table "Results". Every key reads `tok:identifier:v1:` and 26 characters.
3. Click a key. In "Canonical A and B (masked)" the rows `givenName`, `familyName` and `email` hold
   tokens such as `"tok:name:v1:U5BYV4FU3732OEYWTA7S6HYSNS"`. The row `birthDate` holds
   `"class:date_of_birth"`. The rows `segment` and `status`, and the rows under `consents`, are
   readable, because the spec lists them under `security.clear`.
4. Open a run of the linker pair for comparison. Its keys, such as `ORG-000007`, are readable; the row
   `name` of a record is a token.

### On the command line

The steps find the differences of one customer whose number you know, in a run that shows only tokens.

1. Run the customer pair.

   ```bash
   ds run /var/lib/deltasleuth/p0/customer.yaml
   ```

   You should see:

   ```text
   run R-35T7D3RZZJTA53D90MN5T1V0HK started
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 40, compared 40, match 36, different 4, indeterminate 0, cancelled 0, outstanding 0
     KNOWN_DIFF 2, MATCH 35, MISMATCH 2, NOT_FOUND_B 1
   ```

2. Compare the customer you know. The first line of the result starts with the key's token.

   ```bash
   ds compare C00012003 --spec /var/lib/deltasleuth/p0/customer.yaml
   ```

   You should see:

   ```text
   tok:identifier:v1:FRQO6URTWXKUCG2KMKKG274GYF: MISMATCH (DIFFERENT, rechecked)
     CRITICAL VALUE_MISMATCH value_changed /consents/MARKETING/granted
         A: True
         B: False
   run R-35T7D44AA0BHPKCBNEQ4EM00C0; observations a O-f75d98d29e761b60 b O-889dc0c2c9d2abe2
   ```

3. Use the token to ask the run of step 1 for that customer's differences.

   ```bash
   dsapi 'http://localhost:8000/v1/runs/<run id>/findings?key=<masked key>'
   ```

   You should see the same difference, stored in the batch run (shortened):

   ```text
   {"items": [{"key_seq": 2, "outcome": "MISMATCH", "confirmation": "rechecked", ... "taxonomy_code": "VALUE_MISMATCH", "severity": "CRITICAL", "change_type": "value_changed", ... "json_pointer": "/consents/MARKETING/granted", "value_a": true, "value_b": false, ... "key_masked": "tok:identifier:v1:FRQO6URTWXKUCG2KMKKG274GYF"}], "next_cursor": null}
   ```

In the web UI the token goes into the box `key` of the filter bar (section 9).

### How to read the result

A token reads `tok:<class>:v1:` followed by 26 characters. The class says what kind of value it stands
for: `identifier`, `name`, `email`, `phone`, `address`, `account_number`, `device_id`, and `free_text` or
`free_number` for a field the spec does not name. `v1` is the version of the tokeniser key.

Tokens are made from the raw value. Two spellings of one phone number therefore give two tokens: in the
run of section 3, the key `ORG-000018` holds one phone that side a writes as a national number and side
b in the international form. Its difference shows two tokens under "A" and "B", and the kind
`NORMALIZATION_ONLY`, because the spec's clean-up makes the two equal.

A date of birth shows only as `class:date_of_birth`. Not even whether two dates are equal can be seen.

A token is the same in every run of one installation: customer `C00012031` got the same token in
section 7 and in step 1. Another installation has another tokeniser key and gives other tokens.

**Who can see what.**

| Who | Sees |
|---|---|
| Anyone signed in, from the role `viewer` up | The masked results, key pages and reports, and the fields the spec declares `clear` as they are. A `viewer` token was served results, a key's page and a report |
| An operator or a spec author | The same. No role sees more |
| Nobody, through DeltaSleuth | The raw values. No page, command or address turns a token back into its value, and a token cannot be computed back |

The raw answers exist only encrypted, in the volume `evidence`. The raw keys exist only sealed, in the
database, because the worker needs them to ask the services.

### Limits

- A reveal of raw values is not built. The role `pii_reader` is reserved for one.
- A spec can show a field to every viewer by listing it under `security.clear`, and can show keys by
  giving them the class `business_code`. With single sign-on a second person must approve such a spec.
  With token sign-in nothing checks it; the run only carries the label of section 1.4.
- A key list written inside a spec is stored as written and is never deleted. Validating the customer
  spec says so: `warning: /key/source/keys [inline_keys_stored] inline keys of class identifier are
  stored in clear with the spec and never purged; pass them with 'ds run --keys'`.
- Tokens are stand-ins, not anonymous data. Whoever knows a value and may run `ds compare` can find its
  token, as step 2 does on purpose.
- The number of a customer inside the record (`customerId`) has another token than the key, because the
  record's field is masked under another class. The two cannot be matched by eye.

### If something goes wrong

**`{"detail": "key C00012003 not found"}`**
An address or a filter was given the raw key. Find the key's token with step 2 and use that.

## 12. Reports

### What it is for

A report is a file built from the database at the moment you ask, always masked. The CSV files are for
spreadsheets and scripts, the PDF is for people, and a summary tracks the checklist of bringing a new
pair in. Whoever has to pass results on, or keep them, uses them.

### Before you start

The batch run of the linker pair's 61 keys that section 3 made. From the command line, reports are
written into the volume and copied out; keep one folder for each run.

### In the web UI

1. Open the run: click "Runs", then the run's id, as in the first web UI step of section 7.
2. Find the row of buttons under the lines of text. After the run's controls come nine buttons named
   after the files: "report.pdf", "discrepancies.csv", "retry.csv", "coverage.csv", "normalization.csv",
   "root_causes.csv", "suppressed.csv", "graph.csv" and "readiness.csv".
3. Press one. The browser saves a file named after the run and the report, for example
   `R-35T7CT44ZW54C0C6CVH5H63JQ1-report.pdf`. The page shows no preview.
4. For a soak run, choose the cycle in the drop-down "Cycle" first; the buttons then give that cycle's
   reports.

Each download is written to the audit log (`report.download`). The summary of step 2 below is not
offered in the web UI.

### On the command line

1. Write the four standard reports of the run into a folder of its own in the volume.

   ```bash
   ds report <run id> --out /var/lib/deltasleuth/p0/<run id>
   ```

   You should see the four paths:

   ```text
   /var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/R-35T7CT44ZW54C0C6CVH5H63JQ1-discrepancies.csv
   /var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/R-35T7CT44ZW54C0C6CVH5H63JQ1-retry.csv
   /var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/R-35T7CT44ZW54C0C6CVH5H63JQ1-coverage.csv
   /var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/R-35T7CT44ZW54C0C6CVH5H63JQ1-report.pdf
   ```

2. Write the other reports, each named with `--kind`. `summary` stands for two files.

   ```bash
   ds report <run id> --out /var/lib/deltasleuth/p0/<run id> --kind normalization.csv --kind root_causes.csv --kind suppressed.csv --kind graph.csv --kind readiness.csv --kind summary
   ```

   You should see:

   ```text
   /var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/R-35T7CT44ZW54C0C6CVH5H63JQ1-normalization.csv
   /var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/R-35T7CT44ZW54C0C6CVH5H63JQ1-root_causes.csv
   /var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/R-35T7CT44ZW54C0C6CVH5H63JQ1-suppressed.csv
   /var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/R-35T7CT44ZW54C0C6CVH5H63JQ1-graph.csv
   /var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/R-35T7CT44ZW54C0C6CVH5H63JQ1-readiness.csv
   /var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/report.md
   /var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/report.html
   ```

3. Copy the folder out of the volume.

   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/<run id> p0-out/
   ```

   You should see the two copy lines:

   ```text
    deltasleuth-app-1 copy deltasleuth-app-1:/var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1 to p0-out/ Copying
    deltasleuth-app-1 copy deltasleuth-app-1:/var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1 to p0-out/ Copied
   ```

4. List it.

   ```bash
   ls p0-out/<run id>
   ```

   You should see eleven files:

   ```text
   R-35T7CT44ZW54C0C6CVH5H63JQ1-coverage.csv
   R-35T7CT44ZW54C0C6CVH5H63JQ1-discrepancies.csv
   R-35T7CT44ZW54C0C6CVH5H63JQ1-graph.csv
   R-35T7CT44ZW54C0C6CVH5H63JQ1-normalization.csv
   R-35T7CT44ZW54C0C6CVH5H63JQ1-readiness.csv
   R-35T7CT44ZW54C0C6CVH5H63JQ1-report.pdf
   R-35T7CT44ZW54C0C6CVH5H63JQ1-retry.csv
   R-35T7CT44ZW54C0C6CVH5H63JQ1-root_causes.csv
   R-35T7CT44ZW54C0C6CVH5H63JQ1-suppressed.csv
   report.html
   report.md
   ```

5. Print one, the list of keys to run again.

   ```bash
   cat p0-out/<run id>/<run id>-retry.csv
   ```

   You should see the run's four keys without a final judgement:

   ```text
   run_id,key,outcome,reason,side_a_state,side_a_reason,side_b_state,side_b_reason,attempts_a,attempts_b,detail,obs_a_id,obs_b_id
   R-35T7CT44ZW54C0C6CVH5H63JQ1,ORG-000029,DRIFT,b_converged,COMPLETE,,COMPLETE,,1,1,,O-58e26260db6f7ecf,O-284837eb8f416418
   R-35T7CT44ZW54C0C6CVH5H63JQ1,ORG-000034,INCONCLUSIVE,truncated,COMPLETE,,INCOMPLETE,truncated,1,1,b: reported total exceeds the cap of 50,O-9310e4ab90fbad52,O-f2f994a5e6992fa8
   R-35T7CT44ZW54C0C6CVH5H63JQ1,ORG-000046,INCONCLUSIVE,absence_unconfirmed,INCOMPLETE,absence_unconfirmed,COMPLETE,,1,1,a: not-found body matches no declared absence shape,O-e4de58577b1fce28,O-7b7aa61cad4b434b
   R-35T7CT44ZW54C0C6CVH5H63JQ1,ORG-000999,INCONCLUSIVE,absent_both,ABSENT,,ABSENT,,1,1,,O-03e22880110d60af,O-6b143429499d78e6
   ```

For a soak run, `--cycle` and a number picks the cycle (section 6).

### How to read the result

**The files.**

| File | One row is | What it is for |
|---|---|---|
| `discrepancies.csv` | One difference of a key that counts as different | The list to work through. A key that matches never appears. 6 rows for this run |
| `retry.csv` | One key without a final judgement | The keys to run again: `INCONCLUSIVE` and `DRIFT` keys, keys still owed their second read, and cancelled keys |
| `coverage.csv` | The whole cycle, in one row | The counters, for a script or a dashboard |
| `normalization.csv` | One difference of format only | To check once that what the spec treats as equal is equal. 1 row for this run |
| `root_causes.csv` | One cause | Section 8. 6 rows for this run |
| `suppressed.csv` | One difference that a known-difference rule covers | To see what the rules hide. Empty for the linker pair, 2 rows for the customer pair |
| `graph.csv` | A row of a graph comparison | Only its header for every run of this chapter; [chapter 6](06-tables.md) covers graph comparisons |
| `readiness.csv` | One ended cycle of the pair's runs, up to this one | The figures behind the pair's readiness, which [chapter 11](11-sign-off.md) covers |
| `report.pdf` | | The report to read and pass on |
| `report.md`, `report.html` | | The summary: the same text in two formats |

**The columns of `discrepancies.csv`, `normalization.csv` and `suppressed.csv`.** The three share one
layout.

| Column | Meaning |
|---|---|
| `run_id`, `cycle_no` | The run and the cycle |
| `key` | The key, masked |
| `outcome`, `verdict`, `confirmation` | The key's outcome, its verdict and whether it was read twice (section 7) |
| `taxonomy_code`, `severity` | The kind of difference and its severity |
| `path_pattern` | The place in the record, with `[*]` for any member of a list |
| `json_pointer` | The exact place in this key's record |
| `change_type`, `direction` | What changed, and which side holds the value (section 8) |
| `value_a_tok`, `value_b_tok` | Each side's value, masked. Empty when the side has none |
| `value_class` | A finer class of the difference |
| `member_key_tok` | The member of a list that the difference belongs to, masked |
| `root_cause_id`, `signature_hash` | The cause and the signature (section 8) |
| `rule_id`, `rule_classification` | The known-difference rule that covers the difference, and its class: `accepted`, `expected_by_design` or `bug` |
| `mask_id` | Always empty in this build |
| `obs_a_id`, `obs_b_id` | The ids of the first reads |
| `recheck_obs_a_id`, `recheck_obs_b_id` | The ids of the second reads. Empty when the key was read once |
| `spec_version` | The version of the spec the run used |

**The columns of `retry.csv`.**

| Column | Meaning |
|---|---|
| `run_id`, `key` | The run, and the key, masked |
| `outcome` | `INCONCLUSIVE`, `DRIFT`, the first outcome of a key that is owed its second read, or `CANCELLED` |
| `reason` | The reason of section 7; `recheck_owed` for a key owed its second read; the run's stop reason for a cancelled key |
| `side_a_state`, `side_a_reason`, `side_b_state`, `side_b_reason` | Each side's state and its reason |
| `attempts_a`, `attempts_b` | How many calls each side's read took |
| `detail` | A sentence that says more, for example `b: reported total exceeds the cap of 50` |
| `obs_a_id`, `obs_b_id` | The ids of the reads |

**The columns of `coverage.csv`.** It has 81 columns in one row. They come in groups.

| Columns | Meaning |
|---|---|
| `stratum` | `*`: the row is the whole cycle |
| `run_id`, `mode`, `spec_name`, `spec_version`, `spec_hash` | The run and the exact spec |
| `claim_level`, `time_policy` | `observed` and `live` for every run of this chapter: the keys were read live, one by one |
| `attempted`, `compared`, `matched`, `mismatched`, `indeterminate`, `cancelled`, `outstanding` | The counters of section 3 under other names: `attempted` is `total`, `matched` is `match`, `mismatched` is `different` |
| `mismatched_provisional` | Differences that were never read a second time, as in a file-mode run |
| `inconclusive`, `drift` | The two parts of `indeterminate` |
| `suppressed`, `absent_both`, `api_uncomparable`, `schema_drift`, `normalization_only_keys` | Keys with only known differences; keys neither side has; keys cut short by a service; keys touched by a change of a table's layout; keys with a format-only difference |
| `mismatch_rate`, `coverage` | `mismatched` divided by `compared`; `compared` divided by `attempted` |
| `reconciled` | `yes` when the counters add up |
| `outcome_...`, `inconclusive_...`, `drift_...`, `confirmation_...` | One count for each outcome, each reason and each kind of confirmation |
| `suppressed_by_rule`, `suppressed_by_mask` | Keys for each known-difference rule, as `rule=count` |
| `selected_fields`, `excluded_fields`, `field_patterns_compared` | The fields the spec compares; `*` is every field |
| `rows_...`, `columns_only_...`, `filter_...`, `engine_...`, `keys_repeated_...` | Filled for table and file sides only |

`root_causes.csv` is described in section 8.

**`report.pdf`.** Its first lines name the run, its mode, status and cycle, the spec with its content
hash, the two targets, the times, the day until which the stored answers are kept, the approval line of
section 1.4 and, for a run that stopped early, the stop reason. Eight sections follow:

1. "Root causes": the table of section 8.
2. "Coverage counters": the counters, in a grid.
3. "Labelled accuracy (observed)": "Not measured" unless a labelled sample exists, which
   [chapter 11](11-sign-off.md) covers.
4. "Trend against the previous run": each counter before, now, and the change.
5. "Pair readiness (observed)": the pair's readiness gates, which chapter 11 covers.
6. "Outcome changes across soak cycles": the keys of section 6; "Not a soak run." otherwise.
7. "Top findings": up to 25 differences, the most severe first.
8. "Annex: AI-generated, not evidence": the answers AI chat gave about the run; none here.

Read the line "Previous run R-..." before you trust the trend. The previous run is the newest earlier
run of the same stored spec that completed, of whatever kind. Here it was the one-key compare of
section 2, step 5, so the trend set 61 keys against 1.

**`report.md` and `report.html`.** The summary is headed "P0 contract report", P0 being the product's
name for bringing a pair in. It starts with a checklist of seven lines, each `pass`, `fail`, `pending`
or `manual`, then repeats the approval line, the counters, a summary of the spec, the root causes and
the format-only differences, and ends with the open items, here `4 unresolved key(s) (INCONCLUSIVE or
DRIFT) are listed in retry.csv`. The summary of a run that stopped early is headed "P0 contract report
(PARTIAL)": seen for a cancelled run.

### Limits

- `--out` must name a folder in the volume. Everything else in the container is read-only.
- The summary and the files of section 13 exist only on the command line.
- There is no file of the outcome changes of a soak run. They are in `report.pdf`, on the run's page
  and at the API (section 6).
- The summary's two files do not carry the run's id in their names. Keep one folder for each run.
- Not verified here: the runbook says that a cell a spreadsheet could take for a formula is written
  with a leading apostrophe, and that reports of a cycle can no longer be built once its stored answers
  are deleted.

### If something goes wrong

**`error: unknown report outcome_changes.csv; one of report.pdf, discrepancies.csv, retry.csv,
coverage.csv, normalization.csv, root_causes.csv, suppressed.csv, graph.csv, readiness.csv, summary`**
`--kind` takes only the names in the message.

**`error: Read-only file system: p0-out`**
`--out` named a folder of your machine. `ds` runs in a container: give a folder under
`/var/lib/deltasleuth/p0` and copy it out (step 3).

**`error: run R-NOPE not found`**
The run id is mistyped.

## 13. Checking the engine against people

### What it is for

A verdict is only as good as the spec behind it. To test that, DeltaSleuth picks a small sample of a
run's keys that covers every outcome and the main root causes, and writes it as a sheet. Reviewers look
at each key and fill in their own verdict. A score then compares the two. This is called adjudication,
and it is the acceptance test of a spec: bringing a pair in counts as done when at least 50 reviewed
keys agree with the engine, without exception.

### Before you start

A run that has ended with at least 50 keys: the batch run of section 3 has 61. Use the same folder in
the volume as for the run's reports, `/var/lib/deltasleuth/p0/<run id>`.

### In the web UI

With token sign-in, as chapter 1 sets it up, the web UI shows where adjudication would be and offers
nothing of it. Use the commands below.

1. Open the batch run of section 3, as in the first web UI step of section 7, and scroll down to the
   heading "Census, adjudication and evidence packs". The paragraph under it ends with "Evidence
   packs need SSO (auth.oidc); this deployment runs in tokens mode."
2. Look at the panel headed "Cycle 1" under that paragraph. It holds the line `Census: side a not
   covered; side b not covered`, the small heading "Adjudications" with "None." under it, and the
   small heading "Evidence packs" with "None." under it. It has no button.
3. Do not take the next "Draw" button for it. Further down, under the heading "Labelled accuracy", a
   second panel headed "Cycle 1" has the fields "Discrepant items", "Per root cause", "Accepted keys",
   "Agreed keys", "Truth source" and "As of" and a button "Draw", with every kind of sign-in. That is
   another feature, which [chapter 11](11-sign-off.md) covers. It draws no reviewer sheet.

With single sign-on, an operator gets a button "Draw" in the first of the two panels, a button
"Download sheet" in the row of each drawn sheet under "Adjudications", and a field "Upload reviewed
sheet" beside it; the sheet is scored once, when it is uploaded. Not verified here: this installation
has no single sign-on. [Chapter 11](11-sign-off.md) sets single sign-on up and goes through these
buttons step by step.

### On the command line

1. Draw the sample and write the sheet.

   ```bash
   ds adjudicate <run id> --out /var/lib/deltasleuth/p0/<run id>
   ```

   You should see:

   ```text
   selected 50 of 61 key(s): /var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/adjudication.csv
   ```

2. Copy the sheet out of the volume.

   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/<run id>/adjudication.csv p0-out/adjudication.csv
   ```

   You should see the two copy lines:

   ```text
    deltasleuth-app-1 copy deltasleuth-app-1:/var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/adjudication.csv to p0-out/adjudication.csv Copying
    deltasleuth-app-1 copy deltasleuth-app-1:/var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/adjudication.csv to p0-out/adjudication.csv Copied
   ```

3. Look at its first lines.

   ```bash
   head -3 p0-out/adjudication.csv
   ```

   You should see the header and two keys, the last two cells of each still empty:

   ```text
   key,engine_outcome,engine_verdict,confirmation,reason,top_findings,root_cause_id,selected_for,obs_a_id,obs_b_id,reviewer_verdict,reviewer_notes
   ORG-000053,MATCH,MATCH,provisional,,,,outcome MATCH,O-6beaac6b9a739eb2,O-b482ea3c7e450794,,
   ORG-000050,MATCH,MATCH,provisional,,,,outcome MATCH,O-87e2fbbb1e8881d6,O-d96164937424c5e6,,
   ```

4. Fill in the sheet. In a real review, a person opens each key's page (section 10), decides, and
   writes `MATCH`, `DIFFERENT` or `INDETERMINATE` into `reviewer_verdict`, in a spreadsheet saved as
   "CSV UTF-8". To see the scoring work without a review, this command copies the engine's own verdict
   into the reviewer's column. It is a stand-in, not a review.

   ```bash
   awk -F, 'BEGIN { OFS = "," } NR > 1 { $11 = $3 } { print }' p0-out/adjudication.csv > p0-out/adjudication-filled.csv
   ```

   It prints nothing.

5. Copy the filled sheet back, under the name the score expects.

   ```bash
   docker compose cp p0-out/adjudication-filled.csv app:/var/lib/deltasleuth/p0/<run id>/adjudication.csv
   ```

   You should see the two copy lines:

   ```text
    deltasleuth-app-1 copy p0-out/adjudication-filled.csv to deltasleuth-app-1:/var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/adjudication.csv Copying
    deltasleuth-app-1 copy p0-out/adjudication-filled.csv to deltasleuth-app-1:/var/lib/deltasleuth/p0/R-35T7CT44ZW54C0C6CVH5H63JQ1/adjudication.csv Copied
   ```

6. Score it.

   ```bash
   ds score --out /var/lib/deltasleuth/p0/<run id>
   ```

   You should see:

   ```text
   adjudicated 50 of 50; agreement 1.0000
   P0 exit: PASSED
   ```

7. See what a disagreement looks like: fill the sheet again, this time with a reviewer who calls the
   key `ORG-000007` a match.

   ```bash
   awk -F, 'BEGIN { OFS = "," } NR > 1 { $11 = $3 } $1 == "ORG-000007" { $11 = "MATCH" } { print }' p0-out/adjudication.csv > p0-out/adjudication-filled.csv
   ```

   It prints nothing.

8. Copy it back, as in step 5.

   ```bash
   docker compose cp p0-out/adjudication-filled.csv app:/var/lib/deltasleuth/p0/<run id>/adjudication.csv
   ```

   You should see the same two copy lines as in step 5.

9. Score it again.

   ```bash
   ds score --out /var/lib/deltasleuth/p0/<run id>
   ```

   You should see:

   ```text
   adjudicated 50 of 50; agreement 0.9800
     disagreement ORG-000007: engine DIFFERENT, reviewer MATCH
   P0 exit: NOT YET (1 disagreement(s) between reviewers and the engine)
   ```

### How to read the result

**The sheet.** One row is one key of the sample.

| Column | Meaning |
|---|---|
| `key` | The key, masked |
| `engine_outcome`, `engine_verdict`, `confirmation`, `reason` | What the run decided (section 7) |
| `top_findings` | Up to three of the key's differences, the most important first, for example `HIGH FIELD_MISSING missing_in_b $.links[*]` |
| `root_cause_id` | The key's main cause |
| `selected_for` | Why the key is in the sample: `outcome MATCH`, `root cause RC-...`, or `stratum` and a group of keys |
| `obs_a_id`, `obs_b_id` | The ids of the reads |
| `reviewer_verdict` | For the reviewer: `MATCH` (the two sides agree for this key, differences of format are fine), `DIFFERENT` (they disagree in a way that matters), or `INDETERMINATE` (what is there is not enough to decide) |
| `reviewer_notes` | For the reviewer: anything the next reader needs |

**The sample.** The choice is repeatable: the same run, limit and salt give the same keys. Every outcome
of the run gets at least one key. `MATCH` gets a share of its own, a fifth of the sample, because a
match that a person would call different is the mistake the test most needs to find. Each root cause
gets a key, the most severe causes first. The rest is filled from all groups in turn. For this run the
50 keys were 41 `MATCH`, 4 `MISMATCH`, 3 `INCONCLUSIVE`, 1 `DRIFT` and 1 `NOT_FOUND_B`.

**The score.** `adjudicated 50 of 50` counts the rows with a verdict. `agreement` is the share of them
on which reviewer and engine agree. `P0 exit: PASSED` needs three things: at least 50 keys with a
verdict, no disagreement, and at least one reviewed key for every outcome the run has. Otherwise the
line reads `NOT YET` with each reason, and each disagreement is listed by key. A disagreement means the
spec judges something otherwise than the business does: correct the spec, run again, and review again.

**The files.** The folder now holds `adjudication.csv` (the sheet), `adjudication.json` (which keys
were chosen and why, and what the sample could not hold) and `adjudication_score.json` (the score, with
the counts for each outcome). The summary of section 12 picks the score up: written again after a
passing score, its checklist line read `50 adjudicated keys agree with reviewers | pass | 50
adjudicated, agreement 1.0000`.

### Limits

- The web UI and the API do this only with single sign-on.
- The test needs 50 keys. The customer pair has 40, so its sheet can agree in full and still not pass:
  `P0 exit: NOT YET (40 key(s) adjudicated; the exit needs at least 50)`.
- Leave the engine's columns as they are, and keep each key once. The score checks both against the
  run.
- `--limit` and a number sets the size of the sample (50 when left out). `--salt` and a text draws
  another sample. `--cycle` and a number picks a cycle of a soak run.
- The `awk` shortcut of step 4 works only because no cell of this sheet holds a comma.

### If something goes wrong

**`error: /var/lib/deltasleuth/p0/<run id>/adjudication.csv already holds reviewer verdicts or notes;
score it, or force a new sheet (the reviewed one is archived)`**
`ds adjudicate` never writes over a sheet someone filled in. Add `--force` to put the filled sheet
aside as `adjudication.1.csv` and write a new, empty one; the old score is deleted with it.

**`  not represented: 4 root_causes; raise --limit`**
The sample was too small to hold a key for every cause. Provoked with `--limit 5`.

**`error: reviewer_verdict must be one of MATCH, DIFFERENT, INDETERMINATE (lines 2, 3, 4, ...)`**
A verdict is another word. The numbers are lines of the sheet.

**`error: row(s) for keys the run did not select (lines 3, 4, 5, ...)`**
The sheet in the folder is not the one drawn last, for example after a new draw with another limit.
Copy out the new sheet and fill that one in.

**`error: adjudication.csv is not UTF-8 text (a spreadsheet's plain 'CSV' export writes Windows-1252):
save it as 'CSV UTF-8' and score it again`**
The reviewed sheet was saved in another encoding. Provoked with one accented letter in a note, saved
as Latin-1. Save the sheet as "CSV UTF-8", copy it back and score again.

**`error: /var/lib/deltasleuth/p0/nothing-here holds no adjudication selection: adjudicate the run
first`**
`ds score` was given a folder in which `ds adjudicate` never wrote.

**`{"detail": "evidence packs need SSO (auth.oidc); this deployment runs in tokens mode"}`**
The API was asked to draw a sheet in an installation without single sign-on. Use `ds adjudicate`.

## 14. Listing runs

### What it is for

The list shows the runs of the installation, newest first, with their spec, mode, status and cycle. It
is how anyone who may read results finds a run to open.

### Before you start

Nothing.

### In the web UI

1. Click "Runs" in the bar at the top. It is also the page that opens after signing in.
2. Read the table under the panel "New run". Its columns are "Run" (the id, a link to the run's page),
   "Spec" (name and version), "Mode" (`batch`, `soak`, or `one_shot` for a one-key compare), "Status",
   "Cycle" and "Created".
3. Press "More" under the table, where it shows, for older runs. The list loads 50 at a time.

An installation without a run shows "None." in place of the table. The list has no search and no
filter.

### On the command line

1. List the newest runs. Without `--limit` the command prints 20.

   ```bash
   ds runs --limit 4
   ```

   You should see two lines for each run:

   ```text
   R-35T7D44AA0BHPKCBNEQ4EM00C0  mock-customer-v1-vs-v2 1.0.0  one_shot  completed  cycle 1  different 1 of 1
     Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   R-35T7D3RZZJTA53D90MN5T1V0HK  mock-customer-v1-vs-v2 1.0.0  batch  completed  cycle 1  different 4 of 40
     Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   R-35T7D321V6YJCYNX2CPXZXFWKN  mock-linker-unmapped-kind 1.1.0  batch  completed  cycle 1  different 58 of 61
     Unapproved spec version: clear was not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   R-35T7D2YCN35HD5PE04133QSEX7  mock-linker-legacy-vs-new 1.1.0  one_shot  completed  cycle 1  different 0 of 1
     Unapproved spec version: clear was not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   ```

2. Ask the API for the runs with one status. `ds runs` cannot filter.

   ```bash
   dsapi 'http://localhost:8000/v1/runs?status=cancelled&limit=1'
   ```

   You should see the run that section 4 cancelled last (shortened):

   ```text
   {"items": [{"run_id": "R-35T7CW4AZ0N914AQP00VQFFGB4", "spec_id": 4, "mode": "batch", "status": "cancelled", "params": {...}, "cycle": 1, "counters": {...}, ...}], "next_cursor": ...}
   ```

### How to read the result

The first line of a run holds its id, the spec's name and version, the mode, the status of section 3
and the cycle. `different 4 of 40` is the count of keys that are different, of all keys, in the last
cycle that ended; a run whose first cycle is still going has no such part. The second line is the
approval line of section 1.4.

`different 0` does not say that everything matched. A key that could not be compared, or that is owed
its second read, is not counted there: the run of section 3, step 5, reads `different 0 of 61` although
it compared only a few keys, eight here, and still owed one key its second read.

### Limits

- `ds runs` and the web UI list every run. The API also filters by `status`, by `spec_id` and by the
  schedule that started a run.
- Every one-key compare, from `ds compare` or from "Compare now", is a line of the list.
- The list shows no counts in the web UI; open the run for them.

### If something goes wrong

**`command not found: ds`** (zsh) or **`ds: command not found`** (bash)
The alias is not defined in this terminal. Define it again (section 1.1).

## Reference

- [RUNBOOK.md](../mvp/RUNBOOK.md): every `ds` command of this chapter, the table of outcomes, stopping
  a run early, what a run records and each report, reviewing the adjudication sheet, and a table of
  messages.
- [CONTRACTS.md](../mvp/CONTRACTS.md): the addresses of the API and what each screen holds.
- [DEPLOY.md](../mvp/DEPLOY.md): the command line in a Docker installation, with the volume `p0`, and a
  first run on the mocks.
- [AUTH.md](../mvp/AUTH.md): the roles, and who the command line acts as.
- [APPROVALS.md](../mvp/APPROVALS.md): why a run is labelled unapproved, and approvals with single
  sign-on.
- [RETENTION.md](../mvp/RETENTION.md): how raw values are encrypted and how long they are kept.
- [PACK.md](../mvp/PACK.md): adjudication on the run's page with single sign-on.
- [READINESS.md](../mvp/READINESS.md): the readiness line of a run and `readiness.csv`.

Back to [chapter 4](04-specs.md), writing a comparison spec. Next: [chapter 6](06-tables.md), comparing
database tables. The installation is the one of [chapter 1](01-install-docker.md) and its
[appendix](A1-install-by-hand.md).
