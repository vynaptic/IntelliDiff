# Tables: a legacy CRM vs the new platform

Two PostgreSQL databases hold the same customers in different tables:

- **`ds_demo_legacy`** (side a): `crm.customers`, with `email_address` and `full_name`, a
  `branch_code` the spec does not read, closed accounts the platform never migrated, and
  `created_at` as local time in Europe/London without a zone (`timestamp`).
- **`ds_demo_platform`** (side b): `public.users`, with `email` and `name`, `created_at` as
  `timestamptz`, and two columns of its own, `marketing_opt_in` and `updated_at`.

| File | What it holds |
|---|---|
| `seed.sql` | Both databases, for `psql`: 1,000 customers, ten of them closed accounts the platform never migrated, and seven seeded differences. |
| `targets.yaml` | The two database targets: a DSN file reference and a budget each. |
| `spec.yaml` | Side a lists the columns it compares, renames two onto side b's names, and compares open accounts only (`where: "status <> 'closed'"`). Side b omits `columns`, so its own two columns are listed as one-sided and not compared. Both sides map `created_at` to one canonical timestamp (`typeMap`), side a reading its naive values in Europe/London. E-mail addresses ignore case and surrounding spaces. E-mail, name and phone are tokenised, the rest is clear. Customer numbers are `identifier` keys with a `key.pattern` of six digits: an evidence pack tells a raw key from other text by it, and issues none for identifier keys without one. |

