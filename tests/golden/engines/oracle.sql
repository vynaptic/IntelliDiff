-- The golden corpus of canonical rendering 1 on Oracle: one column per
-- supported type with edge values (corpus), the values every engine must render and digest alike
-- (portable: the empty string is NULL here, and a DATE holds a time) and decimal keys (amounts).
-- expected.json and portable.json pin what each row renders as.
CREATE TABLE corpus (
    k VARCHAR2(20) PRIMARY KEY, c_int NUMBER(5), c_big NUMBER(38), c_tens NUMBER(5, -2), c_number NUMBER(10, 2),
    c_plain NUMBER, c_float FLOAT(53), c_double BINARY_DOUBLE, c_single BINARY_FLOAT, c_varchar VARCHAR2(50 CHAR),
    c_nvarchar NVARCHAR2(50), c_clob CLOB, c_nclob NCLOB, c_char CHAR(5), c_nchar NCHAR(5), c_boolean BOOLEAN,
    c_date DATE, c_timestamp TIMESTAMP, c_timestamp9 TIMESTAMP(9), c_timestamp0 TIMESTAMP(0),
    c_zoned TIMESTAMP WITH TIME ZONE, c_local TIMESTAMP(3) WITH LOCAL TIME ZONE, c_raw RAW(16), c_blob BLOB,
    c_json JSON, c_span INTERVAL DAY TO SECOND, c_months INTERVAL YEAR TO MONTH, c_rowid ROWID, c_urowid UROWID);
