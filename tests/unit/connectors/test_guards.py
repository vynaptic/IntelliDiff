"""SSRF address rules, the vetted address of a database target (docs/mvp/ENGINES.md §4), the circuit
breaker's states and retry timing."""

from __future__ import annotations

import asyncio
import random
import socket
import ssl
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import oracledb
import pytest
import yaml
from cryptography.hazmat.primitives import serialization

from deltasleuth.connectors import netguard
from deltasleuth.connectors.base import UnsafeTargetError
from deltasleuth.connectors.breaker import CircuitBreaker
from deltasleuth.connectors.netguard import (
    GuardedResolver,
    check_literal_host,
    is_forbidden_address,
    system_resolve,
    vetted,
)
from deltasleuth.connectors.retry import parse_retry_after, retry_delay
from deltasleuth.connectors.tables import ENGINES, TargetUnreachable, engine, mysql, oracle
from deltasleuth.core.spec import load_targets
from deltasleuth.core.types import RetryPolicy
from tests.conftest import make_ca

FORBIDDEN = [
    "127.0.0.1",
    "127.8.9.10",
    "10.1.2.3",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",  # cloud metadata
    "100.64.0.1",  # shared address space
    "0.0.0.0",  # noqa: S104 - an address under test, nothing binds to it
    "224.0.0.1",
    "240.0.0.1",
    "::1",
    "fe80::1%en0",
    "fc00::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "ff02::1",
    # An IPv4 address in the last 32 bits of the well-known NAT64 prefix, of an IPv4-translated and of
    # an IPv4-compatible address: a translator reaches the IPv4 one.
    "64:ff9b::a01:203",  # 10.1.2.3
    "64:ff9b::7f00:1",  # 127.0.0.1
    "64:ff9b::a9fe:a9fe",  # 169.254.169.254
    "::ffff:0:a9fe:a9fe",
    "::a9fe:a9fe",
]
PUBLIC = ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111", "64:ff9b::808:808", "::ffff:8.8.8.8"]


class TestSSRFGuard:
    @pytest.mark.parametrize(
        ("address", "forbidden"), [(a, True) for a in FORBIDDEN] + [(a, False) for a in PUBLIC]
    )
    def test_address_rules(self, address: str, forbidden: bool) -> None:
        assert is_forbidden_address(address) is forbidden

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "10.0.0.7"])
    def test_forbidden_literal_hosts(self, host: str) -> None:
        with pytest.raises(UnsafeTargetError):
            check_literal_host(host)

    @pytest.mark.parametrize("host", ["93.184.216.34", "svc.example"])
    def test_public_literals_and_names_pass(self, host: str) -> None:
        check_literal_host(host)  # names are checked when resolved

    @pytest.mark.parametrize("answer", [["10.0.0.7"], ["93.184.216.34", "127.0.0.1"], []])
    async def test_resolver_refuses_private_mixed_or_empty_answers(self, answer: list[str]) -> None:
        async def resolve(host: str, port: int) -> list[str]:
            return answer

        with pytest.raises(UnsafeTargetError):
            await GuardedResolver(resolve, allow_private=False).resolve("svc.example", 443)

    async def test_resolver_results(self) -> None:
        async def resolve(host: str, port: int) -> list[str]:
            return ["2606:4700::1", "10.0.0.7"]

        resolver = GuardedResolver(resolve, allow_private=True)  # an explicit opt-in skips the check
        results = await resolver.resolve("svc.example", 443)
        assert [(r["host"], r["family"], r["port"]) for r in results] == [
            ("2606:4700::1", socket.AF_INET6, 443),
            ("10.0.0.7", socket.AF_INET, 443),
        ]
        await resolver.close()

    async def test_system_resolver_on_loopback_literal(self) -> None:
        assert "127.0.0.1" in await system_resolve("127.0.0.1", 80)


