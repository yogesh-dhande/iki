# 0203. A codon optimization is a design, and its product is a registered sequence

- Status: Accepted
- Date: 2026-11-22
- Refines: [0172](0172-what-the-bases-say-orfs-primer-sites-and-the-product-of-a-pcr.md),
  [0163](0163-a-plasmid-is-a-record-about-a-sequence-not-a-kind-of-sequence.md),
  [0131](0131-a-derived-value-follows-from-the-identity-alone.md)

## Context

A lab that wants a protein made in a host it did not come from writes the
gene again in that host's codons. It is the first thing done to any gene
that will be synthesised, and every tool a molecular biologist uses offers
it: Benchling's codon optimization, GeneArt's, IDT's, Twist's. Iki had
nothing, so the sequence a lab ordered was designed somewhere else and
arrived in Iki as bases with no record of where they came from or what they
were designed against — which is exactly the thing a regulated lab cannot
have, because the design is part of the construct's history.

The decisions it needs are not obvious ones:

- **Which codon usage,** counted from what. A codon usage table is evidence,
  and two tables for "human" disagree by more than the difference between
  two hosts.
- **Which measure of how good a gene is,** computed how. CAI is the
  universal answer, and CAI without the table it is against is a number
  nobody can read.
- **What the optimizer must avoid,** and what it must do when it cannot
  avoid it. A gene shifted to the most-used codon of each family is
  repetitive, often GC-extreme, and frequently has a restriction site or a
  splice donor in it that the lab wanted kept out.
- **Where the result lives.** A new sequence is a new molecule; a lab orders
  it, receives it, clones it and sequences it, so it has to be a registered
  DNA sequence, not a text field.

## Decision

### The host's codon usage is counted from its reference assembly, and the counts are shipped

`plugins/dna/data/codon_usage.json` holds nine hosts' tables — _E. coli_
K-12 MG1655, _B. subtilis_ 168, _S. cerevisiae_ S288C, _K. phaffii_
(_P. pastoris_) GS115, human, Chinese hamster (CHO), _S. frugiperda_ (Sf9),
_D. melanogaster_ and _A. thaliana_ — each **counted from the RefSeq coding
sequences of that organism's reference assembly**, which the table names, by
`data/build_codon_usage.py`. Nothing is copied from another database's
table.

**Counts, not frequencies,** so a reader can see how much evidence is behind
each one and compute RSCU (Sharp, Tuohy and Mosurski, _Nucleic Acids Res_
14:5125, 1986) or relative adaptiveness (Sharp and Li, _Nucleic Acids Res_
15:1281, 1987) themselves. Every codon of every kept sequence is counted,
its stop included.

**One coding sequence per gene, the longest.** A table counted transcript by
transcript is a table of the annotation effort, not of the coding: human
genes carry from one to over fifty annotated transcripts each. Sequences
NCBI marks `pseudo` or `partial`, sequences whose length is not a multiple
of three, and sequences with a base other than A, C, G or T are left out:
none of them is a reading frame.

The Kazusa Codon Usage Database, the usual source for such tables, is left
out because its terms don't allow redistribution. A lab with its own
reference set — the highly expressed genes of its own strain, say — counts a
table with the same script.

### CAI is Sharp and Li's, and it says which table it is against

**Relative adaptiveness** w is a codon's count over the count of the
most-used codon for the same amino acid, with **half an occurrence for a
codon the table never shows**, as Sharp and Li say to use so that a gene
carrying such a codon still has an index at all.

**CAI is the geometric mean of w** over a gene's codons, leaving out the
stop codons and the codons whose amino acid has only one codon in the table
— ATG and TGG in the standard code, which Sharp and Li leave out by name,
since their w is 1 whatever the gene is. Generalising from those two codons
to single-codon families is what makes the index right under a table that is
not the standard one.

**A CAI is meaningless without its reference set,** so every record carries
`cai_reference`: the organism, the assembly and how many coding sequences
went into the table. Sharp and Li took their reference set from very highly
expressed genes; a genome-wide table gives a different scale. Two of Iki's
numbers are comparable with each other and not with a published figure from
another reference set, and the record says so rather than leaving a reader
to assume otherwise. The implementation is checked against Biopython's
`Bio.SeqUtils.CodonAdaptationIndex` — an independent implementation of the
same paper — over the same reference set.

