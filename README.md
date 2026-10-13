# DeltaSleuth

DeltaSleuth checks whether two sources agree, one key at a time. A source is a REST service, a database
table or view, or a directory of CSV or Parquet files. It reads both sides, turns each answer into one
canonical document, finds every difference, groups the differences into probable root causes and reports
them with personal values masked.

It is a black box tool: it calls declared endpoints and reads declared tables. How either system is built
never matters. It only reads, and it never decides which side is right.

## What it does

- Compares two services, two tables, two file exports, or any mix of them.
- Runs one key now, a batch of keys, or a soak that repeats the comparison in cycles.
- Keeps to a rate budget for each target, and survives a killed worker without losing or repeating work.
- Reads a difference again before it counts, so a value that changed between two reads is not a mismatch.
- Reports the keys that differ as CSV and PDF, grouped by root cause, with a cut-over brief for each pair.
- Reads PostgreSQL, MySQL and Oracle tables, and CSV and Parquet files.
- Offers pre-checks of tables, schedules, notifications, sign-off sampling, approvals and evidence packs.
- Answers questions about a run through an optional AI chat that must cite stored findings, and serves
  the same reads to an agent of your own through an MCP server.

## Quickstart

With Docker, one command in a checkout gives a running one-machine trial at `http://localhost:8000`, with
two practice service pairs and a first API token saved to a file:

```bash
sh deploy/up.sh
```

Then follow [the user guide](docs/user-guide.md): the first comparison takes about five minutes.

## Documentation

| Document | What it is for |
|---|---|
| [docs/deployment.md](docs/deployment.md) | Install a trial, deploy for production, update, back up, run from source |
| [docs/user-guide.md](docs/user-guide.md) | Compare two services or tables, read the results, write a spec, a short demo |
| [docs/architecture.md] | The design, a map of the code, and where future enhancements fit |
| [SECURITY.md](SECURITY.md) | How to report a vulnerability, and how releases are signed and scanned |

## Development

```bash
uv sync
uv run pytest -q
uv run ruff check deltasleuth mocks tests examples tools
(cd web && npm ci && npm test && npm run build)
```

Python 3.13 and Node 22.12 or later. The database tests start local PostgreSQL, MySQL and Oracle containers
when Docker is present, and skip without it unless `DS_REQUIRE_DB=1` is set.

## Licence

DeltaSleuth is licensed under the Apache License, Version 2.0: see [LICENSE](LICENSE).
