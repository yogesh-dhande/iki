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
    assert report["metrics"] == {"VCF sites": 2, "Variant sites": 1, "Sites without a call": 0}
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
    assert report["metrics"] == {"VCF sites": 2, "Variant sites": 1, "Sites without a call": 0}
    assert [(row["Position"], row["Genotype"]) for row in report["preview"]] == [("2", "0|1")]
    [headings, selected] = list(csv.reader(io.StringIO(output.getvalue()), delimiter="\t"))
    assert headings[-2:] == ["FORMAT", sample]
    assert selected[1] == "2"
    assert selected[-2:] == ["GT", "0|1"]


def test_an_airr_file_with_several_repertoires_needs_a_choice():
    rows = [
        "sequence_id\tproductive\tv_call\tj_call\trepertoire_id",
        "a\tT\tIGHV1-2\tIGHJ6\tR1",
        "b\tF\tIGHV1-2\tIGHJ6\tR2",
    ]
    output = csv.writer(io.StringIO())
    with pytest.raises(ValueError, match="multiple repertoires"):
        SCRIPT["rearrangement_rows"](rows, "", output)
    report = SCRIPT["rearrangement_rows"](rows, "R1", output)
    assert report["preview"][0]["locus"] == "IGH"
    assert report["metrics"] == {"Rearrangements": 1, "Productive": 1}
    assert report["projection"] == "Every rearrangement in repertoire R1."


def test_the_vcf_projection_counts_the_sites_it_leaves_out():
    # A homozygous reference call, an uncalled site, a FORMAT without GT, a
    # symbolic-only call and one alternate call (VCF 4.5, section 1.6.2).
    rows = [
        "##fileformat=VCFv4.5",
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1",
        "1\t1\t.\tA\tG\t0\tPASS\t.\tGT\t0/0",
        "1\t2\t.\tA\tG\t0\tPASS\t.\tGT\t./.",
        "1\t3\t.\tA\tG\t0\tPASS\t.\tDP\t7",
        "1\t4\t.\tA\t<NON_REF>\t0\tPASS\t.\tGT\t0/1",
        "1\t5\t.\tA\tT\t0\tPASS\t.\tGT:DP\t0/1:9",
    ]
    report = SCRIPT["variant_rows"](rows, "", csv.writer(io.StringIO()))
    assert report["metrics"] == {"VCF sites": 5, "Variant sites": 1, "Sites without a call": 2}
    assert report["rows"] == 1
    assert report["projection"].startswith("Sites where S1's genotype calls an alternate allele.")
    sites_only = [row.rsplit("\t", 2)[0] for row in rows[2:]]
    header = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO"
    report = SCRIPT["variant_rows"]([rows[0], header, *sites_only], "", csv.writer(io.StringIO()))
    assert report["metrics"] == {"VCF sites": 5, "Variant sites": 4}
    assert "symbolic reference" in report["projection"]


def test_selected_rows_use_the_worker_artifact_directory(tmp_path, monkeypatch):
    source = Path(__file__).parent / "data/GSE176296-counts.tsv"
    run = {
        "params": {
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
    monkeypatch.setattr("sys.argv", ["import_result.py", "counts"])

    SCRIPT["iki"].main(SCRIPT["import_report"])

    assert output.is_file()
    assert (artifacts / "selected.tsv").read_text().splitlines()[1] == "YDL248W\t679"
