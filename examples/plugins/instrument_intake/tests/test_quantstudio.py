"""Reading QuantStudio results exports: three real exports, checked against what follows
from their own standard curves, and the ways a file can fail to be one."""

import math
import re
import runpy
from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"
reader = runpy.run_path(str(DATA.parents[1] / "scripts" / "quantstudio.py"))
read_export = reader["read_export"]
ExportError = reader["ExportError"]

STANDARD_CURVE = (DATA / "qs7-flex-3utr-standard-curve.txt").read_bytes()
RELATIVE = (DATA / "qs7-flex-rnasep-relative-standard-curve.txt").read_bytes()
COMPARATIVE = (DATA / "qs7-flex-tgfb-comparative-ct.txt").read_bytes()


@pytest.mark.parametrize("source", [STANDARD_CURVE, RELATIVE], ids=["3utr", "rnasep"])
def test_each_efficiency_is_the_one_its_slope_gives(source: bytes) -> None:
    # MIQE: E = 10^(-1/slope) - 1 (Bustin et al. 2009, doi:10.1373/clinchem.2008.112797).
    # The export rounds the slope to three decimals, within 0.05 points of efficiency.
    curves = read_export(source)["standard_curves"]
    assert curves
    for curve in curves:
        assert curve["efficiency"] == pytest.approx(
            100 * (10 ** (-1 / curve["slope"]) - 1), abs=0.05
        )


def test_each_unknown_s_quantity_is_its_cq_on_the_curve() -> None:
    values = read_export(STANDARD_CURVE)
    (curve,) = values["standard_curves"]
    unknowns = [w for w in values["wells"] if w["task"] == "UNKNOWN"]
    assert len(unknowns) == 18
    for well in unknowns:
        # Cq = slope × log10(quantity) + intercept, both rounded to three decimals.
        expected = 10 ** ((well["cq"] - curve["y_intercept"]) / curve["slope"])
        assert well["quantity"] == pytest.approx(expected, rel=5e-3), well["well"]


def test_quantities_are_read_through_their_thousands_separators() -> None:
    wells = {w["well"]: w for w in read_export(STANDARD_CURVE)["wells"]}
    # 100,000,000.000 and 118,248.200 in the file.
    assert wells["A1"]["quantity"] == 1e8 and wells["A1"]["task"] == "STANDARD"
    assert wells["A4"]["quantity"] == 118248.2 and wells["A4"]["sample"] == "5000018696625"
    assert wells["A1"]["target"] == "3'UTR" and wells["A1"]["reporter"] == "VIC"


def test_no_template_controls_have_no_cq() -> None:
    values = read_export(RELATIVE)
    controls = [w for w in values["wells"] if w["task"] == "NTC"]
    assert [w["well"] for w in controls] == ["A1", "A7", "B1"]
    assert all(w["cq"] is None and w["undetermined"] and w["sample"] is None for w in controls)
    assert all(w["cq"] is not None for w in values["wells"] if w["task"] == "STANDARD")
    assert values["expired_calibrations"] == ["Background", "RNaseP", "ROI", "Uniformity"]


def test_a_windows_export_and_its_analysis_settings() -> None:
    assert b"\r\n" in COMPARATIVE
    values = read_export(COMPARATIVE)
    assert (values["endogenous_control"], values["reference_sample"]) == ("GAPDH", "Liver")
    assert values["experiment_type"] == "Comparative Cт (ΔΔCт)"
    targets = {(w["target"], w["reporter"]) for w in values["wells"]}
    assert targets == {("TGF-B", "FAM"), ("GAPDH", "VIC")}
    assert values["standard_curves"] == []


def test_a_byte_order_mark_is_not_part_of_the_first_line() -> None:
    assert read_export(b"\xef\xbb\xbf" + RELATIVE) == read_export(RELATIVE)


def test_a_relative_threshold_export_reads_its_crt_column() -> None:
    text = RELATIVE.decode()
    crt = text.replace("Quantification Cycle Method = Ct", "Quantification Cycle Method = Crt")
    crt = crt.replace("\tCT\t", "\tCRT\t", 1)
    values = read_export(crt.encode())
    assert values["cycle_method"] == "Crt"
    assert [w["cq"] for w in values["wells"]] == [w["cq"] for w in read_export(RELATIVE)["wells"]]


def _with_row(row: str) -> bytes:
    """The relative standard curve export with one more row in its table."""
    head, _, tail = RELATIVE.decode().rpartition("\n13\tB1\t")
    return f"{head}\n{row}\n13\tB1\t{tail}".encode()


@pytest.mark.parametrize(
    ("source", "says"),
    [
        (b"Well,Value\nA1,1\n", "Line 1 is neither a header line nor in a section"),
        (RELATIVE.split(b"[Results]")[0], "has no [Results] section"),
        (RELATIVE.replace(b"\tCT\t", b"\tCq value\t"), "has no CT column"),
        ("* Experiment Name = Bühler".encode("latin-1"), "isn't UTF-8 text (byte 22)"),
        (_with_row("99\tQ1\tfalse\t\tRNaseP\tSTANDARD\tFAM\tNFQ-MGB"), "Q1 isn't a well"),
        (_with_row("99\tA2\tfalse\t\tRNaseP\tSTANDARD\tFAM\tNFQ-MGB"), "second RNaseP result"),
        (_with_row("99\tC1\tmaybe\t\tRNaseP\tSTANDARD\tFAM"), "not true or false"),
        (
            _with_row("99\tC1\tfalse\t\tRNaseP\tSTANDARD\tFAM\tNFQ-MGB\t\t\t\t30,5"),
            "isn't a number",
        ),
        (_with_row("99\tC1\tfalse\t\tRNaseP" + "\t" * 40), "more cells than"),
        (RELATIVE + b"[Results]\n", "two [Results] sections"),
    ],
)
def test_what_is_not_an_export_says_why(source: bytes, says: str) -> None:
    with pytest.raises(ExportError, match=f"(?i){re.escape(says)}"):
        read_export(source)


def test_every_number_is_finite() -> None:
    for source in (STANDARD_CURVE, RELATIVE, COMPARATIVE):
        for well in read_export(source)["wells"]:
            for key in ("cq", "threshold", "quantity"):
                assert well[key] is None or math.isfinite(well[key])
