# Public API samples (test fixtures)

These are small paired responses captured on 2026-09-24 from public, keyless APIs. They let the
tests exercise the product on real response formats without any network access. The layout is
sample layout 1: `<side>/<key>.json` holds the body and `<key>.meta.json` holds the status,
latency and time.

| Pair | Side a | Side b | Keys |
|---|---|---|---|
| `swapi` | `GET https://swapi.dev/api/people/{id}/` (flat record) | `GET https://www.swapi.tech/api/people/{id}` (`result.properties` wrapper) | 1–25 and 84 (17 and 84 are 404 on both) |
| `rickandmorty` | `GET https://rickandmortyapi.com/api/character/{id}` (REST) | `POST https://rickandmortyapi.com/graphql` (GraphQL, same data) | 1–25 and 99999 (REST 404; GraphQL 200 with `data.character: null`) |

The GraphQL request body used for side b:

```json
{"query": "query($id: ID!) { character(id: $id) { id name status species type gender origin { name } location { name } image episode { id } created } }",
 "variables": {"id": "<key>"}}
```

The data belongs to the respective projects (SWAPI, the Rick and Morty API). It is used here only
as test input. To re-capture, run `examples/public/capture.py`: it requests every key exactly as the
example spec does (`examples/public/<pair>/spec.yaml`), at the rate its `targets.yaml` allows.