INSERT INTO corpus VALUES
    ('typical', 7, 12345678901234567890123456789012345678, 1234, 1.50, 1.5, 0.1, 0.1d, 1.1234567f, 'héllo 😀',
     'héllo 😀', 'a b', 'n', 'ab', 'ab', TRUE, TIMESTAMP '2024-02-29 13:14:15',
     TIMESTAMP '2024-05-01 12:00:00.120000', TIMESTAMP '2024-05-01 12:00:00.123456789',
     TIMESTAMP '2024-05-01 12:00:00', TIMESTAMP '2024-05-01 14:00:00.5 +02:00',
     TIMESTAMP '2024-05-01 12:00:00.125 +00:00', HEXTORAW('00ff'), HEXTORAW('010203'),
     JSON('{"a": 1.0, "b": [1e2, "x"]}'), INTERVAL '1 02:03:04' DAY TO SECOND, INTERVAL '1-2' YEAR TO MONTH,
     CHARTOROWID('AAAR5FAAYAAAAANAAA'), CHARTOROWID('AAAR5FAAYAAAAANAAA')),
    ('edges', -99999, -1, -1200, -0.50, 1E125, 1e30, BINARY_DOUBLE_NAN, BINARY_FLOAT_INFINITY, ' padded ', '',
     EMPTY_CLOB(), NULL, '', '', FALSE, TO_DATE('-0044-03-15', 'SYYYY-MM-DD'),
     TO_TIMESTAMP('-0044-03-15 00:00:00', 'SYYYY-MM-DD HH24:MI:SS'), TIMESTAMP '0044-03-15 00:00:00', NULL,
     TO_TIMESTAMP_TZ('-0044-03-15 00:00:00 +00:00', 'SYYYY-MM-DD HH24:MI:SS TZH:TZM'), NULL, HEXTORAW(''),
     EMPTY_BLOB(), JSON('null'), INTERVAL '-1 00:00:00.5' DAY TO SECOND, INTERVAL '-0-1' YEAR TO MONTH, NULL,
     NULL),
    ('floats', NULL, NULL, NULL, 2.00, 0.5, 0.000001, 1e300d, 1.1234599f, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
     DATE '2024-02-29', NULL, NULL, NULL, TIMESTAMP '9999-12-31 23:59:59.999999 +08:00', NULL, NULL, NULL,
     JSON('{"n": 1.10}'), NULL, NULL, NULL, NULL),
    ('nulls', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
     NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
    -- Each type's longest text.
    ('wide', -99999, -99999999999999999999999999999999999999, -9999900, -99999999.99,
     -9.999999999999999999999999999999999999999E125, 1E-130, -1.7976931348623157E308d, -3.4028235E38f,
     RPAD('😀', 100, '😀'), RPAD(N'é', 50, N'é'), RPAD('é', 140, 'é'), N'😀', 'abcde', N'ééééé', TRUE,
     TO_DATE('9999-12-31 23:59:59', 'YYYY-MM-DD HH24:MI:SS'), TIMESTAMP '9999-12-31 23:59:59.999999',
     TIMESTAMP '9999-12-31 23:59:59.999999999', TIMESTAMP '9999-12-31 23:59:59',
     TIMESTAMP '9999-12-31 23:59:59.999999 +14:00', TIMESTAMP '9999-12-30 23:59:59.999 +00:00',
     HEXTORAW('ffffffffffffffffffffffffffffffff'), HEXTORAW(RPAD('ff', 80, 'ff')),
     JSON('[-1.7976931348623157e308, 18446744073709551615, -9223372036854775808]'),
     INTERVAL '-99 23:59:59.999999' DAY TO SECOND, INTERVAL '-99-11' YEAR TO MONTH, NULL, NULL),
    ('tiny', NULL, NULL, NULL, -0.01, 1E-130, NULL, -2.2250738585072014E-308d, -1.17549435E-38f, NULL, NULL, NULL,
     NULL, NULL, NULL, NULL, NULL, NULL, TIMESTAMP '0001-01-01 00:00:00.000000001', NULL, NULL, NULL, NULL, NULL,
     JSON('-0.000012345678901234568'), INTERVAL '-0 00:00:00.000001' DAY TO SECOND, NULL, NULL, NULL);

-- The same rows through a view that joins them to their keys, with a double the view computes: every
-- statement must render a row as the table's own does.
CREATE TABLE corpus_order (k VARCHAR2(20) PRIMARY KEY, j NUMBER(5));
INSERT INTO corpus_order VALUES
    ('typical', 6), ('edges', 5), ('floats', 4), ('nulls', 3), ('wide', 2), ('tiny', 1);
CREATE VIEW corpus_joined AS
    SELECT o.j, c.*, c.c_double * 1 AS c_computed FROM corpus c JOIN corpus_order o ON o.k = c.k;

CREATE TABLE portable (
    k VARCHAR2(20) PRIMARY KEY, c_integer NUMBER(19), c_decimal NUMBER(10, 2), c_double BINARY_DOUBLE,
    c_real BINARY_FLOAT, c_text VARCHAR2(50), c_char CHAR(5), c_date DATE, c_timestamp TIMESTAMP(6),
    c_instant TIMESTAMP(6) WITH TIME ZONE, c_bytes RAW(10), c_json JSON);
INSERT INTO portable VALUES
    ('typical', 9007199254740993, 1.50, 0.1d, 1.1234567f, 'héllo 😀', 'ab', DATE '2024-02-29',
     TIMESTAMP '2024-05-01 12:00:00.120000', TIMESTAMP '2024-05-01 14:00:00.5 +02:00', HEXTORAW('00ff'),
     JSON('{"a": 1.0, "b": [100, "x"], "c": null}')),
    ('edges', -9223372036854775808, -0.50, 1e300d, 1.1234599f, '', '', DATE '1000-01-01',
     TIMESTAMP '9999-12-31 23:59:59.999999', TIMESTAMP '1970-01-01 00:00:01 +00:00', HEXTORAW(''), JSON('[]')),
    ('small', 0, 0.00, -1e-7d, 16777216f, ' padded ', 'a   b', DATE '2000-01-01', TIMESTAMP '2000-01-01 00:00:00',
     TIMESTAMP '2038-01-19 03:14:07 +00:00', HEXTORAW('000102'), JSON('"text"')),
    ('nulls', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
    -- Whole floats at 2^54 and 2^25 and above them, where PostgreSQL's own text is a digit longer than the
    -- shortest that reads back.
    ('power', NULL, NULL, 18014398509481984d, 33554432f, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
    ('beyond', NULL, NULL, 52990648348713776d, 46056592f, NULL, NULL, NULL, NULL, NULL, NULL, NULL);

CREATE TABLE amounts (k NUMBER(10, 2) PRIMARY KEY, v NUMBER(10));
INSERT INTO amounts VALUES (1.50, 1), (2.00, 2), (-0.50, 3), (10.00, 4);
