"""An engine's errors become neutral ones holding only a code: classified by
origin first, then by the server's code, and never with a driver's text, which may quote a key or a value.
"""

from __future__ import annotations

import socket
import ssl
import struct
from typing import Any

import oracledb
import pymysql
import pytest

from deltasleuth.connectors import tables
from deltasleuth.connectors.tables import engine, mysql, oracle
from deltasleuth.core.types import Target
from deltasleuth.runner.cycle import failure

QUOTED = "Incorrect DATETIME value: 's3cret-key'"  # what a server's message may quote
CODES = {
    tables.KeyUnfit: (1267, 1270, 1271, 1292, 1366, 1525, 3854),
    # 1226: the account is at its max_user_connections, as 1203 is the server's (observed on 8.4.11).
    tables.TargetUnreachable: (1040, 1053, 1129, 1203, 1226, 1317, 4031),
    tables.StatementTimeout: (3024,),
    tables.SnapshotLost: (1412,),
    tables.TargetAuthRefused: (1045, 1862, 3118),
    # 3 is what a sort answers a full temporary directory with (observed: test_mysql), 1 its sibling.
    tables.TargetTempSpace: (1, 3, 1021, 1028, 1114),
    # 1054: a column that is gone, which the schema guard looks at first.
    tables.TargetDenied: (1054, 1142, 1143, 1146),
    tables.TargetRefused: (1064, 1792, 3819, 999),  # any other server code
}


@pytest.mark.parametrize(
    ("cls", "number"), [(cls, number) for cls, numbers in CODES.items() for number in numbers]
)
def test_each_mysql_code_has_its_class_and_shows_no_text(cls: type[tables.TableError], number: int) -> None:
    found = mysql._error(pymysql.err.OperationalError(number, QUOTED))
    assert type(found) is cls and str(found) == f"MySQL {number}" and tables.code(found) == f"MySQL {number}"
    assert (
        "s3cret" not in repr(found) + failure(found) and failure(found) == f"{cls.__name__}: MySQL {number}"
    )


def test_the_classes_the_callers_catch() -> None:
    timeout = mysql._error(pymysql.err.OperationalError(3024, QUOTED))
    assert isinstance(timeout, tables.TIMEOUT) and isinstance(
        timeout, tables.UNREACHABLE
    )  # under unreachable
    unfit = mysql._error(pymysql.err.DataError(1366, QUOTED))
    assert isinstance(unfit, mysql.MySQL.unfit) and UnicodeEncodeError in mysql.MySQL.unfit
    assert not isinstance(mysql._error(pymysql.err.ProgrammingError(1146, QUOTED)), tables.UNREACHABLE)


@pytest.mark.parametrize(
    ("error", "code", "gloss"),
    [
        (ConnectionRefusedError(61, QUOTED), "ConnectionRefusedError", "no server answered"),
        (TimeoutError(QUOTED), "TimeoutError", "no server answered"),
        (ConnectionResetError(54, QUOTED), "ConnectionResetError", None),
        (OSError(65, QUOTED), "OSError", None),
        (socket.gaierror(8, QUOTED), "gaierror", "the host name does not resolve"),
        (
            ssl.SSLCertVerificationError(1, QUOTED),
            "SSLCertVerificationError",
            "TLS: the server's certificate was not accepted",
        ),
        (ssl.SSLEOFError(8, QUOTED), "SSLError", "TLS: the handshake failed"),
        (ssl.SSLError(1, QUOTED), "SSLError", "TLS: the handshake failed"),
        (EOFError(QUOTED), "EOFError", None),
        (pymysql.err.OperationalError(2013, QUOTED), "MySQL 2013", None),  # the driver's own: a client code
        (pymysql.err.OperationalError(2026, QUOTED), "MySQL 2026", "TLS: the server offers none"),
        (pymysql.err.OperationalError(2999, QUOTED), "MySQL 2999", None),  # an unlisted client code
        (pymysql.err.InterfaceError(0, ""), "InterfaceError", None),
        (pymysql.err.Error("Already closed"), "Error", None),
        (pymysql.err.InternalError(QUOTED), "InternalError", None),
        # What the driver raises parsing an answer no MySQL server sends: its class, never its text.
        (ValueError(f"invalid literal for int() with base 10: '{QUOTED}'"), "ValueError", mysql._NOT_MYSQL),
        (struct.error("unpack requires a buffer of 4 bytes"), "error", mysql._NOT_MYSQL),
        (IndexError(QUOTED), "IndexError", mysql._NOT_MYSQL),
        (RuntimeError(f"**WARN**: Received LOAD_LOCAL packet {QUOTED}"), "RuntimeError", mysql._NOT_MYSQL),
    ],
)
def test_a_failure_without_a_server_code_is_unreachable(
    error: Exception, code: str, gloss: str | None
) -> None:
    found = mysql._error(error)
    assert type(found) is tables.TargetUnreachable and (str(found), found.gloss) == (code, gloss)
    assert "s3cret" not in repr(found)


