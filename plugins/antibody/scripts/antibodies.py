"""The antibody plugin's own calculations: `assemble`, an antibody from its chains.

It reads the run input from $IKI_INPUT and writes its results to $IKI_OUTPUT, as every
action's command does. A run that can't do what it was asked says why in an `::error`
line and exits 1.

An antibody is its chains: a heavy chain and a light chain for a whole antibody (igg),
one arm of one (fab) or its two variable domains alone (fv), and one chain for a single
domain (vhh) or for both variable domains on one chain (scfv). Its format and isotype
are what its chains are, so the same chains are the same antibody: an IgG1 and an IgG4
of the same variable domains differ in their heavy chains' constant regions, and an IgG
and a Fab in how much of the heavy chain there is. So the format is checked against the
chains, read off their numbering (number_chains), which says where each chain's
variable domain ends, and off the human constant region the heavy chain's matches:

- an Fv's, scFv's and VHH's chains end at their variable domains (fewer residues after
  them than a constant domain has);
- a Fab's light chain has a constant domain after its variable domain, and its heavy
  chain CH1, perhaps with some of the hinge, but no Fc: none of CH2 or CH3;
- an IgG's light chain has its constant domain, and its heavy chain CH1, CH2 and CH3.

Which constant domains a heavy chain has is read off how its residues after the
variable domain align with the human reference they match (constant_regions.py): at
least 10 of a domain's residues are that domain. An immunoglobulin constant domain is
about 100 residues (human IGKC's is 107, IGHG1's CH1 98), so fewer than 90 residues
after a variable domain isn't a constant domain. Where no human reference matches (a
mouse's), the length says the rest: an IgG heavy chain's constant region is 326 to 377
(CH1, hinge, CH2 and CH3: IGHG2's to IGHG3's), so 300 or more is the whole of an IgG's.
An isotype is a heavy chain constant region's class: an IgG's is one of IgG's four
subclasses; a Fab's, any class's; an Fv, scFv or VHH has none. `other`, for any other
format, isn't checked, but its isotype needs a heavy chain with a constant region; one
without numbered variable domains (an Fc, or a receptor's fused to one, which numbering
leaves as it was) is compared whole.

Where the heavy chain has a constant region, it says the isotype: its residues after the
variable domain are compared with IMGT's human constant regions (constant_regions.py).
An isotype given that isn't the one they are is refused; one left out is theirs. A
hybrid's (an IgG2/IgG4, say) may be given as any of its domains' subclasses, and is
left out otherwise, as is one as near two references as each other, saying so; one no
human reference matches, such as a mouse's, keeps the isotype given, unchecked, saying
so.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import constant_regions
import interchain

#: Formats with a heavy and a light chain.
PAIRED = {"igg": "A whole antibody (IgG)", "fab": "A Fab", "fv": "An Fv"}
#: Formats with one chain.
SINGLE = {"vhh": "A VHH", "scfv": "An scFv"}
#: Formats with no constant region, so no isotype.
VARIABLE_ONLY = {"fv", "scfv", "vhh"}
#: The isotypes each format with a constant region may have.
#: IgG's subclasses, whose constant domains' own disulfides EU numbering places.
IGG = ("igg1", "igg2", "igg3", "igg4")
ISOTYPES = {
    "igg": {"igg1", "igg2", "igg3", "igg4"},
    "fab": {"igg1", "igg2", "igg3", "igg4", "iga1", "iga2", "igm", "ige", "igd"},
    "other": {"igg1", "igg2", "igg3", "igg4", "iga1", "iga2", "igm", "ige", "igd"},
}
#: Fewer residues than this after a variable domain aren't a constant domain.
CONSTANT_DOMAIN = 90
#: This many residues or more after a heavy chain's variable domain are an IgG's whole
#: constant region: CH1, the hinge, CH2 and CH3. Only for a constant region no human
#: reference matches: one that matches says which domains it has.
WHOLE_HEAVY = 300
#: A heavy chain constant region's domains, in order; those of its Fc; and those an IgG's
#: heavy chain has all of.
DOMAINS = ("CH1", "hinge", "CH2", "CH3", "CH4")
FC = ("CH2", "CH3", "CH4")
WHOLE = ("CH1", "CH2", "CH3")
MAX_NAME = 255


class RefusedError(Exception):
    """What a run can't do, and why."""


