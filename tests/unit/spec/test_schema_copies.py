"""The packaged schemas are byte-identical copies of the design documents."""

from __future__ import annotations

from pathlib import Path

import pytest

from deltasleuth.core.spec.schemas import core_schema, pack_major, pack_schema

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("packaged", "source"),
    [
        ("deltasleuth/core/spec/spec.schema.json", "docs/architecture/spec.schema.json"),
        (
            "deltasleuth/packs/entity_linking/config.schema.json",
            "docs/architecture/packs/entity-linking.config.schema.json",
        ),
    ],
)
def test_packaged_schema_is_byte_identical(packaged: str, source: str) -> None:
    assert (REPO_ROOT / packaged).read_bytes() == (REPO_ROOT / source).read_bytes()


def test_schemas_are_loaded_from_the_package() -> None:
    assert core_schema()["$id"] == "https://schemas.deltasleuth.example/comparison-spec/v1/spec.schema.json"
    schema = pack_schema("entity-linking")
    assert schema is not None and pack_major(schema) == 1
    assert pack_schema("no-such-pack") is None
    assert pack_schema("../core") is None  # never a path outside the packs package
