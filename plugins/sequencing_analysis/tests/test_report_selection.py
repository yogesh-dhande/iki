"""Check report ambiguity and association handling with synthetic rows."""

import csv
import gzip
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


DATA = Path(__file__).parent / "data"


def report_of(path, read, selector=""):
    """A report read as the action reads it: through the plugin runtime's open_text, so a
    gzip or BGZF file is decompressed by the same code as on a worker."""
    output = io.StringIO()
    with SCRIPT["text"].open_text(path) as lines:
        report = read(lines, selector, csv.writer(output, delimiter="\t"))
    return report, list(csv.reader(io.StringIO(output.getvalue()), delimiter="\t"))


def test_giabs_benchmark_bgzipped_keeps_its_heterozygous_and_multiallelic_calls():
    """HG002's GIAB v4.2.1 benchmark is distributed bgzipped, and so is this excerpt: the
    file's header and seven of its first calls, of each genotype it has (1/1, 0/1, 1/0,
    1/2, 2/1)."""
    path = DATA / "HG002-GIAB-v4.2.1.vcf.gz"
    assert path.read_bytes()[:4] == b"\x1f\x8b\x08\x04"  # BGZF: gzip with an extra field
    report, rows = report_of(path, SCRIPT["variant_rows"], "HG002")
    assert report["metrics"] == {"VCF sites": 7, "Variant sites": 7, "Sites without a call": 0}
    assert [(row["Position"], row["Alternate alleles"], row["Genotype"]) for row in
            report["preview"]] == [
        ("602113", "TGCCCA", "1/1"),
        ("814327", "G", "0/1"),
        ("814843", "C", "0/1"),
        ("1732412", "C", "1/0"),
        ("1735008", "GGTTT,GTTTT", "1/2"),
        ("1769969", "CAAAACA,C", "2/1"),
        ("1866635", "TA,TAAA", "1/2"),
    ]  # fmt: skip
    assert len(rows) == 8


def test_a_deepvariant_gvcf_leaves_out_its_reference_blocks():
    """DeepVariant 1.10's golden gVCF for NA12878 on chr20: 228 records, of which 151
    are 0/0 reference blocks or sites, 6 are no-calls (./.) and 71 call an allele other
    than <*>, counted independently of the importer."""
    report, rows = report_of(
        DATA / "NA12878-chr20-DeepVariant-1.10.g.vcf", SCRIPT["variant_rows"], "NA12878"
    )
    assert report["metrics"] == {
        "VCF sites": 228,
        "Variant sites": 71,
        "Sites without a call": 6,
    }
    assert all(row[4] != "<*>" for row in rows[1:])
    assert {row[-1].split(":")[0] for row in rows[1:]} == {"0/1", "1/1", "1/2"}


def test_a_haploid_chromosome_ys_calls_in_a_cohort_vcf():
    """1000 Genomes phase 3's chrY calls, one allele per male: HG00101 calls the
    alternate at two of these four sites. The cohort's 1,233 samples need a choice."""
    path = DATA / "1000G-phase3-chrY.vcf"
    with pytest.raises(ValueError, match="Choose a VCF sample heading"):
        report_of(path, SCRIPT["variant_rows"])
    report, rows = report_of(path, SCRIPT["variant_rows"], "HG00101")
    assert report["metrics"] == {"VCF sites": 4, "Variant sites": 2, "Sites without a call": 0}
    assert [(row["Position"], row["Genotype"]) for row in report["preview"]] == [
        ("2659347", "1"),
        ("2663712", "1"),
    ]
    assert rows[0][-1] == "HG00101" and all(len(row) == 10 for row in rows)


@pytest.mark.parametrize(("sample", "positions"), [("NA1", ["20"]), ("NA2", ["14"])])
def test_a_spanning_deletion_is_no_variant_of_its_own(sample, positions):
    """GATK's spanning deletion test VCF: NA2's homozygous deletion at 14 spans 20, where
    its genotype calls only the * allele, which the deletion's own site already counts."""
    report, _ = report_of(DATA / "gatk-spanning-deletion.vcf", SCRIPT["variant_rows"], sample)
    assert [row["Position"] for row in report["preview"]] == positions
    assert report["metrics"]["Variant sites"] == 1


def test_a_gzipped_featurecounts_table_skips_its_program_line(tmp_path):
    path = tmp_path / "counts.txt.gz"
    path.write_bytes(gzip.compress((DATA / "featureCounts-2.0.1-tiny.txt").read_bytes()))
    report, rows = report_of(path, SCRIPT["count_rows"])
    assert report["selector"] == "reads.bam"
    assert report["metrics"] == {"Genes": 3, "Total raw count": 14}
    assert rows[1:] == [["feature_1", "7"], ["feature_2", "7"], ["second_half", "0"]]


def test_a_gzipped_airr_file_reads_as_the_plain_one(tmp_path):
    plain = DATA / "pbmc-b-airr.tsv"
    path = tmp_path / "airr.tsv.gz"
    path.write_bytes(gzip.compress(plain.read_bytes()))
    assert report_of(path, SCRIPT["rearrangement_rows"]) == report_of(
        plain, SCRIPT["rearrangement_rows"]
    )
