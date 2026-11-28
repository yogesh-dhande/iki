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
from typing import Any, Literal, NotRequired, TypedDict, cast

from iki.schema import ConsentStatementOut, Problem, ProblemError

__all__ = [
    "ApiError",
    "ClientProblem",
    "Conflict",
    "ConsentRequired",
    "FieldError",
    "Forbidden",
    "IkiError",
    "ImportFailed",
    "Invalid",
    "NotApproved",
    "NotFound",
    "NotSignedIn",
    "PreconditionRequired",
    "RateLimited",
    "RunFailed",
    "SignInFailed",
    "Stale",
    "TimedOut",
    "is_precondition_required",
    "is_stale",
]


class IkiError(Exception):
    """Anything the SDK raises on purpose."""


class NotSignedIn(IkiError):
    """There's no sign-in to use, or it has expired: run ``iki.login()``."""


class SignInFailed(IkiError):
    """Signing in didn't finish: the code expired, or the person declined."""


@dataclass(frozen=True)
class FieldError:
    """One thing wrong with what was sent: a body field, by its JSON Pointer, or a query
    or path parameter, by its name."""

    #: Where in the body, as a JSON Pointer (``#/values/sequence``); None for a parameter.
    pointer: str | None
    detail: str
    #: The plugin's rule it breaks, if it's a rule's.
    rule: str | None = None
    #: The rule holds records for review: saving with a ``hold_reason`` keeps it.
    hold: bool = False
    #: The query or path parameter it's about (``cursor``, ``project``), if it's one's.
    parameter: str | None = None

    def __str__(self) -> str:
        where = f"parameter {self.parameter}" if self.parameter else self.pointer or "#"
        return f"{where}: {self.detail}"


class ClientProblem(TypedDict):
    """What the SDK makes of an answer that isn't a problem, such as a proxy's error page:
    ``unexpected_response``, with the status it came with."""

    type: Literal["about:blank"]
    title: str
    status: int
    detail: str
    code: Literal["unexpected_response"]
    errors: NotRequired[list[ProblemError]]
    request_id: NotRequired[str]


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
        problem: Problem | ClientProblem | None = None,
    ) -> None:
        self.status = status
        self.detail = detail
        #: A stable code for programs, such as ``not_found`` or ``validation_failed``.
        self.code = code
        #: For invalid input: what was wrong, field by field.
        self.errors = errors
        #: Quote it to your administrator: it finds this request in Iki's logs.
        self.request_id = request_id
        #: The whole problem document, as the API sent it (ADR 0290): its ``code`` says
        #: which members it has, so a type checker narrows it by the code::
        #:
        #:     if error.problem["code"] == "scan_ambiguous":
        #:         choose(error.problem["candidates"])
        self.problem: Problem | ClientProblem = problem or ClientProblem(
            type="about:blank", title="", status=status, detail=detail, code="unexpected_response"
        )
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
    named (412, :class:`Stale`): read it again, then decide."""


class Stale(Conflict):
    """It changed since the revision you named (412), and nothing was changed: read it
    again, then decide (ADR 0289). For a change of several things, the problem says which
    changed (``stale``, ``affected_lines``)."""


class PreconditionRequired(ApiError):
    """The change replaces what's recorded, but didn't say which revision it replaces
    (428): pass the ``revision`` you read."""


def is_stale(error: BaseException) -> bool:
    """Whether a change was refused because what it named changed since it was read."""
    return isinstance(error, ApiError) and error.status == 412


def is_precondition_required(error: BaseException) -> bool:
    """Whether a change was refused for not naming the revision it replaces."""
    return isinstance(error, ApiError) and error.status == 428


class ConsentRequired(Conflict):
    """The action sends data to outside APIs (ADR 0130), and you didn't agree to it.
    ``statements`` says what each sends, and where: read them, then start it again with
    ``consent=`` naming each API."""

    @property
    def statements(self) -> list[ConsentStatementOut]:
        problem = self.problem
        return list(problem["consent"]) if problem["code"] == "consent_required" else []

    @property
    def apis(self) -> list[str]:
        """What ``consent=`` names, to agree to every statement."""
        return [s["api"] for s in self.statements]


class NotApproved(Conflict):
    """An outside API the action sends data to isn't approved for the project. An Org
    Admin approves it in Iki's web app; ``approvers`` names some."""

    @property
    def approvers(self) -> list[str]:
        problem = self.problem
        return list(problem["approvers"]) if problem["code"] == "outside_api_not_approved" else []


class RateLimited(ApiError):
    """Too many requests (429), even after waiting as the API asked."""


class RunFailed(IkiError):
    """A run you waited for ended without succeeding."""

    def __init__(self, run: dict[str, Any]) -> None:
        self.run = run
        error = run.get("error") or {}
        why = error.get("message") or run.get("status", "ended")
        super().__init__(f"{run.get('hrid', 'The run')} {run.get('status', 'ended')}: {why}")


class ImportFailed(IkiError):
    """A spreadsheet import that wasn't written: rows with problems, or a run that
    failed. ``problems`` says why, each at its row and column."""

    def __init__(self, imported: dict[str, Any], problems: list[dict[str, Any]]) -> None:
        self.imported = imported
        self.problems = problems
        shown = "; ".join(
            f"row {p.get('row')}: {p.get('detail')}" if p.get("row") else str(p.get("detail"))
            for p in problems[:5]
        )
        more = f" (and {len(problems) - 5} more)" if len(problems) > 5 else ""
        why = imported.get("error") or shown or imported.get("state", "it failed")
        super().__init__(
            f"{imported.get('file', {}).get('name', 'The import')} wasn't imported: {why}{more}"
        )


class TimedOut(IkiError):
    """Waiting took longer than you allowed. The run or file check continues.

    For a file, ``pending`` names the upload or registration Iki is still checking,
    such as ``{"upload": "…"}``: ``iki.files.wait(error.pending)`` waits for it again,
    without sending the file a second time."""

    def __init__(self, message: str, *, pending: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.pending = pending


_BY_STATUS: dict[int, type[ApiError]] = {
    403: Forbidden,
    404: NotFound,
    409: Conflict,
    412: Stale,
    422: Invalid,
    428: PreconditionRequired,
    429: RateLimited,
}

_BY_CODE: dict[str, type[ApiError]] = {
    "consent_required": ConsentRequired,
    "outside_api_not_approved": NotApproved,
}


def from_response(status: int, body: Any) -> ApiError:  # noqa: ANN401 - JSON
    """The error for a response the API refused, from its problem document."""
    problem: dict[str, Any] = body if isinstance(body, dict) else {}
    detail = str(problem.get("detail") or problem.get("title") or f"Iki answered {status}.")
    errors = tuple(
        FieldError(
            pointer=str(e["pointer"]) if e.get("pointer") else None if e.get("parameter") else "#",
            detail=str(e.get("detail", "")),
            rule=str(e["rule"]) if e.get("rule") else None,
            hold=bool(e.get("hold")),
            parameter=str(e["parameter"]) if e.get("parameter") else None,
        )
        for e in problem.get("errors") or []
        if isinstance(e, dict)
    )
    kind = _BY_CODE.get(str(problem.get("code") or ""), _BY_STATUS.get(status, ApiError))
    return kind(
        status,
        detail,
        code=str(problem.get("code") or ""),
        errors=errors,
        request_id=str(problem.get("request_id") or ""),
        problem=cast("Problem", problem) if isinstance(problem.get("code"), str) else None,
    )
