# The DeltaSleuth guide

DeltaSleuth compares what two sources hold and reports the differences key by key. A source can be a REST service, a database table in PostgreSQL, MySQL or Oracle, or a folder of CSV or Parquet files. It reads only what a source answers, so how the source is built never matters.

- Start: [Install](#install), [Your first comparison](#your-first-comparison)
- Services: [Compare your own two services](#compare-your-own-two-services), [Runs](#runs)
- Tables and files: [Compare two database tables](#compare-two-database-tables), [MySQL, Oracle and files](#mysql-oracle-and-files), [Pre-checks, the schema guard and schedules](#pre-checks-the-schema-guard-and-schedules)
- Operate: [Notifications](#notifications), [AI chat and your own agent (MCP)](#ai-chat-and-your-own-agent-mcp), [Signing off a migration](#signing-off-a-migration), [Administration](#administration)
- Look up and show: [Where is it](#where-is-it), [Demo script](DEMO.md)

## Install
One command installs DeltaSleuth on one machine with Docker: a database, the app with its web pages, and practice services called the mocks.

1. With git, OpenSSL, a running Docker with Docker Compose version 2, and internet access for the first build: clone the source, go into its folder and install. The script ends with a block that starts `DeltaSleuth is running`.
   ```bash
   git clone <repository URL> IntelliDiff
   cd IntelliDiff
   sh deploy/up.sh
   ```
2. Open `http://localhost:8000` in a browser on this machine, typed exactly so, and copy the saved token to the clipboard. On Linux, `xclip -selection clipboard` or, with Wayland, `wl-copy` takes the place of `pbcopy`.
   ```bash
   tr -d '\n' < .deltasleuth/tokens/first-run | pbcopy
   ```
3. Paste it into "API token" and press "Sign in": a page headed "Runs" opens. Then empty the clipboard (Linux: `xclip -selection clipboard < /dev/null` or `wl-copy --clear`).
   ```bash
   pbcopy < /dev/null
   ```
4. Define `ds`, the command line, in every terminal that uses it.
   ```bash
   alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
   ```
   Run every command of this guide in the `IntelliDiff` folder. A word in angle brackets, such as `<run id>`, stands for your own value. Before a result, lines that start `{"id":`, a `warning:` about a four-eyes approval and a line about another worker are normal.

| Later, to | Run in the same folder |
|---|---|
| Stop (data, tokens and settings are kept), and start again | `docker compose stop`, and `docker compose --profile mocks up -d` |
| Update | a backup (Administration, step 3), then `git pull` and `sh deploy/up.sh` |
| Remove for good, with data, settings, secrets and token | `docker compose down -v`, then `docker image rm deltasleuth:source`, `cd ..` and `rm -rf IntelliDiff` |

Details: [chapter 1](01-install-docker.md). A second installation on the same machine, more tokens and every message: [appendix A1](A1-install-by-hand.md). Without Docker, from source: [chapter 2](02-run-from-source.md).

## Your first comparison
This compares the two bundled practice services: an old version (side a) and a new version (side b) of a customer service that hold the same 40 customers, with a few differences planted on purpose.

1. Click "Specs". In "Upload a spec", press the file button, choose `mocks/customer/spec.yaml` in the `IntelliDiff` folder, press "Validate" (one warning, `inline_keys_stored`, which stops nothing), then "Save".
   You should see "Saved as spec #1." beside the buttons.
2. Click "Runs". In "New run", leave "Spec" at `mock-customer-v1-vs-v2 1.0.0 (#1)`, "Mode" at `batch` and the "Keys" box empty, and press "Start".
   You should see, after about five seconds, the status `completed`, the line `40/40 keys · done 40` and the counts `MATCH 35`, `MISMATCH 2`, `KNOWN_DIFF 2` and `NOT_FOUND_B 1`.
3. Read the three tables: "Root causes" (4 rows, the gravest first), "Results" (one row for each key, with its outcome) and "Findings" (one row for each single difference). Click the count `MISMATCH 2` to see only those keys in "Results", then "clear". Under the counts, "What the outcomes mean" says each outcome in a line; the spec's name in the grey line under the heading opens the stored spec.
4. In the first row of "Root causes", click the key under "Sample keys", then tick "differences only".
   You should see five rows, the last one `granted  true  false`: the field that differs, with its value on side A and on side B.
5. Click the link "Run R-..." back to the run and press "report.pdf". The browser saves `<run id>-report.pdf`.
6. The same on the command line: store and run the spec.
   ```bash
   ds run mocks/customer/spec.yaml
   ```
   You should see `run R-... started`, which gives the run's id, and as the last line `KNOWN_DIFF 2, MATCH 35, MISMATCH 2, NOT_FOUND_B 1`.
7. Compare one customer again now, for example after a fix on one side.
   ```bash
   ds compare C00012003 --spec mocks/customer/spec.yaml
   ```
   You should see a line that ends `MISMATCH (DIFFERENT, rechecked)`, then `/consents/MARKETING/granted` with `A: True` and `B: False`.
8. Write the run's four standard reports and copy them to your machine; `<run id>` is the id of step 6. The last command lists four files, one of them `<run id>-report.pdf`.
   ```bash
   ds report <run id> --out /var/lib/deltasleuth/p0/<run id>
   mkdir -p p0-out
   docker compose cp app:/var/lib/deltasleuth/p0/<run id> p0-out/
   ls -1 p0-out/<run id>
   ```

| Outcome of a key | In plain words | Counts as |
|---|---|---|
| `MATCH` | Both sides answered in full, and nothing that matters differs. | a match |
| `MISMATCH` | Both sides answered in full, and at least one real difference has no explanation. | different |
| `KNOWN_DIFF` | The sides differ, and a rule in the spec says that every difference is known. | a match, or different when the rule calls it a bug |
| `NOT_FOUND_A`, `NOT_FOUND_B` | One side has the record and the other says clearly that it has none; the letter is the side that lacks it. | different |
| `INCONCLUSIVE`, `DRIFT` | Nothing can be said: a side could not be read in full, or a side changed between the first read and the second. | neither |

Details: [chapter 3](03-first-comparison.md)

## Compare your own two services
A comparison spec names two declared services, says how to ask each for a record, and says what counts as equal. **In the browser**, with no file and no restart:

1. Click "Targets". In "Declare an endpoint" type an "Id" such as `orders-legacy` and the service's "Base URL". For an app on this machine write the host as `host.docker.internal` and keep the port, as in `http://host.docker.internal:8081/api`, and tick "Private or local network". Under "Authentication" keep "None", or choose "Bearer token" or "Header" with its "Header name" (such as `Cookie`) and type the "Credential", which is stored on the server and shown nowhere again; "Add another credential header" takes a second one, and "Credential over plain http" must be ticked when the address starts with `http://`. Press "Save endpoint". You should see "Saved endpoint orders-legacy." and the endpoint in the table with `product` under "Declared in". Do the same for the second service. Skip this step to try steps 2 and 3 as printed.
2. Click "Specs", then "New comparison", and type one key in "Sample key", here `C00012001`. Under "Side A" choose the "Target", here `customer-a`, and type the "Path" of one record with `{{ key }}` where the key goes, here `/api/v1/customers/{{ key }}`. Or press "Import curl…", paste the request as a browser or REST client copies it (as cURL, for bash) and replace the record's id in "Path" with `{{ key }}`. Press "Send". You should see a line such as `HTTP 200, 1 ms, 1 attempt, 764 bytes` and the JSON that came back. Do the same under "Side B", here with `customer-b` and `/v2/customers/{{ key }}`.
3. Press "Validate", which should say "No issues.", then "Save and compare this key". You should see "Saved as spec #...." and "Compared now (run R-...)" with `MISMATCH`: the two answers are not in one shape yet. The first row of the table below gives them one, written into the box "Everything else (YAML)". Keep the name `scratch-comparison` there while you build, because a saved version is never deleted.

"Send" asks one service once and stores nothing. An installation started with `sh deploy/up.sh` shows the answer as it came; any other shows its structure only, with a stand-in for every value. A credential in a pasted command never enters the comparison: the page names its header, and "Declare the endpoint…" opens "Targets" with it filled in. **With files, on the command line** (also for a database or a folder of files):

1. Declare your two services at the end of `deploy/config/targets.yaml`, each like the entry `customer-a` there: an id, `base_url` and `auth: {kind: none}`, plus `allow_private_network: true` for a private address. A service that wants a token takes [chapter 4](04-specs.md), section 1, steps 3 to 9. Then check the file, which must print no line that starts with `error:`, and restart the app, which reads the file only when it starts. Skip this step to try step 2 as printed.
   ```bash
   docker compose run --rm app ds licence && docker compose restart app
   ```
2. Write the smallest spec that runs. As printed it compares the bundled practice services; each comment marks a part to change for your own pair.
```bash
mkdir -p my-specs && cat > my-specs/customer.yaml <<'EOF'
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata: {name: guide-customer, version: "0.1.0", owner: guide-reader}   # change: the pair's name, three numbers in quotes, your team
normalizerVersion: "1.1.0"
key:
  name: customer_id                          # change: a label for the key
  type: string
  pattern: "^C[0-9]{8}$"                     # change: an expression that fits your keys only
  source: {kind: list, keys: [C00012001]}    # change: one key of yours
sides:                                       # truncation: where an answer counts a list, where the list is, the most the service returns
  a:                                         # side a is the old system, side b the new one
    role: legacy
    target: customer-a                       # change: a target id of step 1
    request: {method: GET, path: "/api/v1/customers/{{ key }}"}   # change: the path of one record
    truncation: {countPath: "$.customer.accountCount", itemsPath: "$.customer.accounts", cap: 50}   # change
  b:
    role: new
    target: customer-b                       # change
    request: {method: GET, path: "/v2/customers/{{ key }}"}       # change
    truncation: {countPath: "$.data.productCount", itemsPath: "$.data.products", cap: 100}          # change
compare: {}
recheck: {delay: 1s, times: 1}
EOF
```
3. Copy the folder into the container, validate the spec and compare its one key, which also stores the spec; repeat the three after every edit. You should see `ok: customer.yaml is valid (0 warning(s))`, then `C00012001: MISMATCH (DIFFERENT, rechecked)`: the two answers are not in one shape yet (first row below).
   ```bash
   docker compose cp my-specs app:/var/lib/deltasleuth/p0/
   ds spec validate /var/lib/deltasleuth/p0/my-specs/customer.yaml --targets /etc/deltasleuth/targets.yaml
   ds compare C00012001 --spec /var/lib/deltasleuth/p0/my-specs/customer.yaml
   ```

| A spec can also say | A line of it |
|---|---|
| Which fields are compared: `select` picks the record and `map` gives both sides one shape, under each side; `compare.exclude` leaves paths out | `select: "$.customer"` |
| Under `compare.fields`: clean-ups before values are compared (normalisers), and a gap allowed between two numbers (a tolerance) | `- {path: "$.tags[*]", normalize: [trim, lower]}` and `- {path: "$.accounts[*].interestRate", compare: {tolerance: {abs: 0.000001}}}` |
| Differences you already know, under `knownDiffs`: each rule has `id`, `when`, `classification`, `reason` and `ticket` | `when: "path == '$.segment' && a == 'PRIVATE' && b == 'WEALTH'"` |
| What is masked and what may be shown: `security.tokenize` with a class for each field, `security.clear`, and `key.class` for the key | `- {path: "$.email", class: email}` |
| Where the keys come from: `key.source` as a list, as a service's listing (`kind: paginated_endpoint`) or as a table, a view or a file of keys on a declared target (`kind: table`, at most 1,000,000 keys), or a file given to the run with `--keys` | `keys: [C00012001, C00012003, C00012031]` |
| A key of several values: `key.type: composite` with `key.parts`. Each part goes into a request under its name, and a key is written as its parts joined by `\|` wherever keys are given | `parts: [region, shelf, code]` and `path: "/v1/{{ keyParts.region }}/shelves/{{ keyParts.shelf }}"` |
| A service that says itself when its answer was cut: `flagPaths` under a side's `truncation`. A key whose flag is `true` is INCONCLUSIVE `truncated` and is not compared | `truncation: {flagPaths: ["$..more"]}` |

Details: [chapter 4](04-specs.md)

## Runs
A run reads a set of keys from both sides, once or in repeated cycles, and stores one result for each key.

1. Make a list of three keys, a slow copy of the bundled linker spec, and a copy that repeats every 30 seconds for two cycles; copy the three files into the container.
   ```bash
   mkdir -p p0-out
   printf 'ORG-000007\nORG-000029\nORG-000041\n' > p0-out/three-keys.txt
   sed -e 's/name: mock-linker-legacy-vs-new/name: mock-linker-slow/' -e 's/^  concurrency: 2$/  concurrency: 1/' -e 's/^  delay: 10ms$/  delay: 500ms/' mocks/linker/spec.yaml > p0-out/linker-slow.yaml
   awk '{ sub(/name: mock-linker-legacy-vs-new/, "name: mock-linker-soak"); print } /^  concurrency: 2$/ { print "  cycleInterval: 30s"; print "  stop: {maxCycles: 2}" }' mocks/linker/spec.yaml > p0-out/linker-soak.yaml
   docker compose cp p0-out/. app:/var/lib/deltasleuth/p0/
   ```
2. Run every key of the spec's key source, then only the keys of the file. You should see `completed` twice, first above a line that starts `total 61`, then above one that starts `total 3`.
   ```bash
   ds run mocks/linker/spec.yaml
   ds run mocks/linker/spec.yaml --keys /var/lib/deltasleuth/p0/three-keys.txt
   ```
3. Start the slow copy without waiting for it; it prints `run R-... started` and takes about 40 seconds.
   ```bash
   ds run /var/lib/deltasleuth/p0/linker-slow.yaml --no-wait
   ```
4. Meanwhile, in the browser, click "Runs", click the run's id in the column "Run", and press "pause", "resume" and "cancel" in turn. The badge beside the heading reads `paused`, then `running`, then `cancelled`.
5. Run the soak copy, whose lines `cycleInterval: 30s` and `stop: {maxCycles: 2}` under `execution:` make two cycles. You should see, after about 37 seconds, `completed (stop_condition:maxCycles)`.
   ```bash
   ds run /var/lib/deltasleuth/p0/linker-soak.yaml --soak
   ```
6. `ds runs --limit 4` lists the newest runs with their ids. The `ds report` of "Your first comparison", step 8, writes the files of the first three rows below for any run, and `--kind NAME` after it writes one other file.

| Report | What it holds |
|---|---|
| `report.pdf` | The report to read and pass on |
| `discrepancies.csv` | One row for each difference of a key that counts as different: the list to work through |
| `retry.csv`, `coverage.csv` | One row for each key without a final judgement, the keys to run again; and the whole cycle in one row, the counters |
| `root_causes.csv`, `normalization.csv`, `suppressed.csv` | One row for each cause, for each difference of format only, and for each difference that a known-difference rule covers |
| `graph.csv`, `readiness.csv` | The rows of a graph comparison, and one row for each ended cycle of the pair's runs |
| `report.md`, `report.html` | The summary, the same text in two formats: `--kind summary`, which the run's page does not offer |

Details: [chapter 5](05-runs.md)

## Compare two database tables
A table spec pairs the rows of two tables by a key column, compares their columns, and reports every key whose rows differ or that only one table holds.

1. Start a practice PostgreSQL on the installation's network; its password goes into a file.
   ```bash
   (umask 077; openssl rand -hex 24 > deploy/secrets/practice_db_password)
   chmod 444 deploy/secrets/practice_db_password
   docker run -d --name deltasleuth-practice-db --network deltasleuth_default -e POSTGRES_PASSWORD_FILE=/run/secrets/practice_db_password -v "$PWD/deploy/secrets/practice_db_password:/run/secrets/practice_db_password:ro" postgres:16-alpine
   ```
2. Wait five seconds, then, once only, load the example data (two `NOTICE:` lines are normal), write each database's DSN (its connection string) into a secret file, and hand both files to the app. The targets file already declares the two databases as `crm-legacy` and `platform-users`, each with a line such as `dsn_ref: file:/run/secrets/crm_legacy_dsn`.
   ```bash
   docker exec -i deltasleuth-practice-db psql -q -U postgres -v ON_ERROR_STOP=1 -f - < examples/tables/seed.sql
   (umask 077; { printf 'postgresql://postgres:'; tr -d '\n' < deploy/secrets/practice_db_password; printf '@deltasleuth-practice-db:5432/ds_demo_legacy'; } > deploy/secrets/crm_legacy_dsn)
   (umask 077; { printf 'postgresql://postgres:'; tr -d '\n' < deploy/secrets/practice_db_password; printf '@deltasleuth-practice-db:5432/ds_demo_platform'; } > deploy/secrets/platform_users_dsn)
   chmod 444 deploy/secrets/crm_legacy_dsn deploy/secrets/platform_users_dsn
   sed -i.bak 's|^COMPOSE_FILE=.*|&:examples/tables/compose.yaml|' .env
   rm .env.bak
   docker compose --profile mocks up -d
   ```
3. Write the smallest table spec, copy it into the app's folder and run it; without a list of keys a run scans both tables. You should see `completed` and, last, `MATCH 985, MISMATCH 4, NOT_FOUND_A 1, NOT_FOUND_B 11`.
```bash
mkdir -p practice
cat > practice/customers.yaml <<'EOF'
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata: {name: practice-customers, version: "1.0.0", owner: practice}
normalizerVersion: "1.1.0"
key: {name: customer_id, type: string}
sides:
  a: {role: legacy, target: crm-legacy, table: {schema: crm, name: customers, keyColumns: [customer_id], columns: [phone, status, country, balance]}}
  b: {role: new, target: platform-users, table: {name: users, keyColumns: [user_id], columns: [phone, status, country, balance]}}
compare: {}
recheck: {delay: 5s, times: 1}
security: {clear: ["$.status", "$.country", "$.balance"]}
EOF
docker compose cp practice/. app:/var/lib/deltasleuth/p0/practice
ds run /var/lib/deltasleuth/p0/practice/customers.yaml
```
4. Click "Runs", then the run's id, and read the scan line: `Table scan (done): 1,000 rows on A, 990 on B; 985 identical; 16 to compare; 0 not compared (NULL, empty or over-long key).` Identical rows are only counted; the 16 other keys were read again on both sides and are the ones reported.

| A table spec can also say | A line of it |
|---|---|
| A row filter: only some rows of a side are compared | `where: "status <> 'closed'"` in the side's `table` |
| A key map: the sides write a key differently, such as `C-00100005` and `100005` | `{name: user_id, keyMap: {kind: prefix_pad, prefix: "C-", width: 8, sideType: integer}}` in place of `user_id` in `keyColumns` |
| A type map: the sides store a column in two types | `typeMap: {created_at: {as: timestamp, naiveTimezone: Europe/London}}` in side a beside `table`, `typeMap: {created_at: {as: timestamp}}` in side b, and `created_at` in both `columns` |
| A cap on the scan: stop at the fifth key to compare | `execution: {stop: {maxKeys: 5}}` at the top level |

Details: [chapter 6](06-tables.md)

## MySQL, Oracle and files
A side can also be a MySQL or Oracle table, or a folder of CSV or Parquet files: its entry in `deploy/config/targets.yaml` says which, and the spec stays a table spec.

1. MySQL and Oracle need a server of that kind, which a one-machine trial does not have (the details chapter starts practice ones in Docker, from images of 812 MB and 4.8 GB). For your own, model the entry on `billing-mysql` or `ledger-oracle` in the targets file, show its secret files to the app ([chapter 7](07-engines-and-files.md), section 2, step 11), check it with `ds target check <target id>`, for one line that ends `session ok`, and apply it with the second command of step 4.
2. For files, write the practice export: a CSV file and a Parquet file with planted differences.
   ```bash
   mkdir -p practice/exports/kit
   chmod 777 practice/exports/kit
   docker compose run --rm --no-deps -v "$PWD/examples/tables/files:/kit:ro" -v "$PWD/practice/exports/kit:/export" app python /kit/generate.py /export
   chmod 755 practice/exports/kit
   ```
3. Add the first line under `app:` in `deploy/docker-compose.site.yml`, where it shows the folder to the app read-only, and the second as the last line of `deploy/config/targets.yaml`; keep the leading spaces. `settle_s: 0` reads files just written; without it a file must have been left alone for 60 seconds.
```yaml
    volumes: ["../practice/exports/kit:/var/lib/deltasleuth/files/kit:ro"]
  kit-files: {engine: files, root: /var/lib/deltasleuth/files/kit, settle_s: 0}
```
4. Check the target, which also drafts the CSV's layout for a spec, apply both files, and run the bundled spec, whose sides name their files in a `file` block under `table`.
   ```bash
   ds target check kit-files --table accounts_a/accounts.csv --format csv --header --count
   docker compose --profile mocks up -d
   ds run examples/tables/files/spec.yaml
   ```
   You should see the draft `csv: {delimiter: ";", newline: "\n", header: true, columns: [account_id, holder, balance, opened, status]}`, and the run end `INCONCLUSIVE 1, MATCH 995, MISMATCH 3, NOT_FOUND_A 1, NOT_FOUND_B 1`.

Details: [chapter 7](07-engines-and-files.md)

## Pre-checks, the schema guard and schedules
A pre-check counts and adds up two tables before any row is compared, the schema guard holds a run when a table changed since the last run of its spec, and a schedule starts runs of a stored spec by itself.

1. Pre-check the bundled table pair; it reads the practice database of "Compare two database tables" and fetches no row.
   ```bash
   ds precheck run examples/tables/spec.yaml
   ```
   You should see the pair with its verdict: `1  crm-customers-vs-platform-users  rows differ: side a 989, side b 990 (1 fewer on side a)`.
2. Change a column's type, then run the spec of "Compare two database tables" again: the guard holds the new run.
   ```bash
   docker exec deltasleuth-practice-db psql -U postgres -d ds_demo_legacy -c 'ALTER TABLE crm.customers ALTER COLUMN country TYPE text'
   ds run /var/lib/deltasleuth/p0/practice/customers.yaml
   ```
   You should see `paused (run_paused_schema_drift)` and a line that begins `Paused by the schema guard before any table row was read: column 'country' was character(2) (char)`. A harmless change, such as an added column, only prints a `warning:` line.
3. Accept the change: click "Runs", then the paused run's id, and press "Resume (accept the changes)"; "cancel" refuses it. The run goes on and completes, and later runs of the spec are not held for this change.
4. Schedule a stored spec for two every night, here the mock pair of "Your first comparison". You should see a line that starts `added SC-` and names the next 02:00.
   ```bash
   ds schedules add nightly-customers --spec mock-customer-v1-vs-v2 --cron '0 2 * * *' --tz Europe/London
   ```
5. Or, in place of step 4, in the web UI: click "Schedules"; in the panel "New schedule" type `nightly-customers` in "Name", choose `mock-customer-v1-vs-v2 1.0.0` in "Spec", leave "Runs" at `daily`, set "at" to 02:00 and press "Add". You should see "Added SC-… (nightly-customers)." above the form.

Details: [chapter 8](08-prechecks-and-schedules.md)

## Notifications
DeltaSleuth sends a short message to a destination you declare (a webhook, Slack, Teams or e-mail) when an event you chose happens.

1. Make the signing key and hand it to the app as a secret: [chapter 9](09-notifications.md), section 2, steps 2 to 7. Then add this block, once, at the end of `deploy/config/deltasleuth.yaml`: one webhook channel, here a receiver on this machine, and one rule, here a cycle that shows a new root cause.
```yaml
notify:
  label: trial
  channels:
    local-hook: {kind: webhook, url: "http://127.0.0.1:18421/hook",
                 signing_key_refs: [file:/run/secrets/notify_hook_key], allow_private_network: true}
  rules:
    - {name: new-gaps, events: [cycle.ended], channels: [local-hook], when: {new_root_causes_at_least: 1}}
```
2. Check the block, which sends nothing and must print a line that starts `ok:`, then make the app container anew, with the secret and the block. A later change to the block alone needs only the check and `docker compose restart app`.
   ```bash
   ds notify check
   docker compose --profile mocks up -d
   ```
3. A one-machine trial has no receiver: start the practice one of chapter 9, section 3. Then send a test from inside the app container. You should see `sent to local-hook: HTTP 200 in 2 ms (N-...)` as the last line.
   ```bash
   docker compose exec -e DS_OPERATOR="$(id -un)" app ds notify test local-hook
   ```
4. Or, in the web UI: click "Notifications", which the bar now has after "Licence", then "Send test" in the row of `local-hook`. The same line appears above the grey one.

Details: [chapter 9](09-notifications.md)

## AI chat and your own agent (MCP)
The chat answers questions about a run in plain words through a model that you name, and the MCP server lets your own AI agent read runs and findings.

1. The chat needs a model endpoint that speaks the OpenAI Chat Completions API with tool calling, so a one-machine trial cannot use it. With an endpoint: in `deploy/config/llm_profiles.yaml`, write its host in the first line under `hosts:`, and its address and model in the lines `base_url` and `model` of the profile `gateway`. Then put its key into the file `deploy/secrets/llm_key` and show that file to the app, as [chapter 10](10-chat-and-agents.md), section 1, steps 11 to 16, do.
2. In `deploy/config/deltasleuth.yaml`, remove the `# ` at the start of the three lines `llm_profiles_file: llm_profiles.yaml`, `llm_profile: gateway` and `llm_environment: prod`. Probe the profile and, when its line says `tool_roundtrip=pass`, make the app container anew. A run's page then shows the button "Ask AI about this run" at the bottom right.
   ```bash
   ds llm-probe
   docker compose --profile mocks up -d
   ```
   The run's page then also has two AI drafts in its section "Root causes": "Explain" at the end of each row and "Summarise this run" beside the heading. A panel under the table shows the draft with the ids it cites, beside the facts the comparison stored; a panel headed "No AI draft" means the answer could not be checked and was withheld.
3. The MCP server needs no model. In `deploy/config/deltasleuth.yaml`, remove the `# ` at the start of the line `# mcp:` and of the five lines under it, which keep their two leading spaces; to let an agent read the practice runs, also add `production_clear_declared` to the list of the line `data_classes`. Then check the block, restart the app and wait five seconds.
   ```bash
   ds mcp check
   docker compose restart app
   ```
4. Create a token with the role `viewer` for the agent, straight into a file, define `dscurl`, which sends a request with a token file and never shows the token, and call a tool: the two newest runs. You should see one text that is itself JSON, with `"isError":false` at the end.
   ```bash
   (umask 077; set -C; docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds token create --name my-agent --owner "$(id -un)" --role viewer --days 30 > .deltasleuth/tokens/my-agent)
   dscurl() { t=".deltasleuth/tokens/$1"; shift; { printf 'header = "Authorization: Bearer '; tr -d '\n' < "$t"; printf '"\n'; } | curl -sS -K - "$@"; }
   dscurl my-agent -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_runs","arguments":{"limit":2}}}' http://localhost:8000/v1/mcp
   ```

Details: [chapter 10](10-chat-and-agents.md)

## Signing off a migration
Signing off turns a comparison into evidence that named people accept: readiness, sampled runs and labelled accuracy work on a trial, while approval, sign-off and the evidence pack need single sign-on.

1. Print every pair, which is a spec name that has a batch or soak run, with its state and the gates it does not meet; a new cycle shows within about a minute. You should see a line that starts `(no programme):`, then each pair with a figure such as `2 of 5 gates`. The screen "Readiness" lists the same pairs, and a pair's name opens its gates.
   ```bash
   ds readiness
   ```
   On a pair's screen, "Cut-over brief" shows the pair on one screen: its gates, the stored figures of its newest complete cycle (each a link into the run), a trend of its last 30 measured cycles with a mark where the spec version (V) or the key list (K) changed, and its root-cause groups as `opened`, `still open` or `closed`. It restates stored measures: no score, no forecast, and `closed` means not observed in the newest complete cycle, not fixed. The buttons "brief.pdf" (one page), "brief.csv" and "causes.csv" give the same; so does `ds readiness report --pair <pair> --out <folder in the volume>`.
2. Four-eyes approval of a spec version, sign-off of an ended cycle and the signed evidence pack need single sign-on and two different people, so a one-machine trial stops here. Single sign-on is three things: the client secret in the file `deploy/secrets/oidc_client_secret`, the ten lines that start at `#   oidc:` in the `auth:` block of `deploy/config/deltasleuth.yaml`, with the `# ` at their start removed and your provider's values in them, and the secret's name in the site file; `ds auth check` checks them and `docker compose --profile mocks up -d` applies them ([chapter 11](11-sign-off.md), section 7).
3. Before a pair is signed off, validate its spec: the warning `key_pattern_empty` says that its `key.pattern` is missing or matches the empty string, and such a spec never gets an evidence pack.

Details: [chapter 11](11-sign-off.md); its sections 3 and 4 have sampled runs and labelled accuracy, and section 6 groups pairs into programmes.

## Administration
An administrator manages tokens, sets retention, takes backups, and reads the audit log and the app's health, all on the command line.

1. Create a token straight into a file, so that it is never shown; it lives 90 days. `<name>` is in lower case, `<owner>` answers for it, and `<role>` is `viewer` (reads everything), `operator` (also starts, pauses, resumes and cancels runs, compares a key again and sets up schedules) or `spec_author` (also uploads and checks specs). Then list the tokens, and revoke one by the id that starts its line.
   ```bash
   (umask 077; set -C; docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds token create --name <name> --owner "<owner>" --role <role> > .deltasleuth/tokens/<name>)
   ds token list
   ds token revoke <token id>
   ```
2. Retention: a cycle's raw data is deleted 90 days after the cycle ends; the days are `standard_days` under `retention:` in `deploy/config/deltasleuth.yaml`, read at a restart. `ds purge` holds one run's data, releases it or erases it at once: chapter 12, section 7.
3. Back up while the app runs: the database first, then the stored answers and the settings. Keep a copy of the key files `deploy/secrets/kek` and `deploy/secrets/tokeniser` in another place: without them a backup shows no raw value. A restore loses everything done since the backup, and each of its commands is in chapter 12, section 11.
   ```bash
   mkdir -p -m 700 ~/deltasleuth-backups
   (umask 077; docker compose exec -T db pg_dump -U deltasleuth deltasleuth > ~/deltasleuth-backups/ds-$(date +%F).sql)
   (umask 077; docker compose run --rm --no-deps -T app tar -C /var/lib/deltasleuth/evidence --exclude=./spill -cf - . > ~/deltasleuth-backups/evidence-$(date +%F).tar)
   (umask 077; mkdir -p ~/deltasleuth-backups/settings-$(date +%F) && cp -R deploy/config ~/deltasleuth-backups/settings-$(date +%F)/config && cp deploy/docker-compose.site.yml .env ~/deltasleuth-backups/settings-$(date +%F)/)
   ```
4. Day to day: read the audit log, newest rows first; check that no row of it was changed, which prints `chain: 27 row(s) verified` with your own number; ask the app's health, which answers with `"status": "ok"`; read the app's newest log lines.
   ```bash
   ds audit
   ds audit verify
   curl -fsS http://localhost:8000/v1/health
   docker compose logs --tail 5 app
   ```

Details: [chapter 12](12-administration.md)

## Where is it
Each feature with its screen in the web UI, its `ds` command and its details chapter; "not offered" marks what the web UI does not have.

| Feature | Web UI | `ds` command | Chapter |
|---|---|---|---|
| Store and validate a spec | "Specs", panel "Upload a spec" | `ds spec validate`; `ds run` and `ds compare` store the file they are given | [3](03-first-comparison.md), [4](04-specs.md) |
| Draft a spec from sample answers | "Specs", panel "Draft a spec from sample pairs" | `ds profile` | [4](04-specs.md) |
| Declare a target | "Targets": "Declare an endpoint", for a service | none: the file `deploy/config/targets.yaml`, for every kind; `ds target check`, for database and files targets | [4](04-specs.md), [6](06-tables.md), [7](07-engines-and-files.md) |
| Build a comparison in the browser and try each side's request | "Specs": "New comparison", or a stored spec's name; "Import curl…", "Send", "Save and compare this key" | none | [4](04-specs.md) |
| Start a run, batch or soak | "Runs", panel "New run" | `ds run` | [3](03-first-comparison.md), [5](05-runs.md) |
| Pause, resume and cancel a run | the run's page: "pause", "resume", "cancel" | none | [5](05-runs.md) |
| Read a run: counts, root causes, results, findings | "Runs", then the run's id | `ds runs` lists the runs; the rows are in the reports | [3](03-first-comparison.md), [5](05-runs.md) |
| One key, side by side | the key's page, from a table of the run | none | [3](03-first-comparison.md) |
| Compare one key again now | the key's page: "Compare now" | `ds compare` | [3](03-first-comparison.md), [5](05-runs.md) |
| Reports | the run's page: nine buttons | `ds report` | [3](03-first-comparison.md), [5](05-runs.md) |
| Table, MySQL, Oracle and file sides | the same screens; the run's page adds the line `Table scan (...)` | `ds run`, `ds target check` | [6](06-tables.md), [7](07-engines-and-files.md) |
| Table pre-check | "Pre-checks", or "Pre-check…" in a row of "Specs" | `ds precheck` | [8](08-prechecks-and-schedules.md) |
| Schema guard | the paused run's page: "Resume (accept the changes)", "cancel" | none | [8](08-prechecks-and-schedules.md) |
| Schedules | "Schedules" | `ds schedules` | [8](08-prechecks-and-schedules.md) |
| Notifications | "Notifications", in the bar once they are switched on | `ds notify` | [9](09-notifications.md) |
| AI chat | a run's page: "Ask AI about this run" | `ds llm-probe`, which checks the model only | [10](10-chat-and-agents.md) |
| AI draft: explain a root cause, summarise a run | a run's page, section "Root causes": "Explain", "Summarise this run" | none | [10](10-chat-and-agents.md) |
| MCP server for your own agent | not offered | `ds mcp check`, `ds mcp` | [10](10-chat-and-agents.md) |
| Sampled runs, labelled accuracy | the run's page: "Sample", "Labelled accuracy" | `ds samples`, `ds labellings` | [11](11-sign-off.md) |
| Readiness | "Readiness" | `ds readiness` | [11](11-sign-off.md) |
| Cut-over brief of a pair | "Readiness", a pair's name, then "Cut-over brief"; also "cut-over brief" on a run's page | `ds readiness report --pair` | [11](11-sign-off.md) |
| Approval, sign-off, evidence pack (single sign-on) | "Approvals"; the run's page: "Sign-off", "Issue evidence pack" | `ds approvals`, `ds packs` | [11](11-sign-off.md) |
| Tokens, sessions, the audit log | not offered with token sign-in; "Admin" under single sign-on | `ds token`, `ds session revoke`, `ds audit` | [12](12-administration.md) |
| Retention: hold and erase | not offered: the run's page shows only until when data is kept | `ds purge` | [12](12-administration.md) |
| Backup, restore, update, encryption keys | not offered | `ds keys status`; the rest are Docker commands | [12](12-administration.md) |
| Licence | "Licence" | `ds licence` | [12](12-administration.md) |

## Demo
A presenter's script for 15 to 30 minutes on the bundled practice data, with what to click and what to say: [DEMO.md](DEMO.md).

The numbered chapters in this folder are the detailed reference: every option, every message and every limit.
The steps of each were run on a fresh installation, and each says where a step could not be verified.
