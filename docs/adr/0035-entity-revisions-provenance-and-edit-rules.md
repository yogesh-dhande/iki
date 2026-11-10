# 0035. Entity revisions, provenance, edit rules, duplicates and projects

- Status: Accepted
- Date: 2026-11-10
- Refines: [0007](0007-typed-table-per-type-entity-storage.md),
  [0008](0008-archive-first-deletion-with-purge-and-tombstones.md),
  [0033](0033-plugin-codegen-and-migrations.md)

## Context

ADR 0033 gives every entity type a typed table and an append-only history
table. This ADR settles how entities change: what a change records, which
changes are allowed, what happens when two people create the same thing or
edit the same thing at once, and which projects an entity belongs to. Pharma
and lab customers audit data under GxP and 21 CFR Part 11 expectations. Every
value must be attributable (who, how and when, and why if they said), the
record of past values must be complete and unchangeable, and one person must
never silently overwrite another's work.

## Decision

**Every change is a numbered revision.** `iki_revision` holds one row per
change: the entity, its number (1, 2, …, per entity), the kind (`created`,
`edited`, `archived`, `restored`), the fields it changed, the user, how they
made it (`via`: web, API, MCP or a run, from the request's audit context),
when, and an optional reason. The table is append-only for every role,
enforced by triggers as for history (ADR 0025). The entity keeps its current
revision number, so a reader knows which revision they saw.

**History is written for the tables a revision touched.** A change writes one
history row for each table it changed (a type in the entity's chain, or an
extension), holding all of that table's values after the change. Any
revision can be read back exactly: each table's latest history row at or
before that revision. Tables a change didn't touch get no row, so a revision
of one field doesn't copy every table.

**Provenance comes from revisions.** A field's provenance is the latest
`created` or `edited` revision that changed it: who or what set the value,
how, when and why. There is no separate provenance table to drift from the
revisions.

**Edit rules hold for every caller.** All changes go through one service, so
the web, the API, MCP and runs follow the same rules:

- Values are checked against their field's rules before they reach the
  database, whose constraints check them again.
- `computed` fields are refused from people. Only actions set them.
- `immutable` fields can be set once, and can't change after.
- `required` fields can't be cleared.
- Quantities arrive as a number in the field's unit or as `{value, unit}`,
  which is converted into the field's unit (UCUM). The unit is stored once,
  in the field.
- Date-times need a time zone. A time without one is ambiguous across sites.
- A reference must name an entity the caller can read, of the right type.
  Otherwise it's refused as if that entity didn't exist.
- File fields are set through the files API (commit 16), which checks the
  upload.

**Edits name the revision they were made against.** An edit that names an
older revision than the current one is refused as stale, with the current
number, and the caller reloads. Nothing is merged or overwritten silently.
This is optimistic concurrency, as GitHub uses for file edits. The REST API
carries it as `ETag` and `If-Match`, and an edit without `If-Match` is
refused (428).

**Duplicates follow the type's `on_duplicate`.** Two entities are the same
when their identity hashes match (ADR 0033). A subtype without its own
`on_duplicate` follows its nearest parent's. Identity fields are required and
immutable, so an entity's identity never changes after it's created.

- `reuse` (the default): the existing entity is returned and linked into the
  caller's project, instead of a second copy.
- `warn`: refused, naming the existing entities, unless the caller says to go
  ahead.
- `allow`: always a new entity.

Only entities the caller can already read, and that aren't archived, count.
A duplicate check must never reveal that someone else's project holds a
sequence. Concurrent creations of the same entity take a transaction lock on
its hash, so the second sees the first.

**Entities can belong to several projects.** Each entity has one owner
project and any number of links (`iki_entity_project`). The owner project is
always linked. Reading an entity takes a role in any project it's linked to.
Changing it takes Editor in the owner project. A reused duplicate is shared
with the new project, not copied. Its owner project still decides its
changes, so one team can't edit another's record. The owner project can't be
unlinked. Linking and unlinking change who can read the entity, so both are
recorded in the audit log.

**Archive, don't delete** (ADR 0008). Archiving and restoring are revisions.
An archived entity keeps its history, can't change until it's restored, and
isn't offered as a duplicate.

## Consequences

- Positive: every value is attributable to a person or run, how they made
  it, when, and why if they said.
- Positive: any past state of any entity can be read back exactly, and
  neither revisions nor history can be changed, even with direct database
  access by the app's role.
- Positive: concurrent editors can't lose each other's changes.
- Positive: shared reagents and sequences are recorded once, and visible
  where they're used.
- Negative: every edit writes a revision and history rows. That's a few rows
  per change, which is acceptable for the record it gives.
- Negative: people in a linked project can read but not change a shared
  entity. Only its owner project can. This is deliberate, and the UI will say
  who owns it.

## Alternatives considered

- **A provenance table per field.** Rejected: it duplicates what revisions
  already record, and the two could disagree.
- **Copying every table into history on every change.** Rejected: it costs
  rows for nothing, since unchanged tables can be read from their earlier
  rows.
- **Last write wins.** Rejected: one person's edit would silently replace
  another's, which an audit would count as lost data.
- **Merging concurrent edits field by field.** Rejected for now. A reload
  shows the person what changed, and a silent merge could combine two edits
  that only make sense apart.
- **Duplicate checks across all entities.** Rejected: refusing a duplicate
  the caller can't see would reveal that it exists.
