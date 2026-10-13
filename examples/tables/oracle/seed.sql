-- The Oracle acceptance kit (docs/mvp/ENGINES.md §17): two tables of one schema, compared by spec.yaml
-- to the result of expected.json. Run it on the server version you will compare, in SQL*Plus or SQLcl, as
-- the account whose schema will hold them (it needs CREATE TABLE and quota):
--   sqlplus KIT_OWNER@//HOST:1521/SERVICE @examples/tables/oracle/seed.sql
-- Then give the account DeltaSleuth reads with, which needs CREATE SESSION, READ on both:
--   GRANT READ ON ds_kit_a TO ds_reader;
--   GRANT READ ON ds_kit_b TO ds_reader;
-- To run it again, first: DROP TABLE ds_kit_a PURGE; DROP TABLE ds_kit_b PURGE;
--
-- DS_KIT_A holds customers K0001 to K1000, one column of each type DeltaSleuth reads on Oracle 19c, and
-- ten keys whose order a linguistic sort would change (B Z a "a " e é, an emoji, E, x1, X1; a database
-- that is not AL32UTF8 cannot hold the emoji and is expected to store its replacement character, on both
-- sides alike: not observed). DS_KIT_B is a copy, its AMOUNT declared at another scale, with these
-- differences:
--   K0005 email    K0010 status   K0015 balance  K0020 amount   K0025 ratio    K0030 score
--   K0035 visits   K0040 flag     K0045 born     K0050 opened   K0055 seen     K0060 held
--   K0065 tag      K0070 plain    K0075 doc      K0080 note     é email        MISMATCH, one path each
--   K0090 missing                                               NOT_FOUND_B
--   K1001 only there                                            NOT_FOUND_A
--   K0100 there twice                                           INCONCLUSIVE (identity_ambiguous)
--   one row without a key                                       not compared (skipped)

CREATE TABLE ds_kit_a (
    code    VARCHAR2(20),
    email   VARCHAR2(100),
    status  CHAR(8),
    balance NUMBER(12, 2),
    amount  NUMBER(12, 2),
    ratio   BINARY_DOUBLE,
    score   BINARY_FLOAT,
    visits  NUMBER(10),
    plain   NUMBER,
    flag    NUMBER(1),
    born    DATE,
    opened  TIMESTAMP(6),
    seen    TIMESTAMP(3) WITH TIME ZONE,
    held    INTERVAL DAY(2) TO SECOND(3),
    tag     RAW(16),
    doc     CLOB,
    note    NVARCHAR2(100)
);
CREATE INDEX ds_kit_a_code ON ds_kit_a (code);
INSERT INTO ds_kit_a
SELECT 'K' || LPAD(LEVEL, 4, '0'), 'customer' || LEVEL || '@example.com',
       DECODE(MOD(LEVEL, 3), 0, 'active', 1, 'dormant', 'closed'), LEVEL * 1.25, LEVEL * 0.5,
       LEVEL / 7d, LEVEL / 3f, LEVEL * 1000, LEVEL / 8, MOD(LEVEL, 2),
       DATE '1970-01-01' + LEVEL + LEVEL / 86400,
       TIMESTAMP '2020-01-01 00:00:00' + NUMTODSINTERVAL(LEVEL * 1.000001, 'SECOND'),
       FROM_TZ(TIMESTAMP '2021-06-01 12:00:00.250', '+02:00') + NUMTODSINTERVAL(LEVEL * 3601, 'SECOND'),
       NUMTODSINTERVAL(LEVEL * 61.5, 'SECOND'), HEXTORAW(LPAD(TO_CHAR(LEVEL, 'FMXXXXXXXX'), 8, '0')),
       TO_CLOB('{"n": ' || LEVEL || ', "tags": ["a", "b"]}'), RPAD(N'note ', MOD(LEVEL, 20) * 5, N'note ')
FROM DUAL CONNECT BY LEVEL <= 1000;
INSERT INTO ds_kit_a (code, email, balance) VALUES ('B', 'order', 1);
INSERT INTO ds_kit_a (code, email, balance) VALUES ('Z', 'order', 2);
INSERT INTO ds_kit_a (code, email, balance) VALUES ('a', 'order', 3);
INSERT INTO ds_kit_a (code, email, balance) VALUES ('a ', 'order', 4);
INSERT INTO ds_kit_a (code, email, balance) VALUES ('e', 'order', 5);
INSERT INTO ds_kit_a (code, email, balance) VALUES (UNISTR('\00E9'), 'order', 6);
INSERT INTO ds_kit_a (code, email, balance) VALUES (UNISTR('\D83D\DE00'), 'order', 7);
INSERT INTO ds_kit_a (code, email, balance) VALUES ('E', 'order', 8);
INSERT INTO ds_kit_a (code, email, balance) VALUES ('x1', 'order', 9);
INSERT INTO ds_kit_a (code, email, balance) VALUES ('X1', 'order', 10);

CREATE TABLE ds_kit_b AS SELECT * FROM ds_kit_a;
ALTER TABLE ds_kit_b MODIFY amount NUMBER(14, 4);
CREATE INDEX ds_kit_b_code ON ds_kit_b (code);

UPDATE ds_kit_b SET email = UPPER(email) WHERE code = 'K0005';
UPDATE ds_kit_b SET status = 'frozen' WHERE code = 'K0010';
UPDATE ds_kit_b SET balance = balance + 0.01 WHERE code = 'K0015';
UPDATE ds_kit_b SET amount = amount + 0.0001 WHERE code = 'K0020';
UPDATE ds_kit_b SET ratio = ratio + 1e-15d WHERE code = 'K0025';
UPDATE ds_kit_b SET score = 10.000001f WHERE code = 'K0030';
UPDATE ds_kit_b SET visits = visits + 1 WHERE code = 'K0035';
UPDATE ds_kit_b SET flag = 1 - flag WHERE code = 'K0040';
UPDATE ds_kit_b SET born = born + 1 / 86400 WHERE code = 'K0045';
UPDATE ds_kit_b SET opened = opened + INTERVAL '0.000001' SECOND WHERE code = 'K0050';
UPDATE ds_kit_b SET seen = seen + INTERVAL '0.001' SECOND WHERE code = 'K0055';
UPDATE ds_kit_b SET held = held + INTERVAL '0.001' SECOND WHERE code = 'K0060';
UPDATE ds_kit_b SET tag = HEXTORAW('00FF') WHERE code = 'K0065';
UPDATE ds_kit_b SET plain = plain + 0.001 WHERE code = 'K0070';
UPDATE ds_kit_b SET doc = TO_CLOB('{"n": 75, "tags": ["a", "c"]}') WHERE code = 'K0075';
UPDATE ds_kit_b SET note = note || N'!' WHERE code = 'K0080';
UPDATE ds_kit_b SET email = 'changed' WHERE code = UNISTR('\00E9');
DELETE FROM ds_kit_b WHERE code = 'K0090';
INSERT INTO ds_kit_b (code, email) VALUES ('K1001', 'new@example.com');
INSERT INTO ds_kit_b (code, email) VALUES (NULL, 'no key');
INSERT INTO ds_kit_b SELECT * FROM ds_kit_b WHERE code = 'K0100';
COMMIT;
