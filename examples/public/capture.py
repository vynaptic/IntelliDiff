"""Re-capture the public-pair samples (sample layout 1) exactly as each example's spec requests them,
at the rate its targets.yaml allows.

Usage: python examples/public/capture.py [out_dir]   (default: tests/fixtures/public)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from deltasleuth.connectors.files import SampleWriter
from deltasleuth.connectors.rest import RestConnector
from deltasleuth.core.spec import compile_spec, load_spec_file, load_targets

HERE = Path(__file__).parent
PAIRS = ("swapi", "rickandmorty")


async def capture(example: Path, out: Path) -> None:
    targets = load_targets(example / "targets.yaml")
    spec = compile_spec(load_spec_file(example / "spec.yaml"), targets=targets)
    writer = SampleWriter(out)
    for cfg in spec.sides.values():
        connector = RestConnector(targets.live(cfg.target), spec.execution)
        try:
            for key in (example / "keys.txt").read_text(encoding="utf-8").split():
                writer.write_observation(await connector.fetch(key, cfg))
        finally:
            await connector.aclose()


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parents[1] / "tests/fixtures/public"
    for pair in PAIRS:
        asyncio.run(capture(HERE / pair, out / pair))


if __name__ == "__main__":
    main()