def test_a_socket_error_pymysql_wrapped_at_a_connect_is_classified_by_its_origin() -> None:
    wrapped = pymysql.err.OperationalError(2003, f"Can't connect to MySQL server ({QUOTED})")
    wrapped.original_exception = ssl.SSLCertVerificationError(1, QUOTED)  # type: ignore[attr-defined]
    found = mysql._error(wrapped)
    assert (str(found), found.gloss) == (
        "SSLCertVerificationError",
        "TLS: the server's certificate was not accepted",
    )


@pytest.mark.parametrize(
    ("version", "refused"),
    [
        ("8.4.11", None),
        ("8.0.17", None),
        ("8.0.36-0ubuntu0.22.04.1", None),
        ("9.1.0", None),
        ("8.0.16", "MySQL 8.0"),
        ("5.7.44-log", "MySQL 5.7"),
        ("11.4.2-MariaDB-ubu2404", "MariaDB 11.4"),
        ("5.5.5-10.6.12-MariaDB-1:10.6.12+maria~ubu2004", "MariaDB 10.6"),
        ("garbage", "MySQL 0.0"),
    ],
)
def test_mariadb_and_an_old_mysql_are_refused_by_their_version(version: str, refused: str | None) -> None:
    target = Target("ledger", "")
    if refused is None:
        return mysql._refuse_version(target, version)
    with pytest.raises(tables.Refusal) as exc:  # the plan prefixes it with its side
        mysql._refuse_version(target, version)
    assert str(exc.value) == f"target ledger is {refused}: MySQL 8.0.17 or later is supported, MariaDB is not"


def test_an_engine_changed_since_the_plan_is_refused() -> None:
    plain, declared = Target("ledger", "", dsn_ref="file:dsn"), Target("ledger", "https://svc.example")
    assert engine.planned("a", {"keys": []}, plain) is tables.ENGINES["postgresql"]
    with pytest.raises(tables.EngineChanged) as exc:
        engine.planned("a", {"engine": "mysql"}, plain)
    assert str(exc.value) == "side a: target ledger is postgresql now; this run was planned for mysql"
    assert failure(exc.value) == f"EngineChanged: {exc.value}"
    with pytest.raises(tables.EngineChanged):
        engine.planned("b", {"keys": []}, declared)  # no database any more


# --- Oracle ---------------------------------------------------------------------------------------------

ORA = {
    tables.KeyUnfit: (1722, 1426, 1438, 1830, 1839, 1861, 12899),
    tables.TargetUnreachable: (
        *(18, 20, 28, 1033, 1034, 1035, 1089, 2391, 2396, 3113, 3114, 3135, 3136, 12170, 12514, 12516),
        *(12518, 12520, 12537, 12541, 12547, 12571),  # 01035: a restricted session, a maintenance window
    ),
    tables.StatementTimeout: (1013,),
    tables.SnapshotLost: (1555, 8176, 1466),
    tables.TargetAuthRefused: (1017, 28000, 28001),
    tables.TargetTempSpace: (1652,),
    tables.TargetDenied: (942, 904, 1031, 41900),
    tables.TargetRefused: (1456, 932, 6502, 600, 3302),  # any other server code
}


def _raised(message: str, number: int = 0, cause: BaseException | None = None) -> oracledb.Error:
    """The driver's exception of a server's code ``number`` (its message ``message``), as it raises it."""
    found = oracledb._Error(message=message, code=number)
    exc = found.exc_type(found)
    exc.__cause__ = cause
    return exc


