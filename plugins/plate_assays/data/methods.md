# Fit methods

Each assay result records its method's name and version in `fit_version`.
This plugin ships one method, **4PL-OLS**, at version 1 (`4PL-OLS/1`). A change
to its numerical behaviour gets a new version. A result keeps the version it
was fitted with, and a release criterion names the version it accepts.

## 4PL-OLS, version 1

`scripts/four_pl.py` implements it. `tests/test_four_pl.py` checks it against
synthetic curves whose parameters and limits are known analytically. The
server's dose-response tests fit the Tox21 teaching dataset and compare it
with the deposited potencies.

**Model.** A variable-slope four-parameter logistic in log concentration
(Ritz et al., PLoS ONE 2015, equations 2 and 3,
<https://doi.org/10.1371/journal.pone.0146021>):

    response = left + (right − left) / (1 + exp(−hill · (ln dose − ln midpoint)))

Both plateaus are free. The midpoint is the concentration halfway between the
fitted plateaus, so the reported IC50 or EC50 is **relative**. The stored Hill
slope is positive for a rising curve and negative for a falling one.

**Data.** The fit reads one sample's committed readings from one plate read
and one detector channel. Each reading is in its source unit, for example OD
or percent, and keeps its weight. Replicates aren't averaged first. Wells at
zero dose count as readings of the left plateau. Doses are converted to the
series' molar unit (nM, µM, mM or M). Wells an operator excludes, with their
reasons, stay on the result but aren't fitted.

**Estimation.** Unweighted least squares, with the responses rescaled
linearly for the solver and the plateaus reported on the source scale. The
fit starts from three midpoints across the series and keeps the best
converged solution. It uses Levenberg–Marquardt, or a trust-region solver
with a nonnegative Hill magnitude when zero-dose wells are present.

**Uncertainty.** A two-sided 95% Student t interval on the log midpoint, with
n − 4 degrees of freedom. The covariance is RSS/(n − 4) · (JᵀJ)⁻¹, computed
by singular value decomposition, and the limits are back-transformed to
concentration, so they're asymmetric.

**Reportable fit.** A fit is reported only when:

- there are more than four included readings
- there are at least four distinct concentrations
- the responses aren't constant
- the solver converges
- all four parameters are identified
- the estimates and limits are finite

Otherwise the result is `unreportable` and gives the reason. A midpoint
outside the tested concentrations is qualified `above_range` or
`below_range`. The range runs from the lowest positive concentration to the
highest: zero-dose wells fix the curve's zero-dose plateau, so a midpoint
below the lowest positive concentration is `below_range` with them too.

**Not supported.** Fixed or constrained plateaus, a fixed Hill slope, three-
or five-parameter models, weighted fits, normalization to plate controls,
absolute IC50 (50% of a control), fits shared across samples, automatic
outlier rejection, mass-concentration doses and fits across channels.

## A lab's own method

A lab that has qualified another method, such as a five-parameter logistic,
writes it in its own plugin. That plugin depends on `iki/plate_assays`. Its
action reads a **Plate read** and a sample, and creates an **Assay result**,
or a **Requested assay result** for a request. It records the method under the
lab's own name, such as `acme/5PL-WLS/1`. Iki pins each result to the review
flow its project chose, as it does a 4PL-OLS result: the action doesn't choose.

The result follows the same review, signatures and project flow as a 4PL-OLS
result. A release criterion that names the lab's method accepts it. The
**Curve** tab shows its endpoint, readings and parameters. It draws the
fitted line only for the methods this plugin ships.
