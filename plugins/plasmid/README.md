# plasmid

Iki's plasmid plugin: plasmids and vectors by the sequence that is each one, circular or
linear, the cloning plans that make them, and the common vector parts a plasmid is
annotated from.

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
  - **Its topology is its registry entry's too, and part of what it is**
    ([ADR 0168](../../docs/adr/0168-a-plasmids-topology-is-its-entrys-and-a-linear-molecules-ends-are-ends.md)):
    `circular` or `linear`, derived from its sequence, the same for every project, set
    once and written only by an action, with a rule that keeps it equal to its
    sequence's at every save. The same bases closed into a circle and left with two ends
    are two sequence entries, so they are two plasmids with two IDs — nobody states a
    plasmid's topology against its molecule, and nobody edits it either.
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
    plasmid's sequence is double-stranded and its topology agrees with the sequence;
    one under a material transfer agreement names the
    agreement; one at BSL-2 or above names its biosafety approval.
  - A plasmid here is **circular or linear**. Most are circles; linear plasmids are real
    — _Kluyveromyces lactis_ carries pGKL1 and pGKL2 (INSDC X01095.1 and X01104.1),
    _Borrelia burgdorferi_ a set of them, _Streptomyces coelicolor_ SCP1 — and GenBank
    records them, their LOCUS line reading `linear`, as Benchling and SnapGene both do.
    A linear molecule's two ends are ends: its map is a line, nothing is annotated across
    them, and a cloning plan for it doesn't close.
  - **What its own bases say, and where the lab's primers sit on it, are each project's
    record's too**
    ([ADR 0172](../../docs/adr/0172-what-the-bases-say-orfs-primer-sites-and-the-product-of-a-pcr.md)):
    its `orfs`, in all six frames, with the frame, the coordinates, the length and the
    protein of each, and its `primer_binding_sites`, with the primer, the strand, the base
    each primer's 3' end pairs with, how much annealed, the 5' tail, every mismatch and the
    site's melting temperature. Each follows from the molecule **and** what a run asked
    for — the fewest codons, the translation table, the start codons, the 3' match, the
    mismatch budget, the primers the lab holds — so each keeps that beside what it found,
    in `orfs_found_with` and `primer_binding_sites_found_with`, as the annotation keeps
    `annotated_with`.
  - Its **Map** tab draws the molecule it is. A circle is a ring with base 1 at the top
    going clockwise, each annotated part an arc pointing the way its strand runs, and it
    can be shown as a line as well, cut at base 1 — the view to read beside a gel or a
    sequencing read — which is how SnapGene and Benchling offer the two. A linear plasmid
    opens on the line, with an end drawn at each end, and is offered no ring, since it
    has no origin to go round. The two views are buttons that say which is shown,
    reachable and pressable from the keyboard; both are described in words and every part
    is in the table beneath, so nothing is told by colour or by the drawing alone. Its
    bases, with the features its GenBank record gave, are one click away on its sequence,
    in Iki's own sequence viewer.
  - It **downloads as GenBank or FASTA** from the Export button on its page, at its
    latest revision or, from its history, an earlier one
    ([ADR 0171](../../docs/adr/0171-a-types-records-export-as-files-iki-writes-from-its-fields.md)).
    The file is its sequence's — the bases, topology and the features the sequence's
    own record has — under the plasmid's name and description, with the parts its
    annotation found after them; a part found where the sequence's file already has
    that feature is written once. An earlier revision exports with its sequence as it
    was then. SnapGene opens the GenBank file, and the DNA plugin imports it back.
  - Three **layers** lie along it, each what a run of the plugin found and each a layer the
    person turns off: the library's parts, the open reading frames (on rings inside the
    backbone, and on lanes of their own in the linear view) and the primer binding sites
    (on a ring outside it). Each has a table of its own beneath, holding what a drawing
    can't say — an ORF's frame, length in bases and codons, start codon and protein, and a
    site's 3' end, annealed length, 5' tail, mismatches with how far each is from the 3'
    end, and Tm — and each layer's button says whether it is showing and how many it
    holds. A binding site's row names its primer, and names it as a way to that primer's
    record where Iki gave the map the project's primers
    ([ADR 0117](../../docs/adr/0117-components-show-many-entities-in-more-places.md));
    the map never navigates itself. A layer whose run has been and found nothing says so,
    which isn't what a layer whose run hasn't been says.
