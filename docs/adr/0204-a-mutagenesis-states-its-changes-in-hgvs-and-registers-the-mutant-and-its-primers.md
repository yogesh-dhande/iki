# 0204. A mutagenesis states its changes in HGVS, and registers the mutant and its primers

- Status: Accepted
- Date: 2026-11-23
- Refines: [0172](0172-what-the-bases-say-orfs-primer-sites-and-the-product-of-a-pcr.md),
  [0163](0163-a-plasmid-is-a-record-about-a-sequence-not-a-kind-of-sequence.md),
  [0138](0138-an-identity-reads-sequences-and-sets-of-parts-as-what-they-are.md)

## Context

Changing one base of a construct is the commonest thing a molecular biology
lab does: a point mutation to test a residue, a tag put in, a site taken
out, a codon deleted. Every tool offers it — Benchling, SnapGene,
Geneious — and each offers the same two halves: the change applied to the
sequence, and the primers that make it at the bench.

Iki had neither. A lab wrote the change down somewhere, designed the primers
in another tool, ordered them, and registered the mutant afterwards as bases
with nothing connecting it to the construct it came from. The thing a
regulated lab most needs — what was changed, from what, and with which
oligos — lived in a notebook.

Two decisions carry the slice:

- **How a change is stated.** It has to be exact, it has to be checkable
  against the template, and it has to be the same notation the lab will read
  back from its sequencing.
- **Which primers.** "Design a mutagenesis primer" is not one thing; the
  published protocols disagree, and a primer designed for one is wrong for
  another.

## Decision

### A change is stated as HGVS names it, and checked against the template

`iki/dna.Mutagenesis` takes its changes as **HGVS DNA descriptions on a
genomic reference** (den Dunnen et al., _Hum Mutat_ 37:564, 2016; HGVS
Nomenclature, Hart et al., _Genome Med_ 16:149, 2024): `g.1234C>T`,
`g.1240_1242del`, `g.1250_1251insGGATCC`, `g.1250dup`, `g.3_6inv`,
`g.1260_1261delinsTA`, with `o.` in place of `g.` for a circular reference.

Those are exactly the names `align_reads` already writes for every
difference it finds against a reference (ADR 0172's neighbourhood), and
`g.`/`o.` are the two prefixes HGVS allows for a genomic reference, which is
what an `iki/dna.DNASequence` is (ADR 0138). So **a lab asks for a change in
the notation it will read back**, and the string it typed and the string its
sequencing report prints are the same string. The alternative — a form with
position, from-base and to-base — cannot say an insertion, a duplication or
an inversion without growing into a worse version of HGVS.

**Every base a change states is checked against the template's own**, and a
change whose stated base isn't there is refused rather than made. HGVS asks
for the reference's own base in a substitution for this reason; a change
written against the wrong sequence, or against the other strand, is the one
mistake in mutagenesis that costs a lab a round of cloning. Where a deletion
or a duplication states its bases — which HGVS discourages but allows — they
are checked too.

**Two changes that touch the same bases are refused.** No sequence has both
a base deleted and that base changed, so a design asking for both is a
design nobody can make, and saying so is better than silently applying one.

**The name is reported back where HGVS puts it.** A change is applied where
the lab stated it, and `changes_made` carries both the string as stated and
the name as HGVS normalises it — as far 3' as the repeated bases allow, the
3' rule, which applies to all of HGVS's description types. The normalising
is `scripts/reads.py`'s, the same code that names a sequencing difference,
so the two can be compared without reading either twice.

**A change may run across a circular template's origin**, because a circle
has no end: HGVS allows the range to run from its first place to its last
(`o.2686_1del`). The product is then written from that change's own first
base, so the change is in one piece rather than split between the two ends
of the molecule, and the record says where the product now begins.

### Three primer designs, each a published protocol, each with its own rules

The protocols differ in what a primer _is_, so the design is the lab's
choice and the record says which was made:

- **`overlapping`** — the QuikChange design (Braman, Papworth and Greener,
  _Methods Mol Biol_ 57:31, 1996): a pair complementary to each other, both
  carrying the change, with correct template on both sides. The rules are
  the kit manual's (_QuikChange II Site-Directed Mutagenesis Kit Instruction
  Manual_, Agilent, Cat. #200523, Rev. E1): 25–45 bases, the change near the
  centre and at least 10 bases from either end, at least 40% GC, terminating
  in C or G, and at least 78 °C by the manual's own formula,
  `Tm = 81.5 + 0.41(%GC) − 675/N − %mismatch`, with the manual's
  insertion/deletion variant — no mismatch term, and N without the bases
  inserted.
- **`partially_overlapping`** — Zheng, Baumann and Reymond (_Nucleic Acids
  Res_ 32:e115, 2004) and Liu and Naismith (_BMC Biotechnol_ 8:91, 2008):
  the two primers share the stretch carrying the change at their 5' ends and
  each has bases of its own at its 3' end, so neither can extend on the
  other. Zheng's rules are counted — at least eight non-overlapping bases at
  each 3' terminus, the change in both primers, at least one G or C at each
  end, the change as near as four bases to a 5' terminus — and Liu and
  Naismith's is a temperature: the non-overlapping part anneals 5–10 °C
  above the shared part, both by Wetmur's formula (_Crit Rev Biochem Mol
  Biol_ 26:227, 1991), which is the formula their protocol uses.
