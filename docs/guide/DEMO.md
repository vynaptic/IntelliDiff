# Demo script: 15 to 30 minutes

A presenter's script on the bundled practice data. The core takes 15 minutes; the add-ons bring it to 30. Every step was run and clicked on a fresh installation.

For people who decide on a cut-over, a stronger order in the same time: open with add-on D (the cut-over brief answers "can we cut over, and how do we know"), show core steps 2 to 5 as the evidence behind its figures, then add-on G, on your own two services if they are set up (last section). Add-on H was tried with a scripted model only: rehearse it on your model, or leave it out.

## Prepare

1. The day before, on the machine you present from: do [Install](README.md#install) and steps 1 to 3 of [Compare two database tables](README.md#compare-two-database-tables). For add-on C, add the schedule of [Pre-checks, the schema guard and schedules](README.md#pre-checks-the-schema-guard-and-schedules), step 4.
2. Just before the demo, in the `IntelliDiff` folder with the `ds` of Install, step 4: start everything and rehearse one run. The last line is `KNOWN_DIFF 2, MATCH 35, MISMATCH 2, NOT_FOUND_B 1`.
   ```bash
   docker compose --profile mocks up -d
   docker start deltasleuth-practice-db
   ds run mocks/customer/spec.yaml
   ```
3. Open three things: a browser at `http://localhost:8000`, signed in (Install, steps 2 and 3) and on the page "Runs"; a terminal in the `IntelliDiff` folder; the file `mocks/customer/spec.yaml` in an editor. The page asks for the token again after 30 idle minutes, so sign in shortly before you start.

## Core: 15 minutes

| # | Min | Do | Say |
|---|---|---|---|
| 1 | 1 | Show the page "Runs". | "When a system is replaced, someone must show that the new one holds what the old one holds. DeltaSleuth reads the same records from both and reports every difference, without knowing how either is built." |
| 2 | 2 | In "New run", choose `mock-customer-v1-vs-v2 1.0.0` under "Spec" and press "Start". Wait about five seconds for `completed`. Point at `MATCH 35`, `MISMATCH 2`, `KNOWN_DIFF 2`, `NOT_FOUND_B 1`. If someone asks what an outcome means, open "What the outcomes mean" under the counts. | "An old and a new customer service hold the same 40 customers, with differences planted on purpose. 35 agree, 2 differ without an explanation, 1 is missing on the new side, 2 differ in a way the spec declares as known." |
| 3 | 2 | Point at the table "Root causes", then at "Findings". | "Differences that look alike are grouped into one root cause, the gravest first, so on real data this table is where to start." |
| 4 | 2 | In the first row of "Root causes", click the key under "Sample keys". Tick "differences only". | "One customer, side by side. One field differs: a marketing consent is granted on the old side and withdrawn on the new one. Names, addresses and the customer number show only as stand-ins, so personal data never reaches the screen or a report." |
| 5 | 1 | Click the link "Run R-..." back to the run, press "report.pdf" and open the saved file. | "The same result as a PDF to pass on. The other buttons give CSV files: the list to work through, the keys to retry, the root causes." |
| 6 | 2 | In the editor, scroll `mocks/customer/spec.yaml`: `key`, `sides`, `compare`, `knownDiffs`, `security`. | "Everything that was compared is in this one file: how to ask each side, what counts as equal, which differences are already known and why, and what must be masked. It is reviewed like code and stored with each run." |
| 7 | 3 | Click "Runs", choose `practice-customers 1.0.0` under "Spec", press "Start". Point at the line that starts `Table scan (done)`, then at "Root causes". | "The same works on two database tables. One scan read 1,000 rows on the old side and 990 on the new one, found 985 identical and reported the other 16 keys: 11 missing on the new side, 1 only there, 4 with different values." |
| 8 | 1 | In the terminal: `ds run mocks/customer/spec.yaml --fail-on-diff`, then `echo $?`, which prints `1`. | "The same from the command line. With this option the command fails when keys differ, so a pipeline can stop on it." |
| 9 | 1 | Show the table [Where is it](README.md#where-is-it). | "This is what else is there when you need it: your own services, MySQL, Oracle and files, pre-checks, schedules, notifications, an AI chat, an MCP server for your own agents, and sign-off." |

## Add-ons: pick some to reach 30 minutes

| # | Min | Do | Say |
|---|---|---|---|
| A | 3 | Click "Pre-checks". Under "New table pre-check" choose `practice-customers 1.0.0` in "Specs" and press "Pre-check". Point at the verdict `rows differ: side a 1,000, side b 990 (10 fewer on side b)`. | "Before any row is compared, a pre-check counts and adds up both tables in seconds and says where to look first." |
| B | 4 | In the terminal, change a column's type (command below). Click "Runs", start `practice-customers 1.0.0` again: the run reads `paused` and names the column. Press "Resume (accept the changes)": it completes. | "A table changed since the last run. The schema guard holds the run before a single row is read, says what changed, and a person decides. A changed table never silently changes what was compared." |
| C | 2 | Click "Schedules". Point at the row `nightly-customers` and its next time, then at the panel "New schedule". | "A stored comparison runs every night by itself. During a migration this shows each morning whether the two systems still agree." |
| D | 3 | Click "Readiness". Point at each pair's state and gates, then click a pair's name, then "Cut-over brief". Point at the figures of the newest complete cycle, the trend and the root-cause groups. | "For each pair, named gates say what is still missing before sign-off. And this is one screen for the cut-over meeting: the gates, what the newest complete cycle measured, and which root causes opened, stayed open or closed. It restates what was measured; it adds no score and no forecast." |
| E | 2 | In the terminal: `ds compare C00012003 --spec mocks/customer/spec.yaml`. Point at `/consents/MARKETING/granted`, `A: True`, `B: False`. | "After a fix on one side, one record is compared again in a second, without a new run." |
| F | 2 | In the terminal: `ds audit verify`, which prints `chain: ... row(s) verified`. Then click "Licence". | "Every action is in an audit log whose rows are chained, so a changed row is found. And this build needs no licence and limits nothing." |
| G | 4 | Click "Targets" and point at the form "Declare an endpoint". Click "Specs", then "New comparison". Type `C00012001` in "Sample key". Under "Side A" choose `customer-a`, type `/api/v1/customers/{{ key }}` in "Path" and press "Send". Under "Side B" choose `customer-b`, type `/v2/customers/{{ key }}` and press "Send". Press "Save and compare this key" and point at `MISMATCH`. | "Two services of your own need no file. An endpoint is declared here with its address and its credential, which is typed once and shown nowhere again. Send asks one endpoint once, for one key, and stores nothing. One press stores the comparison and compares the key. The two answers still have different shapes, which the spec then settles." |
| H | 3 | Needs a model endpoint (see below). On a finished run's page, in "Root causes", press "Explain" in the first row and wait for the panel under the table. Point at the engine's facts beside the draft, then at a cited id. Then press "Summarise this run". | "The AI only words what the comparison already decided. Every sentence must cite stored evidence, the engine's own numbers stand beside it, and an answer that cannot be checked is withheld, not shown." |

The command of add-on B:

```bash
docker exec deltasleuth-practice-db psql -U postgres -d ds_demo_legacy -c 'ALTER TABLE crm.customers ALTER COLUMN country TYPE text'
```

The guard holds a run once for a change. To show add-on B again, run the command with `character(2)` in place of `text`, start the run and accept, and the first command works again.

Add-on G leaves a stored version of `scratch-comparison` at each press of "Save and compare this key". Add-on H needs the AI chat switched on ([README](README.md#ai-chat-and-your-own-agent-mcp), steps 1 and 2): rehearse both buttons the day before on your model, on another run than the one you will show.

## On your own two services

To show your own pair in place of the practice services, the day before:

1. Start both apps. On "Targets", declare each as in [Compare your own two services](README.md#compare-your-own-two-services), step 1: an app on this machine is `http://host.docker.internal:<port>/...` with "Private or local network" ticked.
2. On "Specs", open "New comparison" (or your stored spec's name), choose the two targets, type each request as your REST client sends it, or paste it with "Import curl…", and a real key in "Sample key". Press "Send" on each side until both show `HTTP 200` and the JSON you expect.
3. Write the two `map` expressions and the compare rules into "Everything else (YAML)", press "Save and compare this key" until the outcome is the one you expect, then give the pair its real name.
4. On "Runs", start that spec with a handful of real keys in the box "Keys, one per line", and in the demo show steps 2 to 5 of the core on that run.

## Mention, do not show

A one-machine installation cannot show these live: four-eyes approval, sign-off and the signed evidence pack need single sign-on and two people; the AI chat and add-on H need a model endpoint; notifications need a receiver. Each is one sentence at step 9.

## If something goes wrong

- The page asks for the token again: paste it again (Install, steps 2 and 3).
- `cross-origin request refused`: the address must be `http://localhost:8000`, typed exactly so.
- The table run of step 7 reads `paused`: a table changed since its last run; press "Resume (accept the changes)".
