-- Labelled accuracy: a salted sample of an ended cycle's items, labelled once
-- against a named truth source; frozen by the cycle's sign-off, tombstoned by its purge.
CREATE TABLE ds.labellings (
    labelling_id     text PRIMARY KEY CHECK (labelling_id ~ '^V-[0-9A-Z]{26}$'),
    run_id           text NOT NULL,
    cycle            integer NOT NULL,
    salt             text NOT NULL CHECK (salt ~ '^[0-9a-f]{32}$' AND salt ~ '[a-f]'),
    size             integer NOT NULL CHECK (size BETWEEN 0 AND 1000),
    per_cause        integer NOT NULL CHECK (per_cause BETWEEN 0 AND 20),
    accepted         integer NOT NULL CHECK (accepted BETWEEN 0 AND 500),
    agreed           integer NOT NULL CHECK (agreed BETWEEN 0 AND 500),
    truth_source     text NOT NULL CHECK (truth_source ~ '^[A-Za-z][A-Za-z0-9_.:/-]{0,63}$'),
    truth_as_of      date NOT NULL,
    selection        jsonb NOT NULL CHECK (jsonb_typeof(selection) = 'object'),
    selection_sha256 text NOT NULL CHECK (selection_sha256 ~ '^[0-9a-f]{64}$'),
    sheet            text,
    drawn_by         text NOT NULL,
    drawn_at         timestamptz NOT NULL DEFAULT now(),
    sheet_sha256     text CHECK (sheet_sha256 ~ '^[0-9a-f]{64}$'),
    labels_sha256    text CHECK (labels_sha256 ~ '^[0-9a-f]{64}$'),
    accuracy         jsonb CHECK (jsonb_typeof(accuracy) = 'object'),
    score_sha256     text CHECK (score_sha256 ~ '^[0-9a-f]{64}$'),
    labelled_by      text,
    labelled_at      timestamptz,
    purged_at        timestamptz,
    FOREIGN KEY (run_id, cycle) REFERENCES ds.cycles (run_id, cycle),
    CHECK (size + per_cause + accepted + agreed > 0 AND size + accepted + agreed <= 1000),
    CHECK ((sheet IS NULL) = (purged_at IS NOT NULL)),
    CHECK (num_nulls(sheet_sha256, labels_sha256, accuracy, score_sha256, labelled_by, labelled_at) IN (0, 6))
);
CREATE INDEX labellings_cycle_idx ON ds.labellings (run_id, cycle, labelling_id);

CREATE FUNCTION ds.tg_labellings() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_fixed text[] := ARRAY['labelling_id', 'run_id', 'cycle', 'salt', 'size', 'per_cause', 'accepted',
                            'agreed', 'truth_source', 'truth_as_of', 'selection', 'selection_sha256',
                            'drawn_by', 'drawn_at'];
    v_signoff text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ds.labellings: labelling % cannot be deleted', OLD.labelling_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.purged_at IS NULL AND NEW.purged_at IS NOT NULL THEN
        IF (to_jsonb(NEW) - 'purged_at' - 'sheet' - 'selection')
               IS DISTINCT FROM (to_jsonb(OLD) - 'purged_at' - 'sheet' - 'selection')
           OR NEW.selection IS DISTINCT FROM jsonb_set(OLD.selection, '{items}', 'null') THEN
            RAISE EXCEPTION 'ds.labellings: the purge of labelling % keeps its hashes, counts and accuracy',
                    OLD.labelling_id USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF num_nonnulls(NEW.accuracy, NEW.labelled_at, NEW.purged_at) > 0 THEN
            RAISE EXCEPTION 'ds.labellings: labelling % must start unlabelled', NEW.labelling_id
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF OLD.labelled_at IS NOT NULL OR OLD.purged_at IS NOT NULL OR NEW.labelled_at IS NULL
          OR NEW.purged_at IS NOT NULL
          OR (SELECT bool_or(to_jsonb(NEW) -> f IS DISTINCT FROM to_jsonb(OLD) -> f) FROM unnest(v_fixed) f) THEN
        RAISE EXCEPTION 'ds.labellings: labelling % is labelled once, then only purged', OLD.labelling_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    PERFORM 1 FROM ds.cycles WHERE run_id = NEW.run_id AND cycle = NEW.cycle FOR UPDATE;
    SELECT approval_id INTO v_signoff FROM ds.approvals
     WHERE kind = 'signoff' AND run_id = NEW.run_id AND cycle = NEW.cycle AND status IN ('requested', 'approved')
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'ds.labellings: cycle % of run % has a sign-off (%)', NEW.cycle, NEW.run_id, v_signoff
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER labellings_lifecycle BEFORE INSERT OR UPDATE OR DELETE ON ds.labellings
    FOR EACH ROW EXECUTE FUNCTION ds.tg_labellings();
CREATE TRIGGER labellings_no_truncate BEFORE TRUNCATE ON ds.labellings
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();

-- A sign-off's exclusion set also holds whoever labelled the labelling it attests.
CREATE OR REPLACE FUNCTION ds.approval_authors(a ds.approvals, OUT persons text[], OUT unattributed boolean)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF a.kind = 'spec' THEN
        SELECT s.persons, s.unattributed INTO persons, unattributed FROM ds.spec_accountable(a.spec_id) s;
    ELSE
        SELECT coalesce(p.authors, '{}') || ds.accountable(r.created_by) || ds.accountable(j.scored_by)
                   || ds.accountable(v.labelled_by)
                   || ARRAY(SELECT x FROM jsonb_array_elements(coalesce(a.attests->'sample'->'ledger', '[]')) l,
                                   unnest(ds.accountable(l->>'closed_by')) x),
               p.unattributed IS NOT FALSE OR NOT ds.attributed(r.created_by)
          INTO persons, unattributed
          FROM ds.runs r LEFT JOIN ds.approvals p ON p.approval_id = a.attests->>'spec_approval_id'
          LEFT JOIN ds.adjudications j ON j.adjudication_id = a.attests->'adjudication'->>'adjudication_id'
                                      AND j.run_id = a.run_id AND j.cycle = a.cycle
          LEFT JOIN ds.labellings v ON v.labelling_id = a.attests->'accuracy'->>'labelling_id'
                                   AND v.run_id = a.run_id AND v.cycle = a.cycle
         WHERE r.run_id = a.run_id;
    END IF;
    persons := ARRAY(SELECT DISTINCT x FROM unnest(persons || ds.accountable(a.requested_by)) AS x ORDER BY x);
    unattributed := unattributed IS NOT FALSE OR NOT ds.attributed(a.requested_by);
END $$;
