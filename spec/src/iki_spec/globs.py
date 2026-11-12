"""The glob patterns an action's ``artifacts.keep`` lists (ADR 0068), as both Iki and
``iki-worker`` match them: against a path relative to the run's output directory, one
segment at a time. ``*`` and ``?`` match within a segment, ``[...]`` is a set of
characters, and a ``**`` segment matches any number of segments, none included."""

from __future__ import annotations

from fnmatch import fnmatchcase

__all__ = ["matches", "valid_path"]


def matches(pattern: str, path: str) -> bool:
    return _match(pattern.split("/"), path.split("/"))


def _match(pattern: list[str], path: list[str]) -> bool:
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_match(rest, path[i:]) for i in range(len(path) + 1))
    return bool(path) and _segment(head, path[0]) and _match(rest, path[1:])


def _segment(pattern: str, name: str) -> bool:
    # fnmatch reads "[!x]" as "not x" too; "/" never occurs within a segment.
    return fnmatchcase(name, pattern)


def valid_path(path: str) -> bool:
    """A path a run may keep: relative, with no empty, ``.`` or ``..`` segments, no
    backslashes or control characters, and at most 500 characters."""
    if not path or len(path) > 500 or "\\" in path:
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))
