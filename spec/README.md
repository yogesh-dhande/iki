# iki-spec

The versioned specs for Iki plugins, and the Python package that reads and
checks them. Version `iki/v1` covers plugins, entity types, extensions,
fields, units, settings, actions, environments and workflows; the run
protocol, triggers and components arrive in later versions of this document
as they land.

- **JSON Schemas** (draft 2020-12), in [`src/iki_spec/schemas/`](src/iki_spec/schemas/).
  Each `<name>/v1.json` is published at `https://ikisci.org/schemas/<name>/v1`.
  Use [`manifest/v1`](src/iki_spec/schemas/manifest/v1.json) for any manifest;
  it picks the right schema by `kind`.
- **The validator** behind `iki plugin validate` (see [`cli/`](../cli/README.md)),
  which also checks what a schema can't: units, references across files and
  plugins, table and index rules, identities, settings, and what actions read,
  write and run in.
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
actions/             # kind: Action
environments/        # kind: Environment
workflows/           # kind: Workflow
triggers/            # kind: Trigger
components/          # kind: Component, with each one's JavaScript module
migrations/          # made by `iki plugin makemigrations`, committed and reviewed
```

Every manifest starts with `apiVersion: iki/v1` and a `kind`.

```yaml
apiVersion: iki/v1
kind: Plugin
name: acme/proteins # namespace/name; types are acme/proteins.Name, tables acme__proteins_name
version: 3.0.0 # semantic version, for people (deployments pin the git commit)
iki: ">=0.1,<0.3" # the Iki releases it works with: others refuse it
title: Proteins
license: Apache-2.0
dependencies:
  iki/dna:
    types:
      DNASequence: ">=2,<4" # schema versions this plugin works with
settings:
  uniprot_mirror: { type: string, format: uri, default: "https://rest.uniprot.org" }
```

### Names

A plugin is named `namespace/name`, as Terraform's providers and GitHub's
actions are, so two labs' plugins can have the same short name and still be
installed together: `acme/assays` and `globex/assays` are different plugins
([ADR 0113](../docs/adr/0113-plugins-are-named-in-a-namespace.md)). Both
parts are 2 to 30 lower-case letters, digits and single underscores, starting
with a letter. The namespace is your lab's or company's.

- The namespace `iki` is Iki's own: `iki/core`, `iki/dna`, `iki/protein`,
  `iki/antibody`, `iki/small_molecule` and `iki/example_lims`. No other
  plugin may use it, or a namespace starting with `iki_`
  (`reserved-namespace`), and a lockfile takes an `iki/...` plugin only from
  Iki's repository.
- A plugin names its own types, actions and the rest without its namespace
  (`Protein`, or in full, `acme/proteins.Protein`). Anything of another
  plugin's is named in full: `iki/dna.DNASequence`, `iki/protein.translate`,
  and dependencies by their full names. A short name for another plugin's
  (`dna.DNASequence`) is refused, with what to write instead
  (`unqualified-reference`, `unqualified-dependency`).
- Where a name can't hold a `/`, the plugin goes by its label,
  `namespace__name` (`iki__protein`): its Django app and tables, its
  package `iki_plugin_iki__protein`, its code's directory in a worker's
  bundle, and its secrets' keys (`iki__dna.NCBI_API_KEY`).

### Lab roles

A plugin can declare lab roles: who may decide something in a lab, such as
reviewing an analyst's results or releasing a sample
([ADR 0116](../docs/adr/0116-lab-roles-are-declared-by-plugins-and-given-per-project.md)).
A project's Admins give them to its members, people or teams, beside their
project role. A lab role grants no access of its own: it's what a workflow
approval, a record's transition or a signature can require.

```yaml
roles:
  reviewer:
    label: Reviewer
    description: Checks another analyst's results, and approves them or sends them back.
  qa:
    label: QA
    description: Releases reviewed samples, and reopens released ones when they need correcting.
```

Names are lower-case letters, digits and single underscores; `viewer`,
`editor`, `runner` and `admin` are the project roles'. A role is known in
full as `namespace/plugin.name` (`iki/example_lims.qa`). A plugin names its
own by the short name, and another plugin's in full, which needs that plugin
under `dependencies` (`undeclared-dependency`), so several plugins can share
one QA role. Naming a role nobody declares is `unknown-role`; two roles with
the same label are a warning, `duplicate-role-label`.

## Entity types

```yaml
apiVersion: iki/v1
kind: EntityType
name: Protein
schema_version: 3 # increase it whenever the fields change
extends: iki/dna.Sequence # optional parent type (multi-table inheritance)
hrid: { prefix: PRT, digits: 6 } # PRT-000123, never reused
identity: [sequence] # what makes two entities the same: registered once
fields:
  sequence: { type: string, format: protein_sequence, required: true, immutable: true }
  molecular_weight:
    { type: quantity, unit: u, display_unit: ku, computed: true, indexed: true, registry: derived }
  uniprot_accession: { type: string, pattern: "^[A-Z0-9]{6,10}$", registry: fact }
  organism: { type: string, max_length: 200, indexed: true } # each project's own
  source: { type: ref, to: iki/core.Entity } # any entity
display:
  title: "{{ name or hrid }}"
```

| Key                     | Meaning                                                                                                  |
| ----------------------- | -------------------------------------------------------------------------------------------------------- |
| `name`                  | PascalCase. Elsewhere the type is `plugin.Name`; in the same plugin, `Name` is enough.                   |
| `label`, `label_plural` | What people see. Default: the name split into words, and that plus "s".                                  |
| `schema_version`        | A whole number from 1. Dependencies and actions name the versions they work with.                        |
| `extends`               | A parent type. Entities of this type are also entities of the parent, with its fields.                   |
| `abstract`              | Only subtypes have entities. Abstract types have no `hrid`; every concrete type must have one.           |
| `hrid`                  | Human-readable IDs: a 2–8 character prefix (capitals and digits), zero-padded to `digits` (3–12).        |
| `identity`              | Fields that make an entity the same as another. Each must be `immutable`, and one `required`. See below. |
| `display.title`         | Text with `{{ a or b or "text" }}` parts, naming fields or `hrid`.                                       |
| `display.viewers`       | Iki's own viewers, each a tab on its entities' pages, configured by naming fields. See Viewers.          |

### Identities and registries

A type with an identity is registered (ADR 0115). Each identity is one
**registry entry**: its identity fields, the fields marked `registry`, and its
citable ID, such as `CPD-000123`. The entry belongs to no project. Each project
that has the entity has its own **record** of the entry, with the type's other
fields, which it keeps, revises and archives on its own; the record shares the
entry's ID. Creating an entity whose identity is registered already gives the
project a record of that entry, telling the creator only that it exists and its
ID: never who registered it, when, or which projects have it. A project has one
record of each entry. A type without an identity has no entries: its entities
are its project's.

The registry is the deployment's boundary of identity: every project is in
one, and an organization that keeps some programs apart (a CRO's clients, say)
has more than one. References to a type with an identity hold its entry, and
a project refers only to entries it has a record of.

A field marked `registry` is the entry's, the same for every project:

- `registry: derived` comes from the identity alone, such as a compound's
  molecular weight from its structure, usually `computed`. It's set once, by
  whoever registers the entity or the action that computes it, and then never
  changes: registering the identity again with another value keeps the entry's.
- `registry: fact` is curated, such as an INN or a UniProt accession: an enum,
  a number, a date, a reference to another entry, or a string or list of
  strings with a `pattern` (never free text). Whoever registers it may set it
  while it's empty; after that only the registry's registrars change it, with a
  reason, audited and told to every project with a record of it.

Only the type that declares the identity, or an extension of it, has registry
fields, and they refer only to other entries. Everything else (an organism, a
project's notes, a LIMS ID) is each project's own.

Iki stores a hash of each entry's identity values, to find it again.
Subtypes share the identity of the type that declares it, and its entries. The hash is the
SHA-256, in lower-case hex, of UTF-8 JSON with sorted keys and no spaces:

```json
{ "type": "iki/dna.DNASequence", "v": 1, "values": { "bases": "ACGT" } }
```

Here written with spaces for reading; the hashed form is
`{"type":"iki/dna.DNASequence","v":1,"values":{"bases":"ACGT"}}`. `type` is the
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

An optional field without a value is `null`. An antibody is its heavy chain
and its light chain, which a single-domain antibody hasn't got:
`{"type":"iki/antibody.Antibody","v":1,"values":{"heavy_chain":"…","light_chain":null}}`.
Every identity has at least one required field.

[`iki_spec.identity`](src/iki_spec/identity.py) implements it.

### States

A type can declare the states its records move through, and the transitions
between them, each limited to lab roles
([ADR 0118](../docs/adr/0118-a-record-moves-through-its-types-states.md)):

```yaml
states:
  received: { label: Received, initial: true }
  in_testing: { label: In testing }
  in_review: { label: In review }
  approved: { label: Approved, locks: true }
  released: { label: Released, locks: true, description: Its results can be used. }
transitions:
  start_testing: { label: Start testing, from: [received], to: in_testing, roles: [analyst] }
  submit: { label: Submit for review, from: [in_testing], to: in_review, roles: [analyst] }
  approve: { label: Approve, from: [in_review], to: approved, roles: [reviewer], not_by: [submit] }
  send_back:
    label: Send back
    from: [in_review, approved]
    to: in_testing
    roles: [reviewer, qa]
    reason: required
  release:
    { label: Release, from: [approved], to: released, roles: [qa], not_by: [submit, approve] }
  reopen: { label: Reopen, from: [released], to: in_testing, roles: [qa], reason: required }
