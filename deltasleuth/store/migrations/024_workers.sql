-- The worker processes that process keys: each registers while it works and renews its registration every
-- second; one whose registration expired has stopped. The sweep's holder is marked. Each process takes its
-- share of every target's rate budget and in-flight cap by the count of live workers and its place among them.
CREATE TABLE ds.workers (
    worker_id  text PRIMARY KEY CHECK (worker_id <> '' AND length(worker_id) <= 300),
    sweep      boolean NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

-- A pause of a target that one worker met and every worker honours until it ends: a Retry-After (breaker
-- '') or an open circuit breaker (breaker: the declaration it opened under, rate/window/cooldown).
CREATE TABLE ds.target_pauses (
    target_id text NOT NULL CHECK (target_id <> '' AND length(target_id) <= 63),
    breaker   text NOT NULL CHECK (length(breaker) <= 100),
    until     timestamptz NOT NULL,
    PRIMARY KEY (target_id, breaker)
);

-- While more than one worker processes keys, what each saved group of a run gives its stop conditions, for
-- the sweep to judge in the order the groups were saved: each pass's reads, failed or not, whether its first
-- read was INCONCLUSIVE, and its key's unsuppressed CRITICAL findings (the masked key only where the run
-- counts them). The sweep deletes each row as it judges it.
CREATE TABLE ds.stop_feed (
    seq    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id text NOT NULL REFERENCES ds.runs,
    passes jsonb NOT NULL CHECK (jsonb_typeof(passes) = 'array')
);
CREATE INDEX stop_feed_run_idx ON ds.stop_feed (run_id);
