-- Alerts and notifications: the audit hash chain is the outbox. The cursor is the last chain row
-- turned into deliveries, inserted at the head by the first pass with notify configured. Ids, counts and codes only.
CREATE TABLE ds.notify_cursor (
    one      boolean PRIMARY KEY DEFAULT true CHECK (one),
    seq      bigint NOT NULL CHECK (seq >= 0),
    moved_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE ds.notify_deliveries (
    delivery_id text PRIMARY KEY CHECK (delivery_id ~ '^N-[0-9A-Z]{26}$'),
    event_id    text NOT NULL CHECK (event_id ~ ('^(seq:[0-9]+:[a-z]+\.[a-z_]+|daily:[0-9]{4}-[0-9]{2}-[0-9]{2}'
                                                 '|test:N-[0-9A-Z]{26})$')),
    seq         bigint CHECK (seq > 0),  -- a ds.audit_chain seq (no FK: chain rows are never deleted); NULL: daily, test
    event       text NOT NULL CHECK (event ~ '^[a-z]+\.[a-z_]+$'),
    severity    text NOT NULL CHECK (severity IN ('info', 'warning', 'alert')),
    channel     text NOT NULL CHECK (channel ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND length(channel) <= 63),
    rules       text[] NOT NULL DEFAULT '{}',
    dedup_key   text NOT NULL CHECK (length(dedup_key) <= 200),
    run_id      text CHECK (run_id ~ '^R-[0-9A-Z]{26}$'),
    body        text NOT NULL CHECK (octet_length(body) <= 65536),  -- rendered once; e-mail without From and To
    body_sha256 text NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    render      text NOT NULL CHECK (render ~ '^(webhook|slack|teams|email):[01]:[01]$'),  -- kind:labels:links rendered for
    state       text NOT NULL CHECK (state IN ('pending', 'sent', 'dead', 'suppressed')),
    why         text CHECK (why IN ('muted', 'duplicate', 'rate_limited')),
    mute_id     text,
    attempts    smallint NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    queued_at   timestamptz NOT NULL DEFAULT now(),  -- expiry counts from here; a retry resets it
    due_at      timestamptz,
    last_error  text CHECK (last_error ~ '^[a-z][a-z0-9_]{0,31}$'),
    last_status integer CHECK (last_status BETWEEN 100 AND 599),
    created_at  timestamptz NOT NULL DEFAULT now(),
    done_at     timestamptz,
    UNIQUE (event_id, channel),
    CHECK ((state = 'suppressed') = (why IS NOT NULL)),
    CHECK ((why IS NOT DISTINCT FROM 'muted') = (mute_id IS NOT NULL)),
    CHECK ((state = 'pending') = (due_at IS NOT NULL)),
    CHECK ((state IN ('sent', 'dead')) = (done_at IS NOT NULL))
);
CREATE INDEX notify_pending_idx ON ds.notify_deliveries (channel, delivery_id) WHERE state = 'pending';
CREATE INDEX notify_dedup_idx ON ds.notify_deliveries (channel, dedup_key, created_at);
CREATE INDEX notify_channel_idx ON ds.notify_deliveries (channel, created_at);
CREATE INDEX notify_run_idx ON ds.notify_deliveries (run_id, delivery_id) WHERE run_id IS NOT NULL;
CREATE INDEX notify_created_idx ON ds.notify_deliveries (created_at);
CREATE TABLE ds.notify_mutes (
    mute_id    text PRIMARY KEY CHECK (mute_id ~ '^M-[0-9A-Z]{26}$'),
    scope      text NOT NULL CHECK (scope IN ('channel', 'run')),
    target     text NOT NULL CHECK (target ~ '^([a-z][a-z0-9]*(-[a-z0-9]+)*|R-[0-9A-Z]{26})$'),
    until      timestamptz NOT NULL,
    reason     ds.note NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    ended_by   text,
    ended_at   timestamptz,
    CHECK ((scope = 'run') = (target ~ '^R-')),
    CHECK (until > created_at AND until <= created_at + interval '7 days'),
    CHECK ((ended_by IS NULL) = (ended_at IS NULL))
);
CREATE INDEX notify_mutes_active_idx ON ds.notify_mutes (until) WHERE ended_at IS NULL;
CREATE FUNCTION ds.tg_notify_mutes() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN  -- ended once, as of now(), nothing else changed (ds.tg_licences' pattern); the prune deletes
    IF OLD.ended_at IS NOT NULL OR NEW.ended_at IS DISTINCT FROM now()
       OR (to_jsonb(NEW) - ARRAY['ended_by', 'ended_at']) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['ended_by', 'ended_at']) THEN
        RAISE EXCEPTION 'ds.notify_mutes: mute % is only ever ended, once, as of now()', OLD.mute_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER notify_mutes_end_only BEFORE UPDATE ON ds.notify_mutes
    FOR EACH ROW EXECUTE FUNCTION ds.tg_notify_mutes();
