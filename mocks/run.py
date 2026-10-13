"""Serve one variant of one mock pair, or both variants of both pairs in one process.

    python -m mocks.run <pair> --variant a|b --port N [--host 127.0.0.1] [--seed S]
    python -m mocks.run all [--host 127.0.0.1] [--seed S]

The access log is off: request paths carry keys, and the mocks keep the same hygiene as the tool.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import signal
import sys

from aiohttp import web

from mocks.common import VARIANTS
from mocks.registry import PAIRS, PORTS, load_pair

ALL = "all"


def _port(text: str) -> int:
    port = int(text)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be in 1..65535, got {port}")
    return port


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m mocks.run", description="Serve one mock variant, or all four."
    )
    parser.add_argument("pair", choices=(*PAIRS, ALL))
    parser.add_argument("--variant", choices=VARIANTS, help="a = legacy, b = new")
    parser.add_argument("--port", type=_port)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--seed", type=int, default=None, help="dataset seed (default: DEFAULT_SEED)")
    args = parser.parse_args(argv)
    given = (args.variant is not None, args.port is not None)
    if args.pair == ALL and any(given):
        parser.error(
            "all serves both variants of both pairs on their own ports: give neither --variant nor --port"
        )
    if args.pair != ALL and not all(given):
        parser.error("--variant and --port are required")
    return args


def build(args: argparse.Namespace) -> web.Application:
    pair = load_pair(args.pair)
    dataset = pair.build_dataset(pair.DEFAULT_SEED if args.seed is None else args.seed)
    return pair.create_app(args.variant, dataset)


async def serve_all(host: str, seed: int | None) -> str | None:
    """Serve the four on ``PORTS`` until SIGINT or SIGTERM. A taken port starts nothing: the sites
    already bound are closed, and why is returned."""
    runners: list[web.AppRunner] = []
    try:
        for pair, variants in PORTS.items():
            for variant, port in variants.items():
                app = build(argparse.Namespace(pair=pair, variant=variant, seed=seed))
                runners.append(web.AppRunner(app, access_log=None))
                await runners[-1].setup()
                try:
                    await web.TCPSite(runners[-1], host, port).start()
                except OSError as exc:
                    if exc.errno != errno.EADDRINUSE:
                        raise
                    return f"port {port} is in use ({pair} {variant}): nothing was started"
        for pair, variants in PORTS.items():
            for variant, port in variants.items():
                print(f"{pair} {variant}: http://{host}:{port}")
        print("Ctrl-C stops all four; POST /__reset on each forgets its call counts.", flush=True)
        stop = asyncio.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(signum, stop.set)
        await stop.wait()
    finally:
        for runner in runners:
            await runner.cleanup()
    return None


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.pair != ALL:
        web.run_app(build(args), host=args.host, port=args.port, access_log=None)
    elif refused := asyncio.run(serve_all(args.host, args.seed)):
        print(f"error: {refused}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
