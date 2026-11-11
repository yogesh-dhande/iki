"""What can go wrong, as exceptions that say what to do about it.

When the API refuses a request, it says why in a problem document (RFC 9457): a
sentence for people, a code for programs, and, for invalid input, which part was
wrong. An :class:`ApiError` carries all of that, and its subclasses let you catch the
cases you'd handle differently::

    try:
        iki.entities.update("PRT-000123", {"name": "ABL1"}, revision=3, reason="Typo")
    except iki.Conflict:
        ...  # someone changed it since revision 3: read it again
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "ApiError",
    "Conflict",
    "FieldError",
    "Forbidden",
    "IkiError",
    "Invalid",
    "NotFound",
    "NotSignedIn",
    "RateLimited",
    "RunFailed",
    "SignInFailed",
    "TimedOut",
]


class IkiError(Exception):
    """Anything the SDK raises on purpose."""


class NotSignedIn(IkiError):
    """There's no sign-in to use, or it has expired: run ``iki.login()``."""


class SignInFailed(IkiError):
    """Signing in didn't finish: the code expired, or the person declined."""


@dataclass(frozen=True)
class FieldError:
    """One thing wrong with what was sent."""

    #: Where, as a JSON Pointer into the request (``#/values/sequence``).
    pointer: str
    detail: str

    def __str__(self) -> str:
        return f"{self.pointer}: {self.detail}"


class ApiError(IkiError):
    """The API refused a request, and said why."""

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        code: str = "",
        errors: tuple[FieldError, ...] = (),
        request_id: str = "",
        problem: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.detail = detail
        #: A stable code for programs, such as ``not_found`` or ``validation_failed``.
        self.code = code
        #: For invalid input: what was wrong, field by field.
        self.errors = errors
        #: Quote it to your administrator: it finds this request in Iki's logs.
        self.request_id = request_id
        #: The whole problem document, as the API sent it.
        self.problem = problem or {}
        super().__init__(self._message())

    def _message(self) -> str:
        lines = [f"{self.detail} ({self.status}{', ' + self.code if self.code else ''})"]
        lines += [f"  {error}" for error in self.errors]
        if self.request_id:
            lines.append(f"  Request ID: {self.request_id}")
        return "\n".join(lines)


class Invalid(ApiError):
    """What was sent isn't valid (422). ``errors`` says what, field by field."""


class NotFound(ApiError):
    """There's no such thing, or you can't see it (404)."""


class Forbidden(ApiError):
    """You can see it, but can't do this: your role, or your sign-in's scopes (403)."""


class Conflict(ApiError):
    """It can't be done as things are (409), or it changed since the revision you
    named (412): read it again, then decide."""


class RateLimited(ApiError):
    """Too many requests (429), even after waiting as the API asked."""


class RunFailed(IkiError):
    """A run you waited for ended without succeeding."""

    def __init__(self, run: dict[str, Any]) -> None:
        self.run = run
        error = run.get("error") or {}
        why = error.get("message") or run.get("status", "ended")
        super().__init__(f"{run.get('hrid', 'The run')} {run.get('status', 'ended')}: {why}")


class TimedOut(IkiError):
    """Waiting took longer than you allowed. The run goes on: wait for it again."""


_BY_STATUS: dict[int, type[ApiError]] = {
    403: Forbidden,
    404: NotFound,
    409: Conflict,
    412: Conflict,
    422: Invalid,
    429: RateLimited,
}


def from_response(status: int, body: Any) -> ApiError:  # noqa: ANN401 - JSON
    """The error for a response the API refused, from its problem document."""
    problem = body if isinstance(body, dict) else {}
    detail = str(problem.get("detail") or problem.get("title") or f"Iki answered {status}.")
    errors = tuple(
        FieldError(pointer=str(e.get("pointer", "#")), detail=str(e.get("detail", "")))
        for e in problem.get("errors") or []
        if isinstance(e, dict)
    )
    kind = _BY_STATUS.get(status, ApiError)
    return kind(
        status,
        detail,
        code=str(problem.get("code") or ""),
        errors=errors,
        request_id=str(problem.get("request_id") or ""),
        problem=problem,
    )
