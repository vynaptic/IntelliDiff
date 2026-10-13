"""File samples: both layouts, the writer round trip, key encoding, root containment and completeness."""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import stat
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from aiohttp import web

from deltasleuth.connectors.base import Connector, NotCapturedError
from deltasleuth.connectors.files import (
    FileConnector,
    FileSampleSource,
    SampleError,
    SampleWriter,
    decode_key,
    encode_key,
)
from deltasleuth.core import jsonio
from deltasleuth.core.canonical.encoding import content_hash
from deltasleuth.core.canonical.observe import observe
from deltasleuth.core.types import (
    CompareConfig,
    CompiledSpec,
    KeyConfig,
    Observation,
    Pagination,
    Side,
    SideConfig,
    SideStateKind,
    TargetCapabilities,
)
from tests.conftest import evidence_store

from .conftest import Start, side_config

AWKWARD_KEYS = [
    "ORG-000123",
    "a/b",
    "../../etc/passwd",
    ".",
    "..",
    "x.meta",
    "café ß",
    "漢字",
    "%41",
    "a b?c#d",
]
CURSOR = Pagination(kind="cursor", items_path="$.items", cursor_path="$.next", cursor_param="cursor")
OFFSET = Pagination(kind="offset", items_path="$.items", page_param="offset", size_param="limit", size=2)
LINKS = Pagination(kind="link_header", items_path="$.items")
AT = "2026-09-24T08:00:00.000000Z"


def write_jsonl(path: Path, *records: object) -> None:
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in records) + "\n")


def hand_made(root: Path, body: bytes, meta: dict | None = None, name: str = "K") -> None:
    """A layout-1 sample written by hand."""
    (root / "a").mkdir(exist_ok=True)
    (root / "a" / f"{name}.json").write_bytes(body)
    if meta is not None:
        (root / "a" / f"{name}.meta.json").write_text(json.dumps(meta))


def judge(
    obs: Observation, side_cfg: SideConfig, caps: TargetCapabilities
) -> tuple[SideStateKind, str | None]:
    spec = CompiledSpec(
        name="t",
        version="1",
        content_hash="x",
        normalizer_version="1",
        key=KeyConfig(name="id"),
        sides={side_cfg.side: side_cfg},
        compare=CompareConfig(),
    )
    state = observe(obs, side_cfg, spec, caps)
    return state.kind, state.reason


@pytest.fixture
def umask_022() -> Iterator[None]:
    previous = os.umask(0o022)  # the usual default: without explicit modes files end up 0644
    yield
    os.umask(previous)


class TestKeyEncoding:
    @pytest.mark.parametrize("key", AWKWARD_KEYS)
    def test_round_trip_and_flat(self, key: str) -> None:
        stem = encode_key(key)
        assert decode_key(stem) == key
        assert "/" not in stem and "." not in stem and stem.isascii()

    def test_plain_quote_names_are_readable(self) -> None:
        assert decode_key("a.b") == "a.b"

    @pytest.mark.parametrize(("encode", "value"), [(True, ""), (True, None), (False, "%FF"), (False, "")])
    def test_invalid_keys_and_names(self, encode: bool, value: object) -> None:
        with pytest.raises(SampleError):
            (encode_key if encode else decode_key)(value)  # type: ignore[operator]


