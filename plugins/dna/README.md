# dna

Iki's DNA plugin: DNA sequences and primers, and sequencing reads aligned to
them.

- **`iki/dna.DNASequence`** (`DNA-000001`): a plasmid, gene, construct or genome.
  - Its identity is its bases, topology (linear or circular) and
    strandedness (double or single): each sequence is registered once in a
    registry (ADR 0115), with one ID, and registering it again, in any
    project, finds that entry. A circular sequence is found from any of its
    rotations, and a double-stranded one from either strand, as its reverse
    complement ([ADR 0138](../../docs/adr/0138-an-identity-reads-sequences-and-sets-of-parts-as-what-they-are.md)).
    A single-stranded one is found only by its own bases, as its reverse
    complement is another molecule, and the same bases single- and
    double-stranded are two sequences. A project that hadn't got it gets its
    own record of it; one that had finds its record. Its length and GC
    content are the entry's.
  - Each project's record keeps the accession it came from
    ([ADR 0154](../../docs/adr/0154-an-accession-is-the-records-not-the-entrys.md)):
    one molecule is in several databases' records, as pUC19 is GenBank's
    L09137.2 and M77789.2, and each import or fetch keeps the record it read
    as its origin.
  - Its features are kept as GenBank lists them: type, location, strand and
    qualifiers.
  - The plasmid plugin's **Annotate from feature library** action adds
    exact matches to the DNA sequence's **Sequence** tab. Each hit shows its
    location, strand, and source accession. The sequence record keeps the
    library version alongside the hits and retains the imported features.
    [ADR 0206](../../docs/adr/0206-dna-sequence-annotation-records-library-matches-and-provenance.md)
  - Its length and GC content are computed by the plugin's
    `compute_composition` action, which a trigger runs once, when it's
    registered.
- **`iki/dna.Primer`** (`PRM-000001`): an oligonucleotide, up to 200 bases,
  optionally bound to the sequence it targets. Its identity is its bases,
  registered as a sequence's is. Its length, GC content and melting
  temperature are computed by `primer_properties`, which a trigger runs
  when it's registered, and are its entry's.

Both extend `iki/core.Sequence`, so a search for sequences finds them alongside
proteins and other plugins' sequences.

## Settings

- `ncbi_email`: the address NCBI asks automated clients to give, so it can
  contact the lab before blocking heavy use. Fetching from NCBI needs it:
  `fetch_sequences` names it under `requires_settings`, so on a deployment
  that leaves it unset the fetch isn't offered, and a launch is refused before
  it's queued
  ([ADR 0169](../../docs/adr/0169-an-action-names-the-settings-it-cant-run-without.md)).

## Secrets

- `NCBI_API_KEY` (optional, for `fetch_sequences`): an NCBI API key, from an
  NCBI account's settings, raises NCBI's rate limit from 3 to 10 requests a
  second. Set it in the deployment's run secrets as `iki__dna.NCBI_API_KEY` (the plugin's label, `iki__dna`, then the name).

## Actions

