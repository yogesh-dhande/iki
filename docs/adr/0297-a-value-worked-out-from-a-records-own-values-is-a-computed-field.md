# 0297. A value worked out from a record's own values is a computed field

- Status: Accepted
- Date: 2026-11-27
- Amends: [0007](0007-typed-table-per-type-entity-storage.md),
  [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0127](0127-plugins-rules-are-checked-at-every-save.md)
- Refines: [0080](0080-triggers-run-actions-when-entities-change.md)

## Context

Some values follow from a record's other values and nothing else: a polymer
lot's dispersity is its Mw over its Mn; a buffer expires a set time after it
was made; a dilution's final concentration is its stock's over its factor.
Today a plugin keeps one with `computed: true` and an action that sets it:
an action manifest, an environment pinned to an image, a script, its tests,
and two triggers, one on `entity.created` and one on `entity.updated` with
a hand-kept list of the fields it reads (ADR 0080). Each part can drift from
the others. A field added to the formula but not to the trigger's list goes
stale without anyone noticing. And the value is right only once a run has
finished: until then a search, an export or a rule reads the old one.

LIMS and ELNs have calculated fields. LabWare's calculated result components
are worked out from the same sample's other components when results are
entered, and the audit trail shows the calculated value changing with them.
Benchling's computed fields are read-only (the API marks them
`derivationType: COMPUTED`; neither a person nor an integration writes
one), but only Benchling's support configures them, from a fixed menu of
formulas. Its hoisted fields, which copy a linked entity's value, depend on
records other than their own, and Benchling has had to tell customers that
some didn't recompute when their source changed. Data integrity guidance
for calculations (FDA's 2018 guidance, MHRA's GxP data integrity guidance)
asks that a calculation be validated, that its version be known, and that a
calculated value be reproducible from the data it was calculated from.

## Decision

**A field can be computed by an expression**: `computed: {cel: "…"}`, beside
`computed: true`, which a run or Iki sets. The expression is CEL, as rules
and guards are (ADR 0127), type-checked by the same iki-wasm module when the
plugin is validated, and evaluated by the server:

```yaml
dispersity:
  type: float
  computed:
    cel: double(int(record.mw / record.mn * 1000.0 + 0.5)) / 1000.0
```

**It reads the record's own values, and nothing else.** `record` is the
record as it will be saved: its own fields, its ancestors' and those an
extension adds. It can't read:

- what a reference refers to, nor a reference at all. The other record
  changes without this one being saved, so its value would go stale, as
  Benchling's hoisted fields did. A value from another record is an
  action's, which includes it and is flagged for recalculation when it
  changes (ADR 0080's `watch_included`).
- a registry entry's fields (ADR 0115). A registrar changes them without any
  project's record being saved.
- a state field (ADR 0125). A state is set by its own step, not by a save of
  the record's values; what depends on it belongs in its guards.
- `now` or `today`. A value worked out from the clock would be different
  tomorrow, for the same record.
- its `id` or `hrid`. A new record's HRID is given once its values are worked
  out, and neither is a value anyone measured.

So a revision's value is what the expression gives for that revision's other
values, with the plugin version that was installed when it was saved: it can
be worked out again from the revision alone.

**It's worked out at every save**, before the record's rules are checked
(ADR 0127), in the server, whoever or whatever saves it: a person's create
or edit in the web app, the API, MCP or the CLI, a run's results, a bulk or
spreadsheet import, a bundle's records. The value is written to its column
and its history, as any value is, and the save's revision names it among the
fields it changed. Rules, guards, searches, exports and triggers read it as
saved. No one sets it: a person's, an agent's or a run's value for it is
refused (`is worked out by Iki from the record's other values: no one sets
it`), and the validator refuses an action that declares it among its updates
(`updates-cel-computed`).

**It's one value of the field's kind.** The field is a `string`, `text`,
`int`, `float`, `quantity`, `bool`, `enum`, `date` or `datetime`. The
validator checks that the expression works out to that kind: a number for a
`float` or a `quantity`, a whole number for an `int`, a time for a `date` or
a `datetime`, text for the rest, and for an `enum`, text that can only be one
of its values when it's written out (`'high'`, or either branch of a
`? :`). A quantity is a number in its field's `unit`, as stored, both what
the expression reads and what it gives: never the unit someone typed it in
or the display unit. A date is read as midnight UTC, as rules read it, and a
time given to a `date` field is the date it falls on in UTC. A value outside
the field's limits (`min`, `max`, a length or a pattern) is refused, as it
would be if it were typed: the field says what its values may be, however
they're worked out.

