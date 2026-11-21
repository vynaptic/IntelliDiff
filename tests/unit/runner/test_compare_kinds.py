"""A kind of source is one entry of ``compare.CONNECTORS``: a target of that kind (``Target.kind``) is read
through its connector, and the live reader takes it as it takes any connector, by what it can do, never by
its type."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deltasleuth.connectors.base import BatchConnector, Connector
from deltasleuth.connectors.http import Gates
from deltasleuth.connectors.rest import RestConnector
from deltasleuth.core.spec import compile_spec, load_targets
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.core.types import Observation, Side, SideConfig, Target, utc_iso
from deltasleuth.runner import compare

from .conftest import TARGETS, doc, spec_text


class Queue:
    """A connector of a kind the product does not know: it answers each key with its document."""

    credentials: tuple[str, ...] = ()

    def __init__(self, target: Target) -> None:
        self.target, self.read, self.closed = target, list[str](), False

    async def fetch(
        self, key: str, side_cfg: SideConfig, *, run_id: str = "p0", call_seq: int = 0
    ) -> Observation:
        self.read.append(key)
        body = json.dumps(doc(key)).encode()
        when = utc_iso(datetime.now(UTC))
        return Observation(key=key, side=side_cfg.side, http_status=200, body=body, observed_at=when)

    async def aclose(self) -> None:
        self.closed = True


async def test_a_third_kind_is_selected_by_its_target_s_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "targets.yaml").write_text(TARGETS.format(base="http://127.0.0.1:9", rate=50))
    targets = load_targets(tmp_path / "targets.yaml")
    spec = compile_spec(parse_yaml_text(spec_text()), targets=targets)
    targets.put(replace(targets.get("svc-b"), kind="queue"))  # side b's target, now of the new kind
    made: list[Queue] = []

    def queue(target: Target, execution: Any, gate: Any, **limits: Any) -> Connector:
        made.append(Queue(target))
        return made[-1]

    monkeypatch.setitem(compare.CONNECTORS, "queue", queue)
    monkeypatch.setattr(compare.product, "follow", lambda ctx: None)  # no product list to take in
    reader = compare.LiveReader(SimpleNamespace(targets=targets, gates=Gates()), spec)  # type: ignore[arg-type]
    try:
        assert isinstance(made[0], Connector) and not isinstance(made[0], BatchConnector)
        assert isinstance(reader._connectors[Side.A], RestConnector)  # side a's target is still a service
        # A connector that reads keys one by one: no group, no fixed side, no large rows, nothing refused.
        assert (reader.batches, reader.fixed, reader.large) == (False, frozenset(), False)
        found = await reader.fetch("K001", Side.B, run_id="R-1", call_seq=1)
        assert (found.http_status, json.loads(found.body or b"")) == (200, doc("K001"))
        assert made[0].read == ["K001"] and made[0].target.target_id == "svc-b"
        assert reader.refused({Side.B: found}) is None
        assert await reader.standing({}) == {}
    finally:
        await reader.aclose()
    assert made[0].closed


def test_a_target_s_kind_is_what_its_declaration_says() -> None:
    assert Target("svc", "http://svc.example").kind == "service"
    assert Target("db", "", dsn_ref="file:dsn").kind == "database"
    assert Target("files", "", root="/exports").kind == "files"
    assert replace(Target("db", "", dsn_ref="file:dsn"), kind="queue").kind == "queue"
    assert set(compare.CONNECTORS) == {"service", "database", "files"}