class TestLayoutOne:
    def test_writer_round_trip(self, tmp_path: Path) -> None:
        writer = SampleWriter(tmp_path)
        for i, key in enumerate(AWKWARD_KEYS):
            writer.write(
                key,
                Side.A,
                f'{{"k": {i}}}'.encode(),
                status=200 + i,
                headers={"Content-Type": "application/json", "Set-Cookie": "x=1"},
                latency_ms=12.5 + i,
                observed_at="2026-09-24T10:00:00+02:00",
            )
        writer.write(AWKWARD_KEYS[0], "b", b"\x00\xffnot json", status=404)
        source = FileSampleSource(tmp_path)
        assert source.keys() == sorted(AWKWARD_KEYS)
        for i, key in enumerate(AWKWARD_KEYS):
            obs = source.observation(key, Side.A)
            assert obs is not None
            assert (obs.key, obs.side, obs.http_status, obs.body) == (
                key,
                Side.A,
                200 + i,
                f'{{"k": {i}}}'.encode(),
            )
            assert obs.headers == {"content-type": "application/json"}  # lower-cased, cookie dropped
            assert (obs.latency_ms, obs.observed_at) == (12.5 + i, AT)
            assert (obs.source, obs.content_hash, obs.error_class) == ("file", content_hash(obs.body), None)
        other = source.observation(AWKWARD_KEYS[0], "b")
        assert other is not None and (other.body, other.http_status) == (b"\x00\xffnot json", 404)
        assert source.observation(AWKWARD_KEYS[1], Side.B) is None

    def test_write_observation(self, tmp_path: Path) -> None:
        obs = Observation("K/1", Side.B, 410, b"{}", AT, latency_ms=3.25, headers={"etag": "w/1"})
        assert SampleWriter(tmp_path).write_observation(obs) == tmp_path / "b" / "K%2F1.json"
        loaded = FileSampleSource(tmp_path).observation("K/1", Side.B)
        assert loaded == dataclasses.replace(obs, source="file", content_hash=content_hash(b"{}"))
        with pytest.raises(SampleError):  # a bodiless observation is not a layout-1 sample
            SampleWriter(tmp_path).write_observation(
                Observation("K", Side.A, None, None, AT, error_class="timeout")
            )

    def test_defaults_without_meta_and_verbatim_bytes(self, tmp_path: Path) -> None:
        raw = b'{ "n": 1.10,  "n": 2 }\n'  # duplicates are left for observe() to flag
        hand_made(tmp_path, raw)
        os.utime(tmp_path / "a" / "K.json", (1_700_000_000, 1_700_000_000))
        obs = FileSampleSource(tmp_path).observation("K", "a")
        assert obs is not None
        assert (obs.body, obs.http_status, obs.headers, obs.latency_ms) == (raw, 200, {}, None)
        assert obs.observed_at == "2023-11-14T22:13:20.000000Z"

    def test_meta_for_dotted_key_is_unambiguous(self, tmp_path: Path) -> None:
        writer = SampleWriter(tmp_path)
        writer.write("x", Side.A, b"1", status=201)
        writer.write("x.meta", Side.A, b"2", status=202)
        source = FileSampleSource(tmp_path)
        assert source.keys() == ["x", "x.meta"]
        assert [source.observation(k, Side.A).http_status for k in ("x", "x.meta")] == [201, 202]  # type: ignore[union-attr]

    def test_rewrite_same_key_replaces(self, tmp_path: Path) -> None:
        writer = SampleWriter(tmp_path)
        writer.write("K", Side.A, b"old")
        writer.write("K", Side.A, b"new")
        assert FileSampleSource(tmp_path).observation("K", Side.A).body == b"new"  # type: ignore[union-attr]

    def test_case_collision_and_overlong_names_refused(self, tmp_path: Path) -> None:
        SampleWriter(tmp_path).write("abc", Side.A, b"1")
        writer = SampleWriter(tmp_path)  # also against files present before the writer
        with pytest.raises(SampleError, match="collides"):
            writer.write("ABC", Side.A, b"2")
        writer.write("ABC", Side.B, b"2")  # the other side is a separate directory
        with pytest.raises(SampleError, match="too long"):
            writer.write("é" * 100, Side.A, b"1")

    @pytest.mark.parametrize(
        ("meta", "message"),
        [
            ({"status": "200"}, "status"),
            ({"status": 99}, "status"),
            ({"status": True}, "status"),
            ({"headers": {"a": 1}}, "headers"),
            ({"headers": []}, "headers"),
            ({"latency_ms": -1}, "latency_ms"),
            ({"latency_ms": "fast"}, "latency_ms"),
            ({"observed_at": "yesterday"}, "observed_at"),
            ({"observed_at": "2026-09-24T10:00:00"}, "UTC offset"),
            ({"observed_at": 5}, "observed_at"),
            ({"url": "http://x"}, "unknown fields"),
            ({"pages": 2}, "all or none"),
            ({"pages": 2, "pages_eof": True}, "all or none"),
            ({"pages": -1, "pages_eof": True, "pagination_unstable": False}, "pages"),
            ({"pages": 1, "pages_eof": "yes", "pagination_unstable": False}, "pages_eof"),
            ({"attempts": 1.5}, "attempts"),
            ({"error_class": "teapot"}, "error_class"),
            ({"error_detail": 3}, "error_detail"),
        ],
    )
    def test_invalid_meta(self, tmp_path: Path, meta: dict, message: str) -> None:
        hand_made(tmp_path, b"{}", meta)
        source = FileSampleSource(tmp_path)
        with pytest.raises(SampleError, match=message):
            source.observation("K", Side.A)

    def test_meta_with_duplicate_names(self, tmp_path: Path) -> None:
        hand_made(tmp_path, b"{}")
        (tmp_path / "a" / "K.meta.json").write_text('{"status": 200, "status": 404}')
        with pytest.raises(SampleError, match="duplicate"):
            FileSampleSource(tmp_path).observation("K", Side.A)

    def test_writer_validates_meta(self, tmp_path: Path) -> None:
        for capture in ({"status": 42}, {"observed_at": "not a date"}, {"url": "x"}):
            with pytest.raises(SampleError):
                SampleWriter(tmp_path).write("K", Side.A, b"{}", **capture)

    @pytest.mark.parametrize(
        ("files", "message"),
        [
            ({"K.meta.json": b"{}"}, "without a body"),
            ({"a.b.json": b"1", "a%2Eb.json": b"2"}, "same key"),
            ({"nested/x.json": b"1"}, "unexpected directory"),
        ],
    )
    def test_ambiguous_side_directories_rejected(self, tmp_path: Path, files: dict, message: str) -> None:
        for name, data in files.items():
            (tmp_path / "a" / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / "a" / name).write_bytes(data)
        with pytest.raises(SampleError, match=message):
            FileSampleSource(tmp_path)

    def test_non_sample_files_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        for name in ("README.md", ".DS_Store", ".K.json.123.tmp"):
            (tmp_path / "a" / name).write_bytes(b"x")
        assert FileSampleSource(tmp_path).keys() == []

    def test_bad_roots(self, tmp_path: Path) -> None:
        (tmp_path / "file").write_text("x")
        (tmp_path / "side-is-a-file").mkdir()
        (tmp_path / "side-is-a-file" / "a").write_text("oops")
        for root in ("absent", "file", "side-is-a-file"):
            with pytest.raises(SampleError):
                FileSampleSource(tmp_path / root)


