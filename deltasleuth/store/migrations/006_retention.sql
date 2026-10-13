-- Evidence retention and encryption at rest. Raw keys, headers, scan points
-- and chat are sealed under their run's data key; the data keys are wrapped by the keyring's KEKs, held
-- outside the database. observations, results and findings join run_keys on key_masked, already unique
-- per cycle. The plaintext columns run_keys.key, observations.headers and chat_messages.content and
-- tool_calls stay, nullable and written no more, until 007 drops them once 'ds keys migrate' sealed
-- them. Constraints keep the names PostgreSQL gives them by default, as 001's do.
CREATE TABLE ds.data_keys (
    scope        text PRIMARY KEY CHECK (scope ~ '^(address|R-[0-9A-Z]{26}|E-[0-9a-f]{16})$'),
    kek          text NOT NULL CHECK (kek ~ '^K-[0-9a-f]{16}$'),
    wrapped      bytea NOT NULL CHECK (octet_length(wrapped) = 40),  -- AES-KW of a 32-byte key
    created_at   timestamptz NOT NULL DEFAULT now(),
    rewrapped_at timestamptz
);
CREATE INDEX data_keys_kek_idx ON ds.data_keys (kek);

-- The spine moves from the raw key to key_masked (filled while run_keys' old primary key finds it).
ALTER TABLE ds.observations ADD COLUMN key_masked text;
ALTER TABLE ds.results ADD COLUMN key_masked text;
ALTER TABLE ds.findings ADD COLUMN key_masked text;
UPDATE ds.observations t SET key_masked = k.key_masked FROM ds.run_keys k
WHERE (k.run_id, k.cycle, k.key) = (t.run_id, t.cycle, t.key);
UPDATE ds.results t SET key_masked = k.key_masked FROM ds.run_keys k
WHERE (k.run_id, k.cycle, k.key) = (t.run_id, t.cycle, t.key);
UPDATE ds.findings t SET key_masked = k.key_masked FROM ds.run_keys k
WHERE (k.run_id, k.cycle, k.key) = (t.run_id, t.cycle, t.key);

ALTER TABLE ds.findings DROP CONSTRAINT findings_run_id_cycle_key_fkey, DROP CONSTRAINT findings_pkey;
ALTER TABLE ds.results DROP CONSTRAINT results_run_id_cycle_key_fkey, DROP CONSTRAINT results_pkey;
ALTER TABLE ds.observations DROP CONSTRAINT observations_run_id_cycle_key_fkey;
ALTER TABLE ds.run_keys DROP CONSTRAINT run_keys_pkey, DROP CONSTRAINT run_keys_run_id_cycle_key_masked_key,
    ADD PRIMARY KEY (run_id, cycle, key_masked),
    ALTER COLUMN key DROP NOT NULL,
    -- b"\x01" || nonce(12) || AES-256-GCM(key) || tag(16), the key 1 to 1024 bytes
    ADD COLUMN key_enc bytea CHECK (octet_length(key_enc) BETWEEN 30 AND 1053);

ALTER TABLE ds.observations ALTER COLUMN key_masked SET NOT NULL, DROP COLUMN key,
    ADD FOREIGN KEY (run_id, cycle, key_masked) REFERENCES ds.run_keys ON DELETE CASCADE,
    ALTER COLUMN headers DROP NOT NULL, ALTER COLUMN headers DROP DEFAULT,
    ADD COLUMN headers_enc bytea,
    DROP CONSTRAINT observations_content_hash_check,
    ADD CONSTRAINT observations_content_hash_check CHECK (content_hash ~ '^(sha256|hk1):[0-9a-f]{64}$');
CREATE INDEX observations_key_idx ON ds.observations (run_id, cycle, key_masked);
CREATE INDEX observations_blob_idx ON ds.observations (content_hash) WHERE content_hash IS NOT NULL;

ALTER TABLE ds.results ALTER COLUMN key_masked SET NOT NULL, DROP COLUMN key,
    ADD PRIMARY KEY (run_id, cycle, key_masked),
    ADD FOREIGN KEY (run_id, cycle, key_masked) REFERENCES ds.run_keys ON DELETE CASCADE;
-- A deleted observation's NO ACTION checks from results find their rows through these.
CREATE INDEX results_obs_a_idx ON ds.results (run_id, obs_a_id) WHERE obs_a_id IS NOT NULL;
CREATE INDEX results_obs_b_idx ON ds.results (run_id, obs_b_id) WHERE obs_b_id IS NOT NULL;

ALTER TABLE ds.findings ALTER COLUMN key_masked SET NOT NULL, DROP COLUMN key,
    ADD PRIMARY KEY (run_id, cycle, key_masked, seq),
    ADD FOREIGN KEY (run_id, cycle, key_masked) REFERENCES ds.results ON DELETE CASCADE;

-- A scan checkpoints by number: its sealed resume point never compares equal. A finished scan resumes
-- nowhere, so its raw point goes; a running one's is sealed by 'ds keys migrate'.
UPDATE ds.scans SET state = state || CASE WHEN (state->>'done')::boolean
    THEN '{"checkpoint": 0, "after": null}'::jsonb ELSE '{"checkpoint": 0}'::jsonb END;

ALTER TABLE ds.chat_messages ALTER COLUMN content DROP NOT NULL,
    ALTER COLUMN tool_calls DROP NOT NULL, ALTER COLUMN tool_calls DROP DEFAULT,
    ADD COLUMN sealed bytea,  -- JSON {content, tool_calls}
    ADD COLUMN cycle integer CHECK (cycle >= 1);

-- LLM calls keep their usage once a purged run's chat sessions are deleted.
ALTER TABLE ds.llm_calls DROP CONSTRAINT llm_calls_session_id_fkey,
    ADD FOREIGN KEY (session_id) REFERENCES ds.chat_sessions ON DELETE SET NULL;

-- '[\x01-\x1f\x7f-\x9f]' is every C0 and C1 control character, as in 005.
ALTER TABLE ds.runs
    ADD COLUMN retention text NOT NULL DEFAULT 'standard' CHECK (retention IN ('standard', 'audit')),
    ADD COLUMN hold_reason text CHECK (hold_reason <> '' AND length(hold_reason) <= 200
                                       AND hold_reason !~ '[\x01-\x1f\x7f-\x9f]'),
    ADD COLUMN hold_by text,
    ADD COLUMN hold_at timestamptz,
    ADD CHECK ((hold_reason IS NULL) = (hold_by IS NULL) AND (hold_by IS NULL) = (hold_at IS NULL));
UPDATE ds.runs r SET retention = 'audit' FROM ds.specs s
WHERE s.spec_id = r.spec_id AND s.body->>'retention' = 'audit';

-- Blobs whose last reference was purged, deleted 24 h after they were queued.
CREATE TABLE ds.blob_gc (
    content_hash text PRIMARY KEY CHECK (content_hash ~ '^hk1:[0-9a-f]{64}$'),
    queued_at    timestamptz NOT NULL DEFAULT now()
);

-- One row per ended cycle, never deleted: the retention clock, and the tombstone of a purge.
CREATE TABLE ds.cycles (
    run_id       text NOT NULL REFERENCES ds.runs,
    cycle        integer NOT NULL CHECK (cycle >= 1),
    ended_at     timestamptz NOT NULL,
    counters     jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(counters) = 'object'),
    purging_at   timestamptz,  -- from then on its reads answer 410
    purged_at    timestamptz,
    purged_by    text,
    purge_reason text CHECK (purge_reason <> '' AND length(purge_reason) <= 220
                             AND purge_reason !~ '[\x01-\x1f\x7f-\x9f]'),
    PRIMARY KEY (run_id, cycle),
    CHECK ((purging_at IS NULL) = (purge_reason IS NULL)),
    CHECK ((purged_at IS NULL) = (purged_by IS NULL)),
    CHECK (purged_at IS NULL OR purging_at IS NOT NULL)
);
CREATE INDEX cycles_due_idx ON ds.cycles (ended_at) WHERE purged_at IS NULL;

