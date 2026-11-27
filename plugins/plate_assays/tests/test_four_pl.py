"""Analytic checks on synthetic curves, independent of the numerical solver."""

import csv
import importlib
import json
import math
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    fit_curve = importlib.import_module("four_pl").fit_curve
finally:
    sys.path.remove(str(SCRIPTS))


@pytest.mark.parametrize(("kind", "direction"), [("EC50", 1), ("IC50", -1)])
def test_known_midpoint_plateaus_signed_slope_and_ci(kind, direction):
    # Ritz et al., https://doi.org/10.1371/journal.pone.0146021, eqs. 2 and 3.
    # Synthetic unit midpoint, unit Hill magnitude, 0 and 100 as the two plateaus.
    # Paired residuals +/-1 give the exact least-squares parameters and RSS=14.
    doses = [1 / 8, 1 / 4, 1 / 2, 1, 2, 4, 8]
    points = [(x, 100 / (1 + x**-direction) + dy) for x in doses for dy in (-1, 1)]
    result = fit_curve(points, kind)
    assert result["fit_status"] == "fitted"
    assert result["normalization"] == "none"
    assert result["qualifier"] == "within_range"
    assert result["bottom"] == pytest.approx(0, abs=1e-8)
    assert result["top"] == pytest.approx(100)
    assert result["endpoint_value"] == pytest.approx(1)
    assert result["hill_slope"] == pytest.approx(direction)
    assert result["endpoint_kind"] == kind
    assert result["point_count"] == 14
    # The partial derivative for the log midpoint is g = -/+100x/(1+x)^2.
    # Symmetry of the doses makes g orthogonal to the odd Jacobian columns;
    # only its projection on the constant column needs to be removed. Thus
    # Var(log midpoint) = (14/10) / sum((g-mean(g))²) and the 0.975 Student t
    # quantile on 10 df is 2.22813885. Back-transforming the limits as in Ritz
    # et al. gives asymmetric endpoints independently of the Jacobian in four_pl.
    assert result["ci_low"] == pytest.approx(0.8822297361)
    assert result["ci_high"] == pytest.approx(1.133491606)


@pytest.mark.parametrize(("kind", "increasing"), [("EC50", True), ("IC50", False)])
def test_measured_zero_dose_wells_constrain_the_fitted_plateau(kind, increasing):
    doses = [0.125, 0.25, 0.5, 1, 2, 4, 8]
    points = [(dose, 100 * (dose if increasing else 1) / (1 + dose)) for dose in doses]
    vehicle = 0 if increasing else 100
    result = fit_curve([*points, (0, vehicle), (0, vehicle)], kind)
    assert result["fit_status"] == "fitted", result.get("fit_reason")
    assert result["point_count"] == 9
    assert result["endpoint_value"] == pytest.approx(1)
    assert result["hill_slope"] == pytest.approx(1 if increasing else -1)
    assert result["ci_low"] <= result["endpoint_value"] <= result["ci_high"]

    # The measured zero-dose values have weight, including their replicate residuals.
    shifted = fit_curve([*points, (0, vehicle + 2), (0, vehicle + 4)], kind)
    assert shifted["fit_status"] == "fitted"
    assert shifted["bottom" if increasing else "top"] > vehicle + 1
    assert shifted["ci_low"] < shifted["endpoint_value"] < shifted["ci_high"]


@pytest.mark.parametrize(("mid", "status"), [(0.9, "below_range"), (9.0, "above_range")])
def test_report_extrapolation(mid, status):
    # Synthetic 4PL from Ritz et al., eq. 2. The midpoint falls beyond the test doses.
    doses = [1, 1.5, 2, 3, 4, 6, 8]
    points = [(x, 20 + 80 / (1 + (mid / x) ** 1.3)) for x in doses]
    result = fit_curve(points, "EC50")
    assert result["qualifier"] == status
    assert result["endpoint_value"] == pytest.approx(mid)


@pytest.mark.parametrize(("kind", "increasing"), [("EC50", True), ("IC50", False)])
def test_zero_dose_wells_dont_widen_the_tested_range(kind, increasing):
    # The same exact curve, midpoint 0.6, at positive doses 1 to 64 and three vehicle
    # wells. The lowest concentration tested is 1, so 0.6 is an extrapolation: the
    # vehicle wells fix the zero-dose plateau, not where the midpoint can be read.
    doses = [1, 2, 4, 8, 16, 32, 64]
    curve = [(x, 100 / (1 + (0.6 / x) ** (1 if increasing else -1))) for x in doses]
    vehicle = 0 if increasing else 100
    result = fit_curve([*curve, (0, vehicle), (0, vehicle + 0.5), (0, vehicle - 0.5)], kind)
    assert result["fit_status"] == "fitted", result.get("fit_reason")
    assert result["endpoint_value"] == pytest.approx(0.6, rel=1e-3)
    assert result["qualifier"] == "below_range"


def test_concentration_units_change_only_midpoint_and_limits():
    # Synthetic raw signal can use an inverted detector and large response units.
    points = [(x, 1e10 * (1 + 2 / (1 + (3.5 / x) ** -1.3)) + x) for x in (1, 2, 3, 4, 5, 8, 12)]
    in_nm = fit_curve(points, "EC50")
    in_m = fit_curve([(x * 1e-9, y) for x, y in reversed(points)], "EC50")
    assert in_m["hill_slope"] == pytest.approx(-1.3)
    assert in_m["bottom"] == pytest.approx(1e10)
    assert in_m["top"] == pytest.approx(3e10)
    for field in ["endpoint_value", "ci_low", "ci_high"]:
        assert in_m[field] == pytest.approx(in_nm[field] * 1e-9, rel=1e-6, abs=1e-16)


@pytest.mark.parametrize(
    ("points", "reason"),
    [
        ([(1, 4)] * 4, "more than four"),
        ([(1, 0), (2, 0), (2, 1), (4, 0), (4, 1)], "distinct concentrations"),
        ([(x, 23) for x in [1, 2, 3, 4, 5]], "constant"),
        ([(x, 7) for x in [0, 1, 2, 3, 4]], "constant"),
        ([(x, 7) for x in [-1, 1, 2, 3, 4]], "nonnegative concentration"),
        ([(x, math.nan) for x in [1, 2, 3, 4, 5]], "finite values"),
    ],
)
def test_non_estimable_curves_retain_reviewable_status(points, reason):
    result = fit_curve(points, "IC50")
    assert result["fit_status"] == "unreportable"
    assert reason in result["fit_reason"]
    assert result["qualifier"] == "unavailable"
    for field in ["endpoint_value", "ci_low", "ci_high", "bottom", "top", "hill_slope"]:
        assert result[field] is None
    json.dumps(result, allow_nan=False)


def test_unknown_endpoint_label_is_rejected():
    with pytest.raises(ValueError, match="IC50 or EC50"):
        fit_curve([], "dose")


def test_synthetic_mars_series_matches_worked_example():
    fixture = Path(__file__).parent / "data" / "mars-96-series.csv"
    with fixture.open(newline="") as stream:
        row = next(row for row in csv.reader(stream) if row and row[0] == "A")
    points = [(2**i, float(response)) for i, response in enumerate(row[1:9])]
    result = fit_curve(points, "IC50")
    assert result["endpoint_value"] == pytest.approx(12.06983, rel=1e-5)
    assert (result["ci_low"], result["ci_high"]) == pytest.approx((10.76447, 13.53348), rel=1e-5)
