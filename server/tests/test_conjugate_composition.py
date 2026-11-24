"""Conjugate elemental accounting, through the action's RunInput and RunOutput contract."""

import json
import runpy
from pathlib import Path
from typing import Any

import pytest

from iki_spec.identity import identity_hash, kind_of
from iki_spec.model import Field
from iki_spec.yamlsource import read_documents

SCRIPT = Path(__file__).resolve().parents[2] / "plugins/conjugate/scripts/composition.py"
# Real constituents, synthetic attachment examples. Diglycine and discrete m-PEG4 acid
# are PubChem CIDs 11163 and 21941826. IUPAC Gold Book P04479 defines amide water loss.
PEG = {"molecular_formula": "C10H20O6", "name": "m-PEG4-acid", "smiles": "COCCOCCOCCOCCC(=O)O"}
CARRIER = {"sequence": "GG"}


def record(
    *,
    carrier: dict[str, Any] | None = None,
    compound: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows = attachments if attachments is not None else [{"copies": 1, "loss_formula": "H2O"}]
    carrier = CARRIER if carrier is None else carrier
    if "components" in carrier:
        kind = "antibody_drug"
    elif "sequence" in carrier:
        kind = "pegylated_protein"
    else:
        kind = "oligo_conjugate"
    return {
        "id": "00000000-0000-7000-8003-000000000001",
        "values": {
            "carrier": [{"kind": kind, "molecule": "carrier-id"}],
            "attachments": [
                {**row, "part": "part-id", "site": f"site-{i}"} for i, row in enumerate(rows)
            ],
        },
        "included": {
            "carrier.molecule": [{"id": "carrier-id", "values": carrier}],
            "attachments.part": [
                {
                    "id": "part-id",
                    "hrid": "CPD-000001",
                    "values": PEG if compound is None else compound,
                }
                for _ in rows
            ],
        },
    }


def run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *records: dict[str, Any]
) -> dict[str, Any]:
    source, output = tmp_path / "input.json", tmp_path / "output.json"
    source.write_text(json.dumps({"inputs": {"conjugate": records}}), encoding="utf-8")
    monkeypatch.setenv("IKI_INPUT", str(source))
    monkeypatch.setenv("IKI_OUTPUT", str(output))
    runpy.run_path(str(SCRIPT), run_name="__main__")
    return json.loads(output.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    ("left", "right"),
    [("D2O", "[2H]2O"), ("T2O", "[3H]2O"), ("H2O", "OH2"), ("HHO", "H2O")],
)
def test_conjugate_identity_reads_atom_counts(left: str, right: str) -> None:
    manifest = SCRIPT.parents[1] / "types" / "conjugate.yaml"
    documents, problems = read_documents(manifest, manifest.name)
    assert not problems
    fields = documents[0].data["fields"]
    row_kind = kind_of(
        Field.from_manifest("attachments", fields["attachments"], plugin="iki/conjugate"), {}
    )

    def digest(loss: str | None) -> str:
        rows = [
            {
                "part": "01890a5d-ac96-774b-bcce-b302099a8057",
                "copies": 1,
                "site": "A",
                "loss_formula": loss,
            }
        ]
        return identity_hash("iki/conjugate.Conjugate", {"attachments": (row_kind, rows)})

    assert digest(left) == digest(right)
    assert len({digest(v) for v in ("D2O", "T2O", "H2O", "none", None)}) == 5


def test_defined_conjugate_of_discrete_peg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = run(tmp_path, monkeypatch, record())
    values = output["updates"]["conjugate"][0]["values"]
    # PubChem 11163 + 21941826 less H2O for the amide yields C14H26N2O8.
    # The isotope masses use IUPAC's most-abundant-isotope definition (Gold Book 12495).
    assert values["molecular_formula"] == "C14H26N2O8"
    assert values["molecular_weight"] == 350.37
    assert values["monoisotopic_mass"] == 350.1689
    assert "reduced carrier chains without glycans" in values["composition_note"]
    assert values["carrier_heavy_copies"] is None
    assert output["messages"] == []


