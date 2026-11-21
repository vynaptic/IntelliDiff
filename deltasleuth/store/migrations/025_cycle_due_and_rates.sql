-- When a soak's current cycle fell due, as its cycle end set it: a start the soak missed (a pause, an outage,
-- an overrun) moves it past the nominal one. Null for a first cycle and for cycles opened before this column.
ALTER TABLE ds.runs ADD COLUMN cycle_due_at timestamptz;

-- The hits of a call limited to so many a minute per caller (a chat question, a compare_key), so that the
-- limit holds across the app's processes and its restarts. A caller's hits older than the window are dropped
-- as its next one is counted.
CREATE TABLE ds.rate_hits (
    name  text NOT NULL CHECK (name <> '' AND length(name) <= 63),
    actor text NOT NULL CHECK (actor <> '' AND length(actor) <= 300),
    at    timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX rate_hits_idx ON ds.rate_hits (name, actor, at);