- **`iki/plasmid.Assembly`** (`ASM-000001`): a cloning plan — a `backbone`, the
  `inserts` going into it, the `method` and the `enzymes` it cuts with. It has no
  identity: one molecule can be built several ways, and how a lab built its own copy is
  the lab's record, not a fact about the molecule. Its `product` is the
  `iki/dna.DNASequence` the clone turned out to have, stated once it's sequenced, while
  what the plan makes is `derived_from` it, so a plan, the sequence it makes
  and the plasmid that is it are one chain in the graph
  ([ADR 0123](../../docs/adr/0123-queries-walk-the-graph-of-what-depends-on-what.md)).
  - **What it makes is a circle or a linear molecule, and the plan doesn't say which**:
    its `simulated_topology` follows from the method and the parts, computed by the run
    and never stated
    ([ADR 0168](../../docs/adr/0168-a-plasmids-topology-is-its-entrys-and-a-linear-molecules-ends-are-ends.md)).
    Parts that close a circle make that circle, which is what a ligase or an assembly mix
    leaves to transform; parts that only join end to end make the linear molecule they
    make, as a Golden Gate or Gibson construct whose outer ends pair with nothing does,
    or a cut vector with no insert; and the `digest` method cuts one molecule and keeps a
    band, which is linear by definition — a vector linearised by one cut, or a fragment
    cut out of a plasmid. A digest takes no inserts, which a rule keeps.
  - Its `status` goes Planned → Checked → Built → Verified, each state set by the lab
    roles that may, with guards: it's Checked only once the plan matches its product,
    Built only from Checked, and Verified only from Built by someone other than whoever
    built it or edited its record since it entered Built
    ([ADR 0128](../../docs/adr/0128-guards-say-when-a-state-may-be-set.md)). Verified
    locks it. It can be Abandoned from anywhere, with a reason.
  - Whoever recorded a plan hears when a check finds it doesn't make its product, and
    when a reviewer verifies the construct
    ([ADR 0129](../../docs/adr/0129-types-declare-their-notifications.md)).

- **`iki/plasmid.Pcr`** (`PCR-000001`): a PCR — a `template`, a `forward_primer` and a
  `reverse_primer`, and the linear molecule they amplify. Like an assembly it has no
  identity: one fragment can be amplified by many pairs of primers, and how a lab made its
  own copy is the lab's record. Its product is the DNA sequence `simulate_pcr` works out
  and registers, `derived_from` the reaction, so the product, the PCR, its template and
  its primers are one chain in the graph
  ([ADR 0123](../../docs/adr/0123-queries-walk-the-graph-of-what-depends-on-what.md)) and a
  cloning plan takes the product as an insert by its sequence. As with an assembly, the
  product is the reaction's in the graph rather than a field of it, so a reaction worked
  out again, after its primers were corrected, gives what it now says, and the same bases
  are the entry the registry has already. It keeps the
  `product_length`, each primer's melting temperature where it binds
  (`forward_melting_temperature`, `reverse_melting_temperature`), the `tm_method` those
  were computed under, and `products_found`, which is more than one where the primers don't
  say which molecule they mean.

## Find a vector's history and verification

When a project knows which build supplied its material, set `source_assembly`
to the project's assembly. The assembly's stated product must be the plasmid's
registered sequence. Open that assembly for the backbone, inserts and cloning
method. Leave the field empty for an externally sourced vector with unknown
build history. The **Graph** also shows the provenance of registered products.
Several assemblies can produce the same sequence. The plasmid record holds
the hosts (including bacterial,
mammalian, yeast and viral), copy number, biosafety level, annotated selection
markers, replication origins and promoters. Its project record identifies the
source and MTA.

