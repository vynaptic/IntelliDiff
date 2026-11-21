-- Scans per cycle: a soak over two tables scans them every cycle, and a report
-- of an ended cycle counts that cycle's identical keys. state.after holds the raw key the scan resumes
-- after: like run_keys.key it is for fetching only, never displayed.
CREATE TABLE ds.scans (
    run_id text NOT NULL REFERENCES ds.runs ON DELETE CASCADE,
    cycle  integer NOT NULL CHECK (cycle >= 1),
    state  jsonb NOT NULL CHECK (jsonb_typeof(state) = 'object'),
    PRIMARY KEY (run_id, cycle)
);
INSERT INTO ds.scans (run_id, cycle, state) SELECT run_id, cycle, scan FROM ds.runs WHERE scan IS NOT NULL;
ALTER TABLE ds.runs DROP COLUMN scan;

-- Bulk graph parity: the groups whose members differ and the components that
-- split or merge, one row each, masked like every other output.
CREATE TABLE ds.graph_diffs (
    run_id  text NOT NULL REFERENCES ds.runs ON DELETE CASCADE,
    cycle   integer NOT NULL CHECK (cycle >= 1),
    seq     integer NOT NULL CHECK (seq >= 0),
    code    text NOT NULL CHECK (code IN ('GROUP_MEMBERS_DIFFER', 'CLUSTER_SPLIT', 'CLUSTER_MERGE',
                                          'CLUSTER_REARRANGE', 'CLUSTER_OVERSIZED')),
    label   text NOT NULL,
    count_a integer NOT NULL,
    count_b integer NOT NULL,
    only_a  integer NOT NULL DEFAULT 0,
    only_b  integer NOT NULL DEFAULT 0,
    detail  jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail) = 'object'),
    PRIMARY KEY (run_id, cycle, seq)
);
