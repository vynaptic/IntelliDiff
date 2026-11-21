-- Schedules: a standing request to start a run of a stored spec at cron times in
-- a time zone. Rows are never deleted: a run names its schedule. The index on ds.runs is built first,
-- under SHARE (reads go on, run writes wait while it reads every row once); the check is NOT VALID and
-- last, so its ACCESS EXCLUSIVE lock scans nothing and is held only to the end of the migration.
CREATE TABLE ds.schedules (
    schedule_id  text PRIMARY KEY CHECK (schedule_id ~ '^SC-[0-9A-Z]{26}$'),
    name         text NOT NULL CHECK (name ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(name) <= 63),
    spec_name    text NOT NULL,
    spec_id      bigint REFERENCES ds.specs,       -- pinned; NULL: the name's newest version at each firing
    run          jsonb NOT NULL CHECK (jsonb_typeof(run) = 'object' AND run->>'mode' IN ('batch', 'soak')
                                       AND NOT run ?| ARRAY['keys', 'spec_id']),
    cron         text NOT NULL CHECK (length(cron) <= 100 AND cron ~ '^[0-9A-Za-z*,/ -]+$'),
    time_zone    text NOT NULL CHECK (length(time_zone) <= 64 AND (time_zone = 'UTC'
                                       OR time_zone ~ '^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)+$')),
    start_within integer NOT NULL DEFAULT 60 CHECK (start_within BETWEEN 15 AND 1440),
    ends_at      timestamptz,
    auth_mode    text NOT NULL CHECK (auth_mode IN ('none', 'tokens', 'oidc')),
    revision     integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    set_by       text NOT NULL,                    -- its creator, then each PUT's caller
    set_at       timestamptz NOT NULL DEFAULT now(),
    paused_by    text,                             -- a person, or system:worker (a zone or cron it cannot load)
    paused_at    timestamptz,
    deleted_by   text,
    deleted_at   timestamptz,
    anchor_at    timestamptz NOT NULL DEFAULT now(),  -- next_due is the first cron time after it
    next_due     timestamptz,                         -- NULL: paused, ended or deleted
    last         jsonb CHECK (jsonb_typeof(last) = 'object'),  -- its newest settlement's detail and outcome
    last_at      timestamptz,
    last_run_id  text REFERENCES ds.runs,             -- the newest run it started; a miss keeps it
    held         jsonb CHECK (jsonb_typeof(held) = 'object'),  -- a standing refusal's range not yet chained
    CHECK ((paused_at IS NULL) = (paused_by IS NULL)),
    CHECK ((deleted_at IS NULL) = (deleted_by IS NULL)),
    CHECK (next_due IS NULL OR (paused_at IS NULL AND deleted_at IS NULL AND next_due > anchor_at)),
    CHECK ((last IS NULL) = (last_at IS NULL))
);
CREATE UNIQUE INDEX schedules_name_idx ON ds.schedules (name) WHERE deleted_at IS NULL;
CREATE INDEX schedules_due_idx ON ds.schedules (next_due) WHERE next_due IS NOT NULL;
CREATE INDEX schedules_spec_idx ON ds.schedules (spec_name) WHERE deleted_at IS NULL;
-- One run per schedule and due time, for every writer:
CREATE UNIQUE INDEX runs_schedule_due_idx ON ds.runs
    ((params->'schedule'->>'schedule_id'), (params->'schedule'->>'due')) WHERE params ? 'schedule';
ALTER TABLE ds.runs ADD CONSTRAINT runs_schedule CHECK (NOT params ? 'schedule' OR (
    params->'schedule'->>'schedule_id' ~ '^SC-[0-9A-Z]{26}$'
    AND params->'schedule'->>'due' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z$')) NOT VALID;