For the material in a particular tube, open its `iki/lots.PlasmidPrep` and
the **Verification** tab. Each verification links the evidence and outcome
to that physical prep and to a revision of the project's plasmid record.
Opening a recorded result shows the reviewer and its sign-off history.
[Prep verification in the lots plugin](../lots/README.md#verify-a-plasmid-prep)

## Lab roles

A project's Admins give these to its members, beside their project role:

- `cloner` (Cloning scientist): plans a construct, builds it, and records the sequence
  of what was built.
- `construct_reviewer` (Construct reviewer): verifies that a construct's sequence is
  what was planned — someone other than whoever built it.

## Actions

They run on backend workers, in the `plasmid-tools` environment (Python 3.12 with
Biopython, which the annotation's alignments and each restriction enzyme's description
use, and Wasmtime, which runs the packaged molecular module). Each action lists the
software its runs are made with, so that is recorded against what they make
([ADR 0106](../../docs/adr/0106-how-an-entity-was-made-recorded-by-iki-or-stated-by-its-source.md)).

Placing primers, measuring a duplex and cutting a molecule are one implementation,
written in Rust in `worker/crates/iki-wasm` and carried in this plugin's archive as
`data/plasmid-tools.wasm`, so a site, a temperature or a fragment is the same whether a
run, the server or the web app works it out
([ADR 0200](../../docs/adr/0200-one-implementation-places-primers-and-cuts-dna.md)). That
module is never committed: git holds the Rust and the digest beside it,
`data/plasmid-tools.wasm.sha256`, and `scripts/build-plasmid-tools-wasm.sh` builds the
bytes — as iki-spec's build and the server image do. Run it once in a fresh checkout, or
the actions have no module to call. The digest is of the Linux x86-64 build, which is
what an image and a deployment run; on another platform the script builds the module for
the tests here and says it isn't comparing, because rustc's precompiled wasm32 standard
library differs between host platforms. `--update` builds through the image's wasm stage
in Docker, so the digest it writes is the Linux x86-64 one wherever it is run. The
module holds no enzyme data: each request carries the enzymes it names, read here from
Biopython's REBASE-derived table, because REBASE's terms are not a licence Iki can
redistribute that table under. The Python these replaced is kept as the oracle they are
cross-checked against, enzyme by enzyme and over random molecules
(`server/tests/test_plasmid_parity.py`).

- **`register_plasmid`**: registers a plasmid from its bases — the double-stranded DNA
  sequence and the plasmid that is it. Its `topology` param is `circular` unless it's set
  to `linear`, which is the default GenBank's LOCUS line and SnapGene both take, and what
  a linear plasmid needs changed. The same molecule given again, from another origin or
  on the other strand, finds the entries it has already, and the project gets its records
  of them. An Addgene number given with it sets `source` to Addgene.
- **`annotate_plasmid`**: finds the library's parts along a plasmid's sequence and
  records what they say about it. A trigger runs it when a project gets a plasmid — on
  each project's own record, not once per entry, since the annotation is the record's.
  Its `length` and its `topology` are its entry's, so a run fills each only where it's
  still empty.
- **`assemble`**: works out the molecule a plan makes and registers it — the DNA
  sequence, and the plasmid that is it where the plan closes a circle, both
  `derived_from` the plan — so a map and primers can be had before anything is cloned,
  and records that molecule's length, topology and junctions on the plan. A linear
  product is registered as the sequence alone: a cut vector or an assembled fragment is
  on its way to something else, and a linear plasmid is registered as a plasmid by
  whoever knows it is one. It leaves the plan's `product` alone: that's the sequence the clone turned out
  to have, which whoever sequenced it states, and `check_assembly` then compares the two.
- **`check_assembly`**: works out what a plan makes and checks it against the sequence
  the clone turned out to have. A trigger runs it as soon as someone states the product,
  so whoever sequenced the clone hears at once whether the plan explains it.
- **`virtual_digest`**: cuts each of a plan's parts with its enzymes and lists every
  fragment in the plan's `fragments`, largest first, with where it lies and the enzyme
  and overhang at each end, as SnapGene's and Benchling's virtual digests do. It
  registers nothing, so a digest that leaves several bands can be seen before one is
  chosen.
- **`find_orfs`**: finds a plasmid's open reading frames in all six frames, by the
  translation table and the fewest codons the run asks for, with the table's alternative
  start codons where it allows them.
- **`find_primer_binding_sites`**: finds where the primers chosen bind a plasmid's
  sequence, on both strands, exactly or with the mismatches the run allows, each with its
  melting temperature.
- **`simulate_pcr`**: works out what a PCR amplifies from its template and its two primers
  and registers the product as a linear DNA sequence derived from the reaction. A pair that
  doesn't bind, or binds the wrong way round, amplifies nothing and the run says which
  primer and what to do; a pair that amplifies several molecules registers none and lists
  them.

## Annotating from the library

`data/features.json` holds 34 common vector parts: origins of replication (pMB1/pUC, f1,
SV40, ARSH4), the yeast centromere CEN6, bacterial resistance genes (AmpR, KanR, CmR,
TetR), markers for the
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
every 18-mer of the part looked up, which places it; then round each placement the part is
aligned locally to the plasmid (Biopython's `PairwiseAligner`, with BLASTN's scores —
match 2, mismatch −3, a gap of n bases 7 + 2(n − 1), which is BLASTN's own existence 5
plus 2 per base), and the alignment's **identity** is the part's bases that match over the
part's length. A part is annotated where at least 80% of it aligned with at least
`min_identity` identity (0.9 by default); a part under 40 bases long is annotated only
where it matches exactly, since at that length a few mismatches say nothing.

**What the seed can and can't promise.** Like BLAST's word hit, a seed is an exact
stretch: a part is placed by the identity threshold only where it shares a whole exact
18 bases with the plasmid, so a long part whose mismatches fall closer together than
that — more than one in every 18 bases — is missed however well it would align. Where a
part has no seed at all, because it is shorter than one or because every 18-mer of it
carries a mismatch, it is looked for as a plain exact match instead. That is what the
library's shortest parts need: cPPT is 16 bases and the T3 promoter 17.

Vector parts drift between vectors — pUC19's β-lactamase differs from pBR322's at two
bases — so each hit keeps the identity it had, which the map and the features show. Where
two hits of the **same kind of part** lie almost wholly on the same bases, only the better
match is kept: two parts of a family are alike enough to find each other's places
(Gateway's attR1 and attR2 are 95% identical), and only one of them is there. A part
nested inside a part of another kind means something of its own and stays, as the SV40
origin within the SV40 early promoter does, or a T7 promoter within a lac operator.

**A part that is a gene says what the plasmid does only where its frame is whole.** A
resistance or selection gene is read in the plasmid's own bases: a whole number of
codons, from ATG to a stop, with no stop inside. A `ura3` nonsense allele is all but
identical to URA3 and selects nothing, so such a hit is annotated, with
`reading_frame: interrupted` among its qualifiers, and the plasmid's
`selection_markers` leave it out. A centromere is likewise not an origin of replication:
CEN6 segregates a pRS41x vector and ARSH4 is what it replicates from (Sikorski & Hieter
1989), and INSDC keeps `centromere` and `rep_origin` apart, so only ARSH4 is one of the
plasmid's `replication_origins`.

A plasmid's `annotated_with` says which library found its parts and what identity it
asked for, so an annotation can be read years later: `iki/plasmid features 1.0 (34
parts), at least 90% identity`.

## Reading the bases: ORFs, primer binding sites and PCR

See
[ADR 0172](../../docs/adr/0172-what-the-bases-say-orfs-primer-sites-and-the-product-of-a-pcr.md)
for why each of these is the record's rather than the registry entry's, and what is left
out.

- **Open reading frames.** One ORF for each stop codon, from the first start codon after
  the stop before it in the same frame, so a frame's longest reading is the one reported
  and a start inside a gene isn't a second ORF — as NCBI ORFfinder and EMBOSS `getorf`
  report them. At least 75 codons by default, which is SnapGene's and ORFfinder's shortest
  offered protein: shorter frames occur in any stretch of DNA by chance. The genetic code
  is one of NCBI's translation tables by name — the standard code (1) and the bacterial,
  archaeal and plant-plastid code (11), with the vertebrate, yeast, mould/protozoan and
  invertebrate mitochondrial codes (2, 3, 4, 5) and the alternative yeast nuclear code
  (12) — and the table decides what stops as well as what codes. Tables 27, 28 and 31,
  whose stop codons are read as sense codons in some places, are refused rather than
  approximated. ATG starts an ORF unless the run allows the table's other start codons
  (table 11's TTG, CTG, ATT, ATC, ATA and GTG), and a start codon is translated as
  methionine whatever it is. A circle's ORF may read across its origin, and is reported
  from its highest base round to its lowest; nothing is read across a linear molecule's
  two ends, so a frame running off an end is no ORF.
- **Primer binding sites.** A primer is placed by its 15 3'-most bases (or all of it, if
  it is shorter), on either strand, without gaps, since the 3' end is what a polymerase
  extends. A site is kept where its mismatches weigh at most what the run allows: 3 for a
  mismatch among the primer's last five bases and 1 for one further off, against a budget
  of 2 by default. So an exact site and one with mismatches well away from the 3' end are
  found, a mismatch at the 3' end is no site, and a run that raises the budget to 3 is
  told where such a site is. That weighting is what PCR does — a mismatch at or near the
  3' terminus cuts extension sharply, the more the nearer the end (Kwok et al., _Nucleic
  Acids Res_ 18:999, 1990; Stadhouders et al., _J Mol Diagn_ 12:109, 2010) — and it is how
  SnapGene places a primer. Beyond that stretch the primer pairs on toward its 5' end while
  its bases match; the rest is its 5' tail, which a product keeps. A site may cross a
  circle's origin and never a linear molecule's ends.
- **Melting temperatures** are SantaLucia's unified nearest-neighbour model (_PNAS_
  95:1460, 1998), with Allawi & SantaLucia's Watson–Crick stacks (1997) and internal
  mismatches and SantaLucia & Peyret's terminal ones, and SantaLucia's monovalent salt
  correction (his equation 7), at 50 mM Na⁺ with no Mg²⁺ or dNTPs and 25 nM of each
  strand — the conditions `iki/dna` gives a primer's own Tm under, so a primer's record
  and its exact site on a template agree. They are computed by the molecular module, and
  checked against Biopython's `MeltingTemp.Tm_NN` with `DNA_NN3`, `DNA_IMM1`, `DNA_TMM1`
  and `saltcorr=5`, which is where the same numbers used to come from. A site's Tm is of
  its annealed bases against the template's own, so a mismatch lowers it; where no
  published parameter covers a pair of neighbours, no Tm is given. This isn't Primer3's default buffer (1.5 mM Mg²⁺ and 0.6 mM
  dNTPs), under which the same primer's Tm is some 5 to 6 °C higher, so compare a number
  here with one computed the same way; `tm_method` on a PCR says which that is.
