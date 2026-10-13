-- Four-eyes approvals.
CREATE FUNCTION ds.accountable(actor text) RETURNS text[] LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN actor ~ '^user:U-[0-9a-f]{16}$' THEN ARRAY[actor]
        WHEN actor ~ '^token:T-[0-9a-f]{16}$' THEN coalesce((
            SELECT array_remove(ARRAY[substring(t.created_by FROM '^user:U-[0-9a-f]{16}$'),
                                      'user:' || t.owner_user_id], NULL)
            FROM ds.api_tokens t WHERE t.token_id = split_part(actor, ':', 2)), '{}')
        ELSE '{}' END
$$;
CREATE FUNCTION ds.attributed(actor text) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(actor ~ '^user:U-[0-9a-f]{16}$', false)
$$;

ALTER TABLE ds.runs ADD UNIQUE (run_id, spec_id);
CREATE DOMAIN ds.note AS text CHECK (VALUE <> '' AND length(VALUE) <= 1000 AND VALUE !~ '[\x01-\x1f\x7f-\x9f]');

CREATE TABLE ds.approvals (
    approval_id    text PRIMARY KEY CHECK (approval_id ~ '^A-[0-9A-Z]{26}$'),
    kind           text NOT NULL CHECK (kind IN ('spec', 'signoff')),
    spec_id        bigint NOT NULL REFERENCES ds.specs,
    run_id         text,
    cycle          integer,
    scopes         text[] NOT NULL DEFAULT '{}'
                   CHECK (scopes <@ '{clear,unlisted_clear,known_diffs,population_filters,raw_clear}'),
    attests        jsonb CHECK (jsonb_typeof(attests) = 'object'),
    attests_sha256 text CHECK (attests_sha256 ~ '^[0-9a-f]{64}$'),
    status         text NOT NULL DEFAULT 'requested'
                   CHECK (status IN ('requested', 'approved', 'rejected', 'revoked')),
    requested_by   text NOT NULL CHECK (requested_by ~ '^(user:U|token:T)-[0-9a-f]{16}$'),
    requested_at   timestamptz NOT NULL DEFAULT now(),
    request_note   ds.note,
    authors        text[],     -- set by the trigger
    unattributed   boolean,
    first_by       text CHECK (first_by ~ '^user:U-[0-9a-f]{16}$'),
    first_at       timestamptz,
    decided_by     text CHECK (decided_by ~ '^user:U-[0-9a-f]{16}$'),
    decided_at     timestamptz,
    decision_note  ds.note,
    revoked_by     text,
    revoked_at     timestamptz,
    revoke_reason  ds.note,
    UNIQUE (approval_id, spec_id, kind),
    FOREIGN KEY (run_id, spec_id) REFERENCES ds.runs (run_id, spec_id),
    FOREIGN KEY (run_id, cycle) REFERENCES ds.cycles (run_id, cycle),
    CHECK ((kind = 'signoff') = (run_id IS NOT NULL) AND (run_id IS NULL) = (cycle IS NULL)
           AND (run_id IS NULL) = (attests IS NULL) AND (attests IS NULL) = (attests_sha256 IS NULL)),
    CHECK (kind = 'spec' OR scopes = '{}'),
    CHECK ((status = 'requested') = (decided_by IS NULL) AND (decided_by IS NULL) = (decided_at IS NULL)),
    CHECK ((first_by IS NULL) = (first_at IS NULL) AND (status <> 'approved' OR first_by <> decided_by)),
    CHECK (status <> 'approved' OR (authors IS NOT NULL AND (NOT unattributed OR first_by IS NOT NULL))),
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL) AND (revoked_at IS NULL) = (revoked_by IS NULL)
           AND (revoked_at IS NULL) = (revoke_reason IS NULL))
);
CREATE UNIQUE INDEX approvals_open_spec ON ds.approvals (spec_id) WHERE kind = 'spec' AND status = 'requested';
CREATE UNIQUE INDEX approvals_active_spec ON ds.approvals (spec_id) WHERE kind = 'spec' AND status = 'approved';
CREATE UNIQUE INDEX approvals_open_signoff ON ds.approvals (run_id, cycle) WHERE kind = 'signoff' AND status = 'requested';
CREATE UNIQUE INDEX approvals_signed_cycle ON ds.approvals (run_id, cycle) WHERE kind = 'signoff' AND status = 'approved';

