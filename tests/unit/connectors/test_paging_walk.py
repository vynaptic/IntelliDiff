"""A key listing streamed a page at a time: judged as the merged walk judges it,
each page capped on its own, a repeat judged against the previous page only."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from aiohttp import web

from deltasleuth.connectors.rest import ListingIncomplete, RestConnector
from deltasleuth.core.types import Pagination

from .conftest import Recorder, Start, side_config
from .test_paging import CURSOR, LINK, STABLE, cursor_reply, window, window_reply


async def walked(connector: RestConnector, pagination: Pagination) -> tuple[list[list[Any]], str | None]:
    """The pages a walk yielded, and why it was incomplete (None: complete)."""
    pages: list[list[Any]] = []
    try:
        async for page in connector.walk(side_config(pagination=pagination)):
            pages.append(page)
    except ListingIncomplete as exc:
        return pages, str(exc)
    return pages, None


def links(page: int) -> dict[str, str]:
    return {"Link": f'</api/things/keys?p={page + 1}>; rel="next"'} if page < 3 else {}


def linked(request: web.Request) -> web.Response:
    page = int(request.query.get("p", "1"))
    return web.json_response({"items": [page]}, headers=links(page))


def failing(recorder: Recorder) -> Any:
    def reply(request: web.Request) -> web.Response:
        if "cursor" not in request.query:
            return web.json_response({"items": [1, 2], "next": "b"})
        return web.Response(status=503)

    return reply


SCENARIOS = {
    "cursor": (lambda recorder: cursor_reply(), CURSOR, {}, None),
    "loop": (
        lambda recorder: lambda request: web.json_response({"items": [recorder.count], "next": "same"}),
        CURSOR, {}, "HTTP 200: pagination cursor repeated (loop); paging stopped",
    ),
    "max pages": (lambda recorder: cursor_reply(), dataclasses.replace(CURSOR, max_pages=2), {},
                  "HTTP 200: max_pages (2) reached before the last page"),
    "offset declared stable": (lambda recorder: window_reply(recorder, "offset"), window("offset"),
                               {"capabilities": STABLE}, None),
    "offset undeclared": (lambda recorder: window_reply(recorder, "offset"), window("offset"), {},
                          "HTTP 200: an incomplete or unstable page walk"),
    "shifted window": (lambda recorder: window_reply(recorder, "offset", shift=True), window("offset"),
                       {"capabilities": STABLE}, "HTTP 200: an incomplete or unstable page walk"),
    "link header": (lambda recorder: linked, LINK, {}, None),
    "failed page": (failing, CURSOR, {}, "HTTP 503: page 2: HTTP 503"),
}  # fmt: skip


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_the_streaming_walk_judges_as_the_merged_one(
    scenario: str, start: Start, recorder: Recorder
) -> None:
    reply, pagination, options, problem = SCENARIOS[scenario]
    connector = await start(reply(recorder), "/api/things/{key}", **options)
    merged = await connector.fetch("keys", side_config(pagination=pagination))
    recorder.requests.clear()
    pages, incomplete = await walked(connector, pagination)
    complete = merged.pages_eof and not merged.pagination_unstable and merged.error_class is None
    assert (incomplete is None) == complete
    if complete:
        assert [item for page in pages for item in page] == merged_items(merged.body)
    else:
        assert incomplete == f"the key listing of target t was not read completely ({problem})"


def merged_items(body: bytes | None) -> list[Any]:
    doc = json.loads(body or b"")
    return doc["items"] if "items" in doc else doc["data"]["items"]


async def test_each_page_is_capped_never_the_merged_body(start: Start) -> None:
    items = [{"id": i, "pad": "x" * 200} for i in range(12)]
    connector = await start(cursor_reply(items), max_body_bytes=1500)  # a page of 4 items is ~900 bytes
    assert (await connector.fetch("K1", side_config(pagination=CURSOR))).error_class == "resource_limit"
    pages, incomplete = await walked(connector, CURSOR)
    assert incomplete is None and [item["id"] for page in pages for item in page] == list(range(12))
    small = await start(cursor_reply(items), max_body_bytes=500)  # no page fits
    assert (await walked(small, CURSOR))[1] == (
        "the key listing of target t was not read completely (HTTP 200: response body exceeds 500 bytes)"
    )


@pytest.mark.parametrize(
    ("pages", "unstable"),
    [
        ([[1, 2], [2, 3]], True),  # the previous page repeats
        ([[1, 2], [3, 4], [1, 5]], False),  # an earlier page's: its caller finds it
    ],
)
async def test_a_repeat_is_judged_against_the_previous_page_only(
    pages: list[list[int]], unstable: bool, start: Start
) -> None:
    def reply(request: web.Request) -> web.Response:
        n = int(request.query.get("cursor", "0"))
        return web.json_response({"items": pages[n], "next": str(n + 1) if n + 1 < len(pages) else None})

    connector = await start(reply, capabilities=STABLE)
    assert (await connector.fetch("K1", side_config(pagination=CURSOR))).pagination_unstable
    taken, incomplete = await walked(connector, CURSOR)
    assert (incomplete is not None, taken) == (unstable, pages)


async def test_an_unreadable_first_page_is_incomplete(start: Start) -> None:
    connector = await start(lambda request: web.Response(body=b"{not json", content_type="application/json"))
    pages, incomplete = await walked(connector, CURSOR)
    assert pages == [] and incomplete == (
        "the key listing of target t was not read completely (HTTP 200: page 1 could not be parsed"
        " (JSONParseError))"
    )
