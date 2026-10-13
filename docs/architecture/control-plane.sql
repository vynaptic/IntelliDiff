-- =============================================================================
-- DeltaSleuth control plane: PostgreSQL 17 DDL, schema "ds"
-- -----------------------------------------------------------------------------
-- File      docs/architecture/control-plane.sql
-- Version   0.3.2 (design artefact), 2026-09-24
-- Target    PostgreSQL 17. The file uses no feature newer than PostgreSQL 15
--           (NULLS NOT DISTINCT, security_invoker views), so it also applies
--           to PostgreSQL 15 and 16.
-- Names     Names follow docs/architecture.md §6–§7 verbatim: tables,
--           columns, outcomes, change types, severities, taxonomy codes, run
--           modes and key sources.
--
-- Purpose
--   System of record for DeltaSleuth:
--     * datasets (the ABAC scope of targets, specs and runs) and the targets
--       granted to each dataset (dataset_targets), target registry
--       (targets, secrets_ref), immutable comparison specs and their
--       four-eyes approvals (spec_approvals), the taxonomy registry
--       (taxonomy_codes: core codes plus domain packs), LLM provider profiles
--       (llm_profiles), the LLM host trust registry (llm_hosts) and eval-gated
--       LLM promotions (llm_promotions);
--     * runs, their key sets (run_keys, the frozen manifest) and the job queue
--       (work_units, leased with FOR UPDATE SKIP LOCKED; the MVP has no
--       external broker);
--     * observation metadata (bodies live in the object store as zstd
--       Parquet, content-addressed by sha256), per-key results, findings and
--       their roll-ups (signatures, root_causes, clusters);
--     * suppression rules (known_diffs, noise_masks), reports, agent chat
--       history with per-call LLM usage (llm_calls), the append-only
--       audit_log and optional users_roles;
--     * the transactional outbox (outbox, processed_events) and the public
--       API's Idempotency-Key store (idempotency_keys).
--   ds-mcp read tools read ONLY the six curated views (v_run_summary,
--   v_findings_masked, v_root_causes, v_results_masked, v_observations_meta,
--   v_rule_hits) through the read-only login role ds_agent_ro. DuckDB never
--   connects to PostgreSQL or the object store: ds-mcp exports the caller's
--   curated-view rows to a local Parquet file per session, and DuckDB
--   (external access off, locked configuration, no-network container) reads
--   only that file.
--
-- Conventions
--   * Every object name is schema-qualified. Nothing depends on search_path.
--   * Vocabularies from architecture.md §6–§7 that several tables use are
--     enum types or domains (ds.run_mode, ds.outcome, ds.inconclusive_reason,
--     ds.side_state, ds.verdict, ds.change_type, ds.severity, ds.side,
--     ds.finding_direction). Taxonomy
--     codes are rows of the registry table ds.taxonomy_codes, so a domain pack
--     adds its codes with INSERT and needs no DDL change. Vocabularies used by
--     one column are named CHECK constraints. Vocabularies that
--     architecture.md does not define (work unit state, report status, ...)
--     are marked "DDL-local". They are CHECKs, so they can change without a
--     type migration.
--   * path_pattern is the normalised RFC 9535 path in which every array index
--     or member key is replaced by [*], e.g. $.links[*].hop ('$' is the whole
--     document). json_pointer is the exact RFC 6901 pointer, with member keys
--     of tokenised classes replaced by their tokens. Pointer grammar:
--     each member key is RFC 6901-escaped ('~' -> '~0', '/' -> '~1'); the
--     index of a duplicate member is its own segment '@<i>' (e.g.
--     /phones/<tok>/@1), so segments that start with '@' are reserved and a
--     member key that itself starts with '@' is written with a doubled '@@'.
--   * A stratum label is dim=label pairs joined by '|', e.g.
--     degree_bucket=d51_250|id_type=BANK_ACCOUNT, at most 256 characters.
--     The MVP (docs/mvp/SAMPLING.md, migration 011) keeps a sampled key's
--     stratum, signoff_stratum, signoff_member and retries on run_keys only
--     (results and findings join them by key), keeps a union's origins as
--     counts (no attrs.origin), and stores each sign-off stratum's bounds once
--     in ds.cycles.sample, each named one-sided:
--     ub95_one_sided (exact for any x), ub95_one_sided_all_suppressed and
--     ub95_one_sided_worst_case.
--   * The MVP (docs/mvp/ACCURACY.md, migration 013) keeps a labelled sample
--     of an ended cycle in ds.labellings: a salted draw of its discrepant
--     items (one key in one root cause) and, when asked, of its accepted and
--     agreed keys; a named truth source; one labelled sheet and its score
--     (exact two-sided 95% Clopper-Pearson intervals), frozen by the cycle's
--     sign-off and tombstoned by its purge. The engine counts a person's
--     labels; it never decides which side is correct.
--   * The MVP (docs/mvp/READINESS.md, migration 014) keeps each ended cycle's
--     readiness measures in ds.cycles.readiness (jsonb, format 1: counts, ids,
--     codes and stratum labels; one-shot and captured-sample cycles marked
--     excluded), written once by the worker and kept with the tombstone, with
--     seven partial indexes: cycles_unmeasured_idx, cycles_unmeasured_run_idx,
--     cycles_readiness_pair_idx, cycles_readiness_unread_idx (records of
--     another format), cycles_readiness_evidence_idx and a
--     cycles_readiness_breaks_<tier>_idx for each of standard and high.
--     Readiness is named gates and a ladder state computed on read: never a
--     probability, never sign-off evidence.
--   * The MVP (docs/mvp/SCHEDULES.md, migration 015) keeps each schedule in
--     ds.schedules: a stored spec (pinned by spec_id, or followed by name), what a
--     run takes but keys, a 5-field cron expression, an IANA time zone, a start
--     window, who saved it and in which auth mode, its anchor (the last due time
--     settled), next_due, the newest settlement (last, last_at, last_run_id) and a
--     standing refusal's range not yet chained (held). Rows are never deleted: a
--     tombstone frees the name (schedules_name_idx is unique over the live ones)
--     and its runs still name it. On ds.runs, runs_schedule_due_idx is unique over
--     (params->'schedule'->>'schedule_id', params->'schedule'->>'due'), so one run
--     at most starts for a schedule and due time whoever writes it, and the check
--     runs_schedule (NOT VALID) keeps the due time in its canonical text. The
--     firing history is the audit chain (schedule.fire, schedule.missed,
--     schedule.rezone): no firings table. No key and no value is stored.
--   * The MVP (docs/mvp/PRECHECKS.md, migration 016) keeps each table pre-check
--     in ds.prechecks (its source, level, request, who made and cancelled it) and
--     each of its pairs in ds.precheck_pairs: the two targets and relations, a
--     status, when a worker took it (renewed while it reads it), and once read
--     its verdict, rank, differing figures, both row counts, its line and its
--     result (jsonb, format 1: counts, verdicts, closed codes, names and declared
--     types; never a minimum, maximum, distinct or key value).
--     precheck_pairs_due_idx finds the pairs the worker has to read and
--     precheck_pairs_worst_idx lists a pre-check's pairs worst first. A pre-check
--     is no run and no evidence: it goes retention.standard_days after it was
--     made (the pairs cascade) and its audit rows stay. ds.runs.guard (jsonb,
--     nullable, no default and no check, so ds.runs is neither rewritten nor
--     scanned) holds what the schema guard recorded of a run's tables: each side's
--     planned and last seen shape, their changes and who accepted them.
--   * Side a = legacy. Side b = new.
--     MISSING_* and missing_in_b = present in A, absent in B.
--     EXTRA_* and extra_in_b     = present in B, absent in A.
--   * Cycles are numbered from 1. runs.current_cycle_no = 0 means "not started".
--   * Timestamps are timestamptz. Hashes are lowercase hex. Keys are non-empty
--     text of at most 512 bytes, which keeps composite btree entries small.
--   * ds.severity sorts CRITICAL first: ORDER BY severity lists the most severe
--     rows first, min(severity) is the worst severity, and
--     severity <= 'HIGH' selects CRITICAL and HIGH.
--
-- Security
--   * This file contains no secret values, and the schema stores none.
--     targets.auth_ref only references a ds.secrets_ref row. That row holds a
--     locator (vault://, k8s://, awssm://, gcpsm://, env://) that the calling
--     service resolves at runtime. CHECK constraints reject locators and
--     endpoints that embed credentials.
--   * Roles are created without passwords. Login credentials are provisioned
--     out of band (secrets manager or client certificates plus pg_hba.conf).
--   * findings.value_a_tok / value_b_tok / member_key_tok / explaining_id_tok
--     and root_causes.root_key_tok never hold a raw value of a tokenised class.
--     Token format: 'tok:<class>:<kv>:' followed by
--     base32(HMAC-SHA256(K_kv, class || 0x1F || normalised_value))[:26],
--     where kv is the key version. Only the tokeniser service computes tokens
--     (KMS/HSM HMAC, non-exportable key); users and the agent cannot call it.
--     ds-mcp, ds-scheduler and ds-api reach it only through server-side
--     operations (tokenise a stored value given obs_id + pointer, or a
--     key-materialisation batch), never with caller-supplied strings, so it
--     is no dictionary oracle. Tokens are pseudonyms, not anonymisation. date_of_birth is never
--     tokenised: only its value_class is stored. A deployment-level PII floor
--     lists the always-tokenised classes, and a spec can only tighten it.
--     Writers apply the floor plus spec security.tokenize / security.clear
--     before insert.
--   * Keys are business codes shown in clear (spec key.class = business_code,
--     the default). When key.class is an identifier class, the key is stored
--     as its token in every table, view and report; the raw key is kept only
--     envelope-encrypted in run_keys.key_enc (KMS data key), which only
--     ds-fetcher and the reveal endpoint decrypt.
--   * Reveal (pii_reader, audited) re-derives a raw value from the raw
--     observation (results.obs_a_id / obs_b_id + findings.json_pointer).
--     Raw observation bodies are therefore retained at least as long as
--     findings: specs.retention_class standard = raw 90 d / findings 90 d,
--     audit = raw 1 y / findings 1 y. Expired evidence is marked
--     (observations.expired_at, evidence_expired in the curated views) in
--     findings, exports and citations, and AI answers that need it are
--     blocked.
--   * Evidence is served only as a masked rendition (tokenised with the
--     findings' rules), streamed by ds-api or behind a GET-only, single-object
--     URL valid for at most 60 s with Referrer-Policy: no-referrer. Raw
--     content-addressed blobs are never presigned. An unmasked report
--     (reports.masked = false) needs pii_reader, a second approver and an
--     audit row.
--   * Dataset scope. targets, specs and runs carry a NOT NULL
--     dataset_id (ds.datasets; the seeded 'default' dataset is the column
--     default). In this design ds-api enforces the scope on every request,
--     and ds-mcp filters every curated view by the requesting user's datasets
--     (each view exposes dataset_id), so AI visibility never exceeds the
--     user's. Row-level security is not built. A run may call only targets
--     owned by or granted to its dataset (ds.dataset_targets, trigger
--     runs_dataset_targets); the spec loader applies the same rule to every
--     target, template and inferred schema a spec references.
--   * LLM profiles hold only auth_ref (a ds.secrets_ref row), never a key.
--     Their trust is derived from the ds.llm_hosts registry, and a profile
--     with a credential must use https. ds_agent_ro cannot read
--     llm_profiles or llm_calls, and no curated view exposes LLM cost or
--     pricing.
--
-- How to apply (once, on an empty database; the file is a single transaction)
--   psql -X -v ON_ERROR_STOP=1 -d <database> -f docs/architecture/control-plane.sql
--   Run it as a superuser, or as a migration role that has CREATEROLE and can
--   SET ROLE ds_owner. Every object in schema ds is owned by ds_owner.
-- =============================================================================

BEGIN;

SET LOCAL lock_timeout = '10s';
SET LOCAL check_function_bodies = on;

-- =============================================================================
-- 1. Roles (cluster-wide, so they are created only if missing)
-- -----------------------------------------------------------------------------
-- ds_owner     NOLOGIN. Owns every object in schema ds. Migrations SET ROLE to it.
-- ds_app       NOLOGIN group for the service logins (ds-api, ds-fetcher,
--              ds-comparer, ds-scheduler, ds-reporter, ds-agent). The login
--              roles are provisioned per service by ops and granted ds_app.
-- ds_agent_ro  LOGIN role for ds-mcp read tools (and the per-session Parquet
--              export that DuckDB reads). SELECT on the six curated views of
--              section 10c only. It is a LOGIN role
--              because ALTER ROLE ... SET applies only to the role a session
--              logs in as; settings are not inherited through membership.
--              Never grant ds_agent_ro to another role, and never grant
--              another role to it.
-- =============================================================================
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'ds_owner') THEN
        CREATE ROLE ds_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'ds_app') THEN
        CREATE ROLE ds_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'ds_agent_ro') THEN
        CREATE ROLE ds_agent_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS NOINHERIT CONNECTION LIMIT 20;
    END IF;
END
$roles$;

-- Session defaults for the agent role. default_transaction_read_only is a
-- second line of defence: a session can turn it off, so the real control is
-- the grants in section 10d (SELECT on six views, nothing else).
-- statement_timeout matches the agent SQL budget in architecture.md §9.2 / Appendix D.1.
ALTER ROLE ds_agent_ro SET default_transaction_read_only = on;
ALTER ROLE ds_agent_ro SET statement_timeout = '10s';
ALTER ROLE ds_agent_ro SET lock_timeout = '2s';
ALTER ROLE ds_agent_ro SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE ds_agent_ro SET search_path = ds;

-- =============================================================================
-- 2. Schema
-- =============================================================================
CREATE SCHEMA ds AUTHORIZATION ds_owner;
REVOKE ALL ON SCHEMA ds FROM PUBLIC;

SET LOCAL ROLE ds_owner;

COMMENT ON SCHEMA ds IS
    'DeltaSleuth control plane. DDL version 0.3.2 (2026-09-24). See docs/architecture/control-plane.sql.';

-- Functions that ds_owner creates are not executable by PUBLIC. This must be a
-- global default: a per-schema default cannot revoke the global PUBLIC grant.
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- =============================================================================
-- 3. Types and domains (vocabularies from architecture.md §6–§7)
-- =============================================================================
-- rediff        what-if child run: re-compares the parent run's observations
--               (runs.parent_run_id = source run, required by ds-api) under a
--               new spec version; results.obs_a_id / obs_b_id point into the
--               parent run.
-- change_driven P4: keys come from a change feed (Debezium, ingestion offsets).
CREATE TYPE ds.run_mode AS ENUM (
    'one_shot', 'batch', 'soak', 'calibration', 'bulk_parity', 'table_diff',
    'rediff', 'change_driven'
);

-- One outcome per key per cycle.
--   Discrepant (counted as mismatched): MISMATCH, NOT_FOUND_A, NOT_FOUND_B
--                                       once confirmed (results.confirmation
--                                       <> provisional; a provisional one is
--                                       outstanding), and KNOWN_DIFF keys
--                                       with a finding suppressed by a rule of
--                                       classification 'bug' (a known open
--                                       defect)
--   Suppressed (counted separately):    KNOWN_DIFF, NOISE_ONLY
--   Neither parity nor mismatch:        DRIFT (results.drift_reason),
--                                       INCONCLUSIVE
-- MATCH = both sides COMPLETE and no unsuppressed finding with severity above
-- INFO, so a key whose only findings are NORMALIZATION_ONLY or SCHEMA_DRIFT
-- (drift on unselected paths only) stays MATCH. Drift on a selected or
-- compared path, or an undeclared graph type within scope, makes the side
-- INCOMPLETE and the key INCONCLUSIVE(schema_drift). A manifest seed
-- that is ABSENT on both sides is INCONCLUSIVE(absent_both), never MATCH.
-- KNOWN_DIFF: every finding above INFO is suppressed and at least one by an
-- approved known_diffs rule; both sides COMPLETE, or one side ABSENT when the
-- rule suppresses the confirmed NOT_FOUND key's document-level finding
-- (MISSING_DOCUMENT / EXTRA_DOCUMENT).
-- NOISE_ONLY: both sides COMPLETE, every finding above INFO is suppressed, at
-- least one by an approved noise mask, and none by a known_diffs rule.
-- Both need suppressed_count >= 1, so a key without findings is never
-- NOISE_ONLY.
CREATE TYPE ds.outcome AS ENUM (
    'MATCH', 'MISMATCH', 'KNOWN_DIFF', 'NOISE_ONLY', 'DRIFT',
    'NOT_FOUND_A', 'NOT_FOUND_B', 'INCONCLUSIVE'
);

-- Error mapping per side: timeout -> timeout, http_429 -> rate_limited,
-- circuit_open -> circuit_open, any other error -> error_a / error_b; both
-- sides erroring -> both_error. recheck_failed: no recheck reproduced the
-- first diff (an inconclusive recheck never confirms a mismatch).
-- Further reasons: unparseable (invalid date or type), resource_limit
-- (byte/node/page limits), identity_ambiguous (the crosswalk maps to 0 or
-- more than 1 keys), duplicate_member_name (repeated JSON member names,
-- RFC 8259 §4) and absent_both (a manifest seed ABSENT on both sides,
-- which points to a stale manifest).
-- Precedence when several apply (deterministic per N2): both_error >
-- error_a > error_b > timeout > rate_limited > circuit_open > the INCOMPLETE
-- reasons. results_reason_precedence enforces the part that the side states
-- decide: two ERROR sides are both_error, and an ERROR side never carries an
-- INCOMPLETE reason.
CREATE TYPE ds.inconclusive_reason AS ENUM (
    'error_a', 'error_b', 'both_error', 'timeout', 'rate_limited',
    'truncated', 'circuit_open', 'skipped', 'recheck_failed',
    'unparseable', 'resource_limit', 'identity_ambiguous',
    'duplicate_member_name', 'absent_both',
    'absence_unconfirmed',   -- a 404 or empty body without declared authoritative_absence: side INCOMPLETE
    'watermark_missing',     -- time.mode shared_watermark but a side reports no watermark: side INCOMPLETE
    'unstable_pagination',   -- offset/page pagination without stable_ordering or a per-request snapshot, or a key repeated across pages: side INCOMPLETE
    'schema_drift'           -- drift on a selected or compared path, or an undeclared graph type within scope: side INCOMPLETE
);

-- Per-side observation state, set by the connector against its declared
-- capabilities (targets.capabilities):
--   COMPLETE    the full document was read: every page to EOF, no cap hit,
--               structurally parseable (the completeness proof is kept with
--               the observation);
--   ABSENT      authoritative absence: only when the status or shape is in
--               the target's declared authoritative_absence;
--   INCOMPLETE  a cap or truncation, a pagination loop or unstable
--               pagination, a non-authoritative 404, an unparseable value, a
--               resource limit, schema drift on a selected or compared path,
--               or a side that was not read at all (skipped,
--               circuit_open);
--   ERROR       transport, timeout, or 429/5xx after retries (the fetcher sets
--               error_class only for these), or a status outside the side's
--               success statuses.
CREATE TYPE ds.side_state AS ENUM ('COMPLETE', 'ABSENT', 'INCOMPLETE', 'ERROR');

-- Derived three-value verdict for simple reporting and the public API.
-- It is NOT a column of ds.results: the KNOWN_DIFF split depends on the
-- known_diffs.classification of the rules cited by the key's findings (other
-- tables, written after the results row in the same transaction), so a
-- generated column is impossible. Approved rules are immutable, so the
-- split cannot drift after the fact. It is computed at read time in
-- v_result_verdicts, v_results_masked and v_run_summary (section 10c):
--   MATCH         <- MATCH, NOISE_ONLY, KNOWN_DIFF (accepted | expected_by_design)
--   DIFFERENT     <- MISMATCH, NOT_FOUND_A, NOT_FOUND_B, KNOWN_DIFF (bug)
--   INDETERMINATE <- INCONCLUSIVE, DRIFT
--   NULL          <- a provisional MISMATCH / NOT_FOUND_A / NOT_FOUND_B (not
--                    yet reproduced); v_run_summary counts it in
--                    n_outstanding
CREATE TYPE ds.verdict AS ENUM ('MATCH', 'DIFFERENT', 'INDETERMINATE');

-- For set members: the member is present on one side only.
-- For fields: the field is present on one side only.
CREATE TYPE ds.change_type AS ENUM (
    'missing_in_b', 'extra_in_b', 'value_changed', 'type_changed', 'order_changed'
);

-- Declaration order is the sort order: CRITICAL sorts first.
CREATE TYPE ds.severity AS ENUM ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO');

CREATE TYPE ds.side AS ENUM ('a', 'b');

CREATE TYPE ds.finding_direction AS ENUM ('a_only', 'b_only', 'both');

-- Taxonomy codes are not a type: they live in the registry table
-- ds.taxonomy_codes (section 5), so a new domain pack needs no core DDL.

CREATE DOMAIN ds.sha256_hex AS text
    CONSTRAINT sha256_hex_format CHECK (VALUE ~ '^[0-9a-f]{64}$');

-- Signature hashes may be truncated digests; 16..128 lowercase hex characters.
CREATE DOMAIN ds.hex_digest AS text
    CONSTRAINT hex_digest_format CHECK (VALUE ~ '^[0-9a-f]{16,128}$');

-- =============================================================================
-- 4. Trigger functions
-- =============================================================================
CREATE FUNCTION ds.tg_set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$fn$;
COMMENT ON FUNCTION ds.tg_set_updated_at() IS
    'BEFORE UPDATE row trigger: maintains updated_at on control tables. Hot-path tables (results, work_units) set updated_at in the statement instead.';

CREATE FUNCTION ds.tg_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    RAISE EXCEPTION 'ds.%: % is not allowed; this table is append-only/immutable', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation',
              HINT = 'Insert a new row instead (for specs: a new spec_version).';
    RETURN NULL;
END
$fn$;
COMMENT ON FUNCTION ds.tg_reject_mutation() IS
    'Raises on UPDATE, DELETE and TRUNCATE. Used by ds.audit_log (append-only) and ds.specs (immutable).';

-- Presence rule of a taxonomy code, read from the registry instead of a CHECK
-- that lists codes: presence 'missing' needs change_type missing_in_b
-- (present in A, absent in B), 'extra' needs extra_in_b, 'none' allows any.
-- A pack alias (taxonomy_codes.alias_of set, e.g. MISSING_ENTITY for
-- MISSING_DOCUMENT) is a display name: findings always store the core
-- code it aliases. An unknown code is left to the foreign key
-- findings_taxonomy_fk.
CREATE FUNCTION ds.tg_findings_taxonomy_presence()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_presence text;
    v_alias_of text;
BEGIN
    SELECT t.presence, t.alias_of INTO v_presence, v_alias_of
      FROM ds.taxonomy_codes AS t
     WHERE t.code = NEW.taxonomy_code;
    IF v_alias_of IS NOT NULL THEN
        RAISE EXCEPTION 'ds.findings: taxonomy_code % is a pack alias of %; findings store the core code',
                NEW.taxonomy_code, v_alias_of
            USING ERRCODE = 'check_violation',
                  HINT = 'Reports and the UI show the alias of an installed pack (ds.taxonomy_codes.alias_of).';
    END IF;
    IF (v_presence = 'missing' AND NEW.change_type <> 'missing_in_b')
       OR (v_presence = 'extra' AND NEW.change_type <> 'extra_in_b') THEN
        RAISE EXCEPTION 'ds.findings: taxonomy_code % (presence %) does not allow change_type %',
                NEW.taxonomy_code, v_presence, NEW.change_type
            USING ERRCODE = 'check_violation',
                  HINT = 'presence missing needs missing_in_b; presence extra needs extra_in_b (ds.taxonomy_codes.presence).';
    END IF;
    RETURN NEW;
END
$fn$;
COMMENT ON FUNCTION ds.tg_findings_taxonomy_presence() IS
    'BEFORE INSERT/UPDATE row trigger on ds.findings (cloned to every partition): enforces ds.taxonomy_codes.presence against change_type, and rejects pack aliases (alias_of set): findings store the core code.';

-- Four-eyes for noise masks: the approver must not have authored any version
-- of the spec the mask belongs to. (approved_by <> proposed_by is a CHECK.)
CREATE FUNCTION ds.tg_noise_masks_four_eyes()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF NEW.approved_by IS NOT NULL
       AND EXISTS (SELECT 1 FROM ds.specs AS s
                    WHERE s.spec_name = NEW.spec_name
                      AND s.created_by = NEW.approved_by) THEN
        RAISE EXCEPTION 'ds.noise_masks: % authored spec %, so cannot approve its noise masks (four-eyes)',
                NEW.approved_by, NEW.spec_name
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;
COMMENT ON FUNCTION ds.tg_noise_masks_four_eyes() IS
    'BEFORE INSERT/UPDATE OF approved_by row trigger on ds.noise_masks: the approver (rule_approver) is not an author (ds.specs.created_by) of the spec.';

-- Four-eyes for spec approvals, same rule as for noise masks: the
-- approver must not have authored any version of the spec's spec_name.
CREATE FUNCTION ds.tg_spec_approvals_four_eyes()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF EXISTS (SELECT 1
                 FROM ds.specs AS s
                 JOIN ds.specs AS approved ON approved.spec_name = s.spec_name
                WHERE approved.spec_id = NEW.spec_id
                  AND s.created_by = NEW.approved_by) THEN
        RAISE EXCEPTION 'ds.spec_approvals: % authored a version of spec %, so cannot approve it (four-eyes)',
                NEW.approved_by, NEW.spec_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;
COMMENT ON FUNCTION ds.tg_spec_approvals_four_eyes() IS
    'BEFORE INSERT row trigger on ds.spec_approvals: the approver (rule_approver) is not an author (ds.specs.created_by) of any version of the spec_name.';

-- A run needs a spec approval covering every clear-text or population-
-- filter scope its spec uses. clear = security.clear entries; rawClear =
-- sides.*.rawClear entries; unlisted_clear = security.unlisted = 'clear';
-- population_filters = sides.*.table.where, compare.include, compare.exclude,
-- compare.valueFilter or canonical.scope, required only for a certified claim
-- (they narrow the compared population or fields without appearing as
-- suppressed).
CREATE FUNCTION ds.tg_runs_spec_approval()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_body      jsonb;
    v_required  text[] := '{}';
    v_approved  text[];
BEGIN
    SELECT s.body INTO v_body FROM ds.specs AS s WHERE s.spec_id = NEW.spec_id;
    IF v_body IS NULL THEN
        RETURN NEW;                       -- runs_spec_fk reports a missing spec
    END IF;
    IF jsonb_path_exists(v_body, '$.security.clear[*]') THEN
        v_required := array_append(v_required, 'clear');
    END IF;
    IF jsonb_path_exists(v_body, '$.sides.*.rawClear[*]') THEN
        v_required := array_append(v_required, 'rawClear');
    END IF;
    IF v_body #>> '{security,unlisted}' = 'clear' THEN
        v_required := array_append(v_required, 'unlisted_clear');
    END IF;
    IF NEW.claim_level = 'certified'
       AND (jsonb_path_exists(v_body, '$.sides.*.table.where')
            OR jsonb_path_exists(v_body, '$.compare.include[*]')
            OR jsonb_path_exists(v_body, '$.compare.exclude[*]')
            OR jsonb_path_exists(v_body, '$.compare.valueFilter')
            OR jsonb_path_exists(v_body, '$.canonical.scope')) THEN
        v_required := array_append(v_required, 'population_filters');
    END IF;
    IF cardinality(v_required) = 0 THEN
        RETURN NEW;
    END IF;
    SELECT a.scope INTO v_approved FROM ds.spec_approvals AS a WHERE a.spec_id = NEW.spec_id;
    IF v_approved IS NULL OR NOT (v_required <@ v_approved) THEN
        RAISE EXCEPTION 'ds.runs: spec % needs a four-eyes approval of scope % (approved: %)',
                NEW.spec_id, v_required, coalesce(v_approved, '{}'::text[])
            USING ERRCODE = 'check_violation',
                  HINT = 'A rule_approver who authored no version of the spec inserts ds.spec_approvals.';
    END IF;
    RETURN NEW;
END
$fn$;
COMMENT ON FUNCTION ds.tg_runs_spec_approval() IS
    'BEFORE INSERT/UPDATE OF spec_id, claim_level row trigger on ds.runs: rejects a run whose spec uses security.clear, sides.*.rawClear or security.unlisted = clear (and, for a certified claim, population filters) without a ds.spec_approvals row covering those scopes.';

-- A run calls only targets owned by its dataset (targets.dataset_id) or
-- granted to it (ds.dataset_targets). A missing target or dataset is left to
-- the NOT NULL constraints and the foreign keys (runs_target_a_fk / _b_fk).
CREATE FUNCTION ds.tg_runs_dataset_targets()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_target text;
    v_owner  text;
BEGIN
    IF NEW.dataset_id IS NULL THEN
        RETURN NEW;
    END IF;
    FOREACH v_target IN ARRAY ARRAY[NEW.target_a_id, NEW.target_b_id] LOOP
        SELECT t.dataset_id INTO v_owner FROM ds.targets AS t WHERE t.target_id = v_target;
        IF FOUND AND v_owner IS DISTINCT FROM NEW.dataset_id
           AND NOT EXISTS (SELECT 1 FROM ds.dataset_targets AS g
                            WHERE g.dataset_id = NEW.dataset_id AND g.target_id = v_target) THEN
            RAISE EXCEPTION 'ds.runs: target % is not granted to dataset %', v_target, NEW.dataset_id
                USING ERRCODE = 'check_violation',
                      HINT = 'An admin grants it in ds.dataset_targets; a target is always usable by its own dataset.';
        END IF;
    END LOOP;
    RETURN NEW;
END
$fn$;
COMMENT ON FUNCTION ds.tg_runs_dataset_targets() IS
    'BEFORE INSERT/UPDATE OF dataset_id, target_a_id, target_b_id row trigger on ds.runs: both targets are owned by the run''s dataset or granted to it in ds.dataset_targets.';

-- An approved known-difference rule is immutable. The only changes
-- allowed after approval are activation (while never deactivated) and a final
-- deactivation that records deactivated_by / deactivated_at. Reclassifying
-- means a new rule_id with a new approval. An approved rule is never deleted.
CREATE FUNCTION ds.tg_known_diffs_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF OLD.approved_at IS NULL THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'ds.known_diffs: approved rule % cannot be deleted; deactivate it instead', OLD.rule_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['active', 'deactivated_by', 'deactivated_at', 'updated_at'])
       IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['active', 'deactivated_by', 'deactivated_at', 'updated_at']) THEN
        RAISE EXCEPTION 'ds.known_diffs: rule % is approved and immutable', OLD.rule_id
            USING ERRCODE = 'restrict_violation',
                  HINT = 'A change (classification, when_cel, path_pattern, taxonomy_code, ...) is a new rule_id with a new approval.';
    END IF;
    IF OLD.deactivated_at IS NOT NULL
       AND (NEW.active
            OR NEW.deactivated_by IS DISTINCT FROM OLD.deactivated_by
            OR NEW.deactivated_at IS DISTINCT FROM OLD.deactivated_at) THEN
        RAISE EXCEPTION 'ds.known_diffs: rule % is deactivated; deactivation is final', OLD.rule_id
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.active AND NOT NEW.active AND NEW.deactivated_at IS NULL THEN
        RAISE EXCEPTION 'ds.known_diffs: deactivating rule % must record deactivated_by and deactivated_at', OLD.rule_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$fn$;
COMMENT ON FUNCTION ds.tg_known_diffs_immutable() IS
    'BEFORE UPDATE OR DELETE row trigger on ds.known_diffs: once approved_at is set, only activation and a final, recorded deactivation are allowed, and the rule cannot be deleted.';

-- An approved SQL template or inferred schema is immutable, except for
-- status approved -> retired. A change is a new version.
CREATE FUNCTION ds.tg_approved_row_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF OLD.approved_at IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'retired'
       AND (to_jsonb(NEW) - 'status') = (to_jsonb(OLD) - 'status') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'ds.%: an approved row is immutable except status approved -> retired', TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation',
              HINT = 'Insert a new version and have it approved.';
