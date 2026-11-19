"""The antibody plugin's complexes of more than one kind of chain pair: a multispecific
antibody (`assemble_multispecific`) and an Fc fusion protein (`assemble_fc_fusion`).

It reads the run input from $IKI_INPUT and writes its results to $IKI_OUTPUT, as every
action's command does. A run that can't do what it was asked says why in an `::error`
line and exits 1.

A multispecific antibody is IgG-like: two heavy chains, one protein twice or two that
pair at their Fc, and their light chains. Each heavy chain pairs with the light chain
given with it, or both with one light chain (a common light chain, as emicizumab's).
Its binding sites are read off the chains' numbering (number_chains): a heavy chain's
heavy variable domains (VH) pair, in order, with its light chain's light variable
domains (VL), a Fab's one with one, a DVD-Ig's two with two; a VH left over, as a VHH
fused to a heavy chain is, or a VH with a VL beside it on the same chain, an scFv's, is
a site of its own. Each site binds the target given for it, in that order: heavy chain
first, then second heavy chain. Formats whose chains pair otherwise, a 2+1 format's Fd
or a light chain fused to a heavy chain, as tilrekimig's, a CrossMab's or a CODV's, are
registered as they are, with their sites, by the form, the API or a bundle; the type's
rules check them there as here.

An Fc fusion protein is a protein fused to an antibody's Fc: one fusion twice (a
homodimer, as etanercept and aflibercept are), or two chains that pair at their Fc. Its
isotype is what its Fc is, its chain compared whole with IMGT's human constant regions.

Both say what joins their chains (interchain.py): the disulfides an IgG1's or IgG4's
heavy chains make, to their light chains and to each other at the hinge, and how two
different heavy chains are made to pair.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import constant_regions
import interchain
from antibodies import MAX_NAME, RefusedError, _domains, _label, _listed

#: The fewest residues a constant region has: a constant domain's.
CONSTANT_DOMAIN = 90
#: A chain with fewer residues than this before its Fc's hinge is an Fc alone, not a
#: protein fused to one: the hinge's upper residues (IgG1's EPKSCDKTHT) and a tag.
PLAIN_FC = 25
#: How each Fc pairing is written.
PAIRINGS = {
    "knobs_into_holes": "knobs into holes",
    "fab_arm_exchange": "Fab-arm exchange",
    "charge_pairs": "charge pairs",
}
#: The IgG subclasses a multispecific antibody's or an Fc fusion's Fc may have.
IGG = ("igg1", "igg2", "igg3", "igg4")


def _sequence(chain: dict[str, Any]) -> str:
    return str(chain["values"]["sequence"]).removesuffix("*").upper()


def _constant_span(chain: dict[str, Any]) -> tuple[int, int]:
    """Where a heavy chain's constant region is, from 0, end excluded: its longest stretch
    between variable domains, or after its last, which is CH1 to CH3 whatever is fused to
    either end. All of it for a chain without numbered variable domains."""
    sequence = _sequence(chain)
    domains = sorted(_domains(chain) or [], key=lambda d: int(d["start"]))
    if not domains:
        return 0, len(sequence)
    edges = [int(d["end"]) for d in domains]
    starts = [int(d["start"]) - 1 for d in domains[1:]] + [len(sequence)]
    return max(zip(edges, starts, strict=True), key=lambda span: span[1] - span[0])


def _isotype(
    chains: list[dict[str, Any]], given: str | None
) -> tuple[str | None, list[dict[str, str]]]:
    """The heavy chains' subclass, as their constant regions say, and what the run says of
    it. Raises :class:`RefusedError` when they're not an IgG's, differ, or aren't the one
    given."""
    said: list[str] = []
    found: list[tuple[str, ...]] = []
    for chain in chains:
        start, end = _constant_span(chain)
        if end - start < CONSTANT_DOMAIN:
            raise RefusedError(
                f"{_label(chain)} has no constant region, which an IgG-like chain has: "
                f"{end - start} residues outside its variable domains."
            )
        call = constant_regions.call(_sequence(chain)[start:end])
        said.append(f"{_label(chain)}'s constant region {call.describe()}")
        if not call.isotypes:
            found.append(())
            continue
        if not set(call.isotypes) & set(IGG):
            raise RefusedError(f"{said[-1]}: an IgG-like antibody's Fc is an IgG's.")
        found.append(tuple(i for i in call.isotypes if i in IGG))
    text = "; ".join(said)
    if any(not f for f in found):
        if given:
            return given, [
                {
                    "level": "warning",
                    "text": f"{text}. Its isotype, {given}, is as given, unchecked.",
                }
            ]
        return None, [{"level": "info", "text": f"{text}, so it says no isotype."}]
    common = set(found[0]).intersection(*found[1:])
    if not common:
        raise RefusedError(f"{text}: its heavy chains are different subclasses'.")
    if given and given not in common:
        raise RefusedError(
            f"{text}. They aren't {constant_regions.NAMES[given]}'s: give "
            + " or ".join(sorted(common))
            + ", or leave the isotype out."
        )
    if given or len(common) == 1:
        isotype = given or next(iter(common))
        how = "as given" if given else "from its heavy chains"
        return isotype, [{"level": "info", "text": f"Its isotype is {isotype}, {how}: {text}."}]
    return None, [
        {
            "level": "warning",
            "text": f"{text}, so its isotype is left out: give "
            + " or ".join(sorted(common))
            + ".",
        }
    ]


def _pairing(
    first: dict[str, Any], second: dict[str, Any] | None, isotype: str | None
) -> tuple[str | None, list[dict[str, str]]]:
    """How its two Fc chains pair: ``none`` for one chain twice; for two, what their CH3
    domains' mutations say, or None, with a warning, when nothing makes them pair."""
    if second is None:
        return "none", []
    ch3 = [interchain.ch3(_sequence(c), _constant_span(c)[0]) for c in (first, second)]
    pairing, why = interchain.fc_pairing(ch3[0], ch3[1], isotype)
    if pairing is None:
        return None, [
            {
                "level": "warning",
                "text": f"{_label(first)} and {_label(second)} have {why}, so they pair with "
                "themselves as well as with each other: a mixture, unless they're paired "
                "another way.",
            }
        ]
    return pairing, [
        {"level": "info", "text": f"Its Fc chains pair by {PAIRINGS[pairing]}: {why}."}
    ]


