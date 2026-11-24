"""Check report ambiguity and association handling with synthetic rows."""

import csv
import io
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
