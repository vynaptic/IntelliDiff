"""The runner: argument handling, app construction, and a real process serving on loopback."""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import aiohttp
import pytest

from mocks import run
from mocks.common import CALLS, DATASET, VARIANT
from mocks.registry import PORTS
from mocks.run import build, parse_args

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_parses_the_documented_command_line() -> None:
    args = parse_args(["linker", "--variant", "b", "--port", "18082"])
    assert (args.pair, args.variant, args.port, args.host, args.seed) == (
        "linker",
        "b",
        18082,
        "127.0.0.1",
        None,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["nope", "--variant", "a", "--port", "1"],
        ["linker", "--variant", "c", "--port", "1"],
        ["linker", "--variant", "a", "--port", "0"],
        ["linker", "--variant", "a", "--port", "65536"],
        ["linker", "--variant", "a", "--port", "http"],
        ["linker", "--port", "18081"],
        ["linker", "--variant", "a"],
    ],
)
def test_rejects_bad_command_lines(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(argv)


def test_build_uses_the_default_seed_unless_told_otherwise() -> None:
    default = build(parse_args(["customer", "--variant", "a", "--port", "1"]))
    seeded = build(parse_args(["customer", "--variant", "b", "--port", "1", "--seed", "7"]))
    assert default[DATASET].rng_seed == 20260924 and default[VARIANT] == "a"
    assert seeded[DATASET].rng_seed == 7 and seeded[VARIANT] == "b"
    assert len(default[CALLS]) == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_healthy(session: aiohttp.ClientSession, url: str, proc: asyncio.subprocess.Process) -> dict:
    for _ in range(100):
        if proc.returncode is not None:
            raise AssertionError(f"runner exited early with {proc.returncode}")
        try:
            async with session.get(url) as resp:
                return await resp.json()
        except aiohttp.ClientConnectionError:
            await asyncio.sleep(0.1)
    raise AssertionError("runner never became healthy")


async def test_runner_process_serves_the_variant() -> None:
    port = _free_port()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "mocks.run", "linker", "--variant", "b", "--port", str(port),
        cwd=REPO_ROOT, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as session:
            health = await _wait_healthy(session, f"{base}/__health", proc)
            assert health == {"status": "ok", "pair": "linker", "variant": "b"}
            async with session.get(f"{base}/v2/entities/ORG-000052/linked-entities") as first:
                assert first.status == 503
            async with session.get(f"{base}/v2/entities/ORG-000052/linked-entities") as second:
                assert second.status == 200 and second.content_type == "application/json"
    finally:
        proc.terminate()
        await proc.wait()


def test_all_parses_and_takes_neither_a_variant_nor_a_port(capsys: pytest.CaptureFixture[str]) -> None:
    args = parse_args(["all", "--seed", "7"])
    assert (args.pair, args.variant, args.port, args.host, args.seed) == ("all", None, None, "127.0.0.1", 7)
    refusals = {
        ("all", "--variant", "a"): "all serves both variants of both pairs on their own ports: give neither"
        " --variant nor --port",
        ("all", "--port", "18081"): "all serves both variants of both pairs on their own ports: give neither"
        " --variant nor --port",
        ("linker", "--port", "18081"): "--variant and --port are required",
        ("customer",): "--variant and --port are required",
    }
    for argv, refusal in refusals.items():
        with pytest.raises(SystemExit) as refused:
            parse_args(list(argv))
        assert refused.value.code == 2
        assert capsys.readouterr().err.endswith(f"python -m mocks.run: error: {refusal}\n")


def _free_ports() -> dict[str, dict[str, int]]:
    """``PORTS`` with four free ports of this machine."""
    return {pair: {variant: _free_port() for variant in variants} for pair, variants in PORTS.items()}


async def test_all_serves_the_four_in_one_process_until_a_signal() -> None:
    ports = _free_ports()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", f"from mocks import run; run.PORTS = {ports!r}; run.main(['all'])",
        cwd=REPO_ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        assert proc.stdout is not None
        said = [(await asyncio.wait_for(proc.stdout.readline(), 30)).decode() for _ in range(5)]
        served = [
            (pair, variant, port) for pair, variants in ports.items() for variant, port in variants.items()
        ]
        assert said == [
            *(f"{pair} {variant}: http://127.0.0.1:{port}\n" for pair, variant, port in served),
            "Ctrl-C stops all four; POST /__reset on each forgets its call counts.\n",
        ]
        async with aiohttp.ClientSession() as session:
            for pair, variant, port in served:
                async with session.get(f"http://127.0.0.1:{port}/__health") as health:
                    assert await health.json() == {"status": "ok", "pair": pair, "variant": variant}
    finally:
        proc.terminate()
        out, err = await proc.communicate()
    assert (proc.returncode, out, err) == (0, b"", b"")  # SIGTERM is handled, and nothing was logged


def test_all_starts_nothing_on_a_taken_port(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ports = _free_ports()
    monkeypatch.setattr(run, "PORTS", ports)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", ports["customer"]["a"]))
        held.listen()
        with pytest.raises(SystemExit) as refused:
            run.main(["all"])
        said = capsys.readouterr()
        assert refused.value.code == 2 and said.out == ""
        assert said.err == (
            f"error: port {ports['customer']['a']} is in use (customer a): nothing was started\n"
        )
        for port in ports["linker"].values():  # the sites bound before it are closed
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", port))
