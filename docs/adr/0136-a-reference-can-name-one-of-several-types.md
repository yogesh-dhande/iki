# 0136. A reference can name one of several types

- Status: Accepted
- Date: 2026-11-18
- Refines: [0033](0033-plugin-codegen-and-migrations.md),
  [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0123](0123-queries-walk-the-graph-of-what-depends-on-what.md)

## Context

A ref field named one type (`to: Protein`), or an abstract type and so
every type that extends it (`to: iki/core.Sequence`), or any entity
(`to: iki/core.Entity`). Composed molecules need more: a complex's part
is a protein, a conjugate or a small molecule, three types that share no
parent but `iki/core.Entity`; a conjugate's payload is a compound or an
oligo. Declaring a shared abstract parent only to type one field would
bend every plugin's type tree around one reference, and `iki/core.Entity`
says nothing, so a lab's sample could name a project or a view.

Benchling's entity link fields take a list of schemas, and Biovia's
registry a list of allowed component types. PostgreSQL has no foreign
key to "one of these tables", and its usual answers are a key to a
common parent table (Iki's `iki_entity`) with the type checked beside it,
or one nullable column per target, which a schema change would break.

## Decision

**`to` takes one type or a list**: `to: [iki/protein.Protein, Conjugate,
iki/small_molecule.Compound]`. The reference names an entity of any one
of them, or of one of their subtypes. A list may name abstract types
next to concrete ones. It can't list `iki/core.Entity` beside others
(that's every entity already), and it can't list a type and one of its
subtypes (the subtype is in already): the validator says so. The model
holds `to` as a tuple, one type for most references.

**Stored as a reference to `iki_entity`**, as one to an abstract type
already is: a foreign key, `PROTECT`, so nothing it names is deleted.
Refs to a type with an identity hold its registry entry (ADR 0115), so
a list may mix types with entries and types with records.

**The database holds it to its types.** A foreign key to `iki_entity`
can't say which types, so `sync_plugins` puts two statement triggers
(insert, update) on every table holding such a reference, a type's
table, a list's or a list of rows' (ADR 0137), which call
`iki_reference_types` with each column and the types it may name: its
types and their subtypes, worked out from the installed plugins. A write
naming another type is refused, whatever makes it. This covers
references to abstract types too, which weren't held before. When a
deploy changes what a column may name, its trigger is replaced, and what
the table holds already is checked first: the sync stops, naming the
records that don't fit, as it does for unique values (ADR 0082).

**Every surface names the list.** The API's field has `to` as a list
(empty for other kinds of field, its items' for a list); a type's
values schema has `x-iki-ref` as a list. `GET /entities` takes `types`,
any one of several, which the web app's picker uses, saying "a protein
or a conjugate". A reference's edge in the graph (ADR 0123) is as any
other's. Errors say which types it takes: "must refer to a
iki/protein.Protein or a iki/dna.DNASequence, not a iki/protein.Sample".

## Consequences

- A plugin can type a part of a composed molecule exactly, without an
  abstract type made for it.
- Every write to a table holding such a reference pays for a join to
  `iki_entity` per statement; bulk writes pay once per statement.
- A new subtype of a listed type is referable once its plugin is
  installed, as with an abstract type: the deploy's sync widens the
  trigger.