The contract is [docs/mvp/TABLES.md](../../docs/mvp/TABLES.md); the runbook's
[Compare two tables](../../docs/mvp/RUNBOOK.md#compare-two-tables) explains every step.

## What a run shows

A batch run without keys scans both tables. The Run screen reads: 989 rows on A (the scan leaves
out the 11 closed accounts, outside side a's filter), 990 on B; 984 identical; 7 to compare; 0 not
compared; columns only on B: `marketing_opt_in`, `updated_at`.

| Key | Outcome | Why |
|---|---|---|
| 100003 | MATCH | The e-mail differs only in case and a trailing space, which `email_basic` removes. normalization.csv lists it. |
| 100005 | MISMATCH | `$.phone` differs. |
| 100007 | MISMATCH | `$.status` differs. |
| 100009 | MISMATCH | `$.balance` differs (HIGH). |
| 100011 | NOT_FOUND_B | No row on the platform. |
| 100013 | MISMATCH | Closed in the CRM, active on the platform: `FILTER_MEMBERSHIP` (side a's filter leaves the row out, side b has it), and `$.status` differs. Never NOT_FOUND_B: the row exists on both sides. |
| 101001 | NOT_FOUND_A | No row in the CRM. |
| The other 984 | MATCH, confirmation `bulk_exhaustive` | Identical rows, `created_at` included once both sides are converted: the scan counts them, and nothing reads them per key. |

The counters: total 991, compared 991, match 985, different 6. The six discrepant keys are
confirmed `rechecked` by their per-key read, with no second pass (a scan read them first).
Customer numbers are `identifier` keys, so every output shows them as tokens. coverage.csv's
`filter_a` holds the filter as the spec writes it, `status <> 'closed'`, and so does the PDF's
"Tables" line; the Run screen shows the plan's form, `"status" <> 'closed'`.

Without side b's `typeMap`, side a's `created_at` becomes `2020-01-02T00:37:00.000000Z` while side
b's stays `2020-01-02T00:37:00+00:00`: every row differs. Map a converted column on both sides.

## Run it

You need PostgreSQL 16 or later, `psql`, and a settings file as in the runbook's
[Setup](../../docs/mvp/RUNBOOK.md#setup), whose `targets_file` is `examples/tables/targets.yaml`.

1. Seed the two databases, as a role that may create databases:

   ```bash
   psql -v ON_ERROR_STOP=1 -f examples/tables/seed.sql postgresql://postgres@localhost:5432/postgres
   ```

2. Write each DSN to its file, percent-encoding the password (`@` as `%40`). `secrets/` is
   git-ignored. Any role that can read both tables will do; a read-only one is better
   (docs/mvp/DEPLOY.md, section 7).

   ```bash
   mkdir -p -m 700 examples/tables/secrets
   printf '%s' 'postgresql://postgres:PASSWORD@localhost:5432/ds_demo_legacy' > examples/tables/secrets/crm_legacy_dsn
   printf '%s' 'postgresql://postgres:PASSWORD@localhost:5432/ds_demo_platform' > examples/tables/secrets/platform_users_dsn
   ```

3. Validate, scan, compare one key, and write the reports:

   ```bash
   uv run ds spec validate examples/tables/spec.yaml --targets examples/tables/targets.yaml
   uv run ds run examples/tables/spec.yaml                    # prints the run id, then the counters above
   uv run ds compare 100005 --spec examples/tables/spec.yaml  # MISMATCH, with the masked phone values
   uv run ds compare 100013 --spec examples/tables/spec.yaml  # MISMATCH: FILTER_MEMBERSHIP and /status
   uv run ds report R-... --out demo/tables --kind coverage.csv --kind discrepancies.csv
   ```

**On the compose stack** (docs/mvp/DEPLOY.md), in the installation's folder, with the demo databases
on its `db` service and `compose.yaml` mounting their two DSN secrets into the app (paths relative to
`deploy/`). Name `compose.yaml` in `.env` after the site file, and after the CA or source override if
you use one (`…site.yml:deploy/docker-compose.ca.yml:examples/tables/compose.yaml`), never in an
exported `COMPOSE_FILE`, which overrides `.env`:

```bash
printf '%s\n' 'COMPOSE_FILE=deploy/docker-compose.yml:deploy/docker-compose.site.yml:examples/tables/compose.yaml' > .env
docker compose exec -T db psql -q -U deltasleuth -d deltasleuth -v ON_ERROR_STOP=1 -f - < examples/tables/seed.sql
pw=$(openssl rand -hex 24)
docker compose exec -T db psql -q -U deltasleuth -d deltasleuth -v ON_ERROR_STOP=1 -v pw="$pw" <<'SQL'
CREATE ROLE ds_reader LOGIN PASSWORD :'pw';
GRANT CONNECT ON DATABASE ds_demo_legacy, ds_demo_platform TO ds_reader;
\connect ds_demo_legacy
GRANT USAGE ON SCHEMA crm TO ds_reader; GRANT SELECT ON ALL TABLES IN SCHEMA crm TO ds_reader;
\connect ds_demo_platform
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ds_reader;
SQL
printf 'postgresql://ds_reader:%s@db:5432/ds_demo_legacy' "$pw" > deploy/secrets/crm_legacy_dsn
printf 'postgresql://ds_reader:%s@db:5432/ds_demo_platform' "$pw" > deploy/secrets/platform_users_dsn
chmod 444 deploy/secrets/*_dsn; unset pw
```

Then add the `crm-legacy` and `platform-users` targets of `deploy/config/targets.example.yaml` to
`deploy/config/targets.yaml`, run `docker compose --profile mocks up -d`, upload `spec.yaml` on the
Specs screen, and start a batch run without keys. With SSO, first request the version's approval on the
Specs screen and have a `rule_approver` other than you approve it (two for a token upload): its clear
paths and its table `where` and `columns` need one (`clear`, `population_filters`). In tokens mode the
run goes ahead, labelled unapproved.

**In a deployment:** seed the databases on a server the app can reach, and add the secrets
`crm_legacy_dsn` and `platform_users_dsn` (docs/mvp/DEPLOY.md, section 7);
`deploy/config/targets.example.yaml` already declares both targets with these files. Then upload
`spec.yaml`, have its approval requested and granted as above, and start a batch run without keys.
