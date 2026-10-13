# Bulk graph parity: contract

This contract is binding. It follows [PLAN.md](PLAN.md) and
[TABLES.md](TABLES.md).

**Goal:** prove parity of a linked structure (entities that share identifiers) over the whole
population, not only per key. The design names two layers (architecture §4.8, §8.4):
- **L1:** the set difference of the link facts.
- **L2:** the structure those facts make. Which entities each identifier links, and how entities
  fall into connected components: a split, a merge, or both.

**Black-box.** Each side supplies its link facts as a PostgreSQL table or view, for example an
export the system's owners load. DeltaSleuth reads relations only, never a graph database's
internals.

## 1. Spec

A table pair whose composite key is a link fact adds `graph`:

```yaml
key: {name: link, type: composite, parts: [entity_code, id_type, id_value], class: identifier}
sides:
  a: {role: legacy, target: crm, table: {name: entity_identifiers, keyColumns: [entity_code, id_type, id_value], columns: [entity_code]}}
  b: {role: new, target: platform, table: {name: links_export, keyColumns: [org_code, kind, value], columns: [{name: org_code, as: entity_code}]}}
graph:
  members: [entity_code]          # key parts naming a member (an entity)
  groups: [id_type, id_value]     # key parts naming a group (an identifier)
  maxComponent: 10000             # optional: members a component is expanded to (default 10000)
```

**Schema:** a new top-level `graph` object (both schema copies) with:
- `members` and `groups`: non-empty arrays of key part names;
- `maxComponent`: an integer from 1 to 1,000,000.

**Loader rules:**
- Both sides are tables, and PostgreSQL tables: graph parity with a MySQL or Oracle side is refused
  (`not supported: graph parity with a MySQL or Oracle side; export the link facts into PostgreSQL`,
  [ENGINES.md](ENGINES.md) §12 L2), and so is graph parity with a file side (`not supported: graph parity
  with a file side; load the link facts into PostgreSQL`, [FILES.md](FILES.md) §15 L6: each L2 level
  would be a pass over the files). L2's statements are PostgreSQL's (`runner/graph.py`, on
  `connectors.postgres`).
- `key.type` is `composite`.
- `members` and `groups` are disjoint and together name every key part exactly once
  (`invalid_graph`).
- `canonical.mode: graph` (per-key graph projection of service responses) stays unsupported: it is
  a different feature.

## 2. L1: link facts

The run is an ordinary scan run over the fact tables (TABLES.md §5, batch or soak). Each key is a
fact:
- **A one-sided fact** is a missing or extra link, NOT_FOUND_B or NOT_FOUND_A.
- **A compared column that differs** is an attribute difference.

Filters, key maps and type maps apply as for any table pair.

## 3. L2: structure

L2 runs in the scan task after the fact merge ends, before the final checkpoint marks the scan
done. It has no checkpoint of its own. A scanner that dies during L2 resumes from the merge's last
checkpoint, merging at most 10,000 keys again (nothing is counted twice), and runs L2 again, which
replaces the cycle's rows. A scan ended early by `stop.maxKeys` runs no L2, and its reports say
"Not computed".

A hot group (more members than `maxComponent` on either side, known from the group pass) is
reported with its sizes only. It is never read in full, and never expanded on either side while
components are explored; a component that reaches one is oversized.

### Group member sets

Each side streams one row per group, in the UTF-8 byte order of the canonical group texts
(`queries.group_sets_sql`):

```sql
SELECT f.g, count(*), sum(h.hi), sum(h.lo)
FROM (SELECT <group canonical text> AS g, <member canonical text> AS m
      FROM <relation> AS t WHERE <canonical keys not null> AND (<where>)) AS f
CROSS JOIN LATERAL (SELECT sha256(convert_to(f.m, 'UTF8')) AS d) AS x
CROSS JOIN LATERAL (SELECT <bytes 1-8 of x.d as a bigint>::numeric AS hi,
                           <bytes 9-16 of x.d as a bigint>::numeric AS lo) AS h
GROUP BY f.g ORDER BY convert_to(f.g, 'UTF8')
```

The member set's hash is two sums of 64-bit slices of each member's sha256: independent of order,
a multiset's, and held in constant memory, so a group of any size stays under the server's 1 GB
value limit (a sorted concatenation would not).

A composite member's or group's canonical text is its parts, joined as `encode_key` joins them
(`'|'`-escaped in SQL too), so groups are ordered and merged by that one text, not part by part. A
merge like the fact merge compares the groups:
- **Equal hash and count:** the group links the same members.
- **Otherwise:** the group is different, or one-sided.

For every group that differs, both sides' member lists are then read in batches of 500 groups
(`queries.facts_sql`). Each group text maps back to the side values that may hold it (the key maps'
`to_side`), which narrow the rows with one `IN` list per group column, as an index on those columns
can serve; a join of the canonical group text on the batch's texts then selects exactly. A group
text no side value can hold adds nothing to the lists. A batch holding a text this side's column
cannot hold (a text member against an integer column: `session.UNFIT`) is refused by the server;
it is halved, in a savepoint so the snapshot survives, until that text stands alone and reads
nothing, since no row of this side can hold it. The comparison records:
- the member counts per side;
- the members only on a and only on b;
- up to 5 of each, as masked samples.

### Components

- **Seeds:** the members of every group that differs.
- **Exploration:** each side separately, a breadth-first walk over its facts from the seeds.
  Members lead to their groups, and groups to their members, with the same narrowed lookup on the
  member or group columns. Batches hold 500 members or groups. Every lookup of a side reads the one
  `REPEATABLE READ READ ONLY` snapshot L2 opened for that side. The walk keeps a union-find, so it knows each component's
  size.
