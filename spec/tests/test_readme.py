"""The manifest spec (spec/README.md) says what each key a manifest can have does: the
skills send plugin authors to it, so a key the schemas take that it doesn't name is one
an author can't learn of."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

SPEC = Path(__file__).resolve().parents[1]
README = (SPEC / "README.md").read_text(encoding="utf-8")
SCHEMAS = SPEC / "src" / "iki_spec" / "schemas"

#: Each kind of file a plugin author writes, by its schema.
AUTHORED = (
    "plugin",
    "entity-type",
    "entity-type-extension",
    "action",
    "environment",
    "workflow",
    "trigger",
    "component",
    "unit",
    "settings",
    "action-test",
)
#: A field's options, wherever a field is declared: a type's, a row's, a setting's.
FIELD_DEFS = ("typed", "state", "row", "cell")


def documented(key: str) -> bool:
    return re.search(rf"`{re.escape(key)}`|\b{re.escape(key)}:", README) is not None


def keys(schema: str) -> list[str]:
    data: dict[str, Any] = json.loads((SCHEMAS / schema / "v1.json").read_text())
    found = list(data.get("properties", {}))
    if schema == "field":
        for name in FIELD_DEFS:
            found += list(data["$defs"][name].get("properties", {}))
    return found


@pytest.mark.parametrize("schema", [*AUTHORED, "field", "rule"])
def test_every_key_a_manifest_can_have_is_in_the_spec(schema: str) -> None:
    if schema == "rule":
        rule = json.loads((SCHEMAS / "rule" / "v1.json").read_text())["$defs"]["rule"]
        missing = [k for k in rule["properties"] if not documented(k)]
    else:
        missing = [k for k in keys(schema) if not documented(k)]
    assert missing == [], f"spec/README.md doesn't say what {missing} in {schema}/v1 do."


def test_lab_lists_have_a_section_of_their_own() -> None:
    assert "### Lab lists" in README
    for key in ("`code`", "`label`", "`position`", "list: true", "options:"):
        assert key in README, key
