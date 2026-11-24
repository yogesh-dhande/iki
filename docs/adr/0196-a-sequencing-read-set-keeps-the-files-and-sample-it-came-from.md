# 0196. A sequencing read set keeps the files and sample it came from

- Status: Accepted
- Date: 2026-11-23
- Refines: [0036](0036-files-content-addressed-uploads.md),
  [0179](0179-a-corrected-result-goes-back-for-review.md) and
  [0195](0195-core-facility-requests-return-signed-deliverables.md)

## Context

An instrument run can cover several libraries, and one library can appear across
several lanes or runs. A project needs to know which physical sample and output
files went into an analysis. The core facility can deliver the files and its
signed report on the existing request. The sequencing sample and run need records
that downstream actions can consume.

## Decision

The sequencing plugin records an instrument run, a prepared library linked to
its sample, and a read set for one library and lane or output partition. The
read set names one FASTQ for single-end sequencing or two corresponding FASTQs
for paired-end sequencing. The immutable file references can name files registered
in place from a versioned source bucket. Existing file registration pins the
object version and checksum. Existing worker staging checks the bytes when an
action needs them.

The first action streams the selected FASTQ files on Iki compute to check their
structure and mate order. It creates a quality summary linked to the read set
and sample. That summary records the file IDs, SHA-256 values, read set revision
and library revision. The deployment chooses Research sign-off or Regulated QC
submission, independent review and QA release. Each transition signs the
summary, and a correction returns signed findings for review.

Work on whole-plasmid verification by long reads and amplicon/CRISPR-edit
outcomes follows in a separate slice.

## Consequences

A reviewer can trace quality statistics to the files, library and sample and
compare paired mates. When the wrong files were recorded, a corrected read set
and new action run retain the earlier evidence. Labs retain the source files in
their existing storage. Workers need temporary space only for the files the
run reads.
