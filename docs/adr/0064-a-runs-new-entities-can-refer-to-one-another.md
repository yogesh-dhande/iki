# 0064. A run's new entities can refer to one another

- Status: Accepted
- Date: 2026-11-12
- Refines: [0044](0044-run-results-are-written-all-or-nothing.md),
  [0055](0055-batches-and-packing.md),
  [0056](0056-bulk-imports.md)

## Context

A run's `output.json` lists the entities it creates (ADR 0044). A ref field
takes an entity's ID, and a new entity has none until it's written. So a run
couldn't make a protein structure that refers to the chains it made with it,
or an antibody that refers to its new heavy and light chains. A second action
to link them afterwards would leave half-made entities in between, and nothing
to say which went with which.

## Decision

**A create can have a `key`**, unique in `creates`. **A later create's ref
field, or an item of a list of refs, can be `{"$create": "<key>"}`.** When the
results are written, in the same transaction as ever, it becomes the ID of the
entity that create made, or the one it reused, for types that reuse
duplicates. A create refers only to those before it, so there are no cycles
and the order they're written in is the order they're given. A key no earlier
create has, or one given twice, fails the output with where it is.

**A packed job's creates refer only to their own run's** (ADR 0055). Each
run's results are written on their own, so a reference across runs couldn't
be kept. The split says which create's reference crosses.

**A creates file's lines can't** (ADR 0056). Its lines are checked a chunk at
a time and written with `INSERT … SELECT`, and can't attach files either.
Runs that make entities that refer to one another, such as structures, make
few enough of them for `creates` (up to 10,000).

## Consequences

- An import from the PDB makes the structure and its chains in one run, all
  or nothing, linked as they are in the file.
- Commands give keys of their own choosing, such as a chain's ID, so their
  output reads plainly.
- `{"$create": …}` means something only in ref fields. Elsewhere, such as in
  a JSON field, it's kept as written.

## Alternatives considered

- **IDs the command makes up.** Iki gives entities UUIDv7s it makes itself;
  letting commands choose would let them collide with, or guess, others'.
- **Writing creates in dependency order, whatever order they're given.** It
  needs cycle checks and hides mistakes. Earlier-only is simple to explain and
  to check.
