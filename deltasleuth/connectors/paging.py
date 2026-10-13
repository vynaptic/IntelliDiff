"""Per-key pagination: walk the pages, merge their items into page 1, and judge the walk.

Later items are appended to page 1's array at ``itemsPath`` (exact numbers); one page keeps its
bytes. ``eof`` is False when the walk stopped before the last page; ``unstable`` is True when an
item repeats across pages, or offset/page windows span pages of a target without declared
``stable_ordering``. Only an empty window proves the end (servers cap page sizes silently), so a
short window is followed by one more request, even at ``maxPages``. A walk that does not merge (a hash
sample's key listing, docs/mvp/SAMPLING.md §3) keeps only the page it took last and that page's item
identities, so a repeat is judged against the previous page only; its caller finds the others.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from yarl import URL

from deltasleuth.connectors.base import ConnectorConfigError
from deltasleuth.core import jsonio
from deltasleuth.core.canonical.encoding import canonical_json, sha256_hex
from deltasleuth.core.canonical.errors import PathError
from deltasleuth.core.canonical.paths import compile_path
from deltasleuth.core.types import Pagination

DEFAULT_MAX_PAGES = 1000
_WINDOW_KINDS = ("offset", "page")
_REQUIRED = {
    "cursor": ("items_path", "cursor_path", "cursor_param"),
    "offset": ("items_path", "page_param", "size_param", "size"),
    "page": ("items_path", "page_param", "size_param", "size"),
    "link_header": ("items_path",),
}
_LINK = re.compile(r"<([^>]*)>([^<]*)")  # RFC 8288 link-value: <target> and its parameters
_LINK_REL = re.compile(r';\s*rel\s*=\s*(?:"([^"]*)"|([^\s;,"]+))', re.IGNORECASE)


def _compile(path: str) -> Any:
    try:
        return compile_path(path)
    except PathError as exc:
        raise ConnectorConfigError(f"invalid pagination JSONPath {path!r}: {exc}") from exc


def _select(path: str, doc: Any) -> list[Any]:
    return [match.value for match in _compile(path).finditer(doc)]


def check_pagination(cfg: Pagination | None) -> Pagination | None:
    """``cfg`` validated with ``max_pages`` defaulted, or None for an unpaginated side."""
    if cfg is None or cfg.kind == "none":
        return None
    if cfg.kind not in _REQUIRED:
        raise ConnectorConfigError(f"unknown pagination kind {cfg.kind!r}")
    missing = [name for name in _REQUIRED[cfg.kind] if getattr(cfg, name) in (None, "")]
    if missing:
        raise ConnectorConfigError(f"pagination kind {cfg.kind!r} needs {', '.join(missing)}")
    if (cfg.size is not None and cfg.size < 1) or (cfg.max_pages is not None and cfg.max_pages < 1):
        raise ConnectorConfigError("pagination size and max_pages must be at least 1")
    for path in (cfg.items_path, cfg.cursor_path):
        if path:
            _compile(path)
    return dataclasses.replace(cfg, max_pages=cfg.max_pages or DEFAULT_MAX_PAGES)


def window_params(cfg: Pagination, position: int | None = None) -> tuple[tuple[str, str], ...]:
    """Query parameters selecting an offset/page window (the first one by default)."""
    if cfg.kind not in _WINDOW_KINDS:
        return ()
    if position is None:
        position = 0 if cfg.kind == "offset" else 1
    return ((cfg.page_param or "", str(position)), (cfg.size_param or "", str(cfg.size)))


def next_link(headers: Mapping[str, str]) -> str | None:
    """The target of the recorded ``Link`` header's ``rel="next"`` link, unresolved."""
    for target, params in _LINK.findall(headers.get("link", "")):
        if any("next" in (quoted or bare).lower().split() for quoted, bare in _LINK_REL.findall(params)):
            return target
    return None


def names_next_page(cfg: Pagination, doc: Any, headers: Mapping[str, str]) -> bool:
    """Would a walk fetch another page after this one? Only an empty offset/page window ends one."""
    if cfg.kind == "cursor":
        found = _select(cfg.cursor_path or "", doc)
        return bool(found) and found[0] is not None
    if cfg.kind == "link_header":
        return next_link(headers) is not None
    return _select(cfg.items_path or "", doc) != [[]]


@dataclass(frozen=True)
class NextPage:
    """The next request: replacement query parameters, or an absolute link."""

    params: tuple[tuple[str, str], ...] = ()
    link: URL | None = None


def _identities(items: list[Any]) -> set[str]:
    return {sha256_hex(canonical_json(item)) for item in items}


