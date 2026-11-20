"""The lots plugin's calculations, one per kind of lot, by their first argument. Each
works out what follows from what was measured of a lot, and what it's a lot of:

- ``oligo``: an oligonucleotide lot's expected mass, its oligonucleotide's monoisotopic or
  average mass as the free acid, and its measured mass's difference from it, exactly as
  the two decimals differ, which the type's rule holds to its tolerance; and its yield
  in nmol from its OD260 and ε260 (nmol = OD × 10⁶ / ε, an OD260 unit being an
  absorbance of 1 over 1 cm in 1 mL).
- ``plasmid``: a plasmid prep's concentration from its A260: 50 µg/mL of
  double-stranded DNA for an absorbance of 1 over 1 cm (Sambrook & Russell, 2001).
- ``protein``: a lot's concentration from its A280, c = A × dilution / (ε × l), with ε its
  measured mass extinction coefficient, or its protein's or complex's molar ε280 (Pace et
  al., 1995) over its molecular weight. With its cysteines as cystines, each cystine
  takes two hydrogens off its molecular weight: a protein's every pair of cysteines, as
  its oxidized ε280 counts them, and a complex's the disulfides its bonds place, which
  add a cystine's 125 L/(mol·cm) each too, as its Composition tab shows. A complex's are
  kept with each cysteine's thiol free (ADR 0161).
- ``conjugate``: a conjugate lot's DAR from its drug load distribution, the mean drug load
  weighted by each species' share of the area: of the intact conjugate (HIC, intact
  LC-MS), or for reduced chains each chain's mean times the copies of it its conjugate
  has (two of each in an IgG, one in a Fab), which a complex of them says; its
  unconjugated share (DAR 0); and its concentration from its A280 with its own measured
  ε280, which its payload adds to.
- ``polymer``: a polymer lot's molar-mass dispersity, Mw/Mn (IUPAC, 2009).

Each run reads $IKI_INPUT and writes $IKI_OUTPUT, as every action's command does. What
can't be worked out (a measurement left out, a mass not yet computed, a path length or
an extinction coefficient of 0) is left empty, and the run says why; a computed value
whose inputs are gone is emptied, so it never stays behind as if it followed from them.

A concentration or a DAR is an ordinary field: a pool changes a concentration (ADR 0157),
and either can be entered as measured. So what a measurement gives is kept beside it, in
a computed field (``a260_concentration``, ``a280_concentration``, ``distribution_dar``),
and the field follows it: when the measurement gives something new, the field is that;
when it gives nothing any more, the field is emptied if it was what it gave. Otherwise
the field was set since, by a pool or by hand, and is kept, rather than put back to what
an older measurement, of what was there before, gave.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

#: Double-stranded DNA's concentration for an A260 of 1 over 1 cm, in ng/µL (50 µg/mL).
DSDNA_PER_A260 = 50.0
#: A cystine's molar absorbance at 280 nm in water, L/(mol·cm) (Pace et al., 1995).
CYSTINE = 125
#: Hydrogen's standard atomic weight, as the complex plugin weighs a disulfide's two.
HYDROGEN = 1.00794
#: How finely a measured mass is compared with its tolerance, in u: a micro-dalton, far
#: finer than any mass spectrometer resolves, and far coarser than the binary rounding of
#: two masses' difference. The type's rule compares to the same.
MASS_RESOLUTION = Decimal("0.000001")


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


def _trimmed(text: str) -> str:
    """``text`` without the zeros ending its decimals: only after a point, so 182,800
    stays 182,800."""
    return text.rstrip("0").rstrip(".") if "." in text else text


def _shown(value: float, places: int = 2) -> str:
    """``value`` to at most ``places`` decimals, as a result is shown."""
    return _trimmed(f"{value:,.{places}f}")


def _given(value: float | Decimal) -> str:
    """``value`` as it was given, to every decimal it has: a specification's 0.05001 Da
    isn't 0.05."""
    exact = value if isinstance(value, Decimal) else Decimal(repr(value))
    return _trimmed(f"{exact:,f}")


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

    def follows(self, name: str, kept: str, result: float | None, what: str) -> None:
        """``name`` follows ``result``, what its measurement (``what``) gives now, which
        ``kept`` records (see the module's docstring)."""
        before = self.get(kept)
        current = self.get(name)
        self.set(kept, result)
        if result is not None and result != before:
            self.set(name, result)
        elif result is None and before is not None and current == before:
            self.set(name, None)
            self.say(
                "warning",
                f"its {name.replace('_', ' ')} is emptied: it was its {what}'s, which "
                "gives none now.",
            )
        elif result is not None and current != result:
            print(
                f"{self.hrid}: its {name.replace('_', ' ')} is kept as it is, "
                f"{'not known' if current is None else _given(current)}: it was set since "
                f"its {what} gave {_given(result)}, such as by a pool. Measure it again to "
                "work it out from a new reading."
            )


