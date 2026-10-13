# DeltaSleuth user guide

DeltaSleuth compares what two sources hold and reports the differences key by key. A source can be a REST service, a database table in PostgreSQL, MySQL or Oracle, or a folder of CSV or Parquet files. It reads only what a source answers, so how the source is built never matters.

Sections: [Install](#install), [Your first comparison](#your-first-comparison), [Outcomes](#outcomes), [Compare your own two services](#compare-your-own-two-services), [The parts of a spec](#the-parts-of-a-spec), [Runs](#runs), [Compare tables and files](#compare-tables-and-files), [Results, reports and readiness](#results-reports-and-readiness), [Sign-off, sampling and approvals](#sign-off-sampling-and-approvals), [Schedules and notifications](#schedules-and-notifications), [AI chat and the MCP server](#ai-chat-and-the-mcp-server), [Where is it](#where-is-it), [A 15-minute demo](#a-15-minute-demo).

## Install
Install DeltaSleuth on one machine and sign in as [docs/deployment.md](deployment.md) describes. Then define `ds`, the command line, in every terminal that uses it.
```bash
alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
```
Run every command of this guide in the repository's folder. A word in angle brackets, such as `<run id>`, stands for your own value. Before a result, lines that start `{"id":`, a `warning:` about a four-eyes approval and a line about another worker are normal.
What a person may do depends on the role. A viewer reads everything. An operator also starts, pauses, resumes and cancels runs, compares a key again and sets up schedules. A spec author also stores and checks specs. An administrator manages tokens, people and settings.

## Your first comparison
This compares the two bundled practice services: an old version (side a) and a new version (side b) of a customer service. Both hold the same 40 customers, with a few differences planted on purpose.

1. Click "Specs". In "Upload a spec", press the file button and choose `mocks/customer/spec.yaml` in the repository's folder. Press "Validate": it shows one warning, `inline_keys_stored`, which stops nothing. Press "Save".
   You should see "Saved as spec #1." beside the buttons.
2. Click "Runs". In "New run", leave "Spec" at `mock-customer-v1-vs-v2 1.0.0 (#1)`, "Mode" at `batch` and the keys box empty. Press "Start".
   You should see, after about five seconds, the status `completed`, the line `40/40 keys · done 40` and the counts `MATCH 35`, `MISMATCH 2`, `KNOWN_DIFF 2` and `NOT_FOUND_B 1`.
3. Read the three tables: "Root causes" (4 rows, the gravest first), "Results" (one row for each key, with its outcome) and "Findings" (one row for each single difference). Click the count `MISMATCH 2` to see only those keys in "Results", then "clear".
4. In the first row of "Root causes", click the key under "Sample keys". Then tick "differences only".
   You should see five rows, the last one `granted  true  false`: the field that differs, with its value on side A and on side B.
5. Click the link "Run R-..." back to the run and press "report.pdf". The browser saves `<run id>-report.pdf`.
6. Do the same on the command line: store and run the spec. Then compare one customer again now, for example after a fix on one side.
   ```bash
   ds run mocks/customer/spec.yaml
   ds compare C00012003 --spec mocks/customer/spec.yaml
   ```
   The first command prints `run R-... started`, which gives the run's id, and as its last line `KNOWN_DIFF 2, MATCH 35, MISMATCH 2, NOT_FOUND_B 1`. The second prints a line that ends `MISMATCH (DIFFERENT, rechecked)`, then `/consents/MARKETING/granted` with `A: True` and `B: False`.
7. Write the run's four standard reports and copy them to your machine. `<run id>` is the id of step 6. The last command lists four files, one of them `<run id>-report.pdf`.
   ```bash
   ds report <run id> --out /var/lib/deltasleuth/p0/<run id>
   mkdir -p p0-out
   docker compose cp app:/var/lib/deltasleuth/p0/<run id> p0-out/
   ls -1 p0-out/<run id>
   ```

## Outcomes
Each key of a run gets one outcome. Under the counts of a run, "What the outcomes mean" says each in a line. For `INCONCLUSIVE` and `DRIFT`, the column "Why" of "Results" gives the reason, such as `truncated`, `absence_unconfirmed` or `absent_both`.

| Outcome of a key | In plain words | Counts as |
|---|---|---|
| `MATCH` | Both sides answered in full, and nothing that matters differs. | a match |
| `MISMATCH` | Both sides answered in full, and at least one real difference has no explanation. | different |
| `KNOWN_DIFF` | The sides differ, and a rule in the spec says that every difference is known. | a match, or different when the rule calls it a bug |
| `NOT_FOUND_A`, `NOT_FOUND_B` | One side has the record and the other says clearly that it has none. The letter is the side that lacks it. | different |
| `INCONCLUSIVE`, `DRIFT` | Nothing can be said. A side could not be read in full, neither side holds the key, or a side changed between the first read and the second. | neither |

## Compare your own two services
A comparison spec names two declared services, says how to ask each for a record, and says what counts as equal. **In the browser**, with no file and no restart:

1. Click "Targets". In "Declare an endpoint", type an "Id" such as `orders-legacy` and the service's "Base URL". For a service on this machine, write the host as `host.docker.internal`, as in `http://host.docker.internal:8081/api`, and tick "Private or local network". Under "Authentication" keep "None", or choose "Bearer token" or "Header" and type the "Credential", which is stored on the server and shown nowhere again. With a credential and an address that starts with `http://`, also tick "Credential over plain http". Press "Save endpoint".
   You should see "Saved endpoint orders-legacy." Do the same for the second service. Skip this step to try steps 2 and 3 on the practice pair.
2. Click "Specs", then "New comparison". Type one key in "Sample key", here `C00012001`. Under "Side A" choose the "Target", here `customer-a`, and type the "Path" of one record with `{{ key }}` where the key goes, here `/api/v1/customers/{{ key }}`. Or press "Import curl…", paste the request as a browser or REST client copies it (as cURL, for bash) and replace the record's id in "Path" with `{{ key }}`. Press "Send".
   You should see a line such as `HTTP 200, 1 ms, 1 attempt, 764 bytes` and the JSON that came back. Do the same under "Side B", here with `customer-b` and `/v2/customers/{{ key }}`.
3. Press "Validate", which should say "No issues.", then "Save and compare this key".
   You should see "Saved as spec #...." and "Compared now (run R-...)" with `MISMATCH`: the two answers are not in one shape yet. Give them one with `select` and `map` under each side in the box "Everything else (YAML)", as the spec below does. Keep the name `scratch-comparison` while you build, because a saved version is never deleted.

"Send" asks one service once and stores nothing. A one-machine trial shows the answer as it came. Any other installation shows its structure only, with a stand-in for every value. A credential in a pasted command never enters the comparison: the page names its header, and "Declare the endpoint…" opens "Targets" with it filled in. In a trial a spec author declares endpoints. Under single sign-on an administrator does. Otherwise the targets file is the only way.

**With a spec file, on the command line** (also for a database or a folder of files):

1. Declare your two services at the end of `deploy/config/targets.yaml`, each like the entry `customer-a` there: an id, `base_url` and `auth: {kind: none}`, plus `allow_private_network: true` for a private address. For a service that wants a token, `auth` names `kind: bearer` or `kind: header` and a `secret_ref` to a secret file, as [targets.example.yaml](../deploy/config/targets.example.yaml) shows. Then check the file and restart the app, which reads the file only when it starts. The check must print no line that starts with `error:`. Skip this step to try steps 2 and 3 as printed.
   ```bash
   docker compose run --rm app ds licence && docker compose restart app
   ```
2. Write the spec. As printed it compares four customers of the practice pair. Each `# change` marks a part to change for your own pair.
```bash
mkdir -p my-specs && cat > my-specs/customer.yaml <<'EOF'
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata: {name: guide-customer, version: "0.1.0", owner: guide-reader}   # change: the pair's name, three numbers in quotes, who answers for it
normalizerVersion: "1.1.0"
key:
  name: customer_id                          # change: a label for the key
  type: string
  pattern: "^C[0-9]{8}$"                     # change: an expression that fits your keys only
  source: {kind: list, keys: [C00012001, C00012003, C00012027, C00012031]}   # change: a few keys of yours
sides:
  a:                                         # side a is the old system, side b the new one
    role: legacy
    target: customer-a                       # change: a target id of step 1
    request: {method: GET, path: "/api/v1/customers/{{ key }}"}   # change: the path of one record
    select: "$.customer"                     # change: the record inside the answer (JSONPath)
    map: |                                   # change: one shape for both sides (JSONata)
      {"segment": segment, "status": $lookup({"A": "ACTIVE", "S": "SUSPENDED", "C": "CLOSED"}, status),
       "email": email, "consents": [consents.{"purpose": purpose, "granted": granted = "Y"}]}
    notFound: {status: [404]}                # this answer means: no such record
    truncation: {countPath: "$.customer.accountCount", itemsPath: "$.customer.accounts", cap: 50}   # change
  b:
    role: new
    target: customer-b                       # change
    request: {method: GET, path: "/v2/customers/{{ key }}"}       # change
    select: "$.data"
    map: |
      {"segment": segment, "status": lifecycleStatus, "email": contactPoints[channel = "EMAIL"][0].value,
       "consents": [consents.{"purpose": purpose, "granted": status = "GRANTED"}]}
    notFound: {status: [404]}
    truncation: {countPath: "$.data.productCount", itemsPath: "$.data.products", cap: 100}          # change
compare:
  arrays: [{path: "$.consents", mode: keyed, key: [purpose]}]   # pair list members by a field, not by position
  fields:
    - {path: "$.email", normalize: [trim, email_basic]}         # clean-ups before two values are compared
    - {path: "$.consents[*].granted", compare: exact, severity: CRITICAL}
knownDiffs:                                  # a difference with a known reason; classification: expected_by_design, accepted or bug
  - {id: KD-GUIDE-0001, when: "path == '$.segment' && a == 'PRIVATE' && b == 'WEALTH'", classification: expected_by_design,
     reason: "The new service renamed the PRIVATE segment to WEALTH.", ticket: CUST-1042}
recheck: {delay: 1s, times: 1}               # a key that differs is read again after this wait
execution: {concurrency: 2, delay: 10ms}     # keys in flight at once on a side, and the pause between keys
security:
  tokenize: [{path: "$.email", class: email}]   # shown as a stand-in of this class
  clear: ["$.segment", "$.status", "$.consents[*].purpose", "$.consents[*].granted"]   # shown as they are
EOF
```
3. Copy the folder into the container, validate the spec, compare one key and run all four. Repeat the four commands after every edit. The last two also store the spec.
   ```bash
   docker compose cp my-specs app:/var/lib/deltasleuth/p0/
   ds spec validate /var/lib/deltasleuth/p0/my-specs/customer.yaml --targets /etc/deltasleuth/targets.yaml
   ds compare C00012001 --spec /var/lib/deltasleuth/p0/my-specs/customer.yaml
   ds run /var/lib/deltasleuth/p0/my-specs/customer.yaml
   ```
   You should see `ok: customer.yaml is valid (0 warning(s))`, then a line that starts `C00012001: MATCH`, and as the last line `KNOWN_DIFF 1, MATCH 1, MISMATCH 1, NOT_FOUND_B 1`.

## The parts of a spec
The spec above holds the parts a spec author changes most. [mocks/customer/spec.yaml](../mocks/customer/spec.yaml) is a complete worked example.

| Part | What it says | Also |
|---|---|---|
| `key` | The key's label, its type, the `pattern` every key must fit, and where the keys come from (`source`). | `source.kind` is `list`, `paginated_endpoint` (a service's listing) or `table` (a table, a view or a file of keys on a declared target, at most 1,000,000 keys). A run can take a file of keys instead. `class: identifier` masks the key itself. `type: composite` with `parts: [region, shelf, code]` is a key of several values: a request names a part as `{{ keyParts.region }}`, and a key is written as its parts joined by `\|`. |
| `target`, `request` | Which declared target a side asks, and how: `method` (`GET` or `POST`), `path`, and if needed `query`, `headers` and a JSON `body`. | `{{ key }}` stands for the key in each of them. A credential never goes here: it belongs to the target. |
| `select`, `map` | `select` picks the record inside the answer. `map` gives both sides one shape, so that equal things have equal names. | Only what `map` writes is compared. |
| `notFound`, `truncation` | Which answer means that the side has no such record. How to tell that a list was cut: where the answer counts the list (`countPath`), where the list is (`itemsPath`) and the most the service returns (`cap`). | A cut answer makes the key `INCONCLUSIVE`, never a false difference. `flagPaths: ["$..more"]` reads a flag with which the service says itself that it cut the answer. |
| `compare` | `arrays` says how the members of a list pair: `keyed`, `set`, `ordered` or `multiset`. `fields` gives a path its clean-ups (`normalize`), a `severity` and a tolerance. `exclude` leaves paths out. | Clean-ups include `trim`, `upper`, `lower`, `collapse_ws`, `nfkc`, `email_basic`, `digits_only`, `e164`, `decimal` and `timestamp_utc`. A tolerance: `compare: {tolerance: {abs: 0.000001}}`. |
| `knownDiffs` | Differences that are already known. Each rule has `id`, `when`, `classification`, `reason` and `ticket`. | A key whose every difference a rule covers reads `KNOWN_DIFF`. With `classification: bug` it still counts as different. |
| `recheck` | A key that differs is read again after `delay`, at most `times` times, before its outcome is final. | A key whose side changed in between reads `DRIFT`. |
| `execution` | `concurrency` is the number of keys in flight at once on a side. `delay` is the pause between keys. | The request rate belongs to the target: "Requests a second" and "At once" on "Targets", or `rate_per_s` and `max_concurrency` in the targets file. `cycleInterval` and `stop` shape a soak run. |
| `security` | `tokenize` shows a value as a stand-in of its class. `clear` lists the paths whose values may be shown as they are. | A path in neither list is tokenised. Classes: `name`, `phone`, `email`, `address`, `account_number`, `device_id`, `date_of_birth`, `tax_id`, `identifier`, `free_text`, `free_number`. |

