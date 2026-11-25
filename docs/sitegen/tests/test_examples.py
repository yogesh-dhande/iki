"""The worked examples are the plugins' own results on their data, and the same each build.

The numbers asserted here are the ones the pages state in words: if a plugin's code or
data changes them, this fails, and the page is read again before the numbers are."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sitegen import examples


def test_a_run_of_one_base_is_counted_round_the_circle() -> None:
    assert examples.run_length("AACCCGTA", 3) == 3
    assert examples.run_length("AACCCGTA", 1) == 3  # A, A at the start and A at the end
    assert examples.run_length("ACGT", 2) == 1


def test_the_plate_is_laid_out_as_the_guide_says() -> None:
    wells = {well["well"]: well for well in examples.layout()}
    assert [wells[f"A{n}"]["concentration"] for n in range(1, 9)] == [
        1,
        2,
        4,
        8,
        16,
        32,
        64,
        128,
    ]
    assert wells["A9"]["control"] == "blank"
    assert wells["A10"]["control"] == "reference"


@pytest.fixture(scope="module")
def dose() -> dict[str, object]:
    return examples.dose_response()


def test_the_fit_with_a4_excluded_is_the_guides(dose: dict[str, object]) -> None:
    fits = dose["fits"]
    assert isinstance(fits, dict)
    excluded, everything = fits["excluded"], fits["all"]
    assert excluded["excluded"] == ["A4"]
    assert excluded["point_count"] == 7
    assert round(excluded["endpoint_value"], 2) == 12.34
    assert (round(excluded["ci_low"], 2), round(excluded["ci_high"], 2)) == (10.65, 14.30)
    assert excluded["qualifier"] == "within_range"
    assert everything["point_count"] == 8
    assert round(everything["endpoint_value"], 2) == 12.07
    assert (round(everything["ci_low"], 2), round(everything["ci_high"], 2)) == (10.76, 13.53)
    readings = dose["readings"]
    assert isinstance(readings, list)
    assert len(readings) == 10


def test_puc19_is_checked_as_the_prep_use_case_says() -> None:
    result = examples.puc19_reads()
    values = result["values"]
    assert (values["reads_total"], values["reads_aligned"]) == (41, 37)
    assert values["agreement"] == "differs"
    assert (values["mean_depth"], values["least_depth"]) == (35.9, 33)
    assert values["called_percent"] == 99.89
    differences = values["differences"]
    assert [d["hgvs"][:12] for d in differences] == [
        "o.413_418del",
        "o.419_420ins",
        "o.423T>A",
        "o.887del",
        "o.1446del",
    ]
    # The multiple cloning site's three are in no feature; the others are in runs of 6 or 7.
    assert [d["features"] for d in differences[:3]] == [[], [], []]
    assert [d["homopolymer"] for d in differences[3:]] == [6, 7]
    assert result["uncalled"] == [
        {"position": 1134, "base": "C", "run": 6},
        {"position": 2043, "base": "A", "run": 6},
        {"position": 2602, "base": "C", "run": 4},
    ]


def test_the_same_code_and_data_write_the_same_bytes(tmp_path: Path) -> None:
    first = {p.name: p.read_bytes() for p in examples.write(tmp_path / "one")}
    second = {p.name: p.read_bytes() for p in examples.write(tmp_path / "two")}
    assert first == second
    assert set(first) == {"puc19-reads.json", "dose-response.json"}
    assert json.loads(first["dose-response.json"])["exclusion"] == {
        "well": "A4",
        "reason": "Simulated dispensing error",
    }