def _path(lot: Lot) -> float | None:
    """Its path length in cm, which a concentration is worked out over, or None, saying
    why: left out, or not more than 0."""
    path = lot.get("path_length")
    if path is None:
        lot.say(
            "warning",
            "its concentration isn't worked out from its absorbance: it has no path length.",
        )
        return None
    if path <= 0:
        lot.say(
            "warning",
            f"its concentration isn't worked out from its absorbance: its path length is "
            f"{_given(path)} cm, and an absorbance is over a path longer than 0.",
        )
        return None
    return path


# --- Oligonucleotide lots ---------------------------------------------------------------------


def oligo(lot: Lot) -> None:
    oligonucleotide = _included(lot.entity, "oligonucleotide")
    theirs: Mapping[str, Any] = oligonucleotide["values"] if oligonucleotide else {}
    basis = lot.values.get("mass_basis") or "monoisotopic"
    field = "monoisotopic_mass" if basis == "monoisotopic" else "molecular_weight"
    expected = number(theirs.get(field))
    measured = lot.get("measured_mass")
    lot.set("expected_mass", expected)
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
        # The two decimals' difference, exactly: 5555.03254 less 5554.98250 is 0.05004,
        # not 0.0500, which a tolerance of 0.05001 would pass.
        error = Decimal(repr(measured)) - Decimal(repr(expected))
        lot.set("mass_error", float(error))
        tolerance = lot.get("mass_tolerance")
        sign = "+" if error >= 0 else "-"
        said = (
            f"{lot.hrid}: measured {_given(measured)} Da, expected {_given(expected)} Da "
            f"({basis}), {sign}{_given(abs(error))} Da"
        )
        if tolerance is not None:
            within = abs(error) <= Decimal(repr(tolerance)) + MASS_RESOLUTION
            said += f", {'within' if within else 'outside'} ±{_given(tolerance)} Da"
        print(said)
    od = lot.get("yield_od")
    given = lot.get("extinction_260")
    epsilon = given if given is not None else number(theirs.get("extinction_260"))
    if od is None:
        lot.set("yield_amount", None)
    elif given is not None and given <= 0:
        lot.set("yield_amount", None)
        lot.say(
            "warning",
            f"it has no yield in nmol: the ε260 it gives is {_given(given)}, and an OD260 "
            "is nmol only through an ε260 above 0. Give its supplier's.",
        )
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
    if (lot.values.get("concentration_method") or "a260") != "a260":
        # Measured another way, it's as entered; what an A260 gives isn't what it is.
        lot.set("a260_concentration", None)
        return
    a260 = lot.get("a260")
    result: float | None = None
    path = None if a260 is None else _path(lot)
    if a260 is not None and path is not None:
        dilution = lot.get("dilution_factor") or 1.0
        result = round(a260 * dilution * DSDNA_PER_A260 / path, 4)
        print(
            f"{lot.hrid}: A260 {_shown(a260, 4)} × {_shown(dilution, 4)} × 50 ng/µL / "
            f"{_shown(path, 4)} cm = {_shown(result, 4)} ng/µL"
        )
    lot.follows("concentration", "a260_concentration", result, "A260")