## Runs
A run reads a set of keys from both sides, once (batch) or in repeated cycles (soak), and stores one result for each key. These steps use the second practice pair, [mocks/linker/spec.yaml](../mocks/linker/spec.yaml). It compares which entities each side links to an entity, each link with a `flagged` mark. Its 61 keys look like `ORG-000001`.

1. Make a list of three keys, a slow copy of the spec, and a copy that repeats every 30 seconds for two cycles. Copy the three files into the container.
   ```bash
   mkdir -p p0-out
   printf 'ORG-000007\nORG-000029\nORG-000041\n' > p0-out/three-keys.txt
   sed -e 's/name: mock-linker-legacy-vs-new/name: mock-linker-slow/' -e 's/^  concurrency: 2$/  concurrency: 1/' -e 's/^  delay: 10ms$/  delay: 500ms/' mocks/linker/spec.yaml > p0-out/linker-slow.yaml
   awk '{ sub(/name: mock-linker-legacy-vs-new/, "name: mock-linker-soak"); print } /^  concurrency: 2$/ { print "  cycleInterval: 30s"; print "  stop: {maxCycles: 2}" }' mocks/linker/spec.yaml > p0-out/linker-soak.yaml
   docker compose cp p0-out/. app:/var/lib/deltasleuth/p0/
   ```