They run on backend workers, in the `sequence-tools` environment (Python 3.12
with Biopython, which `align_reads` and `align_pair` use; the others need only Python's standard
library), except `align_multiple`, which runs MAFFT on the container tier, in the
`alignment-tools` environment (at least 1 CPU and 2 GiB; MAFFT uses as many CPUs as
it's given). `align_reads` and `align_pair` list Biopython 1.88 as their software,
and `align_multiple` MAFFT 7.505, so each alignment they make is recorded as made with it
([ADR 0106](../../docs/adr/0106-how-an-entity-was-made-recorded-by-iki-or-stated-by-its-source.md)).

- **`compute_composition`**: a DNA sequence's length and GC content, for
  many at once.
- **`primer_properties`**: a primer's length, GC content and melting
  temperature, for many at once.
- **`import_sequences`**: registers each record of a FASTA or GenBank file
  (up to 10,000, and 1 GiB), or a SnapGene file (`.dna`), as a DNA sequence,
  and a SnapGene file's primers as primers. A sequence already registered
  is found, and the project gets its record of it, or keeps the one it has. Each records the file and record it came from, with the record's
  SHA-256.
- **`fetch_sequences`**: fetches nucleotide records from NCBI by accession
  (up to 500, such as `NM_000546.6`, `NZ_CP009273.1`, or `L09137` for the latest version), in
  GenBank format with their features, and registers each as a DNA sequence.
  Each records the NCBI record and version it came from, its link, when it
  was fetched and its SHA-256. It reaches only `eutils.ncbi.nlm.nih.gov`,
  through the deployment's egress proxy, and needs the `ncbi_email` setting.
- A RefSeq model's record (`XM_`, `XR_`, from either) says in its COMMENT that
  NCBI's annotation pipeline predicted it, and with which gene prediction
  method: its origin says so, as NCBI's word
  ([ADR 0106](../../docs/adr/0106-how-an-entity-was-made-recorded-by-iki-or-stated-by-its-source.md)):
  computed, by gene prediction, with Gnomon and the pipeline at its version.
  Other records don't say how their sequence was made, so theirs say nothing.
- **`align_reads`**: aligns sequencing reads to a DNA sequence, such as Sanger
  reads of a clone to the plasmid it should be, into an `iki/core.Alignment` the
  alignment viewer shows, the sequence its reference
  ([ADR 0088](../../docs/adr/0088-alignments-are-entities-of-sequence-entities.md)).
  It takes up to 96 files, chosen together
  ([ADR 0089](../../docs/adr/0089-a-param-can-take-several-files.md)): AB1
  traces, one read each, named by their file, or FASTQ or FASTA files, named
  by their records, up to 1,000 reads in all. It keeps `alignment.fasta` as an
  artifact, which is also the alignment's file: in a read's row, `-` is a
  deletion and `.` a place it has no observation of, beyond its ends. That
  `.` is Iki's: other tools reading aligned FASTA take it for a base (Biopython
  counts a mismatch), so the run keeps `alignment-standard.fasta` too, with `-`
  for both, as they expect. There a `-` can't say whether a read has a deletion
  or no observation, so each read's header says where it lies on the sequence
  (`; on DNA-000001 101–300`), in both files.