**What can't be worked out is empty.** An expression that reads an empty
value, divides by zero, or gives text that isn't one of an enum's values
leaves the field empty, and the save goes on: the values it's worked out from
are what was measured, and a derived value missing is no reason to refuse
them. That's the rule's empty-value semantics (a check that reads an empty
value passes), not the guard's (a guard that can't be worked out refuses):
both are written beside each other in the spec's README. A failure other than
an empty value is logged (`entities.computed_unworkable`), with the field and
the error.

**Computed fields can read each other**, and are worked out in the order
their reads give. A cycle is refused when the plugin is validated
(`computed-cycle`).

**What a computed field can't be.** The validator refuses one that's:

- `required` or has a `default`: its value is the expression's
  (`computed-required`, `computed-with-default`);
- `immutable`: it's worked out again at every save (`computed-immutable`);
- `unique`, or one of a `unique` or `no_overlap` rule's fields: a clash on a
  value no one typed can't be fixed by changing it; declare uniqueness on the
  fields it's worked out from (`computed-unique`);
- a `registry` field, or part of an identity: an entry's values aren't saved
  with a project's record (`computed-registry`);
- of a kind it can't be: a reference, a file, a person, a list, JSON or a state
  (`computed-kind`);
- reading what it may not: a reference, a registry field, a state field,
  `id`, `hrid`, `now` or `today` (`computed-reads`), or an expression that
  doesn't type-check (`invalid-expression`).

**When its expression changes, the deploy works it out again.** A deploy whose
plugins add a computed field, or change one's expression, works out every
record of the types it's on again, before it checks their rules again (ADR
0127's recheck), and keeps a digest per type of what it worked them out by. A
record whose value differs gets a `migrated` revision by no one, `via` the
system, saying which plugin version worked it out, as a plugin's migration's
are, and the deploy's audit event (`plugin.computed_worked_out`) counts them
by field. A locked record keeps the value it was locked with: what was
approved, signed or released is what was reviewed, and the database refuses
to change a locked record (ADR 0125). The audit event counts those too, by
field, and the record takes the new value at its first save once it's
unlocked. A bundle's records are worked out again when they're imported,
after their history from the other deployment, the same way.

**The type page shows the expression.** `GET /entity-types` gives each
computed field's `cel` with the fields it reads, so the web app shows it as
computed, never as an input, with its expression on the type's page.

## Consequences

- A value worked out from a record's own values is one line of its type,
  rather than an action, an environment, a script, its tests and two triggers.
  It's right as soon as the record is saved, on every path, with no worker.
- The lots plugin's polymer lot dispersity is one. Its action, triggers and
  script function are gone.
- The expression is the plugin's, versioned with it, and the history of every
  value it gives says which revision, and which plugin version, gave it.
- A value from another record, the clock, or a library such as RDKit stays an
  action's, and an action is still its own plugin's: running another plugin's
  (ADR 0079's "come later") is unchanged. CEL has no rounding function: rounding to three places is
  `double(int(x * 1000.0 + 0.5)) / 1000.0`, for a number that isn't negative.
- An expression's dimensions aren't checked: a quantity is a number in its
  field's unit, and the author makes the units agree.
- A deploy that changes an expression can write a revision on every record of
  a type. That's what the lab's change control reviews before the upgrade.
- A locked record can hold a value an older expression gave, until it's
  unlocked and saved: the deploy's audit event says how many, so the lab can
  decide whether to reopen them.

## Alternatives considered

- **Keep actions for derived values.** They're what a value that needs a
  library or another record needs, but for arithmetic on a record's own
  values they're five files to keep in step, and the value lags its inputs.
- **Let an expression read references, kept up to date by fan-out.** Each
  change to a referenced record would have to save every record referring to
  it, across projects, writing revisions in projects whose members never
  touched them. Benchling's staleness notice is what happens when that
  fan-out misses one. An action with `watch_included` flags the record for a
  person to recalculate instead.
- **Refuse a save whose computed value can't be worked out.** It would block
  recording a measurement because the value derived from it is undefined
  (an Mn of 0). The derived value's absence is visible on the record.
- **Leave records as they are when an expression changes, and work each out at
  its next save.** A search or an export would then mix values from two
  formulas, and an unrelated edit would silently change one. Working them all
  out at the deploy keeps one formula's values, each change with a reason.
- **Rework locked records too.** It would change what was approved or signed
  without anyone reviewing it again, and would need the deploy to unlock them.
  A lab that wants the new value on a released record reopens it, under its
  own procedure.
- **A rounding or units library in CEL.** cel-rust has cel-go's math
  extension, but rules and guards don't use it yet; adding it is a separate
  change to the language every expression in Iki shares.
