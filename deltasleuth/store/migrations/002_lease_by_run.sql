-- Due keys are looked up run by run, among running runs only (lease_due): the keys of a paused run
-- are never scanned, however many there are.
DROP INDEX ds.run_keys_due_idx;
CREATE INDEX run_keys_due_idx ON ds.run_keys (run_id, available_at, seq) WHERE state IN ('pending', 'leased', 'recheck');
CREATE INDEX runs_running_idx ON ds.runs (run_id) WHERE status = 'running';
