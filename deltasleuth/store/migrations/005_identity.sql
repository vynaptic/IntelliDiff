-- People, sessions, API tokens and an attributable audit log (docs/mvp/AUTH.md).
-- Roles come from the IdP at each sign-in; no table grants them. '[\x01-\x1f\x7f-\x9f]' is every
-- C0 and C1 control character, spelled out so the check does not depend on the database locale.
CREATE TABLE ds.users (
    user_id       text PRIMARY KEY CHECK (user_id ~ '^U-[0-9a-f]{16}$'),
    issuer        text NOT NULL,
    subject       text NOT NULL CHECK (subject <> '' AND octet_length(subject) <= 255),
    username      text NOT NULL CHECK (username <> '' AND length(username) <= 256
                                       AND username !~ '[\x01-\x1f\x7f-\x9f]'),
    display_name  text CHECK (length(display_name) <= 256 AND display_name !~ '[\x01-\x1f\x7f-\x9f]'),
    roles         text[] NOT NULL CHECK (roles <@ '{viewer,operator,spec_author,rule_approver,pii_reader,admin}'),
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz,
    disabled_at   timestamptz,
    disabled_by   text,
    UNIQUE (issuer, subject),
    CHECK ((disabled_at IS NULL) = (disabled_by IS NULL))
);

CREATE TABLE ds.api_tokens (
    token_id      text PRIMARY KEY CHECK (token_id ~ '^T-[0-9a-f]{16}$'),
    name          text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9._-]{2,63}$'),
    owner         text NOT NULL CHECK (owner <> '' AND length(owner) <= 200 AND owner !~ '[\x01-\x1f\x7f-\x9f]'),
    owner_user_id text REFERENCES ds.users,       -- the accountable person; required over the API in oidc (§5)
    role          text NOT NULL CHECK (role IN ('viewer', 'operator', 'spec_author')),  -- TOKEN_ROLES
    secret_hash   bytea NOT NULL CHECK (octet_length(secret_hash) = 32),              -- sha256(token)
    created_by    text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    last_used_at  timestamptz,
    revoked_at    timestamptz,
    revoked_by    text,
    CHECK (expires_at > created_at),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
);
CREATE INDEX api_tokens_owner_idx ON ds.api_tokens (owner_user_id) WHERE revoked_at IS NULL;

CREATE TABLE ds.sessions (
    session_id   text PRIMARY KEY CHECK (session_id ~ '^W-[0-9a-f]{16}$'),
    secret_hash  bytea NOT NULL UNIQUE CHECK (octet_length(secret_hash) = 32),
    user_id      text REFERENCES ds.users,
    token_id     text REFERENCES ds.api_tokens,
    actor        text NOT NULL,
    name         text NOT NULL,
    roles        text[] NOT NULL CHECK (roles <@ '{viewer,operator,spec_author,rule_approver,pii_reader,admin}'),
    roles_sha256 text CHECK (roles_sha256 ~ '^[0-9a-f]{64}$'),   -- the role mapping it was granted under
    client_addr  inet,
    auth_time    timestamptz,          -- ID token claims: token creation (§5), step-up
    acr          text,
    amr          text[],
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz,
    revoked_by   text,
    CHECK ((user_id IS NULL) <> (token_id IS NULL)),
    CHECK ((user_id IS NULL) = (roles_sha256 IS NULL)),
    CHECK (actor = coalesce('user:' || user_id, 'token:' || token_id)),
    CHECK (expires_at > created_at),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
);
CREATE INDEX sessions_user_idx ON ds.sessions (user_id) WHERE revoked_at IS NULL;
CREATE INDEX sessions_token_idx ON ds.sessions (token_id) WHERE revoked_at IS NULL;
CREATE INDEX sessions_expiry_idx ON ds.sessions (expires_at);

ALTER TABLE ds.specs ADD COLUMN created_by text;          -- NULL before 005
ALTER TABLE ds.runs ADD COLUMN created_by text;
ALTER TABLE ds.chat_sessions ADD COLUMN created_by text;

CREATE FUNCTION ds.actor_name(actor text) RETURNS text LANGUAGE sql STABLE AS $$
    SELECT coalesce(CASE split_part(actor, ':', 1)
        WHEN 'user' THEN (SELECT username FROM ds.users WHERE user_id = split_part(actor, ':', 2))
        WHEN 'token' THEN (SELECT name FROM ds.api_tokens WHERE token_id = split_part(actor, ':', 2))
    END, actor)
$$;

-- ADD COLUMN updates no row, so the append-only trigger does not fire; old rows are not re-checked.
ALTER TABLE ds.audit_log
    ADD COLUMN actor_name  text CHECK (actor_name !~ '[\x01-\x1f\x7f-\x9f]'),
    ADD COLUMN request_id  text CHECK (request_id ~ '^[QC]-[0-9a-f]{16}$'),
    ADD COLUMN session_id  text CHECK (session_id ~ '^W-[0-9a-f]{16}$'),
    ADD COLUMN client_addr inet;
ALTER TABLE ds.audit_log ADD CONSTRAINT audit_actor CHECK (actor ~ ('^(user:U-[0-9a-f]{16}'
    '|token:T-[0-9a-f]{16}|local:[A-Za-z0-9._-]{1,64}@[A-Za-z0-9._-]{1,253}'
    '|system:(worker|server|migration)|dev:loopback)$')) NOT VALID;
CREATE INDEX audit_log_actor_idx ON ds.audit_log (actor, id);
CREATE INDEX audit_log_action_idx ON ds.audit_log (action, id);
CREATE INDEX audit_log_object_idx ON ds.audit_log (object, id);
CREATE INDEX audit_log_session_idx ON ds.audit_log (session_id, id) WHERE session_id IS NOT NULL;