```

- A type has 2 to 30 states and up to 60 transitions, named as fields are.
  Exactly one is `initial`: every record starts there, and it can't lock
  (`state-initial`). Every state is reached from it (`state-unreachable`).
- Each transition leads from one or more states to another one
  (`transition-to-itself`), and names the lab roles that may make it, any one
  of them (`unknown-role`). `reason` asks whoever makes it why: `required` or
  `optional`.
- `not_by` names earlier transitions: whoever made the latest of each on a
  record can't make this one, so the analyst who submitted results doesn't
  approve them ([ADR 0119](../docs/adr/0119-a-transition-can-keep-its-maker-apart-from-earlier-ones.md)).
- A state that `locks` keeps its records from changing, by any path, until
  a transition takes them out of it
  ([ADR 0120](../docs/adr/0120-a-state-can-lock-a-record-and-the-database-holds-the-lock.md)).
- A state can't be taken out of a manifest once records may be in it: it's
  kept, `removed: true`, with `moved_to:` the state its records move to,
  which can't be removed itself (`unknown-state`). A removed state doesn't
  lock (`state-removed`), and no transition leads to or from it
  ([ADR 0121](../docs/adr/0121-a-plugin-cant-strand-records-in-a-state-it-removes.md)).
- States are declared on a type whose ancestors have none
  (`states-inherited`): its subtypes go through the same ones. Fields named
  `iki_…` are reserved (`reserved-name`): a record's state is the column
  `iki_state` of its declaring type's table.

### People in records

A field can name people: `type: person` names one, and a list with
`items: { type: person }` several, each once
([ADR 0122](../docs/adr/0122-records-name-people-and-each-person-has-their-work.md)).
Only someone who could do the work can be named: an active person (not a
service account) who can read the record's project, holding the field's
lab `role` there if it has one. The API takes a person's ID or email
address and gives back their ID, with their name in the record's
`people`.

An `assignment` field assigns the record to whomever it names: it's on
their My work, and they're told. `assignment: true` counts in every
state; `assignment: { states: [...] }` only in those, so a reviewer has a
sample only while it's in review. `due:` names a date or datetime field
My work sorts by. A state that locks a record locks whom it names too,
so name whoever has it next before it gets there.

```yaml
fields:
  due_on: { type: date, label: Due }
  analyst: { type: person, role: analyst, assignment: { states: [received, in_testing] } }
  reviewer: { type: person, role: reviewer, assignment: { states: [in_review] } }
  witnesses: { type: list, items: { type: person }, max_items: 5 }
due: due_on
```

- `role` names a lab role, the plugin's own or a dependency's
  (`unknown-role`).
- Only a field naming people can be an `assignment`
  (`assignment-not-people`), and its `states` are its type's
  (`unknown-state`); a type without states uses `assignment: true`
  (`assignment-without-states`).
- `due` is a `date` or `datetime` field of the type's
  (`unknown-field`, `due-not-a-date`), and not a registry's
  (`due-in-registry`).
- People are each project's: a person field can't be a registry's
  (`person-in-registry`).
- Someone who leaves the project, loses the role or is deactivated stays
  named, as the history must say; the record needs reassigning, and the
  project's Admins are told.

### Extensions

A plugin adds fields to another plugin's type with an extension. The fields
live in the extending plugin's own table
(`acme__prediction__iki__protein_proteinstructure`, for `acme/prediction`'s fields
on `iki/protein.ProteinStructure`).

```yaml
apiVersion: iki/v1
kind: EntityTypeExtension
extends_type: iki/protein.ProteinStructure
fields:
  plddt_mean: { type: float, computed: true, indexed: true, min: 0, max: 100 }
```

Extension fields can't be `required` (existing entities, and the owning
plugin, wouldn't have them), can't reuse a name the type already has, and
`iki/core.Entity` can't be extended.

## Fields

Every field has a `type`, and may have `label`, `description`, `required`,
`immutable` (set once, never changed), `computed` (set only by actions),
`indexed` (a database index for fast filtering and sorting; indexed strings
are also fast to search for text they contain), `default` and
`removed`.

A string field that's indexed may also be `unique`: no two entities in a
project have the same value (no two entries in a registry, for a field marked
`registry`), such as the ID a LIMS or ELN gives the record
it keeps for an entity (ADR 0082). The database refuses a second one,
whatever writes it. Another project may have an entity with the same value,
since projects are kept apart. A field can be made unique after entities
have values: the deploy fills them in, and stops, naming them, if any in a
project share one.

A string field may `link` to where its value leads in another system, such
as the record in the LIMS. `{value}` is the value, and `{settings.name}` one
of the plugin's settings, so each deployment can point at its own LIMS. A
link starts with `https://` or a setting, and Iki shows only links that are
https once the settings are filled in. The API, SDK and MCP look entities
up by a unique field's value.

```yaml
lims_id:
  type: string
  max_length: 64
  indexed: true
  unique: true
  link: "{settings.lims_url}/samples/{value}"
```

A field is never deleted, because its history is kept. To stop using one,
mark it `removed: true`: its column is dropped, and its values stay in the
type's history. Identity fields can't be removed.

| Type       | Options                                         | Notes                                                                         |
| ---------- | ----------------------------------------------- | ----------------------------------------------------------------------------- |
| `string`   | `format`, `pattern`, `min_length`, `max_length` | One line. Never empty unless `min_length: 0`. Indexed: `max_length` ≤ 600.    |
| `text`     | `max_length`                                    | Long text. Not indexable: searched by its words (full text) instead.          |
| `int`      | `min`, `max`                                    | 64-bit.                                                                       |
| `float`    | `min`, `max`                                    | Double precision.                                                             |
| `bool`     |                                                 |                                                                               |
| `enum`     | `values` (required)                             | Letters, digits and `_ . + -`.                                                |
| `date`     |                                                 | Defaults are written `YYYY-MM-DD`.                                            |
| `datetime` |                                                 | Always with a time zone. No default.                                          |
| `quantity` | `unit` (required), `display_unit`, `min`, `max` | Stored in `unit`, a UCUM code. `min`/`max` are in that unit.                  |
| `file`     | `media_types`, `max_size`                       | Such as `[application/json, image/*]` and `2GiB` (at most 5 TiB).             |
| `ref`      | `to` (required)                                 | Another entity, of type `to` or its subtypes; `iki/core.Entity` for any.      |
| `person`   | `role`, `assignment`                            | Someone in the record's project. See [People in records](#people-in-records). |
| `list`     | `items` (required), `min_items`, `max_items`    | `items` is a type and its options, without the flags. No lists of lists.      |
| `json`     | `schema`                                        | Opt-in. `schema` is a JSON Schema the value must match. Not indexable.        |

String formats, checked by the database too: `dna_sequence`,
`rna_sequence`, `protein_sequence` (IUPAC codes, either case), `email`,
`uri` and `hostname` (a DNS name in lower case, such as `lims.example.org`). A `pattern` must work in both Python and PostgreSQL, so it can't use
lookarounds, named groups, backreferences, inline flags or possessive
quantifiers.

Field names are lower case with single underscores. Names every entity
already has (`id`, `hrid`, `type`, `project`, `created_at`, `revision`,
`archived_at` and the like), names ending in `_ptr`, Python keywords,
Django model methods, and `models`, `datetime` and `checks`, which a
plugin's generated models use, are reserved.

## Tables

`iki plugin build` compiles a plugin into a Django app with these tables
(see [ADR 0033](../docs/adr/0033-plugin-codegen-and-migrations.md)). Every
name must fit PostgreSQL's 63 characters. Each starts with the plugin's
label, `namespace__name`, so a table says whose it is.

| Table                                             | Holds                                                     |
| ------------------------------------------------- | --------------------------------------------------------- |
| `iki__protein_protein`                            | `iki/protein.Protein`'s own fields, keyed by `entity_id`  |
| `iki__protein_protein__history`                   | Its values at each revision, removed fields included      |
| `iki__protein_proteinstructure__chains`           | A list of references or files: one row per item, in order |
| `acme__prediction__iki__protein_proteinstructure` | The fields `acme/prediction` adds to that type            |

Other lists are arrays. PostgreSQL checks every rule it can (formats,
patterns, enums, bounds, lengths, list sizes) with CHECK constraints, so bad
data is refused however it is written. History tables are append-only, for
every role.

At deploy time, `iki-server sync_plugins` creates each prefix's HRID
sequence, comments every table and column from the manifests, and builds a
read-only view per type in the `iki_views` schema, such as
`iki_views.iki__protein_protein`. Each view joins the type's fields, its parents'
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

## Actions

An action is something a plugin does to entities: compute their properties,
create new entities from them, or import entities from elsewhere. Each run
records what it read, what it wrote, and the code and machine it ran on.