def _label(entity: dict[str, Any]) -> str:
    return str(entity["values"].get("name") or entity["hrid"])


def _noun(form: str) -> str:
    return {"fv": "an Fv", "scfv": "an scFv", "vhh": "a VHH", "fab": "a Fab"}.get(
        form, "a whole antibody (IgG)"
    )


def _domains(chain: dict[str, Any]) -> list[dict[str, Any]] | None:
    found = chain["values"].get("variable_domains")
    return list(found) if found else None


def _after(chain: dict[str, Any], domains: list[dict[str, Any]]) -> int:
    """How many residues a chain has after its last variable domain."""
    length = len(chain["values"]["sequence"].removesuffix("*"))
    return length - max(int(d["end"]) for d in domains)


def _check_chains(
    form: str, heavy: dict[str, Any], light: dict[str, Any] | None, isotype: str | None
) -> None:
    """That the chains are numbered and their variable domains are what the format says.
    Raises :class:`RefusedError`."""
    if form == "other":
        # Only an isotype is checked, and a chain without numbered domains (an Fc, or a
        # receptor's fused to one) is compared whole.
        domains = _domains(heavy)
        if isotype and domains is not None and _after(heavy, domains) < CONSTANT_DOMAIN:
            raise RefusedError(
                f"{_label(heavy)} ends {_after(heavy, domains)} residues after its variable "
                "domain, too few for a constant region, so it has no isotype: leave it out."
            )
        return
    chains = [heavy, *([light] if light is not None else [])]
    unnumbered = [_label(c) for c in chains if _domains(c) is None]
    if unnumbered:
        raise RefusedError(
            f"{' and '.join(unnumbered)} {'has' if len(unnumbered) == 1 else 'have'}n't been "
            "numbered: number the chains first (number_chains), since where their variable "
            "domains end says whether they have constant regions, which the format and "
            "isotype are."
        )
    heavy_domains = _domains(heavy) or []
    # Each domain, counted: a tandem scFv's chain (blinatumomab's) has two heavy and two
    # light domains, a DVD-Ig's heavy chain two heavy ones, and neither is these formats.
    kinds = sorted("K/L" if d["chain"] in ("K", "L") else d["chain"] for d in heavy_domains)
    wanted = ["H", "K/L"] if form == "scfv" else ["H"]
    if kinds != wanted:
        scfv = form == "scfv"
        what = "a heavy and a light variable domain" if scfv else "one heavy variable domain"
        found = ", ".join(d["chain"] for d in heavy_domains) or "none"
        more = len(heavy_domains) > len(wanted)
        raise RefusedError(
            f"The {'chain' if form in SINGLE else 'heavy chain'} of {_noun(form)} has {what}, "
            f"and numbering found {found} in {_label(heavy)}"
            + (
                ", in that order. A chain of more variable domains (a tandem scFv, a "
                "DVD-Ig's) is format other."
                if more
                else "."
            )
        )
    if light is not None:
        light_domains = _domains(light) or []
        if sorted({d["chain"] for d in light_domains} - {"K", "L"}) or not light_domains:
            raise RefusedError(
                f"{_label(light)} isn't a light chain: numbering found "
                f"{', '.join(sorted({d['chain'] for d in light_domains}))} domains in it."
            )
        if len(light_domains) > 1:
            raise RefusedError(
                f"{_label(light)} has {len(light_domains)} light variable domains, and the "
                f"light chain of {_noun(form)} has one. A chain of more (a DVD-Ig's) is "
                "format other."
            )
        light_after = _after(light, light_domains)
        if form in VARIABLE_ONLY and light_after >= CONSTANT_DOMAIN:
            raise RefusedError(
                f"{_label(light)} has {light_after} residues after its variable domain, a "
                "constant domain: an Fv's chains are variable domains alone. Choose fab or igg."
            )
        if form not in VARIABLE_ONLY and light_after < CONSTANT_DOMAIN:
            raise RefusedError(
                f"{PAIRED[form]} has a light chain with a constant domain, and {_label(light)} "
                f"has {light_after} residues after its variable domain: if it's the variable "
                "domains alone, choose fv."
            )
    after = _after(heavy, heavy_domains)
    if form in VARIABLE_ONLY and after >= CONSTANT_DOMAIN:
        raise RefusedError(
            f"{_label(heavy)} has {after} residues after its variable domain, a constant "
            f"region, which {_noun(form)} hasn't. Choose fab, igg or other."
        )
    if form in ("fab", "igg") and after < CONSTANT_DOMAIN:
        raise RefusedError(
            f"{PAIRED[form]}'s heavy chain has a constant region, and {_label(heavy)} has "
            f"{after} residues after its variable domain: choose fv."
        )


