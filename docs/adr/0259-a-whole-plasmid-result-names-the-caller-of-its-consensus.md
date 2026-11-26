# 0259. A whole-plasmid result names the caller of its consensus

- Status: Accepted
- Date: 2026-11-26
- Refines: [0208](0208-whole-plasmid-sequencing-is-checked-against-the-plasmids-own-sequence.md)

## Context

Verifying a plasmid from its long reads has two steps. minimap2 aligns the
reads, and the plugin's own code calls their consensus: a majority at each base,
indels moved 3', ties left uncalled and polishing against the reads. minimap2 is
a published, versioned tool, and its run listed it as software. The caller had
no name or version. A reviewer couldn't tell from a result which consensus
method produced its depths and read counts. A lab couldn't require a particular
caller for release evidence.

A result also kept its classification twice, as the plugin's `agreement` and as
core Alignment's `verdict`, with a rule keeping them equal.

## Decision

The plugin's caller is **Iki majority consensus, version 1**. A result records it
in `consensus_caller` as `iki_majority_1`, and the reads action lists it as
software beside minimap2, so the run's provenance names both. A consensus given
as FASTA, by a service or by the lab's own validated caller, is `supplied`, and
its provider field names who assembled it and how.

A rule ties each caller to its source and evidence. An `iki_majority_1` result
comes from reads and keeps the platform, depths, read counts and polishing
rounds. A `supplied` result comes from a consensus file and has none of them.
A changed caller is a new version, recorded on the new results.

`verdict` is the one classification, and `agreement` is gone.

## Consequences

A reviewer reads a result's caller beside its evidence. A lab whose acceptance
criteria need a validated caller supplies that caller's consensus. Its results
then say so, and a reviewer can tell them from the plugin's inspection method.
The lots verification already reads `verdict`. Its fallback to `agreement`, kept
for results made before core Alignment had a verdict, no longer has records to
read.