- **`align_pair`**: aligns two DNA sequences, globally as EMBOSS needle does or
  locally as water does, with their defaults: EDNAFULL (NCBI's NUC.4.4: a match
  +5, a mismatch -4, IUPAC's ambiguity codes scored by the bases they share), a
  gap opening penalty of 10 and an extension penalty of 0.5, gaps at the ends
  free in a global alignment. The query is aligned as given and as its reverse
  complement, the better kept (either strand, the default), or only one way. It
  records the score, the length, and the identity, similarity (pairs EDNAFULL
  scores above zero, such as A with R) and gaps as percentages of the length,
  unrounded, as needle reports them: pUC19 against pBR322 is 2,238 of 4,806
  columns identical, scoring 10,916, as needle 6.6.0 finds. It keeps
  `alignment.txt` in EMBOSS's pair format and `alignment.fasta`, which is also
  an `iki/core.Alignment` of the two, the target its reference. A row that's
  part of its sequence is headed `DNA-000002/2072-4353`; one that's the query's
  other strand runs down, `DNA-000001/2686-1`, as Rfam writes a row on the
  reverse strand, and the viewer and the EMBOSS file number its bases down.
  Sequences up to 200 million cells (two of some 14 kb); it asks for 1 GiB.
- **`align_multiple`**: aligns up to 2,000 DNA sequences with MAFFT 7.505 (from
  Debian, without its GPL-licensed extensions, `--nuc`) into an
  `iki/core.Alignment` of them, each row headed by its sequence's HRID, in the
  order given. A reference, if chosen, is the first row. MAFFT's strategy is the
  run's, as the protein plugin's `align_multiple` takes it: Auto (the default)
  lets MAFFT choose, and the method records the one it used. MAFFT adjusts
  sequences' direction (`--adjustdirection`, or `--adjustdirectionaccurately`),
  as Benchling's MAFFT alignments do unless told not to: a sequence whose reverse
  complement fits the first better is aligned as that, and its row is headed
  `DNA-000005/8835-1`. The first sequence, the reference if there is one, is
  never turned round. The run fails if MAFFT changes a row's bases rather than
  only adding gaps (or turning it round, when asked to). It keeps
  `alignment.fasta`, the alignment's file. Sequences up to 200,000 bases, and 10
  million in all. MAFFT's alignment can depend on how many threads it runs on
  (the same with one to three threads, another with four, for the six vectors in
  `server/tests/data/plasmids/`), so the method says how many it was given.
- **`consensus`**: registers an alignment's consensus as a DNA sequence, derived
  from the alignment
  ([ADR 0173](../../docs/adr/0173-dna-is-aligned-as-proteins-are-and-a-consensus-is-a-sequence.md)).
  Each column gives one base, as Geneious calls a consensus. Rows count where
  they have a base or a gap; `.` (no observation) counts as neither. A column
  where more rows have a gap than a base is left out. Otherwise its base is the
  one at least the threshold's share (50%, by default) of its rows with a base
  have, or the IUPAC code of the fewest commonest bases that together do, those
  tied at the edge taken together or not at all: 6 A, 3 G and 1 T is A at 60%,
  R above it to 90%, D above that, the example Geneious's manual gives. A row's
  own ambiguity code is an equal share of each base it stands for (R half an A
  and half a G). Without ambiguity codes, such a column is N. A column no row
  observes is N, or left out at the ends of a linear sequence; a circular one
  keeps them all. The reference counts as a row unless the run leaves it out,
  as it should for reads, so the consensus is what they read; then reads of a
  clone that cover all of it give the clone's sequence, found in the registry
  if it's there. The run says how many bases are ambiguous or unobserved and
  how many columns it left out.

GC content counts G, C and S among the bases whose pairing is known (A, C, G,
T, U, S and W). Other IUPAC codes, such as N, are left out. Melting
temperature uses SantaLucia's unified nearest-neighbour parameters (1998) with
his salt correction, at 50 mM Na⁺, with sodium alone: no Mg²⁺ or dNTPs. Its
strands are 50 nM in all, his CT, the total strand concentration: 25 nM each
of the primer and its complement, so the Tm is at CT/4, or 50 nM of a primer
that pairs with itself, at CT. (Biopython's `Tm_NN` takes each strand's:
`dnac1=25, dnac2=25`, its defaults. At 50 nM of each, a primer's Tm is some
0.8 °C higher.) That isn't Primer3's default buffer (1.5 mM Mg²⁺ and 0.6 mM
dNTPs as well), under which the same primer's Tm is some 5 to 6 °C higher, so
compare it with Tms computed for sodium alone. It's computed only for primers
of A, C, G and T alone.

GenBank records give the name (LOCUS), description (DEFINITION), accession
with its version (VERSION), organism, topology, strandedness and features.
LOCUS's molecule type says the strandedness: `ss-DNA` and RNA records
(`mRNA`, `rRNA` and the like) are single-stranded, `ds-DNA` and `ds-RNA`
double, and plain `DNA` doesn't say, so it's left at the default, double. A
mixed-stranded record (`ms-DNA`, partly single and partly double) fails the
import, saying which: as either, it would be registered as another molecule.
An mRNA is kept as its bases in GenBank's T alphabet, as a cDNA is. Each
feature keeps its type, where it lies, strand, name (its `/label`, `/gene`,
`/locus_tag` or `/product`) and qualifiers. Where it lies is its first and
last base, and, for a location with several parts (`join(…)`) or between two
bases (`5^6`), its parts as INSDC's feature table reads them, each with its
strand: the viewer draws and counts those, not what lies between them, so
`join(2..4,10..12)` is six bases and `5^6` none. A join's parts are kept in
the order they're read (a reverse-strand join's last first); a feature with
parts on both strands is `mixed`; one crosses a circle's origin only when
that's the shorter way round its parts. An `order(…)` (its parts found in
that order, nothing said of joining them) rather than a `join(…)` is kept as
`operator: order`, and partial ends (`<2..>9`: ends beyond the bases they
name, as INSDC writes them) as `partial_start` and `partial_end` on its parts,
so the viewer writes them so, and gives such a feature's length as "at least"
the bases annotated. Such locations keep GenBank's own text in a `location`
qualifier too; a record registered from another origin or the other strand
has its features, their parts and that text placed along its entry's sequence
(ADR 0138), partial ends on the ends they were on and an order's parts never
made one. Features on
other records, or whose location isn't on the sequence, are left out, and
the run says so. The organism is the taxon its source feature names
(`/db_xref="taxon:9606"`), an `iki/core.Organism` the import makes the first
time it meets it
([ADR 0140](../../docs/adr/0140-an-organism-is-a-taxon-by-its-ncbi-taxonomy-id.md)),
its name and taxon read together from that feature. A record with several
source features (stretches from several organisms) is of the one INSDC marks
as its main organism, `/focus` or `/transgenic`, or else of the one that spans
the whole record; the others are kept as features, named by their organism.
When none is, its organism is left out and the run says why. A record without
a taxon has no organism, and the run says so. FASTA records give a name (the header's first word) and a
description (the rest), and an accession where the header has one
(`L09137.2`, NCBI's `gb|L09137.2|`, or a RefSeq one such as
`ref|NZ_CP009273.1|`). A FASTA header can say its record's topology with
NCBI's source modifier, `[topology=circular]` or `[topology=linear]`, as Iki's
own FASTA export writes it; the modifier is taken out of the description.
Otherwise FASTA doesn't say whether a sequence is circular, so the "Topology
of FASTA records" param does: it is `linear` unless it's set to `circular`,
which is the right default for a file of genes, reads or primers, and what a
plasmid in a FASTA file needs changed. A GenBank record's own LOCUS line, and
a SnapGene file's own flag, settle it, whatever the param says, since the
file knows; the run's log says, for each record, which of them its topology
came from. Nothing of a linear record
reads across its ends: a location that runs off the end and back to base 1
(`join(2306..3215,1..1623)`, as INSDC writes a gene across a circular genome's
origin) says the molecule is a circle, so on a record whose LOCUS says `linear`
that feature is left out, with a message saying which and why.

SnapGene files: SnapGene doesn't publish its format, so the import reads it as
Biopython and other readers do, and is tested against files SnapGene itself
wrote (`server/tests/data/snapgene/`). It reads:

- the bases (upper-cased) and topology, from the file's DNA packet;
- the name, from the file's custom map label when SnapGene uses one, or else
  the file's name;
- the description and accession, from the file's notes;
- each feature's name, type, strand, qualifiers and segments, as the GenBank
  import keeps them. A segment that runs past the end of a circle and back is
  two parts across the origin; gaps between segments aren't parts;
- each primer, as an `iki/dna.Primer` bound to the sequence, and each binding
  site SnapGene would show as a `primer_bind` feature named for its primer.

What a DNA sequence can't hold isn't kept, and the run says so for each file.
That covers:

- the host's methylation (Dam, Dcm, EcoKI);
- a feature marked as running both ways (kept with no strand), its segments'
  names and its cleavage sites;
- notes with no field here, such as the file's type, references, and what it
  was transformed into;
- a linear molecule's sticky ends and end modifications;
- an organism, which a SnapGene file names without an NCBI Taxonomy ID;
- SnapGene's history of how it was made, and the sequences aligned to it in
  SnapGene, such as sequencing reads (align those with `align_reads`);
- binding sites below the file's own hybridization settings.

SnapGene's display state (colours, the map's layout, enzyme choices) isn't
kept either, and isn't mentioned. The whole file is kept as the record's
origin, so nothing in it is lost. A file of a protein or RNA (`.prot`,
`.rna`), or one that keeps its bases compressed, fails the import, saying
why.

