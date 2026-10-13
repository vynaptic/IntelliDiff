-- The golden corpus of canonical rendering 1 (docs/mvp/ENGINES.md §9, §16) on PostgreSQL: one column per
-- supported type with edge values (corpus), the values every engine must render and digest alike
-- (portable) and decimal keys (amounts). expected.json and portable.json pin what each row renders as.
CREATE TYPE mood AS ENUM ('sad', 'ok', 'happy');
CREATE TABLE corpus (
    k text PRIMARY KEY, c_smallint smallint, c_integer integer, c_bigint bigint, c_numeric numeric(10, 2),
    c_unscaled numeric, c_double double precision, c_real real, c_text text, c_varchar varchar(50),
    c_uuid uuid, c_enum mood, c_char char(5), c_boolean boolean, c_date date, c_timestamp timestamp,
    c_timestamp3 timestamp(3), c_instant timestamptz, c_time time, c_time3 time(3), c_bytea bytea,
    c_json json, c_jsonb jsonb, c_interval interval, c_array integer[]);
INSERT INTO corpus VALUES
    ('typical', 7, -42, 9007199254740993, 1.50, 1.5, 0.1, 1.1234567, 'héllo 😀', 'a b', 
     'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', 'ok', 'ab', true, '2024-02-29', '2024-05-01 12:00:00.120000',
     '2024-05-01 12:00:00.125', '2024-05-01 14:00:00+02', '03:04:05.120000', '03:04:05.120', '\x00ff',
     '{"a": 1.0, "b": [1e2, "x"]}', '{"a": 1.0, "b": [1e2, "x"]}', '1 day 02:03:04', '{1,2}'),
    ('edges', -32768, 2147483647, -9223372036854775808, -0.50, 1e30, 'NaN', 'Infinity', '', ' padded ',
     NULL, 'happy', '', false, '0044-03-15 BC', '0044-03-15 00:00:00 BC', '0044-03-15 00:00:00',
     '0044-03-15 00:00:00+00 BC', '00:00:00', '23:59:59.999', '\x', 'null', '{"n": 1.10}', '-1 mons', '{}'),
    ('floats', NULL, NULL, NULL, 2.00, 0.000001, 1e300, 1.1234599, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
     NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
    ('nulls', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
     NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);

CREATE TABLE portable (
    k text PRIMARY KEY, c_integer bigint, c_decimal numeric(10, 2), c_double double precision, c_real real,
    c_text varchar(50), c_char char(5), c_date date, c_timestamp timestamp, c_instant timestamptz,
    c_time time, c_bytes bytea, c_json jsonb);
INSERT INTO portable VALUES
    ('typical', 9007199254740993, 1.50, 0.1, 1.1234567, 'héllo 😀', 'ab', '2024-02-29',
     '2024-05-01 12:00:00.120000', '2024-05-01 14:00:00.5+02', '03:04:05.120000', '\x00ff',
     '{"a": 1.0, "b": [100, "x"], "c": null}'),
    ('edges', -9223372036854775808, -0.50, 1e300, 1.1234599, '', '', '1000-01-01', '9999-12-31 23:59:59.999999',
     '1970-01-01 00:00:01+00', '23:59:59.999999', '\x', '[]'),
    ('small', 0, 0.00, -1e-7, 16777216, ' padded ', 'a   b', '2000-01-01', '2000-01-01 00:00:00',
     '2038-01-19 03:14:07+00', '00:00:00', '\x000102', '"text"'),
    ('nulls', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
    -- Whole floats at 2^54 and 2^25 and above them, where PostgreSQL's own text is a digit longer than the
    -- shortest that reads back.
    ('power', NULL, NULL, 18014398509481984, 33554432, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
    ('beyond', NULL, NULL, 52990648348713776, 46056592, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);

CREATE TABLE amounts (k numeric(10, 2) PRIMARY KEY, v integer);
INSERT INTO amounts VALUES (1.50, 1), (2.00, 2), (-0.50, 3), (10.00, 4);