CREATE FUNCTION ds.spec_accountable(spec bigint, OUT persons text[], OUT unattributed boolean)
LANGUAGE sql STABLE AS $$
    WITH this AS (SELECT spec_id, name FROM ds.specs WHERE spec_id = spec),
    base AS (
        SELECT coalesce(max(s.spec_id), 0) AS id FROM ds.specs s JOIN this ON s.name = this.name
        WHERE s.spec_id < this.spec_id AND EXISTS (
            SELECT 1 FROM ds.approvals a WHERE a.spec_id = s.spec_id AND a.kind = 'spec' AND a.status = 'approved')),
    versions AS (
        SELECT s.spec_id, s.created_by FROM ds.specs s JOIN this ON s.name = this.name, base
        WHERE s.spec_id > base.id AND s.spec_id <= this.spec_id),
    uploads AS (
        SELECT v.created_by AS actor FROM versions v
        UNION ALL
        SELECT l.actor FROM versions v
        JOIN ds.audit_log l ON l.object = v.spec_id::text AND l.action = 'spec.create')
    SELECT (SELECT coalesce(array_agg(DISTINCT x ORDER BY x), '{}') FROM uploads, unnest(ds.accountable(actor)) AS x),
           coalesce(bool_or(NOT ds.attributed(actor)), true)
    FROM uploads
$$;

CREATE FUNCTION ds.approval_authors(a ds.approvals, OUT persons text[], OUT unattributed boolean)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF a.kind = 'spec' THEN
        SELECT s.persons, s.unattributed INTO persons, unattributed FROM ds.spec_accountable(a.spec_id) s;
    ELSE
        SELECT coalesce(p.authors, '{}') || ds.accountable(r.created_by),
               p.unattributed IS NOT FALSE OR NOT ds.attributed(r.created_by)
          INTO persons, unattributed
          FROM ds.runs r LEFT JOIN ds.approvals p ON p.approval_id = a.attests->>'spec_approval_id'
         WHERE r.run_id = a.run_id;
    END IF;
    persons := ARRAY(SELECT DISTINCT x FROM unnest(persons || ds.accountable(a.requested_by)) AS x ORDER BY x);
    unattributed := unattributed IS NOT FALSE OR NOT ds.attributed(a.requested_by);
END $$;

