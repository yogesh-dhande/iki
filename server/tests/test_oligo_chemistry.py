"""The oligo plugin reads HELM as Iki does, and its chemistry doesn't depend on how HELM
is written (61e; ADR 0144, 0145, 0146, 0288).

The plugin's runs read HELM with iki-wasm, through the plugin runtime's
``iki_runtime.helm``, as Iki reads it for identities and forms, and build each molecule
from its monomers. What they build from must be the reading's monomers at its positions,
or a connection would join the wrong atoms: each HELM here is read by both and compared,
position by position. And one molecule written two ways, one canonical HELM, must weigh
the same.
"""

import hashlib
import importlib
import sys
from collections import Counter
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
        n: sys.modules.pop(n) for n in ("chemistry", "oligos") if n in sys.modules
    }
    sys.path.insert(0, str(SCRIPTS))
    try:
        return tuple(importlib.import_module(n) for n in names)
    finally:
        sys.path.remove(str(SCRIPTS))
        for n in ("chemistry", "oligos"):
            sys.modules.pop(n, None)
        sys.modules.update(kept)


chemistry, oligos = _modules("chemistry", "oligos")

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
    ours = chemistry.read(text)
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
    built = [chemistry.build(chemistry.read(t), library) for t in (one, other)]
    formulas = {chemistry.formula(b.mol) for b in built}
    weights = {round(chemistry.average_weight(b.mol), 4) for b in built}
    assert len(formulas) == len(weights) == 1


