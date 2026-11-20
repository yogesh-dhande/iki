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
from typing import Any

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


helm, chemistry, oligos = _modules("helm", "chemistry", "oligos")

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


# A repeated group of units is one position as written (HELM 2.04, 5.3.2): in the first,
# position 4 is the 3′ phosphate after it, as position 9 is once the repeat is written out.
AFTER_A_GROUP = (
    "RNA1{([dR](A)P)'2'.[dR](C)P}|CHEM1{[BHQ1]}$RNA1,CHEM1,4:R2-1:R1$$$V2.0",
    "RNA1{[dR](A)P.[dR](A)P.[dR](C)P}|CHEM1{[BHQ1]}$RNA1,CHEM1,9:R2-1:R1$$$V2.0",
)


@pytest.mark.wasm
def test_a_connection_after_a_repeated_group_joins_where_iki_reads_it() -> None:
    grouped, written_out = AFTER_A_GROUP
    assert iki_helm.canonical(grouped) == iki_helm.canonical(written_out)
    library = chemistry.library()
    built = [chemistry.build(helm.read(t), library) for t in AFTER_A_GROUP]
    assert len({chemistry.formula(b.mol) for b in built}) == 1
    assert len({round(chemistry.average_weight(b.mol), 4) for b in built}) == 1
    # And a position past the polymer's last is refused, as iki-wasm refuses it.
    with pytest.raises(helm.HelmError, match="has 4"):
        helm.read(grouped.replace("4:R2", "10:R2"))


@pytest.mark.parametrize(
    "text", ["RNA1{}$$$$V2.0", "RNA1{R(A)P..R(C)}$$$$V2.0", "RNA1{R(A)P.}$$$$V2.0"]
)
def test_a_polymer_or_unit_without_a_monomer_isnt_helm(text: str) -> None:
    """As iki-wasm refuses them: "Expected a monomer"."""
    with pytest.raises(helm.HelmError, match="no monomer"):
        helm.read(text)


def _computed(text: str, *lab: Any) -> dict[str, Any]:
    """What registering ``text`` computes, with a lab's own monomers given to the run."""
    given = [
        {
            "id": None,
            "values": {
                "polymer_type": m.polymer_type,
                "symbol": m.symbol,
                "smiles": m.smiles,
                "name": m.name,
                "role": m.role,
                "natural_analogue": m.natural_analogue,
            },
        }
        for m in lab
    ]
    values: dict[str, Any] = oligos.computed(text, oligos.Resolver(given))[0]
    return values


#: Ribose with all five carbons ¹³C, and adenine with ¹³C at C8: a lab's labelled monomers.
RIBOSE_13C5 = chemistry.Monomer(
    "RNA",
    "R13C5",
    "O[13C@H]1[13C@H]([OH:3])O[13C@H]([13CH2]O[H:1])[13C@H]1O[H:2]",
    name="Ribose-13C5",
    role="sugar",
    natural_analogue="R",
)
ADENINE_8_13C = chemistry.Monomer(
    "RNA",
    "A8",
    "Nc1ncnc2n([H:1])[13cH]nc12",
    name="Adenine-8-13C",
    role="base",
    natural_analogue="A",
)


def test_a_labelled_atom_keeps_its_isotope_in_the_formula_and_weight() -> None:
    """Uridine-¹³C₅ (ribose-¹³C₅) is C₄¹³C₅H₁₂N₂O₆, 249.16 g/mol, as TLC Pharmaceutical
    Standards (U-0143) and PubChem (CID 76973315) give it; unlabelled uridine is C9H12N2O6,
    244.20. Each ¹³C weighs NIST's 13.00335483507 u: adenosine-8-¹³C is 268.24 g/mol."""
    labelled = _computed("RNA1{[R13C5](U)}$$$$V2.0", RIBOSE_13C5)
    assert labelled["molecular_formula"] == "C4[13C]5H12N2O6"
    assert labelled["molecular_weight"] == pytest.approx(249.16, abs=0.005)
    plain = _computed("RNA1{R(U)}$$$$V2.0")
    assert (plain["molecular_formula"], plain["molecular_weight"]) == ("C9H12N2O6", 244.2)
    adenosine = _computed("RNA1{R([A8])}$$$$V2.0", ADENINE_8_13C)
    assert adenosine["molecular_formula"] == "C9[13C]H13N5O4"
    assert adenosine["molecular_weight"] == pytest.approx(268.24, abs=0.005)
    assert adenosine["monoisotopic_mass"] == pytest.approx(268.1001, abs=0.0001)
    # A duplex's formula adds its strands' isotopes as their own.
    assert chemistry.add_formulas("C4[13C]5H12N2O6", "C9H12N2O6") == "C13[13C]5H24N4O12"
    # Adenine is C5H5N5, 135.13 g/mol; with one carbon ¹³C, 135.13 - 12.011 + 13.00335.
    free = oligos.monomer_values(ADENINE_8_13C)
    assert (free["molecular_formula"], free["molecular_weight"]) == ("C4[13C]H5N5", 136.122)


