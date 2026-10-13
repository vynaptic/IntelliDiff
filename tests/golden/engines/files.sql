-- The golden corpus of canonical rendering 1 for files (docs/mvp/FILES.md §8, §18), as DuckDB's own writer
-- writes it to Parquet: one column per type a Parquet column is read as, with edge values (corpus), and the
-- values every engine must render and digest alike (portable, its c_char a padded CSV field's). files.json
-- and portable.json pin what each row renders as.
CREATE TABLE corpus (
    k VARCHAR, c_tinyint TINYINT, c_integer INTEGER, c_bigint BIGINT, c_ubigint UBIGINT,
    c_decimal DECIMAL(10, 2), c_wide DECIMAL(38, 10), c_double DOUBLE, c_float FLOAT, c_varchar VARCHAR,
    c_uuid UUID, c_boolean BOOLEAN, c_date DATE, c_time TIME, c_timestamp TIMESTAMP, c_millis TIMESTAMP_MS,
    c_nanos TIMESTAMP_NS, c_instant TIMESTAMPTZ, c_blob BLOB, c_json JSON, c_interval INTERVAL);
INSERT INTO corpus VALUES
    ('typical', 7, -42, 9007199254740993, 18446744073709551615, 1.50, 123456789012345678.1234567891, 0.1,
     1.1234567, 'héllo 😀', 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', true, '2024-02-29', '03:04:05.12',
     '2024-05-01 12:00:00.12', '2024-05-01 12:00:00.125', '2024-05-01 12:00:00.123456789',
     '2024-05-01 14:00:00.5+02', '\x00\xFF'::BLOB, '{"a": 1.0, "b": [1e2, "x"]}', INTERVAL '1 day 2 hours'),
    ('edges', -128, 2147483647, -9223372036854775808, 0, -0.50, -0.0000000001, 'NaN', 'Infinity', '', NULL,
     false, '1000-01-01', '00:00:00', '9999-12-31 23:59:59.999999', '1970-01-01 00:00:00',
     '1970-01-01 00:00:00.000000001', '1970-01-01 00:00:01+00', ''::BLOB, 'null', INTERVAL '1 month'),
    ('floats', NULL, NULL, NULL, NULL, 2.00, 0.0000010000, 1e300, 1.1234599, NULL, NULL, NULL, NULL, NULL, NULL,
     NULL, NULL, NULL, NULL, NULL, NULL),
    ('nulls', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
     NULL, NULL, NULL, NULL);

CREATE TABLE portable (
    k VARCHAR, c_integer BIGINT, c_decimal DECIMAL(10, 2), c_double DOUBLE, c_real FLOAT, c_text VARCHAR,
    c_date DATE, c_timestamp TIMESTAMP, c_instant TIMESTAMPTZ, c_time TIME, c_bytes BLOB, c_json JSON);
INSERT INTO portable VALUES
    ('typical', 9007199254740993, 1.50, 0.1, 1.1234567, 'héllo 😀', '2024-02-29', '2024-05-01 12:00:00.120000',
     '2024-05-01 14:00:00.5+02', '03:04:05.120000', '\x00\xFF'::BLOB, '{"a": 1.0, "b": [100, "x"], "c": null}'),
    ('edges', -9223372036854775808, -0.50, 1e300, 1.1234599, '', '1000-01-01', '9999-12-31 23:59:59.999999',
     '1970-01-01 00:00:01+00', '23:59:59.999999', ''::BLOB, '[]'),
    ('small', 0, 0.00, -1e-7, 16777216, ' padded ', '2000-01-01', '2000-01-01 00:00:00',
     '2038-01-19 03:14:07+00', '00:00:00', '\x00\x01\x02'::BLOB, '"text"'),
    ('nulls', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
    ('power', NULL, NULL, 18014398509481984, 33554432, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
    ('beyond', NULL, NULL, 52990648348713776, 46056592, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