def _hinge(
    first: dict[str, Any],
    first_copy: int,
    second: dict[str, Any],
    second_copy: int,
    isotype: str | None,
    *,
    unpaired: bool = False,
) -> list[interchain.Bond]:
    """The disulfides between two Fc chains' hinges, where their subclass's are known. With
    ``unpaired`` (chains without a light chain, an Fc fusion's), an IgG1 hinge's first
    cysteine (EU C220), which in an antibody bonds the light chain, bonds the other chain's,
    as etanercept's three inter-chain disulfides are (Lamanna et al., Sci Rep 7:3951,
    2017), where aflibercept, whose hinge starts after it, has two (KEGG DRUG D09574)."""
    a = interchain.heavy_cysteines(_sequence(first), _constant_span(first)[0], isotype)
    b = interchain.heavy_cysteines(_sequence(second), _constant_span(second)[0], isotype)
    if a is None or b is None or len(a[1]) != len(b[1]):
        return []
    pairs = list(zip(a[1], b[1], strict=True))
    if unpaired and isotype == "igg1" and a[0] is not None and b[0] is not None:
        pairs.insert(0, (a[0], b[0]))
    return [
        interchain.Bond(first["id"], first_copy, x, second["id"], second_copy, y) for x, y in pairs
    ]


def _to_light(
    heavy: dict[str, Any],
    heavy_copy: int,
    light: dict[str, Any],
    light_copy: int,
    isotype: str | None,
) -> list[interchain.Bond]:
    found = interchain.heavy_cysteines(_sequence(heavy), _constant_span(heavy)[0], isotype)
    partner = interchain.last_cysteine(_sequence(light))
    if found is None or found[0] is None or partner is None:
        return []
    return [interchain.Bond(heavy["id"], heavy_copy, found[0], light["id"], light_copy, partner)]


