# SWAPI: swapi.dev vs swapi.tech

Two public deployments of the Star Wars API serve the same people records in different shapes:

- **swapi.dev** (side a) returns a flat record.
- **swapi.tech** (side b) wraps it in `result.properties`.

Both link to planets, films and starships with URLs on their own host.

| File | What it holds |
|---|---|
| `spec.yaml` | Both sides mapped onto one canonical person. Link URLs become the ids they point at, and link arrays are compared as sets. |
| `targets.yaml` | The two endpoints: 3 calls per second each, and each API's own 404 body declared as its not-found. |
| `keys.txt` | People 1 to 25 and 84. The spec's `key.source` lists the same keys. |

The spec was drafted by the profiler from `tests/fixtures/public/swapi`, then reviewed by hand. The
review added the `select` on side b, the link-to-id rewrite, the set arrays, the timestamp normaliser
and the two known diffs.

## What a run shows

On the captured samples, `tests/e2e/test_public_pairs.py` checks every key:

| Keys | Outcome | Why |
|---|---|---|
| 17, 84 | INCONCLUSIVE `absent_both` | Both APIs answer 404 with their declared not-found body. |
| All others | KNOWN_DIFF | Three findings per key, all suppressed by known diffs. |

The three suppressed findings are:

- `$.created` (VALUE_MISMATCH, `KD-SWAPI-TIMESTAMPS`).
- `$.edited` (VALUE_MISMATCH, `KD-SWAPI-TIMESTAMPS`). swapi.tech sets both fields to the time its
  data was last loaded, the same instant on every record.
- `$.species` (FIELD_MISSING, `KD-SWAPI-SPECIES`). swapi.tech people records have no species member.

Everything else matches after normalisation. A new difference would show as MISMATCH.

## Run it

- **Offline:** `pytest tests/e2e/test_public_pairs.py`.
- **Against the live APIs:** `pytest -m live_public tests/e2e/test_public_pairs.py`. This reads three keys per pair.
- **In a deployment:**
  1. Append the entries of `targets.yaml` under `targets:` in the deployment's targets file.
  2. Upload `spec.yaml` on the Specs screen.
  3. Start a batch run with the keys from `keys.txt` (see `docs/user-guide.md`).