END
$fn$;
COMMENT ON FUNCTION ds.tg_approved_row_immutable() IS
    'BEFORE UPDATE row trigger on ds.sql_templates and ds.inferred_schemas: once approved_at is set, the only allowed change is status approved -> retired.';

-- A profile's trust is derived from the host of its base_url through the
-- admin-maintained ds.llm_hosts registry. A host that is not registered is
-- external (fail-closed), so the llm_profiles_external_floor CHECK applies.
CREATE FUNCTION ds.tg_llm_profiles_derive_trust()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_host  text := lower(substring(NEW.base_url FROM '^[A-Za-z][A-Za-z0-9+.-]*://(?:[^/?#@]*@)?([^/?#:]+)'));
    v_trust text;
BEGIN
    SELECT h.trust INTO v_trust FROM ds.llm_hosts AS h WHERE h.host = v_host;
    NEW.trust := coalesce(v_trust, 'external');
    RETURN NEW;
END
$fn$;
COMMENT ON FUNCTION ds.tg_llm_profiles_derive_trust() IS
    'BEFORE INSERT/UPDATE OF base_url, trust row trigger on ds.llm_profiles: sets trust from ds.llm_hosts for the base_url host; an unregistered host is external.';

-- =============================================================================
-- 5. Registry: datasets, secrets_ref, targets, llm_profiles, specs,
--    taxonomy_codes
-- =============================================================================
-- Dataset scope: every target, spec and run belongs to one dataset, and
-- evidence, findings and exports inherit the dataset of their run. In this
-- design ds-api enforces the scope on every request (a user sees only the
-- datasets granted to them) and ds-mcp filters every curated view by the
-- requesting user's datasets, so the agent never sees more than the user.
-- Row-level security on these tables is not built.
CREATE TABLE ds.datasets (
    dataset_id    text         NOT NULL,
    display_name  text         NOT NULL,
    description   text,
    owner         text         NOT NULL,
    active        boolean      NOT NULL DEFAULT true,
    created_by    text         NOT NULL,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    updated_at    timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT datasets_pk PRIMARY KEY (dataset_id),
    CONSTRAINT datasets_id_format CHECK (dataset_id ~ '^[a-z0-9][a-z0-9_.-]{1,63}$'),
    CONSTRAINT datasets_description_len CHECK (description IS NULL OR length(description) <= 1000)
);
COMMENT ON TABLE ds.datasets IS
    'ABAC scope. targets, specs and runs reference dataset_id (NOT NULL, default ''default''); observations, results, findings, reports and exports inherit it from their run. In this design ds-api enforces the scope and ds-mcp filters the curated views by the caller''s datasets. RLS is not built. Datasets are deactivated, never deleted.';

-- The single-tenant MVP starts with one dataset; it is the column default.
INSERT INTO ds.datasets (dataset_id, display_name, description, owner, created_by)
VALUES ('default', 'Default dataset',
        'Seeded by control-plane.sql. Column default of targets, specs and runs.dataset_id.',
        'platform', 'control-plane.sql');

CREATE TABLE ds.secrets_ref (
    ref_id           text        NOT NULL,
    provider         text        NOT NULL,
    locator          text        NOT NULL,
    secret_kind      text        NOT NULL,
    description      text,
    owner            text        NOT NULL,
    rotation_period  interval,
    last_rotated_at  timestamptz,
    active           boolean     NOT NULL DEFAULT true,
    created_by       text        NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT secrets_ref_pk PRIMARY KEY (ref_id),
    CONSTRAINT secrets_ref_id_format CHECK (ref_id ~ '^[a-z0-9][a-z0-9_.-]{1,63}$'),
    -- DDL-local vocabularies
    CONSTRAINT secrets_ref_provider_chk CHECK (provider IN (
        'vault', 'k8s_secret', 'aws_secrets_manager', 'gcp_secret_manager', 'env')),
    CONSTRAINT secrets_ref_kind_chk CHECK (secret_kind IN (
        'bearer_token', 'api_key', 'basic_auth', 'mtls_client_cert', 'oauth_client', 'db_credentials')),
    -- A locator is an address, never a value: scheme://path[#field], with no
    -- userinfo ("@") and no "password=" / "token:" style inline material.
    CONSTRAINT secrets_ref_locator_format CHECK (
        locator ~ '^(vault|k8s|awssm|gcpsm|env)://[^[:space:]@]+$'),
    CONSTRAINT secrets_ref_locator_no_inline_secret CHECK (
        locator !~* '(passw(or)?d|pwd|secret|token|api[_-]?key)[[:space:]]*[=:]'),
    CONSTRAINT secrets_ref_rotation_chk CHECK (rotation_period IS NULL OR rotation_period > interval '0')
);
COMMENT ON TABLE ds.secrets_ref IS
    'Where a credential lives, never the credential. Referenced only by targets.auth_ref. The calling service resolves the locator at runtime and never persists the value.';
COMMENT ON COLUMN ds.secrets_ref.locator IS
    'Provider address, e.g. vault://kv/data/deltasleuth/legacy-linker#token or k8s://deltasleuth/legacy-linker-auth#token. No secret material.';

