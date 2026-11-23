# 0180. Where material is, and how it's received, moved and counted

- Status: Accepted
- Date: 2026-11-22
- Refines: [0139](0139-a-records-amount-is-a-ledger-of-what-moved.md),
  [0132](0132-a-request-is-owned-by-the-project-that-asks-and-served-by-another.md),
  [0162](0162-a-lot-is-a-projects-record-of-an-entrys-material.md)
- Builds on: [0072](0072-agents-changes-name-the-person-and-the-agent.md),
  [0123](0123-queries-walk-the-graph-of-what-depends-on-what.md),
  [0129](0129-types-declare-their-notifications.md)

## Context

A lab keeps its material in freezers, on shelves, in racks and in boxes, and
a tube is at one position in one box. `example_lims` modelled this with
fields: a Box had a free-text `location` and a count of `positions`, and a
Tube a `position` such as `A1` that matched a pattern. Nothing said what was
in a freezer, that `J12` isn't a position in a 9 × 9 box, or where a tube had
been. A Tube's volume was a quantity field anyone could overwrite, while an
Aliquot's was the ledger (ADR 0139), so the same material had two models.

What's missing is what scientists do at the bench, every day, and what GxP
asks of it:

- **Receiving.** A core facility gets a box of tubes with a request
  (ADR 0132). It checks each against what was meant to come, records what's
  missing, extra, damaged or refused, takes custody, and puts them away. The
  requester needs to hear what didn't arrive as expected.
- **Finding and moving.** Someone looks for a tube: freezer, rack, box, slot.
  They move many at once when a freezer is defrosted, check one out to work
  with, and bring it back. Each move says who, when and why: a sample's
  chain of custody.
- **Labelling and scanning.** A tube's label carries a barcode, and a
  scanner confirms the tube in someone's hand is the one on the screen,
  before they draw from it or move it.
- **Counting.** A box is scanned, and what's in it is compared with what's
  recorded: what's matched, missing, unexpected or in the wrong slot.
- **Using.** Drawing from a tube takes from its balance, says what the
  material went into, and is refused when its lot has expired or is due for
  retest (ADR 0162).

