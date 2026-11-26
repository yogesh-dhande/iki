# 0236. A lab chooses each sample's testing procedure

- Status: Accepted
- Date: 2026-11-26
- Refines: [0211](0211-lots-inventory-samples-and-requests.md),
  [0179](0179-a-corrected-result-goes-back-for-review.md)

## Context

Every sample in `iki/samples` followed one procedure. An analyst signs the measurements
in testing. A reviewer, independent of the submitter and of everyone who edited the
measurements in that cycle, signs them again. A correction returns the sample to testing
with a reason, and both sign again. That's the second-person review of 21 CFR 211.194(a)(8)
for a laboratory record, and a QC lab needs every step of it.

Most samples in a discovery lab aren't laboratory records of that kind. An aliquot drawn as
a plate's input is used, not measured, and the plate's own result is what someone reviews.
The one procedure gave those samples two signatures that certified nothing, or left them
open for good. A lab couldn't choose otherwise without editing Iki's plugin.

Requests already answer the same question with types. A Plain, Research or QC request is
chosen as it's made, and each class keeps its own release (ADR 0195).

## Decision

**Sample keeps what every sample shares, and each testing procedure is a type of its
own.** `Sample` is abstract. It keeps the immutable source, the identified work (a record
or a run), the draw receipt, the measurements, notes and due date. It's still the type
that requests resolve when a deliverable's material is a sample, and the type a search
for every sample names.

| Type             | Prefix | Testing procedure                                                                                                                                                               |
| ---------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ReviewedSample` | `SAM`  | Iki's incumbent procedure, unchanged: the analyst's signed submission, an independent reviewer's signed review, and a signed, reasoned return to testing with fresh signatures. |
| `PlainSample`    | `SMP`  | The analyst completes it (Done) once it's received, with or without measurements, and returns it to testing with a reason. Nobody signs.                                        |

Both receive a sample only once its one draw from its assigned source is recorded. Both
refuse measurements before testing starts, and both lock the sample once it's complete.
The person creating a sample chooses its procedure, and the procedure doesn't change
afterwards. The record's type says which it followed, in its HRID, page and audit trail.

**The assigned reviewer is told, as every assignee is.** A reviewed sample assigns its
`reviewer` in In review, so Iki tells them "It's ready for you" (ADR 0122). Its own
notification tells the sample's watchers.

## Consequences

- A QC lab creates reviewed samples, and its evidence is what it was before.
- A discovery lab's assay inputs complete when they're used, without a signature that
  certifies nothing.
- A lab whose procedure is neither copies one of the two types into its own plugin and
  changes its states.
- Before M1 the samples plugin's migrations are regenerated. Its records would otherwise
  move to the reviewed type.
- The dose-response tutorial creates a plain sample for its research flow and a reviewed
  sample for its regulated QC flow.

## Alternatives considered

- **An optional review on one Sample type.** If the analyst can skip the review on any
  sample, then a QC lab's samples can be completed without it, and nothing on the record
  says that the lab's procedure wasn't followed.
- **A deployment setting.** One deployment serves discovery and QC projects. A sample's
  procedure belongs with the sample.
- **A per-project choice of allowed types.** Iki has no such restriction for any type. If
  a lab needs one, it's a general primitive, for requests and results too.
