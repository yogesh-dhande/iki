# 0247. A project chooses how its experiments are signed

- Status: Accepted
- Date: 2026-11-26
- Refines: [0189](0189-an-experiment-pins-the-evidence-its-witness-signs.md),
  [0198](0198-a-project-applies-a-versioned-assay-starter.md),
  [0240](0240-a-project-chooses-its-release-stability-and-verification-review.md)

## Context

An experiment record was signed one of two ways, chosen once for the whole
deployment: a research witness, or a regulated QC submission, review and
release. Both ask for an ELN entry revision and a protocol revision. Labs differ.
A discovery group records a gel image or a plate observation and the author's
own signature closes it, with no ELN and no SOP. A GxP group in the same
deployment needs the full QC flow. One deployment-wide choice forced one of
them to run the other's procedure.

## Decision

The experiments plugin offers three review flows: **Author sign-off**,
**Research** and **Regulated QC**. Research and Regulated QC are unchanged.

- **Author sign-off.** The author captures the evidence and signs it with an
  authorship signature. The signature locks the record. The signer must have
  created the record or edited it in the current cycle. A colleague who holds
  the author role can't sign someone else's record as its author. An ELN entry
  and protocol are optional, but one that's cited needs its revision. Signing
  still needs a fresh capture and at least one result, observation or
  attachment.

An `experiment-review` starter, version 1.0.0, sets the flow per project and
grants the roles the flow needs. It uses the same preview-and-apply plan as the
dose-response starter. Research and Regulated QC need their signers to be
different people, checked by Hall's condition. Author sign-off needs one
author. With no starter applied, the deployment's `review_flow` setting still
decides. A capture pins the flow its run was launched under into the record,
and each state's guard requires that flow. A project that changes its flow
leaves signed records as they were signed. A record that is still being
recorded follows the new flow at its next capture.

Every flow's records are **Experiment** records. Experiments are created by
hand from the type picker, so one type per flow would make each author choose a
review procedure there. As [ADR 0240](0240-a-project-chooses-its-release-stability-and-verification-review.md) does for stability
measurements, the starter names each flow's route through the type's one state
field, and the starters page draws only that route. The experiment pins its
own flow (`pinned_by`) when its evidence is captured. The API also lists, for
each flow, a summary of what it asks of people and the roles whose signers must
be different people. The setup page checks the same condition the server
applies, instead of deriving it from the type's signatures.

## Consequences

- A lab chooses its procedure per project. The existing two flows stay
  exactly as they were, so a project that applies Research or Regulated QC
  runs the incumbent procedure.
- A starter's flows may share one type. Its `types` lists each type once.
- The setting's default stays `research`. A deployment that never applies the
  starter behaves as before.
- Out of scope: running capture as part of submitting or witnessing, and
  recording freshness separately from the capture run. Both need a core
  primitive for an action that a signature runs first. A fresh capture remains
  a separate step before any signature.
