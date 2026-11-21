# 0168. A plasmid's topology is its entry's, and a linear molecule's ends are ends

- Status: Accepted
- Date: 2026-11-20
- Refines: [0163](0163-a-plasmid-is-a-record-about-a-sequence-not-a-kind-of-sequence.md),
  [0138](0138-an-identity-reads-sequences-and-sets-of-parts-as-what-they-are.md)
- Refined by:
  [0171](0171-a-types-records-export-as-files-iki-writes-from-its-fields.md),
  which writes the topology in GenBank's LOCUS line and leaves out features
  across a linear molecule's ends, saying so;
  [0169](0169-what-the-bases-say-orfs-primer-sites-and-the-product-of-a-pcr.md),
  which reads what a molecule's own bases say: its open reading frames in six
  frames, where the project's primers bind it, and the product a pair of them
  amplifies, each keeping a circle's origin and a line's ends as this ADR has
  them

## Context

ADR 0163 made `iki/plasmid.Plasmid` a record about a DNA sequence, and bounded
it to circular ones: the map drew a ring, the annotation read across the
origin, the cloning simulator kept only products that closed, and a rule
refused a linear sequence. That ADR said what widening the scope would take:
"a `topology` on the entry's side, a linear drawing in the map, and a cloning
plan that can end linear".

The molecules that scope left out are ordinary ones. _Kluyveromyces lactis_
carries the linear killer plasmids pGKL1 and pGKL2 (INSDC X01095.1 and
X01104.1); _Borrelia burgdorferi_ carries a set of linear plasmids beside its
chromosome; _Streptomyces coelicolor_ carries SCP1. GenBank records them: a
record's LOCUS line reads `linear` or `circular`, and both Benchling and
SnapGene let a plasmid be either. A lab's own constructs are linear as often:
a PCR product, a fragment cut out of a vector, a vector linearised by one cut,
a Golden Gate or Gibson assembly of fragments into a linear construct.

Topology is not a label on a molecule, it is part of which molecule it is. The
same bases closed into a circle and left with two ends behave differently, are
drawn differently, and are annotated differently: a circle has an origin that
features read across and no ends, and a linear molecule has two ends and no
origin. GenBank's LOCUS line and SnapGene's file flag both carry it with the
sequence for that reason, and `iki/dna.DNASequence`'s identity has held it
since ADR 0138 (`identity: [bases, topology, strandedness]`).

## Decision

**A plasmid's topology is its registry entry's, derived from its sequence.**
`Plasmid.topology` is `registry: derived` and `computed`: it follows from the
identity alone, since the identity is the sequence entry and that entry's
topology is part of _its_ identity. So it is the same for every project, set
once and then never changed, and only an action writes it. A rule keeps it
honest at every save, however a record is written (ADR 0127):
`record.topology == record.sequence.topology`. A scientist therefore cannot
state it against the molecule, and a run that wrote the wrong value is
refused.

Changing a plasmid's topology is not an edit. The same bases are two sequence
entries, one circular and one linear, so they are two plasmid entries with two
IDs; filling the entry's `topology` is a revision of the entry, audited like
any other field, and shown in its history.

**Identity keeps reading each molecule as what it is** (ADR 0138): a circular
duplex is the least rotation of whichever strand sorts first, so a plasmid
entered from another origin or on the other strand is one entry; a linear
duplex is whichever of its two strands sorts first, because both are the
molecule read from its other end, and a rotation of it is a different molecule
with its ends somewhere else. The cloning simulator's own comparison follows
the same rule, so a clone sequenced from the other strand matches its plan and
a rotation of a linear product doesn't.

**The map draws the molecule it is.** A linear plasmid opens on a linear map, a
line from base 1 on the left to its last base on the right, with an end drawn
at each; it is offered no ring, since it has no origin to go round. A circle
opens on its ring, as a plasmid is read, and can be shown as a line too, cut
at base 1, which is how a map is read beside a gel or a sequencing read —
Benchling and SnapGene both offer the two views this way. Each view is a
button that says whether it is the one being shown, reachable and pressable
from the keyboard, and both are described in words with every part in the
table beneath, so nothing is told by the drawing alone (ADR 0084).

