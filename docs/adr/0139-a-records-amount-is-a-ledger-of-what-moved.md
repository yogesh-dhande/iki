# 0139. A record's amount is a ledger of what moved

- Status: Accepted (pooling and exact movements refined by [0157](0157-a-pool-has-the-concentration-of-what-was-pooled.md))
- Date: 2026-11-18
- Refines: [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0123](0123-queries-walk-the-graph-of-what-depends-on-what.md),
  [0125](0125-a-state-is-a-field-each-state-says-who-sets-it.md)

## Context

A lab keeps track of how much of each thing it has: a sample's volume, a
lot's mass, the vials in a box. That amount goes down when someone uses
some, moves between tubes when samples are pooled, and splits when a sample
is aliquoted. A plugin could store it as a quantity field, as
`example_lims`'s `Aliquot.volume` did. Then three things go wrong:

- **It can go below zero, or lose a draw.** Two analysts each read 10 µL,
  each draw 6 µL, and each write back 4 µL. One draw is lost, and the tube
  has less than the record says. Optimistic revisions (`If-Match`) refuse
  the second write, but then the analyst has to read it again and retry, and
  a run can't.
- **Nothing says what happened to it.** A field's revisions show it went
  from 10 to 4. They don't show that it was a draw of 6 µL, for which assay,
  or that 30 µL went to another tube, which tube, or that a split took
  60 µL into three new aliquots. Inventory reconciliation, and GxP review of
  what was consumed, need each movement and its reason.
- **It's frozen with the record.** A released sample is locked (ADR 0125):
  its values can't change. Releasing a sample is what makes it usable, so
  using it has to keep working.

