# 3. Your first comparison

This chapter is the first hour after [chapter 1](01-install-docker.md). You store the description of a
comparison, start it, read what it found, look at one record side by side, compare that record again,
and download the reports. Each step is shown in the web UI first and on the command line second.

Everything here uses the mocks, the practice services that chapter 1 started. Two of them play an old
and a new version of a customer service that hold the same 40 customers. A few differences are planted
in them on purpose, and the file `mocks/customer/expected.json` in the source lists every one, so you
can check what DeltaSleuth reports against what is really there. Nothing here touches a real system.

**How this chapter was checked.** Every command was run on 2026-10-05, on macOS
27.0.1 (Apple silicon) with Docker Desktop (Docker 29.1.3, Compose 2.40.3), in the order of this
chapter, on a fresh installation. The commands ran in a pseudo-terminal, the thing a terminal window
is to a command, so Docker's lines look here as they look in your terminal. The machine already ran a
DeltaSleuth installation, so the run was a second installation, as chapter 1 describes one, and the
outputs are printed here with the default names. Run ids, masked values, the ids that start with
`F-`, `O-` and `T-`, and times will differ on your machine. Counts, outcomes and field values will
not. The Hash of each spec and the ids that start with `RC-` were the same on installations with
different secrets. The web UI steps were written from the pages' source, and the request behind each
button was sent with curl: the numbers and texts quoted come from those answers. Not verified here:
the pages in a browser.

One thing changed on 2026-10-06: the app's container now sees the checkout's `mocks` and `examples`
folders (section 2). The commands below therefore name the two bundled specs by their path in the
checkout, where on 2026-10-05 they named a copy of each under `/var/lib/deltasleuth/p0`. With the paths,
`ds spec validate` of both specs was run again in an installation built from that source, and the
first-run document records a run of the customer spec with the counts printed here. Not verified here:
the other commands with the paths.

## Before you begin

1. Open a terminal in the installation's folder, `IntelliDiff`, and check that the three containers
   run.
   ```bash
   docker compose ps
   ```
   You should see three lines, one of them for the mocks:
   ```text
   NAME                  IMAGE                COMMAND                  SERVICE   CREATED              STATUS                        PORTS
   deltasleuth-app-1     deltasleuth:source   "ds serve --host 0.0…"   app       About a minute ago   Up About a minute (healthy)   127.0.0.1:8000->8000/tcp
   deltasleuth-db-1      postgres:16-alpine   "docker-entrypoint.s…"   db        About a minute ago   Up About a minute (healthy)   5432/tcp
   deltasleuth-mocks-1   deltasleuth:source   "sh -c 'python -m mo…"   mocks     About a minute ago   Up About a minute             8000/tcp
   ```
   If the line of `deltasleuth-mocks-1` is missing, start the mocks with
   `docker compose --profile mocks up -d` (chapter 1, section 6).
2. Define the short form `ds` in this terminal, as in chapter 1, section 5.
   ```bash
   alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
   ```
3. Check that the short form works.
   ```bash
   ds token list
   ```
   You should see two lines of Docker's, then the token of chapter 1. `<you>` is your user name.
   ```text
   [+] Creating 1/1
    ✔ Container deltasleuth-db-1  Running                                                         0.0s
   T-f68100c06e883639 (first-run)  spec_author  active  expires 2027-01-03T19:50:59+00:00  owner '<you>'  created by local:<you>@deltasleuth-app  last used 2026-10-05T19:51:09+00:00
   ```
4. Sign in at `http://localhost:8000` with the token of chapter 1, section 4. You see a bar with
   "Runs", "Readiness", "Specs" and more, above a page headed "Runs".

**How the outputs of this chapter are printed.** They were captured in a terminal 100 columns wide.
Three things hold for every command below.

- Docker puts the two lines of step 3, `[+] Creating 1/1` and `✔ Container deltasleuth-db-1  Running`,
  before the output of every `ds` command. They say that the database container already runs. The
  "You should see" blocks leave them out from here on. The time at the end of such a line sits at the
  right edge of your window, so the gap before it depends on the window's width.
- When a `ds` command ends with an exit status other than 0, one empty line follows its output.
- When the output does not go to a terminal but into a file or into another program, Docker writes
  plainer lines, ` Container deltasleuth-db-1  Running` without the heading, the tick and the time, as
  chapter 1 prints them. A warning can then also come after the line it stands before here: `ds spec
  validate` printed `ok:` before its `warning:` in five of six such runs.

## 1. What a comparison is

DeltaSleuth compares two sources that should hold the same records. It calls them **side a** and
**side b**. In the bundled examples side a is the old system and side b is the new one that should
agree with it. In this chapter side a is version 1 of the mock customer service and side b is
version 2.

A **key** is the value that identifies one record on both sides. Here it is a customer number such as
`C00012003`. DeltaSleuth asks both sides for the record of a key and compares the two answers.

A **spec**, short for comparison spec, is one YAML file that describes one pair of sides. It says how
to ask each side for the record of a key, how to bring the two answers into one common shape, which
fields to compare and how strictly, which differences are already known, and which values must never
be shown. A spec names its sides by **target** ids, here `customer-a` and `customer-b`. The addresses
behind those ids are in the targets file of chapter 1, `deploy/config/targets.yaml`, never in the spec.

A **run** is one execution of a spec over a set of keys. It has an id that starts with `R-`. Every key
of a run ends with exactly one **outcome**. A single difference inside a key, for example one field
with two values, is a **finding**, and findings that look alike are grouped into a **root cause**.

| Outcome | In plain words | Counted as |
|---|---|---|
| `MATCH` | Both sides answered in full, and nothing that matters differs. A difference of format only, such as upper and lower case, does not matter. | a match |
| `MISMATCH` | Both sides answered in full, and at least one real difference has no explanation. | different |
| `KNOWN_DIFF` | The sides differ, and every difference is covered by a rule in the spec that says it is known. | a match, or different when the rule calls the difference a bug |
| `NOT_FOUND_A`, `NOT_FOUND_B` | One side has the record and the other side says clearly that it has none. The letter is the side where it is missing. | different |
| `INCONCLUSIVE` | A side could not be read in full or reliably, so nothing can be said. A second column, "Why", gives the reason, for example `both_error`. | neither |
| `DRIFT` | The first read showed a difference. A second read, moments later, showed that a side had changed in between. | neither |
| `NOISE_ONLY` | A name you will see with the count 0. It stands for a key whose only differences lie in fields that a spec declares as noise, and this build refuses a spec that declares any. | a match |

