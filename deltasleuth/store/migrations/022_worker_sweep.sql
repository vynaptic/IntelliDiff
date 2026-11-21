-- The worker's sweep: one row, its holder (host name and process id) and when it last swept, written
-- by each sweep once it read what is due and read by the health check, so a stuck or absent worker is seen.
CREATE TABLE ds.worker_sweep (
    one      boolean PRIMARY KEY DEFAULT true CHECK (one),
    holder   text NOT NULL CHECK (holder <> '' AND length(holder) <= 300),
    swept_at timestamptz NOT NULL
);
