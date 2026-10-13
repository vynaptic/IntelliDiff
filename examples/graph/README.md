# Graph: entity links in a legacy CRM vs the new platform

Two PostgreSQL databases record which identifiers (VAT, DUNS and LEI numbers) belong to which
organisations, one row per link:

- **`ds_graph_legacy`** (side a): `crm.entity_identifiers (entity_code, id_type, id_value)`.
- **`ds_graph_platform`** (side b): `public.links_export (org_code, kind, value)`, the export the
  platform's owners load. DeltaSleuth reads this relation only.

| File | What it holds |
|---|---|
| `seed.sql` | Both databases, for `psql`: 200 entities with a VAT and a DUNS number each, the same on both sides, then a merge, a split, a rearrangement and a hot identifier. |
| `targets.yaml` | The two database targets: a DSN file reference and a budget each. |
| `spec.yaml` | The composite key is the link, `entity_code\|id_type\|id_value`; `graph` names the entity part (`members`) and the identifier parts (`groups`), with `maxComponent: 20`. Company codes and numbers are not personal data, so the key is a `business_code` and labels show clear. |

A graph spec is a table spec with a `graph` block. The user guide's
[Compare tables and files](../../docs/user-guide.md#compare-tables-and-files) covers table specs; the
sections below show what the graph comparison adds.

## What a run shows

**L1, the links.** The scan reads 441 links a side: 437 identical, 8 to compare. The counters: total
445, compared 445, match 437, different 8, each discrepant link confirmed `rechecked`.

| Link | Outcome | Scenario |
|---|---|---|
| `ORG-1002\|vat\|GB900001001` | NOT_FOUND_A | merge |
| `ORG-2002\|duns\|900002001` | NOT_FOUND_B | split |
| `ORG-3002\|duns\|900003001`, `ORG-3003\|duns\|900003003` | NOT_FOUND_B | rearrangement |
| `ORG-3003\|duns\|900003001`, `ORG-3002\|duns\|900003003` | NOT_FOUND_A | rearrangement |
| `ORG-4030\|vat\|GB000000000` | NOT_FOUND_B | hot identifier |
| `ORG-4031\|lei\|LEI00000000000004001` | NOT_FOUND_A | hot identifier |

**L2, the structure.** The Run screen reads: "Graph: 409 groups on A, 409 on B; 6 differ; 1 split,
1 merge, 1 rearranged, 2 too large to expand." `graph.csv` (and `GET /v1/runs/{id}/graph`):

```
seq,code,label,count_a,count_b,only_a,only_b,detail
0,GROUP_MEMBERS_DIFFER,duns|900002001,2,1,1,0,"{""only_a"":[""ORG-2002""],""only_b"":[]}"
1,GROUP_MEMBERS_DIFFER,duns|900003001,2,2,1,1,"{""only_a"":[""ORG-3002""],""only_b"":[""ORG-3003""]}"
2,GROUP_MEMBERS_DIFFER,duns|900003003,2,2,1,1,"{""only_a"":[""ORG-3003""],""only_b"":[""ORG-3002""]}"
3,GROUP_MEMBERS_DIFFER,lei|LEI00000000000004001,1,2,0,1,"{""only_a"":[],""only_b"":[""ORG-4031""]}"
4,GROUP_MEMBERS_DIFFER,vat|GB000000000,30,29,0,0,"{""oversized"":true}"
5,GROUP_MEMBERS_DIFFER,vat|GB900001001,1,2,0,1,"{""only_a"":[],""only_b"":[""ORG-1002""]}"
6,CLUSTER_OVERSIZED,ORG-4001,1,0,0,0,"{""side"":""a""}"
7,CLUSTER_OVERSIZED,ORG-4001,0,2,0,0,"{""side"":""b""}"
8,CLUSTER_MERGE,ORG-1001,2,1,0,0,"{""members"":2,""sample"":[""ORG-1001"",""ORG-1002""]}"
9,CLUSTER_SPLIT,ORG-2001,1,2,0,0,"{""members"":2,""sample"":[""ORG-2001"",""ORG-2002""]}"
10,CLUSTER_REARRANGE,ORG-3001,2,2,0,0,"{""members"":4,""sample"":[""ORG-3001"",""ORG-3002"",""ORG-3003"",""ORG-3004""]}"
```

| Scenario | Rows |
|---|---|
| **Merge:** the platform also links ORG-1002 to ORG-1001's VAT number. | 5: that VAT number links 1 entity on a, 2 on b. 8: two components on a are one on b. |
| **Split:** the platform drops ORG-2002 from the DUNS number it shared with ORG-2001. | 0: 2 entities on a, 1 on b. 9: one component on a is two on b. |
| **Rearrangement:** two DUNS numbers swap ORG-3002 and ORG-3003. | 1 and 2: each number gained one entity and lost another. 10: two components on each side, their entities regrouped. |
| **Hot identifier:** 30 entities share the placeholder VAT number GB000000000 (29 on the platform), over `maxComponent`. On the platform, ORG-4031 also joins the LEI of ORG-4001, one of the 30. | 4: compared by size only, no entity lists. 3: the LEI gained ORG-4031. 6 and 7: the LEI's component reaches the placeholder on each side, so it is not expanded or classified; the counts are the entities reached (1 on a, 2 on b). |

The 403 other identifiers link the same entities on both sides. L2 adds no result or finding: it
explains L1's eight. ARI and VI are not reported, since L2 expands only the components the differing
identifiers reach; the PDF's "Graph parity" section says so.

## Run it

You need PostgreSQL 16 or later, `psql`, and a settings file as
[docs/deployment.md](../../docs/deployment.md) describes for a run from source, whose `targets_file` is
`examples/graph/targets.yaml`.

1. Seed the two databases, as a role that may create databases:

   ```bash
   psql -v ON_ERROR_STOP=1 -f examples/graph/seed.sql postgresql://postgres@localhost:5432/postgres
   ```

2. Write each DSN to its file, percent-encoding the password (`@` as `%40`). `secrets/` is
   git-ignored; a read-only role is better.

   ```bash
   mkdir -p -m 700 examples/graph/secrets
   printf '%s' 'postgresql://postgres:PASSWORD@localhost:5432/ds_graph_legacy' > examples/graph/secrets/crm_links_dsn
   printf '%s' 'postgresql://postgres:PASSWORD@localhost:5432/ds_graph_platform' > examples/graph/secrets/platform_links_dsn
   ```

3. Validate, scan, and write the reports:

   ```bash
   uv run ds spec validate examples/graph/spec.yaml --targets examples/graph/targets.yaml
   uv run ds run examples/graph/spec.yaml                     # prints the run id, then the counters above
   uv run ds report R-... --out demo/graph --kind graph.csv --kind report.pdf
   ```

**In a deployment:** seed the databases on a server the app can reach, and add the secrets
`crm_links_dsn` and `platform_links_dsn` as [docs/deployment.md](../../docs/deployment.md) describes;
`deploy/config/targets.example.yaml` already declares both targets with these files. Then upload
`spec.yaml` and start a batch run without keys. With SSO, first request the version's approval on the
Specs screen and have a `rule_approver` other than you approve it (two for a token upload): its clear
paths and its table `columns` need one (`clear`, `population_filters`). In tokens mode the run goes
ahead, labelled unapproved.