The customer pair of this chapter gives the first four. Section 9 gives `INCONCLUSIVE` and `DRIFT`.
`NOISE_ONLY` cannot occur in this build, but the name is in the "Outcome" filter of a run's page, in
its "Counters" and in the file `coverage.csv`: a copy of the customer spec with a noise declaration
added was answered `not supported: noise masks`.

## 2. Store the customer spec

### What it is for

A spec must be stored in DeltaSleuth before anything can run it. Storing checks the file and gives it
an id. Whoever sets up a pair does this once for each version of its spec.

### Before you start

Nothing to switch on. You need the role `spec_author`, which the token of chapter 1 has. The spec is
the file `mocks/customer/spec.yaml` in the `IntelliDiff` folder, and its two targets are already in
the targets file.

### In the web UI

1. Click "Specs" in the bar. You see the heading "Specs", the word "None." because nothing is stored
   yet, and two panels: "Upload a spec" and "Draft a spec from sample pairs".
2. In "Upload a spec", press the file button (the browser names it, for example "Choose File") and
   choose `mocks/customer/spec.yaml`. The text of the file appears in the large box under the button.
3. Press "Validate". A table with one row appears under the buttons: Pointer `/key/source/keys`,
   Severity `warning`, Code `inline_keys_stored`, Message `inline keys of class identifier are stored
   in clear with the spec and never purged; pass them with 'ds run --keys'`. A warning stops nothing.
   A spec without any remark gets the words "No issues." beside the buttons and the word "None." under
   them, where the table would be.
4. Press "Save". Beside the buttons you read "Saved as spec #1.", and the table at the top of the page
   now has one row: Id `1`, Name `mock-customer-v1-vs-v2`, Version `1.0.0`, Hash `dadc627b4d0f`, the
   time under Created, nothing under Approval, and the links "Schedule…" and "Pre-check…".

### On the command line

`ds` runs inside a container, and a container does not see the files of your folder. An installation
built from the source, as chapter 1 makes it, shows the app two folders of the checkout all the same,
`mocks` and `examples`, read-only: `ds` reads a bundled spec by its path in the `IntelliDiff` folder. For
any other file the app has one folder for handing files in and out, `/var/lib/deltasleuth/p0`, which
every `ds` command sees too: a file of your own is copied there first ("If something goes wrong" shows
how).

1. Check the spec. `--targets` names the targets file as the container sees it. Without it the check
   does not notice a target that does not exist.
   ```bash
   ds spec validate mocks/customer/spec.yaml --targets /etc/deltasleuth/targets.yaml
   ```
   You should see the same warning as in the web UI, then `ok`:
   ```text
   warning: /key/source/keys [inline_keys_stored] inline keys of class identifier are stored in clear with the spec and never purged; pass them with 'ds run --keys'
   ok: spec.yaml is valid (1 warning(s))
   ```

No command only stores a spec: `ds spec` has `validate` and nothing else. The commands `ds run`
(section 3) and `ds compare` (section 6) store the file they are given before they use it. The first
time that makes a new stored spec. After that it is the same one, because a spec is stored once for
each content: `ds run` with this file used spec 1 again after the "Save" above.

### How to read the result

The Name and the Version come from the file's `metadata` block. The Hash is the start of a fingerprint
of the content: this file gave `dadc627b4d0f` on every installation of this guide's runs. The Id is
what a run names.

The warning says that this spec carries its 40 customer numbers inside the file, and that DeltaSleuth
keeps a stored spec as it is. That is harmless for mock data.

### Limits

- A stored spec cannot be changed or deleted, and the "Specs" screen cannot open one to show its text.
- A changed content is stored as a new row, also when its name and version stayed the same: the table
  then has two rows that differ only in Id and Hash. Comments and layout are not content. The file
  with a comment line added at its end, and the file with two empty lines before it, were answered
  with the same id `1` and the same Hash. The file with another `owner:` got a new row. Change
  `metadata.version` whenever you change a spec.
- Ids are not consecutive. Each `ds run` or `ds compare` of a file that is already stored uses one
  up, and so does a second "Save" of the same text. The second spec stored in this guide's run, in
  section 9, got `#6`.
- The web UI checks a spec against the server's own targets only: its targets file and the product's
  list ([DEPLOY.md](../mvp/DEPLOY.md), section 7).
- A spec is approved by a second person only under single sign-on. With token sign-in, as here, every
  run of this spec is labelled unapproved (section 3). That does not stop a run.

### If something goes wrong

**`invalid spec`** beside the buttons after "Save", with one row: `/sides/a/target`, `error`,
`unknown_target`, `target 'customer-x' is not in the registry`
The spec names a target that the targets file does not hold. Nothing was stored. Provoked here with a
copy of the file in which `customer-a` was changed to `customer-x`. Only "Save" writes the words
`invalid spec`. "Validate" of the same text writes nothing beside the buttons and shows two rows: the
warning of step 3 and this row. Use the file unchanged.

**Rows with the Code `schema.required`, such as `'apiVersion' is a required property`**
The text in the box is YAML but not a comparison spec: the one line `a: 1` gave nine rows, eight of
them with this Code. A text that is not YAML at all gets one row with the Code `yaml_syntax`, here
`while parsing a flow sequence; expected ',' or ']', but got '<stream end>' (line 2, column 1)`. In
both cases "Save" adds the words `invalid spec` beside the buttons and "Validate" does not. Choose the
file again.

**`error: / [document_unreadable] cannot read customer-x.yaml: No such file or directory (ds runs in a
container, where this path is /app/customer-x.yaml: a file or folder of the host must first be copied in,
with docker compose cp, or mounted)`**
You gave `ds` a file of your own folder, here a changed copy of the spec, which the container does not
see. Copy the file into the app's folder first, and give `ds` the path inside the container,
`/var/lib/deltasleuth/p0/customer-x.yaml`.
```bash
docker compose cp customer-x.yaml app:/var/lib/deltasleuth/p0/customer-x.yaml
```
You should see `[+] Copying 1/1` and a line that ends with `Copied` and a time.

**`error: /sides/a/target [unknown_target] target 'customer-x' is not in the registry`, then `error:
1 blocking issue(s) in customer-x.yaml`**
The same mistake as the first one, found by `ds spec validate` with `--targets`. Without `--targets`
the same file was answered `ok`.

## 3. Start a run and watch it finish

### What it is for

A run asks both sides for every key of a set and stores one result for each key. A key that differs is
read a second time a moment later, so that a difference is confirmed before it is reported. An
operator starts a run whenever the two sides should be checked.

### Before you start

The spec of section 2 is stored, and the mocks run. You need the role `operator`, which the token of
chapter 1 includes.