class TestVetted:
    """What a database session may be opened to: every answer of the resolver, judged at each connect."""

    @pytest.fixture
    def resolved(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
        """Names and what they resolve to; anything else fails the test."""
        names: dict[str, list[str]] = {}

        async def resolve(host: str, port: int) -> list[str]:
            return names[host]

        monkeypatch.setattr(netguard, "system_resolve", resolve)
        return names

    async def test_a_literal_is_its_own_answer(self) -> None:
        assert await vetted("93.184.216.34", 3306, False) == ["93.184.216.34"]
        assert await vetted("127.0.0.1", 3306, True) == ["127.0.0.1"]

    async def test_a_name_is_vetted_by_every_address_it_resolves_to(
        self, resolved: dict[str, list[str]]
    ) -> None:
        resolved["db.example"] = ["2606:4700::1", "93.184.216.34", "2606:4700::1"]
        assert await vetted("db.example", 3306, False) == ["2606:4700::1", "93.184.216.34"]
        resolved["db.example"] = ["93.184.216.34", "10.1.2.3"]  # one private answer refuses them all
        with pytest.raises(UnsafeTargetError) as exc:
            await vetted("db.example", 3306, False)
        assert (
            str(exc.value) == "10.1.2.3 is a private address: set allow_private_network on the target"
        )  # G1
        assert await vetted("db.example", 3306, True) == ["93.184.216.34", "10.1.2.3"]

    @pytest.mark.parametrize(
        "address", ["64:ff9b::a01:203", "64:ff9b::7f00:1", "::ffff:0:a01:203", "::a01:203"]
    )
    async def test_an_address_that_stands_for_a_private_ipv4_one_is_private(
        self, resolved: dict[str, list[str]], address: str
    ) -> None:
        # A DNS64 resolver answers 64:ff9b::a01:203 for a name whose A record is 10.1.2.3.
        resolved["db.example"] = [address]
        with pytest.raises(UnsafeTargetError) as exc:
            await vetted("db.example", 3306, False)
        assert str(exc.value) == f"{address} is a private address: set allow_private_network on the target"
        assert await vetted("db.example", 3306, True) == [address]
        resolved["db.example"] = ["64:ff9b::808:808"]  # 8.8.8.8 through the translator
        assert await vetted("db.example", 3306, False) == ["64:ff9b::808:808"]

    @pytest.mark.parametrize(
        "address",
        [
            *("169.254.169.254", "fd00:ec2::254", "224.0.0.1", "fe80::1", "::"),
            *("64:ff9b::a9fe:a9fe", "::ffff:0:a9fe:a9fe", "::a9fe:a9fe", "::ffff:169.254.169.254"),
        ],
    )
    async def test_a_metadata_or_link_local_answer_is_never_a_target(
        self, resolved: dict[str, list[str]], address: str
    ) -> None:
        resolved["db.example"] = ["93.184.216.34", address]
        with pytest.raises(UnsafeTargetError) as exc:
            await vetted("db.example", 1521, True)  # whatever the target allows
        assert str(exc.value) == (
            f"{address} is never a target (link-local, unspecified, multicast or metadata)"  # G2
        )
        assert "notification" not in str(exc.value)

    def test_the_next_address_is_tried_when_one_refuses_and_the_socket_is_kept_alive(self) -> None:
        with socket.create_server(("127.0.0.1", 0)) as server:  # nothing listens on ::1 at this port
            port = server.getsockname()[1]
            sock = mysql._dial(["::1", "127.0.0.1"], port, time.monotonic() + 5)
            with sock:
                assert sock.getpeername()[:2] == ("127.0.0.1", port) and kept_alive(sock) == [60, 10, 6]
                assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        with pytest.raises(ConnectionRefusedError):
            mysql._dial(["::1", "127.0.0.1"], port, time.monotonic() + 5)  # the last refusal is raised

    def test_a_silent_address_leaves_the_next_one_its_share_of_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 192.0.2.1 never answers (a filtered path, a dead node of a pair): it once took the whole
        # budget, and the address after it a millisecond.
        dialled: list[tuple[str, float]] = []
        connect = socket.create_connection

        def create_connection(address: tuple[str, int], timeout: float) -> socket.socket:
            dialled.append((address[0], timeout))
            if address[0] == "192.0.2.1":
                time.sleep(timeout)
                raise TimeoutError("timed out")
            return connect(address, timeout)

        monkeypatch.setattr(mysql.socket, "create_connection", create_connection)
        with socket.create_server(("127.0.0.1", 0)) as server:
            started = time.monotonic()
            with mysql._dial(["192.0.2.1", "127.0.0.1"], server.getsockname()[1], started + 0.6) as sock:
                assert sock.getpeername()[0] == "127.0.0.1"
            with pytest.raises(TimeoutError):  # and three silent ones share it
                mysql._dial(["192.0.2.1"] * 3, server.getsockname()[1], time.monotonic() + 0.3)
        first, second, *thirds = [timeout for _, timeout in dialled]
        assert 0.25 < first <= 0.3 and 0.15 < second <= 0.3  # half the budget, then what is left of it
        assert len(thirds) == 3 and all(0.03 < share <= 0.1 for share in thirds)

    async def test_oracle_s_silent_address_leaves_the_next_one_its_share_of_the_budget(
        self, resolved: dict[str, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dialled: list[str] = []

        async def connect(**options: Any) -> None:
            dialled.append(options["host"])
            if options["host"] == "2606:4700::1":
                await asyncio.sleep(3600)  # it never answers
            failed = oracledb._Error(message="DPY-6005: cannot connect to database")
            raise oracledb.OperationalError(failed) from ConnectionRefusedError(61, "refused")

        monkeypatch.setattr(oracle.oracledb, "connect_async", connect)
        monkeypatch.setattr(oracle, "CONNECT_S", 1.0)
        (tmp_path / "password").write_text("s3cret-password")
        entry = {
            "engine": "oracle", "host": "crm-db.example", "service": "CRMPDB", "user": "ds_reader",
            "password_ref": "file:password", "allow_unencrypted": True,
        }  # fmt: skip
        (tmp_path / "targets.yaml").write_text(yaml.safe_dump({"targets": {"crm": entry}}))
        target = load_targets(tmp_path / "targets.yaml").get("crm")
        resolved["crm-db.example"] = ["2606:4700::1", "93.184.216.34"]
        started = time.monotonic()
        with pytest.raises(TargetUnreachable) as exc:
            await ENGINES["oracle"].connect(target)
        # The second address was dialled, after half the budget: its refusal is the answer.
        assert dialled == ["2606:4700::1", "93.184.216.34"] and str(exc.value) == "ConnectionRefusedError"
        assert 0.45 < time.monotonic() - started < 0.9
        resolved["crm-db.example"] = ["2606:4700::1"]  # alone, it has the whole budget
        with pytest.raises(TargetUnreachable) as exc:
            await ENGINES["oracle"].connect(target)
        assert (str(exc.value), exc.value.gloss) == ("TimeoutError", "no server answered")

    async def test_oracle_s_driver_is_handed_the_vetted_address_in_every_tls_mode(
        self, resolved: dict[str, list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dialled: list[dict[str, Any]] = []

        async def connect(**options: Any) -> None:
            dialled.append(options)
            failed = oracledb._Error(message="DPY-6005: cannot connect to database")
            if options["host"] == "2606:4700::1":  # no route: the next address is tried
                raise oracledb.OperationalError(failed) from OSError(65, "No route to host")
            raise oracledb.OperationalError(failed) from ConnectionRefusedError(61, "refused")

        monkeypatch.setattr(oracle.oracledb, "connect_async", connect)
        (tmp_path / "password").write_text("s3cret-password")
        (tmp_path / "ca.pem").write_bytes(make_ca("some CA")[0].public_bytes(serialization.Encoding.PEM))
        entry = {
            "engine": "oracle", "host": "crm-db.example", "service": "CRMPDB", "user": "ds_reader",
            "password_ref": "file:password",
        }  # fmt: skip
        modes = {
            "plain": {"allow_unencrypted": True},
            "named": {"tls_server_dn": "CN=crm,O=Bank", "tls_ca_ref": "file:ca.pem"},
            "chain": {"tls_verify_name": False, "tls_ca_ref": "file:ca.pem"},
        }
        (tmp_path / "targets.yaml").write_text(
            yaml.safe_dump({"targets": {mode: entry | members for mode, members in modes.items()}})
        )
        targets = load_targets(tmp_path / "targets.yaml")
        resolved["crm-db.example"] = ["2606:4700::1", "93.184.216.34"]
        for mode in modes:
            with pytest.raises(TargetUnreachable) as exc:
                await ENGINES["oracle"].connect(targets.get(mode))
            assert (str(exc.value), exc.value.gloss) == ("ConnectionRefusedError", "no server answered")
        # The driver's host is an address the guard answered, never the name; the name is in no option.
        assert [options["host"] for options in dialled] == ["2606:4700::1", "93.184.216.34"] * 3
        assert not any("crm-db.example" in str(value) for options in dialled for value in options.values())
        tls = [
            (o["protocol"], o.get("ssl_server_dn_match"), o.get("ssl_server_cert_dn"), "ssl_context" in o)
            for o in dialled[::2]
        ]
        # The driver matches no name: its own match of a subject is not exact, so DeltaSleuth's TLS
        # object checks the declared one in the handshake (test_tables_tls); the chain-only mode has none.
        assert tls == [("tcp", None, None, False), ("tcps", False, None, True), ("tcps", False, None, True)]
        checked = [dialled[i]["ssl_context"].sslobject_class for i in (2, 4)]
        assert checked[0] is not ssl.SSLObject and issubclass(checked[0], ssl.SSLObject)
        assert checked[1] is ssl.SSLObject
        assert {(o["service_name"], o["user"], o["retry_count"], o["expire_time"]) for o in dialled} == {
            ("CRMPDB", "ds_reader", 0, 1)
        }
        assert not {"dsn", "sid", "config_dir", "wallet_location"} & set().union(*dialled)
        assert [dialled[2]["ssl_context"].check_hostname, dialled[4]["ssl_context"].check_hostname] == [
            False
        ] * 2
        resolved["crm-db.example"] = ["93.184.216.34", "10.1.2.3"]  # refused before any address is dialled
        with pytest.raises(UnsafeTargetError):
            await ENGINES["oracle"].connect(targets.get("plain"))
        assert len(dialled) == 6


def kept_alive(sock: Any) -> list[int]:
    """The socket's keepalive options, zeros when it sends no probes: idle seconds, interval, probes."""
    idle = getattr(socket, "TCP_KEEPIDLE", None) or socket.TCP_KEEPALIVE
    options = [
        sock.getsockopt(socket.IPPROTO_TCP, option)
        for option in (idle, socket.TCP_KEEPINTVL, socket.TCP_KEEPCNT)
    ]
    return options if sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) else [0, 0, 0]


def test_the_keepalive_ends_a_silent_flow_within_two_minutes() -> None:
    idle, interval, probes = engine.KEEPALIVE
    assert idle + interval * probes == 120


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def tripped(window: int = 2, cooldown_s: float = 1.0) -> tuple[CircuitBreaker, FakeClock]:
    clock = FakeClock()
    breaker = CircuitBreaker(0.5, window, cooldown_s, clock=clock)
    for _ in range(window):
        breaker.record("call", True)
    assert breaker.state == "open"
    return breaker, clock


class TestCircuitBreaker:
    def test_disabled_never_opens(self) -> None:
        breaker = CircuitBreaker(None, 2, 1.0)
        for _ in range(10):
            breaker.record(breaker.admit() or "call", True)
        assert breaker.state == "closed"

    def test_window_must_fill_before_opening(self) -> None:
        breaker = CircuitBreaker(0.5, 4, 1.0)
        for error in (True, True, True):
            breaker.record("call", error)
        assert breaker.state == "closed"  # 3/3 errors, but the window is not full
        breaker.record("call", False)  # 3/4 >= 0.5
        assert breaker.state == "open"

    def test_below_rate_stays_closed(self) -> None:
        breaker = CircuitBreaker(0.5, 4, 1.0)
        for error in (True, False, False, False, True, False):
            breaker.record("call", error)
        assert breaker.state == "closed"

    def test_open_refuses_until_cooldown_then_single_probe(self) -> None:
        breaker, clock = tripped(cooldown_s=10.0)
        breaker.record("call", False)  # a late outcome of a call admitted before it opened
        assert breaker.state == "open" and breaker.admit() is None
        clock.now += 9.9
        assert breaker.admit() is None
        clock.now += 0.1
        assert breaker.admit() == "probe" and breaker.state == "half_open"
        assert breaker.admit() is None  # one probe at a time

    def test_probe_success_closes_with_a_fresh_window(self) -> None:
        breaker, clock = tripped()
        clock.now += 1
        breaker.record(breaker.admit() or "call", False)
        assert breaker.state == "closed"
        breaker.record("call", True)  # one error in a fresh window of two
        assert breaker.state == "closed"

    def test_probe_failure_reopens(self) -> None:
        breaker, clock = tripped()
        clock.now += 1
        breaker.record(breaker.admit() or "call", True)
        assert breaker.state == "open" and breaker.admit() is None

    def test_released_probe_allows_another(self) -> None:
        breaker, clock = tripped()
        clock.now += 1
        breaker.release(breaker.admit() or "call")
        assert breaker.admit() == "probe"
        breaker = CircuitBreaker(0.5, 2, 1.0)
        breaker.release("call")  # releasing an ordinary call is harmless
        assert breaker.state == "closed"


class TestRetryTiming:
    def test_full_jitter_bounds(self) -> None:
        policy = RetryPolicy(max=5, initial_s=0.5, max_s=3.0, multiplier=2.0)
        rng = random.Random(1)  # noqa: S311 - seeded so the bounds check is reproducible
        for retry_no, ceiling in [(1, 0.5), (2, 1.0), (3, 2.0), (4, 3.0), (5, 3.0)]:
            delays = [retry_delay(retry_no, policy, rng, None) for _ in range(200)]
            assert all(0.0 <= d <= ceiling for d in delays)
            assert max(delays) > ceiling * 0.8  # really spread over the whole interval

    def test_retry_after_is_a_floor_never_capped(self) -> None:
        # The delay used to be min(Retry-After, max_s), i.e. a retry before the server's time.
        policy = RetryPolicy(initial_s=0.25, max_s=2.0)
        rng = random.Random(0)  # noqa: S311 - jitter is not security-relevant
        assert retry_delay(1, policy, rng, 120.0) == 120.0
        assert all(0.5 <= retry_delay(3, policy, rng, 0.5) <= 1.0 for _ in range(50))

    def test_retry_after_forms(self) -> None:
        now = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
        assert (parse_retry_after("7"), parse_retry_after(" 0 ")) == (7.0, 0.0)
        assert parse_retry_after(format_datetime(now + timedelta(seconds=30), usegmt=True), now) == 30.0
        assert parse_retry_after(format_datetime(now - timedelta(seconds=30), usegmt=True), now) == 0.0

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "soon",
            "-5",
            "1.5",
            "²",
            "1²",
            "٣",
            "⁵",
            "9" * 400,
            "9" * 5000,
            "1e3",
            "30.5",
            "Mon, 01 Jan 99999 00:00:00 GMT",
            "Tue, 31 Feb 2026 00:00:00 GMT",
            "Sun, 06 Nov 1994 08:49:37 +9999",
        ],
    )
    def test_unusable_retry_after_ignored_without_raising(self, value: str | None) -> None:
        # str.isdigit() accepted "²" and int() then raised, which crashed the whole run.
        assert parse_retry_after(value) is None
