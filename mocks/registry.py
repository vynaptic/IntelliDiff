"""The mock pairs by name, for the runner and the fixture writer.

Pair packages are imported lazily so that serving one pair never imports the other.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Protocol, cast

from aiohttp import web

from mocks.common import ScriptedDataset, Variant

PAIRS: tuple[str, ...] = ("linker", "customer")
# Each variant's port in the pairs' targets files, mocks/targets.yaml and the Compose mocks service.
PORTS = {"linker": {"a": 18081, "b": 18082}, "customer": {"a": 18091, "b": 18092}}
MOCKS_DIR = Path(__file__).resolve().parent


class PairModule(Protocol):
    """What every ``mocks.<pair>.app`` module provides."""

    DEFAULT_SEED: int

    def build_dataset(self, seed: int = ...) -> ScriptedDataset: ...

    def create_app(self, variant: Variant, dataset: ScriptedDataset | None = None) -> web.Application: ...


def load_pair(name: str) -> PairModule:
    if name not in PAIRS:
        raise ValueError(f"unknown mock pair {name!r}; expected one of {PAIRS}")
    return cast(PairModule, importlib.import_module(f"mocks.{name}.app"))


def pair_dir(name: str) -> Path:
    """Directory holding the pair's spec.yaml, targets.yaml, keys.txt and expected.json."""
    if name not in PAIRS:
        raise ValueError(f"unknown mock pair {name!r}; expected one of {PAIRS}")
    return MOCKS_DIR / name
