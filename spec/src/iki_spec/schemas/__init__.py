"""The iki/v1 JSON Schemas, and checking documents against them with readable messages.

Each schema lives at `<name>/v1.json` here and is published at
`https://ikisci.org/schemas/<name>/v1`. They reference each other by those
URLs; the registry below resolves them locally, so nothing is fetched.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterator
from functools import cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from iki_spec.problems import Path, describe_path

__all__ = ["NAMES", "SchemaIssue", "check", "load", "registry", "url"]

#: Every published schema, by name.
NAMES = (
    "manifest",
    "plugin",
    "entity-type",
    "entity-type-extension",
    "field",
    "settings",
    "unit",
    "plugin-lock",
    "action",
    "environment",
    "workflow",
    "trigger",
    "compute",
    "compute-template",
    "environment-binding",
    "run-input",
    "run-output",
    "setup-input",
)

BASE_URL = "https://ikisci.org/schemas"


def url(name: str, version: str = "v1") -> str:
    return f"{BASE_URL}/{name}/{version}"


@cache
def load(name: str, version: str = "v1") -> dict[str, Any]:
    """A schema as published, by name (for example `entity-type`)."""
    text = files("iki_spec.schemas").joinpath(name, f"{version}.json").read_text("utf-8")
    schema: dict[str, Any] = json.loads(text)
    return schema


@cache
def registry() -> Registry[Any]:
    resources = [(url(n), Resource.from_contents(load(n), DRAFT202012)) for n in NAMES]
    return Registry().with_resources(resources)


@cache
def _validator(name: str, part: str | None = None) -> Draft202012Validator:
    schema = load(name) if part is None else {"$ref": f"{url(name)}#/$defs/{part}"}
    return Draft202012Validator(schema, registry=registry())


class SchemaIssue:
    """One way a document doesn't match its schema, said plainly."""

    def __init__(
        self, path: Path, message: str, *, hint: str | None = None, at_key: bool = False
    ) -> None:
        self.path = path
        self.message = message
        self.hint = hint
        self.at_key = at_key  # point at the key rather than the value

    def __repr__(self) -> str:
        return f"SchemaIssue({self.path!r}, {self.message!r})"


def check(name: str, document: Any, *, part: str | None = None) -> list[SchemaIssue]:  # noqa: ANN401 - manifest data is JSON-shaped
    """Check a document against a schema (or one of its ``$defs``, ``part``), returning the
    problems in reading order."""
    issues: list[SchemaIssue] = []
    seen: set[tuple[Path, str]] = set()
    validator = _validator(name, part)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(map(str, e.path)))
    for error in errors:
        for issue in _explain(error, document):
            key = (issue.path, issue.message)
            if key not in seen:
                seen.add(key)
                issues.append(issue)
    return _drop_redundant(issues)


def _drop_redundant(issues: list[SchemaIssue]) -> list[SchemaIssue]:
    # An unknown option is reported once as "isn't an option for X fields";
    # drop the generic "isn't allowed here" for the same key.
    specific = {i.path for i in issues if "isn't an option" in i.message}
    # A list of lists is reported as such, not also as a list missing its items.
    nested = {i.path[:-1] for i in issues if i.message.startswith("List items can't be")}
    return [
        i
        for i in issues
        if not (i.path in specific and "isn't allowed here" in i.message)
        and not (i.path in nested and "is missing" in i.message)
    ]


