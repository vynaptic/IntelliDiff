# The Oracle acceptance kit

DeltaSleuth's Oracle sides are observed on Oracle 23.26.3 only; 19c and later are accepted, not observed
([docs/mvp/ENGINES.md](../../../docs/mvp/ENGINES.md)). This kit is what a site's DBA runs on its own
server version before any real table is compared: two tables of one schema, one column of each type
DeltaSleuth reads on 19c, keys a linguistic sort would order otherwise than their code points, and
seeded differences whose result is known.

| File | What it holds |
|---|---|
| `seed.sql` | For SQL*Plus or SQLcl: `DS_KIT_A`, and its copy `DS_KIT_B` with the differences its header lists, in the schema of the account that runs it. |
| `targets.yaml` | The one target both sides read, `kit-oracle`: edit its host, service and TLS members. |
| `spec.yaml` | `DS_KIT_A` against `DS_KIT_B`, every non-key column compared. |
| `expected.json` | The result: the scan's counts, the columns the worker hashes, the counters and outcomes `ds run` prints, and each differing key with its outcome and the paths of its findings. |

## Run it

With a settings file as in the runbook's [Setup](../../../docs/mvp/RUNBOOK.md#setup) whose `targets_file`
is `examples/tables/oracle/targets.yaml`:

```bash
sqlplus KIT_OWNER@//HOST:1521/SERVICE @examples/tables/oracle/seed.sql
# as KIT_OWNER: GRANT READ ON ds_kit_a TO ds_reader; GRANT READ ON ds_kit_b TO ds_reader;
#   and add `schema: KIT_OWNER` to both tables of spec.yaml (ds_reader needs only CREATE SESSION besides)
mkdir -p -m 700 examples/tables/oracle/secrets           # git-ignored
printf '%s' 'THE-PASSWORD' > examples/tables/oracle/secrets/kit_oracle_password
cp SERVER-CA.pem examples/tables/oracle/secrets/kit_oracle_ca
uv run ds target check kit-oracle --table KIT_OWNER.DS_KIT_A   # the server's version, its character set, the notes
uv run ds run examples/tables/oracle/spec.yaml
uv run ds report R-... --out kit --kind discrepancies.csv --kind retry.csv
```

`ds run` must print these two lines after `completed`, which are `expected.json`'s `counters` and
`outcomes`:

```
  total 1011, compared 1010, match 991, different 19, indeterminate 1, cancelled 0, outstanding 0
  INCONCLUSIVE 1, MATCH 991, MISMATCH 17, NOT_FOUND_A 1, NOT_FOUND_B 1
```

- `discrepancies.csv` must list exactly the MISMATCH and NOT_FOUND keys of `expected.json`'s
  `discrepancies`, each with the `path_pattern` given there, and `retry.csv` the one INCONCLUSIVE key,
  `K0100`, with reason `identity_ambiguous`. Keys are business codes: the reports show them as they are.
- `scan` is the Run screen's scan line (`GET /v1/runs/{id}`, `scan`): 1,010 rows on A, 1,012 on B, 991
  identical, 20 to compare, 1 not compared. `worker_columns` is the plan's `digest`
  (`params.tables.a.digest`): the server hashes every other column itself.
- The kit is observed in an AL32UTF8 database only. In any other character set `ds target check` notes
  it, the keys are ordered by a conversion observed on `NVARCHAR2` only, which this kit then exercises
  on your version, and the emoji key cannot be stored as it is: the database is expected to store its
  replacement character on both sides alike, which leaves the result unchanged (not observed).

**On the compose stack** ([docs/mvp/DEPLOY.md](../../../docs/mvp/DEPLOY.md)), in the installation's folder,
where no `uv` runs: declare `kit-oracle` in `deploy/config/targets.yaml` as `targets.yaml` here does, its two
references written `file:/run/secrets/kit_oracle_password` and `file:/run/secrets/kit_oracle_ca`; write
those two files to `deploy/secrets/` and mount them (DEPLOY section 3, Secrets: the five steps, which end
with `docker compose up -d`). Then run the same commands in the image, the spec and the reports passing
through the `p0` volume (DEPLOY section 9); `kit-oracle.yaml` is a copy of `spec.yaml`, with `schema:
KIT_OWNER` added to both tables when the reader is another account. While the app runs, `ds run` leaves the
run to its worker and waits.

```bash
alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
docker compose cp kit-oracle.yaml app:/var/lib/deltasleuth/p0/kit-oracle.yaml
ds target check kit-oracle --table KIT_OWNER.DS_KIT_A
ds run /var/lib/deltasleuth/p0/kit-oracle.yaml
ds report R-... --out /var/lib/deltasleuth/p0/kit-oracle --kind discrepancies.csv --kind retry.csv
docker compose cp app:/var/lib/deltasleuth/p0/kit-oracle ~/
```

Anything else is a finding about that server version: send the two lines, the two CSV files and the
first lines of `ds target check` to the maintainer. `DROP TABLE ds_kit_a PURGE; DROP TABLE ds_kit_b PURGE;`
removes the kit.
