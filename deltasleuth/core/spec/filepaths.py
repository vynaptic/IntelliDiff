"""The grammar of a file side's ``file.path`` and ``file.asOfFile``: pure string checks, shared by the
loader, which refuses a spec, and the files engine, which expands the path under its target's root."""

from __future__ import annotations

import re

PATH_MAX = 512
NAME = re.compile(r"[A-Za-z0-9 ._=+@,()-]+")  # the alphabet of a name: no glob, quote or URL scheme
SEGMENT = re.compile(r"[A-Za-z0-9 _=+@,()*-][A-Za-z0-9 ._=+@,()*-]*")  # a path's: none starts with a dot
ALPHABET = "letters, digits, space and . _ - = + @ , ( )"
PATH_REFUSED = (
    f"file.path is a relative path under the target's root: {ALPHABET} in each segment, none starting with"
    " a dot; * matches inside one segment; a trailing / reads the whole directory"
)


def path_error(path: str) -> str | None:
    """The refusal's text when ``path`` is not a relative path of the closed grammar: segments of the
    alphabet, none starting with a dot; ``*`` inside one segment, or a trailing ``/``; else None."""
    segments = path.split("/")
    if path.endswith("/"):
        segments.pop()
    starred = [segment for segment in segments if "*" in segment]
    if (
        not 0 < len(path) <= PATH_MAX
        or not segments
        or not all(SEGMENT.fullmatch(segment) for segment in segments)
        or len(starred) > 1
        or (starred and path.endswith("/"))
    ):
        return PATH_REFUSED
    return None


def globbed(pattern: str, name: str) -> bool:
    """Whether ``name`` is matched by ``pattern``, whose ``*`` matches any characters of one name."""
    first, *rest = pattern.split("*")
    if not rest:
        return name == first
    last = rest.pop()
    if not name.startswith(first) or not name.endswith(last):
        return False
    at, end = len(first), len(name) - len(last)
    # Each literal is placed at its earliest position. Searching never revisits a consumed prefix;
    # consecutive stars add empty literals, not regex branches or backtracking.
    for literal in rest:
        if literal:
            found = name.find(literal, at, end)
            if found < 0:
                return False
            at = found + len(literal)
    return at <= end


def stamp_error(path: str, name: str) -> bool:
    """Whether ``name`` is no stamp file of ``path``: a name of the alphabet, in the path's fixed
    directory, that the path does not match."""
    if not NAME.fullmatch(name) or name.startswith("."):
        return True
    rest = fixed(path)[1]
    if not rest:  # a directory read: every name but those starting with . or _
        return not name.startswith("_")
    return len(rest) == 1 and globbed(rest[0], name)


def fixed(path: str) -> tuple[list[str], list[str]]:
    """The path's fixed directories, and the segments after them: a file's name, a glob and what follows
    it, or none for a directory read."""
    segments = path.removesuffix("/").split("/")
    if path.endswith("/"):
        return segments, []
    at = next((i for i, segment in enumerate(segments) if "*" in segment), len(segments) - 1)
    return segments[:at], segments[at:]
