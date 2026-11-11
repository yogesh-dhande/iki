"""Display titles: what an entity is called on pages, in pickers and in links.

A type's ``display.title`` is text with ``{{ … }}`` parts. Each part names
fields, ``hrid``, or quoted text, joined by ``or``. The part shows the first
of them that has a value, so ``{{ name or hrid }}`` shows the name, or the
HRID for an entity without one. A part where nothing has a value shows
nothing. Types without a title of their own use their nearest parent's, and
an entity whose title comes out blank is called by its HRID.

Values read as people write them: quantities with their unit's symbol
(``52.3 %``), yes-or-no fields as ``Yes`` and ``No``, dates as ISO 8601.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from iki_spec import units
from iki_spec.model import Field

__all__ = ["TOKEN", "render"]

#: One token of a ``{{ … }}`` part: quoted text, a word, or anything else (an error).
TOKEN = re.compile(
    r"\s*(?:(?P<string>\"[^\"]*\"|'[^']*')|(?P<word>[A-Za-z_][A-Za-z0-9_]*)|(?P<other>\S))"
)
_PART = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


def render(
    template: str | None, values: Mapping[str, Any], hrid: str, fields: Mapping[str, Field]
) -> str:
    """The title of an entity with these JSON ``values``. ``template`` was checked when the
    plugin was built, so anything it can't read shows nothing rather than failing."""
    if template is None:
        return hrid
    title = _PART.sub(lambda m: _part(m.group(1), values, hrid, fields), template)
    title = " ".join(title.split())
    return title or hrid


def _part(
    expression: str, values: Mapping[str, Any], hrid: str, fields: Mapping[str, Field]
) -> str:
    for token in TOKEN.finditer(expression):
        string, word = token.group("string"), token.group("word")
        if string is not None:
            if string[1:-1]:
                return string[1:-1]
        elif word == "hrid":
            return hrid
        elif word is not None and word != "or" and word in fields:
            shown = _value(fields[word], values.get(word))
            if shown:
                return shown
    return ""


def _value(f: Field, value: Any) -> str:  # noqa: ANN401 - a JSON value
    if value is None or isinstance(value, list | dict):
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = repr(value).removesuffix(".0") if isinstance(value, float) else str(value).strip()
    if f.type == "quantity" and f.unit and text:
        return f"{text} {units.symbol(f.unit)}".strip()
    return text
