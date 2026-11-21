# 0169. What the bases say: ORFs, primer binding sites, and the product of a PCR

- Status: Accepted
- Date: 2026-11-20
- Refines: [0163](0163-a-plasmid-is-a-record-about-a-sequence-not-a-kind-of-sequence.md),
  [0168](0168-a-plasmids-topology-is-its-entrys-and-a-linear-molecules-ends-are-ends.md),
  [0117](0117-components-show-many-entities-in-more-places-than-a-tab.md)

## Context

A plasmid record said what the plugin's library found along it (ADR 0163) and
a cloning plan what its enzymes make (ADR 0168). Three things a scientist
opens Benchling or SnapGene for every day were missing, and all three are read
out of the bases themselves:

- **What it codes for.** The open reading frames, in all six frames: the way
  you see that a construct's insert is in frame, that a mutation left a stop
  in the middle of a gene, or what an unannotated stretch of a vendor's vector
  is.
- **Where the lab's primers sit on it.** A project has its sequencing,
  screening and cloning primers; what matters is where each one binds this
  molecule, on which strand, and whether it binds anywhere else.
- **What a pair of them amplifies.** The product, with its length and the
  primers' melting temperatures, registered so that a cloning plan can use it
  as a fragment. ADR 0168 left this out and said why: "PCR isn't simulated… a
  PCR product is a linear molecule a lab registers as a DNA sequence and uses
  as an insert; working one out from a template and two primers needs primers
  on an assembly, which this type doesn't have."

Each needs a decision of its own, because each is read with parameters, and a
number computed under parameters nobody recorded can't be read again a year
later.

## Decision

### An ORF is a start codon and the stop that ends it, in the code a run chose

**One ORF for each stop codon,** from the first start codon after the stop
before it in the same frame: a later start before the same stop codes for part
of the same protein, so it isn't a second ORF. That is how NCBI's ORFfinder
and EMBOSS's `getorf` report them, and it keeps a frame's longest reading the
one reported.

**At least 75 codons, and the run says so.** 75 is SnapGene's and NCBI
ORFfinder's shortest offered protein, and shorter frames occur in any stretch
of DNA by chance: 3 kb of random bases has dozens of 25-codon frames and
almost no 75-codon one. The run takes any length from 1 upwards, for a short
peptide or a leader someone is looking for.

**The genetic code is one of NCBI's translation tables,** by name: the standard
code (table 1) and the bacterial, archaeal and plant-plastid code (table 11),
which an _E. coli_ vector's genes are read by, with the vertebrate, yeast,
mould/protozoan and invertebrate mitochondrial codes (2, 3, 4, 5) and the
alternative yeast nuclear code (12) beside them. The table decides what stops
as well as what codes: the same bases whose only in-frame stop is TGA are an
ORF under table 1 and no ORF under table 2, where TGA is tryptophan. Tables
27, 28 and 31 are refused, not silently approximated: in each, a codon is a
stop in some places and a sense codon in others, and the bases alone don't say
which.

**ATG starts an ORF, and the table's other start codons do only when asked.**
Table 11 starts a gene at TTG, CTG, ATT, ATC, ATA or GTG as well, and
bacterial genes do — pBR322's Rop begins at a GTG — so a run that asks for
them reads such a gene from its own start; a run that doesn't is spared the
many frames those codons open. A start codon is translated as methionine
whatever it is, as an initiator tRNA reads it.

**A circle's ORF may read across its origin; a linear molecule's may not**
(ADR 0168). On a circle each frame is followed round, and an ORF is at most the
molecule's length from start to stop, so it is reported from its highest base
round to its lowest. On a linear molecule a frame that runs off an end with no
stop is no ORF: nothing says where that protein would end. pUC19 written from
base 2001 shows the whole difference — its β-lactamase is one 286-codon ORF
across the join as a circle, and no ORF at all as a line. A truncated frame
running off an end, which SnapGene offers as "except at DNA ends", is left out
for the same reason; it would be a claim about bases the molecule doesn't have.

### A primer binds by its 3' end, and a mismatch there weighs more

A polymerase extends a primer from its 3' end, so that end is what has to pair.
**A primer is placed by its 3'-most bases** (15 by default, or all of it if it
is shorter), on either strand, without gaps, and **a site is kept when its
mismatches weigh little enough**: 3 for a mismatch among the primer's last five
bases, 1 for one further away, against a budget the run sets (2 by default).
So by default a site pairs exactly or with mismatches well away from the 3'
end; a mismatch at the end is no site at all; and a run that wants to see such
a site raises the budget to 3 and is told where it is.

That weighting is what PCR does. A mismatch at or near the 3' terminus cuts
extension sharply, the more the nearer the end (Kwok et al., _Nucleic Acids
Res_ 18:999, 1990; Stadhouders et al., _J Mol Diagn_ 12:109, 2010), while
mismatches toward the 5' end are copied into the product without stopping it.
SnapGene places a primer the same way, by a stretch of its 3' end with an
allowance for one isolated mismatch in it; Iki's rule is a weighting rather
than a count, so what it allows is one number a scientist can read and change.
Beyond the placed stretch the primer goes on pairing toward its 5' end while
its bases match, and what is left is its **5' tail**, which the template hasn't
got and a product has.

**A site's melting temperature is of the bases that annealed, against the
template's own bases**, so a mismatch lowers it: SantaLucia's unified
nearest-neighbour model (_PNAS_ 95:1460, 1998) as Biopython's
`MeltingTemp.Tm_NN` computes it with its default table `DNA_NN3` and
SantaLucia's salt correction, at 50 mM Na⁺ with no Mg²⁺ or dNTPs and 25 nM of
each strand. Those are the conditions `iki/dna` gives a primer's own Tm under,
so a primer's record and its site on a template agree where it matches exactly,
and both differ the same way from a Tm computed for a PCR buffer. Where
Biopython has no parameters for a mismatch, no Tm is given rather than a
guessed one.

