"""Per-key pagination through the REST connector: every kind, exact merging, limits, instability."""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal

import pytest
from aiohttp import web

from deltasleuth.connectors.base import ConnectorConfigError
from deltasleuth.connectors.paging import DEFAULT_MAX_PAGES, check_pagination
from deltasleuth.core import jsonio
from deltasleuth.core.canonical.encoding import content_hash
from deltasleuth.core.types import Observation, Pagination, RetryPolicy, TargetCapabilities

from .conftest import Recorder, Start, fast_execution, side_config

ITEMS = [{"id": i, "v": f"{i}.10"} for i in range(10)]
STABLE = TargetCapabilities(stable_ordering=True)
CURSOR = Pagination(kind="cursor", items_path="$.items", cursor_path="$.next", cursor_param="cursor")
LINK = Pagination(kind="link_header", items_path="$.items")


def window(kind: str, **extra: int) -> Pagination:
    page_param = "offset" if kind == "offset" else "page"
    return Pagination(
        kind=kind,
        items_path="$.data.items",
        page_param=page_param,
        size_param="limit",
        **{"size": 4, **extra},
    )  # type: ignore[arg-type]


def raw(body: bytes | str, code: int = 200, **headers: str) -> web.Response:
    return web.Response(status=code, body=body, content_type="application/json", headers=headers)


def ids(obs: Observation, *path: str) -> list:
    doc = json.loads(obs.body or b"")
    for name in path or ("items",):
        doc = doc[name]
    return [item["id"] if isinstance(item, dict) else item for item in doc]


def cursor_reply(items: list = ITEMS, size: int = 4):
    def reply(request: web.Request) -> web.Response:
        start = int(request.query.get("cursor", "0"))
        nxt = str(start + size) if start + size < len(items) else None
        return web.json_response({"items": items[start : start + size], "next": nxt, "total": len(items)})

    return reply


def window_reply(
    recorder: Recorder,
    kind: str,
    items: list = ITEMS,
    *,
    shift: bool = False,
    server_max: int = 0,
    page_math_size: int = 0,
):
    """Offset or page windows. ``shift`` inserts an item at the front after the first page (a
    write during the read); ``server_max`` silently caps the page size, as many APIs do; page
    kind starts pages at ``page_math_size`` multiples (the server's own size by default)."""
    data = list(items)

    def reply(request: web.Request) -> web.Response:
        size = int(request.query["limit"])
        size = min(size, server_max) if server_max else size
        if kind == "offset":
            start = int(request.query["offset"])
        else:
            start = (int(request.query["page"]) - 1) * (page_math_size or size)
        body = {"data": {"items": data[start : start + size]}}
        if shift and recorder.count == 1:
            data.insert(0, {"id": -1, "v": "new"})
        return web.json_response(body)

    return reply


