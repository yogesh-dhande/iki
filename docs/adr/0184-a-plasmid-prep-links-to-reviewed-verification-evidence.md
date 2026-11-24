# 0184. A plasmid prep links to reviewed verification evidence

- Status: Accepted
- Date: 2026-11-22
- Refines: [0162](0162-a-lot-is-a-projects-record-of-an-entrys-material.md),
  [0163](0163-a-plasmid-is-a-record-about-a-sequence-not-a-kind-of-sequence.md),
  [0179](0179-a-corrected-result-goes-back-for-review.md)

## Context

Several physical preps can reference the same registered plasmid. A
verification method on the prep tells a reviewer which test was performed.
To evaluate its outcome the reviewer also needs the evidence from this
particular prep and the revision used as the reference.

The vector catalogue already holds selection markers, replication origins,
copy number, host classes, promoters, containment level, source and MTA.
Assembly records carry the backbone, inserts and cloning method. The assembly
and plasmid share the registered product sequence. Several assemblies can
produce that sequence. A project can name the particular route that supplied
its material in `Plasmid.source_assembly`. Saving the plasmid checks that the
selected assembly names its sequence as the product. A plasmid created by
the assembly action also retains its run lineage.

## Decision

The lots plugin owns prep verification records. An evidence record names
one physical prep, its plasmid registry entry and the project plasmid record
and revision examined when recording the verification. The action obtains
the record and revision from the prep's pinned reference in its run input.
The project-record ID distinguishes different projects' revisions of one
registry entry.

A verification can cite an existing alignment of the prep's reads, a
reviewed external report, or both. For an internal alignment the reference
must be the same DNA sequence entry as the prep's plasmid. The record
preserves the alignment revision and the alignment retains its original run
and read-file provenance. For an external report the reviewer attests to
the association with the physical prep and reference. The recorded outcome
can include a discrepancy or an inconclusive result.

The deployment selects Research or Regulated QC in the lots plugin's
settings. The evidence record retains the selected flow. In Research the
assigned reviewer signs the recorded outcome. In Regulated QC an analyst
signs the submission, an independent reviewer signs the review and QA signs
the release. A signed revision records who made that decision. A correction
returns the record for review and retains the previous signatures in its
history.

## Consequences

The prep's verification tab lists evidence records for that prep. The
reviewer can open each record to inspect the alignment or external file,
reference revision, outcome and signatures. Repeated checks on the same
material can each retain their evidence. Releasing a verification records
what the evidence established. Lot disposition remains a separate decision.

## Amendment. Reasons for overriding an alignment verdict

Where an alignment's producer reports a verdict, the core Alignment carries
it as `matches`, `differs` or `incomplete`. The whole-plasmid result publishes
its existing agreement as this verdict. Its migration copies the agreement
into the new field on existing alignments and their saved revisions.
Alignments with no reported verdict and externally reviewed reports can
still be recorded.

The verification form shows the selected alignment's verdict by the outcome
choice. A recorded match requires a written reason when the alignment reports
`differs` or `incomplete`. The action checks the evidence revision it read, and
the verification record checks the same immutable verdict when a reviewer
edits or corrects the outcome or reason. The reason is part of the record's
revision history and the content attested by its signatures.
