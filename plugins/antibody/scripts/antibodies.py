"""The antibody plugin's own calculations: `assemble`, an antibody from its chains.

It reads the run and writes its results with Iki's action helper, iki_action.py. A run
that can't do what it was asked says why in an `::error` line and exits 1.

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
- an IgG's light chain has its constant domain, and its heavy chain all of CH1, CH2 and
  CH3 (at least 90% of each, so one without its C-terminal lysine is whole, and one that
  stops ten residues into CH3 isn't).

Which constant domains a heavy chain has is read off how its residues after the
variable domain align with the human reference nearest them (constant_regions.py): at
least 10 of a domain's residues are some of that domain. That holds for another mammal's
too, which aligns with a human one domain by domain though it's no human isotype (a
mouse's IgG1 is 64% identical to human IgG2's); one less than 50% identical to every
human reference can't be told, so it can't be a Fab or an IgG here, only other. An
immunoglobulin constant domain is about 100 residues (human IGKC's is 107, IGHG1's CH1
98), so fewer than 90 residues after a variable domain isn't a constant domain.
An isotype is a heavy chain constant region's class: an IgG's is one of IgG's four
subclasses; a Fab's, any class's; an Fv, scFv or VHH has none. `other`, for any other
format, isn't checked, but its isotype needs a heavy chain with a constant region; one
without numbered variable domains (an Fc, or a receptor's fused to one, which numbering
leaves as it was) is compared whole.

Where the heavy chain has a constant region, it says the isotype: its residues after the
variable domain are compared with IMGT's human constant regions, and with a rhesus
macaque's (constant_regions.py). An isotype given that isn't the one they are is
refused; one left out is theirs. A hybrid's (an IgG2/IgG4, say) may be given as any of
its domains' subclasses, and is left out otherwise, as is one as near two references as
each other, saying so. A chain that matches a human reference, and is nearer it than any
macaque one, has a human constant region (`constant_species`). One nearer a macaque's is
an Old World monkey's, whose species the references don't tell (a baboon's is as near a
rhesus macaque's as a cynomolgus macaque's is): it's left out, saying so, and given as
macaque, the isotype is read off the macaque references, as IMGT names a macaque's IGHC
genes. One no reference matches, such as a mouse's, keeps the isotype and the species
given, which must name each other (a mouse has IgG2a, IgG2b and IgG2c, not IgG2 or
IgG4): its isotype is then as given, unread, saying so. An isotype given for one without
its species is refused, since a human subclass's name on another species' chain would be
a guess.
"""

from __future__ import annotations

from typing import Any

import constant_regions
import iki_action as iki
import interchain

#: Formats with a heavy and a light chain.
PAIRED = {"igg": "A whole antibody (IgG)", "fab": "A Fab", "fv": "An Fv"}
#: Formats with one chain.
SINGLE = {"vhh": "A VHH", "scfv": "An scFv"}
#: Formats with no constant region, so no isotype.
VARIABLE_ONLY = {"fv", "scfv", "vhh"}
#: The isotypes each format with a constant region may have.
#: Human IgG's subclasses, whose constant domains' own disulfides EU numbering places.
IGG = ("igg1", "igg2", "igg3", "igg4")
#: The isotypes each species' constant regions have, as each names them.
SPECIES = {
    "human": ("igg1", "igg2", "igg3", "igg4", "iga1", "iga2", "igm", "ige", "igd"),
    "macaque": ("igg1", "igg2", "igg3", "igg4", "iga", "igm", "ige", "igd"),
    "mouse": ("igg1", "igg2a", "igg2b", "igg2c", "igg3", "iga", "igm", "ige", "igd"),
    "rat": ("igg1", "igg2a", "igg2b", "igg2c", "iga", "igm", "ige", "igd"),
    "rabbit": ("igg", "iga", "igm", "ige"),
}
#: IgG and its subclasses, in any species.
IGG_CLASS = {"igg1", "igg2", "igg2a", "igg2b", "igg2c", "igg3", "igg4", "igg"}
ALL_ISOTYPES = {isotype for found in SPECIES.values() for isotype in found}
ISOTYPES = {"igg": IGG_CLASS, "fab": ALL_ISOTYPES, "other": ALL_ISOTYPES}
#: The classes whose subunits polymerize, joined by disulfides between their heavy chains,
#: with a J chain in an IgM pentamer or an IgA dimer: how many subunits each makes. An IgM
#: is a pentamer of five subunits or a hexamer of six (Hiramoto et al., Sci Adv
#: 4:eaau1199, 2018), and a hexamer has no J chain (Wiersma et al., J Immunol
#: 160:5979-5989, 1998: pentamers are made with or without one, hexamers without); an IgA
#: is a monomer, or a dimer or higher polymer with its J chain (Kumar et al., Science
#: 367:1008-1014, 2020, whose structures are of dimers, tetramers and pentamers).
POLYMERS = {
    "igm": (1, 5, 6),
    **dict.fromkeys(("iga", "iga1", "iga2"), (1, 2, 3, 4, 5)),
}
#: Fewer residues than this after a variable domain aren't a constant domain.
CONSTANT_DOMAIN = 90
#: A heavy chain constant region's domains, in order; those of its Fc; and those an IgG's
#: heavy chain has all of.
DOMAINS = ("CH1", "hinge", "CH2", "CH3", "CH4")
FC = ("CH2", "CH3", "CH4")
WHOLE = ("CH1", "CH2", "CH3")
MAX_NAME = 255


