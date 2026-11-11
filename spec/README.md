# iki-spec

The versioned specs for Iki plugins, and the Python package that reads and
checks them. Version `iki/v1` covers plugins, entity types, extensions,
fields, units and settings; actions, environments and the rest arrive in
later versions of this document as they land.

- **JSON Schemas** (draft 2020-12), in [`src/iki_spec/schemas/`](src/iki_spec/schemas/).
  Each `<name>/v1.json` is published at `https://ikisci.org/schemas/<name>/v1`.
  Use [`manifest/v1`](src/iki_spec/schemas/manifest/v1.json) for any manifest;
  it picks the right schema by `kind`.
- **The validator** behind `iki plugin validate` (see [`cli/`](../cli/README.md)),
  which also checks what a schema can't: units, references across files and
  plugins, table and index rules, identities and settings.
- **Units** ([`iki_spec.units`](src/iki_spec/units/__init__.py)): parsing,
  checking and converting [UCUM](https://ucum.org) codes, used by the
  validator now and by the server when it stores quantities.

## A plugin

A plugin is a git repository (or a directory in one):

```text
plugin.yaml          # kind: Plugin — exactly one, at the root
types/               # kind: EntityType and EntityTypeExtension
  protein.yaml       # any number of files, each with one or more documents (---)
  structure.yaml
migrations/          # made by `iki plugin makemigrations`, committed and reviewed
```

Every manifest starts with `apiVersion: iki/v1` and a `kind`.

```yaml
apiVersion: iki/v1
kind: Plugin
name: protein # lower case; types are protein.Name, tables protein_name
version: 3.0.0 # semantic version, for people (deployments pin the git commit)
title: Proteins
license: Apache-2.0
dependencies:
  dna:
    types:
      DNASequence: ">=2,<4" # schema versions this plugin works with
settings:
  uniprot_mirror: { type: string, format: uri, default: "https://rest.uniprot.org" }
```

## Entity types

```yaml
apiVersion: iki/v1
kind: EntityType
name: Protein
schema_version: 3 # increase it whenever the fields change
extends: dna.Sequence # optional parent type (multi-table inheritance)
hrid: { prefix: PRT, digits: 6 } # PRT-000123, never reused
identity: [sequence] # what makes two entities the same
on_duplicate: reuse # reuse (default) | warn | allow
fields:
  sequence: { type: string, format: protein_sequence, required: true, immutable: true }
  molecular_weight: { type: quantity, unit: u, display_unit: ku, computed: true, indexed: true }
  organism: { type: string, max_length: 200, indexed: true }
  source: { type: ref, to: core.Entity } # any entity
display:
  title: "{{ name or hrid }}"
```

| Key                     | Meaning                                                                                             |
| ----------------------- | --------------------------------------------------------------------------------------------------- |
| `name`                  | PascalCase. Elsewhere the type is `plugin.Name`; in the same plugin, `Name` is enough.              |
| `label`, `label_plural` | What people see. Default: the name split into words, and that plus "s".                             |
| `schema_version`        | A whole number from 1. Dependencies and actions name the versions they work with.                   |
| `extends`               | A parent type. Entities of this type are also entities of the parent, with its fields.              |
| `abstract`              | Only subtypes have entities. Abstract types have no `hrid`; every concrete type must have one.      |
| `hrid`                  | Human-readable IDs: a 2–8 character prefix (capitals and digits), zero-padded to `digits` (3–12).   |
| `identity`              | Fields that make an entity the same as another. Each must be `required` and `immutable`. See below. |
| `on_duplicate`          | When someone creates a duplicate: `reuse` the existing entity, `warn` and let them choose, `allow`. |
| `display.title`         | Text with `{{ a or b or "text" }}` parts, naming fields or `hrid`.                                  |

### Identities

Iki stores a hash of each entity's identity values, to find duplicates.
Subtypes share the identity of the type that declares it. The hash is the
SHA-256, in lower-case hex, of UTF-8 JSON with sorted keys and no spaces:

```json
{ "type": "dna.DNASequence", "v": 1, "values": { "bases": "ACGT" } }
```

Here written with spaces for reading; the hashed form is
`{"type":"dna.DNASequence","v":1,"values":{"bases":"ACGT"}}`. `type` is the
type that declares the identity. Each value is encoded by its field's type:

| Type                     | Encoded as                                |
| ------------------------ | ----------------------------------------- |
| `string`, `text`, `enum` | The text, in Unicode NFC                  |
| `int`                    | A JSON integer                            |
| `bool`                   | `true` or `false`                         |
| `date`                   | `"YYYY-MM-DD"`                            |
| `datetime`               | UTC: `"YYYY-MM-DDTHH:MM:SS.ffffffZ"`      |
| `ref`                    | The entity's UUID, lower case             |
| `file`                   | The SHA-256 of the file's content, in hex |

[`iki_spec.identity`](src/iki_spec/identity.py) implements it.

### Extensions

A plugin adds fields to another plugin's type with an extension. The fields
live in the extending plugin's own table
(`structure_prediction__protein_proteinstructure`).

```yaml
apiVersion: iki/v1
kind: EntityTypeExtension
extends_type: protein.ProteinStructure
fields:
  plddt_mean: { type: float, computed: true, indexed: true, min: 0, max: 100 }
```

Extension fields can't be `required` (existing entities, and the owning
plugin, wouldn't have them), can't reuse a name the type already has, and
`core.Entity` can't be extended.

## Fields

Every field has a `type`, and may have `label`, `description`, `required`,
`immutable` (set once, never changed), `computed` (set only by actions),
`indexed` (a database index for fast filtering and sorting; indexed strings
are also fast to search for text they contain), `default` and
`removed`.

A field is never deleted, because its history is kept. To stop using one,
mark it `removed: true`: its column is dropped, and its values stay in the
type's history. Identity fields can't be removed.

| Type       | Options                                         | Notes                                                                      |
| ---------- | ----------------------------------------------- | -------------------------------------------------------------------------- |
| `string`   | `format`, `pattern`, `min_length`, `max_length` | One line. Never empty unless `min_length: 0`. Indexed: `max_length` ≤ 600. |
| `text`     | `max_length`                                    | Long text. Not indexable: searched by its words (full text) instead.       |
| `int`      | `min`, `max`                                    | 64-bit.                                                                    |
| `float`    | `min`, `max`                                    | Double precision.                                                          |
| `bool`     |                                                 |                                                                            |
| `enum`     | `values` (required)                             | Letters, digits and `_ . + -`.                                             |
| `date`     |                                                 | Defaults are written `YYYY-MM-DD`.                                         |
| `datetime` |                                                 | Always with a time zone. No default.                                       |
| `quantity` | `unit` (required), `display_unit`, `min`, `max` | Stored in `unit`, a UCUM code. `min`/`max` are in that unit.               |
| `file`     | `media_types`, `max_size`                       | Such as `[application/json, image/*]` and `2GiB` (at most 5 TiB).          |
| `ref`      | `to` (required)                                 | Another entity, of type `to` or its subtypes; `core.Entity` for any.       |
| `list`     | `items` (required), `min_items`, `max_items`    | `items` is a type and its options, without the flags. No lists of lists.   |
| `json`     | `schema`                                        | Opt-in. `schema` is a JSON Schema the value must match. Not indexable.     |

String formats, checked by the database too: `dna_sequence`,
`rna_sequence`, `protein_sequence` (IUPAC codes, either case), `email` and
`uri`. A `pattern` must work in both Python and PostgreSQL, so it can't use
lookarounds, named groups, backreferences, inline flags or possessive
quantifiers.

Field names are lower case with single underscores. Names every entity
already has (`id`, `hrid`, `type`, `project`, `created_at`, `revision`,
`archived_at` and the like), names ending in `_ptr`, Python keywords and
Django model methods are reserved.

## Tables

`iki plugin build` compiles a plugin into a Django app with these tables
(see [ADR 0033](../docs/adr/0033-plugin-codegen-and-migrations.md)). Every
name must fit PostgreSQL's 63 characters.

| Table                                            | Holds                                                     |
| ------------------------------------------------ | --------------------------------------------------------- |
| `protein_protein`                                | `protein.Protein`'s own fields, keyed by `entity_id`      |
| `protein_protein__history`                       | Its values at each revision, removed fields included      |
| `protein_proteinstructure__chains`               | A list of references or files: one row per item, in order |
| `structure_prediction__protein_proteinstructure` | The fields `structure_prediction` adds to that type       |

Other lists are arrays. PostgreSQL checks every rule it can (formats,
patterns, enums, bounds, lengths, list sizes) with CHECK constraints, so bad
data is refused however it is written. History tables are append-only, for
every role.

At deploy time, `iki-server sync_plugins` creates each prefix's HRID
sequence, comments every table and column from the manifests, and builds a
read-only view per type in the `iki_views` schema, such as
`iki_views.protein_protein`. Each view joins the type's fields, its parents'
and those other plugins add. Plugins installed together can't share an HRID
prefix, a table, or a field they add to the same type.

## Units

Quantities use [UCUM](https://ucum.org) codes, the units standard of lab
systems, HL7 and FHIR. They are case-sensitive: `mg` is a milligram, `Mg` a
megagram. Some codes people expect are spelled differently:

| You might write | UCUM                          |
| --------------- | ----------------------------- |
| `Da`, `kDa`     | `u`, `ku` (or `g/mol`)        |
| `°C`            | `Cel`                         |
| `µg`            | `ug`                          |
| `mM`, `nM`      | `mmol/L`, `nmol/L`            |
| `hr`            | `h`                           |
| `IU`, `ppm`     | `[IU]`, `[ppm]`               |
| `bp`            | `{bp}` (a counted annotation) |

The validator suggests these. Special units (`Cel`, `[degF]`, `[pH]`,
decibels) can't be multiplied, divided or raised to a power; use `K` for
temperature differences and rates.

Iki reads the official UCUM table (version 2.2), which ships unmodified in
[`src/iki_spec/units/`](src/iki_spec/units/) under the
[UCUM License](src/iki_spec/units/UCUM-LICENSE.md). The parser and
converter are Iki's own (Apache-2.0).

## Settings

```yaml
settings:
  ncbi_email: { type: string, format: email, required: true, description: "…" }
  batch_size: { type: int, min: 1, default: 100 }
```

Settings take `string`, `text`, `int`, `float`, `bool`, `enum` and `list`
(of those), with the same options as fields. A setting is either `required`
or has a `default`. Each deployment sets the values in its config
repository; check them with `iki plugin validate --settings FILE`. Settings
aren't secret, so names that look like secrets (`api_key`, `password`,
`token`) get a warning: declare secrets on the actions that use them.

## Dependencies

A plugin lists the plugins whose types it uses, with the schema versions it
works with. To check those references, pass the dependencies' directories
with `--with`. Without them, the validator says it can't check them rather
than guessing. `core.Entity` needs no dependency.

## The core plugin

Every deployment has the core plugin, released with Iki. Its manifests are
in [`iki_spec/core`](src/iki_spec/core). Other plugins use its types, such as
`core.Entity`, without declaring a dependency, and no other plugin can be
named `core`.

## A deployment's lockfile

Each deployment's config repository pins the plugins it installs in
`plugins.lock.yaml` ([schema](src/iki_spec/schemas/plugin-lock/v1.json)):

```yaml
apiVersion: iki/v1
kind: PluginLock
plugins:
  - name: dna
    git: https://github.com/acme/iki-plugins.git
    path: dna # the plugin's directory in the repository, if not its root
    sha: 4f1c2b9e0d6a7c3b8e5f1a2d9c0b7e6f5a4d3c2b
    state: active
    settings: settings/dna.yaml # relative to the lockfile
```

`sha` is a full commit, never a branch or tag, so the same lockfile always
installs the same plugins. `git ls-remote <repository> <tag>` prints a
tag's commit. In development, `local: ../path/to/plugin` takes a directory
instead. `iki deploy build` builds the image's plugins from the lockfile
(see [`cli/`](../cli/README.md) and ADR 0034).

## YAML

Manifests are read as YAML 1.2: only `true` and `false` are booleans, so
`no`, `on` and `yes` stay text, and `010` is ten. Duplicate keys, aliases
(`*name`), merge keys (`<<`), tags (`!!python/…`) and files over 1 MiB are
refused, so what is reviewed is what Iki reads.

## Problem codes

Every problem has a stable `code`, shown in brackets and in `--format json`:

| Code                                                                                                                                                                                                         | What's wrong                    |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------- |
| `yaml-syntax`, `yaml-duplicate-key`, `yaml-alias`, `yaml-merge-key`, `yaml-tag`, `yaml-key`                                                                                                                  | The YAML itself                 |
| `file-too-large`, `not-utf8`, `unreadable-file`, `symlink`, `not-a-mapping`                                                                                                                                  | The file                        |
| `missing-plugin-manifest`, `duplicate-plugin-manifest`, `wrong-file`, `not-a-directory`                                                                                                                      | Where manifests are             |
| `missing-api-version`, `unsupported-api-version`, `missing-kind`, `unknown-kind`                                                                                                                             | A manifest's header             |
| `schema`                                                                                                                                                                                                     | Anything the JSON Schemas catch |
| `reserved-name`, `name-too-long`, `duplicate-table`, `column-conflict`                                                                                                                                       | Names                           |
| `duplicate-type`, `unknown-type`, `extends-cycle`, `field-shadows-parent`                                                                                                                                    | Types                           |
| `missing-hrid`, `hrid-on-abstract`, `duplicate-hrid-prefix`                                                                                                                                                  | HRIDs                           |
| `unknown-field`, `identity-type`, `identity-not-fixed`, `identity-removed`, `on-duplicate-without-identity`                                                                                                  | Identities                      |
| `computed-with-default`, `computed-required`, `invalid-default`, `min-above-max`, `invalid-pattern`                                                                                                          | Field options                   |
| `unsupported-index`, `index-needs-max-length`                                                                                                                                                                | Indexes                         |
| `unknown-unit`, `display-unit-mismatch`                                                                                                                                                                      | Units                           |
| `file-too-large`, `invalid-json-schema`, `display-title`                                                                                                                                                     | Files, JSON fields and titles   |
| `extends-builtin`, `extension-of-own-type`, `duplicate-extension`, `extension-field-conflict`, `extension-field-required`                                                                                    | Extensions                      |
| `undeclared-dependency`, `dependency-not-available`, `dependency-invalid`, `dependency-is-self`, `dependency-cycle`, `dependency-version`, `dependency-unpinned` (warning)                                   | Dependencies                    |
| `required-with-default`, `setting-looks-secret` (warning), `unknown-setting`, `missing-setting`, `invalid-setting`, `settings-file`                                                                          | Settings                        |
| `missing-lockfile`, `not-a-lockfile`, `lockfile-documents`, `duplicate-plugin`, `core-is-builtin`, `missing-source`, `mixed-source`, `missing-sha`, `path-outside`, `absolute-path`, `missing-settings-file` | Lockfiles                       |
