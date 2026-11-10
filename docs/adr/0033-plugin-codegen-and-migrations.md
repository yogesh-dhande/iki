# 0033. Plugins compiled to Django apps, and migrations that keep history

- Status: Accepted
- Date: 2026-11-10
- Refines: [0006](0006-deploy-time-plugins-as-django-apps.md),
  [0007](0007-typed-table-per-type-entity-storage.md)

## Context

ADR 0006 says plugins compile to Django apps, and that their migrations are
made when the plugin is authored and reviewed in its pull request. ADR 0007
says each type gets a typed table with a typed history table. This ADR
settles how the compiled apps look, what the database enforces, and how
migrations keep history when a plugin changes. Pharma and lab customers
audit both: history must not be lost, and the database must reject bad data
even when something bypasses the app.

## Decision

**`iki plugin build` compiles plugins deterministically.** It writes a package
`iki_plugin_<name>` per plugin (app label `<name>`), holding the models, the
plugin's manifest as JSON, and its migrations, copied unchanged from the
plugin repository. It also writes `iki_installed_plugins`, which lists the apps
in dependency order. The server's settings install the apps if that package
is importable. The same manifests always produce the same bytes.

**One Django model per table.** For each entity type:

- **The type's own table** (`protein_protein`). A root type extends
  `core.Entity`, and a subtype extends its parent, with multi-table
  inheritance. Each row's primary key is `entity_id`. Parent links have no
  reverse accessor, so renaming a type is a rename to Django rather than a
  new model.
- **Its history table** (`protein_protein__history`). This is one row per
  (entity, revision), declared `APPEND_ONLY` so the app role can't update or
  delete it (ADR 0025). Its columns have wide types and no constraints. A
  reference is a bare UUID with no foreign key, so history outlives what it
  pointed to. It keeps the columns of removed fields.
- **A table per list of references or files** (`protein_proteinstructure__chains`).
  This is one row per item, keyed by (entity, position), so every item has a
  real foreign key. Other lists are PostgreSQL arrays.

An extension (`structure_prediction__protein_proteinstructure`) gets the same
three kinds of table. Its rows are keyed by the extended type's `entity_id`.
The double underscore marks what Iki adds to a name, since plugin and field
names only use single underscores.

**The database enforces the rules it can.** Each rule is a named CHECK
constraint, such as `protein_protein__sequence__format`:

- Strings: formats, patterns (anchored, as the validator checks them), one
  line, never empty, and lengths.
- Enums, and numeric bounds.
- Floats and quantities are finite, so NaN and infinity are refused.
- Lists are plain one-dimensional arrays with no NULL items, with their
  cardinality and per-item rules.

Per-item rules call small `IMMUTABLE` SQL functions with a fixed
`search_path`, because PostgreSQL forbids subqueries in CHECK constraints.
References and files are foreign keys, so nothing they point to can be
deleted from under them.
Required fields are `NOT NULL`, defaults are database defaults, and
`indexed` fields get B-tree indexes. A missing value is NULL, never an
empty string.

**A quantity column records its unit.** `QuantityField(unit=…)` puts the
canonical UCUM unit into the migration state, so a unit change shows up as a
migration.

**Descriptions aren't in migrations.** Table and column comments (labels,
descriptions, units) are documentation. `sync_plugins` (below) sets them from
the manifests, so editing a description needs no migration.

**`iki plugin makemigrations` is Django's makemigrations, with history
rules.** It builds the plugin and its dependencies (`--with`) into a
temporary directory. It then runs Django's command there against the dummy
database backend, with Django's own rename questions, and copies new
migration files into the plugin's `migrations/`. On top of Django:

- **History is never deleted.** If a field disappears from a manifest, the
  command refuses and says either to answer that it was renamed or to mark
  it `removed: true`. Removing a field drops its live column but keeps its
  history column. The command also refuses a deleted type or extension.
