# Rick and Morty: REST vs GraphQL

The Rick and Morty API serves the same characters over two protocols:

- **REST** (side a): `GET /api/character/{id}`.
- **GraphQL** (side b): `POST /graphql`, with the query in `spec.yaml`.

This pair shows two things DeltaSleuth handles. It compares a GET side with a POST side, and it
treats a GraphQL "not found" (a 200 response) as authoritative absence.

| File | What it holds |
|---|---|
| `spec.yaml` | Both sides mapped onto one canonical character. Episode links (REST URLs, GraphQL `{id}` objects) become ids, compared as a set. |
| `targets.yaml` | The two endpoints and their not-found contracts (see below). |
| `keys.txt` | Characters 1 to 25 and 99999. The spec's `key.source` lists the same keys. |

The two not-found contracts differ:

- **REST** answers 404 `{"error": ...}`.
- **GraphQL** answers 200 `{"data": {"character": null}}`. The spec's `notFound` lists status 200 with
  `when: has(body.data) && body.data.character == null`. The target declares status 200 authoritative
  only for a body without `errors`, because a failed resolver also returns null data.

The spec was drafted by the profiler from `tests/fixtures/public/rickandmorty`, then reviewed by hand.
The review changed four things:

- It selected `$.data.character`.
- It mapped episode links to ids.
- It left out the REST-only `url` and location links, which the GraphQL query does not ask for.
- It normalised `id` with `decimal: {scale: 0}` instead of the drafted scale 38.

## What a run shows

On the captured samples, `tests/e2e/test_public_pairs.py` checks every key:

| Keys | Outcome | Why |
|---|---|---|
| 99999 | INCONCLUSIVE `absent_both` | REST answers 404 and GraphQL answers `character: null`, and both targets confirm the absence. |
| All others | MATCH | One INFO finding each: `$.id` NORMALIZATION_ONLY. REST sends the id as a number, GraphQL's `ID` is a string. The normaliser reconciles them, and the gap stays visible. |

## Run it

- **Offline:** `pytest tests/e2e/test_public_pairs.py`.
- **Against the live APIs:** `pytest -m live_public tests/e2e/test_public_pairs.py`.
- **In a deployment:** append the entries of `targets.yaml` under `targets:` in the deployment's
  targets file, upload `spec.yaml`, then run the keys from `keys.txt` (see `docs/mvp/DEPLOY.md`).