RefusedError = iki.Refused


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


def _region(
    form: str, heavy: dict[str, Any], species: str | None
) -> tuple[constant_regions.Call, bool] | None:
    """What the heavy chain's constant region is, if it may have one, and whether the
    chain was compared whole: its residues after its last variable domain, or, for a
    format `other` chain without numbered domains (an Fc, or a fusion to one), all of it.
    It's read as the species given's, where there are references for it, and as a
    human's otherwise. None when it has no constant region."""
    if form in VARIABLE_ONLY:
        return None
    as_species = species if species in constant_regions.SPECIES_REFERENCES else "human"
    sequence = heavy["values"]["sequence"].removesuffix("*")
    domains = _domains(heavy)
    if domains is None:
        return (constant_regions.call(sequence, as_species), True) if form == "other" else None
    if _after(heavy, domains) < CONSTANT_DOMAIN:
        return None
    constant = sequence[max(int(d["end"]) for d in domains) :]
    return constant_regions.call(constant, as_species), False


def _listed(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _check_domains(
    form: str, heavy: dict[str, Any], region: tuple[constant_regions.Call, bool] | None
) -> None:
    """That a Fab's or an IgG's heavy chain has the constant domains its format says: a
    Fab's CH1, perhaps with some of the hinge, and no Fc (CH2 or CH3); an IgG's all of
    CH1, CH2 and CH3. Read off how the constant region aligns with the reference nearest
    it, whether or not it's that isotype (a mouse's aligns with a human one domain by
    domain); one too unlike every reference for its domains to be told is refused, as its
    format can't be checked. Raises :class:`RefusedError`."""
    if form not in ("fab", "igg") or region is None:
        return
    found, _ = region
    nearest = found.matches[0]
    name = constant_regions.NAMES[nearest.isotype]
    if not found.homologous:
        raise RefusedError(
            f"{_label(heavy)}'s constant region is too unlike every {found.species} one for "
            "its domains "
            f"to be told (the nearest, {name}'s, is {nearest.identity:.1%} identical), so "
            f"whether it's {_noun(form)}'s can't be checked: choose other, with its copies."
        )
    present = [d for d in DOMAINS if found.has(d)]
    fc = [d for d in present if d in FC]
    said = (
        f"{_label(heavy)}'s constant region has {name}'s {_listed(present)}"
        if found.isotypes
        else f"{_label(heavy)}'s constant region aligns with {name}'s {_listed(present)}, "
        f"the nearest {found.species} one's ({nearest.identity:.1%} identical)"
    )
    if form == "fab" and fc:
        whole = all(found.covers(d) for d in WHOLE)
        raise RefusedError(
            f"A Fab's heavy chain has CH1, and perhaps some of the hinge, but no Fc, and "
            f"{said}: " + ("choose igg for a whole antibody." if whole else "choose other.")
        )
    if form == "igg":
        missing = [d for d in WHOLE if d not in present]
        if missing:
            raise RefusedError(
                f"A whole antibody's heavy chain has CH1, the hinge, CH2 and CH3, and {said} "
                f"but no {_listed(missing)}: " + ("choose other." if fc else "choose fab.")
            )
        partial = [
            f"{has} of {of} {d} residues"
            for d in WHOLE
            if not found.covers(d)
            for has, of in [found.residues(d)]
        ]
        if partial:
            raise RefusedError(
                f"A whole antibody's heavy chain has all of CH1, CH2 and CH3, and "
                f"{_label(heavy)}'s constant region has {_listed(partial)} of {name}'s: if its "
                "sequence is the whole chain, choose other."
            )


def _check_polymer(
    form: str,
    params: dict[str, Any],
    heavy: dict[str, Any],
    light: dict[str, Any] | None,
    j_chain: dict[str, Any] | None,
) -> int:
    """How many subunits it has, each two heavy and two light chains, for an IgM or IgA
    polymer of format other, and that its J chain, if it has one, is one: a protein other
    than its heavy and light chains, not numbered as an antibody's chain. Raises
    :class:`RefusedError`."""
    subunits = int(params.get("subunits") or 1)
    if j_chain is not None and subunits == 1:
        raise RefusedError(
            "A J chain joins a polymer's subunits: give how many it has, 5 for an IgM "
            "pentamer or 2 for an IgA dimer, or leave the J chain out."
        )
    if subunits == 1:
        return 1
    if form != "other":
        raise RefusedError(
            f"An antibody of {subunits} subunits, an IgM's or an IgA's polymer, is format "
            "other: choose other."
        )
    if light is None:
        raise RefusedError(
            "An IgM's or an IgA's subunit is two heavy and two light chains: give its light chain."
        )
    if params.get("copies") is not None:
        raise RefusedError(
            f"Its copies are its {subunits} subunits', two of each chain in each: leave copies out."
        )
    if j_chain is not None:
        if j_chain["id"] in (heavy["id"], light["id"]):
            raise RefusedError(f"{_label(j_chain)} can't be both its J chain and another chain.")
        if _domains(j_chain):
            raise RefusedError(
                f"{_label(j_chain)} has a numbered antibody variable domain, so it's an "
                "antibody's chain, not a J chain: give the J chain."
            )
    return subunits


def _check_polymer_isotype(subunits: int, isotype: str | None, j_chain: bool) -> None:
    """That an antibody of more than one subunit is an IgM's or an IgA's polymer, of a
    number of subunits its class makes. Raises :class:`RefusedError`."""
    if subunits == 1:
        return
    if isotype is None:
        raise RefusedError(
            f"An antibody of {subunits} subunits is an IgM's or an IgA's polymer, and its "
            "isotype wasn't read off its heavy chain: give it, with its constant region's "
            "species if it isn't human."
        )
    made = POLYMERS.get(isotype)
    if made is None:
        raise RefusedError(
            f"Only an IgM's or an IgA's subunits make a polymer, not an "
            f"{constant_regions.NAMES.get(isotype, isotype)}'s: leave subunits out."
        )
    if subunits not in made:
        raise RefusedError(
            f"An {'IgM' if isotype == 'igm' else 'IgA'} has "
            + " or ".join(str(n) for n in made)
            + f" subunits, not {subunits}."
        )
    if isotype == "igm" and subunits == 6 and j_chain:
        raise RefusedError(
            "An IgM hexamer has no J chain, which only a pentamer takes (Wiersma et al., "
            "1998): leave it out, or give 5 subunits."
        )


def assembled(
    heavy: dict[str, Any],
    light: dict[str, Any] | None,
    params: dict[str, Any],
    j_chain: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The antibody the chains make, as a run's output: its create, and what the run says
    of its isotype. Raises :class:`RefusedError`."""
    form = params.get("format") or "igg"
    isotype = params.get("isotype")
    species = params.get("constant_species")
    if light is not None and light["id"] == heavy["id"]:
        raise RefusedError(f"{_label(heavy)} can't be both its heavy and its light chain.")
    if form in PAIRED and light is None:
        raise RefusedError(
            f"{PAIRED[form]} has a light chain: give one, or choose vhh or scfv for an "
            "antibody of one chain."
        )
    if form in SINGLE and light is not None:
        raise RefusedError(f"{SINGLE[form]} has one chain: leave out the light chain.")
    if (isotype or species) and form in VARIABLE_ONLY:
        what = SINGLE.get(form) or PAIRED[form]
        noun = "isotype" if isotype else "constant region species"
        raise RefusedError(f"{what} has no constant region, so no {noun}: leave it out.")
    if isotype and isotype not in ISOTYPES[form]:
        raise RefusedError(
            f"An IgG's isotype is IgG or one of its subclasses, not {isotype}: for a whole "
            "antibody of another class, choose other."
        )
    if isotype and species and isotype not in SPECIES[species]:
        raise RefusedError(
            f"A {species} constant region has no {isotype}: give one of "
            + ", ".join(i for i in SPECIES[species] if i in ISOTYPES[form])
            + "."
        )
    subunits = _check_polymer(form, params, heavy, light, j_chain)
    _check_chains(form, heavy, light, isotype)
    region = _region(form, heavy, species)
    _check_domains(form, heavy, region)
    isotype, species, messages = _isotype(form, heavy, isotype, species, region)
    _check_polymer_isotype(subunits, isotype, j_chain is not None)
    if subunits > 1:
        noun = {2: "dimer", 3: "trimer", 4: "tetramer", 5: "pentamer", 6: "hexamer"}[subunits]
        messages.append(
            {
                "level": "info",
                "text": f"Its chains make {'an IgM' if isotype == 'igm' else 'an IgA'} {noun}: "
                f"{subunits} subunits, {2 * subunits} heavy and {2 * subunits} light chains"
                + (", and its J chain." if j_chain is not None else ", without a J chain."),
            }
        )
    elif isotype == "igm":
        messages.append(
            {
                "level": "info",
                "text": "An IgM is secreted as a pentamer, 10 heavy chains, 10 light chains "
                "and a J chain (Hiramoto et al., Sci Adv 4:eaau1199, 2018), or a hexamer of 12 "
                "of each without one: this is one of its subunits, two of each. For the whole "
                "molecule, choose other and give 5 subunits, with its J chain, or 6.",
            }
        )
    name = params.get("name") or (
        f"{_label(heavy)} / {_label(light)}" if light is not None else _label(heavy)
    )
    copies = 2 * subunits if subunits > 1 else _copies(form, params, region)
    components = [{"part": heavy["id"], "copies": copies, "role": "heavy"}]
    if light is not None:
        components.append({"part": light["id"], "copies": copies, "role": "light"})
    if j_chain is not None:
        components.append({"part": j_chain["id"], "copies": 1, "role": "j_chain"})
    # A subclass places its disulfides only where the chain's constant region was read as
    # that one human subclass's: not a hybrid's, a macaque's (whose hinges are other
    # lengths), nor one no human reference matches.
    read = region is not None and len(region[0].isotypes) == 1 and region[0].species == "human"
    bonds, said, bond_map = _bonds(
        form,
        heavy,
        light,
        isotype if read else None,
        copies,
        polymer=subunits > 1,
        joined=j_chain is not None,
    )
    messages += said
    values: dict[str, Any] = {
        "components": components,
        "name": name[:MAX_NAME],
        "format": form,
        "bond_map": bond_map,
    }
    if bonds:
        # Placed where the format, subclass and numbering put them, not read from
        # this molecule: a registrar records curated or observed ones (ADR 0267).
        values["bonds"] = bonds
        values["bonds_from"] = "pattern"
    if isotype:
        values["isotype"] = isotype
    if species:
        values["constant_species"] = species
    if params.get("target"):
        values["target"] = params["target"]
    derived = [heavy["id"], *([light["id"]] if light is not None else [])]
    derived += [j_chain["id"]] if j_chain is not None else []
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
    # Its CH3 (an IgM's or IgE's CH4) pairs with another chain's: an isolated CH2 is a
    # monomer (Prabakaran et al., Acta Crystallogr D 64:1062-1067, 2008).
    paired = found is not None and found.homologous and (found.has("CH3") or found.has("CH4"))
    return 2 if paired else 1


def _bonds(
    form: str,
    heavy: dict[str, Any],
    light: dict[str, Any] | None,
    isotype: str | None,
    copies: int,
    polymer: bool = False,
    joined: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    """Its disulfides (interchain.py): those between its chains, as an IgG's subclass places
    them (each light chain to its heavy chain, and, with two heavy chains, those to each
    other), then each domain's own, in each copy; what the run says of them; and its
    `bond_map`, how much of what bonds it they are. ``isotype`` is the subclass its heavy
    chain's constant region was read as, or None: then only its variable domains' and its
    light chain's constant domain's are given. Between its chains, none for a format
    without a constant region, nor for `other`, whose chains may be joined any way (an Fc
    fusion's, joined at its hinge, which aren't an antibody's), nor for an IgM's or IgA's
    polymer, whose subunits, and its J chain, are joined by bonds these patterns don't
    place."""
    sequence = _sequence_of(heavy)
    domains = _domains(heavy) or []
    offset = max((int(d["end"]) for d in domains), default=0)
    between: list[interchain.Bond] = []
    # What isn't given, by why: what the run says, and whether its bonds are all it has.
    unknown = ""
    missing: list[str] = []
    predominant = ""
    cl: interchain.Light | None = None
    if light is not None:
        light_domains = _domains(light) or []
        light_end = max((int(d["end"]) for d in light_domains), default=0)
        kind = str(light_domains[0]["chain"]) if light_domains else "K"
        if len(_sequence_of(light)) - light_end >= CONSTANT_DOMAIN:
            cl = interchain.light_cysteines(_sequence_of(light), light_end, kind)
    if form in ("igg", "fab"):
        found = interchain.heavy_cysteines(sequence, offset, isotype)
        if found is None:
            unknown = (
                f"an {constant_regions.NAMES[isotype]}'s take more than one arrangement, or "
                "more than an IgG1's or IgG4's"
                if isotype
                else "its heavy chain's constant region isn't one human subclass's"
            )
        else:
            to_light, to_heavy = found
            partner = cl.to_heavy if cl is not None else None
            if light is not None and to_light is not None:
                if partner is not None:
                    between += [
                        interchain.Bond(heavy["id"], n, to_light, light["id"], n, partner)
                        for n in range(1, copies + 1)
                    ]
                else:
                    missing.append(
                        f"H{to_light}'s to its light chain, which has no cysteine where a "
                        "light chain's bonds its heavy chain, at its constant domain's end"
                    )
            if copies == 2:
                between += [interchain.Bond(heavy["id"], 1, c, heavy["id"], 2, c) for c in to_heavy]
                if isotype == "igg4" and interchain.hinge_228(sequence, offset) == "S":
                    predominant = (
                        "Its hinge is a native IgG4's, S228 (EU), so some of its molecules bond "
                        "it within each heavy chain instead of between them (Angal et al., "
                        "1993; Bloom et al., 1997): its bonds are its predominant form's"
                    )
    elif polymer:
        unknown = "its subunits are joined by bonds between their heavy chains that the "
        unknown += (
            "patterns don't place, as is its J chain, whose own bonds aren't given either"
            if joined
            else "patterns don't place"
        )
    elif form == "other" and (light is not None or copies > 1):
        unknown = "its format is other, whose chains may be joined any way"
    pairs, gone = interchain.variable_within(
        sequence, domains, heavy["values"].get("numbering_scheme")
    )
    missing += [f"its heavy chain's {g}" for g in gone]
    if isotype in IGG:
        # A chain without a variable domain (an Fc fusion's) has an Fc's alone.
        constant, gone = interchain.heavy_within(sequence, offset, fc_only=not domains)
        pairs += constant
        missing += [f"its heavy chain's {g}" for g in gone]
    own = interchain.within(heavy["id"], pairs, copies)
    if light is not None:
        light_pairs, gone = interchain.variable_within(
            _sequence_of(light), _domains(light) or [], light["values"].get("numbering_scheme")
        )
        missing += [f"its light chain's {g}" for g in gone]
        if cl is not None:
            light_pairs += cl.within
            if not cl.within:
                missing.append("its light chain's constant domain's own")
        own += interchain.within(light["id"], light_pairs, copies)
    said = []
    if between:
        listed = ", ".join(_bond_text(b, heavy, light) for b in between)
        text = f"Its chains' disulfides, as its subclass places them: {listed}"
    elif unknown:
        text = (
            f"The disulfides between its chains aren't given: {unknown}, so give them "
            "yourself if they're known"
        )
    else:
        text = ""
    if own:
        within = f"{len(own)} within its domains, each domain's own"
        text = f"{text}; and {within}" if text else f"Its chains' disulfides: {within}"
    if text:
        said.append({"level": "info", "text": f"{text}."})
    if missing:
        said.append(
            {
                "level": "warning",
                "text": "Not given, as a cysteine one needs isn't where its chain's domains "
                f"put it: {'; '.join(missing)}. So its bonds don't include every one it has.",
            }
        )
    if predominant:
        said.append({"level": "warning", "text": f"{predominant}."})
    bond_map = "incomplete" if unknown or missing else "predominant" if predominant else "complete"
    return [b.row() for b in [*between, *own]], said, bond_map


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
    species: str | None,
    region: tuple[constant_regions.Call, bool] | None,
) -> tuple[str | None, str | None, list[dict[str, str]]]:
    """The isotype and the constant region's species, as the heavy chain's constant region
    says, and what the run says of them. Raises :class:`RefusedError` when the ones given
    aren't what the chain is."""
    if region is None:
        return isotype, species, []
    found, whole = region
    what = (
        f"{_label(heavy)}, compared whole as it has no numbered variable domain,"
        if whole
        else f"{_label(heavy)}'s constant region"
    )
    said = f"{what} {found.describe()}"
    names = constant_regions.NAMES
    if not found.isotypes and found.nearer:
        other = found.nearer
        if species:
            fix = "leave the species out" if other == "human" else f"give {other}"
            raise RefusedError(
                f"{said}, so it isn't a {species} constant region: {fix}, or check the chain."
            )
        if isotype:
            raise RefusedError(
                f"{said}. Give its constant region species with its isotype, {isotype}: "
                f"{other} for a rhesus or cynomolgus macaque's, whose isotype is then read "
                "off the chain."
            )
        return (
            None,
            None,
            [
                {
                    "level": "warning",
                    "text": f"{said}. That's an Old World monkey's, whose species these "
                    "references don't tell apart (a baboon's is as near them as a macaque's), "
                    f"so its species and isotype are left out: give {other} for a rhesus or "
                    "cynomolgus macaque's, and its isotype is read off the chain.",
                }
            ],
        )
    if not found.isotypes:
        if species in constant_regions.SPECIES_REFERENCES:
            raise RefusedError(
                f"{said}, so it isn't a {species} constant region: give the species it is, or "
                "check the chain."
            )
        if isotype and not species:
            raise RefusedError(
                f"{said}. Give its constant region species with its isotype, {isotype}, "
                "since a human subclass's name can't be read off it."
            )
        if isotype:
            return (
                isotype,
                species,
                [
                    {
                        "level": "warning",
                        "text": f"{said}. Its isotype, {species} {isotype}, is as given, unread.",
                    }
                ],
            )
        return None, species, [{"level": "info", "text": f"{said}, so it says no isotype."}]
    if species and species != found.species:
        raise RefusedError(
            f"{said}, a {found.species} constant region, not a {species} one: "
            + ("leave the species out" if found.species == "human" else f"give {found.species}")
            + ", or check the chain."
        )
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
            return (
                isotype,
                found.species,
                [{"level": "warning", "text": f"{said}. It's given as {isotype}."}],
            )
        return (
            None,
            found.species,
            [
                {
                    "level": "warning",
                    "text": f"{said}, so its isotype is left out: give "
                    + " or ".join(allowed)
                    + ", whichever it's called.",
                }
            ],
        )
    how = "as given" if isotype else "from its heavy chain"
    return (
        found.isotypes[0],
        found.species,
        [{"level": "info", "text": f"Its isotype is {found.isotypes[0]}, {how}: {said}."}],
    )


def assemble(run: dict[str, Any]) -> dict[str, Any]:
    inputs = run["inputs"]
    (heavy,) = inputs["heavy_chain"]
    lights = inputs.get("light_chain") or []
    joining = inputs.get("j_chain") or []
    result = assembled(
        heavy,
        lights[0] if lights else None,
        run.get("params") or {},
        joining[0] if joining else None,
    )
    values = result["creates"][0]["values"]
    print(
        f"{values['name']}: {values['format']}"
        + (f", {values['isotype']}" if "isotype" in values else "")
    )
    return result


if __name__ == "__main__":
    iki.main({"assemble": assemble})