class TestContainment:
    def test_symlinks_outside_the_root_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside.json"
        outside.write_bytes(b'{"secret": 1}')
        (tmp_path / "elsewhere").mkdir()
        write_jsonl(tmp_path / "x.jsonl", {"key": "K", "side": "a", "body": {}})
        for link, target in (
            ("a/K.json", outside),
            ("a", tmp_path / "elsewhere"),
            ("x.jsonl", tmp_path / "x.jsonl"),
        ):
            root = tmp_path / f"root-{link.replace('/', '-')}"
            (root / link).parent.mkdir(parents=True, exist_ok=True)
            (root / link).symlink_to(target)
            with pytest.raises(SampleError, match="outside the sample root"):
                FileSampleSource(root)

    def test_symlink_inside_root_allowed_and_broken_one_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "store").mkdir()
        (tmp_path / "store" / "real.json").write_bytes(b"{}")
        (tmp_path / "a" / "K.json").symlink_to(tmp_path / "store" / "real.json")
        assert FileSampleSource(tmp_path).observation("K", Side.A).body == b"{}"  # type: ignore[union-attr]
        (tmp_path / "a" / "B.json").symlink_to(tmp_path / "missing.json")
        with pytest.raises(SampleError, match="cannot be resolved"):
            FileSampleSource(tmp_path)

    @pytest.mark.parametrize("was_link", [True, False])
    def test_sample_swapped_for_an_escaping_symlink_after_scan(self, was_link: bool, tmp_path: Path) -> None:
        root = tmp_path / "root"
        (root / "a").mkdir(parents=True)
        (root / "inside.json").write_bytes(b"{}")
        (tmp_path / "outside.json").write_bytes(b'{"secret": 1}')
        sample = root / "a" / "K.json"
        sample.symlink_to(root / "inside.json") if was_link else sample.write_bytes(b"{}")
        source = FileSampleSource(root)
        sample.unlink()
        sample.symlink_to(tmp_path / "outside.json")
        with pytest.raises(SampleError, match="outside the sample root"):
            source.observation("K", Side.A)


