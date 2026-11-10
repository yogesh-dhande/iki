# 0032. The iki/v1 plugin manifest spec, its validator, and UCUM units

- Status: Accepted
- Date: 2026-11-10
- Refines: [0006](0006-deploy-time-plugins-as-django-apps.md),
  [0007](0007-typed-table-per-type-entity-storage.md),
  [0016](0016-versioned-specs-compatibility-and-lts.md)

## Context

Plugins describe entity types in YAML manifests (ADR 0006), which compile
to typed tables (ADR 0007). Many things read those manifests: codegen, the
build, the web app, the worker, the SDK, MCP and the codemods of
`iki plugin upgrade`. None of them should have to run plugin code to learn
what a type is. The rules must therefore be written down once, as data, in
a form editors and other languages can use. A plugin must also fail early,
on its author's machine or pull request, with messages that say what to
fix, rather than at deploy time.

Scientific values carry units. Labs, LIMS and FHIR use UCUM codes, so Iki
should too, and it must check and convert them itself.

## Decision

**Declarations are data, behaviour is code.** Entity types, extensions,
fields and settings are YAML manifests. Code appears only in actions, which
run isolated in workers. A plugin can't add model methods or signals that
would bypass revisions, provenance or edit rules.

**The spec is a set of JSON Schemas (draft 2020-12),** one per kind, at
`https://ikisci.org/schemas/<name>/v1`: `plugin`, `entity-type`,
`entity-type-extension`, `field`, `settings`, `unit`, and `manifest`, which
dispatches on `kind`. The validator loads them from the package and never
fetches anything.

**The layout is fixed.** `plugin.yaml` holds the Plugin manifest, and
`types/**/*.yaml` hold entity types and extensions. Each later kind will get
its own directory.

**YAML is read strictly, as YAML 1.2.** Only `true` and `false` are
booleans. Duplicate keys, aliases, merge keys and tags are refused, and so
are symbolic links and files over 1 MiB. What a reviewer reads is exactly
what Iki reads, and a manifest can't expand into something huge.

**Validation runs in two stages.**

- _Schemas._ Each document is checked against its schema. jsonschema's
  errors are rewritten into plain sentences with "did you mean" hints.
- _Semantics._ These are the checks no schema can express:
  - UCUM units, and whether a display unit can show a field's values
  - references between types, across files and across plugins
  - subtype cycles and fields that shadow a parent's
  - HRID rules
  - identities, which must be required and immutable
  - indexes: indexable types, and a length limit that keeps B-tree entries valid
  - patterns that both Python and PostgreSQL support
  - defaults that fit their field
  - PostgreSQL's 63-character name limit
  - reserved names
  - extensions, whose fields can't be required or clash with the type's
  - dependency constraints, against schema versions
  - settings

Every problem has a stable code, a file, a line and column, and a hint where
one helps. `iki plugin validate` prints problems the way compilers do. It can
also emit JSON, or GitHub annotations that show on a pull request's diff.

**Dependencies must be present to be checked.** They are passed with
`--with`. When one is missing, the validator says once that it can't check
that plugin, instead of guessing or reporting each use. `core.Entity` is
built in. Using another plugin's type without saying which schema versions
the plugin works with is a warning.

**Field types are those of ADR 0007, plus `date`.** Lists are written
`{type: list, items: {…}}` rather than `list<T>`, so items can carry options
such as `to` or `values`, and editors can complete them.

**Settings aren't secret.** They live in each deployment's config
repository, so a setting named like a secret gets a warning that points to
action secrets. A setting is either required or has a default.

**UCUM, implemented by Iki, over the official table.** Iki bundles UCUM 2.2's
`ucum-essence.xml`, byte for byte, under Regenstrief's UCUM License (version
1.1). A test pins its SHA-256. That licence allows redistribution, including
in commercial products, if the table is unmodified and its notice ships with
it. Our NOTICE and the package include that notice.

The parser and converter are Iki's own Apache-2.0 code:

- They are case-sensitive.
- They handle prefixes, annotations and arbitrary units.
- They handle special units such as `Cel`, `[pH]` and decibels. These can't
  be combined with other units.
- They suggest UCUM spellings for common mistakes (`Da` becomes `u`, `°C`
  becomes `Cel`).

This is the precedent set by FHIR tooling such as HAPI's ucum-java and NLM's
ucum-lhc.

## Consequences

- Positive: one written contract, which editors, other languages and future
  codemods can use.
- Positive: plugins fail in seconds, in the author's editor or CI, with the
  line and the fix.
- Positive: units are checked against the standard labs already use, and
  convert correctly, including temperatures.
- Negative: rules live in two places, the schemas and the semantic checks.
  Tests cover both, but a rule must be added in the right one.
- Negative: the UCUM License is not an OSI licence. It forbids modifying the
  table and asks redistributors to indemnify its contributors. Pharma legal
  teams may ask about it. FHIR's wide use of the same terms is the usual
  answer.
- Negative: the schemas' URLs resolve only once ikisci.org is published.
  Until then, editors need the files from this package.

## Alternatives considered

- **Entity types as Python classes or Django models.** Rejected. Every
  reader would have to run plugin code, only Python could read it, codemods
  get much harder, and plugins could bypass the audited write path.
- **PyYAML's default YAML 1.1 loader.** Rejected. It turns `no` and `on`
  into booleans and silently keeps the last of two duplicate keys.
- **Pint or ucumvert for units.** Pint doesn't speak UCUM. ucumvert maps a
  subset of UCUM onto Pint. Neither keeps to the UCUM table as the single
  source of truth.
- **Writing our own unit table.** Rejected. It would be a modified or
  derived UCUM table, which the licence forbids, and it would drift from the
  standard.
