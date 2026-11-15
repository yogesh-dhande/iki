# dna

Iki's DNA plugin: DNA sequences and primers, and sequencing reads aligned to
them.

- **`dna.DNASequence`** (`DNA-000001`): a plasmid, gene, construct or genome.
  - Its identity is its bases and topology (linear or circular), so
    registering the same sequence again reuses it.
  - Its features are kept as GenBank lists them: type, location, strand and
    qualifiers.
  - Its length and GC content are computed by the plugin's actions.
- **`dna.Primer`** (`PRM-000001`): an oligonucleotide, up to 200 bases,
  optionally bound to the sequence it targets. Its identity is its bases. Its
  length, GC content and melting temperature are computed.

Both extend `core.Sequence`, so a search for sequences finds them alongside
proteins and other plugins' sequences.

## Settings

- `ncbi_email`: the address NCBI asks automated clients to give, so it can
  contact the lab before blocking heavy use. Fetching from NCBI needs it.

## Secrets

- `NCBI_API_KEY` (optional, for `fetch_sequences`): an NCBI API key, from an
  NCBI account's settings, raises NCBI's rate limit from 3 to 10 requests a
  second. Set it in the deployment's run secrets as `dna.NCBI_API_KEY`.

## Actions

They run on backend workers, in the `sequence-tools` environment (Python 3.12
with Biopython, which `align_reads` uses; the others need only Python's standard
library).

- **`compute_composition`**: a DNA sequence's length and GC content, for
  many at once.
- **`primer_properties`**: a primer's length, GC content and melting
  temperature, for many at once.
- **`import_sequences`**: registers each record of a FASTA or GenBank file
  (up to 10,000, and 1 GiB) as a DNA sequence. A sequence already in Iki is
  reused. Each records the file and record it came from, with the record's
  SHA-256.
- **`fetch_sequences`**: fetches nucleotide records from NCBI by accession
  (up to 500, such as `NM_000546.6`, or `L09137` for the latest version), in
  GenBank format with their features, and registers each as a DNA sequence.
  Each records the NCBI record and version it came from, its link, when it
  was fetched and its SHA-256. It reaches only `eutils.ncbi.nlm.nih.gov`,
  through the deployment's egress proxy, and needs the `ncbi_email` setting.
- **`align_reads`**: aligns sequencing reads to a DNA sequence, such as Sanger
  reads of a clone to the plasmid it should be, into a `core.Alignment` the
  alignment viewer shows, the sequence its reference
  ([ADR 0088](../../docs/adr/0088-alignments-are-entities-of-sequence-entities.md)).
  It takes up to 96 files, chosen together
  ([ADR 0089](../../docs/adr/0089-a-param-can-take-several-files.md)): AB1
  traces, one read each, named by their file, or FASTQ or FASTA files, named
  by their records, up to 1,000 reads in all. It keeps `alignment.fasta` as an
  artifact, which is also the alignment's file.

GC content counts G, C and S among the bases whose pairing is known (A, C, G,
T, U, S and W). Other IUPAC codes, such as N, are left out. Melting
temperature uses SantaLucia's unified nearest-neighbour parameters (1998) with
his salt correction, at 50 mM Na⁺ and 50 nM primer, as Primer3 does by
default. It's computed only for primers of A, C, G and T alone.

GenBank records give the name (LOCUS), description (DEFINITION), accession
with its version (VERSION), organism, topology and features. Each feature
keeps its type, first and last base, strand, name (its `/label`, `/gene`,
`/locus_tag` or `/product`) and qualifiers. A location with several parts
(`join(…)`), fuzzy ends (`<1`, `>99`) or between two bases (`5^6`) keeps
GenBank's own text in a `location` qualifier too. Features on other
records are left out, and the run says so. FASTA records give a name (the
header's first word) and a description (the rest), and an accession where
the header has one (`L09137.2`, or NCBI's `gb|L09137.2|`). FASTA doesn't say
whether a sequence is circular, so the `topology` param does.

A record that can't be read (RNA, gaps, letters that aren't IUPAC bases, a
file cut short) fails the whole import, and the run says which record and
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
scores (match 2, mismatch -3, gap opening 5, extension 2) and N matching
anything at no score. A circular sequence is aligned as if it went round
again, so a read across its origin aligns in one piece. A read aligns if at
least half its trimmed bases do (and 20); others are left out, and the run
says so. The run says where the reads cover the sequence, and lists each
difference from it in its numbering, as HGVS names them: substitutions
(`C1234T`), deletions (`1240delA`, `1240_1242del`), insertions
(`1250_1251insG`) and duplications (`1250dupG`), a deletion or insertion in
repeated bases named at its 3′-most place, with the reads that show it and
their quality there. In the alignment, each read's row is as it lies on the
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
