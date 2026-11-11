"""Display titles, as pages and pickers show them."""

import pytest

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