def _kinds(chain: dict[str, Any]) -> list[str]:
    """Its numbered variable domains' kinds, in order: H, or K/L as L."""
    domains = sorted(_domains(chain) or [], key=lambda d: int(d["start"]))
    return ["H" if d["chain"] == "H" else "L" for d in domains]


def _sites(heavy: dict[str, Any], light: dict[str, Any] | None) -> list[tuple[int, str | None]]:
    """A heavy chain's binding sites: each VH's number on it (from 1), and the light chain
    it pairs with, or None for a VH alone or with a VL beside it on the chain (an scFv)."""
    kinds = _kinds(heavy)
    pairs = len([k for k in _kinds(light) if k == "L"]) if light is not None else 0
    out: list[tuple[int, str | None]] = []
    number = 0
    for kind in kinds:
        if kind != "H":
            continue
        number += 1
        if number <= pairs:
            assert light is not None  # noqa: S101 - pairs is 0 without one
            out.append((number, light["id"]))
        else:
            out.append((number, None))
    return out


def _check_numbered(heavies: list[dict[str, Any]], lights: list[dict[str, Any]]) -> None:
    """That every chain is numbered, each heavy chain has a VH and each light chain a VL.
    Raises :class:`RefusedError`."""
    unnumbered = [_label(c) for c in [*heavies, *lights] if _domains(c) is None]
    if unnumbered:
        raise RefusedError(
            f"{_listed(unnumbered)} {'has' if len(unnumbered) == 1 else 'have'}n't been "
            "numbered: number the chains first (number_chains), since their variable domains "
            "are its binding sites."
        )
    for chain in heavies:
        if "H" not in _kinds(chain):
            raise RefusedError(
                f"{_label(chain)} has no heavy variable domain (VH), which a heavy chain has."
            )
    for chain in lights:
        if _kinds(chain) != ["L"]:
            raise RefusedError(
                f"{_label(chain)} isn't a light chain: numbering found "
                f"{', '.join(_kinds(chain))} domains in it, and a light chain has one VL."
            )


def _messages_for(bonds: list[interchain.Bond], names: dict[str, str]) -> list[dict[str, str]]:
    if not bonds:
        return []
    listed = ", ".join(
        f"{names[b.part]}{chr(0x2032) * (b.copy - 1)}{b.residue}-"
        f"{names[b.partner]}{chr(0x2032) * (b.partner_copy - 1)}{b.partner_residue}"
        for b in bonds
    )
    return [
        {"level": "info", "text": f"Its chains' disulfides, as its subclass places them: {listed}."}
    ]


