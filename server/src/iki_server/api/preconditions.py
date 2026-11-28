"""Preconditions (ADR 0289): a change names the revision it replaces, in ``If-Match``.

Everything with a revision answers it as its ``ETag``, the number quoted: ``"7"``. A change
that replaces what's recorded sends it back. Without a usable one it's refused with 428
``precondition_required``; with an old one, 412 ``stale``, with the current ``ETag``, and
nothing changes. A change made where nothing is recorded yet sends ``If-None-Match: *``.
"""

from typing import Annotated, Any

from django.http import HttpResponse
from ninja import HeaderEx

from iki_server.api.problems import Problem

#: ``If-Match``: the ETag of the revision a change replaces.
IfMatch = Annotated[
    str | None,
    HeaderEx(
        alias="If-Match",
        description='The ETag of the revision you read, such as `"3"`. Required.',
    ),
]
#: ``If-Match`` on a read, to stay on the revision being read.
ReadIfMatch = Annotated[
    str | None,
    HeaderEx(
        alias="If-Match",
        description="The ETag of the revision you're reading. Optional: a newer one answers 412.",
    ),
]
#: ``If-None-Match: *``: there was nothing recorded when you read it.
IfNoneMatch = Annotated[
    str | None,
    HeaderEx(
        alias="If-None-Match",
        description="`*` when nothing was recorded yet, in place of If-Match.",
    ),
]

#: Never a revision: an ETag Iki didn't give matches nothing.
_NONE = -1


def etag(revision: int) -> str:
    """A revision's ETag: the number, quoted."""
    return f'"{revision}"'


def tagged(response: HttpResponse, revision: int) -> None:
    """Answers with the revision's ETag."""
    response["ETag"] = etag(revision)


def _tags(header: str) -> list[str]:
    return [tag.strip() for tag in header.split(",") if tag.strip()]


def required(detail: str | None = None) -> Problem:
    """428: a change that needs the revision it replaces didn't say which."""
    return Problem(
        428,
        detail
        or "Send If-Match with the one ETag you read, so you can’t overwrite someone else’s "
        "change.",
        code="precondition_required",
    )


def expected(if_match: str | None) -> int:
    """The revision ``If-Match`` names. No header, ``*``, a list or a weak ETag is 428:
    ``*`` would skip the check (ADR 0035), and one change is made from one reading. A strong
    ETag Iki never gave matches no revision, so it's answered as stale."""
    if if_match is None:
        raise required()
    tags = _tags(if_match)
    if len(tags) != 1 or tags[0] == "*" or tags[0].startswith("W/"):
        raise required('If-Match must be the one ETag you read, such as "3".')
    tag = tags[0]
    if len(tag) < 2 or not (tag.startswith('"') and tag.endswith('"')):
        raise required('If-Match must be the one ETag you read, such as "3".')
    number = tag[1:-1]
    return int(number) if number.isdigit() else _NONE


def expected_or_nothing(if_match: str | None, if_none_match: str | None) -> int | None:
    """The revision ``If-Match`` names, or None for ``If-None-Match: *``: the caller saw
    nothing recorded. One of the two is required."""
    if if_none_match is not None:
        if if_match is not None or _tags(if_none_match) != ["*"]:
            raise required(
                "Send If-Match with the ETag you read, or If-None-Match: * if nothing was recorded."
            )
        return None
    if if_match is None:
        raise required(
            "Send If-Match with the ETag you read, or If-None-Match: * if nothing was recorded."
        )
    return expected(if_match)


def stale(
    current: int | None,
    detail: str = "Someone changed it since you read it. Read it again, then make your change.",
    **extensions: Any,  # noqa: ANN401 - members for the problem
) -> Problem:
    """412: a revision the change named isn't the current one, and nothing changed. The
    current ``ETag`` comes with it when there's one thing it names."""
    return Problem(
        412,
        detail,
        code="stale",
        headers={"ETag": etag(current)} if current is not None else None,
        extensions=extensions or None,
    )


def stale_revision(current: int) -> Problem:
    """412 for a record whose revision moved on."""
    return stale(
        current,
        f"Someone changed it since you read it: it’s at revision {current} now. "
        "Read it again, then make your change.",
    )


# --- The OpenAPI document ------------------------------------------------------------------------

_ETAG = {
    "ETag": {
        "description": 'Its revision, quoted: `"7"`. Send it as If-Match to change it.',
        "schema": {"type": "string"},
    }
}


def reads(status: int = 200) -> dict[str, Any]:
    """``openapi_extra`` for a read of something with a revision: its ``ETag``."""
    return {"responses": {status: {"headers": _ETAG}}}


def checked_reads() -> dict[str, Any]:
    """``openapi_extra`` for a read that may send ``If-Match``: its 412."""
    return {
        "responses": {
            412: {
                "description": "`stale`: a newer revision replaced the one you're reading. "
                "Its current ETag comes with it.",
                "headers": _ETAG,
            }
        }
    }


def changes(status: int = 200, *, tagged: bool = True) -> dict[str, Any]:
    """``openapi_extra`` for a change that takes a precondition: 412 and 428, and the
    ``ETag`` of what it answers with, unless it answers with nothing that has one."""
    responses: dict[Any, Any] = {
        412: {
            "description": "`stale`: what you read isn't current. Nothing changed. Its current "
            "ETag comes with it.",
            "headers": _ETAG,
        },
        428: {
            "description": "`precondition_required`: send the ETag you read as If-Match.",
        },
    }
    if tagged and status != 204:
        responses[status] = {"headers": _ETAG}
    return {"responses": responses}


__all__ = [
    "IfMatch",
    "IfNoneMatch",
    "ReadIfMatch",
    "changes",
    "checked_reads",
    "etag",
    "expected",
    "expected_or_nothing",
    "reads",
    "required",
    "stale",
    "stale_revision",
    "tagged",
]
