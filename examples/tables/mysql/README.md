# The MySQL acceptance kit

DeltaSleuth's MySQL sides are observed on MySQL 8.4.11 only; 8.0.17 and later are accepted, not observed.
This kit is what a site's DBA runs on its own server version before any real table is compared: two
tables of one database, one column of each type DeltaSleuth reads, keys the default collation orders and
matches otherwise than their code points, and seeded differences whose result is known. MySQL sides are
in the user guide's [Compare tables and files](../../../docs/user-guide.md#compare-tables-and-files).

| File | What it holds |
|---|---|
| `seed.sql` | The database `ds_kit`, for the `mysql` client: `accounts_a`, and its copy `accounts_b` with the differences its header lists. |
| `targets.yaml` | The one target both sides read, `kit-mysql`: edit its host and TLS members. |
| `spec.yaml` | `accounts_a` against `accounts_b`, every non-key column compared. |
| `expected.json` | The result: the scan's counts, the columns the worker hashes, the counters and outcomes `ds run` prints, and each differing key with its outcome and the paths of its findings. |

## Run it

With a settings file as [docs/deployment.md](../../../docs/deployment.md) describes for a run from
source, whose `targets_file` is `examples/tables/mysql/targets.yaml`:

```bash
mysql --default-character-set=utf8mb4 -h HOST -u ADMIN -p < examples/tables/mysql/seed.sql
# as ADMIN: CREATE USER 'ds_reader'@'%' IDENTIFIED BY '...'; GRANT SELECT ON ds_kit.* TO 'ds_reader'@'%';
mkdir -p -m 700 examples/tables/mysql/secrets            # git-ignored
printf '%s' 'THE-PASSWORD' > examples/tables/mysql/secrets/kit_mysql_password
cp SERVER-CA.pem examples/tables/mysql/secrets/kit_mysql_ca
uv run ds target check kit-mysql --table accounts_a      # the server's version and what this release notes of it
uv run ds run examples/tables/mysql/spec.yaml
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
- The key `é` alone differing shows that keys are found by their bytes: in the default collation `e`,
  `E` and `é` are one value. Had the keys merged in the collation's order, the run would have failed
  with `KeysOutOfOrder`.

**On the compose stack** ([docs/deployment.md](../../../docs/deployment.md)), in the installation's folder,
where no `uv` runs: declare `kit-mysql` in `deploy/config/targets.yaml` as `targets.yaml` here does, its two
references written `file:/run/secrets/kit_mysql_password` and `file:/run/secrets/kit_mysql_ca`; write those
two files to `deploy/secrets/` and mount them as that document describes for a further secret, ending with
`docker compose up -d`. Then run the same commands in the image, the spec and the reports passing through
the `p0` volume. While the app runs, `ds run` leaves the run to its worker and waits.

```bash
alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
docker compose cp examples/tables/mysql/spec.yaml app:/var/lib/deltasleuth/p0/kit-mysql.yaml
ds target check kit-mysql --table accounts_a
ds run /var/lib/deltasleuth/p0/kit-mysql.yaml
ds report R-... --out /var/lib/deltasleuth/p0/kit-mysql --kind discrepancies.csv --kind retry.csv
docker compose cp app:/var/lib/deltasleuth/p0/kit-mysql ~/
```

Anything else is a finding about that server version: send the two lines, the two CSV files and the
first line of `ds target check` to the maintainer. `DROP DATABASE ds_kit` removes the kit.
