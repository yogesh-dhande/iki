# plasmid

Iki's plasmid plugin: plasmids and vectors by the circular sequence that is each one,
the cloning plans that make them, and the common vector parts a plasmid is annotated
from.

- **`iki/plasmid.Plasmid`** (`PLS-000001`): a plasmid or vector.
  - **Its identity is its sequence**: a reference to an `iki/dna.DNASequence` registry
    entry, and nothing else
    ([ADR 0163](../../docs/adr/0163-a-plasmid-is-a-record-about-a-sequence-not-a-kind-of-sequence.md)).
    The DNA plugin reads the molecule — a circular sequence from any origin, a duplex
    from either strand, one entry either way
    ([ADR 0138](../../docs/adr/0138-an-identity-reads-sequences-and-sets-of-parts-as-what-they-are.md))
    — so this plugin never spells a plasmid's bases again. Two plasmids of one sequence
    are one entry: pUC19 fetched from GenBank in one project and registered from a
    vendor's file in another is the same PLS in both.
  - **What a plasmid is, for everyone who holds it, is its registry entry's.** Its
    `length` follows from the sequence alone (`registry: derived`). Its `vector_type`,
    the `hosts` it works in, its `copy_number` (high for a pUC origin, low for pBR322's,
    single for a yeast centromere vector) and its `biosafety_level` are curated
    (`registry: fact`): whoever registers it may set them, and then only a registrar
    changes them, with a reason. None of the four follows from the bases — a copy number
    depends on the host and how it's grown, and a biosafety level is a judgement about a
    molecule rather than a property of it.
  - **What a run or a project chose is each project's record's**: the annotation
    (`annotated_features`, and the `replication_origins`, `bacterial_resistance`,
    `selection_markers` and `promoters` it finds), which depends on the library and the
    identity the run asked for, as an antibody's numbering depends on its scheme; where
    this project's plasmid came from (`source`, `addgene_id`, `vendor`,
    `catalog_number`, `depositor`, `received_on`); the agreement it came under
    (`mta_required`, `mta_reference`, `mta_provider`, `mta_signed_on`,
    `mta_restrictions`); how this lab grows it (`growth_strain`,
    `growth_temperature`); and the institutional `biosafety_approval` that covers its
    own work with it.
  - Its rules hold at every save, however it's written — by an action, through the API,
    by a bulk import or in a bundle
    ([ADR 0127](../../docs/adr/0127-plugins-rules-are-checked-at-every-save.md)): a
    plasmid's sequence is circular; one under a material transfer agreement names the
    agreement; one at BSL-2 or above names its biosafety approval.
  - Its **Map** tab draws it round a circle, each annotated part an arc pointing the way
    its strand runs, with every part also in a table beneath, so nothing is told by
    colour alone. Its bases, with the features its GenBank record gave, are one click
    away on its sequence, in Iki's own sequence viewer.
- **`iki/plasmid.Assembly`** (`ASM-000001`): a cloning plan — a `backbone`, the
  `inserts` going into it, the `method` and the `enzymes` it cuts with. It has no
  identity: one molecule can be built several ways, and how a lab built its own copy is
  the lab's record, not a fact about the molecule. Its `product` is the
  `iki/dna.DNASequence` the clone turned out to have, stated once it's sequenced, while
  what the plan makes is `derived_from` it, so a plan, the sequence it makes
  and the plasmid that is it are one chain in the graph
  ([ADR 0123](../../docs/adr/0123-queries-walk-the-graph-of-what-depends-on-what.md)).
  - Its `status` goes Planned → Checked → Built → Verified, each state set by the lab
    roles that may, with guards: it's Checked only once the plan matches its product,
    Built only from Checked, and Verified only from Built by someone other than whoever
    built it
    ([ADR 0128](../../docs/adr/0128-guards-say-when-a-state-may-be-set.md)). Verified
    locks it. It can be Abandoned from anywhere, with a reason.
  - Whoever recorded a plan hears when a check finds it doesn't make its product, and
    when a reviewer verifies the construct
    ([ADR 0129](../../docs/adr/0129-types-declare-their-notifications.md)).

## Lab roles

A project's Admins give these to its members, beside their project role:

- `cloner` (Cloning scientist): plans a construct, builds it, and records the sequence
  of what was built.
- `construct_reviewer` (Construct reviewer): verifies that a construct's sequence is
  what was planned — someone other than whoever built it.

