-- Table pre-checks and the schema guard (docs/mvp/PRECHECKS.md). A pre-check is no run and no evidence:
-- its rows go retention.standard_days after it was made; its audit rows stay. ds.runs.guard holds what
-- the schema guard recorded of a run's tables: added without a default or a check, so ds.runs is neither
-- rewritten nor scanned.
CREATE TABLE ds.prechecks (
    precheck_id  text PRIMARY KEY CHECK (precheck_id ~ '^PC-[0-9A-Z]{26}$'),
    source       text NOT NULL CHECK (source IN ('specs', 'catalogue')),
    level        text NOT NULL CHECK (level IN ('schema', 'counts', 'columns', 'distinct')),
    request      jsonb NOT NULL CHECK (jsonb_typeof(request) = 'object'),  -- ids, target ids, schemas, globs
    pairs        integer NOT NULL CHECK (pairs BETWEEN 1 AND 500),
    skipped      integer NOT NULL DEFAULT 0,
    evaluation   boolean NOT NULL DEFAULT false,     -- made in an unlicensed evaluation (LICENCE O1's label)
    created_by   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    cancelled_by text,
    cancelled_at timestamptz,
    finished_at  timestamptz,
    CHECK ((cancelled_at IS NULL) = (cancelled_by IS NULL))
);
CREATE INDEX prechecks_open_idx ON ds.prechecks (precheck_id) WHERE finished_at IS NULL;
CREATE INDEX prechecks_created_idx ON ds.prechecks (created_at);
CREATE TABLE ds.precheck_pairs (
    precheck_id text NOT NULL REFERENCES ds.prechecks ON DELETE CASCADE,
    n           integer NOT NULL CHECK (n >= 1),
    spec_id     bigint REFERENCES ds.specs,          -- NULL: paired by name
    label       text NOT NULL,                       -- the spec's name, or the relation's lower-cased name
    target_a    text NOT NULL, relation_a text,
    target_b    text NOT NULL, relation_b text,
    status      text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'done', 'cancelled')),
    tries       smallint NOT NULL DEFAULT 0,
    claimed_at  timestamptz,                         -- when a worker took it, renewed while it reads it
    verdict     text CHECK (verdict IN ('unpaired', 'refused', 'not_read', 'rows_differ',
                                        'keys_null_or_repeated', 'columns_not_compared', 'values_differ',
                                        'incomplete', 'schema_differs', 'no_difference_observed')),
    rank        smallint,
    differing   integer,
    rows_a      bigint,                              -- what the list prints, so that it never reads result
    rows_b      bigint,
    line        text,                                -- the verdict's line (precheck.line)
    result      jsonb CHECK (jsonb_typeof(result) = 'object'),   -- format 1: counts, verdicts, names
    finished_at timestamptz,
    PRIMARY KEY (precheck_id, n),
    CHECK ((status = 'done') = (verdict IS NOT NULL AND rank IS NOT NULL AND line IS NOT NULL
                                AND result IS NOT NULL))
);
CREATE INDEX precheck_pairs_due_idx ON ds.precheck_pairs (precheck_id, n) WHERE status IN ('pending', 'running');
CREATE INDEX precheck_pairs_worst_idx ON ds.precheck_pairs (precheck_id, rank, differing DESC, label, n);
ALTER TABLE ds.runs ADD COLUMN guard jsonb;
