"""The lots plugin's calculations, one per kind of lot, by their first argument. Each
works out what follows from what was measured of a lot, and what it's a lot of:

- ``oligo``: an oligonucleotide lot's expected mass, its oligonucleotide's monoisotopic or
  average mass as the free acid, and its measured mass's difference from it, which the
  type's rule holds to its tolerance; and its yield in nmol from its OD260 and ε260
  (nmol = OD × 10⁶ / ε, an OD260 unit being an absorbance of 1 over 1 cm in 1 mL).
- ``plasmid``: a plasmid prep's concentration from its A260: 50 µg/mL of
  double-stranded DNA for an absorbance of 1 over 1 cm (Sambrook & Russell, 2001).
- ``protein``: a lot's concentration from its A280, c = A × dilution / (ε × l), with ε its
  measured mass extinction coefficient, or its protein's or complex's molar ε280 (Pace et
  al., 1995) over its molecular weight. A complex's are kept with each cysteine's thiol
  free (ADR 0161); with its cysteines as cystines, each disulfide its bonds place takes
  two hydrogens off and adds a cystine's 125 L/(mol·cm), as its Composition tab shows.
- ``conjugate``: a conjugate lot's DAR from its drug load distribution, the mean drug load
  weighted by each species' share of the area: of the intact conjugate (HIC, intact
  LC-MS), or for reduced chains twice the light chain's mean and twice the heavy chain's,
  an IgG having two of each; its unconjugated share (DAR 0); and its concentration from
  its A280 with its own measured ε280, which its payload adds to.
- ``polymer``: a polymer lot's dispersity, Mw/Mn (IUPAC, 2009).

Each run reads $IKI_INPUT and writes $IKI_OUTPUT, as every action's command does. What
can't be worked out (a measurement left out, a mass not yet computed) is left empty, and
the run says why; a computed value whose inputs are gone is emptied, so it never stays
behind as if it followed from them.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

#: Double-stranded DNA's concentration for an A260 of 1 over 1 cm, in ng/µL (50 µg/mL).
DSDNA_PER_A260 = 50.0
#: A cystine's molar absorbance at 280 nm in water, L/(mol·cm) (Pace et al., 1995).
CYSTINE = 125
#: Hydrogen's standard atomic weight, as the complex plugin weighs a disulfide's two.
HYDROGEN = 1.00794


def number(value: Any) -> float | None:  # noqa: ANN401 - a value's JSON
    """A number or a quantity's value (in its field's unit), or None."""
    if isinstance(value, Mapping):
        value = value.get("value")
    if value is None or isinstance(value, bool):
        return None
    return float(value)


def _included(entity: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    found = (entity.get("included") or {}).get(name) or []
    return found[0] if found else None


def _shown(value: float, places: int = 2) -> str:
    return f"{value:,.{places}f}".rstrip("0").rstrip(".")


class Lot:
    """One lot's run: its values, what it changes, and what it says."""

    def __init__(self, entity: Mapping[str, Any]) -> None:
        self.entity = entity
        self.values: Mapping[str, Any] = entity["values"]
        self.changes: dict[str, Any] = {}
        self.messages: list[dict[str, str]] = []

    @property
    def hrid(self) -> str:
        return str(self.entity.get("hrid") or self.entity["id"])

    def get(self, name: str) -> float | None:
        return number(self.values.get(name))

    def set(self, name: str, value: Any) -> None:  # noqa: ANN401 - a field's value
        """Changes ``name`` to ``value``, only if it differs, so nothing is written for
        nothing (an empty value stays empty)."""
        if number(self.values.get(name)) != number(value) or (
            not isinstance(value, int | float) and self.values.get(name) != value
        ):
            self.changes[name] = value

    def say(self, level: str, text: str) -> None:
        self.messages.append(
            {"level": level, "text": f"{self.hrid}: {text}", "entity": self.entity["id"]}
        )


# --- Oligonucleotide lots ---------------------------------------------------------------------


