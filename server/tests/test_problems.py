"""ADR 0290: every problem the API answers is one its catalogue declares, and the OpenAPI
document describes each by its ``code``."""

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from django.test import override_settings

from iki_server.api.problem_codes import CODES, DEFAULT_CODES
from iki_server.api.problems import Problem, UndeclaredProblem, body

SOURCE = Path(__file__).parents[1] / "src" / "iki_server"

#: Exceptions that carry a problem's code as an argument, and which: the API answers them
#: with that code (entity exports, a component's place).
_CODE_ARGUMENT = {("entities", "Unexportable"): 1, ("components", "Refused"): 0}


def _raised() -> dict[str, set[tuple[int | None, str]]]:
    """Each code the server's source gives a problem, with its status where it says one,
    and where: a `Problem(...)`'s `code`, or its status's default; a refusal's `code`
    attribute; a literal code given to an exception the API answers with it."""
    found: dict[str, set[tuple[int | None, str]]] = {}
    for path in SOURCE.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(), str(path))
        imports_problem = any(
            isinstance(n, ast.ImportFrom)
            and (n.module or "").endswith("api.problems")
            and any(a.name == "Problem" for a in n.names)
            for n in ast.walk(tree)
        ) or path.name in ("problems.py", "preconditions.py")
        where = str(path.relative_to(SOURCE))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", getattr(node.func, "attr", None))
                literal = [a.value if isinstance(a, ast.Constant) else None for a in node.args]
                code = next(
                    (
                        str(k.value.value)
                        for k in node.keywords
                        if k.arg == "code" and isinstance(k.value, ast.Constant)
                    ),
                    None,
                )
                if name == "Problem" and imports_problem:
                    first = literal[0] if literal else None
                    status = first if isinstance(first, int) else None
                    dynamic = any(k.arg == "code" and code is None for k in node.keywords)
                    if code is None and status is not None and not dynamic:
                        code = DEFAULT_CODES.get(status, f"<no default for {status}>")
                    if code is not None:
                        found.setdefault(code, set()).add((status, where))
                elif (
                    (at := _CODE_ARGUMENT.get((path.parts[-2], name or ""))) is not None
                    and len(literal) > at
                    and isinstance(literal[at], str)
                ):
                    found.setdefault(str(literal[at]), set()).add((None, where))
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (
                        isinstance(item, ast.Assign)
                        and [getattr(t, "id", None) for t in item.targets] == ["code"]
                        and isinstance(item.value, ast.Constant)
                        and any(getattr(b, "id", None) == "Refused" for b in node.bases)
                    ):
                        found.setdefault(str(item.value.value), set()).add((409, where))
    return found


def test_every_code_the_server_gives_a_problem_is_declared() -> None:
    raised = _raised()
    assert len(raised) > 50
    undeclared = {code: sorted(map(str, at)) for code, at in raised.items() if code not in CODES}
    assert undeclared == {}
    wrong = {
        code: sorted({s for s, _ in at if s is not None and s != CODES[code].status})
        for code, at in raised.items()
    }
    assert {code: s for code, s in wrong.items() if s} == {}
    # And nothing is declared that nothing gives: a code is declared because it's answered.
    # The status defaults are given by Iki's handlers for whatever answers that status.
    unused = set(CODES) - set(raised) - set(DEFAULT_CODES.values())
    assert unused == set()


