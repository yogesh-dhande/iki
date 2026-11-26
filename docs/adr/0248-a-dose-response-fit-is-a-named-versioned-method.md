# 0248. A dose-response fit is a named, versioned method

- Status: Accepted
- Date: 2026-11-26
- Refines: [0188](0188-a-plate-read-keeps-the-wells-behind-a-fitted-endpoint.md)

## Context

Every assay result was fitted the same way: an unweighted four-parameter
logistic with free plateaus and a t interval on the log midpoint. Its results
recorded `fit_version: 4PL-OLS/1`, but the method wasn't declared anywhere a
lab could read, and what it didn't do wasn't written down. A lab that has
qualified another model, such as a five-parameter logistic or a fit with
constrained plateaus, had no route to Iki other than through 4PL-OLS.
`assays.py` also mixed three separate concerns in one function: gathering the
readings, the numerical fit, and choosing the result's review class.

## Decision

**The fit is a named method.** The `fit_curve` action takes a `method`
parameter. Its only value today is `4PL-OLS`, and the result records the
version this release implements, `4PL-OLS/1`, in `fit_version`. A change to the
method's numerical behaviour is a new version. Earlier results keep the
version they were fitted with. `data/methods.md` states the model,
estimation, uncertainty, the conditions for a reportable fit, and what the
method doesn't support.

**The parts are separate.** `series` gathers one sample's committed readings,
doses in one molar unit, and exclusions with their authors. It doesn't depend
on the method. A method maps the included readings to its result. The review
flow and request coverage choose `result_type`, whatever the method is.

**`AssayResult` is a dose-response result.** Its description says so. Its
fields (endpoint, limits, plateaus, slope) describe an IC50 or EC50 fitted to
a dose series. Another analysis of a plate read, such as a Z′ factor or a
single-point percent inhibition, is a type of its own. It isn't an
`AssayResult` with fields left empty. We kept the fields on the base, rather
than adding a new dose-response type between the base and its four concrete
classes. A new type would break the result types that `qc_release` already
consumes, and gain nothing today.

**A lab's own method lives in the lab's plugin.** That plugin depends on
`iki/plate_assays`. It creates Research or QC assay results under its own
method name, such as `acme/5PL-WLS/1`, and sets the review flow of the class it
creates. Its results get the same review, signatures, project flow and
release-criterion matching as 4PL-OLS results. The **Curve** tab draws the
fitted line only for methods this plugin ships, because a lab's method may
record a bottom, top and slope that describe a different curve. For other
methods the tab shows the endpoint, readings and parameters without the line.

## Consequences

- The default method is unchanged, so existing callers and results behave
  as before.
- A release criterion that names `4PL-OLS/1` still accepts exactly those
  results.
- A test fixture plugin shows that a lab's manifest can declare results of
  both classes.
- Out of scope: doses in mass concentration (ng/mL, µg/mL). Enum values
  can't contain `/`, and the dose unit is an enum on the plate layout, the
  readings and the result. Supporting them needs either enum value labels in
  the spec or a unit vocabulary shared with amounts. That is a core change,
  not a plate-assays one.