def assembled_multispecific(
    heavies: list[dict[str, Any]], lights: list[dict[str, Any]], params: dict[str, Any]
) -> dict[str, Any]:
    """The multispecific antibody its chains make. Raises :class:`RefusedError`."""
    ids = [c["id"] for c in [*heavies, *lights]]
    if len(set(ids)) != len(ids):
        raise RefusedError("Each chain is given once: a chain twice is one chain, with two copies.")
    if len(lights) > len(heavies):
        raise RefusedError(
            "Each heavy chain pairs with the light chain given with it, or both with one: give "
            "a light chain for each heavy chain at most."
        )
    if len(heavies) == 1 and len(lights) > 1:
        raise RefusedError("One heavy chain, twice, pairs with one light chain.")
    _check_numbered(heavies, lights)
    messages: list[dict[str, str]] = []
    isotype, said = _isotype(heavies, params.get("isotype"))
    messages += said
    pairing, said = _pairing(heavies[0], heavies[1] if len(heavies) > 1 else None, isotype)
    messages += said
    # Each heavy chain's light chain: its own, or the one both share.
    paired = [
        lights[i] if len(lights) == len(heavies) else (lights[0] if lights else None)
        for i in range(len(heavies))
    ]
    sites: list[dict[str, Any]] = []
    for heavy, light in zip(heavies, paired, strict=True):
        for number, partner in _sites(heavy, light):
            site: dict[str, Any] = {"heavy": heavy["id"], "domain": number}
            if partner is not None:
                site["light"] = partner
            sites.append(site)
    targets = [str(t).strip() for t in params.get("targets") or [] if str(t).strip()]
    if len(targets) != len(sites):
        listed = "; ".join(
            f"{_label(next(c for c in heavies if c['id'] == s['heavy']))}'s VH {s['domain']}"
            + (
                f" with {_label(next(c for c in lights if c['id'] == s['light']))}"
                if "light" in s
                else ""
            )
            for s in sites
        )
        plural = "s" if len(sites) != 1 else ""
        raise RefusedError(
            f"Its chains make {len(sites)} binding site{plural} ({listed}): give "
            f"{len(sites)} target{plural}, in that order, not {len(targets)}."
        )
    for site, target in zip(sites, targets, strict=True):
        site["target"] = target
    if len({s["target"] for s in sites}) < 2:
        raise RefusedError(
            f"Its binding sites all bind {targets[0]}: an antibody of one specificity is an "
            "Antibody (assemble)."
        )
    # Copies: two heavy chains in all, and each light chain once per heavy chain it pairs with.
    heavy_copies = 2 if len(heavies) == 1 else 1
    light_copies = 2 if len(lights) == 1 else 1
    components = [{"part": h["id"], "copies": heavy_copies, "role": "heavy"} for h in heavies]
    components += [{"part": lc["id"], "copies": light_copies, "role": "light"} for lc in lights]
    bonds: list[interchain.Bond] = []
    if len(heavies) == 1:
        for n in (1, 2):
            if paired[0] is not None:
                bonds += _to_light(heavies[0], n, paired[0], n, isotype)
        bonds += _hinge(heavies[0], 1, heavies[0], 2, isotype)
    else:
        for index, (heavy, light) in enumerate(zip(heavies, paired, strict=True)):
            if light is not None:
                bonds += _to_light(heavy, 1, light, index + 1 if len(lights) == 1 else 1, isotype)
        bonds += _hinge(heavies[0], 1, heavies[1], 1, isotype)
    # As KEGG DRUG writes them: H and L, and h and l for the second heavy and light chains.
    names = dict(zip([c["id"] for c in heavies], "Hh", strict=False))
    names |= dict(zip([c["id"] for c in lights], "Ll", strict=False))
    messages += _messages_for(bonds, names)
    name = params.get("name") or " / ".join(_label(c) for c in [*heavies, *lights])
    values: dict[str, Any] = {
        "components": components,
        "name": name[:MAX_NAME],
        "binding_sites": sites,
    }
    if bonds:
        values["bonds"] = [b.row() for b in bonds]
    if isotype:
        values["isotype"] = isotype
    if pairing:
        values["fc_pairing"] = pairing
    return {
        "creates": [
            {
                "type": "iki/antibody.MultispecificAntibody",
                "values": values,
                "derived_from": ids,
            }
        ],
        "messages": messages,
    }


def _plain_fc(chain: dict[str, Any]) -> bool:
    """Whether a chain is an Fc alone: its hinge starts near its N-terminus."""
    eu = interchain.eu_numbers(_sequence(chain))
    hinge = [at for number, at in eu.items() if number >= 216]
    return bool(hinge) and min(hinge) < PLAIN_FC