A record that can't be read (RNA written with U, gaps, letters that aren't
IUPAC bases, a file cut short, or a GenBank record whose bases aren't as many
as its LOCUS line says) fails the whole import, and the run says which record and
why, so nothing is half imported.

Fetching follows NCBI's rules for automated clients: it names itself
(`tool=iki-dna`) with the lab's address, asks for up to 100 records at a time,
and makes at most three requests a second (ten with `NCBI_API_KEY`). When NCBI says to slow down or
fails on its side, it waits and asks again. An accession NCBI has no record
for fails the run, which names it.

Aligning reads: each file's format is read from its first bytes. A read with
qualities (AB1, FASTQ) is trimmed to its best stretch by Mott's algorithm,
as phred does (limit 0.05), unless the run says not to. Each is aligned
locally to the sequence on both strands, and the better kept, with BLASTN's
match and mismatch scores (2 and -3), a gap of n bases costing 5 + 2(n - 1),
and N matching anything at no score. (BLASTN's gap opening 5 and extension 2
would charge 5 + 2n: Iki's opening cost is the first base's, as EMBOSS and
Biopython count it.) A circular sequence is aligned as if it went round
again, so a read across its origin aligns in one piece. A read aligns if at
least half its trimmed bases do (and 20); others are left out, and the run
says so, as is one with nothing scoring above zero (all N, say): a warning
for each reason, naming the reads, and each read, with why, in the run's
artifact `left_out.tsv`. Two more reads are left out there. One that lies
equally well on another stretch of the sequence, or as well on both strands,
says nothing of the place it was put on — bowtie2 gives such a read a mapping
quality of 0 — so neither its coverage nor a difference in it is claimed for
one of them. And one that reads round a circular sequence more than once
passes a place twice, which the alignment's one base per place can't hold:
align it in parts. The run says
where the reads cover the sequence: a read's base covers its place only when
it says what's there, so N, or another ambiguity code that could be the
sequence's base, covers nothing, and the run lists where reads have only such
bases. Bases compare as the sets their IUPAC codes stand for: a read's Y where
the sequence has R is a difference, and where the sequence is itself
ambiguous, the base a read has there is listed apart (`R31A`). It lists each
difference from the sequence, as HGVS's DNA recommendations name them with the
sequence as their reference, numbered from its first base: `o.` for a
circular sequence (a plasmid), `g.` for a linear one. They are substitutions
(`g.1234C>T`), deletions (`g.1240del`, `g.1240_1242del`), insertions
(`g.1250_1251insG`), duplications (`g.1250dup`), inversions, where more than
one base is replaced by its own reverse complement (`g.3_6inv`), and, for
other changes side by side, deletion-insertions (`g.1260_1261delinsTA`), with
the reads that show each and their least quality there. A deletion or
insertion in repeated bases is named at its 3′-most place; a circular
sequence's origin is no end, so one may move on round it, and one across it is
named from the last place to the first (`o.2686_1insA`). Where that rule would
carry a deletion or an insertion onto a base another change names — no
sequence has both a base deleted and that base changed — the two are one
change, named from the whole stretch that differs (`o.223_224delinsC`).