def oligo(lot: Lot) -> None:
    oligonucleotide = _included(lot.entity, "oligonucleotide")
    theirs: Mapping[str, Any] = oligonucleotide["values"] if oligonucleotide else {}
    basis = lot.values.get("mass_basis") or "monoisotopic"
    field = "monoisotopic_mass" if basis == "monoisotopic" else "molecular_weight"
    expected = number(theirs.get(field))
    measured = lot.get("measured_mass")
    lot.set("expected_mass", None if expected is None else round(expected, 4))
    if expected is None:
        lot.set("mass_error", None)
        if measured is not None:
            lot.say(
                "warning",
                f"its oligonucleotide's {basis} mass isn't computed, so its measured mass "
                "isn't checked against it.",
            )
    elif measured is None:
        lot.set("mass_error", None)
    else:
        error = round(measured - expected, 4)
        lot.set("mass_error", error)
        tolerance = lot.get("mass_tolerance")
        within = tolerance is None or abs(error) <= tolerance
        print(
            f"{lot.hrid}: measured {_shown(measured, 4)} Da, expected {_shown(expected, 4)} Da "
            f"({basis}), {error:+.4f} Da"
            + (
                ""
                if tolerance is None
                else f", {'within' if within else 'outside'} ±{_shown(tolerance)} Da"
            )
        )
    od = lot.get("yield_od")
    epsilon = lot.get("extinction_260") or number(theirs.get("extinction_260"))
    if od is None:
        lot.set("yield_amount", None)
    elif not epsilon:
        lot.set("yield_amount", None)
        lot.say(
            "warning",
            "it has no yield in nmol: its oligonucleotide has no ε260 worked out (ADR 0146), "
            "and the lot gives none. Give its supplier's.",
        )
    else:
        nmol = round(od * 1e6 / epsilon, 2)
        lot.set("yield_amount", nmol)
        print(
            f"{lot.hrid}: {_shown(od)} OD260 at ε260 {_shown(epsilon, 0)} L/(mol·cm) "
            f"is {_shown(nmol)} nmol"
        )


# --- Plasmid preps ----------------------------------------------------------------------------


def plasmid(lot: Lot) -> None:
    a260 = lot.get("a260")
    if a260 is None or (lot.values.get("concentration_method") or "a260") != "a260":
        return
    path = lot.get("path_length") or 1.0
    dilution = lot.get("dilution_factor") or 1.0
    concentration = round(a260 * dilution * DSDNA_PER_A260 / path, 4)
    lot.set("concentration", concentration)
    print(
        f"{lot.hrid}: A260 {_shown(a260, 4)} × {_shown(dilution, 4)} × 50 ng/µL / "
        f"{_shown(path, 4)} cm = {_shown(concentration, 4)} ng/µL"
    )


# --- Protein, complex and conjugate lots ------------------------------------------------------


def by_a280(lot: Lot, molar: float | None, weight: float | None, of: str) -> None:
    """Its concentration from its A280, with its measured ε280, or ``molar`` (L/(mol·cm))
    over ``weight`` (g/mol), which is in mL/(mg·cm)."""
    given = lot.get("extinction_coefficient")
    if given:
        epsilon: float | None = given
        how = "its measured ε280"
    elif molar and weight:
        epsilon = molar / weight
        how = f"{of}'s ε280, {_shown(molar, 0)} L/(mol·cm), over its {_shown(weight)} g/mol"
    else:
        epsilon = None
        how = ""
    lot.set("a280_extinction", None if epsilon is None else round(epsilon, 4))
    a280 = lot.get("a280")
    if a280 is None or (lot.values.get("concentration_method") or "a280") != "a280":
        return
    if epsilon is None:
        lot.say(
            "warning",
            f"its concentration isn't worked out from its A280: {of} has no ε280 and "
            "molecular weight computed, and the lot gives no measured ε280.",
        )
        return
    path = lot.get("path_length") or 1.0
    dilution = lot.get("dilution_factor") or 1.0
    concentration = round(a280 * dilution / (epsilon * path), 4)
    lot.set("concentration", concentration)
    print(
        f"{lot.hrid}: A280 {_shown(a280, 4)} × {_shown(dilution, 4)} / "
        f"({_shown(epsilon, 4)} mL/(mg·cm) × {_shown(path, 4)} cm) = "
        f"{_shown(concentration, 4)} mg/mL, by {how}"
    )


