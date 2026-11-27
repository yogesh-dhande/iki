"""Register the HELM corresponding to a lab's selected pattern of modifications."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import iki_action as iki
from registration import register

# The sugars a design names, by the library's symbols.
SUGARS = {
    "DNA": "dR",
    "RNA": "R",
    "MOE": "MOE",
    "LNA": "LR",
    "cEt": "cet",
    "2-OMe": "mR",
    "2-F": "fR",
}
LINKERS = {
    "phosphorothioate": "sP",
    "Rp-phosphorothioate": "Rsp",
    "Sp-phosphorothioate": "Ssp",
    "phosphodiester": "P",
}
FIVE_PRIME = {
    "6-FAM": "5FAM",
    "Cy3": "5Cy3",
    "Cy5": "5Cy5",
    "biotin-TEG": "5BioTEG",
    "amino-C6": "nC6o",
    "cholesterol-TEG": "cholTEG",
}
THREE_PRIME = {
    "BHQ-1": "BHQ1",
    "BHQ-2": "BHQ2",
    "cholesterol-TEG": "cholTEG",
    "GalNAc-L96": "3GN3C12hp",
    "C3-spacer": "SpC3",
}


def bracket(symbol: str) -> str:
    return symbol if len(symbol) == 1 else f"[{symbol}]"


def design_helm(params: Mapping[str, Any]) -> str:
    """The HELM of a designed oligonucleotide (see the design action's params)."""
    bases = str(params["bases"]).strip().upper()
    if not bases or any(c not in "ACGTU" for c in bases):
        raise iki.Refused("Its bases are A, C, G, T and U, 5′ to 3′.")
    sugar = SUGARS[params.get("sugar") or "DNA"]
    wings = (params.get("wings") or "").strip()
    sugars = [sugar] * len(bases)
    if wings:
        try:
            five, gap, three = (int(x) for x in wings.split("-"))
        except ValueError:
            raise iki.Refused(
                f"Its wings are three counts, such as 5-10-5, not “{wings}”."
            ) from None
        if five + gap + three != len(bases):
            raise iki.Refused(
                f"A {wings} gapmer has {five + gap + three} nucleotides; its bases are "
                f"{len(bases)}."
            )
        wing = SUGARS[params.get("wing_sugar") or "MOE"]
        sugars = [wing] * five + [sugar] * gap + [wing] * three
    linker = LINKERS[params.get("backbone") or "phosphorothioate"]
    methyl = bool(params.get("methyl_cytosine"))
    five_prime = FIVE_PRIME.get(params.get("five_prime") or "")
    three_prime = THREE_PRIME.get(params.get("three_prime") or "")
    units = []
    for i, (base, s) in enumerate(zip(bases, sugars, strict=True)):
        b = "5meC" if (methyl and base == "C") else base
        last = i == len(bases) - 1
        joiner = "" if last and not three_prime else ("P" if last else bracket(linker))
        units.append(f"{bracket(s)}({bracket(b)}){joiner}")
    if five_prime:
        units.insert(0, "P")
    polymers = ["RNA1{" + ".".join(units) + "}"]
    connections = []
    if five_prime:
        polymers.append("CHEM1{" + bracket(five_prime) + "}")
        connections.append("RNA1,CHEM1,1:R1-1:R1")
    if three_prime:
        n = len(polymers)
        final = 1 + 3 * len(bases) if five_prime else 3 * len(bases)
        polymers.append(f"CHEM{n}{{{bracket(three_prime)}}}")
        connections.append(f"RNA1,CHEM{n},{final}:R2-1:R1")
    return "|".join(polymers) + "$" + "|".join(connections) + "$$$V2.0"


def design(run: iki.Run) -> dict[str, Any]:
    run.params["helm"] = design_helm(run.params)
    return register(run)


if __name__ == "__main__":
    iki.main(design)