```yaml
apiVersion: iki/v1
kind: Action
name: predict_structure_with_alphafold
inputs: # leave out for actions that import or fetch
  protein: { type: iki/protein.Protein, schema: ">=2" } # cardinality: one (default) or many
  # include: [encoded_by] # references whose entities the run reads too (ADR 0105)
params:
  model_preset: { type: enum, values: [monomer, multimer], default: monomer }
effects: # what a run may write; anything else is refused
  creates: [{ type: iki/protein.ProteinStructure, cardinality: many, derived_from: [protein] }]
  updates: [] # e.g. [{ input: protein, fields: [molecular_weight] }]
runs_on: { tier: vm, environment: alphafold-gpu, lifecycle: ephemeral, resources: { memory: 96Gi } }
command: [python, /app/run.py] # a list of arguments, no shell; ./path is a file in the plugin
deterministic: true # the same inputs give the same results, so runs can be reused
checkpointable: true # saves progress in $IKI_CHECKPOINT_DIR and resumes
batchable: 1 # more than 1 packs up to that many runs of a batch into one job (one input only)
artifacts: { keep: ["msas/**", ranking_debug.json], retention: 365d }
network: [] # hosts a run may reach; none by default
software: [{ name: AlphaFold, version: 2.3.2 }] # what its runs make entities with (ADR 0106)
metrics: # numbers its runs report under `metrics`, which workflow conditions read
  mean_plddt: { label: Mean pLDDT, description: The model's confidence, from 0 to 100. }
secrets: [] # e.g. [LIMS_TOKEN, { name: NCBI_API_KEY, required: false, description: ... }]
timeout: 8h # 1h by default, at most 7d
retries: { max: 2, on: [worker_lost, interrupted] }
```

An action that several tools could do names the one it uses:
`predict_structure_with_alphafold` and `predict_structure_with_immunebuilder`,
never a `predict_structure` whose model goes unnamed. Each tool has its own
inputs, params, environment, tier and licence terms, so each is its own action:
an admin can allow one and not another, and the action a structure's run used
says what made it. A choice among one tool's own models, as `model_preset` is,
is a param.

The tiers are `self_hosted` (machines a compute admin registered for a team,
such as an instrument's PC or a licensed workstation, matched to runs by their
labels), `backend` (Iki's isolated worker pods), `container` (the plugin's
image on Kubernetes) and `vm`. On the
container and vm tiers, `lifecycle` says what machine a run needs: `ephemeral`
(the default), a fresh one that runs nothing else, or `persistent`, one that
runs one job after another and is kept warm, for quick actions that run often.
A persistent machine starts each run clean, but other projects' runs have run
on it. The validator checks that:

- inputs, created types and updated fields exist, and the fields aren't
  removed or `immutable`; updates reach inherited fields and fields this
  plugin adds to the type
- every `derived_from` and `updates` names an input
- an input's `include` names references of its type, its own or a parent's
- a create's `match_on` is a unique field of the type, its own, a parent's,
  or one this plugin's extension adds
- params follow the rules for settings, and also take `date`, `quantity`
  and `file` (one file, such as one to import, with `media_types` and
  `max_size`; never a default, and never a reference, which is an input)
- the environment is one of this plugin's and runs on the action's tier
- self-hosted actions declare no `secrets` or `network`, since Iki can't keep
  the deployment's secrets from, or limit, a machine its team looks after
- only the self-hosted, container and vm tiers ask for GPUs
- files the command names with `./` are in the plugin, and aren't symbolic links

A run's input carries references as IDs. An input can `include` some of its
type's references, or lists of them, and the run reads the entities they name
too, under each input entity's `included`, by reference: an antibody's
`heavy_chain` and `light_chain`, or a structure's `chains`. Like the inputs,
they're pinned when the run is launched, at the revisions they have then, and
the run keeps them. Whoever launches it must be able to read them, and they
must be in its project, which the launch can add them to as it does inputs.
They count in what makes two runs identical, so a change to one runs it again.
Only the input's own references are included, not theirs.

A run keeps as artifacts the files in its output directory (`$IKI_OUT_DIR`)
that one of the `artifacts.keep` patterns matches, whether the command
succeeded or not, for people to download from the run's page. Patterns match
a path relative to that directory one segment at a time: `*` and `?` within
a segment, `[...]` a set of characters, and a `**` segment any number of
segments. Each attempt keeps at most 1,000. They're deleted once
`retention` is over (the deployment's default if it's left out, 90 days
unless it says otherwise, and never less than its minimum). Batchable
actions can't keep artifacts.

