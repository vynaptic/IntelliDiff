-- A batch run over two tables: the scan's progress and counters. Its keys are
-- the rows that differ, added as the scan finds them. NULL for every other run. scan.after holds the
-- raw key the scan resumes after: like run_keys.key it is for fetching only, never displayed.
ALTER TABLE ds.runs ADD COLUMN scan jsonb CHECK (jsonb_typeof(scan) = 'object');