### In the web UI

1. Click "Runs" in the bar. You see the heading "Runs", the line "Open a run to see its differences
   and ask AI about them.", the panel "New run" and, below it, "None." because no run exists yet.
2. In "New run", "Spec" already shows `mock-customer-v1-vs-v2 1.0.0 (#1)` and "Mode" shows `batch`,
   which means every key once. Leave the box "Keys, one per line (none: the spec's key source; a table
   pair without one scans both tables)" empty: the run then takes the 40 keys listed in the spec.
3. Press "Start". The page changes to the run. Its heading is "Run", the run's id, and the status
   `running`. Further down a line counts the keys, for example `23/40 keys · done 23 · leased 2 ·
   pending 13 · recheck 2`: `pending` keys are waiting, `leased` keys are being read, and `recheck`
   keys wait for their second read. Under that line a count appears for each outcome met so far, for
   example `MATCH 23` and `MISMATCH 2`.
4. Wait. The page asks for the status again every 2 seconds by itself. After four or five seconds the
   status reads `completed` and the line reads `40/40 keys · done 40`.

While the status is `running`, the three tables further down, "Root causes", "Results" and "Findings",
read "None.", or "Results" holds the few rows that were stored when the page opened. The page loads
these tables when it opens and again when the status changes, not every 2 seconds. They fill in at
the moment the status turns `completed`.

### On the command line

1. Start a run of the spec file and wait for its end.
   ```bash
   ds run mocks/customer/spec.yaml
   ```
   You should see, after six or seven seconds:
   ```text
   {"id": 14, ... "action": "spec.create", "object": "1", ...}
   {"id": 15, ... "action": "create_run", "object": "R-35T79WJW852E56EBTFRM1HEAP5", ...}
   run R-35T79WJW852E56EBTFRM1HEAP5 started
   warning: spec 1 (mock-customer-v1-vs-v2 1.0.0) needs a four-eyes approval of clear, known_diffs; tokens mode has no approvers, so run R-35T79WJW852E56EBTFRM1HEAP5 is labelled unapproved
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 40, compared 40, match 36, different 4, indeterminate 0, cancelled 0, outstanding 0
     KNOWN_DIFF 2, MATCH 35, MISMATCH 2, NOT_FOUND_B 1
   ```

### How to read the result

`completed` means that every key was processed. It does not mean that the two sides agree: this run
is `completed` with 4 keys that differ.

On the page, four lines stand under the heading once the run has ended:

- `mock-customer-v1-vs-v2 1.0.0 · batch · cycle 1 · created ... by first-run (T-...) · finished ...`
  says which spec ran, how, and that your token started it.
- `Evidence retained until 2027-01-03 (retention standard)` says until when DeltaSleuth keeps what the
  two sides answered: 90 days.
- `Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has
  no approvers). Not sign-off evidence.` is expected here, although the page prints it in the colour
  of a warning. This spec shows some values unmasked and holds known-difference rules, which a second
  person should approve, and approving needs single sign-on. The run is complete all the same.
- `Pair readiness: not ready (1 of 5 gates)` is a link to the screen "Readiness", which judges whether
  a pair is ready for a cut-over. This one is not: the run has differences. Leave it for now.

In the terminal, the lines that start with `{"id":` are copies of audit records, as in chapter 1. `run
R-... started` gives the run's id. The line that starts `warning: spec 1` says the same as the third
line of the page, and it comes with every `ds run` and `ds compare` of this spec. The line about
another worker is not an error: the app container carries out the run, and `ds run` waits for it. The
last two lines are the counts, which section 4 explains.

### Limits

- To compare a few keys only, type them into the "Keys" box, one in each line, as they really are, for
  example `C00012003`. Three keys given that way made a run of three keys.
- "Mode" `soak` repeats a run in cycles and needs a setting this spec does not have. It answers `a
  soak run needs the spec's execution.cycleInterval`.
- `ds run ... --no-wait` starts the run and returns at once with `processed by 'ds worker' or 'ds
  serve'`. The app carries it out.
- The page of a running run has the buttons "pause" and "cancel". `ds` has no command for either.
  Not verified here: pressing them, because this run ends before one can. [Chapter 5](05-runs.md)
  goes on with runs.

### If something goes wrong

**Every key ends `INCONCLUSIVE`, and `ds run` prints `total 40, compared 0, match 0, different 0,
indeterminate 40, cancelled 0, outstanding 0` and `INCONCLUSIVE 40`**
The mocks do not run, so neither side answered. The status still reads `completed`, `ds runs` lists
the run with `different 0 of 40`, and `docker compose ps` shows two lines, not three. Start the mocks,
then start a new run.
```bash
docker compose --profile mocks up -d
```
You should see `[+] Running 3/3` and three lines under it. The line of the mocks reads `✔ Container
deltasleuth-mocks-1  Started`, or `Running` when the mocks ran already.

**"Results" and "Findings" say "None." while the key line already counts finished keys**
The run is still `running`, and the page loads these tables only when it opens and when the status
changes (see above). Wait for `completed`, or reload the page in the browser to see the rows stored so
far. Not verified here in a browser: this is written from the page's source and from the answers of
its requests, which held 0, 9, 25 and 40 result rows at successive moments of one run.

**`1 key(s) do not match key.pattern after key.normalize, or are over 1024 bytes or hold NUL
(positions 2)`** under "Start"
A line of the "Keys" box is not a customer number, which is `C` and eight digits. The message gives
the position of the line and never the key. No run was started.

**`error: set DS_OPERATOR to your user name (docs/mvp/AUTH.md §4.2)`**
`ds` was started through Docker without the name of who runs it, for example as `docker compose run
--rm app ds run ...`. A command that only reads, such as `ds runs`, works that way; `ds run` does not.
Use the alias of "Before you begin", which carries `-e DS_OPERATOR="$(id -un)"`.

**`command not found: ds`** (zsh) or **`ds: command not found`** (bash)
The alias is not defined in this terminal. Define it again ("Before you begin", step 2).

**`error: / [document_unreadable] cannot read ...: No such file or directory (ds runs in a container,
...)`**
See section 2.

## 4. Read the run

### What it is for

The page of a run answers three questions: how many keys differ, what kinds of difference there are,
and which keys and fields they are in. Anyone who may sign in can read it.

### Before you start

Nothing. Open a finished run: click "Runs", then the run's id in the column "Run".

### In the web UI

1. Read the counts under the progress line: `MATCH 35`, `MISMATCH 2`, `KNOWN_DIFF 2` and
   `NOT_FOUND_B 1`. Their order on the page can vary.
