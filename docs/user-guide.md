# DeltaSleuth user guide

DeltaSleuth compares what two sources hold and reports the differences key by key. A source can be a REST service, a database table in PostgreSQL, MySQL or Oracle, or a folder of CSV or Parquet files. It reads only what a source answers, so how the source is built never matters.

Sections: [Install](#install), [Your first comparison](#your-first-comparison), [Outcomes](#outcomes), [Use a spec file](#use-a-spec-file), [The parts of a spec](#the-parts-of-a-spec), [Runs](#runs), [Compare tables and files](#compare-tables-and-files), [Results, reports and readiness](#results-reports-and-readiness), [Sign-off, sampling and approvals](#sign-off-sampling-and-approvals), [Schedules and notifications](#schedules-and-notifications), [AI chat and the MCP server](#ai-chat-and-the-mcp-server), [Where is it](#where-is-it), [Demo with your own data](#demo-with-your-own-data).

## Install
Start the application and sign in using the [deployment guide](deployment.md). A fresh installation has no targets or comparisons. Keep your specs, keys and exports outside the checkout. From the checkout root, define this shortcut for the CLI in each new terminal.

```bash
alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
```

A viewer reads results. An operator manages runs and schedules. A spec author can also store comparisons. Under single sign-on, a rule approver can approve another person's spec and an administrator manages access. Replace angle-bracket placeholders throughout this guide before running commands.

## Your first comparison

Choose two read-only sources you may compare and one key that both can look up. For database tables or file exports, follow [Compare tables and files](#compare-tables-and-files). For two REST endpoints, use the browser:

1. Open **Targets → Declare an endpoint**. Give side A a target ID and enter its base URL and authentication method. Repeat for B. For a service on the Docker host, use `host.docker.internal` and enable **Private or local network**. A spec author can declare endpoints in a local token trial. With single sign-on this takes a recently authenticated administrator. For other token deployments, ask the operator to declare the targets in the settings file.
2. Open **Comparisons → New comparison**. Enter your sample key. For each side choose its target and supply the relative lookup path, using `{{ key }}` at the record ID. Use **Import curl…** if you already have a request. Credentials belong to the target.
3. Press **Send** on each side. Check the HTTP status and the structure of the answer. This makes one preview request per side. In a local trial the preview can show raw values. Shared installations show structure with masked values.
4. Under **Everything else (YAML)**, set your comparison name, version, owner and key pattern. Supply a key source only if the comparison should choose its own keys. Use `key.class: identifier` to mask record identifiers. Check any truncation declaration against the actual response. If the answers wrap the same record differently, use each side's `select` or `map` to align them.
5. Choose **Validate**, resolve errors, then **Save and compare this key**. Complete any required approval on **Comparisons**. Use **Runs** to open the result and both observations. Confirm the expected response was read from each side before starting a batch.
6. For a batch, open **Runs → New run**, select your saved comparison and enter a small list in **Keys, one per line**, or use the key source you defined. Select **Batch** and **Start run**. Review the counts when the run ends.

## Outcomes
Each key of a run gets one outcome. The screens show it in words, such as "Not found in B", with its code as the tooltip; the reports and the command line print the code, `NOT_FOUND_B`. Under the counts of a run, "What the outcomes mean" says each in a line. For `INCONCLUSIVE` and `DRIFT`, the column "Why" gives the reason, such as "Truncated", "Absence unconfirmed" or "Absent both".

| Outcome of a key | In plain words | Counts as |
|---|---|---|
| `MATCH` | Both sides answered in full, and nothing that matters differs. | a match |
| `MISMATCH` | Both sides answered in full, and at least one real difference has no explanation. | different |
| `KNOWN_DIFF` | The sides differ, and a rule in the spec says that every difference is known. | a match, or different when the rule calls it a bug |
| `NOT_FOUND_A`, `NOT_FOUND_B` | One side has the record and the other says clearly that it has none. The letter is the side that lacks it. | different |
| `INCONCLUSIVE`, `DRIFT` | Nothing can be said. A side could not be read in full, neither side holds the key, or a side changed between the first read and the second. | neither |

## Use a spec file

Declare connections in the deployment's private `deploy/config/targets.yaml`. Replace the empty `targets: {}` with a mapping keyed by your target IDs. For each REST service, set `base_url` and `auth.kind` (`none`, `bearer` or `header`). For authenticated requests, use a mounted `file:` secret reference as described in the [deployment guide](deployment.md). Set `allow_private_network: true` for a private address. After editing the targets file, restart the app. Recreate it with `docker compose up -d` if you also changed Compose mounts or secrets.

Save the outline below as `comparison.yaml` outside this repository. Replace every placeholder. The lookup path on each side must begin with a single slash. Use a key pattern that matches your actual keys. For each side provide the JSONPath of its actual truncation flag, or replace the block with its declared list cap and items/count paths. A declaration on the target can supply this information. This outline leaves keys outside the spec. Enter them in the browser or use a separate local file.

```yaml
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata: {name: "<comparison-name>", version: "1.0.0", owner: "<owner>"}
normalizerVersion: "1.1.0"
key:
  name: record_id
  type: string
  class: identifier
  pattern: '<key-pattern>'
sides:
  a:
    role: baseline
    target: "<first-target-id>"
    request: {method: GET, path: "/<first-lookup-path>/{{ key }}"}
    truncation: {flagPaths: ["<first-truncation-flag-JSONPath>"]}
  b:
    role: candidate
    target: "<second-target-id>"
    request: {method: GET, path: "/<second-lookup-path>/{{ key }}"}
    truncation: {flagPaths: ["<second-truncation-flag-JSONPath>"]}
compare: {}
recheck: {delay: 5s, times: 1}
```

Upload the file under **Comparisons → Upload a comparison (YAML or JSON)**, validate it and save it. For the CLI, save the keys to `keys.txt` beside your private spec, one per line. Copy both into the deployment's shared input volume. These commands write them with the app user's permissions.

```bash
INPUT_DIR='/absolute/path/to/your/private-inputs'
SPEC=/var/lib/deltasleuth/p0/comparison.yaml
KEYS=/var/lib/deltasleuth/p0/keys.txt
docker compose exec -T app sh -c 'umask 077; cat > /var/lib/deltasleuth/p0/comparison.yaml' < "$INPUT_DIR/comparison.yaml"
docker compose exec -T app sh -c 'umask 077; cat > /var/lib/deltasleuth/p0/keys.txt' < "$INPUT_DIR/keys.txt"
ds spec validate "$SPEC" --targets /etc/deltasleuth/targets.yaml
ds run "$SPEC" --keys "$KEYS"
```

The optional `--targets` check uses only targets declared in that file. If you declared them in the browser, omit the option. The running service resolves its registered targets when you save or run a comparison. To repeat a single lookup, use `ds compare '<your-key>' --spec "$SPEC"` or **Compare now** on its result page.

## The parts of a spec

The [spec schema](../deltasleuth/core/spec/spec.schema.json) defines the required fields and their accepted values. Validate the file after editing it.

| Part | What it says | Also |
|---|---|---|
| `key` | The key's label, its type, the `pattern` every key must fit, and where the keys come from (`source`). | `source.kind` is `list`, `paginated_endpoint` (a service's listing) or `table` (a table, a view or a file of keys on a declared target, at most 1,000,000 keys). A run can take a file of keys instead. `class: identifier` masks the key itself, and a stored comparison then shows its inline keys as `masked key 1 of N`: type them again to save the text. Inline identifiers remain in the stored spec. Use a separate key file for sensitive keys. New runs use a separate key token (`tok:identifier:k2:…`) for each distinct spelling after the spec’s normalisers. Older runs keep their original tokens. Start a fresh rolling soak if its source distinguishes Unicode spellings. `type: composite` with `parts: [region, shelf, code]` is a key of several values: a request names a part as `{{ keyParts.region }}`, and a key is written as its parts joined by `\|`. |
| `target`, `request` | Which declared target a side asks, and how: `method` (`GET` or `POST`), `path`, and if needed `query`, `headers` and a JSON `body`. | `{{ key }}` stands for the key in each of them. A credential never goes here: it belongs to the target. |
| `select`, `map` | `select` picks the record inside the answer. `map` gives both sides one shape, so that equal things have equal names. | Only what `map` writes is compared. A `map` that calculates with a number of more than 15 significant digits or an integer outside ±(2⁵³−1), or writes one as text, leaves that side `INCOMPLETE`, since JSONata keeps 15; passing such a value through unchanged is exact. Numeric literals, conversions with `$number` and intermediate integers must also fit these bounds. Fractional arithmetic uses binary floating point; pass monetary values through to compare them as exact decimals. A regular expression in `map`, or in a `match()` or `search()` filter of a path, may not nest a repeat inside a repeat (`(x+)+`; write `x++` or `(?>x+)`), nor in `map` use `\B`, a back-reference, a conditional or a scoped flag, and reads at most 10,000 characters of a string within its bound (a map's 2 seconds, a path's 1 second), or the side is `INCOMPLETE`. |
| `notFound`, `truncation` | Which answer means that the side has no such record. How to tell that a list was cut: where the answer counts the list (`countPath`), where the list is (`itemsPath`) and the most the service returns (`cap`). | A cut answer makes the key `INCONCLUSIVE`, never a false difference. `flagPaths: ["$..more"]` checks every page for the service's own truncation flag. A declared total must be a non-negative integer on the first page. Later pages may omit it or repeat the same total. |
| `compare` | `arrays` says how the members of a list pair: `keyed`, `set`, `ordered` or `multiset`. `fields` gives a path its clean-ups (`normalize`), a `severity` and a tolerance. `exclude` leaves paths out. | Clean-ups include `trim`, `upper`, `lower`, `collapse_ws`, `nfkc`, `email_basic`, `digits_only`, `e164`, `decimal` and `timestamp_utc`. A tolerance: `compare: {tolerance: {abs: 0.000001}}`. |
| `knownDiffs` | Differences that are already known. Each rule has `id`, `when`, `classification`, `reason` and `ticket`. | A key whose every difference a rule covers reads `KNOWN_DIFF`. With `classification: bug` it still counts as different. |
| `recheck` | A key that differs is read again after `delay`, at most `times` times, before its outcome is final. | A table scan selects candidates. Their first detailed read starts the same recheck policy as an explicitly supplied key. Fixed files retain their pinned observation. |
| `execution` | `concurrency` is the number of keys in flight at once on a side. `delay` is the pause between keys. | The request rate belongs to the target: "Requests a second" and "At once" on "Targets", or `rate_per_s` and `max_concurrency` in the targets file. `cycleInterval` and `stop` shape a soak run; a late soak skips the cycle starts it missed. |
| `security` | `tokenize` shows a value as a stand-in of its class. `clear` lists the paths whose values may be shown as they are. | Unlisted values, including booleans, and unknown field names are masked. With single sign-on, an unapproved run shows only trusted table catalogue labels. A second person can approve the spec to expose its declared labels. Classes: `name`, `phone`, `email`, `address`, `account_number`, `device_id`, `date_of_birth`, `tax_id`, `identifier`, `free_text`, `free_number`. |

## Runs
A document's exact numeric text has a 64 MiB aggregate expansion budget, including mapped values and
merged pages. Nonzero numbers have an adjusted exponent between -4,000 and 4,000; zero remains zero at
any exponent. A body holds at most 2,000,000 values (each number, string, array and object, its pages
merged), and a key listing 8,000,000. A service's answer is at most 64 MiB, its pages together; a target
may set a smaller `max_body_bytes`. Exceeding a numeric or body limit produces
`INCOMPLETE(resource_limit)`, never a match.

Batch mode processes the selected keys once. Soak mode repeats the comparison in cycles. For service comparisons, supply `--keys "$KEYS"` as [above](#use-a-spec-file) or configure `key.source`.

- Add `--no-wait` to submit work to the running worker. `ds runs --limit 4` lists recent runs. Use the run page to pause, resume or cancel. An evidence disk error also pauses the run. Restore writable space, then resume.
- For a bounded soak, set `execution.cycleInterval` and `execution.stop.maxCycles` in your spec, validate it, and run `ds run "$SPEC" --keys "$KEYS" --soak`. To reread a configured key source each cycle, omit `--keys` and add `--rolling`. **Changes across cycles** shows changes since the previous cycle.
- In a pipeline, `ds run "$SPEC" --keys "$KEYS" --fail-on-diff` returns status 1 for differences or outstanding keys. `ds compare --fail-on-diff` returns status 1 for a key judged different.

## Compare tables and files

A table comparison pairs rows by key. It needs a readable target for each side and the table name and key columns on both. A file export uses the same comparison rules and a read-only directory as its target.

1. **Declare database targets.** In `deploy/config/targets.yaml`, give each PostgreSQL connection a `dsn_ref` pointing to a mounted secret file. For MySQL or Oracle, set the target's `engine`, `host`, `port`, `user`, `database` (MySQL) or `service` (Oracle), `password_ref` and appropriate TLS settings. Use credentials with read access to the chosen objects. Set `allow_private_network: true` if required. Mount secrets as described in the [deployment guide](deployment.md).
2. **Declare file targets.** Mount each export directory read-only into the app under `/var/lib/deltasleuth/files/` using `deploy/docker-compose.site.yml`, for example `/absolute/path/to/your/export:/var/lib/deltasleuth/files/input:ro`. The container user (UID 10001) needs read access. Under `targets`, set `engine: files` and `root` to that container path. Mount the same path in any additional worker. Stable files must have been settled for 60 seconds by default.
3. Apply changed mounts with `docker compose up -d`, then restart the app if the target file changed. For each database run `ds target check '<target-id>' --table '<schema>.<table>'` and check for `session ok`. For each file target run `ds target check '<target-id>' --table '<relative-path>' --format csv --header` (for Parquet use `--format parquet` and omit `--header`). For CSV, use the returned dialect and column list to draft the spec.
4. In the [spec outline](#use-a-spec-file), replace each service-side `request` and `truncation` with a `table` block. Supply `name`, `schema` if needed and `keyColumns` in key-part order. Set `columns` to the subset you want, using `{name: "<column>", as: "<common-name>"}` to align differently named columns. For a file side also set `table.file.path` relative to its root and `table.file.format`. CSV needs its declared `table.file.csv` layout, including its delimiter and columns in file order.
5. To scan a table pair, leave out `key.source` and run `ds run "$SPEC"` after copying and validating the edited file. Identical rows are counted in bulk. The run shows scan totals, including keys skipped as uncomparable, and stores detailed results for candidate keys. For a restricted key list use `--keys` as described above.

**Pre-check and schema guard.** `ds precheck run "$SPEC"` or **Pre-checks** compares aggregates before a row-level run. The schema guard pauses a comparison when a participating table's schema changes. Review the change on the run page before accepting it or cancelling.

**Table options.** Use `table.where` for a supported row filter, `keyMap` on a key column when the sides encode their keys differently, and `typeMap` beside `table` to align types. `execution.stop.maxKeys` bounds keys needing detailed comparison. At its final key two rows on either side prove a duplicate. Scan totals cover consumed rows. Time limits are checked between batches and can stop inside a duplicate group. On resume that key is read again.

**Scan cost.** Tested database versions include MySQL 8.4 and Oracle 23. Check the actual engine and version with `ds target check`. On MySQL, eligible byte-ordered text or binary keys can stream through an index. Whole-number keys that qualify on both sides can stream in numeric order. Other key types may need a sort on each pass and resume. Budget temporary space for large tables.

**File limits.** Each path expansion visits at most 200,000 directory entries and has a 30-second deadline. Matching also stops at `files.max_files` (1,000 by default, up to 20,000). Narrow the export path if you reach a limit. Later checks apply the same bounds and pause the run on an entry-limit refusal.

## Results, reports and readiness
- **A run's page** ("Runs", then the run's id) shows the keys by outcome as a bar with its counts, then tabs. "Not matching" lists the keys whose outcome is not a match, by outcome. "Root causes" groups the findings that look alike, the gravest first: start there. A root cause's id opens "Findings" narrowed to its findings. "All results" has one row for each key. "Findings" has one row for each single difference, with its severity (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW` or `INFO`), its value on each side and the known-difference rule that covers it. A difference of format only is `INFO` and leaves the key a `MATCH`. The filters above a tab's table narrow it, and "Clear filters" resets them.
- **A key's page** (click a key in any table) shows both sides in one shape under "Canonical A and B (masked)", then the key's "Findings" and "Observations": each read with its HTTP status, latency and attempts. "Compare now" reads the key again. A tokenised value shows as a stand-in, and equal values get equal stand-ins.
- **Reports** are buttons on the run's page. `ds report <run id> --out <folder>` writes the files of the first three rows below, and `--kind NAME` after it writes one other file.

| Report | What it holds |
|---|---|
| `report.pdf` | The report to read and pass on; its AI annex includes up to 20 cited drafts from the cycle’s fixed Explain and Summarise prompts |
| `discrepancies.csv` | One row for each difference of a key that counts as different: the list to work through |
| `retry.csv`, `coverage.csv` | One row for each key without a final judgement, the keys to run again; and the whole cycle in one row, the counters |
| `root_causes.csv`, `normalization.csv`, `suppressed.csv` | One row for each cause, for each difference of format only, and for each difference that a known-difference rule covers |
| `graph.csv`, `readiness.csv` | The rows of a graph comparison, and one row for each ended cycle of the pair's runs |
| `report.md`, `report.html` | The contract report, the same text in two formats: `--kind summary`, which the run's page does not offer |

- **Readiness** lists every pair, which is a spec name that has a batch or soak run, with its state and the gates it does not meet. A new cycle shows within about a minute. `ds readiness` prints the same: a line that starts `(no programme):`, then each pair with a figure such as `2 of 5 gates`. Readiness is a set of named gates, each met or not on what was observed. It is not a probability and not a forecast.
- **The cut-over brief** ("Readiness", a pair's name, then "Cut-over brief") shows one pair on one screen: its gates, the stored figures of its newest complete cycle, a trend of its last 30 measured cycles, and its root-cause groups as opened, still open or closed. Closed means not observed in the newest complete cycle, not fixed. The buttons "brief.pdf", "brief.csv" and "causes.csv" save it. So does `ds readiness report --pair <pair> --out /var/lib/deltasleuth/p0/<folder>`.

## Sign-off, sampling and approvals
- **Approvals** need single sign-on and two different people. A spec version that shows values in clear, declares known differences, filters what is compared, reshapes an answer with `map` or with a `select` of part of it, or draws a sample needs a second person's approval. A spec author presses "Request approval" in its row of "Comparisons". A person with the role `rule_approver` who did not write it opens the request on "Approvals" and presses "Approve" or "Reject". With token sign-in such a run goes ahead and is labelled unapproved.
- **Sign-off** accepts one ended cycle as evidence. An operator presses "Request sign-off" under "Sign-off" in the tab "Evidence and sign-off" of the run's page, and a person who neither wrote the spec, started the run nor asked for the sign-off approves it. With a signing key in the `packs` block of the settings, "Issue evidence pack" then writes one signed ZIP, which `ds packs verify <file>.zip --fingerprint <hex>` checks offline. Validate the spec first: with the warning `key_pattern_empty`, a spec never gets an evidence pack. The tab lists a run's newest 20 ended cycles; "Show older cycles" adds older ones a page at a time.
- **Sampling** serves a pair with too many keys to compare them all. A `key.source` of `kind: hash_sample` with `method: bottom_k` draws a random sample of a fixed size, and the run's page shows each stratum's bound and verdict under "Sample". `ds samples size --tier standard` prints the size: 2,995 cleanly compared keys without a discrepancy show a rate of at most 0.1% at 95% confidence (`--tier high`: 29,956 keys for 0.01%). Fix the size before any key is compared, and never extend it. `ds samples` lists every draw. Below `rule_approver`, a draw's nonce and each stratum's `u_max` are withheld, on its page and in a sign-off's review alike.
- **Labelled accuracy** checks the comparison itself against a source of truth. In the tab "Labelled accuracy" of the run's page, an operator names the truth source, presses "Draw", downloads the sheet, labels every item and uploads it. `ds labellings` lists the results.
- To enable single sign-on, fill the `auth.oidc` settings, mount the client secret and configure the public HTTPS address and trusted proxy. Run `ds auth check` to obtain the redirect URIs. Apply the changes following the [deployment guide](deployment.md).

Reviewer CSV sheets accept at most 256 columns and 1,000,000 padded cells. Stored adjudication and
accuracy uploads also keep their draw limits of 500 and 1,000 rows. A sampling frame may retain at most
16,384 distinct bucket/combined labels totalling 1 MiB of UTF-8 text; exceeding either budget pauses the
draw without storing a partial sample. Reduce strata or compare the population as a census.

## Schedules and notifications
A schedule starts runs of a stored spec by itself, at cron times in a time zone.

Open **Schedules → New schedule**, choose a stored comparison, its local time and time zone, then add the schedule. A service comparison needs a configured key source to run unattended. The app must be running to start a scheduled run. On the CLI, use the comparison's name to follow its newest stored version, or `--spec-id` to pin one version.

```bash
ds schedules add '<schedule-name>' --spec '<comparison-name>' --cron '0 2 * * *' --tz '<IANA-time-zone>'
ds schedules
```

Use `ds schedules pause`, `resume` or `delete` with the schedule's name as needed.

A notification is a short message to a webhook, Slack, Teams or e-mail when an event you chose happens, such as a cycle that ends with a new root cause, a run that paused or failed, or a schedule that missed its time.

1. Add `notify.channels` and `notify.rules` to your settings. Choose an event, channel and routing condition. For a webhook, the channel takes `kind: webhook`, its HTTPS destination and `signing_key_refs` pointing to a mounted secret whose content starts with `whsec_`. For a difference group observed for the first time, choose the event `cycle.ended` and `when: {new_root_causes_at_least: 1}`.
2. Run `ds notify check` to validate the configuration. Apply changed mounts with `docker compose up -d` and reload settings with `docker compose restart app`. Use `ds notify test '<channel-id>'` or **Notifications → Send test** to confirm delivery.

## AI chat and the MCP server
Both are optional, and everything else works without them. The chat answers questions about a run in plain words. Configure a supported model endpoint with tool calling. Without one, the tab "Root causes" of a run reads "AI is off: no LLM profile is configured."

1. Copy `deploy/config/llm_profiles.example.yaml` to `deploy/config/llm_profiles.yaml`. In your copy, write the endpoint's host in the first line under `hosts:`, and its address and model in the lines `base_url` and `model` of the profile `gateway`. Put its key into the file `deploy/secrets/llm_key` and name the secret `llm_key` in `deploy/docker-compose.site.yml`. Enable `production_clear_declared` only for endpoints approved to receive clear values. Chat and MCP require it for runs with clear paths, business-code keys or an older member-name masking policy. Add `analyst_text` only when the endpoint is approved for questions typed by staff. The fixed Explain and Summarise prompts need no such permission.
2. In `deploy/config/deltasleuth.yaml`, remove the `# ` at the start of the three lines `llm_profiles_file: llm_profiles.yaml`, `llm_profile: gateway` and `llm_environment: prod`. Run `ds llm-probe`. When its line says `tool_roundtrip=pass`, apply changed mounts with `docker compose up -d` and reload settings with `docker compose restart app`.
3. A run's page then has the button "Ask AI about this run", and a key's page "Ask AI about this key". In the tab "Root causes", "Explain" in a row and "Summarise this run" beside the heading write an AI draft. Its panel shows the facts the comparison stored beside the draft, and each sentence that states a number or an outcome cites the ids it rests on, whose records hold its numbers. A panel headed "No AI draft" means that the answer could not be checked and was withheld. An AI answer is never evidence. A question holds at most 4,000 characters, and each person asks at most 10 a minute. Each model attempt reserves its budget before dispatch. The profile's `budgets` bound the tokens of every chat about a run (`per_run_tokens`) and of one person's chats in 24 hours (`per_actor_tokens_per_day`, 2,000,000 unless set, `null` for no bound); the answer names a budget that is spent.

The MCP server lets an AI agent of your own read runs and findings. It needs no model.

1. In `deploy/config/deltasleuth.yaml`, remove the `# ` at the start of the line `# mcp:` and of the five lines under it. To serve runs whose spec declares clear values or a business-code key, add `production_clear_declared` to the list `data_classes`. Run `ds mcp check`, which lists the tools a viewer and an operator are offered, then `docker compose restart app`.
2. Create a token with the role `viewer` for the agent, straight into a file. Point the agent at `http://localhost:8000/v1/mcp` with the header `Authorization: Bearer <token>`.
   ```bash
   (umask 077; set -C; docker compose run --rm -T -e DS_OPERATOR="$(id -un)" app ds token create --name my-agent --owner "$(id -un)" --role viewer --days 30 > .deltasleuth/tokens/my-agent)
   ```

## Where is it
Each feature with its screen and its `ds` command. "Not offered" marks what the browser does not have.

| Feature | In the browser | `ds` command |
|---|---|---|
| Store and validate a spec | "Comparisons", panel "Upload a comparison (YAML or JSON)" | `ds spec validate`; `ds run` and `ds compare` store the file they are given |
| Draft a spec from sample answers | "Comparisons", panel "Draft a comparison from sample pairs" | `ds profile` |
| Declare a target | "Targets": "Declare an endpoint", for a service | none: the file `deploy/config/targets.yaml`, for every kind; `ds target check`, for database and files targets |
| Build a comparison and try each side's request | "Comparisons": "New comparison", or a stored spec's name; "Import curl…", "Send", "Save and compare this key" | none |
| Start a run, batch or soak; pause, resume and cancel it | "Runs", panel "New run"; the run's page: "Pause run", "Resume run", "Cancel run" | `ds run` starts one |
| Read a run: counts, root causes, results, findings | "Runs", then the run's id | `ds runs` lists the runs; the rows are in the reports |
| One key, side by side, and compared again now | the key's page, from a table of the run; "Compare now" | `ds compare` |
| Reports | the run's page: nine buttons | `ds report` |
| Table, MySQL, Oracle and file sides | the same screens; the run's page adds the line `Table scan (...)` | `ds run`, `ds target check` |
| Table pre-check; the schema guard | "Pre-checks", or "Pre-check…" in a row of "Comparisons"; the paused run's page: "Resume (accept the changes)", "Cancel run" | `ds precheck`; none for the guard |
| Schedules | "Schedules", or "Schedule…" in a row of "Comparisons" | `ds schedules` |
| Notifications | "Notifications", in the bar once they are switched on | `ds notify` |
| AI chat and AI drafts | a run's page: "Ask AI about this run", "Explain", "Summarise this run" | `ds llm-probe`, which checks the model only |
| MCP server for an agent of your own | not offered | `ds mcp check`, `ds mcp` |
| Sampled runs, labelled accuracy | the run's page: "Sample" in the tab "Evidence and sign-off"; the tab "Labelled accuracy" | `ds samples`, `ds labellings` |
| Readiness and the cut-over brief | "Readiness"; a pair's name, then "Cut-over brief" | `ds readiness`, `ds readiness report --pair` |
| Approval, sign-off, evidence pack (single sign-on) | "Approvals"; the run's page, tab "Evidence and sign-off": "Sign-off", "Issue evidence pack" | `ds approvals`, `ds packs` |
| Tokens, sessions, the audit log | not offered with token sign-in; "Admin" under single sign-on | `ds token`, `ds session revoke`, `ds audit`, `ds audit verify [--anchor FILE]`, `ds db grants`, `ds db setup` |
| Retention (hold and erase); the licence | the run's page shows only until when data is kept; "Licence", a link at the foot of each page where the build needs none | `ds purge`; `ds licence` |

## Demo with your own data

Choose two approved sources and a small set of keys whose expected relationship you know. Keep this selection outside the checkout. If you want to demonstrate a discrepancy, choose one already known to differ.

1. Show the two declared targets and the comparison version. Explain which fields are in scope and which values are masked.
2. Start a batch on the selected keys and open its run ID. Compare the displayed counts with your expectations. For an inconclusive result, inspect the reason and the underlying observations.
3. If differences were found, open **Root causes**, select a sample key and inspect its masked A/B values and findings. For a matching key, use **All results**. On a table scan, identical rows contribute to the count. Use an explicit key list when the demo needs a detailed result for a matching row.
4. Download the PDF or a CSV from the run page and show how the report refers back to the evidence. For a batch or soak, open **Readiness** to inspect the measured gates for this comparison.