- **PCR.** The product is the forward primer whole, the template between the two primers'
  3' ends, and the reverse primer's reverse complement whole: both 5' tails are in it, and
  so is every base a primer mismatched, since there the product is copied from the primer.
  A circular template is read round its origin, so a product may cross it; a linear one is
  read between its ends, so only a pair with the forward site to the left amplifies
  anything. The product is linear and double-stranded whatever the template was. A primer
  that binds nowhere, or only the strand the other should be on, is named with what to do;
  a pair that amplifies several molecules registers none and lists their lengths, as an
  ambiguous assembly does.
- Nothing here designs primers, picks a pair, or checks one for hairpins or dimers; a run
  keeps at most 5,000 ORFs, 500 binding sites and 20 products, and says to ask for longer
  ORFs or a stricter 3' match rather than keeping some and not others. A primer under 8
  bases isn't placed at all, and the run says why.

## Working out what a plan makes

- **Restriction–ligation and Golden Gate.** Each part is cut with every enzyme the plan
  names, each described from Biopython's REBASE-derived table, so any of REBASE's enzymes
  whose cut REBASE states can be named — 754 of them — and each one's own cut positions
  and overhang are used: a 5′ overhang (EcoRI), a
  3′ one (PstI), a blunt cut (SmaI), or a Type IIS enzyme cutting outside its site
  (BsaI, BsmBI, BbsI). A circular part is cut round its origin. Then every circle one
  fragment from each part makes is tried, in both orientations, and the ones whose ends
  all pair are the plan's products. Two ends pair when their overhangs are the same
  kind, the same length and the same bases — which is how Golden Gate's four-base
  overhangs direct an assembly, and why a blunt plan has many products. A 5′ overhang's
  bases are the ones after the cut in the top strand and a 3′ overhang's the ones before
  it, as REBASE writes them (EcoRI `G^AATT_C`, PstI `C_TGCA^G`), so PstI's TGCA and
  KpnI's GTAC don't pair although both are four bases of 3′ overhang. A fragment turned
  round offers the strand that was its other one, which is why an insert can go in
  either way and keep a whole site at each junction.
  - **A cut is one break in the strands, not one enzyme's work**: isoschizomers that
    recognise the same site and break the same bonds (SacI and SstI, BsaI and Eco31I)
    give the fragments either one gives alone, with both names kept against the
    boundary. Two enzymes that cut the same bond but leave different overhangs don't
    describe one molecule, and the run says so rather than choosing.
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
  that makes **several** says how many and how long each is, and `assemble` makes none,
  since the plan doesn't say which. The **check** does settle such a plan: the clone's
  sequence says which of the circles was made, so the product being one of them is a
  match, with a warning that the plan on its own didn't predict it. A product that is
  none of them doesn't match, and then no length or junctions are claimed either.
