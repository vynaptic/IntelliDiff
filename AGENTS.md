# AGENTS.md

Guidance for any coding agent that works in this repository. It holds what one file does not show. The
full design is in [docs/architecture.md](docs/architecture.md): read it before a change that crosses
packages.

## What this is

DeltaSleuth compares two sources key by key and reports every difference. A source is a REST service, a
database table or view, or a directory of CSV or Parquet files. The backend is Python (`deltasleuth/`:
FastAPI, PostgreSQL), the UI is React and TypeScript (`web/`), and `ds` is the command line
(`deltasleuth/cli`).

## Commands

Python 3.13 only (`>=3.13,<3.14`), managed by uv. Node 22.12 or later.

```bash
uv sync
uv run pytest -q                                             # every Python test
uv run pytest tests/unit/core/test_cron.py -q                # one file
uv run pytest tests/unit/core/test_cron.py::test_name -q     # one test
uv run pytest tests/golden -k arrays-set -q                  # one golden case, by its file name
uv run ruff check deltasleuth mocks tests examples tools     # lint
uv run mypy                                                  # type check (settings in pyproject.toml)

npm --prefix web ci
npm --prefix web test                                        # the UI's logic tests
npm --prefix web run build                                   # type check, then bundle into web/dist
npm --prefix web run dev                                     # dev server; forwards /v1 to 127.0.0.1:8000
(cd web && node --experimental-transform-types --no-warnings --test test/nav.test.ts)   # one UI test file
```

Run from source, after the one-time setup in section 4 of [docs/deployment.md](docs/deployment.md) (the
settings file, the database address and `ds init`):

```bash
export DS_CONFIG="$PWD/.deltasleuth/deltasleuth.yaml"
uv run ds migrate
uv run ds serve                      # the API, the UI and the worker on 127.0.0.1:8000
uv run python -m mocks.run all       # the two practice service pairs, in a second terminal
```

`sh deploy/up.sh` builds and starts a Docker trial instead.

About the tests:

- Tests marked `db`, `mysql` and `oracle` start throwaway containers from local images and never pull.
  Pull `postgres:16-alpine`, `mysql:8.4` and `gvenzl/oracle-free:23-slim-faststart` once. Without Docker or
  an image the tests skip; `DS_REQUIRE_DB=1` turns the skip into a failure.
- The default options deselect `live_llm` and `live_public`. A `-m` on the command line replaces that
  expression, so repeat those two terms in it.
- The end-to-end tests serve the mock pairs on free loopback ports themselves. Nothing has to be running.
- The full suite is large. Run the folder you changed first.
- Each test fails after 600 s (`timeout` in the pytest options); `--timeout=0` lifts it.

## Architecture

One pass over one key is `deltasleuth/runner/compare.py`: a connector reads each side, the read is stored
as evidence before it is judged, `core/canonical` observes, maps and normalises it, `core/diff` compares
the two documents, and `core/classify` gives each finding a code and the key an outcome. The worker saves
a group's observations, results and findings in one transaction.

- `deltasleuth/core` is pure logic: no network and no database. Its shared types are in `core/types.py`
  and nothing redefines them. A JSON number is a `Decimal`, never a float. It imports nothing of the
  package but `errors` and the domain packs, and only the modules `tests/unit/test_integration_contracts.py`
  names read files or start a process.
- `deltasleuth/connectors` fetch and never judge. A source implements `Connector` (`connectors/base.py`),
  and `BatchConnector` to read groups of keys; `CONNECTORS` in `runner/compare.py` picks one by
  `Target.kind`. A database implements every member of `Engine` (`connectors/tables/engine.py`), turns its
  driver's errors into `TableError` classes and has its row in `core/engines.py`: nothing branches on an
  engine's name.
- `deltasleuth/runner` runs the work: runs, key sets, the worker (one active worker per database, by an
  advisory lock, with leased keys), rechecks, cycle ends and schedules.
- `deltasleuth/store/repo/` holds every SQL query, one async function per use case, each taking an open
  connection: one module per kind of object (`runs.py`, `results.py`, `audit.py` and so on), the helpers
  they share in `base.py`. Its `__init__.py` re-exports every name, so callers write `repo.<name>`; a new
  query goes in its module and in the imports and `__all__` of `__init__.py`. A query calls what its own
  module imported, so a test that patches one of its callees patches that module (`repo.audit`), not the
  package. The schema is the numbered files in `store/migrations`, each applied once. A schema change is a
  new numbered file.
- `deltasleuth/context.py` defines `Context`: the pool, the target registry, the tokeniser, the evidence
  store, the keyring and one gate per target. The API, the CLI, the worker and the MCP server each open
  one and call the same code. The modules at the package's top level (`approvals.py`, `schedules.py`,
  `readiness.py`, `prechecks.py`, `notifications.py`, `targets.py`, `licence.py`, `specs.py`, `errors.py`)
  hold the logic those entry points share.