A run gets the secrets its action declares under `secrets` as environment
variables, and no others. Each deployment keeps them in its own secret
manager, for this plugin alone (`iki__dna.NCBI_API_KEY` is the `iki/dna` plugin's), so a
secret is never another plugin's even under the same name. Their values are
never in `input.json`, and `***` stands for them in the log. A required secret
the deployment hasn't set keeps the action from running, and says so; give
optional ones (`required: false`) a `description` that tells administrators
what they're for and where to get one.

A run reaches the network only if its action names the hosts under
`network` (`*.example.com` covers subdomains, not example.com itself), and
only over HTTPS on port 443, through the deployment's egress proxy. The
command finds the proxy, with its attempt's credentials, in `HTTPS_PROXY`,
which Python's `urllib`, `requests`, `httpx` and `curl` use as they are.
Hosts it doesn't name, plain HTTP and private addresses are refused, and the
run's page lists each host an attempt reached or was refused. On a deployment
without the proxy, such actions can't run.

When the host differs between deployments, as a LIMS's does, name a setting
instead: `network: ["{settings.lims_host}"]`, where `lims_host` is a string
setting with `format: hostname`. A deployment's build fills in the host it
sets, so the manifest the server reads, and what the run's page shows, names
the real host; a setting left unset reaches nothing.

Runs whose machine went away (`worker_lost`, `interrupted`) are retried twice
by default. Actions that reach the network aren't retried unless they say so,
since a retry could repeat what they did elsewhere, such as placing an order.

## Environments

An environment is what a plugin's actions need, wherever they run. Each
deployment binds it to compute templates of its own.

```yaml
apiVersion: iki/v1
kind: Environment
name: alphafold-gpu # kebab-case
tiers: [vm, container]
resources: { cpu: 8, memory: 64Gi, gpu: { count: 1, min_memory: 40Gi }, scratch_disk: 200Gi }
image: ghcr.io/acme/alphafold@sha256:9f86d0… # by digest, for container and vm
volumes:
  af-db:
    size: 3Ti
    access: read_only_many # or read_write_once
    performance: high_iops # standard, high_iops or high_throughput
    populated_by: setup
    mount: /data/af
setup: { script: setup/download_dbs.sh, timeout: 12h, requires_approval: true }
```

An environment on the self-hosted tier names the `labels` a machine needs
to run it, such as `labels: [chromeleon]` for the software its actions
drive. A self-hosted run goes only to a machine with every label it needs
(ADR 0112). Name what the machine must have, not where it is: each
deployment adds its own labels in its binding.

The backend tier builds a Python or conda environment from a lockfile in the
plugin; the self-hosted tier does too, or runs the image with Docker or Podman:

```yaml
python: { version: "3.12", requirements: requirements.txt }
# or
conda: { lockfile: conda.lock }
```

Everything an environment installs is pinned, so a run can be reproduced
exactly: images by digest, every Python package with `==` and its
`--hash=sha256:` (as `uv pip compile --generate-hashes` writes them), and
conda packages in an explicit lockfile with their hashes. Requirements can't
include other files or editable packages. Volumes mount on the container and
vm tiers only, at absolute paths that don't overlap.

## Workflows

A workflow runs several of the plugin's actions as one (ADR 0079). Each step
runs an action, by its name in the plugin, and says where each of its inputs
comes from: the workflow's input (`$inputs.<name>`), the entities a step
created of a type (`$steps.<id>.created.<Type>`), or what a step was given
(`$steps.<id>.inputs.<name>`), once that step has run. A param takes a value,
or the workflow's param (`$params.<name>`); a value that starts with `$` is
written `$$`.

```yaml
apiVersion: iki/v1
kind: Workflow
name: translate_and_characterize # snake_case; qualified as iki/protein.translate_and_characterize
label: Translate and characterize
inputs:
  sequence: { type: iki/dna.DNASequence, schema: ">=1" } # as an action's inputs
params:
  frame: { type: int, min: 1, max: 3, default: 1 } # as an action's params
steps:
  translate:
    action: translate
    inputs: { sequence: $inputs.sequence }
    params: { frame: $params.frame }
  properties:
    action: compute_properties
    inputs: { protein: $steps.translate.created.Protein }
    # needs: [other_step]  # wait for a step it doesn't take from
```

A step starts once the steps it takes from, and those it `needs`, have
succeeded; steps that don't wait for each other run side by side.

A step with `each: <input>` runs once for each entity its source gives that
input, as a batch (up to 1,000 runs), and succeeds once every run has. What
it then created, and what it was given for that input, are many: a later
step takes them all where its input takes many (fan-in), or runs once for
each of them in turn.

```yaml
steps:
  import:
    action: import_proteins # creates many proteins
    params: { file: $params.file }
  properties:
    action: compute_properties # takes one protein
    each: protein
    inputs: { protein: $steps.import.created.Protein }
```

A step with `when:` runs only if all its conditions hold, checked once the
steps it starts after have succeeded. Each compares a `value`, the workflow's
param (`$params.<name>`) or a number a step's run reported
(`$steps.<id>.metrics.<name>`, from a step that runs once), with one of `is`,
`is_not`, `in` (a list), `at_least`, `at_most`, `above` or `below`. The
operand is a value, or the workflow's param. A step whose conditions don't
hold is skipped, and so is every step after it; the workflow still succeeds. A
param left unset holds no condition. A metric its run didn't report fails the
step: whether it should have run isn't known.

```yaml
steps:
  align:
    action: align_pair # declares metrics: { identity_percent: ... }
    inputs: { query: $inputs.query, target: $inputs.target }
  characterize:
    action: compute_properties
    inputs: { protein: $inputs.target }
    when:
      - { value: $steps.align.metrics.identity_percent, at_least: $params.min_identity }
```

A step with `approval:` waits, once its turn comes and its conditions hold,
for a person to approve it: someone with `role` in the project, other than
whoever started the workflow, in the web app. The role is a project role
(`viewer`, `editor` by default, `runner` or `admin`), meaning that or above,
or a [lab role](#lab-roles) (`qa`, or another plugin's in full), meaning
someone who holds it; being an Admin doesn't. They're told, and see the `instructions`. Approving
starts the step as whoever started the workflow; declining it, with a reason,
fails the workflow, as does nobody deciding within 30 days.

```yaml
steps:
  characterize:
    action: compute_properties
    inputs: { protein: $inputs.target }
    needs: [align]
    approval: { role: admin, instructions: Check the alignment covers the kinase domain. }
```

The build checks each step against its action: every input it needs comes from
somewhere, of a type it takes and, where it takes one entity, not from
something that can be many; every required param is given, each value fits,
and a workflow param given to a step's param takes no value the step's
doesn't. A condition reads a metric its step's action declares, and compares
it, or a param, with a value of its type, or a param of the same type; the
ordering comparisons take numbers. No step may come before itself.

## Triggers

A trigger runs one of the plugin's actions by itself when an entity of a type
(or a subtype) is created (`entity.created`, for each project's new record),
registered (`entity.registered`, once for each new registry entry of a type
with an identity, by the record that registered it: ADR 0115) or changed
(`entity.updated`), on that entity (ADR 0080). Computing what's derived from
an identity belongs on `entity.registered`: the entry keeps it for every
project. Installing the plugin doesn't turn it on: a
project's Admins do, project by project, and its runs are theirs. They're
launched as the Admin who turned it on, recorded as started by the trigger,
and a run on the container or VM tier waits for that Admin's approval first.

```yaml
apiVersion: iki/v1
kind: Trigger
name: compute_new_protein_properties
label: Compute each new protein's properties
on: entity.registered
type: Protein
run: compute_properties # the plugin's action
input: protein # which input takes the entity; left out when only one can
params: {} # values; a string starting with `$` is written `$$`
```

On a type with states (ADR 0118), `entity.transitioned` runs it when a record
moves to another state; `states` narrows it to those entering one of them
(`trigger-transitioned-without-states`, `trigger-states-not-transitioned`).

```yaml
on: entity.transitioned
type: Sample
states: [released]
```

On `entity.updated`, `fields` narrows it to changes of those fields. A
change a triggered run makes never fires a trigger, so a trigger can't fire
itself, or another that fires it back: chain actions in a workflow instead.

```yaml
on: entity.updated
type: Protein
fields: [sequence, organism]
```

The build checks that the type exists and has the fields, that the action
takes an entity of the type in one input and needs no other, that its params
fit.

A trigger can run on a schedule instead, in cron's five fields (minute, hour,
day of the month, month, day of the week) and a timezone, UTC if left out
(ADR 0081). Its action must need no input; `params` give it what it takes.
It runs at most every 5 minutes, and never while its last run is still
going. An Admin turning it on picks the service account it runs as.

```yaml
apiVersion: iki/v1
kind: Trigger
name: fetch_hemoglobin_nightly
on: schedule
schedule: 0 3 * * * # 03:00 each day
timezone: Europe/Zurich
run: fetch_proteins
params: { accession: P69905 }
```

Each run's `input.json` has `run.trigger`, with `scheduled_for`, the time
it was for, and `since`, the time the last run that succeeded was for (null
the first time), so an import can fetch only what's new.

## Viewers

A type shows its entities in Iki's own viewers by naming the fields they
show, under `display.viewers`. Each is a tab on its entities' pages, and its
subtypes' too unless they declare their own. No plugin code runs: this is
the first of ADR 0084's tiers, configuring what Iki has.

```yaml
display:
  viewers:
    - { viewer: sequence, sequence: bases, features: features }
    - { viewer: structure, structure: model } # on a type with a structure file
```

On a protein type with a per-residue profile:

```yaml
display:
  viewers:
    - { viewer: sequence, sequence: residues, tracks: [{ field: hydropathy }] }
```

| Viewer      | Fields                                                                                                                                                                                                                                                                  |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sequence`  | `sequence`: a string with format `dna_sequence` or `protein_sequence`. `features` (optional): a json field of regions, each with `type`, `start`, `end`, `name`, `strand`. `tracks` (optional): json fields of one number per residue, each with an optional `label`.   |
| `structure` | `structure`: a file field whose `media_types` are `chemical/x-mmcif`, `chemical/x-pdb` or both.                                                                                                                                                                         |
| `alignment` | `alignment`: a file field whose `media_types` are FASTA (`chemical/seq-aa-fasta`, `chemical/seq-na-fasta`, `text/plain`). `rows` (optional): a list of references, the sequences headed by their HRIDs. `reference` (optional): a reference, the row pinned at the top. |

The sequence viewer shows the residues in rows of 60, in groups of 10, with
each feature as a bar under the residues it covers, coloured by its kind and
pointing the way its strand runs, and lists the features in a table. A
feature whose end comes before its start wraps the origin of a circular
sequence. Each track is drawn under the residues, a bar per residue above or
below a baseline, on a scale from its field's schema's `minimum` and
`maximum` if it gives them (ADR 0087). The structure viewer shows the file
in Mol\*, in a sandboxed frame with no network (ADR 0085). The alignment
viewer reads the file with iki-wasm in a Web Worker and draws what's in
view: the reference pinned, a consensus and how much each column agrees,
and the rows linked to the sequences they are, to pick and run an action
on (ADR 0088). `label` names the
tab. The validator checks that each field exists and is what its viewer
shows, and that each viewer is given once.

A viewer's tab lists the views it's in and saves what it shows as a
`iki/core.View`, on the revision it shows, with other entities beside it if
wanted (ADR 0086, ADR 0110).

## Components

A component is a piece of UI the plugin adds to Iki's pages, for what Iki's
own components can't show, such as a plate map where people pick wells. It
is a Web Component, in one JavaScript module with everything it needs
bundled in, and Iki runs it in a sandboxed frame: no network, no access to
the person's session or Iki's pages (ADR 0084). Iki gives it the data it
shows, with the viewer's permissions, and does what it asks for itself.

```yaml
apiVersion: iki/v1
kind: Component
name: plate_map
label: Plate map
module: ./components/plate-map.js # one file, up to 10 MiB
element: acme-plate-map # the custom element it defines
slots: [{ slot: entity.tab, type: Plate }] # a tab on each plate's page
events: { select: { type: Well } } # it lets people pick wells
```

`entity.tab` is a tab on the pages of entities of `type` and its subtypes,
called `label`, or the component's. The module defines the element with
`customElements.define`. Iki sets its `entity` property to the entity whose
page it's on, as the person may read it, and listens for `iki-select`
events, whose `detail.entities` lists the HRIDs of the entities picked, of
the type `events.select` declares. The page shows what was picked, among
what the person may read, and offers to run an action on them.

### Components that show many entities

A component can show many entities at once, such as a box map, a bookings
calendar or a pedigree, by declaring which it reads in `entities`: a type,
the fields it reads of each, and how many it may hold, at most 1000 (ADR
0117). It then fills more places than a tab, each with a search of its own
or one it's given:

```yaml
entities: { type: Booking, fields: [instrument, starts_at, ends_at], limit: 500 }
slots:
  - slot: entity.tab # an instrument's bookings, on its page
    type: Instrument
    search: { where: { field: instrument, op: eq, value: "{{ entity }}" }, sort: starts_at }
  - slot: search.results # "Show as" in the grid of bookings: the grid's own search
    label: Calendar
  - slot: project.page # a section of the project's page
    search: { sort: starts_at }
  - slot: person.home # a section of each person's home
    label: Your bookings
    search: { where: { field: created_by, op: eq, value: "{{ me }}" } }
```

| Slot             | What it's given                                                                       |
| ---------------- | ------------------------------------------------------------------------------------- |
| `entity.tab`     | the entity whose page it is; with a `search`, what that finds in the entity's project |
| `search.results` | what the grid's search finds, as the person set it; it declares no search             |
| `project.page`   | what its `search` finds in the project; the section shows once it finds something     |
| `person.home`    | what its `search` finds in every project the person reads; it names `{{ me }}`        |

A search is the API's, of `entities.type`: `where` conditions on its fields
and `hrid`, `created_by`, `created_at` and `updated_at`, at most 20, 4
levels deep, and a field to `sort` by. `{{ entity }}` is the entity whose
page it is, for a reference to its type; `{{ me }}` is whoever reads it, for
`created_by`. Iki's server runs it, as the person may read, and the element
gets the results as its `entities` property: `items`, each with `id`,
`hrid`, `title`, `type`, `revision`, `project`, the fields declared in
`values` and the entities those fields name in `refs`, a page at a time.
Its `place` property says which slot it's in. It may send `iki-more` for
the next page, `iki-refine` to narrow what it shows, with conditions on
only the fields it reads, and `iki-open` with an HRID it was given, which
Iki opens. It can't widen what its place shows, and it writes nothing:
picking entities offers Iki's run page, as on a tab.

The validator checks that the module is a text file in the plugin, not a
symbolic link and at most 10 MiB, that the types named exist, and that no
two components define the same element. It checks each search's fields,
operators, sort and context, and that a slot is declared once. The build
copies each module with the digest it had then, and the server checks it
before serving it.

## The Iki it works with

`iki:` lists the Iki releases a plugin works with, as comparisons with
release numbers, such as `">=0.1,<0.3"`
([ADR 0095](../docs/adr/0095-a-plugin-says-which-iki-it-works-with-and-versions-its-changes.md)).
On any other release, `iki plugin validate`, `build` and `deploy` refuse it
(`iki-version`), and so does the deployment's migrate Job, before it
migrates anything. A build of Iki counts as its release: `0.2.0.dev3` is
`0.2.0`. Leaving `iki:` out says nothing either way.

## Dependencies

A plugin lists the plugins whose types it uses, with the schema versions it
works with. To check those references, pass the dependencies' directories
with `--with`. Without them, the validator says it can't check them rather
than guessing. Core types (`iki/core.Entity`, `iki/core.Sequence`, `iki/core.Collection`,
`iki/core.View`, `iki/core.Alignment`, `iki/core.App`) need no dependency.

## The core plugin

Every deployment has the core plugin, released with Iki. Its manifests are
in [`iki_spec/core`](src/iki_spec/core). Other plugins use its types without
declaring a dependency. It's `iki/core`:

- `iki/core.Entity`: any entity. It's the server's own model, so it isn't generated.
- `iki/core.Sequence`: anything with a sequence of residues (abstract). Plugins'
  sequence types extend it, so one search finds DNA and proteins alike.
- `iki/core.Collection`: a named set of entities. A static collection lists its
  `members`; a dynamic one has a `search`, run as whoever reads it
  ([ADR 0038](../docs/adr/0038-core-plugin-app-and-collections.md)).
- `iki/core.Alignment`: sequences aligned, as FASTA in `aligned`. Its `members`
  are the sequences in Iki it aligns, each the row headed by its HRID; other
  rows, such as reads, go by their own names. Its `reference`, if it has
  one, is the sequence the others were aligned to. Iki's alignment viewer
  draws it
  ([ADR 0088](../docs/adr/0088-alignments-are-entities-of-sequence-entities.md)).
- `iki/core.View`: a saved look at 1 to 20 entities in Iki's viewers, each
  pinned to the revision it showed: its `targets`, their `target_revisions`
  and `viewers` in the same order, and what the viewers keep of each in
  `state`. Its structures share one Mol\* scene, superposed if wanted. Only
  people who can read every entity it shows can read it
  ([ADR 0086](../docs/adr/0086-saved-views-are-entities-pinned-to-a-revision.md),
  [ADR 0110](../docs/adr/0110-a-view-shows-several-entities-each-at-its-revision.md)).
- `iki/core.App`: a page of Iki's own components. Its `spec` (`app/v1`) lists
  panels on a twelve-column grid, each an entities grid, a viewer or text,
  bound to the panels before it by CEL expressions between `{{` and `}}`
  ([ADR 0090](../docs/adr/0090-apps-are-compositions-of-ikis-components.md)).
  An app about an entity type (`entity.type`) is a tab on their pages in its
  projects, and reads the one it's on as `entity`. A panel can also be an
  installed plugin's component, by its name (`acme/lab.box_map`), given
  `entity` (one it's a tab of) or a search of the type it shows, as a grid's
  props say; one that lets people pick offers its `selection`. If the
  plugin is gone, its panel says so and the others still show
  ([ADR 0117](../docs/adr/0117-components-show-many-entities-in-more-places.md)).

The server ships the core plugin's app, generated like any plugin's. After
changing these manifests, run `make core` to regenerate it and make its
migrations.

## A deployment's lockfile

Each deployment's config repository pins the plugins it installs in
`plugins.lock.yaml` ([schema](src/iki_spec/schemas/plugin-lock/v1.json)):

```yaml
apiVersion: iki/v1
kind: PluginLock
plugins:
  - name: iki/dna # Iki's own plugins come from Iki's repository
    git: https://github.com/yogesh-dhande/iki.git
    path: plugins/dna # the plugin's directory in the repository, if not its root
    sha: 4f1c2b9e0d6a7c3b8e5f1a2d9c0b7e6f5a4d3c2b
    state: active
    settings: settings/dna.yaml # relative to the lockfile
  - name: acme/assays # a lab's, in its namespace, from its repository
    git: https://github.com/acme/iki-assays.git
    sha: 9b2e4c1d0a7f6e5d4c3b2a1f0e9d8c7b6a5f4e3d
    state: active
```

`sha` is a full commit, never a branch or tag, so the same lockfile always
installs the same plugins. `git ls-remote <repository> <tag>` prints a
tag's commit. In development, `local: ../path/to/plugin` takes a directory
instead. `iki deploy build` builds the image's plugins from the lockfile
(see [`cli/`](../cli/README.md) and ADR 0034).

A site that can't reach github.com still names Iki's repository here, and
fetches Iki's own plugins from its mirrors of it, which its deployment's
admin names (`plugins.mirrors` in its Helm values, and `iki deploy build
--mirror`). A plugin from a mirror must be at the commit pinned here, with
a tree Iki's repository has had, as
[`first_party_trees.json`](src/iki_spec/first_party_trees.json) lists them
([ADR 0114](../docs/adr/0114-ikis-own-plugins-can-come-from-a-mirror.md)).
`make first-party` adds a plugin's tree when it changes.

## A deployment's compute

Beside its lockfile, a deployment's directory can hold `compute.yaml`: the
machines it runs actions on (compute templates) and which environments run
on which (bindings). Compute admins own it; changes go through pull requests
like the lockfile's. Point editors at
[`compute/v1`](src/iki_spec/schemas/compute/v1.json).

```yaml
apiVersion: iki/v1
kind: ComputeTemplate
name: a100-spot
provisioner: aws_ec2 # backend_pool, k8s_job, k8s_deployment, aws_ec2 or gcp_gce
lifecycle: ephemeral # aws_ec2 and gcp_gce; k8s_job is ephemeral, k8s_deployment persistent
capacity:
  { cpu: 96, memory: 1152Gi, gpu: { count: 8, memory: 40Gi, model: A100 }, scratch_disk: 8Ti }
aws:
  instance_type: p4d.24xlarge
  # Docker, and for GPUs NVIDIA's drivers and container toolkit
  image: resolve:ssm:/aws/service/ecs/optimized-ami/amazon-linux-2023/gpu/recommended/image_id
  spot: true
  fallback: on_demand # or none: wait for spot capacity
  region: us-east-1
  subnets: [subnet-0a1b2c3d4e5f60718, subnet-0f1e2d3c4b5a69788] # private, one or more zones
  security_groups: [sg-0a1b2c3d4e5f60718] # no inbound; outbound to Iki, storage, registry
  instance_profile: iki-vm-worker # needs no permissions
  # root_disk: 9Ti (scratch_disk and 40Gi unless set), root_device: /dev/xvda,
  # kms_key: alias/iki-runs, tags: { cost-center: rd-42 }
volumes: { af-db: { strategy: clone_from_snapshot, fast_snapshot_restore: true } }
limits: { max_concurrent: 10 } # persistent ones also take min_warm and idle_shutdown (10m)
price: { per_hour: 32.77, currency: USD }
---
apiVersion: iki/v1
kind: EnvironmentBinding
environment: structure_prediction.alphafold-gpu # plugin.environment
templates:
  - { template: a100-spot } # priority 0 is tried first
  - { template: a100-on-demand, priority: 1 }
```

For each job, Iki picks among the bound templates on the action's tier that
it fits and whose lifecycle the action asks for: the lowest priority number
first, then the smallest. A persistent template keeps `min_warm` machines
running for each environment bound to it, and stops others once they've had
nothing to do for `idle_shutdown` (ten minutes unless it says). Kubernetes
templates take `kubernetes`, with `namespace`, `node_selector`,
`tolerations` and `runtime_class` (such as `gvisor`). `aws_ec2` templates
take `aws`: each instance boots `image`, which needs Docker (and for GPUs,
NVIDIA's drivers and container toolkit), in one of `subnets`, with
`security_groups` and `instance_profile`. With `spot`, Iki tries spot capacity
in each subnet's zone first, then on-demand with `fallback: on_demand`; spot
instances AWS takes back are retried, and checkpointable runs resume (ADR
0074).

`gcp_gce` templates take `gcp` instead, on Compute Engine (ADR 0107). Each
instance starts from the deployment's instance template (the `vm-tier`
module's), which gives it its subnet with no public address, its service
account and network tag; the template says what changes:

```yaml
provisioner: gcp_gce
gcp:
  machine_type: a2-highgpu-8g # or n1-standard-16 with gpu_type: nvidia-tesla-t4
  image: projects/cos-cloud/global/images/family/cos-stable # Docker; for GPUs, NVIDIA's too
  spot: true # taken back with two minutes' notice
  fallback: on_demand
  region: us-central1
  zones: [us-central1-a, us-central1-b] # tried in turn
  # root_disk: 9Ti, disk_type: pd-ssd, labels: { cost-center: rd-42 },
  # kms_key and instance_template: the deployment's unless set
```

Each is a Shielded VM, named for the deployment (`<owner>-run-<launch>`),
with its disk encrypted by the deployment's Cloud KMS key. With `spot`, Iki
tries Spot capacity in each zone, then standard with `fallback: on_demand`;
Spot VMs Compute Engine takes back are retried, and checkpointable runs
resume. `iki deploy build` checks that:

- every binding names an environment of an installed plugin, and templates
  in this file on that environment's tiers
- every bound template is at least the environment's minimum, and every
  action fits at least one template on its tier
- each environment volume has a strategy on every template it's bound to
  (`clone_from_snapshot`, `attach_exclusive`, `shared_filesystem` or
  `object_mount`), and volumes written by one run at a time are attached
  exclusively

Actions whose environments aren't bound on their tier can't run on that
deployment; the build warns about each. Self-hosted actions need no
template. A binding's `labels` are what this deployment's machines need for
the environment's self-hosted runs, besides the environment's own, such as
the lab whose instruments they drive. A binding may give only labels:

```yaml
apiVersion: iki/v1
kind: EnvironmentBinding
environment: chromatography.chromeleon
labels: [lab-3]
```

A self-hosted run needs its environment's labels, its binding's, and any
its launcher adds, which only narrow which machines take it.

A volume an environment's setup fills gets its data from a setup build
(ADR 0075): a Compute admin asks for one, the setup script fills a fresh
disk on a machine of its own, and Iki snapshots it as a new version. On
Kubernetes, with `clone_from_snapshot`, each run pod gets a read-only disk
made from the newest version's snapshot; the template's `storage_class` and
`snapshot_class` name the StorageClass and VolumeSnapshotClass to use (the
cluster's defaults unless set):

```yaml
volumes:
  af-db:
    strategy: clone_from_snapshot
    storage_class: ebs-gp3
    snapshot_class: ebs-snapshots
```

On `aws_ec2`, each instance gets a new EBS disk from the snapshot, mounted
read-only (ADR 0076). With `fast_snapshot_restore`, a version is ready only
once its snapshot starts disks at full speed in each of the template's
zones, at an hourly cost for each; `iops` and `throughput` (MiB/s) set its
disks' performance, gp3's baseline unless set:

```yaml
volumes:
  af-db:
    strategy: clone_from_snapshot
    fast_snapshot_restore: true
    iops: 16000
    throughput: 1000
```

On `gcp_gce`, the build's disk is made into an image in the template's
region, encrypted with the deployment's key, and each instance gets a new
persistent disk from it (of the template's `disk_type`), mounted read-only
and deleted with the instance: an image rather than a snapshot, as Compute
Engine makes at most six disks an hour from one snapshot in a zone. Compute
Engine has no fast snapshot restore, and Iki doesn't yet give volumes there
on a shared file system or in a bucket.

On Kubernetes, with `shared_filesystem`, each version is a directory of its
own on a file system many pods mount at once, such as FSx for Lustre, EFS or
NFS (ADR 0077). `claim` names its PersistentVolumeClaim, in the namespace
runs go in; Iki keeps versions under `path` in it (`iki` unless set), which
must exist, writable by runs' user (UID 10001). A setup build writes its
version's directory, nothing is copied, and each run pod mounts the newest
version's directory read-only:

```yaml
volumes:
  af-db:
    strategy: shared_filesystem
    claim: fsx-reference
    path: iki
```

On `aws_ec2`, machines mount the file system themselves: name an NFS export
(`nfs`, such as EFS's) or a Lustre file system (`lustre`, such as FSx for
Lustre's, whose client the machine image needs). A small on-demand machine
(`aws.cleanup_instance_type`, `t3.small` unless set) removes retired
versions:

```yaml
volumes:
  af-db:
    strategy: shared_filesystem
    lustre: fs-0123.fsx.us-east-1.amazonaws.com@tcp:/abcdefgh
```

On Kubernetes, with `object_mount`, each version is a directory (a prefix)
of its own in a bucket that the Mountpoint for Amazon S3 CSI driver mounts
(ADR 0078). `claim` and `path` are as for a shared file system. The bucket
must be mounted with `uid=10001`, `gid=10001` and `allow-delete`. A bucket
can't be written in place, so a setup build's script fills a disk
(`storage_class`, the cluster's default unless set). Iki then copies the
disk into the version's directory. Symbolic links can't be copied, and a
build that leaves one fails, saying which.

```yaml
volumes:
  af-db:
    strategy: object_mount
    claim: s3-reference
    path: iki
```

On `aws_ec2`, name the `bucket` instead, in the template's region. The
machine image needs Mountpoint for Amazon S3 (`mount-s3`), and the template's
role must be able to read the bucket and to write and delete under `path`. A
setup build's machine fills a disk, then copies it into the bucket before it
shuts down:

```yaml
volumes:
  af-db:
    strategy: object_mount
    bucket: acme-reference
    path: iki
```

Until a version is ready, the environment's runs wait, saying why; bound to
templates in several places, they go to the first where one is ready. Each
attempt records the version it read.

A template's `price` is what one of its machines costs an hour, up or idle
(ADR 0070). Iki records what each run's machine time cost at that price,
shows it on the run and in cost reports, and estimates what a run will cost
before it's launched. All of a deployment's prices are in one currency
(`mixed-currencies`), and the build warns about a container or VM template
without one (`unpriced-template`): its runs show machine time but no cost.
Backend and self-hosted tiers have no per-run cost.

## What a command reads

When a worker leases a job, it gets the run input and writes it to
`input.json` for the action's command
([`run-input/v1`](src/iki_spec/schemas/run-input/v1.json)):

```json
{
  "apiVersion": "iki/v1",
  "kind": "RunInput",
  "run": {
    "id": "…",
    "hrid": "RUN-000123",
    "attempt": 1,
    "action": "iki/protein.compute_properties",
    "plugin": { "name": "protein", "version": "1.2.0", "sha": "…" },
    "project": "…"
  },
  "params": { "ph": 7.4 },
  "settings": { "ncbi_email": "lab@example.com" },
  "inputs": {
    "protein": [
      {
        "id": "…",
        "hrid": "PRT-000042",
        "type": "iki/protein.Protein",
        "schema_version": 1,
        "revision": 3,
        "values": { "sequence": "MKV…" }
      }
    ]
  },
  "files": {
    "…": {
      "name": "abl.pdb",
      "media_type": "chemical/x-pdb",
      "size": 81234,
      "sha256": "…",
      "path": "/…/inputs/…/abl.pdb"
    }
  },
  "command": ["python", "scripts/properties.py"],
  "environment": "iki/protein.protein-tools",
  "job": { "…": "the lease, for iki-worker" }
}
```

Each input is a list, even when the input takes one entity. Values are the
ones at the revision the run was launched on, as the REST API shows them,
with references and files given as IDs. An input that includes references
gives each entity an `included` object too: for each reference, a list of
the entities it names, in the same form. A file param's value is the
file's ID too. `files` describes every file the inputs and what they include
hold and every file param names, by ID. The worker downloads each one before the command
starts, checks its size and SHA-256, and adds its `path`. `settings` holds
the plugin's settings as the deployment set them when the run was launched.
An action with no inputs (an import) gets `"inputs": {}`.

The command runs from its plugin's directory, and finds its files through
the environment ([ADR 0046](../docs/adr/0046-how-iki-worker-runs-a-command.md)):

| Variable                        | What it is                                   |
| ------------------------------- | -------------------------------------------- |
| `IKI_INPUT`                     | `input.json`                                 |
| `IKI_OUTPUT`                    | where to write `output.json`                 |
| `IKI_OUT_DIR`                   | a directory for files it makes               |
| `TMPDIR`, `HOME`                | the job's own, removed when it ends          |
| `IKI_RUN_ID`, `IKI_RUN_ATTEMPT` | the run, and which attempt this is           |
| `IKI_SEED`                      | a seed for its random numbers (0 to 2³¹ − 1) |
| `IKI_CHECKPOINT_DIR`            | where a checkpointable action saves progress |

What it prints goes to the run's log. A line like
`::progress 0.4 Folding chain A` reports progress (a fraction, or `40%`).
It should exit 0 once it has written `output.json`; any other exit fails
the attempt. Before failing, it can say why with a line like
`::error Record 3 (pUC19) has no bases`: the last one is what the run's
page shows. On SIGTERM (a cancel, its timeout, or the machine stopping)
it has 10 seconds to stop.

A `checkpointable` action saves its progress in `IKI_CHECKPOINT_DIR`
([ADR 0069](../docs/adr/0069-checkpointable-runs-resume.md)). While the command
runs, the worker saves the directory every 10 minutes (as the deployment sets
it) if anything in it changed, and once more if the attempt ends in a way the
run is retried for: interrupted, say, or the machine stopping. A later attempt
finds the newest checkpoint in the directory before it starts, and
`IKI_RUN_ATTEMPT` says which attempt it is. So:

- on start, resume from what's in the directory, if anything is
- write each file under a temporary name, then rename it into place, so a
  save never holds half a file: a file that changes as it's saved is left for
  the next save
- on SIGTERM, save what you can within the 10 seconds, then exit

Only regular files and directories are saved, not links. A checkpoint is
deleted once a newer one is saved, and when the run ends. Batchable actions
can't be checkpointable.

The command writes `output.json`
([`run-output/v1`](src/iki_spec/schemas/run-output/v1.json)). It can only
change what its action declares:

```json
{
  "apiVersion": "iki/v1",
  "kind": "RunOutput",
  "updates": {
    "protein": [{ "entity": "…", "values": { "molecular_weight": 45123.4 } }]
  },
  "creates": [
    {
      "type": "iki/protein.ProteinStructure",
      "values": { "method": "predicted", "structure": "…" },
      "derived_from": ["…"]
    },
    {
      "type": "iki/dna.DNASequence",
      "values": { "name": "pUC19", "bases": "TCGCGCGTTT…" },
      "origin": {
        "source": "NCBI Nucleotide",
        "id": "L09137",
        "version": "L09137.2",
        "url": "https://www.ncbi.nlm.nih.gov/nuccore/L09137.2",
        "retrieved_at": "2026-11-11T09:30:00Z",
        "sha256": "…"
      }
    }
  ],
  "metrics": { "plddt": 91.2 },
  "messages": [{ "level": "info", "text": "Used model 3 of 5." }]
}
```

`updates` are keyed by input name and may set only the declared fields of
entities given there. `creates` may make only the declared types. Leave out
`derived_from` when the inputs the type is derived from hold one entity.
An imported entity says where it came from in `origin`: a database's record
(its `version`, an `https` `url`, when it was `retrieved_at`, and the
`sha256` of what was read), or a record in a file the run was given (`file`,
one of its file params). Iki keeps it with the entity, for whoever reads
it, and again each time an import finds the same entity.

How an entity was made is kept too (ADR 0106), and says who says so:

- **Iki's record.** An action lists the tools its runs make entities with, by
  name and version, in `software`:

  ```yaml
  software: [{ name: ImmuneBuilder, version: "1.2", url: https://github.com/oxpig/ImmuneBuilder }]
  ```

  Each entity its runs create is recorded as computed with them, and the
  run, with its environment build, proves it. An action that could make an
  entity with one tool or another names which in the create's `made_with`
  (`"made_with": ["Vina"]`), among its own `software` only.

- **Its source's word.** An origin's `made` is what an imported record says
  about how its source made the entity, read from the record, never guessed:
  mmCIF's `_exptl` and `_software`, ModelCIF's `_ma_` categories.

  ```json
  "origin": {
    "source": "AlphaFold DB",
    "id": "AF-P69905-F1",
    "version": "4",
    "made": {
      "kind": "computational",
      "software": [{ "name": "AlphaFold", "version": "2.3.2" }],
      "details": { "mean_plddt": 97.1 }
    }
  }
  ```

  `kind` is `experimental` or `computational`; `method` is the source's name
  for it, such as `X-RAY DIFFRACTION`. Iki shows it as the source's word,
  never as its own record.

An entity's page, `GET /entities/{ref}/making` and MCP's `get_entity` give
both, Iki's records first.

An import from a LIMS or ELN matches its records to entities with
`match_on`, one of the type's unique fields (ADR 0082). A create whose value
an entity of the run's project has already updates that entity instead: a
revision by the run, with its origin, setting the values the create gives.
One that changes nothing is found again, as the project's own record is when
its identity is registered again. Only the project's own entities match,
never one linked in from elsewhere; for a field marked `registry`, the
project's record of the registry entry that has the value. In a creates
file, a record may come only once.

A record with no match yet, of a type with an identity, claims the
project's own record of that identity's registry entry, if it has no value
for the field yet: a protein registered before the LIMS knew it takes the record's
ID. If it's another record's already, the run fails, naming both, since one
entity can't be two records; so does a file with two records of one entity.

```yaml
effects:
  creates: [{ type: lab.Sample, cardinality: many, match_on: lims_id }]
```

A file field takes a file the inputs already had, by ID, or one the
command made:

```json
{ "structure": { "$file": "relaxed.pdb", "media_type": "chemical/x-pdb" } }
```

`$file` is a path inside `IKI_OUT_DIR`; `name` defaults to its last part,
and `media_type` to a guess from its extension. The worker uploads it for
this run and puts the new file's ID in its place, so only files this run
uploaded can be attached. Iki writes all of it in one transaction, or none.
A message can name the input `entity` it's about. One at level `error`
fails that entity's run, which then writes nothing.

A new entity can refer to one made with it, such as a structure to its
chains ([ADR 0064](../docs/adr/0064-a-runs-new-entities-can-refer-to-one-another.md)).
Give the earlier create a `key`, unique in `creates`, and the later one's ref
field (or an item of a list of refs) `{"$create": "<key>"}`:

```json
[
  { "type": "iki/protein.Protein", "key": "A", "values": { "sequence": "MVLSPADKTN…" } },
  {
    "type": "iki/protein.ProteinStructure",
    "values": { "structure": { "$file": "1abc.cif" }, "chains": [{ "$create": "A" }] }
  }
]
```

It becomes the ID of the entity that create made, or the project's record it
found; a reference to a type with an identity holds its registry entry. A
create can refer only to those before it, and, in a job that runs several
runs at once, only to its own run's.

### Creates in a file

`creates` holds at most 10,000, and `output.json` at most 2.5 MB. A command
that makes more (an import of a million sequences, say) writes them to a file
in `IKI_OUT_DIR`, one create per line as an item of `creates` is (JSON
Lines), and names it instead of giving `creates`
([ADR 0056](../docs/adr/0056-bulk-imports.md)):

```json
{ "apiVersion": "iki/v1", "kind": "RunOutput", "creates_file": { "$file": "creates.ndjson" } }
```

The worker uploads it with the run's other files. Iki then checks every line,
a chunk at a time, going on from where it was if it's interrupted, and writes
them all in one transaction, or none: nobody sees half an import. A line's
problems point at `#/creates_file/<line>`, counting from 1. Lines can't
attach files or refer to one another (give those in `creates`), and a job that runs several runs at
once can't give a creates file. Webhooks hear of an import once, as
`entity.imported`, not `entity.created` for each entity. A file holds at most
100 million lines, each at most 256 MiB.

### Batches and packing

Launching an action on several entities at once makes a batch
(`BAT-000123`) of runs, one for each
([ADR 0055](../docs/adr/0055-batches-and-packing.md)). An action that's
`batchable: N` runs up to N of a batch's runs in one job: its one input
lists all their entities, and `pack` says which is whose:

```json
"pack": [
  { "run": "…", "hrid": "RUN-000123", "entity": "…" },
  { "run": "…", "hrid": "RUN-000124", "entity": "…" }
]
```

Iki splits the output back into each run's own: updates by their `entity`,
new entities by their `derived_from` (which a packed job must give), and
messages by their `entity` (one without goes to every run). Metrics go to
every run. Each run's part is then checked and written on its own, so one
entity's bad result fails only its run. To fail one entity and go on with
the rest, report an `error` message about it.

### Seeds, reuse and Reproduce exactly

Every run gets a seed, as `IKI_SEED` and `run.seed` in `input.json`
([ADR 0057](../docs/adr/0057-reuse-reproduce-exactly-and-seeds.md)). A
command that uses random numbers should seed them from it: a run reproduced
exactly gets the same seed again. A packed job's command gets its first
run's.

An action that's `deterministic: true` promises that its results depend only
on its plugin's commit, its params, its plugin's settings and its inputs at
the revisions it reads. Its runs can then be reused: launching one identical
to a run that succeeded, which the launcher can read, ends at once with that
run's results (`reused_from`), and runs nothing. Launch with `reuse: false`
to run it anyway. Don't declare it for an action that fetches what may
change, or whose results depend on the time or the machine.

`POST /runs/{ref}/reproduce` runs any run again exactly: the same commit,
params, settings, seed and input revisions. It writes nothing: its
`reproduction` says whether its results (creates, updates, the creates file
and metrics; files by their content) and environment build match the
original's, and where they differ.

## Action tests

A plugin's `tests/` directory holds fixtures for its actions
([`action-test/v1`](src/iki_spec/schemas/action-test/v1.json),
[ADR 0093](../docs/adr/0093-plugin-action-tests-run-fixtures-through-the-worker.md)).
`iki plugin test` runs each as a worker would and compares what it writes:

```yaml
apiVersion: iki/v1
kind: ActionTest
name: haemoglobin_alpha
action: compute_properties
inputs:
  protein:
    - values: { sequence: MVLSPADKTNVKAAWGKVGAHAGEYG… }
expect:
  output:
    updates:
      protein:
        - values: { length: 142, isoelectric_point: 8.72 }
  tolerance: 1.0e-6
```

- `params` and `settings` are given as a person and a deployment would
  give them; settings left out take their defaults.
- Each input is a list of entities, by their `values`, and optionally
  their `hrid` and `type` (a subtype of the input's). An input that takes
  one entity can take several for a batchable action: they're packed as
  the server packs runs.
- For an input that includes references, an entity's `included` gives the
  entities each names, in the same form:
  `included: { heavy_chain: [{ values: { sequence: EVQ… } }] }`. Each gets
  an ID, which the reference holds.
- `files` names files in the plugin. A value or param uses one as
  `{"$file": "name"}`.
- `expect.output` is compared by what it gives: every key in a mapping,
  every item of a list in order, numbers within `tolerance` (relative,
  1e-9 unless given). With `expect.error`, the run must fail, and its last
  `::error` line must contain that text.

The run input has fixed IDs and seed, so a deterministic action writes the
same every time. Every output is also checked against `run-output/v1`, and
against the fields and types its action declares it changes.

## Bundles

A bundle carries entities from one project of one deployment to another,
with every revision, the files they name, who made them and the runs that
did
([ADR 0096](../docs/adr/0096-bundles-carry-entities-between-deployments-with-their-history.md)).
It's a `.tar.gz`:

```text
people.ndjson      # each person a record names, matched by email where it's imported
agents.ndjson      # agents that acted for them (ADR 0072)
files.ndjson       # the files revisions name, and their SHA-256
entities.ndjson    # each entity: its revisions, and each table's values at each
outside.ndjson     # what the values or runs refer to that isn't in it
runs.ndjson        # the finished runs that made or changed them: inputs, attempts, logs, lineage
blobs/sha256/<sha> # the bytes of Iki's own files, once each
bundle.json        # who, when, why, the plugins and schema versions, every member's SHA-256
bundle.json.sig    # the source deployment's Ed25519 signature of bundle.json
```

[`bundle/v1`](src/iki_spec/schemas/bundle/v1.json) is `bundle.json`'s
schema, and its `$defs` are the NDJSON members' lines.

## YAML

Manifests are read as YAML 1.2: only `true` and `false` are booleans, so
`no`, `on` and `yes` stay text, and `010` is ten. Duplicate keys, aliases
(`*name`), merge keys (`<<`), tags (`!!python/…`) and files over 1 MiB are
refused, so what is reviewed is what Iki reads.

## Problem codes

Every problem has a stable `code`, shown in brackets and in `--format json`:

| Code                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | What's wrong                    |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| `yaml-syntax`, `yaml-duplicate-key`, `yaml-alias`, `yaml-merge-key`, `yaml-tag`, `yaml-key`                                                                                                                                                                                                                                                                                                                                                                                                                                            | The YAML itself                 |
| `file-too-large`, `not-utf8`, `unreadable-file`, `symlink`, `not-a-mapping`                                                                                                                                                                                                                                                                                                                                                                                                                                                            | The file                        |
| `missing-plugin-manifest`, `duplicate-plugin-manifest`, `wrong-file`, `not-a-directory`                                                                                                                                                                                                                                                                                                                                                                                                                                                | Where manifests are             |
| `missing-api-version`, `unsupported-api-version`, `missing-kind`, `unknown-kind`                                                                                                                                                                                                                                                                                                                                                                                                                                                       | A manifest's header             |
| `schema`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | Anything the JSON Schemas catch |
| `unqualified-name`, `reserved-namespace`, `unqualified-dependency`, `unqualified-reference`, `reserved-name`, `name-too-long`, `duplicate-table`, `column-conflict`                                                                                                                                                                                                                                                                                                                                                                    | Names                           |
| `unknown-role`; `duplicate-role-label` (warning)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Lab roles                       |
| `states-inherited`, `state-initial`, `state-removed`, `unknown-state`, `transition-to-itself`, `unknown-transition`, `state-unreachable`                                                                                                                                                                                                                                                                                                                                                                                               | States                          |
| `assignment-not-people`, `assignment-without-states`, `due-not-a-date`, `due-in-registry`, `person-in-registry`                                                                                                                                                                                                                                                                                                                                                                                                                        | People in records               |
| `duplicate-type`, `unknown-type`, `extends-cycle`, `field-shadows-parent`                                                                                                                                                                                                                                                                                                                                                                                                                                                              | Types                           |
| `missing-hrid`, `hrid-on-abstract`, `duplicate-hrid-prefix`                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | HRIDs                           |
| `unknown-field`, `identity-type`, `identity-not-fixed`, `identity-not-required`, `identity-removed`, `identity-inherited`, `identity-redeclared`, `identity-under-concrete`, `registry-without-identity`, `registry-on-identity`, `registry-fact-free-text`, `registry-refers-to-record`                                                                                                                                                                                                                                               | Identities                      |
| `computed-with-default`, `computed-required`, `invalid-default`, `min-above-max`, `invalid-pattern`                                                                                                                                                                                                                                                                                                                                                                                                                                    | Field options                   |
| `unsupported-index`, `index-needs-max-length`, `unique-needs-index`, `link-not-https`, `link-without-value`, `link-unknown-placeholder`                                                                                                                                                                                                                                                                                                                                                                                                | Indexes                         |
| `unknown-unit`, `display-unit-mismatch`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | Units                           |
| `file-too-large`, `invalid-json-schema`, `display-title`                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | Files, JSON fields and titles   |
| `extends-builtin`, `extension-of-own-type`, `duplicate-extension`, `extension-field-conflict`, `extension-field-required`                                                                                                                                                                                                                                                                                                                                                                                                              | Extensions                      |
| `undeclared-dependency`, `dependency-not-available`, `dependency-invalid`, `dependency-is-self`, `dependency-cycle`, `dependency-version`, `dependency-unpinned` (warning), `iki-version`                                                                                                                                                                                                                                                                                                                                              | Dependencies                    |
| `required-with-default`, `setting-looks-secret` (warning), `unknown-setting`, `missing-setting`, `invalid-setting`, `settings-file`                                                                                                                                                                                                                                                                                                                                                                                                    | Settings                        |
| `missing-lockfile`, `not-a-lockfile`, `lockfile-documents`, `duplicate-plugin`, `unqualified-name`, `reserved-namespace`, `first-party-source`, `core-is-builtin`, `missing-source`, `mixed-source`, `purged-source`, `missing-sha`, `path-outside`, `absolute-path`, `missing-settings-file`                                                                                                                                                                                                                                          | Lockfiles                       |
| `duplicate-action`, `duplicate-name`, `param-looks-secret` (warning), `max-needs-many`, `input-schema-unsatisfied`, `batch-needs-one-input`, `batch-keeps-artifacts`, `batch-checkpoints`, `timeout-too-long`, `duplicate-software`                                                                                                                                                                                                                                                                                                    | Actions                         |
| `creates-abstract`, `match-on-unknown-field`, `match-on-not-unique`, `unknown-input`, `duplicate-updates`, `updates-immutable`, `action-does-nothing`                                                                                                                                                                                                                                                                                                                                                                                  | What actions write              |
| `unknown-environment`, `tier-not-supported`, `lifecycle-wrong-tier`, `gpu-wrong-tier`, `self-hosted-secrets`, `self-hosted-network`, `reserved-secret`, `absolute-path-on-backend`, `missing-file`                                                                                                                                                                                                                                                                                                                                     | Where actions run               |
| `network-unknown-setting`, `network-setting-not-hostname`, `deterministic-with-network`, `retries-deterministic-failure` (warnings)                                                                                                                                                                                                                                                                                                                                                                                                    | Reuse and retries               |
| `workflow-unknown-action`, `workflow-unknown-input`, `workflow-unknown-param`, `workflow-input-missing`, `workflow-param-missing`, `workflow-unknown-reference`, `workflow-unknown-step`, `workflow-wrong-type`, `workflow-many-into-one`, `workflow-each-takes-many`, `workflow-unknown-metric`, `workflow-condition-type`, `workflow-condition-each`, `workflow-invalid-param`, `workflow-cycle`, `duplicate-workflow`                                                                                                               | Workflows                       |
| `trigger-unknown-action`, `trigger-unknown-input`, `trigger-input-ambiguous`, `trigger-input-missing`, `trigger-wrong-type`, `trigger-fields-not-updated`, `trigger-states-not-transitioned`, `trigger-transitioned-without-states`, `trigger-unknown-param`, `trigger-invalid-param`, `trigger-param-missing`, `trigger-type-missing`, `trigger-schedule-missing`, `trigger-schedule-invalid`, `trigger-schedule-never`, `trigger-schedule-entity`, `trigger-schedule-not-scheduled`, `trigger-timezone-unknown`, `duplicate-trigger` | Triggers                        |
| `duplicate-component`, `element-twice`, `component-too-big`, `missing-file`, `unreadable-file`, `symlink`, `slot-needs-entities`, `slot-twice`, `home-not-personal`, `unknown-operator`, `unsortable-field`, `unknown-context`, `context-misplaced`, `search-too-big`                                                                                                                                                                                                                                                                  | Components                      |
| `duplicate-environment`, `environment-needs-image`, `environment-needs-packages`, `unused-packages`, `unused-image` and `unused-environment` (warnings)                                                                                                                                                                                                                                                                                                                                                                                | Environments                    |
| `requirements-unpinned`, `requirements-include`, `conda-not-explicit`, `conda-unpinned`                                                                                                                                                                                                                                                                                                                                                                                                                                                | Pinned packages                 |
| `volumes-wrong-tier`, `overlapping-mounts`, `volume-needs-setup`, `setup-fills-nothing` (warning)                                                                                                                                                                                                                                                                                                                                                                                                                                      | Volumes and setup               |
| `not-a-test`, `duplicate-test`, `unknown-action`, `unknown-param`, `missing-param`, `unknown-input`, `missing-input`, `input-count`, `wrong-type`, `unknown-field`, `unknown-file`, `missing-file`, `unknown-setting`                                                                                                                                                                                                                                                                                                                  | Action tests                    |
| `duplicate-template`, `duplicate-binding`, `unknown-kind`, `wrong-provisioner`, `gpu-wrong-tier`, `limit-needs-persistent`, `fallback-without-spot`, `root-disk-too-small`, `volume-strategy-unsupported`, `fast-restore-wrong-strategy`, `volume-option-kubernetes-only`, `snapshot-class-wrong-strategy`, `mixed-currencies`; `unpriced-template` (warning)                                                                                                                                                                          | Compute templates               |
| `unknown-environment`, `unknown-template`, `template-wrong-tier`, `template-too-small`, `action-does-not-fit`, `volume-strategy-missing`, `volume-strategy-access`; `lifecycle-unbound`, `unbound-environment` and `unused-template` (warnings)                                                                                                                                                                                                                                                                                        | Bindings                        |