2. Click "Counters". A grid of labelled numbers opens. The ones to read first are `total 40`,
   `compared 40`, `match 36`, `different 4`, `indeterminate 0`, `mismatch_rate 0.1` and `coverage 1`.
   The grid holds about fifty more entries. Names with a dot, such as `by_outcome.MATCH 35` and
   `by_confirmation.rechecked 3`, split the same keys by outcome, reason, confirmation and rule. One
   entry, `reconciliation`, has a long line of JSON as its value: it is the product's own check that
   the counters add up. Skip both kinds for now.
3. Read the table "Root causes". It has four rows, the gravest first:

   | Root cause | Severity | Taxonomy | Path | Change | Keys | Findings |
   |---|---|---|---|---|---|---|
   | `RC-4b68f1b25392` | CRITICAL | VALUE_MISMATCH | `$.consents[*].granted` | value_changed (both) | 1 | 1 |
   | `RC-cd785916a3ea` | CRITICAL | MISSING_DOCUMENT | `$` | missing_in_b (a_only) | 1 | 1 |
   | `RC-a6fb0cb9c0a3` | HIGH | FIELD_MISSING | `$.accounts[*]` | missing_in_b (a_only) | 1 | 1 |
   | `RC-f180ff24af25` | MEDIUM | FIELD_MISSING | `$.preferences.marketing.sms` | missing_in_b (a_only) | 1 | 1 |

   A last column, "Sample keys", links to keys that have the cause.
4. Read the table "Results": 40 rows, one for each key, with the columns Key, Outcome, Why,
   Confirmation, A and B. The keys read `tok:identifier:v1:...` (section 5 says why). They stand in
   the order of the spec's key list, so row 3 is the third key, `C00012003`: `MISMATCH`, `rechecked`,
   `COMPLETE`, `COMPLETE`. Row 27 is `NOT_FOUND_B`, `rechecked`, `COMPLETE`, `ABSENT`.
5. Click the count `MISMATCH 2` above the tables. "Results" now shows only the two rows with that
   outcome. Click "clear" in the row of filters to see all rows again.
6. Read the table "Findings": 10 rows, one for each single difference, with the columns Key, Finding,
   Pointer, Change, Severity, Taxonomy, A, B, Class and Rule. The first row is the difference of row 3:
   Pointer `/consents/MARKETING/granted`, Change `value_changed`, Severity `CRITICAL`, A `true`, B
   `false`.
7. Click `RC-4b68f1b25392` in "Root causes". "Findings" now shows the one finding of that cause, and
   the field `root_cause_id` in the row of filters holds the id.
8. Click "clear", then choose `CRITICAL` under "Severity". "Findings" shows two findings. Click
   "clear" once more. Filters add up: a filter stays set until you click "clear", so `CRITICAL` chosen
   without the first "clear" would have left the one finding of step 7.

Below these tables the page has the sections "Sign-off", "Census, adjudication and evidence packs" and
"Labelled accuracy", and at the bottom right the words "AI is off". They belong to later work. Two of
them say that they need single sign-on.

### On the command line

1. List the runs, newest first.
   ```bash
   ds runs
   ```
   You should see two lines for each run. Here the run of the command line stands above the run of the
   web UI:
   ```text
   R-35T79WJW852E56EBTFRM1HEAP5  mock-customer-v1-vs-v2 1.0.0  batch  completed  cycle 1  different 4 of 40
     Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   R-35T79VQKZFJ9CTX2NFXRSDZ942  mock-customer-v1-vs-v2 1.0.0  batch  completed  cycle 1  different 4 of 40
     Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   ```

The command line has no tables to browse. Its counts are the two lines `ds run` printed, and the rows
are in the files of section 7. `ds runs` lists the latest 20 runs; `--limit` changes that number.

### How to read the result

**The counts.** 35 keys are `MATCH`. `match 36` is one more, because one of the two `KNOWN_DIFF` keys
is covered by a rule that accepts its difference. `different 4` is the 2 `MISMATCH` keys, the
`NOT_FOUND_B` key, and the other `KNOWN_DIFF` key, whose rule calls its difference a bug: a known bug
still counts. `mismatch_rate 0.1` is 4 of 40, and `coverage 1` says that all 40 keys could be compared.

**What the mocks really hold.** The planted differences in `mocks/customer/expected.json` are these,
and the run found each one:

| Key | Row | Outcome | What differs |
|---|---|---|---|
| `C00012003` | 3 | `MISMATCH` | A marketing consent is granted on side a and withdrawn on side b |
| `C00012007` | 7 | `MISMATCH` | Side a holds two accounts, side b one |
| `C00012027` | 27 | `NOT_FOUND_B` | Side b answers that it has no such customer |
| `C00012031` | 31 | `KNOWN_DIFF` | Segment `PRIVATE` on side a, `WEALTH` on side b: rule `KD-CP-0001`, expected by design |
| `C00012035` | 35 | `KNOWN_DIFF` | Side b leaves a marketing preference out: rule `KD-CP-0002`, a known bug |

**Confirmation.** `rechecked` means the key was read a second time and the difference was still
there. This spec rechecks `MISMATCH` and `NOT_FOUND` keys after one second: 3 keys here. `provisional`
means the key was read once.

**Findings that do not count.** Five of the ten findings have the Severity `INFO` and the Taxonomy
`NORMALIZATION_ONLY`: `PAPERLESS` against `paperless`, `VIP` against `vip`, a phone number written two
ways, a balance as text and as a number, a time with and without milliseconds. The spec says how to
bring such values into one form, so their four keys stay `MATCH`. Two findings carry a rule id and the
word `(suppressed)` in the column Rule: they belong to the two `KNOWN_DIFF` keys.

**Root causes.** Each cause here has one key, because each difference was planted once. On real data
one cause can cover many keys, and the table is then the place to start.

### Limits

- The page has no legend for the outcomes. Use the table of section 1.
- The filter "Outcome" narrows "Results" only. The other filters narrow "Findings" only.
- The filter `key` takes the masked key, not the customer number: `C00012003` given there finds
  nothing.
- "Results" and "Findings" show 100 rows and then a button "More". Not verified here: the button,
  because these runs have 40 and 61 rows.
- While a run is `running`, the page renews only its top part: the status, the key line and the
  counts. "Results" and "Findings" show what was stored when the page opened, and all three tables
  load in full when the run ends (section 3). "Counters" is empty, and no root cause exists, until the
  run ends.

### If something goes wrong

**Every row of "Results" reads `INCONCLUSIVE`, Why `both_error`, A `ERROR: error_a`, B `ERROR:
error_b`, and "Root causes" says "None."**
The mocks did not run when the run was made. See section 3.

