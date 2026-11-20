-- A spec approval may cover the transforms scope: a side's map, or its select of part of an answer.
ALTER TABLE ds.approvals DROP CONSTRAINT approvals_scopes_check, ADD CONSTRAINT approvals_scopes_check
    CHECK (scopes <@ '{clear,unlisted_clear,known_diffs,population_filters,sampling,transforms,raw_clear}');
