"""Display titles, as pages and pickers show them."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from iki_spec.model import Field
from iki_spec.titles import render

FIELDS = {
    "name": Field(name="name", type="string"),
    "code": Field(name="code", type="string"),
    "gc": Field(name="gc", type="quantity", unit="%"),
    "length": Field(name="length", type="quantity", unit="{bp}"),
    "circular": Field(name="circular", type="bool"),
    "count": Field(name="count", type="int"),
}


@pytest.mark.parametrize(
    ("template", "values", "title"),
    [
        ("{{ name or hrid }}", {"name": "pUC19"}, "pUC19"),
        ("{{ name or hrid }}", {"name": None}, "DNA-000042"),
        ("{{ name or hrid }}", {"name": "  "}, "DNA-000042"),
        ("{{ name or code or 'untitled' }}", {"code": "X1"}, "X1"),
        ("{{ name or 'untitled' }}", {}, "untitled"),
        ("{{ name }} ({{ hrid }})", {"name": "pUC19"}, "pUC19 (DNA-000042)"),
        ("{{ name }} ({{ code }})", {}, "()"),
        ("{{ name }}", {}, "DNA-000042"),
        ("GC {{ gc }}", {"gc": 52.5}, "GC 52.5 %"),
        ("{{ length }}", {"length": 2686.0}, "2686 bp"),
        ("{{ circular }}", {"circular": False}, "No"),
        ("{{ count }}", {"count": 0}, "0"),
        ("{{ missing or hrid }}", {}, "DNA-000042"),
        (None, {"name": "pUC19"}, "DNA-000042"),
    ],
)
def test_titles(template: str | None, values: dict[str, object], title: str) -> None:
    assert render(template, values, "DNA-000042", FIELDS) == title


# --- Iki's own types -----------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
#: The fields a record is called by, nearest first: what a lab writes on its label.
CALLED_BY = ("name", "lot_number")


def _types() -> dict[str, dict[str, Any]]:
    """Iki's own entity types, core's and its plugins', by qualified name."""
    out: dict[str, dict[str, Any]] = {}
    sources = [("iki/core", ROOT / "spec" / "src" / "iki_spec" / "core" / "types")]
    sources += [(f"iki/{p.name}", p / "types") for p in sorted((ROOT / "plugins").iterdir())]
    for plugin, directory in sources:
        for file in sorted(directory.glob("*.yaml")):
            for body in yaml.safe_load_all(file.read_text(encoding="utf-8")):
                if body and body.get("kind") == "EntityType":
                    out[f"{plugin}.{body['name']}"] = {**body, "plugin": plugin}
    return out


def _lineage(name: str, types: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    while name in types:
        body = types[name]
        out.append(body)
        parent = body.get("extends")
        if not parent:
            break
        name = parent if "." in parent else f"{body['plugin']}.{parent}"
    return out


def test_iki_s_own_types_are_called_by_their_names() -> None:
    # A record stored with a name, or a lot with its number, is called by it in lists,
    # pickers and headings: an HRID alone tells a batch of aspirin from one of aspirin
    # sodium only to whoever opens both.
    types = _types()
    uncalled = []
    for name, body in types.items():
        lineage = _lineage(name, types)
        fields = {f for b in lineage for f in b.get("fields") or {}}
        title = next(
            (b["display"]["title"] for b in lineage if "title" in (b.get("display") or {})), None
        )
        called = next((f for f in CALLED_BY if f in fields), None)
        if called and not body.get("abstract") and (title is None or called not in title):
            uncalled.append(f"{name}: {title!r} without {called}")
    assert uncalled == []