# --- Protein, complex and conjugate lots ------------------------------------------------------


def by_a280(
    lot: Lot, molar: float | None, weight: float | None, of: str, *, note: str = ""
) -> None:
    """Its concentration from its A280, with its measured ε280, or ``molar`` (L/(mol·cm))
    over ``weight`` (g/mol), which is in mL/(mg·cm). ``note``: how ``molar`` and ``weight``
    were worked out, said only if they're what's used."""
    given = lot.get("extinction_coefficient")
    if given is not None and given <= 0:
        epsilon: float | None = None
        how = ""
        lot.say(
            "warning",
            f"its concentration isn't worked out from its A280: its measured ε280 is "
            f"{_given(given)} mL/(mg·cm), and an absorbance gives a concentration only "
            "through an ε280 above 0.",
        )
    elif given is not None:
        epsilon = given
        how = "its measured ε280"
    elif molar and weight:
        epsilon = molar / weight
        how = f"{of}'s ε280, {_shown(molar, 0)} L/(mol·cm), over its {_shown(weight)} g/mol"
        if note:
            print(f"{lot.hrid}: {note}")
    else:
        epsilon = None
        how = ""
    lot.set("a280_extinction", None if epsilon is None else round(epsilon, 4))
    if (lot.values.get("concentration_method") or "a280") != "a280":
        lot.set("a280_concentration", None)
        return
    a280 = lot.get("a280")
    result: float | None = None
    if a280 is not None and epsilon is None and given is None:
        lot.say(
            "warning",
            f"its concentration isn't worked out from its A280: {of} has no ε280 and "
            "molecular weight computed, and the lot gives no measured ε280.",
        )
    elif a280 is not None and epsilon is not None and (path := _path(lot)) is not None:
        dilution = lot.get("dilution_factor") or 1.0
        result = round(a280 * dilution / (epsilon * path), 4)
        print(
            f"{lot.hrid}: A280 {_shown(a280, 4)} × {_shown(dilution, 4)} / "
            f"({_shown(epsilon, 4)} mL/(mg·cm) × {_shown(path, 4)} cm) = "
            f"{_shown(result, 4)} mg/mL, by {how}"
        )
    lot.follows("concentration", "a280_concentration", result, "A280")


def protein(lot: Lot) -> None:
    found = _included(lot.entity, "protein")
    theirs: Mapping[str, Any] = found["values"] if found else {}
    oxidized = lot.values.get("cysteines") != "reduced"
    weight = number(theirs.get("molecular_weight"))
    if "components" not in theirs:
        reduced = number(theirs.get("extinction_coefficient_reduced"))
        if not oxidized:
            by_a280(lot, reduced, weight, "its protein")
            return
        molar = number(theirs.get("extinction_coefficient_oxidized"))
        # Its oxidized ε280 counts a cystine for every pair of its cysteines; each is two
        # hydrogens lighter, as a complex's disulfides are.
        if molar is None or reduced is None:
            by_a280(lot, None, None, "its protein")
            return
        cystines = round((molar - reduced) / CYSTINE)
        note = ""
        if cystines and weight is not None:
            weight -= 2 * HYDROGEN * cystines
            note = (
                f"its protein's {cystines} cystine{'s' if cystines != 1 else ''} take "
                f"{2 * HYDROGEN * cystines:.2f} g/mol off its sequence's molecular weight"
            )
        by_a280(lot, molar, weight, "its protein", note=note)
        return
    molar = number(theirs.get("extinction_coefficient_reduced"))
    bonds = theirs.get("bonds") or []
    # Its own disulfides, and those its parts have of their own (a Fab's, in a complex of
    # the Fab and its antigen), which its composition counted for this project.
    own = sum(1 for b in bonds if b.get("kind") == "disulfide")
    disulfides = own + int(theirs.get("part_disulfides") or 0) if oxidized else 0
    if oxidized and (
        theirs.get("bond_map") in ("incomplete", "predominant")
        or theirs.get("part_bonds_complete") is False
        or (theirs.get("molecular_formula") and theirs.get("part_disulfides") is None)
    ):
        lot.say(
            "warning",
            "its complex's disulfides may not all be counted (its Composition tab says "
            "which), so its concentration from its A280 may be off by a cystine's 125 "
            "L/(mol·cm) for each.",
        )
    note = ""
    if disulfides:
        molar = None if molar is None else molar + CYSTINE * disulfides
        weight = None if weight is None else weight - 2 * HYDROGEN * disulfides
        note = (
            f"its complex's {disulfides} disulfide{'s' if disulfides != 1 else ''} "
            f"add {CYSTINE * disulfides} L/(mol·cm) and take {2 * HYDROGEN * disulfides:.2f} "
            "g/mol off"
        )
    by_a280(lot, molar, weight, "its complex", note=note)


