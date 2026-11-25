# 0185. A transformed strain is its base and its set of plasmids

- Status: Accepted (stock custody updated by [0221](0221-strain-isolates-and-their-glycerol-stocks.md))
- Date: 2026-11-22
- Builds on [registry identities](0115-registries-keep-identities-projects-keep-records.md), [sets of parts](0138-an-identity-reads-sequences-and-sets-of-parts-as-what-they-are.md), [amounts](0139-a-records-amount-is-a-ledger-of-what-moved.md) and [storage](0180-where-material-is-and-how-its-received-moved-and-counted.md).

[ADR 0221](0221-strain-isolates-and-their-glycerol-stocks.md) supersedes the physical stock and box decision below. The base and transformed strain identities still apply.

## Context

Two scientists can register the same transformed strain with different names
and list its plasmids in a different order. They need a common strain entry
and a separate position and volume balance for each vial.

## Decision

Ship three independently installable first-party plugins for microbial
strains, cell lines and mouse strains. Microbial strains use a base and its
plasmids to establish identity. Cell lines need banking, passage and QC
history. Mouse strains use strain and allele nomenclature and have their own
cryopreserved materials. Each plugin reuses Iki's registry, graph and
storage services.

In `iki/strains`, the collection code and accession identify a base strain.
A lab can issue its own code and accession for a local base. The species is
an NCBI taxon reference, and the reported genotype is a registry fact.
Accessions stay as text to preserve leading zeros.

A transformed strain identifies an immutable base entry and a nonempty set
of plasmid entries. Iki hashes a list of references independently of order
and counts repeated items. A blocking type rule therefore requires each
plasmid to occur once. Registrations through different projects can resolve
to one registry entry while retaining each project's strain name.

A glycerol stock is one physical vial. It refers to the strain and has a
volume ledger whose material is that strain. A strain stock box subclasses
the existing cryobox and accepts both stocks and tubes, so existing racks
can hold it. Each draw requires a scan. A draw that names the data record
where the stock was used connects that record, the stock, the strain, and
its base and plasmids in the graph.

## Consequences

Changing the base or adding or removing a plasmid registers a different
strain. Additional vials share its identity and retain independent
locations and balances. A catalogue genotype correction updates the common
entry and keeps its history. A distinct local isolate can receive its own
accession when the lab needs to track its different genotype.