-- Every cycle ended before this migration: a finished run's last one when the run finished, any other
-- when its last result was saved, else when the run started. Only the run's latest counters are known.
INSERT INTO ds.cycles (run_id, cycle, ended_at, counters)
SELECT r.run_id, c.cycle,
       CASE WHEN c.cycle = r.cycle THEN r.finished_at
            ELSE coalesce((SELECT max(x.updated_at) FROM ds.results x
                           WHERE x.run_id = r.run_id AND x.cycle = c.cycle), r.started_at, r.created_at) END,
       CASE WHEN r.counters->>'cycle' = c.cycle::text THEN r.counters ELSE '{}' END
FROM ds.runs r
CROSS JOIN LATERAL generate_series(1, CASE WHEN r.finished_at IS NULL THEN r.cycle - 1 ELSE r.cycle END)
    AS c (cycle);

-- A message belongs to the first cycle that ended after it, else to the run's current cycle.
UPDATE ds.chat_messages m SET cycle = coalesce(
    (SELECT min(c.cycle) FROM ds.cycles c WHERE c.run_id = s.run_id AND c.ended_at >= m.created_at), r.cycle)
FROM ds.chat_sessions s JOIN ds.runs r USING (run_id)
WHERE s.session_id = m.session_id;
ALTER TABLE ds.chat_messages ALTER COLUMN cycle SET NOT NULL;
