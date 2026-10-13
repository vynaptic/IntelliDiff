"""Report documents: titled sections of text lines and one table, rendered as Markdown, HTML or PDF.

Builders produce a ``Document`` of plain values and renderers only lay it out, so a new format is
one function. Nothing a payload, a reviewer or a model wrote can become markup: HTML is
autoescaped, Markdown escapes every character that opens syntax, and PDF text is
XML-escaped before ReportLab's paragraph markup sees it. Every output is self-contained.
"""

from __future__ import annotations

import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import jinja2
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import KeepInFrame, KeepTogether, Paragraph, SimpleDocTemplate, Table, TableStyle

_TABLE_STYLE = TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke)])  # fmt: skip
_MD_SPECIAL = re.compile(r"([\\`*\[\]()!|])")
# An underscore between word characters cannot open emphasis (CommonMark), so "json_object" stays.
_MD_UNDERSCORE = re.compile(r"(?<![^\W_])_|_(?![^\W_])")


@dataclass(frozen=True)
class Section:
    title: str
    lines: Sequence[str] = ()
    header: Sequence[str] = ()
    rows: Sequence[Sequence[Any]] = ()


@dataclass(frozen=True)
class Document:
    title: str
    meta: str
    sections: Sequence[Section]
    warning: str | None = None  # e.g. a PARTIAL watermark
    compact: bool = False  # a PDF of one page: small text, scaled down when it is over the page, never split


def text(value: Any) -> str:
    return "" if value is None else str(value)


def md_escape(value: Any) -> str:
    """Text that renders as itself in a Markdown paragraph or table cell, never as syntax."""
    escaped = _MD_UNDERSCORE.sub(r"\\_", _MD_SPECIAL.sub(r"\\\1", text(value)))
    return " ".join(escaped.replace("<", "&lt;").replace(">", "&gt;").split())


@cache
def _env(markdown: bool) -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(Path(__file__).with_name("templates")),
        autoescape=not markdown,  # noqa: S701 - Markdown values go through the md filter instead
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["md"] = md_escape
    return env


def render_markdown(doc: Document) -> str:
    return _env(True).get_template("document.md.j2").render(doc=doc)


def render_html(doc: Document) -> str:
    return _env(False).get_template("document.html.j2").render(doc=doc)


def _pdf_text(value: Any) -> str:
    return escape(text(value)).replace("\n", "<br/>")


def render_pdf(doc: Document) -> bytes:
    styles = getSampleStyleSheet()
    body, cell = styles["BodyText"], styles["BodyText"].clone("cell", fontSize=7, leading=8.5)
    title, heading, margins = styles["Title"], styles["Heading2"], {}
    if doc.compact:  # 7pt lines without space between them, a bold line a section, 24pt margins
        body = cell.clone("line", spaceBefore=0)
        title = title.clone("brief", fontSize=11, leading=13, spaceAfter=2)
        heading = heading.clone("part", fontSize=7, leading=8.5, spaceBefore=4, spaceAfter=0)
        margins = dict.fromkeys(("leftMargin", "rightMargin", "topMargin", "bottomMargin"), 24)
    out = io.BytesIO()
    pdf = SimpleDocTemplate(out, pagesize=landscape(A4), title=doc.title, **margins)
    story = [Paragraph(_pdf_text(doc.title), title), Paragraph(_pdf_text(doc.meta), body)]
    story += [Paragraph(f"<b>{_pdf_text(doc.warning)}</b>", body)] if doc.warning else []
    for number, section in enumerate(doc.sections, 1):
        part = [Paragraph(_pdf_text(f"{number}. {section.title}"), heading)]
        part += [Paragraph(_pdf_text(line), body) for line in section.lines]
        if section.header:
            rows = [[Paragraph(_pdf_text(c), cell) for c in row] for row in (section.header, *section.rows)]
            widths = [pdf.width / len(section.header)] * len(section.header)
            part.append(Table(rows, colWidths=widths, repeatRows=1, style=_TABLE_STYLE))
        # A section that fits on a page is not split across two; a compact document is kept whole below.
        story += part if doc.compact else [KeepTogether(part)]
    if doc.compact:  # content over the page is scaled down, never split
        story = [KeepInFrame(pdf.width, pdf.height, story, mode="shrink")]
    pdf.build(story)
    return out.getvalue()
