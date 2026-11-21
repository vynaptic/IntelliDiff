"""L2's classification: the cell components coded by how many components of
each side they join, oversized components reported and left out, and the union-find under them."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from deltasleuth.runner.graph import _contingency, _Forest


def forest(components: Sequence[Sequence[str]]) -> _Forest:
    """A side's components, component i linking its members through group i."""
    found = _Forest()
    for i, members in enumerate(components):
        for member in members:
            found.add_member(member)
            found.join(("m", member), ("g", str(i)))
    return found


Components = Sequence[Sequence[str]]


def classify(
    a: Components, b: Components, big_a: Sequence[int] = (), big_b: Sequence[int] = ()
) -> tuple[dict[str, int], list[tuple[str, int, int]]]:
    sides = {s: (forest(c), big) for s, c, big in (("a", a, big_a), ("b", b, big_b))}
    explored = {s: (f, {f.root(("g", str(i))) for i in big}) for s, (f, big) in sides.items()}
    rows, counts = _contingency(explored, str)
    return counts, sorted((r["code"], r["count_a"], r["count_b"]) for r in rows)


NONE = {"split": 0, "merge": 0, "rearrange": 0, "oversized": 0}
SPLIT, REARRANGE = ("CLUSTER_SPLIT", 1, 2), ("CLUSTER_REARRANGE", 2, 2)


@pytest.mark.parametrize(
    ("a", "b", "counts", "rows"),
    [
        ([["1", "2"]], [["1", "2"]], NONE, []),
        ([["1", "2"]], [["1"], ["2"]], NONE | {"split": 1}, [SPLIT]),
        ([["1"], ["2"]], [["1", "2"]], NONE | {"merge": 1}, [("CLUSTER_MERGE", 2, 1)]),
        ([["1", "2"], ["3", "4"]], [["1", "3"], ["2", "4"]], NONE | {"rearrange": 1}, [REARRANGE]),
        # a member on one side only is L1's finding: one-to-one cells are no structural difference
        ([["1", "2"]], [["1"]], NONE, []),
        # two separate splits are two rows
        ([["1", "2"], ["3", "4"]], [["1"], ["2"], ["3"], ["4"]], NONE | {"split": 2}, [SPLIT] * 2),
    ],
    ids=["same", "split", "merge", "rearrange", "one-sided member", "two splits"],
)
def test_cell_components_are_coded_by_the_components_they_join(
    a: list[list[str]], b: list[list[str]], counts: dict[str, int], rows: list[tuple[str, int, int]]
) -> None:
    assert classify(a, b) == (counts, rows)


def test_an_oversized_component_is_reported_with_its_size_and_never_classified() -> None:
    counts, rows = classify([["1", "2", "3"]], [["1"], ["2", "3"]], big_a=[0])
    assert counts == NONE | {"oversized": 1}
    assert rows == [("CLUSTER_OVERSIZED", 3, 0)]


def test_a_node_not_reached_weighs_nothing_and_is_not_added() -> None:
    found = forest([["1", "2"]])
    assert found.weight(("m", "9")) == 0
    assert sorted(found.members()) == ["1", "2"]
    assert found.weight(("g", "0")) == 2


def test_a_long_chain_of_components_joins_without_recursion() -> None:
    found, length = _Forest(), 200_000
    for i in range(length):  # member i links groups i and i + 1: one component
        found.add_member(str(i))
        found.join(("m", str(i)), ("g", i))
        found.join(("m", str(i)), ("g", i + 1))
    assert found.weight(("g", 0)) == length
    assert found.add_member("0") is False