**A filter shows fewer findings than this chapter says, for example one `CRITICAL` finding and not
two**
Another filter is still set, most often `root_cause_id` after a click on a root cause. Click "clear"
and set the filter again.

**The panel "Sign in" replaces the page**
A session ends after 30 minutes without use. Sign in again as in chapter 1, section 4. The runs are
kept. Not verified here: this was written from the pages' source and not provoked.

## 5. Open one key

### What it is for

The page of a key shows what the two sides answered for one record, field by field, with the
differing fields marked. It is where a person checks whether a reported difference is real.

### Before you start

Nothing. You need a finished run.

### In the web UI

1. On the page of the run, in the first row of "Root causes", click the key under "Sample keys". It is
   the key of row 3 in "Results", and clicking it there opens the same page.
2. You see a link back to the run, the heading "Key", the masked key and the outcome `MISMATCH`, a
   button "Compare now", and a grid of facts: `cycle 1`, `state done`, `pass 2`, `first outcome
   MISMATCH`, `confirmation rechecked`, `revision 2`, `side A COMPLETE`, `side B COMPLETE`, `why` with
   nothing beside it, and `suppressed 0`. `why` is filled only for a key that ended `INCONCLUSIVE` or
   `DRIFT`.
3. Under "Canonical A and B (masked)" read the two records side by side. Each row is one field, with
   the columns `path`, `A` and `B`. The record has 59 rows. A row such as `$ {14} {14}` is a group
   with 14 members on each side, and `[1]` is a list with one entry.
4. Tick "differences only". Five rows stay: the differing field and the groups it lies in.
   ```text
   path             A      B
   $                {14}   {14}
     consents       {1}    {1}
       MARKETING    [1]    [1]
         [0]        {3}    {3}
           granted  true   false
   ```
   The row `granted` has the colour that the legend beside the tick box gives to `value_changed`.
5. Read "Findings": one row, the same as in section 4, step 6.
6. Read "Observations": four rows, one for each read of a side. Pass 1 is the first read of sides A
   and B, pass 2 the second read. All four have HTTP `200`.

### On the command line

The command line has no command that shows a key of a finished run. `ds compare`, in section 6, reads
the key again and prints the fresh result. It prints the differing fields but not the two records.

### How to read the result

**Why most values look like `tok:name:v1:CU6UKHGN...`.** DeltaSleuth never shows a protected value.
It shows a stand-in, which it calls a token. This is not the API token you sign in with. The spec
decides what is protected, in its `security` block:

- A field listed under `tokenize` shows as `tok:<class>:v1:` and 26 characters. The class is the kind
  of value: `name`, `email`, `phone`, `address`, `account_number`.
- A date of birth shows only as `class:date_of_birth`.
- A field listed under `clear` shows as it is. Here these are, among others, `segment`, `status`,
  `createdAt` and the consent fields, which is why you can read `true` and `false` above.
- A field the spec does not list is masked too. That is why `customerId` reads `tok:free_text:...` and
  a balance reads `tok:free_number:...`.
- The key has the class `identifier` in this spec, so every key reads `tok:identifier:v1:...`.

The same value always gives the same stand-in, so equal values still look equal: `givenName` has the
same stand-in on side A and on side B, and a key has the same one in every run. The stand-ins are made
with the tokeniser key that chapter 1 created, so yours differ from the ones printed here. `ds` has no
command, and these pages have no button, that turns a stand-in back into the value.

**Finding a customer you know.** The row number in "Results" is the position in the spec's key list.
Or run `ds compare` with the customer number (section 6): its first line starts with the masked key.

**A whole record missing.** For the `NOT_FOUND_B` key of row 27, column B is empty in every row, and
"Observations" shows HTTP `404` for side B in both passes. For the key of row 7, one member of
`accounts` has values under A and nothing under B.

### Limits

- The page shows the two records after the spec brought them into the common shape, not the answers as
  the sides sent them.
- The marking is a reading aid. The table "Findings" is what counts.

### If something goes wrong

**`key C00012003 not found`**
The address of a key page ends with the masked key, not with the customer number. Open the key from a
table of the run and do not type its address.

## 6. Compare one key again now

### What it is for

After a fix on one side you want to know whether one record now agrees, without running everything
again. Comparing one key reads that record from both sides right now and gives a short verdict. If the
first read differs, the key is read once more at once to confirm.

### Before you start

The mocks run. You need the role `operator`.

### In the web UI

1. On the page of the key (section 5), press "Compare now". The button reads "Comparing…" for a
   moment.
2. A panel appears above the stored result, headed "Compared now (run R-...)" with the outcome
   `MISMATCH`. It holds the same four parts as the page: the facts, the two records, "Findings" and
   "Observations". Nothing changed in the mocks, so the result is the same as before, with one
   exception: the facts of the panel read `pass 1` where the stored result below reads `pass 2`. Both
   were read twice: each says `confirmation rechecked` and has four observations.
3. Click "Runs" in the bar. The list has a new first row with the Mode `one_shot`: every such compare
   is kept as a small run of its own.

### On the command line

1. Compare one customer number. `--spec` is the spec file.
   ```bash
   ds compare C00012003 --spec mocks/customer/spec.yaml
   ```
   You should see three audit lines and the warning of section 3, then the verdict:
   ```text
   tok:identifier:v1:DKJCTUGYN2IOOCDYJA3SQ66YNR: MISMATCH (DIFFERENT, rechecked)
     CRITICAL VALUE_MISMATCH value_changed /consents/MARKETING/granted
         A: True
         B: False
   run R-35T7A0BH6SKVQRECA52PR6KMD3; observations a O-8022027c3975e4f6 b O-8f37c1a34ea15017
   ```

### How to read the result

The first line is the masked key, the outcome and, in brackets, how the key counts (`MATCH`,
`DIFFERENT` or `INDETERMINATE`) and its confirmation. Each finding follows with its severity, kind,
change and field, and the value on each side. `True` and `False` are the values the page shows as
`true` and `false`. The last line names the small run that holds this result and the two reads.

A key that agrees prints one line and the run line: `C00012001` gave `...: MATCH (MATCH,
provisional)`. A difference covered by a rule ends with the rule: `C00012031` gave `MEDIUM
VALUE_MISMATCH value_changed /segment suppressed by KD-CP-0001`.

For a key that one side lacks, the two value lines mislead. `C00012027` gave `NOT_FOUND_B (DIFFERENT,
rechecked)`, then `CRITICAL MISSING_DOCUMENT missing_in_b /` with `A: <absent>` and `B: <absent>`,
although side a holds the record. Read the first line, or open the key in the web UI (section 5).

