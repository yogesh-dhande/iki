"""Checking apps' text and expressions (ADR 0090) with iki-wasm, the module browsers
render them with, run here by wasmtime: what the server accepts is exactly what browsers
run. Text has expressions between ``{{`` and ``}}``; iki-wasm splits it, as browsers do.

The module, and how it's run, are :mod:`iki_spec.wasm`'s, which plugins' rules and
guards are checked and evaluated with too (ADR 0127, ADR 0128).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from iki_spec import wasm
from iki_spec.wasm import FUEL, UnavailableError

__all__ = ["FUEL", "MAX_LENGTH", "Checked", "UnavailableError", "check"]

#: The longest expression, in characters, as iki-wasm also checks.
MAX_LENGTH = 1000


@dataclass(frozen=True)
class Checked:
    """What one text's expressions read, or why one doesn't."""

    #: The names they read, sorted: panels' IDs, say.
    names: tuple[str, ...] = ()
    #: The fields they read of those names, as ``name.field``, sorted.
    fields: tuple[str, ...] = ()
    #: Whether it's one expression and nothing else, so its value is that one's.
    whole: bool = False
    error: str | None = None

    @property
    def valid(self) -> bool:
        return self.error is None


def check(texts: Sequence[str]) -> list[Checked]:
    """Whether each text's expressions read, and the names and fields they use (those
    that `map`, `filter`, `all` and `exists` bind aside), as browsers will read them."""
    # Text without braces has no expressions to read: plain, as iki-wasm would say.
    asked = [t for t in texts if "{{" in t or "}}" in t]
    if not asked:
        return [Checked() for _ in texts]
    fuel = FUEL * (1 + sum(len(t) for t in asked) // MAX_LENGTH)
    found = iter(
        cast("list[dict[str, Any]]", wasm.call("iki_check", {"templates": asked}, fuel)["checked"])
    )
    out = []
    for text in texts:
        if "{{" not in text and "}}" not in text:
            out.append(Checked())
            continue
        each = next(found)
        if "error" in each:
            out.append(Checked(error=str(each["error"])))
        else:
            out.append(
                Checked(
                    names=tuple(each["names"]),
                    fields=tuple(each["fields"]),
                    whole=bool(each["whole"]),
                )
            )
    return out
