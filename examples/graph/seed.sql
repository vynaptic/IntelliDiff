-- Demo databases for examples/graph/spec.yaml: which identifiers (VAT, DUNS, LEI) link which
-- organisations, in a legacy CRM and in the new platform's export, on one PostgreSQL server. It drops and
-- recreates both databases, so run it as a role that may create databases:
--   psql -v ON_ERROR_STOP=1 -f examples/graph/seed.sql postgresql://postgres@localhost:5432/postgres
--
-- Entities ORG-0001 to ORG-0200 each hold a VAT and a DUNS number, the same on both sides. Then:
--   merge       the platform also links ORG-1002 to ORG-1001's VAT number
--   split       the platform no longer links ORG-2002 to the DUNS number it shares with ORG-2001
--   rearrange   two DUNS numbers swap ORG-3002 and ORG-3003
--   hot         30 entities share the placeholder VAT GB000000000 (29 on the platform); on the
--               platform, ORG-4031 also joins the LEI of ORG-4001, one of the 30

DROP DATABASE IF EXISTS ds_graph_legacy WITH (FORCE);
DROP DATABASE IF EXISTS ds_graph_platform WITH (FORCE);
CREATE DATABASE ds_graph_legacy;
CREATE DATABASE ds_graph_platform;

\connect ds_graph_legacy
CREATE SCHEMA crm;
CREATE TABLE crm.entity_identifiers (
    entity_code text NOT NULL,
    id_type     text NOT NULL,
    id_value    text NOT NULL,
    PRIMARY KEY (entity_code, id_type, id_value)            -- serves L2's member lookups
);
CREATE INDEX ON crm.entity_identifiers (id_type, id_value);  -- serves L2's identifier lookups
INSERT INTO crm.entity_identifiers
SELECT 'ORG-' || lpad(g::text, 4, '0'), t, CASE t WHEN 'vat' THEN 'GB1' || lpad(g::text, 8, '0') ELSE '1' || lpad(g::text, 8, '0') END
FROM generate_series(1, 200) AS g CROSS JOIN (VALUES ('vat'), ('duns')) AS types (t);
INSERT INTO crm.entity_identifiers VALUES
    ('ORG-1001', 'vat', 'GB900001001'), ('ORG-1002', 'vat', 'GB900001002'),
    ('ORG-2001', 'duns', '900002001'), ('ORG-2002', 'duns', '900002001'), ('ORG-2002', 'vat', 'GB900002002'),
    ('ORG-3001', 'duns', '900003001'), ('ORG-3002', 'duns', '900003001'),
    ('ORG-3003', 'duns', '900003003'), ('ORG-3004', 'duns', '900003003'),
    ('ORG-4001', 'lei', 'LEI00000000000004001'), ('ORG-4031', 'vat', 'GB900004031');
INSERT INTO crm.entity_identifiers
SELECT 'ORG-' || (4000 + g), 'vat', 'GB000000000' FROM generate_series(1, 30) AS g;

\connect ds_graph_platform
CREATE TABLE public.links_export (
    org_code text NOT NULL,
    kind    text NOT NULL,
    value   text NOT NULL,
    PRIMARY KEY (org_code, kind, value)
);
CREATE INDEX ON public.links_export (kind, value);
INSERT INTO public.links_export
SELECT 'ORG-' || lpad(g::text, 4, '0'), t, CASE t WHEN 'vat' THEN 'GB1' || lpad(g::text, 8, '0') ELSE '1' || lpad(g::text, 8, '0') END
FROM generate_series(1, 200) AS g CROSS JOIN (VALUES ('vat'), ('duns')) AS types (t);
INSERT INTO public.links_export VALUES
    ('ORG-1001', 'vat', 'GB900001001'), ('ORG-1002', 'vat', 'GB900001002'), ('ORG-1002', 'vat', 'GB900001001'),
    ('ORG-2001', 'duns', '900002001'), ('ORG-2002', 'vat', 'GB900002002'),
    ('ORG-3001', 'duns', '900003001'), ('ORG-3003', 'duns', '900003001'),
    ('ORG-3002', 'duns', '900003003'), ('ORG-3004', 'duns', '900003003'),
    ('ORG-4001', 'lei', 'LEI00000000000004001'), ('ORG-4031', 'vat', 'GB900004031'),
    ('ORG-4031', 'lei', 'LEI00000000000004001');
INSERT INTO public.links_export
SELECT 'ORG-' || (4000 + g), 'vat', 'GB000000000' FROM generate_series(1, 29) AS g;
