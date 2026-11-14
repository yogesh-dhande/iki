# 0082. Unique fields link records in other systems

- Status: Accepted
- Date: 2026-11-13
- Refines: [0033](0033-plugin-codegen-and-migrations.md),
  [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0067](0067-a-project-can-keep-its-entities-to-itself.md)

## Context

Labs keep records in a LIMS or an ELN, such as Benchling, LabWare or
STARLIMS, and the same samples and proteins in Iki. Connecting the two is a
plugin's job: it adds a field for the other system's ID to a type (an
extension, ADR 0033) and imports new records on a schedule (ADR 0081).

For that to work, the ID must lead to one entity. Identity (ADR 0035) is the
wrong tool: it describes what an entity is, such as its sequence, and two
LIMS records can hold the same sequence. A record's ID is a key, as a
`UNIQUE` column is in SQL and an external ID field is in Salesforce.

Projects complicate this. A project can keep its entities to itself (ADR
0067), and nobody outside it may learn what it holds, even that an ID is
taken there.

## Decision

**A plugin marks an indexed string field `unique`.** No two entities in a
project have the same value. The field may be the type's own or one an
extension adds. Only strings can be unique, since IDs are strings, and the
field must be indexed, so it has a `max_length` and a lookup is fast.

**Unique within the owner project.** The same value may be on an entity in
another project. That keeps projects apart: a private project's IDs never
block, or reveal anything to, another project. A value stays taken while its
entity is archived, as the record it names still exists.

**The database refuses a duplicate, whatever writes it.** A table,
`iki_unique_value`, holds each unique field's values once per project, under
a `UNIQUE (project, key, value)` constraint. The key is the column,
`table.column`. A trigger on each unique column keeps it in step, so a
person's edit, a run's results, a bulk import (ADR 0056) and SQL are all held
to it. The triggers aren't in plugin migrations, which Django writes:
`sync_plugins` installs them on each deploy, drops those of fields no longer
unique, and fills in values written before a field was made unique. If
entities in a project already share a value, the deploy stops and names
them.

**People are told which entity has it.** Before writing, Iki looks up the
value, and a refusal points at the field: "is PRT-000003's already". Two
writes at the same moment are refused by the constraint instead, with a
plainer message. A bulk import that would write a taken value, or the same
value twice, fails as a whole, naming the field and value.

**Entities are looked up by a unique field's value**, in the API
(`GET /entities/lookup`), the SDK and an MCP tool. A lookup finds the
entities the reader can read, in any project, so it shows at most one of each
project's own. It never says that a project the reader can't see has one.

**A field can link to its record.** A string field's `link` is a URL with
`{value}` in it, such as `https://lims.example.org/samples/{value}`. It may
start with one of the plugin's settings, since each deployment has its own
LIMS. The build checks the link's shape, and the server gives a field's link
only if, with the settings filled in, it is https. So no setting can turn a
value into a script. The web app shows the value as a link that opens a new
tab, with the value URL-encoded.

## Consequences

- A LIMS plugin can rely on one entity per record in each project. Looking
  entities up by an ID, imports that update the entity a record matches,
  and write-back build on this.
- The guarantee lives in the database, so it holds for code paths written
  later, and for anyone with SQL access.
- Each write of a unique field costs a trigger and an index entry, and only
  unique fields pay it.
- The same record in two projects is two entities. Finding and merging such
  copies is left to people who can read both.

## Alternatives considered

- **A unique constraint on the plugin table.** Rejected: the owner project is
  on the core entity row, not the plugin table, and PostgreSQL constraints
  can't span tables. Copying the project onto every plugin table would add a
  column to keep right everywhere.
- **Checks in application code only.** Rejected: bulk imports write with
  SQL, and a check without a constraint loses races.
- **Unique across the deployment.** Rejected: it would leak whether a private
  project has a record, and stop two projects from tracking the same one.