class TestCursor:
    async def test_merges_all_pages(self, start: Start, recorder: Recorder) -> None:
        obs = await (await start(cursor_reply())).fetch("K1", side_config(pagination=CURSOR))
        assert ids(obs) == list(range(10))
        assert (obs.pages, obs.pages_eof, obs.pagination_unstable, obs.error_detail) == (3, True, False, None)
        assert [r["query"] for r in recorder.requests] == [[], [("cursor", "4")], [("cursor", "8")]]
        assert obs.content_hash == content_hash(obs.body)
        assert json.loads(obs.body or b"")["total"] == 10  # the first page's envelope is kept

    async def test_max_pages(self, start: Start, recorder: Recorder) -> None:
        pagination = dataclasses.replace(CURSOR, max_pages=2)
        obs = await (await start(cursor_reply())).fetch("K1", side_config(pagination=pagination))
        assert (ids(obs), obs.pages, obs.pages_eof, recorder.count) == (list(range(8)), 2, False, 2)
        assert "max_pages" in (obs.error_detail or "")
        assert check_pagination(CURSOR).max_pages == DEFAULT_MAX_PAGES  # type: ignore[union-attr]

    async def test_single_page_keeps_original_bytes(self, start: Start) -> None:
        body = b'{ "items" : [ {"id": 1, "n": 1.10} ],\n  "next": null }'
        obs = await (await start(lambda request: raw(body))).fetch("K1", side_config(pagination=CURSOR))
        assert (obs.body, obs.pages, obs.pages_eof) == (body, 1, True)

    async def test_numbers_stay_exact_when_merged(self, start: Start) -> None:
        pages = {
            "0": b'{"items": [{"n": 12345678901234567890.123456789}], "next": "p2"}',
            "p2": b'{"items": [{"n": 0.1}, {"n": 1E+2}], "next": null}',
        }
        conn = await start(lambda request: raw(pages[request.query.get("cursor", "0")]))
        obs = await conn.fetch("K1", side_config(pagination=CURSOR))
        doc, _ = jsonio.parse_json(obs.body or b"")
        assert [item["n"] for item in doc["items"]] == [
            Decimal("12345678901234567890.123456789"),
            Decimal("0.1"),
            Decimal("100"),
        ]
        assert b"12345678901234567890.123456789" in (obs.body or b"")

    async def test_numeric_cursor(self, start: Start, recorder: Recorder) -> None:
        def reply(request: web.Request) -> web.Response:
            return (
                raw(b'{"items": [1], "next": 20}')
                if "cursor" not in request.query
                else raw(b'{"items": [2]}')
            )

        obs = await (await start(reply)).fetch("K1", side_config(pagination=CURSOR))
        assert recorder.requests[1]["query"] == [("cursor", "20")]
        assert ids(obs) == [1, 2]

    @pytest.mark.parametrize(
        ("nxt", "unstable", "calls", "detail"),
        [("same", True, 2, "repeated"), ({"token": "x"}, False, 1, "not a string or number")],
    )
    async def test_cursor_loops_and_bad_cursors_stop(
        self, nxt: object, unstable: bool, calls: int, detail: str, start: Start, recorder: Recorder
    ) -> None:
        conn = await start(lambda request: web.json_response({"items": [recorder.count], "next": nxt}))
        obs = await conn.fetch("K1", side_config(pagination=CURSOR))
        assert (obs.pages_eof, obs.pagination_unstable, recorder.count) == (False, unstable, calls)
        assert detail in (obs.error_detail or "")

    @pytest.mark.parametrize(("second", "unstable"), [([{"id": 2}, {"id": 3}], True), ([{"id": 9}], False)])
    async def test_items_repeated_across_pages_are_unstable_within_a_page_are_data(
        self, second: list, unstable: bool, start: Start
    ) -> None:
        first = [{"id": 1}, {"id": 2}] if unstable else [{"id": 1}, {"id": 1}]

        def reply(request: web.Request) -> web.Response:
            if "cursor" not in request.query:
                return web.json_response({"items": first, "next": "b"})
            return web.json_response({"items": second, "next": None})

        obs = await (await start(reply, capabilities=STABLE)).fetch("K1", side_config(pagination=CURSOR))
        assert (obs.pagination_unstable, obs.pages_eof, ids(obs)) == (
            unstable,
            True,
            [i["id"] for i in first + second],
        )


