"""A table side's row filter (docs/mvp/TABLES.md §9): a small predicate language, parsed into a tree.

Comparisons of a column with literals, IS [NOT] NULL, [NOT] IN, [NOT] BETWEEN, [NOT] [I]LIKE and a
bare boolean column, combined with AND, OR, NOT and parentheses. There are no functions, subqueries
or casts: a connector renders the tree with its own identifier quoting and bound literals, so no
text of the filter ever reaches a statement.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

_TOKEN = re.compile(
    r"""\s*(?:(?P<str>'(?:[^']|'')*')
       |(?P<num>-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)
       |(?P<qid>"(?:[^"]|"")+")
       |(?P<id>[A-Za-z_][A-Za-z0-9_$]*)
       |(?P<op><>|!=|<=|>=|=|<|>|\(|\)|,))""",
    re.VERBOSE,
)
_KEYWORDS = {"and", "or", "not", "is", "null", "in", "between", "like", "ilike", "true", "false"}
OPERATORS = ("=", "<>", "!=", "<", "<=", ">", ">=")


class WhereError(ValueError):
    """The filter is not in the predicate language; the message names the position."""


@dataclass(frozen=True)
class Value:
    kind: str  # "str", "num" or "bool"
    text: str  # the literal's value as text: a string's content, a number as written, true/false


@dataclass(frozen=True)
class Node:
    """``op`` is and, or, not, cmp, null, in, between, like or column; ``column`` for the leaves."""

    op: str
    children: tuple[Node, ...] = ()
    column: str | None = None
    test: str | None = None  # cmp: the operator; like: LIKE or ILIKE
    values: tuple[Value, ...] = ()
    negated: bool = False


def parse(text: str) -> Node:
    """The filter's tree; raises ``WhereError``."""
    tokens: list[tuple[str, str, int]] = []
    at = 0
    while text[at:].strip():
        match = _TOKEN.match(text, at)
        if match is None:
            raise WhereError(f"unexpected text at position {at + len(text[at:]) - len(text[at:].lstrip())}")
        kind = match.lastgroup or ""
        word, start = match.group(kind), match.start(kind)
        if kind == "id" and word.lower() in _KEYWORDS:
            kind, word = "kw", word.lower()
        tokens.append((kind, word, start))
        at = match.end()
    parser = _Parser(tokens)
    node = parser.disjunction()
    if parser.i < len(tokens):
        raise WhereError(f"unexpected {parser._found()}")
    return node


MAX_DEPTH = 100  # nested parentheses and NOTs: deeper would exhaust Python's stack


class _Parser:
    def __init__(self, tokens: list[tuple[str, str, int]]) -> None:
        self.tokens, self.i, self.depth = tokens, 0, 0

    def _peek(self, kind: str, word: str | None = None) -> bool:
        if self.i >= len(self.tokens):
            return False
        k, w, _ = self.tokens[self.i]
        return k == kind and (word is None or w == word)

    def _found(self) -> str:
        if self.i >= len(self.tokens):
            return "the end"
        return f"{self.tokens[self.i][1]!r} at position {self.tokens[self.i][2]}"

    def _take(self, kind: str, word: str | None = None) -> str:
        if not self._peek(kind, word):
            expected = word or {"id": "a column", "op": "an operator"}.get(kind, kind)
            raise WhereError(f"expected {expected}, found {self._found()}")
        self.i += 1
        return self.tokens[self.i - 1][1]

    def _accept(self, kind: str, word: str | None = None) -> bool:
        if self._peek(kind, word):
            self.i += 1
            return True
        return False

    def disjunction(self) -> Node:
        items = [self.conjunction()]
        while self._accept("kw", "or"):
            items.append(self.conjunction())
        return items[0] if len(items) == 1 else Node("or", tuple(items))

    def conjunction(self) -> Node:
        items = [self.term()]
        while self._accept("kw", "and"):
            items.append(self.term())
        return items[0] if len(items) == 1 else Node("and", tuple(items))

    def term(self) -> Node:
        if self._peek("kw", "not") or self._peek("op", "("):
            self.depth += 1
            if self.depth > MAX_DEPTH:
                raise WhereError(f"the filter nests more than {MAX_DEPTH} levels deep")
            try:
                if self._accept("kw", "not"):
                    return Node("not", (self.term(),))
                self._take("op", "(")
                node = self.disjunction()
                self._take("op", ")")
                return node
            finally:
                self.depth -= 1
        return self.comparison()

    def comparison(self) -> Node:
        column = self._column()
        for op in OPERATORS:
            if self._accept("op", op):
                return Node("cmp", column=column, test=op, values=(self._value(),))
        if self._accept("kw", "is"):
            negated = self._accept("kw", "not")
            self._take("kw", "null")
            return Node("null", column=column, negated=negated)
        negated = self._accept("kw", "not")
        if self._accept("kw", "in"):
            self._take("op", "(")
            values = [self._value()]
            while self._accept("op", ","):
                values.append(self._value())
            self._take("op", ")")
            return Node("in", column=column, values=tuple(values), negated=negated)
        if self._accept("kw", "between"):
            low = self._value()
            self._take("kw", "and")
            return Node("between", column=column, values=(low, self._value()), negated=negated)
        for test in ("like", "ilike"):
            if self._accept("kw", test):
                at = self.tokens[self.i][2] if self.i < len(self.tokens) else None
                value = self._value()
                if value.kind != "str":
                    raise WhereError(f"{test.upper()} needs a string pattern at position {at}")
                if (len(value.text) - len(value.text.rstrip("\\"))) % 2:  # PostgreSQL refuses it per row
                    raise WhereError(f"the {test.upper()} pattern at position {at} ends in an escape (\\)")
                return Node("like", column=column, test=test.upper(), values=(value,), negated=negated)
        if negated:
            raise WhereError(f"NOT after a column needs IN, BETWEEN, LIKE or ILIKE, found {self._found()}")
        return Node("column", column=column)  # a boolean column

    def _column(self) -> str:
        if self._peek("qid"):
            return self._take("qid")[1:-1].replace('""', '"')
        return self._take("id")

    def _value(self) -> Value:
        if self._peek("str"):
            return Value("str", self._take("str")[1:-1].replace("''", "'"))
        if self._peek("num"):
            return Value("num", self._take("num"))
        for word in ("true", "false"):
            if self._accept("kw", word):
                return Value("bool", word)
        raise WhereError(f"expected a literal (string, number, TRUE or FALSE), found {self._found()}")


