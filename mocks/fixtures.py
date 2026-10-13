"""Write a mock pair's samples: layout-1 files, or one pairs file.

    python -m mocks.fixtures <pair> --out DIR [--seed S]
    python -m mocks.fixtures <pair> --pairs FILE [--seed S]

Layout 1 is ``DIR/<side>/<pct-encoded key>.json`` holding the response body exactly as served,
plus ``<pct-encoded key>.meta.json`` with ``{status, latency_ms, observed_at}``, for every key in
the pair's sample and both sides. ``ds profile`` and ``ds run --samples`` read it.

The pairs file is one JSON array of ``{"key": ..., "a": ..., "b": ...}``, a line for each key of the
sample: what the Specs screen's "Draft a spec from sample pairs" takes as its file, and the ``pairs``
of ``POST /v1/profile``. A side is its recorded body, or null where the recorded status is not 2xx (a
side without a document, as ``ds profile`` reads a sample).

The recorded reply is the one a polite client keeps: it retries 5xx and 429, so it records the
first reply that is not retryable. The drift key therefore records variant b's first (stale)
answer, and the 5xx key its final 200.

Latencies and capture times derive from the key, not a clock, so the samples are reproducible
byte for byte and file-mode results never depend on when the fixtures were written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from mocks.common import VARIANTS, Reply, Script, ScriptedDataset, Variant, encode_body
from mocks.registry import PAIRS, load_pair

CAPTURE_START = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
KEY_SPACING = timedelta(seconds=1)
B_CAPTURE_LAG = timedelta(milliseconds=150)  # b is read just after a, as a live run would


def is_retryable(status: int) -> bool:
    return status == 429 or status >= 500


def recorded_reply(script: Script) -> Reply:
    """The first non-retryable reply; the last reply when every one is retryable."""
    return next((reply for reply in script if not is_retryable(reply.status)), script[-1])


def sample_name(key: str) -> str:
    """The key percent-encoded as one file-name segment (``/``, ``%`` and spaces included)."""
    return quote(key, safe="")


def latency_ms(pair: str, variant: Variant, key: str) -> float:
    """A stable pseudo-latency in [5, 45) ms, spread by a hash of the sample's identity."""
    digest = hashlib.sha256(f"{pair}\x1f{variant}\x1f{key}".encode()).digest()
    return (500 + int.from_bytes(digest[:4], "big") % 4000) / 100


def observed_at(index: int, variant: Variant) -> str:
    moment = CAPTURE_START + index * KEY_SPACING + (B_CAPTURE_LAG if variant == "b" else timedelta())
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _dataset(pair: str, seed: int | None) -> ScriptedDataset:
    module = load_pair(pair)
    return module.build_dataset(module.DEFAULT_SEED if seed is None else seed)


def write_fixtures(pair: str, out: Path, *, seed: int | None = None) -> int:
    """Write both sides' samples for every key of ``pair`` under ``out``; return the sample count."""
    dataset = _dataset(pair, seed)
    count = 0
    for variant in VARIANTS:
        side_dir = out / variant
        side_dir.mkdir(parents=True, exist_ok=True)
        for index, key in enumerate(dataset.keys):
            reply = recorded_reply(dataset.script(variant, key))
            meta = {
                "status": reply.status,
                "latency_ms": latency_ms(pair, variant, key),
                "observed_at": observed_at(index, variant),
            }
            name = sample_name(key)
            (side_dir / f"{name}.json").write_bytes(encode_body(reply.body))
            (side_dir / f"{name}.meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
            count += 1
    return count


def write_pairs(pair: str, path: Path, *, seed: int | None = None) -> int:
    """Write every key of ``pair`` with both sides' documents to the one file ``path``, a JSON array of
    ``{key, a, b}`` with a line for each key; return the pair count. Bodies are encoded as served."""
    dataset = _dataset(pair, seed)
    lines = []
    for key in dataset.keys:
        replies = {variant: recorded_reply(dataset.script(variant, key)) for variant in VARIANTS}
        sides = {side: reply.body if 200 <= reply.status < 300 else None for side, reply in replies.items()}
        lines.append(encode_body({"key": key, **sides}))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"[\n" + b",\n".join(lines) + b"\n]\n")
    return len(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m mocks.fixtures",
        description="Write a mock pair's samples: layout-1 files for ds profile and ds run --samples (--out),"
        " or the one pairs file the Specs screen's draft form and POST /v1/profile take (--pairs).",
    )
    parser.add_argument("pair", choices=PAIRS)
    parser.add_argument("--out", type=Path, metavar="DIR", help="directory to write a/ and b/ into")
    parser.add_argument(
        "--pairs", type=Path, metavar="FILE", help="file to write the array of {key, a, b} to"
    )
    parser.add_argument("--seed", type=int, default=None, help="dataset seed (default: DEFAULT_SEED)")
    args = parser.parse_args(argv)
    if args.out is None and args.pairs is None:
        parser.error("give --out DIR, --pairs FILE or both")
    if args.out is not None:
        count = write_fixtures(args.pair, args.out, seed=args.seed)
        print(f"wrote {count} samples for {args.pair} to {args.out}")
    if args.pairs is not None:
        count = write_pairs(args.pair, args.pairs, seed=args.seed)
        print(f"wrote {count} pairs for {args.pair} to {args.pairs}")


if __name__ == "__main__":
    main()
