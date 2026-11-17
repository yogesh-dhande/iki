"""The small_molecule plugin's actions: registering compounds from SMILES, and their
descriptors and batches' formula weights, run as a worker runs them (an input.json in,
an output.json out)."""

import json
import subprocess
import sys
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
        "type": "small_molecule.Compound",
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
    compounds = [c for c in creates if c["type"] == "small_molecule.Compound"]
    batches = [c for c in creates if c["type"] == "small_molecule.CompoundBatch"]
    assert [c["values"]["inchikey"] for c in compounds[:3]] == [ASPIRIN_KEY] * 3
    assert compounds[3]["values"]["smiles"] == "CN(C)CCC(c1ccccc1)c1ccccn1"
    # Each batch refers to its line's compound, made with it (or the one reused).
    assert [b["values"]["compound"] for b in batches] == [{"$create": c["key"]} for c in compounds]
    free, sodium, calcium, pheniramine = (b["values"] for b in batches)
    assert free == {
        "compound": {"$create": "line-1"},
        "structure_as_drawn": ASPIRIN,
        "formula_weight": 180.159,
    }
    assert sodium == {
        "compound": {"$create": "line-2"},
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
        },
    }
    assert output["messages"] == []


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
    import importlib

    import yaml

    monkeypatch.syspath_prepend(str(SCRIPTS))
    chemistry = importlib.import_module("chemistry")

    manifest = yaml.safe_load((SCRIPTS.parent / "types" / "compound_batch.yaml").read_text())
    assert manifest["fields"]["salt"]["values"] == list(chemistry.SALTS)
    assert manifest["fields"]["solvate"]["values"] == list(chemistry.SOLVENTS)
    # Each is told apart from the others by its structure.
    assert len(chemistry.KNOWN) == len(chemistry.SALTS) + len(chemistry.SOLVENTS)
    sys.modules.pop("chemistry", None)
