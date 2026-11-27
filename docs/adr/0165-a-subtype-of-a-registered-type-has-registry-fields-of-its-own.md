# 0165. A subtype of a registered type has registry fields of its own

- Status: Accepted
- Date: 2026-11-19
- Refines: [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0161](0161-a-complex-is-its-parts-and-their-copies.md)

## Context

A registry entry's fields (ADR 0115) were the declaring type's, and its
extensions': only the type with the identity had registry fields. Complexes
(ADR 0161) declare their identity once, on `iki/core.Complex`, and each kind
has facts of its own: an antibody's isotype, which its heavy chain's
constant region says; a protein complex's Complex Portal ID; a lab's own
bispecific's arms.
They're facts of the molecule, the same for every project, so they're the
entry's, not each project's record's. Put on the abstract type, they'd be
every complex's.

## Decision

**A subtype of a type with an identity may declare registry fields.** Its
entry table extends its parent's, as its record table does (Django's
multi-table inheritance): `iki_plugin_iki__antibody.Antibody_Entry` extends
`iki__core.Complex_Entry` by a one-to-one key to it, and holds the subtype's
registry fields, and its lists' tables are its own
(`acme__protein_bispecific__entry__arms`). An entry's
type is the type it was registered as, and it has that type's registry
fields and its ancestors'.

**A reference to a type with entries refers to that type's own entry
table**, so the database holds it to the type: an antibody's heavy chain
refers to `iki__protein_protein__entry`, not to any entity. A reference to
any entry (`iki/core.Entity`, as a complex's parts are) is held by the
reference-type trigger to types with entries.

**Registering an identity again as another type is refused**, naming the
type and entry it's registered as ("This is registered already, as a
iki/antibody.Antibody (ABY-000001): register it as one"): the entry's facts
are its type's, and a registration as another type would give facts it
can't hold. A lab that wants the other type supersedes the entry (ADR 0115).

## Consequences

- Each type keeps its own facts on its entry, and searching the abstract
  type joins only the tables it reads.
- A subtype's entry is two rows, its parent's and its own, read together as
  a record's are.
- An entry's type is fixed when it's registered: registering the same
  molecule as a more specific type later doesn't change it.

## Amendment of 2026-11-27 — a command sees a record's lineage

An action's input that takes a type offers the lab's subtypes of it too, and
a command was told only a record's own `type`. Commands that asked what a
record is compared that name: a complex's part of a lab's `acme/enzymes.Enzyme`,
which extends `iki/protein.Protein`, wasn't weighed by its sequence, and a lab's
own kind of protein lot wasn't taken as one for mass spectrometry.

**Each record in a run input has `types`**: its type and each type it extends,
nearest first, ending with `iki/core.Entity` (`run-input/v1`). A command asks
`"iki/protein.Protein" in record["types"]`, never `record["type"] ==`. The
server gives it for inputs and what they include, and `iki plugin test` gives
it to ActionTests, from the same lineage.

`type` stays: it's what a command names in what it writes back, and what a
record is registered as.
