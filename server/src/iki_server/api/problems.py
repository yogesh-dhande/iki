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
from django.conf import settings
from django.http import JsonResponse

from iki_server.api.problem_codes import CODES, DEFAULT_CODES, SHARED_SCHEMAS, Code

CONTENT_TYPE = "application/problem+json"

logger = structlog.get_logger("iki.api")

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
        self.code = code or DEFAULT_CODES.get(status, "")
        self.detail = detail or DEFAULT_DETAILS.get(status, HTTPStatus(status).phrase)
        self.errors = errors
        self.headers = headers or {}
        #: More members for the body (RFC 9457 extensions), for clients to act on.
        self.extensions = extensions or {}
        super().__init__(self.detail)
        _check(self)


class UndeclaredProblem(AssertionError):  # noqa: N818 - it is the problem, as Problem is
    """A problem whose code, status or members the catalogue (ADR 0290) doesn't declare."""


def _wrong(problem: Problem) -> str | None:
    declared = CODES.get(problem.code)
    if declared is None:
        return f"`{problem.code or problem.status}` isn't a declared problem code."
    if declared.status != problem.status:
        return f"`{problem.code}` is declared as {declared.status}, not {problem.status}."
    given = set(problem.extensions) | ({"errors"} if problem.errors else set())
    if unknown := given - set(declared.members) - {"errors"}:
        return f"`{problem.code}` doesn't declare {', '.join(sorted(unknown))}."
    if missing := set(declared.required) - given:
        return f"`{problem.code}` always carries {', '.join(sorted(missing))}."
    return None


def _check(problem: Problem) -> None:
    """Every problem is one the catalogue declares, so the OpenAPI document says what a
    client gets. The tests fail on one it doesn't; production answers it and logs it."""
    wrong = _wrong(problem)
    if wrong is None:
        return
    if getattr(settings, "IKI_STRICT_PROBLEMS", False):
        raise UndeclaredProblem(wrong)
    logger.error("problem.undeclared", code=problem.code, status=problem.status, why=wrong)


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


# --- The OpenAPI document (ADR 0290) -----------------------------------------------------------

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
        "kind": {
            "type": "string",
            "description": "For a sheet's `sheet_problems`: what kind of problem it is.",
        },
    },
}

#: The members every problem has (RFC 9457, ADR 0027).
_BASE: dict[str, Any] = {
    "type": {"type": "string", "description": "`about:blank`."},
    "title": {"type": "string", "description": "The HTTP status's phrase."},
    "status": {"type": "integer", "description": "The HTTP status."},
    "detail": {"type": "string", "description": "What went wrong, for people."},
    "errors": {
        "type": "array",
        "description": "What's wrong, and where: a body field or a parameter.",
        "items": {"$ref": "#/components/schemas/ProblemError"},
    },
    "request_id": {
        "type": "string",
        "description": "The request's ID, which helps an administrator find it.",
    },
}
_REQUIRED = ["type", "title", "status", "code", "detail"]


def _schema_name(code: str) -> str:
    """``scan_ambiguous`` → ``ScanAmbiguousProblem``."""
    return "".join(part.capitalize() for part in code.split("_")) + "Problem"


def _plain_name(status: int) -> str:
    return f"Plain{status}Problem"


def _schemas(codes: dict[str, Code]) -> dict[str, dict[str, Any]]:
    """A schema for each code that carries members, and one for each status's codes that
    carry none: each with every member a problem of it has, so a generated client needs
    no intersection to read one."""
    out: dict[str, dict[str, Any]] = {}
    plain: dict[int, list[str]] = {}
    for code, declared in codes.items():
        if not declared.members:
            plain.setdefault(declared.status, []).append(code)
            continue
        out[_schema_name(code)] = {
            "type": "object",
            "description": f"`{code}` ({declared.status}): {declared.summary}",
            "required": _REQUIRED + [m for m in declared.required if m not in _REQUIRED],
            "properties": {
                **_BASE,
                "status": {**_BASE["status"], "enum": [declared.status]},
                "code": {"type": "string", "enum": [code]},
                **declared.members,
            },
        }
    for status, names in sorted(plain.items()):
        out[_plain_name(status)] = {
            "type": "object",
            "description": f"A {status} problem that carries no more members: "
            + "; ".join(f"`{c}`: {codes[c].summary}" for c in names),
            "required": _REQUIRED,
            "properties": {
                **_BASE,
                "status": {**_BASE["status"], "enum": [status]},
                "code": {"type": "string", "enum": names},
            },
        }
    return out


