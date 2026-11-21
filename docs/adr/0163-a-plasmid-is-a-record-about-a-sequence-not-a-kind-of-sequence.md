# 0163. A plasmid is a record about a sequence, not a kind of sequence

- Status: Accepted
- Date: 2026-11-19
- Refines: [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0131](0131-a-derived-value-follows-from-the-identity-alone.md),
  [0138](0138-an-identity-reads-sequences-and-sets-of-parts-as-what-they-are.md)
- Refined by:
  [0168](0168-a-plasmids-topology-is-its-entrys-and-a-linear-molecules-ends-are-ends.md),
  which widens it to linear plasmids: topology on the entry, a linear map, and
  plans whose products can end linear;
  [0169](0169-a-types-records-export-as-files-iki-writes-from-its-fields.md),
  which exports a plasmid as its sequence's GenBank under its own name

## Context

A plasmid is a circular double-stranded DNA molecule, and
`iki/dna.DNASequence` already is that molecule: its identity is its bases,
topology and strandedness, read as the molecule they are, so one plasmid is
one entry however it's spelled — from any origin, on either strand
(ADR 0138). What a lab keeps about a plasmid beyond its sequence is
everything a vector catalogue holds: how it's selected, where it replicates
and at what copy number, the hosts it works in, its promoters, its biosafety
level, the Addgene number or vendor it came from, and the material transfer
agreement it came under.

Three ways to hold that were open:

- **A subtype of `iki/dna.DNASequence`.** A subtype shares the identity's
  entries (ADR 0115), so registering pUC19's bases as a `Plasmid` would find
  the `DNASequence` entry that an import from GenBank made — and be refused,
  because an entry registered as one type can't be registered as another
  ("This is registered already, as a iki/dna.DNASequence"). The usual first
  step with a plasmid is to fetch its record from GenBank, so the usual
  first step would make it impossible ever to call it a plasmid. A subtype
  also can't have registry fields of its own.
- **An extension of `iki/dna.DNASequence`**, as `iki/antibody` extends
  `iki/protein.Protein`. An extension may have registry fields, but its
  fields go on every DNA sequence: a gene, an mRNA and a genome would each
  get a copy number and a biosafety level, and "the plasmids" would be a
  search for sequences that happen to have one. A plasmid wants an ID of its
  own, PLS-000012, that a notebook can cite.
- **A type of its own whose identity is the sequence.**

Vector catalogues work the third way. Addgene's plasmid record and GenBank's
sequence record are separate things about one molecule, cross-referenced;
Benchling registers a plasmid against a DNA sequence schema; Dotmatics and
Biovia hold a registration with components pointing at sequences.

## Decision

**`iki/plasmid.Plasmid`'s identity is `[sequence]`, a reference to an
`iki/dna.DNASequence` entry.** The plasmid is the record about that molecule,
and the molecule's own reading stays the DNA plugin's: this plugin never
spells a plasmid's bases, never rotates one and never reverse-complements one
for an identity. Two plasmids of one sequence are one entry, so a plasmid
fetched from GenBank in one project and registered from a vendor's file in
another is PLS-000012 in both.

A plasmid, its sequence and the cloning plans that make it are then one chain
in the graph (ADR 0123): `Plasmid.sequence` and `Assembly.product` both point
at the DNA sequence, so walking upstream from a plasmid reaches the plan that
made it, and walking downstream from a fragment reaches every construct
built from it.

**What a plasmid is, for everyone who holds it, is its entry's; what a run or
a project chose is each project's record's.** This follows the line
`iki/antibody` drew between a chain type (the sequence alone says it, so the
entry's) and a numbering (the scheme a run chose says it, so the record's):

- The entry's: `length`, which follows from the sequence alone
  (`registry: derived`); and `vector_type`, `hosts`, `copy_number` and
  `biosafety_level`, which a registration team curates (`registry: fact`).
  None of these four can be computed from the bases: a copy number depends on
  the host and how it's grown, and a biosafety level is a judgement about a
  molecule, not a property of it.
- The record's: `annotated_features` and the `replication_origins`,
  `bacterial_resistance`, `selection_markers` and `promoters` it finds.
  They're not derived, because they follow from the identity **and** the
  library a run annotated with and the identity it asked for — which
  `annotated_with` records. A better library finds a marker the old one
  missed, and a project re-annotates; a derived value couldn't be corrected
  (ADR 0131), and a fact can't be computed at all.
- The record's also: where this project's plasmid came from (`source`,
  `addgene_id`, `vendor`, `depositor`), the agreement it came under
  (`mta_*`), how this lab grows it, and the institutional biosafety approval
  that covers its own work with it. Two labs holding one plasmid got it from
  different places under different terms.

**An assembly's product is the sequence, not the plasmid.** A cloning plan is
checked by working out the molecule it makes and comparing it with the
molecule the clone turned out to have, so what it names has to be the
sequence. The plasmid record for that sequence is registered beside it.

**An assembly has no identity.** One molecule can be built by several routes,
and how a lab built its own copy is the lab's record, not a fact about the
molecule.

**`iki/plasmid.Plasmid` covers circular plasmids, and says so.** Linear
plasmids are real -- _Kluyveromyces lactis_ carries pGKL1 and pGKL2,
_Borrelia burgdorferi_ a set of them, _Streptomyces coelicolor_ SCP1 -- and
GenBank records them (their `LOCUS` line reads `linear`), as Benchling and
SnapGene both let a plasmid record be linear. So the refusal here isn't a
claim about what a plasmid can be. It's the boundary of what this plugin
reads correctly: its map draws a circle, its annotation looks across the
origin, and its cloning simulator keeps only the products that close into
one. A linear sequence let in would be drawn and annotated as though its two
ends were joined, which is worse than being told to register it as a DNA
sequence. The type's rule and its `sequence` field say that this is the
plugin's boundary rather than biology's, and naming the molecules it excludes
is what makes it a scope to widen later: a `topology` on the entry's side, a
linear drawing in the map, and a cloning plan that can end linear.

## Consequences

- One molecule has two citable IDs, DNA-000001 and PLS-000012: the sequence
  and the vector. That's the price of keeping vector fields off every gene
  and genome, and it mirrors how a lab already cites GenBank for the sequence
  and Addgene for the plasmid.
- A plasmid's page has no sequence viewer, since its own fields hold no
  bases: its bases are one click away on its sequence, and its own page shows
  the circular map its annotation draws, which is how a plasmid is read.
- Registering a plasmid takes two writes (the sequence, then the plasmid).
  `register_plasmid` and `assemble` do both in one run.
- A plugin that wants another kind of record about the same molecule (a
  cosmid, a BAC, a viral genome) makes its own type the same way, without
  touching `iki/dna`.
- A linear plasmid is held as an `iki/dna.DNASequence`, which reads it
  correctly, and has no vector fields and no map until the plugin's map,
  annotation and cloning simulator read a linear molecule too.