@pytest.mark.parametrize(
    ("cls", "number"), [(cls, number) for cls, numbers in ORA.items() for number in numbers]
)
def test_each_oracle_code_has_its_class_and_shows_no_text(cls: type[tables.TableError], number: int) -> None:
    # The driver shows some of the server's codes as one of its own (ORA-03113 as DPY-4011): the server's
    # is the one shown.
    found = oracle._error(_raised(f"ORA-{number:05d}: {QUOTED}", number))
    assert type(found) is cls and str(found) == tables.code(found) == f"ORA-{number:05d}"
    assert "s3cret" not in repr(found) + failure(found)
    assert failure(found) == f"{cls.__name__}: ORA-{number:05d}"


def test_the_oracle_classes_the_callers_catch() -> None:
    timeout = oracle._error(_raised("DPY-4024: call timeout of 500 ms exceeded"))
    assert type(timeout) is tables.StatementTimeout and str(timeout) == "DPY-4024"
    assert isinstance(timeout, tables.TIMEOUT) and isinstance(timeout, tables.UNREACHABLE)
    assert isinstance(oracle._error(_raised("ORA-01013: cancelled", 1013)), tables.TIMEOUT)
    unfit = oracle._error(_raised(f"ORA-01722: {QUOTED}", 1722))
    assert isinstance(unfit, oracle.Oracle.unfit) and UnicodeEncodeError in oracle.Oracle.unfit
    glossed = {
        str(e): e.gloss for e in (oracle._error(_raised(f"ORA-{n:05d}: x", n)) for n in (1017, 28000, 28001))
    }
    assert glossed == {
        "ORA-01017": "the credential was refused",
        "ORA-28000": "the account is locked",
        "ORA-28001": "the password has expired",
    }


@pytest.mark.parametrize(
    ("error", "code", "gloss"),
    [
        (_raised("DPY-4011: the database or network closed the connection"), "DPY-4011", None),
        (_raised("DPY-1001: not connected to database"), "DPY-1001", None),  # an unlisted driver code
        (_raised("DPY-6005: cannot connect to database"), "DPY-6005", "no server answered"),
        (
            _raised("DPY-6005: cannot connect", cause=ConnectionRefusedError(61, QUOTED)),
            "ConnectionRefusedError",
            "no server answered",
        ),
        (
            _raised("DPY-6005: cannot connect", cause=TimeoutError(QUOTED)),
            "TimeoutError",
            "no server answered",
        ),
        (
            _raised("DPY-6005: cannot connect", cause=ssl.SSLCertVerificationError(1, QUOTED)),
            "SSLCertVerificationError",
            "TLS: the server's certificate was not accepted",
        ),
        (
            _raised(
                "DPY-6005: cannot connect", cause=_raised(f"DPY-6001: Service {QUOTED} is not registered")
            ),
            "DPY-6001",
            "the listener does not know the service",
        ),
        (
            _raised(f"DPY-3001: {QUOTED}"),
            "DPY-3001",
            "the server requires Native Network Encryption: ask for a TCPS listener",
        ),
        (_raised(f"DPY-3015: {QUOTED}"), "DPY-3015", "the account has only a 10G password verifier"),
        (oracledb.InterfaceError(), "InterfaceError", None),
        (ConnectionResetError(54, QUOTED), "ConnectionResetError", None),
        (socket.gaierror(8, QUOTED), "gaierror", "the host name does not resolve"),
    ],
)
def test_an_oracle_failure_without_a_server_code_is_unreachable(
    error: Exception, code: str, gloss: str | None
) -> None:
    found = oracle._error(error)
    assert type(found) is tables.TargetUnreachable and (str(found), found.gloss) == (code, gloss)
    assert "s3cret" not in repr(found)


SETTLED = ["BINARY", "BINARY", "GREGORIAN", ".,", "UTC", "tcps", "APP", "AL32UTF8"]


@pytest.mark.parametrize(
    ("version", "refused"),
    [
        ("23.26.3.0.0", None),
        ("19.21.0.0.0", None),
        ("18.3.0.0.0", "18.3"),
        ("12.2.0.1.0", "12.2"),
        ("x", "x.0"),
    ],
)
def test_an_oracle_before_19_is_refused_by_its_version(version: str, refused: str | None) -> None:
    target = Target("crm-legacy", "")
    if refused is None:
        return oracle._settled(target, version, SETTLED, True)
    with pytest.raises(tables.Refusal) as exc:  # the plan prefixes it with its side
        oracle._settled(target, version, SETTLED, True)
    assert str(exc.value) == f"target crm-legacy is Oracle {refused}: 19c or later is supported"


