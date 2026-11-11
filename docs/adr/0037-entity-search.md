# 0037. Entity search: typed JSON conditions, compiled to indexed SQL

- Status: Accepted
- Date: 2026-11-10
- Refines: [0005](0005-postgres-only-data-infrastructure.md),
  [0007](0007-typed-table-per-type-entity-storage.md),
  [0033](0033-plugin-codegen-and-migrations.md)

## Context

Scientists find entities by what they are: human kinases over 50 kDa,
structures solved by X-ray below 2 Å, sequences whose notes mention a
mutation. Every type has its own fields, plugins add more, and subtypes
inherit their parents'. A search on a parent type (all sequences) should
find its subtypes (DNA and proteins). Tables reach millions of rows, so
searches must use indexes and page without slowing down, and no one search
may tie up the database that runs the lab's work. The same search has to
work from the web app, the SDK and agents (MCP), and be saved and shared.

## Decision

**A search is JSON: a type, then conditions on its fields.**
`POST /entities/search` takes a type (with its subtypes, or exactly), a
project, archived or not, `q` (text in the HRID or title) and `where`, a
tree of conditions:

```json
{
  "all": [
    { "field": "organism", "op": "eq", "value": "Homo sapiens" },
    { "field": "molecular_weight", "op": "gte", "value": { "value": 50, "unit": "kDa" } },
    { "not": { "field": "summary", "op": "matches", "value": "fragment" } }
  ]
}
```

A structure rather than a query language: clients build it without
escaping, agents produce it reliably, it's validated with a pointer to the
mistake, and saving a search is storing its JSON. (Jira's JQL and GitHub's
search syntax are strings; their APIs still parse them into structures.
The web app can offer a text syntax over this later.)

**Each kind of field takes its own operators, and values as it stores
them.** Strings: `eq`, `ne`, `in`, `contains`, `starts_with`. Text:
`contains` and `matches` (its words, as people type them into a search
engine: `"active site" -fragment`). Numbers, quantities, dates and times:
comparisons. Quantities take any unit of their kind, converted to the
field's own; equality allows for the last digits of unit conversion (86 °F
is 30.000000000000004 °C). References take IDs or HRIDs, of entities the
caller can read; naming one they can't read reveals nothing. Lists take
`has`; every field takes `is_set`. `ne` and `not` match entities without a
value too, as a scientist asking for "not human" expects unannotated ones.
Every entity's own columns (HRID, type, owner project, creator, created,
updated and archived times) can be named without a type.

**Conditions compile to the ORM over the type's own table.** A type's table
holds its subtypes' rows (multi-table inheritance), so a condition on
`dna.Sequence.name` finds proteins too, by joining, with no union across
types. Extension fields join their plugin's table; lists of references
check their item table with `EXISTS`. Only entities the caller can read
are searched: the same `EXISTS` on project links as every read.

**Text is searched through indexes made for it.** pg_trgm, a trusted
extension that the database's owner may create, so no superuser is
needed. Indexed strings get a trigram GIN index beside their B-tree, so
`contains` and `starts_with` are fast at any size. Iki compares with
`ILIKE`, which the index serves, rather than Django's `UPPER(…) LIKE`,
which it can't. Every text field gets a full-text GIN index over its words
with the `simple` configuration: no stemming or stop words, which would
mangle gene names, accessions and chemical names. HRIDs get a trigram
index, and `q` searches them and the title's text fields as one `IN` over
a `UNION`, so each part uses its own index. Tests check the plans.

**Results sort by one field, then ID, and page by keyset.** Unset values
sort last. The cursor holds the last row's value and ID, signed, and names
the sort it belongs to, so it can't be used with another. Pages stay fast
at any depth, and stable while entities are added. Sorting by when
entities were made is sorting by ID (UUIDv7), which is indexed.

**Every search has bounds.** At most 50 conditions, 6 levels deep, 100
values in an `in`, 200 characters of text. Each runs in a transaction with
a 15-second statement timeout. One that runs out answers 422,
`search_too_slow`, saying to narrow it with indexed fields. One costly
search can't hold the database the lab's work runs on.

**Searches can be saved, for oneself or for the project.** A saved search
keeps its JSON under a name, in a project. Anyone who can read the project
saves searches for themselves. Sharing one with everyone who can read the
project takes Editor there, so viewers can't fill a project's list. Only
its owner changes it, and the project's Admins can remove a shared one,
as Jira's shared filters work. A search is checked as its owner when it's
saved, and again each time it runs, since types change. Shared searches
are audited (shared, changed, unshared, removed), because they're what a
project's people see. Personal ones aren't: they're like bookmarks.

## Consequences

- Positive: one search for the web app, the SDK, agents and saved
  searches, with errors that point to the condition.
- Positive: searches on parent types find every subtype, and extension
  fields are searchable like the type's own.
- Positive: text search and deep pages stay fast on large tables.
- Negative: conditions on unindexed fields scan the type's table. The
  time limit stops the worst of these. Plugin authors index the fields
  people filter on, as the spec says.
- Negative: deployments need pg_trgm. It comes with PostgreSQL and is
  available on RDS, Aurora, Cloud SQL, Azure (where it must be allowed in
  `azure.extensions`) and CloudNativePG.

## Alternatives considered

- **A separate search engine (OpenSearch, Elasticsearch).** Rejected
  (ADR 0005): another system to secure, back up and keep consistent, and
  access checks would have to be copied into it. Postgres's indexes
  cover typed filters and text search at our scale.
- **A query language string as the API.** Rejected for the API: see above.
  A text syntax can be added in the web app's search box over the JSON.
- **Offset pagination.** Rejected: deep pages get slow, and pages shift
  as entities are added.
