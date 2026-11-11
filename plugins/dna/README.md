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

Both run on backend workers, in the `sequence-tools` environment (Python's
standard library only), and on many entities at once.

- **`compute_composition`**: a DNA sequence's length and GC content.
- **`primer_properties`**: a primer's length, GC content and melting
  temperature.

GC content counts G, C and S among the bases whose pairing is known (A, C, G,
T, U, S and W). Other IUPAC codes, such as N, are left out. Melting
temperature uses SantaLucia's unified nearest-neighbour parameters (1998) with
his salt correction, at 50 mM Na⁺ and 50 nM primer, as Primer3 does by
default. It's computed only for primers of A, C, G and T alone.

Importing FASTA and GenBank files, fetching from NCBI, reverse complements,
restriction digests, primer design and GenBank export come later.

## Developing it

From the repository root:

```sh
uv run iki plugin validate plugins/dna
cd plugins && uv run iki plugin makemigrations dna   # after changing a manifest
```

`server/tests/test_first_party_plugins.py` checks that it's valid, that its
migrations match its manifests, and that it migrates forwards and back.
