"""The one reader of operator-written documents (specs, targets, settings, LLM profiles): YAML or
JSON into plain JSON data, problems raised as ``SpecError`` with a pointer and a line.

YAML follows the 1.2 core schema (``on``/``no`` stay strings, ``017`` is 17, dates stay strings);
only JSON-compatible tags, string keys and no duplicate keys. The alias-expanded size and depth
are bounded before anything walks the document (alias bombs). Items of inline key lists keep the
exact text they were written with (``00012001``, ``1E3``), unless explicitly tagged. A number no
float holds (``1e999``) is refused in both forms: it would read as an infinity, which is no JSON data.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from deltasleuth.core.spec.errors import SpecError, SpecIssue, pointer

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_EXPANDED_NODES = 100_000  # after alias expansion; the largest example spec has about 2,000
MAX_DEPTH = 64
_CONTAINER = ("/.dockerenv", "/run/.containerenv")  # the file Docker, and Podman, put in every container

_TAG = "tag:yaml.org,2002:"
_STR, _INT, _FLOAT, _BOOL, _NULL, _MERGE = (
    _TAG + t for t in ("str", "int", "float", "bool", "null", "merge")
)
_ALLOWED_TAGS = frozenset({_STR, _INT, _FLOAT, _BOOL, _NULL, _MERGE, _TAG + "seq", _TAG + "map"})
_TOO_DEEP = f"the document nests deeper than {MAX_DEPTH} levels"


class _Loader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 core-schema scalars; remembers which scalars carry an explicit tag."""

    yaml_implicit_resolvers: ClassVar[dict[Any, Any]] = {}

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.tagged_scalars: set[int] = set()

    def compose_scalar_node(self, anchor: str | None) -> ScalarNode:
        tagged = self.peek_event().tag not in (None, "!")
        node: ScalarNode = super().compose_scalar_node(anchor)
        if tagged:
            self.tagged_scalars.add(id(node))
        return node

    def construct_core_bool(self, node: ScalarNode) -> bool:
        text = str(self.construct_scalar(node)).lower()
        if text not in ("true", "false"):
            raise ValueError("not a YAML 1.2 boolean")
        return text == "true"

    def construct_core_int(self, node: ScalarNode) -> int:
        text = str(self.construct_scalar(node))
        base = {"0o": 8, "0x": 16}.get(text[:2], 10)
        return int(text[2:] if base != 10 else text, base)

    def construct_core_float(self, node: ScalarNode) -> float:
        return _finite(str(self.construct_scalar(node)), node)


