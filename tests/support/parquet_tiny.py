"""Tiny Parquet files for the shapes DuckDB's writer does not produce: one row group,
one row, required columns, PLAIN-encoded and uncompressed, the footer written by a test-only Thrift compact
encoder. Test-only: no dependency, no reader.

A column is ``(name, physical type, logical type or None, the value's PLAIN bytes)``; ``type_length`` is
the bytes of a ``FIXED_LEN_BYTE_ARRAY``, ``children`` the ``num_children`` its schema element claims (a
leaf has none: only a hostile footer's does), ``statistics`` the deprecated ``min`` and ``max`` of its one
column chunk, which writers before the format's 2.4 computed in signed-byte order, ``encrypted`` a column
chunk that says its column is encrypted with the footer's key.

``laid`` and ``footed`` are what ``write`` writes a file from, for the footers it does not build; ``crafted``
builds the ones a footer walk and Thrift's generated reader once read differently, ``swollen`` one that
DuckDB's reader holds in far more memory than its bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

BOOLEAN, INT32, INT64, INT96, FLOAT, DOUBLE, BYTE_ARRAY, FIXED_LEN_BYTE_ARRAY = range(8)
_I32, _I64, _BINARY, _LIST, _STRUCT, _TRUE, _FALSE = 5, 6, 8, 9, 12, 1, 2
_DECIMAL, _DATE, _TIME, _TIMESTAMP, _NULL = 5, 6, 7, 8, 11  # LogicalType's union members
_UNITS = {"MILLIS": 1, "MICROS": 2, "NANOS": 3}


@dataclass(frozen=True)
class Column:
    name: str
    physical: int
    value: bytes  # PLAIN: little-endian numbers, a length-prefixed byte array, or fixed bytes
    logical: dict[str, Any] | None = (
        None  # {"decimal": (precision, scale)}, {"timestamp" | "time": (utc, unit)}, {"date" | "null": True}
    )
    type_length: int | None = None
    children: int | None = None
    statistics: tuple[bytes, bytes] | None = None  # (min, max)
    encrypted: bool = False


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _zigzag(n: int) -> bytes:
    return _varint((n << 1) ^ (n >> 63))


class _Struct:
    """A Thrift compact struct, its fields written in increasing id order."""

    def __init__(self) -> None:
        self.out, self.last = bytearray(), 0

    def _field(self, fid: int, kind: int) -> None:
        delta = fid - self.last
        self.out += bytes([delta << 4 | kind]) if 0 < delta <= 15 else bytes([kind]) + _zigzag(fid)
        self.last = fid

    def i32(self, fid: int, n: int) -> _Struct:
        self._field(fid, _I32)
        self.out += _zigzag(n)
        return self

    def i64(self, fid: int, n: int) -> _Struct:
        self._field(fid, _I64)
        self.out += _zigzag(n)
        return self

    def binary(self, fid: int, data: bytes) -> _Struct:
        self._field(fid, _BINARY)
        self.out += _varint(len(data)) + data
        return self

    def boolean(self, fid: int, value: bool) -> _Struct:
        self._field(fid, _TRUE if value else _FALSE)
        return self

    def struct(self, fid: int, inner: _Struct) -> _Struct:
        self._field(fid, _STRUCT)
        self.out += inner.end()
        return self

    def items(self, fid: int, kind: int, items: list[Any], named: int | None = None) -> _Struct:
        """A list of ``kind``; ``named``: the element type its header names instead (a hostile footer's)."""
        self._field(fid, _LIST)
        size, named = len(items), kind if named is None else named
        self.out += bytes([size << 4 | named]) if size < 15 else bytes([0xF0 | named]) + _varint(size)
        for item in items:
            if kind == _STRUCT:
                self.out += item.end()
            elif kind == _BINARY:
                self.out += _varint(len(item)) + item
            else:
                self.out += _zigzag(item)
        return self

    def end(self) -> bytes:
        return bytes(self.out) + b"\x00"


def _logical(found: dict[str, Any]) -> _Struct:
    if "decimal" in found:
        precision, scale = found["decimal"]
        return _Struct().struct(_DECIMAL, _Struct().i32(1, scale).i32(2, precision))
    if "date" in found or "null" in found:
        return _Struct().struct(_DATE if "date" in found else _NULL, _Struct())
    member = _TIME if "time" in found else _TIMESTAMP
    utc, unit = found["time" if "time" in found else "timestamp"]
    return _Struct().struct(
        member, _Struct().boolean(1, utc).struct(2, _Struct().struct(_UNITS[unit], _Struct()))
    )


def laid(columns: list[Column], *, groups: int = 0) -> tuple[bytes, list[_Struct], _Struct]:
    """A file of one row holding each column's value, before its footer: its body, its schema's elements
    and its one row group; with ``groups``, its columns lie that many groups deep (``g.g. ... .name``), each
    group holding the next."""
    body, chunks = bytearray(b"PAR1"), []
    for column in columns:
        header = _Struct().i32(1, 0).i32(2, len(column.value)).i32(3, len(column.value))
        header.struct(5, _Struct().i32(1, 1).i32(2, 0).i32(3, 3).i32(4, 3))  # 1 value, PLAIN, RLE levels
        offset = len(body)
        body += header.end() + column.value
        size = len(body) - offset
        path = [b"g"] * groups + [column.name.encode()]
        meta = (
            _Struct().i32(1, column.physical).items(2, _I32, [0]).items(3, _BINARY, path)
            .i32(4, 0).i64(5, 1).i64(6, size).i64(7, size).i64(9, offset)
        )  # fmt: skip
        if column.statistics is not None:  # Statistics: 1 max, 2 min, the deprecated pair
            meta.struct(12, _Struct().binary(1, column.statistics[1]).binary(2, column.statistics[0]))
        chunks.append(_Struct().i64(2, offset).struct(3, meta))
        if column.encrypted:  # ColumnCryptoMetaData: ENCRYPTION_WITH_FOOTER_KEY
            chunks[-1].struct(8, _Struct().struct(1, _Struct()))
    schema = [_Struct().binary(4, b"schema").i32(5, 1 if groups else len(columns))]
    schema += [_Struct().i32(3, 0).binary(4, b"g").i32(5, 1) for _ in range(groups)]
    if groups:
        schema[-1] = _Struct().i32(3, 0).binary(4, b"g").i32(5, len(columns))
    for column in columns:
        element = _Struct().i32(1, column.physical)
        if column.type_length is not None:
            element.i32(2, column.type_length)
        element.i32(3, 0).binary(4, column.name.encode())  # REQUIRED
        if column.children is not None:
            element.i32(5, column.children)
        if column.logical == {"utf8": True}:  # the converted type alone, as writers before LogicalType
            element.i32(6, 0)
        elif column.logical is not None and "decimal" in column.logical:
            precision, scale = column.logical["decimal"]
            element.i32(6, 5).i32(7, scale).i32(8, precision)  # converted type DECIMAL
        elif column.logical == {"date": True}:
            element.i32(6, 6)  # converted type DATE: DuckDB reads a date by it
        if column.logical is not None and "utf8" not in column.logical:
            element.struct(10, _logical(column.logical))
        schema.append(element)
    total = len(body) - 4
    return bytes(body), schema, _Struct().items(1, _STRUCT, chunks).i64(2, total).i64(3, 1)


def footed(body: bytes, footer: bytes, magic: bytes = b"PAR1") -> bytes:
    """A file of ``body``, then ``footer``, its length and ``magic``."""
    return body + footer + struct.pack("<I", len(footer)) + magic


def write(columns: list[Column], *, groups: int = 0, created_by: str | None = None) -> bytes:
    """A Parquet file of ``laid``'s row: version 1, its schema, one row, its row group, ``created_by``."""
    body, schema, group = laid(columns, groups=groups)
    footer = _Struct().i32(1, 1).items(2, _STRUCT, schema).i64(3, 1).items(4, _STRUCT, [group])
    if created_by is not None:
        footer.binary(6, created_by.encode())
    return footed(body, footer.end())


def crafted(depth: int) -> dict[str, bytes]:
    """Files of one ``INT64`` column whose footer shows a walk that stops at the first schema, or takes any
    field 5, a flat schema, and Thrift's generated reader one nested ``depth`` groups deep (DuckDB 1.5.5
    followed it by recursion and ended its process from a few thousand): the schema written twice, the deep
    one after the row groups (a reader takes the last); the same under the id 65538, which a reader cuts to
    16 bits, 2; and each group's ``num_children`` as an i32 followed by field 5 again as an i64, 0 (a reader
    takes the i32 alone)."""
    one = [Column("id", INT64, struct.pack("<q", 1))]
    flat = laid(one)[1]
    body, deep, group = laid(one, groups=depth)  # its chunk's path names the groups
    start = _Struct().i32(1, 1).items(2, _STRUCT, flat).i64(3, 1).items(4, _STRUCT, [group])
    listed = bytes(_Struct().items(1, _STRUCT, deep).out[1:])  # the list without its field's header
    cut = bytes(start.out) + bytes([_LIST]) + _zigzag(65538) + listed
    again = [_Struct().binary(4, b"schema").i32(5, 1).i64(5, depth + 1)]
    again += [_Struct().i32(3, 0).binary(4, b"g").i32(5, 1).i64(5, 0) for _ in range(depth)]
    counted = _Struct().i32(1, 1).items(2, _STRUCT, [*again, flat[1]]).i64(3, 1).items(4, _STRUCT, [group])
    return {
        "the schema twice": footed(body, start.items(2, _STRUCT, deep).end()),
        "the schema again under an id a reader cuts": footed(body, cut + b"\x00"),
        "num_children again as an i64": footed(body, counted.end()),
    }


def swollen(chunks: int) -> bytes:
    """A file of one ``INT64`` column whose footer holds a second row group of ``chunks`` column chunks, a
    byte each: DuckDB 1.5.5's reader made each before it read one, 672 MiB for a million of them, outside
    its memory limit, and then refused the file."""
    body, flat, group = laid([Column("id", INT64, struct.pack("<q", 1))])
    start = _Struct().i32(1, 1).items(2, _STRUCT, flat).i64(3, 1)
    second = bytes([0x10 | _LIST, 0xF0 | _STRUCT]) + _varint(chunks) + b"\x00" * chunks + b"\x00"
    groups = bytes([0x10 | _LIST, 0x20 | _STRUCT]) + group.end() + second
    return footed(body, bytes(start.out) + groups + b"\x00")


def decimal(name: str, unscaled: int, precision: int, scale: int) -> Column:
    """A ``DECIMAL(precision, scale)`` as a big-endian two's complement ``FIXED_LEN_BYTE_ARRAY``."""
    width = next(n for n in range(1, 64) if 10**precision <= 2 ** (8 * n - 1))
    return Column(name, FIXED_LEN_BYTE_ARRAY, unscaled.to_bytes(width, "big", signed=True),
                  {"decimal": (precision, scale)}, width)  # fmt: skip


def timestamp(name: str, ticks: int, *, utc: bool, unit: str) -> Column:
    """An ``INT64`` timestamp of ``ticks`` since the epoch in ``unit``, adjusted to UTC or not."""
    return Column(name, INT64, struct.pack("<q", ticks), {"timestamp": (utc, unit)})


def time(name: str, ticks: int, *, utc: bool, unit: str) -> Column:
    """An ``INT64`` time of ``ticks`` since midnight in ``unit`` (``MICROS`` or ``NANOS``)."""
    return Column(name, INT64, struct.pack("<q", ticks), {"time": (utc, unit)})


def int96(name: str, julian_day: int, nanos_of_day: int) -> Column:
    """An un-annotated ``INT96``, the legacy timestamp of Impala and Spark."""
    return Column(name, INT96, struct.pack("<qi", nanos_of_day, julian_day))


def byte_array(name: str, data: bytes, **more: Any) -> Column:
    """An un-annotated ``BYTE_ARRAY``; with ``logical={"utf8": True}``, a string."""
    return Column(name, BYTE_ARRAY, struct.pack("<i", len(data)) + data, **more)