### Limits

- The web UI compares only a key that is already in a run, and always with that run's spec. For any
  other key use `ds compare`, or give the key in the "Keys" box of "New run" (section 3).
- Each compare adds a run to the list. A report made later counts it as the previous run of the spec
  (section 7).
- `ds compare --json` prints the result and the findings in full, without the two records.
- With `--fail-on-diff` the command ends with exit status 1 when the key counts as different.

### If something goes wrong

**The outcome is `INCONCLUSIVE`, and `ds compare` prints `... INCONCLUSIVE (INDETERMINATE,
provisional) reason both_error`**
The mocks do not run. Start them (section 3) and compare again.

**`error: 1 key(s) do not match key.pattern after key.normalize, or are over 1024 bytes or hold NUL
(positions 1)`**
The key is not a customer number of this spec, for example `12003`. A number of the right form that
no side holds, such as `C99999999`, is compared and ends `INCONCLUSIVE` with the reason `absent_both`.

## 7. Download the reports

### What it is for

A report is a file made from what a run stored, for people who do not sign in or who want to work in
a spreadsheet. Every report is masked like the pages. It is made anew each time you ask.

### Before you start

Nothing. You need a finished run.

### In the web UI

1. Open the run. Under the four lines of section 3 there is a row of nine buttons, each named after
   its file.
2. Press "report.pdf". The browser saves a file named after the run, here
   `R-35T79VQKZFJ9CTX2NFXRSDZ942-report.pdf`.
3. Press the other buttons in the same way. Each file is named `<run id>-<button>`.

### On the command line

`<run id>` is the id of the run, as "Runs", `ds run` or `ds runs` shows it.

1. Write the four usual reports into a folder of their own inside the container.
   ```bash
   ds report <run id> --out /var/lib/deltasleuth/p0/<run id>
   ```
   You should see four audit lines, then the files:
   ```text
   /var/lib/deltasleuth/p0/R-35T79VQKZFJ9CTX2NFXRSDZ942/R-35T79VQKZFJ9CTX2NFXRSDZ942-discrepancies.csv
   /var/lib/deltasleuth/p0/R-35T79VQKZFJ9CTX2NFXRSDZ942/R-35T79VQKZFJ9CTX2NFXRSDZ942-retry.csv
   /var/lib/deltasleuth/p0/R-35T79VQKZFJ9CTX2NFXRSDZ942/R-35T79VQKZFJ9CTX2NFXRSDZ942-coverage.csv
   /var/lib/deltasleuth/p0/R-35T79VQKZFJ9CTX2NFXRSDZ942/R-35T79VQKZFJ9CTX2NFXRSDZ942-report.pdf
   ```
2. Write other reports by name. `--kind` can be given several times.
   ```bash
   ds report <run id> --out /var/lib/deltasleuth/p0/<run id> --kind normalization.csv --kind root_causes.csv --kind suppressed.csv --kind readiness.csv --kind graph.csv
   ```
   You should see five audit lines and five more files, listed in the same way.
3. Make a folder on your machine for the copies. Git ignores `p0-out`.
   ```bash
   mkdir -p p0-out
   ```
