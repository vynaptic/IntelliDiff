"""The engine seam's registry: every engine declares every member of ``Engine``, so nothing branches on an
engine's name, and the seam and core know the same engines."""

from __future__ import annotations

import inspect

import pytest

from deltasleuth.connectors.tables import ENGINES, Engine, engine_name
from deltasleuth.core import engines as core_engines

# The members of the protocol: what it annotates and the methods it declares.
MEMBERS = sorted(
    {*inspect.get_annotations(Engine)}
    | {name for name, member in vars(Engine).items() if callable(member) and not name.startswith("_")}
)


@pytest.mark.parametrize("name", sorted(ENGINES))
def test_every_engine_declares_every_member(name: str) -> None:
    engine = ENGINES[name]
    declared = {member for klass in type(engine).__mro__[:-1] for member in vars(klass)} | set(vars(engine))
    assert [member for member in MEMBERS if member not in declared] == []
    assert engine.name == name and engine.traits is core_engines.ENGINES[name]


def test_the_seam_and_core_know_the_same_engines() -> None:
    assert list(ENGINES) == list(core_engines.ENGINES)
    # A plan names its engine unless both its sides are of the native engine: one at most is.
    assert [name for name, engine in ENGINES.items() if engine.native] == [engine_name({})]
    assert {"traits", "native", "fixed", "planning", "percent_placeholder"} <= set(MEMBERS)