2. Batch: run every key of the spec's key source, then only the keys of the file, one key a line. You should see `completed` twice, first above a line that starts `total 61`, then above one that starts `total 3`. In the browser, the box "Keys, one per line" of "New run" takes the same keys.
   ```bash
   ds run mocks/linker/spec.yaml
   ds run mocks/linker/spec.yaml --keys /var/lib/deltasleuth/p0/three-keys.txt
   ```
3. Pause, resume and cancel: start the slow copy without waiting for it. It prints `run R-... started` and takes about 40 seconds.
   ```bash
   ds run /var/lib/deltasleuth/p0/linker-slow.yaml --no-wait
   ```
   Meanwhile, in the browser, click "Runs", click the run's id in the column "Run", and press "pause", "resume" and "cancel" in turn. The badge beside the heading reads `paused`, then `running`, then `cancelled`.
4. Soak: run the soak copy, whose lines `cycleInterval: 30s` and `stop: {maxCycles: 2}` under `execution:` make two cycles. You should see, after about 37 seconds, `completed (stop_condition:maxCycles)`. In the browser, a soak is "Mode" `soak` in "New run", and its page adds the table "Outcome changes across cycles".
   ```bash
   ds run /var/lib/deltasleuth/p0/linker-soak.yaml --soak
   ```
