-- Cut-over evidence packs. The hash-chained audit log: every audit row, from
-- any writer, gets a ds.audit_chain row in commit order under the DSAU lock; the rows already written are
-- chained first, in id order, while inserts wait on the table lock.
CREATE FUNCTION ds.audit_text(a ds.audit_log) RETURNS text LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_object('id', a.id, 'at', to_char(a.at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'actor', a.actor, 'actor_name', a.actor_name, 'action', a.action, 'object', a.object, 'detail', a.detail,
        'request_id', a.request_id, 'session_id', a.session_id, 'client_addr', host(a.client_addr))::text
$$;
CREATE FUNCTION ds.chain_hash(prev text, obj_prev text, body text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT encode(sha256(convert_to(prev || E'\n' || obj_prev || E'\n' || body, 'UTF8')), 'hex')
$$;
CREATE TABLE ds.audit_chain (
    seq bigint PRIMARY KEY, id bigint NOT NULL UNIQUE REFERENCES ds.audit_log, object text NOT NULL,
    prev text NOT NULL, obj_prev text NOT NULL, hash text NOT NULL UNIQUE, text text NOT NULL,
    CHECK (hash = ds.chain_hash(prev, obj_prev, text))
);
CREATE INDEX audit_chain_object_idx ON ds.audit_chain (object, seq);
CREATE FUNCTION ds.chain_append(r ds.audit_log) RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_seq bigint; v_prev text; v_obj text; v_text text := ds.audit_text(r);
BEGIN
    PERFORM pg_advisory_xact_lock(1146306901);  -- 0x44534155 "DSAU": one chain writer, in commit order
    SELECT seq, hash INTO v_seq, v_prev FROM ds.audit_chain ORDER BY seq DESC LIMIT 1;
    SELECT hash INTO v_obj FROM ds.audit_chain WHERE object = r.object ORDER BY seq DESC LIMIT 1;
    v_prev := coalesce(v_prev, repeat('0', 64));
    v_obj := coalesce(v_obj, repeat('0', 64));
    INSERT INTO ds.audit_chain VALUES (coalesce(v_seq, 0) + 1, r.id, r.object, v_prev, v_obj,
                                       ds.chain_hash(v_prev, v_obj, v_text), v_text);
END $$;
CREATE FUNCTION ds.tg_audit_chain() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN PERFORM ds.chain_append(NEW); RETURN NULL; END $$;
LOCK TABLE ds.audit_log IN SHARE ROW EXCLUSIVE MODE;  -- inserts wait, then are chained
DO $$ DECLARE r ds.audit_log;
BEGIN FOR r IN SELECT * FROM ds.audit_log ORDER BY id LOOP PERFORM ds.chain_append(r); END LOOP; END $$;
CREATE TABLE ds.audit_chain_upgrade AS SELECT coalesce(max(seq), 0) AS upgrade_seq FROM ds.audit_chain;
CREATE TRIGGER audit_log_chain AFTER INSERT ON ds.audit_log FOR EACH ROW EXECUTE FUNCTION ds.tg_audit_chain();
CREATE TRIGGER audit_chain_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON ds.audit_chain
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();
CREATE TRIGGER audit_chain_upgrade_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON ds.audit_chain_upgrade
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();

-- The census of each cycle: {cycle, at, a, b}, stored on the run for its current cycle and
-- copied to the cycle's tombstone when that cycle ends.
ALTER TABLE ds.runs ADD COLUMN census jsonb CHECK (jsonb_typeof(census) = 'object');
ALTER TABLE ds.cycles ADD COLUMN census jsonb CHECK (jsonb_typeof(census) = 'object');

-- Reviewer sheets of an ended cycle: drawn with a server salt, scored once, tombstoned by the purge.
CREATE TABLE ds.adjudications (
    adjudication_id text PRIMARY KEY CHECK (adjudication_id ~ '^J-[0-9A-Z]{26}$'),
    run_id          text NOT NULL,
    cycle           integer NOT NULL,
    salt            text NOT NULL CHECK (salt ~ '^[0-9a-f]{32}$'),
    sample_limit    integer NOT NULL CHECK (sample_limit BETWEEN 1 AND 500),
    selection       jsonb NOT NULL CHECK (jsonb_typeof(selection) = 'object'),
    sheet           text,
    drawn_by        text NOT NULL,
    drawn_at        timestamptz NOT NULL DEFAULT now(),
    upload_sha256   text CHECK (upload_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    sheet_sha256    text CHECK (sheet_sha256 ~ '^[0-9a-f]{64}$'),
    score           jsonb CHECK (jsonb_typeof(score) = 'object'),
    score_sha256    text CHECK (score_sha256 ~ '^[0-9a-f]{64}$'),
    passed          boolean,
    scored_by       text,
    scored_at       timestamptz,
    purged_at       timestamptz,
    FOREIGN KEY (run_id, cycle) REFERENCES ds.cycles (run_id, cycle),
    CHECK ((sheet IS NULL) = (purged_at IS NOT NULL)),
    CHECK (num_nulls(upload_sha256, sheet_sha256, score, score_sha256, passed, scored_by, scored_at) IN (0, 7))
);
CREATE INDEX adjudications_cycle_idx ON ds.adjudications (run_id, cycle, adjudication_id);

-- A draw, one scoring and one purge; no draw or score while the cycle has a requested or approved
-- sign-off. The cycle's row lock orders a draw or score against a sign-off request, which takes it FOR
-- KEY SHARE before it reads the adjudications, so each sees the other or waits for it.
CREATE FUNCTION ds.tg_adjudications() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_fixed text[] := ARRAY['adjudication_id', 'run_id', 'cycle', 'salt', 'sample_limit', 'drawn_by', 'drawn_at'];
    v_signoff text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ds.adjudications: adjudication % cannot be deleted', OLD.adjudication_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.purged_at IS NULL AND NEW.purged_at IS NOT NULL THEN
        IF (to_jsonb(NEW) - 'purged_at' - 'sheet' - 'selection' - 'score')
               IS DISTINCT FROM (to_jsonb(OLD) - 'purged_at' - 'sheet' - 'selection' - 'score')
           OR NEW.selection IS DISTINCT FROM jsonb_set(OLD.selection, '{keys}', 'null')
           OR NEW.score IS DISTINCT FROM jsonb_set(OLD.score, '{disagreements}', 'null') THEN
            RAISE EXCEPTION 'ds.adjudications: the purge of adjudication % keeps its hashes and counts',
                    OLD.adjudication_id
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF num_nonnulls(NEW.score, NEW.purged_at) > 0 THEN
            RAISE EXCEPTION 'ds.adjudications: adjudication % must start as a blank sheet', NEW.adjudication_id
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF OLD.scored_at IS NOT NULL OR OLD.purged_at IS NOT NULL OR NEW.scored_at IS NULL
          OR NEW.purged_at IS NOT NULL OR NEW.selection IS DISTINCT FROM OLD.selection
          OR (SELECT bool_or(to_jsonb(NEW) -> f IS DISTINCT FROM to_jsonb(OLD) -> f) FROM unnest(v_fixed) f) THEN
        RAISE EXCEPTION 'ds.adjudications: adjudication % is scored once, then only purged', OLD.adjudication_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    PERFORM 1 FROM ds.cycles WHERE run_id = NEW.run_id AND cycle = NEW.cycle FOR UPDATE;
    SELECT approval_id INTO v_signoff FROM ds.approvals
     WHERE kind = 'signoff' AND run_id = NEW.run_id AND cycle = NEW.cycle AND status IN ('requested', 'approved')
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'ds.adjudications: cycle % of run % has a sign-off (%)', NEW.cycle, NEW.run_id, v_signoff
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER adjudications_lifecycle BEFORE INSERT OR UPDATE OR DELETE ON ds.adjudications
    FOR EACH ROW EXECUTE FUNCTION ds.tg_adjudications();
CREATE TRIGGER adjudications_no_truncate BEFORE TRUNCATE ON ds.adjudications
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();

-- A sign-off's exclusion set also holds whoever scored the adjudication it attests.
CREATE OR REPLACE FUNCTION ds.approval_authors(a ds.approvals, OUT persons text[], OUT unattributed boolean)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF a.kind = 'spec' THEN
        SELECT s.persons, s.unattributed INTO persons, unattributed FROM ds.spec_accountable(a.spec_id) s;
    ELSE
        SELECT coalesce(p.authors, '{}') || ds.accountable(r.created_by) || ds.accountable(j.scored_by),
               p.unattributed IS NOT FALSE OR NOT ds.attributed(r.created_by)
          INTO persons, unattributed
          FROM ds.runs r LEFT JOIN ds.approvals p ON p.approval_id = a.attests->>'spec_approval_id'
          LEFT JOIN ds.adjudications j ON j.adjudication_id = a.attests->'adjudication'->>'adjudication_id'
                                      AND j.run_id = a.run_id AND j.cycle = a.cycle
         WHERE r.run_id = a.run_id;
    END IF;
    persons := ARRAY(SELECT DISTINCT x FROM unnest(persons || ds.accountable(a.requested_by)) AS x ORDER BY x);
    unattributed := unattributed IS NOT FALSE OR NOT ds.attributed(a.requested_by);
END $$;

-- Evidence packs: the record of each issued pack stays; its zip goes when its cycle is purged. An issued
-- pack needs its sign-off approved (read FOR SHARE, so a revoke waits for the issue, or the issue sees it);
-- a pack is only revoked, with its sign-off, or marked purged by its cycle's purge.
CREATE TABLE ds.packs (
    pack_id         text PRIMARY KEY CHECK (pack_id ~ '^P-[0-9A-Z]{26}$'),
    run_id          text NOT NULL,
    cycle           integer NOT NULL,
    signoff_id      text NOT NULL REFERENCES ds.approvals,
    adjudication_id text REFERENCES ds.adjudications,
    manifest        jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    sums_sha256     text NOT NULL UNIQUE CHECK (sums_sha256 ~ '^[0-9a-f]{64}$'),
    zip_sha256      text NOT NULL CHECK (zip_sha256 ~ '^[0-9a-f]{64}$'),
    key_fingerprint text NOT NULL CHECK (key_fingerprint ~ '^[0-9a-f]{64}$'),
    audit_seq       bigint NOT NULL,
    issued_by       text NOT NULL CHECK (issued_by ~ '^user:U-[0-9a-f]{16}$'),
    issued_at       timestamptz NOT NULL DEFAULT now(),
    status          text NOT NULL DEFAULT 'issued' CHECK (status IN ('issued', 'revoked')),
    revoked_by      text,
    revoked_at      timestamptz,
    revoke_reason   text,
    purged_at       timestamptz,
    FOREIGN KEY (run_id, cycle) REFERENCES ds.cycles (run_id, cycle),
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL) AND (revoked_at IS NULL) = (revoked_by IS NULL)
           AND (revoked_at IS NULL) = (revoke_reason IS NULL))
);
CREATE INDEX packs_cycle_idx ON ds.packs (run_id, cycle, pack_id);
CREATE INDEX packs_signoff_idx ON ds.packs (signoff_id);
CREATE TABLE ds.pack_zips (pack_id text PRIMARY KEY REFERENCES ds.packs, zip bytea NOT NULL);

CREATE FUNCTION ds.tg_packs() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_revoke text[] := ARRAY['status', 'revoked_by', 'revoked_at', 'revoke_reason'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ds.packs: pack % cannot be deleted', OLD.pack_id USING ERRCODE = 'restrict_violation';
    ELSIF TG_OP = 'UPDATE' THEN
        IF (OLD.status = 'issued' AND NEW.status = 'revoked'
            AND (to_jsonb(NEW) - v_revoke) = (to_jsonb(OLD) - v_revoke))
           OR (OLD.purged_at IS NULL AND NEW.purged_at IS NOT NULL
               AND (to_jsonb(NEW) - 'purged_at') = (to_jsonb(OLD) - 'purged_at')
               AND EXISTS (SELECT 1 FROM ds.cycles c WHERE c.run_id = NEW.run_id AND c.cycle = NEW.cycle
                                                       AND c.purging_at IS NOT NULL)) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'ds.packs: pack % is only revoked, or marked purged by its cycle''s purge', OLD.pack_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.status <> 'issued' OR NEW.purged_at IS NOT NULL THEN
        RAISE EXCEPTION 'ds.packs: pack % must start issued', NEW.pack_id USING ERRCODE = 'check_violation';
    END IF;
    PERFORM 1 FROM ds.approvals a
     WHERE a.approval_id = NEW.signoff_id AND a.kind = 'signoff' AND a.run_id = NEW.run_id
       AND a.cycle = NEW.cycle AND a.status = 'approved'
       FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ds.packs: the sign-off % of pack % is not approved', NEW.signoff_id, NEW.pack_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER packs_lifecycle BEFORE INSERT OR UPDATE OR DELETE ON ds.packs
    FOR EACH ROW EXECUTE FUNCTION ds.tg_packs();
CREATE TRIGGER packs_no_truncate BEFORE TRUNCATE ON ds.packs FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();

-- A zip is written with its pack and deleted only by its cycle's purge.
CREATE FUNCTION ds.tg_pack_zips() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' AND EXISTS (
        SELECT 1 FROM ds.packs p JOIN ds.cycles c USING (run_id, cycle)
         WHERE p.pack_id = OLD.pack_id AND c.purging_at IS NOT NULL) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'ds.pack_zips: the zip of pack % stays until its cycle is purged', OLD.pack_id
        USING ERRCODE = 'restrict_violation';
END $$;
CREATE TRIGGER pack_zips_lifecycle BEFORE INSERT OR UPDATE OR DELETE ON ds.pack_zips
    FOR EACH ROW EXECUTE FUNCTION ds.tg_pack_zips();
CREATE TRIGGER pack_zips_no_truncate BEFORE TRUNCATE ON ds.pack_zips
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();

-- A sign-off leaving approved, by any writer, revokes its packs, copying who, when and why.
CREATE FUNCTION ds.tg_signoff_packs() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE ds.packs SET status = 'revoked', revoked_by = NEW.revoked_by, revoked_at = NEW.revoked_at,
                        revoke_reason = NEW.revoke_reason
     WHERE signoff_id = NEW.approval_id AND status = 'issued';
    RETURN NULL;
END $$;
CREATE TRIGGER signoff_packs AFTER UPDATE OF status ON ds.approvals
    FOR EACH ROW WHEN (OLD.status = 'approved' AND NEW.status <> 'approved')
    EXECUTE FUNCTION ds.tg_signoff_packs();
