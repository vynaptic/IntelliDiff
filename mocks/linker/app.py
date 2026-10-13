"""The linker mock services: variant a (legacy links API) and variant b (new linked-entities API).

Each variant serves only its own route; the other variant's route is an ordinary unknown path.
"""

from __future__ import annotations

from aiohttp import web

from mocks.common import ScriptedDataset, Variant, build_app
from mocks.linker.dataset import DEFAULT_SEED, build_dataset

ROUTES: dict[str, str] = {
    "a": "/entities/{key}/links",
    "b": "/v2/entities/{key}/linked-entities",
}

__all__ = ["DEFAULT_SEED", "ROUTES", "build_dataset", "create_app"]


def create_app(variant: Variant, dataset: ScriptedDataset | None = None) -> web.Application:
    """The aiohttp app for ``variant``; ``dataset`` defaults to the one built from DEFAULT_SEED."""
    if variant not in ROUTES:
        raise ValueError(f"variant must be one of {tuple(ROUTES)}, got {variant!r}")
    return build_app(build_dataset() if dataset is None else dataset, variant, ROUTES[variant])