**A site may cross a circle's origin and never a linear molecule's ends**, for
the reason an ORF may not: beyond an end there is nothing to pair with.

**Which primers are looked for is the run's input**, a list of the project's
`iki/dna.Primer` records the person picks, as every run reads only what is in
its project (ADR 0066). Nothing searches a registry: a site is interesting
because the lab holds the primer.

### A PCR is a record, and its product is a registered sequence

**`iki/plasmid.Pcr` is a record of the reaction**, as `Assembly` is of a
cloning plan: a `template`, a `forward_primer` and a `reverse_primer`, and what
the run works out from the three. It has no identity, for the reason an
assembly has none — one fragment can be amplified by many pairs of primers, and
how a lab made its own copy is the lab's record.

**The product is the forward primer whole, the template between the two
primers' 3' ends, and the reverse primer's reverse complement whole.** Both 5'
tails are in it, and so is every base where a primer mismatched the template,
because there the product is copied from the primer: that is what puts a
restriction site or a Gibson overlap on a fragment, and what a plan built on
the product has to see. A circular template is read round its origin, so a
product may cross it; a linear one is read between its ends, so only a pair
with the forward site to the left of the reverse one amplifies anything.

**The product is a linear double-stranded DNA sequence,** registered
`derived_from` the reaction, whatever the template's topology was: a PCR
product has two ends. So the product, the PCR that made it, its template and
its primers are one chain in the graph (ADR 0123), walkable in both directions,
and a cloning plan takes the product as a fragment by its sequence, exactly as
it takes a synthesised one. The reaction keeps the product's length, each
primer's Tm where it binds, and `tm_method`, the conditions those were computed
under.

**A reaction that doesn't make one molecule makes none, and says why.** A
primer that binds nowhere is named, with the 3' match and mismatch budget it
was looked for under. A primer that binds only the strand the other one should
be on is named too, with what to do: a PCR's primers bind opposite strands and
point at each other, so they are swapped. A pair that amplifies several
molecules registers none and lists their lengths, as an ambiguous assembly does
(ADR 0168): the reaction doesn't say which it means, and a band on a gel
settles it.

### The three live on the plasmid record, and the map draws them as layers

**The ORFs and the binding sites are each project's record's, not the registry
entry's,** as the annotation is (ADR 0163). They follow from the molecule **and**
what a run asked for — the fewest codons, the translation table, the start
codons, the 3' match, the mismatch budget, and which primers the lab holds — so
they are not derived values (ADR 0131), and a better run corrects them. Each
layer keeps what it was found with beside what it found (`orfs_found_with`,
`primer_binding_sites_found_with`), so a number can be read years later, as
`annotated_with` already did.

**The map draws each as a layer the person turns off:** the ORFs on rings
inside the backbone and the sites on one outside it, each on lanes of its own
in the linear view, and each with a table beneath holding what a drawing can't
say — an ORF's frame, length in bases and codons, start codon and protein, and
a site's 3' end, annealed length, 5' tail, mismatches with their distance from
the 3' end, and Tm. Every layer is a button that says whether it is showing and
how many it holds, and the words above the drawing name every item of every
layer, so nothing is told by the drawing or by colour alone (ADR 0084). A
layer whose run has been and found nothing says so, which is not what a layer
whose run hasn't been says.

**A site's row names its primer as a way to the primer's record.** The map
declares the project's primers as the entities it reads (ADR 0117), so Iki
gives it their names and bases, as the person viewing may read them; pressing a
row asks Iki to open that primer's page, which Iki checks and navigates itself.
A primer Iki didn't give it is named in plain words.

## Consequences

- A plasmid's page answers what its bases code for, where the lab's primers sit
  on it and what they amplify, without leaving Iki, and every answer carries the
  parameters it was computed under.
- A PCR product is an ordinary DNA sequence, so the cloning plans take it as an
  insert with no new concept, and a construct's graph reaches back through the
  PCR to the template and the primers it came from.
- Melting temperatures here are for sodium alone. Primer3's default buffer adds
  1.5 mM Mg²⁺ and 0.6 mM dNTPs and gives a Tm some 5 to 6 °C higher, so a
  number here is compared with another computed the same way; `tm_method` says
  which that is.
- **ORFs and binding sites are a plasmid record's.** A DNA sequence nobody
  registered a plasmid for — a PCR product, a gene, a genome — has neither yet.
  That is where they belong: Iki's own sequence viewer draws one set of features
  and has no layers to switch, so showing them there is a change to the viewer
  (ADR 0085) rather than to this plugin, and it waits for a slice of its own.
  Registering the sequence as a plasmid is not the answer for a gene, and this
  ADR doesn't pretend it is.
- **Nothing here designs primers,** picks a pair, or checks one for hairpins,
  self-dimers or cross-dimers. Those are a primer-design slice, with Primer3's
  job and Primer3's parameters; this works out what stated primers do.
- The search is bounded: a run keeps at most 500 binding sites and 20 products,
  and at most 5,000 ORFs, and says to ask for longer ORFs or a stricter 3' match
  rather than keeping some and not others. A primer shorter than 8 bases is not
  placed at all, and the run says so: it would bind too many places to mean
  anything.
- A primer's IUPAC codes pair with what they may be, so a degenerate primer is
  placed; a degenerate **template** base pairs only where every base it may be
  is one the primer's code may be, since nothing else says the primer pairs
  there.
- Multiplex reactions, nested PCR, asymmetric or inverse designs beyond one pair
  of primers, and the overhangs a sticky-ended product carries (ADR 0168) are
  still outside the model.
