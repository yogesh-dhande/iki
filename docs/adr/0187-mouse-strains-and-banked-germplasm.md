# 0187. Register mouse strains by MGI accession and trace their banked germplasm

- Status: Accepted
- Date: 2026-11-22
- Builds on [registry entries](0115-registries-keep-identities-projects-keep-records.md), [the graph](0123-queries-walk-the-graph-of-what-depends-on-what.md), [the amounts ledger](0139-a-records-amount-is-a-ledger-of-what-moved.md) and [storage](0180-where-material-is-and-how-its-received-moved-and-counted.md).

## Context

A mouse repository assigns a catalogue number to a stock. MGI gives the strain and each associated allele its own accession and curates their nomenclature. A strain can carry segregating alleles, so a genotype assertion must identify the particular animal or group of specimens described by its evidence. JAX stock 000058 and its MGI strain entry illustrate the identifiers and the alleles carried by a single catalogue stock. [JAX stock 000058](https://www.jax.org/strain/000058) · [MGI strain entry](https://www.informatics.jax.org/strain/MGI:2163869)

## Decision

Ship a separate first-party plugin for each of microbial strains, cell lines and mouse strains. A lab can install the mouse catalogue and cryobank workflow independently. All three workflows reuse Iki's storage and provenance services.

In `iki/mouse_strains`, a MouseStrain and a MouseAllele each use their own MGI accession as registry identity. Curated facts carry the strain designation, the optional six-digit JAX stock number, associated allele references and the allele symbol. JAX stock numbers remain strings to preserve leading zeros. The registration action reads the public MGI strain nomenclature report and keeps the source file and row digest.

A MouseGenotype is a project's observation about an identified subject. It links the strain and reported alleles, preserves the reported genotype notation and can carry the original file. A MouseCryostock is one physical carrier, identified by its HRID or barcode. It records embryos or sperm, its strain and bank date. An optional genotype must refer to the same strain. Its subject is the embryos for an embryo carrier, or the donor for a sperm carrier.

MouseCryobox specializes the LIMS box so the existing racks and shelves accept it. Its positions accept cryostocks. Measured suspension volumes move through the amount ledger, and embryo counts at banking are recorded as whole numbers. A draw can identify its receiving result. Native graph edges join this result to the carrier, genotype, strain and alleles. Both genotypes and cryostocks participate in sample-based file lookup.

## Consequences

Projects share a catalogue identity while keeping individual genotyping evidence, containers, balances and locations in project records. Published registry facts can be corrected without relabelling a local carrier. MGI report imports need the strain accession. JAX cross-references come from the catalogue separately. Stock volume and embryo count have separate units and observation times.