- **The cap:** a component whose member count exceeds `maxComponent` is no longer expanded and is
  reported as `CLUSTER_OVERSIZED` (with its size so far). It is never classified, never silently
  dropped.

The contingency follows architecture §8.4:
- **Cells:** for members found on both sides, a cell is (component on a, component on b).
- **Cell components:** the cells form a bipartite graph, split into connected components.
- **Classification:** each cell component gets one code:
  - `CLUSTER_REARRANGE` when it both fans out and fans in;
  - `CLUSTER_SPLIT` when it only fans out (one a-component, several b-components);
  - `CLUSTER_MERGE` when it only fans in.

  A one-to-one cell component is not a structural difference: members present on one side only
  are L1's findings.
- **No ARI/VI:** a global ARI or VI needs every component, and L2 expands only the affected ones,
  so neither is reported. The report says so.

## 4. Store and outputs

**Migration `004`** (with TABLES.md §11) adds this table:

```sql
CREATE TABLE ds.graph_diffs (
    run_id text NOT NULL REFERENCES ds.runs ON DELETE CASCADE,
    cycle integer NOT NULL, seq integer NOT NULL,
    code text NOT NULL CHECK (code IN ('GROUP_MEMBERS_DIFFER', 'CLUSTER_SPLIT', 'CLUSTER_MERGE',
                                       'CLUSTER_REARRANGE', 'CLUSTER_OVERSIZED')),
    label text NOT NULL,              -- the masked group key, or the masked first member of a component
    count_a integer NOT NULL, count_b integer NOT NULL,   -- members (GROUP_MEMBERS_DIFFER, CLUSTER_OVERSIZED)
                                                          -- or components (SPLIT, MERGE, REARRANGE)
    only_a integer NOT NULL DEFAULT 0, only_b integer NOT NULL DEFAULT 0,
    detail jsonb NOT NULL DEFAULT '{}', -- masked samples, members per component
    PRIMARY KEY (run_id, cycle, seq));
```

**How it is written:** L2 replaces the cycle's rows in one transaction, and they are all masked.
Group and member texts go through `mask_key(text, spec, tokeniser)`, so a key class that holds
personal data shows tokens.

**Scan state** (`ds.scans.state.graph`):

```json
{"groups_a": 0, "groups_b": 0, "groups_equal": 0, "groups_different": 0,
 "split": 0, "merge": 0, "rearrange": 0, "oversized": 0}
```

**Outputs:**
- **`graph.csv`:** a new report kind with one row per `graph_diffs` row.
- **API:** `GET /v1/runs/{id}/graph?cycle=` returns the rows, paged.
- **PDF:** a "Graph parity" section: the counts, then the first 20 rows of each code.
- **UI:** the run page shows one "Graph" line of counts, and the reports list gains `graph.csv`.
- **The agent:** `get_run_summary` includes `scan.graph`.
- **Counters are unchanged:** L2 adds no results or findings. It explains the L1 findings.

## 5. Scale

- **Member sets** stream with O(1) memory: one row per group, in group order.
- **Exploration** holds only the affected components, each bounded by `maxComponent`. Each
  breadth-first level costs two lookups per side per 500 nodes, and a component takes as many
  levels as its diameter: up to about `maxComponent` for a chain. Member lookups use an index that
  leads with the member columns (usually the primary key); give group lookups an index on the group
  columns, or each one scans the relation. A 200,000-fact chain took 4.9 s of L2 without such an
  index and 1.4 s with it.
- **Measured** (opt-in, `DS_PERF=1`,
  `tests/e2e/test_graph.py::test_a_million_facts_a_side_finish_in_two_minutes`): 1M facts a side
  (500,000 entities, each with a phone and an email shared by pairs), with 100 entities re-linked on
  side b, complete L1 and L2 in about 15 s on a laptop (14.6 s and 14.9 s), against a 2-minute
  target. That is 200 differing groups and 100 rearranged components. An index on the group columns
  made no difference at this size.
- **Benchmark** ([BENCHMARK.md](BENCHMARK.md) Results; another profile than the test above): its
  generator scaled, with the group index on `(id_type, id_value)` and `maxComponent: 20`, on an Apple M3
  Max with one stock PostgreSQL 16 in Docker holding both sides and the store (synthetic facts, warm
  cache, worker poll 0.05 s). 0.1M, 1M and 10M facts a side (20, 200 and 2,000 differing groups) took
  1.63 s (1.61–1.65), 17.9 s (17.9–18.0) and 143.9 s (143.7–144.1), warm medians with their ranges (L1
  72.4 s (72.3–72.4) and L2 71.1 s (71.0–71.2) at 10M), with about 110 to 120 MB of peak memory; each
  side's sorts wrote 201 to 211 bytes a fact in two phases, L1's then L2's, whose files are never all held
  at once (at most 107 to 121 bytes a fact a side).

## 6. Tests

- **Loader:** the `graph` rules.
- **E2E:** two fact tables seeded so that:
  - one identifier gains a member (a merge of two components);
  - one identifier loses a member (a split);
  - one identifier is re-pointed (a rearrange);
  - one component exceeds `maxComponent`.

  The `graph_diffs` rows and the scan's `graph` counts must be exactly those, and every label
  masked. The same holds after a kill during L2.
- **Unit** (`tests/unit/runner/test_graph.py`): the contingency classification (split, merge,
  rearrange, one-to-one, a one-sided member, an oversized component) and the union-find (a node not
  reached weighs nothing; a 200,000-link chain joins without recursion).