def _explain(error: ValidationError, document: Any) -> Iterator[SchemaIssue]:  # noqa: ANN401 - manifest data is JSON-shaped
    # if/then and allOf wrap the real errors; report the leaves.
    if error.validator in ("allOf", "anyOf", "oneOf") and error.context:
        for sub in error.context:
            yield from _explain(sub, document)
        return
    path: Path = tuple(error.absolute_path)
    where = describe_path(path)
    validator = error.validator
    schema_path = list(error.absolute_schema_path)

    if "propertyNames" in schema_path:
        # The error is about a key, not a value.
        name = str(error.instance)
        key_path = (*path, name)
        parent = _at(document, path)
        if validator == "enum" and isinstance(parent, dict) and isinstance(parent.get("type"), str):
            allowed = [str(v) for v in _values(error)]
            if path[:1] == ("settings",):
                allowed = [a for a in allowed if a not in ("immutable", "computed", "indexed")]
            yield SchemaIssue(
                key_path,
                f"`{name}` isn't an option for {parent['type']} fields.",
                hint=_did_you_mean(name, allowed) or "Options: " + _list(allowed) + ".",
                at_key=True,
            )
            return
        if validator == "pattern":
            rule = _PATTERN_MESSAGES.get(str(error.validator_value), "")
            example = " (such as `molecular_weight`)." if rule.endswith("letter.") else ""
            message = f"`{name}` isn't a valid name. {rule}".strip()
            yield SchemaIssue(key_path, message[:-1] + example if example else message, at_key=True)
            return
        yield SchemaIssue(key_path, f"`{name}` isn't a valid name here.", at_key=True)
        return

    if validator == "required":
        missing = _missing(error)
        yield SchemaIssue(
            path, f"`{missing}` is missing{_in(where)}.", hint=_required_hint(missing)
        )
        return
    if validator in ("additionalProperties", "unevaluatedProperties"):
        allowed = sorted(_allowed_keys(error.schema))
        for name in _unexpected(error):
            yield SchemaIssue(
                (*path, name),
                f"`{name}` isn't allowed here{_in(where)}.",
                hint=_did_you_mean(name, allowed),
                at_key=True,
            )
        return
    if validator == "const":
        yield SchemaIssue(
            path,
            f"`{where}` must be `{error.validator_value}`, not {_show(error.instance)}.",
        )
        return
    if validator == "enum":
        allowed = [str(v) for v in _values(error)]
        yield SchemaIssue(
            path,
            f"`{where}` can't be {_show(error.instance)}. Choose one of: {_list(allowed)}.",
            hint=_did_you_mean(str(error.instance), allowed),
        )
        return
    if validator == "type":
        expected = error.validator_value
        yield SchemaIssue(
            path,
            f"`{where}` must be {_type_name(expected)}, not {_show(error.instance)}.",
            hint=_type_hint(expected, error.instance),
        )
        return
    if validator == "pattern":
        yield SchemaIssue(path, _bad_value(where, error.instance, str(error.validator_value)))
        return
    negated = error.validator_value
    if validator == "not" and isinstance(negated, dict) and set(negated) == {"required"}:
        names = " or ".join(f"`{n}`" for n in negated["required"])
        yield SchemaIssue(path, f"Give {names}{_in(where)}, not both.")
        return
    if validator == "not" and path and path[-1] == "type":
        yield SchemaIssue(
            path,
            f"List items can't be {_show(error.instance)}.",
            hint="Use a json field for nested structures.",
        )
        return
    yield SchemaIssue(path, f"`{where}`: {_plain(error)}")


# --- Wording ---------------------------------------------------------------------------


def _values(error: ValidationError) -> list[Any]:
    value = error.validator_value
    return list(value) if isinstance(value, (list, tuple)) else []


def _at(document: Any, path: Path) -> Any:  # noqa: ANN401 - manifest data is JSON-shaped
    for part in path:
        try:
            document = document[part]
        except (KeyError, IndexError, TypeError):
            return None
    return document


def _in(where: str) -> str:
    return f" in `{where}`" if where else ""


def _missing(error: ValidationError) -> str:
    # jsonschema says "'x' is a required property".
    message = str(error.message)
    return message.split("'")[1] if message.count("'") >= 2 else message


def _unexpected(error: ValidationError) -> list[str]:
    if isinstance(error.instance, dict):
        allowed = _allowed_keys(error.schema)
        extra = [k for k in error.instance if k not in allowed]
        if extra:
            return extra
    return [error.message]  # pragma: no cover (always a mapping here)


def _allowed_keys(schema: Any) -> set[str]:  # noqa: ANN401 - manifest data is JSON-shaped
    if not isinstance(schema, dict):
        return set()
    keys = set(schema.get("properties", {}))
    ref = schema.get("$ref")
    if isinstance(ref, str):
        resolved = registry().resolver().lookup(ref if "://" in ref else url("field") + ref)
        keys |= _allowed_keys(resolved.contents)
    return keys


def _did_you_mean(name: str, options: list[str]) -> str | None:
    same_letters = [o for o in options if o.lower() == name.lower()]
    if same_letters:
        return f"Did you mean `{same_letters[0]}`?"
    close = difflib.get_close_matches(name, options, n=1, cutoff=0.6)
    return f"Did you mean `{close[0]}`?" if close else None


def _list(values: list[str]) -> str:
    return ", ".join(f"`{v}`" for v in values)