**Nothing is annotated across a linear molecule's ends.** The library's parts
are looked for round a circle's origin and not past a line's ends, so pUC19
linearised with ScaI — one site, inside its beta-lactamase gene — loses the
AmpR annotation that the same bases as a circle carry, and keeps the origin
and the lac promoter. An imported GenBank feature whose location reads across
the origin (`join(2306..3215,1..1623)`, as INSDC writes a gene across a
circular genome's origin, or a site `100^1`) says the molecule is a circle: on
a record whose LOCUS line says `linear` the two can't both be right, so that
feature is left out and the run's message says which feature, that only a
circle has an origin to cross, and what to do. The rest of the record is
imported, as it is for any location that isn't one on the sequence; refusing
the whole file would lose the records that are right along with the one that
isn't.

A digest counts fragments by topology, which is where the difference is
plainest: _n_ cuts leave _n_ + 1 fragments of a linear molecule and _n_ of a
circle. Wild-type lambda (J02459.1) has five EcoRI sites, so its linear genome
gives the six-band ladder the digest is sold as, and the same bases read as a
circle give five, the two end fragments being one piece across the join.

**A cloning plan's product topology follows from its method and its parts, and
nobody states it.** `Assembly.simulated_topology` is computed by the run:

- A plan whose parts close a circle makes that circle. That is what a ligase
  or an assembly mix leaves to transform, so where a circle is possible it is
  the product, as Benchling's and SnapGene's assembly tools also take it.
- A plan whose parts only join end to end makes the linear molecule they make:
  a Golden Gate or Gibson construct whose outer ends pair with nothing, or a
  cut vector with no insert. One fragment of each part, every order and every
  orientation, with the chain and its reverse counted once, since they are the
  same molecule read from its other end.
- A new `digest` method cuts one molecule and keeps a band, which is linear by
  definition: a vector linearised by a single cut, or a fragment cut out of a
  plasmid. It takes no inserts, which a rule keeps.

A plan that could make several molecules still makes none, and says which they
are, largest first, as a gel reads them; the check against the clone's sequence
settles it, as it did for circles. To see the bands without making anything,
`virtual_digest` lists every fragment of each of the plan's parts in its
`fragments`, with where it lies and the enzyme and overhang at each end, as
SnapGene's and Benchling's virtual digests do.
A product of another topology is not what the plan makes, however its bases
read, so a clone stated as linear can't pass for a plasmid and nobody reaches
Checked on it.

**`assemble` registers a plasmid record only for a circle.** A linear product
of a plan is a cut vector or an assembled fragment, on its way to something
else, so it is registered as the DNA sequence it is. A linear plasmid — pGKL1,
one of _Borrelia_'s — is a plasmid, and whoever knows that registers it as one
(`register_plasmid`, with `topology: linear`).

## Consequences

- The rule that refused a linear plasmid is gone, and pGKL1 is a plasmid
  record with a map, an annotation and vector fields of its own.
- A plasmid's topology is filled by the run that annotates it, as its length
  is, so a record imported through the API shows it once that run has been.
  Until then the map draws a ring, which is what every plasmid record was.
- A linear product's sequence is its top strand from the cut at one end to the
  cut at the other — the reading that gives a digest band its length on a gel.
  An overhang beyond those cuts lies on one strand only and isn't kept, since
  a linear duplex is read as fully paired (ADR 0138): a sticky-ended product
  is four or so bases short of the molecule in the tube at one end, and a plan
  that must record its overhangs needs more than this model.
- A plan with blunt-cut parts has more linear arrangements than circular ones,
  so an ambiguous blunt plan is answered with more candidates than before. The
  arrangement limits (`MOST_ARRANGEMENTS`) still bound the search.
- SnapGene's `.dna` files are still not read, and no sequence is exported from
  Iki yet, so topology is read from GenBank's LOCUS line and from the import's
  `topology` param (linear unless it says otherwise, which a plasmid in a FASTA
  file needs changed) and written nowhere. Reading `.dna` and writing GenBank
  are file-format work for `iki/dna`, not topology work, and each needs its own
  slice: a `.dna` reader has to read features and primers too, and a GenBank
  writer has to write every location INSDC allows.
- PCR isn't simulated. A PCR product is a linear molecule a lab registers as a
  DNA sequence and uses as an insert; working one out from a template and two
  primers needs primers on an assembly, which this type doesn't have.
