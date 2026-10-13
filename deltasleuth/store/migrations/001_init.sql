-- DeltaSleuth MVP schema: a lean subset of docs/architecture/control-plane.sql (same names, same meanings).
--
-- Raw keys live in run_keys.key and the columns that reference it (the fetch needs them); they are
-- never displayed. Every value shown to people or models is masked by the caller before it is stored:
-- run_keys.key_masked; findings.value_a, value_b, member_key_tok, json_pointer, path_pattern;
-- root_causes.*; chat_sessions.key; chat_messages of role tool. A chat question is stored as the
-- analyst typed it (an answer may quote it). Raw bodies live only in the evidence directory.

CREATE DOMAIN ds.outcome AS text CHECK (VALUE IN (
    'MATCH', 'MISMATCH', 'KNOWN_DIFF', 'NOISE_ONLY', 'DRIFT', 'NOT_FOUND_A', 'NOT_FOUND_B', 'INCONCLUSIVE'));
CREATE DOMAIN ds.side_state AS text CHECK (VALUE IN ('COMPLETE', 'ABSENT', 'INCOMPLETE', 'ERROR'));
CREATE DOMAIN ds.severity AS text CHECK (VALUE IN ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO'));
CREATE DOMAIN ds.change_type AS text CHECK (VALUE IN (
    'missing_in_b', 'extra_in_b', 'value_changed', 'type_changed', 'order_changed'));
CREATE DOMAIN ds.direction AS text CHECK (VALUE IN ('a_only', 'b_only', 'both'));

CREATE TABLE ds.specs (
    spec_id      bigserial PRIMARY KEY,
    name         text NOT NULL,
    version      text NOT NULL,
    content_hash text NOT NULL UNIQUE,
    body         jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'),
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE ds.runs (
    run_id      text PRIMARY KEY CHECK (run_id ~ '^R-[0-9A-Z]{26}$'),
    spec_id     bigint NOT NULL REFERENCES ds.specs,
    mode        text NOT NULL CHECK (mode IN ('one_shot', 'batch', 'soak')),
    status      text NOT NULL DEFAULT 'queued' CHECK (status IN (
                    'queued', 'running', 'paused', 'cancelling', 'cancelled',
                    'completed', 'completed_with_issues', 'failed')),
    params      jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(params) = 'object'),
    cycle       integer NOT NULL DEFAULT 1 CHECK (cycle >= 1),
    counters    jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(counters) = 'object'),
    stop_reason text,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    started_at  timestamptz,
    finished_at timestamptz,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CHECK ((status IN ('cancelled', 'completed', 'completed_with_issues', 'failed')) = (finished_at IS NOT NULL))
);
CREATE INDEX runs_spec_idx ON ds.runs (spec_id, run_id);

-- available_at: when a pending or recheck key is due. Leasing moves it to lease_until, so an expired
-- lease is due again and one index finds every due key.
CREATE TABLE ds.run_keys (
    run_id       text NOT NULL REFERENCES ds.runs ON DELETE CASCADE,
    cycle        integer NOT NULL CHECK (cycle >= 1),
    key          text NOT NULL CHECK (key <> '' AND octet_length(key) <= 1024),
    key_masked   text NOT NULL,
    seq          integer NOT NULL CHECK (seq >= 0),
    state        text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'leased', 'recheck', 'done', 'cancelled')),
    pass_no      smallint NOT NULL DEFAULT 1 CHECK (pass_no >= 1),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_until  timestamptz,
    lease_owner  text,
    PRIMARY KEY (run_id, cycle, key),
    UNIQUE (run_id, cycle, seq),
    UNIQUE (run_id, cycle, key_masked),
    CHECK ((state = 'leased') = (lease_owner IS NOT NULL)),
    CHECK ((lease_owner IS NULL) = (lease_until IS NULL))
);
CREATE INDEX run_keys_due_idx ON ds.run_keys (available_at, seq) WHERE state IN ('pending', 'leased', 'recheck');
CREATE INDEX run_keys_open_idx ON ds.run_keys (run_id, cycle) WHERE state IN ('pending', 'leased', 'recheck');

-- Metadata of evidence; obs_id is the evidence store's HMAC id, unique within a run.
CREATE TABLE ds.observations (
    run_id              text NOT NULL,
    obs_id              text NOT NULL CHECK (obs_id ~ '^O-[0-9a-f]{16}$'),
    cycle               integer NOT NULL,
    key                 text NOT NULL,
    side                text NOT NULL CHECK (side IN ('a', 'b')),
    pass_no             smallint NOT NULL CHECK (pass_no >= 1),
    http_status         integer,
    content_hash        text CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    latency_ms          double precision CHECK (latency_ms >= 0),
    attempts            integer NOT NULL CHECK (attempts >= 0),
    pages               integer NOT NULL CHECK (pages >= 0),
    pages_eof           boolean NOT NULL,
    pagination_unstable boolean NOT NULL,
    error_class         text,
    headers             jsonb NOT NULL DEFAULT '{}',  -- credentials already dropped by the connector
    observed_at         timestamptz NOT NULL,
    PRIMARY KEY (run_id, obs_id),
    FOREIGN KEY (run_id, cycle, key) REFERENCES ds.run_keys ON DELETE CASCADE
);
CREATE INDEX observations_key_idx ON ds.observations (run_id, cycle, key);