def mean_load(rows: list[Mapping[str, Any]]) -> float | None:
    """The mean drug load of ``rows``, weighted by each one's area; None without area."""
    total = sum(float(r["area"]) for r in rows)
    if total <= 0:
        return None
    return sum(int(r["drug_load"]) * float(r["area"]) for r in rows) / total


def chains(conjugate: Mapping[str, Any] | None) -> tuple[int, int] | None:
    """How many light and heavy chains each molecule of ``conjugate`` (its values) has, by
    its parts' copies, as a complex of them, such as an antibody, says: (2, 2) for an IgG,
    (1, 1) for a Fab. None if it doesn't say."""
    if conjugate is None:
        return None
    parts = conjugate.get("components") or []
    light = sum(int(p.get("copies") or 0) for p in parts if p.get("role") == "light")
    heavy = sum(int(p.get("copies") or 0) for p in parts if p.get("role") == "heavy")
    return (light, heavy) if light and heavy else None


def dar(
    rows: list[Mapping[str, Any]], copies: tuple[int, int] | None = None
) -> tuple[float | None, float | None, str]:
    """A distribution's DAR, its unconjugated share (intact only), and how it was worked
    out, or why it wasn't. A reduced one's needs ``copies``, its light and heavy chains."""
    intact = [r for r in rows if r["chain"] == "intact"]
    if intact:
        mean = mean_load(intact)
        total = sum(float(r["area"]) for r in intact)
        naked = sum(float(r["area"]) for r in intact if int(r["drug_load"]) == 0)
        unconjugated = None if not total else 100 * naked / total
        if mean is None:
            return None, unconjugated, "its distribution has no area"
        return mean, unconjugated, f"{len(intact)} intact species"
    light = mean_load([r for r in rows if r["chain"] == "light"])
    heavy = mean_load([r for r in rows if r["chain"] == "heavy"])
    if light is None or heavy is None:
        return None, None, "its distribution has no area"
    if copies is None:
        return (
            None,
            None,
            "what it's a lot of doesn't say how many light and heavy chains each molecule "
            "has, as a complex of them, such as an antibody, does",
        )
    each_light, each_heavy = copies
    return (
        each_light * light + each_heavy * heavy,
        None,
        f"{each_light} × {light:.4f} light + {each_heavy} × {heavy:.4f} heavy",
    )


def conjugate(lot: Lot) -> None:
    rows = list(lot.values.get("drug_loads") or [])
    found = _included(lot.entity, "conjugate")
    result: float | None = None
    unconjugated: float | None = None
    if rows:
        average, unconjugated, how = dar(rows, chains(found["values"] if found else None))
        if average is None:
            lot.say("warning", f"its DAR isn't worked out: {how}.")
        else:
            result = round(average, 2)
            print(f"{lot.hrid}: DAR {average:.2f}, from {how}")
    lot.set("unconjugated", None if unconjugated is None else round(unconjugated, 2))
    if lot.values.get("dar_method") == "uv_vis":
        # Measured by UV/Vis, its DAR is as entered, whatever a distribution gives.
        lot.set("distribution_dar", None)
    else:
        lot.follows("dar", "distribution_dar", result, "drug load distribution")
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