- **A history column only changes type when every old value still fits.**
  The types are wide (`string`, `text` and `enum` are all text), and whole
  numbers may become floats. Any other change is refused, with a hint to add
  a new field.
- **Renames are asked about once.** The history table follows the answer
  given for its live table.
- **Unit changes convert values.** A `ConvertUnit` operation rescales current
  and historical values (`new = old × scale + offset`, so temperatures work),
  and the reverse migration rescales them back. Units that measure
  different things, or that don't convert linearly (`[pH]`), are refused.
- **Type renames follow through.** A `RenameEntityType` operation renames the
  type in `iki_entity.type`.
- No code formatter runs on the output, so every machine writes the same
  migration.

`--check` fails when a manifest has changed without a migration. The plugin
template's CI runs it.

**`iki-server sync_plugins` does what migrations shouldn't.** The migrate Job
runs it as the migrator, after `migrate` and before `grant_app_privileges`.
It is idempotent and works from the manifests the build installed:

- **HRID sequences.** There is one per prefix (`iki_hrid_prt`). They are
  created if missing and never dropped, so a number is never reused, even if
  a plugin leaves and comes back. A new entity gets its type, HRID and
  identity hash when it is first saved. Abstract types can't have entities.
- **Comments** on every plugin table and column: label, description, unit,
  and what a reference points to.
- **A view per type** in the schema `iki_views`, for analysts and BI tools.
  Each view joins the entity row, the type's tables up its parents, and
  every installed extension, with lists of references or files as arrays.
  The views use `security_invoker`, so they never grant more than their
  reader already has. Views are rebuilt on every deploy. If someone has built
  their own view on one of them, the deploy fails rather than dropping it.
- **An analyst role.** If `database.analystRole` names one, it can read the
  views and the entity tables behind them, current values and history, and
  nothing else.

**History is append-only for every role.** Each history table gets triggers,
created by its own migration (`AppendOnly`), that refuse UPDATE, DELETE and
TRUNCATE, even from the tables' owner. Only `ConvertUnit` lifts them, inside
a migration, to rewrite a quantity in its new unit.

**Identity hashes follow a written encoding** (`iki_spec.identity`). This is
canonical JSON with sorted keys and no spaces, with values encoded by type:
NFC text, integers, UTC datetimes with microseconds, UUIDs, and a file's
content SHA-256. It is versioned, and it names the type that declares the
identity, so subtypes share their parent's duplicates. Other languages can
compute the same hash.

**Plugins installed together must not clash.** `iki plugin build` refuses two
plugins with the same HRID prefix, the same table, or the same field added to
one type. Each plugin's own validator can't see these, because it only knows
its dependencies.

## Consequences

- Positive: bad data is refused by PostgreSQL itself, with a constraint name
  that says which rule failed. That holds for analysts' SQL and future code
  paths too.
- Positive: no change made through the tools can silently lose history.
  Destructive changes are refused with what to do instead.
- Positive: migrations are plain Django migrations, reviewed in plugin pull
  requests and reversible for rollback.
- Negative: PostgreSQL can't check item counts for lists of references or
  files, which live in separate tables. The app checks those.
- Negative: deleting a type isn't supported. Its manifest stays in the
  repository for as long as its history must be kept.
- Negative: a unit conversion rewrites historical numbers into the new unit,
  with floating-point rounding. The physical quantity is unchanged, and the
  migration that did it is recorded.
- Negative: plugin migrations import `iki_server.entities.fields`,
  `iki_server.entities.checks` and `iki_server.plugins.operations`. Those
  names are now API and change only with a deprecation.

## Alternatives considered

- **Generating migrations at deploy time.** Rejected. Nobody would review
  them, and renames would become drops.
- **Keeping history in one JSON table.** Rejected by ADR 0007. History would
  lose its types, and its rows would be hard to query.
- **Foreign keys in history tables.** Rejected. Purging what they point to
  would mean rewriting or deleting history.
- **CHECK constraints only in the app.** Rejected. SQL clients and future
  code paths would bypass them.
