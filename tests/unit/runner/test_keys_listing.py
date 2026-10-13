"""A run's keys read from a ``paginated_endpoint`` listing through the connectors."""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.runner.keys import resolve_keys

from .conftest import TARGETS, OpenHarness, spec_text

pytestmark = pytest.mark.db

LISTED = """\
  source:
    kind: paginated_endpoint
    target: svc-a
    request: {method: GET, path: /list}
    pagination: {kind: cursor, itemsPath: "$.items", cursorPath: "$.next", cursorParam: cursor}
    keyPath: "$.id"
"""


@pytest.mark.parametrize(
    "body",
    [
        b'{"items": [{"id": "K001"}, {"id": "K002"}], "items": [{"id": "K003"}], "next": null}',
        b'{"items": [{"id": "K001", "id": "K009"}, {"id": "K002"}], "next": null}',
    ],
    ids=["repeated items", "repeated key"],
)
async def test_a_one_page_listing_repeating_a_member_name_is_refused(
    open_harness: OpenHarness, body: bytes
) -> None:
    async def listing(_: web.Request) -> web.Response:
        return web.Response(body=body, content_type="application/json")

    app = web.Application()
    app.router.add_get("/list", listing)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        async with open_harness(TARGETS.format(base=f"http://127.0.0.1:{server.port}", rate=500)) as h:
            text = spec_text().replace("  source: {kind: list, keys: [K001, K002, K003]}\n", LISTED)
            spec = compile_spec(parse_yaml_text(text), targets=h.ctx.targets)
            with pytest.raises(ValueError, match="repeats a member name"):
                await resolve_keys(h.ctx, spec)
    finally:
        await server.close()


async def test_key_path_selects_the_parts(open_harness: OpenHarness) -> None:
    """A multi-part key's ``keyPath`` selects its parts, in the selector's order whatever the item's: as
    many values as the key has parts, each a string or a number. Any other item is unfit, and refused by
    its position as a key that does not fit is."""
    listed: list[dict[str, Any]] = [
        {"region": " eu ", "shelf": "s|1", "code": 7},
        {"code": "k2", "more": "x", "shelf": "s2", "region": "us"},
    ]
    fit = {"region": "eu", "shelf": "s", "code": 1}
    unfit = [fit | {"shelf": s} for s in (None, True, ["s"], {"s": 1})] + [{"region": "eu", "code": 1}]

    async def listing(_: web.Request) -> web.Response:
        return web.Response(body=json.dumps({"items": listed, "next": None}), content_type="application/json")

    app = web.Application()
    app.router.add_get("/list", listing)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    by_parts = LISTED.replace("$.id", "$['region','shelf','code']")
    text = spec_text().replace("  source: {kind: list, keys: [K001, K002, K003]}\n", by_parts)
    text = text.replace("type: string", "type: composite\n  parts: [region, shelf, code]")
    text = text.replace('"^K[0-9]{3}$"', '"^[A-Z0-9|]+$"')
    try:
        async with open_harness(TARGETS.format(base=f"http://127.0.0.1:{server.port}", rate=500)) as h:
            spec = compile_spec(parse_yaml_text(text), targets=h.ctx.targets)
            assert await resolve_keys(h.ctx, spec) == ["EU|S\\|1|7", "US|S2|K2"]
            listed += [*unfit, fit | {"code": ".."}, fit]
            with pytest.raises(
                ValueError, match=r"^6 key\(s\) are not 3 parts .* \(positions 3, 4, 5, 6, 7, 8\)$"
            ):
                await resolve_keys(h.ctx, spec)
    finally:
        await server.close()