Benchling's inventory nests locations, boxes and plates with a grid per box
schema, keeps a container's amount and contents, and checks containers out
to people. Labguru and eLabNext have box maps, bulk moves and rack-scanner
reconciliation (eLabNext's RackScan compares scanned and recorded positions).
LabWare records multi-sample login and chain of custody, and LabWare Mobile
receives with a camera. Each makes location a property of the inventory, not
a value someone types in.

## Decision

### Where something is kept is Iki's, as its amount is

**A type declares `storage:`, and Iki keeps where each of its records is.**
What a plugin declares is what its records are: what a container holds, the
shape of its positions, the field holding its barcode. Where each record is,
how it moves, and who has it are the same for every plugin, so they're core:

```yaml
# A freezer or a shelf: holds other places and boxes, with no positions.
storage: { holds: [Rack, Box] }
# A box: positions in rows and columns.
storage:
  holds: [Tube]
  grid: { rows: rows, columns: columns, labels: letter_number, order: by_row }
  barcode: barcode
# A tube: kept somewhere, labelled with its barcode, scanned before a draw.
storage: { barcode: barcode, label: "{{ sample }}", confirm: [draw] }
```

A record of a type with `storage` can be kept in a record of a type that
`holds` its type or an ancestor. A container's `grid` gives its rows and
columns, either fixed numbers (a 96-well plate is 8 × 12) or the names of its
own required, immutable whole-number fields (a box is 9 × 9 or 10 × 10, as
the lab orders it), so a container's shape never changes under what's in it.
Positions are labelled `letter_number` (rows A, B, …, Z, AA, … and columns 1,
2, …: `A1` to `H12`) or `number` (1 to 81), and numbered and filled
`by_row` (A1, A2, …) or `by_column` (A1, B1, …). A grid has at most 32 rows,
48 columns, and 1,536 positions: a 1536-well plate. Any container may have a
grid: a rack of boxes and a shelf of positions are grids too.

**Each record is in one place: `iki_storage_place`.** One row per record
that's been kept anywhere: the container it's in (or none, once it's out of
storage), its row and column (for a container with a grid), and who has it
checked out. The database refuses two records in one position of one
container, and a row or column outside the container's grid, whatever writes
them: each container's shape is kept in `iki_storage_container` when it's
first used, and a trigger checks each position against it. The check that
two records aren't in one position is deferred to the end of a transaction,
so a move that swaps two tubes is one change. Iki refuses a container
inside itself, through any depth.

**A position is a container's, and the container's project decides it.**
Putting something in a container, or taking it out, needs Editor in the
container's project: it's that project's freezer. A record may go into a
container only if it's in the container's project (its own, or linked there,
as a request's samples are): everyone who reads a box can read what's in
it. Moving a record doesn't need Editor on the record, and a locked record (a
released sample) moves: where it is isn't one of its values, as its amount
isn't, and isn't in its revisions.

**Where it is is shown as far as you can read.** A record's place is the
path from the outermost container down: Freezer 3 › Rack B › BOX-000014 ›
C7. A container the reader can't read ends the path at "a place you can't
open", and nothing inside it: no name, no project, no position. Another
project's boxes never show in a reader's storage, search or box maps.

**Every change is a movement of a ledger: `iki_storage_event`.** Append-only,
as revisions and amounts are. Each is `placed` (into storage from nowhere),
`moved`, `removed` (out of storage: used up, discarded, shipped),
`checked_out` (to a person, its position kept for it), `checked_in`,
`handed_over` (from one person to another while it's out), or `received`
(custody taken with a request's receipt). Each keeps where it was and where
it went, who has it, the operation it was part of, who did it, via what,
with which agent (ADR 0072), and why. A move and a removal need a reason;
the others may have one. Each is an audit event, `storage.moved`, and a
webhook event. The record's page shows its location history beside its
amount's.

**Many moves are one change.** A bulk move names each record and its
container and position, or asks for the container's free positions in its
order. Iki locks the records' places and the containers in ID order, checks
every move, and applies all of them or none, with one reason and one
operation ID. A preview gives the same plan without changing anything, so
the person sees each record's new position, and anything refused, before
they confirm. Moving a box moves what's in it: its tubes stay in its
positions, and no tube's place changes.

**Checking out keeps its position.** A record checked out is still in its
container's position, which nothing else can take, and is with its
custodian, a person who can read it. Checking it in returns it there, or
moves it to another position, in the same change. Handing it to someone else
while it's out is a movement of its own. A person's checked-out records are
on their storage page.

### Receiving is a request's receipt

**A request type says which of what it sends is received.** `request:
{ receives: { field: tubes, by: [coordinator] } }` names a list of
references the request `sends` (ADR 0132), to a type with `storage`, and the
service's lab roles that receive them (any Editor of the service, if it
names none). The asking project lists the tubes it's sending; the service
reads them, and nothing else of the project's, as before.

**A receipt checks what came against what was expected.** A receiver in the
service starts a receipt for a request sent to it. Its lines are what the
request expects that no earlier receipt accepted, so a request delivered in
two shipments has two receipts. Each line is scanned or ticked, and is
`matched`, `missing`, `damaged`, or `rejected`; a code scanned that isn't
expected is an `extra` line, with the record it names if the service can
read one, or just the code. Each line keeps who checked it, when and a
note; a damaged, rejected or extra line needs one. The receipt names who
handed it over (a courier and a waybill, or a person) and a note, such as
its temperature on arrival.

**Completing it is one change.** Every expected line needs an outcome.
The receiver can explicitly mark the rest missing. For every matched or
damaged line, the receiver chooses a position or chooses to hold the
material without a storage location. The completion request names each such
line exactly once, in `put_away` with its box and position or in
`keep_in_custody`. Iki suggests free positions once the selected box has
loaded. The completion button waits for that lookup and for a placement
decision on every matched or damaged line. The server refuses an incomplete
decision and leaves the receipt open. Completion takes custody from
wherever the sending project recorded the material. A rejected line stays
with the receiver pending return or destruction. A missing line never
arrived, and an extra scan records a discrepancy.

**Unplaced material stays visible until put away.** A completed receipt
marks a matched or damaged line held without a location as
`received_unplaced`. The Storage page lists received material awaiting
put-away with its current custodian. Record permissions determine who sees
each entry. Placing the material clears it from the list. If a box count
finds the material, the receiver can resolve the unexpected scan by placing
it in the scanned position. A completed receipt and its lines are immutable,
so the original placement decision remains in the history. Completion is an
audit event in both projects, `request.receipt_completed`, and a webhook
event.

**Discrepancies go back to the requester.** A receipt with any line that
isn't matched tells whoever submitted the request, and its watchers, who can
read it (ADR 0129): what was missing, extra, damaged or rejected, with each
note. It's a notification kind of its own, which nobody turns off.

### Labels and scanning

**What's scanned is a record's code: its barcode, or its HRID.** A type's
`storage.barcode` names a unique string field holding a code already on the
tube, such as a vendor's 2D code on a pre-barcoded tube; a record without
one is labelled and scanned by its HRID. `GET /storage/scan?code=` finds the
records a code names, among those the caller can read: by HRID, by a
storable type's barcode, or by an Iki link to a record.

**A label is printed from Iki, and each print is recorded.** A label has the
record's code as a DataMatrix or QR code and as Code 128, its HRID, its
title, and the type's `storage.label` line. Iki lays labels out for common
sizes (a cryo tube label of 32.5 × 12.7 mm, 50.8 × 25.4 mm, and a box label
of 76.2 × 25.4 mm), for the browser to print to a label printer's driver, or
as ZPL for Zebra printers at 203 or 300 dpi, using the printer's own barcode
commands. The smallest has no room for a Code 128 a scanner reads, so it has
the 2D code alone. Each label prepared is an audit event, `entity.labelled`,
on the record: its size, format and copies. The browser draws barcodes with
bwip-js (MIT).

**Scanning works with what labs have.** A USB or Bluetooth scanner types the
code and a final Enter or Tab as a keyboard does. On Iki's storage, receiving
and counting pages, typing in an editable field belongs to that field. With
focus elsewhere, at least four characters form a scan if the gaps between
them, including the final Enter or Tab, are at most 50 ms, with one gap
allowed up to 250 ms. This accommodates a brief scheduling or rendering
stall while excluding a person's steadily slower typing. Two consecutive
characters within 50 ms identify a possible scan. From then until its
terminator, a longer pause or a second stall rejects the whole code. Iki
consumes the terminator either way so it cannot activate the focused
control. For an unreadable scan the scan box asks the person to try again
or type the code. On a tablet, the camera reads codes where
the browser has `BarcodeDetector`; elsewhere, Iki says so and offers the
code typed.

**A type can require a scan before a draw or a move.** `storage.confirm:
[draw, move]`: the code scanned is sent with the change, Iki checks it names
the record, and the movement keeps it. A mismatch is refused before anything
changes: the tube in someone's hand isn't the one they chose.

### A container's amount, and what a draw went into

**A tube's volume is its amount.** A container type keeps how much it holds
as an `amount` (ADR 0139), drawn, transferred, split and adjusted as any is:
one balance, one ledger, the same for a tube, an aliquot and a lot.

**A container's material follows its source.** `amount: { follows: [lot,
sample] }` names references whose records' eligibility a draw from it must
also meet: a state holding the source's amount holds the tube's, and the
source's draw guards (ADR 0139) are checked with the source as `record`. A
tube of an expired lot is refused, as the lot is, with the same reason, and
a tube of a lot due for retest is drawn from only for QA sampling, by
someone who could sample the lot. The record the first of them names is the
draw's `source`, kept with its movement.

**A draw names what it went into.** `used_in`, a record the caller can
read, such as an analysis result or an experiment, is kept with the
movement, and is an edge of the graph (`used`, ADR 0123): the result depends
on the tube, the tube on its lot, so the lot's Used by finds the result, and
a recall reaches it.

**Who holds material may use it.** A draw needs Editor in the record's
project, or in the project whose storage holds it: a core that received a
project's tubes tests them, as it's asked to, without being able to change
the tubes' values. Transfers and splits still need Editor in the record's
project, as they make or change its records.

### Counting a box

**A reconciliation compares what's scanned with what's recorded.** Someone
starts one for a container with a grid. Iki notes what's recorded in each
position, and they scan each position's code, or say it's empty, in any
order, or upload a rack scanner's file of positions and codes. Each record
is `matched`, `missing`, `unexpected` (scanned here, recorded elsewhere or
nowhere) or `wrong_slot` (scanned at another position of this box), and each
that isn't matched is resolved: moved to where it was found, removed from
storage, placed here, or kept as recorded with a note. Each decision records the
position it refers to. A changed observation requires the operator to review
the affected decision. The completion screen shows each tube and its planned
destination. A move or placement requires a positive scan at that exact slot.

Each edit advances the count's revision. Saving or clearing a line decision
and completing the count each require the revision the operator reviewed.
The server checks it under the same lock that protects the count. Completion
also verifies each saved decision against its current observation. Another operator can save
a valid replacement while the first has the form open. The first operator
must review that revision and enter a fresh approval before completing.
Completing it applies
every resolution as one change, which its movements share as their
operation, and is refused if anything moved in or out of the container since
it started. Each movement records its scan position or why the item was
removed. A removal distinguishes a recorded position checked without finding
the item from an unscanned position the operator chose to treat as missing.
The `storage.reconciled` audit counts the observed findings, each resolution,
the unscanned records treated as missing, and the movements saved under the
operation. It also emits a webhook event.

### What's core and what's a plugin's

Core: the `storage` declaration, the place table, its ledger and custody,
bulk moves, receipts, reconciliations, labels and scan lookup, the
container's amount following its source, a draw's `used_in`, and the
screens: Storage, a box map, receiving and reconciling. A plugin: its kinds
of places and containers and their names, sizes and fields, its tubes'
references to samples and lots, which roles receive, and which changes need
a scan. `example_lims` has a Freezer, a Rack (a grid of boxes), a Box and a
Tube of a sample and a lot, kept in µL, following both.

## Consequences

- One model of where material is, the same in every plugin, with the
  chain of custody a GxP lab is inspected on: every placement, move,
  check-out and receipt says who, when and why.
- Where a record is isn't in its revisions or a bundle's records, as its
  amount isn't; its location history is where that is. A bundle doesn't
  carry places yet, nor does an import or an action run's `creates`.
- A record's place isn't a search condition or a grid column yet: the
  storage pages and its record's page are where it's found.
- A receipt doesn't change a request's own status, which is the service's:
  a plugin's trigger on `request.receipt_completed` can.
- A reconciliation is of one container at a time; scanning a whole freezer is
  each of its boxes in turn.
- A label's printing isn't confirmed by the printer: Iki records that it
  was prepared, and printed through the browser or downloaded as ZPL.
- Camera scanning needs `BarcodeDetector`, which Chrome on Android has and
  Safari and Firefox don't yet; a USB or Bluetooth scanner works everywhere.
