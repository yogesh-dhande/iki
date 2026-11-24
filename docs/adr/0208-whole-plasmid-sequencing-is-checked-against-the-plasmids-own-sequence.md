# 0208. Whole-plasmid sequencing is checked against the plasmid's own sequence

- Status: Accepted
- Date: 2026-11-23
- Refines: [0184](0184-a-plasmid-prep-links-to-reviewed-verification-evidence.md),
  [0163](0163-a-plasmid-is-a-record-about-a-sequence-not-a-kind-of-sequence.md),
  [0204](0204-a-mutagenesis-states-its-changes-in-hgvs-and-registers-the-mutant-and-its-primers.md)

## Context

Labs now sequence whole plasmids on long reads as routine. A core or a
commercial service sends basecalled nanopore reads, or PacBio HiFi reads, of
each prep, and often the consensus it assembled from them as well. The lab
then needs to know whether the prep is the plasmid it should be. ADR 0184
lets a prep's verification cite an alignment whose reference is the prep
plasmid's registered DNA, but no action made that alignment from long reads.

A plasmid is a circle. A read can cross the origin, and so can a difference
from the record. A service may also send its consensus rotated to a
different start, or as the other strand. Nanopore reads have errors that
cluster in runs of one base (homopolymers) (Delahaye & Nicolas 2021). A
caller that doesn't account for this reports errors as differences, or
reports one difference at several positions.

Iki ships no basecaller. Basecalling is the customer's licensed tool. The
usual nanopore polisher, medaka, is under the Oxford Nanopore public
licence, which allows research use only. Iki can't ship it.

## Decision

A first-party plugin, `iki/plasmid_sequencing`, records a
**Whole-plasmid alignment**. This type extends `iki/core.Alignment`, so the
lots plugin's prep verification takes it as the read alignment (ADR 0184)
without depending on this plugin. Its reference is the plasmid's
registered DNA sequence. A rule checks that the reference is the
plasmid's sequence. The result pins the plasmid's project record and
revision, as ADR 0184's verification does. It is derived from both the
plasmid and the read set.

The plugin is separate from `iki/sequencing`, so that plugin keeps no
dependency on the DNA and plasmid plugins. Lots also never depends on
this plugin.

Two actions make the alignment:

- **Verify a plasmid from its long reads** reads a FASTQ read set from
  where it already lies, using the version and checksum the read set pins.
  - It aligns each read with minimap2 (Li 2018, `-x map-ont` or
    `map-hifi`) to the plasmid sequence doubled, so a read across the
    origin aligns in one piece.
  - It moves each read's indels to their 3'-most position, the end HGVS's
    3' rule names a change at, so a deletion in a homopolymer counts at
    one position. (VCF normalizes to the other end: Tan et al. 2015.)
  - It counts each read once at each position, however many times it
    passes it: most reads are longer than the plasmid, and their passes
    are one molecule's evidence. A read's passes vote among themselves
    first; a read whose passes tie is one of the reads there but votes
    for no allele.
  - It calls the base, or insertion, more than half of the reads have, at
    each position with at least the minimum depth of reads. A position
    with fewer reads, or no majority, is N. Where most reads insert
    something but two sets of inserted bases tie, the insertion is N.
  - It then polishes: it aligns the reads to the consensus again and calls
    again until the consensus stops changing, in at most four rounds of
    alignment. This is
    the approach of racon (Vaser et al. 2017), without its code.
  - For each difference it records the reads across it and those that
    have its whole allele, each read once, and how many on each strand. A
    read has the allele where its own bases there are closer to the
    consensus's than to the record's, in edit distance. A change of
    several bases is then supported only by reads that have all of it.
- **Verify a plasmid from a service's consensus** takes the one FASTA
  sequence a service assembled. It refuses a file holding more than one
  sequence, and a sequence that isn't DNA in IUPAC codes.

Both actions compare the consensus with the record using minimap2's
`asm5` preset, against the record tripled. This finds the consensus
whatever its rotation or strand, and a difference across the origin. They
name each difference in HGVS (den Dunnen et al. 2016):

- `o.` for a circle and `g.` for a linear molecule.
- The positions are 1-based, with indels moved 3'.
- The kinds are substitution, deletion, duplication, insertion, inversion
  and deletion-insertion.

Each difference also records:

- the features of the plasmid that hold all of it, from the sequence's own
  features and the plasmid's annotation, kept on the result as they were
  when compared, with their strands. A join holds only its parts' bases,
  not the gaps between them.
- the length of the run of one base it lies in, so a reviewer can see
  which differences are candidate nanopore errors. The reads alone can't
  settle such a difference: that takes the reviewer, or another result
  for the same prep.

A base that the record's IUPAC code allows, and an N in the consensus, are
not differences. Every base of the consensus must be placed on the record
once. Bases that align nowhere, such as more sequence than the plasmid
has, and bases placed where earlier ones already were, such as a second
lap of the circle, are recorded as unplaced. The consensus file then keeps
the consensus as it was, not cut to the record's frame.

The result's agreement is one of three values:

- **matches**: every base is called and agrees with the record, and the
  whole consensus is placed once
- **differs**: at least one difference, or some of the consensus isn't
  placed once
- **incomplete**: nothing differs, but some bases aren't called

The result also records:

- the depth of reads along the plasmid in at most 360 bins
- each stretch under the minimum depth
- the consensus as a FASTA file
- the comparison as an aligned FASTA file
- the tables of depth and differences
- the reads' alignments, gzipped PAF
- the method, with minimap2's version and its options

The alignment is evidence, and its values are computed and immutable. The
prep's verification record (ADR 0184) holds the outcome, notes,
signatures and the review flow: Research or Regulated QC. A clean result
lets the reviewer record a match. Differences go to the reviewer, who
records a discrepancy, or a match with notes explaining why. For example,
the differences might all be homopolymer errors on a minority of reads.

The Map tab draws the result on the plasmid map from `@ikisci/science`:

- the depth of reads round the ring, or along the line
- the minimum depth as a dashed threshold
- uncalled stretches dashed on the backbone
- a mark at each difference

The tab also gives the same information in words and in tables.

The long-read tools environment is locked with conda-lock for linux-64 and
linux-aarch64, with each package's URL and SHA-256. The plugin validator
now accepts conda-lock's unified format as well as an explicit lockfile.
It requires each listed platform to have packages, and every package to
be a conda package with a URL and hash. The file name must end in
`-lock.yml` or `-lock.yaml`, because micromamba reads the format only from
such a name.

## Consequences

A prep sequenced as a whole plasmid gets a reviewable comparison with its
plasmid's record. Each difference has a name, a place, a feature and its
read support. The reads stay in their bucket. Iki keeps pinned references
and checksums, and the run's own copies of the consensus and tables.

- Without a basecaller or a neural polisher, a consensus from raw reads is
  a majority call. Homopolymer errors at modest depth can survive it. The
  run length shown on each difference is how the reviewer judges them.
  A service's own polished consensus can be compared instead.
- Large structural changes are named as deletion-insertions. Mixed
  populations of plasmids are reported only through each difference's
  fraction of reads, not as separate haplotypes.
- The tests replay minimap2's recorded answers for public reads (SRA run
  SRR17915342, pUC19 on a MinION, subsampled to every 900th read) and for
  synthetic edits of pUC19 (GenBank L09137.2). The demo deployment runs
  the action itself on the same reads.

## References

- Li H. Minimap2: pairwise alignment for nucleotide sequences.
  _Bioinformatics_ 2018;34:3094–3100. doi:10.1093/bioinformatics/bty191
- Tan A, Abecasis GR, Kang HM. Unified representation of genetic variants.
  _Bioinformatics_ 2015;31:2202–2204. doi:10.1093/bioinformatics/btv112
  (VCF's left normalization, the opposite end to HGVS's)
- HGVS. General recommendations: the 3' rule.
  https://hgvs-nomenclature.org/stable/recommendations/general/
- Vaser R, Sović I, Nagarajan N, Šikić M. Fast and accurate de novo genome
  assembly from long uncorrected reads. _Genome Res_ 2017;27:737–746.
  doi:10.1101/gr.214270.116
- den Dunnen JT et al. HGVS recommendations for the description of sequence
  variants: 2016 update. _Hum Mutat_ 2016;37:564–569. doi:10.1002/humu.22981
- Delahaye C, Nicolas J. Sequencing DNA with nanopores: troubles and biases.
  _PLoS ONE_ 2021;16:e0257521. doi:10.1371/journal.pone.0257521