def test_different_sites_and_explicit_isotopic_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Synthetic isotope bookkeeping: two identical labelled acetates, each losing
    # water, and one labelled acetate at another site losing one carbon-13 atom.
    # Isotope masses per NIST Atomic Weights and Isotopic Compositions of Carbon.
    example = record(
        compound={"molecular_formula": "C[13C]H4O2"},
        attachments=[{"copies": 2, "loss_formula": "H2O"}, {"copies": 1, "loss_formula": "[13C]"}],
    )
    values = run(tmp_path, monkeypatch, example)["updates"]["conjugate"][0]["values"]
    assert values["molecular_formula"] == "C7[13C]2H16N2O7"
    assert values["monoisotopic_mass"] == pytest.approx(
        7 * 12 + 2 * 13.003354835 + 16 * 1.007825032 + 2 * 14.003074004 + 7 * 15.994914620,
        abs=0.00005,
    )
    # The same two fixed carbon-13 atoms contribute their isotope mass to the average.
    assert values["molecular_weight"] == round(
        7 * 12.011 + 2 * 13.003354835 + 16 * 1.008 + 2 * 14.007 + 7 * 15.999, 2
    )


def test_antibody_chain_counts_survive_distributed_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    antibody = {"components": [{"role": "heavy", "copies": 2}, {"role": "light", "copies": 2}]}
    example = record(carrier=antibody, attachments=[{}])
    # A recomputation clears an obsolete fixed-species result while keeping IgG
    # multiplicities. A distribution measured on a lot cannot imply a single formula.
    example["values"].update(molecular_formula="H2", molecular_weight=2, monoisotopic_mass=2)
    output = run(tmp_path, monkeypatch, example)
    values = output["updates"]["conjugate"][0]["values"]
    assert values["carrier_heavy_copies"] == values["carrier_light_copies"] == 2
    assert (
        values["molecular_formula"]
        is values["molecular_weight"]
        is values["monoisotopic_mass"]
        is None
    )
    assert "distributed loading" in values["composition_note"]
    assert output["messages"] == [
        {"entity": example["id"], "level": "warning", "text": values["composition_note"]}
    ]


def test_oligo_free_acid_basis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Adenylated DNA in RCSB PDB 8V1W, chain C. The 5′-phosphorylated carrier
    # and adenosine monophosphate (PubChem CID 6083) contribute a pyrophosphate
    # linkage. Both inputs are the neutral free acids, with a formal loss of H2O.
    example = record(
        carrier={"molecular_formula": "C68H87N28O43P7"},
        compound={"molecular_formula": "C10H14N5O7P"},
    )
    values = run(tmp_path, monkeypatch, example)["updates"]["conjugate"][0]["values"]
    assert values["molecular_formula"] == "C78H99N33O49P8"
    assert values["molecular_weight"] == 2530.62
    assert values["monoisotopic_mass"] == 2529.4170
    assert "free-acid" in values["composition_note"]


@pytest.mark.parametrize(
    ("example", "why"),
    [
        (record(attachments=[{"copies": 1}]), "net atom loss"),
        (record(attachments=[{"copies": 1, "peg_grade": "disperse"}]), "polydisperse"),
        (record(compound={}), "CPD-000001 has no molecular formula"),
        (record(carrier={"sequence": "QBX"}), "no unique amino-acid formula"),
        (record(compound={"molecular_formula": "Zn+2"}), "neutral elemental formula"),
        (
            record(compound={"molecular_formula": "C2H3NaO2", "smiles": "CC(=O)[O-].[Na+]"}),
            "counterions",
        ),
        (record(compound={"molecular_formula": "CH3", "formal_charge": 1}), "neutral"),
        (record(compound={"molecular_formula": "[999C]"}), "Unknown element or isotope"),
        (record(attachments=[{"copies": 1, "loss_formula": "H888"}]), "losses exceed"),
    ],
)
def test_one_unknown_composition_leaves_others_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, example: dict[str, Any], why: str
) -> None:
    second = record()
    second["id"] = "00000000-0000-7000-8003-000000000002"
    output = run(tmp_path, monkeypatch, example, second)
    missing, computed = (u["values"] for u in output["updates"]["conjugate"])
    assert why in missing["composition_note"]
    assert (
        missing["molecular_formula"]
        is missing["molecular_weight"]
        is missing["monoisotopic_mass"]
        is None
    )
    assert output["messages"][0]["entity"] == example["id"]
    assert computed["molecular_formula"] == "C14H26N2O8"