class TestLayoutTwo:
    def test_body_variants(self, tmp_path: Path) -> None:
        write_jsonl(
            tmp_path / "samples.jsonl",
            {"key": "K1", "side": "a", "body": {"n": 1}},
            "",
            {"key": "K1", "side": "b", "body_text": "<html>é</html>", "status": 503},
            {"key": "K2", "side": "a", "body_b64": base64.b64encode(b"\x00\x01binary").decode()},
            {"key": "K2", "side": "b", "body": None, "headers": {"ETag": "v1"}, "latency_ms": 7},
        )
        source = FileSampleSource(tmp_path)
        assert source.keys() == ["K1", "K2"]
        a1, b1, a2, b2 = (source.observation(k, s) for k in ("K1", "K2") for s in ("a", "b"))
        assert a1 is not None and b1 is not None and a2 is not None and b2 is not None
        assert (a1.body, a1.http_status) == (b'{"n": 1}', 200)
        assert (b1.body, b1.http_status) == ("<html>é</html>".encode(), 503)
        assert a2.body == b"\x00\x01binary"
        assert (b2.body, b2.headers, b2.latency_ms) == (b"null", {"etag": "v1"}, 7.0)
        assert all(o.source == "file" and o.content_hash == content_hash(o.body) for o in (a1, b1, a2, b2))

    def test_body_numbers_reserialised_exactly(self, tmp_path: Path) -> None:
        line = '{"key": "K", "side": "a", "body": {"big": 12345678901234567890.123456789, "small": 1e-30}}'
        write_jsonl(tmp_path / "s.jsonl", line)
        doc, _ = jsonio.parse_json(FileSampleSource(tmp_path).observation("K", Side.A).body or b"")  # type: ignore[union-attr]
        assert doc == {"big": Decimal("12345678901234567890.123456789"), "small": Decimal("1e-30")}

    def test_observed_at_default_and_explicit(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        write_jsonl(
            path,
            {"key": "K", "side": "a", "body": 1},
            {"key": "K", "side": "b", "body": 1, "observed_at": "2026-01-02T03:04:05.5Z"},
        )
        os.utime(path, (1_700_000_000, 1_700_000_000))
        source = FileSampleSource(tmp_path)
        assert [source.observation("K", s).observed_at for s in "ab"] == [  # type: ignore[union-attr]
            "2023-11-14T22:13:20.000000Z",
            "2026-01-02T03:04:05.500000Z",
        ]

    @pytest.mark.parametrize(
        ("record", "message"),
        [
            ('{"key": "K", "side": "a"}', "exactly one"),
            ('{"key": "K", "side": "a", "body": 1, "body_text": "1"}', "exactly one"),
            ('{"key": "K", "side": "c", "body": 1}', "side"),
            ('{"key": "", "side": "a", "body": 1}', "key"),
            ('{"key": 5, "side": "a", "body": 1}', "key"),
            ('{"key": "K", "side": "a", "body_text": 5}', "must be a string"),
            ('{"key": "K", "side": "a", "body_b64": "***"}', "base64"),
            ('{"key": "K", "side": "a", "body": 1, "extra": 1}', "unknown fields"),
            ('{"key": "K", "side": "a", "body": {"x": 1, "x": 2}}', "duplicate"),
            ("[1, 2]", "JSON object"),
            ("{not json", "not valid JSON"),
        ],
    )
    def test_invalid_records(self, tmp_path: Path, record: str, message: str) -> None:
        write_jsonl(tmp_path / "s.jsonl", record)
        with pytest.raises(SampleError, match=message) as info:
            FileSampleSource(tmp_path)
        assert "s.jsonl line 1" in str(info.value)

    def test_duplicate_key_side_across_files_and_layouts(self, tmp_path: Path) -> None:
        write_jsonl(tmp_path / "one.jsonl", {"key": "K", "side": "a", "body": 1})
        write_jsonl(tmp_path / "two.jsonl", {"key": "K", "side": "a", "body": 2})
        with pytest.raises(SampleError, match="second sample"):
            FileSampleSource(tmp_path)
        (tmp_path / "two.jsonl").unlink()
        SampleWriter(tmp_path).write("K", Side.A, b"3")
        with pytest.raises(SampleError, match="second sample"):
            FileSampleSource(tmp_path)

    def test_layouts_combine(self, tmp_path: Path) -> None:
        SampleWriter(tmp_path).write("K1", Side.A, b"{}")
        write_jsonl(
            tmp_path / "s.jsonl",
            {"key": "K1", "side": "b", "body": {}},
            {"key": "K2", "side": "a", "body": {}},
        )
        source = FileSampleSource(tmp_path)
        assert source.keys() == ["K1", "K2"] and source.observation("K2", Side.B) is None


class TestFileConnector:
    async def test_fetch(self, tmp_path: Path) -> None:
        SampleWriter(tmp_path).write("K1", Side.A, b'{"a": 1}')
        connector = FileConnector(FileSampleSource(tmp_path))
        assert isinstance(connector, Connector)
        obs = await connector.fetch("K1", side_config(), run_id="r", call_seq=3)
        assert (obs.body, obs.source) == (b'{"a": 1}', "file")
        await connector.aclose()

    async def test_not_captured(self, tmp_path: Path) -> None:
        SampleWriter(tmp_path).write("K1", Side.A, b"{}")
        connector = FileConnector(FileSampleSource(tmp_path))
        with pytest.raises(NotCapturedError):
            await connector.fetch("K1", side_config(side=Side.B))


# Regression tests


class TestCompletenessRoundTrip:
    """A fetched sample must be judged exactly like the live read it came from."""

    @pytest.mark.parametrize(
        ("pages", "eof", "unstable"), [(2, False, False), (3, True, True), (4, True, False), (1, True, False)]
    )
    def test_write_observation_keeps_every_completeness_field(
        self, tmp_path: Path, pages: int, eof: bool, unstable: bool
    ) -> None:
        obs = Observation(
            key="K1",
            side=Side.A,
            http_status=200,
            body=b'{"items": [1, 2], "next": "c2"}',
            observed_at=AT,
            latency_ms=7.5,
            headers={"etag": "w/1"},
            error_detail="max_pages (2) reached before the last page" if not eof else None,
            attempts=3,
            pages=pages,
            pages_eof=eof,
            pagination_unstable=unstable,
        )
        SampleWriter(tmp_path).write_observation(obs)
        loaded = FileSampleSource(tmp_path).observation("K1", Side.A)
        assert loaded == dataclasses.replace(obs, source="file", content_hash=content_hash(obs.body))

    def test_recorded_failure_round_trips(self, tmp_path: Path) -> None:
        obs = Observation(
            "K1", Side.B, 503, b"busy", AT, error_class="http_5xx", error_detail="HTTP 503", attempts=3
        )
        SampleWriter(tmp_path).write_observation(obs)
        loaded = FileSampleSource(tmp_path).observation("K1", Side.B)
        assert loaded is not None and (loaded.error_class, loaded.error_detail, loaded.attempts) == (
            "http_5xx",
            "HTTP 503",
            3,
        )

    def test_meta_written_by_the_writer(self, tmp_path: Path) -> None:
        SampleWriter(tmp_path).write_observation(Observation("K1", Side.A, 200, b"{}", AT, pages=2))
        assert json.loads((tmp_path / "a" / "K1.meta.json").read_text()) == {
            "status": 200,
            "headers": {},
            "observed_at": AT,
            "attempts": 1,
            "pages": 2,
            "pages_eof": True,
            "pagination_unstable": False,
        }

    def test_jsonl_records_carry_the_walk(self, tmp_path: Path) -> None:
        record = {
            "key": "K1",
            "side": "a",
            "body": {},
            "pages": 2,
            "pages_eof": False,
            "pagination_unstable": False,
        }
        write_jsonl(tmp_path / "s.jsonl", record)
        obs = FileSampleSource(tmp_path).observation("K1", Side.A)
        assert obs is not None and (obs.pages, obs.pages_eof) == (2, False)

    @pytest.mark.parametrize(
        ("pagination", "caps", "reason"),
        [
            (dataclasses.replace(CURSOR, max_pages=2), TargetCapabilities(stable_ordering=True), "truncated"),
            (OFFSET, TargetCapabilities(), "unstable_pagination"),
        ],
    )
    async def test_fetched_sample_judged_like_the_live_read(
        self, tmp_path: Path, start: Start, pagination: Pagination, caps: TargetCapabilities, reason: str
    ) -> None:
        items = [1, 2, 3, 4, 5]

        def reply(request: web.Request) -> web.Response:
            if "offset" in request.query:
                begin, size = int(request.query["offset"]), int(request.query["limit"])
                return web.json_response({"items": items[begin : begin + size]})
            begin = int(request.query.get("cursor", "0"))
            nxt = str(begin + 2) if begin + 2 < len(items) else None
            return web.json_response({"items": items[begin : begin + 2], "next": nxt})

        side_cfg = side_config(pagination=pagination)
        live = await (await start(reply, capabilities=caps)).fetch("K1", side_cfg)
        assert judge(live, side_cfg, caps) == (SideStateKind.INCOMPLETE, reason)
        SampleWriter(tmp_path).write_observation(live)
        from_file = await FileConnector(FileSampleSource(tmp_path)).fetch("K1", side_cfg)
        assert judge(from_file, side_cfg, caps) == (SideStateKind.INCOMPLETE, reason)


class TestSampleWithoutRecordedWalk:
    """A shared sample of a paginated side that shows only its first page."""

    async def fetch(
        self, tmp_path: Path, body: bytes, pagination: Pagination | None, meta: dict | None = None
    ):
        hand_made(tmp_path, body, meta, name="K1")
        return await FileConnector(FileSampleSource(tmp_path)).fetch("K1", side_config(pagination=pagination))

    async def test_page_naming_a_next_cursor_is_not_complete(self, tmp_path: Path) -> None:
        obs = await self.fetch(tmp_path, b'{"items": [1, 2], "next": "c2"}', CURSOR)
        assert not obs.pages_eof and "not shown to be the last" in (obs.error_detail or "")
        assert judge(obs, side_config(pagination=CURSOR), TargetCapabilities()) == (
            SideStateKind.INCOMPLETE,
            "truncated",
        )

    @pytest.mark.parametrize(
        ("body", "pagination", "meta", "eof"),
        [
            (b'{"items": [1, 2], "next": null}', CURSOR, None, True),  # the last page
            (
                b'{"items": [1]}',
                LINKS,
                {"headers": {"Link": '</api/things/K1?p=2>; rel="next", </x>; rel=prev'}},
                False,
            ),
            # A merged multi-page body keeps page 1's cursor: the recorded walk decides, not the body.
            (
                b'{"items": [1, 2, 3], "next": "c2"}',
                CURSOR,
                {"pages": 3, "pages_eof": True, "pagination_unstable": False},
                True,
            ),
            (b'{"items": [1, 2], "next": "c2"}', None, None, True),  # not paginated
            (b'{"items": [1, 2], "next": "c2"}', OFFSET, None, False),  # a walk asks for the next window
            (b'{"items": []}', OFFSET, None, True),  # only an empty window ends a walk
            (b'{"next": "c2"}', CURSOR, {"status": 404}, True),  # not a success
        ],
    )
    async def test_when_the_page_decides(
        self, tmp_path: Path, body: bytes, pagination: Pagination | None, meta: dict | None, eof: bool
    ) -> None:
        obs = await self.fetch(tmp_path, body, pagination, meta)
        assert obs.pages_eof is eof
        assert eof is False or obs.error_detail is None


class TestBodyCap:
    """A body above the store's cap is INCOMPLETE(resource_limit), never a crash."""

    def test_layout_one_body_above_cap_is_not_read(self, tmp_path: Path) -> None:
        writer = SampleWriter(tmp_path)
        writer.write("big", Side.A, b"x" * 11)
        writer.write("fits", Side.A, b"x" * 10)
        source = FileSampleSource(tmp_path, max_body_bytes=10)
        big, fits = source.observation("big", Side.A), source.observation("fits", Side.A)
        assert big is not None and fits is not None
        assert (big.error_class, big.body, big.content_hash) == ("resource_limit", None, None)
        assert "10-byte limit" in (big.error_detail or "") and "xxx" not in (big.error_detail or "")
        assert (fits.error_class, fits.body) == (None, b"x" * 10)
        store = evidence_store(tmp_path / "evidence", max_blob_bytes=10)
        assert store.put_blob(big).obs_id is not None  # the store accepts what the source serves

    def test_jsonl_body_that_grows_when_reserialised(self, tmp_path: Path) -> None:
        # 1E+99 is 5 bytes in the record and 100 digits once re-serialised with exact numbers.
        (tmp_path / "s.jsonl").write_text('{"key": "K1", "side": "a", "body": [1E+99]}\n')
        obs = FileSampleSource(tmp_path, max_body_bytes=50).observation("K1", Side.A)
        assert obs is not None and (obs.error_class, obs.body) == ("resource_limit", None)

    def test_recorded_failure_is_kept_when_the_body_is_dropped(self, tmp_path: Path) -> None:
        SampleWriter(tmp_path).write("K1", Side.A, b"x" * 20, status=503, error_class="http_5xx")
        obs = FileSampleSource(tmp_path, max_body_bytes=10).observation("K1", Side.A)
        assert obs is not None and (obs.error_class, obs.body) == ("http_5xx", None)

    async def test_file_connector_serves_it_to_observe(self, tmp_path: Path) -> None:
        SampleWriter(tmp_path).write("K1", Side.A, b"[" + b"1," * 20 + b"1]")
        obs = await FileConnector(FileSampleSource(tmp_path, max_body_bytes=16)).fetch("K1", side_config())
        assert judge(obs, side_config(), TargetCapabilities()) == (SideStateKind.INCOMPLETE, "resource_limit")


class TestWriterNamesAndModes:
    def test_names_differing_only_in_case_collide(self, tmp_path: Path) -> None:
        # Path.exists() is case-insensitive on macOS.
        SampleWriter(tmp_path).write("aB3x", Side.A, b"{}")
        with pytest.raises(SampleError, match="collides"):
            SampleWriter(tmp_path).write("Ab3X", Side.A, b"{}")  # files present before the writer

    def test_orphan_meta_is_not_a_sample(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "K1.meta.json").write_text("{}")  # a crash between meta and body
        SampleWriter(tmp_path).write("K1", Side.A, b"{}")
        assert (tmp_path / "a" / "K1.json").read_bytes() == b"{}"

    def test_sample_directories_are_private(self, tmp_path: Path, umask_022: None) -> None:
        # File names are raw keys, so the directories must not be listable.
        root = tmp_path / "samples"
        (root / "b").mkdir(parents=True, mode=0o755)
        SampleWriter(root).write("jane.doe@example.org", Side.A, b"{}", status=200)
        modes = {path: stat.S_IMODE(path.stat().st_mode) for path in (root, root / "a", root / "b")}
        assert set(modes.values()) == {0o700}  # an existing 0755 tightened
        assert {stat.S_IMODE(p.stat().st_mode) for p in (root / "a").iterdir()} == {0o600}


class TestMessagesNameNoKey:
    """A sample's file name is its percent-encoded key (maybe personal data); messages reach
    terminals and CI logs, so they locate a sample by side and position only."""

    KEY = "jane.doe@example.org"

    def assert_no_key(self, message: str) -> None:
        for form in (self.KEY, encode_key(self.KEY), self.KEY.upper(), encode_key(self.KEY.upper()), "jane"):
            assert form not in message, message

    def test_malformed_meta(self, tmp_path: Path) -> None:
        SampleWriter(tmp_path).write("K0", Side.B, b"{}")
        SampleWriter(tmp_path).write(self.KEY, Side.B, b"{}")
        (tmp_path / "b" / (encode_key(self.KEY) + ".meta.json")).write_text(
            '{"status": 200, "latency_ms": "12ms"}'
        )
        with pytest.raises(SampleError, match="latency_ms") as info:
            FileSampleSource(tmp_path).observation(self.KEY, Side.B)
        assert "b/#" in str(info.value)  # side and position in file-name order
        self.assert_no_key(str(info.value))

    def test_two_names_for_one_key(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / f"{self.KEY}.json").write_bytes(b"1")
        (tmp_path / "a" / f"{encode_key(self.KEY)}.json").write_bytes(b"2")
        with pytest.raises(SampleError, match="same key") as info:
            FileSampleSource(tmp_path)
        self.assert_no_key(str(info.value))

    def test_undecodable_name_and_escaping_symlink(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        (root / "a").mkdir(parents=True)
        (root / "a" / "jane%FF.json").write_bytes(b"{}")
        with pytest.raises(SampleError, match="percent-encoded") as info:
            FileSampleSource(root)
        self.assert_no_key(str(info.value))
        (root / "a" / "jane%FF.json").unlink()
        (tmp_path / "outside.json").write_bytes(b"{}")
        (root / "a" / f"{encode_key(self.KEY)}.json").symlink_to(tmp_path / "outside.json")
        with pytest.raises(SampleError, match="outside the sample root") as info:
            FileSampleSource(root)
        self.assert_no_key(str(info.value))

    def test_case_collision_on_write(self, tmp_path: Path) -> None:
        writer = SampleWriter(tmp_path)
        writer.write(self.KEY, Side.A, b"{}")
        with pytest.raises(SampleError, match="collides") as info:
            writer.write(self.KEY.upper(), Side.A, b"{}")
        self.assert_no_key(str(info.value))
