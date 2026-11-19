# 0154. An accession is the record's, not the entry's

- Status: Accepted
- Date: 2026-11-19
- Refines: [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0052](0052-imports-file-params-and-origins.md)

## Context

A DNA sequence's `accession` and a protein's `uniprot_accession` were
registry facts (ADR 0115): one value per registry entry, set by whoever
registered it first, then changed only by a registrar. A registration that
gave another value was refused.

But an accession names a database's record, and one molecule is in several:

- Human calmodulin's 149 residues are UniProtKB's P0DP23 (from CALM1) and
  P0DP24 (from CALM2), two genes encoding the same protein.
- Histone H4's 103 residues are the human P62805 and the pig P62802.
- pUC19 is GenBank's L09137.2 and M77789.2, the second read from the
  other strand and from another origin: one molecule (ADR 0138).

Each pair is one identity, so one registry entry. With the accession a
fact, whoever imported P0DP24 after a colleague had imported P0DP23 was
refused: the whole run failed, and the second database record couldn't be
held at all. The registrar could only choose one, and either choice is
wrong for half the projects.

ADR 0115 made `organism` a record's field for the same reason: the same
sequence occurs in several species. The gene a protein came from was
already a record's. Registration systems that hold external
cross-references (Benchling's and Dotmatics' registries, PubChem's
substances against its compounds) keep many per structure, each with where
it came from.

## Decision

**An accession is the record's: each project's record of an entry keeps
the accession it came from.** `iki/dna.DNASequence.accession` and
`iki/protein.Protein.uniprot_accession` are record fields, like `organism`
and `gene`. A project that imports P0DP23 and another that imports P0DP24
hold one entry, PRT-000123, and each record says which UniProtKB entry it
came from. Neither is refused.

**Every database record a run reads is kept as an origin, those that find
a record again too.** An import's or fetch's creates each carry an origin
(ADR 0052): the database, its accession and version, its link, when it was
read and its checksum. A create that finds a record the project has
already keeps its origin, with no revision, as before. Now so does a
second create of the same identity in one run (P0DP23 and P0DP24 in one
fetch), and every line of a bulk import (ADR 0056), where before only the
first of each record was kept. So a project that imported calmodulin from
both entries lists both among its record's origins, each attributable to
the run, its file and who ran it, and its record's `uniprot_accession`
says the one it came from first.

**A fact is what a registrar curates about the identity itself:** an INN,
a CAS number, an EC number, a hazard class. A database's accession is
provenance, not a fact, and the first-party types no longer declare one as
a fact. A plugin may still declare a curated cross-reference as a fact,
when its registry chooses one record per identity on purpose.

The types' schema versions went up (DNASequence 3, Protein 3), and their
plugins' migrations were regenerated, before M1.

## Consequences

- One molecule can be imported from any of its database records, in any
  project, and each record is kept: what it said, where it came from.
- A search by accession finds the project's records that came from that
  accession. One project's accession isn't another's: a project that
  imported only M77789.2 doesn't find its pUC19 by L09137.2, as it never
  read that record.
- Holders no longer see one accession on the entry. The entry's page lists
  each holder's own record; a registrar sees no accession for an entry,
  only its identity and facts.
- A run that reads the same molecule from two records makes one record
  with two origins, not one refused.

## Alternatives considered

- **A list fact on the entry, which each registration adds to.** Rejected:
  it would be the only fact that grows on its own, without a registrar, and
  it would tell every holder which databases other projects had read, a
  disclosure ADR 0115 keeps to identity alone.
- **Keep the fact and refuse the second.** Rejected: a correct second
  record of a molecule can't be held, and the choice between them is
  arbitrary.
- **Fill the fact only while it's empty, keeping the first** (`fills`,
  ADR 0140). Rejected: the entry would say one database record as if it
  were the molecule's, and creates-file lines can't use `fills`.
