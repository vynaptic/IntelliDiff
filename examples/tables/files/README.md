# The files acceptance kit

DeltaSleuth's file sides are observed with DuckDB 1.5.5 and files of DuckDB's own writer only. This kit
is what a site runs first, before any real export is compared: a CSV as a legacy system writes one
against a Parquet file, with seeded differences whose result is known. Then run a small export of your
own writer (Spark, Hive, Impala, Arrow) the same way: those are not observed. File sides are in the user
guide's [Compare tables and files](../../../docs/user-guide.md#compare-tables-and-files).

| File | What it holds |
|---|---|
| `generate.py` | Writes the two exports under a directory: `accounts_a/accounts.csv` (semicolons, a decimal comma, `DD/MM/YYYY` dates, zero-padded keys, a quoted field holding the delimiter) with its stamp file `_AS_OF`, and `accounts_b/accounts.parquet` with the differences its code lists. |
| `targets.yaml` | The one files target both sides read, `kit-files`: edit its `root`. |
| `spec.yaml` | `accounts_a` against `accounts_b`: the CSV's declared dialect, its key read as an integer, its decimal comma and dates through the `typeMap`. |
| `expected.json` | The result: the scan's counts, each side's files, rows and lines that are not rows, the columns the worker hashes, the counters and outcomes `ds run` prints, and each differing key with its outcome and the paths of its findings. |

## Run it

With a settings file as [docs/deployment.md](../../../docs/deployment.md) describes for a run from
source, whose `targets_file` is `examples/tables/files/targets.yaml` and whose `files.roots_under` holds
the kit's directory (the target's `root` is that directory's real path):

```bash
uv run python examples/tables/files/generate.py /var/lib/deltasleuth/files/kit
uv run ds target check kit-files --table accounts_a/accounts.csv --format csv --header --count
uv run ds target check kit-files --table accounts_b/ --format parquet --count
uv run ds run examples/tables/files/spec.yaml
uv run ds report R-... --out kit --kind discrepancies.csv --kind retry.csv
```

The first `ds target check` drafts a starting point for what `spec.yaml` declares: the delimiter `;`, a
header, `opened` as a date with the format `%d/%m/%Y`, and the note that `account_id` holds only digits
and starts with 0 (so it stays text until `csv.keys` declares it an integer). It leaves `balance` as
text: a decimal comma is not sniffed, and the spec's `typeMap` declares it. `ds run` must print these two
lines after `completed`, which are `expected.json`'s `counters` and `outcomes`:

```
  total 1001, compared 1000, match 995, different 5, indeterminate 1, cancelled 0, outstanding 0
  INCONCLUSIVE 1, MATCH 995, MISMATCH 3, NOT_FOUND_A 1, NOT_FOUND_B 1
```

- `discrepancies.csv` must list exactly the MISMATCH and NOT_FOUND keys of `expected.json`'s
  `discrepancies`, each with the `path_pattern` given there, and `retry.csv` the one INCONCLUSIVE key,
  `30`, with reason `identity_ambiguous`: side b holds it in two rows, and a file promises no unique
  key. Keys are business codes: the reports show them as they are.
- `scan` is the Run screen's scan line: 1,000 rows on A, 1,002 on B, 995 identical, 6 to compare, 1 not
  compared (a row of side b without a key), and for side b `1 repeated keys` (side a holds none: 0).
  `files` gives each side's files, rows and lines that are not rows (none here). `worker_columns` is the
  plan's `digest`: a CSV field and a Parquet column are never declared alike, so the worker hashes every
  compared column.
- Key `5` shows the declared key type at work: side a's file holds `000005`, read as the integer 5.

**On the compose stack** ([docs/deployment.md](../../../docs/deployment.md)), in the
installation's folder, where no `uv` runs: the image's Python writes the exports into a host directory, here
`/srv/exports/kit`, which the container's user (uid 10001) must be able to write for that one command. Then
mount the directory read-only into the app (the site file's commented `volumes` line), declare `kit-files`
in `deploy/config/targets.yaml` with `settle_s: 0` and without `allow_writable` (the mount is read-only), and
run `docker compose up -d`. The spec and the reports pass through the `p0` volume; while the app runs,
`ds run` leaves the run to its worker and waits.

```bash
docker compose run --rm --no-deps -v "$PWD/examples/tables/files:/kit:ro" \
    -v /srv/exports/kit:/var/lib/deltasleuth/files/kit app python /kit/generate.py /var/lib/deltasleuth/files/kit
# the site file: volumes: ["/srv/exports/kit:/var/lib/deltasleuth/files/kit:ro"]; then docker compose up -d
alias ds='docker compose run --rm -e DS_OPERATOR="$(id -un)" app ds'
docker compose cp examples/tables/files/spec.yaml app:/var/lib/deltasleuth/p0/kit-files.yaml
ds target check kit-files --table accounts_a/accounts.csv --format csv --header --count
ds target check kit-files --table accounts_b/ --format parquet --count
ds run /var/lib/deltasleuth/p0/kit-files.yaml
ds report R-... --out /var/lib/deltasleuth/p0/kit-files --kind discrepancies.csv --kind retry.csv
docker compose cp app:/var/lib/deltasleuth/p0/kit-files ~/
```

The target declares `settle_s: 0` and `allow_writable: true` because you have just written the
directory yourself; a real export keeps the default 60 seconds and is mounted read-only. Anything else
than the expected result is a finding: send the two lines, the two CSV files and the output of the two
`ds target check` commands to the maintainer. Removing the directory removes the kit.