for _tag, _regex, _first in (
    (_BOOL, r"true|True|TRUE|false|False|FALSE", "tTfF"),
    (_INT, r"[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+", "-+0123456789"),
    (_FLOAT, r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?", "-+.0123456789"),
    (_NULL, r"~|null|Null|NULL|", ["~", "n", "N", ""]),
    (_MERGE, r"<<", "<"),
):
    _Loader.add_implicit_resolver(_tag, re.compile(f"^(?:{_regex})$"), list(_first))
_Loader.add_constructor(_BOOL, _Loader.construct_core_bool)
_Loader.add_constructor(_INT, _Loader.construct_core_int)
_Loader.add_constructor(_FLOAT, _Loader.construct_core_float)


def in_container(path: str | os.PathLike[str]) -> str:
    """What the refusal of a ``path`` that is not there adds where the process runs in a container, as
    ``ds`` does under ``docker compose run``: the path was read there, where a file or folder of the host
    is seen only once it is copied or mounted in. Empty outside a container."""
    if not any(os.path.exists(mark) for mark in _CONTAINER):
        return ""
    return (
        f" (ds runs in a container, where this path is {os.path.abspath(path)}: a file or folder of the"
        " host must first be copied in, with docker compose cp, or mounted)"
    )


def read_document(path: Path) -> dict[str, Any]:
    """Read a YAML (default) or JSON (``.json`` suffix) document whose root is a mapping."""
    try:
        size = path.stat().st_size
        if size > MAX_DOCUMENT_BYTES:
            raise _fail("document_too_large", f"document is {size} bytes; the limit is {MAX_DOCUMENT_BYTES}")
        text = path.read_bytes().decode("utf-8-sig")
    except FileNotFoundError as exc:
        raise _fail("document_unreadable", f"cannot read {path}: {exc.strerror}{in_container(path)}") from exc
    except OSError as exc:
        raise _fail("document_unreadable", f"cannot read {path}: {exc.strerror}") from exc
    except UnicodeDecodeError:
        raise _fail("document_unreadable", f"{path} is not UTF-8 text") from None
    data = parse_json_text(text) if path.suffix.lower() == ".json" else parse_yaml_text(text)
    if not isinstance(data, dict):
        raise _fail("document_not_mapping", "the document root must be a mapping")
    return data


def parse_yaml_text(text: str) -> Any:
    try:
        loader = _Loader(text)  # the reader rejects non-printable characters here
        try:
            root = loader.get_single_node()
            if root is None:
                return None
            _check_tree(root)
            for keys in _inline_key_lists(root):
                keys.value = [_as_key_text(item, loader.tagged_scalars) for item in keys.value]
            return loader.construct_document(root)
        finally:
            loader.dispose()
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        problem = "; ".join(part for part in (exc.context, exc.problem) if part) or "invalid YAML"
        raise _fail("yaml_syntax", problem + where) from None
    except yaml.YAMLError as exc:
        raise _fail("yaml_syntax", str(exc).splitlines()[0]) from None
    except RecursionError:
        raise _fail("document_too_deep", _TOO_DEEP) from None
    except ValueError:  # an explicitly tagged scalar that does not parse (e.g. !!int abc)
        raise _fail("yaml_bad_scalar", "a tagged scalar does not match its tag") from None


def parse_json_text(text: str) -> Any:
    """JSON with duplicate-key rejection; numbers stay int/float like the YAML reader's."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise _fail("json_duplicate_key", f"duplicate key {key!r}")
            out[key] = value
        return out

    def no_constant(name: str) -> Any:
        raise _fail("json_syntax", f"{name} is not valid JSON")

    try:
        data = json.loads(text, object_pairs_hook=pairs, parse_constant=no_constant, parse_float=_finite)
    except json.JSONDecodeError as exc:
        raise _fail("json_syntax", f"{exc.msg} (line {exc.lineno}, column {exc.colno})") from None
    except RecursionError:
        raise _fail("document_too_deep", _TOO_DEEP) from None
    _check_json_depth(data, 1)
    return data


def _fail(code: str, message: str, parts: tuple[str | int, ...] = ()) -> SpecError:
    return SpecError([SpecIssue(pointer(parts), message, code)])


def _finite(text: str, node: Node | None = None) -> float:
    """The float ``text`` writes. One it cannot hold is refused: read as an infinity it could be neither
    stored nor sent (JSON has none), and is never quoted."""
    value = float(text)
    if not math.isfinite(value):
        message = "a number is out of range: a document's numbers are finite, about 1.8e308 at most"
        raise _fail("number_out_of_range", _at(node, message) if node else message)
    return value


def _at(node: Node, message: str) -> str:
    return f"{message} (line {node.start_mark.line + 1}, column {node.start_mark.column + 1})"


def _check_tree(root: Node) -> None:
    """Tags, string keys, duplicate keys, alias cycles, and the alias-expanded size and depth.

    Each node is visited once (sizes and heights are memoised), so the cost stays linear in the
    composed graph even when the expanded tree would be astronomically large.
    """
    measured: dict[int, tuple[int, int]] = {}
    active: set[int] = set()

    def visit(node: Node, depth: int, parts: tuple[str | int, ...]) -> tuple[int, int]:
        if id(node) in measured:
            return measured[id(node)]
        if id(node) in active:
            raise _fail("yaml_recursive_alias", _at(node, "an alias refers to its own ancestor"))
        if depth > MAX_DEPTH:
            raise _fail("document_too_deep", _TOO_DEEP)
        if node.tag not in _ALLOWED_TAGS:
            raise _fail("yaml_unsupported_tag", _at(node, f"tag {node.tag!r} is not JSON data"), parts)
        active.add(id(node))
        children = _children(node, parts)
        sizes = [visit(child, depth + 1, at) for child, at in children]
        active.discard(id(node))
        size = 1 + sum(s for s, _ in sizes) + (len(node.value) if isinstance(node, MappingNode) else 0)
        height = 1 + max((h for _, h in sizes), default=0)
        if size > MAX_EXPANDED_NODES:
            message = f"the document expands to more than {MAX_EXPANDED_NODES} nodes (alias expansion)"
            raise _fail("yaml_alias_limit", message)
        if height > MAX_DEPTH:
            raise _fail("document_too_deep", _TOO_DEEP)
        measured[id(node)] = (size, height)
        return size, height

    visit(root, 1, ())


def _children(node: Node, parts: tuple[str | int, ...]) -> list[tuple[Node, tuple[str | int, ...]]]:
    if isinstance(node, SequenceNode):
        return [(child, (*parts, i)) for i, child in enumerate(node.value)]
    if not isinstance(node, MappingNode):
        return []
    names: set[str] = set()
    children: list[tuple[Node, tuple[str | int, ...]]] = []
    for key, value in node.value:
        if key.tag == _MERGE:
            children.append((value, parts))
            continue
        if not isinstance(key, ScalarNode) or key.tag != _STR:
            raise _fail("yaml_non_string_key", _at(key, "mapping keys must be strings"), parts)
        if key.value in names:
            message = _at(key, f"duplicate key {key.value!r}")
            raise _fail("yaml_duplicate_key", message, (*parts, key.value))
        names.add(key.value)
        children.append((value, (*parts, key.value)))
    return children


def _as_key_text(item: Node, tagged: set[int]) -> Node:
    """A plain, untagged scalar as a new string node (so an aliased anchor keeps its type elsewhere)."""
    if not isinstance(item, ScalarNode) or item.style is not None or id(item) in tagged:
        return item
    return ScalarNode(_STR, item.value, item.start_mark, item.end_mark)


def _inline_key_lists(root: Node) -> Iterator[SequenceNode]:
    """``key.source.keys`` and the key lists of the sources nested in ``key.source``."""
    pending = [_member(_member(root, "key"), "source")]
    visited: set[int] = set()
    while pending:
        source = pending.pop()
        if not isinstance(source, MappingNode) or id(source) in visited:
            continue
        visited.add(id(source))
        if isinstance(keys := _member(source, "keys"), SequenceNode):
            yield keys
        pending.append(_member(source, "source"))
        if isinstance(members := _member(source, "sources"), SequenceNode):
            pending.extend(members.value)


def _member(node: Node | None, name: str) -> Node | None:
    """Mapping member ``name`` as the constructor resolves it: explicit members win over ``<<`` merges."""
    if not isinstance(node, MappingNode):
        return None
    merged: list[Node] = []
    for key, value in node.value:
        if key.tag == _MERGE:
            merged.extend(value.value if isinstance(value, SequenceNode) else [value])
        elif isinstance(key, ScalarNode) and key.value == name:
            return value
    for source in merged:
        if (found := _member(source, name)) is not None:
            return found
    return None


def _check_json_depth(value: Any, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise _fail("document_too_deep", _TOO_DEEP)
    for child in value.values() if isinstance(value, dict) else value if isinstance(value, list) else ():
        _check_json_depth(child, depth + 1)
