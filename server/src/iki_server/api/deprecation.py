"""Deprecated REST operations say so, in the OpenAPI document and in every response
(ADR 0293, as ADR 0016's one-minor-release window needs).

An operation is deprecated by passing ``deprecated=True`` to its router decorator, which
marks it in the OpenAPI document, and decorating it with `deprecated` beneath, which says
when and what replaces it::

    @router.get("/things", deprecated=True, ...)
    @deprecated(since="0.3", on=date(2027, 3, 1), sunset=date(2027, 6, 1), use="GET /items")
    def things(request): ...

Each response then carries ``Deprecation`` (RFC 9745: when it was deprecated) and
``Sunset`` (RFC 8594: when it goes), and a ``Link`` to its replacement's documentation
when there is one. `test_deprecation.py` checks the two markings agree.
"""

from __future__ import annotations

import calendar
import functools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, TypeVar

from django.http import HttpRequest, HttpResponse
from django.utils.http import http_date

__all__ = ["Deprecation", "add_headers", "deprecated", "headers"]

View = TypeVar("View", bound=Callable[..., Any])


@dataclass(frozen=True)
class Deprecation:
    #: The Iki release that deprecated it, such as ``0.3``.
    since: str
    #: The date that release came out: what ``Deprecation`` says.
    on: date
    #: When it goes: one minor release later at the earliest (ADR 0016).
    sunset: date
    #: What to use instead, such as ``GET /api/v1/items``.
    use: str
    #: Where its replacement is documented, if anywhere.
    link: str | None = None

    def __post_init__(self) -> None:
        if self.sunset <= self.on:
            raise ValueError("An operation's sunset comes after it's deprecated.")


def deprecated(
    *, since: str, on: date, sunset: date, use: str, link: str | None = None
) -> Callable[[View], View]:
    info = Deprecation(since=since, on=on, sunset=sunset, use=use, link=link)

    def mark(view: View) -> View:
        @functools.wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - any view's return
            request.iki_deprecation = info  # type: ignore[attr-defined]
            return view(request, *args, **kwargs)

        wrapper.iki_deprecation = info  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return mark


def headers(info: Deprecation) -> dict[str, str]:
    """The response headers a deprecated operation sends."""
    out = {
        # RFC 9745: a structured-field date, seconds since the epoch.
        "Deprecation": f"@{calendar.timegm(info.on.timetuple())}",
        # RFC 8594: an HTTP-date.
        "Sunset": http_date(calendar.timegm(info.sunset.timetuple())),
    }
    if info.link:
        out["Link"] = f'<{info.link}>; rel="deprecation"; type="text/html"'
    return out


def add_headers(request: HttpRequest, response: HttpResponse) -> None:
    info = getattr(request, "iki_deprecation", None)
    if isinstance(info, Deprecation):
        for name, value in headers(info).items():
            if name == "Link" and "Link" in response:
                response["Link"] = f"{response['Link']}, {value}"
            else:
                response[name] = value