def test_a_problem_the_catalogue_doesnt_declare_fails_the_tests_and_is_logged_in_production(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(UndeclaredProblem, match="`no_such_code` isn't a declared problem code"):
        Problem(409, "Nope.", code="no_such_code")
    with pytest.raises(UndeclaredProblem, match="`stale` is declared as 412, not 409"):
        Problem(409, "Old.", code="stale")
    with pytest.raises(UndeclaredProblem, match="`guard_refused` always carries guard"):
        Problem(409, "Refused.", code="guard_refused")
    with pytest.raises(UndeclaredProblem, match="`refused` doesn't declare plan"):
        Problem(409, "Refused.", code="refused", extensions={"plan": {}})
    # `errors` may come with any problem: what was wrong, and where.
    Problem(409, "Refused.", code="refused", errors=[{"pointer": "#/name", "detail": "Taken."}])
    with override_settings(IKI_STRICT_PROBLEMS=False):
        answered = Problem(409, "Nope.", code="no_such_code")
    assert answered.code == "no_such_code"


def _document() -> dict[str, Any]:
    from iki_server.api.v1 import api

    document: dict[str, Any] = json.loads(json.dumps(api.get_openapi_schema()))
    return document


def _resolve(document: dict[str, Any], ref: str) -> dict[str, Any]:
    found: Any = document
    for part in ref.removeprefix("#/").split("/"):
        found = found[part]
    return found  # type: ignore[no-any-return]


def test_the_document_describes_each_problem_by_its_code() -> None:
    document = _document()
    schemas = document["components"]["schemas"]
    problem = schemas["Problem"]
    assert problem["discriminator"]["propertyName"] == "code"
    mapping = problem["discriminator"]["mapping"]
    assert set(mapping) == {c for c, d in CODES.items() if not d.worker}
    for code, ref in mapping.items():
        declared, schema = CODES[code], _resolve(document, ref)
        assert {"$ref": ref} in problem["oneOf"]
        assert code in schema["properties"]["code"]["enum"]
        assert schema["properties"]["status"]["enum"] == [declared.status]
        assert {"type", "title", "status", "code", "detail"} <= set(schema["required"])
        assert set(declared.required) <= set(schema["required"])
        assert set(declared.members) <= set(schema["properties"])
        # One schema per code that carries members; the rest share their status's.
        assert len(schema["properties"]["code"]["enum"]) == 1 or not declared.members
    # Every reference the problems make resolves, so a generator reads them all.
    text = json.dumps({k: v for k, v in schemas.items() if "Problem" in k or "Stale" in k})
    for ref in {part.split('"')[0] for part in text.split('"$ref": "')[1:]}:
        _resolve(document, ref)
    error = schemas["ProblemError"]["properties"]
    assert {"pointer", "parameter", "detail", "rule", "hold", "kind"} == set(error)


def test_each_operation_names_the_problems_its_shape_implies() -> None:
    """401 and 403 when it's signed in, 404 when its path names something, 409 when it
    changes something, 422 when it takes input, 412 and 428 when it takes If-Match: each
    with that status's problems, and `default` any problem."""
    document = _document()
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            responses = operation["responses"]
            at = (method, path)
            assert responses["default"] == {"$ref": "#/components/responses/Problem"}, at
            parameters = operation.get("parameters", [])
            implied = {"401", "403"}
            if any(p["in"] == "path" for p in parameters):
                implied.add("404")
            if method != "get":
                implied.add("409")
            if parameters or "requestBody" in operation:
                implied.add("422")
            if any(p["name"] == "If-Match" and p["in"] == "header" for p in parameters):
                implied |= {"412"} | ({"428"} if method != "get" else set())
            assert implied <= set(responses), at
            for status, answer in responses.items():
                if status[0] not in "45":
                    continue
                schema = answer["content"]["application/problem+json"]["schema"]
                assert schema == {"$ref": f"#/components/schemas/Problem{status}"}, (at, status)
                for ref in _resolve(document, schema["$ref"])["oneOf"]:
                    member = _resolve(document, ref["$ref"])
                    assert member["properties"]["status"]["enum"] == [int(status)], (at, ref)
            if "412" in responses:
                assert "ETag" in responses["412"]["headers"], at


def test_a_stale_change_answers_the_problem_the_document_describes() -> None:
    """The document's `StaleProblem` is what a stale change gets."""
    document = _document()
    schema = document["components"]["schemas"]["StaleProblem"]
    answered = body(Problem(412, "Old.", code="stale", extensions={"stale": []}))
    assert set(answered) <= set(schema["properties"])
    assert set(schema["required"]) <= set(answered)