CREATE FUNCTION ds.tg_approvals() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_mutable text[] := ARRAY['status', 'authors', 'unattributed', 'first_by', 'first_at', 'decided_by',
                              'decided_at', 'decision_note', 'revoked_by', 'revoked_at', 'revoke_reason'];
    v_step boolean;
    v_by text;
    v record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ds.approvals: approval % cannot be deleted', OLD.approval_id
            USING ERRCODE = 'restrict_violation';
    ELSIF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'requested' OR num_nonnulls(NEW.first_by, NEW.authors, NEW.unattributed) > 0 THEN
            RAISE EXCEPTION 'ds.approvals: approval % must start as an undecided request', NEW.approval_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF (OLD.status, NEW.status) NOT IN (('requested', 'requested'), ('requested', 'approved'),
                                        ('requested', 'rejected'), ('approved', 'revoked'))
       OR (to_jsonb(NEW) - v_mutable) IS DISTINCT FROM (to_jsonb(OLD) - v_mutable)
       OR (OLD.first_by IS NOT NULL AND (NEW.first_by, NEW.first_at) IS DISTINCT FROM (OLD.first_by, OLD.first_at))
       OR (OLD.status <> 'requested'
           AND (NEW.authors, NEW.unattributed, NEW.decided_by, NEW.decided_at, NEW.decision_note)
               IS DISTINCT FROM (OLD.authors, OLD.unattributed, OLD.decided_by, OLD.decided_at, OLD.decision_note))
    THEN
        RAISE EXCEPTION 'ds.approvals: approval % cannot go from % to % or change what it covers',
                OLD.approval_id, OLD.status, NEW.status
            USING ERRCODE = 'restrict_violation';
    END IF;
    v_step := NEW.status = 'approved' OR (OLD.first_by IS NULL AND NEW.first_by IS NOT NULL);
    v_by := CASE WHEN OLD.status = 'requested' AND NEW.status <> 'requested' THEN NEW.decided_by
                 WHEN v_step THEN NEW.first_by END;
    -- Every person this update names as deciding: the decider, and a first approver set by the same
    -- statement (the app writes them apart, but every writer meets this check).
    SELECT x INTO v_by FROM unnest(ARRAY[v_by, CASE WHEN OLD.first_by IS NULL THEN NEW.first_by END]) AS x
     WHERE x IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM ds.users u WHERE 'user:' || u.user_id = x AND u.disabled_at IS NULL
                                   AND 'rule_approver' = ANY (u.roles))
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'ds.approvals: % is not an enabled rule_approver', v_by USING ERRCODE = 'check_violation';
    END IF;
    v_by := CASE WHEN OLD.status = 'requested' AND NEW.status <> 'requested' THEN NEW.decided_by
                 WHEN v_step THEN NEW.first_by END;
    IF v_step THEN
        IF NEW.kind = 'signoff' THEN  -- a revoke waits
            PERFORM 1 FROM ds.approvals p WHERE p.approval_id = NEW.attests->>'spec_approval_id'
                AND p.kind = 'spec' AND p.spec_id = NEW.spec_id AND p.status = 'approved' FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'ds.approvals: the spec approval that % cites is not approved', NEW.approval_id
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        SELECT * INTO v FROM ds.approval_authors(NEW);
        NEW.authors := ARRAY(SELECT DISTINCT x FROM unnest(coalesce(OLD.authors, '{}') || v.persons) AS x ORDER BY x);
        NEW.unattributed := coalesce(OLD.unattributed, false) OR v.unattributed;
        IF v_by = ANY (NEW.authors) OR NEW.first_by = ANY (NEW.authors) THEN
            RAISE EXCEPTION 'ds.approvals: % may not approve % (four-eyes)', v_by, NEW.approval_id
                USING ERRCODE = 'check_violation';
        END IF;
        IF (NEW.status = 'approved') <> (NOT NEW.unattributed OR NEW.first_by IS NOT NULL AND NEW.first_by <> v_by) THEN
            RAISE EXCEPTION 'ds.approvals: approval % needs % approver(s)', NEW.approval_id,
                    CASE WHEN NEW.unattributed THEN 2 ELSE 1 END
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER approvals_lifecycle BEFORE INSERT OR UPDATE OR DELETE ON ds.approvals
    FOR EACH ROW EXECUTE FUNCTION ds.tg_approvals();
CREATE TRIGGER approvals_no_truncate BEFORE TRUNCATE ON ds.approvals
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();

ALTER TABLE ds.runs ADD COLUMN approval_id text,
    ADD COLUMN approval_kind text NOT NULL DEFAULT 'spec' CHECK (approval_kind = 'spec'),
    ADD COLUMN keys_cut jsonb,
    ADD FOREIGN KEY (approval_id, spec_id, approval_kind) REFERENCES ds.approvals (approval_id, spec_id, kind);
ALTER TABLE ds.cycles ADD COLUMN approval_id text REFERENCES ds.approvals, ADD COLUMN keys_cut jsonb;
CREATE INDEX runs_approval_idx ON ds.runs (approval_id) WHERE approval_id IS NOT NULL;

-- A stored version never changes: an approval covers its content hash, and runs and approvals name it.
-- save_spec's upsert of known content changes nothing, so it passes.
CREATE FUNCTION ds.tg_specs_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'ds.specs: spec % cannot be changed or deleted', OLD.spec_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER specs_immutable BEFORE UPDATE OR DELETE ON ds.specs
    FOR EACH ROW EXECUTE FUNCTION ds.tg_specs_immutable();
CREATE TRIGGER specs_no_truncate BEFORE TRUNCATE ON ds.specs
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();
