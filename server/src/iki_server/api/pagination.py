"""Cursor pagination for every list.

A page holds ``items`` and ``next_cursor``; pass the cursor back to get the
next page, until it is null. Items come oldest first (IDs are UUIDv7, so
ordering by ID is ordering by creation), which keeps pages stable while
items are added. Inboxes (notifications) come newest first instead, and stay
stable the same way: newer items never push older ones onto the next page.
Cursors are signed, so they are opaque and can't be forged to skip filters.
"""

from typing import Any

from django.core import signing
from django.db.models import Model, QuerySet
from ninja import Field, Schema

from iki_server.api.problems import Problem

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
_SALT = "iki.api.cursor"


class PageParams(Schema):
    cursor: str | None = Field(None, description="From the previous page's next_cursor.")
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Items per page.")


def _encode(pk: Any) -> str:  # noqa: ANN401 - a primary key
    return signing.Signer(salt=_SALT).sign_object(str(pk), compress=False)


def _decode(cursor: str) -> str:
    try:
        return str(signing.Signer(salt=_SALT).unsign_object(cursor))
    except (signing.BadSignature, ValueError) as error:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"parameter": "cursor", "detail": "Not a cursor from this API."}],
        ) from error


def cursor_after(pk: Any) -> str:  # noqa: ANN401 - a primary key
    """A cursor for the page after the item with this key, as ``next_cursor`` would be."""
    return _encode(pk)


def page[M: Model](
    queryset: QuerySet[M], params: PageParams, *, key: str = "pk", newest_first: bool = False
) -> dict[str, Any]:
    """One page of the queryset, ordered by ``key`` (unique and increasing)."""
    queryset = queryset.order_by(f"-{key}" if newest_first else key)
    if params.cursor:
        after = "lt" if newest_first else "gt"
        queryset = queryset.filter(**{f"{key}__{after}": _decode(params.cursor)})
    rows = list(queryset[: params.limit + 1])
    more = len(rows) > params.limit
    rows = rows[: params.limit]
    last = rows[-1] if rows else None
    next_cursor = _encode(_value(last, key)) if more and last is not None else None
    return {"items": rows, "next_cursor": next_cursor}


def _value(obj: Model, key: str) -> Any:  # noqa: ANN401
    for part in key.split("__"):
        obj = getattr(obj, part)
    return obj
