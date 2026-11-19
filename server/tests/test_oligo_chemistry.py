"""The oligo plugin reads HELM as Iki does, and its chemistry doesn't depend on how HELM
is written (61e; ADR 0144, 0145, 0146).

The plugin's runs read HELM in Python (plugins/oligo/scripts/helm.py), to build each
molecule from its monomers. Iki reads it in iki-wasm, for identities and forms. Both must
see the same monomers at the same positions, or a connection would join the wrong atoms:
each HELM here is read by both and compared, position by position. And one molecule
written two ways, one canonical HELM, must weigh the same.
"""

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

from iki_spec import helm as iki_helm

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "oligo" / "scripts"


def _modules(*names: str) -> tuple[ModuleType, ...]:
    """The plugin's scripts, imported as its runs import them (by bare name), without
    leaving those names to another plugin's scripts of the same name."""
    kept: dict[str, ModuleType] = {
        n: sys.modules.pop(n) for n in ("helm", "chemistry", "oligos") if n in sys.modules
    }
    sys.path.insert(0, str(SCRIPTS))
    try:
        return tuple(importlib.import_module(n) for n in names)
    finally:
        sys.path.remove(str(SCRIPTS))
        for n in ("helm", "chemistry", "oligos"):
            sys.modules.pop(n, None)
        sys.modules.update(kept)


helm, chemistry = _modules("helm", "chemistry")

READ_ALIKE = [
    "RNA1{R(A)P.R(C)P.R(G)}$$$$V2.0",
    "RNA1{[dR](A)[sP].[MOE]([5meC])[sP].[LR](G)}$$$$V2.0",
    "RNA1{P.[dR](A)P.[dR](C)P}|CHEM1{[5FAM]}|CHEM2{[BHQ1]}"
    "$RNA1,CHEM1,1:R1-1:R1|RNA1,CHEM2,7:R2-1:R1$$$V2.0",
    # HELM 1, with its base pairs in its third section.
    "RNA1{R(A)P.R(C)}|RNA2{R(G)P.R(U)}$$RNA1,RNA2,2:pair-5:pair$$",
    'RNA1{[mR](A)[sP].[fR](C)P.[mR](U)}"guide"|CHEM1{[3GN3C12hp]}$RNA1,CHEM1,8:R2-1:R1$$$V2.0',
    "PEPTIDE1{A.G.C}$$$$V2.0",
]


@pytest.mark.wasm
@pytest.mark.parametrize("text", READ_ALIKE)
def test_the_plugin_reads_each_position_as_iki_does(text: str) -> None:
    ours = helm.read(text)
    theirs = iki_helm.read(text)
    assert [p.id for p in ours.polymers] == [p["id"] for p in theirs["polymers"]]
    for mine, given in zip(ours.polymers, theirs["polymers"], strict=True):
        assert mine.type == given["type"]
        assert mine.units == given["units"]
        for monomer in given["monomers"]:
            node = mine.nodes[mine.positions[monomer["position"]]]
            assert (node.id, node.branch_of is not None, node.unit + 1) == (
                monomer["id"],
                monomer["branch"],
                monomer["unit"],
            ), f"{given['id']} position {monomer['position']}"


SAME_MOLECULE = [
    (
        "RNA1{P.[dR](A)P.[dR](C)}|CHEM1{[5BioTEG]}$RNA1,CHEM1,1:R1-1:R1$$$V2.0",
        'CHEM1{[5BioTEG]}|RNA1{[P].[dR]([A])P.[dR](C)}"biotin"$CHEM1,RNA1,1:R1-1:R1$$$V2.0',
    ),
    (
        "RNA1{[dR](T)[sP].[dR](T)[sP].[dR](T)[sP].[dR](G)}$$$$V2.0",
        "RNA1{([dR](T)[sP])'3'.[dR](G)}$$$$V2.0",
    ),
]


@pytest.mark.wasm
@pytest.mark.parametrize(("one", "other"), SAME_MOLECULE)
def test_one_molecule_written_two_ways_weighs_the_same(one: str, other: str) -> None:
    assert iki_helm.canonical(one) == iki_helm.canonical(other)
    library = chemistry.library()
    built = [chemistry.build(helm.read(t), library) for t in (one, other)]
    formulas = {chemistry.formula(b.mol) for b in built}
    weights = {round(chemistry.average_weight(b.mol), 4) for b in built}
    assert len(formulas) == len(weights) == 1


def test_a_strands_bases_are_its_natural_analogues() -> None:
    """5-methylcytosine reads as C, and T on a ribose stays T: the base sequence a search
    finds a modified oligonucleotide by."""
    built = chemistry.build(
        helm.read("RNA1{[MOE]([5meC])[sP].R(T)[sP].[cet](A)P.[fR](U)}$$$$V2.0"),
        chemistry.library(),
    )
    assert chemistry.bases(built) == "CTAU"


def test_the_library_has_one_structure_for_each_symbol() -> None:
    """Every monomer in the library reads, with its attachment points capped H or OH, and
    no two of a polymer type are one structure (the registry's structure key is unique)."""
    library = chemistry.library()
    keys = [chemistry.structure_key(m) for m in library.values()]
    assert len(set(keys)) == len(keys) == 28
    for m in library.values():
        caps = chemistry.structure(m.smiles).caps
        wanted = {"sugar": {1, 2, 3}, "base": {1}, "linker": {1, 2}}.get(m.role, {1})
        assert wanted <= set(caps), m.symbol


def test_phosphorothioate_stereo_reads_as_its_symbol_says() -> None:
    """[Rsp] and [Ssp], from HELMCoreLibrary's molfiles, give the phosphorus RDKit's new
    CIP labeller (Hanson et al., 2018) labels R and S once joined between two nucleosides,
    as in d(TpsT); [sP] gives it none."""
    library = chemistry.library()
    labels = {}
    for link in ("Rsp", "Ssp", "sP"):
        built = chemistry.build(helm.read(f"RNA1{{[dR](T)[{link}].[dR](T)}}$$$$V2.0"), library)
        labels[link] = chemistry.stereo_labels(built.mol)
    assert labels == {"Rsp": ["R"], "Ssp": ["S"], "sP": [""]}