CREATE TABLE ds.targets (
    target_id           text         NOT NULL,
    dataset_id          text         NOT NULL DEFAULT 'default',
    display_name        text         NOT NULL,
    kind                text         NOT NULL,
    system_name         text         NOT NULL,
    environment         text         NOT NULL,
    endpoint            text         NOT NULL,
    auth_ref            text,
    rate_limit_rps      numeric(8,2) NOT NULL DEFAULT 20,
    max_concurrency     integer      NOT NULL DEFAULT 8,
    timeouts            jsonb        NOT NULL DEFAULT '{"connect": "2s", "read": "10s", "total": "15s"}',
    circuit_breaker     jsonb        NOT NULL DEFAULT '{}',
    capabilities        jsonb        NOT NULL DEFAULT '{}',
    consistency_profile jsonb        NOT NULL DEFAULT '{}',
    tls_verify          boolean      NOT NULL DEFAULT true,
    allow_live_compare  boolean      NOT NULL DEFAULT true,
    active              boolean      NOT NULL DEFAULT true,
    owner               text         NOT NULL,
    created_by          text         NOT NULL,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    updated_at          timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT targets_pk PRIMARY KEY (target_id),
    CONSTRAINT targets_dataset_fk FOREIGN KEY (dataset_id) REFERENCES ds.datasets (dataset_id),
    CONSTRAINT targets_auth_ref_fk FOREIGN KEY (auth_ref) REFERENCES ds.secrets_ref (ref_id),
    CONSTRAINT targets_id_format CHECK (target_id ~ '^[a-z0-9][a-z0-9_.-]{1,63}$'),
    CONSTRAINT targets_kind_chk CHECK (kind IN ('service', 'table')),                          -- DDL-local
    CONSTRAINT targets_environment_chk CHECK (environment IN ('dev', 'test', 'staging', 'prod')), -- DDL-local
    CONSTRAINT targets_endpoint_format CHECK (endpoint ~ '^[a-z][a-z0-9+.-]*://[^[:space:]]+$'),
    -- no credentials in the endpoint: no userinfo, no secret-bearing query parameters
    CONSTRAINT targets_endpoint_no_userinfo CHECK (endpoint !~ '^[a-z][a-z0-9+.-]*://[^/?#]*@'),
    CONSTRAINT targets_endpoint_no_secret_params CHECK (
        endpoint !~* '[?&;](password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|sslpassword)='),
    CONSTRAINT targets_rate_limit_chk CHECK (rate_limit_rps > 0),
    CONSTRAINT targets_max_concurrency_chk CHECK (max_concurrency BETWEEN 1 AND 1024),
    CONSTRAINT targets_json_objects CHECK (
        jsonb_typeof(timeouts) = 'object'
        AND jsonb_typeof(circuit_breaker) = 'object'
        AND jsonb_typeof(capabilities) = 'object'),
    -- the connector capability contract (architecture.md §4.12). Only
    -- these keys are allowed, so a typo (e.g. in truncation_indicators) cannot
    -- silently disable a completeness check. link_provenance is the optional
    -- domain-pack declaration.
    CONSTRAINT targets_capabilities_keys CHECK (
        CASE WHEN jsonb_typeof(capabilities) = 'object'
             THEN capabilities - ARRAY['pagination', 'stable_ordering', 'snapshot', 'as_of', 'watermark',
                                       'authoritative_absence', 'truncation_indicators', 'max_depth',
                                       'max_page_size', 'max_bytes', 'enumerate', 'existence_check',
                                       'link_provenance']::text[]
                  = '{}'::jsonb
             ELSE false
        END),
    -- Vocabularies and shapes. An absent capability reads as none / not declared.
    CONSTRAINT targets_capabilities_values CHECK (
        CASE WHEN jsonb_typeof(capabilities) <> 'object' THEN false ELSE
            -- pagination: array of the kinds the connector supports
            coalesce(jsonb_typeof(capabilities -> 'pagination'), 'array') = 'array'
            AND coalesce(capabilities -> 'pagination', '[]'::jsonb)
                <@ '["none", "cursor", "offset", "page", "link_header"]'::jsonb
            AND coalesce(jsonb_typeof(capabilities -> 'stable_ordering'), 'boolean') = 'boolean'
            AND coalesce(jsonb_typeof(capabilities -> 'link_provenance'), 'boolean') = 'boolean'
            AND coalesce(capabilities ->> 'snapshot', 'none')  IN ('none', 'per_request', 'shared')
            AND coalesce(capabilities ->> 'as_of', 'none')     IN ('none', 'per_request', 'shared')
            AND coalesce(capabilities ->> 'watermark', 'none') IN ('none', 'per_request', 'shared')
            AND coalesce(capabilities ->> 'enumerate', 'none') IN ('none', 'full', 'partitioned')
            -- max_depth, max_page_size, max_bytes: positive integers
            AND NOT jsonb_path_exists(capabilities,
                    'strict $.keyvalue() ? (@.key like_regex "^max_" && (@.value.type() != "number" || @.value < 1 || @.value != @.value.floor()))')
            -- authoritative_absence {status: [404 | 410], shapes: [shape names]}
            AND CASE jsonb_typeof(capabilities -> 'authoritative_absence')
                    WHEN 'object' THEN
                        (capabilities -> 'authoritative_absence') - ARRAY['status', 'shapes']::text[] = '{}'::jsonb
                        AND coalesce(jsonb_typeof(capabilities #> '{authoritative_absence,status}'), 'array') = 'array'
                        AND coalesce(capabilities #> '{authoritative_absence,status}', '[]'::jsonb) <@ '[404, 410]'::jsonb
                        AND coalesce(jsonb_typeof(capabilities #> '{authoritative_absence,shapes}'), 'array') = 'array'
                        AND NOT jsonb_path_exists(capabilities,
                                '$.authoritative_absence.shapes[*] ? (@.type() != "string" || !(@ like_regex "^[a-z][a-z0-9_]{0,63}$"))')
                    ELSE capabilities -> 'authoritative_absence' IS NULL
                END
            -- truncation_indicators {countPath?, itemsPath?, cap}: the default of a spec side's truncation
            AND CASE jsonb_typeof(capabilities -> 'truncation_indicators')
                    WHEN 'object' THEN
                        (capabilities -> 'truncation_indicators') - ARRAY['countPath', 'itemsPath', 'cap']::text[] = '{}'::jsonb
                        AND ((capabilities -> 'truncation_indicators') ? 'countPath'
                             OR (capabilities -> 'truncation_indicators') ? 'itemsPath')
                        AND coalesce(jsonb_typeof(capabilities #> '{truncation_indicators,countPath}'), 'string') = 'string'
                        AND coalesce(capabilities #>> '{truncation_indicators,countPath}', '$') LIKE '$%'
                        AND coalesce(jsonb_typeof(capabilities #> '{truncation_indicators,itemsPath}'), 'string') = 'string'
                        AND coalesce(capabilities #>> '{truncation_indicators,itemsPath}', '$') LIKE '$%'
                        AND coalesce(jsonb_typeof(capabilities #> '{truncation_indicators,cap}'), '') = 'number'
                        AND jsonb_path_exists(capabilities, '$.truncation_indicators.cap ? (@ >= 1 && @ == @.floor())')
                    ELSE capabilities -> 'truncation_indicators' IS NULL
                END
            -- existence_check {probes: [probe names]}: observations.probe values other than primary
            AND CASE jsonb_typeof(capabilities -> 'existence_check')
                    WHEN 'object' THEN
                        (capabilities -> 'existence_check') - 'probes' = '{}'::jsonb
                        AND coalesce(jsonb_typeof(capabilities #> '{existence_check,probes}'), '') = 'array'
                        AND coalesce(capabilities #> '{existence_check,probes}', '[]'::jsonb) <> '[]'::jsonb
                        AND NOT jsonb_path_exists(capabilities,
                                '$.existence_check.probes[*] ? (@.type() != "string" || @ == "primary" || !(@ like_regex "^[a-z][a-z0-9_]{0,31}$"))')
                    ELSE capabilities -> 'existence_check' IS NULL
                END
        END),
    -- only the six declared flags. An unknown key is rejected, so a typo
    -- cannot pass silently as "undeclared".
    CONSTRAINT targets_consistency_profile_keys CHECK (
        jsonb_typeof(consistency_profile) = 'object'
        AND consistency_profile - ARRAY['write_atomicity', 'read_isolation', 'secondary_indexes',
                                        'consistent_cut', 'bulk_readers', 'change_feed']::text[]
            = '{}'::jsonb),
    -- Vocabularies. An absent flag reads as unknown / none.
    CONSTRAINT targets_consistency_profile_values CHECK (
        coalesce(consistency_profile ->> 'write_atomicity', 'unknown')
            IN ('atomic_multi_row', 'per_row', 'unknown')
        AND coalesce(consistency_profile ->> 'read_isolation', 'unknown')
            IN ('snapshot', 'statement', 'none', 'unknown')
        AND coalesce(consistency_profile ->> 'consistent_cut', 'none')
            IN ('pitr_cluster', 'pitr_multi_cluster', 'storage_snapshot', 'quiesce', 'none')
        AND coalesce(jsonb_typeof(consistency_profile -> 'secondary_indexes'), 'array') = 'array'
        AND coalesce(jsonb_typeof(consistency_profile -> 'bulk_readers'), 'array') = 'array'
        AND coalesce(consistency_profile -> 'bulk_readers', '[]'::jsonb)
            <@ '["oltp_key_batches", "scan_job_local", "scan_job_cluster"]'::jsonb
        AND coalesce(jsonb_typeof(consistency_profile -> 'change_feed'), 'object') = 'object'
        AND coalesce(consistency_profile #>> '{change_feed,kind}', 'none')
            IN ('ingest_journal', 'engine_tx_log', 'db_trigger_log', 'debezium_rows', 'sql_watermark', 'none')
        AND coalesce(consistency_profile #>> '{change_feed,completeness}', 'none')
            IN ('exact', 'superset', 'none'))
);
COMMENT ON TABLE ds.targets IS
    'Target registry. Spec sides reference target_id. Service targets are HTTP endpoints; table targets are databases or exports (host/port/db only, no credentials).';
COMMENT ON COLUMN ds.targets.dataset_id IS
    'ABAC scope of this registry entry. ds-api enforces it; RLS is not built. A run''s dataset is its spec''s dataset (runs_spec_dataset_fk). A target is usable by its own dataset and by the datasets it is granted to in ds.dataset_targets: trigger runs_dataset_targets checks both run targets, and the spec loader checks every target a spec references.';
COMMENT ON COLUMN ds.targets.auth_ref IS
    'Reference to ds.secrets_ref.ref_id. Only a reference; no secret value is stored anywhere in ds.';
COMMENT ON COLUMN ds.targets.rate_limit_rps IS
    'Per-target rate budget enforced by ds-fetcher (architecture.md default: 20 req/s per target).';
COMMENT ON COLUMN ds.targets.capabilities IS
    'Connector capability contract (CHECKs targets_capabilities_keys / _values): pagination [none | cursor | offset | page | link_header], stable_ordering (boolean), snapshot / as_of / watermark (none | per_request | shared), authoritative_absence {status: [404 | 410], shapes: [names]} (without it a 404 is INCOMPLETE(absence_unconfirmed)), truncation_indicators {countPath, itemsPath, cap} (the default of a spec side''s truncation; a spec with neither is rejected by the loader), max_depth / max_page_size / max_bytes (positive integers), enumerate (none | full | partitioned), existence_check {probes: [names]} (the core calls connector.existence_check(key) only when declared). Offset or page pagination without stable_ordering or a per-request snapshot makes a side INCOMPLETE(unstable_pagination). Link provenance is never assumed; a domain pack may declare {"link_provenance": true}.';
COMMENT ON COLUMN ds.targets.consistency_profile IS
    'Consistency profile: write_atomicity (atomic_multi_row | per_row | unknown), read_isolation (snapshot | statement | none | unknown), secondary_indexes [{name, kind, async}], consistent_cut (pitr_cluster | pitr_multi_cluster | storage_snapshot | quiesce | none), bulk_readers [oltp_key_batches | scan_job_local | scan_job_cluster], change_feed {kind: ingest_journal | engine_tx_log | db_trigger_log | debezium_rows | sql_watermark | none, completeness: exact | superset | none}. ds-scheduler chooses the certification mode from these flags alone; {} means unknown (observed claims only).';
COMMENT ON COLUMN ds.targets.allow_live_compare IS
    'Whether compare_key (the only MCP tool that calls targets) may call this target.';

-- Targets granted to a dataset besides its own (targets.dataset_id).
-- Admin-managed through ds-api, and every grant or revoke is audited
-- (dataset_target.grant / dataset_target.revoke). Trigger runs_dataset_targets
-- checks both run targets. The spec loader (POST /v1/specs, /v1/runs,
-- /v1/compare) checks every target a spec references (sides.*, every
-- key.source member including union members and paginated_endpoint targets,
-- identity.crosswalk.target) and requires every referenced sql_templates and
-- inferred_schemas row to carry the spec's dataset_id.
CREATE TABLE ds.dataset_targets (
    dataset_id  text         NOT NULL,
    target_id   text         NOT NULL,
    granted_by  text         NOT NULL,
    granted_at  timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT dataset_targets_pk PRIMARY KEY (dataset_id, target_id),
    CONSTRAINT dataset_targets_dataset_fk FOREIGN KEY (dataset_id) REFERENCES ds.datasets (dataset_id),
    CONSTRAINT dataset_targets_target_fk FOREIGN KEY (target_id) REFERENCES ds.targets (target_id)
);
COMMENT ON TABLE ds.dataset_targets IS
    'Admin-managed, audited grants of a target to a dataset other than its owner. A run, and every target a spec references, must be owned by or granted to the run''s (spec''s) dataset, so a spec author in dataset X cannot label dataset Y''s data as X.';

-- LLM host trust registry, maintained by admins. A profile's trust is
-- derived from the host of its base_url (trigger llm_profiles_derive_trust);
-- an unregistered host is external. ds_app may add hosts but not change or
-- remove one: changing a registered host's trust is a reviewed migration that
-- re-saves the profiles on that host, so they are re-derived.
CREATE TABLE ds.llm_hosts (
    host      text         NOT NULL,
    trust     text         NOT NULL,
    note      text,
    added_by  text         NOT NULL,
    added_at  timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT llm_hosts_pk PRIMARY KEY (host),
    -- a lower-case DNS name or IPv4 address, without scheme, port or path
    CONSTRAINT llm_hosts_host_format CHECK (
        host ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
        AND octet_length(host) <= 253),
    CONSTRAINT llm_hosts_trust_chk CHECK (trust IN ('internal', 'external')),
    CONSTRAINT llm_hosts_note_len CHECK (note IS NULL OR length(note) <= 1000)
);
COMMENT ON TABLE ds.llm_hosts IS
    'Admin-maintained registry of LLM endpoint hosts and their trust (internal | external). llm_profiles.trust is derived from it by trigger; an unregistered host is external, so the external floor applies (fail-closed). The egress NetworkPolicy is generated from the hosts of the active profiles.';

-- LLM provider profiles. Specs and agents reference profile_id only,
-- the same pattern as the target registry. Credentials by reference only
-- (auth_ref -> ds.secrets_ref); the key itself never reaches the database.
-- The egress policy is enforced in ds_llm before any network call; the
-- CHECKs below are its fail-closed floor: an external profile can only be
-- enabled for dev/test/eval and synthetic/golden data. Widening that floor
-- is a reviewed migration, not a row update. trust is derived from
-- ds.llm_hosts by trigger, never taken from the profile author.
CREATE TABLE ds.llm_profiles (
    profile_id              text           NOT NULL,
    dialect                 text           NOT NULL,
    base_url                text           NOT NULL,
    auth_ref                text,
    auth_header             text           NOT NULL DEFAULT 'Authorization',
    auth_scheme             text,
    model                   text           NOT NULL,
    params                  jsonb          NOT NULL DEFAULT '{}',
    trust                   text           NOT NULL,
    allowed_data_classes    text[]         NOT NULL DEFAULT '{}',
    environments            text[]         NOT NULL DEFAULT '{}',
    capabilities            jsonb          NOT NULL DEFAULT '{}',
    limits                  jsonb          NOT NULL DEFAULT '{}',
    budgets                 jsonb          NOT NULL DEFAULT '{"max_tool_calls": 8, "max_input_tokens_per_call": 20000, "max_wall_s": 60}',
    pricing                 jsonb          NOT NULL DEFAULT '{}',
    retention               jsonb          NOT NULL DEFAULT '{"provider_store": false}',
    fallbacks               text[]         NOT NULL DEFAULT '{}',
    capability_report       jsonb,
    capability_report_hash  ds.sha256_hex,
    status                  text           NOT NULL DEFAULT 'draft',
    approved_by             text,
    approved_at             timestamptz,
    owner                   text           NOT NULL,
    created_by              text           NOT NULL,
    created_at              timestamptz    NOT NULL DEFAULT now(),
    updated_at              timestamptz    NOT NULL DEFAULT now(),
    CONSTRAINT llm_profiles_pk PRIMARY KEY (profile_id),
    CONSTRAINT llm_profiles_auth_ref_fk FOREIGN KEY (auth_ref) REFERENCES ds.secrets_ref (ref_id),
    CONSTRAINT llm_profiles_id_format CHECK (profile_id ~ '^[a-z0-9][a-z0-9_.-]{1,63}$'),
    -- DDL-local vocabularies (adapters, trust, status)
    CONSTRAINT llm_profiles_dialect_chk CHECK (dialect IN (
        'openai_chat', 'anthropic_messages', 'openai_responses', 'custom_http')),
    CONSTRAINT llm_profiles_trust_chk CHECK (trust IN ('internal', 'external')),
    CONSTRAINT llm_profiles_status_chk CHECK (status IN ('draft', 'active', 'disabled')),
    -- same credential rules as targets.endpoint: no userinfo, no secret-bearing query parameters
    CONSTRAINT llm_profiles_base_url_format CHECK (base_url ~ '^https?://[^[:space:]]+$'),
    CONSTRAINT llm_profiles_base_url_no_userinfo CHECK (base_url !~ '^https?://[^/?#]*@'),
    CONSTRAINT llm_profiles_base_url_no_secret_params CHECK (
        base_url !~* '[?&;](password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|key)='),
    -- a credential is never sent in cleartext
    CONSTRAINT llm_profiles_auth_needs_https CHECK (auth_ref IS NULL OR base_url ~ '^https://'),
    CONSTRAINT llm_profiles_auth_header_format CHECK (auth_header ~ '^[A-Za-z0-9-]{1,64}$'),
    CONSTRAINT llm_profiles_auth_scheme_format CHECK (auth_scheme IS NULL OR auth_scheme ~ '^[A-Za-z0-9._-]{1,32}$'),
    CONSTRAINT llm_profiles_model_len CHECK (length(model) BETWEEN 1 AND 200),
    -- data classes: lower_snake names, no NULL or empty elements
    CONSTRAINT llm_profiles_data_classes_format CHECK (
        array_position(allowed_data_classes, NULL) IS NULL
        AND NOT ('' = ANY (allowed_data_classes))
        AND array_to_string(allowed_data_classes, ',') ~ '^([a-z][a-z0-9_]{0,63}(,|$))*$'),
    CONSTRAINT llm_profiles_environments_chk CHECK (
        array_position(environments, NULL) IS NULL
        AND environments <@ ARRAY['dev', 'test', 'eval', 'staging', 'prod']::text[]),
    -- floor: external profiles only in dev/test/eval, only for synthetic or
    -- golden data; golden means synthetic-derived with a provenance
    -- attestation, never real (even redacted) samples
    CONSTRAINT llm_profiles_external_floor CHECK (
        trust <> 'external'
        OR (environments <@ ARRAY['dev', 'test', 'eval']::text[]
            AND allowed_data_classes <@ ARRAY['synthetic', 'golden']::text[])),
    CONSTRAINT llm_profiles_json_objects CHECK (
        jsonb_typeof(params) = 'object' AND jsonb_typeof(capabilities) = 'object'
        AND jsonb_typeof(limits) = 'object' AND jsonb_typeof(pricing) = 'object'
        AND jsonb_typeof(retention) = 'object' AND jsonb_typeof(budgets) = 'object'
        AND (capability_report IS NULL OR jsonb_typeof(capability_report) = 'object')),
    CONSTRAINT llm_profiles_capabilities_keys CHECK (
        capabilities - ARRAY['tool_calling', 'strict_tools', 'structured_output', 'parallel_tools',
                             'max_context', 'max_output', 'prompt_caching',
                             'forced_tool_choice', 'json_object', 'sampling_params']::text[] = '{}'::jsonb
        AND coalesce(capabilities ->> 'prompt_caching', 'none') IN ('none', 'automatic', 'explicit')
        AND coalesce(capabilities ->> 'sampling_params', 'full') IN ('none', 'temperature', 'full')),
    CONSTRAINT llm_profiles_limits_keys CHECK (
        limits - ARRAY['rps', 'tpm', 'timeout_s', 'max_concurrency']::text[] = '{}'::jsonb),
    -- per-call and per-turn budgets, plus per-user/day and per-run
    -- token caps; every value is a positive number
    CONSTRAINT llm_profiles_budgets_keys CHECK (
        CASE WHEN jsonb_typeof(budgets) = 'object'
             THEN budgets - ARRAY['max_tool_calls', 'max_input_tokens_per_call', 'max_input_tokens_per_turn',
                                  'max_wall_s', 'per_user_day_tokens', 'per_run_tokens']::text[] = '{}'::jsonb
                  AND NOT jsonb_path_exists(budgets, 'strict $.* ? (@.type() != "number" || @ <= 0)')
             ELSE false
        END),
    CONSTRAINT llm_profiles_pricing_keys CHECK (
        pricing - ARRAY['version', 'currency', 'input_per_mtok', 'output_per_mtok',
                        'cache_read_mult', 'cache_write_mult', 'cache_write_1h_mult',
                        'effective_from']::text[] = '{}'::jsonb
        AND (pricing = '{}'::jsonb
             OR (pricing ? 'version' AND coalesce(pricing ->> 'currency', '') ~ '^[A-Z]{3}$'))),
    -- the provider never stores requests (OpenAI store: false)
    CONSTRAINT llm_profiles_retention_keys CHECK (
        retention - ARRAY['provider_store', 'zdr', 'notes']::text[] = '{}'::jsonb
        AND coalesce(retention -> 'provider_store', 'false'::jsonb) = 'false'::jsonb),
    CONSTRAINT llm_profiles_fallbacks_chk CHECK (
        array_position(fallbacks, NULL) IS NULL AND NOT (profile_id = ANY (fallbacks))),
    CONSTRAINT llm_profiles_capability_report_pair CHECK (
        (capability_report IS NULL) = (capability_report_hash IS NULL)),
    CONSTRAINT llm_profiles_approval_pair CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
    CONSTRAINT llm_profiles_active_requires_approval CHECK (status <> 'active' OR approved_at IS NOT NULL),
    CONSTRAINT llm_profiles_four_eyes CHECK (approved_by IS NULL OR approved_by <> created_by)
);
COMMENT ON TABLE ds.llm_profiles IS
    'LLM provider profiles: dialect, base_url, auth (auth_ref/auth_header/auth_scheme; secrets by reference only), model, trust, allowed_data_classes, environments, capabilities, limits, pricing, retention, fallbacks. Effective capabilities = declared (capabilities) intersected with probed (capability_report). Activation needs approval by someone other than the author. Not readable by ds_agent_ro; pricing is never exposed to the agent.';
COMMENT ON COLUMN ds.llm_profiles.dialect IS
    'Wire adapter: openai_chat (an OpenAI-compatible gateway, vLLM, OpenAI tests), anthropic_messages (Claude tests; never through Anthropic''s OpenAI-compat endpoint), openai_responses (P3), custom_http.';
COMMENT ON COLUMN ds.llm_profiles.auth_ref IS
    'Reference to ds.secrets_ref.ref_id (NULL = no credential, e.g. a local vLLM). ds_llm resolves it and passes api_key, base_url, timeout and max_retries=0 explicitly; it never reads OPENAI_* / ANTHROPIC_* variables.';
COMMENT ON COLUMN ds.llm_profiles.trust IS
    'internal | external, derived by trigger llm_profiles_derive_trust from the base_url host in ds.llm_hosts (an unregistered host is external); a value supplied by the author is ignored. Failover only to profiles with trust >= the primary''s and allowed_data_classes containing the request''s classes, so internal -> external never happens automatically.';
COMMENT ON COLUMN ds.llm_profiles.allowed_data_classes IS
    'Data classes a request may carry to this profile (e.g. aggregate, tokenised_payload, synthetic, golden). golden = the eval golden set, synthetic-derived with a provenance attestation. A call proceeds only if request classes are a subset and the environment is listed; otherwise PolicyDenied.';
COMMENT ON COLUMN ds.llm_profiles.capabilities IS
    'Declared capabilities {tool_calling, strict_tools, structured_output, parallel_tools, max_context, max_output, prompt_caching: none | automatic | explicit, forced_tool_choice, json_object, sampling_params: none | temperature | full}. The adapter drops sampling parameters the model does not support; where temperature is unsupported, reproducibility is pinned by (model_reported, prompt_version, effort).';
COMMENT ON COLUMN ds.llm_profiles.budgets IS
    'Budgets, checked before each call: max_tool_calls (per turn), max_input_tokens_per_call (default 20000; a 9-call turn ends near 17.5k), max_input_tokens_per_turn (absent: bounded by (max_tool_calls + 1) x max_input_tokens_per_call), max_wall_s (per turn), per_user_day_tokens, per_run_tokens. Positive numbers only.';
COMMENT ON COLUMN ds.llm_profiles.pricing IS
    'Current price version {version, currency, input_per_mtok, output_per_mtok, cache_read_mult, cache_write_mult (5-minute cache writes), cache_write_1h_mult (1-hour cache writes), effective_from}. llm_calls.price_version records the version that priced each call. Never exposed to ds_agent_ro.';
COMMENT ON COLUMN ds.llm_profiles.capability_report IS
    'Latest conformance-probe report {endpoint, model_reported, adapter_version, results}; capability_report_hash is its sha256, part of the eval promotion tuple.';

-- Eval-gated promotion of an LLM tuple per environment. The unit is
-- (profile_id, model_reported, prompt_version, adapter_version,
-- capability_report_hash), evaluated k times on the synthetic golden set. The
-- cheapest passing tuple is promoted with four-eyes approval; at most one
-- tuple per (profile_id, environment) is promoted at a time. Rows are
-- evidence: ds_app may only change status and the approval columns.
CREATE TABLE ds.llm_promotions (
    promotion_id             bigint         GENERATED ALWAYS AS IDENTITY,
    profile_id               text           NOT NULL,
    environment              text           NOT NULL,
    model_reported           text           NOT NULL,
    prompt_version           text           NOT NULL,
    adapter_version          text           NOT NULL,
    capability_report_hash   ds.sha256_hex  NOT NULL,
    k                        integer        NOT NULL DEFAULT 3,
    passed                   boolean        NOT NULL,
    cost_per_correct_micros  bigint,
    currency                 text,
    eval_ref                 text           NOT NULL,
    status                   text           NOT NULL DEFAULT 'proposed',
    created_by               text           NOT NULL,
    created_at               timestamptz    NOT NULL DEFAULT now(),
    approved_by              text,
    approved_at              timestamptz,
    CONSTRAINT llm_promotions_pk PRIMARY KEY (promotion_id),
    CONSTRAINT llm_promotions_profile_fk FOREIGN KEY (profile_id) REFERENCES ds.llm_profiles (profile_id),
    CONSTRAINT llm_promotions_environment_chk CHECK (environment IN ('dev', 'test', 'eval', 'staging', 'prod')),
    -- DDL-local vocabulary
    CONSTRAINT llm_promotions_status_chk CHECK (status IN ('proposed', 'promoted', 'rejected', 'superseded')),
    CONSTRAINT llm_promotions_k_chk CHECK (k >= 1),
    CONSTRAINT llm_promotions_text_lengths CHECK (
        length(model_reported) BETWEEN 1 AND 200 AND length(prompt_version) BETWEEN 1 AND 64
        AND length(adapter_version) BETWEEN 1 AND 64 AND length(eval_ref) BETWEEN 1 AND 1024),
    CONSTRAINT llm_promotions_cost_priced CHECK (
        cost_per_correct_micros IS NULL
        OR (cost_per_correct_micros >= 0 AND currency IS NOT NULL AND currency ~ '^[A-Z]{3}$')),
    CONSTRAINT llm_promotions_approval_pair CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
    -- only a passing, approved tuple is promoted
    CONSTRAINT llm_promotions_promoted_needs_approval CHECK (status <> 'promoted' OR (approved_at IS NOT NULL AND passed)),
    CONSTRAINT llm_promotions_four_eyes CHECK (approved_by IS NULL OR approved_by <> created_by)
);
COMMENT ON TABLE ds.llm_promotions IS
    'Eval results and promotions of (profile_id, model_reported, prompt_version, adapter_version, capability_report_hash) per environment: k repeats, passed, cost_per_correct_micros in currency, eval_ref (the eval report), status proposed | promoted | rejected | superseded. Promotion needs passed = true and approval by someone other than created_by; one promoted tuple per (profile_id, environment). Not readable by ds_agent_ro.';

CREATE TABLE ds.specs (
    spec_id              bigint        GENERATED ALWAYS AS IDENTITY,
    spec_name            text          NOT NULL,
    spec_version         integer       NOT NULL,
    version_label        text          NOT NULL,
    content_hash         ds.sha256_hex NOT NULL,
    dataset_id           text          NOT NULL DEFAULT 'default',
    retention_class      text          NOT NULL DEFAULT 'standard',
    api_version          text          NOT NULL DEFAULT 'deltasleuth/v1',
    kind                 text          NOT NULL DEFAULT 'ComparisonSpec',
    owner                text          NOT NULL,
    description          text,
    domain_pack_name     text,
    domain_pack_version  text,
    normalizer_version   text          NOT NULL,
    source_yaml          text          NOT NULL,
    body                 jsonb         NOT NULL,
    created_by           text          NOT NULL,
    created_at           timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT specs_pk PRIMARY KEY (spec_id),
    CONSTRAINT specs_dataset_fk FOREIGN KEY (dataset_id) REFERENCES ds.datasets (dataset_id),
    CONSTRAINT specs_name_version_uq UNIQUE (spec_name, spec_version),
    CONSTRAINT specs_content_hash_uq UNIQUE (content_hash),
    -- target of the composite FK from runs (keeps runs.spec_name/spec_version honest)
    CONSTRAINT specs_id_name_version_uq UNIQUE (spec_id, spec_name, spec_version),
    -- target of runs_spec_dataset_fk (a run's dataset is its spec's dataset)
    CONSTRAINT specs_id_dataset_uq UNIQUE (spec_id, dataset_id),
    -- retention classes: standard = raw 90 d / findings 90 d; audit = raw 1 y / findings 1 y
    CONSTRAINT specs_retention_class_chk CHECK (retention_class IN ('standard', 'audit')),
    CONSTRAINT specs_api_version_chk CHECK (api_version = 'deltasleuth/v1'),
    CONSTRAINT specs_kind_chk CHECK (kind = 'ComparisonSpec'),
    CONSTRAINT specs_name_format CHECK (spec_name ~ '^[a-z0-9][a-z0-9_.-]{1,127}$'),
    CONSTRAINT specs_version_chk CHECK (spec_version > 0),
    CONSTRAINT specs_name_label_uq UNIQUE (spec_name, version_label),
    CONSTRAINT specs_version_label_semver CHECK (version_label ~ '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'),
    CONSTRAINT specs_normalizer_version_semver CHECK (
        normalizer_version ~ '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'),
    -- the column mirrors the body, so content_hash covers the normaliser version
    CONSTRAINT specs_normalizer_version_in_body CHECK (body ->> 'normalizerVersion' = normalizer_version),
    CONSTRAINT specs_body_object CHECK (jsonb_typeof(body) = 'object'),
    CONSTRAINT specs_source_size CHECK (octet_length(source_yaml) <= 1048576)
);
COMMENT ON TABLE ds.specs IS
    'ComparisonSpec (apiVersion deltasleuth/v1), versioned and immutable. A change is a new spec_version. UPDATE, DELETE and TRUNCATE raise.';
COMMENT ON COLUMN ds.specs.spec_version IS 'Store-assigned revision: a positive integer, monotonic per spec_name. Recorded on runs and results.';
COMMENT ON COLUMN ds.specs.version_label IS 'metadata.version from the YAML (semver, author-controlled). Unique per spec_name; maps 1:1 to spec_version.';
COMMENT ON COLUMN ds.specs.content_hash IS 'sha256 of the canonical JSON form of the parsed spec (body).';
COMMENT ON COLUMN ds.specs.body IS 'Parsed spec. Carries no credentials: auth comes from targets.auth_ref.';
COMMENT ON COLUMN ds.specs.normalizer_version IS
    'Spec normalizerVersion (semver) of the canonicaliser and normaliser chain. It equals body.normalizerVersion, so content_hash covers it and a re-diff (rediff run) is pinned bit for bit.';
COMMENT ON COLUMN ds.specs.created_by IS
    'Spec author. Noise masks of this spec_name cannot be approved by any author of any of its versions (four-eyes).';
COMMENT ON COLUMN ds.specs.dataset_id IS
    'ABAC scope. Every run of this spec belongs to the same dataset (runs_spec_dataset_fk). ds-api enforces the scope; RLS is not built.';
COMMENT ON COLUMN ds.specs.retention_class IS
    'standard = raw observations 90 d / findings 90 d; audit = raw 1 y / findings 1 y. Raw is always retained at least as long as findings, because reveal re-derives raw values from raw bodies. Expired evidence is marked in findings, exports and citations, and AI answers that need it are blocked.';

-- Four-eyes approval of a spec version's clear-text exposure and
-- population filters. One row per spec version (specs are immutable, so a new
-- version needs a new approval); rows are never changed. Trigger
-- runs_spec_approval rejects a run whose spec needs a scope this row does not
-- cover. Population filters (sides.*.table.where, compare.exclude,
-- compare.valueFilter) are printed verbatim in the coverage header and the
-- sign-off report with pre- and post-filter counts (runs.stats).
CREATE TABLE ds.spec_approvals (
    spec_id      bigint       NOT NULL,
    scope        text[]       NOT NULL,
    approved_by  text         NOT NULL,
    approved_at  timestamptz  NOT NULL DEFAULT now(),
    note         text,
    CONSTRAINT spec_approvals_pk PRIMARY KEY (spec_id),
    CONSTRAINT spec_approvals_spec_fk FOREIGN KEY (spec_id) REFERENCES ds.specs (spec_id),
    CONSTRAINT spec_approvals_scope_chk CHECK (
        cardinality(scope) >= 1 AND array_position(scope, NULL) IS NULL
        AND scope <@ ARRAY['clear', 'rawClear', 'unlisted_clear', 'population_filters']::text[]),
    CONSTRAINT spec_approvals_note_len CHECK (note IS NULL OR length(note) <= 1000)
);
COMMENT ON TABLE ds.spec_approvals IS
    'Four-eyes approval of one spec version. scope is a non-empty subset of {clear (security.clear), rawClear (sides.*.rawClear), unlisted_clear (security.unlisted = clear), population_filters (sides.*.table.where, compare.include, compare.exclude, compare.valueFilter, canonical.scope; needed for a certified claim)}. The approver (rule_approver) authored no version of the spec_name (trigger spec_approvals_four_eyes). Insert-only.';

-- Taxonomy registry. The core codes and the entity-linking pack codes are
-- seeded below (the pack installer's seed). Another domain pack INSERTs its
-- own codes at install time; no core DDL change is needed. Rows are
-- insert-only for ds_app: changing the presence of a code already used by
-- findings needs a migration that re-validates those findings.
CREATE TABLE ds.taxonomy_codes (
    code              text         NOT NULL,
    pack              text         NOT NULL,
    default_severity  ds.severity  NOT NULL,
    presence          text         NOT NULL DEFAULT 'none',
    alias_of          text,
    description       text,
    created_at        timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT taxonomy_codes_pk PRIMARY KEY (code),
    -- target of taxonomy_codes_alias_fk
    CONSTRAINT taxonomy_codes_code_presence_uq UNIQUE (code, presence),
    -- a pack alias names a core code with the same presence (e.g.
    -- MISSING_ENTITY for MISSING_DOCUMENT); findings store the core code
    CONSTRAINT taxonomy_codes_alias_fk FOREIGN KEY (alias_of, presence)
        REFERENCES ds.taxonomy_codes (code, presence),
    CONSTRAINT taxonomy_codes_alias_not_self CHECK (alias_of IS NULL OR alias_of <> code),
    CONSTRAINT taxonomy_codes_code_format CHECK (code ~ '^[A-Z][A-Z0-9_]*$' AND octet_length(code) <= 64),
    CONSTRAINT taxonomy_codes_pack_format CHECK (pack ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
    CONSTRAINT taxonomy_codes_presence_chk CHECK (presence IN ('missing', 'extra', 'none')),
    CONSTRAINT taxonomy_codes_description_len CHECK (description IS NULL OR length(description) <= 1000)
);
COMMENT ON TABLE ds.taxonomy_codes IS
    'Taxonomy registry (architecture.md §7.2). findings, signatures, root_causes and known_diffs reference code. Severities can be overridden per field; similarity-compared fields are never CRITICAL/HIGH.';
COMMENT ON COLUMN ds.taxonomy_codes.pack IS 'Owning pack: core, entity-linking, or another domain pack name.';
COMMENT ON COLUMN ds.taxonomy_codes.presence IS
    'missing: findings need change_type missing_in_b (present in A, absent in B). extra: extra_in_b. none: any change_type. Enforced by trigger findings_taxonomy_presence.';
COMMENT ON COLUMN ds.taxonomy_codes.alias_of IS
    'The core code this pack code is a display alias of (same presence, taxonomy_codes_alias_fk). Findings always store the core code (trigger findings_taxonomy_presence rejects an alias); reports and the UI show the alias of an installed pack, e.g. MISSING_ENTITY for MISSING_DOCUMENT.';

INSERT INTO ds.taxonomy_codes (code, pack, default_severity, presence, alias_of, description) VALUES
    -- generic (core); FIELD_*, VALUE_, TYPE_ and ORDER_MISMATCH take their severity per field
    ('FIELD_MISSING',             'core',           'MEDIUM',   'missing', NULL, 'Field present in A, absent in B; no domain pack claims the finding.'),
    ('FIELD_EXTRA',               'core',           'MEDIUM',   'extra',   NULL, 'Field present in B, absent in A; no domain pack claims the finding.'),
    ('VALUE_MISMATCH',            'core',           'MEDIUM',   'none',    NULL, 'Value differs; no domain pack claims the finding.'),
    ('TYPE_MISMATCH',             'core',           'MEDIUM',   'none',    NULL, 'JSON type differs; no domain pack claims the finding.'),
    ('ORDER_MISMATCH',            'core',           'MEDIUM',   'none',    NULL, 'Order of an ordered array differs; no domain pack claims the finding.'),
    ('NORMALIZATION_ONLY',        'core',           'INFO',     'none',    NULL, 'Raw values differ but canonical values are equal (emitted per pointer whenever the raw hash differs). Never makes a key MISMATCH.'),
    -- document-level (core): always emitted for NOT_FOUND_B / NOT_FOUND_A at path_pattern $, json_pointer '', member_key_tok = key
    ('MISSING_DOCUMENT',          'core',           'CRITICAL', 'missing', NULL, 'The whole document is present in A and authoritatively absent in B (NOT_FOUND_B). One finding per such key, so each appears in discrepancies.csv.'),
    ('EXTRA_DOCUMENT',            'core',           'CRITICAL', 'extra',   NULL, 'The whole document is present in B and authoritatively absent in A (NOT_FOUND_A). One finding per such key, so each appears in discrepancies.csv.'),
    -- generic graph codes (core; graph/v1 canonical mode, severity per node or edge type)
    ('MISSING_NODE',              'core',           'HIGH',     'missing', NULL, 'Graph mode: typed node (type, namespace, business_key) present in A, absent in B within the declared scope.'),
    ('EXTRA_NODE',                'core',           'HIGH',     'extra',   NULL, 'Graph mode: typed node (type, namespace, business_key) present in B, absent in A within the declared scope.'),
    ('MISSING_EDGE',              'core',           'HIGH',     'missing', NULL, 'Graph mode: edge (from, type, to, qualifier) present in A, absent in B within the declared scope.'),
    ('EXTRA_EDGE',                'core',           'HIGH',     'extra',   NULL, 'Graph mode: edge (from, type, to, qualifier) present in B, absent in A within the declared scope.'),
    -- selection and schema (core)
    ('FILTER_MEMBERSHIP',         'core',           'MEDIUM',   'none',    NULL, 'The selection predicate is true on one side only. Filters run on both sides and the union of selected keys is compared.'),
    ('SCHEMA_DRIFT',              'core',           'INFO',     'none',    NULL, 'An observation violates the approved inferred schema on an unselected path only (new, retyped or missing field). Always INFO, so never makes a key MISMATCH; counted in coverage (n_schema_drift). Drift on a selected or compared path, or an undeclared graph type within scope, makes the side INCOMPLETE and the key INCONCLUSIVE(schema_drift).'),
    -- source self-contradiction (core): the connector's existence_check is a core capability
    ('INTERNAL_INCONSISTENCY',    'core',           'HIGH',     'none',    NULL, 'A side contradicts itself: the connector''s declared existence_check finds a key its primary read reported absent (the key stays NOT_FOUND_A / NOT_FOUND_B and gets this finding), or a graph anomaly such as an edge stored at one endpoint only, a node left without its attributes or a stale index.'),
    -- entity-linking pack
    ('MISSING_ENTITY',            'entity-linking', 'CRITICAL', 'missing', 'MISSING_DOCUMENT', 'The pack''s name for MISSING_DOCUMENT: key present on side a only (confirmed one-sided absence). Display alias; findings store MISSING_DOCUMENT.'),
    ('EXTRA_ENTITY',              'entity-linking', 'CRITICAL', 'extra',   'EXTRA_DOCUMENT',   'The pack''s name for EXTRA_DOCUMENT: key present on side b only (confirmed one-sided absence). Display alias; findings store EXTRA_DOCUMENT.'),
    ('MISSING_ATTACHMENT',        'entity-linking', 'HIGH',     'missing', NULL, 'Attachment (entity_key, id_type, id_norm_tok) on side a only.'),
    ('EXTRA_ATTACHMENT',          'entity-linking', 'HIGH',     'extra',   NULL, 'Attachment (entity_key, id_type, id_norm_tok) on side b only.'),
    ('MISSING_LINK',              'entity-linking', 'HIGH',     'missing', NULL, 'Linked-entity set member on side a only (the pack''s name for MISSING_EDGE).'),
    ('EXTRA_LINK',                'entity-linking', 'HIGH',     'extra',   NULL, 'Linked-entity set member on side b only (the pack''s name for EXTRA_EDGE).'),
    ('LINK_EVIDENCE_MISMATCH',    'entity-linking', 'MEDIUM',   'none',    NULL, 'Same linked pair, different shared identifier types.'),
    ('ATTRIBUTE_MISMATCH',        'entity-linking', 'MEDIUM',   'none',    NULL, 'value_changed on a non-key field (severity overridable per field).'),
    ('FLAG_MISMATCH',             'entity-linking', 'CRITICAL', 'none',    NULL, 'The flag differs.'),
    ('CLUSTER_SPLIT',             'entity-linking', 'HIGH',     'none',    NULL, 'One old component maps to more than one new component.'),
    ('CLUSTER_MERGE',             'entity-linking', 'HIGH',     'none',    NULL, 'More than one old component maps to one new component.'),
    ('CLUSTER_REARRANGE',         'entity-linking', 'HIGH',     'none',    NULL, 'Components both split and merge.'),
    ('HOP_SEMANTICS',             'entity-linking', 'MEDIUM',   'none',    NULL, 'The diff disappears under the other side''s hop depth.'),
    ('DIRECTIONALITY',            'entity-linking', 'MEDIUM',   'none',    NULL, 'The diff disappears under the other side''s link direction.'),
    ('TEMPORAL_VALIDITY',         'entity-linking', 'MEDIUM',   'none',    NULL, 'The differing link is expired or soft-deleted on one side as of T.'),
    ('HOT_IDENTIFIER_POLICY',     'entity-linking', 'HIGH',     'none',    NULL, 'The diff involves an identifier above the stoplist or degree cap on one side.'),
    ('DUPLICATE_IDENTIFIER_NODE', 'entity-linking', 'HIGH',     'none',    NULL, 'More than one identifier node for the same (id_type, id_norm).');

-- =============================================================================
-- 6. Runs, keys and the work queue
-- =============================================================================
CREATE TABLE ds.runs (
    run_id                bigint      GENERATED ALWAYS AS IDENTITY,
    spec_id               bigint      NOT NULL,
    spec_name             text        NOT NULL,
    spec_version          integer     NOT NULL,
    dataset_id            text        NOT NULL DEFAULT 'default',
    mode                  ds.run_mode NOT NULL,
    status                text        NOT NULL DEFAULT 'queued',
    pause_reason          text,
    target_a_id           text        NOT NULL,
    target_b_id           text        NOT NULL,
    key_source_kind       text,
    manifest_hash         ds.sha256_hex,
    manifest_eof          jsonb,
    claim_level           text        NOT NULL DEFAULT 'observed',
    time_mode             text        NOT NULL DEFAULT 'live',
    cut_metadata          jsonb,
    parent_run_id         bigint,
    requested_by          text        NOT NULL,
    idempotency_route     text,
    idempotency_key_hash  ds.sha256_hex,
    reason                text,
    planned_keys          bigint,
    current_cycle_no      integer     NOT NULL DEFAULT 0,
    max_cycles            integer,
    cycle_interval        interval,
    dedicated_partitions  boolean     NOT NULL DEFAULT false,
    agent_visible         boolean     NOT NULL DEFAULT true,
    options               jsonb       NOT NULL DEFAULT '{}',
    labels                jsonb       NOT NULL DEFAULT '{}',
    stats                 jsonb       NOT NULL DEFAULT '{}',
    stop_reason           text,
    error_message         text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    started_at            timestamptz,
    finished_at           timestamptz,
    heartbeat_at          timestamptz,
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT runs_pk PRIMARY KEY (run_id),
    CONSTRAINT runs_spec_fk FOREIGN KEY (spec_id, spec_name, spec_version)
        REFERENCES ds.specs (spec_id, spec_name, spec_version),
    CONSTRAINT runs_dataset_fk FOREIGN KEY (dataset_id) REFERENCES ds.datasets (dataset_id),
    -- a run's dataset is its spec's dataset
    CONSTRAINT runs_spec_dataset_fk FOREIGN KEY (spec_id, dataset_id)
        REFERENCES ds.specs (spec_id, dataset_id),
    CONSTRAINT runs_target_a_fk FOREIGN KEY (target_a_id) REFERENCES ds.targets (target_id),
    CONSTRAINT runs_target_b_fk FOREIGN KEY (target_b_id) REFERENCES ds.targets (target_id),
    CONSTRAINT runs_parent_fk FOREIGN KEY (parent_run_id) REFERENCES ds.runs (run_id) ON DELETE SET NULL,
    -- a retried POST with the same Idempotency-Key cannot create a second
    -- run, even after its in_flight idempotency row was reclaimed. The principal
    -- is the end user (requested_by); the key is stored as sha256(key) only.
    CONSTRAINT runs_idempotency_uq UNIQUE (requested_by, idempotency_route, idempotency_key_hash),
    CONSTRAINT runs_idempotency_pair CHECK ((idempotency_route IS NULL) = (idempotency_key_hash IS NULL)),
    CONSTRAINT runs_idempotency_route_format CHECK (
        idempotency_route IS NULL
        OR (idempotency_route ~ '^(POST|PUT|PATCH|DELETE) /[!-~]*$' AND octet_length(idempotency_route) <= 256)),
    -- Run state machine owned by ds-scheduler (architecture.md §6 Run
    -- states). v0.2 'pending' is renamed 'queued'; completed_with_issues =
    -- finished, but some units dead-lettered or some manifest keys outstanding
    -- (see v_run_summary counters).
    -- why a run is paused (auth, schema or mapping drift; breaker; storage; operator)
    CONSTRAINT runs_pause_reason_chk CHECK (pause_reason IS NULL OR pause_reason IN (
        'schema_drift', 'mapping_drift', 'auth', 'source_health', 'circuit_open', 'storage', 'operator')),
    CONSTRAINT runs_pause_reason_only_paused CHECK (pause_reason IS NULL OR status IN ('paused', 'cancelling', 'cancelled')),
    CONSTRAINT runs_status_chk CHECK (status IN (
        'queued', 'running', 'paused', 'cancelling', 'cancelled',
        'completed', 'completed_with_issues', 'failed')),
    CONSTRAINT runs_key_source_kind_chk CHECK (key_source_kind IN (
        'list', 'csv', 'sql', 'paginated_endpoint', 'run_keys', 'hash_sample', 'union')),
    -- claim level and time policy (DDL-local vocabularies)
    CONSTRAINT runs_claim_level_chk CHECK (claim_level IN ('certified', 'observed')),
    CONSTRAINT runs_time_mode_chk CHECK (time_mode IN ('frozen', 'shared_watermark', 'live')),
    -- certified = a frozen, hashed manifest whose every source proved EOF, under a
    -- frozen or shared_watermark time policy; anything else is observed
    CONSTRAINT runs_certified_needs_manifest CHECK (
        claim_level <> 'certified'
        OR (manifest_hash IS NOT NULL
            AND manifest_eof IS NOT NULL AND manifest_eof <> '{}'::jsonb
            AND jsonb_array_length(jsonb_path_query_array(manifest_eof, '$.*'))
                = jsonb_array_length(jsonb_path_query_array(manifest_eof, '$.* ? (@.eof == true)'))
            AND time_mode IN ('frozen', 'shared_watermark'))),
    -- a change-feed run never implies full coverage
    CONSTRAINT runs_change_driven_observed CHECK (mode <> 'change_driven' OR claim_level = 'observed'),
    -- a frozen certified run records both ingestion pipelines' upstream
    -- watermarks at the cut T (cut_metadata.wm_a / wm_b); without both the
    -- claim is observed. Keys touched in (min, max] of the two are DRIFT(cut_window).
    CONSTRAINT runs_frozen_certified_needs_watermarks CHECK (
        claim_level <> 'certified' OR time_mode <> 'frozen'
        OR (cut_metadata IS NOT NULL
            AND coalesce(cut_metadata ->> 'wm_a', '') <> ''
            AND coalesce(cut_metadata ->> 'wm_b', '') <> '')),
    CONSTRAINT runs_manifest_eof_object CHECK (manifest_eof IS NULL OR jsonb_typeof(manifest_eof) = 'object'),
    CONSTRAINT runs_cut_metadata_object CHECK (cut_metadata IS NULL OR jsonb_typeof(cut_metadata) = 'object'),
    CONSTRAINT runs_cycles_chk CHECK (
        current_cycle_no >= 0 AND (max_cycles IS NULL OR max_cycles >= 1)),
    CONSTRAINT runs_one_shot_single_cycle CHECK (mode <> 'one_shot' OR coalesce(max_cycles, 1) = 1),
    CONSTRAINT runs_cycle_interval_chk CHECK (cycle_interval IS NULL OR cycle_interval > interval '0'),
    CONSTRAINT runs_terminal_has_finished_at CHECK (
        (status IN ('cancelled', 'completed', 'completed_with_issues', 'failed')) = (finished_at IS NOT NULL)),
    CONSTRAINT runs_time_order CHECK (
        finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at),
    CONSTRAINT runs_planned_keys_chk CHECK (planned_keys IS NULL OR planned_keys >= 0),
    CONSTRAINT runs_json_objects CHECK (
        jsonb_typeof(options) = 'object'
        AND jsonb_typeof(labels) = 'object'
        AND jsonb_typeof(stats) = 'object')
);
COMMENT ON TABLE ds.runs IS
    'One comparison run. It pins an immutable spec and two targets (a = legacy, b = new). ds-scheduler owns the status state machine and soak cycles.';
COMMENT ON COLUMN ds.runs.dedicated_partitions IS
    'true once ds.create_run_partitions() has attached per-run partitions of observations, results and findings. false = rows live in the DEFAULT partitions.';
COMMENT ON COLUMN ds.runs.agent_visible IS
    'false hides the run from the curated views (and so from ds_agent_ro, ds-mcp read tools and DuckDB).';
COMMENT ON COLUMN ds.runs.stats IS
    'Run-level totals written by ds-scheduler / ds-bulkdiff, including the pre- and post-filter counts of each side''s population filters (printed in the coverage header and sign-off). Trade-off for the P2 bulk modes (bulk_parity, table_diff): their MATCH rows are written as Parquet to the object store (results/run_id=..., same columns as ds.results) and ds.results holds their non-MATCH rows only, so the run totals live here and are exposed as v_run_summary.run_stats.';
COMMENT ON COLUMN ds.runs.idempotency_key_hash IS
    'sha256 of the Idempotency-Key of the POST that created the run (never the key itself), with idempotency_route. runs_idempotency_uq (requested_by, idempotency_route, idempotency_key_hash) makes run creation idempotent; the run, its outbox event and the completed ds.idempotency_keys row are written in one transaction.';
COMMENT ON COLUMN ds.runs.parent_run_id IS
    'mode = rediff: the source run whose observations are re-compared (what-if). Otherwise the run this one was derived from, if any.';
COMMENT ON COLUMN ds.runs.key_source_kind IS
    'Top-level spec key.source kind. union = several base sources (e.g. side a SQL plus a side b paginated_endpoint), deduplicated after key.normalize.';
COMMENT ON COLUMN ds.runs.status IS
    'Run state (architecture.md §6 Run states): queued | running | paused | cancelling | cancelled | completed | completed_with_issues | failed. v0.2 pending was renamed queued. Terminal states (cancelled, completed, completed_with_issues, failed) have finished_at. Counters reconcile exactly: manifest total = verdict MATCH + DIFFERENT + INDETERMINATE + cancelled + outstanding (v_run_summary).';
COMMENT ON COLUMN ds.runs.dataset_id IS
    'ABAC scope; always equals the spec''s dataset (runs_spec_dataset_fk). Evidence, findings and exports of the run inherit it. ds-api enforces the scope; RLS is not built.';
COMMENT ON COLUMN ds.runs.manifest_hash IS
    'sha256 of the frozen key set (run_keys) computed at run start. The key cursor advances atomically with durable seed insertion; once set, the manifest is not changed. P2 bulk runs: sha256 of the sorted union of the extracted keys.';
COMMENT ON COLUMN ds.runs.manifest_eof IS
    'Per-source enumeration EOF proof, {"<source>": {"eof": true, "count": n, "source_version": "...", "as_of": "..."}}. certified requires eof = true for every source. P2 bulk runs: one entry per extractor, {"eof", "count"}.';
COMMENT ON COLUMN ds.runs.claim_level IS
    'certified (every inventory proved exhaustive under a frozen or shared_watermark time policy) | observed (partial or unstable inventory, live time policy, or a change-feed run). Default observed; reports and API responses always carry it.';
COMMENT ON COLUMN ds.runs.time_mode IS
    'The spec time.mode the run executed under (frozen | shared_watermark | live). A certified claim needs frozen or shared_watermark; live is labelled observed.';
COMMENT ON COLUMN ds.runs.cut_metadata IS
    'Frozen certification: the EnvironmentSnapshot cut (recovery_target_time T, actual stop LSN and timestamp per restored cluster, service versions, pg_stat_user_tables deltas) and wm_a / wm_b: the upstream watermark (event offset or batch id) each ingestion pipeline persisted with its writes as of T. A frozen certified run needs both (runs_frozen_certified_needs_watermarks); keys touched by events in (min, max] of the two are DRIFT(cut_window). Printed in the sign-off report.';

CREATE TABLE ds.run_keys (
    run_id         bigint      NOT NULL,
    key            text        NOT NULL,
    key_enc        bytea,
    stratum        text,
    signoff_stratum text,
    signoff_member boolean     NOT NULL DEFAULT false,
    source_kind    text        NOT NULL,
    sample_bucket  bigint,
    census         boolean     NOT NULL DEFAULT false,
    attrs          jsonb       NOT NULL DEFAULT '{}',
    added_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT run_keys_pk PRIMARY KEY (run_id, key),
    CONSTRAINT run_keys_run_fk FOREIGN KEY (run_id) REFERENCES ds.runs (run_id) ON DELETE CASCADE,
    CONSTRAINT run_keys_key_len CHECK (key <> '' AND octet_length(key) <= 512),
    -- an identifier-class key is stored as its token, and its raw value
    -- only envelope-encrypted, so ds-fetcher can still call the targets
    CONSTRAINT run_keys_key_enc_iff_token CHECK (
        (key ~ '^tok:[a-z][a-z0-9_]*:[A-Za-z0-9]+:[A-Z2-7]{26}$') = (key_enc IS NOT NULL)),
    CONSTRAINT run_keys_key_enc_size CHECK (key_enc IS NULL OR octet_length(key_enc) BETWEEN 16 AND 4096),
    -- only a key with a sign-off label can be a member of that label's sample
    CONSTRAINT run_keys_signoff_member_needs_stratum CHECK (NOT signoff_member OR signoff_stratum IS NOT NULL),
    -- dim=label pairs joined by '|'; bucket labels may contain '+'
    CONSTRAINT run_keys_stratum_format CHECK (stratum ~ '^[A-Za-z0-9][A-Za-z0-9_.:=|+-]{0,255}$'),
    CONSTRAINT run_keys_signoff_stratum_format CHECK (signoff_stratum ~ '^[A-Za-z0-9][A-Za-z0-9_.:=|+-]{0,255}$'),
    CONSTRAINT run_keys_source_kind_chk CHECK (source_kind IN (
        'list', 'csv', 'sql', 'paginated_endpoint', 'run_keys', 'hash_sample', 'union')),
    CONSTRAINT run_keys_attrs_object CHECK (jsonb_typeof(attrs) = 'object'),
    -- union key source: the side(s) whose base source produced the key
    CONSTRAINT run_keys_attrs_origin CHECK (
        attrs ->> 'origin' IS NULL OR attrs ->> 'origin' IN ('a', 'b', 'both'))
);
COMMENT ON TABLE ds.run_keys IS
    'The key set of a run, materialised from spec key.source (list, csv, sql, paginated_endpoint, run_keys, hash_sample, union). It is the run''s frozen manifest: hashed into runs.manifest_hash at run start, and the denominator (manifest total) of v_run_summary.coverage. ds-comparer writes results only for keys in run_keys.';
COMMENT ON COLUMN ds.run_keys.stratum IS
    'Stratum label as stored in results.stratum: dim=label pairs joined by | (e.g. degree_bucket=d51_250|id_type=BANK_ACCOUNT). CEL sees it as stratum (string) and strata (map dim -> label).';
COMMENT ON COLUMN ds.run_keys.sample_bucket IS 'hash_sample: salted hash bucket that selected the key (for reproducible sampling).';
COMMENT ON COLUMN ds.run_keys.census IS 'true when the key was included by a census rule (hot identifiers, flagged entities), not by sampling.';
COMMENT ON COLUMN ds.run_keys.attrs IS
    'Coarse, non-identifying attributes used for stratification and contrast_by_attribute. origin (a | b | both): the side(s) whose base source produced the key under a union key source (also when a hash_sample wraps a union).';
COMMENT ON COLUMN ds.run_keys.key_enc IS
    'The raw identifier-class key, envelope-encrypted with a KMS data key; set exactly when key is a token (run_keys_key_enc_iff_token). Only ds-fetcher (to call the targets) and the audited reveal endpoint decrypt it. NULL for business-code keys, which are stored in clear.';
COMMENT ON COLUMN ds.run_keys.signoff_member IS
    'true iff the key was selected by its own sign-off bucket''s bottom-k or rate (a uniform sample of signoff_stratum). Copied to results.signoff_member. Sign-off bounds use members only; keys that carry the label but were added by another bucket or the base rate are reported as n_contrast.';

CREATE TABLE ds.work_units (
    unit_id           bigint      GENERATED ALWAYS AS IDENTITY,
    run_id            bigint      NOT NULL,
    cycle_no          integer,
    kind              text        NOT NULL,
    state             text        NOT NULL DEFAULT 'pending',
    priority          smallint    NOT NULL DEFAULT 100,
    available_at      timestamptz NOT NULL DEFAULT now(),
    dedupe_key        text        NOT NULL,
    payload           jsonb       NOT NULL DEFAULT '{}',
    key_count         integer     NOT NULL DEFAULT 0,
    attempts          smallint    NOT NULL DEFAULT 0,
    max_attempts      smallint    NOT NULL DEFAULT 5,
    lease_owner       text,
    lease_token       uuid,
    leased_at         timestamptz,
    last_error_class  text,
    last_error        text,
    result_summary    jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    completed_at      timestamptz,
    CONSTRAINT work_units_pk PRIMARY KEY (unit_id),
    CONSTRAINT work_units_run_fk FOREIGN KEY (run_id) REFERENCES ds.runs (run_id) ON DELETE CASCADE,
    CONSTRAINT work_units_dedupe_uq UNIQUE (run_id, dedupe_key),
    -- DDL-local vocabularies
    CONSTRAINT work_units_kind_chk CHECK (kind IN (
        'fetch', 'compare', 'recheck', 'existence_check', 'calibration',
        'extract', 'bulk_diff', 'report')),
    CONSTRAINT work_units_state_chk CHECK (state IN (
        'pending', 'leased', 'done', 'failed', 'dead', 'cancelled')),
    CONSTRAINT work_units_priority_chk CHECK (priority BETWEEN 0 AND 1000),
    CONSTRAINT work_units_cycle_chk CHECK (cycle_no IS NULL OR cycle_no >= 1),
    CONSTRAINT work_units_attempts_chk CHECK (
        attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts),
    CONSTRAINT work_units_key_count_chk CHECK (key_count >= 0),
    CONSTRAINT work_units_lease_fields CHECK (
        state <> 'leased'
        OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND leased_at IS NOT NULL)),
    CONSTRAINT work_units_payload_object CHECK (jsonb_typeof(payload) = 'object')
) WITH (
    fillfactor = 80,
    autovacuum_vacuum_scale_factor = 0.01,
    autovacuum_analyze_scale_factor = 0.02
);
COMMENT ON TABLE ds.work_units IS
    'Job queue (no external broker in the MVP). Workers lease rows with FOR UPDATE SKIP LOCKED; see section 11. A unit is claimable when state IN (pending, leased) and available_at <= now(): for pending rows available_at is the not-before time (recheck delay, retry backoff); for leased rows it is the lease expiry, so an expired lease is claimable again.';
COMMENT ON COLUMN ds.work_units.priority IS 'Lower is more urgent (one_shot/compare_key 0, recheck 10, default 100).';
COMMENT ON COLUMN ds.work_units.available_at IS
    'pending: the not-before time. A recheck is its own unit (kind recheck) enqueued with available_at = now() + recheck.delay + uniform(0, recheck.jitter); no worker sleeps inline. leased: the lease expiry, extended by heartbeat and fenced by lease_token (there is no separate lease_expires_at column). Workers lease with FOR UPDATE SKIP LOCKED LIMIT $n (section 11.1).';
COMMENT ON COLUMN ds.work_units.dedupe_key IS 'Idempotent enqueue: INSERT ... ON CONFLICT (run_id, dedupe_key) DO NOTHING.';
COMMENT ON COLUMN ds.work_units.lease_token IS 'Fencing token. Heartbeat and completion must match it, so a worker whose lease expired cannot complete the unit.';

-- =============================================================================
-- 7. Partitioned fact tables: observations, results, findings
-- -----------------------------------------------------------------------------
-- Partitioning choice: PARTITION BY LIST (run_id), plus a DEFAULT partition.
--
-- Why LIST (run_id):
--   * Every hot query is scoped to a run: MCP tools (get_run_summary,
--     get_findings, list_root_causes), sign-off, reports and comparer writes.
--     LIST(run_id) prunes those queries to one partition at plan time or at
--     executor start.
--   * The run lifecycle becomes the partition lifecycle. A run is removed
--     with DETACH plus DROP, not a multi-million-row DELETE. That leaves no
--     vacuum debt and no index bloat.
--   * Bulk modes (bulk_parity, table_diff) can COPY into a standalone table
--     and then ATTACH it.
--   * Each partition gets its own planner statistics, so a mismatch-heavy
--     run does not skew the plans of clean runs.
--
-- Why not HASH:
--   * HASH(run_id) gives a fixed partition count, but a run cannot be dropped
--     as a unit, so retention becomes DELETE plus VACUUM. A large soak run
--     still lands in a single bucket, so HASH gives no spread.
--   * HASH(run_id, key) spreads a run, but run-scoped queries then touch
--     every partition, because hash pruning needs equality on all partition
--     key columns.
--
-- Costs and mitigations:
--   * Partition count grows with runs. Only runs that ds-scheduler passes to
--     ds.create_run_partitions() get dedicated partitions (soak, calibration,
--     bulk_parity, table_diff and large batch runs). one_shot and small batch
--     runs share the DEFAULT partitions. Retention keeps the count bounded.
--   * Attaching a partition scans the DEFAULT partition and takes an ACCESS
--     EXCLUSIVE lock on it. Keep the DEFAULT partitions small with a short
--     retention for one_shot runs. Call create_run_partitions() before the
--     first write for the run. If rows for that run already sit in DEFAULT,
--     ATTACH fails and the run stays in DEFAULT.
--   * DETACH PARTITION ... CONCURRENTLY is unavailable while a DEFAULT
--     partition exists, so retention uses plain DETACH under a short
--     lock_timeout (section 11.6).
--   * There are deliberately no partitioned-to-partitioned foreign keys
--     (findings -> results, observations -> results). Detaching a referenced
--     partition would have to validate against every referencing partition.
--     Instead, ds-comparer writes the results row and its findings for one key
--     in one transaction (section 11.5). FKs to small regular tables (runs,
--     targets, root_causes, known_diffs, noise_masks) are kept.
--   * Surrogate ids (obs_id, finding_id) come from explicit sequences, not
--     identity columns, so partitions created with LIKE ... + ATTACH share a
--     single id space.
-- =============================================================================
CREATE SEQUENCE ds.observations_obs_id_seq AS bigint;
CREATE SEQUENCE ds.findings_finding_id_seq AS bigint;

-- 7.1 observations ------------------------------------------------------------
CREATE TABLE ds.observations (
    obs_id         bigint        NOT NULL DEFAULT nextval('ds.observations_obs_id_seq'),
    run_id         bigint        NOT NULL,
    cycle_no       integer       NOT NULL,
    key            text          NOT NULL,
    side           ds.side       NOT NULL,
    call_seq       text          NOT NULL,
    probe          text          NOT NULL DEFAULT 'primary',
    target_id      text          NOT NULL,
    request_hash   ds.sha256_hex NOT NULL,
    http_status    smallint,
    error_class    text,
    latency_ms     integer,
    observed_at    timestamptz   NOT NULL,
    content_hash   ds.sha256_hex,
    blob_uri       text,
    bytes          bigint,
    truncated      boolean       NOT NULL DEFAULT false,
    attempt        smallint      NOT NULL DEFAULT 1,
    work_unit_id   bigint,
    fetcher_id     text,
    response_meta  jsonb         NOT NULL DEFAULT '{}',
    expired_at     timestamptz,
    CONSTRAINT observations_pk PRIMARY KEY (run_id, obs_id),
    -- idempotent persist: ON CONFLICT (run_id, cycle_no, key, side, call_seq, probe) DO NOTHING
    CONSTRAINT observations_call_uq UNIQUE (run_id, cycle_no, key, side, call_seq, probe),
    CONSTRAINT observations_run_fk FOREIGN KEY (run_id) REFERENCES ds.runs (run_id),
    CONSTRAINT observations_target_fk FOREIGN KEY (target_id) REFERENCES ds.targets (target_id),
    CONSTRAINT observations_cycle_chk CHECK (cycle_no >= 1),
    CONSTRAINT observations_key_len CHECK (key <> '' AND octet_length(key) <= 512),
    CONSTRAINT observations_call_seq_format CHECK (call_seq ~ '^[A-Z][0-9]{1,3}$'),
    -- probe names declared by the connector (targets.capabilities.existence_check.probes), or primary
    CONSTRAINT observations_probe_chk CHECK (probe ~ '^[a-z][a-z0-9_]{0,31}$'),
    CONSTRAINT observations_http_status_chk CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599),
    CONSTRAINT observations_has_status_or_error CHECK (http_status IS NOT NULL OR error_class IS NOT NULL),
    CONSTRAINT observations_latency_chk CHECK (latency_ms IS NULL OR latency_ms >= 0),
    CONSTRAINT observations_bytes_chk CHECK (bytes IS NULL OR bytes >= 0),
    CONSTRAINT observations_attempt_chk CHECK (attempt >= 1),
    -- content addressing: a stored body has a hash, and its URI contains that hash
    CONSTRAINT observations_blob_uri_format CHECK (blob_uri IS NULL OR blob_uri ~ '^s3://[^[:space:]]+$'),
    CONSTRAINT observations_blob_content_addressed CHECK (
        blob_uri IS NULL OR (content_hash IS NOT NULL AND strpos(blob_uri, content_hash) > 0)),
    CONSTRAINT observations_response_meta_object CHECK (jsonb_typeof(response_meta) = 'object'),
    -- the lifecycle job marks the evidence expired when it deletes the blob
    CONSTRAINT observations_expired_after_observed CHECK (expired_at IS NULL OR expired_at >= observed_at)
) PARTITION BY LIST (run_id);
COMMENT ON TABLE ds.observations IS
    'One row per call made to a target (metadata only). Bodies are zstd Parquet in the object store at blob_uri, content-addressed by sha256 (content_hash), uploaded and hash-verified before the results commit (evidence first). Partitioned by LIST (run_id). Raw bodies are the only source for reveal (raw values are re-derived from them with findings.json_pointer), so they are retained at least as long as findings (specs.retention_class: standard 90 d, audit 1 y). Evidence is served only as a masked rendition addressed by obs_id; a raw blob is never presigned. The agent sees v_observations_meta, which has neither blob_uri nor content_hash.';
COMMENT ON COLUMN ds.observations.call_seq IS
    'Position in the call pattern for the key and cycle: L1, N1, L2, N2 (sandwich), R1 (recheck), and so on.';
COMMENT ON COLUMN ds.observations.probe IS
    'primary = the normal read. Any other value names a probe of the connector''s declared existence_check capability (targets.capabilities.existence_check.probes; e.g. a second lookup endpoint the target declares), run on a one-sided absence.';
COMMENT ON COLUMN ds.observations.expired_at IS
    'Set by the retention lifecycle job when it deletes the raw blob. Exposed as evidence_expired in v_observations_meta, v_results_masked and v_findings_masked; findings, exports and citations that need expired evidence are marked, and AI answers that need it are blocked. ds_app may update only this column.';
COMMENT ON COLUMN ds.observations.response_meta IS
    'Selected response headers (content-type, etag, paging cursors). Never Authorization, Cookie or Set-Cookie.';

CREATE TABLE ds.observations_default PARTITION OF ds.observations DEFAULT
    WITH (autovacuum_vacuum_scale_factor = 0.05);

-- 7.2 results -------------------------------------------------------------------
CREATE TABLE ds.results (
    run_id               bigint                 NOT NULL,
    cycle_no             integer                NOT NULL,
    key                  text                   NOT NULL,
    outcome              ds.outcome             NOT NULL,
    inconclusive_reason  ds.inconclusive_reason,
    drift_reason         text,
    confirmation         text                   NOT NULL DEFAULT 'provisional',
    side_a_state         ds.side_state          NOT NULL,
    side_b_state         ds.side_state          NOT NULL,
    first_outcome        ds.outcome             NOT NULL,
    revision             integer                NOT NULL DEFAULT 1,
    hash_a               ds.sha256_hex,
    hash_b               ds.sha256_hex,
    stratum              text,
    signoff_stratum      text,
    signoff_member       boolean                NOT NULL DEFAULT false,
    attrs                jsonb                  NOT NULL DEFAULT '{}',
    recheck_count        smallint               NOT NULL DEFAULT 0,
    compared_at          timestamptz            NOT NULL DEFAULT now(),
    spec_version         integer                NOT NULL,
    obs_a_id             bigint,
    obs_b_id             bigint,
    finding_count        integer                NOT NULL DEFAULT 0,
    suppressed_count     integer                NOT NULL DEFAULT 0,
    worst_severity       ds.severity,
    work_unit_id         bigint,
    comparer_id          text,
    created_at           timestamptz            NOT NULL DEFAULT now(),
    updated_at           timestamptz            NOT NULL DEFAULT now(),
    CONSTRAINT results_pk PRIMARY KEY (run_id, cycle_no, key),
    CONSTRAINT results_run_fk FOREIGN KEY (run_id) REFERENCES ds.runs (run_id),
    CONSTRAINT results_cycle_chk CHECK (cycle_no >= 1),
    CONSTRAINT results_key_len CHECK (key <> '' AND octet_length(key) <= 512),
    CONSTRAINT results_stratum_format CHECK (stratum ~ '^[A-Za-z0-9][A-Za-z0-9_.:=|+-]{0,255}$'),
    CONSTRAINT results_signoff_stratum_format CHECK (signoff_stratum ~ '^[A-Za-z0-9][A-Za-z0-9_.:=|+-]{0,255}$'),
    -- inconclusive_reason is set exactly when the outcome is INCONCLUSIVE
    CONSTRAINT results_inconclusive_reason_chk CHECK (
        (outcome = 'INCONCLUSIVE') = (inconclusive_reason IS NOT NULL)),
    -- drift_reason is set exactly when the outcome is DRIFT
    CONSTRAINT results_drift_reason_iff_drift CHECK ((outcome = 'DRIFT') = (drift_reason IS NOT NULL)),
    CONSTRAINT results_drift_reason_chk CHECK (drift_reason IS NULL OR drift_reason IN (
        'changed_since', 'a_changed', 'b_converged', 'watermark_mismatch', 'skew_exceeded', 'cut_window')),
    -- how a discrepant outcome was reproduced. provisional = before any
    -- recheck (counted as outstanding, not discrepant); rechecked = reproduced
    -- by a recheck (and, for a one-sided absence, the connector's declared
    -- existence_check); frozen_cut / bulk_exhaustive = reproduction is implied
    -- by an immutable snapshot or an exhaustive bulk extract, so no recheck
    -- runs. (Replaces v0.3.0 results_not_found_confirmed.)
    CONSTRAINT results_confirmation_chk CHECK (
        confirmation IN ('provisional', 'rechecked', 'frozen_cut', 'bulk_exhaustive')),
    CONSTRAINT results_rechecked_needs_recheck CHECK (confirmation <> 'rechecked' OR recheck_count >= 1),
    -- once a discrepant key has been rechecked it is no longer provisional
    CONSTRAINT results_provisional_not_rechecked CHECK (
        outcome NOT IN ('MISMATCH', 'NOT_FOUND_A', 'NOT_FOUND_B')
        OR confirmation <> 'provisional' OR recheck_count = 0),
    -- KNOWN_DIFF and NOISE_ONLY suppress at least one finding (a key with
    -- no findings is MATCH, never NOISE_ONLY)
    CONSTRAINT results_suppressed_outcome_has_suppression CHECK (
        outcome NOT IN ('KNOWN_DIFF', 'NOISE_ONLY') OR suppressed_count >= 1),
    -- KNOWN_DIFF compares two COMPLETE documents, or suppresses the
    -- document-level finding of a confirmed one-sided absence
    CONSTRAINT results_known_diff_sides CHECK (
        outcome <> 'KNOWN_DIFF'
        OR (side_a_state = 'COMPLETE' AND side_b_state = 'COMPLETE')
        OR (confirmation <> 'provisional'
            AND ((side_a_state = 'COMPLETE' AND side_b_state = 'ABSENT')
                 OR (side_a_state = 'ABSENT' AND side_b_state = 'COMPLETE')))),
    -- reason precedence, the part the side states decide: two ERROR sides
    -- are both_error, and an ERROR side never carries an INCOMPLETE reason
    CONSTRAINT results_reason_precedence CHECK (
        (side_a_state <> 'ERROR' OR side_b_state <> 'ERROR' OR inconclusive_reason = 'both_error')
        AND ((side_a_state <> 'ERROR' AND side_b_state <> 'ERROR')
             OR inconclusive_reason IN ('both_error', 'error_a', 'error_b', 'timeout', 'rate_limited', 'circuit_open'))),
    -- only a key with a sign-off label can be a member of that label's sample
    CONSTRAINT results_signoff_member_needs_stratum CHECK (NOT signoff_member OR signoff_stratum IS NOT NULL),
    CONSTRAINT results_not_found_a_no_hash CHECK (outcome <> 'NOT_FOUND_A' OR hash_a IS NULL),
    CONSTRAINT results_not_found_b_no_hash CHECK (outcome <> 'NOT_FOUND_B' OR hash_b IS NULL),
    CONSTRAINT results_counts_chk CHECK (
        recheck_count >= 0 AND finding_count >= 0
        AND suppressed_count >= 0 AND suppressed_count <= finding_count),
    CONSTRAINT results_spec_version_chk CHECK (spec_version > 0),
    CONSTRAINT results_attrs_object CHECK (jsonb_typeof(attrs) = 'object'),
    -- a manifest seed ABSENT on both sides is INCONCLUSIVE(absent_both), never
    -- MATCH. The reason is authoritative; the legacy attrs.absent_both flag, if a
    -- writer still sets it, must agree. (Replaces v0.2 results_absent_both_is_match.)
    CONSTRAINT results_absent_both_is_inconclusive CHECK (
        NOT (attrs @> '{"absent_both": true}')
        OR (outcome = 'INCONCLUSIVE' AND inconclusive_reason = 'absent_both')),
    -- both sides ABSENT <=> INCONCLUSIVE(absent_both)
    CONSTRAINT results_absent_both_sides CHECK (
        (side_a_state = 'ABSENT' AND side_b_state = 'ABSENT')
        = coalesce(inconclusive_reason = 'absent_both', false)),
    -- above the API cap: the api_uncomparable group, excluded from the INCONCLUSIVE + DRIFT gate
    CONSTRAINT results_api_uncomparable_is_truncated CHECK (
        NOT (attrs @> '{"api_uncomparable": true}')
        OR (outcome = 'INCONCLUSIVE' AND inconclusive_reason = 'truncated')),
    -- launch gate "zero false matches on incomplete evidence": any INCOMPLETE
    -- or ERROR side makes the key INCONCLUSIVE
    CONSTRAINT results_incomplete_is_inconclusive CHECK (
        outcome = 'INCONCLUSIVE'
        OR (side_a_state IN ('COMPLETE', 'ABSENT') AND side_b_state IN ('COMPLETE', 'ABSENT'))),
    -- MATCH, MISMATCH and NOISE_ONLY compare two complete documents (a
    -- NOT_FOUND whose existence_check hits stays NOT_FOUND with an
    -- INTERNAL_INCONSISTENCY finding, never MISMATCH with an ABSENT side)
    CONSTRAINT results_compared_sides_complete CHECK (
        outcome NOT IN ('MATCH', 'MISMATCH', 'NOISE_ONLY')
        OR (side_a_state = 'COMPLETE' AND side_b_state = 'COMPLETE')),
    -- NOT_FOUND_x: side x authoritatively ABSENT, the other side COMPLETE
    CONSTRAINT results_not_found_sides CHECK (
        (outcome <> 'NOT_FOUND_A' OR (side_a_state = 'ABSENT' AND side_b_state = 'COMPLETE'))
        AND (outcome <> 'NOT_FOUND_B' OR (side_a_state = 'COMPLETE' AND side_b_state = 'ABSENT'))),
    -- an ABSENT side has no document, so no canonical hash
    CONSTRAINT results_absent_side_no_hash CHECK (
        (side_a_state <> 'ABSENT' OR hash_a IS NULL) AND (side_b_state <> 'ABSENT' OR hash_b IS NULL)),
    -- per-side error reasons name the failed side(s)
    CONSTRAINT results_error_reason_sides CHECK (
        (inconclusive_reason IS DISTINCT FROM 'error_a' OR side_a_state = 'ERROR')
        AND (inconclusive_reason IS DISTINCT FROM 'error_b' OR side_b_state = 'ERROR')
        AND (inconclusive_reason IS DISTINCT FROM 'both_error'
             OR (side_a_state = 'ERROR' AND side_b_state = 'ERROR'))),
    -- truncation, unparseable values, resource limits, unstable pagination
    -- and schema drift on a compared path make a side INCOMPLETE
    CONSTRAINT results_incomplete_reason_sides CHECK (
        inconclusive_reason IS NULL
        OR inconclusive_reason NOT IN ('truncated', 'unparseable', 'resource_limit',
                                       'absence_unconfirmed', 'watermark_missing',
                                       'unstable_pagination', 'schema_drift')
        OR side_a_state = 'INCOMPLETE' OR side_b_state = 'INCOMPLETE'),
    -- revision 1 is the first write, so it still carries the first outcome
    CONSTRAINT results_revision_chk CHECK (revision >= 1 AND (revision > 1 OR outcome = first_outcome))
) PARTITION BY LIST (run_id);
COMMENT ON TABLE ds.results IS
    'One outcome per key per cycle (PK run_id, cycle_no, key), matches included. Written with an idempotent upsert (section 11.4) in the same transaction as the key''s findings and an outbox event. Per-side states gate the outcome: any INCOMPLETE or ERROR side is INCONCLUSIVE, and both sides ABSENT is INCONCLUSIVE(absent_both). A discrepant outcome carries how it was reproduced (confirmation) and a DRIFT its drift_reason. The first outcome is preserved (first_outcome) and revision counts overwrites. The three-value verdict is derived in v_result_verdicts / v_results_masked, not stored. Partitioned by LIST (run_id). Stated trade-off (P2 bulk modes bulk_parity and table_diff): MATCH rows go to Parquet in the object store and only non-MATCH rows are stored here; the run totals are in runs.stats.';
COMMENT ON COLUMN ds.results.side_a_state IS
    'Per-side observation state of side a: COMPLETE | ABSENT (only when the status or shape is in the target''s declared authoritative_absence) | INCOMPLETE (cap, truncation, pagination loop or unstable pagination, non-authoritative 404, unparseable value, resource limit, schema drift on a compared path, or not read) | ERROR (transport, timeout or 429/5xx after retries, or a status outside the side''s success statuses).';
COMMENT ON COLUMN ds.results.drift_reason IS
    'Why the key is DRIFT, set exactly when outcome = DRIFT: changed_since (the change feed shows the key changed during the run), a_changed (side a''s state or unmasked hash changed on recheck), b_converged (only side b changed and no unsuppressed diff remains), watermark_mismatch (a shared watermark still disagrees after the recheck), skew_exceeded (capture skew above time.skewBudget), cut_window (a frozen cut: the key was touched by an event between the two pipelines'' watermarks wm_a and wm_b). DRIFT is impossible only under consistent_cut pitr_cluster with aligned watermarks.';
COMMENT ON COLUMN ds.results.confirmation IS
    'provisional (no recheck yet; a provisional MISMATCH / NOT_FOUND_A / NOT_FOUND_B is counted in n_outstanding and its verdict is NULL) | rechecked (recheck_count >= 1) | frozen_cut (frozen EnvironmentSnapshot: re-reading is a no-op) | bulk_exhaustive (P2 exhaustive extract or anti-join). The last two satisfy reproduction without a recheck.';
COMMENT ON COLUMN ds.results.signoff_member IS
    'Copied from run_keys.signoff_member. v_run_summary computes n_clean, n_discrepant, mismatch_rate and ub95_one_sided_zero_fail over members only (keys without a sign-off label count as members of their plain stratum) and reports the other labelled keys as n_contrast.';
COMMENT ON COLUMN ds.results.first_outcome IS
    'The outcome of the first write for this key and cycle. Set on INSERT (= outcome) and never updated, so a recheck that overwrites outcome keeps the original verdict; the recheck evidence is its own observations (call_seq R1...).';
COMMENT ON COLUMN ds.results.revision IS
    '1 on INSERT, incremented by every overwrite (11.4). Reports are built from a fixed revision time (reports.result_revision_at vs results.updated_at).';
COMMENT ON COLUMN ds.results.key IS
    'Spec key after key.normalize: a business code in clear (key.class business_code, the default), or its token when key.class is an identifier class.';
COMMENT ON COLUMN ds.results.stratum IS
    'Stratum label: dim=label pairs joined by | (e.g. degree_bucket=d51_250|id_type=BANK_ACCOUNT|flag_status=FLAGGED|recency=gt_90d).';
COMMENT ON COLUMN ds.results.signoff_stratum IS
    'The single sign-off stratum label from spec key.source.signoffStratum (high-risk takes precedence), copied from run_keys.signoff_stratum. When set, v_run_summary groups its per-stratum rows by it, so the sign-off bound is per sign-off stratum (the 12 + 3 strata of architecture.md §15), not per cross-product cell.';
COMMENT ON COLUMN ds.run_keys.signoff_stratum IS
    'Sign-off stratum label assigned at key materialisation by key.source.signoffStratum (bottom-k sampling targets are per this label).';
COMMENT ON COLUMN ds.results.attrs IS
    'Coarse, non-identifying per-key attributes. Flags: api_uncomparable (INCONCLUSIVE truncated above the API cap; excluded from the INCONCLUSIVE + DRIFT gate and listed in the report) and the legacy absent_both (v0.2; optional, and if set it must agree with inconclusive_reason = absent_both, which is authoritative).';
COMMENT ON COLUMN ds.results.hash_a IS 'Canonical hash (sha256) of side a after project, map, normalize. NULL when side a has no document (always NULL when side_a_state = ABSENT).';
COMMENT ON COLUMN ds.results.recheck_count IS
    'Rechecks performed for this key and cycle. The upsert only lets a higher recheck_count overwrite a row, so the full recheck (a later recheck work unit) replaces the fast recheck.oneShot confirmation of one-shot and compare_key. Each overwrite increments revision and keeps first_outcome.';
COMMENT ON COLUMN ds.results.obs_a_id IS 'Observation of side a used for the outcome. For a rediff run it points into the parent run''s observations.';
COMMENT ON COLUMN ds.results.worst_severity IS 'Most severe severity among this key''s findings (min() over ds.severity).';

CREATE TABLE ds.results_default PARTITION OF ds.results DEFAULT
    WITH (autovacuum_vacuum_scale_factor = 0.05);

-- 7.3 suppression rules and roll-ups referenced by findings -------------------
CREATE TABLE ds.known_diffs (
    rule_id         text             NOT NULL,
    spec_name       text             NOT NULL,
    when_cel        text             NOT NULL,
    classification  text             NOT NULL,
    reason          text             NOT NULL,
    ticket          text,
    taxonomy_code   text,
    path_pattern    text,
    approved_by     text,
    approved_at     timestamptz,
    active          boolean          NOT NULL DEFAULT false,
    expires_at      timestamptz,
    created_by      text             NOT NULL,
    created_at      timestamptz      NOT NULL DEFAULT now(),
    updated_at      timestamptz      NOT NULL DEFAULT now(),
    deactivated_by  text,
    deactivated_at  timestamptz,
    CONSTRAINT known_diffs_pk PRIMARY KEY (rule_id),
    CONSTRAINT known_diffs_taxonomy_fk FOREIGN KEY (taxonomy_code) REFERENCES ds.taxonomy_codes (code),
    CONSTRAINT known_diffs_rule_id_format CHECK (rule_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    CONSTRAINT known_diffs_classification_chk CHECK (
        classification IN ('accepted', 'bug', 'expected_by_design')),
    CONSTRAINT known_diffs_when_cel_chk CHECK (length(btrim(when_cel)) BETWEEN 1 AND 4096),
    -- RFC 9535 wildcard form, e.g. $.status or $.links[*].hop
    CONSTRAINT known_diffs_path_pattern_format CHECK (
        path_pattern IS NULL OR path_pattern LIKE '$%'),
    CONSTRAINT known_diffs_approval_pair CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
    CONSTRAINT known_diffs_active_requires_approval CHECK (NOT active OR approved_at IS NOT NULL),
    CONSTRAINT known_diffs_four_eyes CHECK (approved_by IS NULL OR approved_by <> created_by),
    CONSTRAINT known_diffs_bug_needs_ticket CHECK (classification <> 'bug' OR ticket IS NOT NULL),
    CONSTRAINT known_diffs_deactivation_pair CHECK ((deactivated_by IS NULL) = (deactivated_at IS NULL)),
    -- a deactivated rule no longer suppresses
    CONSTRAINT known_diffs_deactivated_inactive CHECK (deactivated_at IS NULL OR NOT active)
);
COMMENT ON TABLE ds.known_diffs IS
    'Approved known-difference rules (spec knownDiffs[]). A matching finding is suppressed and the key outcome becomes KNOWN_DIFF. A rule suppresses only while active; activation requires approval by someone other than the author. An approved rule is immutable (trigger known_diffs_immutable): only activation and a final, recorded deactivation are allowed, and a change (e.g. reclassification) is a new rule_id with a new approval. ds-comparer matches bug rules first, then the others in rule_id order. Reports and sign-offs snapshot the classifications of the rules they cite at result_revision_at. Rules are deactivated, never deleted.';
COMMENT ON COLUMN ds.known_diffs.when_cel IS
    'CEL predicate evaluated per finding by ds-comparer. Its path variable is path_pattern in RFC 9535 form (path == ''$.status''); stratum is the stored label and strata the parsed map.';
COMMENT ON COLUMN ds.known_diffs.classification IS
    'accepted / expected_by_design explain a difference. bug is a known open defect: its KNOWN_DIFF keys still count as discrepant in sign-off (v_run_summary.n_discrepant), and its CRITICAL findings stay unexplained.';

CREATE TABLE ds.noise_masks (
    mask_id             bigint        GENERATED ALWAYS AS IDENTITY,
    spec_name           text          NOT NULL,
    path_pattern        text          NOT NULL,
    side                ds.side,
    noise_rate          numeric(7,6),
    sample_size         integer,
    calibration_run_id  bigint,
    status              text          NOT NULL DEFAULT 'proposed',
    blocked_reason      text,
    proposed_by         text          NOT NULL DEFAULT 'ds-scheduler',
    approved_by         text,
    approved_at         timestamptz,
    rejected_by         text,
    rejected_at         timestamptz,
    review_note         text,
    expires_at          timestamptz,
    created_at          timestamptz   NOT NULL DEFAULT now(),
    updated_at          timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT noise_masks_pk PRIMARY KEY (mask_id),
    CONSTRAINT noise_masks_calibration_run_fk FOREIGN KEY (calibration_run_id)
        REFERENCES ds.runs (run_id) ON DELETE SET NULL,
    CONSTRAINT noise_masks_status_chk CHECK (status IN ('proposed', 'approved', 'rejected')),
    CONSTRAINT noise_masks_rate_chk CHECK (noise_rate IS NULL OR (noise_rate >= 0 AND noise_rate <= 1)),
    -- a mask measured by a calibration run carries its measured rate
    CONSTRAINT noise_masks_calibration_has_rate CHECK (calibration_run_id IS NULL OR noise_rate IS NOT NULL),
    CONSTRAINT noise_masks_sample_size_chk CHECK (sample_size IS NULL OR sample_size > 0),
    -- RFC 9535 wildcard form, e.g. $.lastEvaluatedAt
    CONSTRAINT noise_masks_path_pattern_format CHECK (path_pattern LIKE '$%'),
    CONSTRAINT noise_masks_approval_pair CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
    CONSTRAINT noise_masks_rejection_pair CHECK ((rejected_by IS NULL) = (rejected_at IS NULL)),
    CONSTRAINT noise_masks_status_fields CHECK (
        (status = 'approved') = (approved_at IS NOT NULL)
        AND (status = 'rejected') = (rejected_at IS NOT NULL)),
    -- four-eyes: the approver is not the proposer (and, by trigger, not a spec author)
    CONSTRAINT noise_masks_four_eyes CHECK (approved_by IS NULL OR approved_by <> proposed_by),
    -- raw A-vs-B rate significantly above the noise rate: a real defect, never masked
    CONSTRAINT noise_masks_blocked_not_approved CHECK (blocked_reason IS NULL OR status <> 'approved'),
    CONSTRAINT noise_masks_expiry_after_approval CHECK (
        expires_at IS NULL OR approved_at IS NULL OR expires_at > approved_at)
);
COMMENT ON TABLE ds.noise_masks IS
    'Noise masks. Calibration runs propose one per path only if its noise rate >= noise.calibration.maskThreshold (A called twice, B called 1 + repeatB times per key). Spec noise.masks[] are mirrored here (spec_name = metadata.name, path_pattern = path, side both -> NULL; the spec maskId is mask_id). Approval is a rule_approver action with four-eyes: the approver is neither the proposer nor an author of the spec. Only approved, unblocked, unexpired masks suppress; a key with both sides COMPLETE whose findings above INFO are all suppressed, at least one by an approved mask and none by a known_diffs rule, is NOISE_ONLY.';
COMMENT ON COLUMN ds.noise_masks.side IS 'Side on which the noise was observed. NULL = both sides (spec side both).';
COMMENT ON COLUMN ds.noise_masks.noise_rate IS 'Measured noise rate on this path. NULL only for a spec mask that no calibration run measured.';
COMMENT ON COLUMN ds.noise_masks.blocked_reason IS
    'Set by calibration when the one-sided 95% Clopper-Pearson lower bound of the raw A-vs-B rate exceeds the upper bound of the noise rate. A blocked mask cannot be approved.';
COMMENT ON COLUMN ds.noise_masks.expires_at IS 'The mask stops suppressing at this time; re-calibrate and re-approve to extend.';

CREATE TABLE ds.clusters (
    cluster_id           bigint       GENERATED ALWAYS AS IDENTITY,
    run_id               bigint       NOT NULL,
    method               text         NOT NULL,
    member_signatures    text[]       NOT NULL,
    label                text,
    hypothesis           text,
    suspected_component  text,
    confidence           numeric(4,3),
    label_model          text,
    prompt_version       text,
    review_state         text         NOT NULL DEFAULT 'pending',
    reviewed_by          text,
    reviewed_at          timestamptz,
    finding_count        bigint       NOT NULL DEFAULT 0,
    key_count            bigint       NOT NULL DEFAULT 0,
    params               jsonb        NOT NULL DEFAULT '{}',
    created_at           timestamptz  NOT NULL DEFAULT now(),
    updated_at           timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT clusters_pk PRIMARY KEY (cluster_id),
    CONSTRAINT clusters_run_fk FOREIGN KEY (run_id) REFERENCES ds.runs (run_id) ON DELETE CASCADE,
    CONSTRAINT clusters_method_chk CHECK (method IN ('signature', 'fingerprint', 'hdbscan')),
    -- DDL-local vocabulary
    CONSTRAINT clusters_review_state_chk CHECK (review_state IN ('pending', 'accepted', 'edited', 'rejected')),
    CONSTRAINT clusters_members_nonempty CHECK (cardinality(member_signatures) >= 1),
    CONSTRAINT clusters_confidence_chk CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    CONSTRAINT clusters_model_needs_prompt CHECK (label_model IS NULL OR prompt_version IS NOT NULL),
    CONSTRAINT clusters_review_pair CHECK ((reviewed_by IS NULL) = (reviewed_at IS NULL)),
    CONSTRAINT clusters_reviewed_unless_pending CHECK ((review_state = 'pending') = (reviewed_at IS NULL)),
    CONSTRAINT clusters_counts_chk CHECK (finding_count >= 0 AND key_count >= 0),
    CONSTRAINT clusters_params_object CHECK (jsonb_typeof(params) = 'object')
);
COMMENT ON TABLE ds.clusters IS
    'Groups of signatures (method signature | fingerprint | hdbscan). label and hypothesis may be LLM-generated (label_model, prompt_version); review_state records human review.';
COMMENT ON COLUMN ds.clusters.member_signatures IS 'signature_hash values of ds.signatures rows in the same run.';

CREATE TABLE ds.root_causes (
    root_cause_id   bigint            GENERATED ALWAYS AS IDENTITY,
    run_id          bigint            NOT NULL,
    kind            text              NOT NULL,
    root_key_tok    text,
    path_pattern    text,
    change_type     ds.change_type,
    taxonomy_code   text,
    severity        ds.severity,
    direction       ds.finding_direction,
    signature_hash  ds.hex_digest,
    rule_id         text,
    cluster_id      bigint,
    affected_keys   bigint            NOT NULL DEFAULT 0,
    finding_count   bigint            NOT NULL DEFAULT 0,
    sample_keys     text[]            NOT NULL DEFAULT '{}',
    summary         text,
    evidence        jsonb             NOT NULL DEFAULT '{}',
    status          text              NOT NULL DEFAULT 'open',
    created_at      timestamptz       NOT NULL DEFAULT now(),
    updated_at      timestamptz       NOT NULL DEFAULT now(),
    CONSTRAINT root_causes_pk PRIMARY KEY (root_cause_id),
    CONSTRAINT root_causes_run_fk FOREIGN KEY (run_id) REFERENCES ds.runs (run_id) ON DELETE CASCADE,
    CONSTRAINT root_causes_rule_fk FOREIGN KEY (rule_id) REFERENCES ds.known_diffs (rule_id),
    CONSTRAINT root_causes_taxonomy_fk FOREIGN KEY (taxonomy_code) REFERENCES ds.taxonomy_codes (code),
    CONSTRAINT root_causes_cluster_fk FOREIGN KEY (cluster_id) REFERENCES ds.clusters (cluster_id) ON DELETE SET NULL,
    -- idempotent recomputation: one row per natural key within a run. path_pattern
    -- and change_type separate member_key groups that share a member key token
    -- (NULL for the other kinds, so their natural key is unchanged).
    CONSTRAINT root_causes_natural_uq UNIQUE NULLS NOT DISTINCT
        (run_id, kind, root_key_tok, signature_hash, rule_id, path_pattern, change_type),
    -- member_key is the generic, domain-agnostic fan-out; graph_anchor
    -- groups a graph's node and edge findings by findings.anchor_key_tok
    -- across $.nodes.* and $.edges.*; identifier is the optional domain-pack
    -- enrichment (link provenance is never assumed)
    CONSTRAINT root_causes_kind_chk CHECK (kind IN ('member_key', 'graph_anchor', 'identifier', 'attachment', 'rule', 'signature')),
    CONSTRAINT root_causes_kind_identifier CHECK (kind NOT IN ('identifier', 'attachment') OR root_key_tok IS NOT NULL),
    -- (coalesce: a NULL change_type must fail the CHECK, not pass it as unknown)
    CONSTRAINT root_causes_kind_member_key CHECK (
        kind <> 'member_key'
        OR (root_key_tok IS NOT NULL AND path_pattern IS NOT NULL
            AND coalesce(change_type IN ('missing_in_b', 'extra_in_b'), false))),
    -- one cause per (anchor node, direction); it spans node and edge paths, so no path_pattern
    CONSTRAINT root_causes_kind_graph_anchor CHECK (
        kind <> 'graph_anchor'
        OR (root_key_tok IS NOT NULL AND path_pattern IS NULL
            AND coalesce(change_type IN ('missing_in_b', 'extra_in_b'), false))),
    CONSTRAINT root_causes_kind_rule CHECK (kind <> 'rule' OR rule_id IS NOT NULL),
    CONSTRAINT root_causes_kind_signature CHECK (kind <> 'signature' OR signature_hash IS NOT NULL),
    CONSTRAINT root_causes_path_pattern_format CHECK (path_pattern IS NULL OR path_pattern LIKE '$%'),
    -- DDL-local vocabulary
    CONSTRAINT root_causes_status_chk CHECK (status IN ('open', 'acknowledged', 'resolved', 'wont_fix')),
    CONSTRAINT root_causes_counts_chk CHECK (affected_keys >= 0 AND finding_count >= 0),
    CONSTRAINT root_causes_sample_keys_cap CHECK (cardinality(sample_keys) <= 20),
    CONSTRAINT root_causes_evidence_object CHECK (jsonb_typeof(evidence) = 'object')
);
COMMENT ON TABLE ds.root_causes IS
    'Root causes that explain many findings; INFO findings are never grouped. kind member_key (generic): the same array member or graph node/edge missing or extra under >= 2 request keys, grouped by (path_pattern, member_key_tok, change_type); no provenance needed. kind graph_anchor: a one-sided neighbour''s node finding and edge findings grouped by (findings.anchor_key_tok, change_type) across $.nodes.* and $.edges.*; root_key_tok = the anchor node key, path_pattern NULL. kind identifier: optional domain-pack enrichment grouping link findings by (link_reason, explaining_id_tok) when a side exposes provenance. Also an attachment, a known-diff rule or a signature.';
COMMENT ON COLUMN ds.root_causes.root_key_tok IS
    'kind member_key: the findings.member_key_tok shared by the grouped findings (a token when the member key class is tokenised, otherwise the clear business code). kind graph_anchor: the shared findings.anchor_key_tok. kind identifier / attachment: the tokenised identifier or attachment key (tok:<class>:<kv>:<26 base32>), for identifier the findings.explaining_id_tok of the grouped MISSING_LINK / EXTRA_LINK findings, with the identifier type in evidence.id_type. Never a raw value of a tokenised class.';
COMMENT ON COLUMN ds.root_causes.path_pattern IS
    'kind member_key: the array (or graph node/edge set) path_pattern the member belongs to, e.g. $.links[*]. NULL for the other kinds.';
COMMENT ON COLUMN ds.root_causes.change_type IS
    'kind member_key and graph_anchor: missing_in_b or extra_in_b (one root cause per direction). NULL for the other kinds.';
COMMENT ON COLUMN ds.root_causes.direction IS
    'a_only if every grouped finding is a_only, b_only if every one is b_only, otherwise both.';
COMMENT ON COLUMN ds.root_causes.affected_keys IS 'Number of distinct keys in the run that this root cause explains.';
COMMENT ON COLUMN ds.root_causes.evidence IS 'Structured evidence (counts, hop histograms, tokens). Writers must not put raw values here; it is exposed through v_root_causes.';

CREATE TABLE ds.signatures (
    run_id              bigint            NOT NULL,
    signature_hash      ds.hex_digest     NOT NULL,
    path_pattern        text              NOT NULL,
    change_type         ds.change_type    NOT NULL,
    value_class         text,
    link_reason         text,
    hop_count           smallint,
    taxonomy_code       text,
    worst_severity      ds.severity,
    finding_count       bigint            NOT NULL DEFAULT 0,
    key_count           bigint            NOT NULL DEFAULT 0,
    suppressed_count    bigint            NOT NULL DEFAULT 0,
    sample_finding_ids  bigint[]          NOT NULL DEFAULT '{}',
    first_cycle_no      integer,
    last_cycle_no       integer,
    computed_at         timestamptz       NOT NULL DEFAULT now(),
    CONSTRAINT signatures_pk PRIMARY KEY (run_id, signature_hash),
    CONSTRAINT signatures_run_fk FOREIGN KEY (run_id) REFERENCES ds.runs (run_id) ON DELETE CASCADE,
    CONSTRAINT signatures_taxonomy_fk FOREIGN KEY (taxonomy_code) REFERENCES ds.taxonomy_codes (code),
    CONSTRAINT signatures_path_pattern_format CHECK (path_pattern LIKE '$%'),
    CONSTRAINT signatures_hop_count_chk CHECK (hop_count IS NULL OR hop_count >= 0),
    CONSTRAINT signatures_counts_chk CHECK (
        finding_count >= 0 AND key_count >= 0 AND key_count <= finding_count
        AND suppressed_count >= 0 AND suppressed_count <= finding_count),
    CONSTRAINT signatures_sample_cap CHECK (cardinality(sample_finding_ids) <= 20),
    CONSTRAINT signatures_cycle_order CHECK (
        first_cycle_no IS NULL OR last_cycle_no IS NULL OR first_cycle_no <= last_cycle_no)
);
COMMENT ON TABLE ds.signatures IS
    'Per-run roll-up of findings by signature (path_pattern, change_type, value_class, plus the optional domain-pack enrichment link_reason and hop_count, NULL when no pack supplies them). signature_hash does not include run_id, so compare_runs can match signatures across runs. The roll-up is derived and refreshed by aggregation, so findings has no FK to it and the finding write path never serialises on signature rows.';

-- 7.4 findings ------------------------------------------------------------------
CREATE TABLE ds.findings (
    finding_id       bigint                NOT NULL DEFAULT nextval('ds.findings_finding_id_seq'),
    run_id           bigint                NOT NULL,
    cycle_no         integer               NOT NULL,
    key              text                  NOT NULL,
    path_pattern     text                  NOT NULL,
    json_pointer     text                  NOT NULL,
    change_type      ds.change_type        NOT NULL,
    direction        ds.finding_direction  NOT NULL,
    value_a_tok      text,
    value_b_tok      text,
    value_class      text,
    value_diff_desc  text,
    severity         ds.severity           NOT NULL,
    taxonomy_code    text                  NOT NULL,
    signature_hash   ds.hex_digest         NOT NULL,
    root_cause_id    bigint,
    rule_id          text,
    mask_id          bigint,
    suppressed       boolean               NOT NULL DEFAULT false,
    member_key_tok   text,
    anchor_key_tok   text,
    link_reason      text,
    explaining_id_tok text,
    hop_count        smallint,
    stratum          text,
    created_at       timestamptz           NOT NULL DEFAULT now(),
    CONSTRAINT findings_pk PRIMARY KEY (run_id, finding_id),
    -- idempotent replace of a key's findings (section 11.5)
    CONSTRAINT findings_natural_uq UNIQUE (run_id, cycle_no, key, json_pointer, change_type),
    CONSTRAINT findings_run_fk FOREIGN KEY (run_id) REFERENCES ds.runs (run_id),
    CONSTRAINT findings_root_cause_fk FOREIGN KEY (root_cause_id)
        REFERENCES ds.root_causes (root_cause_id) ON DELETE SET NULL,
    CONSTRAINT findings_rule_fk FOREIGN KEY (rule_id) REFERENCES ds.known_diffs (rule_id),
    CONSTRAINT findings_mask_fk FOREIGN KEY (mask_id) REFERENCES ds.noise_masks (mask_id),
    CONSTRAINT findings_taxonomy_fk FOREIGN KEY (taxonomy_code) REFERENCES ds.taxonomy_codes (code),
    CONSTRAINT findings_cycle_chk CHECK (cycle_no >= 1),
    CONSTRAINT findings_key_len CHECK (key <> '' AND octet_length(key) <= 512),
    -- RFC 6901: '' is the whole document, otherwise the pointer starts with '/'
    CONSTRAINT findings_json_pointer_format CHECK (json_pointer = '' OR json_pointer LIKE '/%'),
    -- RFC 9535 wildcard form: '$' is the whole document, e.g. $.links[*].hop
    CONSTRAINT findings_path_pattern_format CHECK (path_pattern LIKE '$%'),
    -- keeps findings_natural_uq and findings_run_path_change_idx entries well under the btree row limit
    CONSTRAINT findings_pointer_size CHECK (
        octet_length(json_pointer) <= 1024 AND octet_length(path_pattern) <= 512),
    -- direction follows change_type: missing_in_b = present in A only, extra_in_b = present in B only
    CONSTRAINT findings_direction_matches_change_type CHECK (
        CASE change_type
            WHEN 'missing_in_b' THEN direction = 'a_only'
            WHEN 'extra_in_b'   THEN direction = 'b_only'
            ELSE direction = 'both'
        END),
    -- MISSING_* = present in A, absent in B. EXTRA_* = present in B, absent in A.
    -- Enforced per code by trigger findings_taxonomy_presence (ds.taxonomy_codes.presence).
    CONSTRAINT findings_normalization_only_is_info CHECK (
        taxonomy_code <> 'NORMALIZATION_ONLY' OR severity = 'INFO'),
    -- schema drift is reported, never a reason for MISMATCH
    CONSTRAINT findings_schema_drift_is_info CHECK (
        taxonomy_code <> 'SCHEMA_DRIFT' OR severity = 'INFO'),
    -- the member key of a set/keyed array member or graph element (token or clear business code)
    CONSTRAINT findings_member_key_size CHECK (
        member_key_tok IS NULL OR (member_key_tok <> '' AND octet_length(member_key_tok) <= 512)),
    -- the core document-level codes sit at the document root and name the key itself
    CONSTRAINT findings_document_level_shape CHECK (
        taxonomy_code NOT IN ('MISSING_DOCUMENT', 'EXTRA_DOCUMENT')
        OR (path_pattern = '$' AND json_pointer = '' AND member_key_tok IS NOT DISTINCT FROM key)),
    -- the anchor node key of a graph node or edge finding (graph/v1 {nodes, edges})
    CONSTRAINT findings_anchor_key_size CHECK (
        anchor_key_tok IS NULL OR (anchor_key_tok <> '' AND octet_length(anchor_key_tok) <= 512)),
    CONSTRAINT findings_anchor_key_graph_only CHECK (
        anchor_key_tok IS NULL OR path_pattern ~ '^\$\.(nodes|edges)(\.|\[|$)'),
    -- a suppressed finding must cite the rule or mask that suppressed it
    CONSTRAINT findings_suppression_cited CHECK (NOT suppressed OR rule_id IS NOT NULL OR mask_id IS NOT NULL),
    CONSTRAINT findings_hop_count_chk CHECK (hop_count IS NULL OR hop_count >= 0),
    CONSTRAINT findings_stratum_format CHECK (stratum ~ '^[A-Za-z0-9][A-Za-z0-9_.:=|+-]{0,255}$'),
    CONSTRAINT findings_value_size CHECK (
        coalesce(octet_length(value_a_tok), 0) <= 4096
        AND coalesce(octet_length(value_b_tok), 0) <= 4096
        AND coalesce(octet_length(value_diff_desc), 0) <= 4096),
    -- always a token: tok:<class>:<kv>:<first 26 base32 characters of the HMAC>
    CONSTRAINT findings_explaining_id_tok_format CHECK (
        explaining_id_tok IS NULL
        OR explaining_id_tok ~ '^tok:[a-z][a-z0-9_]*:[A-Za-z0-9]+:[A-Z2-7]{26}$')
) PARTITION BY LIST (run_id);
COMMENT ON TABLE ds.findings IS
    'One row per difference within a key''s documents. Partitioned by LIST (run_id). Findings for (run_id, cycle_no, key) are replaced in the same transaction as the results upsert.';
COMMENT ON COLUMN ds.findings.path_pattern IS
    'Normalised RFC 9535 path with every array index or member key replaced by [*], e.g. $.links[*].hop. The CEL path variable. Safe to expose.';
COMMENT ON COLUMN ds.findings.json_pointer IS
    'Exact RFC 6901 pointer. Member keys of keyed or set arrays whose class is tokenised appear as their tokens (e.g. /phones/tok:phone:v1:ABCD...), never as raw values. Grammar: each member key is RFC 6901-escaped (~ -> ~0, / -> ~1); the index of a duplicate member is its own segment @<i> (e.g. /phones/<tok>/@1); segments starting with @ are reserved, so a member key that starts with @ is written with a doubled @@. The raw pointer is re-derived only through the audited reveal endpoint. Not exposed by v_findings_masked.';
COMMENT ON COLUMN ds.findings.value_a_tok IS
    'Side a value after the deployment PII floor and spec security.tokenize / security.clear: a token (tok:<class>:<kv>:<26 base32>) for tokenised classes, the clear value otherwise. NULL for date_of_birth (value_class only). Never a raw value of a tokenised class.';
COMMENT ON COLUMN ds.findings.value_class IS
    'Class of the differing value (e.g. phone, date_of_birth), or a marker such as duplicate_key (surplus member of a keyed array) or intermittent_b (A unchanged while B changed on recheck and an unsuppressed diff remains; counted as MISMATCH).';
COMMENT ON COLUMN ds.findings.member_key_tok IS
    'Key of the array member (keyed or set array) or graph node/edge the finding is about, tokenised when its class is tokenised, otherwise the clear business code; NULL for plain field findings. For MISSING_DOCUMENT / EXTRA_DOCUMENT it is the key itself. The generic root-causer groups non-INFO missing_in_b / extra_in_b findings by (path_pattern, member_key_tok, change_type): the same member under >= 2 request keys is one root cause of kind member_key.';
COMMENT ON COLUMN ds.findings.anchor_key_tok IS
    'Graph mode only: the node key a node finding is about, or for an edge finding the far-end node key (the endpoint that is not the request root). The root-causer groups non-INFO graph findings by (anchor_key_tok, change_type) across $.nodes.* and $.edges.*, so a one-sided neighbour''s node and edge findings form one root cause of kind graph_anchor.';
COMMENT ON COLUMN ds.findings.link_reason IS
    'OPTIONAL domain-pack enrichment: identifier TYPE that explains a link finding (e.g. PHONE), from side a provenance or the explainer. NULL when no pack supplies it; the core never requires it.';
COMMENT ON COLUMN ds.findings.explaining_id_tok IS
    'OPTIONAL domain-pack enrichment: token of the identifier VALUE that explains a link finding (side a provenance, or the explainer for EXTRA_LINK). Root causes of kind identifier group findings by (link_reason, explaining_id_tok); the generic root cause is kind member_key.';
COMMENT ON COLUMN ds.findings.value_diff_desc IS 'Short machine-written description of the difference (e.g. "case differs", "delta 0.01"). Must not quote raw values.';
COMMENT ON COLUMN ds.findings.stratum IS 'Denormalised from results.stratum for per-stratum sign-off and contrast queries.';

CREATE TABLE ds.findings_default PARTITION OF ds.findings DEFAULT
    WITH (autovacuum_vacuum_scale_factor = 0.05);

-- =============================================================================
-- 8. Reports, chat, audit, users
-- =============================================================================
CREATE TABLE ds.reports (
    report_id      bigint        GENERATED ALWAYS AS IDENTITY,
    run_id         bigint        NOT NULL,
    format         text          NOT NULL,
    status         text          NOT NULL DEFAULT 'queued',
    params         jsonb         NOT NULL DEFAULT '{}',
    masked         boolean       NOT NULL DEFAULT true,
    blob_uri       text,
    content_hash   ds.sha256_hex,
    bytes          bigint,
    requested_by   text          NOT NULL,
    requested_at   timestamptz   NOT NULL DEFAULT now(),
    unmasked_approved_by text,
    unmasked_approved_at timestamptz,
    unmasked_audit_id    bigint,
    result_revision_at timestamptz,
    snapshot_id    text,
    started_at     timestamptz,
    completed_at   timestamptz,
    expires_at     timestamptz,
    error_message  text,
    updated_at     timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT reports_pk PRIMARY KEY (report_id),
    CONSTRAINT reports_run_fk FOREIGN KEY (run_id) REFERENCES ds.runs (run_id) ON DELETE CASCADE,
    CONSTRAINT reports_format_chk CHECK (format IN ('csv', 'parquet', 'xlsx', 'pdf')),
    -- DDL-local vocabulary
    CONSTRAINT reports_status_chk CHECK (status IN ('queued', 'rendering', 'ready', 'failed', 'expired')),
    CONSTRAINT reports_ready_has_blob CHECK (
        status <> 'ready' OR (blob_uri IS NOT NULL AND content_hash IS NOT NULL AND completed_at IS NOT NULL)),
    -- every report is built from a fixed result revision, all of
    -- its parts from one exported snapshot
    CONSTRAINT reports_ready_has_revision CHECK (
        status <> 'ready' OR (result_revision_at IS NOT NULL AND snapshot_id IS NOT NULL)),
    -- pg_export_snapshot() identifier
    CONSTRAINT reports_snapshot_id_format CHECK (snapshot_id IS NULL OR snapshot_id ~ '^[0-9A-F]{8}-[0-9A-F]{8}-[0-9]+$'),
    -- an unmasked report is rendered only after a second person approved
    -- it and the approval was audited (ds-api checks pii_reader / rule_approver;
    -- the FK reports_unmasked_audit_fk to ds.audit_log is added after that table)
    CONSTRAINT reports_unmasked_approval_pair CHECK (
        (unmasked_approved_by IS NULL) = (unmasked_approved_at IS NULL)
        AND (unmasked_approved_by IS NULL) = (unmasked_audit_id IS NULL)),
    CONSTRAINT reports_unmasked_approval_only_unmasked CHECK (masked = false OR unmasked_approved_by IS NULL),
    CONSTRAINT reports_unmasked_four_eyes CHECK (
        masked OR status IN ('queued', 'failed', 'expired')
        OR (unmasked_approved_by IS NOT NULL AND unmasked_approved_by <> requested_by)),
    CONSTRAINT reports_revision_before_completion CHECK (
        result_revision_at IS NULL OR completed_at IS NULL OR result_revision_at <= completed_at),
    CONSTRAINT reports_blob_uri_format CHECK (blob_uri IS NULL OR blob_uri ~ '^s3://[^[:space:]]+$'),
    CONSTRAINT reports_bytes_chk CHECK (bytes IS NULL OR bytes >= 0),
    CONSTRAINT reports_params_object CHECK (jsonb_typeof(params) = 'object')
);
COMMENT ON TABLE ds.reports IS
    'Reports rendered by ds-reporter (CSV/Parquet/XLSX/PDF) into the object store. Requested through POST /v1/reports (Idempotency-Key) and rendered from committed outbox events. masked = false needs a requester with pii_reader and an approval by another person (rule_approver) recorded with its audit row (unmasked_approved_by/_at, unmasked_audit_id, action report.approve_unmasked) before rendering starts; every download of it is audited (report.download_unmasked). Reports snapshot the classifications of the known_diffs rules they cite. CSV/XLSX escaping never quote-prefixes a numeric cell (a leading - or + on ^[-+]?\d+(\.\d+)?([eE][-+]?\d+)?$ is left as is).';
COMMENT ON COLUMN ds.reports.result_revision_at IS
    'The fixed result revision the report was built from: the commit timestamp of its snapshot (snapshot_id). Staleness is detected by commit order, not by results.updated_at: a result whose pg_xact_commit_timestamp(xmin) is later than result_revision_at makes the report stale, and ds-reporter labels it so (needs track_commit_timestamp = on; reference query 11.10). Required once status = ready.';
COMMENT ON COLUMN ds.reports.snapshot_id IS
    'The pg_export_snapshot() identifier of the single REPEATABLE READ snapshot that every part of the report was rendered from (the workers SET TRANSACTION SNAPSHOT to it). Required once status = ready.';

CREATE TABLE ds.chat_sessions (
    session_id      uuid         NOT NULL DEFAULT gen_random_uuid(),
    user_id         text         NOT NULL,
    run_id          bigint,
    scope_key       text,
    title           text,
    status          text         NOT NULL DEFAULT 'open',
    created_at      timestamptz  NOT NULL DEFAULT now(),
    last_active_at  timestamptz  NOT NULL DEFAULT now(),
    closed_at       timestamptz,
    CONSTRAINT chat_sessions_pk PRIMARY KEY (session_id),
    CONSTRAINT chat_sessions_run_fk FOREIGN KEY (run_id) REFERENCES ds.runs (run_id) ON DELETE SET NULL,
    CONSTRAINT chat_sessions_scope_key_chk CHECK (
        scope_key IS NULL OR (scope_key <> '' AND octet_length(scope_key) <= 512)),
    -- DDL-local vocabulary
    CONSTRAINT chat_sessions_status_chk CHECK (status IN ('open', 'closed')),
    CONSTRAINT chat_sessions_closed_pair CHECK ((status = 'closed') = (closed_at IS NOT NULL))
);
COMMENT ON TABLE ds.chat_sessions IS
    'Investigation chat sessions (ds-agent). A session is scoped to a run (run_id), a key (scope_key), or a key within a run (both). ds_app cannot DELETE sessions (that would cascade to the audited turns); retention runs as ds_owner.';
COMMENT ON COLUMN ds.chat_sessions.scope_key IS 'Key the session is scoped to, if any (same form as results.key).';

CREATE TABLE ds.chat_turns (
    turn_id           bigint       GENERATED ALWAYS AS IDENTITY,
    session_id        uuid         NOT NULL,
    turn_no           integer      NOT NULL,
    user_id           text         NOT NULL,
    question          text         NOT NULL,
    tool_calls        jsonb        NOT NULL DEFAULT '[]',
    answer            jsonb,
    citations         jsonb        NOT NULL DEFAULT '[]',
    model_reported    text,
    prompt_version    text         NOT NULL,
    profile_id        text,
    adapter_version   text,
    capability_report_hash ds.sha256_hex,
    generation_params jsonb        NOT NULL DEFAULT '{}',
    validator_status  text         NOT NULL DEFAULT 'pending',
    validator_errors  jsonb,
    input_tokens      integer,
    output_tokens     integer,
    latency_ms        integer,
    created_at        timestamptz  NOT NULL DEFAULT now(),
    completed_at      timestamptz,
    CONSTRAINT chat_turns_pk PRIMARY KEY (turn_id),
    CONSTRAINT chat_turns_session_fk FOREIGN KEY (session_id)
        REFERENCES ds.chat_sessions (session_id) ON DELETE CASCADE,
    CONSTRAINT chat_turns_profile_fk FOREIGN KEY (profile_id) REFERENCES ds.llm_profiles (profile_id),
    -- a turn served by a profile records the rest of the promotion tuple
    CONSTRAINT chat_turns_profile_tuple CHECK (profile_id IS NULL OR adapter_version IS NOT NULL),
    CONSTRAINT chat_turns_profile_model_reported CHECK (profile_id IS NULL OR model_reported IS NOT NULL),
    CONSTRAINT chat_turns_adapter_version_len CHECK (adapter_version IS NULL OR length(adapter_version) BETWEEN 1 AND 64),
    CONSTRAINT chat_turns_session_turn_uq UNIQUE (session_id, turn_no),
    CONSTRAINT chat_turns_turn_no_chk CHECK (turn_no >= 1),
    -- DDL-local vocabulary
    CONSTRAINT chat_turns_validator_status_chk CHECK (
        validator_status IN ('pending', 'passed', 'repaired', 'failed')),
    CONSTRAINT chat_turns_json_shapes CHECK (
        jsonb_typeof(tool_calls) = 'array'
        AND jsonb_typeof(citations) = 'array'
        AND (answer IS NULL OR jsonb_typeof(answer) = 'object')
        AND jsonb_typeof(generation_params) = 'object'),
    CONSTRAINT chat_turns_model_reported_len CHECK (model_reported IS NULL OR length(model_reported) BETWEEN 1 AND 200),
    CONSTRAINT chat_turns_metrics_chk CHECK (
        coalesce(input_tokens, 0) >= 0 AND coalesce(output_tokens, 0) >= 0 AND coalesce(latency_ms, 0) >= 0)
);
COMMENT ON TABLE ds.chat_turns IS
    'One question/answer turn: the tool calls made (MCP tool name, arguments, row counts), the structured answer, citations (finding_id, root_cause_id, obs_id, run_id), and the model and prompt versions used. validator_status is the result of the citation/answer validator. For every turn ds-agent also writes an audit_log row (action chat.turn, details {turn_id, sha256 of tool_calls and of answer}). Per-call usage and cost are in ds.llm_calls (turn_id). ds_app cannot DELETE turns; retention runs as ds_owner.';
COMMENT ON COLUMN ds.chat_turns.profile_id IS
    'LLM profile that served the turn (ds.llm_profiles). With model_reported, prompt_version, adapter_version and capability_report_hash it forms the eval promotion tuple. NULL when AI is disabled or the turn was answered without a model call.';
COMMENT ON COLUMN ds.chat_turns.model_reported IS
    'The model name the provider reported (same meaning as llm_calls.model_reported; replaces v0.3.0 model_digest, which API providers do not expose). Required when profile_id is set; NULL for a turn answered without a model call.';
COMMENT ON COLUMN ds.chat_turns.generation_params IS
    'Sampling and reasoning parameters the turn ran with, e.g. {"effort": "high"} or {"temperature": 0}. Where the model does not support temperature, reproducibility is pinned by (model_reported, prompt_version, effort).';
COMMENT ON COLUMN ds.chat_turns.capability_report_hash IS
    'sha256 of the llm_profiles.capability_report in force for this turn.';

-- One row per LLM call, including calls outside a chat turn (outbox-
-- triggered analysis, cluster labels, the mapping advisor, probes, evals).
-- Usage is normalised to OpenTelemetry GenAI meaning: input_total INCLUDES
-- cache reads and writes, which are also given as sub-counts. Cost is priced
-- from the profile's versioned price table (price_version). ds_app inserts
-- only (the log is not rewritten); ds_agent_ro cannot read it.
CREATE TABLE ds.llm_calls (
    call_id            bigint        GENERATED ALWAYS AS IDENTITY,
    turn_id            bigint,
    run_id             bigint,
    purpose            text          NOT NULL DEFAULT 'chat',
    profile_id         text          NOT NULL,
    dialect            text          NOT NULL,
    model_reported     text,
    input_total        integer,
    input_cache_read   integer,
    input_cache_write  integer,
    input_cache_write_1h integer,
    output_total       integer,
    output_reasoning   integer,
    usage_reported     boolean       NOT NULL,
    cost_micros        bigint,
    currency           text,
    price_version      text,
    latency_ms         integer,
    stop               text          NOT NULL,
    error_class        text,
    mode_used          text,
    created_at         timestamptz   NOT NULL DEFAULT now(),
    CONSTRAINT llm_calls_pk PRIMARY KEY (call_id),
    CONSTRAINT llm_calls_turn_fk FOREIGN KEY (turn_id) REFERENCES ds.chat_turns (turn_id) ON DELETE SET NULL,
    CONSTRAINT llm_calls_run_fk FOREIGN KEY (run_id) REFERENCES ds.runs (run_id) ON DELETE SET NULL,
    CONSTRAINT llm_calls_profile_fk FOREIGN KEY (profile_id) REFERENCES ds.llm_profiles (profile_id),
    -- DDL-local vocabularies
    CONSTRAINT llm_calls_purpose_chk CHECK (purpose IN (
        'chat', 'analysis', 'cluster_label', 'mapping_advisor', 'capability_probe', 'eval')),
    CONSTRAINT llm_calls_dialect_chk CHECK (dialect IN (
        'openai_chat', 'anthropic_messages', 'openai_responses', 'custom_http')),
    -- normalised stop reasons and error taxonomy
    CONSTRAINT llm_calls_stop_chk CHECK (stop IN (
        'END', 'TOOL_USE', 'MAX_TOKENS', 'CONTEXT_EXCEEDED', 'REFUSAL', 'CONTENT_FILTER', 'PAUSE', 'ERROR')),
    CONSTRAINT llm_calls_error_class_chk CHECK (error_class IS NULL OR error_class IN (
        'RateLimited', 'Overloaded', 'Timeout', 'Connection', 'Auth', 'PermissionDenied', 'BadRequest',
        'RequestTooLarge', 'ContextTooLong', 'ContentFiltered', 'ProviderInternal', 'PolicyDenied')),
    CONSTRAINT llm_calls_error_iff_stop_error CHECK ((stop = 'ERROR') = (error_class IS NOT NULL)),
    -- a chat call belongs to a turn
    CONSTRAINT llm_calls_chat_has_turn CHECK (purpose <> 'chat' OR turn_id IS NOT NULL),
    -- usage invariants: counts are non-negative, cache counts are part of
    -- input_total, reasoning tokens are part of output_total
    CONSTRAINT llm_calls_usage_nonneg CHECK (
        coalesce(input_total, 0) >= 0 AND coalesce(input_cache_read, 0) >= 0
        AND coalesce(input_cache_write, 0) >= 0 AND coalesce(input_cache_write_1h, 0) >= 0
        AND coalesce(output_total, 0) >= 0
        AND coalesce(output_reasoning, 0) >= 0 AND coalesce(latency_ms, 0) >= 0),
    CONSTRAINT llm_calls_cache_within_input CHECK (
        input_total IS NULL
        OR coalesce(input_cache_read, 0) + coalesce(input_cache_write, 0) <= input_total),
    -- cache writes split by TTL; the 1-hour writes are a sub-count of input_cache_write
    CONSTRAINT llm_calls_cache_write_1h_within_write CHECK (
        input_cache_write_1h IS NULL OR input_cache_write_1h <= coalesce(input_cache_write, 0)),
    CONSTRAINT llm_calls_reasoning_within_output CHECK (
        output_total IS NULL OR coalesce(output_reasoning, 0) <= output_total),
    CONSTRAINT llm_calls_reported_has_totals CHECK (
        NOT usage_reported OR (input_total IS NOT NULL AND output_total IS NOT NULL)),
    -- a priced call names its currency and price version
    CONSTRAINT llm_calls_cost_priced CHECK (
        cost_micros IS NULL
        OR (cost_micros >= 0 AND currency IS NOT NULL AND price_version IS NOT NULL)),
    CONSTRAINT llm_calls_currency_format CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    CONSTRAINT llm_calls_text_lengths CHECK (
        coalesce(length(model_reported), 0) <= 200 AND coalesce(length(price_version), 0) <= 64
        AND (mode_used IS NULL OR mode_used ~ '^[a-z][a-z0-9_+.-]{0,63}$'))
);
COMMENT ON TABLE ds.llm_calls IS
    'One row per LLM call: turn_id (NULL outside chat), profile_id, dialect, model_reported, usage (input_total includes cache; input_cache_read / input_cache_write and output_reasoning are sub-counts; input_cache_write_1h is the 1-hour-TTL part of input_cache_write, priced with cache_write_1h_mult, the rest with cache_write_mult; usage_reported = false when the endpoint omitted usage), cost_micros in currency priced with price_version, latency_ms, the normalised stop and error_class and mode_used (the structured-output / tool ladder rung). Rolled up per turn, user, run and profile; budgets are checked before each call. Never exposed to ds_agent_ro.';
COMMENT ON COLUMN ds.llm_calls.cost_micros IS
    'Cost in millionths of currency, from the profile''s price table version price_version (a site''s own model endpoint: chargeback rate or 0). Not visible to the agent.';

CREATE TABLE ds.audit_log (
    audit_id     bigint       GENERATED ALWAYS AS IDENTITY,
    occurred_at  timestamptz  NOT NULL DEFAULT clock_timestamp(),
    actor        text         NOT NULL,
    actor_kind   text         NOT NULL,
    action       text         NOT NULL,
    object_type  text,
    object_id    text,
    run_id       bigint,
    outcome      text         NOT NULL DEFAULT 'success',
    request_id   text,
    client_addr  inet,
    db_role      text         NOT NULL DEFAULT session_user,
    details      jsonb        NOT NULL DEFAULT '{}',
    CONSTRAINT audit_log_pk PRIMARY KEY (audit_id),
    -- DDL-local vocabularies
    CONSTRAINT audit_log_actor_kind_chk CHECK (actor_kind IN ('user', 'service', 'agent', 'system')),
    CONSTRAINT audit_log_outcome_chk CHECK (outcome IN ('success', 'denied', 'error')),
    CONSTRAINT audit_log_action_format CHECK (action ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
    CONSTRAINT audit_log_details_object CHECK (jsonb_typeof(details) = 'object')
);
COMMENT ON TABLE ds.audit_log IS
    'Append-only audit trail: run control, compare_key calls (the only MCP tool that calls targets), agent chat turns, known-diff and noise-mask approvals, reveals and unmasked report downloads, target and secrets_ref changes. UPDATE, DELETE and TRUNCATE are revoked, and triggers raise on them. run_id has no FK so the log outlives run retention. details must not contain secrets or raw values.';
COMMENT ON COLUMN ds.audit_log.action IS
    'Dotted verb, e.g. run.start, run.cancel, compare_key.call, chat.turn, known_diff.approve, noise_mask.approve, spec.approve, dataset_target.grant, dataset_target.revoke, pii.reveal, report.approve_unmasked, report.download_unmasked, target.update.';

-- The approval of an unmasked report cites its audit row (audit_log is
-- append-only and never deleted, so the reference cannot dangle).
ALTER TABLE ds.reports
    ADD CONSTRAINT reports_unmasked_audit_fk FOREIGN KEY (unmasked_audit_id) REFERENCES ds.audit_log (audit_id);

CREATE TABLE ds.users_roles (
    user_id     text         NOT NULL,
    role_name   text         NOT NULL,
    source      text         NOT NULL DEFAULT 'oidc',
    oidc_group  text,
    granted_by  text,
    granted_at  timestamptz  NOT NULL DEFAULT now(),
    expires_at  timestamptz,
    CONSTRAINT users_roles_pk PRIMARY KEY (user_id, role_name),
    -- DDL-local vocabularies
    CONSTRAINT users_roles_role_chk CHECK (role_name IN ('viewer', 'operator', 'spec_author', 'rule_approver', 'pii_reader', 'admin')),
    CONSTRAINT users_roles_source_chk CHECK (source IN ('oidc', 'manual')),
    CONSTRAINT users_roles_oidc_group CHECK (source <> 'oidc' OR oidc_group IS NOT NULL),
    CONSTRAINT users_roles_manual_grantor CHECK (source <> 'manual' OR granted_by IS NOT NULL)
);
COMMENT ON TABLE ds.users_roles IS
    'Optional. Cache of UI roles derived from OIDC groups (oauth2-proxy), plus audited manual overrides. OIDC remains the source of truth.';

-- =============================================================================
-- 8b. Transactional outbox and idempotency keys
-- =============================================================================
-- Approved SQL templates and approved inferred-schema bundles.
-- Specs reference them by (id, version); their sha256 is covered by the spec
-- content_hash. Both need four-eyes approval before use, and both must carry
-- the referencing spec's dataset_id (loader rule).
-- =============================================================================
CREATE TABLE ds.sql_templates (
    template_id    text          NOT NULL,
    version        integer       NOT NULL,
    dataset_id     text          NOT NULL DEFAULT 'default' REFERENCES ds.datasets (dataset_id),
    dialect        text          NOT NULL,
    sql_text       text          NOT NULL,
    params_schema  jsonb         NOT NULL DEFAULT '{}',
    sha256         ds.sha256_hex NOT NULL,
    status         text          NOT NULL DEFAULT 'draft',
    created_by     text          NOT NULL,
    created_at     timestamptz   NOT NULL DEFAULT now(),
    approved_by    text,
    approved_at    timestamptz,
    CONSTRAINT sql_templates_pk PRIMARY KEY (template_id, version),
    CONSTRAINT sql_templates_version_chk CHECK (version > 0),
    CONSTRAINT sql_templates_dialect_chk CHECK (dialect IN ('postgresql', 'oracle', 'sqlserver', 'mysql', 'duckdb', 'generic')),
    CONSTRAINT sql_templates_status_chk CHECK (status IN ('draft', 'approved', 'retired')),
    CONSTRAINT sql_templates_select_only CHECK (sql_text ~* '^\s*(with|select)\s'),
    -- coarse guard: no data-modifying CTE such as WITH x AS (DELETE ...) SELECT
    CONSTRAINT sql_templates_no_dml_cte CHECK (sql_text !~* '\(\s*(insert|update|delete|merge)\M'),
    -- the pinned hash is the hash of the text, so the text cannot change under it
    CONSTRAINT sql_templates_sha256_matches CHECK (sha256 = encode(sha256(convert_to(sql_text, 'UTF8')), 'hex')),
    CONSTRAINT sql_templates_approval_pair CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
    CONSTRAINT sql_templates_approved_needs_approver CHECK (status <> 'approved' OR approved_by IS NOT NULL),
    CONSTRAINT sql_templates_four_eyes CHECK (approved_by IS NULL OR approved_by <> created_by),
    CONSTRAINT sql_templates_params_object CHECK (jsonb_typeof(params_schema) = 'object'),
    CONSTRAINT sql_templates_sha_uq UNIQUE (sha256)
);
COMMENT ON TABLE ds.sql_templates IS
    'Approved, versioned, parameterised SELECT templates. The UI and the LLM never send free SQL; table sides, SQL key sources and crosswalks reference (template_id, version) and pin sha256 = sha256(UTF-8 sql_text) (sql_templates_sha256_matches); the loader re-verifies it at run start. ds-api re-validates sql_text with a SQLGlot AST allowlist: a single SELECT, no data-modifying CTE, allowlisted functions only; the CHECKs are only a coarse guard, and the template roles on the targets have no EXECUTE on dblink*, lo_*, pg_read_* or pg_sleep. An approved row is immutable except status approved -> retired (trigger sql_templates_immutable); ds_app may update only status, approved_by and approved_at. A change is a new version.';

CREATE TABLE ds.inferred_schemas (
    schema_ref     text          NOT NULL,
    version        integer       NOT NULL,
    dataset_id     text          NOT NULL DEFAULT 'default' REFERENCES ds.datasets (dataset_id),
    digest         text          NOT NULL,
    side_a         jsonb         NOT NULL,
    side_b         jsonb         NOT NULL,
    evidence       jsonb         NOT NULL DEFAULT '{}',
    status         text          NOT NULL DEFAULT 'draft',
    created_by     text          NOT NULL,
    created_at     timestamptz   NOT NULL DEFAULT now(),
    approved_by    text,
    approved_at    timestamptz,
    CONSTRAINT inferred_schemas_pk PRIMARY KEY (schema_ref, version),
    CONSTRAINT inferred_schemas_digest_fmt CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT inferred_schemas_digest_uq UNIQUE (digest),
    CONSTRAINT inferred_schemas_status_chk CHECK (status IN ('draft', 'approved', 'retired')),
    CONSTRAINT inferred_schemas_objects CHECK (jsonb_typeof(side_a) = 'object' AND jsonb_typeof(side_b) = 'object'),
    CONSTRAINT inferred_schemas_approval_pair CHECK ((approved_by IS NULL) = (approved_at IS NULL)),
    CONSTRAINT inferred_schemas_approved_needs_approver CHECK (status <> 'approved' OR approved_by IS NOT NULL),
    -- four-eyes, as for templates
    CONSTRAINT inferred_schemas_four_eyes CHECK (approved_by IS NULL OR approved_by <> created_by),
    -- the digest is the hash of the two schemas: sha256 of the UTF-8
    -- text form of the jsonb array [side_a, side_b]
    CONSTRAINT inferred_schemas_digest_matches CHECK (
        digest = 'sha256:' || encode(sha256(convert_to(jsonb_build_array(side_a, side_b)::text, 'UTF8')), 'hex'))
);
COMMENT ON TABLE ds.inferred_schemas IS
    'ds-profiler output approved by a human: one JSON Schema per side with x-ds-* annotations (x-ds-array-mode, x-ds-array-keys, x-ds-map, x-ds-align, x-ds-normalize) plus the evidence table. Specs reference it via inferredSchema {ref, digest}; the digest is part of the spec content_hash and equals sha256 of jsonb_build_array(side_a, side_b)::text (inferred_schemas_digest_matches). Approval is four-eyes; an approved row is immutable except status approved -> retired (trigger inferred_schemas_immutable). Observations are validated against it: drift on unselected paths is a SCHEMA_DRIFT finding, drift on a selected or compared path is INCONCLUSIVE(schema_drift).';


-- =============================================================================
-- Outbox. An event row is written in the SAME transaction as the
-- results upsert, the key's findings and the work unit's terminal state, so
-- report, AI and notification workers see committed events only and there is
-- no dual write. There is no broker in the MVP: consumers poll with FOR UPDATE
-- SKIP LOCKED in (run_id, seq) order (section 11.8) and record
-- (consumer, event_id) in ds.processed_events in the same transaction as their
-- side effect, so at-least-once delivery is processed exactly once per
-- consumer. If Kafka arrives (P4), Debezium's outbox event router replaces the
-- poller with columns renamed to id, aggregatetype, aggregateid, type, payload.
CREATE TABLE ds.outbox (
    event_id      uuid         NOT NULL DEFAULT gen_random_uuid(),
    run_id        bigint,
    seq           bigserial    NOT NULL,
    type          text         NOT NULL,
    payload       jsonb        NOT NULL DEFAULT '{}',
    created_at    timestamptz  NOT NULL DEFAULT now(),
    published_at  timestamptz,
    CONSTRAINT outbox_pk PRIMARY KEY (event_id),
    CONSTRAINT outbox_seq_uq UNIQUE (seq),
    CONSTRAINT outbox_run_fk FOREIGN KEY (run_id) REFERENCES ds.runs (run_id) ON DELETE CASCADE,
    -- dotted event type, e.g. result.committed, cycle.completed, run.completed
    CONSTRAINT outbox_type_format CHECK (
        type ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$' AND octet_length(type) <= 128),
    CONSTRAINT outbox_payload_object CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT outbox_published_after_created CHECK (published_at IS NULL OR published_at >= created_at)
) WITH (
    fillfactor = 90,
    autovacuum_vacuum_scale_factor = 0.02
);
COMMENT ON TABLE ds.outbox IS
    'Transactional outbox: event_id, run_id, seq (bigserial, per-run order), type, payload, created_at, published_at. Written in the same transaction as the result/findings upsert and the work unit''s terminal state. Consumers (root-causer per cycle, reporter, AI analysis of selected discrepancies) poll with SKIP LOCKED in (run_id, seq) order and are idempotent through ds.processed_events. payload carries ids and counts only, never raw values. The MVP notifies without it (docs/mvp/ALERTS.md D1): the audit hash chain is the notifications'' outbox, followed from ds.notify_cursor into ds.notify_deliveries (migration 012).';
COMMENT ON COLUMN ds.outbox.seq IS
    'Monotonic sequence (bigserial). Consumers process a run''s events in seq order; order across runs is not guaranteed.';
COMMENT ON COLUMN ds.outbox.published_at IS
    'Set once every registered consumer has a ds.processed_events row for the event (or, later, when the Debezium relay has published it). Published events are purged after a grace period; the purge cascades to processed_events.';

CREATE TABLE ds.processed_events (
    consumer      text         NOT NULL,
    event_id      uuid         NOT NULL,
    processed_at  timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT processed_events_pk PRIMARY KEY (consumer, event_id),
    CONSTRAINT processed_events_event_fk FOREIGN KEY (event_id) REFERENCES ds.outbox (event_id) ON DELETE CASCADE,
    CONSTRAINT processed_events_consumer_format CHECK (consumer ~ '^[a-z][a-z0-9_.-]{0,63}$')
);
COMMENT ON TABLE ds.processed_events IS
    'Idempotent-consumer ledger: one row per (consumer, event_id), inserted in the same transaction as the consumer''s side effect. A redelivered event hits the primary key and the consumer skips it.';

-- Idempotency keys, following draft-ietf-httpapi-idempotency-
-- key-header-07 (an expired Internet-Draft, not an RFC). ds-api requires the
-- Idempotency-Key header on POST /v1/runs, /v1/reports, /v1/investigations
-- and the compare route behind compare_key:
--   missing key                                  -> 400 (nothing stored)
--   same (principal, route, key), other fingerprint -> 422
--   same key while the original is in_flight     -> 409
--   same key and fingerprint, completed          -> re-authorise the caller, then
--                                                   re-render the response from
--                                                   resource_ref
-- The principal is always the end user (on-behalf-of when ds-agent calls,
-- and ds-agent sends a fresh UUIDv4 per tool call); only sha256(key) is
-- stored; only resource references are stored, never a response body, so a
-- replay re-applies the caller's role, dataset scope, tokenisation and
-- retention. The run (runs.idempotency_key_hash), its outbox event and the
-- completed row are written in ONE transaction. 400, 409 and 422 answers, and
-- a 5xx produced before the resource existed, are never stored, so they can
-- be retried. Rows are purged 24 h after creation (expires_at); an expired
-- row is reclaimed by the next request with that key (section 11.9).
CREATE TABLE ds.idempotency_keys (
    principal        text          NOT NULL,
    route            text          NOT NULL,
    key_hash         ds.sha256_hex NOT NULL,
    fingerprint      ds.sha256_hex NOT NULL,
    status           text          NOT NULL DEFAULT 'in_flight',
    response_status  smallint,
    resource_ref     jsonb,
    created_at       timestamptz   NOT NULL DEFAULT now(),
    expires_at       timestamptz   NOT NULL DEFAULT now() + interval '24 hours',
    CONSTRAINT idempotency_keys_pk PRIMARY KEY (principal, route, key_hash),
    -- DDL-local vocabulary
    CONSTRAINT idempotency_keys_status_chk CHECK (status IN ('in_flight', 'completed')),
    CONSTRAINT idempotency_keys_principal_len CHECK (principal <> '' AND octet_length(principal) <= 256),
    CONSTRAINT idempotency_keys_route_format CHECK (
        route ~ '^(POST|PUT|PATCH|DELETE) /[!-~]*$' AND octet_length(route) <= 256),
    CONSTRAINT idempotency_keys_completed_has_response CHECK (
        (status = 'completed') = (response_status IS NOT NULL)),
    CONSTRAINT idempotency_keys_response_status_chk CHECK (
        response_status IS NULL OR response_status BETWEEN 100 AND 599),
    -- references only (run_id, report_id, investigation_id, or a result
    -- reference run_id + cycle_no + key), never a response body
    CONSTRAINT idempotency_keys_resource_ref_shape CHECK (
        resource_ref IS NULL
        OR (status = 'completed' AND jsonb_typeof(resource_ref) = 'object' AND resource_ref <> '{}'::jsonb
            AND resource_ref - ARRAY['run_id', 'report_id', 'investigation_id', 'cycle_no', 'key']::text[] = '{}'::jsonb)),
    -- a 5xx is stored only once the resource exists
    CONSTRAINT idempotency_keys_no_bare_5xx CHECK (
        response_status IS NULL OR response_status < 500 OR resource_ref IS NOT NULL),
    CONSTRAINT idempotency_keys_expiry_chk CHECK (expires_at > created_at)
);
COMMENT ON TABLE ds.idempotency_keys IS
    'Idempotency-Key store for the public API: PK (principal, route, key_hash), key_hash = sha256 of the key, fingerprint = sha256 of the canonical request body plus spec name and version, status in_flight | completed, response_status and resource_ref (references only). 422 on a reused key with a different fingerprint, 409 while in flight; once completed, a replay re-runs the authorisation checks and re-renders the response from resource_ref. Purge after 24 h.';
COMMENT ON COLUMN ds.idempotency_keys.principal IS
    'Always the end user (for ds-agent calls, the user on whose behalf it acts), never a shared service identity.';
COMMENT ON COLUMN ds.idempotency_keys.key_hash IS
    'sha256 (lowercase hex) of the client Idempotency-Key; the key itself is never stored. ds-api validates the key first: RFC 8941 sf-string content, at most 255 printable ASCII characters, a random value such as a UUIDv4 (ds-agent sends a fresh one per tool call).';
COMMENT ON COLUMN ds.idempotency_keys.status IS
    'in_flight while the first request executes (a duplicate gets 409); completed once response_status/resource_ref are stored, in the same transaction that created the resource. ds-api deletes an in_flight row whose request died once the request timeout has passed, so the client can retry; runs_idempotency_uq still prevents a second run.';

-- =============================================================================
-- 9. Indexes (beyond primary keys and unique constraints)
-- =============================================================================
-- Hot path: findings by (run_id, path_pattern, change_type): drill-down and
-- get_findings filters.
CREATE INDEX findings_run_path_change_idx ON ds.findings (run_id, path_pattern, change_type);
-- Hot path: findings by (run_id, signature_hash): signature roll-up and drill-down.
CREATE INDEX findings_run_signature_idx   ON ds.findings (run_id, signature_hash);
-- Hot path: (key): the history of one key across runs (compare_key, get_observations).
CREATE INDEX findings_key_idx             ON ds.findings (key);
CREATE INDEX results_key_idx              ON ds.results (key);
CREATE INDEX observations_key_idx         ON ds.observations (key);
CREATE INDEX run_keys_key_idx             ON ds.run_keys (key);

-- findings: sign-off ("zero unexplained CRITICAL") and suppression look-ups
CREATE INDEX findings_run_severity_open_idx ON ds.findings (run_id, severity, taxonomy_code)
    WHERE NOT suppressed;
CREATE INDEX findings_root_cause_idx      ON ds.findings (root_cause_id) WHERE root_cause_id IS NOT NULL;
CREATE INDEX findings_run_rule_idx        ON ds.findings (run_id, rule_id) WHERE rule_id IS NOT NULL;
-- root causes of kind member_key (generic): group by (path_pattern, member_key_tok)
CREATE INDEX findings_run_member_key_idx  ON ds.findings (run_id, path_pattern, member_key_tok)
    WHERE member_key_tok IS NOT NULL;
-- root causes of kind identifier (optional pack enrichment): group link findings by (link_reason, explaining_id_tok)
CREATE INDEX findings_run_explaining_idx  ON ds.findings (run_id, link_reason, explaining_id_tok)
    WHERE explaining_id_tok IS NOT NULL;
-- root causes of kind graph_anchor: group graph findings by anchor_key_tok
CREATE INDEX findings_run_anchor_idx      ON ds.findings (run_id, anchor_key_tok, change_type)
    WHERE anchor_key_tok IS NOT NULL;

-- results: non-MATCH drill-down
CREATE INDEX results_run_outcome_idx      ON ds.results (run_id, outcome) WHERE outcome <> 'MATCH';

-- observations: blob GC ("is this content_hash still referenced?") and dedupe
CREATE INDEX observations_content_hash_idx ON ds.observations (content_hash) WHERE content_hash IS NOT NULL;

-- Hot path: the lease scan. It matches the WHERE and ORDER BY of the leasing
-- query in section 11.1. The partial predicate keeps finished units out of
-- the index, so it stays small.
CREATE INDEX work_units_lease_scan_idx ON ds.work_units (kind, priority, available_at, unit_id)
    WHERE state IN ('pending', 'leased');
CREATE INDEX work_units_run_state_idx  ON ds.work_units (run_id, state);

CREATE INDEX run_keys_run_stratum_idx  ON ds.run_keys (run_id, stratum);

CREATE INDEX runs_active_idx           ON ds.runs (status, created_at)
    WHERE status IN ('queued', 'running', 'paused', 'cancelling');
CREATE INDEX runs_spec_idx             ON ds.runs (spec_name, spec_version);
CREATE INDEX runs_mode_created_idx     ON ds.runs (mode, created_at DESC);
CREATE INDEX runs_parent_idx           ON ds.runs (parent_run_id) WHERE parent_run_id IS NOT NULL;
-- dataset scope: list a user's runs per granted dataset
CREATE INDEX runs_dataset_created_idx  ON ds.runs (dataset_id, created_at DESC);

CREATE INDEX targets_auth_ref_idx      ON ds.targets (auth_ref) WHERE auth_ref IS NOT NULL;
CREATE INDEX targets_dataset_idx       ON ds.targets (dataset_id);
CREATE INDEX dataset_targets_target_idx ON ds.dataset_targets (target_id);  -- FK, and "which datasets may call this target"
CREATE INDEX specs_dataset_idx         ON ds.specs (dataset_id);
CREATE INDEX llm_profiles_auth_ref_idx ON ds.llm_profiles (auth_ref) WHERE auth_ref IS NOT NULL;
-- At most one promoted tuple per (profile, environment)
CREATE UNIQUE INDEX llm_promotions_promoted_uq ON ds.llm_promotions (profile_id, environment)
    WHERE status = 'promoted';
CREATE INDEX llm_promotions_profile_idx ON ds.llm_promotions (profile_id, environment, created_at DESC);

CREATE INDEX signatures_hash_idx       ON ds.signatures (signature_hash);   -- compare_runs across runs
CREATE INDEX signatures_run_count_idx  ON ds.signatures (run_id, finding_count DESC);

CREATE INDEX root_causes_run_count_idx ON ds.root_causes (run_id, finding_count DESC);
CREATE INDEX root_causes_cluster_idx   ON ds.root_causes (cluster_id) WHERE cluster_id IS NOT NULL;
CREATE INDEX root_causes_rule_idx      ON ds.root_causes (rule_id) WHERE rule_id IS NOT NULL;

CREATE INDEX clusters_run_idx          ON ds.clusters (run_id);
CREATE INDEX clusters_members_gin      ON ds.clusters USING gin (member_signatures);

CREATE INDEX known_diffs_spec_active_idx ON ds.known_diffs (spec_name) WHERE active;

CREATE UNIQUE INDEX noise_masks_approved_uq ON ds.noise_masks (spec_name, path_pattern, side)
    NULLS NOT DISTINCT WHERE status = 'approved';
CREATE INDEX noise_masks_spec_status_idx   ON ds.noise_masks (spec_name, status);
CREATE INDEX noise_masks_calibration_idx   ON ds.noise_masks (calibration_run_id)
    WHERE calibration_run_id IS NOT NULL;

CREATE INDEX reports_run_idx           ON ds.reports (run_id, requested_at DESC);
CREATE INDEX reports_pending_idx       ON ds.reports (requested_at) WHERE status IN ('queued', 'rendering');

CREATE INDEX chat_sessions_user_idx    ON ds.chat_sessions (user_id, last_active_at DESC);
CREATE INDEX chat_sessions_run_idx     ON ds.chat_sessions (run_id) WHERE run_id IS NOT NULL;
CREATE INDEX chat_sessions_scope_key_idx ON ds.chat_sessions (scope_key) WHERE scope_key IS NOT NULL;
CREATE INDEX chat_turns_user_idx       ON ds.chat_turns (user_id, created_at DESC);

-- LLM usage roll-ups: per turn, per profile over time, per run
CREATE INDEX llm_calls_turn_idx        ON ds.llm_calls (turn_id) WHERE turn_id IS NOT NULL;
CREATE INDEX llm_calls_profile_created_idx ON ds.llm_calls (profile_id, created_at);
CREATE INDEX llm_calls_run_idx         ON ds.llm_calls (run_id) WHERE run_id IS NOT NULL;

-- Outbox: the consumer poll scans unpublished events in (run_id, seq)
-- order; the partial predicate keeps published events out of the index.
CREATE INDEX outbox_poll_idx           ON ds.outbox (run_id, seq) WHERE published_at IS NULL;
CREATE INDEX outbox_run_idx            ON ds.outbox (run_id);                -- FK cascade on run retention
CREATE INDEX outbox_published_idx      ON ds.outbox (published_at) WHERE published_at IS NOT NULL;  -- purge
CREATE INDEX processed_events_event_idx ON ds.processed_events (event_id);   -- FK cascade on outbox purge

-- Idempotency keys: the 24 h purge
CREATE INDEX idempotency_keys_expires_idx ON ds.idempotency_keys (expires_at);

CREATE INDEX audit_log_occurred_brin   ON ds.audit_log USING brin (occurred_at);
CREATE INDEX audit_log_object_idx      ON ds.audit_log (object_type, object_id, occurred_at);
CREATE INDEX audit_log_actor_idx       ON ds.audit_log (actor, occurred_at);
CREATE INDEX audit_log_run_idx         ON ds.audit_log (run_id) WHERE run_id IS NOT NULL;

-- =============================================================================
-- 10a. Triggers
-- =============================================================================
CREATE TRIGGER secrets_ref_set_updated_at BEFORE UPDATE ON ds.secrets_ref
    FOR EACH ROW EXECUTE FUNCTION ds.tg_set_updated_at();
CREATE TRIGGER targets_set_updated_at     BEFORE UPDATE ON ds.targets
    FOR EACH ROW EXECUTE FUNCTION ds.tg_set_updated_at();
CREATE TRIGGER runs_set_updated_at        BEFORE UPDATE ON ds.runs
    FOR EACH ROW EXECUTE FUNCTION ds.tg_set_updated_at();
CREATE TRIGGER known_diffs_set_updated_at BEFORE UPDATE ON ds.known_diffs
    FOR EACH ROW EXECUTE FUNCTION ds.tg_set_updated_at();
CREATE TRIGGER noise_masks_set_updated_at BEFORE UPDATE ON ds.noise_masks
    FOR EACH ROW EXECUTE FUNCTION ds.tg_set_updated_at();
CREATE TRIGGER clusters_set_updated_at    BEFORE UPDATE ON ds.clusters
    FOR EACH ROW EXECUTE FUNCTION ds.tg_set_updated_at();
CREATE TRIGGER root_causes_set_updated_at BEFORE UPDATE ON ds.root_causes
    FOR EACH ROW EXECUTE FUNCTION ds.tg_set_updated_at();
CREATE TRIGGER reports_set_updated_at     BEFORE UPDATE ON ds.reports
    FOR EACH ROW EXECUTE FUNCTION ds.tg_set_updated_at();
CREATE TRIGGER datasets_set_updated_at    BEFORE UPDATE ON ds.datasets
    FOR EACH ROW EXECUTE FUNCTION ds.tg_set_updated_at();
CREATE TRIGGER llm_profiles_set_updated_at BEFORE UPDATE ON ds.llm_profiles
    FOR EACH ROW EXECUTE FUNCTION ds.tg_set_updated_at();

-- Taxonomy presence rule. A row trigger on the partitioned parent is cloned to
-- every partition, including those that create_run_partitions() attaches later.
CREATE TRIGGER findings_taxonomy_presence BEFORE INSERT OR UPDATE OF taxonomy_code, change_type
    ON ds.findings FOR EACH ROW EXECUTE FUNCTION ds.tg_findings_taxonomy_presence();

-- Noise-mask four-eyes against the spec's authors.
CREATE TRIGGER noise_masks_four_eyes BEFORE INSERT OR UPDATE OF approved_by
    ON ds.noise_masks FOR EACH ROW EXECUTE FUNCTION ds.tg_noise_masks_four_eyes();

-- Spec-approval four-eyes against the spec's authors, and the run gate.
CREATE TRIGGER spec_approvals_four_eyes BEFORE INSERT OR UPDATE OF approved_by, spec_id
    ON ds.spec_approvals FOR EACH ROW EXECUTE FUNCTION ds.tg_spec_approvals_four_eyes();
CREATE TRIGGER runs_spec_approval BEFORE INSERT OR UPDATE OF spec_id, claim_level
    ON ds.runs FOR EACH ROW EXECUTE FUNCTION ds.tg_runs_spec_approval();

-- Both run targets are owned by or granted to the run's dataset.
CREATE TRIGGER runs_dataset_targets BEFORE INSERT OR UPDATE OF dataset_id, target_a_id, target_b_id
    ON ds.runs FOR EACH ROW EXECUTE FUNCTION ds.tg_runs_dataset_targets();

-- Approved known_diffs are immutable and never deleted.
CREATE TRIGGER known_diffs_immutable BEFORE UPDATE OR DELETE
    ON ds.known_diffs FOR EACH ROW EXECUTE FUNCTION ds.tg_known_diffs_immutable();

-- Approved templates and inferred schemas are immutable except retirement.
CREATE TRIGGER sql_templates_immutable BEFORE UPDATE
    ON ds.sql_templates FOR EACH ROW EXECUTE FUNCTION ds.tg_approved_row_immutable();
CREATE TRIGGER inferred_schemas_immutable BEFORE UPDATE
    ON ds.inferred_schemas FOR EACH ROW EXECUTE FUNCTION ds.tg_approved_row_immutable();

-- A profile's trust is derived from ds.llm_hosts.
CREATE TRIGGER llm_profiles_derive_trust BEFORE INSERT OR UPDATE OF base_url, trust
    ON ds.llm_profiles FOR EACH ROW EXECUTE FUNCTION ds.tg_llm_profiles_derive_trust();

-- specs are immutable
CREATE TRIGGER specs_immutable_row BEFORE UPDATE OR DELETE ON ds.specs
    FOR EACH ROW EXECUTE FUNCTION ds.tg_reject_mutation();
CREATE TRIGGER specs_immutable_truncate BEFORE TRUNCATE ON ds.specs
    FOR EACH STATEMENT EXECUTE FUNCTION ds.tg_reject_mutation();

-- audit_log is append-only. ENABLE ALWAYS makes the triggers fire even under
-- session_replication_role = replica. A table owner or superuser can still
-- disable them; that is itself visible in pg_trigger and in the server log.
CREATE TRIGGER audit_log_append_only_row BEFORE UPDATE OR DELETE ON ds.audit_log
    FOR EACH ROW EXECUTE FUNCTION ds.tg_reject_mutation();
CREATE TRIGGER audit_log_append_only_truncate BEFORE TRUNCATE ON ds.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION ds.tg_reject_mutation();
ALTER TABLE ds.audit_log ENABLE ALWAYS TRIGGER audit_log_append_only_row;
ALTER TABLE ds.audit_log ENABLE ALWAYS TRIGGER audit_log_append_only_truncate;
ALTER TABLE ds.specs ENABLE ALWAYS TRIGGER specs_immutable_row;
ALTER TABLE ds.specs ENABLE ALWAYS TRIGGER specs_immutable_truncate;
ALTER TABLE ds.known_diffs ENABLE ALWAYS TRIGGER known_diffs_immutable;
ALTER TABLE ds.sql_templates ENABLE ALWAYS TRIGGER sql_templates_immutable;
ALTER TABLE ds.inferred_schemas ENABLE ALWAYS TRIGGER inferred_schemas_immutable;

-- =============================================================================
-- 10b. Partition management helper
-- =============================================================================
-- Creates dedicated partitions ds.observations_r<run_id>, ds.results_r<run_id>
-- and ds.findings_r<run_id>. It uses CREATE TABLE ... (LIKE ...) plus ATTACH
-- PARTITION, which takes SHARE UPDATE EXCLUSIVE on the parent instead of the
-- ACCESS EXCLUSIVE that CREATE TABLE ... PARTITION OF would take. ATTACH
-- still scans the DEFAULT partition (see section 7). The function is
-- idempotent and returns true if it created anything. ds-scheduler calls it
-- when a run starts, before the first work unit is leased.
CREATE FUNCTION ds.create_run_partitions(p_run_id bigint)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET lock_timeout = '5s'
AS $fn$
DECLARE
    v_parent  text;
    v_part    text;
    v_created boolean := false;
BEGIN
    IF p_run_id IS NULL OR p_run_id <= 0 THEN
        RAISE EXCEPTION 'create_run_partitions: invalid run_id %', p_run_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    PERFORM 1 FROM ds.runs WHERE run_id = p_run_id FOR NO KEY UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'create_run_partitions: run % does not exist', p_run_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    -- serialise partition DDL across schedulers
    PERFORM pg_advisory_xact_lock(hashtext('ds.create_run_partitions'));

    FOREACH v_parent IN ARRAY ARRAY['observations', 'results', 'findings']::text[] LOOP
        v_part := format('%s_r%s', v_parent, p_run_id);
        IF to_regclass(format('ds.%I', v_part)) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE ds.%I (LIKE ds.%I INCLUDING DEFAULTS INCLUDING CONSTRAINTS '
                'INCLUDING STORAGE INCLUDING COMPRESSION)',
                v_part, v_parent);
            EXECUTE format(
                'ALTER TABLE ds.%I ATTACH PARTITION ds.%I FOR VALUES IN (%s)',
                v_parent, v_part, p_run_id);
            v_created := true;
        END IF;
    END LOOP;

    UPDATE ds.runs
       SET dedicated_partitions = true
     WHERE run_id = p_run_id
       AND NOT dedicated_partitions;

    RETURN v_created;
END
$fn$;
COMMENT ON FUNCTION ds.create_run_partitions(bigint) IS
    'Attach dedicated LIST partitions of observations, results and findings for one run. Call it before the first write for that run. Idempotent. SECURITY DEFINER (owner ds_owner); EXECUTE is granted to ds_app only.';

-- =============================================================================
-- 10c. Curated views for ds_agent_ro (ds-mcp read tools)
-- -----------------------------------------------------------------------------
-- Six views: v_run_summary, v_findings_masked, v_root_causes,
-- v_results_masked, v_observations_meta and v_rule_hits. DuckDB agent SQL
-- never reads them directly: ds-mcp exports the caller's rows of these views
-- to a per-session local Parquet file, and DuckDB reads only that file.
-- The views run with their owner's rights (security_invoker = false), so
-- ds_agent_ro needs no privilege on the base tables. security_barrier = true
-- keeps a caller's non-leakproof functions from seeing rows filtered out by
-- the view (runs.agent_visible = false). Leakproof filters such as
-- run_id = <n> are still pushed down, so they reach one partition.
-- The views use built-in functions only. PostgreSQL checks EXECUTE on
-- functions in a view against the caller, and ds_agent_ro has no EXECUTE
-- grant on any ds function.
-- Dataset scope: every curated view exposes dataset_id, and ds-mcp
-- always filters on the requesting user's datasets, so the agent never sees
-- more than the user. No curated view exposes LLM cost or pricing.
-- Derived verdict: computed here, never stored (see ds.verdict). The
-- same CASE appears in v_result_verdicts (services), v_results_masked (agent)
-- and, as counts, in v_run_summary; the smoke suite checks they agree.
-- =============================================================================

-- Service-side verdict per key and cycle (ds-api public API, ds-reporter
-- exports). Not curated and not granted to ds_agent_ro: it covers hidden runs
-- too, and it runs with the caller's rights (security_invoker = true).
CREATE VIEW ds.v_result_verdicts
WITH (security_invoker = true) AS
SELECT
    r.run_id,
    r.cycle_no,
    r.key,
    r.outcome,
    r.inconclusive_reason,
    r.drift_reason,
    r.confirmation,
    r.side_a_state,
    r.side_b_state,
    r.signoff_member,
    r.first_outcome,
    r.revision,
    r.updated_at,
    b.bug_suppressed,
    CASE
        WHEN r.outcome IN ('MISMATCH', 'NOT_FOUND_A', 'NOT_FOUND_B')
             AND r.confirmation = 'provisional'                     THEN NULL::ds.verdict   -- not yet reproduced
        WHEN r.outcome IN ('MATCH', 'NOISE_ONLY')                   THEN 'MATCH'::ds.verdict
        WHEN r.outcome IN ('MISMATCH', 'NOT_FOUND_A', 'NOT_FOUND_B') THEN 'DIFFERENT'::ds.verdict
        WHEN r.outcome IN ('INCONCLUSIVE', 'DRIFT')                 THEN 'INDETERMINATE'::ds.verdict
        WHEN b.bug_suppressed                                       THEN 'DIFFERENT'::ds.verdict
        ELSE 'MATCH'::ds.verdict                    -- KNOWN_DIFF from accepted / expected_by_design rules
    END AS verdict
FROM ds.results AS r
CROSS JOIN LATERAL (
    SELECT CASE WHEN r.outcome = 'KNOWN_DIFF' THEN EXISTS (
               SELECT 1
               FROM ds.findings AS f
               JOIN ds.known_diffs AS kd ON kd.rule_id = f.rule_id
               WHERE f.run_id = r.run_id AND f.cycle_no = r.cycle_no AND f.key = r.key
                 AND f.suppressed AND kd.classification = 'bug')
           ELSE false END AS bug_suppressed
) AS b;
COMMENT ON VIEW ds.v_result_verdicts IS
    'Services only (ds_app): the derived verdict per key and cycle, MATCH <- MATCH, NOISE_ONLY, KNOWN_DIFF (accepted | expected_by_design); DIFFERENT <- MISMATCH, NOT_FOUND_A/B, KNOWN_DIFF with a finding suppressed by a bug rule; INDETERMINATE <- INCONCLUSIVE, DRIFT; NULL for a provisional MISMATCH / NOT_FOUND_A / NOT_FOUND_B (not yet reproduced; counted in n_outstanding). Computed at read time because the KNOWN_DIFF split depends on known_diffs.classification (another table), so a generated column is impossible; approved rules are immutable.';

-- One row per run with stratum = '*' (whole run), plus one row per stratum
-- ('(none)' for keys without a stratum). Sign-off columns count DISTINCT KEYS,
-- each at its latest cycle (the final cycle once the run has finished it), so
-- a soak run never counts a key once per cycle. Coverage arithmetic
-- (architecture.md §7.1 and §15):
--   n_manifest      manifest keys (run_keys, the frozen manifest)
--   n_results       key-cycles stored for the run (all cycles; informational)
--   n_keys          distinct keys with a result; every outcome count below
--                   counts keys
--   n_absent_both   INCONCLUSIVE keys with reason absent_both (a manifest seed
--                   ABSENT on both sides; never MATCH)
--   n_known_diff_bug KNOWN_DIFF keys with a finding suppressed by a known_diffs
--                   rule of classification 'bug' (a known open defect)
--   n_api_uncomparable INCONCLUSIVE keys with attrs.api_uncomparable (above the
--                   API cap): excluded from the INCONCLUSIVE + DRIFT gate
--   n_provisional   MISMATCH / NOT_FOUND_A / NOT_FOUND_B keys still
--                   provisional (results.confirmation): outstanding, not
--                   discrepant and not clean
--   n_schema_drift  keys with at least one SCHEMA_DRIFT finding or reason
--                   schema_drift (coverage.csv schema_drift)
--   member          a key is a sign-off member when it has no sign-off label
--                   (it is judged in its plain stratum) or signoff_member is
--                   true; n_contrast counts the other keys
--   n_compared      = n_keys - DRIFT - INCONCLUSIVE - n_provisional (all keys;
--                   the "compared" count of §7.1)
--   n_clean         = n_compared over members only (the bound's denominator)
--   n_discrepant    = members' confirmed MISMATCH + NOT_FOUND_A + NOT_FOUND_B
--                   + members' KNOWN_DIFF(bug)
--   n_discrepant_all_suppressed = members' confirmed MISMATCH + NOT_FOUND_A
--                   + NOT_FOUND_B + KNOWN_DIFF + NOISE_ONLY (numerator of the
--                   second bound)
--   n_suppressed    = KNOWN_DIFF + NOISE_ONLY                (counted separately)
--   mismatch_rate   = n_discrepant / n_clean
--   unresolved_rate = (INCONCLUSIVE - n_api_uncomparable + DRIFT)
--                     / (n_keys - n_api_uncomparable)   (gate per sign-off stratum:
--                     at most the stratum target, 0.1% / 0.01% of n_clean)
--   ub95_one_sided_zero_fail = 1 - 0.05^(1/n_clean) when n_discrepant = 0
--                   (the MVP stores ub95_one_sided, exact for any x, instead),
--                   i.e. the one-sided 95% Clopper-Pearson upper bound at x = 0
--                   (n = 2,995 gives <= 0.1%; n = 29,956 gives <= 0.01%). NULL
--                   for stratum '*': pooling strata sampled at different rates
--                   is not a valid population bound. For x > 0, ds-mcp computes
--                   beta.ppf(0.95, x+1, n-x) from n_discrepant and n_clean.
--   inconclusive_by_reason  {reason: keys} over INCONCLUSIVE keys
--   Derived verdict and the run counters, which reconcile exactly:
--   n_verdict_match         = MATCH + NOISE_ONLY + KNOWN_DIFF - n_known_diff_bug
--   n_verdict_different     = confirmed MISMATCH + NOT_FOUND_A + NOT_FOUND_B
--                             + n_known_diff_bug (all keys, members or not)
--   n_verdict_indeterminate = INCONCLUSIVE + DRIFT
--   n_cancelled     manifest keys without a result, or provisional, in a
--                   cancelled run
--   n_outstanding   manifest keys without a result, or provisional, otherwise
--   n_manifest      = n_verdict_match + n_verdict_different
--                     + n_verdict_indeterminate + n_cancelled + n_outstanding
--                   (holds because ds-comparer writes results only for
--                   manifest keys; per stratum it holds when results copy the
--                   stratum labels of run_keys, as they do)
--   coverage        = n_compared / n_manifest  (compared / manifest total)
--   finding counts  cover the findings of each key's latest cycle;
--                   n_critical_unexplained = CRITICAL findings that are
--                   unsuppressed or suppressed by a 'bug' rule (sign-off item 1)
--   run_stats       runs.stats. For bulk_parity/table_diff runs (P2) MATCH
--                   rows stay in Parquet, so the counts here cover stored rows
--                   only and the run totals are in run_stats.
CREATE VIEW ds.v_run_summary
WITH (security_barrier = true, security_invoker = false) AS
SELECT
    ru.run_id,
    ru.dataset_id,
    ru.spec_name,
    ru.spec_version,
    ru.mode,
    ru.status,
    ru.claim_level,
    ru.time_mode,
    ru.manifest_hash,
    ru.target_a_id,
    ru.target_b_id,
    ru.current_cycle_no,
    ru.created_at,
    ru.started_at,
    ru.finished_at,
    s.stratum,
    s.n_manifest,
    s.n_results,
    s.n_keys,
    s.n_match,
    s.n_absent_both,
    s.n_mismatch,
    s.n_known_diff,
    s.n_known_diff_bug,
    s.n_noise_only,
    s.n_drift,
    s.n_not_found_a,
    s.n_not_found_b,
    s.n_inconclusive,
    s.n_api_uncomparable,
    s.inconclusive_by_reason,
    s.n_rechecked,
    s.n_provisional,
    s.n_schema_drift,
    s.n_discrepant,
    s.n_discrepant_all_suppressed,
    s.n_known_diff + s.n_noise_only                                         AS n_suppressed,
    s.n_compared,
    s.n_clean,
    s.n_contrast,
    s.n_match + s.n_noise_only + s.n_known_diff - s.n_known_diff_bug        AS n_verdict_match,
    s.n_verdict_different,
    s.n_inconclusive + s.n_drift                                            AS n_verdict_indeterminate,
    s.n_cancelled,
    s.n_outstanding,
    CASE WHEN s.n_manifest > 0
         THEN round(s.n_compared::numeric / s.n_manifest, 8)
    END                                                                     AS coverage,
    CASE WHEN s.n_clean > 0
         THEN round(s.n_discrepant::numeric / s.n_clean, 8)
    END                                                                     AS mismatch_rate,
    CASE WHEN s.n_keys > 0
         THEN round(s.n_inconclusive::numeric / s.n_keys, 8)
    END                                                                     AS inconclusive_rate,
    CASE WHEN s.n_keys - s.n_api_uncomparable > 0
         THEN round((s.n_inconclusive - s.n_api_uncomparable + s.n_drift)::numeric
                    / (s.n_keys - s.n_api_uncomparable), 8)
    END                                                                     AS unresolved_rate,
    CASE WHEN s.stratum <> '*' AND s.n_discrepant = 0 AND s.n_clean > 0
         THEN 1 - power(0.05::float8, 1.0::float8 / s.n_clean)
    END                                                                     AS ub95_one_sided_zero_fail,
    s.n_findings,
    s.n_findings_unsuppressed,
    s.n_critical_unsuppressed,
    s.n_critical_unexplained,
    s.n_high_unsuppressed,
    ru.stats                                                                AS run_stats
FROM ds.runs AS ru
CROSS JOIN LATERAL (
    WITH latest AS (
        -- each key at its latest cycle; n_cycles counts all stored cycles of the key
        SELECT DISTINCT ON (r.key)
               r.key, r.cycle_no, coalesce(r.signoff_stratum, r.stratum, '(none)') AS stratum,
               r.outcome, r.inconclusive_reason, r.recheck_count, r.attrs,
               -- a discrepant outcome not yet reproduced
               r.outcome IN ('MISMATCH', 'NOT_FOUND_A', 'NOT_FOUND_B')
                   AND r.confirmation = 'provisional'                       AS provisional,
               -- sign-off membership (keys without a sign-off label are members)
               r.signoff_stratum IS NULL OR r.signoff_member                AS member,
               count(*) OVER (PARTITION BY r.key) AS n_cycles
        FROM ds.results AS r
        WHERE r.run_id = ru.run_id
        ORDER BY r.key, r.cycle_no DESC
    ), keyed AS (
        SELECT l.*,
               l.outcome = 'KNOWN_DIFF' AND EXISTS (
                   SELECT 1
                   FROM ds.findings AS f
                   JOIN ds.known_diffs AS kd ON kd.rule_id = f.rule_id
                   WHERE f.run_id = ru.run_id AND f.cycle_no = l.cycle_no AND f.key = l.key
                     AND f.suppressed AND kd.classification = 'bug') AS bug_suppressed,
               -- schema drift on the key (INFO finding or INCONCLUSIVE reason)
               coalesce(l.inconclusive_reason = 'schema_drift', false) OR EXISTS (
                   SELECT 1
                   FROM ds.findings AS f
                   WHERE f.run_id = ru.run_id AND f.cycle_no = l.cycle_no AND f.key = l.key
                     AND f.taxonomy_code = 'SCHEMA_DRIFT') AS schema_drift
        FROM latest AS l
    ), manifest AS (
        -- the frozen manifest (run_keys); has_result = the key has a result in some cycle
        SELECT coalesce(mk.signoff_stratum, mk.stratum, '(none)') AS stratum,
               l.key IS NOT NULL                                  AS has_result
        FROM ds.run_keys AS mk
        LEFT JOIN latest AS l ON l.key = mk.key
        WHERE mk.run_id = ru.run_id
    )
    SELECT u.stratum,
           sum(u.n_manifest)::bigint                  AS n_manifest,
           sum(u.n_results)::bigint                   AS n_results,
           sum(u.n_keys)::bigint                      AS n_keys,
           sum(u.n_match)::bigint                     AS n_match,
           sum(u.n_absent_both)::bigint               AS n_absent_both,
           sum(u.n_mismatch)::bigint                  AS n_mismatch,
           sum(u.n_known_diff)::bigint                AS n_known_diff,
           sum(u.n_known_diff_bug)::bigint            AS n_known_diff_bug,
           sum(u.n_noise_only)::bigint                AS n_noise_only,
           sum(u.n_drift)::bigint                     AS n_drift,
           sum(u.n_not_found_a)::bigint               AS n_not_found_a,
           sum(u.n_not_found_b)::bigint               AS n_not_found_b,
           sum(u.n_inconclusive)::bigint              AS n_inconclusive,
           sum(u.n_api_uncomparable)::bigint          AS n_api_uncomparable,
           coalesce(jsonb_object_agg(u.reason, u.reason_n) FILTER (WHERE u.reason IS NOT NULL),
                    '{}'::jsonb)                      AS inconclusive_by_reason,
           sum(u.n_rechecked)::bigint                 AS n_rechecked,
           sum(u.n_provisional)::bigint               AS n_provisional,
           sum(u.n_schema_drift)::bigint              AS n_schema_drift,
           sum(u.n_discrepant)::bigint                AS n_discrepant,
           sum(u.n_discrepant_all_suppressed)::bigint AS n_discrepant_all_suppressed,
           sum(u.n_compared)::bigint                  AS n_compared,
           sum(u.n_clean)::bigint                     AS n_clean,
           sum(u.n_contrast)::bigint                  AS n_contrast,
           sum(u.n_verdict_different)::bigint         AS n_verdict_different,
           sum(u.n_findings)::bigint                  AS n_findings,
           sum(u.n_findings_unsuppressed)::bigint     AS n_findings_unsuppressed,
           sum(u.n_critical_unsuppressed)::bigint     AS n_critical_unsuppressed,
           sum(u.n_critical_unexplained)::bigint      AS n_critical_unexplained,
           sum(u.n_high_unsuppressed)::bigint         AS n_high_unsuppressed,
           sum(u.n_cancelled)::bigint                 AS n_cancelled,
           sum(u.n_outstanding)::bigint               AS n_outstanding
    FROM (
        -- outcomes: one row per key
        SELECT CASE WHEN GROUPING(k.stratum) = 1 THEN '*' ELSE k.stratum END AS stratum,
               0::bigint                                                AS n_manifest,
               coalesce(sum(k.n_cycles), 0)                             AS n_results,
               count(*)                                                 AS n_keys,
               count(*) FILTER (WHERE k.outcome = 'MATCH')              AS n_match,
               count(*) FILTER (WHERE k.outcome = 'INCONCLUSIVE'
                                  AND k.inconclusive_reason = 'absent_both') AS n_absent_both,
               count(*) FILTER (WHERE k.outcome = 'MISMATCH')           AS n_mismatch,
               count(*) FILTER (WHERE k.outcome = 'KNOWN_DIFF')         AS n_known_diff,
               count(*) FILTER (WHERE k.bug_suppressed)                 AS n_known_diff_bug,
               count(*) FILTER (WHERE k.outcome = 'NOISE_ONLY')         AS n_noise_only,
               count(*) FILTER (WHERE k.outcome = 'DRIFT')              AS n_drift,
               count(*) FILTER (WHERE k.outcome = 'NOT_FOUND_A')        AS n_not_found_a,
               count(*) FILTER (WHERE k.outcome = 'NOT_FOUND_B')        AS n_not_found_b,
               count(*) FILTER (WHERE k.outcome = 'INCONCLUSIVE')       AS n_inconclusive,
               count(*) FILTER (WHERE k.outcome = 'INCONCLUSIVE'
                                  AND k.attrs @> '{"api_uncomparable": true}') AS n_api_uncomparable,
               NULL::text                                               AS reason,
               NULL::bigint                                             AS reason_n,
               count(*) FILTER (WHERE k.recheck_count > 0)              AS n_rechecked,
               count(*) FILTER (WHERE k.provisional)                    AS n_provisional,
               count(*) FILTER (WHERE k.schema_drift)                   AS n_schema_drift,
               count(*) FILTER (WHERE k.member
                                  AND ((k.outcome IN ('MISMATCH', 'NOT_FOUND_A', 'NOT_FOUND_B') AND NOT k.provisional)
                                       OR k.bug_suppressed))            AS n_discrepant,
               count(*) FILTER (WHERE k.member
                                  AND ((k.outcome IN ('MISMATCH', 'NOT_FOUND_A', 'NOT_FOUND_B') AND NOT k.provisional)
                                       OR k.outcome IN ('KNOWN_DIFF', 'NOISE_ONLY'))) AS n_discrepant_all_suppressed,
               count(*) FILTER (WHERE k.outcome NOT IN ('DRIFT', 'INCONCLUSIVE') AND NOT k.provisional) AS n_compared,
               count(*) FILTER (WHERE k.member
                                  AND k.outcome NOT IN ('DRIFT', 'INCONCLUSIVE') AND NOT k.provisional) AS n_clean,
               count(*) FILTER (WHERE NOT k.member)                     AS n_contrast,
               count(*) FILTER (WHERE (k.outcome IN ('MISMATCH', 'NOT_FOUND_A', 'NOT_FOUND_B') AND NOT k.provisional)
                                  OR k.bug_suppressed)                  AS n_verdict_different,
               0::bigint                                                AS n_findings,
               0::bigint                                                AS n_findings_unsuppressed,
               0::bigint                                                AS n_critical_unsuppressed,
               0::bigint                                                AS n_critical_unexplained,
               0::bigint                                                AS n_high_unsuppressed,
               count(*) FILTER (WHERE k.provisional AND ru.status = 'cancelled')  AS n_cancelled,
               count(*) FILTER (WHERE k.provisional AND ru.status <> 'cancelled') AS n_outstanding
        FROM keyed AS k
        GROUP BY GROUPING SETS ((), (k.stratum))
        UNION ALL
        -- INCONCLUSIVE keys per reason
        SELECT CASE WHEN GROUPING(k.stratum) = 1 THEN '*' ELSE k.stratum END AS stratum,
               0 AS n_manifest, 0 AS n_results, 0 AS n_keys, 0 AS n_match, 0 AS n_absent_both,
               0 AS n_mismatch, 0 AS n_known_diff, 0 AS n_known_diff_bug, 0 AS n_noise_only,
               0 AS n_drift, 0 AS n_not_found_a, 0 AS n_not_found_b, 0 AS n_inconclusive,
               0 AS n_api_uncomparable,
               k.inconclusive_reason::text AS reason,
               count(*) AS reason_n,
               0 AS n_rechecked, 0 AS n_provisional, 0 AS n_schema_drift, 0 AS n_discrepant,
               0 AS n_discrepant_all_suppressed, 0 AS n_compared, 0 AS n_clean, 0 AS n_contrast,
               0 AS n_verdict_different,
               0 AS n_findings, 0 AS n_findings_unsuppressed, 0 AS n_critical_unsuppressed,
               0 AS n_critical_unexplained, 0 AS n_high_unsuppressed,
               0 AS n_cancelled, 0 AS n_outstanding
        FROM keyed AS k
        WHERE k.outcome = 'INCONCLUSIVE'
        GROUP BY GROUPING SETS ((k.inconclusive_reason), (k.stratum, k.inconclusive_reason))
        UNION ALL
        -- findings of each key's latest cycle
        SELECT CASE WHEN GROUPING(k.stratum) = 1 THEN '*' ELSE k.stratum END AS stratum,
               0 AS n_manifest, 0 AS n_results, 0 AS n_keys, 0 AS n_match, 0 AS n_absent_both,
               0 AS n_mismatch, 0 AS n_known_diff, 0 AS n_known_diff_bug, 0 AS n_noise_only,
               0 AS n_drift, 0 AS n_not_found_a, 0 AS n_not_found_b, 0 AS n_inconclusive,
               0 AS n_api_uncomparable,
               NULL AS reason, NULL AS reason_n,
               0 AS n_rechecked, 0 AS n_provisional, 0 AS n_schema_drift, 0 AS n_discrepant,
               0 AS n_discrepant_all_suppressed, 0 AS n_compared, 0 AS n_clean, 0 AS n_contrast,
               0 AS n_verdict_different,
               count(*)                                                     AS n_findings,
               count(*) FILTER (WHERE NOT f.suppressed)                     AS n_findings_unsuppressed,
               count(*) FILTER (WHERE NOT f.suppressed AND f.severity = 'CRITICAL') AS n_critical_unsuppressed,
               count(*) FILTER (WHERE f.severity = 'CRITICAL'
                                  AND (NOT f.suppressed OR kd.classification = 'bug')) AS n_critical_unexplained,
               count(*) FILTER (WHERE NOT f.suppressed AND f.severity = 'HIGH') AS n_high_unsuppressed,
               0 AS n_cancelled, 0 AS n_outstanding
        FROM keyed AS k
        JOIN ds.findings AS f
          ON f.run_id = ru.run_id AND f.cycle_no = k.cycle_no AND f.key = k.key
        LEFT JOIN ds.known_diffs AS kd ON kd.rule_id = f.rule_id
        GROUP BY GROUPING SETS ((), (k.stratum))
        UNION ALL
        -- manifest keys: total, and those without any result
        SELECT CASE WHEN GROUPING(m.stratum) = 1 THEN '*' ELSE m.stratum END AS stratum,
               count(*) AS n_manifest,
               0 AS n_results, 0 AS n_keys, 0 AS n_match, 0 AS n_absent_both,
               0 AS n_mismatch, 0 AS n_known_diff, 0 AS n_known_diff_bug, 0 AS n_noise_only,
               0 AS n_drift, 0 AS n_not_found_a, 0 AS n_not_found_b, 0 AS n_inconclusive,
               0 AS n_api_uncomparable,
               NULL AS reason, NULL AS reason_n,
               0 AS n_rechecked, 0 AS n_provisional, 0 AS n_schema_drift, 0 AS n_discrepant,
               0 AS n_discrepant_all_suppressed, 0 AS n_compared, 0 AS n_clean, 0 AS n_contrast,
               0 AS n_verdict_different,
               0 AS n_findings, 0 AS n_findings_unsuppressed, 0 AS n_critical_unsuppressed,
               0 AS n_critical_unexplained, 0 AS n_high_unsuppressed,
               count(*) FILTER (WHERE NOT m.has_result AND ru.status = 'cancelled')  AS n_cancelled,
               count(*) FILTER (WHERE NOT m.has_result AND ru.status <> 'cancelled') AS n_outstanding
        FROM manifest AS m
        GROUP BY GROUPING SETS ((), (m.stratum))
    ) AS u
    GROUP BY u.stratum
) AS s
WHERE ru.agent_visible;
COMMENT ON VIEW ds.v_run_summary IS
    'Curated (ds_agent_ro). Run header (dataset_id, status, claim_level, time_mode, manifest_hash) plus coverage per run (stratum = ''*'') and per stratum over distinct keys at their latest cycle: outcome counts, INCONCLUSIVE reasons (absent_both included), n_provisional, n_schema_drift, n_compared (INCONCLUSIVE, DRIFT and provisional excluded), the sign-off numbers over members only (n_clean, n_discrepant with bug-rule KNOWN_DIFF keys, mismatch_rate, the one-sided zero-failure bound, NULL for ''*''; other labelled keys in n_contrast), the derived verdict counts and the run counters (n_manifest = n_verdict_match + n_verdict_different + n_verdict_indeterminate + n_cancelled + n_outstanding, provisional keys outstanding), coverage = n_compared / n_manifest, and unsuppressed/unexplained CRITICAL and HIGH finding counts for sign-off. Filter on run_id and dataset_id; each run is aggregated from its own partition.';

-- Findings without exact pointers. json_pointer is left out: its member keys
-- are tokenised, but path_pattern plus finding_id is enough to group and to
-- cite, and exact pointers are resolved only by the audited reveal endpoint.
-- value_*_tok and explaining_id_tok are already policy-applied (tokens for
-- tokenised classes) and are capped at 256 characters to bound agent
-- payloads. evidence_expired is true when an observation the key's
-- result cites has expired (for a rediff run the observations of the parent
-- run). Only runs with agent_visible = true are exposed.
CREATE VIEW ds.v_findings_masked
WITH (security_barrier = true, security_invoker = false) AS
SELECT
    f.finding_id,
    f.run_id,
    ru.dataset_id,
    f.cycle_no,
    f.key,
    f.stratum,
    f.path_pattern,
    f.change_type,
    f.direction,
    f.value_class,
    left(f.value_a_tok, 256)      AS value_a_tok,
    left(f.value_b_tok, 256)      AS value_b_tok,
    left(f.value_diff_desc, 512)  AS value_diff_desc,
    f.severity,
    f.taxonomy_code,
    f.signature_hash,
    f.root_cause_id,
    f.rule_id,
    f.mask_id,
    f.suppressed,
    left(f.member_key_tok, 256)   AS member_key_tok,
    left(f.anchor_key_tok, 256)   AS anchor_key_tok,
    f.link_reason,
    left(f.explaining_id_tok, 256) AS explaining_id_tok,
    f.hop_count,
    EXISTS (
        SELECT 1
        FROM ds.results AS r
        JOIN ds.observations AS o
          ON o.run_id IN (r.run_id, ru.parent_run_id) AND o.obs_id IN (r.obs_a_id, r.obs_b_id)
        WHERE r.run_id = f.run_id AND r.cycle_no = f.cycle_no AND r.key = f.key
          AND o.expired_at IS NOT NULL) AS evidence_expired,
    f.created_at
FROM ds.findings AS f
JOIN ds.runs AS ru ON ru.run_id = f.run_id
WHERE ru.agent_visible;
COMMENT ON VIEW ds.v_findings_masked IS
    'Curated (ds_agent_ro). Findings with tokenized, length-capped values, the member key (member_key_tok), the graph anchor key (anchor_key_tok), the optional explaining identifier (link_reason type + explaining_id_tok token, domain-pack enrichment), evidence_expired and no exact json_pointer. Carries dataset_id for scope filtering. Hidden runs are excluded.';

CREATE VIEW ds.v_root_causes
WITH (security_barrier = true, security_invoker = false) AS
SELECT
    rc.root_cause_id,
    rc.run_id,
    ru.dataset_id,
    ru.spec_name,
    ru.mode,
    rc.kind,
    rc.root_key_tok,
    rc.path_pattern,
    rc.change_type,
    rc.taxonomy_code,
    rc.severity,
    rc.direction,
    rc.affected_keys,
    rc.finding_count,
    rc.sample_keys,
    rc.signature_hash,
    rc.rule_id,
    kd.classification      AS rule_classification,
    kd.ticket              AS rule_ticket,
    rc.cluster_id,
    c.label                AS cluster_label,
    c.hypothesis           AS cluster_hypothesis,
    c.suspected_component  AS cluster_suspected_component,
    c.confidence           AS cluster_confidence,
    c.review_state         AS cluster_review_state,
    rc.status,
    rc.summary,
    rc.evidence,
    rc.created_at,
    rc.updated_at
FROM ds.root_causes AS rc
JOIN ds.runs AS ru ON ru.run_id = rc.run_id
LEFT JOIN ds.known_diffs AS kd ON kd.rule_id = rc.rule_id
LEFT JOIN ds.clusters AS c ON c.cluster_id = rc.cluster_id
WHERE ru.agent_visible;
COMMENT ON VIEW ds.v_root_causes IS
    'Curated (ds_agent_ro). Root causes (kind member_key, graph_anchor, identifier, attachment, rule, signature) with tokenized root keys, the member path_pattern and change_type for member_key (change_type for graph_anchor), their direction, the known-diff classification if any, and the cluster label and hypothesis with their review state. Carries dataset_id for scope filtering. Hidden runs are excluded.';

-- Per-key outcomes (matches included) without canonical hashes: compare_runs
-- (new / fixed / persistent / flapping between runs or cycles), the
-- validator's check that cited keys and obs_ids exist, and get_observations.
-- evidence_expired: an observation the result cites has expired.
CREATE VIEW ds.v_results_masked
WITH (security_barrier = true, security_invoker = false) AS
SELECT
    r.run_id,
    ru.dataset_id,
    r.cycle_no,
    r.key,
    r.stratum,
    r.outcome,
    r.inconclusive_reason,
    r.drift_reason,
    r.confirmation,
    r.side_a_state,
    r.side_b_state,
    r.signoff_stratum,
    r.signoff_member,
    -- derived verdict (same CASE as v_result_verdicts)
    CASE
        WHEN r.outcome IN ('MISMATCH', 'NOT_FOUND_A', 'NOT_FOUND_B')
             AND r.confirmation = 'provisional'                     THEN NULL::ds.verdict   -- not yet reproduced
        WHEN r.outcome IN ('MATCH', 'NOISE_ONLY')                   THEN 'MATCH'::ds.verdict
        WHEN r.outcome IN ('MISMATCH', 'NOT_FOUND_A', 'NOT_FOUND_B') THEN 'DIFFERENT'::ds.verdict
        WHEN r.outcome IN ('INCONCLUSIVE', 'DRIFT')                 THEN 'INDETERMINATE'::ds.verdict
        WHEN EXISTS (
            SELECT 1
            FROM ds.findings AS f
            JOIN ds.known_diffs AS kd ON kd.rule_id = f.rule_id
            WHERE f.run_id = r.run_id AND f.cycle_no = r.cycle_no AND f.key = r.key
              AND f.suppressed AND kd.classification = 'bug')      THEN 'DIFFERENT'::ds.verdict
        ELSE 'MATCH'::ds.verdict
    END                           AS verdict,
    r.first_outcome,
    r.revision,
    r.recheck_count,
    r.obs_a_id,
    r.obs_b_id,
    r.attrs,
    r.finding_count,
    r.suppressed_count,
    r.worst_severity,
    r.spec_version,
    EXISTS (
        SELECT 1
        FROM ds.observations AS o
        WHERE o.run_id IN (r.run_id, ru.parent_run_id) AND o.obs_id IN (r.obs_a_id, r.obs_b_id)
          AND o.expired_at IS NOT NULL) AS evidence_expired,
    r.compared_at
FROM ds.results AS r
JOIN ds.runs AS ru ON ru.run_id = r.run_id
WHERE ru.agent_visible;
COMMENT ON VIEW ds.v_results_masked IS
    'Curated (ds_agent_ro). One row per key and cycle, matches included, without hash_a/hash_b: outcome, inconclusive and drift reasons, confirmation, per-side states, sign-off stratum and membership, the derived verdict (NULL while a discrepant outcome is provisional), first_outcome and revision, and evidence_expired. For a rediff run obs_a_id/obs_b_id point into the parent run. Carries dataset_id for scope filtering. Hidden runs are excluded.';

-- Observation metadata without blob_uri, content_hash, request_hash or
-- response headers. content_hash is an unkeyed digest of the body, so a
-- low-entropy body could be confirmed by guessing. get_observations
-- loads bodies by obs_id through the internal blob service (never the agent)
-- and re-runs the pure comparer for the engine diff.
CREATE VIEW ds.v_observations_meta
WITH (security_barrier = true, security_invoker = false) AS
SELECT
    o.obs_id,
    o.run_id,
    ru.dataset_id,
    o.cycle_no,
    o.key,
    o.side,
    o.call_seq,
    o.probe,
    o.target_id,
    o.http_status,
    o.error_class,
    o.latency_ms,
    o.observed_at,
    o.bytes,
    o.truncated,
    o.expired_at IS NOT NULL AS evidence_expired
FROM ds.observations AS o
JOIN ds.runs AS ru ON ru.run_id = o.run_id
WHERE ru.agent_visible;
COMMENT ON VIEW ds.v_observations_meta IS
    'Curated (ds_agent_ro). One row per call made to a target: status, error class, latency, truncation and evidence_expired; no blob_uri and no content_hash. Carries dataset_id for scope filtering. Hidden runs are excluded.';

-- Suppressed findings per rule or mask: get_run_summary's "suppressed counts
-- per rule". Counts cover all cycles of the run; n_keys counts distinct keys.
CREATE VIEW ds.v_rule_hits
WITH (security_barrier = true, security_invoker = false) AS
SELECT
    ru.run_id,
    ru.dataset_id,
    h.rule_id,
    h.mask_id,
    kd.classification,
    h.n_findings,
    h.n_keys
FROM ds.runs AS ru
CROSS JOIN LATERAL (
    SELECT f.rule_id,
           f.mask_id,
           count(*)              AS n_findings,
           count(DISTINCT f.key) AS n_keys
    FROM ds.findings AS f
    WHERE f.run_id = ru.run_id
      AND f.suppressed
    GROUP BY f.rule_id, f.mask_id
) AS h
LEFT JOIN ds.known_diffs AS kd ON kd.rule_id = h.rule_id
WHERE ru.agent_visible;
COMMENT ON VIEW ds.v_rule_hits IS
    'Curated (ds_agent_ro). Suppressed findings per known_diffs rule (with its classification; bug rules still count as discrepant in v_run_summary) or noise mask, over all cycles of the run. Carries dataset_id for scope filtering. Hidden runs are excluded.';

-- =============================================================================
-- 10d. Privileges
-- =============================================================================
GRANT USAGE ON SCHEMA ds TO ds_app, ds_agent_ro;

-- ds_app: least privilege per table.
GRANT SELECT, INSERT, UPDATE, DELETE ON
    ds.secrets_ref, ds.targets, ds.runs, ds.run_keys, ds.work_units,
    ds.findings, ds.signatures, ds.root_causes, ds.clusters,
    ds.known_diffs, ds.noise_masks, ds.reports, ds.users_roles
TO ds_app;
-- Templates and inferred schemas: approval and retirement only (column-
-- level UPDATE; the trigger freezes approved rows); no DELETE (retire instead)
GRANT SELECT, INSERT ON ds.sql_templates, ds.inferred_schemas TO ds_app;
GRANT UPDATE (status, approved_by, approved_at) ON ds.sql_templates, ds.inferred_schemas TO ds_app;
GRANT SELECT, INSERT, UPDATE ON ds.results TO ds_app;          -- upsert; retention is by partition (owner)
GRANT SELECT, INSERT ON ds.observations TO ds_app;             -- insert-once facts
GRANT UPDATE (expired_at) ON ds.observations TO ds_app;        -- the lifecycle job marks expired evidence
GRANT SELECT, INSERT ON ds.specs TO ds_app;                    -- immutable
GRANT SELECT, INSERT ON ds.audit_log TO ds_app;                -- append-only
GRANT SELECT, INSERT ON ds.taxonomy_codes TO ds_app;           -- a pack installer adds its codes
-- Chat is audited (chat.turn), so ds_app cannot delete turns, nor sessions
-- (that would cascade to their turns). Retention runs as ds_owner.
GRANT SELECT, INSERT, UPDATE ON ds.chat_sessions, ds.chat_turns TO ds_app;
GRANT SELECT, INSERT, UPDATE ON ds.datasets, ds.llm_profiles TO ds_app;   -- deactivate / disable, never delete
GRANT SELECT, INSERT ON ds.llm_calls TO ds_app;                -- per-call usage log, not rewritten
GRANT SELECT, INSERT, DELETE ON ds.outbox TO ds_app;           -- DELETE = purge of published events
GRANT UPDATE (published_at) ON ds.outbox TO ds_app;            -- events are otherwise immutable
GRANT SELECT, INSERT, DELETE ON ds.processed_events TO ds_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ds.idempotency_keys TO ds_app;
GRANT USAGE ON SEQUENCE ds.outbox_seq_seq TO ds_app;           -- outbox.seq is a bigserial
GRANT SELECT, INSERT, DELETE ON ds.dataset_targets TO ds_app;  -- admin grant / revoke through ds-api (audited)
GRANT SELECT, INSERT ON ds.spec_approvals TO ds_app;           -- insert-only approvals
GRANT SELECT, INSERT ON ds.llm_hosts TO ds_app;                -- add a host; changing one is a migration
GRANT SELECT, INSERT ON ds.llm_promotions TO ds_app;           -- eval evidence is not rewritten ...
GRANT UPDATE (status, approved_by, approved_at) ON ds.llm_promotions TO ds_app;  -- ... only its decision
GRANT SELECT ON ds.v_run_summary, ds.v_findings_masked, ds.v_root_causes,
                ds.v_results_masked, ds.v_observations_meta, ds.v_rule_hits,
                ds.v_result_verdicts TO ds_app;
GRANT USAGE ON SEQUENCE ds.findings_finding_id_seq, ds.observations_obs_id_seq TO ds_app;
GRANT EXECUTE ON FUNCTION ds.create_run_partitions(bigint) TO ds_app;

-- audit_log: append-only for everyone except the owner, and the triggers
-- above catch the owner too.
REVOKE UPDATE, DELETE, TRUNCATE ON ds.audit_log FROM PUBLIC, ds_app, ds_agent_ro;
REVOKE UPDATE, DELETE, TRUNCATE ON ds.specs FROM PUBLIC, ds_app, ds_agent_ro;

-- ds_agent_ro: SELECT on the six curated views and nothing else. The MVP tools
-- (get_run_summary, list_root_causes, get_findings, get_observations,
-- compare_runs; compare_key goes through ds-api) read only these views, which
-- now also carry dataset_id, per-side states, the derived verdict, revisions,
-- the claim level, the run counters and evidence_expired. No grant on
-- llm_profiles, llm_hosts, llm_promotions, llm_calls, outbox,
-- idempotency_keys, datasets, dataset_targets, spec_approvals or
-- v_result_verdicts: the agent never sees LLM cost or pricing.
REVOKE ALL ON ALL TABLES IN SCHEMA ds FROM ds_agent_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA ds FROM ds_agent_ro;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ds FROM PUBLIC, ds_agent_ro;
GRANT SELECT ON ds.v_run_summary, ds.v_findings_masked, ds.v_root_causes,
                ds.v_results_masked, ds.v_observations_meta, ds.v_rule_hits TO ds_agent_ro;

-- =============================================================================
-- 11. Reference queries (commented; executed by the services, not here)
-- =============================================================================
-- 11.1 Lease up to $2 work units of kind $1 (ds-fetcher: 'fetch'; ds-comparer:
--      'compare'). $3 = worker id, $4 = lease duration. SKIP LOCKED lets many
--      workers scan at once without blocking each other. A leased row whose
--      lease expired (available_at <= now()) is claimable again. Units of runs
--      that are not 'running' (paused, cancelling) are skipped. The index
--      work_units_lease_scan_idx serves the WHERE and ORDER BY.
--
-- WITH picked AS (
--     SELECT w.unit_id
--     FROM ds.work_units AS w
--     WHERE w.kind = $1
--       AND w.state IN ('pending', 'leased')
--       AND w.available_at <= now()
--       AND w.attempts < w.max_attempts
--       AND EXISTS (SELECT 1 FROM ds.runs AS r
--                   WHERE r.run_id = w.run_id AND r.status = 'running')
--     ORDER BY w.priority, w.available_at, w.unit_id
--     LIMIT $2
--     FOR UPDATE OF w SKIP LOCKED
-- )
-- UPDATE ds.work_units AS w
--    SET state        = 'leased',
--        lease_owner  = $3,
--        lease_token  = gen_random_uuid(),
--        leased_at    = now(),
--        available_at = now() + $4::interval,
--        attempts     = w.attempts + 1,
--        updated_at   = now()
--   FROM picked
--  WHERE w.unit_id = picked.unit_id
-- RETURNING w.unit_id, w.run_id, w.cycle_no, w.kind, w.payload, w.lease_token, w.attempts;
--
-- 11.2 Heartbeat, complete, or retry, fenced by lease_token. 0 rows = lease lost.
-- UPDATE ds.work_units SET available_at = now() + $3::interval, updated_at = now()
--  WHERE unit_id = $1 AND lease_token = $2 AND state = 'leased';                 -- heartbeat
-- UPDATE ds.work_units
--    SET state = 'done', completed_at = now(), result_summary = $3,
--        lease_owner = NULL, lease_token = NULL, updated_at = now()
--  WHERE unit_id = $1 AND lease_token = $2 AND state = 'leased';                 -- complete
-- UPDATE ds.work_units
--    SET state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
--        available_at = now() + $3::interval,                                     -- backoff with jitter
--        last_error_class = $4, last_error = left($5, 2000),
--        lease_owner = NULL, lease_token = NULL, updated_at = now()
--  WHERE unit_id = $1 AND lease_token = $2 AND state = 'leased';                 -- fail / retry
--
-- 11.3 Reaper (ds-scheduler): expired leases on their last attempt become dead letters.
-- UPDATE ds.work_units
--    SET state = 'dead', lease_owner = NULL, lease_token = NULL, updated_at = now()
--  WHERE state IN ('pending', 'leased') AND attempts >= max_attempts AND available_at <= now();
--
-- 11.4 Idempotent upsert of a result (ds-comparer). Replaying the same write is
--      a no-op. A recheck (higher recheck_count) overwrites. A late, stale write
--      (lower recheck_count) is ignored. RETURNING yields a row only when the
--      table changed; only then are the key's findings replaced (11.5) and an
--      outbox event written (11.8), all in the same transaction, after the
--      evidence was uploaded and hash-verified. first_outcome is set on
--      INSERT only, and revision counts overwrites. confirmation:
--      provisional on the first write, rechecked once a recheck reproduced the
--      outcome, frozen_cut / bulk_exhaustive for frozen or P2 bulk runs.
--      drift_reason is set exactly for DRIFT. signoff_stratum and
--      signoff_member are copied from run_keys.
-- INSERT INTO ds.results AS r
--     (run_id, cycle_no, key, outcome, first_outcome, revision, inconclusive_reason,
--      side_a_state, side_b_state, hash_a, hash_b, stratum,
--      attrs, recheck_count, compared_at, spec_version, obs_a_id, obs_b_id,
--      finding_count, suppressed_count, worst_severity, work_unit_id, comparer_id,
--      confirmation, drift_reason, signoff_stratum, signoff_member)
-- VALUES ($1, $2, $3, $4, $4, 1, $5, $19, $20, $6, $7, $8, $9, $10, now(), $11, $12, $13,
--         $14, $15, $16, $17, $18, $21, $22, $23, $24)
-- ON CONFLICT (run_id, cycle_no, key) DO UPDATE
--    SET outcome             = EXCLUDED.outcome,
--        revision            = r.revision + 1,          -- first_outcome is never updated
--        inconclusive_reason = EXCLUDED.inconclusive_reason,
--        confirmation        = EXCLUDED.confirmation,
--        drift_reason        = EXCLUDED.drift_reason,
--        side_a_state        = EXCLUDED.side_a_state,
--        side_b_state        = EXCLUDED.side_b_state,
--        hash_a              = EXCLUDED.hash_a,
--        hash_b              = EXCLUDED.hash_b,
--        stratum             = EXCLUDED.stratum,
--        signoff_stratum     = EXCLUDED.signoff_stratum,
--        signoff_member      = EXCLUDED.signoff_member,
--        attrs               = EXCLUDED.attrs,
--        recheck_count       = EXCLUDED.recheck_count,
--        compared_at         = EXCLUDED.compared_at,
--        spec_version        = EXCLUDED.spec_version,
--        obs_a_id            = EXCLUDED.obs_a_id,
--        obs_b_id            = EXCLUDED.obs_b_id,
--        finding_count       = EXCLUDED.finding_count,
--        suppressed_count    = EXCLUDED.suppressed_count,
--        worst_severity      = EXCLUDED.worst_severity,
--        work_unit_id        = EXCLUDED.work_unit_id,
--        comparer_id         = EXCLUDED.comparer_id,
--        updated_at          = now()
--  WHERE r.recheck_count < EXCLUDED.recheck_count
-- RETURNING r.run_id, r.cycle_no, r.key, r.outcome, r.first_outcome, r.revision, r.recheck_count;
-- (Do not add the usual "xmax = 0 AS inserted" trick: PostgreSQL cannot return
-- system columns from a partitioned table. Either way the caller does the same
-- thing next: it replaces the key's findings.)
--
-- 11.5 Replace a key's findings in the SAME transaction, only if 11.4 returned a row.
-- DELETE FROM ds.findings WHERE run_id = $1 AND cycle_no = $2 AND key = $3;
-- INSERT INTO ds.findings (run_id, cycle_no, key, path_pattern, json_pointer, change_type,
--                          direction, value_a_tok, value_b_tok, value_class, value_diff_desc,
--                          severity, taxonomy_code, signature_hash, rule_id, mask_id,
--                          suppressed, member_key_tok, anchor_key_tok, link_reason,
--                          explaining_id_tok, hop_count, stratum)
-- VALUES (...), (...)
-- ON CONFLICT (run_id, cycle_no, key, json_pointer, change_type) DO NOTHING;
-- (link_reason, explaining_id_tok and hop_count are NULL unless a domain pack enriches them.
-- anchor_key_tok is set in graph mode only. A NOT_FOUND_B / NOT_FOUND_A key always gets
-- one MISSING_DOCUMENT / EXTRA_DOCUMENT finding at path_pattern '$', json_pointer '',
-- member_key_tok = key.)
--
-- 11.6 Retention of run 42 (ops, as ds_owner). There are no partitioned-to-partitioned
--      FKs, so the three detaches are independent. CONCURRENTLY is unavailable while a
--      DEFAULT partition exists.
-- SET lock_timeout = '3s';
-- ALTER TABLE ds.findings     DETACH PARTITION ds.findings_r42;      DROP TABLE ds.findings_r42;
-- ALTER TABLE ds.results      DETACH PARTITION ds.results_r42;       DROP TABLE ds.results_r42;
-- ALTER TABLE ds.observations DETACH PARTITION ds.observations_r42;  DROP TABLE ds.observations_r42;
-- For runs that live in the DEFAULT partitions: batched DELETE ... WHERE run_id = 42.
-- Then: DELETE FROM ds.runs WHERE run_id = 42;  -- cascades to run_keys, work_units,
--       signatures, root_causes, clusters, reports, outbox (and processed_events);
--       the audit_log is kept. The retention horizon is the spec's retention_class
--       (standard: raw and findings 90 d; audit: 1 y), raw never shorter than findings.
--
-- 11.7 Root causes of kind member_key (generic; architecture.md §8.2): the same
--      array member or graph node/edge missing or extra under at least 2 request
--      keys. No provenance is needed. INFO findings (NORMALIZATION_ONLY, SCHEMA_DRIFT)
--      are excluded. Upserted ON CONFLICT on root_causes_natural_uq with
--      kind = 'member_key', root_key_tok = member_key_tok.
-- SELECT path_pattern, member_key_tok, change_type,
--        count(DISTINCT key) AS affected_keys, count(*) AS finding_count,
--        CASE WHEN bool_and(direction = 'a_only') THEN 'a_only'
--             WHEN bool_and(direction = 'b_only') THEN 'b_only' ELSE 'both' END AS direction
--   FROM ds.findings
--  WHERE run_id = $1 AND cycle_no = $2 AND NOT suppressed AND severity <> 'INFO'
--    AND change_type IN ('missing_in_b', 'extra_in_b') AND member_key_tok IS NOT NULL
--  GROUP BY 1, 2, 3
-- HAVING count(DISTINCT key) >= 2;
--
-- 11.7a Graph mode: a one-sided neighbour's node finding and edge findings share
--      anchor_key_tok, so they are grouped across $.nodes.* and $.edges.* into one root
--      cause of kind graph_anchor (root_key_tok = anchor_key_tok, path_pattern NULL).
-- SELECT anchor_key_tok, change_type,
--        count(DISTINCT key) AS affected_keys, count(*) AS finding_count,
--        CASE WHEN bool_and(direction = 'a_only') THEN 'a_only'
--             WHEN bool_and(direction = 'b_only') THEN 'b_only' ELSE 'both' END AS direction
--   FROM ds.findings
--  WHERE run_id = $1 AND cycle_no = $2 AND NOT suppressed AND severity <> 'INFO'
--    AND change_type IN ('missing_in_b', 'extra_in_b') AND anchor_key_tok IS NOT NULL
--  GROUP BY 1, 2;
--
-- 11.7b Optional domain-pack enrichment, kind identifier: only when a side exposes
--      link provenance, link findings grouped by the explaining identifier (type, token).
-- SELECT link_reason AS id_type, explaining_id_tok AS id_tok,
--        count(DISTINCT key) AS affected_keys, count(*) AS finding_count,
--        CASE WHEN bool_and(direction = 'a_only') THEN 'a_only'
--             WHEN bool_and(direction = 'b_only') THEN 'b_only' ELSE 'both' END AS direction
--   FROM ds.findings
--  WHERE run_id = $1 AND cycle_no = $2 AND taxonomy_code IN ('MISSING_LINK', 'EXTRA_LINK')
--    AND NOT suppressed AND severity <> 'INFO' AND explaining_id_tok IS NOT NULL
--  GROUP BY 1, 2
-- HAVING count(DISTINCT key) >= 2;
--
-- 11.8 Outbox. Producer, inside the 11.4/11.5 transaction:
-- INSERT INTO ds.outbox (run_id, type, payload)
-- VALUES ($1, 'result.committed', jsonb_build_object('cycle_no', $2, 'key', $3, 'outcome', $4));
--      Consumer $1 polls a batch of $2, then per event performs its side effect and
--      records it in ONE transaction. A redelivery hits processed_events_pk. Consumers
--      of different types can briefly skip each other's locked rows, so order is
--      per run (seq) and best-effort; consumers must stay idempotent.
-- SELECT o.event_id, o.run_id, o.seq, o.type, o.payload
--   FROM ds.outbox AS o
--  WHERE o.published_at IS NULL
--    AND NOT EXISTS (SELECT 1 FROM ds.processed_events AS p
--                     WHERE p.consumer = $1 AND p.event_id = o.event_id)
--  ORDER BY o.run_id, o.seq
--  LIMIT $2
--    FOR UPDATE OF o SKIP LOCKED;
-- INSERT INTO ds.processed_events (consumer, event_id) VALUES ($1, $3);
--      Relay: mark events that every registered consumer has processed, then purge
--      after a grace period (cascades to processed_events).
-- UPDATE ds.outbox AS o SET published_at = now()
--  WHERE o.published_at IS NULL
--    AND (SELECT count(*) FROM ds.processed_events AS p WHERE p.event_id = o.event_id) >= $1;
-- DELETE FROM ds.outbox WHERE published_at < now() - $2::interval;
--
-- 11.9 Idempotency-Key (ds-api). $1 = the end user (never a service
--      identity), $3 = sha256(Idempotency-Key) in hex. Claim the key, or reclaim an
--      expired row:
-- INSERT INTO ds.idempotency_keys AS ik (principal, route, key_hash, fingerprint)
-- VALUES ($1, $2, $3, $4)
-- ON CONFLICT (principal, route, key_hash) DO UPDATE
--    SET fingerprint = EXCLUDED.fingerprint, status = 'in_flight',
--        response_status = NULL, resource_ref = NULL,
--        created_at = now(), expires_at = now() + interval '24 hours'
--  WHERE ik.expires_at <= now()
-- RETURNING ik.status;
--      1 row  -> execute the request. For POST /v1/runs, in ONE transaction: insert the
--      run (requested_by = $1, idempotency_route = $2, idempotency_key_hash = $3; a
--      duplicate hits runs_idempotency_uq), its outbox event, and complete the key with
--      references only:
-- UPDATE ds.idempotency_keys SET status = 'completed', response_status = 202,
--        resource_ref = jsonb_build_object('run_id', $5)
--  WHERE principal = $1 AND route = $2 AND key_hash = $3 AND status = 'in_flight';
--      A 5xx before the resource exists is not stored (idempotency_keys_no_bare_5xx):
--      delete the in_flight row instead so the client can retry.
--      0 rows -> SELECT fingerprint, status, response_status, resource_ref for the key:
--                fingerprint <> $4 -> 422; status = 'in_flight' -> 409; else re-run the
--                caller's authorisation (role, dataset scope) and re-render the response
--                from resource_ref.
-- Purge (hourly): DELETE FROM ds.idempotency_keys WHERE expires_at <= now();
--
-- 11.10 Report staleness by commit order (needs track_commit_timestamp = on).
--      Every part of report $1 is rendered in one exported snapshot
--      (SELECT pg_export_snapshot() -> reports.snapshot_id; each worker runs
--      SET TRANSACTION ISOLATION LEVEL REPEATABLE READ; SET TRANSACTION SNAPSHOT '<id>').
--      result_revision_at is that snapshot's time. The report is stale when a result
--      committed later:
-- SELECT EXISTS (SELECT 1 FROM ds.results AS r JOIN ds.reports AS p ON p.run_id = r.run_id
--                 WHERE p.report_id = $1
--                   AND pg_xact_commit_timestamp(r.xmin) > p.result_revision_at) AS stale;

COMMIT;
