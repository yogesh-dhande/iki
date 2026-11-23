# 0206. DNA sequence annotation records library matches and provenance

- Status: Proposed
- Date: 2026-11-22
- Related: [0085](0085-types-configure-ikis-own-viewers.md),
  [0138](0138-an-identity-reads-sequences-and-sets-of-parts-as-what-they-are.md),
  [0163](0163-a-plasmid-is-a-record-about-a-sequence-not-a-kind-of-sequence.md)

## Context

DNA imports preserve the annotations supplied by the source record. Some records,
including FASTA imports, arrive with bases alone. A project can already annotate a
registered plasmid from a versioned reference library. A scientist also needs to
locate routine vector features directly on a DNA sequence, such as a linear fragment.
Reference-based annotation can make each call traceable to the evidence for it.
[pLannotate paper](https://doi.org/10.1093/nar/gkab374)

## Decision

`iki/plasmid.annotate_dna_sequence` annotates a project’s `iki/dna.DNASequence` from the
versioned Routine cloning-vector features library. The library bundles intervals
from the original INSDC feature tables, identified by accession version and location.
[Source records and terms](../../plugins/plasmid/data/README.md)

Rust code behind `iki-wasm` uses Aho–Corasick to find full-length exact matches against
the bases in their stored orientation. It reads both strands of double-stranded
sequences and continues across the origin for circular sequences.
Coordinates are one-based and inclusive. A match across the origin ends before it
starts. Each match keeps its strand and source.
[Aho–Corasick paper](https://doi.org/10.1145/360825.360855),
[INSDC feature-table specification](https://www.insdc.org/submitting-standards/feature-table/)

The project record holds the results in `library_features` and the library name and
version in `library_annotated_with`. The Sequence tab shows these with the source
record’s features. An empty result records that the run found zero matches. Rerunning
the action replaces the previous library results.

The action accepts up to 16 MiB of bases and records up to 5,000 hits. Exceeding
either limit fails the run before any results are written. The worker runs a standalone
WebAssembly module compiled from the same Rust code. The `iki-spec` distribution
includes the module and its Rust standard library notice. `iki deploy build`
verifies the module against the digest in the plugin's source before staging it for
downloadable worker archives and worker images.

`scripts/build-dna-annotation-wasm.sh` builds the module and notice into
`spec/src/iki_spec`. It uses the Linux x86-64 Rust toolchain for reproducible builds.
On other hosts it runs the server's pinned Rust image. Its `--check` flag rebuilds
the module and verifies it against `plugins/plasmid/data/dna-annotation.sha256`.
The module and library can support a future `@ikisci/science` entry point after the
shared package lands.

## Consequences

- A FASTA import can gain library matches as soon as the DNA sequence is registered.
- The record keeps the source annotations and library matches together with their
  respective provenance.
- A variant of a reference feature needs an exact match to another library entry.
