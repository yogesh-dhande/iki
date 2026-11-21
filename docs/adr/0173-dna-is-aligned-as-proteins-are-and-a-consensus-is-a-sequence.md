# 0173. DNA is aligned as proteins are, on either strand, and an alignment's consensus is a sequence

- Status: Accepted
- Date: 2026-11-21
- Refines: [0088](0088-alignments-are-entities-of-sequence-entities.md),
  [0065](0065-first-party-plugins-images-are-built-and-signed-like-ikis.md)

## Context

The protein plugin aligns two proteins as EMBOSS needle and water do, and
many through MAFFT, each into an `iki/core.Alignment` the alignment viewer
shows (ADR 0088). The DNA plugin aligned only sequencing reads to a
reference. So two plasmids or two genes couldn't be compared in Iki, and
neither could a family of sequences. Benchling does both for DNA, RNA and
proteins, with MAFFT for multiple alignments, and it builds a consensus
sequence from an alignment's rows, as Geneious and SnapGene do. Nothing in
Iki made a sequence from an alignment. The viewer drew a consensus band, but
only for display.

DNA differs from protein in three ways that matter here:

- **A sequence has two strands.** A DNA sequence's identity is either of its
  strands (ADR 0138). Records of one plasmid or gene in different databases
  often run opposite ways, as pET vectors' do. MAFFT's `--adjustdirection`
  takes a sequence's reverse complement where that fits better, and
  Benchling's MAFFT alignments turn it on by default. A row that's a
  sequence's other strand, though, still has to link to that sequence and be
  numbered as its bases.
- **Reads observe only part of a sequence.** Their alignments mark
  everything beyond a read's ends `.`, which isn't a gap.
- **Its ambiguity codes are sets of bases,** so a consensus can say "A or G"
  exactly, as R.

## Decision

**The DNA plugin aligns as the protein plugin does, with DNA's scoring.**

- `align_pair` uses EMBOSS's nucleotide defaults: EDNAFULL (NCBI's NUC.4.4),
  gap opening 10 and extension 0.5, with end gaps free globally. Its
  identity, similarity, gaps and score match needle 6.6.0 and water 6.6.0 on
  pUC19 against pBR322. By default the query is aligned both as given and
  as its reverse complement, and the better is kept, since either strand is
  the sequence. A run can also align it one way only; reverse is needle's
  `-sreverse1`.
- `align_multiple` runs MAFFT 7.505 with `--nuc`, taking the protein side's
  strategies. It adjusts direction (`--adjustdirection`) by default, as
  Benchling does, and can run `--adjustdirectionaccurately` or not adjust at
  all. The first row, the reference if there is one, is never turned round.
  A row's bases must be its sequence's, or their reverse complement when
  MAFFT says it turned it round, gaps aside. Otherwise the run fails.
- MAFFT's alignment can differ with the number of threads it runs on. For
  the six vectors in the test data, one to three threads give one alignment,
  and four or eight give another. A run uses the threads its container is
  given, so the alignment's method records how many it had, in both plugins.

**A row that's its sequence's other strand is headed `HRID/end-start`.** An
example is `DNA-000005/8835-1`: the span runs down, as Rfam and Pfam write a
row on the reverse strand. iki-wasm reads such a header as the sequence named
(so the row links to it), from its `end`th base, counting down. The viewer
numbers the row's residues down and says it's the reverse strand. The pairwise
action's EMBOSS file numbers the row down too, as needle numbers a reversed
sequence. Headers that only name a part, `HRID/start-end`, keep their meaning.

**`consensus` registers an alignment's consensus as a sequence derived from
the alignment.** The DNA plugin's version registers a DNA sequence, and the
protein plugin's a protein. Both plugins ship the same script, since each
plugin ships its own scripts; a test checks the two copies match. Each
column gives at most one residue, as Geneious calls a consensus:

- Rows count where they have a residue or a gap. `.` counts as neither.
- A column where more rows have a gap than a residue is left out. With as
  many gaps as residues, the column is kept.
- Otherwise the column's residue is the one at least the threshold's share
  of rows have (50% by default). If none has, it's the fewest commonest
  residues that together reach the threshold, written as their ambiguity
  code: IUPAC's for bases; B, Z or J for D/N, E/Q or I/L; X for any other set
  of amino acids. Residues tied at the edge are taken together or not at
  all, so no call depends on letter order. Geneious's worked example holds:
  6 A, 3 G and 1 T gives A at a threshold up to 60%, R from there to 90%, and
  D above 90%.
- A row's own ambiguity code is an equal share of each residue it stands
  for, as Geneious counts it, so N says nothing of which base.
- Shares are counted as exact fractions.
- A place no row observes is N (or X) inside the consensus, so the stretches
  on either side keep their places. At the ends of a linear consensus, such
  places are left out. A circular consensus keeps every place.
- The alignment's reference counts as a row unless the run leaves it out.
  Leave it out for reads, so the consensus is what was read, not what was
  designed.

The new sequence is registered like any other, so a consensus that's already
in Iki is found. The consensus of reads of pUC19 is pUC19's own registry
entry. Topology and strandedness are the run's params (linear and double by
default), since an alignment doesn't say.

**The DNA plugin has its own MAFFT image**, `images/alignment-tools`, built,
scanned and signed by the publish workflow like the protein plugin's
(ADR 0065). CI builds it and runs its check through the plugin's own
`multiple.py`. The workflow can only publish an image from `main`. Until it
has published this one, the DNA plugin's environment pins the protein
plugin's signed alignment-tools image, which has the same base and the same
Debian MAFFT build. `multiple.py` needs only MAFFT and Python's standard
library. Once the DNA image is published, its digest replaces the pin.

## Consequences

- Two DNA sequences, or many, can be compared in Iki, and each result is an
  alignment with its lineage, opened in the same viewer as a protein's.
- A clone's reads give its sequence as a registered entity, derived from
  the alignment and so from the reads' run. A consensus already registered
  is found rather than duplicated.
- A deployment binds `iki/dna.alignment-tools` to a compute template, as it
  binds the protein plugin's (the dev cluster puts both on one).
- The protein plugin's `align_multiple` records its thread count in its
  method too, for the same reason.
- Circular sequences are aligned from their origins as given, as needle and
  MAFFT align them. Two plasmids with different origins align as two linear
  molecules, with the stretch across one's origin as end gaps. Rotating one
  to the other's origin would need rows that wrap round, which neither the
  FASTA headers nor the viewer can say yet.

## Alternatives considered

- **Cavener's rules** (1987, as Biopython's `degenerate_consensus` and
  TRANSFAC apply them) for ambiguity. They are fixed, made for bases alone,
  and not adjustable. A threshold works for both alphabets, and it's what
  Benchling's and Geneious's users set.
- **Leaving the reference out by default.** That's right for reads but wrong
  for a pairwise alignment, whose reference is one of the two sequences, and
  for a family aligned to its wild type. Counting every row is what Jalview
  and EMBOSS `cons` do. The param says when to leave the reference out.
- **Headers like MAFFT's own `_R_HRID`** for a turned-round row. Such a header
  isn't the sequence's HRID, so the row wouldn't link to its sequence, and
  its bases would be numbered up from 1 along the other strand.
- **Pinning MAFFT to one thread** for an alignment that doesn't depend on
  the machine. That would make thousands of sequences slow. Recording the
  thread count keeps a run reproducible on the same template.
