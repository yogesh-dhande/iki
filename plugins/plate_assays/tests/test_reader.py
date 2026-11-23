"""BMG MARS raw OD endpoint import, from a public export and a synthetic series."""

import csv
import io
import runpy
from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"
preview = runpy.run_path(str(DATA.parents[1] / "scripts" / "reader.py"))["preview_reader"]
SAMPLE = "00000000-0000-0000-0000-000000000123"
SERIES = (DATA / "mars-96-series.csv").read_bytes()


def series_layout():
    return [
        {
            "well": f"a{i:02}",
            "sample": SAMPLE,
            "concentration": dose,
            "concentration_unit": "nM",
            "replicate": 1,
        }
        for i, dose in enumerate([1, 2, 4, 8, 16, 32, 64, 128], 1)
    ] + [
        {"well": "A9", "control": "blank", "replicate": 1},
        {"well": "A10", "control": "reference", "replicate": 1},
    ]


def test_join_retains_source_well_controls_and_concentration():
    result = preview(b"\xef\xbb\xbf" + SERIES, 96, list(reversed(series_layout())))
    assert result["issues"] == []
    assert len(result["readings"]) == 10
    assert result["channel"] == "Absorbance 484 nm"
    # OD absorbance is dimensionless, recorded as AU for display (IUPAC A00028,
    # https://doi.org/10.1351/goldbook.A00028). These values are explicitly synthetic.
    assert result["readings"][0] == {
        "well": "A1",
        "sample": SAMPLE,
        "concentration": 1,
        "concentration_unit": "nM",
        "replicate": 1,
        "value": 0.933769,
        "unit": "AU",
        "channel": "Absorbance 484 nm",
        "source_row": 10,
    }
    assert [r.get("control") for r in result["readings"][-2:]] == ["blank", "reference"]


def test_public_pherastar_384_export():
    # The public 472 ABS 384 QC export. Raw Data (484), physical lines 10..25:
    # https://github.com/Benchling-Open-Source/allotropy/tree/5bcc4c92/tests/parsers/bmg_mars/testdata
    # Row/column positions follow ANSI/SLAS 4-2004 (R2012), §§4.1–4.2.
    content = (DATA / "mars-384-qc.csv").read_bytes()
    # Synthetic labels here let us join every measured well in the public file.
    layout = [
        {"well": f"{row}{col}", "control": "QC fixture", "replicate": col}
        for row in "ABCDEFGHIJKLMNOP"
        for col in range(1, 25)
        if (row in "ABOP" and (col <= 7 or col >= 18))
        or (row in "CDEFGH" and col <= 6)
        or (row == "I" and col <= 7)
    ]
    result = preview(content, 384, layout)
    assert result["issues"] == []
    by_well = {r["well"]: r for r in result["readings"]}
    assert len(by_well) == 99
    assert by_well["C1"]["value"] == 2.368
    assert by_well["P24"]["value"] == 0.032
    assert by_well["P24"]["source_row"] == 25
    assert "A8" not in by_well


def test_preview_explains_missing_repeated_and_unassigned_together():
    layout = series_layout()
    layout[0]["well"] = "B1"  # B1 empty, A1 now unassigned.
    layout.append(dict(layout[1], well="A2"))  # Same coordinate as a02.
    result = preview(SERIES + b"A,1\n", 96, layout)
    assert result["issues"] == [
        "Layout row 11: repeated assignment for A2.",
        "File row 18: repeated plate row A.",
        "Measured wells without assignments: A1.",
        "Assigned wells missing a reading: B1.",
    ]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"sample": SAMPLE, "control": "blank"}, "one sample ID or one named control"),
        ({"sample": ""}, "one sample ID or one named control"),
        ({"sample": "lookup by name"}, "sample record UUID"),
        ({"replicate": True}, "positive integer replicate"),
        ({"concentration": -1}, "nonnegative concentration"),
        ({"concentration": float("nan")}, "finite"),
        ({"concentration_unit": None}, "concentration unit"),
        ({"well": "I1"}, "outside this 96-well plate"),
        ({"well": "A13"}, "outside this 96-well plate"),
    ],
)
def test_invalid_layout_addresses_and_samples(change, message):
    layout = series_layout()
    layout[0].update(change)
    assert message in preview(SERIES, 96, layout)["issues"][0]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"well,signal\nA1,1", "MARS MicroPlate View"),
        (SERIES.replace(b"Absorbance", b"Luminescence"), "raw absorbance endpoint in OD"),
        (SERIES + b"Raw Data (650)\n", "exactly one Raw Data"),
        (SERIES.replace(b"Raw Data (484)", b"Raw Data (No filter)"), "one absorbance wavelength"),
        (SERIES.replace(b",1,2,3", b",1,1,3"), "label columns 1 through 12"),
        (SERIES.replace(b"0.933769", b"OVER"), "A1: expected a finite OD reading"),
        (SERIES.replace(b"0.933769", b"1e309"), "A1: expected a finite OD reading"),
        (SERIES.replace(b"0.933769", b"=1+2"), "A1: expected a finite OD reading"),
        (SERIES + b"B,0.1", "repeated plate row B"),
        (SERIES + b"P,0.1", "expected a plate row letter"),
    ],
)
def test_unsupported_or_malformed_export_is_refused(content, message):
    assert any(message in issue for issue in preview(content, 96, series_layout())["issues"])


def test_zero_is_a_reading_and_empty_cells_have_no_reading():
    rows = list(csv.reader(io.StringIO(SERIES.decode())))
    rows[9][1:3] = ["0", ""]
    out = io.StringIO(newline="")
    csv.writer(out).writerows(rows)
    result = preview(out.getvalue().encode(), 96, series_layout())
    assert result["readings"][0]["value"] == 0
    assert result["issues"] == ["Assigned wells missing a reading: A2."]
