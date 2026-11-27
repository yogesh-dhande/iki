"""Method 4PL-OLS, version 1: an unweighted four-parameter logistic fit of one sample's
raw, continuous readout. data/methods.md states what it supports and what it doesn't."""

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import least_squares
from scipy.special import expit
from scipy.stats import t

Vector = NDArray[np.float64]

#: The method's name and version, as each result records it in `fit_version`.
METHOD = "4PL-OLS/1"


def _response(params: Vector, log_dose: Vector) -> Vector:
    # Ritz et al., PLoS ONE 2015, equations 2–3, doi:10.1371/journal.pone.0146021.
    # expit evaluates the logistic without overflowing at concentrations far from e.
    left, right, midpoint, hill = params
    return left + (right - left) * expit(hill * (log_dose - midpoint))


def _residual(params: Vector, log_dose: Vector, observed: Vector) -> Vector:
    # Zero-dose sample wells observe the left asymptote directly. Each replicate
    # contributes a residual with the same weight as a positive-dose reading.
    zero_count = observed.size - log_dose.size
    predicted = np.concatenate((np.full(zero_count, params[0]), _response(params, log_dose)))
    return predicted - observed


def _jacobian(params: Vector, log_dose: Vector, observed: Vector) -> Vector:
    # Partial derivatives of the same four-parameter function, in parameter order.
    left, right, midpoint, hill = params
    distance = log_dose - midpoint
    position = expit(hill * distance)
    complement = expit(-hill * distance)
    gradient = (right - left) * position * complement
    positive = np.column_stack((complement, position, -hill * gradient, distance * gradient))
    zero = np.zeros((observed.size - log_dose.size, 4))
    zero[:, 0] = 1
    return np.vstack((zero, positive))


def fit_curve(points: Sequence[tuple[float, float]], endpoint: str) -> dict[str, Any]:
    """Fit included replicate readings in common units, giving each well equal weight.

    The caller retains the point IDs, units, excluded wells and their audit trail.
    IC50/EC50 names the assay's biological endpoint. Raw detector signals can run
    in either direction. A successful midpoint is relative to the fitted asymptotes.
    """
    if endpoint not in {"IC50", "EC50"}:
        raise ValueError("Choose IC50 or EC50.")
    result: dict[str, Any] = {
        "fit_status": "unreportable",
        "fit_version": METHOD,
        "normalization": "none",
        "endpoint_kind": endpoint,
        "point_count": len(points),
        "ci_method": "95% t interval on log midpoint (n - 4 df)",
        "qualifier": "unavailable",
        "bottom": None,
        "top": None,
        "hill_slope": None,
        "endpoint_value": None,
        "ci_low": None,
        "ci_high": None,
    }
    if len(points) <= 4:
        result["fit_reason"] = "A four-parameter fit needs more than four readings for uncertainty."
        return result
    doses, signals = np.asarray(points, dtype=np.float64).T
    if not (np.isfinite(doses).all() and np.isfinite(signals).all() and (doses >= 0).all()):
        result["fit_reason"] = "Each well needs a nonnegative concentration and finite values."
        return result
    if len(np.unique(doses)) < 4:
        result["fit_reason"] = "A four-parameter fit needs at least four distinct concentrations."
        return result
    if signals.min() == signals.max():
        result["fit_reason"] = "The included responses are constant."
        return result

    # Linear rescaling conditions the solver across detector units. The returned
    # plateaus use the original detector scale and the analysis uses raw readings.
    signal_scale = float(np.max(np.abs(signals)))
    scaled = signals / signal_scale
    low, span = scaled.min(), np.ptp(scaled)
    observed = (scaled - low) / span
    positive = doses > 0
    log_dose = np.log(doses[positive])
    offset = float(np.median(log_dose))
    log_dose -= offset
    left = observed[doses == doses.min()].mean()
    right = observed[doses == doses.max()].mean()
    zero = ~positive
    observed = np.concatenate((observed[zero], observed[positive]))
    # With measured zeros, a positive Hill magnitude makes "left" the limit at
    # zero for either response direction. Both asymptotes remain free.
    has_zero = bool(zero.any())
    # Start at three positions across the concentration series. The asymptotes and
    # midpoint are unconstrained so extrapolated midpoints can be marked as such.
    best = None
    for center in np.unique(np.quantile(log_dose, [0.25, 0.5, 0.75])):
        candidate = least_squares(
            _residual,
            [left, right, center, 1.0],
            jac=_jacobian,
            args=(log_dose, observed),
            method="trf" if has_zero else "lm",
            bounds=([-np.inf, -np.inf, -np.inf, 0.0], np.inf) if has_zero else (-np.inf, np.inf),
            max_nfev=600,
        )
        if candidate.success and (best is None or candidate.cost < best.cost):
            best = candidate
    if best is None:
        result["fit_reason"] = "The least-squares solver did not converge."
        return result

    # Ritz et al. (2015), uncertainty for effective doses, doi:10.1371/journal.pone.0146021.
    # Approximate parameter covariance = RSS/(n-p) * (JᵀJ)⁻¹. SVD avoids squaring
    # the condition number and detects unidentified parameters before reporting a CI.
    _, singular, vectors = np.linalg.svd(best.jac, full_matrices=False)
    tolerance = singular[0] * max(best.jac.shape) * np.finfo(float).eps
    if singular[-1] <= tolerance:
        result["fit_reason"] = "The readings do not identify all four curve parameters."
        return result
    df = len(points) - 4
    log_se = np.linalg.norm(vectors[:, 2] / singular) * math.sqrt(2 * best.cost / df)
    # Back-transform a two-sided 95% t interval computed on log concentration.
    margin = float(t.ppf(0.975, df) * log_se)
    left, right, midpoint, hill = map(float, best.x)
    midpoint += offset
    with np.errstate(over="ignore", under="ignore"):
        ci_low, concentration, ci_high = np.exp([midpoint - margin, midpoint, midpoint + margin])
        plateaus = (np.array([left, right]) * span + low) * signal_scale
    if ci_low <= 0 or not (math.isfinite(ci_high) and np.isfinite(plateaus).all()):
        result["fit_reason"] = "The parameter estimates or confidence limits exceed numeric range."
        return result
    # The tested range is the positive concentrations': a zero-dose well fixes the left
    # asymptote, not a midpoint on the log scale (Ritz et al. 2015, effective doses).
    qualifier = "within_range"
    if concentration > doses.max():
        qualifier = "above_range"
    elif concentration < doses[positive].min():
        qualifier = "below_range"
    return {
        **result,
        "fit_status": "fitted",
        "qualifier": qualifier,
        "bottom": float(plateaus.min()),
        "top": float(plateaus.max()),
        "hill_slope": hill if left < right else -hill,
        "endpoint_value": float(concentration),
        "ci_low": float(ci_low),
        "ci_high": float(ci_high),
    }
