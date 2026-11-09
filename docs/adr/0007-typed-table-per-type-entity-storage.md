# 0007. Typed table-per-type entity storage with history and provenance

- Status: Accepted
- Date: 2026-11-09

## Context

Entities range from short primers to 100-million-row compound libraries.
Scientists filter and sort on typed fields. Analysts query with SQL and BI
tools. Every property value must trace back to the run or user that set
it. Plugins extend each other's types, and types have subtypes.

## Decision

- A core `Entity` row holds identity and bookkeeping (UUID, HRID,
  type, owner project, creator, creating run, `archived_at`, identity
  hash). HRIDs are human-readable IDs such as `PRT-000123`, never reused.
- **One table per entity type**, owned by its plugin, with typed columns,
  B-tree indexes for fields marked `indexed`, and CHECK constraints for
  enums and formats. Its primary key references the core entity.
- **Extension tables** hold fields one plugin adds to another plugin's
  type. Their primary key is a foreign key to the owner table.
- **Subtypes use multi-table inheritance**: each subtype has its own table
  holding only the fields it adds, joined one-to-one to its parent's table
  by primary key.
- Each plugin table has a typed **`_history`** table. `Revision` records
  each change. `PropertyProvenance` records, per field, which revision, run
  or user set the current value.
- A read-only SQL view per type joins the owner and extension tables.
- Edit rules: `computed` fields are set only by actions, `immutable` fields
  never change, and user edits create revisions with `set_by=user`.
- Quantities are stored in a canonical UCUM unit and converted for display.
- `json` is an opt-in field type, not the default storage model.

## Consequences

- Positive: real types, indexes and constraints keep queries fast and data
  valid at large scale.
- Positive: analysts get plain SQL views.
- Positive: full history and per-field provenance make every value
  traceable.
- Positive: each table has one owning plugin, which makes detach and purge
  well defined (ADR 0008).
- Negative: many tables, and joins for extensions and subtypes. Queries
  across a type and all its subtypes are more complex.
- Negative: every write also writes history and provenance rows.
- Negative: schema changes need migrations (ADR 0006).

## Alternatives considered

- **Schemaless JSON documents per entity.** Rejected as the default. They
  lose database types, constraints and efficient indexes. `json` remains
  available per field when a plugin needs it.
