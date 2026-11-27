# 0165. A subtype of a registered type has registry fields of its own

- Status: Accepted
- Date: 2026-11-19
- Refines: [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0161](0161-a-complex-is-its-parts-and-their-copies.md)
- Amended: 2026-11-27 — a registrar supersedes an entry with its identity registered as
  another type, which it hands its identity to (see Amendment)

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

## Amendment. A registrar registers an identity again as another type

The decision above promised that a lab wanting the other type supersedes the entry, but
nothing could: a replacement had to be of the entry's own type (ADR 0115), the database
held one entry per identity whether superseded or not, and a registration found the
superseded entry and was refused. A protein complex's chains registered as a protein
complex couldn't become the antibody they are.

**A registrar supersedes an entry with its identity registered as another type**
(`POST /entries/{ref}/supersede` with `type` and `values`): a new entry of a concrete type
under the same identity owner (two subtypes of `iki/core.Complex`, say). The new entry
has the superseded one's values for the tables the two types share, re-checked where the
new type refines a field (its parts' roles, ADR 0164), and what the registrar gives for
the rest: `can.supersede_as` lists each type and the fields to give. Its identity is the
superseded entry's, and anything else is refused. The new entry's first revision names
the one it replaced (`moved_from`). The superseded entry keeps its ID, values and
history, and **hands its identity to the new entry**: the database now lets a superseded
entry, and only a superseded one, have no identity, and still holds one entry per
identity. A registration of the identity finds the new entry from then on, and so do
imports, runs and bundles, which look entries up by their identity. Each holder is told,
as for any supersession.

A replacement registered already may be of another type under the same identity owner
too, as one of the entry's own type may.

**A record moved to an entry of another type is a new record.** A record's type is its
tables, which can't change, so `POST /entities/{id}/move` makes the project's record of
the new entry, with the old record's values for the fields the two types' records share
(`superseded.carries` lists them; a unique value goes with them), and archives the old
record, which keeps its values, history, runs and signatures under the entry it was of.
The project's own records that referred to the old record refer to the new one, as for
any move. A move that would leave a record referring to a type that can't hold the new
one (a measurement whose reference takes only the old type) is refused, naming each
such record. A restored old record can't take back a unique value the new one has.

**Why not change the entry's type in place.** An entry's type is its tables: a type
change would delete its rows of the old type, and the revisions, signatures and exports
that cite them; earlier revisions could no longer be read as they were signed. Its ID's
prefix is its type's, so a citation of ABY-000001 would name a protein complex. A
supersession keeps every one of them as it was and says, for good, what replaced it.