def _union(codes: dict[str, Code], description: str) -> dict[str, Any]:
    """The problems ``codes`` name, as one schema discriminated by ``code``."""
    names = {
        code: _schema_name(code) if declared.members else _plain_name(declared.status)
        for code, declared in codes.items()
    }
    return {
        "description": description,
        "oneOf": [{"$ref": f"#/components/schemas/{n}"} for n in dict.fromkeys(names.values())],
        "discriminator": {
            "propertyName": "code",
            "mapping": {code: f"#/components/schemas/{n}" for code, n in names.items()},
        },
    }


def _content(schema: dict[str, Any]) -> dict[str, Any]:
    return {CONTENT_TYPE: {"schema": schema}}


#: The statuses an operation answers that its shape implies, and what each means.
_IMPLIED = {
    "401": "Not signed in, or the token isn't one Iki gave.",
    "403": "Signed in, but not allowed to: `code` says why.",
    "404": "There's no such thing, or you can't see it.",
    "409": "Refused by one of Iki's rules, or in conflict with what's recorded.",
    "422": "Some fields or parameters are invalid: `errors` says which, and why.",
}


def documented(schema: dict[str, Any]) -> dict[str, Any]:
    """The OpenAPI document with the problems its operations answer: a schema per code
    that carries members, and per status for the rest; `Problem`, their union; and each
    operation's responses for the statuses its shape implies (401 and 403 when it's
    signed in, 404 when its path names something, 409 when it changes something, 422
    when it takes input), beside the ones it declares, each with the problems of that
    status. `default` is any problem."""
    codes = {c: d for c, d in CODES.items() if not d.worker}
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas.update(ProblemError=ERROR_SCHEMA, **SHARED_SCHEMAS, **_schemas(codes))
    schemas["Problem"] = _union(
        codes,
        "An RFC 9457 problem: how every error is described (ADR 0027). `code` is stable, for "
        "programs, and says which members it carries (ADR 0290); `detail` is for people.",
    )
    by_status: dict[str, dict[str, Code]] = {}
    for code, declared in codes.items():
        by_status.setdefault(str(declared.status), {})[code] = declared
    for status, those in sorted(by_status.items()):
        schemas[f"Problem{status}"] = _union(those, f"A {status} problem: one of these codes.")
    components.setdefault("responses", {})["Problem"] = {
        "description": "An error, as a problem (ADR 0027).",
        "content": _content({"$ref": "#/components/schemas/Problem"}),
    }
    for operations in schema.get("paths", {}).values():
        for method, operation in operations.items():
            if method not in ("get", "put", "post", "delete", "patch"):
                continue
            # Ninja keys them by number until the document is written out, and a
            # `default` beside them must be a string, as they all are in JSON.
            responses = {str(k): v for k, v in operation.get("responses", {}).items()}
            parameters = operation.get("parameters", [])
            implied = {
                "401": bool(operation.get("security")),
                "403": bool(operation.get("security")),
                "404": any(p.get("in") == "path" for p in parameters),
                "409": method != "get",
                "422": bool(parameters) or "requestBody" in operation,
            }
            for status, applies in implied.items():
                if applies:
                    responses.setdefault(status, {"description": _IMPLIED[status]})
            for status, answer in responses.items():
                if status[:1] in ("4", "5") and "content" not in answer:
                    name = f"Problem{status}" if status in by_status else "Problem"
                    answer["content"] = _content({"$ref": f"#/components/schemas/{name}"})
            responses.setdefault("default", {"$ref": "#/components/responses/Problem"})
            operation["responses"] = dict(sorted(responses.items()))
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
