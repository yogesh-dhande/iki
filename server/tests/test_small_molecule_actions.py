"""The small_molecule plugin's actions: registering compounds from SMILES and SDF files,
their descriptors and batches' formula weights, exporting them, and fetching them from
PubChem and ChEMBL (from fakes of them), run as a worker runs them (an input.json in, an
output.json out) or, for the fetches, in-process."""

import hashlib
import importlib
import io
import json
import re
import subprocess
import sys
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
    assert compound["values"]["name"] == "aspirin sodium"
    assert salt["values"] == {
        "compound": {"$create": "record-1"},
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
    assert base["values"]["name"] == "pheniramine"


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
            "text": "Record 1 (aspirin sodium): Its CAS number and PubChem CID are its "
            "structure's as drawn, not its parent's, so they were left out.",
        }
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
    ],
)
def test_batch_fields_that_cant_be_read_name_their_record(
    tmp_path: Path, fields: dict[str, str], error: str
) -> None:
    text = sdf_text(("CC(=O)Oc1ccccc1C(=O)[O-].[Na+]", "aspirin sodium", fields))
    result, _ = imported(tmp_path, text, parent=True, batches=True)
    assert result.returncode == 1
    assert f"Record 1 (aspirin sodium): {error}" in last_error(result)


def test_exports_compounds_with_their_values_as_sd_fields(tmp_path: Path) -> None:
    from rdkit import Chem

    result, output = script(
        tmp_path,
        "sdf.py",
        {
            "inputs": {
                "compounds": [
                    entity(
                        1,
                        smiles=ASPIRIN,
                        inchikey=ASPIRIN_KEY,
                        name="aspirin",
                        molecular_weight=180.159,
                        synonyms=["ASA", "acetylsalicylic acid"],
                    ),
                    entity(2, smiles="CC(=O)Nc1ccc(O)cc1", inchikey="RZVAJINKPMORJF-UHFFFAOYSA-N"),
                ]
            }
        },
        "export",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output is not None
    assert output["metrics"] == {"compounds": 2}
    written = list(Chem.SDMolSupplier(str(tmp_path / "out" / "compounds.sdf")))
    assert [m.GetProp("_Name") for m in written] == ["aspirin", "X-2"]
    assert written[0].GetPropsAsDict() == {
        "hrid": "X-1",
        "name": "aspirin",
        "smiles": ASPIRIN,
        "inchikey": ASPIRIN_KEY,
        "synonyms": "ASA; acetylsalicylic acid",
        "molecular_weight": 180.159,
    }
    # The structure written, read back, is the one given.
    assert Chem.MolToSmiles(written[0]) == ASPIRIN
    assert written[0].GetConformer().Is3D() is False


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
    for name in ("chemistry", "fetching", "pubchem", "chembl"):
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
    assert create["type"] == "small_molecule.Compound"
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
