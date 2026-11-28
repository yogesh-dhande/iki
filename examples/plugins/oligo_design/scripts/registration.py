"""HELM registration with the same monomer identities and calculated properties as iki/oligo."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

import chemistry
import helm
import iki_action as iki

MONOMER = "iki/oligo.Monomer"
OLIGO = "iki/oligo.Oligonucleotide"


Refused = iki.Refused


def monomer_values(m: chemistry.Monomer, source: str | None = None) -> dict[str, Any]:
    """A monomer's registry values: what it is, and what's computed from its structure."""
    s = chemistry.structure(m.smiles)
    free = chemistry.free(m.smiles)
    values: dict[str, Any] = {
        "polymer_type": m.polymer_type,
        "symbol": m.symbol,
        "name": m.name,
        "role": m.role,
        "natural_analogue": m.natural_analogue,
        "smiles": m.smiles,
        "structure": chemistry.structure_key(m),
        "attachments": [f"R{n} ({s.cap_groups[n]})" for n in sorted(s.caps)],
        "molecular_formula": chemistry.formula(free),
        "molecular_weight": round(chemistry.average_weight(free), 3),
    }
    if m.extinction_260 is not None:
        values["extinction_260"] = m.extinction_260
    if source is not None:
        values["source"] = source
    return {k: v for k, v in values.items() if v is not None}


_LIBRARY_SOURCES: dict[tuple[str, str], dict[str, Any]] = {}


def _library_entry(key: tuple[str, str]) -> dict[str, Any]:
    if not _LIBRARY_SOURCES:
        found = json.loads(chemistry.LIBRARY.read_text(encoding="utf-8"))
        for m in found["monomers"]:
            _LIBRARY_SOURCES[(m["polymer_type"], m["symbol"])] = m
    return _LIBRARY_SOURCES[key]


class Resolver:
    """Each monomer by (polymer type, symbol): the run's given ones, then the library's.
    It remembers which library monomers it used, to register them."""

    def __init__(self, given: Iterable[Mapping[str, Any]]) -> None:
        self.given = {m.key: m for m in (chemistry.from_entity(e) for e in given)}
        self.library = chemistry.library()
        self.used: dict[tuple[str, str], chemistry.Monomer] = {}

    def __call__(self, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], chemistry.Monomer]:
        out: dict[tuple[str, str], chemistry.Monomer] = {}
        missing = []
        for key in keys:
            if key in self.given:
                out[key] = self.given[key]
            elif key in self.library:
                out[key] = self.used[key] = self.library[key]
            else:
                missing.append(key)
        if missing:
            raise chemistry.MissingError(sorted(missing))
        return out

    def explain(self, error: chemistry.MissingError) -> str:
        known = list(self.given) + list(self.library)
        hints = []
        for polymer_type, symbol in error.missing:
            same = [s for t, s in known if t == polymer_type and s.lower() == symbol.lower()]
            if same:
                hints.append(f"[{symbol}] isn't [{same[0]}]: symbols are compared exactly.")
        return " ".join(
            [
                str(error),
                "Register each as a monomer, with its structure, then give it to this "
                "action as one of its monomers.",
                *hints,
            ]
        )

    def creates(self) -> list[dict[str, Any]]:
        """The library monomers it used, registered: found where the registry has them."""
        out = []
        for key in sorted(self.used):
            entry = _library_entry(key)
            values = monomer_values(self.used[key], entry["source"])
            if entry.get("extinction_source"):
                values["extinction_source"] = entry["extinction_source"]
            out.append(
                {
                    "type": MONOMER,
                    "key": _monomer_key(key),
                    "values": values,
                    # Only what a registrar may correct fills, of what it gives; its
                    # structure must match.
                    "fills": [n for n in ("name", "source", "extinction_source") if n in values],
                }
            )
        return out

    def reference(self, m: chemistry.Monomer) -> Any:  # noqa: ANN401 - an ID or a create's key
        return m.id if m.id is not None else {"$create": _monomer_key(m.key)}


def _monomer_key(key: tuple[str, str]) -> str:
    return f"monomer:{key[0]}:{key[1]}"


# --- Oligonucleotides -----------------------------------------------------------------------


def computed(helm_text: str, resolve: Resolver) -> tuple[dict[str, Any], list[chemistry.Monomer]]:
    """What's computed from an oligonucleotide's HELM: its values, and its monomers."""
    read = helm.read(helm_text)
    chemistry.check_joined(read)
    keys = chemistry.unique_keys(read)
    monomers = resolve(keys)
    built = chemistry.build(read, monomers)
    units = [n for n in chemistry.nucleotides(built) if n.nucleotide]
    e = chemistry.extinction(built)
    values: dict[str, Any] = {
        "bases": chemistry.bases(built),
        "length": len(units),
        "molecular_formula": chemistry.formula(built.mol),
        "molecular_weight": round(chemistry.average_weight(built.mol), 2),
        "monoisotopic_mass": round(chemistry.monoisotopic_mass(built.mol), 4),
        "extinction_note": e.note,
        "modifications": chemistry.modifications(built),
        "positions": chemistry.positions(built),
    }
    if e.value is not None:
        values["extinction_260"] = e.value
    sodium = chemistry.sodium_salt_weight(built.mol)
    if sodium is not None:
        values["molecular_weight_sodium"] = round(sodium, 2)
    return values, [monomers[k] for k in keys]


def oligo_create(
    helm_text: str, resolve: Resolver, extra: Mapping[str, Any], key: str = "oligo"
) -> dict[str, Any]:
    values, used = computed(helm_text, resolve)
    return {
        "type": OLIGO,
        "key": key,
        "values": {
            "helm": helm_text,
            **{k: v for k, v in extra.items() if v not in (None, "")},
            **values,
            "monomers": [resolve.reference(m) for m in used],
        },
    }


def register(run: Mapping[str, Any]) -> dict[str, Any]:
    params = run["params"]
    resolve = Resolver(run["inputs"].get("monomers", []))
    extra = {k: params.get(k) for k in ("name", "description", "purpose")}
    try:
        oligo = oligo_create(params["helm"].strip(), resolve, extra)
    except chemistry.MissingError as missing:
        raise Refused(resolve.explain(missing)) from None
    except helm.HelmError as why:
        raise Refused(str(why)) from None
    return {"creates": [*resolve.creates(), oligo]}
