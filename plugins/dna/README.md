# dna

Iki's DNA plugin: DNA sequences and primers.

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

## Actions

They run on backend workers, in the `sequence-tools` environment (Python's
standard library only).

- **`compute_composition`**: a DNA sequence's length and GC content, for
  many at once.
- **`primer_properties`**: a primer's length, GC content and melting
  temperature, for many at once.
- **`import_sequences`**: registers each record of a FASTA or GenBank file
  (up to 10,000, and 1 GiB) as a DNA sequence. A sequence already in Iki is
  reused. Each records the file and record it came from, with the record's
  SHA-256.

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

Fetching from NCBI, reverse complements, restriction digests, primer design
and GenBank export come later.

## Developing it

From the repository root:

```sh
uv run iki plugin validate plugins/dna
cd plugins && uv run iki plugin makemigrations dna   # after changing a manifest
```

`server/tests/test_first_party_plugins.py` checks that it's valid, that its
migrations match its manifests, and that it migrates forwards and back.