def test_a_group_not_joined_to_the_strand_isnt_part_of_it() -> None:
    """Adenosine (C10H13N5O4, 267.25 g/mol, PubChem CID 60961) with a 6-FAM written but
    not connected: two molecules, not one oligonucleotide."""
    with pytest.raises(helm.HelmError, match="CHEM1 isn't joined to the strand"):
        _computed("RNA1{R(A)}|CHEM1{[5FAM]}$$$$V2.0")
    joined = _computed("RNA1{R(A)}|CHEM1{[5FAM]}$RNA1,CHEM1,1:R1-1:R1$$$V2.0")
    assert joined["modifications"] == ["5′ 6-FAM (6-carboxyfluorescein on a C6 amino linker)"]


def test_a_ring_has_no_linear_extinction() -> None:
    """A cyclic RNA trinucleotide written from each of its nucleotides is one molecule, of
    one weight; nearest neighbours describe a linear strand's ends, so no ε260."""
    rings = [
        "RNA1{R(A)P.R(C)P.R(G)P}$RNA1,RNA1,1:R1-9:R2$$$V2.0",
        "RNA1{R(C)P.R(G)P.R(A)P}$RNA1,RNA1,1:R1-9:R2$$$V2.0",
    ]
    found = [_computed(r) for r in rings]
    assert {f["molecular_weight"] for f in found} == {979.6}
    for f in found:
        assert "extinction_260" not in f
        assert "ring" in str(f["extinction_note"])


def test_an_abasic_site_is_a_position_without_a_base() -> None:
    """d(A-abasic-T): three nucleotide positions, the second with no base, which nearest
    neighbours don't describe (ATDBio's Nucleic Acids Book, chapter 13); A and T aren't
    neighbours, so neither the AT doublet nor a search for AT fits it."""
    found = _computed("RNA1{[dR](A)P.[dR]P.[dR](T)}$$$$V2.0")
    assert (found["bases"], found["length"]) == ("A-T", 3)
    assert found["modifications"] == ["Abasic site (1)"]
    assert "extinction_260" not in found
    assert "abasic" in str(found["extinction_note"])


def test_a_5_prime_phosphate_is_an_end_not_a_nucleotide() -> None:
    """5′-phosphorylated dAC: two nucleotides, numbered 1 and 2, after a 5′ phosphate."""
    found = _computed("RNA1{P.[dR](A)P.[dR](C)}$$$$V2.0")
    positions = found["positions"]
    assert [(n["position"], n["base"]) for n in positions["nucleotides"]] == [(1, "A"), (2, "C")]
    assert [g["symbol"] for g in positions["ends"]["5′"]] == ["P"]
    assert (found["bases"], found["length"]) == ("AC", 2)


def test_a_phosphorothioate_reads_as_a_phosphate_whatever_its_stereo() -> None:
    """d(TpsT): the nearest-neighbour estimate reads a phosphorothioate as the phosphate
    it replaces, Rp, Sp or undefined: TT is 16,800 L/(mol·cm) (Cantor et al., 1970)."""
    for link in ("sP", "Rsp", "Ssp"):
        found = _computed(f"RNA1{{[dR](T)[{link}].[dR](T)}}$$$$V2.0")
        assert found["extinction_260"] == 16800, link