## Actions

They run on backend workers, in the `plasmid-tools` environment (Python 3.12 with
Biopython, which the annotation's alignments and the digests' restriction enzymes use).
`annotate_plasmid`, `assemble` and `check_assembly` list Biopython 1.88 as their
software, so what their runs make is recorded as made with it
([ADR 0106](../../docs/adr/0106-how-an-entity-was-made-recorded-by-iki-or-stated-by-its-source.md)).

- **`register_plasmid`**: registers a plasmid from its bases — the circular,
  double-stranded DNA sequence, and the plasmid that is it. The same molecule given
  again, from another origin or on the other strand, finds the entries it has already,
  and the project gets its records of them. An Addgene number given with it sets
  `source` to Addgene.
- **`annotate_plasmid`**: finds the library's parts along a plasmid's sequence and
  records what they say about it. A trigger runs it when a project gets a plasmid — on
  each project's own record, not once per entry, since the annotation is the record's.
  Its `length` is its entry's, so a run fills that only where it's still empty.
- **`assemble`**: works out the circle a plan makes and registers what it makes — the DNA
  sequence and the plasmid that is it, both `derived_from` the plan — so a map and primers
  can be had before anything is cloned, and records the circle's length and junctions on
  the plan. It leaves the plan's `product` alone: that's the sequence the clone turned out
  to have, which whoever sequenced it states, and `check_assembly` then compares the two.
- **`check_assembly`**: works out what a plan makes and checks it against the sequence
  the clone turned out to have. A trigger runs it as soon as someone states the product,
  so whoever sequenced the clone hears at once whether the plan explains it.

## Annotating from the library

`data/features.json` holds 34 common vector parts: origins of replication (pMB1/pUC, f1,
SV40, CEN6, ARSH4), bacterial resistance genes (AmpR, KanR, CmR, TetR), markers for the
cells a vector is expressed in (PuroR, NeoR/KanR, URA3), promoters (T7, T3, lac, lacI,
CMV, SV40 early), terminators, affinity tags (6xHis, V5), the elements a lentiviral
vector is packaged by (the 5′ and 3′ LTRs, the packaging signal, the Rev response
element, cPPT) and Gateway's attR1 and attR2.

**Every part is the bases of a feature of a public-domain INSDC record**, taken at the
location that record's own feature table gives, and each part names its record and
location, so the library can be checked against it. NCBI places no restrictions on the
use or distribution of GenBank's data; annotation sets whose terms don't allow this were
left out, SnapGene's common features and Addgene's annotations among them.
[`data/README.md`](data/README.md) lists every record, says which parts come from it,
and gives the licence in full. `data/build_features.py` builds the file again from NCBI,
checking what it writes.

A part is looked for on both strands, and round the origin of a circular plasmid, in two
steps, as BLAST-based annotators (pLannotate) do: the plasmid's 18-mers are indexed and
each 18-mer of the part looked up, which places it; then round each placement the part is
aligned locally to the plasmid (Biopython's `PairwiseAligner`, with BLASTN's scores —
match 2, mismatch −3, a gap of n bases 5 + 2(n − 1)), and the alignment's **identity** is
the part's bases that match over the part's length. A part is annotated where at least
80% of it aligned with at least `min_identity` identity (0.9 by default); a part under 40
bases long is annotated only where it matches exactly, since at that length a few
mismatches say nothing.

Vector parts drift between vectors — pUC19's β-lactamase differs from pBR322's at two
bases — so each hit keeps the identity it had, which the map and the features show. Where
one part's hit lies almost wholly within another's, only the better match is kept: two
parts of a family are alike enough to find each other's places (Gateway's attR1 and
attR2 are 95% identical), and only one of them is there. Parts that merely overlap both
stay, as the SV40 origin within the SV40 early promoter does.

A plasmid's `annotated_with` says which library found its parts and what identity it
asked for, so an annotation can be read years later: `iki/plasmid features 1.0 (34
parts), at least 90% identity`.

## Working out what a plan makes