5. `ds runs --limit 4` lists the newest runs with their ids. With `--fail-on-diff`, `ds run` and `ds compare` exit with status 1 when a key is different, so a pipeline can stop on it.

## Compare tables and files
A table spec pairs the rows of two tables by a key column, compares their columns, and reports every key whose rows differ or that only one table holds. A side names a `table` in place of a `request`.

1. Start a practice PostgreSQL on the installation's network. Its password goes into a file.
   ```bash
   (umask 077; openssl rand -hex 24 > deploy/secrets/practice_db_password)
   chmod 444 deploy/secrets/practice_db_password
   docker run -d --name deltasleuth-practice-db --network deltasleuth_default -e POSTGRES_PASSWORD_FILE=/run/secrets/practice_db_password -v "$PWD/deploy/secrets/practice_db_password:/run/secrets/practice_db_password:ro" postgres:16-alpine
   ```
2. Wait five seconds. Then, once only, load the example data (two `NOTICE:` lines are normal), write each database's connection string into a secret file, and hand both files to the app. The targets file already declares the two databases as `crm-legacy` and `platform-users`.
   ```bash
   docker exec -i deltasleuth-practice-db psql -q -U postgres -v ON_ERROR_STOP=1 -f - < examples/tables/seed.sql
   (umask 077; { printf 'postgresql://postgres:'; tr -d '\n' < deploy/secrets/practice_db_password; printf '@deltasleuth-practice-db:5432/ds_demo_legacy'; } > deploy/secrets/crm_legacy_dsn)
   (umask 077; { printf 'postgresql://postgres:'; tr -d '\n' < deploy/secrets/practice_db_password; printf '@deltasleuth-practice-db:5432/ds_demo_platform'; } > deploy/secrets/platform_users_dsn)
   chmod 444 deploy/secrets/crm_legacy_dsn deploy/secrets/platform_users_dsn
   sed -i.bak 's|^COMPOSE_FILE=.*|&:examples/tables/compose.yaml|' .env
   rm .env.bak
   docker compose --profile mocks up -d
   ```
