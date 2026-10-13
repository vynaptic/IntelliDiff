-- The end of plaintext storage (docs/mvp/RETENTION.md §7.1): the raw keys, headers and chat that
-- releases before 006 stored in clear go, once 'ds keys migrate' sealed them and moved every blob to its
-- keyed hk1: address. While legacy data remains the migration refuses, and the transaction that would
-- apply it (with 006, on a first start) rolls back: every entry point stops with the message.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM ds.run_keys WHERE key_enc IS NULL)
       OR EXISTS (SELECT 1 FROM ds.observations WHERE headers_enc IS NULL OR content_hash LIKE 'sha256:%')
       OR EXISTS (SELECT 1 FROM ds.chat_messages WHERE sealed IS NULL)
       OR EXISTS (SELECT 1 FROM ds.scans WHERE jsonb_typeof(state->'after') = 'array') THEN
        RAISE EXCEPTION 'legacy plaintext remains: stop the app and run ''ds keys migrate'' (docs/mvp/RETENTION.md §7.2)';
    END IF;
END $$;

ALTER TABLE ds.run_keys DROP COLUMN key, ALTER COLUMN key_enc SET NOT NULL;
ALTER TABLE ds.observations DROP COLUMN headers, ALTER COLUMN headers_enc SET NOT NULL,
    DROP CONSTRAINT observations_content_hash_check,
    ADD CONSTRAINT observations_content_hash_check CHECK (content_hash ~ '^hk1:[0-9a-f]{64}$');
ALTER TABLE ds.chat_messages DROP COLUMN content, DROP COLUMN tool_calls, ALTER COLUMN sealed SET NOT NULL;
