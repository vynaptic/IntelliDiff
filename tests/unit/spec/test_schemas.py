"""The schemas are loaded from the package, by name and never by a path."""

from __future__ import annotations

from deltasleuth.core.spec.schemas import core_schema, pack_major, pack_schema


def test_schemas_are_loaded_from_the_package() -> None:
    assert core_schema()["$id"] == "https://schemas.deltasleuth.example/comparison-spec/v1/spec.schema.json"
    schema = pack_schema("entity-linking")
    assert schema is not None and pack_major(schema) == 1
    assert pack_schema("no-such-pack") is None
    assert pack_schema("../core") is None  # never a path outside the packs package