def protein(lot: Lot) -> None:
    found = _included(lot.entity, "protein")
    theirs: Mapping[str, Any] = found["values"] if found else {}
    oxidized = lot.values.get("cysteines") != "reduced"
    if "components" not in theirs:
        form = "oxidized" if oxidized else "reduced"
        by_a280(
            lot,
            number(theirs.get(f"extinction_coefficient_{form}")),
            number(theirs.get("molecular_weight")),
            "its protein",
        )
        return
    molar = number(theirs.get("extinction_coefficient_reduced"))
    weight = number(theirs.get("molecular_weight"))
    bonds = theirs.get("bonds") or []
    disulfides = sum(1 for b in bonds if b.get("kind") == "disulfide") if oxidized else 0
    if disulfides:
        molar = None if molar is None else molar + CYSTINE * disulfides
        weight = None if weight is None else weight - 2 * HYDROGEN * disulfides
        print(
            f"{lot.hrid}: its complex's {disulfides} disulfide{'s' if disulfides != 1 else ''} "
            f"add {CYSTINE * disulfides} L/(mol·cm) and take {2 * HYDROGEN * disulfides:.2f} "
            "g/mol off"
        )
    by_a280(lot, molar, weight, "its complex")


def mean_load(rows: list[Mapping[str, Any]]) -> float | None:
    """The mean drug load of ``rows``, weighted by each one's area; None without area."""
    total = sum(float(r["area"]) for r in rows)
    if total <= 0:
        return None
    return sum(int(r["drug_load"]) * float(r["area"]) for r in rows) / total


def dar(rows: list[Mapping[str, Any]]) -> tuple[float | None, float | None, str]:
    """A distribution's DAR, its unconjugated share (intact only) and how it was worked out."""
    intact = [r for r in rows if r["chain"] == "intact"]
    if intact:
        mean = mean_load(intact)
        total = sum(float(r["area"]) for r in intact)
        naked = sum(float(r["area"]) for r in intact if int(r["drug_load"]) == 0)
        unconjugated = None if not total else 100 * naked / total
        return mean, unconjugated, f"{len(intact)} intact species"
    light = mean_load([r for r in rows if r["chain"] == "light"])
    heavy = mean_load([r for r in rows if r["chain"] == "heavy"])
    if light is None or heavy is None:
        return None, None, ""
    return 2 * (light + heavy), None, f"2 × ({light:.4f} light + {heavy:.4f} heavy)"


def conjugate(lot: Lot) -> None:
    rows = list(lot.values.get("drug_loads") or [])
    if rows:
        average, unconjugated, how = dar(rows)
        if average is None:
            lot.say("warning", "its DAR isn't worked out: its distribution has no area.")
        else:
            lot.set("dar", round(average, 2))
            print(f"{lot.hrid}: DAR {average:.2f}, from {how}")
        lot.set("unconjugated", None if unconjugated is None else round(unconjugated, 2))
    else:
        lot.set("unconjugated", None)
    by_a280(lot, None, None, "a conjugate")


# --- Polymer lots -----------------------------------------------------------------------------


def polymer(lot: Lot) -> None:
    mn, mw = lot.get("mn"), lot.get("mw")
    if not mn or mw is None:
        lot.set("dispersity", None)
        return
    dispersity = round(mw / mn, 3)
    lot.set("dispersity", dispersity)
    print(f"{lot.hrid}: Đ = Mw/Mn = {_shown(mw)} / {_shown(mn)} = {dispersity:.3f}")


COMPUTE: dict[str, Callable[[Lot], None]] = {
    "oligo": oligo,
    "plasmid": plasmid,
    "protein": protein,
    "conjugate": conjugate,
    "polymer": polymer,
}


def compute(kind: str, run: Mapping[str, Any]) -> dict[str, Any]:
    updates: list[dict[str, Any]] = []
    messages: list[dict[str, str]] = []
    for entity in run["inputs"]["lot"]:
        lot = Lot(entity)
        COMPUTE[kind](lot)
        if lot.changes:
            updates.append({"entity": entity["id"], "values": lot.changes})
        messages.extend(lot.messages)
    return {"updates": {"lot": updates}, "messages": messages[:100]}


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", **compute(sys.argv[1], run)}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