4. Copy the run's folder out of the container.
   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/<run id> p0-out/
   ```
   You should see `[+] Copying 1/1` and a line that ends with `Copied` and a time.
5. List the copies, one in each line.
   ```bash
   ls -1 p0-out/<run id>
   ```
   You should see:
   ```text
   R-35T79VQKZFJ9CTX2NFXRSDZ942-coverage.csv
   R-35T79VQKZFJ9CTX2NFXRSDZ942-discrepancies.csv
   R-35T79VQKZFJ9CTX2NFXRSDZ942-graph.csv
   R-35T79VQKZFJ9CTX2NFXRSDZ942-normalization.csv
   R-35T79VQKZFJ9CTX2NFXRSDZ942-readiness.csv
   R-35T79VQKZFJ9CTX2NFXRSDZ942-report.pdf
   R-35T79VQKZFJ9CTX2NFXRSDZ942-retry.csv
   R-35T79VQKZFJ9CTX2NFXRSDZ942-root_causes.csv
   R-35T79VQKZFJ9CTX2NFXRSDZ942-suppressed.csv
   ```

The eight CSV files written this way were the same, byte for byte, as the ones the buttons' requests
gave.

### How to read the result

| File | What it holds | In this run |
|---|---|---|
| `report.pdf` | A summary to read or pass on: the run and its spec, then the root causes, the counters, labelled accuracy, the trend against the previous run, pair readiness, outcome changes of a soak, the top findings, and an annex for AI chat answers | 3 pages |
| `discrepancies.csv` | One row for each difference that counts, with the key, its outcome, how it counts, the field, both masked values and the root cause | 4 rows |
| `retry.csv` | The keys worth reading again: `INCONCLUSIVE` and `DRIFT` keys with their reason | the header only |
| `coverage.csv` | One row with the run's counters, and one column for each outcome and reason | 1 row |
| `normalization.csv` | The differences of format only, which did not count | 5 rows |
| `root_causes.csv` | The table "Root causes", with a few more columns | 4 rows |
| `suppressed.csv` | The differences covered by a known-difference rule, with the rule and its kind | 2 rows |
| `graph.csv` | Rows only for a graph comparison, which this is not | the header only |
| `readiness.csv` | One row for each finished batch run of this spec's name up to this one, with the figures behind the line "Pair readiness" | 1 row |

`discrepancies.csv` has a column `verdict`, the "counted as" of section 1, which no page shows. Its
fourth row is the `KNOWN_DIFF` key with the rule `KD-CP-0002`, `bug` and the verdict `DIFFERENT`. The
other `KNOWN_DIFF` key is only in `suppressed.csv`, with `expected_by_design` and `MATCH`.

The PDF of the first run of a spec says under "4. Trend against the previous run": `No earlier
completed run of this spec to compare with.` A later run shows a table of each counter before and now.

### Limits

- A report is a download. No page shows one.
- `ds report` writes inside the container. Only `/var/lib/deltasleuth/p0` is kept and can be copied
  out. A folder under `/tmp` is accepted, and its files are gone when the command ends.
- The trend in the PDF takes the latest finished run of the same spec as "previous", and a compare of
  one key is such a run. After section 6, the PDF of the next run (section 8) named the compare of one
  key as `Previous run` and set `total 1` against `total 40`. Read the trend only when the run before
  was a full one.
- `--kind summary` writes two more files, `report.md` and `report.html`, a checklist for taking a pair
  into use. The web UI does not offer them.
- Every download and every `ds report` is written to the audit log.

### If something goes wrong

**`error: Read-only file system: reports`**
`--out` named a folder outside `/var/lib/deltasleuth/p0`, here the relative name `reports`. Give a
path under `/var/lib/deltasleuth/p0`.

**`error: run R-00000000000000000000000000 not found`**
The run id is mistyped. Copy it from `ds runs`.

**`error: unknown report bogus.csv; one of report.pdf, discrepancies.csv, retry.csv, coverage.csv,
normalization.csv, root_causes.csv, suppressed.csv, graph.csv, readiness.csv, summary`**
A name after `--kind` is not a report. The line lists the names.

## 8. The same run from the command line, from start to finish

### What it is for

Here are the commands of sections 2 to 7 in one sequence, for a reader without a browser at hand and
as the pattern for a script. It adds one thing: an exit status that says whether keys differ.

If you typed the commands of sections 2 to 7, only steps 2 to 5 are new to you. Steps 1 and 6 to 9
repeat sections 2 and 7, and you can skip them.

### Before you start

A terminal in the `IntelliDiff` folder with the alias `ds` (see "Before you begin"). The mocks run.

### In the web UI

Sections 2 to 7 are the same work in the web UI.

### On the command line

1. Check the spec.
   ```bash
   ds spec validate mocks/customer/spec.yaml --targets /etc/deltasleuth/targets.yaml
   ```
   You should see, as its last line:
   ```text
   ok: spec.yaml is valid (1 warning(s))
   ```
2. Run it, and ask for exit status 1 when a key differs.
   ```bash
   ds run mocks/customer/spec.yaml --fail-on-diff
   ```
   You should see two audit lines, then the lines of section 3 in the same order, then one empty
   line:
   ```text
   run R-35T7A3ZBSY6Y6RSQ9JHEYFNMFH started
   warning: spec 1 (mock-customer-v1-vs-v2 1.0.0) needs a four-eyes approval of clear, known_diffs; tokens mode has no approvers, so run R-35T7A3ZBSY6Y6RSQ9JHEYFNMFH is labelled unapproved
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 40, compared 40, match 36, different 4, indeterminate 0, cancelled 0, outstanding 0
     KNOWN_DIFF 2, MATCH 35, MISMATCH 2, NOT_FOUND_B 1
   ```
3. Print the exit status of that command. Do this before any other command.
   ```bash
   echo $?
   ```
   You should see `1`, because 4 keys differ. Without `--fail-on-diff` the status is `0`.
   ```text
   1
   ```
4. Show the newest run.
   ```bash
   ds runs --limit 1
   ```
   You should see:
   ```text
   R-35T7A3ZBSY6Y6RSQ9JHEYFNMFH  mock-customer-v1-vs-v2 1.0.0  batch  completed  cycle 1  different 4 of 40
     Unapproved spec version: clear, known_diffs were not approved by a second person (tokens mode has no approvers). Not sign-off evidence.
   ```
5. Look at one of the differing customers.
   ```bash
   ds compare C00012007 --spec mocks/customer/spec.yaml
   ```
   You should see, after three audit lines and the warning:
   ```text
   tok:identifier:v1:4L2TIN55DOU2NOUQKVU6W27F35: MISMATCH (DIFFERENT, rechecked)
     HIGH FIELD_MISSING missing_in_b /accounts/tok:account_number:v1:LHFF25IIQUB7HIAV6EJY6B4PHT
         A: {"accountNo": "tok:account_number:v1:BRVDQMCICWAWTWHMC63KSQ…
         B: <absent>
   run R-35T7A4GE1TE7DWQGEEYDYM4WTJ; observations a O-d8e4e9c1260a288e b O-0c8ba2ef4a90c863
   ```
6. Write the reports of the run of step 2. `<run id>` is the id that step 2 printed.
   ```bash
   ds report <run id> --out /var/lib/deltasleuth/p0/<run id>
   ```
   You should see four audit lines and four file names, the last one:
   ```text
   /var/lib/deltasleuth/p0/R-35T7A3ZBSY6Y6RSQ9JHEYFNMFH/R-35T7A3ZBSY6Y6RSQ9JHEYFNMFH-report.pdf
   ```
7. Make the folder for the copies.
   ```bash
   mkdir -p p0-out
   ```
8. Copy the reports out.
   ```bash
   docker compose cp app:/var/lib/deltasleuth/p0/<run id> p0-out/
   ```
   You should see `[+] Copying 1/1` and a line that ends with `Copied` and a time.
9. List them.
   ```bash
   ls -1 p0-out/<run id>
   ```
   You should see:
   ```text
   R-35T7A3ZBSY6Y6RSQ9JHEYFNMFH-coverage.csv
   R-35T7A3ZBSY6Y6RSQ9JHEYFNMFH-discrepancies.csv
   R-35T7A3ZBSY6Y6RSQ9JHEYFNMFH-report.pdf
   R-35T7A3ZBSY6Y6RSQ9JHEYFNMFH-retry.csv
   ```

### How to read the result

Step 5 shows a second kind of finding. `missing_in_b` on a member of `accounts` says that side a has an
account that side b lacks. `A:` prints the start of that account, masked, and `B: <absent>` says that
side b has nothing there.

The exit status of `ds run` and `ds compare` was `0` when the command worked, `1` with
`--fail-on-diff` when keys differ, `2` when the command was refused with a line that starts `error:`,
and `130` when `ds run` was stopped with Ctrl-C.

### Limits

- `ds` acts on the database directly and not through the web address, so it needs no token. Whoever
  can run it in this folder can do everything. It records the name in `DS_OPERATOR`, which the alias
  sets to your user name.
- `--fail-on-diff` looks only at keys that count as different, so its `0` is not proof that the sides
  agree. With the mocks stopped, the command of step 2 printed `total 40, compared 0, match 0,
  different 0, indeterminate 40, cancelled 0, outstanding 0` and `INCONCLUSIVE 40`, and its exit
  status was `0`. A script must read `compared` in the counts as well.
- Ctrl-C while `ds run` waits ends the command, not the run. Pressed during a run of the linker spec
  of section 9, it printed `^Cinterrupted`, the exit status was `130`, and the app finished the run
  about four seconds later: `ds runs` then listed it as `completed`. A run itself is stopped with the
  "cancel" button on its page (section 3, Limits).

### If something goes wrong

The messages are those of sections 2, 3, 6 and 7, and these two.

**The exit status is `0` with `--fail-on-diff`, and the counts say `compared 0`**
No side answered, so no key counts as different (see Limits). Start the mocks (section 3) and run
again.

**`^Cinterrupted`**
You pressed Ctrl-C while `ds run` waited. The run goes on in the app. Find it with `ds runs`.

## 9. A second exercise: the linker pair

### What it is for

The second mock pair, in `mocks/linker`, is an old and a new version of a service that answers which
entities are linked to a given one. Its spec declares its 61 keys, such as `ORG-000007`, as business
codes that may be shown, so this run shows its keys as they are. It also gives the two outcomes that
the customer pair lacks, `DRIFT` and `INCONCLUSIVE`.

### Before you start

The linker mocks remember. They count the calls for each key and answer some keys differently the
first time: for `ORG-000029`, side b gives an out-of-date answer once and the current one from then on.
So the first run after the mocks started gives one set of counts, and every later run another. The
mocks forget their counts when they start again.

Restart the mocks before each of the two exercises below, the one in the web UI and the one on the
command line, so that each is a first run. Without the second restart, the run on the command line
would be the second run and would print the counts of its step 3.

1. Restart the mocks.
   ```bash
   docker compose restart mocks
   ```
   You should see, after about a second:
   ```text
   [+] Restarting 1/1
    ✔ Container deltasleuth-mocks-1  Started                                                      1.2s
   ```

The customer pair has no such memory: its runs gave the same counts each time.

### In the web UI

1. On "Specs", choose `mocks/linker/spec.yaml` with the file button and press "Validate". You read
   "No issues." beside the buttons and "None." under them.
2. Press "Save". You read "Saved as spec #" and a number. It was `6` in this guide's run, after the
   four `ds run` and `ds compare` commands of sections 3, 6 and 8: each of them used an id up
   (section 2, Limits). Yours differs if you typed fewer or more of them.
3. Click "Runs". In "New run", "Spec" now shows `mock-linker-legacy-vs-new 1.1.0 (#6)`, with your
   number, because the list has the newest spec first. Press "Start".
4. Wait about five seconds. The run ends `completed` with `61/61 keys · done 61` and the counts
   `MATCH 52`, `MISMATCH 4`, `DRIFT 1`, `NOT_FOUND_B 1` and `INCONCLUSIVE 3`.

### On the command line

1. Restart the mocks, also when you did so before the web UI steps.
   ```bash
   docker compose restart mocks
   ```
   You should see the two lines of "Before you start".
2. Run the linker spec.
   ```bash
   ds run mocks/linker/spec.yaml
   ```
   You should see, after two audit lines, these lines with your spec's number in place of `6`:
   ```text
   run R-35T7B500V5FRKNPFZKPFJ4A7E7 started
   warning: spec 6 (mock-linker-legacy-vs-new 1.1.0) needs a four-eyes approval of clear; tokens mode has no approvers, so run R-35T7B500V5FRKNPFZKPFJ4A7E7 is labelled unapproved
   another worker is processing keys (a ds serve, ds worker or ds run on this database): it does the work, and this one takes over only if it stops
   completed
     total 61, compared 57, match 52, different 5, indeterminate 4, cancelled 0, outstanding 0
     DRIFT 1, INCONCLUSIVE 3, MATCH 52, MISMATCH 4, NOT_FOUND_B 1
   ```
3. Run it a second time, without a restart, to see the mocks' memory at work.
   ```bash
   ds run mocks/linker/spec.yaml
   ```
   You should see, as the last three lines:
   ```text
   completed
     total 61, compared 58, match 53, different 5, indeterminate 3, cancelled 0, outstanding 0
     INCONCLUSIVE 3, MATCH 53, MISMATCH 4, NOT_FOUND_B 1
   ```

### How to read the result

In "Results" of a first run, four rows show the new outcomes:

| Key | Outcome | Why | A | B |
|---|---|---|---|---|
| `ORG-000029` | `DRIFT` | `b_converged` | `COMPLETE` | `COMPLETE` |
| `ORG-000034` | `INCONCLUSIVE` | `truncated` | `COMPLETE` | `INCOMPLETE: truncated` |
| `ORG-000046` | `INCONCLUSIVE` | `absence_unconfirmed` | `INCOMPLETE: absence_unconfirmed` | `COMPLETE` |
| `ORG-000999` | `INCONCLUSIVE` | `absent_both` | `ABSENT` | `ABSENT` |

`b_converged` says that side b differed at the first read and agreed at the second. `truncated` says
that side b cut its list short. `absence_unconfirmed` says that side a answered "not found" in a form
its target does not declare as reliable. `absent_both` says that neither side has the key. These four
keys are the `indeterminate 4`, and the file `retry.csv` of this run lists exactly them. `different 5`
is the four `MISMATCH` keys and the `NOT_FOUND_B` key. The file `mocks/linker/expected.json` explains
every planted key.

In the second run of step 3, the first run had used up the one out-of-date answer for `ORG-000029`. The
key is then a plain `MATCH`: `match 53`, `indeterminate 3` and no `DRIFT`.

### Limits

- Neither the web UI nor `ds` can reset the mocks. Restarting their container is the way.
- A restart of the mocks takes about a second. Not verified here: a restart while a run is under way.

### If something goes wrong

**`MATCH 53` and no `DRIFT` where this section prints `MATCH 52` and `DRIFT 1`**
The run was not the first one since the mocks started: an earlier run of the linker spec, in the web
UI or on the command line, used up the first answers. Restart the mocks ("Before you start") and run
again.

**Every key ends `INCONCLUSIVE`**
The mocks do not run. See section 3.

## Where to go next

- [Chapter 4, Writing a comparison spec](04-specs.md): the spec itself, for a pair of your own.
- [Chapter 5, Runs in depth](05-runs.md): more about runs than this first one showed.
- [Chapter 6, Comparing database tables](06-tables.md): database tables as the two sides, in place of
  services.

## Reference

- [RUNBOOK.md](../mvp/RUNBOOK.md): the outcomes, verdicts and counters; every report and what it holds;
  the `ds` commands and their exit statuses; the mock pairs.
- [DEPLOY.md](../mvp/DEPLOY.md): a first run through the API, and the `p0` folder that carries files
  in and out of the container.
- [CONTRACTS.md](../mvp/CONTRACTS.md): the screens of the web UI and the API routes behind them.
- [APPROVALS.md](../mvp/APPROVALS.md): why a run is labelled unapproved, and what approving needs.
- [RETENTION.md](../mvp/RETENTION.md): how long a run's evidence is kept, and the keys behind masking.
- [READINESS.md](../mvp/READINESS.md): the line "Pair readiness" and the file `readiness.csv`.
- [AUTH.md](../mvp/AUTH.md): the roles `viewer`, `operator` and `spec_author`.