3. Write the smallest table spec, copy it into the container and run it. Without a list of keys, a run scans both tables. You should see `completed` and, last, `MATCH 985, MISMATCH 4, NOT_FOUND_A 1, NOT_FOUND_B 11`.
```bash
mkdir -p practice && cat > practice/customers.yaml <<'EOF'
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
4. Click "Runs", then the run's id, and read the scan line, which starts `Table scan (done): 1,000 rows on A, 990 on B; 985 identical; 16 to compare; 0 not compared (NULL, empty or over-long key).` Identical rows are only counted. The 16 other keys were read again on both sides and are the ones reported.

| A table spec can also say | A line of it |
|---|---|
| A row filter: only some rows of a side are compared | `where: "status <> 'closed'"` in the side's `table` |
| A key map: the sides write a key differently, such as `C-00100005` and `100005` | `{name: user_id, keyMap: {kind: prefix_pad, prefix: "C-", width: 8, sideType: integer}}` in place of `user_id` in `keyColumns` |
| A type map: the sides store a column in two types | `typeMap: {created_at: {as: timestamp, naiveTimezone: Europe/London}}` in side a beside `table`, `typeMap: {created_at: {as: timestamp}}` in side b, and `created_at` in both `columns` |
| A cap on the scan: stop at the fifth key to compare | `execution: {stop: {maxKeys: 5}}` at the top level |

**A pre-check** counts and adds up two tables before any row is compared. Run `ds precheck run examples/tables/spec.yaml`, or use the screen "Pre-checks". You should see the pair with its verdict: `1  crm-customers-vs-platform-users  rows differ: side a 989, side b 990 (1 fewer on side a)`. **The schema guard** holds a run when a table it reads changed since the last run of its spec, for example in a column's type. The run reads `paused`, and "Resume (accept the changes)" or "cancel" on its page decides. Step B of the demo shows it.

**MySQL and Oracle.** A side can be a MySQL or Oracle table, and the spec stays a table spec. Model the target on `billing-mysql` or `ledger-oracle` in [targets.example.yaml](../deploy/config/targets.example.yaml), with its password and certificate in secret files. `ds target check <target id>` must print a line that ends `session ok`; it reports what a server lacks before a run is tried. Observed on MySQL 8.4 and Oracle 23 (MySQL 8.0 and Oracle 19c are supported but were not observed: run the kits in `examples/tables/mysql` and `examples/tables/oracle` first). Oracle is reached over plain TCP or TCPS. MariaDB and SQL Server are not supported.

**Files.** A side can be a folder of CSV or Parquet files, read in place.

1. Write the practice export: a CSV file and a Parquet file with planted differences.
   ```bash
   mkdir -p practice/exports/kit
   chmod 777 practice/exports/kit
   docker compose run --rm --no-deps -v "$PWD/examples/tables/files:/kit:ro" -v "$PWD/practice/exports/kit:/export" app python /kit/generate.py /export
   chmod 755 practice/exports/kit
   ```
2. Add the first line below under `app:` in `deploy/docker-compose.site.yml`, where it shows the folder to the app read-only. Add the second as the last line of `deploy/config/targets.yaml`. Keep the leading spaces. `settle_s: 0` reads files just written; without it a file must have been left alone for 60 seconds.
```yaml
    volumes: ["../practice/exports/kit:/var/lib/deltasleuth/files/kit:ro"]
  kit-files: {engine: files, root: /var/lib/deltasleuth/files/kit, settle_s: 0}
```
3. Check the target, which also drafts the CSV's layout for a spec. Apply both files and run [the bundled spec](../examples/tables/files/spec.yaml), whose sides name their files in a `file` block under `table`. The run ends `INCONCLUSIVE 1, MATCH 995, MISMATCH 3, NOT_FOUND_A 1, NOT_FOUND_B 1`.
   ```bash
   ds target check kit-files --table accounts_a/accounts.csv --format csv --header --count
   docker compose --profile mocks up -d
   ds run examples/tables/files/spec.yaml
   ```

## Results, reports and readiness
- **A run's page** ("Runs", then the run's id) shows the counts by outcome, then three tables. "Root causes" groups the findings that look alike, the gravest first: start there. A root cause's id narrows the page to its findings. "Results" has one row for each key. "Findings" has one row for each single difference, with its severity (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW` or `INFO`), its value on each side and the known-difference rule that covers it. A difference of format only is `INFO` and leaves the key a `MATCH`. The bar above the tables filters them, and "clear" resets it.
- **A key's page** (click a key in any table) shows both sides in one shape under "Canonical A and B (masked)", then the key's "Findings" and "Observations": each read with its HTTP status, latency and attempts. "Compare now" reads the key again. A tokenised value shows as a stand-in, and equal values get equal stand-ins.
- **Reports** are buttons on the run's page. `ds report <run id> --out <folder>` writes the files of the first three rows below, and `--kind NAME` after it writes one other file.