def _region(form: str, heavy: dict[str, Any]) -> tuple[constant_regions.Call, bool] | None:
    """What the heavy chain's constant region is, if it may have one, and whether the
    chain was compared whole: its residues after its last variable domain, or, for a
    format `other` chain without numbered domains (an Fc, or a fusion to one), all of it.
    None when it has no constant region."""
    if form in VARIABLE_ONLY:
        return None
    sequence = heavy["values"]["sequence"].removesuffix("*")
    domains = _domains(heavy)
    if domains is None:
        return (constant_regions.call(sequence), True) if form == "other" else None
    if _after(heavy, domains) < CONSTANT_DOMAIN:
        return None
    return constant_regions.call(sequence[max(int(d["end"]) for d in domains) :]), False


def _listed(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _check_domains(
    form: str, heavy: dict[str, Any], region: tuple[constant_regions.Call, bool] | None
) -> None:
    """That a Fab's or an IgG's heavy chain has the constant domains its format says: a
    Fab's CH1, perhaps with some of the hinge, and no Fc (CH2 or CH3); an IgG's CH1, CH2
    and CH3. Read off how the constant region aligns with the human reference it matches,
    and, where none matches (a mouse's), off its length. Raises :class:`RefusedError`."""
    if form not in ("fab", "igg") or region is None:
        return
    found, _ = region
    after = _after(heavy, _domains(heavy) or [])
    if not found.isotypes:
        if form == "fab" and after >= WHOLE_HEAVY:
            raise RefusedError(
                f"A Fab's heavy chain has CH1 but no Fc, and {_label(heavy)} has {after} "
                "residues after its variable domain: choose igg for a whole antibody."
            )
        if form == "igg" and after < WHOLE_HEAVY:
            raise RefusedError(
                f"A whole antibody's heavy chain has CH1, the hinge, CH2 and CH3, and "
                f"{_label(heavy)} has {after} residues after its variable domain: choose fab."
            )
        return
    present = [d for d in DOMAINS if found.has(d)]
    fc = [d for d in present if d in FC]
    whole = all(d in present for d in WHOLE)
    nearest = constant_regions.NAMES[found.matches[0].isotype]
    said = f"{_label(heavy)}'s constant region has {nearest}'s {_listed(present)}"
    if form == "fab" and fc:
        raise RefusedError(
            f"A Fab's heavy chain has CH1, and perhaps some of the hinge, but no Fc, and "
            f"{said}: " + ("choose igg for a whole antibody." if whole else "choose other.")
        )
    if form == "igg" and not whole:
        missing = [d for d in WHOLE if d not in present]
        raise RefusedError(
            f"A whole antibody's heavy chain has CH1, the hinge, CH2 and CH3, and {said} "
            f"but no {_listed(missing)}: " + ("choose other." if fc else "choose fab.")
        )


def assembled(
    heavy: dict[str, Any], light: dict[str, Any] | None, params: dict[str, Any]
) -> dict[str, Any]:
    """The antibody the chains make, as a run's output: its create, and what the run says
    of its isotype. Raises :class:`RefusedError`."""
    form = params.get("format") or "igg"
    isotype = params.get("isotype")
    if light is not None and light["id"] == heavy["id"]:
        raise RefusedError(f"{_label(heavy)} can't be both its heavy and its light chain.")
    if form in PAIRED and light is None:
        raise RefusedError(
            f"{PAIRED[form]} has a light chain: give one, or choose vhh or scfv for an "
            "antibody of one chain."
        )
    if form in SINGLE and light is not None:
        raise RefusedError(f"{SINGLE[form]} has one chain: leave out the light chain.")
    if isotype and form in VARIABLE_ONLY:
        what = SINGLE.get(form) or PAIRED[form]
        raise RefusedError(f"{what} has no constant region, so no isotype: leave it out.")
    if isotype and isotype not in ISOTYPES[form]:
        raise RefusedError(
            f"An IgG's isotype is one of its subclasses (igg1 to igg4), not {isotype}: for a "
            "whole antibody of another class, choose other."
        )
    _check_chains(form, heavy, light, isotype)
    region = _region(form, heavy)
    _check_domains(form, heavy, region)
    isotype, messages = _isotype(form, heavy, isotype, region)
    name = params.get("name") or (
        f"{_label(heavy)} / {_label(light)}" if light is not None else _label(heavy)
    )
    copies = _copies(form, params, region)
    components = [{"part": heavy["id"], "copies": copies, "role": "heavy"}]
    if light is not None:
        components.append({"part": light["id"], "copies": copies, "role": "light"})
    # A subclass places its disulfides only where the chain's constant region was read as
    # that one subclass's: not a hybrid's, nor one no human reference matches.
    read = region is not None and len(region[0].isotypes) == 1
    bonds, said = _bonds(form, heavy, light, isotype if read else None, copies)
    messages += said
    values: dict[str, Any] = {
        "components": components,
        "name": name[:MAX_NAME],
        "format": form,
    }
    if bonds:
        values["bonds"] = bonds
    if isotype:
        values["isotype"] = isotype
    if params.get("target"):
        values["target"] = params["target"]
    derived = [heavy["id"], *([light["id"]] if light is not None else [])]
    create = {"type": "iki/antibody.Antibody", "values": values, "derived_from": derived}
    return {"creates": [create], "messages": messages}


def _copies(
    form: str, params: dict[str, Any], region: tuple[constant_regions.Call, bool] | None
) -> int:
    """How many copies of each chain: an IgG's two, a Fab's, Fv's, scFv's and VHH's one;
    for another format, as given, or two for a chain with an Fc (CH2 and CH3, which pair),
    one without. Raises :class:`RefusedError` for copies its format can't have."""
    given = params.get("copies")
    fixed = {"igg": 2, "fab": 1, "fv": 1, "scfv": 1, "vhh": 1}.get(form)
    if fixed is not None:
        if given is not None and given != fixed:
            raise RefusedError(
                f"{_noun(form)[0].upper()}{_noun(form)[1:]} has {fixed} cop"
                f"{'y' if fixed == 1 else 'ies'} of each chain, not {given}: for another "
                "number, choose other."
            )
        return fixed
    if given is not None:
        return int(given)
    found = region[0] if region is not None else None
    fc = found is not None and bool(found.isotypes) and any(found.has(d) for d in FC)
    return 2 if fc else 1


def _bonds(
    form: str,
    heavy: dict[str, Any],
    light: dict[str, Any] | None,
    isotype: str | None,
    copies: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Its disulfides (interchain.py): those between its chains, as an IgG's subclass places
    them (each light chain to its heavy chain, and, with two heavy chains, those to each
    other), then each domain's own, in each copy. ``isotype`` is the subclass its heavy
    chain's constant region was read as, or None: then only its variable domains' and its
    light chain's constant domain's are given. Between its chains, none for a format
    without a constant region, nor for `other`, whose chains may be joined any way (an Fc
    fusion's, joined at its hinge, which aren't an antibody's)."""
    sequence = _sequence_of(heavy)
    domains = _domains(heavy) or []
    offset = max((int(d["end"]) for d in domains), default=0)
    between: list[interchain.Bond] = []
    unknown = False
    if form in ("igg", "fab"):
        found = interchain.heavy_cysteines(sequence, offset, isotype)
        if found is None:
            unknown = True
        else:
            to_light, to_heavy = found
            if light is not None and to_light is not None:
                partner = interchain.last_cysteine(_sequence_of(light))
                if partner is not None:
                    between += [
                        interchain.Bond(heavy["id"], n, to_light, light["id"], n, partner)
                        for n in range(1, copies + 1)
                    ]
            if copies == 2:
                between += [interchain.Bond(heavy["id"], 1, c, heavy["id"], 2, c) for c in to_heavy]
    pairs = interchain.variable_within(sequence, domains, heavy["values"].get("numbering_scheme"))
    if isotype in IGG:
        # A chain without a variable domain (an Fc fusion's) has an Fc's alone.
        pairs += interchain.heavy_within(sequence, offset, fc_only=not domains)
    own = interchain.within(heavy["id"], pairs, copies)
    if light is not None:
        light_domains = _domains(light) or []
        light_sequence = _sequence_of(light)
        light_pairs = interchain.variable_within(
            light_sequence, light_domains, light["values"].get("numbering_scheme")
        )
        light_end = max((int(d["end"]) for d in light_domains), default=0)
        light_pairs += interchain.light_within(light_sequence, light_end)
        own += interchain.within(light["id"], light_pairs, copies)
    said = []
    if between:
        listed = ", ".join(_bond_text(b, heavy, light) for b in between)
        text = f"Its chains' disulfides, as its subclass places them: {listed}"
    elif unknown:
        why = (
            f"an {constant_regions.NAMES[isotype]}'s take more than one arrangement, or more "
            "than an IgG1's or IgG4's"
            if isotype
            else "its heavy chain's constant region isn't one human subclass's"
        )
        text = (
            f"The disulfides between its chains aren't given: {why}, so give them yourself "
            "if they're known"
        )
    else:
        text = ""
    if own:
        within = f"{len(own)} within its domains, each domain's own"
        text = f"{text}; and {within}" if text else f"Its chains' disulfides: {within}"
    if text:
        said.append({"level": "info", "text": f"{text}."})
    return [b.row() for b in [*between, *own]], said


def _sequence_of(chain: dict[str, Any]) -> str:
    return str(chain["values"]["sequence"]).removesuffix("*").upper()


def _bond_text(bond: interchain.Bond, heavy: dict[str, Any], light: dict[str, Any] | None) -> str:
    def end(part: str, copy: int, residue: int) -> str:
        letter = "H" if part == heavy["id"] else "L"
        return f"{letter}{chr(0x2032) * (copy - 1)}{residue}"

    return (
        f"{end(bond.part, bond.copy, bond.residue)}-"
        f"{end(bond.partner, bond.partner_copy, bond.partner_residue)}"
    )


def _isotype(
    form: str,
    heavy: dict[str, Any],
    isotype: str | None,
    region: tuple[constant_regions.Call, bool] | None,
) -> tuple[str | None, list[dict[str, str]]]:
    """The isotype, as the heavy chain's constant region says, and what the run says of it.
    Raises :class:`RefusedError` when the one given isn't what the chain is."""
    if region is None:
        return isotype, []
    found, whole = region
    what = (
        f"{_label(heavy)}, compared whole as it has no numbered variable domain,"
        if whole
        else f"{_label(heavy)}'s constant region"
    )
    said = f"{what} {found.describe()}"
    names = constant_regions.NAMES
    if not found.isotypes:
        if isotype:
            return isotype, [
                {
                    "level": "warning",
                    "text": f"{said}. Its isotype, {isotype}, is as given, unchecked.",
                }
            ]
        return None, [{"level": "info", "text": f"{said}, so it says no isotype."}]
    allowed = [i for i in found.isotypes if i in ISOTYPES[form]]
    if not allowed:
        raise RefusedError(
            f"{said}: an IgG's isotype is one of IgG's subclasses, so choose other, or check "
            "the chain."
        )
    if isotype and isotype not in found.isotypes:
        given = found.match(isotype)
        raise RefusedError(
            f"{said}. It isn't {names[isotype]}'s ({given.identity:.1%} identical to it): give "
            + " or ".join(allowed)
            + ", or leave the isotype out"
            + (" to take it from the chain." if len(allowed) == 1 else ".")
        )
    if len(found.isotypes) > 1:
        if isotype:
            return isotype, [{"level": "warning", "text": f"{said}. It's given as {isotype}."}]
        return None, [
            {
                "level": "warning",
                "text": f"{said}, so its isotype is left out: give "
                + " or ".join(allowed)
                + ", whichever it's called.",
            }
        ]
    how = "as given" if isotype else "from its heavy chain"
    return found.isotypes[0], [
        {"level": "info", "text": f"Its isotype is {found.isotypes[0]}, {how}: {said}."}
    ]


def assemble(run: dict[str, Any]) -> dict[str, Any]:
    inputs = run["inputs"]
    (heavy,) = inputs["heavy_chain"]
    lights = inputs.get("light_chain") or []
    result = assembled(heavy, lights[0] if lights else None, run.get("params") or {})
    values = result["creates"][0]["values"]
    print(
        f"{values['name']}: {values['format']}"
        + (f", {values['isotype']}" if "isotype" in values else "")
    )
    return result


def main() -> None:
    kind = sys.argv[1]
    compute = {"assemble": assemble}[kind]
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        result = compute(run)
    except RefusedError as why:
        print(f"::error {why}", flush=True)
        sys.exit(1)
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", **result}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
