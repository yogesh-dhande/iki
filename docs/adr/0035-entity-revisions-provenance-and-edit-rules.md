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
refused (428). `If-Match: *` is refused too, because it would skip the check.

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

**One typed API for every type.** `/entity-types` lists the installed types.
Each comes with its fields and a JSON Schema (2020-12) of its values, which
forms and clients use. `/entities` serves every type the same way:

- An entity is addressed by its ID or its HRID.
- Values are JSON, as the type's schema says.
- Reads return the revision as the `ETag`, and `If-None-Match` answers 304.
- `PATCH` changes values, archives and restores. A restore and an edit can
  go in one request, in that order, under one lock.
- Each entity's revisions, any past revision's values and changes, its
  provenance and its projects have endpoints of their own.

An entity the caller can't read answers 404, wherever it's named: as a path,
a filter, a reference or a duplicate. An owner project the caller can't see
shows as null.

**Webhooks hear of changes, but not values.** Each revision sends
`entity.created`, `entity.updated`, `entity.archived` or `entity.restored` to
the owner project's webhooks and the organization's. Linking and unlinking
send `entity.linked` and `entity.unlinked` to that project's webhooks, by way
of the audit log (ADR 0031). Payloads hold identifiers: the entity's ID,
HRID, type and revision, its owner project's ID, the changed field names,
who made the change, how and why. They never hold values. A receiver reads
the values through the API with its own token, and that token's access
decides what it sees. So a webhook can't send a project's sequences out of
Iki, and revoking a token stops the data. This differs from ADR 0031's
before-and-after payloads for projects and teams, whose fields aren't
research data.

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
- Negative: a webhook receiver makes an API call to learn the new values.

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
- **A route per type** (`/dna-sequences`). Rejected: every plugin would grow
  the API surface, and clients would need regenerating whenever a deployment
  changed its plugins. The schema endpoint gives clients the types instead.
- **Values in webhook payloads.** Rejected: they would leave Iki under the
  webhook's settings rather than the reader's access.
