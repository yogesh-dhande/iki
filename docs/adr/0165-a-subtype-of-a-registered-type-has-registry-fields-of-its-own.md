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
constant region says; a multispecific's binding sites; an Fc fusion's
isotype and how its chains pair; a protein complex's Complex Portal ID.
They're facts of the molecule, the same for every project, so they're the
entry's, not each project's record's. Put on the abstract type, they'd be
every complex's.

## Decision

**A subtype of a type with an identity may declare registry fields.** Its
entry table extends its parent's, as its record table does (Django's
multi-table inheritance): `iki_plugin_iki__antibody.Antibody_Entry` extends
`iki__core.Complex_Entry` by a one-to-one key to it, and holds the subtype's
registry fields, and its lists' tables are its own
(`iki__antibody_multispecificantibody__entry__binding_sites`). An entry's
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
