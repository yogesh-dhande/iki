"""Which Python SDK method and which MCP tool make each REST call.

The API reference's "Same call elsewhere" comes from the code that makes the call, not
from a list kept by hand: this reads the SDK's client (``sdk/python/src/iki/client.py``)
and the MCP server's tools (``server/src/iki_server/mcp/tools.py``) and finds each
``request("GET", _path("/entities/{ref}/signatures", ...))`` or
``call("GET", path(...))`` in them. A call is keyed by its method and its path under
``/api/v1``, its parameters' names left out: ``GET /entities/{}/signatures``.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from sitegen.repo import GENERATED, ROOT

SDK = ROOT / "sdk" / "python" / "src" / "iki" / "client.py"
MCP = ROOT / "server" / "src" / "iki_server" / "mcp" / "tools.py"
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def key(method: str, path: str) -> str:
    """``GET /api/v1/entities/{ref}/signatures`` and ``GET /entities/{id}/signatures``
    are one call: ``GET /entities/{}/signatures``."""
    path = re.sub(r"^/api/v\d+(?=/)", "", path)
    path = re.sub(r"\{[^}]*\}", "{}", path)
    return f"{method.upper()} {path}"


def _path_of(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # An f-string path: its fixed parts, each value a parameter.
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        return "".join(parts)
    if isinstance(node, ast.Call) and node.args:
        return _path_of(node.args[0])
    return None


def _calls(function: ast.AST, caller: str) -> list[str]:
    """The REST calls a function makes through ``caller`` (``request`` or ``call``)."""
    found = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", None)
        )
        method = node.args[0]
        if name != caller or not isinstance(method, ast.Constant) or method.value not in METHODS:
            continue
        path = _path_of(node.args[1])
        if path and path.startswith("/"):
            found.append(key(str(method.value), path))
    return found


def sdk_calls(source: str) -> dict[str, list[str]]:
    """Each call, and the SDK methods that make it: ``client.signatures.list``."""
    tree = ast.parse(source)
    parts: dict[str, str] = {}
    for node in ast.walk(tree):
        # self.signatures = Signatures(self)
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "self"
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ):
            parts[node.value.func.id] = node.targets[0].attr
    found: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        owner = (
            "client"
            if node.name == "Client"
            else f"client.{parts[node.name]}"
            if node.name in parts
            else None
        )
        if owner is None:
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and not item.name.startswith("_"):
                for call in _calls(item, "request"):
                    found.setdefault(call, []).append(f"{owner}.{item.name}")
    return found


def mcp_calls(source: str) -> dict[str, list[str]]:
    """Each call, and the MCP tools that make it: ``get_signatures``."""
    found: dict[str, list[str]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        tool = any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            for decorator in node.decorator_list
        )
        if tool:
            for call in _calls(node, "call"):
                found.setdefault(call, []).append(node.name)
    return found


def collect(sdk: Path = SDK, mcp: Path = MCP) -> dict[str, dict[str, list[str]]]:
    calls: dict[str, dict[str, list[str]]] = {}
    for kind, found in (
        ("sdk", sdk_calls(sdk.read_text(encoding="utf-8"))),
        ("mcp", mcp_calls(mcp.read_text(encoding="utf-8"))),
    ):
        for call, names in found.items():
            calls.setdefault(call, {"sdk": [], "mcp": []})[kind] = sorted(set(names))
    return dict(sorted(calls.items()))


def write(directory: Path = GENERATED) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "calls.json"
    path.write_text(json.dumps(collect(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