-- revision counts the saves of a row (1, 2 after a recheck, ...); first_outcome is the first save's.
CREATE TABLE ds.results (
    run_id              text NOT NULL,
    cycle               integer NOT NULL,
    key                 text NOT NULL,
    outcome             ds.outcome NOT NULL,
    side_a_state        ds.side_state NOT NULL,
    side_a_reason       text,
    side_b_state        ds.side_state NOT NULL,
    side_b_reason       text,
    inconclusive_reason text,
    drift_reason        text CHECK (drift_reason IN (
                            'changed_since', 'a_changed', 'b_converged', 'watermark_mismatch',
                            'skew_exceeded', 'cut_window')),
    confirmation        text NOT NULL CHECK (confirmation IN ('provisional', 'rechecked', 'frozen_cut', 'bulk_exhaustive')),
    first_outcome       ds.outcome NOT NULL,
    revision            integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    suppressed_count    integer NOT NULL DEFAULT 0 CHECK (suppressed_count >= 0),
    obs_a_id            text,
    obs_b_id            text,
    attrs               jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(attrs) = 'object'),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, cycle, key),
    FOREIGN KEY (run_id, cycle, key) REFERENCES ds.run_keys ON DELETE CASCADE,
    FOREIGN KEY (run_id, obs_a_id) REFERENCES ds.observations (run_id, obs_id),
    FOREIGN KEY (run_id, obs_b_id) REFERENCES ds.observations (run_id, obs_id),
    CHECK ((outcome = 'INCONCLUSIVE') = (inconclusive_reason IS NOT NULL)),
    CHECK ((outcome = 'DRIFT') = (drift_reason IS NOT NULL))
);
CREATE INDEX results_outcome_idx ON ds.results (run_id, cycle, outcome);

CREATE TABLE ds.findings (
    run_id              text NOT NULL,
    cycle               integer NOT NULL,
    key                 text NOT NULL,
    seq                 integer NOT NULL CHECK (seq >= 0),
    finding_id          text NOT NULL UNIQUE CHECK (finding_id ~ '^F-[0-9a-f]{16}$'),
    taxonomy_code       text,
    severity            ds.severity,
    change_type         ds.change_type NOT NULL,
    direction           ds.direction NOT NULL,
    path_pattern        text NOT NULL,
    json_pointer        text NOT NULL,
    value_a             jsonb,
    value_b             jsonb,
    value_class         text,
    member_key_tok      text,
    signature_hash      text,
    rule_id             text,
    rule_classification text CHECK (rule_classification IN ('accepted', 'bug', 'expected_by_design')),
    suppressed          boolean NOT NULL,
    root_cause_id       text,  -- set when the cycle's root causes are computed
    PRIMARY KEY (run_id, cycle, key, seq),
    FOREIGN KEY (run_id, cycle, key) REFERENCES ds.results ON DELETE CASCADE
);
CREATE INDEX findings_root_cause_idx ON ds.findings (run_id, cycle, root_cause_id);

CREATE TABLE ds.root_causes (
    run_id         text NOT NULL REFERENCES ds.runs ON DELETE CASCADE,
    cycle          integer NOT NULL,
    root_cause_id  text NOT NULL CHECK (root_cause_id ~ '^RC-[0-9a-f]{12}$'),
    seq            integer NOT NULL CHECK (seq >= 0),  -- rank: most probable cause first
    kind           text NOT NULL CHECK (kind IN ('member_key', 'identifier', 'signature')),
    taxonomy_code  text,
    max_severity   ds.severity NOT NULL,
    path_pattern   text NOT NULL,
    change_type    ds.change_type NOT NULL,
    direction      ds.direction NOT NULL,
    affected_keys  integer NOT NULL CHECK (affected_keys >= 1),
    finding_count  integer NOT NULL CHECK (finding_count >= 1),
    member_key_tok text,
    value_class    text,
    signature_hash text,
    sample_keys    text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, cycle, root_cause_id),
    UNIQUE (run_id, cycle, seq)
);

CREATE TABLE ds.chat_sessions (
    session_id text PRIMARY KEY,
    run_id     text NOT NULL REFERENCES ds.runs ON DELETE CASCADE,
    key        text,  -- the masked key a key-scoped session is about
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX chat_sessions_run_idx ON ds.chat_sessions (run_id);

CREATE TABLE ds.chat_messages (
    session_id text NOT NULL REFERENCES ds.chat_sessions ON DELETE CASCADE,
    seq        integer NOT NULL CHECK (seq >= 1),
    role       text NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content    text NOT NULL,
    citations  jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(citations) = 'array'),
    tool_calls jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(tool_calls) = 'array'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE ds.llm_calls (
    call_id        text PRIMARY KEY,
    session_id     text REFERENCES ds.chat_sessions ON DELETE CASCADE,
    profile_id     text NOT NULL,
    model_reported text,
    usage          jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(usage) = 'object'),
    cost           numeric CHECK (cost >= 0),  -- in the profile's pricing currency
    latency_ms     integer CHECK (latency_ms >= 0),
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_calls_session_idx ON ds.llm_calls (session_id);

CREATE TABLE ds.audit_log (
    id     bigserial PRIMARY KEY,
    at     timestamptz NOT NULL DEFAULT now(),
    actor  text NOT NULL,
    action text NOT NULL,
    object text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(detail) = 'object')
);

CREATE FUNCTION ds.append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END $$;
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON ds.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();
