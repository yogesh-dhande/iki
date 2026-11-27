# 0283. A subtype narrows what it inherits

- Status: Accepted
- Date: 2026-11-27
- Amends: [0164](0164-a-subtype-says-which-roles-its-rows-take.md),
  [0235](0235-a-services-own-request-types-carry-its-fields.md)

## Context

A subtype could narrow an inherited field in two ways: `refines` with `roles`
gave a list of rows' roles their own counts and types (ADR 0164), and
`refines` with `to` held a reference to fewer types (ADR 0235). Nothing else
could be narrowed. A lab whose GMP lots must name their material, or whose
clinical lots take only the grades its release allows, wrote a rule
(`check: record.material != null`) that says the same thing less clearly,
checks only on save and isn't a constraint the database holds. The analytical
result type did exactly that.

A subtype's records are each one of its parent's too (ADR 0007): whatever is
true of every parent record must stay true of the subtype's. That's the Liskov
substitution principle, and schema languages that refine an inherited slot
hold to it: LinkML's `slot_usage` may make a slot required, narrow its range
or tighten its bounds but not loosen them, and JSON Schema's `allOf` can only
add constraints.

## Decision

**`refines` narrows any part of an inherited field that a narrower value
fits within.** For each field, any of:

| Part       | Narrowing                                                                               |
| ---------- | --------------------------------------------------------------------------------------- |
| `required` | `true`: its records need it. Not for a computed field.                                  |
| `values`   | Some of an enum's values (or a list of enums' items').                                  |
| `min`      | A higher lower bound on a number.                                                       |
| `max`      | A lower upper bound.                                                                    |
| `default`  | A default of its own, among the values it allows, for a field that takes one.           |
| `to`       | Fewer types for a reference: each one a type it allowed or a subtype of one (ADR 0235). |
| `roles`    | A list of rows' roles, as ADR 0164 gives them.                                          |

Each part is checked against the field as the subtype's parent has it, after
every narrowing above it (ADR 0282), so a narrowing of a narrowing is held
to the nearer one: a clinical lot under a GMP lot whose purity is at least 95
can raise it to 99, not lower it to 90. Anything wider is refused
(`refines-wider`), as is narrowing a field the type declares itself
(`refines-own-field`), one no ancestor has (`refines-unknown-field`), and a
part that doesn't apply to the field.

**Its records are held to it everywhere.** The type's effective fields are the
narrowed ones, so forms, imports, runs and the API check and fill them with
the narrowing. The database holds `required`, `values`, `min` and `max` too:
the column is in the declaring type's table, whose CHECKs can't tell one
subtype's rows from another's, so `sync_plugins` puts a trigger on that table
holding each narrowed column for records of the narrowing type and its
subtypes. Records already there that don't fit stop the deploy and are named,
as with a rule (ADR 0127). A narrowed default is the subtype's records'
default; the column's default stays the declaring type's.

**`refines` follows the `narrow` policy** of ADR 0282: inherited, and only
ever narrower.

## Consequences

- A lab writes `refines: { material: { required: true } }` where it wrote a
  rule; the first-party analytical result does.
- `iki_spec.model.Refinement` replaces the reference- and role-only forms, and
  their helpers are gone.
- ADR 0164's `roles` and ADR 0235's `to` are two parts of the one block; their
  checks are unchanged.