class PageWalk:
    """The pages of one key. Feed page 1 to ``start`` and later pages to ``add``."""

    def __init__(self, cfg: Pagination, *, stable_ordering: bool, merge: bool = True) -> None:
        self._cfg = cfg
        self._merge = merge
        self._max_pages = cfg.max_pages or DEFAULT_MAX_PAGES
        self._stable = stable_ordering
        self._first = b""
        self._doc: Any = None
        self._items: list[Any] = []  # page 1's array (inside ``_doc``); later items join it
        self._pages = 0  # pages taken, an empty end page included
        self._merged = False
        self._position = 0 if cfg.kind == "offset" else 1
        self._short = False  # the last non-empty window held fewer items than ``size``
        self._end_check = False  # past maxPages: the next page may only confirm the end
        self._seen: set[str] = set()  # identities of the items taken (not merging: the last page's)
        self._steps: set[tuple[str, str]] = set()  # cursors and links followed
        self.next: NextPage | None = None
        self.eof = True
        self.unstable = False
        self.detail: str | None = None
        self.ended_on_empty_page = False
        self.page: list[Any] = []  # the items of the page just taken, none when it was refused

    def start(self, body: bytes, headers: Mapping[str, str], url: URL) -> None:
        """Take page 1; a page that cannot be walked stays a single document."""
        self._first = body
        try:
            doc, duplicate = jsonio.parse_json(body)
        except (jsonio.JSONParseError, jsonio.ResourceLimitError) as exc:
            if not self._merge:  # no observe() judges the pages of a listing
                self.stop(f"page 1 could not be parsed ({type(exc).__name__})")
            return  # observe() classifies the unreadable body
        items = self._items_of(doc)
        if items is None:
            self.stop("pagination items_path does not select one array on the first page; not paged")
            return
        self._doc, self._items, self._pages, self._seen = doc, items, 1, _identities(items)
        self.page = items
        self._advance(doc, items, headers, url)
        if duplicate and (self.next or not self._merge):  # merging would drop the duplicate names
            self.stop("duplicate member names on page 1; later pages not fetched")

    def add(self, body: bytes, headers: Mapping[str, str], url: URL) -> None:
        """Take a later page; a problem stops the walk and is explained in ``detail``."""
        page_no, self.page = self._pages + 1, []
        try:
            doc, duplicate = jsonio.parse_json(body)
        except (jsonio.JSONParseError, jsonio.ResourceLimitError) as exc:
            self.stop(f"page {page_no} could not be parsed ({type(exc).__name__}); paging stopped")
            return
        items = self._items_of(doc)
        if duplicate or items is None:
            problem = "duplicate member names" if duplicate else "no array at items_path"
            self.stop(f"page {page_no}: {problem}; paging stopped")
            return
        if items and self._end_check:
            self.stop(f"max_pages ({self._max_pages}) reached before the last page")
            return
        identities = _identities(items)
        if items and self._cfg.kind in _WINDOW_KINDS and not self._window_advances(page_no, identities):
            return
        self._pages = page_no
        if items:
            window_unstable = self._cfg.kind in _WINDOW_KINDS and not self._stable
            self.unstable |= window_unstable or not identities.isdisjoint(self._seen)
            self.page = items
            if self._merge:
                self._seen |= identities
                self._items.extend(items)
                self._merged = True
            else:
                self._seen = identities
        self._advance(doc, items, headers, url)
        self.ended_on_empty_page = not items and self.next is None and self.eof

    def stop(self, detail: str) -> None:
        self.eof, self.detail, self.next = False, detail, None

    def body(self) -> bytes:
        return jsonio.dumps(self._doc).encode("utf-8") if self._merged else self._first

    def _items_of(self, doc: Any) -> list[Any] | None:
        found = _select(self._cfg.items_path or "", doc)
        return found[0] if len(found) == 1 and isinstance(found[0], list) else None

    def _window_advances(self, page_no: int, identities: set[str]) -> bool:
        """May a non-empty offset/page window join the walk? If not, the walk is stopped."""
        if identities <= self._seen:
            # A server ignoring the paging parameter would repeat itself up to maxPages.
            self.unstable = True
            self.stop(
                f"page {page_no} only repeats items of earlier pages (paging parameter ignored?);"
                " paging stopped"
            )
            return False
        if self._short and self._cfg.kind == "page":
            # The server may multiply page numbers by its own (capped) size: items could be skipped.
            self.stop(
                f"page {page_no - 1} held fewer items than the requested size yet more followed: the server"
                " caps the page size, so page numbers are not proven to cover every item; paging stopped"
                " (set pagination size to the server's maximum)"
            )
            return False
        if self._short:  # offsets advance by the items returned, so they still tile
            self.detail = self.detail or (
                "the server returned fewer items than the requested page size before the last page"
                " (it caps the page size); offsets advanced by the items returned"
            )
        return True

    def _advance(self, doc: Any, items: list[Any], headers: Mapping[str, str], url: URL) -> None:
        step = self._step(doc, items, headers, url)
        if step and self._pages >= self._max_pages:
            if self._cfg.kind in _WINDOW_KINDS and self._short:
                self._end_check = True
            else:
                self.stop(f"max_pages ({self._max_pages}) reached before the last page")
                step = None
        self.next = step

    def _step(self, doc: Any, items: list[Any], headers: Mapping[str, str], url: URL) -> NextPage | None:
        if self._cfg.kind == "cursor":
            found = _select(self._cfg.cursor_path or "", doc)
            cursor = found[0] if found else None
            if cursor is None:
                return None
            if isinstance(cursor, Decimal):
                cursor = jsonio.number_text(cursor)
            if not isinstance(cursor, str):
                self.stop("pagination cursor is not a string or number; paging stopped")
                return None
            return self._once("cursor", cursor, NextPage(params=((self._cfg.cursor_param or "", cursor),)))
        if self._cfg.kind == "link_header":
            target = next_link(headers)
            if target is None:
                return None
            try:
                link = url.join(URL(target))
            except ValueError:
                self.stop("pagination next link is not a valid URL; paging stopped")
                return None
            return self._once("next link", str(link), NextPage(link=link))
        if not items:
            return None
        self._short = len(items) < (self._cfg.size or 0)
        self._position += len(items) if self._cfg.kind == "offset" else 1
        return NextPage(params=window_params(self._cfg, self._position))

    def _once(self, what: str, value: str, step: NextPage) -> NextPage | None:
        """``step``, unless this cursor or link was followed before (a loop)."""
        if (what, value) in self._steps:
            self.unstable = True
            self.stop(f"pagination {what} repeated (loop); paging stopped")
            return None
        self._steps.add((what, value))
        return step