@pytest.mark.parametrize(
    ("at", "found", "setting"),
    [
        (0, "BINARY_CI", "NLS_SORT"),
        (1, "LINGUISTIC", "NLS_COMP"),
        (2, "Thai Buddha", "NLS_CALENDAR"),
        (3, ",.", "NLS_NUMERIC_CHARACTERS"),
        (4, "+02:00", "TIME_ZONE"),
    ],
)
def test_a_setting_a_logon_trigger_overrides_is_named(at: int, found: str, setting: str) -> None:
    read = [*SETTLED[:at], found, *SETTLED[at + 1 :]]
    with pytest.raises(tables.Refusal) as exc:
        oracle._settled(Target("crm-legacy", ""), "19.21.0.0.0", read, True)
    assert (
        str(exc.value) == f"the session could not be set ({setting} is {found}): a logon trigger overrides it"
    )


def test_an_oracle_session_that_reports_no_tls_is_refused_where_the_target_declares_it() -> None:
    plain = [*SETTLED[:5], "tcp", *SETTLED[6:]]
    oracle._settled(Target("crm-legacy", ""), "19.21.0.0.0", plain, False)  # allow_unencrypted
    with pytest.raises(tables.TargetUnreachable) as exc:
        oracle._settled(Target("crm-legacy", ""), "19.21.0.0.0", plain, True)
    assert (str(exc.value), exc.value.gloss) == ("tcp", "TLS: the server offers none")


@pytest.mark.parametrize(
    ("declared", "keyable"),
    [
        (("VARCHAR2", 20, None, None, 20, "B", "USING_NLS_COMP"), True),
        (("VARCHAR2", 20, None, None, 20, "B", "BINARY"), True),
        (("VARCHAR2", 20, None, None, 20, "B", "BINARY_CI"), False),  # its own comparison is no key text's
        (("CHAR", 4, None, None, 4, "B", "XGERMAN_AI"), False),
        (("NUMBER", 22, None, None, 0, None, None), True),
        (("NUMBER", 22, 10, 2, 0, None, None), True),
        (("FLOAT", 22, 126, None, 0, None, None), False),
        (("DATE", 7, None, None, 0, None, None), True),
        (("TIMESTAMP(0)", 7, None, 0, 0, None, None), True),
        (("TIMESTAMP(6)", 11, None, 6, 0, None, None), False),
        (("NVARCHAR2", 40, None, None, 20, "C", "USING_NLS_COMP"), False),
        (("CLOB", 4000, None, None, 0, None, "USING_NLS_COMP"), False),
        (("RAW", 16, None, None, 0, None, None), True),
    ],
)
def test_which_oracle_columns_can_be_a_key(declared: tuple[Any, ...], keyable: bool) -> None:
    assert oracle._column(True, "K", *declared).keyable is keyable


def test_an_oracle_number_s_class_and_declared_type_follow_its_scale() -> None:
    declared = {
        (None, None): ("NUMBER", "decimal"),
        (10, 0): ("NUMBER(10)", "integer"),
        (None, 0): ("NUMBER(*,0)", "integer"),  # INTEGER
        (5, -2): ("NUMBER(5,-2)", "integer"),
        (10, 2): ("NUMBER(10,2)", "decimal"),
        (None, 2): ("NUMBER(*,2)", "decimal"),
    }
    for (precision, scale), expected in declared.items():
        column = oracle._column(True, "N", "NUMBER", 22, precision, scale, 0, None, None)
        assert (column.declared, column.cls) == expected
    floating = oracle._column(True, "F", "FLOAT", 22, 126, None, 0, None, None)
    assert (floating.declared, floating.cls) == ("FLOAT(126)", "decimal")
    spatial = oracle._column(True, "G", "SDO_GEOMETRY", 1, None, None, 0, None, None)
    assert (spatial.declared, spatial.cls, spatial.keyable) == ("SDO_GEOMETRY", None, False)
