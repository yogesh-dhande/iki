# 0255. A quality summary can be recorded without sign-off

- Status: Accepted
- Date: 2026-11-26
- Refines: [0196](0196-a-sequencing-read-set-keeps-the-files-and-sample-it-came-from.md)

## Context

A FASTQ quality summary is evidence about the files a read set names. Many labs
look at read quality before they've decided whether that look needs anyone's
signature, and some never sign it: the signed step is the analysis or the
release that uses it. The sequencing plugin offered only two procedures,
Research sign-off and Regulated QC. A lab without a review step still saw every
summary waiting in Recorded for a signature nobody would give.

An INSDC run accession (SRR, ERR or DRR) names an archived run, which belongs to
one library. It isn't the instrument run: one flow cell can carry many
libraries, and each is archived as its own run. The accession sat on the
instrument run, where a multiplexed run had nowhere to put each library's
accession.

## Decision

The deployment's `review_flow` setting has a third value, **Record only**,
beside Research and Regulated QC. A summary recorded under it keeps the same
evidence: the statistics, the file IDs and checksums, the read set and library
revisions, and the procedure itself in `review_flow`. Its review state stays
Recorded. Every signed state refuses it with one guard,
`reviewed_procedure`, which says the summary was recorded under Record only.

Research and Regulated QC keep their states, signatures, locks, returns on edit
and independence guards unchanged. Each summary still pins the procedure it was
recorded under, so changing the setting changes only new summaries. A lab that
wants an old summary reviewed summarizes the read set again under a review
procedure.

The INSDC run accession moves to the read set. Read sets from one instrument
run can each name their own archived run. Read sets for several lanes of one
library can share the accession that combines them.

## Consequences

A lab that only records read quality isn't left with summaries that look as if
they await a signature: the guard names the procedure in its refusal. Labs that
sign keep exactly the procedures they had. Another result type that records a
procedure can adopt the same third value the same way.

The choice is still made for the whole deployment. A deployment that runs both
unreviewed and signed sequencing projects records them under one procedure
until projects can choose their own, as an assay starter does (ADR 0223).