def test_a_monomers_structure_key_tells_its_free_caps_apart() -> None:
    """A methyl capped with H is methane, CH4; capped with OH, methanol, CH4O."""
    keys = {
        chemistry.structure_key(chemistry.Monomer("CHEM", s, smiles))
        for s, smiles in (("Me", "C[H:1]"), ("MeOH", "C[OH:1]"))
    }
    assert len(keys) == 2


def test_a_self_complementary_strand_is_both_strands_of_its_duplex() -> None:
    """The Dickerson–Drew dodecamer, d(CGCGAATTCGCG), pairs with itself across all 12
    positions as a B-DNA duplex (Drew et al., PNAS 78, 2179, 1981; PDB 1BNA): one
    registered strand in both roles, two copies, so twice its formula and weight."""
    text = "RNA1{" + "P.".join(f"[dR]({b})" for b in "CGCGAATTCGCG") + "}$$$$V2.0"
    strand: dict[str, Any] = {"id": "OLI-1", "hrid": "OLI-000001", "values": _computed(text)}
    assert (strand["values"]["molecular_formula"], strand["values"]["molecular_weight"]) == (
        "C116H147N46O70P11",
        3646.42,
    )
    made = oligos.duplex({"params": {}, "inputs": {"sense": [strand], "antisense": [strand]}})
    (duplex,) = made["creates"]
    assert duplex["values"]["strands"] == [
        {"role": "sense", "strand": "OLI-1"},
        {"role": "antisense", "strand": "OLI-1"},
    ]
    assert (duplex["values"]["duplex_length"], duplex["values"]["mismatches"]) == (12, 0)
    assert duplex["values"]["molecular_formula"] == "C232H294N92O140P22"
    assert duplex["values"]["molecular_weight"] == 7292.84
    assert duplex["derived_from"] == ["OLI-1"]


def test_the_library_galnac_has_l96s_stereochemistry() -> None:
    """L96's three GalNAcs are N-acetyl-β-D-galactosamine and its prolinol is
    trans-4-hydroxy-L-prolinol (EMA's assessment report for Leqvio). Each GalNAc, cut
    from its arm at O1 and capped with a methyl, is methyl 2-acetamido-2-deoxy-β-D-
    galactopyranoside (PubChem CID 6559236); the prolinol, cut from its acyl, is
    (3R,5S)-5-(hydroxymethyl)pyrrolidin-3-ol (CID 15817396); and nothing is left
    undefined."""
    from rdkit import Chem

    galnac = chemistry.free(chemistry.library()[("CHEM", "3GN3C12hp")].smiles)
    assert not [
        s for s in Chem.FindPotentialStereo(galnac) if s.specified != Chem.StereoSpecified.Specified
    ]
    sugar = Chem.MolFromSmarts(
        "[CH2][OX2][CH1]1[OX2][CH1]([CH2][OH])[CH1]([OH])[CH1]([OH])[CH1]1[NH]C(C)=O"
    )
    ring = Chem.MolFromSmarts("C(=O)N1C[CH1](O)C[CH1]1CO")

    def cut(at: tuple[int, int], cap: int) -> str:
        bond = galnac.GetBondBetweenAtoms(*at).GetIdx()
        pieces = Chem.FragmentOnBonds(galnac, [bond], dummyLabels=[(0, 0)])
        piece = min(Chem.GetMolFrags(pieces, asMols=True), key=lambda f: f.GetNumAtoms())
        rw = Chem.RWMol(piece)
        for atom in rw.GetAtoms():
            if atom.GetAtomicNum() == 0:
                atom.SetAtomicNum(cap)
                atom.SetIsotope(0)
        mol = rw.GetMol()
        Chem.SanitizeMol(mol)
        return str(Chem.MolToInchiKey(Chem.RemoveHs(mol)))  # type: ignore[no-untyped-call]

    sugars = [cut((m[0], m[1]), 6) for m in galnac.GetSubstructMatches(sugar)]
    assert sugars == ["ZEVOCXOZYFLVKN-SYHAXYEDSA-N"] * 3
    ((acyl, _, n, *_),) = galnac.GetSubstructMatches(ring)
    assert cut((acyl, n), 1) == "CALDMMCNNFPJSI-CRCLSJGQSA-N"
