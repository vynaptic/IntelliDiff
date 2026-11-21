"""Numeric resource failures retain their public classification across connector boundaries."""

from functools import partial

from deltasleuth.connectors.files import FileSampleSource
from deltasleuth.connectors.http import TargetGate
from deltasleuth.connectors.postgres import engine as postgres
from deltasleuth.connectors.tables import compose, render
from deltasleuth.connectors.tables.connector import TableConnector
from deltasleuth.connectors.tables.mysql import PRIMITIVES
from deltasleuth.core import jsonio
from deltasleuth.core.types import ExecutionConfig, Observation, Side, Target


def table_side():
    return {
        "engine": "mysql", "name": "rows", "schema": "db", "keys": ["id"],
        "key_maps": [None], "columns": {"n": "n"}, "digest": ["n"],
        "types": {"n": {"as": "decimal"}}, "classes": {"id": "integer", "n": "text"},
        "declared": {"id": "int", "n": "varchar(100)"}, "where": None,
    }


def test_table_type_map_resource_error_remains_a_resource_observation() -> None:
    target = Target("db", "")
    connector = TableConnector(target, ExecutionConfig(), table_side(), gate=TargetGate.for_target(target))
    observation = partial(Observation, key="k", side=Side.A, observed_at="2026-11-17T00:00:00Z")
    rejected = connector._observed(observation, [('{"n":"1e4001"}', True)])
    good = connector._observed(observation, [('{"n":"1.2300"}', True)])
    assert (rejected.error_class, rejected.body) == ("resource_limit", None)
    assert (good.error_class, good.body) == (None, b'{"n": 1.23}')


async def test_scanned_type_map_resource_markers_are_distinct_from_bad_values() -> None:
    side = table_side()
    digest = compose.digested(PRIMITIVES, side)
    rows = [("1", "1e4001"), ("2", "bad"), ("3", "1.5")]
    found = digest(rows)
    assert found[:2] == [("1", render.RESOURCE_LIMIT), ("2", render.UNCONVERTIBLE)]

    async def batches():
        yield [("1", render.EMPTY, '{"n":"1e4001"}'),
               ("2", render.EMPTY, '{"n":"bad"}'), ("3", render.EMPTY, '{"n":"1.5"}')]

    assert [row async for batch in postgres._digested(side, batches()) for row in batch] == found


def test_capture_envelope_preserves_aggregate_oversize_body_as_resource_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(jsonio, "MAX_NUMBER_BYTES", 5000)
    (tmp_path / "samples.jsonl").write_text(
        '{"key":"big","side":"a","body":[1e3999,1e3999]}\n'
        '{"key":"good","side":"a","body":{"n":1.2300}}\n'
    )
    source = FileSampleSource(tmp_path)
    rejected, good = source.observation("big", Side.A), source.observation("good", Side.A)
    assert rejected is not None and (rejected.error_class, rejected.body) == ("resource_limit", None)
    assert good is not None and (good.error_class, good.body) == (None, b'{"n": 1.23}')
