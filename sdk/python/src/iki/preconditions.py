"""The headers a change sends to say what it replaces (ADR 0289).

Everything in Iki with a revision answers it as its ETag, the number quoted. A change
sends it back as ``If-Match``; one made where nothing was recorded yet sends
``If-None-Match: *``. The SDK's methods take ``revision=`` and send these for you; use
them when you call the API yourself::

    iki.client().request("PATCH", "/entities/SAM-000012", json=..., headers=if_match(3))
"""

from __future__ import annotations

__all__ = ["if_match", "if_none_match_any"]


def if_match(revision: int) -> dict[str, str]:
    """``If-Match`` for the revision you read."""
    if type(revision) is not int or revision < 1:
        raise ValueError(f"A revision is a positive number, not {revision!r}.")
    return {"If-Match": f'"{revision}"'}


def if_none_match_any() -> dict[str, str]:
    """``If-None-Match: *``, for a change where you read nothing recorded."""
    return {"If-None-Match": "*"}
