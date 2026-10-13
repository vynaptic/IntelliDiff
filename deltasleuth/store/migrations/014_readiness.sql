-- Cut-over readiness (docs/mvp/READINESS.md §4): each ended cycle's measures, written once by the worker
-- and kept with the tombstone; one-shot and captured-sample cycles are marked excluded here and by end_cycle.
ALTER TABLE ds.cycles ADD COLUMN readiness jsonb
    CHECK (jsonb_typeof(readiness) = 'object' AND readiness ? 'format'
           AND readiness ?| ARRAY['pair', 'excluded']);
UPDATE ds.cycles c SET readiness = jsonb_build_object('format', 1, 'excluded',
        CASE WHEN r.mode = 'one_shot' THEN 'one_shot' ELSE 'captured_samples' END)
FROM ds.runs r
WHERE r.run_id = c.run_id AND (r.mode = 'one_shot' OR r.params->>'samples_dir' IS NOT NULL);
CREATE INDEX cycles_unmeasured_idx ON ds.cycles (ended_at DESC)
    WHERE readiness IS NULL OR (readiness ? 'error' AND NOT readiness ? 'final');
CREATE INDEX cycles_unmeasured_run_idx ON ds.cycles (run_id, cycle) WHERE readiness IS NULL;
CREATE INDEX cycles_readiness_pair_idx ON ds.cycles ((readiness->>'pair'), ended_at DESC)
    WHERE readiness ? 'pair';
CREATE INDEX cycles_readiness_unread_idx ON ds.cycles ((readiness->>'pair'), ended_at DESC)
    WHERE readiness ? 'pair' AND readiness->'format' <> '1';
CREATE INDEX cycles_readiness_evidence_idx
    ON ds.cycles ((readiness->>'pair'), (readiness->>'spec_id'), ended_at DESC)
    WHERE readiness @> '{"evidence": true, "format": 1}';
CREATE INDEX cycles_readiness_breaks_standard_idx ON ds.cycles ((readiness->>'pair'), ended_at DESC)
    WHERE readiness->'fails'->'standard' <> '[]';
CREATE INDEX cycles_readiness_breaks_high_idx ON ds.cycles ((readiness->>'pair'), ended_at DESC)
    WHERE readiness->'fails'->'high' <> '[]';