LIMS and ELN products (Benchling's inventory, LabWare and STARLIMS) keep
an amount per container, changed by check-out, transfer and aliquoting
transactions, each logged. Ledgers in accounting work the same way: the
balance is derived from movements that are never edited.

## Decision

**An amount is the type's, kept by Iki.** A type declares `amount:` with a
UCUM `unit` (a volume, a mass, an amount of substance or a count), a
`label`, an optional `display_unit`, an optional `concentration` field, and
optional `aliquots: { type, of }`. Iki keeps the amount in its own table
(`iki_amount`): one row per record, an exact decimal (40 digits, 12 after
the point) in the type's unit, with a database check that it's never below
zero. What a plugin declares is what it is and how it converts. How it moves
is the same for every plugin, so it's core.

**It changes only through operations, each a movement.** Recorded when a
record is made (`POST /entities` with `amount`), then drawn, transferred,
split into aliquots, or adjusted (with a reason required). Each operation
appends movements to `iki_amount_movement`, append-only for every role as
history is (the same triggers as revisions). A movement keeps its kind, the
signed change, the balance after, what was asked for in the unit it was
asked in (exactly as given, to 40 places either side of the point, and
refused beyond them, since 0.0000000000001 L is exactly 0.0000001 µL), the
other record, an operation ID shared by a transfer's two
sides and a split's movements, and who did it, via what, with which agent
(ADR 0072), and why. Each is an audit event and a webhook
event, `entity.amount_moved`.

Each draw records its `purpose`, either `routine` (the default) or
`qa_sampling`. The optional reason can name the assay or test.

**A type says who can draw for a purpose.** For example,
`draw_purposes: {qa_sampling: {by: [qa]}}` allows QA sampling for a caller
who can draw from the source record and holds the declared lab role in its
owning project (ADR 0116). One role in the list suffices, granted directly
or through a team. Admins need the same grant. For a delegated token, the
person and the service account must each qualify (ADR 0073). Each purpose
inherits its nearest ancestor's declaration. A subtype may narrow the list
of roles.
Routine use is available to anyone with draw permission unless a role list
restricts it. QA sampling is refused unless the type or an ancestor declares
who can perform it. All purposes are subject to the record's holds and
draw guards.

**A withdrawal locks the record and its amount.** The server locks the
records involved in ID order, then locks their amounts in the same order.
That keeps the values used to check eligibility stable through the movement
and lets transfers in opposite directions finish without deadlocking. A
second draw waits for the first to commit, then sees what's left. The
service refuses a draw above the balance, and the database checks that
the amount stays at or above zero.

**Conversion is exact.** An amount given in another unit is converted with
the UCUM factors as fractions, then rounded once, half to even, to 12
places. A type with a `concentration` field (mg/mL, mmol/L) also takes a
mass for a volume, or a volume for a mass, at that record's concentration.
So 50 µg of a 2 mg/mL sample is 25 µL. A record without a concentration
refuses the conversion and says why.

**A split makes records.** Splitting into _n_ aliquots of _x_ each creates
_n_ records in the source's project. They're of its own type by default,
with its values but not its unique fields or its states. Or they're of
`aliquots.type`, with `aliquots.of` (its reference to the source type) set.
Each aliquot's first movement records _x_, and the source loses _n·x_
in the same transaction: all of them or none, so nothing is made or lost.
The aliquot type's unit must measure the same thing as the source's.

**What moved is an edge of the graph.** A trigger on the movements adds an
`aliquot` edge from each aliquot to its source, and a `transfer` edge to the
record that received some, once per pair (ADR 0123). Upstream and downstream
walks follow them like references, so a recall, or a donor's withdrawn
consent, reaches everything split or pooled from a sample.

A destination type can declare `draws_received` to include draw evidence in
its own revision (ADR 0189). The draw appends the ledger movement ID, the
material record and revision, the normalized amount and unit, and who drew
it and when. When the draw came from a container, the receipt can also name
its upstream lot and revision under the receiving project's sharing rules.
Iki locks the participating records before writing the debit and receipt in
one transaction. A locked destination refuses the draw.

**A state can hold it.** A state with `holds_amount: true` (a quarantine, a
rejection, a sample not yet received) refuses draws, transfers out and
splits while the record is in it. Adjusting still works, because a recount is
a fact, not a use. Locking doesn't hold: the amount is not one of the
record's values, isn't in its revisions, and changes on a released sample,
which is what release is for.

**A type can say when material may be taken.** A type declares named
`draw_guards`, inherited by its subtypes. Like a state guard (ADR 0128),
each has a CEL `check`, a refusal `message` and an optional `when`.
It can read the source `record`, `now`, `today` (midnight UTC),
the `operation` (`draw`, `transfer` or `split`) and the `purpose`.
References are their stored IDs, so showing a refusal on the source record
doesn't disclose fields from another project.
Transfers out and splits use their usual permissions. For the guard checks
they count as routine use. The draw dialog shows the
reason a purpose is blocked. The server checks eligibility again when it
takes the material. If a state holds the amount, or an applicable check
is false or cannot be evaluated, the server refuses the operation before
recording any movement.

**Not on identities.** A type with an identity has no amount
(`amount-on-identity`). Its project records are one per registry entry
(ADR 0115), the molecule, not a tube of it. The lab's things are records of
their own that refer to the entry: a sample, a lot.

**What's left to plugins.** Where something is kept (a freezer, a rack, a
well) stays a field, as `location` is in `example_lims`. Containers,
plates and their positions aren't modelled here. A plugin can use a state
to hold an amount or a draw guard to check a value such as a date at the
time of use.

## Consequences

- One way to keep inventory across plugins. Two draws at once can't both
  take the last of a sample. Every change says who made it and why, from
  the web app, the API, the SDK, the CLI or an agent (MCP).
- A record's amount isn't in its revisions or a bundle's records: its ledger
  is where its history is. A bundle doesn't carry amounts yet, and neither
  does an action run's `creates`, a search condition or the grid. Each is a
  follow-up, not a change to this model.
- A heavily used record serializes its draws. That's the point, and a draw
  holds the lock only for one short transaction.
- Splitting 200 aliquots makes 200 records in one transaction, the most a
  split takes (`MAX_ALIQUOTS`).
- `example_lims`'s `Aliquot.volume` is gone. It's the Aliquot's amount now,
  and the Sample's volume splits into Aliquots that name it.