- `deltasleuth/api` is FastAPI with one router per resource in `routes/`. Every `/v1` route declares the
  role that may call it (`require` in `api/deps.py`). The app serves the built UI from `web/dist`.
- `deltasleuth/cli` is `ds`: `main.py` builds `app` from one module per command group (`runs.py`,
  `schedules.py` and so on), and `common.py` holds the options, exit codes and helpers they share. A command
  module imports the store, the runner, the connectors and the reports inside the command that uses them,
  so `ds --help` and `ds spec validate` load no database driver. A test that patches a name a command calls
  patches the command's module (`deltasleuth.cli.runs._in_context`), not `main`.
- `web/src` is a hash-routed single page. A screen is a `.tsx` file. Logic lives in plain `.ts` modules,
  and only those are tested (`web/test`, Node's test runner, no browser).
- `mocks/` holds two service pairs with planted differences and an `expected.json` each, the ground truth
  of the end-to-end tests. `tools/` holds maintainer tools that are not part of the product.

Where a new connector, database engine, model dialect, domain pack or report format goes: section 11 of
[docs/architecture.md](docs/architecture.md).

## Rules the code keeps

- **Black box.** Nothing names how a compared system is built. A target is an endpoint or a table plus the
  behaviour it declares. `tests/unit/test_integration_contracts.py` scans the package, the end-to-end
  tests and the documents for backend names.
- **Read only.** Nothing writes to a compared system, and nothing decides which side is right.
- **The engine decides.** Outcomes, findings and root causes come from code and the spec. A language model
  only explains them and must cite stored ids.
- **Every key gets a result.** A key that cannot be read is `INCONCLUSIVE`. Nothing is dropped silently: a
  spec member the engine does not run is refused as `unsupported` or accepted with an `ignored` warning
  (`core/spec/rules.py`, beside `spec.schema.json`).
- **Values are masked.** Protected values are tokenised before they are stored for display. Raw keys,
  headers and chat text are sealed under the run's data key. Add no column, log line or report field that
  holds a raw value.
- **Refusals are `errors.Refused`.** A request the package declines raises `Invalid` (422), `NotFound` (404),
  `Conflict` (409), `Gone` (410) or a module's `Refused(status, message)`, whose message names ids, names and
  counts only. The API answers its status, the CLI exits 2 with one `error:` line, the MCP tools show it. Any
  other exception, a Python or library `ValueError` or `LookupError` included, is a fault: 500, exit 3, its
  type and place only (`errors.told`).
- **Credentials are `file:` references.** Never an environment variable, never inside a spec. The settings
  are one YAML file (`--config` or `DS_CONFIG`), and an unknown setting is an error.
- **The UI and the engine share constants.** The same test file pins the outcome legend in
  `web/src/Run.tsx`, the citation expression in `web/src/explain.ts` and the audit detail keys the notifier
  reads. Change both sides together.
- **The open build enforces no licence.** `licence.SHIPPED_KEYS` is empty, and the tests run as that build.
  A test of a vendor build asks for the `vendor_keys` fixture.

## Conventions

- Ruff with line length 110; the rule set, with its complexity ceiling (`max-complexity`), is in
  `pyproject.toml`. `ruff check` and `mypy` are the gates: every function of `deltasleuth` is annotated,
  and a `# type: ignore` names its error code and says why. Do not reformat a file you did not change.
- Prose and identifiers use British spelling (`licence`, `normaliser`, `tokenise`). Spec members are
  camelCase as the schema spells them (`normalizerVersion`).
- Comments, docstrings and messages say what the code does. They do not cite document sections, ticket
  numbers or the history of a decision.
- A change to the engine's findings or to a key's outcome comes with a golden case: one YAML file in `tests/golden/cases`, in the
  layout that `tests/golden/conftest.py` describes.
- `[tool.uv] exclude-newer` in `pyproject.toml` is a cooldown date: a newer release does not resolve until
  that date moves. The UI's dependencies are pinned to exact versions.
- The image's build context is an allow-list (`deploy/Dockerfile.dockerignore`), and `SHIPPED` in
  `tools/release.py` lists what a release archive holds. A new top-level path the product needs goes in
  both.

## Documentation

Apart from this file and `CLAUDE.md`, the documentation is exactly `README.md`, `docs/deployment.md` (at
most 220 lines), `docs/user-guide.md` (at most 380), `docs/architecture.md` (at most 260) and
`SECURITY.md`. Add no other document: no specification, plan, design note or changelog. A new feature gets
a few lines in the user guide and, if it changes the design, in the architecture document.
`tests/unit/deploy/test_docs.py` follows every relative link, and fails when any file outside `tests/`
names a document folder that was removed.

## A public repository

Examples, fixtures, tests and commit messages use invented data only, such as the codes `ORG-000123` and
`MBR-0045123`. Never commit an endpoint, a table or column name, a key, a token or a value from a real
system. `my-specs/`, `practice/`, `.deltasleuth/`, `.env`, `deploy/config/*.yaml` and every `secrets/`
folder are git-ignored for that reason: keep them untracked.