class TestOffsetAndPage:
    @pytest.mark.parametrize("kind", ["offset", "page"])
    async def test_walk_with_stable_ordering(self, kind: str, start: Start, recorder: Recorder) -> None:
        conn = await start(window_reply(recorder, kind), capabilities=STABLE)
        obs = await conn.fetch("K1", side_config(pagination=window(kind), query={"limit": 99, "q": "x"}))
        assert ids(obs, "data", "items") == list(range(10))
        assert (obs.pages, obs.pages_eof, obs.pagination_unstable) == (3, True, False)
        # The short third page is not proof of the end (servers cap page sizes): one more request
        # must come back empty, and that empty page is not counted in ``pages``.
        param = "offset" if kind == "offset" else "page"
        expected = ["0", "4", "8", "10"] if kind == "offset" else ["1", "2", "3", "4"]
        assert [dict(r["query"])[param] for r in recorder.requests] == expected
        assert all(
            dict(r["query"])["limit"] == "4" and dict(r["query"])["q"] == "x" for r in recorder.requests
        )

    @pytest.mark.parametrize("kind", ["offset", "page"])
    async def test_unstable_without_declared_ordering(
        self, kind: str, start: Start, recorder: Recorder
    ) -> None:
        obs = await (await start(window_reply(recorder, kind))).fetch(
            "K1", side_config(pagination=window(kind))
        )
        assert obs.pagination_unstable and obs.pages_eof

    async def test_single_short_page_is_not_unstable(self, start: Start, recorder: Recorder) -> None:
        conn = await start(window_reply(recorder, "offset", ITEMS[:3]))
        obs = await conn.fetch("K1", side_config(pagination=window("offset")))
        assert (obs.pages, obs.pagination_unstable, obs.pages_eof) == (1, False, True)

    async def test_exact_multiple_needs_an_empty_last_page(self, start: Start, recorder: Recorder) -> None:
        conn = await start(window_reply(recorder, "page", ITEMS[:8]), capabilities=STABLE)
        obs = await conn.fetch("K1", side_config(pagination=window("page")))
        # The empty third page proves the end but is not counted: a one-page read must never
        # look multi-page to observe().
        assert (obs.pages, obs.pages_eof, ids(obs, "data", "items"), recorder.count) == (
            2,
            True,
            list(range(8)),
            3,
        )

    async def test_write_during_paginated_read_is_flagged(self, start: Start, recorder: Recorder) -> None:
        # Even a target that declares stable ordering cannot hide a shifted item: it repeats.
        conn = await start(window_reply(recorder, "offset", shift=True), capabilities=STABLE)
        assert (await conn.fetch("K1", side_config(pagination=window("offset")))).pagination_unstable

    async def test_max_pages(self, start: Start, recorder: Recorder) -> None:
        conn = await start(window_reply(recorder, "page"), capabilities=STABLE)
        obs = await conn.fetch("K1", side_config(pagination=window("page", max_pages=1)))
        assert (obs.pages, obs.pages_eof, recorder.count) == (1, False, 1)


class TestLinkHeader:
    def reply(self, links: dict[str, str]):
        def reply(request: web.Request) -> web.Response:
            page = request.query.get("p", "1")
            headers = (
                {"Link": f'<{links[page]}>; rel="next", </api/other>; rel="prev"'} if page in links else {}
            )
            return web.json_response({"items": [int(page)]}, headers=headers)

        return reply

    async def test_follows_same_origin_links(self, start: Start) -> None:
        links = {"1": "/api/things/K1?p=2", "2": "K1?p=3"}  # an absolute path, then a relative one
        obs = await (await start(self.reply(links))).fetch("K1", side_config(pagination=LINK))
        assert (ids(obs), obs.pages, obs.pages_eof) == ([1, 2, 3], 3, True)

    @pytest.mark.parametrize("link", ["http://evil.example/api/things/K1?p=2", "/admin/things/K1?p=2"])
    async def test_links_leaving_the_origin_or_base_path_not_followed(
        self, link: str, start: Start, recorder: Recorder
    ) -> None:
        # Auth headers are reused on link pages, so they must never reach another origin.
        obs = await (await start(self.reply({"1": link}))).fetch("K1", side_config(pagination=LINK))
        assert (obs.pages, obs.pages_eof, recorder.count) == (1, False, 1)
        assert "same-origin" in (obs.error_detail or "")

    async def test_link_loop(self, start: Start, recorder: Recorder) -> None:
        links = {"1": "/api/things/K1?p=2", "2": "/api/things/K1?p=2"}
        obs = await (await start(self.reply(links))).fetch("K1", side_config(pagination=LINK))
        assert (obs.pages_eof, obs.pagination_unstable, recorder.count) == (False, True, 2)


