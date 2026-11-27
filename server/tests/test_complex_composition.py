"""The complex plugin's compute_composition script, beyond its action tests: a pack of as
many complexes as the action batches still writes an output its schema takes, and its
formulas and masses agree with RDKit's and Biopython's, which the plugin doesn't need
but these tests have."""

import importlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from Bio.SeqUtils import molecular_weight
from rdkit import Chem
from rdkit.Chem import rdMolDescriptors as Calc

from iki_spec import schemas

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "complex" / "scripts"
#: The core Complex type's molecular_formula pattern.
FORMULA = yaml.safe_load((ROOT / "spec/src/iki_spec/core/types/complex.yaml").read_text())[
    "fields"
]["molecular_formula"]["pattern"]


def _module(name: str) -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(SCRIPTS))


composition = _module("composition")


def weight(bases: str, **options: bool) -> float:
    """Biopython's mass of a DNA sequence."""
    return float(molecular_weight(bases, "DNA", **options))  # type: ignore[no-untyped-call]


#: The most complexes one run of compute_composition takes (its action's `batchable`).
BATCH = 100
#: Human insulin's chains (UniProt P01308), and its three disulfides.
INSULIN_A = "GIVEQCCTSICSLYQLENYCN"
INSULIN_B = "FVNQHLCGSHLVEALYLVCGERGFFYTPKT"


#: Each part type's roles, as a run's input gives them (ADR 0284): its residues' field
#: and alphabet, and its formula's field.
ROLES: dict[str, dict[str, Any]] = {
    "iki/protein.Protein": {"residues": {"field": "sequence", "alphabet": "protein"}},
    "iki/dna.DNASequence": {"residues": {"field": "bases", "alphabet": "dna"}},
    "iki/oligo.Oligonucleotide": {
        "residues": {"field": "helm", "alphabet": "helm"},
        "formula": "molecular_formula",
    },
    "iki/small_molecule.Compound": {"formula": "molecular_formula"},
    "iki/antibody.Antibody": {"formula": "molecular_formula"},
    "iki/complex.ProteinComplex": {"formula": "molecular_formula"},
}


def with_roles(entity: dict[str, Any]) -> dict[str, Any]:
    """The entity, and those it includes, with their types' roles and lineage."""
    type_ = entity.get("type", "")
    out = {**entity, "roles": ROLES.get(type_, {}), "types": [type_, "iki/core.Entity"]}
    if "included" in entity:
        out["included"] = {k: [with_roles(e) for e in v] for k, v in entity["included"].items()}
    return out


def uuid(n: int) -> str:
    return f"00000000-0000-7000-8003-{n:012d}"


def insulin_with_its_antibody(n: int) -> dict[str, Any]:
    """Insulin bound to a Fab, as each of a pack's complexes: everything a run says of one
    at once (its own disulfides, its parts', a part with no absorbance, a bond it doesn't
    weigh, a part complex whose bonds aren't all counted)."""
    a, b, fab, heme = uuid(10 * n + 1), uuid(10 * n + 2), uuid(10 * n + 3), uuid(10 * n + 4)
    bonds = [
        {"kind": "disulfide", "part": a, "residue": 6, "partner": a, "partner_residue": 11},
        {"kind": "disulfide", "part": a, "residue": 7, "partner": b, "partner_residue": 7},
        {"kind": "disulfide", "part": a, "residue": 20, "partner": b, "partner_residue": 19},
        {"kind": "isopeptide", "part": b, "residue": 29, "partner": a, "partner_residue": 1},
    ]
    return {
        "id": uuid(10 * n),
        "hrid": f"PCX-{n:06d}",
        "type": "iki/complex.ProteinComplex",
        "values": {
            "name": f"insulin with a Fab and a heme, number {n} of a long run of them",
            "components": [{"part": p, "copies": 1} for p in (a, b, fab, heme)],
            "bonds": bonds,
            "bond_map": "incomplete",
        },
        "included": {
            "components.part": [
                {
                    "id": a,
                    "type": "iki/protein.Protein",
                    "values": {"name": "A chain", "sequence": INSULIN_A},
                },
                {
                    "id": b,
                    "type": "iki/protein.Protein",
                    "values": {"name": "B chain", "sequence": INSULIN_B},
                },
                {
                    "id": fab,
                    "type": "iki/antibody.Antibody",
                    "values": {
                        "name": "an anti-insulin Fab",
                        "components": [],
                        "bonds": bonds[:2],
                        "bond_map": "predominant",
                        "molecular_formula": "C2074H3209N555O659S12",
                        "extinction_coefficient_reduced": 71280,
                    },
                },
                {
                    "id": heme,
                    "type": "iki/small_molecule.Compound",
                    "values": {"name": "heme b", "molecular_formula": "C34H32FeN4O4"},
                },
            ]
        },
    }


