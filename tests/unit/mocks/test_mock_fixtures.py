"""The fixture writer: layout 1's names, meta, recorded replies, byte fidelity and determinism, and the one
pairs file."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

from mocks.common import Reply, ScriptedDataset, encode_body
from mocks.customer.dataset import SEEDED_KEYS as CUSTOMER_KEYS
from mocks.customer.dataset import Seed as CustomerSeed
from mocks.fixtures import (
    is_retryable,
    latency_ms,
    main,
    recorded_reply,
    sample_name,
    write_fixtures,
    write_pairs,
)
from mocks.linker.dataset import SEEDED_KEYS as LINKER_KEYS
from mocks.linker.dataset import Seed as LinkerSeed
from mocks.registry import PAIRS

Serve = Callable[..., Any]


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = {}
    for pair in PAIRS:
        out[pair] = tmp_path_factory.mktemp(pair)
        write_fixtures(pair, out[pair])
    return out


class TestRecordedReply:
    def test_skips_retryable_replies(self) -> None:
        ok = Reply(200, {"ok": True})
        assert recorded_reply((Reply(503, {}), Reply(429, {}), ok)) == ok

    def test_keeps_a_stale_first_answer(self) -> None:
        stale, fresh = Reply(200, {"v": 1}), Reply(200, {"v": 2})
        assert recorded_reply((stale, fresh)) == stale

    def test_not_found_is_final(self) -> None:
        nf = Reply(404, {"error": "NOT_FOUND"})
        assert recorded_reply((nf, Reply(200, {}))) == nf

    def test_all_retryable_records_the_last(self) -> None:
        last = Reply(502, {"n": 2})
        assert recorded_reply((Reply(503, {"n": 1}), last)) == last

    @pytest.mark.parametrize(
        ("status", "retry"), [(200, False), (404, False), (429, True), (500, True), (503, True)]
    )
    def test_retryable_statuses(self, status: int, retry: bool) -> None:
        assert is_retryable(status) is retry


class TestSampleName:
    @pytest.mark.parametrize(
        "key", ["ORG-000007", "C00012003", "a/b", "sp ace", "50%", "ünï", "..", "k.meta"]
    )
    def test_round_trips_as_one_segment(self, key: str) -> None:
        name = sample_name(key)
        assert "/" not in name and unquote(name) == key

    def test_latency_is_stable_and_bounded(self) -> None:
        values = {latency_ms("linker", v, k) for v in ("a", "b") for k in ("x", "y", "z")}
        assert all(5 <= x < 45 for x in values)
        assert latency_ms("linker", "a", "x") == latency_ms("linker", "a", "x")


@pytest.mark.parametrize("pair", PAIRS)
def test_every_key_and_side_has_body_and_meta(
    pair: str, written: dict[str, Path], datasets: dict[str, ScriptedDataset]
) -> None:
    ds = datasets[pair]
    for variant in ("a", "b"):
        side = written[pair] / variant
        names = sorted(p.name for p in side.iterdir())
        assert names == sorted(
            f"{sample_name(k)}{suffix}" for k in ds.keys for suffix in (".json", ".meta.json")
        )
        for index, key in enumerate(ds.keys):
            reply = recorded_reply(ds.script(variant, key))
            body = (side / f"{sample_name(key)}.json").read_bytes()
            meta = json.loads((side / f"{sample_name(key)}.meta.json").read_text())
            assert body == encode_body(reply.body)
            assert set(meta) == {"status", "latency_ms", "observed_at"}
            assert meta["status"] == reply.status
            assert 5 <= meta["latency_ms"] < 45
            stamp = datetime.fromisoformat(meta["observed_at"])
            assert meta["observed_at"].endswith("Z") and stamp.utcoffset() is not None
            if variant == "b":
                a_meta = json.loads((written[pair] / "a" / f"{sample_name(key)}.meta.json").read_text())
                assert datetime.fromisoformat(a_meta["observed_at"]) < stamp, index


def test_drift_key_records_the_stale_answer(
    written: dict[str, Path], datasets: dict[str, ScriptedDataset]
) -> None:
    key = LINKER_KEYS[LinkerSeed.DRIFT]
    stale, current = datasets["linker"].script("b", key)
    body = (written["linker"] / "b" / f"{sample_name(key)}.json").read_bytes()
    assert body == encode_body(stale.body) != encode_body(current.body)


def test_server_error_key_records_the_final_200(written: dict[str, Path]) -> None:
    key = LINKER_KEYS[LinkerSeed.SERVER_ERROR_THEN_SUCCESS]
    meta = json.loads((written["linker"] / "b" / f"{sample_name(key)}.meta.json").read_text())
    body = json.loads((written["linker"] / "b" / f"{sample_name(key)}.json").read_bytes())
    assert meta["status"] == 200 and "data" in body


@pytest.mark.parametrize(
    ("pair", "variant", "key", "body"),
    [
        ("linker", "b", LINKER_KEYS[LinkerSeed.ONE_SIDED_404], {"error": "NOT_FOUND"}),
        ("linker", "a", LINKER_KEYS[LinkerSeed.UNANNOUNCED_404], {"message": "no route"}),
        ("linker", "a", LINKER_KEYS[LinkerSeed.ABSENT_BOTH], {"error": "NOT_FOUND"}),
        ("customer", "b", CUSTOMER_KEYS[CustomerSeed.ONE_SIDED_404], {"error": "NOT_FOUND"}),
    ],
)
def test_not_found_samples_keep_status_and_body(
    written: dict[str, Path], pair: str, variant: str, key: str, body: dict[str, Any]
) -> None:
    side = written[pair] / variant
    assert json.loads((side / f"{sample_name(key)}.meta.json").read_text())["status"] == 404
    assert json.loads((side / f"{sample_name(key)}.json").read_bytes()) == body


@pytest.mark.parametrize("pair", PAIRS)
async def test_samples_are_the_bytes_the_services_serve(
    pair: str, written: dict[str, Path], datasets: dict[str, ScriptedDataset], serve: Serve
) -> None:
    ds = datasets[pair]
    routes = {"linker": ("/entities/{}/links", "/v2/entities/{}/linked-entities"),
              "customer": ("/api/v1/customers/{}", "/v2/customers/{}")}[pair]  # fmt: skip
    for variant, route in zip(("a", "b"), routes, strict=True):
        client = await serve(pair, variant, ds)
        for key in ds.keys:
            script = ds.script(variant, key)
            served = [await (await client.get(route.format(key))).read() for _ in script]
            sample = (written[pair] / variant / f"{sample_name(key)}.json").read_bytes()
            assert served[script.index(recorded_reply(script))] == sample, key


def test_writing_twice_is_byte_identical(tmp_path: Path) -> None:
    first, second = tmp_path / "one", tmp_path / "two"
    assert write_fixtures("linker", first) == write_fixtures("linker", second) == 2 * 61
    for path in sorted(first.rglob("*")):
        if path.is_file():
            assert path.read_bytes() == (second / path.relative_to(first)).read_bytes(), path


def test_seed_option_changes_content_not_layout(tmp_path: Path) -> None:
    write_fixtures("customer", tmp_path / "default")
    write_fixtures("customer", tmp_path / "other", seed=99)
    default = sorted(p.relative_to(tmp_path / "default") for p in (tmp_path / "default").rglob("*.json"))
    other = sorted(p.relative_to(tmp_path / "other") for p in (tmp_path / "other").rglob("*.json"))
    assert default == other
    assert any(
        (tmp_path / "default" / p).read_bytes() != (tmp_path / "other" / p).read_bytes() for p in default
    )


def test_cli_writes_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["customer", "--out", str(tmp_path)])
    assert capsys.readouterr().out.strip() == f"wrote 80 samples for customer to {tmp_path}"
    assert len(list((tmp_path / "a").glob("*.meta.json"))) == 40


def test_cli_rejects_unknown_pair(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["nope", "--out", str(tmp_path)])


@pytest.mark.parametrize("pair", PAIRS)
def test_the_pairs_file_is_one_array_of_what_the_samples_hold(
    pair: str, written: dict[str, Path], datasets: dict[str, ScriptedDataset], tmp_path: Path
) -> None:
    """``--pairs``: one JSON array of {key, a, b}, a line for each key in the sample's order. A side is the
    body its layout-1 sample holds, byte for byte, or null where its recorded status is not 2xx."""
    ds, path = datasets[pair], tmp_path / "made" / "pairs.json"
    assert write_pairs(pair, path) == len(ds.keys)
    first, *lines, last = path.read_bytes().split(b"\n")[:-1]
    assert (first, last) == (b"[", b"]") and path.read_bytes().endswith(b"]\n")
    assert [set(item) for item in json.loads(path.read_bytes())] == [{"key", "a", "b"}] * len(ds.keys)
    absent = 0
    for key, line in zip(ds.keys, lines, strict=True):
        sides = []
        for variant in ("a", "b"):
            sample = written[pair] / variant / sample_name(key)
            found = 200 <= json.loads(Path(f"{sample}.meta.json").read_text())["status"] < 300
            sides.append(Path(f"{sample}.json").read_bytes() if found else b"null")
            absent += not found
        key_text = json.dumps(key, ensure_ascii=False).encode()
        assert line.rstrip(b",") == b'{"key":%b,"a":%b,"b":%b}' % (key_text, *sides), key
    assert absent  # each pair's sample holds a side without a document
    again = tmp_path / "again.json"
    write_pairs(pair, again)
    assert again.read_bytes() == path.read_bytes()


def test_cli_writes_the_pairs_file_the_samples_or_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pairs, out = tmp_path / "customer-pairs.json", tmp_path / "samples"
    main(["customer", "--pairs", str(pairs)])
    assert capsys.readouterr().out.strip() == f"wrote 40 pairs for customer to {pairs}"
    assert len(json.loads(pairs.read_bytes())) == 40 and not out.exists()
    main(["linker", "--out", str(out), "--pairs", str(pairs), "--seed", "7"])
    assert capsys.readouterr().out.splitlines() == [
        f"wrote 122 samples for linker to {out}",
        f"wrote 61 pairs for linker to {pairs}",
    ]
    with pytest.raises(SystemExit) as refused:
        main(["customer"])
    assert refused.value.code == 2
    assert "error: give --out DIR, --pairs FILE or both" in capsys.readouterr().err