### The protein is never changed, and everything else is best effort

**Every codon written codes for the amino acid the template's codon coded
for,** read by the same NCBI translation table, and the run translates both
the template's region and the product and registers nothing at all if the
two proteins differ. That is the one thing a codon optimizer may not get
wrong, so it is checked rather than trusted.

**The first codon and a final stop codon are the template's.** A construct's
start and its stop are what the lab put there — an ATG in a Kozak context, a
GTG a bacterial gene really starts at, a particular stop before a
terminator — and an optimizer that changed either would be changing
something nobody asked it to.

**Everything else is a constraint, repaired codon by codon, and reported
when it can't be met.** Codons are chosen by a strategy and then, one
violation at a time, each codon the violation lies across is tried with each
of that residue's other allowed codons and the substitution leaving the
fewest and least-broken constraints is taken; where several are equal, the
codon the host uses more wins, so a repair costs as little adaptation as it
can. This is the shape published gene designers have (Hoover and Lubkowski,
_Nucleic Acids Res_ 30:e43, 2002; Villalobos et al., _BMC Bioinformatics_
7:285, 2006). What is avoided:

- **Rare codons**: no codon whose w is below a threshold (0.1 by default,
  which leaves out _E. coli_'s AGA and AGG), unless its amino acid has no
  other. Rare codons are read by rare tRNAs, and a run of them stalls
  translation.
- **GC content**, 30–70% overall and 25–75% in any 50-base window by
  default. Both extremes make a gene hard to synthesise and hard to express;
  in _E. coli_ the GC content of a gene's first stretch is among the
  strongest predictors of how much protein is made (Kudla et al., _Science_
  324:255, 2009). The window matters because a gene inside the overall band
  can still have a GC-rich stretch that fails synthesis.
- **Runs of one base** (more than six) and **repeated stretches** (15 bases
  or more): where chemical synthesis and the polymerase slip, and where a
  construct recombines.
- **Hairpins**: an inverted repeat whose arms are ten bases or more, closing
  a loop of up to thirty. Structure in the message, particularly near its 5'
  end, lowers how much protein is made (Kudla et al., 2009).
- **The restriction sites the design names**, from Biopython's
  REBASE-derived set, on both strands, because an enzyme cuts a duplex.
- **The host kind's own motifs**, which differ by host: for a bacterium, the
  Shine–Dalgarno-like `AGGAGG`, which pauses the ribosome inside a gene (Li,
  Oh and Weissman, _Nature_ 484:538, 2012); for a eukaryote, the 5' splice
  donor consensus `MAGGTRAGT` (Shapiro and Senapathy, _Nucleic Acids Res_
  15:7155, 1987) and the `AATAAA` polyadenylation signal (Proudfoot and
  Brownlee, _Nature_ 263:211, 1976). Which set a host gets follows from its
  own translation table: a host read by table 11 is a bacterium.
- **Anything else the lab names**, as IUPAC motifs, on both strands.

**A constraint no synonymous codon can meet is registered with the design,
placed and named** (`constraints_met`, `unmet_constraints`), and the run
warns. Five methionines with ATGATG forbidden have no answer at all —
methionine has one codon — and a tool that silently returned a sequence
breaking what was asked for, or that changed the methionine, would be worse
than one that says so.

### Three strategies, each because a published method needs it

- **`highest_usage`**, the default: each amino acid's most-used codon in the
  host. The highest CAI, and what a synthesis order usually means by
  optimization.
- **`usage_matched`**: each amino acid's codons in the proportions the host
  uses them, allocated by largest remainder and **dealt round the positions
  so synonyms alternate**. The gene keeps the host's codon distribution and
  is far less repetitive; dealing them round rather than in blocks is what
  stops the allocation itself creating the repeats the constraints then have
  to repair.
- **`harmonized`**: each codon's share within its family kept as near as it
  can be to the share the template's codon has in the organism the gene came
  from, so that where the template's ribosome slowed the new gene's slows
  too (Angov et al., _PLoS ONE_ 3:e2189, 2008). It needs the source
  organism, and the run refuses without it rather than quietly optimizing
  some other way. Its CAI is lower than a highest-usage design's, which is
  the point of it.

**No randomness.** The action is deterministic, so the allocation is by
largest remainder with a stated tie-break and the repair is a stated greedy
order. The same design always gives the same bases, which is what lets a
record be checked years later.

### The design is a record, and its product is the whole template rewritten

**`iki/dna.CodonOptimization` is a record of the design**, as `Assembly` is
of a cloning plan and `Pcr` of a reaction (ADR 0172): the template, the
region of it that codes, which strand, the host, the strategy, the
translation table, and the sites and motifs to keep out. It has no identity:
the same protein is optimized differently for different hosts and different
constraints, and which design a lab ordered is the lab's record.

**The product is the template with the coding region rewritten,** not the
region alone. Optimizing a gene registered on its own gives the optimized
gene; optimizing a gene inside a vector gives the vector with that gene
optimized, which is what a lab ordering a whole construct needs. A
synonymous rewrite changes no length, so every coordinate outside the region
still points where it did — which is why **the template's annotated features
come across unchanged**, and why the product's topology and strandedness are
the template's.

**The product is a registered `iki/dna.DNASequence` derived from the
design,** so the gene, the design and the sequence ordered are one chain in
the graph (ADR 0123), and a cloning plan takes the optimized sequence as a
fragment exactly as it takes any other. As with an assembly, the record
keeps what is known of the product — codons changed, CAI and GC content
before and after, what is still broken — and the product itself is the
record's in the graph, not a reference field: a run's updates are written
before what it creates (ADR 0064).

**Every number the design was computed with is in `optimized_with`,** in
words, as `annotated_with` and `orfs_found_with` already are (ADR 0163,
ADR 0172). These are not derived values (ADR 0131): they follow from the
molecule **and** from what a run asked for, and a better run corrects them.

**A gene on the other strand is read on its own strand and written back
complemented,** so a coding sequence annotated `complement(...)` in a vector
is optimized where it lies and the product still reads as the template does.

## Consequences

- A lab can design a synthesis order inside Iki, and the order, the gene it
  came from and the construct built from it are one walkable chain with the
  parameters recorded. The CAI and GC content before and after are on the
  record, so a reviewer can see what the design did without rerunning it.
- **A CAI here is not a CAI published elsewhere.** It is against a
  genome-wide table, and `cai_reference` names it. A lab comparing with a
  vendor's figure has to compute both the same way.
- **The optimizer measures structure as inverted repeats, not as folding
  energy.** A full nearest-neighbour fold (Turner's rules, as RNAfold
  computes) would give a ΔG for the message's 5' end, which is the quantity
  Kudla et al. actually correlate with expression; a stem length is a
  cruder proxy for it. Folding belongs in a slice of its own, with the
  parameters it needs.
- **Nothing here designs a 5' untranslated region, a Kozak context, a
  ribosome binding site or a terminator**, and nothing chooses where to put
  a tag. Those are parts, and the plasmid plugin's library annotates them;
  an optimizer that silently added them would be designing a construct, not
  rewriting a gene.
- **The 3' splice acceptor consensus is left out.** It is a polypyrimidine
  tract and a position-weight matrix (Shapiro and Senapathy, 1987), not a
  motif, and a motif loose enough to catch it would fire on most genes.
  Avoiding the donor consensus removes the half of a cryptic intron a design
  can remove without guessing.
- **Codon usage is not the only thing that sets expression**, and the ADR
  does not pretend it is. tRNA abundance, the charged fraction of each tRNA,
  mRNA stability, the host's growth state and the plasmid's copy number all
  matter; CAI is the measure the literature and every competing tool use,
  and the lab's own expression data is what settles a design.
- **Nine hosts, and a tenth needs the table counted.** Adding one is adding
  its reference assembly to the build script and rerunning it; it is not a
  code change, and the table it writes names what it counted.
- The repair is bounded: at most 24 codons are tried for one violation and
  at most 20,000 substitutions in all, and a coding region is at most 30,000
  bases. A gene whose GC share is far out of band takes the longest, and the
  bound is why it ends.
