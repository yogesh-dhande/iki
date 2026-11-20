# 0157. A pool has the concentration of what was pooled

- Status: Accepted
- Date: 2026-11-19
- Refines: [0139](0139-a-records-amount-is-a-ledger-of-what-moved.md)

## Context

ADR 0139 keeps a record's amount as a ledger. A type with a `concentration`
field converts a mass asked of a volume through it: 50 µg of a 2 mg/mL sample
is 25 µL. Transfers are how samples are pooled or topped up. Two things went
wrong between them:

- **A pool kept its old concentration.** Moving 1 mL at 1 mg/mL into 1 mL at
  3 mg/mL gave 2 mL still recorded at 3 mg/mL. The pool holds 4 mg in 2 mL,
  2 mg/mL, so the next 1 mg draw was recorded as 333.33 µL when it takes
  500 µL. Nothing changed the concentration, and on a locked record (a
  released sample) nobody could.
- **A transfer could debit one side and credit nothing.** Each side is kept to
  12 decimal places in its own unit. Moving 0.4 pL from a record kept in pL to
  one kept in L credited 0 L, and 2.5 pL credited 2 pL: the source lost what
  the destination never gained. A draw of 0.4 pL from the L record took
  nothing, yet recorded a draw.

## Decision

**A transfer into a record with a concentration sets it to the pool's.** The
pool's concentration is the solute of both over their volume (IUPAC's mass,
or amount, concentration). The solute and volume of each side follow from its
amount and its concentration, whichever of the two the amount is kept in. It
is the solute's, whatever the solute is, which is what a mass draw converts
through.

- When the record it came from has no concentration, or one that doesn't
  measure the same kind of thing, the pool's isn't known. Its concentration is
  emptied, so a mass draw is refused until someone measures it and sets it.
- Into an empty record, the pool's is what came in.
- When the pool's is what it was (equal concentrations), nothing changes.

The change is a revision of the record, by the person who transferred, with
the transfer as its reason, so the record's history says why its
concentration changed. A record that can't take the change, because a state
locks it or the field is required, refuses the transfer, saying why, and
nothing moves. Pooling into a released sample changes what was released, so
it takes only material in the same state, released too, and then only if
its concentration stays the same. An equal concentration isn't enough on
its own: quarantined material at the same 100 µM is still untested (ADR
0162).

**Every movement is exact on both sides.** A quantity given in another unit
is converted with UCUM's factors as fractions, as before. If the record's 12
places can't hold the result exactly, it's refused, naming the exact value,
instead of rounded. A transfer's credit must be exactly its debit. A mass
converted through a concentration can't be exact (1 mg at 3 mg/mL is a third
of a millilitre), so it is still rounded, half to even, to 12 places. That's
far below what any concentration is measured to. But it's refused if it rounds
to nothing.

## Consequences

- Draws by mass from a pool take what it holds, and its history shows each
  concentration it had and why.
- Pooling material whose concentration isn't recorded, such as a buffer
  without one, leaves the pool's unknown. Recording the buffer at 0 mg/mL
  makes it a dilution instead.
- A record kept in a coarse unit (L) refuses movements finer than its 12
  places (a picolitre). Its type should keep a finer unit if it needs them.
- A record's concentration is the solute's, not one analyte's. A pool of two
  different materials has their total concentration. Tracking each analyte of
  a mixture would need a record of its contents, which Iki doesn't model. So
  a type whose amount is of one entity (`material`, such as a lot) refuses
  material of another (ADR 0162): a mixture is a record of its own.
