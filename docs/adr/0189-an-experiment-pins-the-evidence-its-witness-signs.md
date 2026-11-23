# 0189. An experiment pins the evidence its witness signs

- Status: Accepted
- Date: 2026-11-22

## Context

A scientist follows a controlled protocol, records work in the lab's ELN,
draws material from lots and collects results in Iki. An ELN entry, protocol
or analytical result can change after a witness signs the experiment. A live
link alone cannot identify the evidence that was closed.

## Decision

An experiment names the external protocol and revision followed and the
external ELN entry and revision containing the notebook narrative. It records
sample references, dated observations and direct attachments. When the author
captures the results, Iki writes the concrete result IDs and their record
revisions into the experiment. For a registered result it also captures the
registry entry ID and revision. An update against the revision the capture
action read prevents a delayed run from overwriting a concurrent change.
[ADR 0188](0188-a-plate-read-keeps-the-wells-behind-a-fitted-endpoint.md)

A draw naming an experiment writes its movement to the source amount ledger
and appends the evidence to the experiment in the same transaction. The
evidence identifies the source, amount, unit and movement. The experiment's
revision advances, so a signature covers the materials used up to that
revision. A locked experiment refuses further draws. A failed update to the
experiment rolls back the withdrawal.
[ADR 0139](0139-a-records-amount-is-a-ledger-of-what-moved.md)

A research witness signs the recorded experiment. In regulated QC, the author
signs the submission, an independent reviewer signs the review and QA signs
the release. Each signature binds the selected result revisions, external
revision identifiers, material usage and hashes of the experiment's direct
files. The experiment retains the setup's selected flow in its signed content.
[ADR 0126](0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md)

## Consequences

A witness can open the numbered result revisions even after newer values are
recorded. The ELN remains responsible for retaining and serving its named
revision. A lab that needs a rendition available inside Iki can attach it to
the experiment before capture and sign-off.