- **`back_to_back`** — Hemsley, Arnheim, Toney, Cortopassi and Galas
  (_Nucleic Acids Res_ 17:6545, 1989): two primers lying back to back on
  opposite strands, 5' ends apposing, amplifying the whole circle outwards,
  the product ligated blunt. The change rides on the 5' end of the forward
  primer and lands at the join; a deletion needs no tail at all. It needs a
  circular template, and is refused on a linear one rather than given primers
  that can't be used.

**The acceptance formula a protocol is written around is reported under its
own name,** never as the primer's melting temperature. Every
`iki/dna.Primer` Iki registers carries a Tm by SantaLucia's unified
nearest-neighbour parameters (_PNAS_ 95:1460, 1998) at stated conditions,
computed by `scripts/sequences.py`; the QuikChange formula and Wetmur's
formula are empirical rules, not nearest-neighbour models, and giving either
as "the Tm" would make two primers in the same project incomparable. So the
design's own measures go in each primer's description, named, and the
primer's Tm field stays the one thing it has always been.

**The citation that isn't.** The QuikChange protocol is usually cited as
"Papworth et al., _Strategies_ 9(3):3–4, 1996". _Strategies_ was
Stratagene's own newsletter; it is not indexed by MEDLINE, has no DOI and no
retrievable text, and Agilent's own documents give the issue number two
different ways. Iki cites the kit manual as the protocol document, which is
what it is, and Braman, Papworth and Greener (1996) as the paper by the
same authors that a reader can actually obtain.

### The mutant is a registered sequence, and so is every primer

The run registers the mutant as an `iki/dna.DNASequence` **derived from the
design**, and each primer as an `iki/dna.Primer` derived from it too, with
the template as the primer's `target`. So the construct, the design, the
oligos ordered and the mutant are one chain in the graph (ADR 0123), a
cloning plan can take the mutant as a fragment, and a sequencing run against
the mutant finds the record of what was meant to change.

A primer's identity is its bases, so ordering the same oligo again finds its
registry entry (ADR 0138): a lab that makes the same change twice gets the
same two primers, not four.

**What the change does to the protein** is reported where the design says
where the coding region is: silent, missense, nonsense, a frameshift, an
in-frame insertion or deletion, with the HGVS protein name in three-letter
code (`p.Glu7Val`, `p.Leu4_Thr5del`, `p.Lys18Ter`, `p.Glu7ValfsTer17`). The
coding region's end moves by what the changes added or took away, so the
same gene is read in the mutant; a change that leaves it short of a whole
number of codons is a frameshift, and is named as one, with the new reading
frame's stop counted from the first residue that changed.

## Consequences

- A lab designs the change, gets the oligos to order and the mutant to
  expect, and the chain back to the construct, inside Iki. What was asked
  for and what sequencing reads back are written the same way.
- **The notation is a learning curve.** `g.1250_1251insGGATCC` is less
  obvious than a form with three boxes, so the type's description, the
  refusals and the tab all carry worked examples, and a name Iki can't read
  is refused with the six forms spelled out. The gain is that the notation
  is the one the field already uses and the one Iki already writes.
- **Only a genomic reference's numbering is accepted.** `c.` and `n.` number
  along a transcript and `m.` says a reference is mitochondrial; none of
  those is what a registered DNA sequence is, so a change called on a
  transcript is renamed against the sequence it is to be made in. The DNA
  plugin's README already says this of the names `align_reads` writes.
- **No protocol is simulated.** Iki designs the primers and says what the
  product will be; whether the polymerase extends them, whether DpnI
  digested enough template and whether the ligation closed are the bench's
  business, and the record of what happened is the lab's.
- **The primers are not checked for binding anywhere else on the template.**
  Where a primer binds a molecule is the plasmid plugin's `primer_binding_sites`
  (ADR 0172), and a designed primer can be run through it; building a second
  search here would be a second implementation of the same thing.
- **No design is offered for a change that runs across a circular origin.**
  The mutant is registered, since applying the change is well defined, but
  the primers would be designed against a molecule written from a different
  base, and the run says so instead of guessing. Registering the mutant and
  designing against it is the way round.
- **Hemsley phosphorylated the product, not the primers.** The kits built on
  the method differ — some ask for phosphorylated oligos, some phosphorylate
  and circularise in one step after amplification — so the record says the
  product must be phosphorylated before it is ligated and leaves which way
  to the lab, rather than ordering oligos a kit may not want.
