# 0188. A plate read keeps the wells behind a fitted endpoint

- Status: Accepted
- Date: 2026-11-22

## Context

A reader export contains detector values at well coordinates. The plate layout
identifies what occupied those coordinates. An experiment can only reuse a
measurement if the two agree. A reviewer also needs to know which readings
went into the reported IC50 or EC50, including the replicates an analyst chose
to exclude.

## Decision

The plate assays plugin joins the reader file to the layout by well. The
analyst previews the join before committing it. A commit keeps the input file
and creates a reusable plate read with links to individual measurement records.
This follows the preview and transactional write used for spreadsheet imports.
The instrument parser understands the matrix structure of endpoint absorbance
exports from BMG LABTECH MARS. [ADR 0178](0178-spreadsheets-are-imported-by-mapping-previewing-then-one-run.md)

The fit operates on one sample and readout at a time. It retains the individual
observations, their units, the inclusion decision for each point, and the
identity of the person responsible for each exclusion. The result records the
model version, response scale, fitted parameters, status, midpoint and its
approximate 95% confidence interval. A midpoint outside the tested range carries
an explicit qualifier. A fit that cannot estimate an endpoint reports why.

The deployment sets the default research or regulated QC review flow. A project
can select its own flow by applying a versioned assay starter.
[ADR 0198](0198-a-project-applies-a-versioned-assay-starter.md)

The result records that choice so the signer can see which flow applies. Research needs
one signature. In regulated QC the analyst signs the submission, an independent
reviewer signs the review, and QA signs the release. Reviewers and QA must be
independent of the people who edited the submitted work. The signature is
bound to the revision the signer saw. [ADR 0179](0179-a-corrected-result-goes-back-for-review.md)

## Consequences

An experiment can cite the original measurement or fitted result by ID and
revision. A service can send the signed endpoint back on a request. Revisiting
a decision about a well preserves the earlier fit as evidence.

The fit assumes independent observations with constant residual variance on
the raw detector scale. It estimates a midpoint relative to the two fitted
plateaus. Assays that need a different normalization, weighting model, or
absolute activity threshold need their own analysis method.
