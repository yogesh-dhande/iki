# Whole-plasmid sequencing

Check a plasmid prep against its plasmid's own registered sequence, using
whole-plasmid long reads or the consensus a sequencing service sent ([ADR
0208](../../docs/adr/0208-whole-plasmid-sequencing-is-checked-against-the-plasmids-own-sequence.md)).

## From reads

1. Record the run, the prep's library and its **FASTQ read set** in the
   sequencing plugin. Register the basecalled FASTQ where it already lies, in
   an approved, versioned bucket. Iki pins its version and checksum, and the
   run reads it from there.
2. On the plasmid, run **Verify a plasmid from its long reads** with the read
   set. Choose nanopore or PacBio HiFi reads, and the minimum depth a base
   needs for the consensus to call it (10 by default).

Basecalling is your own licensed tool, and Iki ships no basecaller. The
action aligns the reads round the circle with minimap2 2.31 and calls their
consensus with this plugin's own caller, **Iki majority consensus, version 1**
(`iki_majority_1`): each base takes the majority of at least the minimum depth
of reads, each indel is moved 3' first, and the reads are aligned again to
their consensus until it holds. It then compares the consensus with the record.
The result names the caller in `consensus_caller` and its run lists it as
software. The platform, depths, read counts and polishing rounds describe that
caller's call.

The caller is a reference-inspection method, not a validated release method.
Its handling of ties and homopolymers is its own. If your lab's acceptance
criteria require a validated caller, call the consensus with it and compare
that consensus instead, as below.

## From a supplied consensus

Run **Verify a plasmid from a service's consensus** with the one FASTA
sequence a service assembled for the prep, or your lab's own validated caller
did. In **Assembled by**, name the service and its order or sample ID, or the
caller and its version. The comparison finds the consensus whatever its
rotation or strand. The result's `consensus_caller` is `supplied`. It has no
depths, read counts or per-read support, because none were given.

## The result

A **Whole-plasmid alignment** (WPA) records in its verdict whether the
consensus **matches** the record, **differs** from it, or is **incomplete**:
nothing differs, but some bases aren't called. A consensus with more
sequence than the plasmid, or with some of it twice over, differs: those
bases are listed as not placed once.

Each difference is named in HGVS, with the plasmid's features that hold it
and the run of one base it lies in, and how many bases it replaces and has.
The record keeps the first 200 bases of each side of a long change; the
differences table the run keeps has every base. For reads it also records the reads
across it and those with its whole allele, each read once however many
times it passes, on each strand. A one-base deletion in a long homopolymer
is a candidate nanopore error, to be weighed by its support: the reads
alone can't settle it.

The **Map** tab shows the depth of reads round the plasmid, the stretches
under the minimum depth, and a mark at each difference, with the same
information in words and tables. The run keeps the consensus FASTA, the
depth and difference tables, and the reads' alignments.

To record the verification, open the prep, run **Record plasmid
verification** (lots plugin) and select the alignment. The reviewer then
signs the outcome in the deployment's review flow, Research or Regulated
QC. A clean result supports a match. A result with differences supports a
discrepancy, or a match with notes explaining why each difference doesn't
count against the prep.

## Compute

The actions run in the **long-read tools** environment, on the backend
tier. It is locked with conda-lock for linux-64 and linux-aarch64 and
holds Python and minimap2. Bind it to a pool in the deployment's compute
settings.

## Tests

`tests/test_whole_plasmid.py` runs the script on minimap2's recorded
answers:

- public reads: SRA run SRR17915342, pUC19 on a MinION, subsampled to every
  900th read
- synthetic edits of pUC19 (GenBank L09137.2): rotated, the other strand, a
  deletion across the origin, an insertion, and a linear molecule that stops
  short

`tests/replay.py` says how to record the answers again.
