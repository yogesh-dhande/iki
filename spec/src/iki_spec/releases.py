"""Which Iki releases a plugin works with: its ``iki:`` (ADR 0095)."""

from __future__ import annotations

import re

_PART = re.compile(r"\s*(==|!=|>=|<=|>|<)\s*([0-9]+(?:\.[0-9]+){0,2})\s*")
_RELEASE = re.compile(r"[0-9]+(?:\.[0-9]+){0,2}")


def release(version: str) -> tuple[int, int, int]:
    """A version's release numbers: ``0.2`` is (0, 2, 0), and so is ``0.2.0.dev3``.

    A build of Iki counts as the release it leads to, so a plugin made against what's
    coming loads on the builds before it.
    """
    match = _RELEASE.match(version.strip())
    if match is None:
        raise ValueError(f"{version!r} isn't a version")
    major, minor, patch = (*(int(n) for n in match.group(0).split(".")), 0, 0)[:3]
    return major, minor, patch


def iki() -> str:
    """This Iki's release, as plugins compare it: ``0.1.0`` for ``0.1.0.dev0``."""
    from iki_spec import __version__

    return ".".join(map(str, release(__version__)))


def fits(requirement: str, version: str | None = None) -> bool:
    """Whether Iki at ``version`` (this one if None) is a release ``requirement`` allows."""
    have = release(version or iki())
    for part in requirement.split(","):
        match = _PART.fullmatch(part)
        if match is None:
            raise ValueError(f"{requirement!r} isn't a list of comparisons with releases")
        op, want = match.group(1), release(match.group(2))
        ok = {
            "==": have == want,
            "!=": have != want,
            ">=": have >= want,
            "<=": have <= want,
            ">": have > want,
            "<": have < want,
        }[op]
        if not ok:
            return False
    return True


def lowest(requirement: str) -> tuple[int, int, int] | None:
    """The oldest release ``requirement`` allows, or None if it allows none.

    The releases a list of comparisons allows are runs between its bounds, so the oldest
    is the first release, a bound, or the release just after one.
    """
    bounds = []
    for part in requirement.split(","):
        match = _PART.fullmatch(part)
        if match is None:
            raise ValueError(f"{requirement!r} isn't a list of comparisons with releases")
        bounds.append(release(match.group(2)))
    candidates = {(0, 0, 0), *bounds, *((a, b, c + 1) for a, b, c in bounds)}
    allowed = [c for c in candidates if fits(requirement, ".".join(map(str, c)))]
    return min(allowed) if allowed else None


def admits_before(requirement: str, version: str) -> bool:
    """Whether ``requirement`` admits a release earlier than ``version``.

    A plugin whose ``iki:`` does can't use a manifest key ``version`` added (ADR 0293):
    it says it loads on a release that doesn't know the key. Only the lower bounds
    matter: ``>=0.3`` and ``==0.3`` admit nothing before 0.3, ``>0.2`` admits 0.2.1, and
    a range with no lower bound admits 0.0.0.
    """
    want = release(version)
    earliest = (0, 0, 0)
    for part in requirement.split(","):
        match = _PART.fullmatch(part)
        if match is None:
            raise ValueError(f"{requirement!r} isn't a list of comparisons with releases")
        op, bound = match.group(1), release(match.group(2))
        if op in (">=", "=="):
            earliest = max(earliest, bound)
        elif op == ">":
            earliest = max(earliest, (bound[0], bound[1], bound[2] + 1))
    return earliest < want