def columns(node: Node) -> set[str]:
    """Every column the filter reads, as spelled in it."""
    found = {node.column} if node.column is not None else set()
    return found.union(*(columns(child) for child in node.children))


def resolve(node: Node, column: Callable[[str], str]) -> Node:
    """The same filter reading ``column(name)`` for every column (the catalogue's spelling)."""
    children = tuple(resolve(child, column) for child in node.children)
    return replace(node, children=children, column=None if node.column is None else column(node.column))


def text(node: Node) -> str:
    """The filter as canonical text: keywords upper-case, every column double-quoted."""

    def quoted(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def literal(value: Value, column: str) -> str:
        if value.kind == "str":
            return "'" + value.text.replace("'", "''") + "'"
        return value.text.upper() if value.kind == "bool" else value.text

    return render(node, quoted, literal, " ".join)


Join = Callable[[list[Any]], Any]


def render(node: Node, column: Callable[[str], Any], literal: Callable[[Value, str], Any], join: Join) -> Any:
    """``node`` written out: ``column`` and ``literal`` give each part (a literal with the column it is
    compared with, so an engine can type it), ``join`` joins a list of parts and keyword strings with
    spaces (text for display, SQL composition for a statement)."""
    not_ = ["NOT"] if node.negated else []

    def operand(child: Node) -> list[Any]:
        # Parentheses only where the parse had them (AND binds tighter than OR, NOT tighter than AND),
        # so the text nests no deeper than the filter as written.
        grouped = child.op in ("and", "or") and not (node.op == "or" and child.op == "and")
        written = render(child, column, literal, join)
        return ["(", written, ")"] if grouped else [written]

    if node.op in ("and", "or"):
        parts: list[Any] = []
        for i, child in enumerate(node.children):
            parts += ([node.op.upper()] if i else []) + operand(child)
        return join(parts)
    if node.op == "not":
        return join(["NOT", *operand(node.children[0])])
    of = node.column or ""
    name = column(of)
    if node.op == "cmp":
        return join([name, node.test, literal(node.values[0], of)])
    if node.op == "null":
        return join([name, "IS", *not_, "NULL"])
    if node.op == "in":
        items: list[Any] = []
        for i, value in enumerate(node.values):
            items += ([","] if i else []) + [literal(value, of)]
        return join([name, *not_, "IN", "(", *items, ")"])
    if node.op == "between":
        return join([name, *not_, "BETWEEN", literal(node.values[0], of), "AND", literal(node.values[1], of)])
    if node.op == "like":
        return join([name, *not_, node.test, literal(node.values[0], of)])
    return name  # a boolean column
