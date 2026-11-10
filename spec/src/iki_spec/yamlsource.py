"""Read manifest YAML strictly, remembering where every value came from.

Manifests are read as YAML 1.2 (the core schema), not PyYAML's default
YAML 1.1. In 1.1, `no`, `on` and `off` silently become booleans, `010` is
octal and `1:30` is sixty-ninety, which turns an enum of country codes or a
field named `on` into nonsense. Here only `true`/`false` are booleans, and
everything that isn't plainly a number, boolean or null is a string.

It also refuses what makes manifests hard to review or unsafe to read:
duplicate keys (YAML keeps the last one silently), aliases and merge keys
(which can expand a small file into a huge one), custom tags, and files
over `MAX_BYTES`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path as FilePath
from typing import Any

import yaml
from yaml.composer import Composer
from yaml.events import AliasEvent
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from iki_spec.problems import Location, Path, Problem

__all__ = ["MAX_BYTES", "Document", "parse_documents", "read_documents"]

#: Largest manifest file Iki reads. Real ones are a few kilobytes.
MAX_BYTES = 1024 * 1024

_STR = "tag:yaml.org,2002:str"
_SEQ = "tag:yaml.org,2002:seq"
_MAP = "tag:yaml.org,2002:map"

# YAML 1.2 core schema (https://yaml.org/spec/1.2.2/#1032-tag-resolution).
_NULL = re.compile(r"^(?:~|null|Null|NULL|)$")
_TRUE = re.compile(r"^(?:true|True|TRUE)$")
_FALSE = re.compile(r"^(?:false|False|FALSE)$")
_INT = re.compile(r"^[-+]?[0-9]+$")
_OCT = re.compile(r"^0o[0-7]+$")
_HEX = re.compile(r"^0x[0-9a-fA-F]+$")
_FLOAT = re.compile(r"^[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?$")
_INF = re.compile(r"^[-+]?\.(?:inf|Inf|INF)$")
_NAN = re.compile(r"^\.(?:nan|NaN|NAN)$")


@dataclass
class Document:
    """One YAML document from a manifest file, with the location of each value and key."""

    file: str
    index: int
    data: Any
    start: Location
    values: dict[Path, Location] = field(default_factory=dict)
    keys: dict[Path, Location] = field(default_factory=dict)

    def locate(self, path: Path, *, key: bool = False) -> Location:
        """Where `path` is, or its nearest ancestor that exists."""
        lookup = self.keys if key else self.values
        for end in range(len(path), -1, -1):
            prefix = path[:end]
            if prefix in lookup:
                return lookup[prefix]
            if key and prefix in self.values:
                return self.values[prefix]
        return self.start


class _Error(Exception):
    def __init__(self, code: str, message: str, node_or_mark: Any, hint: str | None = None) -> None:  # noqa: ANN401 - manifest data is JSON-shaped
        super().__init__(message)
        self.code = code
        self.message = message
        mark = getattr(node_or_mark, "start_mark", node_or_mark)
        self.location = Location(mark.line + 1, mark.column + 1) if mark is not None else None
        self.hint = hint


class _StrictLoader(yaml.SafeLoader):
    """Composes nodes only: every plain scalar stays a string until `_value` reads it."""

    def compose_node(self, parent: Node | None, index: Any) -> Node | None:  # noqa: ANN401 - manifest data is JSON-shaped
        if self.check_event(AliasEvent):
            event = self.peek_event()  # type: ignore[no-untyped-call]
            raise _Error(
                "yaml-alias",
                "Manifests can't use YAML aliases (`*name`).",
                event.start_mark,
                "Write each value out in full, so the manifest reads the same as what Iki sees.",
            )
        return Composer.compose_node(self, parent, index)

    # PyYAML's stubs type `anchor` as a dict; it is the anchor's name.
    def compose_scalar_node(self, anchor: dict[Any, Node]) -> ScalarNode:
        event = self.peek_event()  # type: ignore[no-untyped-call]
        explicit = event.tag not in (None, "!")
        node = Composer.compose_scalar_node(self, anchor)
        if explicit:
            _check_tag(node, _STR)
            node.style = node.style or "!"  # an explicit `!!str`: keep it as text
        return node


# Drop YAML 1.1's implicit types: plain scalars come back tagged as strings.
_StrictLoader.yaml_implicit_resolvers = {}


def read_documents(file: FilePath, name: str) -> tuple[list[Document], list[Problem]]:
    """Read every document in a manifest file. `name` is how problems refer to the file."""
    try:
        size = file.stat().st_size
        if size > MAX_BYTES:
            return [], [
                Problem(
                    "file-too-large",
                    f"This file is {size:,} bytes; manifests can be at most {MAX_BYTES:,}.",
                    file=name,
                    hint="Split it into several files.",
                )
            ]
        raw = file.read_bytes()
    except OSError as error:
        return [], [
            Problem("unreadable-file", f"Iki couldn't read this file: {error.strerror}.", file=name)
        ]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        return [], [
            Problem(
                "not-utf8",
                f"Manifests must be UTF-8; byte {error.start} isn't.",
                file=name,
            )
        ]
    return parse_documents(text, name)


def parse_documents(text: str, name: str) -> tuple[list[Document], list[Problem]]:
    loader = _StrictLoader(text)
    documents: list[Document] = []
    try:
        index = 0
        while loader.check_node():
            node = loader.get_node()
            if node is None:  # pragma: no cover (check_node said there is one)
                break
            document = Document(
                file=name,
                index=index,
                data=None,
                start=Location(node.start_mark.line + 1, node.start_mark.column + 1),
            )
            document.data = _value(node, (), document)
            documents.append(document)
            index += 1
    except _Error as error:
        return documents, [
            Problem(error.code, error.message, file=name, location=error.location, hint=error.hint)
        ]
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark or error.context_mark
        location = Location(mark.line + 1, mark.column + 1) if mark else None
        detail = error.problem or error.context or "invalid YAML"
        return documents, [
            Problem(
                "yaml-syntax",
                f"This isn't valid YAML: {detail}.",
                file=name,
                location=location,
                hint=_syntax_hint(detail),
            )
        ]
    except yaml.YAMLError as error:  # pragma: no cover (reader errors carry no mark)
        return documents, [Problem("yaml-syntax", f"This isn't valid YAML: {error}.", file=name)]
    finally:
        loader.dispose()
    return documents, []


def _syntax_hint(detail: str) -> str | None:
    if "tab" in detail or "'\\t'" in detail:
        return "Indent with spaces; YAML doesn't allow tabs."
    if "mapping values are not allowed" in detail:
        return "A value containing `: ` needs quotes."
    return None


def _value(node: Node, path: Path, document: Document) -> Any:  # noqa: ANN401 - manifest data is JSON-shaped
    document.values[path] = Location(node.start_mark.line + 1, node.start_mark.column + 1)
    if isinstance(node, MappingNode):
        _check_tag(node, _MAP)
        result: dict[str, Any] = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode):
                raise _Error(
                    "yaml-key", "Keys must be plain text, not lists or mappings.", key_node
                )
            _check_tag(key_node, _STR)
            key = str(key_node.value)
            if key == "<<" and key_node.style is None:
                raise _Error(
                    "yaml-merge-key",
                    "Manifests can't use YAML merge keys (`<<`).",
                    key_node,
                    "Write each value out in full.",
                )
            if key in result:
                first = document.keys[(*path, key)]
                raise _Error(
                    "yaml-duplicate-key",
                    f"`{key}` appears twice here (first on line {first.line}).",
                    key_node,
                    "YAML would silently keep only the last one. Remove or rename one of them.",
                )
            document.keys[(*path, key)] = Location(
                key_node.start_mark.line + 1, key_node.start_mark.column + 1
            )
            result[key] = _value(value_node, (*path, key), document)
        return result
    if isinstance(node, SequenceNode):
        _check_tag(node, _SEQ)
        return [_value(item, (*path, i), document) for i, item in enumerate(node.value)]
    assert isinstance(node, ScalarNode)  # noqa: S101 - an invariant, not a check
    _check_tag(node, _STR)
    if node.style is not None:
        return node.value
    return _plain(node.value)


def _check_tag(node: Node, expected: str) -> None:
    if node.tag != expected:
        raise _Error(
            "yaml-tag",
            f"Manifests can't use YAML tags (`{node.tag}`).",
            node,
            "Remove the tag; quote the value if it should be text.",
        )


def _plain(value: str) -> Any:  # noqa: ANN401 - manifest data is JSON-shaped
    if _NULL.match(value):
        return None
    if _TRUE.match(value):
        return True
    if _FALSE.match(value):
        return False
    if _INT.match(value):
        return int(value)
    if _OCT.match(value):
        return int(value[2:], 8)
    if _HEX.match(value):
        return int(value[2:], 16)
    if _FLOAT.match(value):
        return float(value)
    if _INF.match(value):
        return float("-inf") if value.startswith("-") else float("inf")
    if _NAN.match(value):
        return float("nan")
    return value
