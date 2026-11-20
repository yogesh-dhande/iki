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
  contact the lab before blocking heavy use. Fetching from NCBI needs it.

## Secrets

- `NCBI_API_KEY` (optional, for `fetch_sequences`): an NCBI API key, from an
  NCBI account's settings, raises NCBI's rate limit from 3 to 10 requests a
  second. Set it in the deployment's run secrets as `iki__dna.NCBI_API_KEY` (the plugin's label, `iki__dna`, then the name).

## Actions

They run on backend workers, in the `sequence-tools` environment (Python 3.12
with Biopython, which `align_reads` uses; the others need only Python's standard
library). `align_reads` lists Biopython 1.88 as its software, so each alignment it
makes is recorded as made with it
([ADR 0106](../../docs/adr/0106-how-an-entity-was-made-recorded-by-iki-or-stated-by-its-source.md)).

- **`compute_composition`**: a DNA sequence's length and GC content, for
  many at once.
- **`primer_properties`**: a primer's length, GC content and melting
  temperature, for many at once.
- **`import_sequences`**: registers each record of a FASTA or GenBank file
  (up to 10,000, and 1 GiB) as a DNA sequence. A sequence already registered
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
`ref|NZ_CP009273.1|`). FASTA doesn't say whether a sequence is circular, so
the `topology` param does.

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
change, named from the whole stretch that differs (`o.223_224delinsC`). HGVS names a mitochondrial genome's with `m.`, which
Iki can't tell from another circular sequence's: such a sequence's are `o.`. In the alignment, each read's row is as it lies on the
sequence (its reverse complement if it read the other strand), and bases a
read has that the sequence doesn't get columns of their own.

Reverse complements, restriction digests, primer design and GenBank export
come later.

## Developing it

From the repository root:

```sh
uv run iki plugin validate plugins/dna
cd plugins && uv run iki plugin makemigrations dna   # after changing a manifest
```

Its environment's packages are pinned, with their hashes, in
`requirements.txt`, compiled from `requirements.in` as it says. Pin the same
releases in the repository's dev dependencies, where
`server/tests/test_dna_reads.py` runs `align_reads`: `make check-deps` checks
they match, and that each is at least 3 days old.

`server/tests/test_first_party_plugins.py` checks that it's valid, that its
migrations match its manifests, and that it migrates forwards and back.
