"""Reply scripts, the app skeleton and the pair registry."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mocks.common import (
    JSON_CONTENT_TYPE,
    Reply,
    ScriptedDataset,
    build_app,
    encode_body,
    not_found_reply,
)
from mocks.registry import PAIRS, load_pair, pair_dir


def _dataset() -> ScriptedDataset:
    script = (Reply(200, {"n": 1}), Reply(200, {"n": 2}))
    return ScriptedDataset(
        pair="toy",
        rng_seed=0,
        keys=("k1", "k2"),
        seeded={"two_step": "k1"},
        scripts={"a": {"k1": script, "k2": (Reply(200, {"n": 0}),)}, "b": {}},
    )


class TestScriptedDataset:
    def test_replies_follow_the_script_and_the_last_repeats(self) -> None:
        ds = _dataset()
        assert [ds.reply("a", "k1", n).body["n"] for n in (1, 2, 3, 10)] == [1, 2, 2, 2]

    def test_call_numbers_start_at_one(self) -> None:
        with pytest.raises(ValueError, match="start at 1"):
            _dataset().reply("a", "k1", 0)

    def test_unknown_key_gets_the_documented_not_found(self) -> None:
        ds = _dataset()
        assert ds.reply("a", "nope", 1) == Reply(404, {"error": "NOT_FOUND"})
        assert ds.reply("b", "k1", 1) == Reply(404, {"error": "NOT_FOUND"})

    def test_not_found_bodies_are_never_shared(self) -> None:
        first = not_found_reply()
        first.body["error"] = "changed"
        assert not_found_reply().body == {"error": "NOT_FOUND"}


class TestEncodeBody:
    def test_compact_utf8_json(self) -> None:
        assert encode_body({"a": [1, 2.5], "b": "é"}) == '{"a":[1,2.5],"b":"é"}'.encode()

    def test_rejects_non_finite_numbers(self) -> None:
        with pytest.raises(ValueError):
            encode_body({"x": float("nan")})


class TestBuildApp:
    def test_rejects_unknown_variant(self) -> None:
        with pytest.raises(ValueError, match="variant"):
            build_app(_dataset(), "c", "/things/{key}")  # type: ignore[arg-type]

    def test_rejects_route_without_key(self) -> None:
        with pytest.raises(ValueError, match="key"):
            build_app(_dataset(), "a", "/things")

    @pytest.mark.parametrize("pair", PAIRS)
    def test_pair_apps_reject_unknown_variant(self, pair: str) -> None:
        with pytest.raises(ValueError, match="variant"):
            load_pair(pair).create_app("c")  # type: ignore[arg-type]


class TestRegistry:
    def test_unknown_pair_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown mock pair"):
            load_pair("nope")
        with pytest.raises(ValueError, match="unknown mock pair"):
            pair_dir("../linker")

    @pytest.mark.parametrize("pair", PAIRS)
    def test_every_pair_ships_its_files(self, pair: str) -> None:
        for name in ("spec.yaml", "targets.yaml", "keys.txt", "expected.json", "__init__.py", "app.py"):
            assert (pair_dir(pair) / name).is_file(), name

    @pytest.mark.parametrize("pair", PAIRS)
    def test_every_pair_module_has_the_protocol(self, pair: str) -> None:
        module = load_pair(pair)
        assert module.build_dataset(module.DEFAULT_SEED).pair == pair


Client = TestClient[Any, Any]


@pytest.fixture
async def toy_client() -> AsyncIterator[Client]:
    client: Client = TestClient(TestServer(build_app(_dataset(), "a", "/things/{key}")))
    await client.start_server()
    yield client
    await client.close()


class TestHousekeepingRoutes:
    async def test_health_names_pair_and_variant(self, toy_client: Client) -> None:
        resp = await toy_client.get("/__health")
        assert resp.status == 200
        assert resp.content_type == JSON_CONTENT_TYPE
        assert await resp.json() == {"status": "ok", "pair": "toy", "variant": "a"}

    async def test_calls_are_counted_per_key_and_reset(self, toy_client: Client) -> None:
        for _ in range(2):
            await toy_client.get("/things/k1")
        await toy_client.get("/things/k2")
        assert (await (await toy_client.get("/__calls")).json()) == {"calls": {"k1": 2, "k2": 1}}
        reset = await toy_client.post("/__reset")
        assert reset.status == 200
        assert (await (await toy_client.get("/__calls")).json()) == {"calls": {}}
        assert (await (await toy_client.get("/things/k1")).json()) == {"n": 1}

    async def test_head_is_not_routed_and_does_not_count(self, toy_client: Client) -> None:
        assert (await toy_client.head("/things/k1")).status == 405
        assert (await (await toy_client.get("/things/k1")).json()) == {"n": 1}

    async def test_only_get_serves_keys(self, toy_client: Client) -> None:
        assert (await toy_client.post("/things/k1")).status == 405
        assert (await toy_client.get("/__reset")).status == 405

    async def test_concurrent_calls_are_numbered_exactly(self, toy_client: Client) -> None:
        responses = await asyncio.gather(*(toy_client.get("/things/k1") for _ in range(8)))
        bodies = [await r.json() for r in responses]
        assert sorted(b["n"] for b in bodies) == [1, 2, 2, 2, 2, 2, 2, 2]
