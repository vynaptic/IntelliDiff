-- Demo databases for examples/tables/spec.yaml: a legacy CRM's customers and the new platform's
-- users, on one PostgreSQL server. It drops and recreates both databases, so run it as a role that
-- may create databases:
--   psql -v ON_ERROR_STOP=1 -f examples/tables/seed.sql postgresql://postgres@localhost:5432/postgres
--
-- The CRM keeps closed accounts (100050, 100150, ..., 100950 and 100013); the platform migrated only
-- the others, and the spec's filter on side a leaves closed accounts out. The CRM stores created_at
-- as local time in Europe/London without a zone, the platform as timestamptz; the spec's typeMap
-- reads both as the same instant.
--
-- Customers 100001-101000 agree, except for these differences on the platform side:
--   100003  the e-mail in other case, with a trailing space   MATCH (email_basic reconciles it)
--   100005  another phone number                              MISMATCH
--   100007  status suspended instead of active                MISMATCH
--   100009  balance 21.25 instead of 11.25                    MISMATCH
--   100011  missing                                           NOT_FOUND_B
--   100013  active, while closed in the CRM                   MISMATCH (FILTER_MEMBERSHIP)
--   101001  only on the platform                              NOT_FOUND_A

DROP DATABASE IF EXISTS ds_demo_legacy WITH (FORCE);
DROP DATABASE IF EXISTS ds_demo_platform WITH (FORCE);
CREATE DATABASE ds_demo_legacy;
CREATE DATABASE ds_demo_platform;

\connect ds_demo_legacy
CREATE SCHEMA crm;
CREATE TABLE crm.customers (
    customer_id   integer PRIMARY KEY,
    email_address text NOT NULL,
    full_name     text NOT NULL,
    phone         text,
    status        text NOT NULL,
    country       char(2) NOT NULL,
    balance       numeric(12, 2) NOT NULL,
    created_at    timestamp NOT NULL,       -- local time in Europe/London, without a zone
    branch_code   text                      -- not in the spec's columns: never read
);
INSERT INTO crm.customers
SELECT 100000 + g, 'customer' || g || '@example.com', 'Customer ' || g, '+447700' || (900000 + g),
       CASE WHEN g % 100 = 50 OR g = 13 THEN 'closed' WHEN g % 10 = 0 THEN 'suspended' ELSE 'active' END,
       CASE WHEN g % 3 = 0 THEN 'IE' ELSE 'GB' END, g * 1.25,
       (timestamptz '2020-01-01 00:00:00+00' + g * interval '1 day 37 minutes') AT TIME ZONE 'Europe/London',
       'BR' || g % 40
FROM generate_series(1, 1000) AS g;

\connect ds_demo_platform
CREATE TABLE public.users (
    user_id          bigint PRIMARY KEY,
    email            text NOT NULL,
    name             text NOT NULL,
    phone            text,
    status           text NOT NULL,
    country          text NOT NULL,
    balance          numeric(12, 2) NOT NULL,
    created_at       timestamptz NOT NULL,
    marketing_opt_in boolean NOT NULL DEFAULT false,   -- only on this side: listed, not compared
    updated_at       timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.users (user_id, email, name, phone, status, country, balance, created_at)
SELECT 100000 + g, 'customer' || g || '@example.com', 'Customer ' || g, '+447700' || (900000 + g),
       CASE WHEN g % 10 = 0 THEN 'suspended' ELSE 'active' END, CASE WHEN g % 3 = 0 THEN 'IE' ELSE 'GB' END,
       g * 1.25, timestamptz '2020-01-01 00:00:00+00' + g * interval '1 day 37 minutes'
FROM generate_series(1, 1000) AS g
WHERE g <> 11 AND g % 100 <> 50;
UPDATE public.users SET email = 'Customer3@Example.com ' WHERE user_id = 100003;
UPDATE public.users SET phone = '+447700900555' WHERE user_id = 100005;
UPDATE public.users SET status = 'suspended' WHERE user_id = 100007;
UPDATE public.users SET balance = 21.25 WHERE user_id = 100009;
INSERT INTO public.users (user_id, email, name, phone, status, country, balance, created_at)
VALUES (101001, 'new.customer@example.com', 'New Customer', NULL, 'active', 'GB', 0, now());
