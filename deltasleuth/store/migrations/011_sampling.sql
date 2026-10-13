-- Sign-off sampling: a drawn key's strata, whether its own bucket chose it and its
-- fresh reads; a run's pending draw; each ended cycle's judgement; every draw, append-only but for its one
-- close. The run_keys checks are NOT VALID: every earlier row holds NULL or the default, which satisfies
-- them, so nothing scans run_keys under ACCESS EXCLUSIVE; new rows are checked.
ALTER TABLE ds.run_keys
    ADD COLUMN stratum text,
    ADD COLUMN signoff_stratum text,
    ADD COLUMN signoff_member boolean NOT NULL DEFAULT false,
    ADD COLUMN retries smallint NOT NULL DEFAULT 0,
    ADD CONSTRAINT run_keys_stratum_format
        CHECK (stratum ~ '^[A-Za-z0-9][A-Za-z0-9_.:=|+-]{0,255}$') NOT VALID,
    ADD CONSTRAINT run_keys_signoff_stratum_format
        CHECK (signoff_stratum ~ '^[A-Za-z0-9][A-Za-z0-9_.:=|+-]{0,255}$') NOT VALID,
    ADD CONSTRAINT run_keys_retries CHECK (retries BETWEEN 0 AND 10) NOT VALID,
    ADD CONSTRAINT run_keys_signoff_member_needs_stratum
        CHECK (NOT signoff_member OR signoff_stratum IS NOT NULL) NOT VALID;
ALTER TABLE ds.runs ADD COLUMN draw jsonb CHECK (jsonb_typeof(draw) = 'object');  -- {due[, error, at]}
ALTER TABLE ds.cycles ADD COLUMN sample jsonb CHECK (jsonb_typeof(sample) = 'object');  -- its judgement
CREATE TABLE ds.sample_draws (
    draw_id          text PRIMARY KEY CHECK (draw_id ~ '^D-[0-9A-Z]{26}$'),
    run_id           text NOT NULL,
    spec_id          bigint NOT NULL,
    cycle            integer NOT NULL CHECK (cycle >= 1),
    method           text NOT NULL CHECK (method IN ('bottom_k', 'rate')),
    population       text[] NOT NULL CHECK (cardinality(population) BETWEEN 1 AND 2),
    salt_label       text NOT NULL CHECK (salt_label ~ '^[A-Za-z0-9_.+-]{4,64}$'),
    nonce            text NOT NULL UNIQUE CHECK (nonce ~ '^[0-9a-f]{32}$' AND nonce ~ '[a-f]'),
    drawn_at         timestamptz NOT NULL,
    keys             integer NOT NULL CHECK (keys >= 1),
    frame            jsonb NOT NULL CHECK (jsonb_typeof(frame) = 'object'),
    strata           jsonb NOT NULL CHECK (jsonb_typeof(strata) = 'array'),
    selection_sha256 text NOT NULL CHECK (selection_sha256 ~ '^[0-9a-f]{64}$'),
    closed_by        text CHECK (closed_by ~ '^user:U-[0-9a-f]{16}$'),
    closed_at        timestamptz,
    close_note       ds.note,
    UNIQUE (run_id, cycle),
    FOREIGN KEY (run_id, spec_id) REFERENCES ds.runs (run_id, spec_id),
    CHECK ((closed_by IS NULL) = (closed_at IS NULL) AND (closed_at IS NULL) = (close_note IS NULL))
);
CREATE INDEX sample_draws_population_idx ON ds.sample_draws USING gin (population);
CREATE FUNCTION ds.tg_sample_draws() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_close text[] := ARRAY['closed_by', 'closed_at', 'close_note'];
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.drawn_at IS DISTINCT FROM now() OR NEW.closed_at IS NOT NULL
           OR NOT EXISTS (SELECT 1 FROM ds.runs r WHERE r.run_id = NEW.run_id AND r.cycle = NEW.cycle
                            AND r.status = 'running' AND r.draw IS NOT NULL)
           OR EXISTS (SELECT 1 FROM ds.run_keys k WHERE k.run_id = NEW.run_id AND k.cycle = NEW.cycle) THEN
            RAISE EXCEPTION 'ds.sample_draws: draw % is made as of now(), for a running run''s current cycle, before its keys',
                NEW.draw_id USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' OR OLD.closed_at IS NOT NULL OR NEW.closed_at IS DISTINCT FROM now()
       OR (to_jsonb(NEW) - v_close) IS DISTINCT FROM (to_jsonb(OLD) - v_close)
       OR NOT EXISTS (SELECT 1 FROM ds.cycles c WHERE (c.run_id, c.cycle) = (OLD.run_id, OLD.cycle)) THEN
        RAISE EXCEPTION 'ds.sample_draws: draw % is only ever closed, once, as of now(), after its cycle ended',
            OLD.draw_id USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sample_draws_guard BEFORE INSERT OR UPDATE OR DELETE ON ds.sample_draws
    FOR EACH ROW EXECUTE FUNCTION ds.tg_sample_draws();
CREATE TRIGGER sample_draws_no_truncate BEFORE TRUNCATE ON ds.sample_draws
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();
ALTER TABLE ds.approvals DROP CONSTRAINT approvals_scopes_check, ADD CONSTRAINT approvals_scopes_check
    CHECK (scopes <@ '{clear,unlisted_clear,known_diffs,population_filters,sampling,raw_clear}');

-- A sign-off's exclusion set also holds whoever closed a draw of the ledger it attests, the scorer
-- of its adjudication kept.
CREATE OR REPLACE FUNCTION ds.approval_authors(a ds.approvals, OUT persons text[], OUT unattributed boolean)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF a.kind = 'spec' THEN
        SELECT s.persons, s.unattributed INTO persons, unattributed FROM ds.spec_accountable(a.spec_id) s;
    ELSE
        SELECT coalesce(p.authors, '{}') || ds.accountable(r.created_by) || ds.accountable(j.scored_by)
                   || ARRAY(SELECT x FROM jsonb_array_elements(coalesce(a.attests->'sample'->'ledger', '[]')) l,
                                   unnest(ds.accountable(l->>'closed_by')) x),
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