class TestPageFailures:
    def two_pages(self, recorder: Recorder, second: tuple[int, bytes], *, fail_once: bool = False):
        def reply(request: web.Request) -> web.Response:
            if "cursor" not in request.query:
                return web.json_response({"items": [1, 2], "next": "b"})
            return (
                web.Response(status=503) if fail_once and recorder.count == 2 else raw(second[1], second[0])
            )

        return reply

    async def test_later_page_error_after_retries(self, start: Start, recorder: Recorder) -> None:
        obs = await (await start(self.two_pages(recorder, (500, b"{}")))).fetch(
            "K1", side_config(pagination=CURSOR)
        )
        assert (obs.error_class, obs.http_status, obs.pages, obs.attempts) == ("http_5xx", 500, 2, 3)
        assert (obs.error_detail or "").startswith("page 2:")

    async def test_attempts_and_latency_accumulate(self, start: Start, recorder: Recorder) -> None:
        conn = await start(self.two_pages(recorder, (200, b'{"items": [3], "next": null}'), fail_once=True))
        obs = await conn.fetch("K1", side_config(pagination=CURSOR))
        assert (ids(obs), obs.pages, obs.attempts, obs.error_class) == ([1, 2, 3], 2, 2, None)
        assert obs.latency_ms is not None and obs.latency_ms > 0

    async def test_retry_policy_applies_per_page(self, start: Start, recorder: Recorder) -> None:
        conn = await start(
            self.two_pages(recorder, (502, b"{}")), execution=fast_execution(retries=RetryPolicy(max=0))
        )
        obs = await conn.fetch("K1", side_config(pagination=CURSOR))
        assert (obs.error_class, obs.attempts, recorder.count) == ("http_5xx", 1, 2)

    @pytest.mark.parametrize(
        ("second", "reason"),
        [
            ((404, b'{"error": "gone"}'), "HTTP 404"),
            ((200, b"not json"), "could not be parsed"),
            ((200, b'{"rows": []}'), "no array at items_path"),
            ((200, b'{"items": [3], "items": [4], "next": null}'), "duplicate member names"),
        ],
    )
    async def test_later_page_problems_stop_the_walk(
        self, second: tuple[int, bytes], reason: str, start: Start, recorder: Recorder
    ) -> None:
        obs = await (await start(self.two_pages(recorder, second))).fetch(
            "K1", side_config(pagination=CURSOR)
        )
        assert (obs.http_status, obs.error_class, obs.pages_eof, obs.pages, ids(obs)) == (
            200,
            None,
            False,
            2,
            [1, 2],
        )
        assert reason in (obs.error_detail or "")

    async def test_body_cap_counts_all_pages(self, start: Start) -> None:
        def reply(request: web.Request) -> web.Response:
            return web.json_response(
                {"items": ["x" * 300], "next": None if "cursor" in request.query else "b"}
            )

        obs = await (await start(reply, max_body_bytes=500)).fetch("K1", side_config(pagination=CURSOR))
        assert (obs.error_class, obs.pages) == ("resource_limit", 2)


class TestFirstPage:
    @pytest.mark.parametrize(
        ("code", "body", "pages_eof", "detail"),
        [
            (404, b'{"next": "x"}', True, None),  # not a success: not paged
            (200, b"<html>", True, None),  # unreadable: returned raw for observe()
            (200, b'{"items": [1], "a": 1, "a": 2, "next": null}', True, None),  # kept verbatim
            (200, b'{"items": [1], "a": 1, "a": 2, "next": "b"}', False, "duplicate member names"),
            (200, b'{"rows": [], "next": "x"}', False, "items_path"),
        ],
    )
    async def test_first_page_that_cannot_be_walked(
        self, code: int, body: bytes, pages_eof: bool, detail: str | None, start: Start
    ) -> None:
        obs = await (await start(lambda request: raw(body, code))).fetch("K1", side_config(pagination=CURSOR))
        assert (obs.http_status, obs.body, obs.pages, obs.pages_eof) == (code, body, 1, pages_eof)
        assert detail is None or detail in (obs.error_detail or "")

    @pytest.mark.parametrize(
        "cfg",
        [
            Pagination(kind="cursor", items_path="$.items", cursor_path="$.next"),
            Pagination(kind="cursor", items_path="$.items"),
            Pagination(kind="offset", items_path="$.items", page_param="o", size_param="l"),
            Pagination(kind="page", items_path="$.items", page_param="p", size_param="s", size=0),
            Pagination(kind="link_header"),
            Pagination(kind="link_header", items_path="$.items", max_pages=0),
            Pagination(kind="link_header", items_path="$[?"),
            Pagination(kind="scroll", items_path="$.items"),  # type: ignore[arg-type]
        ],
    )
    async def test_invalid_settings_rejected_before_any_call(
        self, cfg: Pagination, start: Start, recorder: Recorder
    ) -> None:
        with pytest.raises(ConnectorConfigError):
            await (await start(cursor_reply())).fetch("K1", side_config(pagination=cfg))
        assert recorder.count == 0


