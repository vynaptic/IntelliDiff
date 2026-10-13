"""The customer mock services: variant a (profile API v1) and variant b (profile API v2).

Query parameters (``expand``, ``include``) are accepted and ignored: both variants always return
the full profile, as the example spec's requests expect.
"""

from __future__ import annotations

from aiohttp import web

from mocks.common import ScriptedDataset, Variant, build_app
from mocks.customer.dataset import DEFAULT_SEED, build_dataset

ROUTES: dict[str, str] = {
    "a": "/api/v1/customers/{key}",
    "b": "/v2/customers/{key}",
}

__all__ = ["DEFAULT_SEED", "ROUTES", "build_dataset", "create_app"]


def create_app(variant: Variant, dataset: ScriptedDataset | None = None) -> web.Application:
    """The aiohttp app for ``variant``; ``dataset`` defaults to the one built from DEFAULT_SEED."""
    if variant not in ROUTES:
        raise ValueError(f"variant must be one of {tuple(ROUTES)}, got {variant!r}")
    return build_app(build_dataset() if dataset is None else dataset, variant, ROUTES[variant])
