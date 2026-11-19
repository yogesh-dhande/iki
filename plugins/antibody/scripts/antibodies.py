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
variable domain ends:

- an Fv's, scFv's and VHH's chains end at their variable domains (fewer residues after
  them than a constant domain has);
- a Fab's light chain has a constant domain after its variable domain, and its heavy
  chain one (CH1, perhaps with some of the hinge) but no Fc;
- an IgG's light chain has its constant domain, and its heavy chain all of CH1, the
  hinge, CH2 and CH3.

An immunoglobulin constant domain is about 100 residues (human IGKC's is 107, IGHG1's
CH1 98), and an IgG heavy chain's constant region 326 to 377 (CH1, hinge, CH2 and CH3:
IGHG2's to IGHG3's), so fewer than 90 residues after a variable domain isn't a constant
domain, and 300 or more is the whole of an IgG's. An isotype is a heavy chain constant
region's class: an IgG's is one of IgG's four subclasses; a Fab's, any class's; an Fv,
scFv or VHH has none. `other`, for any other format, isn't checked, but its isotype
needs a heavy chain with a constant region.

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

#: Formats with a heavy and a light chain.
PAIRED = {"igg": "A whole antibody (IgG)", "fab": "A Fab", "fv": "An Fv"}
#: Formats with one chain.
SINGLE = {"vhh": "A VHH", "scfv": "An scFv"}
#: Formats with no constant region, so no isotype.
VARIABLE_ONLY = {"fv", "scfv", "vhh"}
#: The isotypes each format with a constant region may have.
ISOTYPES = {
    "igg": {"igg1", "igg2", "igg3", "igg4"},
    "fab": {"igg1", "igg2", "igg3", "igg4", "iga1", "iga2", "igm", "ige", "igd"},
    "other": {"igg1", "igg2", "igg3", "igg4", "iga1", "iga2", "igm", "ige", "igd"},
}
#: Fewer residues than this after a variable domain aren't a constant domain.
CONSTANT_DOMAIN = 90
#: This many residues or more after a heavy chain's variable domain are an IgG's whole
#: constant region: CH1, the hinge, CH2 and CH3.
WHOLE_HEAVY = 300
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
    """That the chains are what the format says they are. Raises :class:`RefusedError`."""
    if form == "other" and not isotype:
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
    heavy_kinds = sorted({d["chain"] for d in heavy_domains})
    after = _after(heavy, heavy_domains)
    if form == "other":
        if after < CONSTANT_DOMAIN:
            raise RefusedError(
                f"{_label(heavy)} ends {after} residues after its variable domain, too few for "
                "a constant region, so it has no isotype: leave it out."
            )
        return
    wanted = {"vhh": ["H"], "scfv": ["H", "K/L"]}.get(form, ["H"])
    kinds = ["K/L" if k in ("K", "L") else k for k in heavy_kinds]
    if sorted(set(kinds)) != wanted or (form == "vhh" and len(heavy_domains) != 1):
        what = {"vhh": "one heavy variable domain", "scfv": "a heavy and a light variable domain"}
        raise RefusedError(
            f"The {'chain' if form in SINGLE else 'heavy chain'} of "
            f"{_noun(form)} has {what.get(form, 'a heavy variable domain')}, and numbering "
            f"found {', '.join(heavy_kinds) or 'none'} in {_label(heavy)}."
        )
    if light is not None:
        light_domains = _domains(light) or []
        if sorted({d["chain"] for d in light_domains} - {"K", "L"}) or not light_domains:
            raise RefusedError(
                f"{_label(light)} isn't a light chain: numbering found "
                f"{', '.join(sorted({d['chain'] for d in light_domains}))} domains in it."
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
    if form in VARIABLE_ONLY and after >= CONSTANT_DOMAIN:
        raise RefusedError(
            f"{_label(heavy)} has {after} residues after its variable domain, a constant "
            f"region, which {_noun(form)} hasn't. Choose fab, igg or other."
        )
    if form == "fab" and not CONSTANT_DOMAIN <= after < WHOLE_HEAVY:
        raise RefusedError(
            f"A Fab's heavy chain has CH1 but no Fc, and {_label(heavy)} has {after} residues "
            "after its variable domain: "
            + ("choose igg for a whole antibody." if after >= WHOLE_HEAVY else "choose fv.")
        )
    if form == "igg" and after < WHOLE_HEAVY:
        raise RefusedError(
            f"A whole antibody's heavy chain has CH1, the hinge, CH2 and CH3, and "
            f"{_label(heavy)} has {after} residues after its variable domain: "
            + ("choose fab." if after >= CONSTANT_DOMAIN else "choose fv.")
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
    isotype, messages = _isotype(form, heavy, isotype)
    name = params.get("name") or (
        f"{_label(heavy)} / {_label(light)}" if light is not None else _label(heavy)
    )
    values: dict[str, Any] = {
        "heavy_chain": heavy["id"],
        "name": name[:MAX_NAME],
        "format": form,
    }
    if light is not None:
        values["light_chain"] = light["id"]
    if isotype:
        values["isotype"] = isotype
    if params.get("target"):
        values["target"] = params["target"]
    derived = [heavy["id"], *([light["id"]] if light is not None else [])]
    create = {"type": "iki/antibody.Antibody", "values": values, "derived_from": derived}
    return {"creates": [create], "messages": messages}


def _isotype(
    form: str, heavy: dict[str, Any], isotype: str | None
) -> tuple[str | None, list[dict[str, str]]]:
    """The isotype, as the heavy chain's constant region says, and what the run says of it.
    Raises :class:`RefusedError` when the one given isn't what the chain is."""
    domains = _domains(heavy)
    if form in VARIABLE_ONLY or domains is None or _after(heavy, domains) < CONSTANT_DOMAIN:
        return isotype, []
    sequence = heavy["values"]["sequence"].removesuffix("*")
    found = constant_regions.call(sequence[max(int(d["end"]) for d in domains) :])
    region = f"{_label(heavy)}'s constant region {found.describe()}"
    names = constant_regions.NAMES
    if not found.isotypes:
        if isotype:
            return isotype, [
                {
                    "level": "warning",
                    "text": f"{region}. Its isotype, {isotype}, is as given, unchecked.",
                }
            ]
        return None, [{"level": "info", "text": f"{region}, so it says no isotype."}]
    allowed = [i for i in found.isotypes if i in ISOTYPES[form]]
    if not allowed:
        raise RefusedError(
            f"{region}: an IgG's isotype is one of IgG's subclasses, so choose other, or check "
            "the chain."
        )
    if isotype and isotype not in found.isotypes:
        given = found.match(isotype)
        raise RefusedError(
            f"{region}. It isn't {names[isotype]}'s ({given.identity:.1%} identical to it): give "
            + " or ".join(allowed)
            + ", or leave the isotype out"
            + (" to take it from the chain." if len(allowed) == 1 else ".")
        )
    if len(found.isotypes) > 1:
        if isotype:
            return isotype, [{"level": "warning", "text": f"{region}. It's given as {isotype}."}]
        return None, [
            {
                "level": "warning",
                "text": f"{region}, so its isotype is left out: give "
                + " or ".join(allowed)
                + ", whichever it's called.",
            }
        ]
    said = "as given" if isotype else "from its heavy chain"
    return found.isotypes[0], [
        {"level": "info", "text": f"Its isotype is {found.isotypes[0]}, {said}: {region}."}
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