# Regression tests


class TestShortPageIsNotTheEnd:
    """A page shorter than the requested size used to end the walk as 'complete'."""

    async def test_clamped_offset_walk_reads_every_item(self, start: Start, recorder: Recorder) -> None:
        items = [{"id": i} for i in range(120)]
        conn = await start(window_reply(recorder, "offset", items, server_max=50), capabilities=STABLE)
        obs = await conn.fetch("K1", side_config(pagination=window("offset", size=200)))
        assert ids(obs, "data", "items") == list(range(120))
        assert [dict(r["query"])["offset"] for r in recorder.requests] == ["0", "50", "100", "120"]
        assert (obs.pages, obs.pages_eof, obs.pagination_unstable) == (3, True, False)
        assert "caps the page size" in (obs.error_detail or "")

    async def test_clamped_offset_walk_without_stable_ordering_is_unstable(
        self, start: Start, recorder: Recorder
    ) -> None:
        conn = await start(window_reply(recorder, "offset", [{"id": i} for i in range(120)], server_max=50))
        obs = await conn.fetch("K1", side_config(pagination=window("offset", size=200)))
        assert obs.pagination_unstable and obs.pages == 3  # never a one-page 'complete' read

    async def test_clamped_page_walk_stops_unproven(self, start: Start, recorder: Recorder) -> None:
        # Page numbers multiplied by the REQUESTED size skip items 50-199: never trusted.
        items = [{"id": i} for i in range(300)]
        conn = await start(
            window_reply(recorder, "page", items, server_max=50, page_math_size=200), capabilities=STABLE
        )
        obs = await conn.fetch("K1", side_config(pagination=window("page", size=200)))
        assert (obs.pages_eof, recorder.count, ids(obs, "data", "items")) == (False, 2, list(range(50)))
        assert "caps the page size" in (obs.error_detail or "")

    @pytest.mark.parametrize("kind", ["offset", "page"])
    async def test_empty_first_page_needs_no_second_request(
        self, kind: str, start: Start, recorder: Recorder
    ) -> None:
        obs = await (await start(window_reply(recorder, kind, []))).fetch(
            "K1", side_config(pagination=window(kind))
        )
        assert (obs.pages, obs.pages_eof, obs.pagination_unstable, recorder.count) == (1, True, False, 1)

    async def test_short_single_page_keeps_its_bytes(self, start: Start, recorder: Recorder) -> None:
        # The end-proof page adds nothing: the document is page 1, verbatim, with its own hash.
        body = b'{"data": {"items": [{"id": 1, "n": 1.10}]}}'
        conn = await start(
            lambda request: raw(body if request.query["offset"] == "0" else b'{"data": {"items": []}}')
        )
        obs = await conn.fetch("K1", side_config(pagination=window("offset")))
        assert (obs.body, obs.content_hash, obs.pages) == (body, content_hash(body), 1)
        assert [dict(r["query"])["offset"] for r in recorder.requests] == ["0", "1"]