- The check compares what the plan makes with the product's sequence **as molecules**:
  the same bases from any origin and on either strand are the same molecule (ADR 0138),
  found by the least rotation of each strand (Booth's algorithm, in linear time). So a
  clone sequenced from another origin, or on the other strand, matches its plan. A
  molecule is its bases, its topology and its strand count, so a linear or
  single-stranded product isn't what a plan closing a duplex circle makes, however its
  bases read.
- **A result stands for the plan it was found from.** A plan's method, enzymes, backbone
  or inserts changing checks it again, and a check that can't reach a result clears the
  old one rather than leaving it to be read as current. Built and Verified each need the
  plan to match at that moment, not only when it was first checked — Verified locks the
  record, so what it attests has to hold then.

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

`server/tests/test_plasmid_orfs.py` checks the ORFs against the records' own coding
sequences: pBR322's (J01749.1) feature table gives tet at 86..1276 and bla at
complement(3293..4153), and both come out there with the proteins its `/translation`
qualifiers give; its Rop gene starts at a GTG, so reading it from its own start needs the
table's alternative start codons. All four CDSs of pGKL1's feature table (X01095.1, a
linear plasmid) are found at the record's coordinates. pUC19 has no CDS in its 1986 record,
so its two long ORFs are checked against what the record's COMMENT and the publication
give: β-lactamase, 286 codons at 1,626..2,486 on the reverse strand, translating
MSIQHFRVALIPFFAAFCLPVFA — TEM-1's signal peptide — and the lacZα fragment, 107 codons from
pUC19's own ATG. Written from base 2,001, that β-lactamase gene lies across the join: one
ORF of the circle, the same protein, and no ORF of the same bases read as a line.

`server/tests/test_plasmid_pcr.py` uses NEB's M13/pUC sequencing primers (#S1201S,
GTAAAACGACGGCCAGT, and #S1233S, CAGGAAACAGCTATGAC) on pUC19: each binds once, at the bases
a plain search of the record puts it at, with the Tm Biopython's own `Tm_NN` gives under
the stated conditions (49.2 °C and 43.6 °C), and the pair amplifies 103 bp of the
polylinker whose bases are the record's own. The same primers with ten-base tails give a
product 20 bp longer that begins and ends with them; pUC19 written from inside the
polylinker gives the very same product across the origin; the forward primer with its last
base changed binds nowhere, and with a base changed 13 from its 3' end still binds, with a
lower Tm.

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
