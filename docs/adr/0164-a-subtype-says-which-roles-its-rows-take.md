# 0164. A subtype says which roles its rows take

- Status: Accepted
- Amended by [0283](0283-a-subtype-narrows-what-it-inherits.md): `refines` narrows any inherited part, of which these are two
- Date: 2026-11-19
- Refines: [0137](0137-a-list-can-hold-rows-of-typed-fields.md),
  [0161](0161-a-complex-is-its-parts-and-their-copies.md)

## Context

A list of rows' roles (ADR 0137) are fixed where the list is declared: `by`
names an enum of the row, and `roles` gives each value its count and what
its references may refer to. A complex's `components` are declared once, on
the abstract `iki/core.Complex` (ADR 0161), so search, the graph and the
identity see one list for every type. Each type of complex takes its own
parts, though: an antibody a heavy chain and at most one light chain, each a
protein; a lab's own bispecific two heavy chains and a light chain; a
lab's own type whatever its format takes. An enum
on the abstract type would have to list every role of every type, Iki's and
every lab's, and couldn't say what each type's rows take.

Benchling's and Dotmatics' templates do the same thing: one chain table for
every biologic, and a template per format that says which chains, how many
and of what.

## Decision

**A role can be a string.** `by` may name a required string field, with a
`pattern` (the complex's role is lower-case words, `^[a-z][a-z0-9]*(_[a-z0-9]+)*$`).
Without `roles`, a row may have any role its pattern takes, as a protein
complex's may (`alpha`, `beta`, `heme`). With `roles`, those are every role
a row may have: a row with another is refused, saying which it may have
("isn't one of its roles: heavy, light").

**A subtype refines an inherited list's roles**:

```yaml
extends: iki/core.Complex
refines:
  components:
    roles:
      heavy: { label: Heavy chain, min: 1, max: 1, to: { part: iki/protein.Protein } }
      light: { label: Light chain, max: 1, to: { part: iki/protein.Protein } }
```

A refinement names a list of rows an ancestor declares (`refines-own-field`,
`refines-not-rows`), and its roles are every role the subtype's rows may
have. Each is no wider than what its parent allows: a role its parent's
`by` field takes (an enum's value, or a string its pattern takes), a count
within the parent's role's, and types among those the parent's role, or the
row field, refers to, each with its subtypes (`refines-wider`,
`row-role-type-not-allowed`). A refinement of a list in the identity refers
only to types with entries (`registry-refers-to-record`). A subtype of a
refining subtype refines it again, narrower still, and is compared with all
of its parent's roles, not only those it repeats: it keeps every role its
parent's rows must have (`refines-drops-role`), and a role whose references
its parent holds to some types says them again, or types among them, since a
refinement's roles replace its parent's and one left out would refer to
anything its field does (`refines-wider`). A role its parent's rows may have
but needn't, it may leave out: that narrows them.

**Every surface reads the refined rows.** Saving checks a record's rows by
its own type's roles; the API's type gives `components` with its type's
roles and `closed: true`; the web app's form offers a closed string role as
a choice of its roles by label. The rows' table, their search and their
graph edges stay the declaring type's, so searching `iki/core.Complex` for a
part finds an antibody beside a protein complex.

## Consequences

- Iki's types and a lab's add their own formats without changing the
  abstract type or each other.
- A row's role is checked by the type the record is, wherever it's saved:
  the form, the API, a bulk import, a bundle (ADR 0127).
- A role is a string in the database, so roles that differ only by type
  share one column, and search by role reads one table.
- A refinement can't add cells to a row, only say what its roles take: a
  type that needs more about each part keeps it in a list of its own, as the
  test plugin's bispecific's arms are.
