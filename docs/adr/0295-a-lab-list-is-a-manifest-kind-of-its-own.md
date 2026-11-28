# 0295. A lab list is a manifest kind of its own

- Status: Accepted
- Date: 2026-11-27
- Amends: [0270](0270-a-lab-curates-its-own-lists.md) (its manifest form)

## Context

ADR 0270 made a lab list an entity type marked as one: `list: true`, or
`list:` with starting options, on an `EntityType`. Its options are registry
entries, and a field that uses a list is an ordinary reference. That model
holds: options get identity, supersede, revisions, audit, references and
bundle mapping from the registry, and nothing about that changes here.

The marker doesn't hold up as a manifest form:

- An `EntityType` has some thirty keys, and almost none of them are for a
  list. The validator refused nineteen by name, and viewers, state fields
  and computed fields separately. A refusal by name fails open: a key added
  to entity types later is allowed on lists until someone remembers to add
  it to the refusals, and after the first release a lab's list could come
  to depend on it.
- An author still had to give a list a `schema_version` and an HRID
  prefix, though options are named by their `code`. The fourteen lists in
  Iki's own plugins spent fourteen prefixes from the space every lab's
  types share.
- Editors offered every entity type key on a list.
- "List" meant three things in a type file: the marker, a field of
  `type: list`, and lists of rows.

ADR 0270 didn't weigh a separate kind. Its alternatives were enums a lab
extends in settings, per-project lists, free text, lab roles and webhooks.

Benchling's Dropdowns and LabWare's `LIST` and `LIST_ENTRY` tables are
objects of their own, not entity schemas with most of their options turned
off. A Benchling dropdown's options have IDs of one kind across every
dropdown, and a schema field refers to the dropdown.

## Decision

**A list is `kind: List`.** Its schema names everything a list can have,
and nothing else is allowed: `apiVersion`, `kind`, `name`, `label`,
`label_plural`, `description`, `schema_version`, `fields` and `options`. A
key added to entity types later isn't a list's unless the list schema adds
it too.

```yaml
apiVersion: iki/v1
kind: List
name: PurificationMethod
label: Purification method
schema_version: 1
options:
  - { code: protein_a, label: Protein A, description: Affinity capture on protein A. }
  - { code: imac, label: IMAC, category: chromatography }
fields:
  category: { type: ref, to: MethodCategory }
  sop: { type: string, format: uri }
```

- `fields` are the options' facts, so a field takes only what a fact can
  be. The kinds are strings, numbers, quantities, Booleans, enums, dates
  and times, references and lists of them. The keys a fact takes are its
  label and description, `required`, `immutable` (a fixed fact,
  ADR 0143), `indexed`, `unique`, `link`, `removed`, `default`, and the
  value options of its kind. A fact isn't computed or derived, and doesn't
  say `registry`, because every one of a list's fields is a fact. A state
  field, a person or free text is the schema's problem, so the message is
  the one any manifest gets for a key it can't have. The general rules for
  registry facts still apply, such as a string fact needing a `pattern` or
  `format`, and a reference among them still names another list.
- `options` are the starting options, as `list: { options }` gave them.
  Without it a list starts empty.
- A list has no `hrid`, `identity`, `extends`, `abstract` or `display`, and
  none of a record type's keys. Its options are told apart by `code`, and
  shown by their `label`, which is their title wherever they appear.
- `schema_version` stays. A list's fields change as any type's do, and the
  plugin's migrations and its dependents read the version.
- `label_plural` stays, because a list's page and its dropdown's hints name
  its options in the plural ("sample matrices").

A list lives in `types/`, beside the types that refer to it. A field refers
to it with `{ type: ref, to: PurificationMethod }`, as before. `list:` on an
`EntityType` is gone, with its refusals. A type that `extends` a list, a
reference that names a list among other types, and an extension that adds a
record field to a list are still refused, because those name a list from
elsewhere.

**It compiles to the same entity type.** The built plugin has a list as an
entity type with its options' `code`, `label`, `description` and
`position`, as ADR 0270 has it, so the registry, entries, API, curators,
seeding, bundles and pages work as they did. An `EntityTypeExtension` can
still add facts to a list.

**Options share one ID sequence.** Every entity in Iki has an HRID,
which pages and links use. An option's comes from one sequence that every
list shares, `OPTION-000123`, and Iki reserves the prefix: no type may
declare `OPTION`. That's Benchling's precedent, one ID kind for every dropdown's
options. An option's HRID is only its address. Pages show its label, and
exports, the API and bundles name it by its code, so a list spends no
prefix and two lists never clash. The type reports no `hrid_prefix` of its
own.

## Consequences

- A list's manifest is the nine keys it can have, and editors complete only
  those.
- The fourteen first-party lists give their prefixes back. An option
  installed before this change had its list's prefix. Pre-M1, databases are
  regenerated, so no option keeps one.
- A lab's list can't grow a record capability by accident. Giving lists a
  new key means changing the list schema, deliberately.
- Server code calls a lab-list type `is_lab_list` and a list field kept in
  its own table `stored_in_list_table`. Each was a single word, "list",
  before.

## Alternatives considered

- **Keep the marker, and refuse what isn't for lists.** That's what was
  built, and its refusals fail open.
- **A prefix per list, as before, or one derived from the name.** Options
  are named by their code. A prefix per list spends the shared prefix
  space and gives people a second name for an option that nobody uses.
- **No HRID for options.** Every entity row, every link and every page
  addresses an entity by its HRID. Making it optional for one kind of entry
  would add a case to each of them, and a shared sequence costs nothing.
- **A `lists/` directory.** A list compiles to a type, and types refer to
  it by name. Keeping it in `types/` keeps a plugin's vocabulary next to
  the records that use it.
