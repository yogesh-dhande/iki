"""Check report ambiguity and association handling with synthetic rows."""

import csv
import io
import json
import runpy
from pathlib import Path

import pytest

SCRIPT = runpy.run_path(str(Path(__file__).parents[1] / "scripts/import_result.py"))


def test_a_variant_present_in_the_other_sample_is_not_copied():
    rows = (
        "##fileformat=VCFv4.3\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS2\n"
        "1\t10\t.\tA\tG\t9\tPASS\t.\tGT\t0/0\t1/1\n"
        "1\t11\t.\tT\tA\t8\tPASS\t.\tGT\t1/1\t0/0\n"
    )
    output = io.StringIO()
    report = SCRIPT["variant_rows"](rows.splitlines(), "S1", csv.writer(output, delimiter="\t"))
    assert report["metrics"] == {"VCF sites": 2, "Variant sites": 1}
    assert len(output.getvalue().splitlines()) == 2
    assert report["preview"][0]["Genotype"] == "1/1"


@pytest.mark.parametrize("sample", ["POS", "ID", "INFO", "QUAL"])
def test_vcf_sample_names_are_resolved_within_the_sample_columns(sample):
    rows = [
        "##fileformat=VCFv4.5",
        f"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tother\t{sample}",
        "1\t1\t.\tA\tG\t0\tPASS\t.\tGT\t1/1\t0/0",
        "1\t2\t.\tT\tA\t0\tPASS\t.\tGT\t0/0\t0|1",
    ]
    output = io.StringIO()
    report = SCRIPT["variant_rows"](rows, sample, csv.writer(output, delimiter="\t"))
    assert report["metrics"] == {"VCF sites": 2, "Variant sites": 1}
    assert [(row["Position"], row["Genotype"]) for row in report["preview"]] == [("2", "0|1")]
    [headings, selected] = list(csv.reader(io.StringIO(output.getvalue()), delimiter="\t"))
    assert headings[-2:] == ["FORMAT", sample]
    assert selected[1] == "2"
    assert selected[-2:] == ["GT", "0|1"]


def test_curated_airr_match_must_exist_in_the_selected_repertoire():
    rows = [
        "sequence_id\tproductive\tv_call\tj_call\trepertoire_id",
        "a\tT\tIGHV1-2\tIGHJ6\tR1",
        "b\tF\tIGHV1-2\tIGHJ6\tR2",
    ]
    output = csv.writer(io.StringIO())
    with pytest.raises(ValueError, match="multiple repertoires"):
        SCRIPT["rearrangement_rows"](rows, "", set(), output)
    with pytest.raises(ValueError, match="absent from the imported repertoire"):
        SCRIPT["rearrangement_rows"](rows, "R1", {"b"}, output)
    report = SCRIPT["rearrangement_rows"](rows, "R1", {"a"}, output)
    assert report["preview"][0]["locus"] == "IGH"
    assert report["metrics"]["Antibody links"] == 1


def test_associations_keep_the_registry_identity():
    selected = [{"hrid": "ABY-000001", "entry_id": "entry", "id": "record"}]
    assert SCRIPT["antibody_links"]("query-a\tABY-000001", selected) == [
        {"sequence_id": "query-a", "antibody": "entry"},
    ]
    with pytest.raises(ValueError, match="outside the selected inputs"):
        SCRIPT["antibody_links"]("query-a\tABY-000002", selected)


def test_selected_rows_use_the_worker_artifact_directory(tmp_path, monkeypatch):
    source = Path(__file__).parent / "data/GSE176296-counts.tsv"
    run = {
        "params": {
            "format": "counts",
            "report": "counts",
            "reference": "sacCer3",
            "tool": "featureCounts",
            "tool_version": "GEO GSE176296",
            "column": "WTJ.1",
        },
        "inputs": {"library": [{"id": "library", "revision": 1, "values": {"sample": "sample"}}]},
        "files": {"counts": {"name": source.name, "sha256": "fixture", "path": str(source)}},
    }
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(run))
    output = tmp_path / "output.json"
    artifacts = tmp_path / "out"
    artifacts.mkdir()
    monkeypatch.setenv("IKI_INPUT", str(input_path))
    monkeypatch.setenv("IKI_OUTPUT", str(output))
    monkeypatch.setenv("IKI_OUT_DIR", str(artifacts))

    SCRIPT["main"]()

    assert output.is_file()
    assert (artifacts / "selected.tsv").read_text().splitlines()[1] == "YDL248W\t679"
