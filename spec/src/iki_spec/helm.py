"""HELM, the Pistoia Alliance's notation for macromolecules (HELM 2.04), read by iki-wasm.

iki-wasm (``worker/crates/iki-wasm/src/helm.rs``) reads, checks and writes canonical
HELM, so the server, the browser and plugins read a HELM string alike, and its
canonical form is an identity's (ADR 0144): the same molecule written two ways is one
record, and one written with another monomer is another.

This reads HELM's grammar, not its chemistry: what a monomer ID means is its monomer
library's, which for oligonucleotides is the ``iki/oligo`` plugin's registry of monomers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from iki_spec import wasm

__all__ = ["HelmError", "canonical", "problem", "read", "read_all"]

#: Instructions reading 1,000 characters of HELM may take, with room for the identical
#: polymers whose numberings canonicalising tries.
FUEL = 20_000_000


class HelmError(ValueError):
    """Why a string isn't HELM, or can't be canonical, and at which character (from 0)."""

    def __init__(self, message: str, at: int) -> None:
        super().__init__(message)
        self.message = message
        self.at = at


def read_all(texts: Sequence[str]) -> list[dict[str, Any] | HelmError]:
    """Each HELM string read: its canonical form, its polymers (their monomers in order)
    and connections, or why it isn't HELM. Raises :class:`iki_spec.wasm.UnavailableError`
    without the module."""
    if not texts:
        return []
    size = sum(len(t) for t in texts)
    found = wasm.call("iki_helm", {"helm": list(texts)}, FUEL * (1 + size // 1000))
    out: list[dict[str, Any] | HelmError] = []
    for each in found["readings"]:
        if "error" in each:
            out.append(HelmError(str(each["error"]), int(each.get("at", 0))))
        else:
            out.append(each)
    return out


def read(text: str) -> dict[str, Any]:
    """``text`` read (see :func:`read_all`). Raises :class:`HelmError` if it isn't HELM."""
    (reading,) = read_all([text])
    if isinstance(reading, HelmError):
        raise reading
    return reading


def canonical(text: str) -> str:
    """``text``'s canonical form. Raises :class:`HelmError` if it isn't HELM."""
    return str(read(text)["canonical"])


def problem(text: str) -> str | None:
    """Why ``text`` isn't HELM, as a sentence that follows "The value ", or None."""
    try:
        read(text)
    except HelmError as error:
        return f"isn't HELM, at character {error.at + 1}: {error.message}"
    return None
