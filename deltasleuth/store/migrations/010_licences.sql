-- Licence keys and entitlements: this database's installation id,
-- made once and kept by a dump and restore, and the licences installed on it, verified again at every
-- read against the vendor keys the build embeds: a row written with SQL grants nothing. The id comes from
-- the first audit chain row, the same in a dump from before 010, so 010 applied to that dump again makes
-- the same id; it is random only on a store with no audit row yet (a fresh one).
CREATE TABLE ds.installation (
    installation_id text PRIMARY KEY CHECK (installation_id ~ '^I-[0-9a-f]{16}$'),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX installation_one ON ds.installation ((true));  -- one row
INSERT INTO ds.installation (installation_id)
    VALUES ('I-' || left(encode(sha256(convert_to(coalesce(
        'deltasleuth-installation' || E'\n' || (SELECT hash FROM ds.audit_chain ORDER BY seq LIMIT 1),
        gen_random_uuid()::text), 'UTF8')), 'hex'), 16));
CREATE TRIGGER installation_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON ds.installation
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();
CREATE TABLE ds.licences (
    licence_id      text PRIMARY KEY CHECK (licence_id ~ '^LI-[0-9A-Z]{26}$'),
    text            text NOT NULL CHECK (octet_length(text) <= 4096),  -- the normalised file
    sha256          text NOT NULL UNIQUE CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    installed_by    text NOT NULL,
    installed_at    timestamptz NOT NULL DEFAULT now(),
    withdrawn_by    text,
    withdrawn_at    timestamptz,
    withdraw_reason ds.note,
    CHECK ((withdrawn_by IS NULL) = (withdrawn_at IS NULL) AND (withdrawn_at IS NULL) = (withdraw_reason IS NULL))
);
-- A withdrawal is written once, as of the transaction's now(): a backdated one would re-date a licence.
CREATE FUNCTION ds.tg_licences() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_withdrawal text[] := ARRAY['withdrawn_by', 'withdrawn_at', 'withdraw_reason'];
BEGIN
    IF TG_OP = 'DELETE' OR OLD.withdrawn_at IS NOT NULL OR NEW.withdrawn_at IS DISTINCT FROM now()
       OR (to_jsonb(NEW) - v_withdrawal) IS DISTINCT FROM (to_jsonb(OLD) - v_withdrawal) THEN
        RAISE EXCEPTION 'ds.licences: licence % is only ever withdrawn, once, as of now()', OLD.licence_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER licences_withdraw_only BEFORE UPDATE OR DELETE ON ds.licences
    FOR EACH ROW EXECUTE FUNCTION ds.tg_licences();
CREATE TRIGGER licences_no_truncate BEFORE TRUNCATE ON ds.licences
    FOR EACH STATEMENT EXECUTE FUNCTION ds.append_only();
-- The runs in a licence window, those not finished before it starts (repo.counted_pairs).
CREATE INDEX runs_window_idx ON ds.runs ((coalesce(finished_at, 'infinity')), spec_id, created_at);
