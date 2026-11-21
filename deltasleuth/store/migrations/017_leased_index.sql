-- A worker's leases are renewed by owner, among leased keys only (renew_lease): the keys of a run
-- that are pending or done are never scanned, however many there are.
CREATE INDEX run_keys_leased_idx ON ds.run_keys (lease_owner) WHERE state = 'leased';
