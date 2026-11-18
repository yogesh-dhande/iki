# 0137. A list can hold rows of typed fields

- Status: Accepted
- Date: 2026-11-18
- Refines: [0033](0033-plugin-codegen-and-migrations.md),
  [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0037](0037-entity-search.md),
  [0120](0120-a-state-can-lock-a-record-and-the-database-holds-the-lock.md),
  [0123](0123-queries-walk-the-graph-of-what-depends-on-what.md)

## Context

A field held one value, or a list of values of one kind. Composed
molecules need more: a complex is its parts, each with how many copies,
which chain and what role; a mixture is its ingredients and their
amounts; a sample has what was added to it and when. Each of those is a
row of typed fields, and a lab wants a specific type to refuse a
malformed one: a bispecific antibody has two heavy chains, one or two
light chains and at most one linker, and its heavy chains are proteins.

Benchling models these as "entity link" lists beside parallel lists,
which drift apart; Biovia and Dotmatics registries have component tables
per registration. A JSON field would hold the shape, but nothing in the
database would check it, its references wouldn't be foreign keys, and
neither search, the graph nor locks would see inside it.

## Decision

**A list's items can be rows**: `items: { type: row, fields: {...} }`.
Each row's fields are fields as a type's are (`string`, `text`, `int`,
`float`, `bool`, `enum`, `date`, `datetime`, `quantity`, `ref`, `file`,
`person`), with a label, a description, `required` and a default, and
the same checks. They can't be lists, JSON or states, nor flagged
(indexed, unique, computed, registry): those belong to the type.

**Roles say what each kind of row takes.** `by` names a required enum
field of the row; `roles` gives each of its values a `min` and `max`
count and, for each of the row's references, the types a row with that
role may refer to, among those the field names (each with subtypes).
Saving checks each row's fields, then each role's count and types, and
every problem points where it is: `#/values/components/1/part` ("must
refer to a acme/protein.Protein in a heavy row, not a
acme/dna.DNASequence"), or `#/values/components` for a count. The web
app shows each on its row's field. Anything subtler, such as "the light
chains are the same protein" or "copies sum to 4", is a rule's (step
58): roles are only what every such type needs and the database can be
told.

**Stored in a table of its own**, as a list of references is: one row per
row, `(entity_id, position)` its key, and a column per row field, with
its foreign key (`PROTECT`) and the same CHECK constraints a type's
column gets. Not JSON, so each reference is a real foreign key, each
value is checked by PostgreSQL however it's written, and search and the
graph index it. The existing list tables were for one value; a row's
columns are the row's fields, so a list of rows gets its table from the
same codegen with a column each, rather than a generic key-value child
table, which couldn't hold a foreign key per field.

**Every part of Iki sees inside it.**

- The API gives the value as a list of objects, every field present,
  `null` where empty, and takes one; a list changes as a whole. A type's
  field says `items: row` and gives `row`: its fields, `by`, roles and
  identity fields. Values schemas describe each row as an object.
- History keeps the rows as JSON in the history table's column, as each
  revision's values are kept, so a revision's rows read back as they
  were.
- A lock (ADR 0120, 0125) guards the rows' table as it does a list's: a
  locked record's rows don't change, whatever writes them.
- A reference in a row is an edge of the graph (ADR 0123), named
  `components.part` and kept by `iki_graph_rows`, a statement trigger
  with an edge per row and reference; a reference to several types is
  held to them by ADR 0136's trigger.
- Search's `has` takes an object of row fields that one row must all
  match; the search box reads `components.part:PRT-000001
components.role:heavy` as that.
- Bulk imports write rows to their table in one statement; bundles carry
  them with what they refer to and whom they name; a registrar's
  correction re-points references in rows; signatures hash the files rows
  hold; analysts' views show each list as JSON.
- The web app types rows in a row of inputs at a time, shows them as a
  table on a record's page and as text in the grid; both SDKs read them,
  the Python SDK's dataframes with references as HRIDs.

## Consequences

- A specific type of composed molecule says its shape in YAML, and Iki
  refuses a record that doesn't have it, wherever it comes from.
- A row has no identity of its own: rows are told apart by position, and
  editing one rewrites the list, as editing a list of references does.
- Rows can't nest. A part that is itself composed is an entity of its own
  type that a row refers to, so it has its own identity and history.
- Each list of rows is another table per type, and each write of the
  list deletes and inserts its rows, like any list's.
