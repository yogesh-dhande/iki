"""Check the report adapters on small, explicitly synthetic output tables."""

import json
import runpy
from pathlib import Path

import pytest

SCRIPT = runpy.run_path(str(Path(__file__).parents[1] / "scripts/import_search.py"))


def test_maxquant_keeps_native_quantity_columns_and_zero():
    rows = [
        "Sequence\tProteins\tIntensity sample A\tLFQ intensity sample A\tReverse",
        "PEPTIDE\tsp|P00330|ADH1_YEAST;P12345\t0\tNA\t+",
    ]
    (row,) = SCRIPT["parsed"]("maxquant", rows)
    assert row["quantities"] == {"Intensity sample A": 0.0, "LFQ intensity sample A": None}
    assert row["accessions"] == ["P00330", "P12345"]
    assert row["level"] == "peptide"
    assert row["decoy"]


def test_diann_keeps_two_precursors_and_one_shared_protein_group_quantity():
    rows = [
        "Run\tProtein.Group\tStripped.Sequence\tPrecursor.Id\tPrecursor.Quantity\tPG.MaxLFQ",
        "A\tP00330\tPEPTIDE\tPEPTIDE2\t20\t123",
        "A\tP00330\tPEPTIDE\tPEPTIDE3\t30\t123",
    ]
    first, group, second = SCRIPT["parsed"]("diann", rows)
    assert first["quantities"] == {"Precursor.Quantity": 20.0}
    assert second["quantities"] == {"Precursor.Quantity": 30.0}
    assert group["quantities"] == {"PG.MaxLFQ": 123.0}
    assert group["level"] == "protein_group"


def test_fragpipe_preserves_peptide_and_protein_intensity_labels():
    peptide = ["Peptide Sequence\tProtein ID\tA Intensity", "PEPTIDE\tP00330\t2.5"]
    protein = ["Protein ID\tA MaxLFQ Intensity\tA Spectral Count", "P00330\t9\t1"]
    (peptide_hit,) = SCRIPT["parsed"]("fragpipe", peptide)
    (protein_hit,) = SCRIPT["parsed"]("fragpipe", protein)
    assert peptide_hit["quantities"] == {"A Intensity": 2.5}
    assert protein_hit["quantities"] == {"A MaxLFQ Intensity": 9.0, "A Spectral Count": 1.0}


def test_result_keeps_raw_references_and_matches_the_registry_entry(tmp_path):
    source = tmp_path / "synthetic.tsv"
    source.write_text("Protein IDs\tIntensity\nP00330;P12345\t42\n")
    run = {
        "params": {"format": "maxquant", "reports": ["report"], "version": "synthetic"},
        "inputs": {
            "dataset": [{"id": "dataset", "revision": 3, "values": {"raw_files": ["raw"]}}],
            "proteins": [
                {
                    "id": "project-record",
                    "entry_id": "registry-entry",
                    "values": {"uniprot_accession": "P00330"},
                },
            ],
        },
        "files": {"report": {"name": source.name, "path": str(source), "sha256": "synthetic"}},
    }
    artifact = tmp_path / "artifact.jsonl"
    result = SCRIPT["result"](run, artifact)["creates"][0]
    assert result["derived_from"] == ["dataset", "project-record"]
    assert result["values"]["dataset_revision"] == 3
    assert result["values"]["raw_files"] == ["raw"]
    assert result["values"]["proteins"] == ["registry-entry"]
    assert json.loads(artifact.read_text())["registered_proteins"] == ["registry-entry"]


def test_invalid_columns_point_to_the_report_problem():
    with pytest.raises(ValueError, match="Choose MaxQuant"):
        list(SCRIPT["parsed"]("maxquant", ["Unexpected\tHeader", "row\t1"]))