| Report | What it holds |
|---|---|
| `report.pdf` | The report to read and pass on |
| `discrepancies.csv` | One row for each difference of a key that counts as different: the list to work through |
| `retry.csv`, `coverage.csv` | One row for each key without a final judgement, the keys to run again; and the whole cycle in one row, the counters |
| `root_causes.csv`, `normalization.csv`, `suppressed.csv` | One row for each cause, for each difference of format only, and for each difference that a known-difference rule covers |
| `graph.csv`, `readiness.csv` | The rows of a graph comparison, and one row for each ended cycle of the pair's runs |
| `report.md`, `report.html` | The summary, the same text in two formats: `--kind summary`, which the run's page does not offer |

- **Readiness** lists every pair, which is a spec name that has a batch or soak run, with its state and the gates it does not meet. A new cycle shows within about a minute. `ds readiness` prints the same: a line that starts `(no programme):`, then each pair with a figure such as `2 of 5 gates`. Readiness is a set of named gates, each met or not on what was observed. It is not a probability and not a forecast.
- **The cut-over brief** ("Readiness", a pair's name, then "Cut-over brief") shows one pair on one screen: its gates, the stored figures of its newest complete cycle, a trend of its last 30 measured cycles, and its root-cause groups as opened, still open or closed. Closed means not observed in the newest complete cycle, not fixed. The buttons "brief.pdf", "brief.csv" and "causes.csv" save it. So does `ds readiness report --pair <pair> --out /var/lib/deltasleuth/p0/<folder>`.

## Sign-off, sampling and approvals
- **Approvals** need single sign-on and two different people. A spec version that shows values in clear, declares known differences, filters what is compared or draws a sample needs a second person's approval. A spec author presses "Request approval" in its row of "Specs". A person with the role `rule_approver` who did not write it opens the request on "Approvals" and presses "Approve" or "Reject". With token sign-in such a run goes ahead and is labelled unapproved.
- **Sign-off** accepts one ended cycle as evidence. An operator presses "Request sign-off" under "Sign-off" on the run's page, and a person who neither wrote the spec, started the run nor asked for the sign-off approves it. With a signing key in the `packs` block of the settings, "Issue evidence pack" then writes one signed ZIP, which `ds packs verify <file>.zip --fingerprint <hex>` checks offline. Validate the spec first: with the warning `key_pattern_empty`, a spec never gets an evidence pack.
- **Sampling** serves a pair with too many keys to compare them all. A `key.source` of `kind: hash_sample` with `method: bottom_k` draws a random sample of a fixed size, and the run's page shows each stratum's bound and verdict under "Sample". `ds samples size --tier standard` prints the size: 2,995 cleanly compared keys without a discrepancy show a rate of at most 0.1% at 95% confidence (`--tier high`: 29,956 keys for 0.01%). Fix the size before any key is compared, and never extend it. `ds samples` lists every draw.
- **Labelled accuracy** checks the comparison itself against a source of truth. Under "Labelled accuracy" on the run's page, an operator names the truth source, presses "Draw", downloads the sheet, labels every item and uploads it. `ds labellings` lists the results.
- Single sign-on is three things: the client secret in `deploy/secrets/oidc_client_secret`, the ten lines that start at `#   oidc:` in the `auth:` block of `deploy/config/deltasleuth.yaml` with the `# ` at their start removed and your provider's values in them, and the secret's name in `deploy/docker-compose.site.yml`. `ds auth check` checks them, and `docker compose --profile mocks up -d` applies them.

## Schedules and notifications
A schedule starts runs of a stored spec by itself, at cron times in a time zone.

1. Schedule the practice pair for two every night. You should see a line that starts `added SC-` and names the next 02:00. `--spec` follows the name's newest stored version, and `--spec-id <n>` pins one version. `ds schedules` lists every schedule with its next due time, and `ds schedules pause`, `resume` and `delete` take a schedule's name.
   ```bash
   ds schedules add nightly-customers --spec mock-customer-v1-vs-v2 --cron '0 2 * * *' --tz Europe/London
   ```
2. Or, in the browser: click "Schedules". In "New schedule", type `nightly-customers` in "Name", choose the spec in "Spec", leave "Runs" at `daily`, set "at" to 02:00 and press "Add". You should see "Added SC-… (nightly-customers)." above the form. Only a running app starts scheduled runs.

A notification is a short message to a webhook, Slack, Teams or e-mail when an event you chose happens, such as a cycle that ends with a new root cause, a run that paused or failed, or a schedule that missed its time.

1. Add a `notify` block at the end of `deploy/config/deltasleuth.yaml`: channels, then rules that route events to them. A webhook needs a signing key: a secret file whose content starts with `whsec_`, handed to the app as [docs/deployment.md](deployment.md) describes for every secret.
```yaml
notify:
  label: trial
  channels:
    ops-hook: {kind: webhook, url: "https://events.example/deltasleuth", signing_key_refs: [file:/run/secrets/notify_hook_key]}   # change: the url
  rules:
    - {name: new-gaps, events: [cycle.ended], channels: [ops-hook], when: {new_root_causes_at_least: 1}}
```
2. Run `ds notify check`, which sends nothing and must print no line that starts with `error:`. Apply the block with `docker compose --profile mocks up -d`. Then send a test with `ds notify test ops-hook`, or click "Notifications" and press "Send test" in the channel's row. You should see a line such as `sent to ops-hook: HTTP 200 in 2 ms (N-...)`.

## AI chat and the MCP server
Both are optional, and everything else works without them. The chat answers questions about a run in plain words. It needs a model endpoint that speaks the OpenAI Chat Completions API with tool calling. Without one, a run's page reads "AI is off".

1. In `deploy/config/llm_profiles.yaml`, write the endpoint's host in the first line under `hosts:`, and its address and model in the lines `base_url` and `model` of the profile `gateway`. Put its key into the file `deploy/secrets/llm_key` and name the secret `llm_key` in `deploy/docker-compose.site.yml`.
2. In `deploy/config/deltasleuth.yaml`, remove the `# ` at the start of the three lines `llm_profiles_file: llm_profiles.yaml`, `llm_profile: gateway` and `llm_environment: prod`. Run `ds llm-probe`. When its line says `tool_roundtrip=pass`, apply the change with `docker compose --profile mocks up -d`.
3. A run's page then has the button "Ask AI about this run", and a key's page "Ask AI about this key". In "Root causes", "Explain" in a row and "Summarise this run" beside the heading write an AI draft. Its panel shows the facts the comparison stored beside the draft, and each sentence cites the ids it rests on. A panel headed "No AI draft" means that the answer could not be checked and was withheld. An AI answer is never evidence.

The MCP server lets an AI agent of your own read runs and findings. It needs no model.

1. In `deploy/config/deltasleuth.yaml`, remove the `# ` at the start of the line `# mcp:` and of the five lines under it. To serve runs whose spec declares clear values or a business-code key, such as the practice runs, also add `production_clear_declared` to the list `data_classes`. Run `ds mcp check`, which lists the tools a viewer and an operator are offered, then `docker compose restart app`.
2. Create a token with the role `viewer` for the agent, straight into a file. Point the agent at `http://localhost:8000/v1/mcp` with the header `Authorization: Bearer <token>`.
   ```bash
   (umask 077; set -C; docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds token create --name my-agent --owner "$(id -un)" --role viewer --days 30 > .deltasleuth/tokens/my-agent)
   ```

## Where is it
Each feature with its screen and its `ds` command. "Not offered" marks what the browser does not have.

| Feature | In the browser | `ds` command |
|---|---|---|
| Store and validate a spec | "Specs", panel "Upload a spec" | `ds spec validate`; `ds run` and `ds compare` store the file they are given |
| Draft a spec from sample answers | "Specs", panel "Draft a spec from sample pairs" | `ds profile` |
| Declare a target | "Targets": "Declare an endpoint", for a service | none: the file `deploy/config/targets.yaml`, for every kind; `ds target check`, for database and files targets |
| Build a comparison and try each side's request | "Specs": "New comparison", or a stored spec's name; "Import curl…", "Send", "Save and compare this key" | none |
| Start a run, batch or soak; pause, resume and cancel it | "Runs", panel "New run"; the run's page: "pause", "resume", "cancel" | `ds run` starts one |
| Read a run: counts, root causes, results, findings | "Runs", then the run's id | `ds runs` lists the runs; the rows are in the reports |
| One key, side by side, and compared again now | the key's page, from a table of the run; "Compare now" | `ds compare` |
| Reports | the run's page: nine buttons | `ds report` |
| Table, MySQL, Oracle and file sides | the same screens; the run's page adds the line `Table scan (...)` | `ds run`, `ds target check` |
| Table pre-check; the schema guard | "Pre-checks", or "Pre-check…" in a row of "Specs"; the paused run's page: "Resume (accept the changes)", "cancel" | `ds precheck`; none for the guard |
| Schedules | "Schedules", or "Schedule…" in a row of "Specs" | `ds schedules` |
| Notifications | "Notifications", in the bar once they are switched on | `ds notify` |
| AI chat and AI drafts | a run's page: "Ask AI about this run", "Explain", "Summarise this run" | `ds llm-probe`, which checks the model only |
| MCP server for an agent of your own | not offered | `ds mcp check`, `ds mcp` |
| Sampled runs, labelled accuracy | the run's page: "Sample", "Labelled accuracy" | `ds samples`, `ds labellings` |
| Readiness and the cut-over brief | "Readiness"; a pair's name, then "Cut-over brief" | `ds readiness`, `ds readiness report --pair` |
| Approval, sign-off, evidence pack (single sign-on) | "Approvals"; the run's page: "Sign-off", "Issue evidence pack" | `ds approvals`, `ds packs` |
| Tokens, sessions, the audit log | not offered with token sign-in; "Admin" under single sign-on | `ds token`, `ds session revoke`, `ds audit`, `ds audit verify` |
| Retention (hold and erase); the licence | the run's page shows only until when data is kept; "Licence" | `ds purge`; `ds licence` |

## A 15-minute demo
The steps below show DeltaSleuth on the bundled practice data. Steps 1 to 8 take 15 minutes. Steps A to C are extras to choose from.

1. Beforehand, do "Your first comparison", steps 1 and 2, and "Compare tables and files", steps 1 to 3.
2. Just before, start everything and rehearse one run. The last line is `KNOWN_DIFF 2, MATCH 35, MISMATCH 2, NOT_FOUND_B 1`.
   ```bash
   docker compose --profile mocks up -d
   docker start deltasleuth-practice-db
   ds run mocks/customer/spec.yaml
   ```
3. Open a browser at `http://localhost:8000`, signed in and on the page "Runs", a terminal in the repository's folder, and `mocks/customer/spec.yaml` in an editor. The page asks for the token again after 30 idle minutes.

| # | Min | Do | What it shows |
|---|---|---|---|
| 1 | 2 | In "New run", choose `mock-customer-v1-vs-v2 1.0.0` under "Spec" and press "Start". Wait about five seconds for `completed`. | An old and a new customer service hold the same 40 customers. 35 agree, 2 differ without an explanation, 1 is missing on the new side, and 2 differ in a way the spec declares as known. |
| 2 | 2 | Read the table "Root causes", then "Findings". | Differences that look alike are grouped into one root cause, the gravest first. |
| 3 | 2 | In the first row of "Root causes", click the key under "Sample keys". Tick "differences only". | One customer, side by side. One field differs: a marketing consent is granted on the old side and withdrawn on the new one. Names, addresses and the customer number show only as stand-ins. |
| 4 | 1 | Click the link "Run R-..." back to the run, press "report.pdf" and open the saved file. | The same result as a PDF to pass on. The other buttons give CSV files. |
| 5 | 2 | In the editor, scroll `mocks/customer/spec.yaml`: `key`, `sides`, `compare`, `knownDiffs`, `security`. | Everything that was compared is in one file: how to ask each side, what counts as equal, which differences are known and why, and what is masked. |
| 6 | 3 | Click "Runs", choose `practice-customers 1.0.0` under "Spec" and press "Start". Read the line that starts `Table scan (done)`, then "Root causes". | The same works on two database tables. One scan read 1,000 rows on side A and 990 on side B, found 985 identical and reported the other 16 keys: 11 missing on side B, 1 only there, 4 with different values. |
| 7 | 1 | In the terminal: `ds run mocks/customer/spec.yaml --fail-on-diff`, then `echo $?`, which prints `1`. | The same from the command line. With this option the command fails when keys differ, so a pipeline can stop on it. |
| 8 | 2 | Click "Readiness", then a pair's name, then "Cut-over brief". | Named gates say what is still missing before sign-off. The brief restates what the newest complete cycle measured, with no score and no forecast. |
| A | 3 | Click "Pre-checks". Under "New table pre-check" choose `practice-customers 1.0.0` in "Specs" and press "Pre-check". | A pre-check counts and adds up both tables before any row is compared. Its verdict reads `rows differ: side a 1,000, side b 990 (10 fewer on side b)`. |
| B | 4 | Change a column's type with the command below. Start `practice-customers 1.0.0` again on "Runs": the run reads `paused` and names the column. Press "Resume (accept the changes)": it completes. | The schema guard holds a run before a single row is read, says what changed, and a person decides. |
| C | 4 | Do "Compare your own two services", in the browser, steps 2 and 3, as printed. | Two services need no file. "Send" asks one endpoint once for one key and stores nothing. One press stores the comparison and compares the key. |

```bash
docker exec deltasleuth-practice-db psql -U postgres -d ds_demo_legacy -c 'ALTER TABLE crm.customers ALTER COLUMN country TYPE text'
```
The guard holds a run once for a change. To show step B again, run the command with `character(2)` in place of `text`, start the run and accept.

If the page asks for the token again, paste it again. If it says `cross-origin request refused`, the address must be `http://localhost:8000`, typed exactly so. If the table run of step 6 reads `paused`, a table changed since its last run: press "Resume (accept the changes)".
