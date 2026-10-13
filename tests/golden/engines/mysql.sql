-- The golden corpus of canonical rendering 1 on MySQL: one column per
-- supported type with edge values (corpus), the values every engine must render and digest alike
-- (portable) and decimal keys (amounts). expected.json and portable.json pin what each row renders as.
SET SESSION sql_mode = '', time_zone = '+00:00';
CREATE TABLE corpus (
    k VARCHAR(20) PRIMARY KEY, c_tinyint TINYINT, c_flag TINYINT(1), c_smallint SMALLINT,
    c_mediumint MEDIUMINT, c_int INT, c_bigint BIGINT, c_unsigned BIGINT UNSIGNED,
    c_zerofill INT(5) UNSIGNED ZEROFILL, c_year YEAR, c_bit BIT(10), c_decimal DECIMAL(10, 2),
    c_decimal_zerofill DECIMAL(8, 2) ZEROFILL, c_double DOUBLE, c_float FLOAT, c_varchar VARCHAR(50),
    c_text TEXT, c_enum ENUM('sad', 'ok', 'happy'), c_set SET('x', 'y', 'z'), c_char CHAR(5), c_date DATE,
    c_datetime DATETIME, c_datetime6 DATETIME(6), c_timestamp TIMESTAMP(3) NULL, c_time TIME,
    c_time3 TIME(3), c_time6 TIME(6), c_binary BINARY(4), c_varbinary VARBINARY(10), c_blob BLOB,
    c_json JSON);
INSERT INTO corpus VALUES
    ('typical', 7, 1, -42, 8388607, -42, 9007199254740993, 18446744073709551615, 7, 2024, b'1000000001',
     1.50, 1.5, 0.1, 1.1234567, 'héllo 😀', 'a b', 'ok', 'x,z', 'ab', '2024-02-29', '2024-05-01 12:00:00',
     '2024-05-01 12:00:00.120000', '2024-05-01 12:00:00.125', '03:04:05', '03:04:05.120',
     '03:04:05.120000', 0x6162, 0x00ff, 0x010203, '{"a": 1.0, "b": [1e2, "x"]}'),
    ('edges', -128, 0, -32768, -8388608, 2147483647, -9223372036854775808, 0, 0, 1901, b'0', -0.50, 0,
     1e300, 1.1234599, ' padded ', '', 'happy', '', '', '1000-01-01', '9999-12-31 23:59:59',
     '1000-01-01 00:00:00.000001', '1970-01-01 00:00:01.000', '-838:59:59', '-12:00:00.5',
     '838:59:59.000000', 0x00, '', '', 'null'),
    ('zeros', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, 0.00, NULL, -1e-7, 16777216, NULL,
     NULL, NULL, NULL, NULL, '0000-00-00', '2024-00-00 00:00:00', '0000-00-00 00:00:00.000000', NULL,
     '00:00:00', NULL, NULL, NULL, NULL, NULL, '{"n": 1.10}'),
    ('nulls', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
     NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
    -- Each type's longest text, and doubles whose text passes the 22 characters a cast to CHAR declares.
    ('wide', -128, -128, -32768, -8388608, -2147483648, -9223372036854775808, 18446744073709551615,
     4294967295, 2155, b'1111111111', -99999999.99, 999999.99, -1.7976931348623157e308, -3.4028235e38,
     REPEAT('😀', 50), REPEAT('é', 70), 'happy', 'x,y,z', '😀😀😀😀😀', '9999-12-31', '9999-12-31 23:59:59',
     '9999-12-31 23:59:59.999999', '2038-01-19 03:14:07.999', '-838:59:59', '-838:59:58.999',
     '-838:59:58.999999', 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFFFFFF, REPEAT(0xFF, 40),
     '[-1.7976931348623157e308, 18446744073709551615, -9223372036854775808]'),
    ('tiny', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, -0.01, 0.01,
     -2.2250738585072014e-308, -1.17549435e-38, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
     '-00:00:00.001', '-00:00:00.000001', NULL, NULL, NULL, '-0.000012345678901234568');

-- The same rows through a view whose scan sorts in a temporary table (its key is the joined table's), with
-- a double the view computes: every statement must render a row as the table's own does.
CREATE TABLE corpus_order (k VARCHAR(20) PRIMARY KEY, j INT);
INSERT INTO corpus_order VALUES ('typical', 6), ('edges', 5), ('zeros', 4), ('nulls', 3), ('wide', 2), ('tiny', 1);
CREATE VIEW corpus_joined AS
    SELECT o.j, c.*, c.c_double * 1 AS c_computed FROM corpus c STRAIGHT_JOIN corpus_order o ON o.k = c.k;

CREATE TABLE portable (
    k VARCHAR(20) PRIMARY KEY, c_integer BIGINT, c_decimal DECIMAL(10, 2), c_double DOUBLE, c_real FLOAT,
    c_text VARCHAR(50), c_char CHAR(5), c_date DATE, c_timestamp DATETIME(6), c_instant TIMESTAMP(6) NULL,
    c_time TIME(6), c_bytes VARBINARY(10), c_json JSON);
INSERT INTO portable VALUES
    ('typical', 9007199254740993, 1.50, 0.1, 1.1234567, 'héllo 😀', 'ab', '2024-02-29',
     '2024-05-01 12:00:00.120000', '2024-05-01 12:00:00.5', '03:04:05.120000', 0x00ff,
     '{"a": 1.0, "b": [100, "x"], "c": null}'),
    ('edges', -9223372036854775808, -0.50, 1e300, 1.1234599, '', '', '1000-01-01', '9999-12-31 23:59:59.999999',
     '1970-01-01 00:00:01', '23:59:59.999999', '', '[]'),
    ('small', 0, 0.00, -1e-7, 16777216, ' padded ', 'a   b', '2000-01-01', '2000-01-01 00:00:00',
     '2038-01-19 03:14:07', '00:00:00', 0x000102, '"text"'),
    ('nulls', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
    -- Whole floats at 2^54 and 2^25 and above them, where PostgreSQL's own text is a digit longer than the
    -- shortest that reads back.
    ('power', NULL, NULL, 18014398509481984, 33554432, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
    ('beyond', NULL, NULL, 52990648348713776, 46056592, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);

CREATE TABLE amounts (k DECIMAL(10, 2) PRIMARY KEY, v INT);
INSERT INTO amounts VALUES (1.50, 1), (2.00, 2), (-0.50, 3), (10.00, 4);