- **Restriction–ligation and Golden Gate.** Each part is cut with every enzyme the plan
  names, with Biopython's REBASE-derived enzymes, so any of REBASE's enzymes can be
  named and each one's own cut positions and overhang are used: a 5′ overhang (EcoRI), a
  3′ one (PstI), a blunt cut (SmaI), or a Type IIS enzyme cutting outside its site
  (BsaI, BsmBI, BbsI). A circular part is cut round its origin. Then every circle one
  fragment from each part makes is tried, in both orientations, and the ones whose ends
  all pair are the plan's products. Two ends pair when their overhangs are the same
  kind, the same length and the same bases — which is how Golden Gate's four-base
  overhangs direct an assembly, and why a blunt plan has many products.
  - **The backbone goes in as the band cut out of the gel**: its longest fragment, which
    is what a vector digest is purified down to, so a double digest's little piece can't
    carry the insert instead. Every other part offers any of its fragments, as a
    purified insert band may be any size. A plan that keeps another fragment of its
    backbone sets `backbone_fragment` to `any`, and then hears about every circle its
    parts make.
- **Gibson assembly.** Fragments are joined where the last bases of one are the first
  bases of the next, at least `min_overlap` of them (15 by default, as NEB's protocol
  asks for 15 to 80), each shared stretch counted once. A circular backbone is
  linearised first by the enzyme the plan names; a plan with a circular part and no
  enzyme says so rather than guessing.
- **Gateway LR and BP, and TOPO** are recorded as what was done, and not simulated: the
  run says so.
- A plan that makes **no** circle says that no arrangement joins all the way round; one
  that makes **several** says how many and how long each is, and makes none, since the
  plan doesn't say which. Giving the product and checking the plan against it settles an
  ambiguous plan.
- The check compares what the plan makes with the product's sequence **as molecules**:
  the same bases from any origin and on either strand are the same molecule (ADR 0138),
  found by the least rotation of each strand (Booth's algorithm, in linear time). So a
  clone sequenced from another origin, or on the other strand, matches its plan.

Nothing here models partly double-stranded molecules beyond an end's overhang,
methylation, star activity or a nick left by a ligation: a plan whose ends don't pair
exactly makes nothing. Nor does it design primers, pick enzymes or propose a route —
`assemble` works out what a stated plan makes, and `check_assembly` whether it makes
what the clone turned out to be.

## Example data

The lengths and positions in `server/tests/test_plasmid_annotation.py` are checked
against the INSDC records in
[`server/tests/data/plasmids/`](../../server/tests/data/plasmids/README.md): pUC19
(L09137.2, 2,686 bp), pBR322 (J01749.1, 4,361 bp), pRS416 (U03450.1, 4,898 bp),
pcDNA3.1(+) (MN996867.1, 5,428 bp), the lentiviral vector pLenti6-V5-Puro (LT009457.1,
8,835 bp) and a pET-28a(+) derivative (OL989372.1, 6,835 bp). Among what they confirm:
pUC19's origin at 867–1455 and its AmpR at 1626–2486, pBR322's β-lactamase at
3293–4153 and its Rop at 1915–2106 (both found from parts taken out of other records,
and both where pBR322's own feature table has them), pRS416's URA3 at 417–1220, and
pLenti6's two SV40 origins. Every resistance and selection gene found in all six is also
checked to be a whole intact reading frame in that plasmid's own bases — a whole number
of codons from ATG to a stop with none inside — which a hit even one base out of place
would fail.

`server/tests/test_plasmid_cloning.py` clones the 795 bp neo gene of pcDNA3.1(+) into
pUC19's polylinker with EcoRI and HindIII (2,635 + 801 = 3,436 bp), and by Gibson
assembly with 25 bases of homology at each end (2,686 + 845 − 50 = 3,481 bp); it checks
the digests against Biopython's own `catalyse`, that pUC19 is cut after base 396 and base
447 where its published polylinker has its sites, and that the clone read from another
origin on the other strand still matches its plan.

## Developing it

From the repository root:

```sh
uv run iki plugin validate plugins/plasmid --with plugins/dna
uv run iki plugin test plugins/plasmid --with plugins/dna
cd plugins && uv run iki plugin makemigrations plasmid --with dna   # after changing a manifest
```

Its environment's packages are pinned, with their hashes, in `requirements.txt`,
compiled from `requirements.in` as it says. Pin the same release in the repository's dev
dependencies, where `server/tests/test_plasmid_*.py` run its scripts: `make check-deps`
checks they match, and that each is at least 3 days old.

`server/tests/test_first_party_plugins.py` checks that it's valid, that its migrations
match its manifests, and that it migrates forwards and back.