def _show(value: Any) -> str:  # noqa: ANN401 - manifest data is JSON-shaped
    if isinstance(value, bool):
        return "`true`" if value else "`false`"
    if value is None:
        return "empty"
    if isinstance(value, str):
        return f"`{value}`" if len(value) <= 60 else f"`{value[:57]}…`"
    if isinstance(value, dict):
        return "a mapping"
    if isinstance(value, list):
        return "a list"
    return f"`{value}`"


def _type_name(expected: Any) -> str:  # noqa: ANN401 - manifest data is JSON-shaped
    names = {
        "string": "text",
        "integer": "a whole number",
        "number": "a number",
        "boolean": "`true` or `false`",
        "object": "a mapping",
        "array": "a list",
    }
    if isinstance(expected, list):
        return " or ".join(names.get(str(e), str(e)) for e in expected)
    return names.get(expected, str(expected))


def _type_hint(expected: Any, instance: Any) -> str | None:  # noqa: ANN401 - manifest data is JSON-shaped
    if expected == "boolean" and isinstance(instance, str):
        return "Write `true` or `false`. (YAML 1.2 reads `yes`, `no`, `on` and `off` as text.)"
    if expected == "string" and isinstance(instance, (int, float, bool)):
        return "Quote it to make it text."
    if expected == "array" and isinstance(instance, str):
        return f"Write a list, such as `[{instance}]`."
    return None


def _required_hint(missing: str) -> str | None:
    return {
        "apiVersion": "Start each manifest with `apiVersion: iki/v1`.",
        "schema_version": "Start at `schema_version: 1`, and increase it when the fields change.",
        "unit": "Quantities need the UCUM unit their values are stored in, such as `unit: mg`.",
        "values": "List the allowed values, such as `values: [low, high]`.",
        "to": "Name the type it refers to, such as `to: core.Entity`.",
        "items": "Say what each item is, such as `items: { type: string }`.",
    }.get(missing)


_PATTERN_MESSAGES = {
    "^[A-Z][A-Z0-9]{1,7}$": "HRID prefixes are 2 to 8 capital letters or digits, starting with a "
    "letter (such as `PRT`).",
    "^[a-z][a-z0-9]*(_[a-z0-9]+)*$": "Names use lower-case letters, digits and single "
    "underscores, starting with a letter.",
    "^[A-Z][A-Za-z0-9]*$": "Type names are PascalCase (such as `ProteinStructure`).",
    "^([a-z][a-z0-9]*(_[a-z0-9]+)*\\.)?[A-Z][A-Za-z0-9]*$": "Types are written `plugin.Type` "
    "(such as `protein.Protein`), or `Type` for one in this plugin.",
    "^[A-Za-z0-9][A-Za-z0-9_.+-]*$": "Enum values use letters, digits and `_ . + -`, starting with "
    "a letter or digit.",
    "^[\\x21-\\x7e]+$": "Units are UCUM codes in plain ASCII, with no spaces.",
    "^[1-9][0-9]*(B|KiB|MiB|GiB|TiB|kB|MB|GB|TB)$": "Sizes are a whole number and a unit, such as "
    "`100MiB` or `2GiB`.",
    "^[0-9]{4}-[0-9]{2}-[0-9]{2}$": "Dates are written YYYY-MM-DD.",
    "^https://[^\\s]+$": "Use an https:// address.",
}


def _bad_value(where: str, value: Any, pattern: str) -> str:  # noqa: ANN401 - manifest data is JSON-shaped
    explanation = _PATTERN_MESSAGES.get(pattern)
    if explanation is None:
        if "(==|!=|" in pattern:
            explanation = "Constraints compare whole schema versions, such as `>=2` or `>=2,<4`."
        elif pattern.startswith("^(0|[1-9]"):
            explanation = "Versions are semantic versions, such as `1.4.0`."
        elif "media_types" in where:
            explanation = "Media types look like `application/json` or `image/*`."
        else:
            explanation = "It doesn't have the expected form."
    return f"`{where}` can't be {_show(value)}. {explanation}"


def _plain(error: ValidationError) -> str:
    messages = {
        "minimum": f"must be at least {error.validator_value}",
        "maximum": f"must be at most {error.validator_value}",
        "minLength": f"must be at least {error.validator_value} characters long",
        "maxLength": f"must be at most {error.validator_value} characters long",
        "minItems": f"needs at least {error.validator_value} item(s)",
        "maxItems": f"can have at most {error.validator_value} items",
        "uniqueItems": "lists something twice",
        "minProperties": f"needs at least {error.validator_value} entry",
        "maxProperties": f"can have at most {error.validator_value} entries",
    }
    return messages.get(str(error.validator), str(error.message)) + "."
