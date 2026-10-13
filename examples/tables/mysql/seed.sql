-- The MySQL acceptance kit (docs/mvp/ENGINES.md §17): two tables of one database, compared by
-- spec.yaml to the result of expected.json. Run it on the server version you will compare, as an account
-- that may create a database:
--   mysql --default-character-set=utf8mb4 -h HOST -u ADMIN -p < examples/tables/mysql/seed.sql
-- It drops and recreates the database ds_kit. Then give the account DeltaSleuth reads with SELECT on it:
--   CREATE USER 'ds_reader'@'%' IDENTIFIED BY '...';
--   GRANT SELECT ON ds_kit.* TO 'ds_reader'@'%';
--
-- accounts_a holds customers K0001 to K1000, one column of each type DeltaSleuth reads, and ten keys
-- that the default collation orders and matches otherwise than their code points (B Z a "a " e é, an
-- emoji, E, x1, X1). accounts_b is a copy, its amount declared at another scale, with these differences:
--   K0005 email    K0010 status   K0015 balance  K0020 amount   K0025 ratio    K0030 score
--   K0035 visits   K0040 flag     K0045 born     K0050 opened   K0055 seen     K0060 at
--   K0065 tag      K0070 kind     K0075 doc      K0080 note     é email        MISMATCH, one path each
--   K0090 missing                                               NOT_FOUND_B
--   K1001 only there                                            NOT_FOUND_A
--   K0100 there twice                                           INCONCLUSIVE (identity_ambiguous)
--   one row without a key                                       not compared (skipped)

DROP DATABASE IF EXISTS ds_kit;
CREATE DATABASE ds_kit CHARACTER SET utf8mb4;
USE ds_kit;
SET NAMES utf8mb4;
SET SESSION time_zone = '+00:00';

CREATE TABLE accounts_a (
    code    VARCHAR(20),               -- the key: in the default collation, so not unique by it
    email   VARCHAR(100),
    status  CHAR(8),
    balance DECIMAL(12, 2),
    amount  DECIMAL(12, 2),            -- DECIMAL(14, 4) in accounts_b: the worker hashes it
    ratio   DOUBLE,
    score   FLOAT,
    visits  INT UNSIGNED,
    flag    TINYINT(1),
    born    DATE,
    opened  DATETIME(6),
    seen    TIMESTAMP(3) NULL,
    at      TIME(3),
    tag     VARBINARY(16),
    kind    ENUM('retail', 'corporate'),
    doc     JSON,                      -- a large type: the worker hashes it
    note    TEXT,                      -- likewise
    KEY (code)
);
INSERT INTO accounts_a
WITH RECURSIVE g (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM g WHERE n < 1000)
SELECT CONCAT('K', LPAD(n, 4, '0')), CONCAT('customer', n, '@example.com'),
       ELT(1 + n % 3, 'active', 'dormant', 'closed'), n * 1.25, n * 0.5, n / 7e0, n / 3e0, n * 1000, n % 2,
       DATE_ADD('1970-01-01', INTERVAL n DAY),
       TIMESTAMPADD(MICROSECOND, n * 1000001, '2020-01-01 00:00:00'),
       TIMESTAMPADD(SECOND, n * 3601, '2021-06-01 12:00:00.250'),
       SEC_TO_TIME(n * 61.5), UNHEX(MD5(n)), ELT(1 + n % 2, 'retail', 'corporate'),
       JSON_OBJECT('n', n, 'rate', 0.5, 'tags', JSON_ARRAY('a', 'b')), REPEAT('note ', n % 20)
FROM g;
-- é and the emoji as their UTF-8 bytes, whatever character set the client sends.
INSERT INTO accounts_a (code, email, balance) VALUES
    ('B', 'order', 1), ('Z', 'order', 2), ('a', 'order', 3), ('a ', 'order', 4), ('e', 'order', 5),
    (_utf8mb4 X'C3A9', 'order', 6), (_utf8mb4 X'F09F9880', 'order', 7), ('E', 'order', 8),
    ('x1', 'order', 9), ('X1', 'order', 10);

CREATE TABLE accounts_b LIKE accounts_a;
ALTER TABLE accounts_b MODIFY amount DECIMAL(14, 4);
INSERT INTO accounts_b SELECT * FROM accounts_a;

UPDATE accounts_b SET email = UPPER(email) WHERE code = 'K0005';
UPDATE accounts_b SET status = 'frozen' WHERE code = 'K0010';
UPDATE accounts_b SET balance = balance + 0.01 WHERE code = 'K0015';
UPDATE accounts_b SET amount = amount + 0.0001 WHERE code = 'K0020';
UPDATE accounts_b SET ratio = ratio + 1e-15 WHERE code = 'K0025';
UPDATE accounts_b SET score = 10.000001 WHERE code = 'K0030';
UPDATE accounts_b SET visits = visits + 1 WHERE code = 'K0035';
UPDATE accounts_b SET flag = 1 - flag WHERE code = 'K0040';
UPDATE accounts_b SET born = DATE_ADD(born, INTERVAL 1 DAY) WHERE code = 'K0045';
UPDATE accounts_b SET opened = TIMESTAMPADD(MICROSECOND, 1, opened) WHERE code = 'K0050';
UPDATE accounts_b SET seen = TIMESTAMPADD(MICROSECOND, 1000, seen) WHERE code = 'K0055';
UPDATE accounts_b SET at = ADDTIME(at, '00:00:00.001') WHERE code = 'K0060';
UPDATE accounts_b SET tag = X'00FF' WHERE code = 'K0065';
UPDATE accounts_b SET kind = 'corporate' WHERE code = 'K0070';
UPDATE accounts_b SET doc = JSON_SET(doc, '$.rate', 0.25) WHERE code = 'K0075';
UPDATE accounts_b SET note = CONCAT(note, '!') WHERE code = 'K0080';
-- By its bytes: in the default collation `code = 'é'` also finds e and E.
UPDATE accounts_b SET email = 'changed' WHERE CAST(code AS BINARY) = X'C3A9';
DELETE FROM accounts_b WHERE code = 'K0090';
INSERT INTO accounts_b (code, email) VALUES ('K1001', 'new@example.com'), (NULL, 'no key');
INSERT INTO accounts_b SELECT * FROM accounts_b WHERE code = 'K0100';
