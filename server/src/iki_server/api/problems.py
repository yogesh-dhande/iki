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
        extensions: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.code = code or DEFAULT_CODES.get(status, "error")
        self.detail = detail or DEFAULT_DETAILS.get(status, HTTPStatus(status).phrase)
        self.errors = errors
        self.headers = headers or {}
        #: More members for the body (RFC 9457 extensions), for clients to act on.
        self.extensions = extensions or {}
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
    content.update({k: v for k, v in problem.extensions.items() if k not in content})
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    if request_id:
        content["request_id"] = str(request_id)
    return content


def response(problem: Problem) -> JsonResponse:
    result = JsonResponse(body(problem), status=problem.status, content_type=CONTENT_TYPE)
    for header, value in problem.headers.items():
        result[header] = value
    return result


#: One thing wrong with a request, as the OpenAPI document gives it.
ERROR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One thing wrong with the request: a body field, by its JSON Pointer, "
    "or a query or path parameter, by its name. Each has one of the two.",
    "required": ["detail"],
    "properties": {
        "pointer": {
            "type": "string",
            "description": "Where in the body, as a JSON Pointer: `#/values/sequence`.",
        },
        "parameter": {
            "type": "string",
            "description": "The query or path parameter: `cursor`, `project`.",
        },
        "detail": {"type": "string", "description": "What's wrong there, for people."},
        "rule": {
            "type": "string",
            "description": "The plugin's rule it breaks, if it's a rule's (ADR 0127).",
        },
        "hold": {
            "type": "boolean",
            "description": "The rule holds the record for review rather than refusing it: "
            "saving with a `hold_reason` keeps it.",
        },
    },
}
#: The problem document every error under /api/ is (RFC 9457, ADR 0027).
PROBLEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "An RFC 9457 problem: how every error is described (ADR 0027). `code` is "
    "stable, for programs; `detail` is for people. Some codes carry more members, which "
    "each operation's description names.",
    "required": ["type", "title", "status", "code", "detail"],
    "properties": {
        "type": {"type": "string", "description": "`about:blank`."},
        "title": {"type": "string", "description": "The HTTP status's phrase."},
        "status": {"type": "integer", "description": "The HTTP status."},
        "code": {
            "type": "string",
            "description": "Stable, for programs: `validation_failed`, `refused`, "
            "`insufficient_scope`, …",
        },
        "detail": {"type": "string", "description": "What went wrong, for people."},
        "errors": {
            "type": "array",
            "description": "For `validation_failed` and `hold_reason_required`: what's wrong, "
            "and where.",
            "items": {"$ref": "#/components/schemas/ProblemError"},
        },
        "request_id": {
            "type": "string",
            "description": "The request's ID, which helps an administrator find it.",
        },
    },
    # Members particular codes carry are allowed, as JSON Schema allows any by default;
    # saying so would make every member of a generated client's type an unknown.
}


def documented(schema: dict[str, Any]) -> dict[str, Any]:
    """The OpenAPI document with the problem its operations answer errors with: a
    `Problem` schema, each operation's `default` response, and the content of each error
    response it names, so a client generated from it reads errors as they are."""
    components = schema.setdefault("components", {})
    components.setdefault("schemas", {}).update(Problem=PROBLEM_SCHEMA, ProblemError=ERROR_SCHEMA)
    content = {CONTENT_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}}
    components.setdefault("responses", {})["Problem"] = {
        "description": "An error, as a problem (ADR 0027).",
        "content": content,
    }
    for operations in schema.get("paths", {}).values():
        for method, operation in operations.items():
            if method not in ("get", "put", "post", "delete", "patch"):
                continue
            # Ninja keys them by number until the document is written out, and a
            # `default` beside them must be a string, as they all are in JSON.
            responses = {str(k): v for k, v in operation.get("responses", {}).items()}
            operation["responses"] = responses
            for status, answer in responses.items():
                if status[:1] in ("4", "5") and "content" not in answer:
                    answer["content"] = content
            responses.setdefault("default", {"$ref": "#/components/responses/Problem"})
    return schema


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
