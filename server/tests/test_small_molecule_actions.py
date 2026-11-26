"""The small_molecule plugin's actions: registering compounds from SMILES and SDF files,
their descriptors and Molfiles and batches' formula weights, and fetching them from
PubChem and ChEMBL (from fakes of them), generating conformers, and searching them by
similarity and substructure, run as a worker runs them (an input.json in, an output.json
out) or, for the fetches, in-process."""

import hashlib
import importlib
import io
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from iki_spec import schemas

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "small_molecule" / "scripts"
ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
ASPIRIN_KEY = "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"


def entity(i: int, **values: Any) -> dict[str, Any]:
    return {"id": f"00000000-0000-0000-0000-{i:012d}", "hrid": f"X-{i}", "values": values}


def script(
    tmp_path: Path, name: str, run_input: dict[str, Any], *args: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    """Runs one of the plugin's scripts as the worker does: from its own directory, so it
    finds the scripts it shares."""
    (tmp_path / "input.json").write_text(json.dumps(run_input))
    out = tmp_path / "out.json"
    out.unlink(missing_ok=True)
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    result = subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(SCRIPTS / name), *args],
        env={
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(out),
            "IKI_OUT_DIR": str(out_dir),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if not out.exists():
        return result, None
    output: dict[str, Any] = json.loads(out.read_text())
    assert not list(schemas.check("run-output", output))
    return result, output


def last_error(result: subprocess.CompletedProcess[str]) -> str:
    return [line for line in result.stdout.splitlines() if line.startswith("::error ")][-1]


# --- Registering from SMILES ---------------------------------------------------------------------


def registered(tmp_path: Path, smiles: str, **params: Any) -> dict[str, Any]:
    result, output = script(
        tmp_path, "register.py", {"params": {"smiles": smiles, **params}, "inputs": {}}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    return output


def test_registers_each_line_by_its_standard_inchikey(tmp_path: Path) -> None:
    output = registered(
        tmp_path,
        "SMILES Name\n# from the stockroom\n"
        f"{ASPIRIN} aspirin\nOC(=O)c1ccccc1OC(C)=O\tacetylsalicylic acid\n\n"
        "CC(=O)Nc1ccc(O)cc1 paracetamol (acetaminophen)",
    )
    first, second, third = output["creates"]
    assert first == {
        "type": "iki/small_molecule.Compound",
        "key": "line-3",
        "values": {
            "smiles": ASPIRIN,
            "inchikey": ASPIRIN_KEY,
            "inchi": "InChI=1S/C9H8O4/c1-6(10)13-8-5-3-2-4-7(8)9(11)12/h2-5H,1H3,(H,11,12)",
            "name": "aspirin",
        },
    }
    # The same compound drawn another way is the same compound, which Iki reuses.
    assert second["values"]["inchikey"] == ASPIRIN_KEY
    assert second["values"]["smiles"] == ASPIRIN
    assert third["values"]["name"] == "paracetamol (acetaminophen)"
    assert third["values"]["inchikey"] == "RZVAJINKPMORJF-UHFFFAOYSA-N"
    assert output["metrics"] == {"lines": 3, "skipped": 0}


def test_standardises_as_molstandardize_does(tmp_path: Path) -> None:
    # A nitro group drawn with five bonds to N, and a sodium bonded to an oxygen.
    output = registered(tmp_path, "CN(=O)=O\nCC(=O)O[Na]")
    nitro, sodium = (c["values"] for c in output["creates"])
    assert nitro["smiles"] == "C[N+](=O)[O-]"
    # Bonds to metals are broken: the salt as drawn, whole, without the parent.
    assert sodium["smiles"] == "CC(=O)[O-].[Na+]"


def test_the_parent_takes_salts_and_solvents_off_and_batches_record_them(
    tmp_path: Path,
) -> None:
    output = registered(
        tmp_path,
        f"{ASPIRIN} aspirin\n"
        "CC(=O)Oc1ccccc1C(=O)[O-].[Na+] aspirin sodium\n"
        "[Ca+2].CC(=O)Oc1ccccc1C(=O)[O-].CC(=O)Oc1ccccc1C(=O)[O-] calcium aspirin\n"
        "CN(C)CCC(c1ccccc1)c1ccccn1.Cl.O.O pheniramine hydrochloride dihydrate",
        parent=True,
        batches=True,
    )
    creates = output["creates"]
    compounds = [c for c in creates if c["type"] == "iki/small_molecule.Compound"]
    batches = [c for c in creates if c["type"] == "iki/small_molecule.CompoundBatch"]
    assert [c["values"]["inchikey"] for c in compounds[:3]] == [ASPIRIN_KEY] * 3
    assert compounds[3]["values"]["smiles"] == "CN(C)CCC(c1ccccc1)c1ccccn1"
    # Each batch refers to its line's compound, made with it (or the one reused).
    assert [b["values"]["compound"] for b in batches] == [{"$create": c["key"]} for c in compounds]
    free, sodium, calcium, pheniramine = (b["values"] for b in batches)
    assert free == {
        "compound": {"$create": "line-1"},
        "name": "aspirin",
        "structure_as_drawn": ASPIRIN,
        "formula_weight": 180.159,
    }
    # The salt's name is its batch's: its compound is aspirin, not aspirin sodium.
    assert [c["values"].get("name") for c in compounds] == ["aspirin", None, None, None]
    assert sodium == {
        "compound": {"$create": "line-2"},
        "name": "aspirin sodium",
        "structure_as_drawn": "CC(=O)Oc1ccccc1C(=O)[O-].[Na+]",
        "salt": "sodium",
        "salt_equivalents": 1.0,
        "formula_weight": 202.141,
    }
    # Two aspirins to one calcium: half a calcium each, in place of its acid's hydrogen.
    assert (calcium["salt"], calcium["salt_equivalents"]) == ("calcium", 0.5)
    assert calcium["formula_weight"] == pytest.approx(180.159 + (40.078 - 2 * 1.008) / 2, abs=1e-3)
    assert pheniramine["salt"] == "hydrochloride"
    assert (pheniramine["solvate"], pheniramine["solvate_equivalents"]) == ("water", 2.0)
    assert pheniramine["formula_weight"] == pytest.approx(240.35 + 36.461 + 2 * 18.015, abs=0.01)


def test_a_batchs_formula_weight_is_its_drawn_materials(tmp_path: Path) -> None:
    output = registered(
        tmp_path,
        # Choline chloride: a quaternary ammonium keeps its charge, so the salt adds Cl, not
        # HCl. C5H14ClNO is 139.62 g/mol (PubChem CID 6209).
        "C[N+](C)(C)CCO.[Cl-] choline chloride\n"
        # Methylamine citrate, three to one: a third of a citrate each, kept whole. The
        # drawn C9H23N3O7 is 285.30 g/mol, so 95.099 for each methylamine.
        "CN.CN.CN.OC(=O)CC(O)(CC(=O)O)C(=O)O methylamine citrate",
        parent=True,
        batches=True,
    )
    choline, citrate = (
        c["values"] for c in output["creates"] if c["type"] == "iki/small_molecule.CompoundBatch"
    )
    assert (choline["salt"], choline["formula_weight"]) == ("hydrochloride", 139.626)
    assert citrate["salt_equivalents"] == pytest.approx(1 / 3, rel=1e-15)
    # 12.011·9 + 1.008·23 + 14.007·3 + 15.999·7 = 285.297, IUPAC's abridged weights.
    assert citrate["formula_weight"] == pytest.approx(285.297 / 3, abs=1e-3)
    assert not [m for m in output["messages"] if "formula weight" in m["text"]]


def test_a_drawing_whose_charges_dont_balance_says_so(tmp_path: Path) -> None:
    # An amine with a chloride ion but no proton: 1.008 g/mol less than its hydrochloride.
    output = registered(tmp_path, "CN(C)CCC(c1ccccc1)c1ccccn1.[Cl-]", parent=True, batches=True)
    assert output["messages"] == [
        {
            "level": "warning",
            "text": "Line 1: Its formula weight from its parent, salt and solvent is 276.811 "
            "g/mol, but its structure as drawn weighs 275.803 g/mol for each parent: its "
            "charges may not balance.",
        }
    ]


#: Sunitinib, as PubChem gives it (CID 5329102): C22H27FN4O2, 398.48 g/mol.
SUNITINIB = r"CCN(CC)CCNC(=O)C1=C(NC(=C1C)/C=C\2/C3=C(C=CC(=C3)F)NC2=O)C"
#: Metoprolol, as PubChem gives it in its tartrate (CID 441308).
METOPROLOL = "CC(C)NCC(COC1=CC=C(C=C1)CCOC)O"
#: Hydroxyzine pamoate, as PubChem gives it (CID 25096): C44H43ClN2O8, 763.3 g/mol.
#: Pamoate isn't a salt the batch type has.
HYDROXYZINE_PAMOATE = (
    "C1CN(CCN1CCOCCO)C(C2=CC=CC=C2)C3=CC=C(C=C3)Cl."
    "C1=CC=C2C(=C1)C=C(C(=C2CC3=C(C(=CC4=CC=CC=C43)C(=O)O)O)O)C(=O)O"
)
PAMOIC_ACID = "O=C(O)c1cc2ccccc2c(Cc2c(O)c(C(=O)O)cc3ccccc23)c1O"
HYDROXYZINE = "OCCOCCN1CCN(C(c2ccccc2)c2ccc(Cl)cc2)CC1"


def test_a_salt_is_known_whatever_stereochemistry_its_drawn_with(tmp_path: Path) -> None:
    output = registered(
        tmp_path,
        # Sunitinib (S)-malate, as PubChem gives it (CID 6456015): C26H33FN4O7, 532.6 g/mol.
        f"{SUNITINIB}.C([C@@H](C(=O)O)O)C(=O)O sunitinib malate\n"
        # Milrinone lactate (CID 172293, C15H15N3O4, 301.30 g/mol), its lactate drawn (S).
        "CC1=C(C=C(C(=O)N1)C#N)C2=CC=NC=C2.C[C@H](O)C(=O)O milrinone lactate\n"
        # Metoprolol tartrate, two to one, as PubChem gives it (CID 441308: C34H56N2O12,
        # 684.8 g/mol), with L-tartrate; then with D- and with meso-tartrate.
        f"{METOPROLOL}.{METOPROLOL}.[C@@H]([C@H](C(=O)O)O)(C(=O)O)O metoprolol tartrate\n"
        f"{METOPROLOL}.{METOPROLOL}.O=C(O)[C@@H](O)[C@H](O)C(=O)O metoprolol D-tartrate\n"
        f"{METOPROLOL}.{METOPROLOL}.O=C(O)[C@@H](O)[C@@H](O)C(=O)O metoprolol meso-tartrate\n"
        # Ondansetron hydrochloride dihydrate (CID 59774): C18H24ClN3O3, 365.9 g/mol.
        "CC1=NC=CN1CC2CCC3=C(C2=O)C4=CC=CC=C4N3C.O.O.Cl ondansetron hydrochloride dihydrate\n"
        # Sertraline hydrochloride (CID 63009): C17H18Cl3N, 342.7 g/mol.
        "CN[C@H]1CC[C@H](C2=CC=CC=C12)C3=CC(=C(C=C3)Cl)Cl.Cl sertraline hydrochloride",
        parent=True,
        batches=True,
    )
    compounds = [c for c in output["creates"] if c["type"] == "iki/small_molecule.Compound"]
    batches = [
        c["values"] for c in output["creates"] if c["type"] == "iki/small_molecule.CompoundBatch"
    ]
    forms = [
        (
            b.get("salt"),
            b.get("salt_equivalents"),
            b.get("solvate"),
            b.get("solvate_equivalents"),
            b.get("formula_weight"),
        )
        for b in batches
    ]
    # Each is what RDKit weighs the whole drawing at, for each parent, from IUPAC's
    # standard atomic weights: PubChem's, to its one decimal.
    assert forms == [
        ("malate", 1.0, None, None, 532.569),
        ("lactate", 1.0, None, None, 301.302),
        ("tartrate", 0.5, None, None, 342.412),
        ("tartrate", 0.5, None, None, 342.412),
        ("tartrate", 0.5, None, None, 342.412),
        ("hydrochloride", 1.0, "water", 2.0, 365.861),
        ("hydrochloride", 1.0, None, None, 342.697),
    ]
    # The parent is the free base: sunitinib, not its malate.
    assert compounds[0]["values"]["inchikey"] == "WINHZLLDWRZWRT-ATVHPVEESA-N"
    # The three metoprolol tartrates are batches of one compound, each kept as drawn.
    assert len({c["values"]["inchikey"] for c in compounds[2:5]}) == 1
    assert [b["structure_as_drawn"].split(".")[-1] for b in batches[2:5]] == [
        "O=C(O)[C@H](O)[C@@H](O)C(=O)O",
        "O=C(O)[C@@H](O)[C@H](O)C(=O)O",
        "O=C(O)[C@@H](O)[C@@H](O)C(=O)O",
    ]
    assert batches[0]["structure_as_drawn"].endswith(".O=C(O)C[C@H](O)C(=O)O")
    said = [m["text"] for m in output["messages"]]
    assert not [t for t in said if "formula weight" in t or "other parts" in t], said


def test_a_part_iki_doesnt_know_leaves_its_batch_without_a_formula_weight(
    tmp_path: Path,
) -> None:
    output = registered(tmp_path, f"{HYDROXYZINE_PAMOATE} hydroxyzine pamoate", parent=True)
    # Without batches, there's no formula weight to leave out.
    assert [m["text"] for m in output["messages"]] == [
        "Line 1: It has other parts, which aren't salts or solvents Iki knows, so only its "
        f"largest part was kept: {PAMOIC_ACID} left out.",
        "Line 1: Its name, hydroxyzine pamoate, is its structure's as drawn, not its "
        "parent's, so its compound wasn't given it.",
    ]
    output = registered(
        tmp_path, f"{HYDROXYZINE_PAMOATE} hydroxyzine pamoate", parent=True, batches=True
    )
    compound, batch = output["creates"]
    assert compound["values"]["smiles"] == HYDROXYZINE
    # Hydroxyzine's 374.91 g/mol would be half the material's: no weight is given at all.
    assert batch["values"] == {
        "compound": {"$create": "line-1"},
        "name": "hydroxyzine pamoate",
        "structure_as_drawn": f"{PAMOIC_ACID}.{HYDROXYZINE}",
    }
    assert {
        "level": "warning",
        "text": f"Line 1: Its batch has no formula weight: {PAMOIC_ACID} is part of the "
        "material, but not a salt or solvent Iki knows, so what it adds isn't known. Its "
        "structure as drawn weighs 763.287 g/mol for each parent.",
    } in output["messages"]


#: Imatinib, as PubChem gives it (CID 5291): C29H31N7O, 493.6 g/mol.
IMATINIB = "CC1=C(C=C(C=C1)NC(=O)C2=CC=C(C=C2)CN3CCN(CC3)C)NC4=NC=CC(=N4)C5=CN=CC=C5"


def test_a_counterion_drawn_as_an_isotope_isnt_the_salt_iki_knows(tmp_path: Path) -> None:
    from rdkit import Chem
    from rdkit.Chem import Descriptors

    output = registered(
        tmp_path,
        # Lithium aspirinate, its lithium drawn as lithium-6 (6.0151228874 u, NIST): with
        # aspirinate's 179.151 g/mol (aspirin, 180.159, less a hydrogen), 185.166 g/mol,
        # not the 186.092 of lithium's standard atomic weight, which is mostly lithium-7's.
        "CC(=O)Oc1ccccc1C(=O)[O-].[6Li+] lithium-6 aspirinate\n"
        # And with lithium as it is in nature: 186.092 (aspirin's 180.159, less H, plus Li).
        "CC(=O)Oc1ccccc1C(=O)[O-].[Li+] lithium aspirinate\n"
        # Imatinib mesylate (CID 123596, 589.7 g/mol), its mesylate's methyl deuterated.
        f"{IMATINIB}.[2H]C([2H])([2H])S(=O)(=O)O imatinib mesylate-d3\n"
        # And with methanesulfonic acid as it is in nature.
        f"{IMATINIB}.CS(=O)(=O)O imatinib mesylate",
        parent=True,
        batches=True,
    )
    batches = [
        c["values"] for c in output["creates"] if c["type"] == "iki/small_molecule.CompoundBatch"
    ]
    assert "formula_weight" not in batches[0]
    assert "salt" not in batches[0]
    assert (batches[1]["salt"], batches[1]["formula_weight"]) == ("lithium", 186.092)
    assert "formula_weight" not in batches[2]
    assert (batches[3]["salt"], batches[3]["formula_weight"]) == ("mesylate", 589.722)
    d3 = Descriptors.MolWt(  # type: ignore[attr-defined]
        Chem.MolFromSmiles(f"{IMATINIB}.[2H]C([2H])([2H])S(=O)(=O)O")
    )
    said = [m["text"] for m in output["messages"]]
    assert (
        "Line 1: Its batch has no formula weight: [6Li+] is part of the material, but not a "
        "salt or solvent Iki knows, so what it adds isn't known. Its structure as drawn "
        "weighs 185.166 g/mol for each parent."
    ) in said
    assert (
        "Line 3: Its batch has no formula weight: [2H]C([2H])([2H])S(=O)(=O)O is part of the "
        "material, but not a salt or solvent Iki knows, so what it adds isn't known. Its "
        f"structure as drawn weighs {d3:.3f} g/mol for each parent."
    ) in said
    assert not [t for t in said if t.startswith(("Line 2", "Line 4")) and "weight" in t], said
    # Recomputing a batch registered as lithium-6 aspirinate gives it none either.
    lithium_6 = {
        **entity(1, structure_as_drawn="CC(=O)Oc1ccccc1C(=O)[O-].[6Li+]", salt="lithium"),
        "included": {"compound": [entity(101, smiles=ASPIRIN)]},
    }
    lithium_6["values"]["formula_weight"] = 186.092
    result, recomputed = script(
        tmp_path, "descriptors.py", {"inputs": {"batch": [lithium_6]}}, "batches"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert recomputed is not None
    assert recomputed["updates"]["batch"] == [
        {"entity": lithium_6["id"], "values": {"formula_weight": None}}
    ]


def test_a_batch_recomputes_its_weight_only_when_all_its_material_is_known(
    tmp_path: Path,
) -> None:
    sunitinib = r"CCN(CC)CCNC(=O)c1c(C)[nH]c(/C=C2\C(=O)Nc3ccc(F)cc32)c1C"
    drawn = f"{PAMOIC_ACID}.{HYDROXYZINE}"

    def batch(i: int, compound: str, **values: Any) -> dict[str, Any]:
        return {**entity(i, **values), "included": {"compound": [entity(100 + i, smiles=compound)]}}

    batches = [
        # Its malate, drawn (S), is its salt: 532.569 g/mol, as registered.
        batch(
            1,
            sunitinib,
            structure_as_drawn=f"{sunitinib}.O=C(O)C[C@H](O)C(=O)O",
            salt="malate",
            salt_equivalents=1.0,
        ),
        # Its pamoate isn't a salt Iki knows: a weight it has is taken away, and none given.
        batch(2, HYDROXYZINE, structure_as_drawn=drawn, formula_weight=374.912),
        batch(3, HYDROXYZINE, structure_as_drawn=drawn),
        # Registered whole, its compound is all of it.
        batch(4, drawn, structure_as_drawn=drawn),
    ]
    result, output = script(tmp_path, "descriptors.py", {"inputs": {"batch": batches}}, "batches")
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    assert output["updates"]["batch"] == [
        {"entity": batches[0]["id"], "values": {"formula_weight": 532.569}},
        {"entity": batches[1]["id"], "values": {"formula_weight": None}},
        {"entity": batches[3]["id"], "values": {"formula_weight": 763.287}},
    ]
    assert output["messages"] == [
        {
            "level": "warning",
            "text": f"X-{i} has no formula weight: its structure as drawn has {PAMOIC_ACID}, "
            f"which is neither X-{100 + i} nor a salt or solvent Iki knows, so what it adds "
            "to the material isn't known.",
            "entity": batches[i - 1]["id"],
        }
        for i in (2, 3)
    ]


def test_a_charged_parents_batch_recomputes_to_the_same_weight(tmp_path: Path) -> None:
    def batch(i: int, smiles: str, **values: Any) -> dict[str, Any]:
        return {**entity(i, **values), "included": {"compound": [entity(100 + i, smiles=smiles)]}}

    batches = [
        batch(1, "C[N+](C)(C)CCO", salt="hydrochloride", salt_equivalents=1),
        # Tetramethylammonium bromide, C4H12BrN: 154.05 g/mol.
        batch(2, "C[N+](C)(C)C", salt="hydrobromide"),
        # A cation alone, with no salt, is just itself.
        batch(3, "C[N+](C)(C)CCO"),
    ]
    result, output = script(tmp_path, "descriptors.py", {"inputs": {"batch": batches}}, "batches")
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    weights = [u["values"]["formula_weight"] for u in output["updates"]["batch"]]
    assert weights == pytest.approx([139.626, 154.051, 104.173], abs=1e-3)


def test_without_the_parent_a_salt_is_its_own_compound(tmp_path: Path) -> None:
    output = registered(tmp_path, "CC(=O)Oc1ccccc1C(=O)[O-].[Na+]")
    # Sodium aspirinate's standard InChIKey, not aspirin's.
    assert output["creates"][0]["values"]["inchikey"] == "JZLOKWGVGHYBKD-UHFFFAOYSA-M"


def test_parts_that_arent_salts_leave_the_largest_saying_so(tmp_path: Path) -> None:
    output = registered(tmp_path, f"{ASPIRIN}.c1ccccc1", parent=True)
    assert output["creates"][0]["values"]["smiles"] == ASPIRIN
    assert output["messages"] == [
        {
            "level": "warning",
            "text": "Line 1: It has other parts, which aren't salts or solvents Iki knows, so "
            "only its largest part was kept: c1ccccc1 left out.",
        }
    ]


def test_a_line_that_cant_be_read_fails_the_run_naming_it(tmp_path: Path) -> None:
    result, output = script(
        tmp_path,
        "register.py",
        {"params": {"smiles": f"{ASPIRIN}\nC1CC ring\nCN(C)(C)(C)C\n*C"}, "inputs": {}},
    )
    assert result.returncode == 1
    assert output is None
    assert last_error(result) == (
        "::error 3 of 4 lines can't be registered, so none was. "
        "Line 2: 'C1CC' isn't SMILES RDKit can read: unclosed ring. "
        "Line 3: 'CN(C)(C)(C)C' isn't a valid structure: Explicit valence for atom # 1 N, 5, "
        "is greater than permitted. "
        "Line 4: InChI can't represent it: Unsupported in this mode element '*'."
    )


def test_skipping_registers_the_rest_and_names_whats_left_out(tmp_path: Path) -> None:
    output = registered(tmp_path, f"{ASPIRIN}\nC1CC ring", on_error="skip")
    assert len(output["creates"]) == 1
    assert output["metrics"] == {"lines": 2, "skipped": 1}
    assert output["messages"] == [
        {
            "level": "warning",
            "text": "Line 2 was left out: 'C1CC' isn't SMILES RDKit can read: unclosed ring.",
        }
    ]


@pytest.mark.parametrize(
    ("smiles", "error"),
    [
        ("# nothing\n\n", "::error Give at least one SMILES, such as CC(=O)Oc1ccccc1C(=O)O."),
        (
            "C\n" * 5001,
            "::error That's 5,001 compounds, more than the 5,000 one run registers: split "
            "them, or import an SDF file.",
        ),
        (
            "CCN.Cl.Br",
            "::error 1 of 1 lines can't be registered, so none was. Line 1: it has more than "
            "one salt (hydrobromide, hydrochloride), and a batch records one.",
        ),
    ],
)
def test_what_cant_be_registered_fails_whole(tmp_path: Path, smiles: str, error: str) -> None:
    result, _ = script(
        tmp_path, "register.py", {"params": {"smiles": smiles, "parent": True}, "inputs": {}}
    )
    assert result.returncode == 1
    assert last_error(result) == error


# --- Descriptors ---------------------------------------------------------------------------------


def described(tmp_path: Path, *compounds: dict[str, Any]) -> dict[str, Any]:
    result, output = script(
        tmp_path, "descriptors.py", {"inputs": {"compound": list(compounds)}}, "compounds"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    return output


# Aspirin as RDKit 2026.09.2 lays it out with CoordGen: V2000, kekulized, its title (the
# first line) empty for Iki to write the record's name in.
ASPIRIN_MOLFILE = """
     RDKit          2D

 13 13  0  0  0  0  0  0  0  0999 V2000
   -2.6640    0.3130    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
   -1.7990   -0.1888    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
   -1.8012   -1.1888    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
   -0.9320    0.3096    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
   -0.0672   -0.1922    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
   -0.0690   -1.1922    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    0.7960   -1.6940    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.6630   -1.1956    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.6650   -0.1956    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    0.8000    0.3062    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    0.8020    1.3060    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
   -0.0630    1.8078    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
    1.6690    1.8042    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0
  2  3  2  0
  2  4  1  0
  4  5  1  0
  5  6  2  0
  6  7  1  0
  7  8  2  0
  8  9  1  0
  9 10  2  0
 10 11  1  0
 11 12  2  0
 11 13  1  0
 10  5  1  0
M  END
"""


def test_descriptors_are_rdkits(tmp_path: Path) -> None:
    output = described(tmp_path, entity(1, smiles=ASPIRIN, inchikey=ASPIRIN_KEY))
    (update,) = output["updates"]["compound"]
    assert update == {
        "entity": entity(1)["id"],
        "values": {
            "inchi": "InChI=1S/C9H8O4/c1-6(10)13-8-5-3-2-4-7(8)9(11)12/h2-5H,1H3,(H,11,12)",
            "molecular_formula": "C9H8O4",
            "molecular_weight": 180.159,
            "exact_mass": 180.04226,
            "formal_charge": 0,
            "heavy_atoms": 13,
            "logp": 1.31,
            "tpsa": 63.6,
            "hbd": 1,
            "hba": 3,
            "rotatable_bonds": 2,
            "rings": 1,
            "aromatic_rings": 1,
            "fraction_csp3": 0.111,
            "stereocentres": 0,
            "undefined_stereocentres": 0,
            "lipinski_violations": 0,
            "qed": 0.55,
            "molfile": ASPIRIN_MOLFILE,
        },
    }
    assert output["messages"] == []
    # The same SMILES, with the same RDKit, gives the same Molfile.
    again = described(tmp_path, entity(1, smiles=ASPIRIN, inchikey=ASPIRIN_KEY))
    assert again["updates"]["compound"][0]["values"]["molfile"] == ASPIRIN_MOLFILE


def inchikey(mol: Any) -> str:
    """Its standard InChIKey, from RDKit's InChI library."""
    from rdkit.Chem import rdinchi

    key: str = rdinchi.InchiToInchiKey(rdinchi.MolToInchi(mol)[0])
    return key


def molfile_lines(block: str) -> tuple[list[str], list[str], list[str]]:
    """A V2000 Molfile's atom lines, bond lines and properties (its M lines)."""
    lines = block.splitlines()
    atoms, bonds = int(lines[3][:3]), int(lines[3][3:6])
    return (
        lines[4 : 4 + atoms],
        lines[4 + atoms : 4 + atoms + bonds],
        lines[4 + atoms + bonds :],
    )


def test_each_compound_is_kept_as_a_molfile_of_its_stereoisomer(tmp_path: Path) -> None:
    from rdkit import Chem

    # Each with PubChem's InChIKey for it.
    compounds = {
        # One stereocentre: (S)-ibuprofen, CID 3672.
        "CC(C)Cc1ccc([C@H](C)C(=O)O)cc1": "HEFNNWSXXWATRW-JTQLQIEISA-N",
        # A Z double bond: tamoxifen, CID 2733526.
        "CC/C(=C(\\c1ccccc1)c1ccc(OCCN(C)C)cc1)c1ccccc1": "NKANXQFJJICGDU-QPLCGJKRSA-N",
        # A double bond whose configuration isn't given: clomifene, a mixture of its E and
        # Z isomers, CID 2800.
        "CCN(CC)CCOc1ccc(C(=C(Cl)c2ccccc2)c2ccccc2)cc1": "GKIRPKYJQBWNGO-UHFFFAOYSA-N",
        # Fluorine-18 and five stereocentres: fludeoxyglucose (18F), CID 450503.
        "OC[C@H]1O[C@H](O)[C@H]([18F])[C@@H](O)[C@@H]1O": "ZCXUVYAZINUVJD-AHXZWLDOSA-N",
        # A quaternary ammonium cation with carbon-11: [11C]choline, CID 449688.
        "C[N+](C)([11CH3])CCO": "OEYIOHPDSNJKLS-BJUDXGSMSA-N",
        # A cation with four stereocentres in a tropane and its epoxide: tiotropium,
        # CID 5487427.
        "C[N+]1(C)[C@@H]2CC(OC(=O)C(O)(c3cccs3)c3cccs3)C[C@H]1[C@@H]1O[C@@H]12": (
            "LERNTVKEWCAPOY-FPISHFTHSA-N"
        ),
    }
    output = described(
        tmp_path,
        *(entity(i, smiles=s, inchikey=k) for i, (s, k) in enumerate(compounds.items())),
    )
    assert output["messages"] == []
    blocks = [u["values"]["molfile"] for u in output["updates"]["compound"]]
    for (smiles, key), block in zip(compounds.items(), blocks, strict=True):
        mol = Chem.MolFromSmiles(smiles)
        lines = block.splitlines()
        assert lines[0] == ""  # Iki writes the record's name here when it exports it
        assert lines[1].strip() == "RDKit          2D"
        assert lines[3].endswith(" V2000")
        assert (int(lines[3][:3]), int(lines[3][3:6])) == (mol.GetNumAtoms(), mol.GetNumBonds())
        atoms, bonds, properties = molfile_lines(block)
        assert properties[-1] == "M  END"
        # Laid out in 2D: no two atoms at one point, and none out of the plane.
        points = {(line[0:10], line[10:20]) for line in atoms}
        assert len(points) == len(atoms)
        assert {line[20:30] for line in atoms} == {"    0.0000"}
        # Kekulized: single and double bonds, none aromatic (4).
        assert {line[6:9].strip() for line in bonds} <= {"1", "2", "3"}
        # Read back, it's the same compound, its stereoisomer and its isotopes and charges.
        back = Chem.MolFromMolBlock(block)
        assert inchikey(back) == key
        assert Chem.MolToSmiles(back) == Chem.MolToSmiles(mol)
    ibuprofen, tamoxifen, clomifene, fdg, choline, tiotropium = (molfile_lines(b) for b in blocks)
    # Stereocentres are wedged (1) or hashed (6), one bond for each.
    wedged = [sum(line[9:12].strip() in ("1", "6") for line in m[1]) for m in (ibuprofen, fdg)]
    assert wedged == [1, 5]
    # A double bond whose configuration isn't given is drawn crossed (3), so reading the
    # Molfile doesn't take one from its coordinates; one that's given isn't.
    assert [line[9:12].strip() for line in clomifene[1] if line[6:9].strip() == "2"].count("3") == 1
    assert all(line[9:12].strip() != "3" for line in tamoxifen[1])
    assert fdg[2] == ["M  ISO  1   8  18", "M  END"]
    assert choline[2] == ["M  CHG  1   2   1", "M  ISO  1   4  11", "M  END"]
    assert tiotropium[2] == ["M  CHG  1   2   1", "M  END"]


def test_a_structure_of_more_than_999_atoms_is_a_v3000_molfile(tmp_path: Path) -> None:
    from rdkit import Chem

    # Monodisperse methoxy PEG of 340 units (15 kDa), a PEGylation reagent: 1,022 heavy
    # atoms, more than a V2000 counts line holds.
    peg = "CO" + "CCO" * 340
    key = inchikey(Chem.MolFromSmiles(peg))
    output = described(tmp_path, entity(1, smiles=peg, inchikey=key))
    assert output["messages"] == []
    block = output["updates"]["compound"][0]["values"]["molfile"]
    lines = block.splitlines()
    assert lines[0] == ""
    assert lines[3].endswith(" V3000")
    assert "M  V30 COUNTS 1022 1021 0 0 0" in lines
    assert lines[-1] == "M  END"
    back = Chem.MolFromMolBlock(block)
    assert back.GetNumAtoms() == 1022
    assert inchikey(back) == key


def test_a_structure_coordgen_cant_lay_out_in_time_is_laid_out_by_rdkits_own(
    scripts: Any,
) -> None:
    from rdkit import Chem

    chemistry = scripts.import_module("chemistry")
    # Cyclodotetracontane: CoordGen doesn't lay out a carbon ring of 42 after minutes;
    # RDKit's own depictor does at once.
    ring = "C1" + "C" * 41 + "1"
    with chemistry.Layouts(seconds=2) as layouts:
        block = layouts.molfile(ring, until=time.monotonic() + 60)
        # And the next is laid out by CoordGen again, in a new process.
        assert layouts.molfile(ASPIRIN, until=time.monotonic() + 60) == ASPIRIN_MOLFILE
        # Past the run's time for layouts, nothing is.
        assert layouts.molfile(ASPIRIN, until=time.monotonic()) is None
    assert block is not None
    assert inchikey(Chem.MolFromMolBlock(block)) == "HGYWDMKVEUIDAK-UHFFFAOYSA-N"


def test_a_molfile_that_would_overfill_the_output_is_left_out_saying_so(
    scripts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptors = scripts.import_module("descriptors")
    # Room for one aspirin and its Molfile, not two.
    monkeypatch.setattr(descriptors, "UPDATES_BYTES", 2_500)
    output = descriptors.compounds(
        {
            "inputs": {
                "compound": [
                    entity(1, smiles=ASPIRIN, inchikey=ASPIRIN_KEY),
                    entity(2, smiles=ASPIRIN, inchikey=ASPIRIN_KEY),
                ]
            }
        }
    )
    first, second = (u["values"] for u in output["updates"]["compound"])
    assert first["molfile"] == ASPIRIN_MOLFILE
    assert "molfile" not in second
    assert second["molecular_weight"] == 180.159
    assert output["messages"] == [
        {
            "level": "warning",
            "text": "X-2 has no Molfile, so it doesn't export as a Molfile or an SDF yet: its "
            "Molfile, of 1,151 characters, would take the run's output over the 2.5 MB it "
            "holds. Run compute_descriptors on it with fewer compounds.",
            "entity": entity(2)["id"],
        }
    ]


def test_labelled_atoms_show_in_the_formula_as_in_the_masses(tmp_path: Path) -> None:
    structures = {
        "[2H]O[2H]": "XLYOFNOQVPJJNP-ZSJDYOACSA-N",  # heavy water (PubChem CID 24602)
        "[13CH3]O": "OKKJLVBELUTLKV-OUBTZVSYSA-N",
        "C12C3C4C1C5C2C3C45": "TXWRERCHRDBNLG-UHFFFAOYSA-N",  # cubane
        "CNC(C)=O": "OHLUUHNLEMFGTQ-UHFFFAOYSA-N",  # N-methylacetamide
    }
    output = described(
        tmp_path,
        *(entity(i, smiles=s, inchikey=k) for i, (s, k) in enumerate(structures.items())),
    )
    water, methanol, cubane, amide = (u["values"] for u in output["updates"]["compound"])
    # Monoisotopic masses from IUPAC's isotope masses: ²H 2.014 101 78, ¹H 1.007 825 03,
    # ¹²C 12, ¹³C 13.003 354 84, ¹⁶O 15.994 914 62.
    assert (water["molecular_formula"], water["exact_mass"]) == ("D2O", 20.02312)
    assert (methanol["molecular_formula"], methanol["exact_mass"]) == ("[13C]H4O", 33.02957)
    # Cubane's 8 atoms and 12 bonds make 12 - 8 + 1 = 5 independent rings; RDKit's
    # symmetrised set, which the field counts, has all 6 faces.
    assert cubane["rings"] == 6
    # N-methylacetamide's one non-terminal single bond is its amide C-N, which RDKit's
    # strict count leaves out.
    assert amide["rotatable_bonds"] == 0


def test_stereocentres_and_lipinskis_rule_of_five(tmp_path: Path) -> None:
    # Ibuprofen with its stereocentre undefined, (S)-ibuprofen, and ciclosporin.
    # PubChem CID 5284373.
    ciclosporin = (
        "CC[C@H]1C(=O)N(CC(=O)N([C@H](C(=O)N[C@H](C(=O)N([C@H](C(=O)N[C@H](C(=O)N[C@@H](C"
        "(=O)N([C@H](C(=O)N([C@H](C(=O)N([C@H](C(=O)N([C@H](C(=O)N1)[C@@H]([C@H](C)C/C=C/"
        "C)O)C)C(C)C)C)CC(C)C)C)CC(C)C)C)C)C)CC(C)C)C)C(C)C)CC(C)C)C)C"
    )
    output = described(
        tmp_path,
        entity(1, smiles="CC(C)Cc1ccc(C(C)C(=O)O)cc1", inchikey="HEFNNWSXXWATRW-UHFFFAOYSA-N"),
        entity(2, smiles="CC(C)Cc1ccc([C@H](C)C(=O)O)cc1", inchikey="HEFNNWSXXWATRW-JTQLQIEISA-N"),
        entity(3, smiles=ciclosporin, inchikey="PMATZTZNYRCHOR-CGLBZJNRSA-N"),
    )
    assert output["messages"] == []
    racemic, single, cyclic = (u["values"] for u in output["updates"]["compound"])
    assert (racemic["stereocentres"], racemic["undefined_stereocentres"]) == (1, 1)
    assert (single["stereocentres"], single["undefined_stereocentres"]) == (1, 0)
    assert racemic["lipinski_violations"] == 0
    # 1202.6 g/mol, and 23 nitrogens and oxygens: two of the rule of five's limits.
    assert cyclic["molecular_formula"] == "C62H111N11O12"
    assert cyclic["lipinski_violations"] == 2


def test_a_compound_whose_inchikey_isnt_its_structures_fails_its_own_run(tmp_path: Path) -> None:
    output = described(
        tmp_path,
        entity(1, smiles=ASPIRIN, inchikey="RZVAJINKPMORJF-UHFFFAOYSA-N"),
        entity(2, smiles=ASPIRIN, inchikey=ASPIRIN_KEY),
    )
    assert [u["entity"] for u in output["updates"]["compound"]] == [entity(2)["id"]]
    assert output["messages"] == [
        {
            "level": "error",
            "text": "X-1's InChIKey is RZVAJINKPMORJF-UHFFFAOYSA-N, but its SMILES gives "
            f"{ASPIRIN_KEY}: it isn't the compound its structure is. Register its structure "
            "with register_compounds instead.",
            "entity": entity(1)["id"],
        }
    ]


def test_a_batchs_formula_weight_counts_its_salt_and_solvent(tmp_path: Path) -> None:
    amine = "CN(C)CCC(c1ccccc1)c1ccccn1"  # pheniramine, 240.35 g/mol

    def batch(i: int, **values: Any) -> dict[str, Any]:
        return {**entity(i, **values), "included": {"compound": [entity(100 + i, smiles=amine)]}}

    batches = [
        batch(1),
        batch(2, salt="hydrochloride"),
        batch(3, salt="maleate", salt_equivalents=1, solvate="water", solvate_equivalents=0.5),
        batch(4, salt="sulfate", salt_equivalents=0.5),
    ]
    result, output = script(tmp_path, "descriptors.py", {"inputs": {"batch": batches}}, "batches")
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    weights = [u["values"]["formula_weight"] for u in output["updates"]["batch"]]
    assert output["messages"] == []
    assert weights == pytest.approx(
        [240.35, 240.35 + 36.461, 240.35 + 116.072 + 9.008, 240.35 + 98.079 / 2], abs=0.01
    )


def test_the_batch_types_salts_and_solvates_are_the_scripts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml

    monkeypatch.syspath_prepend(str(SCRIPTS))
    chemistry = importlib.import_module("chemistry")

    manifest = yaml.safe_load((SCRIPTS.parent / "types" / "compound_batch.yaml").read_text())
    assert manifest["fields"]["salt"]["values"] == list(chemistry.SALTS)
    assert manifest["fields"]["solvate"]["values"] == list(chemistry.SOLVENTS)
    # Each is told apart from the others by its structure.
    assert len(chemistry.KNOWN) == len(chemistry.SALTS) + len(chemistry.SOLVENTS)
    sys.modules.pop("chemistry", None)


# --- SDF files -----------------------------------------------------------------------------------

FILE_ID = "00000000-0000-0000-0000-00000000f11e"


def sdf_text(*records: tuple[str, str, dict[str, str]]) -> str:
    """An SDF file, written by RDKit, of (SMILES, title, SD fields) records."""
    from rdkit import Chem

    out = io.StringIO()
    writer = Chem.SDWriter(out)
    for smiles, title, fields in records:
        mol = Chem.MolFromSmiles(smiles)
        mol.SetProp("_Name", title)
        for name, value in fields.items():
            mol.SetProp(name, value)
        writer.write(mol)
    writer.close()
    return out.getvalue()


def imported(
    tmp_path: Path, text: str, **params: Any
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    path = tmp_path / "library.sdf"
    path.write_text(text)
    return script(
        tmp_path,
        "sdf.py",
        {
            "params": {"file": FILE_ID, **params},
            "inputs": {},
            "files": {
                FILE_ID: {
                    "name": "library.sdf",
                    "media_type": "chemical/x-mdl-sdfile",
                    "size": len(text),
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "path": str(path),
                }
            },
        },
        "import",
    )


def test_each_record_is_a_compound_with_the_fields_it_has(tmp_path: Path) -> None:
    text = sdf_text(
        (ASPIRIN, "aspirin", {"CAS": "50-78-2", "PUBCHEM_COMPOUND_CID": "2244", "Colour": "white"}),
        ("CC(=O)Nc1ccc(O)cc1", "paracetamol", {"Synonyms": "acetaminophen; APAP"}),
    )
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    aspirin, paracetamol = output["creates"]
    assert aspirin["values"] == {
        "smiles": ASPIRIN,
        "inchikey": ASPIRIN_KEY,
        "inchi": "InChI=1S/C9H8O4/c1-6(10)13-8-5-3-2-4-7(8)9(11)12/h2-5H,1H3,(H,11,12)",
        "name": "aspirin",
        "cas_number": "50-78-2",
        "pubchem_cid": "2244",
    }
    record = text.split("$$$$\n")[0] + "$$$$\n"
    assert aspirin["origin"] == {
        "source": "SDF file",
        "id": "aspirin",
        "file": FILE_ID,
        "sha256": hashlib.sha256(record.encode()).hexdigest(),
    }
    assert paracetamol["values"]["synonyms"] == ["acetaminophen", "APAP"]
    assert output["metrics"] == {"records": 2, "skipped": 0}
    assert output["messages"] == [
        {
            "level": "info",
            "text": "These SD fields aren't fields of compounds, so they weren't kept: Colour (1).",
        }
    ]


def test_records_name_from_a_field_and_batches_from_theirs(tmp_path: Path) -> None:
    text = sdf_text(
        (
            "CC(=O)Oc1ccccc1C(=O)[O-].[Na+]",
            "Z1234",
            {
                "Name": "aspirin sodium",
                "Purity": "98.5%",
                "Amount": "0.25 g",
                "Supplier": "Enamine",
                "Catalog_No": "EN300-1",
                "Lot": "B-17",
                "Received": "2026-11-02",
            },
        ),
        ("CN(C)CCC(c1ccccc1)c1ccccn1", "Z1235", {"Name": "pheniramine", "Salt": "HCl"}),
    )
    result, output = imported(tmp_path, text, name_field="name", parent=True, batches=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    salt, compound, base, pheniramine = (
        output["creates"][1],
        output["creates"][0],
        output["creates"][2],
        output["creates"][3],
    )
    assert compound["values"]["inchikey"] == ASPIRIN_KEY
    # Its name is the salt's, so it's the batch's, not its parent compound's.
    assert "name" not in compound["values"]
    assert salt["values"] == {
        "compound": {"$create": "record-1"},
        "name": "aspirin sodium",
        "structure_as_drawn": "CC(=O)Oc1ccccc1C(=O)[O-].[Na+]",
        "salt": "sodium",
        "salt_equivalents": 1.0,
        "formula_weight": 202.141,
        "purity": 98.5,
        "amount": 250.0,
        "supplier": "Enamine",
        "catalogue_number": "EN300-1",
        "supplier_lot": "B-17",
        "received": "2026-11-02",
    }
    # A salt its field gives, which its structure doesn't show.
    assert (pheniramine["values"]["salt"], pheniramine["values"]["salt_equivalents"]) == (
        "hydrochloride",
        1.0,
    )
    assert pheniramine["values"]["formula_weight"] == pytest.approx(240.35 + 36.461, abs=0.01)
    # The record is a hydrochloride, by its Salt field, so its name is its batch's.
    assert "name" not in base["values"]
    assert pheniramine["values"]["name"] == "pheniramine"


def v3000(smiles: str, title: str) -> str:
    """An SDF record of a CXSMILES, as a V3000 molfile, which keeps its enhanced stereo."""
    from rdkit import Chem

    mol = Chem.MolFromSmiles(smiles)
    return f"{title}\n" + Chem.MolToV3KMolBlock(mol).split("\n", 1)[1] + "$$$$\n"


def test_enhanced_stereo_isnt_registered_as_the_stereoisomer_drawn(tmp_path: Path) -> None:
    # Alanine drawn as L with an AND group is DL-alanine, and with an OR group one of the
    # two, not known which: neither is L-alanine (QNAYBMKLOCPYGJ-REOHCLBHSA-N). A standard
    # InChI has no such layer, so the centre is undefined: alanine, QNAYBMKLOCPYGJ-UHFFFAOYSA-N
    # (PubChem CID 602, DL-alanine).
    text = (
        v3000("C[C@H](N)C(=O)O", "L")
        + v3000("C[C@H](N)C(=O)O |&1:1|", "DL")
        + v3000("C[C@H](N)C(=O)O |o1:1|", "D or L")
    )
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    keys = [c["values"]["inchikey"] for c in output["creates"]]
    assert keys == [
        "QNAYBMKLOCPYGJ-REOHCLBHSA-N",
        "QNAYBMKLOCPYGJ-UHFFFAOYSA-N",
        "QNAYBMKLOCPYGJ-UHFFFAOYSA-N",
    ]
    assert output["messages"] == [
        {
            "level": "warning",
            "text": "Record 2 (DL): Its stereocentre C2 is in an AND group, both configurations "
            "together (its two enantiomers), which a standard InChIKey can't record, so it was "
            "registered undefined.",
        },
        {
            "level": "warning",
            "text": "Record 3 (D or L): Its stereocentre C2 is in an OR group, one "
            "configuration, not known which, which a standard InChIKey can't record, so it "
            "was registered undefined.",
        },
    ]
    # The same from CXSMILES, its extensions before its name.
    smiles = registered(tmp_path, "C[C@H](N)C(=O)O |&1:1| DL-alanine")
    values = smiles["creates"][0]["values"]
    assert (values["inchikey"], values["name"]) == ("QNAYBMKLOCPYGJ-UHFFFAOYSA-N", "DL-alanine")


@pytest.mark.parametrize("group", ["&1", "o1"])
def test_relative_stereo_in_a_group_cant_be_registered(tmp_path: Path, group: str) -> None:
    # Two centres in one group: how they relate is known, which a standard InChIKey can't
    # hold (it would give YQMUFYJDRKBLFF-HRFVKAFMSA-N, the one stereoisomer drawn).
    result, output = imported(
        tmp_path, v3000(f"C[C@H](F)[C@H](Cl)Br |{group}:1,3|", "rel"), on_error="fail"
    )
    assert result.returncode == 1
    assert output is None
    kind = "AND" if group == "&1" else "OR"
    assert f"Record 1 (rel): its stereocentres C2, C4 are an {kind} group" in last_error(result)


#: 1,1'-Bi-2-naphthol (BINOL), as PubChem gives it (CID 11762), whose two atropisomers,
#: (R) and (S), are one standard InChIKey there, PPTXVXKCQZKFBN-UHFFFAOYSA-N: a standard
#: InChI doesn't record an atropisomer's axis. Its axis is the bond between atoms 9 and 10.
BINOL = "C1=CC=C2C(=C1)C=CC(=C2C3=C(C=CC4=CC=CC=C43)O)O"
#: Sotorasib, as PubChem gives it (CID 137278711, NXQKSXLFSAEQCZ-SFHVURJKSA-N, with only
#: its stereocentre in it): one atropisomer, its axis from its pyrimidinone's N1 (atom 11)
#: to its pyridine (atom 27).
SOTORASIB = "C[C@H]1CN(CCN1C2=NC(=O)N(C3=NC(=C(C=C32)F)C4=C(C=CC=C4F)O)C5=C(C=CN=C5C(C)C)C)C(=O)C=C"


def atropisomer(smiles: str, axis: tuple[int, int], dash: bool = False) -> Any:
    """``smiles`` drawn in 2D with a wedge (or a hash) on a bond beside its axis, from the
    axis's atom, which RDKit reads as the axis's configuration (RDKit Book, "Atropisomeric
    bonds"). A hash is the other atropisomer."""
    from rdkit import Chem
    from rdkit.Chem import rdDepictor

    mol = Chem.MolFromSmiles(smiles)
    rdDepictor.Compute2DCoords(mol)
    axial = mol.GetBondBetweenAtoms(*axis).GetIdx()
    beside = next(
        b
        for i in axis
        for b in mol.GetAtomWithIdx(i).GetBonds()
        if b.GetIdx() != axial and b.GetBeginAtomIdx() == i
    )
    beside.SetBondDir(Chem.BondDir.BEGINDASH if dash else Chem.BondDir.BEGINWEDGE)
    read = Chem.MolFromMolBlock(Chem.MolToMolBlock(mol))
    # RDKit reads it as clockwise or anticlockwise about the axis.
    assert read.GetBondBetweenAtoms(*axis).GetStereo() in (
        Chem.BondStereo.STEREOATROPCW,
        Chem.BondStereo.STEREOATROPCCW,
    )
    return read


def test_an_atropisomer_isnt_registered_as_its_axis_undefined(tmp_path: Path) -> None:
    from rdkit import Chem

    (r, s), (m, p) = (
        (atropisomer(BINOL, (9, 10)), atropisomer(BINOL, (9, 10), dash=True)),
        (atropisomer(SOTORASIB, (11, 27)), atropisomer(SOTORASIB, (11, 27), dash=True)),
    )
    assert r.GetBondBetweenAtoms(9, 10).GetStereo() != s.GetBondBetweenAtoms(9, 10).GetStereo()
    text = "".join(
        f"{title}\n" + Chem.MolToMolBlock(mol).split("\n", 1)[1] + "$$$$\n"
        for title, mol in (
            ("BINOL", r),
            ("its enantiomer", s),
            ("ethanol", Chem.MolFromSmiles("CCO")),
        )
    )
    result, output = imported(tmp_path, text, on_error="fail")
    assert result.returncode == 1
    assert output is None
    error = last_error(result)
    assert "2 of 3 records can't be imported, so none was." in error
    assert (
        "Record 1 (BINOL): its bond C10-C11 is a stereogenic axis drawn with its "
        "configuration (an atropisomer). A standard InChI doesn't record an atropisomer's "
        "axis, so both atropisomers would have one InChIKey, and be one compound."
    ) in error
    assert "Record 2 (its enantiomer): its bond C10-C11 is a stereogenic axis" in error
    # Drawn without the wedge, its axis is undefined: BINOL, whichever it is.
    plain = registered(tmp_path, f"{BINOL} BINOL")
    assert plain["creates"][0]["values"]["inchikey"] == "PPTXVXKCQZKFBN-UHFFFAOYSA-N"
    assert plain["messages"] == []
    # Sotorasib's CXSMILES, with its axis's wedge, fails its line too; the rest go on.
    result, output = script(
        tmp_path,
        "register.py",
        {
            "params": {
                "smiles": "\n".join(
                    [f"{Chem.MolToCXSmiles(x)} sotorasib" for x in (m, p)] + ["CCO ethanol"]
                ),
                "on_error": "skip",
            },
            "inputs": {},
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    assert [c["values"]["name"] for c in output["creates"]] == ["ethanol"]
    assert [m["text"][:72] for m in output["messages"]] == [
        # Its axis named as its CXSMILES numbers its atoms.
        f"Line {n} was left out: its bond N13-C14 is a stereogenic axis drawn with i"
        for n in (1, 2)
    ]


@pytest.mark.parametrize(("group", "kind"), [("&1", "AND"), ("o1", "OR")])
def test_an_axis_in_a_group_of_its_own_is_registered_undefined(
    tmp_path: Path, group: str, kind: str
) -> None:
    from rdkit import Chem

    # One axis in an AND group is racemic BINOL; in an OR group, one of the two, not known
    # which. Neither says more than the axis undefined, as one centre in a group doesn't.
    binol = atropisomer(BINOL, (9, 10))
    kinds = {"&1": Chem.StereoGroupType.STEREO_AND, "o1": Chem.StereoGroupType.STEREO_OR}
    grouped = Chem.RWMol(binol)
    axial = binol.GetBondBetweenAtoms(9, 10).GetIdx()
    grouped.SetStereoGroups([Chem.CreateStereoGroup(kinds[group], binol, [], [axial])])
    text = "".join(
        f"{title}\n" + Chem.MolToV3KMolBlock(mol).split("\n", 1)[1] + "$$$$\n"
        for title, mol in (("BINOL", grouped), ("ethanol", Chem.MolFromSmiles("CCO")))
    )
    result, output = imported(tmp_path, text, on_error="skip")
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    keys = [c["values"]["inchikey"] for c in output["creates"]]
    assert keys == ["PPTXVXKCQZKFBN-UHFFFAOYSA-N", "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"]
    meaning = (
        "both configurations together (its two enantiomers)"
        if kind == "AND"
        else "one configuration, not known which"
    )
    assert output["messages"] == [
        {
            "level": "warning",
            "text": f"Record 1 (BINOL): Its stereogenic axis C10-C11 (an atropisomer's) is in "
            f"an {kind} group, {meaning}, which a standard InChIKey can't record, so it was "
            "registered undefined.",
        }
    ]


def test_a_centre_in_a_group_with_the_rest_defined_is_a_mixture_of_epimers(
    tmp_path: Path,
) -> None:
    # Threonine drawn (2S,3R), L-threonine (PubChem CID 6288), with an AND group on C3
    # alone: (2S,3R) with (2S,3S), L-threonine with L-allothreonine (CID 99289). They're
    # diastereomers, not enantiomers, so the mixture isn't a racemate, which IUPAC keeps for
    # an equimolar pair of enantiomers (Gold Book, R05025). Its C3 is the CXSMILES's atom 2.
    text = v3000("C[C@@H](O)[C@H](N)C(=O)O |&1:1|", "threonines") + v3000(
        "C[C@@H](O)C(N)C(=O)O |&1:1|", "C2 undefined"
    )
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    assert output["messages"] == [
        {
            "level": "warning",
            "text": "Record 1 (threonines): Its stereocentre C2 is in an AND group, both "
            "configurations together (two epimers, diastereomers of each other, as the rest of "
            "its stereochemistry is defined), which a standard InChIKey can't record, so it was "
            "registered undefined.",
        },
        # With its other centre undefined, how the two relate isn't known either.
        {
            "level": "warning",
            "text": "Record 2 (C2 undefined): Its stereocentre C2 is in an AND group, both "
            "configurations together, which a standard InChIKey can't record, so it was "
            "registered undefined.",
        },
    ]
    # Registered with C3 undefined and C2 kept: (2S)-threonine, either configuration at C3.
    keys = [c["values"]["inchikey"] for c in output["creates"]]
    assert keys == ["AYFVYJQAPQTCCC-NFJMKROFSA-N", "AYFVYJQAPQTCCC-UHFFFAOYSA-N"]


def _mirrored_3d(smiles: str, title: str) -> tuple[str, str]:
    """SDF records of a structure embedded in 3D, and of its mirror image."""
    from rdkit import Chem
    from rdkit.Chem import AllChem
    from rdkit.Geometry import Point3D

    mol = Chem.AddHs(Chem.MolFromSmiles(smiles))
    assert AllChem.EmbedMolecule(mol, randomSeed=7) == 0  # type: ignore[attr-defined]
    mirror = Chem.Mol(mol)
    conformer = mirror.GetConformer()
    for i in range(mirror.GetNumAtoms()):
        at = conformer.GetAtomPosition(i)
        conformer.SetAtomPosition(i, Point3D(-at.x, at.y, at.z))
    return tuple(  # type: ignore[return-value]
        f"{title}{n}\n" + Chem.MolToMolBlock(m).split("\n", 1)[1] + "$$$$\n"
        for n, m in ((1, mol), (2, mirror))
    )


def test_a_cumulenes_configuration_isnt_registered_without_it(tmp_path: Path) -> None:
    from rdkit import Chem
    from rdkit.Chem import rdinchi

    # Penta-2,3-diene (PubChem CID 136378) is axially chiral, as an allene with two
    # different groups at each end is. A standard InChI records which enantiomer from 3D
    # coordinates, in its /t layer (the InChI Trust's technical FAQ), but no SMILES RDKit
    # reads or writes holds it, so both would be CC=C=CC, which InChI reads as neither.
    allene = _mirrored_3d("CC=C=CC", "penta-2,3-diene ")
    inchis = {rdinchi.MolBlockToInchi(r.removesuffix("$$$$\n"))[0] for r in allene}
    assert inchis == {
        "InChI=1S/C5H8/c1-3-5-4-2/h3-4H,1-2H3/t5-/m0/s1",
        "InChI=1S/C5H8/c1-3-5-4-2/h3-4H,1-2H3/t5-/m1/s1",
    }
    # Hexa-2,3,4-triene, a cumulene of three double bonds, is E or Z, which InChI records
    # (/b) and no SMILES does either.
    (cumulene, _) = _mirrored_3d("CC=C=C=CC", "hexa-2,3,4-triene ")
    flat = Chem.MolFromSmiles("CC=C=CC")
    flat.SetProp("_Name", "penta-2,3-diene")
    text = "".join(allene) + cumulene + Chem.MolToMolBlock(flat) + "$$$$\n"
    result, output = imported(tmp_path, text)
    assert result.returncode == 1
    assert output is None
    error = last_error(result)
    assert "3 of 4 records can't be imported, so none was." in error
    for n, name, at in (
        (1, "penta-2,3-diene 1", "C3"),
        (2, "penta-2,3-diene 2", "C3"),
        (3, "hexa-2,3,4-triene 1", "C3, C4"),
    ):
        assert (
            f"Record {n} ({name}): its configuration at {at}, where its double bonds meet (an "
            "allene's or cumulene's), is in its standard InChI, from its drawing's "
            "coordinates, but no SMILES holds it, so its compound's SMILES would be another "
            "stereoisomer's too. Draw it without its configuration (in 2D, without wedges), "
            "say which it is in its name, and record what's known as the compound's "
            "stereochemistry."
        ) in error
    # Drawn without its configuration, it's the compound whichever enantiomer it is.
    result, output = imported(tmp_path, text, on_error="skip")
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    assert [c["values"]["inchikey"] for c in output["creates"]] == ["PODAMDNJNMAKAZ-UHFFFAOYSA-N"]
    assert output["metrics"] == {"records": 4, "skipped": 3}


def test_a_salt_field_isnt_counted_again_on_a_salt_registered_whole(tmp_path: Path) -> None:
    # Sodium aspirinate, C9H7NaO4, is 202.14 g/mol: its Salt field doesn't add sodium again.
    sodium = "CC(=O)Oc1ccccc1C(=O)[O-].[Na+]"
    result, output = imported(
        tmp_path, sdf_text((sodium, "aspirin sodium", {"Salt": "sodium"})), batches=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    batch = output["creates"][1]["values"]
    assert "salt" not in batch
    assert batch["formula_weight"] == 202.141
    assert {
        "level": "warning",
        "text": "Record 1 (aspirin sodium): Its salt field says sodium, which its structure, "
        "registered whole, already has, so its batch doesn't add it again. Register its "
        "parent to record the salt on its batch.",
    } in output["messages"]
    # One it doesn't have contradicts it.
    result, _ = imported(
        tmp_path, sdf_text((sodium, "aspirin sodium", {"Salt": "K"})), batches=True
    )
    assert result.returncode == 1
    assert (
        "its salt field says potassium, but its structure, registered whole, is a sodium salt"
        in last_error(result)
    )


def test_a_records_salt_is_known_whatever_its_stereochemistry(tmp_path: Path) -> None:
    text = sdf_text(
        # PubChem's sunitinib malate, its (S)-malate wedged in the molfile, and its Salt field.
        (f"{SUNITINIB}.C([C@@H](C(=O)O)O)C(=O)O", "sunitinib malate", {"Salt": "malate"}),
        (HYDROXYZINE_PAMOATE, "hydroxyzine pamoate", {}),
    )
    result, output = imported(tmp_path, text, parent=True, batches=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    malate, pamoate = (
        c["values"] for c in output["creates"] if c["type"] == "iki/small_molecule.CompoundBatch"
    )
    assert (malate["salt"], malate["salt_equivalents"], malate["formula_weight"]) == (
        "malate",
        1.0,
        532.569,
    )
    assert "formula_weight" not in pamoate
    said = [m["text"] for m in output["messages"]]
    assert not [t for t in said if t.startswith("Record 1") and "formula weight" in t]
    assert (
        f"Record 2 (hydroxyzine pamoate): Its batch has no formula weight: {PAMOIC_ACID} is "
        "part of the material, but not a salt or solvent Iki knows, so what it adds isn't "
        "known. Its structure as drawn weighs 763.287 g/mol for each parent."
    ) in said


def test_a_salts_ids_arent_given_to_its_parent(tmp_path: Path) -> None:
    text = sdf_text(
        ("CC(=O)Oc1ccccc1C(=O)[O-].[Na+]", "aspirin sodium", {"CAS": "493-53-8", "CID": "10176596"})
    )
    result, output = imported(tmp_path, text, parent=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    (compound,) = output["creates"]
    assert compound["values"]["inchikey"] == ASPIRIN_KEY
    assert "cas_number" not in compound["values"]
    assert "pubchem_cid" not in compound["values"]
    assert output["messages"] == [
        {
            "level": "warning",
            "text": "Record 1 (aspirin sodium): Its name, aspirin sodium, is its structure's "
            "as drawn, not its parent's, so its compound wasn't given it.",
        },
        {
            "level": "warning",
            "text": "Record 1 (aspirin sodium): Its CAS number and PubChem CID are its "
            "structure's as drawn, not its parent's, so they were left out.",
        },
    ]


def test_a_salt_field_gives_a_salt_of_an_acid_iki_knows_as_a_counterion(
    tmp_path: Path,
) -> None:
    text = sdf_text(
        # Benzoic acid (CID 243, 122.12 g/mol), as its sodium salt: sodium benzoate, CID
        # 517055, 144.10 g/mol. Benzoate is a salt Iki knows, but nothing here is benzoate
        # of something else: the acid is the parent.
        ("C1=CC=C(C=C1)C(=O)O", "benzoic acid", {"Salt": "Na"}),
        # Acetic acid as sodium acetate (CID 31372): 82.03 g/mol.
        ("CC(=O)O", "acetic acid", {"Salt": "Na"}),
        # 2-Methylbenzoic acid, which isn't a counterion, as its sodium salt: 158.13 g/mol.
        ("Cc1ccccc1C(=O)O", "o-toluic acid", {"Salt": "sodium"}),
        # Drawn as sodium benzoate, whole, its Salt field adds nothing.
        ("[Na+].[O-]C(=O)c1ccccc1", "sodium benzoate", {"Salt": "Na"}),
        # Drawn with its water, the acid is still the parent: sodium acetate trihydrate,
        # C2H9NaO5 (CID 23665404), 136.08 g/mol; sodium benzoate monohydrate, 162.12.
        ("CC(=O)O.O.O.O", "sodium acetate trihydrate", {"Salt": "Na"}),
        ("OC(=O)c1ccccc1.O", "sodium benzoate hydrate", {"Salt": "Na"}),
    )
    for parent in (False, True):
        result, output = imported(tmp_path, text, parent=parent, batches=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert output is not None
        batches = [
            c["values"]
            for c in output["creates"]
            if c["type"] == "iki/small_molecule.CompoundBatch"
        ]
        assert [(b.get("salt"), b["formula_weight"]) for b in batches] == [
            ("sodium", 144.105),
            ("sodium", 82.034),
            ("sodium", 158.132),
            (None, 144.105),
            ("sodium", 136.079),
            ("sodium", 162.12),
        ]
        # The parent is the acid, its water the solvate; registered whole, the water is in
        # the compound.
        assert [(b.get("solvate"), b.get("solvate_equivalents")) for b in batches[4:]] == (
            [("water", 3.0), ("water", 1.0)] if parent else [(None, None)] * 2
        )
        assert not [m for m in output["messages"] if "formula weight" in m["text"]]


#: Atorvastatin, as PubChem gives it in its calcium salt (CID 60822), its acid's hydrogen on.
ATORVASTATIN = (
    "CC(C)C1=C(C(=C(N1CC[C@H](C[C@H](CC(=O)O)O)O)C2=CC=C(C=C2)F)C3=CC=CC=C3)C(=O)NC4=CC=CC=C4"
)
#: Folinic acid (leucovorin), as PubChem gives it in its calcium salt (CID 135403647), its
#: two acids' hydrogens on.
FOLINIC_ACID = "C1C(N(C2=C(N1)N=C(NC2=O)N)C=O)CNC3=CC=C(C=C3)C(=O)N[C@@H](CCC(=O)O)C(=O)O"
#: Esomeprazole, as PubChem gives it in its magnesium salt (CID 9568613), whose
#: benzimidazole gives the magnesium its hydrogen.
ESOMEPRAZOLE = "CC1=CN=C(C(=C1OC)C)C[S@](=O)C2=NC3=C(N2)C=CC(=C3)OC"


def test_a_divalent_metals_equivalents_are_from_charge_balance(tmp_path: Path) -> None:
    text = sdf_text(
        # Calcium acetate, Ca(C2H3O2)2 (CID 6116, 158.17 g/mol): one calcium for two
        # acetates, each with one acid's hydrogen to give, so 79.083 g/mol for each.
        ("CC(=O)O", "calcium acetate", {"Salt": "Ca"}),
        # Atorvastatin calcium (CID 60822, C66H68CaF2N4O10, 1155.36 g/mol): 577.681 each.
        (ATORVASTATIN, "atorvastatin calcium", {"Salt": "calcium"}),
        # Zinc gluconate (CID 443445, C12H22O14Zn, 455.68 g/mol): 227.842 each.
        ("C([C@H]([C@H]([C@@H]([C@H](C(=O)O)O)O)O)O)O", "zinc gluconate", {"Salt": "Zn"}),
        # Magnesium valproate (CID 147515, C16H30MgO4, 310.72 g/mol): 155.358 each.
        ("CCCC(CCC)C(=O)O", "magnesium valproate", {"Salt": "Mg"}),
        # Folinic acid has two acids: calcium folinate is one to one (CID 135403647,
        # C20H21CaN7O7, 511.5 g/mol), but a calcium of two hydrogen folinates balances too.
        (FOLINIC_ACID, "calcium folinate", {"Salt": "Ca"}),
        (FOLINIC_ACID, "calcium folinate", {"Salt": "Ca", "Salt_eq": "1"}),
        # Esomeprazole magnesium (CID 9568613, 713.1 g/mol) is two to one, but by its
        # benzimidazole's NH, which isn't an acid Iki counts.
        (ESOMEPRAZOLE, "esomeprazole magnesium", {"Salt": "Mg"}),
        (ESOMEPRAZOLE, "esomeprazole magnesium", {"Salt": "Mg", "Salt_eq": "0.5"}),
    )
    for parent in (False, True):
        result, output = imported(tmp_path, text, parent=parent, batches=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert output is not None
        batches = [
            c["values"]
            for c in output["creates"]
            if c["type"] == "iki/small_molecule.CompoundBatch"
        ]
        assert [
            (b["salt"], b.get("salt_equivalents"), b.get("formula_weight")) for b in batches
        ] == [
            ("calcium", 0.5, 79.083),
            ("calcium", 0.5, 577.681),
            ("zinc", 0.5, 227.842),
            ("magnesium", 0.5, 155.358),
            ("calcium", None, None),
            ("calcium", 1.0, 511.508),
            ("magnesium", None, None),
            ("magnesium", 0.5, 356.569),
        ]
        said = [m["text"] for m in output["messages"] if "salt field says" in m["text"]]
        assert said == [
            *(
                f"Record {n} ({name}): Its salt field says {salt} but not how many for each "
                f"of its compound. {salt.capitalize()} is 2+, and its compound has one acid "
                "group to give a hydrogen for it (a carboxylic acid), so by charge balance "
                f"it's 0.5: one {salt} for two of it."
                for n, name, salt in (
                    (1, "calcium acetate", "calcium"),
                    (2, "atorvastatin calcium", "calcium"),
                    (3, "zinc gluconate", "zinc"),
                    (4, "magnesium valproate", "magnesium"),
                )
            ),
            "Record 5 (calcium folinate): Its batch has no formula weight: its salt field "
            "says calcium but not how many for each of its compound. Calcium is 2+, and its "
            "compound has 2 acid groups (carboxylic acids), so charge balance doesn't say how "
            "many: give its Salt_eq field, such as 1 for one calcium for each of it.",
            "Record 7 (esomeprazole magnesium): Its batch has no formula weight: its salt "
            "field says magnesium but not how many for each of its compound. Magnesium is 2+, "
            "and Iki finds no acid group in its compound to give a hydrogen for it, so charge "
            "balance doesn't say how many: give its Salt_eq field, such as 0.5 for one "
            "magnesium for two of it.",
        ]
    # Recomputing a batch whose equivalents are blank does the same.
    batches = [
        {**entity(i, salt=salt), "included": {"compound": [entity(100 + i, smiles=smiles)]}}
        for i, (smiles, salt) in enumerate(
            [("CC(=O)O", "calcium"), (FOLINIC_ACID, "calcium"), ("CC(=O)O", "sodium")], start=1
        )
    ]
    batches[1]["values"]["formula_weight"] = 609.6
    result, recomputed = script(
        tmp_path, "descriptors.py", {"inputs": {"batch": batches}}, "batches"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert recomputed is not None
    assert recomputed["updates"]["batch"] == [
        {"entity": batches[0]["id"], "values": {"formula_weight": 79.083}},
        {"entity": batches[1]["id"], "values": {"formula_weight": None}},
        # A sodium salt, one equivalent unless it says otherwise: sodium acetate, 82.034.
        {"entity": batches[2]["id"], "values": {"formula_weight": 82.034}},
    ]
    assert [(m["level"], m["text"]) for m in recomputed["messages"]] == [
        (
            "info",
            "X-1's salt, calcium, has no equivalents. Calcium is 2+, and its compound has one "
            "acid group to give a hydrogen for it (a carboxylic acid), so by charge balance "
            "it's 0.5: one calcium for two of it.",
        ),
        (
            "warning",
            "X-2 has no formula weight: its salt, calcium, has no equivalents. Calcium is 2+, "
            "and its compound has 2 acid groups (carboxylic acids), so charge balance doesn't "
            "say how many: give its salt equivalents, such as 1 for one calcium for each of it.",
        ),
    ]


def test_a_salt_field_isnt_counted_again_on_a_salt_drawn_with_an_isotope(
    tmp_path: Path,
) -> None:
    # Lithium-6 aspirinate (185.166 g/mol: aspirinate's 179.151 and lithium-6's 6.015, NIST)
    # with its Salt field Li: its lithium is drawn, as lithium-6, so the field adds none.
    lithium_6 = "CC(=O)Oc1ccccc1C(=O)[O-].[6Li+]"
    text = sdf_text((lithium_6, "lithium-6 aspirinate", {"Salt": "Li"}))
    result, output = imported(tmp_path, text, batches=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    batch = output["creates"][1]["values"]
    assert "salt" not in batch
    assert batch["formula_weight"] == 185.166
    assert (
        "Record 1 (lithium-6 aspirinate): Its salt field says lithium, which its structure, "
        "registered whole, already has, so its batch doesn't add it again. Register its "
        "parent to record the salt on its batch."
    ) in [m["text"] for m in output["messages"]]
    # Its parent leaves the lithium-6 out, as a part Iki doesn't know: its batch has no
    # formula weight, and its field doesn't make it a lithium salt again.
    result, output = imported(tmp_path, text, batches=True, parent=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    batch = output["creates"][1]["values"]
    assert "salt" not in batch
    assert "formula_weight" not in batch
    # Another salt contradicts it, either way.
    for parent in (False, True):
        result, _ = imported(
            tmp_path,
            sdf_text((lithium_6, "lithium-6 aspirinate", {"Salt": "Na"})),
            batches=True,
            parent=parent,
        )
        assert result.returncode == 1
        assert "its salt field says sodium, but its structure" in last_error(result)
        assert "is a lithium salt" in last_error(result)


def test_salt_equivalents_must_agree_with_a_salt_registered_whole(tmp_path: Path) -> None:
    sodium = "CC(=O)Oc1ccccc1C(=O)[O-].[Na+]"
    result, _ = imported(
        tmp_path,
        sdf_text((sodium, "aspirin sodium", {"Salt": "Na", "Salt_eq": "2"})),
        batches=True,
    )
    assert result.returncode == 1
    assert (
        "Record 1 (aspirin sodium): its salt equivalents field says 2, but its structure has "
        "1 sodium for each parent"
    ) in last_error(result)
    # Calcium aspirinate, drawn two to one, agrees with 0.5.
    calcium = "[Ca+2].CC(=O)Oc1ccccc1C(=O)[O-].CC(=O)Oc1ccccc1C(=O)[O-]"
    result, _ = imported(
        tmp_path,
        sdf_text((calcium, "aspirin calcium", {"Salt": "Ca", "Salt_eq": "0.5"})),
        batches=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_salt_fields_names_and_ids_arent_its_compounds_without_batches(
    tmp_path: Path,
) -> None:
    # Aspirin drawn neutral, its Salt field and its supplier's title, CAS number, CID and
    # synonym those of its sodium salt (CAS 493-53-8, CID 23666729): aspirin is CID 2244.
    text = sdf_text(
        (
            ASPIRIN,
            "aspirin sodium",
            {
                "Salt": "Na",
                "CAS": "493-53-8",
                "PUBCHEM_COMPOUND_CID": "23666729",
                "Synonyms": "sodium acetylsalicylate",
            },
        )
    )
    for parent in (False, True):
        result, output = imported(tmp_path, text, parent=parent)
        assert result.returncode == 0, result.stdout + result.stderr
        assert output is not None
        (compound,) = output["creates"]
        assert compound["values"] == {
            "smiles": ASPIRIN,
            "inchikey": ASPIRIN_KEY,
            "inchi": "InChI=1S/C9H8O4/c1-6(10)13-8-5-3-2-4-7(8)9(11)12/h2-5H,1H3,(H,11,12)",
        }
        assert [m["text"] for m in output["messages"]] == [
            "Record 1 (aspirin sodium): Its name, aspirin sodium, is a sodium salt's, by its "
            "salt field, not its compound's, so its compound wasn't given it.",
            "Record 1 (aspirin sodium): Its CAS number, PubChem CID and synonyms are a sodium "
            "salt's, by its salt field, not its compound's, so they were left out.",
            "These SD fields aren't fields of compounds, so they weren't kept: Salt (1).",
        ]


@pytest.mark.parametrize("says", ["N/A", "n/a", "None", "free base", "Free acid", "-"])
def test_a_salt_field_saying_there_is_none_is_no_salt(tmp_path: Path, says: str) -> None:
    # "N/A", stripped to its letters, would read as Na: aspirin isn't its sodium salt.
    text = sdf_text((ASPIRIN, "aspirin", {"Salt": says}))
    result, output = imported(tmp_path, text, batches=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    compound, batch = output["creates"]
    assert compound["values"]["name"] == "aspirin"
    assert "salt" not in batch["values"]
    assert batch["values"]["formula_weight"] == 180.159


def test_a_salts_names_and_ids_stay_with_its_batch(tmp_path: Path) -> None:
    text = sdf_text(
        # Aspirin drawn neutral, its Salt field and its supplier's title, CAS number, CID and
        # synonym those of its sodium salt (CID 23666729; aspirin is 2244).
        (
            ASPIRIN,
            "aspirin sodium",
            {
                "Salt": "Na",
                "CAS": "493-53-8",
                "PUBCHEM_COMPOUND_CID": "23666729",
                "Synonyms": "sodium acetylsalicylate",
                "Lot": "B-17",
            },
        ),
        # Drawn as the salt, its synonym is the salt's too.
        (
            "CC(=O)Oc1ccccc1C(=O)[O-].[Na+]",
            "aspirin sodium",
            {"Synonyms": "sodium 2-acetyloxybenzoate", "Description": "White powder"},
        ),
    )
    result, output = imported(tmp_path, text, parent=True, batches=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    neutral, neutral_batch, drawn, drawn_batch = output["creates"]
    assert neutral["values"] == {
        "smiles": ASPIRIN,
        "inchikey": ASPIRIN_KEY,
        "inchi": "InChI=1S/C9H8O4/c1-6(10)13-8-5-3-2-4-7(8)9(11)12/h2-5H,1H3,(H,11,12)",
    }
    assert neutral_batch["values"] == {
        "compound": {"$create": "record-1"},
        "name": "aspirin sodium",
        "structure_as_drawn": ASPIRIN,
        "salt": "sodium",
        "salt_equivalents": 1.0,
        "formula_weight": 202.141,
        "supplier_lot": "B-17",
    }
    assert drawn["values"]["inchikey"] == ASPIRIN_KEY
    assert "synonyms" not in drawn["values"]
    assert drawn["values"]["description"] == "White powder"
    assert drawn_batch["values"]["name"] == "aspirin sodium"
    said = [m["text"] for m in output["messages"]]
    assert said == [
        "Record 1 (aspirin sodium): Its name, aspirin sodium, is its batch's, a sodium salt "
        "by its salt field, not its compound's, so its compound wasn't given it; its batch "
        "was.",
        "Record 1 (aspirin sodium): Its CAS number, PubChem CID and synonyms are its batch's, "
        "a sodium salt by its salt field, not its compound's, so they were left out.",
        "Record 2 (aspirin sodium): Its name, aspirin sodium, is its structure's as drawn, "
        "not its parent's, so its compound wasn't given it; its batch was.",
        "Record 2 (aspirin sodium): Its synonyms are its structure's as drawn, not its "
        "parent's, so they were left out.",
    ]


def test_a_mass_without_a_unit_isnt_taken_as_an_amount(tmp_path: Path) -> None:
    text = sdf_text(
        # A bare 180.159 in a Mass field is glucose's molecular mass as likely as an amount.
        ("OC[C@H]1OC(O)[C@H](O)[C@@H](O)[C@@H]1O", "glucose", {"Mass": "180.159"}),
        ("OC[C@H]1OC(O)[C@H](O)[C@@H](O)[C@@H]1O", "glucose", {"Amount": "25"}),
        # With a unit, or in a field whose name gives one, it's the amount.
        ("OC[C@H]1OC(O)[C@H](O)[C@@H](O)[C@@H]1O", "glucose", {"Mass": "1.5 g"}),
        ("OC[C@H]1OC(O)[C@H](O)[C@@H](O)[C@@H]1O", "glucose", {"Mass_mg": "25"}),
    )
    result, output = imported(tmp_path, text, batches=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    batches = [
        c["values"] for c in output["creates"] if c["type"] == "iki/small_molecule.CompoundBatch"
    ]
    assert [b.get("amount") for b in batches] == [None, None, 1500.0, 25.0]
    assert [m["text"] for m in output["messages"]] == [
        f"Record {n} (glucose): Its {field} field, {value!r}, has no unit, so it wasn't "
        "taken as its batch's amount: a bare number there could be its molecular mass. Give "
        "the amount with its unit, such as 25 mg, or in an Amount_mg field."
        for n, field, value in ((1, "Mass", "180.159"), (2, "Amount", "25"))
    ]


def test_a_large_file_goes_in_a_creates_file(tmp_path: Path) -> None:
    text = sdf_text(*((f"C{'C' * (i % 40)}O", f"alcohol {i}", {}) for i in range(1200)))
    result, output = imported(tmp_path, text)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    assert output["creates_file"] == {"$file": "creates.ndjson"}
    lines = (tmp_path / "out" / "creates.ndjson").read_text().splitlines()
    assert len(lines) == 1200
    assert json.loads(lines[1])["values"]["smiles"] == "CCO"


def test_a_record_that_cant_be_read_fails_the_import_naming_it(tmp_path: Path) -> None:
    good = sdf_text((ASPIRIN, "aspirin", {}))
    # Its carbon with five bonds, and a record with no structure.
    bad = good.replace("aspirin", "pentavalent", 1).replace(
        "  1  2  1  0\n", "  1  2  1  0\n  1  3  2  0\n  1  4  2  0\n", 1
    )
    text = good + bad.replace(" 13 13", " 13 15", 1) + "nothing\n\n$$$$\n"
    result, output = imported(tmp_path, text, name_field="")
    assert result.returncode == 1
    assert output is None
    error = last_error(result)
    assert error.startswith("::error library.sdf: 2 of 3 records can't be imported, so none was.")
    assert "Record 2 (pentavalent): its structure isn't valid: Explicit valence" in error
    assert "Record 3 (nothing): it has no structure (no M  END line)." in error
    result, output = imported(tmp_path, text, on_error="skip")
    assert result.returncode == 0
    assert output is not None
    assert [c["values"]["name"] for c in output["creates"]] == ["aspirin"]
    assert output["metrics"] == {"records": 3, "skipped": 2}


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"Purity": "high"}, "its purity 'high' isn't a number"),
        ({"Salt": "unobtainium"}, "its salt 'unobtainium' isn't one Iki knows"),
        ({"Received": "last week"}, "its received date 'last week' isn't a date"),
        ({"Salt": "HCl"}, "its salt field says hydrochloride, but its structure is a sodium salt"),
        (
            {"Salt": "Na", "Salt_Eq": "2"},
            "its salt equivalents field says 2, but its structure has 1 sodium for each parent",
        ),
    ],
)
def test_batch_fields_that_cant_be_read_name_their_record(
    tmp_path: Path, fields: dict[str, str], error: str
) -> None:
    text = sdf_text(("CC(=O)Oc1ccccc1C(=O)[O-].[Na+]", "aspirin sodium", fields))
    result, _ = imported(tmp_path, text, parent=True, batches=True)
    assert result.returncode == 1
    assert f"Record 1 (aspirin sodium): {error}" in last_error(result)


# --- Fetching from PubChem and ChEMBL ------------------------------------------------------------


class Answer:
    """What a fake service answers a request with: JSON, or an HTTP error."""

    def __init__(self, body: Any = None, status: int = 200) -> None:
        self.body = json.dumps(body).encode()
        self.status = status

    def __enter__(self) -> "Answer":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self.body


class Service:
    """A fake PubChem or ChEMBL: each request's URL (and form) answered from `answers`,
    by the first key it starts with; each request recorded."""

    def __init__(self, answers: dict[str, list[Answer]]) -> None:
        self.answers = answers
        self.asked: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> Answer:
        url = request.full_url
        data = request.data
        form = dict(urllib.parse.parse_qsl(data.decode())) if isinstance(data, bytes) else {}
        self.asked.append((url, form))
        key = next(k for k in self.answers if url.startswith(k) or f"{url} {form}" == k)
        answer = self.answers[key].pop(0) if len(self.answers[key]) > 1 else self.answers[key][0]
        if answer.status != 200:
            raise urllib.error.HTTPError(url, answer.status, "Busy", Message(), io.BytesIO(b""))
        return answer


@pytest.fixture
def scripts(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Imports the plugin's scripts (`scripts.import_module("pubchem")`), with nothing
    slept, and forgets them after."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    yield importlib
    for name in ("chemistry", "descriptors", "fetching", "pubchem", "chembl"):
        sys.modules.pop(name, None)


def fake(monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[Answer]]) -> Service:
    service = Service(answers)
    monkeypatch.setattr("urllib.request.urlopen", service)
    return service


PUG = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound"
ASPIRIN_ROW = {
    "CID": 2244,
    "SMILES": "CC(=O)OC1=CC=CC=C1C(=O)O",
    "InChI": "InChI=1S/C9H8O4/c1-6(10)13-8-5-3-2-4-7(8)9(11)12/h2-5H,1H3,(H,11,12)",
    "InChIKey": ASPIRIN_KEY,
    "IUPACName": "2-acetyloxybenzoic acid",
    "Title": "Aspirin",
}
ASPIRIN_SYNONYMS = {
    "InformationList": {
        "Information": [
            {"CID": 2244, "Synonym": ["aspirin", "ACETYLSALICYLIC ACID", "50-78-2", "50-78-3"]}
        ]
    }
}


def test_pubchem_gives_a_compound_its_cid_names_and_cas_number(
    scripts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = fake(
        monkeypatch,
        {
            f"{PUG}/name/property/": [
                Answer(status=503),
                Answer({"PropertyTable": {"Properties": [ASPIRIN_ROW]}}),
            ],
            f"{PUG}/cid/2244/synonyms/JSON": [Answer(ASPIRIN_SYNONYMS)],
        },
    )
    pubchem = scripts.import_module("pubchem")
    output = pubchem.fetch({"params": {"identifiers": "acetylsalicylic acid\n"}})
    assert service.asked[0] == (
        f"{PUG}/name/property/SMILES,InChI,InChIKey,IUPACName,Title/JSON",
        {"name": "acetylsalicylic acid"},
    )
    # Asked again after PubChem was busy.
    assert len(service.asked) == 3
    (create,) = output["creates"]
    assert create["type"] == "iki/small_molecule.Compound"
    assert create["values"] == {
        "smiles": ASPIRIN,
        "inchikey": ASPIRIN_KEY,
        "inchi": ASPIRIN_ROW["InChI"],
        "name": "Aspirin",
        "pubchem_cid": "2244",
        "synonyms": ["2-acetyloxybenzoic acid", "ACETYLSALICYLIC ACID", "50-78-2", "50-78-3"],
        "cas_number": "50-78-2",
    }
    origin = create["origin"]
    assert {k: origin[k] for k in ("source", "id", "url")} == {
        "source": "PubChem",
        "id": "2244",
        "url": "https://pubchem.ncbi.nlm.nih.gov/compound/2244",
    }
    assert re.match(r"^[0-9a-f]{64}$", origin["sha256"])
    assert origin["retrieved_at"].endswith("+00:00")
    assert output["metrics"] == {"compounds": 1}
    assert not list(
        schemas.check("run-output", {"apiVersion": "iki/v1", "kind": "RunOutput", **output})
    )


@pytest.mark.parametrize(
    ("identifier", "namespace"),
    [
        ("2244", ("cid", "2244")),
        ("CID 2244", ("cid", "2244")),
        ("bsynrymutxbxsq-uhfffaoysa-n", ("inchikey", ASPIRIN_KEY)),
        ("1,3-dichloro/benzene", ("name", "1,3-dichloro/benzene")),
    ],
)
def test_pubchem_is_asked_by_what_each_identifier_is(
    scripts: Any, identifier: str, namespace: tuple[str, str]
) -> None:
    assert scripts.import_module("pubchem").namespace(identifier) == namespace


def test_what_pubchem_doesnt_have_fails_the_fetch_naming_it(
    scripts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake(monkeypatch, {f"{PUG}/name/property/": [Answer(status=404)]})
    pubchem = scripts.import_module("pubchem")
    chemistry = scripts.import_module("chemistry")
    with pytest.raises(
        chemistry.UnreadableError, match="PubChem has no compound with the name zzz"
    ):
        pubchem.fetch({"params": {"identifiers": "zzz"}})


def test_pubchem_that_stays_busy_fails_the_fetch(
    scripts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = fake(monkeypatch, {f"{PUG}/cid/property/": [Answer(status=503)]})
    pubchem = scripts.import_module("pubchem")
    chemistry = scripts.import_module("chemistry")
    with pytest.raises(chemistry.UnreadableError, match="PubChem answered 503 Busy"):
        pubchem.fetch({"params": {"identifiers": "2244"}})
    assert len(service.asked) == 4


CHEMBL = "https://www.ebi.ac.uk/chembl/api/data"
CHEMBL25 = {
    "molecule_chembl_id": "CHEMBL25",
    "pref_name": "ASPIRIN",
    "molecule_type": "Small molecule",
    "molecule_structures": {
        "canonical_smiles": "CC(=O)Oc1ccccc1C(=O)O",
        "standard_inchi_key": ASPIRIN_KEY,
    },
    "molecule_synonyms": [
        {"molecule_synonym": "Aspirin", "syn_type": "BAN"},
        {"molecule_synonym": "Acetylsalicylic Acid", "syn_type": "OTHER"},
    ],
    "molecule_hierarchy": {"molecule_chembl_id": "CHEMBL25", "parent_chembl_id": "CHEMBL25"},
}


def test_chembl_gives_a_compound_its_id_and_names_from_its_release(
    scripts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = fake(
        monkeypatch,
        {
            f"{CHEMBL}/status.json": [Answer({"chembl_db_version": "ChEMBL_37"})],
            f"{CHEMBL}/molecule.json?pref_name__iexact=aspirin": [
                Answer({"molecules": [{"molecule_chembl_id": "CHEMBL25"}]})
            ],
            f"{CHEMBL}/molecule/CHEMBL25.json": [Answer(CHEMBL25)],
            f"{CHEMBL}/molecule/{ASPIRIN_KEY}.json": [Answer(CHEMBL25)],
        },
    )
    chembl = scripts.import_module("chembl")
    output = chembl.fetch({"params": {"identifiers": f"aspirin\n{ASPIRIN_KEY}"}})
    assert [url for url, _ in service.asked] == [
        f"{CHEMBL}/status.json",
        f"{CHEMBL}/molecule.json?pref_name__iexact=aspirin&only=molecule_chembl_id&limit=20",
        f"{CHEMBL}/molecule/CHEMBL25.json",
        f"{CHEMBL}/molecule/{ASPIRIN_KEY}.json",
    ]
    # The same molecule twice is one compound.
    (create,) = output["creates"]
    assert create["values"] == {
        "smiles": ASPIRIN,
        "inchikey": ASPIRIN_KEY,
        "inchi": "InChI=1S/C9H8O4/c1-6(10)13-8-5-3-2-4-7(8)9(11)12/h2-5H,1H3,(H,11,12)",
        "name": "ASPIRIN",
        "chembl_id": "CHEMBL25",
        "synonyms": ["Acetylsalicylic Acid"],
    }
    origin = create["origin"]
    assert {k: origin[k] for k in ("source", "id", "version", "url")} == {
        "source": "ChEMBL",
        "id": "CHEMBL25",
        "version": "ChEMBL_37",
        "url": "https://www.ebi.ac.uk/chembl/explore/compound/CHEMBL25",
    }
    assert not list(
        schemas.check("run-output", {"apiVersion": "iki/v1", "kind": "RunOutput", **output})
    )


def test_a_chembl_salts_parent_takes_neither_its_id_nor_its_names(
    scripts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    salt = {
        "molecule_chembl_id": "CHEMBL2260549",
        "pref_name": "ASPIRIN SODIUM",
        "molecule_type": "Small molecule",
        "molecule_structures": {"canonical_smiles": "CC(=O)Oc1ccccc1C(=O)[O-].[Na+]"},
        "molecule_synonyms": [{"molecule_synonym": "Sodium acetylsalicylate"}],
        "molecule_hierarchy": {"parent_chembl_id": "CHEMBL25"},
    }
    fake(
        monkeypatch,
        {
            f"{CHEMBL}/status.json": [Answer({"chembl_db_version": "ChEMBL_37"})],
            f"{CHEMBL}/molecule/CHEMBL2260549.json": [Answer(salt)],
        },
    )
    chembl = scripts.import_module("chembl")
    output = chembl.fetch({"params": {"identifiers": "CHEMBL2260549", "parent": True}})
    (create,) = output["creates"]
    assert create["values"]["inchikey"] == ASPIRIN_KEY
    assert {"name", "synonyms", "chembl_id"}.isdisjoint(create["values"])
    assert output["messages"] == [
        {
            "level": "warning",
            "text": "CHEMBL2260549 (CHEMBL2260549) is CC(=O)Oc1ccccc1C(=O)[O-].[Na+]; its "
            "parent was registered, which isn't that molecule, so it doesn't take its ChEMBL "
            "ID or names (ChEMBL's parent is CHEMBL25).",
        }
    ]


def test_a_chembl_molecule_without_a_structure_fails_the_fetch(
    scripts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake(
        monkeypatch,
        {
            f"{CHEMBL}/status.json": [Answer({"chembl_db_version": "ChEMBL_37"})],
            f"{CHEMBL}/molecule/CHEMBL1201580.json": [
                Answer(
                    {
                        "molecule_chembl_id": "CHEMBL1201580",
                        "molecule_type": "Antibody",
                        "molecule_structures": None,
                    }
                )
            ],
        },
    )
    chembl = scripts.import_module("chembl")
    chemistry = scripts.import_module("chemistry")
    with pytest.raises(
        chemistry.UnreadableError,
        match=r"no structure to register \(its molecule type is Antibody\)",
    ):
        chembl.fetch({"params": {"identifiers": "chembl1201580"}})


# --- Conformers ----------------------------------------------------------------------------------


def conformers(tmp_path: Path, smiles: str, seed: int = 42, **params: Any) -> Any:
    (tmp_path / "input.json").write_text(
        json.dumps(
            {"params": params, "inputs": {"compound": [entity(1, smiles=smiles, name="it")]}}
        )
    )
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    return subprocess.run(  # noqa: S603 - our own script
        [sys.executable, str(SCRIPTS / "conformers.py")],
        env={
            "IKI_INPUT": str(tmp_path / "input.json"),
            "IKI_OUTPUT": str(tmp_path / "out.json"),
            "IKI_OUT_DIR": str(out_dir),
            "IKI_SEED": str(seed),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def test_conformers_are_embedded_minimised_and_ranked_by_energy(tmp_path: Path) -> None:
    from rdkit import Chem

    result = conformers(tmp_path, "CCCCCCO", count=20)
    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads((tmp_path / "out.json").read_text())
    assert not list(schemas.check("run-output", output))
    (create,) = output["creates"]
    values = create["values"]
    assert create["type"] == "iki/small_molecule.ConformerSet"
    assert values["compound"] == entity(1)["id"]
    assert values["conformers"] == {
        "$file": "conformers.sdf",
        "media_type": "chemical/x-mdl-sdfile",
    }
    assert values["structure"] == {"$file": "conformers.pdb", "media_type": "chemical/x-pdb"}
    assert values["method"] == (
        "RDKit ETKDGv3, 20 embedded, pruned at 0.5 Å heavy-atom RMSD; minimised in MMFF94, "
        "up to 2,000 steps"
    )
    energies = values["energies"]
    assert 1 < values["count"] == len(energies) <= 20
    assert energies == sorted(energies)
    assert values["energy_window"] == pytest.approx(energies[-1] - energies[0], abs=1e-3)
    written = list(Chem.SDMolSupplier(str(tmp_path / "out" / "conformers.sdf"), removeHs=False))
    assert len(written) == values["count"]
    assert written[0].GetProp("_Name") == "it conformer 1"
    assert float(written[0].GetProp("relative_energy")) == 0
    assert written[0].GetConformer().Is3D()
    # With its hydrogens: hexanol is C6H14O.
    assert written[0].GetNumAtoms() == 21
    assert Chem.MolToSmiles(Chem.RemoveHs(written[0])) == "CCCCCCO"
    pdb = (tmp_path / "out" / "conformers.pdb").read_text()
    assert pdb.count("MODEL ") == values["count"] and pdb.endswith("ENDMDL\nEND\n")
    # The same seed, the same conformers.
    first = (tmp_path / "out" / "conformers.sdf").read_text()
    assert conformers(tmp_path, "CCCCCCO", count=20).returncode == 0
    assert (tmp_path / "out" / "conformers.sdf").read_text() == first


def test_conformers_that_werent_minimised_say_so(tmp_path: Path) -> None:
    # MMFF94 has no parameters for selenium (Halgren, 1996), so dimethyl selenide's are
    # left as embedded, with no energies.
    result = conformers(tmp_path, "C[Se]C", count=5)
    assert result.returncode == 0, result.stdout + result.stderr
    values = json.loads((tmp_path / "out.json").read_text())["creates"][0]["values"]
    assert values["method"].endswith(
        "; not minimised (MMFF94 has no parameters for it), in no order"
    )
    assert "energies" not in values
    # With no minimising steps, they're as embedded, ranked by their energies as they are.
    result = conformers(tmp_path, "CCCCCCO", count=5, iterations=0)
    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads((tmp_path / "out.json").read_text())
    values = output["creates"][0]["values"]
    assert values["method"].endswith("; not minimised, ranked by their MMFF94 energies as embedded")
    assert values["energies"] == sorted(values["energies"])
    assert output["messages"] == []
    # The set's type says so: their energies are single points, not minima.
    described = (SCRIPTS.parent / "types" / "conformer_set.yaml").read_text()
    assert "asked for no\n  minimising steps, as embedded, each energy a single point" in described


def test_a_salt_has_no_conformers_of_its_own(tmp_path: Path) -> None:
    result = conformers(tmp_path, "CC(=O)Oc1ccccc1C(=O)[O-].[Na+]")
    assert result.returncode == 1
    assert last_error(result) == (
        "::error X-1 has more than one part (a salt, say): register its parent, and "
        "generate its conformers."
    )


# --- Searching -----------------------------------------------------------------------------------

LIBRARY = [
    entity(1, smiles=ASPIRIN, name="aspirin"),
    entity(2, smiles="O=C(O)c1ccccc1O", name="salicylic acid"),
    entity(3, smiles="CC(=O)Nc1ccc(O)cc1", name="paracetamol"),
    entity(4, smiles="c1ccccc1", name="benzene"),
    entity(5, smiles="COC(=O)c1ccccc1OC(C)=O", name="methyl acetylsalicylate"),
]


def test_similar_compounds_are_a_collection_most_similar_first(tmp_path: Path) -> None:
    result, output = script(
        tmp_path,
        "search.py",
        {
            "params": {"threshold": 0.3},
            "inputs": {"compounds": LIBRARY, "query": [LIBRARY[0]]},
        },
        "similar",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    (collection,) = output["creates"]
    assert collection["type"] == "iki/core.Collection"
    members = collection["values"]["members"]
    assert members[0] == LIBRARY[0]["id"]
    assert set(members) == {LIBRARY[0]["id"], LIBRARY[1]["id"], LIBRARY[4]["id"]}
    assert collection["values"]["name"] == "Like aspirin (Tanimoto ≥ 0.3)"
    assert collection["values"]["description"].startswith(
        "3 of 5 compounds with a Tanimoto similarity of at least 0.3 to aspirin, by Morgan "
        "fingerprints (radius 2, 2,048 bits), most similar first: X-1 1.00, X-5 "
    )
    assert output["metrics"] == {"hits": 3, "searched": 5, "best": 1.0}
    table = (tmp_path / "out" / "similarity.csv").read_text().splitlines()
    assert table[0] == "hrid,name,smiles,tanimoto"
    assert table[1] == f"X-1,aspirin,{ASPIRIN},1.0"
    assert len(table) == 6


def test_similarity_is_compared_with_the_threshold_unrounded(tmp_path: Path) -> None:
    from rdkit import Chem, DataStructs
    from rdkit.Chem import rdFingerprintGenerator

    salicylic = "O=C(O)c1ccccc1O"
    # Morgan radius 2, 2,048 bits: aspirin and salicylic acid share 13 of the 29 bits
    # either sets, a Tanimoto of 13/29 = 0.44828 (c / (a + b - c)), under 0.4483.
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    a, b = (generator.GetFingerprint(Chem.MolFromSmiles(s)) for s in (ASPIRIN, salicylic))
    assert ((a & b).GetNumOnBits(), (a | b).GetNumOnBits()) == (13, 29)
    assert DataStructs.TanimotoSimilarity(a, b) == 13 / 29
    query = entity(1, smiles=ASPIRIN, inchikey=ASPIRIN_KEY, name="aspirin")
    library = [query, entity(2, smiles=salicylic, inchikey="YGSDEFSMJLZEOE-UHFFFAOYSA-N")]
    found = {}
    for threshold in (0.4482, 0.4483):
        result, output = script(
            tmp_path,
            "search.py",
            {
                "params": {"threshold": threshold},
                "inputs": {"compounds": library, "query": [query]},
            },
            "similar",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert output is not None
        found[threshold] = output["metrics"]["hits"]
    assert found == {0.4482: 2, 0.4483: 1}


def test_a_query_is_a_compound_or_a_smiles_not_both(tmp_path: Path) -> None:
    result, _ = script(
        tmp_path,
        "search.py",
        {"params": {"smiles": "c1ccccc1"}, "inputs": {"compounds": LIBRARY, "query": [LIBRARY[0]]}},
        "similar",
    )
    assert result.returncode == 1
    assert last_error(result) == "::error Give a query compound, or a query's SMILES, but not both."


def test_compounds_with_a_substructure_are_a_collection(tmp_path: Path) -> None:
    result, output = script(
        tmp_path,
        "search.py",
        {"params": {"smarts": "c1ccccc1C(=O)[OX2H1]"}, "inputs": {"compounds": LIBRARY}},
        "substructure",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    (collection,) = output["creates"]
    assert collection["values"] == {
        "name": "With c1ccccc1C(=O)[OX2H1]",
        "description": (
            "2 of 5 compounds with the substructure c1ccccc1C(=O)[OX2H1], as RDKit matches SMARTS."
        ),
        "members": [LIBRARY[0]["id"], LIBRARY[1]["id"]],
    }
    assert output["metrics"] == {"hits": 2, "searched": 5}


def test_smarts_that_cant_be_read_fails_the_search_saying_why(tmp_path: Path) -> None:
    result, _ = script(
        tmp_path,
        "search.py",
        {"params": {"smarts": "c1ccccc1C(=O"}, "inputs": {"compounds": LIBRARY}},
        "substructure",
    )
    assert result.returncode == 1
    assert last_error(result).startswith("::error 'c1ccccc1C(=O' isn't SMARTS RDKit can read: ")