def run(tmp_path: Path, complexes: list[dict[str, Any]]) -> tuple[int, str, dict[str, Any]]:
    given = tmp_path / "input.json"
    given.write_text(json.dumps({"inputs": {"complex": [with_roles(c) for c in complexes]}}))
    out = tmp_path / "output.json"
    ran = subprocess.run(  # noqa: S603 - the plugin's own script
        [sys.executable, str(SCRIPTS / "composition.py")],
        env={"PATH": os.environ["PATH"], "IKI_INPUT": str(given), "IKI_OUTPUT": str(out)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return ran.returncode, ran.stdout, json.loads(out.read_text()) if out.exists() else {}


def test_a_full_pack_says_everything_in_an_output_its_schema_takes(tmp_path: Path) -> None:
    code, log, output = run(tmp_path, [insulin_with_its_antibody(n) for n in range(BATCH)])
    assert code == 0, log
    assert not list(schemas.check("run-output", output))
    assert len(output["updates"]["complex"]) == BATCH
    # One message for each complex, about it, which says all of it.
    messages = output["messages"]
    assert [m["entity"] for m in messages] == [uuid(10 * n) for n in range(BATCH)]
    first = messages[0]
    assert first["level"] == "warning"
    for said in (
        "Its molar absorbance at 280 nm isn't computed: heme b isn't a protein",
        "With its 3 disulfides: ",
        "Its parts have 2 disulfides of their own (an anti-insulin Fab's 2)",
        "its bonds don't include every bond it has",
        "an anti-insulin Fab's bonds are only its predominant form's",
        "Its isopeptide bonds aren't weighed",
    ):
        assert said in first["text"]
    assert "Its parts have 2 disulfides" in log


def test_a_message_too_long_for_the_output_is_cut_at_a_sentence() -> None:
    sentences = [f"Sentence {n} says something of its complex, at some length." for n in range(60)]
    cut = composition.message(uuid(1), "info", sentences)
    assert len(cut["text"]) <= composition.MAX_TEXT
    assert cut["text"].endswith(". The rest is in the run's log.")
    assert cut["text"].startswith("Sentence 0 says")


def test_a_refused_complex_in_a_pack_is_an_error_about_it(tmp_path: Path) -> None:
    unreadable = insulin_with_its_antibody(1)
    unreadable["included"]["components.part"][0]["values"]["sequence"] = "GIVEQXCTSICSLYQLENYCN"
    code, log, output = run(tmp_path, [insulin_with_its_antibody(0), unreadable])
    assert code == 0, log
    assert not list(schemas.check("run-output", output))
    assert [u["entity"] for u in output["updates"]["complex"]] == [uuid(0)]
    assert output["messages"][-1] == {
        "level": "error",
        "text": "A chain has 'X', an ambiguous or unknown residue, which has no one formula.",
        "entity": uuid(10),
    }


@pytest.mark.parametrize(
    "smiles",
    [
        "[2H]O[2H]",  # heavy water
        "[13CH3]O",
        "OC([2H])([2H])[13CH3]",
        "C[13CH2]C(Cl)([2H])[37Cl]",
        "[18OH2]",
        "[3H]C([3H])([3H])O",
        # Deutetrabenazine (PubChem CID 73442840).
        "[2H]C([2H])([2H])OC1=C(C=C2[C@@H]3CC(=O)[C@H](CN3CCC2=C1)CC(C)C)OC([2H])([2H])[2H]",
        "OC(=O)C(=O)O",  # oxalic acid: carbon, no hydrogen
        "O=C=O",
    ],
)
def test_a_formula_as_rdkit_writes_it_is_read_and_written_back(smiles: str) -> None:
    mol = Chem.MolFromSmiles(smiles)
    formula = Calc.CalcMolFormula(mol, separateIsotopes=True, abbreviateHIsotopes=True)
    atoms = composition.parse(formula)
    assert composition.hill(atoms) == formula
    assert composition.mass(atoms, composition.MONOISOTOPIC) == pytest.approx(
        Calc.CalcExactMolWt(mol), abs=1e-5
    )


def test_hill_order_writes_only_what_it_has() -> None:
    # Oxalic acid, deprotonated twice and written by its atoms: no H0.
    assert composition.hill(Counter({"C": 2, "H": 0, "O": 4})) == "C2O4"
    assert composition.hill(Counter({"Cl": 1, "Na": 1})) == "ClNa"


@pytest.mark.parametrize("formula", ["C2H3O2-", "Zn+2", "H4[15N]+", "C2H5[99Q]"])
def test_a_formula_that_isnt_neutral_or_isnt_weighed_is_refused(formula: str) -> None:
    with pytest.raises((ValueError, KeyError)):
        composition.parse(formula)


@pytest.mark.parametrize(
    ("bases", "strandedness", "topology"),
    [
        ("CGCGAATTCGCG", "double", "linear"),  # the Drew-Dickerson dodecamer (PDB 1BNA)
        ("ACGT", "single", "linear"),
        ("GATTACAGATTACA", "double", "circular"),
    ],
)
def test_a_dna_sequence_weighs_as_biopython_weighs_it(
    bases: str, strandedness: str, topology: str
) -> None:
    part = with_roles(
        {
            "id": uuid(1),
            "type": "iki/dna.DNASequence",
            "values": {"bases": bases, "strandedness": strandedness, "topology": topology},
        }
    )
    formula, chains = composition._dna(part)
    double, circular = strandedness == "double", topology == "circular"
    assert chains == (2 if double else 1)
    assert composition.mass(formula, composition.MONOISOTOPIC) == pytest.approx(
        weight(bases, double_stranded=double, circular=circular, monoisotopic=True),
        abs=1e-4,
    )
    assert composition.mass(formula, composition.AVERAGE) == pytest.approx(
        weight(bases, double_stranded=double, circular=circular), abs=1e-2
    )


#: Uridine labelled in its ribose's five carbons ([1',2',3',4',5'-13C5]uridine), as RDKit
#: writes its formula and as the oligo plugin keeps a labelled nucleoside's.
LABELLED_URIDINE = "O=C1C=CN([13C@@H]2O[13C@H]([13CH2]O)[13C@@H](O)[13C@H]2O)C(=O)N1"


def test_an_isotope_labelled_formula_as_the_oligo_plugin_keeps_it_is_weighed() -> None:
    formula = "C4[13C]5H12N2O6"
    mol = Chem.MolFromSmiles(LABELLED_URIDINE)
    assert Calc.CalcMolFormula(mol, separateIsotopes=True, abbreviateHIsotopes=True) == formula
    atoms = composition.parse(formula)
    assert atoms == Counter({"C": 4, "13C": 5, "H": 12, "N": 2, "O": 6})
    assert composition.hill(atoms) == formula
    assert composition.mass(atoms, composition.MONOISOTOPIC) == pytest.approx(
        Calc.CalcExactMolWt(mol), abs=1e-5
    )
    # As a part of a complex, beside insulin's A chain: its 13C stays 13C in the
    # complex's formula, which the core type's pattern takes, and in its mass.
    chain = {
        "id": uuid(2),
        "type": "iki/protein.Protein",
        "values": {"name": "A chain", "sequence": INSULIN_A},
    }
    labelled = {
        "id": uuid(3),
        "type": "iki/oligo.Oligonucleotide",
        "values": {"name": "13C5-uridine", "molecular_formula": formula},
    }
    made = composition.composition(
        with_roles(
            {
                "id": uuid(1),
                "type": "iki/complex.ProteinComplex",
                "values": {
                    "name": "A chain with a labelled uridine",
                    "components": [{"part": p["id"], "copies": 1} for p in (chain, labelled)],
                    "bonds": [],
                },
                "included": {"components.part": [chain, labelled]},
            }
        )
    )
    values = made["update"]["values"]
    assert "[13C]5" in values["molecular_formula"]
    assert re.fullmatch(FORMULA, values["molecular_formula"])
    assert composition.parse(values["molecular_formula"]) == (
        composition.protein_formula(INSULIN_A) + atoms
    )
    assert values["monoisotopic_mass"] == pytest.approx(
        composition.mass(composition.protein_formula(INSULIN_A), composition.MONOISOTOPIC)
        + Calc.CalcExactMolWt(mol),
        abs=1e-3,
    )