def assembled_fc_fusion(
    first: dict[str, Any], second: dict[str, Any] | None, params: dict[str, Any]
) -> dict[str, Any]:
    """The Fc fusion protein its chains make. Raises :class:`RefusedError`."""
    if second is not None and second["id"] == first["id"]:
        raise RefusedError(
            "A chain twice is one chain, with two copies: leave out the second chain."
        )
    if _plain_fc(first) and (second is None or _plain_fc(second)):
        raise RefusedError(
            f"{_label(first)} is an Fc alone: an Fc fusion's first chain is a protein fused to one."
        )
    chains = [first, *([second] if second is not None else [])]
    messages: list[dict[str, str]] = []
    calls = [constant_regions.call(_sequence(c)) for c in chains]
    for chain, call in zip(chains, calls, strict=True):
        if not call.isotypes or not call.has("CH2") or not call.has("CH3"):
            raise RefusedError(
                f"{_label(chain)}, compared whole, {call.describe()}: an Fc fusion's chains each "
                "have an IgG's Fc (CH2 and CH3)."
            )
    given = params.get("isotype")
    common = {i for i in calls[0].isotypes if i in IGG}.intersection(
        *(set(c.isotypes) for c in calls[1:])
    )
    said = "; ".join(
        f"{_label(c)}, compared whole, {call.describe()}"
        for c, call in zip(chains, calls, strict=True)
    )
    if not common:
        raise RefusedError(f"{said}: its Fc is an IgG's, the same subclass in each chain.")
    if given and given not in common:
        raise RefusedError(
            f"{said}. It isn't {constant_regions.NAMES[given]}'s: give "
            + " or ".join(sorted(common))
            + ", or leave the isotype out."
        )
    isotype = given or (next(iter(common)) if len(common) == 1 else None)
    if isotype:
        how = "as given" if given else "from its Fc"
        messages.append({"level": "info", "text": f"Its isotype is {isotype}, {how}: {said}."})
    else:
        messages.append({"level": "warning", "text": f"{said}, so its isotype is left out."})
    pairing, more = _pairing(first, second, isotype)
    messages += more
    if second is None:
        components = [{"part": first["id"], "copies": 2, "role": "fusion"}]
        bonds = _hinge(first, 1, first, 2, isotype, unpaired=True)
    else:
        role = "fc" if _plain_fc(second) else "fusion"
        components = [
            {"part": first["id"], "copies": 1, "role": "fusion"},
            {"part": second["id"], "copies": 1, "role": role},
        ]
        bonds = _hinge(first, 1, second, 1, isotype, unpaired=True)
    messages += _messages_for(bonds, {first["id"]: "A", **({second["id"]: "B"} if second else {})})
    name = params.get("name") or " / ".join(_label(c) for c in chains)
    values: dict[str, Any] = {"components": components, "name": name[:MAX_NAME]}
    if bonds:
        values["bonds"] = [b.row() for b in bonds]
    if isotype:
        values["isotype"] = isotype
    if pairing:
        values["fc_pairing"] = pairing
    if params.get("target"):
        values["target"] = params["target"]
    return {
        "creates": [
            {
                "type": "iki/antibody.FcFusion",
                "values": values,
                "derived_from": [c["id"] for c in chains],
            }
        ],
        "messages": messages,
    }


def assemble_multispecific(run: dict[str, Any]) -> dict[str, Any]:
    inputs = run["inputs"]
    heavies = [*inputs["heavy_chain"], *(inputs.get("second_heavy_chain") or [])]
    lights = [*(inputs.get("light_chain") or []), *(inputs.get("second_light_chain") or [])]
    result = assembled_multispecific(heavies, lights, run.get("params") or {})
    values = result["creates"][0]["values"]
    print(f"{values['name']}: " + ", ".join(s["target"] for s in values["binding_sites"]))
    return result


def assemble_fc_fusion(run: dict[str, Any]) -> dict[str, Any]:
    inputs = run["inputs"]
    (first,) = inputs["fusion_chain"]
    seconds = inputs.get("second_chain") or []
    result = assembled_fc_fusion(first, seconds[0] if seconds else None, run.get("params") or {})
    print(result["creates"][0]["values"]["name"])
    return result


def main() -> None:
    kind = sys.argv[1]
    compute = {
        "assemble_multispecific": assemble_multispecific,
        "assemble_fc_fusion": assemble_fc_fusion,
    }[kind]
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
