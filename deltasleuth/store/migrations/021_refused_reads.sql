-- How often a key went back unread because every read it made was refused unsent (its target's breaker
-- open, or the target asking callers to wait past the call's timeout): a key refused that often is judged
-- as read. The check is NOT VALID: every earlier row holds the default, which satisfies it, so nothing
-- scans run_keys under ACCESS EXCLUSIVE; new rows are checked.
ALTER TABLE ds.run_keys
    ADD COLUMN refused smallint NOT NULL DEFAULT 0,
    ADD CONSTRAINT run_keys_refused CHECK (refused BETWEEN 0 AND 100) NOT VALID;
