"""RFC 9457 problem details: what every API error looks like.

    {
      "type": "about:blank",
      "title": "Unprocessable Content",
      "status": 422,
      "code": "validation_failed",
      "detail": "Some fields are invalid.",
      "errors": [{"pointer": "#/name", "detail": "Field required"}],
      "request_id": "..."
    }

``code`` is stable and meant for programs; ``detail`` is for people.
``errors`` lists invalid body fields as JSON Pointers and invalid query or
path parameters by name.
"""

from http import HTTPStatus
from typing import Any

import structlog
from django.http import JsonResponse

CONTENT_TYPE = "application/problem+json"

#: The code a problem gets when nothing more specific applies.
DEFAULT_CODES = {
    400: "bad_request",
    401: "not_authenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    413: "too_large",
    415: "unsupported_media_type",
    422: "validation_failed",
    429: "rate_limited",
    500: "internal_error",
}

DEFAULT_DETAILS = {
    400: "The request couldn’t be read.",
    401: "Sign in first.",
    403: "You don’t have permission to do that.",
    404: "There’s nothing here, or you can’t see it.",
    405: "This method isn’t supported here.",
    429: "Too many requests. Wait before trying again.",
    500: "Something went wrong on Iki’s side. The request ID helps your administrator find it.",
}


class Problem(Exception):  # noqa: N818 - it is the problem, not an error about one
    """Raise from an endpoint to answer with a problem."""

    def __init__(
        self,
        status: int,
        detail: str | None = None,
        *,
        code: str | None = None,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.code = code or DEFAULT_CODES.get(status, "error")
        self.detail = detail or DEFAULT_DETAILS.get(status, HTTPStatus(status).phrase)
        self.errors = errors
        self.headers = headers or {}
        super().__init__(self.detail)


def body(problem: Problem) -> dict[str, Any]:
    content: dict[str, Any] = {
        "type": "about:blank",
        "title": HTTPStatus(problem.status).phrase,
        "status": problem.status,
        "code": problem.code,
        "detail": problem.detail,
    }
    if problem.errors:
        content["errors"] = problem.errors
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    if request_id:
        content["request_id"] = str(request_id)
    return content


def response(problem: Problem) -> JsonResponse:
    result = JsonResponse(body(problem), status=problem.status, content_type=CONTENT_TYPE)
    for header, value in problem.headers.items():
        result[header] = value
    return result


def from_validation(errors: list[dict[str, Any]]) -> Problem:
    """A 422 problem from django-ninja's validation errors."""
    items = []
    for error in errors:
        loc = [str(part) for part in error.get("loc", ())]
        message = str(error.get("msg", "Invalid value."))
        if error.get("type") == "json_invalid" or (loc[:1] == ["body"] and len(loc) == 1):
            return Problem(400, "The request body isn’t valid JSON.", code="invalid_json")
        if loc and loc[0] == "body":
            # django-ninja nests the body under the parameter's name.
            items.append({"pointer": "#/" + "/".join(loc[2:]), "detail": message})
        else:
            items.append({"parameter": loc[-1] if loc else "", "detail": message})
    return Problem(422, "Some fields are invalid.", errors=items)