def test_a_strands_bases_are_its_natural_analogues() -> None:
    """5-methylcytosine reads as C, and T on a ribose stays T: the base sequence a search
    finds a modified oligonucleotide by."""
    built = chemistry.build(
        chemistry.read("RNA1{[MOE]([5meC])[sP].R(T)[sP].[cet](A)P.[fR](U)}$$$$V2.0"),
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
        built = chemistry.build(chemistry.read(f"RNA1{{[dR](T)[{link}].[dR](T)}}$$$$V2.0"), library)
        labels[link] = chemistry.stereo_labels(built.mol)
    assert labels == {"Rsp": ["R"], "Ssp": ["S"], "sP": [""]}


#: A CSV file as Excel writes one, lines ending CRLF: two records whose descriptions span
#: two lines and end alike, one with a LINE SEPARATOR (U+2028) in a cell, which isn't a
#: line end in CSV, and one after them.
MULTILINE_CSV = (
    "name,helm,description\r\n"
    'two-line,RNA1{[dR](A)P.[dR](C)}$$$$V2.0,"first line\r\nsame end"\r\n'
    'other two-line,RNA1{[dR](G)P.[dR](T)}$$$$V2.0,"other line\r\nsame end"\r\n'
    "\r\n"
    "separator,RNA1{[dR](A)P.[dR](T)}$$$$V2.0,A\u2028B\r\n"
    "after,RNA1{[dR](C)P.[dR](G)}$$$$V2.0,plain"
)


def test_each_csv_record_is_digested_whole_as_written() -> None:
    """ADR 0052: a create's origin has the SHA-256 of the record it came from, as the file
    has it. Each record here is every line it spans, with its line ends; the blank line
    between two isn't either's. The digests were computed apart from Python, with
    ``printf '…' | shasum -a 256`` of each record's bytes."""
    (oligos,) = _modules("oligos")
    rows = oligos._rows_csv(MULTILINE_CSV)
    assert [r["name"] for r in rows] == ["two-line", "other two-line", "separator", "after"]
    assert rows[0]["description"] == "first line\r\nsame end"
    assert rows[2]["description"] == "A\u2028B"
    assert [hashlib.sha256(r[""].encode()).hexdigest() for r in rows] == [
        "486164aadd808391e7d7d9f12ff93dfe33dd1ec896dc4f114bf1921e7d2f5557",
        "9396ffd5e05d4428713792f3f324d328435a114c2db48555d64c38e3c724820b",
        "f81f2ff0e4aa4802a58b30546a83a85a8de90ea0401c8212ea932bb07556b435",
        # The last record, with no line end after it.
        "8e227151ba2f8fa77cb101bbe58d425e3212ccf8f122b6f5f5a3a17d2f8f5e55",
    ]


FOMIVIRSEN = (
    "RNA1{[dR](G)[sP].[dR](C)[sP].[dR](G)[sP].[dR](T)[sP].[dR](T)[sP].[dR](T)[sP].[dR](G)[sP]."
    "[dR](C)[sP].[dR](T)[sP].[dR](C)[sP].[dR](T)[sP].[dR](T)[sP].[dR](C)[sP].[dR](T)[sP]."
    "[dR](T)[sP].[dR](C)[sP].[dR](T)[sP].[dR](T)[sP].[dR](G)[sP].[dR](C)[sP].[dR](G)}$$$$V2.0"
)
EMPTY_MOLBLOCK = "  Iki\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n"


def test_an_sdf_value_runs_to_its_blank_line() -> None:
    """A data item's value runs to a blank line (CTfile Formats, SDfiles), as the
    small_molecule plugin reads it: a line of it starting with `>` is its value's, so a
    description quoting a HELM header doesn't replace the record's HELM, and a MACCS-II
    field number with no name is passed over with its value."""
    (oligos,) = _modules("oligos")
    text = (
        "fomivirsen\n"
        + EMPTY_MOLBLOCK
        + f"> <HELM>\n{FOMIVIRSEN}\n\n"
        + "> <Description>\nVitravene, against CMV retinitis.\n"
        + "> <HELM> as its label gives it: RNA1{[dR](G)}$$$$V2.0\n\n"
        + "> DT12   55\n> <Name> isn't this record's\n\n"
        + "$$$$\n"
    )
    said: list[str] = []
    (row,) = oligos._rows_sdf(text, said)
    assert row["helm"] == FOMIVIRSEN
    assert row["name"] == "fomivirsen"
    assert row["description"] == (
        "Vitravene, against CMV retinitis.\n> <HELM> as its label gives it: RNA1{[dR](G)}$$$$V2.0"
    )
    assert said == [
        "Record 1: Its Description field has a line, '> <HELM> as its label gives it: "
        "RNA1{[dR](G)}$$$$V2.0', that reads as the start of its HELM field, but an SD field "
        "ends only at a blank line, so the line is part of Description's value and HELM "
        "wasn't read. Put a blank line before it to make it a field of its own."
    ]


def test_an_sdf_records_helm_over_several_lines_is_one_helm() -> None:
    """CTfile Formats allows 200 characters a line, so a long HELM is wrapped: its lines
    are one HELM, even where one starts with HELM's own `$$$$`, which ends a record only
    on a line of its own."""
    (oligos,) = _modules("oligos")
    cut = FOMIVIRSEN.index("$$$$")
    wrapped = f"{FOMIVIRSEN[:100]}\r\n{FOMIVIRSEN[100:cut]}\r\n{FOMIVIRSEN[cut:]}\r\n"
    text = "fomivirsen\r\n" + EMPTY_MOLBLOCK.replace("\n", "\r\n") + "> <HELM>\r\n" + wrapped
    text += "\r\n$$$$  \r\nthe next\r\n" + EMPTY_MOLBLOCK + "> <HELM>\nPEPTIDE1{A.G.C}$$$$V2.0\n"
    rows = oligos._rows_sdf(text, [])
    assert [(r["name"], r["helm"]) for r in rows] == [
        ("fomivirsen", FOMIVIRSEN),
        ("the next", "PEPTIDE1{A.G.C}$$$$V2.0"),
    ]


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
    built = [chemistry.build(chemistry.read(t), library) for t in AFTER_A_GROUP]
    assert len({chemistry.formula(b.mol) for b in built}) == 1
    assert len({round(chemistry.average_weight(b.mol), 4) for b in built}) == 1
    # And a position past the polymer's last is refused, as iki-wasm refuses it.
    with pytest.raises(chemistry.HelmError, match="has 4"):
        chemistry.read(grouped.replace("4:R2", "10:R2"))


@pytest.mark.parametrize(
    "text", ["RNA1{}$$$$V2.0", "RNA1{R(A)P..R(C)}$$$$V2.0", "RNA1{R(A)P.}$$$$V2.0"]
)
def test_a_polymer_or_unit_without_a_monomer_isnt_helm(text: str) -> None:
    """As iki-wasm refuses them: "Expected a monomer"."""
    with pytest.raises(chemistry.HelmError, match="no monomer"):
        chemistry.read(text)


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
    with pytest.raises(chemistry.HelmError, match="CHEM1 isn't joined to the strand"):
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


# --- The sodium salt ---------------------------------------------------------------------

#: Nusinersen (Spinraza): 18 2′-O-methoxyethyl nucleotides, phosphorothioates throughout.
NUSINERSEN = (
    "RNA1{[MOE](T)[sP].[MOE]([5meC])[sP].[MOE](A)[sP].[MOE]([5meC])[sP].[MOE](T)[sP]."
    "[MOE](T)[sP].[MOE](T)[sP].[MOE]([5meC])[sP].[MOE](A)[sP].[MOE](T)[sP].[MOE](A)[sP]."
    "[MOE](A)[sP].[MOE](T)[sP].[MOE](G)[sP].[MOE]([5meC])[sP].[MOE](T)[sP].[MOE](G)[sP]."
    "[MOE](G)}$$$$V2.0"
)
#: A methylphosphonate, a lab's own linker: a phosphate with a methyl for its acidic OH.
METHYLPHOSPHONATE = chemistry.Monomer(
    "RNA",
    "mP",
    "CP([OH:1])([OH:2])=O",
    name="Methylphosphonate",
    role="linker",
    natural_analogue="P",
)


def test_a_sodium_salt_weighs_a_sodium_for_each_acidic_proton() -> None:
    """Nusinersen is marketed as its sodium salt, C234H323N61O128P17S17Na17: 7,501.0
    g/mol, beside 7,127 g/mol as the free acid (EMA's EPAR for Spinraza; PubChem CID
    131801471). Each of its 17 phosphorothioates gives up its proton to a sodium,
    22.990 - 1.008 g/mol more each (CIAAW 2021)."""
    found = _computed(NUSINERSEN)
    assert found["molecular_weight"] == 7127.17
    assert found["molecular_weight_sodium"] == pytest.approx(7127.17 + 17 * 21.982, abs=0.01)
    assert found["molecular_weight_sodium"] == pytest.approx(7501.0, abs=0.5)


@pytest.mark.parametrize(
    ("text", "sodiums"),
    [
        # A 5′ phosphate has two acidic protons, beside its linkage's one.
        ("RNA1{P.[dR](A)P.[dR](C)}$$$$V2.0", 3),
        # Cy3's charge is taken by one phosphate's proton (an inner salt), which leaves
        # one fewer for a sodium: here the one linkage's, so none.
        ("RNA1{[dR](A)P.[dR](C)}|CHEM1{[5Cy3]}$RNA1,CHEM1,1:R1-1:R1$$$V2.0", 0),
    ],
)
def test_its_sodiums_are_its_acidic_protons(text: str, sodiums: int) -> None:
    found = _computed(text)
    if sodiums:
        assert found["molecular_weight_sodium"] == pytest.approx(
            found["molecular_weight"] + sodiums * 21.982, abs=0.01
        )
    else:
        assert "molecular_weight_sodium" not in found


def test_a_molecule_with_no_acidic_proton_has_no_sodium_salt() -> None:
    """A methylphosphonate has a methyl where a phosphate has its acidic OH, so d(ApmC)
    is neutral, as a morpholino's phosphorodiamidates are: no salt, and no salt's weight."""
    found = _computed("RNA1{[dR](A)[mP].[dR](C)}$$$$V2.0", METHYLPHOSPHONATE)
    assert "molecular_weight_sodium" not in found


def test_a_duplexs_sodium_salt_is_its_strands() -> None:
    """Patisiran (Onpattro) is 13,424 Da as the free acid and 14,304 Da as its sodium
    salt, C412H480N148Na40O290P40 (EMA's EPAR for Onpattro, Table 1): its 40
    phosphates' protons, 20 on each strand."""
    sense = {
        "hrid": "OLI-000001",
        "values": {
            "bases": "GUAACCAAGAGUAUUCCAUTT",
            "molecular_weight": 6764.32,
            "molecular_weight_sodium": round(6764.32 + 20 * 21.982, 2),
            "molecular_formula": "C210H268N76O143P20",
        },
    }
    antisense = {
        "hrid": "OLI-000002",
        "values": {
            "bases": "AUGGAAUACUCUUGGUUACTT",
            "molecular_weight": 6660.07,
            "molecular_weight_sodium": round(6660.07 + 20 * 21.982, 2),
            "molecular_formula": "C202H252N72O147P20",
        },
    }
    values = oligos.duplex_values(sense, antisense)
    assert values["molecular_weight"] == 13424.39
    assert values["molecular_weight_sodium"] == pytest.approx(14303.67, abs=0.01)
    assert values["molecular_weight_sodium"] == pytest.approx(14304, abs=0.5)


# --- Vendors' published weights -----------------------------------------------------------
#
# IDT and LGC Biosearch publish, for each modification they sell, the weight it adds to an
# oligonucleotide: "the increase in MW of the oligo upon addition of the modification and
# includes attachment groups used to incorporate the modification into the sequence"
# (IDT, https://www.idtdna.com/site/catalog/modifications), its phosphate among them.
# Each is checked as that: the oligonucleotide with it, less the one without, both
# free acids, as the plugin weighs them.
#
# Tolerance: half the value's last published digit, since it's rounded to it, and 0.01
# g/mol more for the atomic weights it was computed with, which neither vendor states
# (Biosearch's own pages give one unit 0.01 apart: 2'-OMe G is 359.24 on its amidite's
# page and 359.23 on its modification's, BHQ-2 556.47 and 556.46). The plugin's are
# CIAAW's abridged standard atomic weights.

IDT_DEFINITIONS = "https://www.idtdna.com/calc/Analyzer/Home/definitions"
IDT_PRODUCT = "https://www.idtdna.com/site/Catalog/Modifications/Product/{}"
BIOSEARCH_PRODUCT = "https://www.biosearchtech.com/p/{}"
BIOSEARCH_MODIFICATION = "https://oligos.biosearchtech.com/support/resources/oligo-modifications/{}"

_T10 = ["[dR](T)"] * 10


def _weight(text: str) -> float:
    return float(chemistry.average_weight(chemistry.build(chemistry.read(text), LIBRARY).mol))


def _counts(text: str) -> Counter[str]:
    return Counter(chemistry.atom_counts(chemistry.build(chemistry.read(text), LIBRARY).mol))


def _strand(units: list[str], link: str = "P") -> str:
    return "RNA1{" + f"{link}.".join(units) + "}$$$$V2.0"


def _with_5_prime(symbol: str) -> str:
    """dT10 with a modifier on a 5′ phosphate, as a 5′ modification is joined."""
    return "RNA1{P." + "P.".join(_T10) + "}|CHEM1{[" + symbol + "]}$RNA1,CHEM1,1:R1-1:R1$$$V2.0"


def _tolerance(published: str) -> float:
    digits = len(published.partition(".")[2])
    return 0.5 * 10.0**-digits + 0.01


LIBRARY = chemistry.library()

#: (unit as HELM, its published weight as printed, source)
NUCLEOTIDES = [
    # IDT's OligoAnalyzer definitions, residue weights to 0.001 g/mol.
    ("[dR](A)", "313.209", IDT_DEFINITIONS),
    ("[dR](C)", "289.184", IDT_DEFINITIONS),
    ("[dR](G)", "329.208", IDT_DEFINITIONS),
    ("[dR](T)", "304.196", IDT_DEFINITIONS),
    ("R(A)", "329.208", IDT_DEFINITIONS),  # "rA is dA (313.209) + 15.999 = 329.208"
    ("[mR](A)", "343.235", IDT_DEFINITIONS),  # "mA is dA (313.209) + 30.026 = 343.235"
    ("[LR](A)", "341.220", IDT_DEFINITIONS),  # "+A is dA (313.209) + 28.011 = 341.220"
    # IDT's catalog, to 0.1 g/mol: 2′-F, 2′-MOE and 5-methyl-dC, alike at either end or inside.
    ("[fR](A)", "331.2", IDT_PRODUCT.format(2792)),
    ("[fR](C)", "307.2", IDT_PRODUCT.format(2310)),
    ("[fR](G)", "347.2", IDT_PRODUCT.format(2794)),
    ("[fR](U)", "308.2", IDT_PRODUCT.format(2308)),
    ("[MOE](A)", "387.3", IDT_PRODUCT.format(48909)),
    ("[MOE]([5meC])", "377.3", IDT_PRODUCT.format(48910)),
    ("[MOE](G)", "403.3", IDT_PRODUCT.format(48911)),
    ("[MOE](T)", "378.3", IDT_PRODUCT.format(48912)),
    ("[dR]([5meC])", "303.2", IDT_PRODUCT.format(1101)),
    # Biosearch's amidites, each its "Unit Wt.": 2′-OMe (LK2041, LK2043, LK2044, LK2045)
    # and 2′-F (LK2079–LK2082).
    ("[mR](A)", "343.24", BIOSEARCH_PRODUCT.format("NACJ1-005")),
    ("[mR](C)", "319.21", BIOSEARCH_PRODUCT.format("NACJ1-005")),
    ("[mR](G)", "359.24", BIOSEARCH_PRODUCT.format("NACJ1-005")),
    ("[mR](U)", "320.20", BIOSEARCH_PRODUCT.format("NACJ1-005")),
    ("[fR](C)", "307.18", BIOSEARCH_PRODUCT.format("NACJ5-001")),
    ("[fR](U)", "308.16", BIOSEARCH_PRODUCT.format("NACJ5-001")),
    ("[fR](A)", "331.20", BIOSEARCH_PRODUCT.format("NACJ5-001")),
    ("[fR](G)", "347.19", BIOSEARCH_PRODUCT.format("NACJ5-001")),
    # Biosearch's modifications, each "the mass this product adds after conjugation and
    # work-up (the additional mass seen by mass spectrometry)".
    ("[mR](A)", "343.23", BIOSEARCH_MODIFICATION.format("2rsquoomethyl-a")),
    ("[mR](C)", "319.21", BIOSEARCH_MODIFICATION.format("2rsquoomethyl-c")),
    ("[mR](G)", "359.23", BIOSEARCH_MODIFICATION.format("2rsquoomethyl-g")),
    ("[mR](U)", "320.19", BIOSEARCH_MODIFICATION.format("2rsquoomethyl-u")),
    ("[MOE](A)", "387.29", BIOSEARCH_MODIFICATION.format("moe-a")),
    ("[MOE](G)", "403.29", BIOSEARCH_MODIFICATION.format("moe-g")),
    ("[MOE](T)", "378.27", BIOSEARCH_MODIFICATION.format("2-o-moe-thymidine")),
    (
        "[dR]([5meC])",
        "303.21",
        BIOSEARCH_MODIFICATION.format("5-methyl-deoxycytidine-internal-modification"),
    ),
]


@pytest.mark.wasm
@pytest.mark.parametrize(("unit", "published", "source"), NUCLEOTIDES)
def test_a_nucleotide_adds_the_weight_its_vendor_publishes(
    unit: str, published: str, source: str
) -> None:
    """A nucleotide and its phosphate, put in the middle of dT10."""
    added = _weight(_strand([*_T10[:5], unit, *_T10[5:]])) - _weight(_strand(_T10))
    assert added == pytest.approx(float(published), abs=_tolerance(published)), source


MODIFIERS = [
    ("5FAM", "537.5", IDT_PRODUCT.format(1108)),  # /56-FAM/
    ("BHQ1", "554.5", IDT_PRODUCT.format(2584)),  # /5BHQ_1/
    ("BHQ2", "556.5", IDT_PRODUCT.format(2585)),  # /5BHQ_2/
    ("5Cy3", "506.6", IDT_PRODUCT.format(1106)),  # /5Cy3/
    ("5Cy5", "532.6", IDT_PRODUCT.format(1107)),  # /5Cy5/
    ("5BioTEG", "481.5", IDT_PRODUCT.format(8859)),  # /5BiotinTEG/
    ("nC6o", "179.2", IDT_PRODUCT.format(1082)),  # /5AmMC6/
    ("SpC3", "138.1", IDT_PRODUCT.format(1087)),  # /5SpC3/
    ("5FAM", "537.46", BIOSEARCH_PRODUCT.format("NACI1-010")),  # 6-FAM, LK2134
    ("nC6o", "179.16", BIOSEARCH_PRODUCT.format("NACG1-020")),  # LK2123, LK2124
    ("SpC3", "138.06", BIOSEARCH_PRODUCT.format("NACM1-017")),  # LK2113
    ("BHQ1", "554.49", BIOSEARCH_PRODUCT.format("NACI6-001")),  # LK2379
    ("BHQ2", "556.47", BIOSEARCH_PRODUCT.format("NACI6-001")),  # LK2380
    (
        "BHQ1",
        "554.49",
        BIOSEARCH_MODIFICATION.format("bhq1-dark-quencher44-abs-480580-nm-5-modification"),
    ),
    (
        "BHQ2",
        "556.46",
        BIOSEARCH_MODIFICATION.format("bhq2-dark-quencher44-abs-560670-nm-5-modification"),
    ),
    ("nC6o", "179.15", BIOSEARCH_MODIFICATION.format("amino-c6-linker-ahe-x5-modification")),
    ("SpC3", "138.06", BIOSEARCH_MODIFICATION.format("spacer-3-c3-internal-modification")),
]


@pytest.mark.wasm
@pytest.mark.parametrize(("symbol", "published", "source"), MODIFIERS)
def test_a_modification_adds_the_weight_its_vendor_publishes(
    symbol: str, published: str, source: str
) -> None:
    """A modifier on dT10's 5′ end, through the phosphate it's coupled with. Cy3 and Cy5
    are IDT's alone: Biosearch weighs them as the dye's cation with its phosphate
    protonated, 1.008 more (Cy3's unit is 507.59 on its page, NACI2-001), where the plugin,
    as IDT does, weighs the inner salt."""
    added = _weight(_with_5_prime(symbol)) - _weight(_strand(_T10))
    assert added == pytest.approx(float(published), abs=_tolerance(published)), source


@pytest.mark.wasm
@pytest.mark.parametrize(
    ("symbol", "published", "slug"),
    [
        ("BHQ1", "C25H27N6O8P", "bhq1-dark-quencher44-abs-480580-nm-5-modification"),
        ("BHQ2", "C24H25N6O9P", "bhq2-dark-quencher44-abs-560670-nm-5-modification"),
        ("nC6o", "C6H14NO4P", "amino-c6-linker-ahe-x5-modification"),
    ],
)
def test_a_5_prime_modification_adds_the_atoms_biosearch_publishes(
    symbol: str, published: str, slug: str
) -> None:
    """Biosearch's formula for a 5′ modification is what it adds and the 5′ oxygen it's
    joined through: BHQ-1's C25H27N6O8P is 570.49, and the 554.49 it adds is that less
    an oxygen's 16.00 (the same holds on each page here)."""
    added = _counts(_with_5_prime(symbol))
    added.subtract(_counts(_strand(_T10)))
    added["O"] += 1
    assert chemistry.hill(+added) == published, BIOSEARCH_MODIFICATION.format(slug)


@pytest.mark.wasm
def test_a_mixmer_weighs_what_idts_oligoanalyzer_computes() -> None:
    """IDT's whole-oligo weight: "Anhydrous MW = Individual Base MW + Individual Mod MW -
    PO2H + H2 where PO2H = 63.980 and H2 = 2.016", with each phosphorothioate "+ 16.061"
    (IDT_DEFINITIONS). 5′-mA*+A*C*G*mU-3′: 2′-OMe A, LNA A, dC, dG and 2′-OMe U (dU's
    290.169 + 30.026), all phosphorothioates. Tolerance as above, for each of its five
    nucleotides' weights."""
    text = _strand(["[mR](A)", "[LR](A)", "[dR](C)", "[dR](G)", "[mR](U)"], "[sP]")
    idt = 343.235 + 341.220 + 289.184 + 329.208 + (290.169 + 30.026) + 4 * 16.061
    assert _weight(text) == pytest.approx(idt - 63.980 + 2.016, abs=5 * _tolerance("0.001"))


@pytest.mark.wasm
def test_inotersen_is_the_molecule_its_label_describes() -> None:
    """Inotersen (Tegsedi), a 5-10-5 2′-MOE gapmer, every linkage a phosphorothioate.
    FDA's label, section 11, names it in full, "d(P-thio)([2'-O-(2-methoxyethyl)]m5rU-
    [2'-O-(2-methoxyethyl)]m5rC-[…]m5rU-[…]m5rU-[…]rG-G-T-T-A-m5C-A-T-G-A-A-[…]rA-[…]m5rU-
    […]m5rC-[…]m5rC-[…]m5rC)", and gives "inotersen free base" as C230H318N69O121P19S19,
    7183.08, and its sodium salt as C230H299N69Na19O121P19S19, 7600.73 Da
    (https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/8513207e-b55f-417b-9473-af785146a543.xml).
    m5rU is thymine on the sugar; tolerance as above, for each of its 20 nucleotides."""
    moe, d = "[MOE]({})", "[dR]({})"
    units = [
        *(moe.format(b) for b in ("T", "[5meC]", "T", "T", "G")),
        *(d.format(b) for b in ("G", "T", "T", "A", "[5meC]", "A", "T", "G", "A", "A")),
        *(moe.format(b) for b in ("A", "T", "[5meC]", "[5meC]", "[5meC]")),
    ]
    built = chemistry.build(chemistry.read(_strand(units, "[sP]")), LIBRARY)
    assert chemistry.formula(built.mol) == "C230H318N69O121P19S19"
    assert chemistry.average_weight(built.mol) == pytest.approx(7183.08, abs=_tolerance("0.01"))
    sodium = chemistry.sodium_salt_weight(built.mol)
    assert sodium == pytest.approx(7600.73, abs=_tolerance("0.01"))
