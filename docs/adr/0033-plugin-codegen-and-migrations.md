# 0033. Plugins compiled to Django apps, and migrations that keep history

- Status: Accepted (amended 2026-11-27: a deployment can give a type another HRID
  prefix, and Iki publishes the prefixes it keeps)
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
`indexed` fields get B-tree indexes (indexed strings also get trigram
indexes, and text fields full-text ones: see ADR 0037). A missing value is NULL, never an
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

## Amendment of 2026-11-27 — a plugin's HRID prefix is a default

**Context.** Every type in a deployment shares one space of HRID prefixes,
and the build refuses two types with one. Iki's own plugins use 118 of them,
106 of three letters. After M1, a release of Iki's plugins can add a type
whose prefix a lab's plugin already took, and the lab's upgrade is then
blocked. Renaming isn't a way out: HRIDs are printed on labels and tubes,
and sequences are never dropped. Nor can the lab change Iki's plugin, or
Iki know every lab's prefixes.

**Decision.**

- The global check stays: no two types in a build share a prefix, and no
  plugin takes one of core's or of the records the server numbers itself
  (runs, batches, workflow runs, setup builds).
- **A plugin's prefix is a default.** A deployment's `plugins.lock.yaml`
  may give a concrete type another, `hrids: { acme/buffers.BufferPrep: ABUF }`.
  The build checks every plugin with the deployment's prefixes and compiles
  them into the manifests the server reads, so the HRIDs, their sequences
  and every later check use them. An override must name a concrete type of a
  plugin the lockfile builds; core's types can't be given one.
- **A clash names its settlement.** The build's refusal names both types and
  the override that settles it, moving a lab's type before one of Iki's and
  never core's, with a free prefix suggested.
- **A type with records keeps its prefix.** The deploy refuses a build that
  gives such a type another, by an override or by its plugin, and says how
  to keep the old one. It checks again under the deploy's lock, since a
  record may be made after preflight.
- **Iki publishes the prefixes it keeps**, in
  `iki_spec/reserved_hrid_prefixes.json`: core's, those its plugins use, and
  those it reserves for first-party types on its roadmap.
  `scripts/first_party_trees.py` refreshes it with the plugins' trees, and
  CI fails when it's stale. `iki plugin validate` warns a lab's plugin that
  takes one of them (`reserved-hrid-prefix`), while the type has no records
  and the prefix is cheap to change.

Precedent: Benchling's registry ID prefixes are set per schema by the
tenant's admin, not by whoever wrote the schema.

**Consequences.**

- Positive: two plugins that don't know each other can be installed
  together without changing either. A first-party release can't strand a
  lab on an older one.
- Positive: a lab is told about a likely clash when it writes its plugin,
  not when it upgrades.
- Negative: a type's prefix can differ between deployments, so an HRID
  alone doesn't say which plugin declared its type. Its record does.
- Negative: the reserved list is one more generated file; it's refreshed in
  the same step as the trees list, so a merge that changes a plugin's
  prefixes refreshes both.

**Alternatives considered.**

- **Prefixes namespaced by plugin** (`ACME-BUF-…`). Rejected: HRIDs are
  short so people can read them off a tube, and every existing label would
  change.
- **Iki assigns prefixes from a registry.** Rejected: a lab's plugin may
  never be published, and the registry would still not prevent two labs'
  plugins clashing in one deployment.
- **A per-plugin override in the plugin's settings.** Rejected: settings
  are the lab's choices about a plugin's behaviour, and a prefix clash is a
  property of the deployment's set of plugins, which only the lockfile sees.