class TestMaxPagesCountsPagesOfData:
    """The end check after a short window is not a page of data: maxPages must not truncate it."""

    async def test_short_page_at_the_cap_is_confirmed(self, start: Start, recorder: Recorder) -> None:
        conn = await start(window_reply(recorder, "offset", ITEMS[:3]))
        obs = await conn.fetch("K1", side_config(pagination=window("offset", max_pages=1)))
        assert (obs.pages, obs.pages_eof, recorder.count, ids(obs, "data", "items")) == (
            1,
            True,
            2,
            [0, 1, 2],
        )

    async def test_data_past_the_cap_is_not_merged(self, start: Start, recorder: Recorder) -> None:
        items = [{"id": i} for i in range(120)]
        conn = await start(window_reply(recorder, "offset", items, server_max=50), capabilities=STABLE)
        obs = await conn.fetch("K1", side_config(pagination=window("offset", size=200, max_pages=1)))
        assert (obs.pages_eof, recorder.count, ids(obs, "data", "items")) == (False, 2, list(range(50)))
        assert "max_pages (1)" in (obs.error_detail or "")


class TestWindowLoops:
    """Offset/page walks had no loop detection and ran to maxPages (1000) per side."""

    async def test_server_ignoring_the_page_parameter(self, start: Start, recorder: Recorder) -> None:
        conn = await start(
            lambda request: web.json_response({"data": {"items": ITEMS[:4]}}), capabilities=STABLE
        )
        obs = await conn.fetch("K1", side_config(pagination=window("page")))
        assert (recorder.count, obs.pages_eof, obs.pagination_unstable) == (2, False, True)
        assert "repeats items of earlier pages" in (obs.error_detail or "")

    async def test_server_clamping_pages_past_the_end(self, start: Start, recorder: Recorder) -> None:
        items = [{"id": i} for i in range(20)]  # five full pages of 4

        def reply(request: web.Request) -> web.Response:
            page = min(int(request.query["page"]), 5)  # past the end: the last page again
            return web.json_response({"data": {"items": items[(page - 1) * 4 : page * 4]}})

        obs = await (await start(reply, capabilities=STABLE)).fetch(
            "K1", side_config(pagination=window("page"))
        )
        assert (recorder.count, obs.pages_eof, obs.pagination_unstable) == (6, False, True)
        assert ids(obs, "data", "items") == list(range(20))


class TestMalformedLinks:
    """An invalid Link URL raised ValueError out of the connector and killed the run."""

    @pytest.mark.parametrize(
        "link",
        [
            '<http://[::1>; rel="next"',
            "<http://h:99999/x>; rel=next",
            '<http://h/x>; rel=preload, <http://[bad>; rel="next"',
        ],
    )
    async def test_link_header_side_stops_unproven(self, link: str, start: Start, recorder: Recorder) -> None:
        conn = await start(lambda request: web.json_response({"items": [1]}, headers={"Link": link}))
        obs = await conn.fetch("K1", side_config(pagination=LINK))
        assert (obs.error_class, obs.pages_eof, recorder.count) == (None, False, 1)
        assert "not a valid URL" in (obs.error_detail or "")

    @pytest.mark.parametrize("pagination", [None, CURSOR])
    async def test_other_sides_never_parse_links(self, pagination: Pagination | None, start: Start) -> None:
        conn = await start(
            lambda request: web.json_response(
                {"items": [1], "next": None}, headers={"Link": '<http://[::1>; rel="next"'}
            )
        )
        obs = await conn.fetch("K1", side_config(pagination=pagination))
        assert (obs.http_status, obs.error_class, obs.pages_eof) == (200, None, True)


async def test_merged_body_above_the_cap_is_a_resource_limit(start: Start) -> None:
    # 1E+99 is 5 bytes on the wire and 100 digits once merged with exact numbers, so
    # pages that fit the cap used to merge into a body the evidence store refused (run crash).
    def reply(request: web.Request) -> web.Response:
        nxt = None if "cursor" in request.query else "b"
        return raw('{"items": [' + ", ".join(["1E+99"] * 20) + '], "next": ' + json.dumps(nxt) + "}")

    obs = await (await start(reply, max_body_bytes=1000)).fetch("K1", side_config(pagination=CURSOR))
    assert (obs.error_class, obs.body, obs.content_hash, obs.pages) == ("resource_limit", None, None, 2)
    assert "exceeds 1000 bytes" in (obs.error_detail or "")