`o.` and `g.` are HGVS's two prefixes for a genomic reference sequence, which
is what a DNA sequence here is: its identity is its bases, topology and
strandedness, and says nothing of what the molecule is in a cell. HGVS's other
prefixes need what this reference doesn't carry — `c.` and `n.` number along a
transcript, from its coding start or its first base, and `m.` says a reference
is mitochondrial DNA — so a mitochondrial genome's differences here are `o.`
like any other circle's, and a variant to be sent to a variant database is
renamed against the transcript or chromosome it's called on, which is a
different reference from this one.

In the alignment, each read's row is as it lies on the
sequence (its reverse complement if it read the other strand), and bases a
read has that the sequence doesn't get columns of their own.

Where a primer binds a molecule, and what a pair of primers amplifies, are the
plasmid plugin's: it holds the annotation and the maps those are read on
([ADR 0172](../../docs/adr/0172-what-the-bases-say-orfs-primer-sites-and-the-product-of-a-pcr.md)).
A PCR product it works out is registered here, as the linear `DNASequence` it
is. Reverse complements, restriction digests and primer design come later.

## Exports

A DNA sequence downloads as GenBank (`.gb`) or FASTA from the Export button on
its page, at its latest revision or, from its history, at an earlier one
([ADR 0171](../../docs/adr/0171-a-types-records-export-as-files-iki-writes-from-its-fields.md)).
Iki writes the file from the record's values at that revision, for anyone who
can read it, and each download is in the audit log with the file's SHA-256.

- **GenBank** gives the name, length, molecule (`DNA`, or `ss-DNA` for a
  single strand), topology and the revision's date in its LOCUS line, then:
  - the description;
  - the accession, when the record has one;
  - the organism and its NCBI Taxonomy ID;
  - in COMMENT, the record's Iki ID and revision;
  - each feature with its parts, partial ends, `order()` and qualifiers. A
    feature across a circle's origin is a `join(…)` across it, and a name no
    qualifier gives is its `/label`.

  What GenBank can't hold is left out and listed in COMMENT: a feature across
  a linear molecule's ends, or one beyond the sequence. A feature with no
  strand reads as forward in GenBank, and COMMENT says so.

- **FASTA** gives the ID, name, description and topology in its header
  (`>DNA-000001 pUC19 Cloning vector pUC19 [topology=circular]`), and the
  bases in lines of 70. It holds no features.

Importing either back gives the same sequence, and GenBank its features too.
Iki reads SnapGene files but doesn't write them, since SnapGene doesn't publish
its format; SnapGene opens the GenBank file with its features (ADR 0171).

## Developing it

From the repository root:

```sh
uv run iki plugin validate plugins/dna
cd plugins && uv run iki plugin makemigrations dna   # after changing a manifest
```

Its environment's packages are pinned, with their hashes, in
`requirements.txt`, compiled from `requirements.in` as it says. Pin the same
releases in the repository's dev dependencies, where
`server/tests/test_dna_reads.py` runs `align_reads`,
`server/tests/test_dna_alignment.py` `align_pair` and `align_multiple`, and
`server/tests/test_consensus.py` `consensus` (whose script the protein plugin
ships too, the same file, as that test checks): `make check-deps` checks they
match, and that each is at least 3 days old.

MAFFT's image is built from `images/alignment-tools` (CI builds it and runs its
`check.py`, which aligns through `scripts/multiple.py`), and published, signed,
as `iki-dna-alignment-tools` from `main`. `environments/alignment-tools.yaml`
pins it by the digest the publish workflow's summary gives; changing the image
is a commit to its Dockerfile, then a new pin there and in `deploy/dev/sso.py`.

`server/tests/test_first_party_plugins.py` checks that it's valid, that its
migrations match its manifests, and that it migrates forwards and back.
