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

### How a receiving or counting observation is recorded

The receipt or count line holds its current state. Its append-only observation
history records what a person saw, who recorded it and when. A correction points
to the preceding event for that line and kind of observation. It records both
values and the operator’s reason for changing the earlier observation. The
condition note and the correction reason have separate meanings.

As with a movement, one transaction verifies the reviewed state, appends the
event, advances the current row and writes an audit entry referring to the same
event and operation. A first observation can proceed independently of observations
on other lines. A correction must identify the affected line and the version
reviewed. Handoff changes, cancellation and completion apply to the whole plan
and use its revision. One command can record changes to several observations
under the same operation ID.

The history uses a stable line ID that survives a retraction. A reader can
inspect it on open and closed receipts and counts. The stored snapshots use
user IDs for the people who checked, scanned or resolved a line. The history
response supplies each event's author for display. Access follows the parent,
and a linked code is visible only while its record is readable. Existing rows
can supply known values for the first recorded change. An unknown earlier actor,
time or scan stays unknown.

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

**A receiver can record a fresh observation while a colleague checks another
tube.** A scan takes just the code. A first manual check can send its outcome
and condition note without a revision. If someone already checked the line,
the unversioned check returns `found: "again"` and leaves their observation
intact. A scan or check returns `{found, line, receipt}`, identifying the
action's result, the line and the receipt as it now stands. Each line carries
its own `line_revision`. Changes to other lines leave that revision valid.

A condition `note` says what the receiver saw. A correction `reason` says why
they changed an observation already recorded. For example, an editor who
reviews a damaged line and finds the wrong tube was checked can send
`PATCH /api/v1/receipts/{id}/lines/{line}` with the line revision just read.

```json
{
  "line_revision": 2,
  "outcome": null,
  "reason": "The damaged package belonged to the next delivery."
}
```

The line becomes unchecked. Its earlier observation, note, actor and time
remain available at `GET /api/v1/receipts/{id}/history`. Omitting `note` from
the PATCH preserves the saved text, including on an outcome change. Supplying
`null`, an empty string or whitespace clears it. A receiver can add the first
condition note with a check, or send the current line revision to add it after
a scan. Neither needs a correction reason. Replacing or clearing saved text
requires both the line revision and a nonblank reason. Omitting the reason
or sending null, empty text or whitespace counts as giving no reason.

Removing an extra uses its line revision and reason in the DELETE body.
The line keeps its ID in `removed_lines`. Scanning that code
again reports `found: "removed"`. To restore it, the receiver sends that
line's revision and a reason to
`POST /api/v1/receipts/{id}/lines/{line}/restore`.

The handover, completion and cancellation concern the whole receipt.
Each takes the `plan_revision` of the complete receipt the receiver saw.
Cancellation also records a reason, the cancelling actor and the closing
time. A stale line or receipt revision returns HTTP 409 with `affected_lines`.
A line conflict identifies that line and its current revision. A receipt conflict
lists each line changed since the reviewed revision, including removed extras.
A change to the handoff alone returns an empty list. The receiver can read the
current evidence before deciding whether to change it.

**Completing it is one change.** Every expected line must have an outcome.
The receiver can mark the remaining unchecked lines missing with
`missing_rest: true`. For each active matched or damaged line linked to a
record, the receiver chooses a storage destination or explicitly keeps it in
custody without a position. A rejected record stays in the receiver's custody
until returned or destroyed. A missing outcome leaves the tube’s recorded
location unchanged.

Read `GET /api/v1/receipts/{id}` and review its `lines` and `plan_revision`.
Then submit `POST /api/v1/receipts/{id}/complete` with that revision and one
choice for each matched or damaged record. This example puts one tube in A1
and keeps the other in custody. Both line IDs come from the reviewed receipt.

```json
{
  "plan_revision": 7,
  "put_away": [
    {
      "line": "00000000-0000-4000-8000-000000000001",
      "to": "IKI:00000000000040008000000000000003",
      "position": "A1"
    }
  ],
  "keep_in_custody": ["00000000-0000-4000-8000-000000000002"]
}
```

Omitting or repeating a required line, including a line in both lists, or
including a rejected, missing or extra line returns HTTP 422. A changed
open receipt returns HTTP 409 for a stale `plan_revision`. These refusals
leave the receipt, custody and placements unchanged.

Completing clears the former recorded location of each accepted tube. Its
completed receipt line records its `put_away` destination or sets `received_unplaced` when the
receiver took custody without placement. These fields describe the receipt
at completion. To find records still awaiting placement, read
`GET /api/v1/storage/awaiting-put-away`. Each entry identifies a readable
tube, its current `custodian` and `custody_since`. Completing also records
`request.receipt_completed` in both projects' audit trails and emits a webhook
event.

**Discrepancies go back to the requester.** A receipt with any line that
isn't matched tells whoever submitted the request, and its watchers, who can
read it (ADR 0129): what was missing, extra, damaged or rejected, with each
note. It's a notification kind of its own, which nobody turns off.

### Labels and scanning

**A vendor code identifies a record within its owning project and the field
that defines the code.** A service can receive tubes from several projects
and a box can hold several types. Two records in the same box can therefore
have identical vendor labels. A typed Iki ID is looked up only as an HRID.
An Iki machine symbol encodes `IKI:` followed by the record UUID's 32
uppercase hexadecimal digits, without hyphens. This compact payload lets a
reader identify the record without knowing a deployment's hostname.
Existing Iki record links still identify the record. Any scan beginning
with `IKI:`, in any letter case, is reserved for Iki identity. A malformed
payload or an unavailable record is refused. Entry and import reject a
vendor code using this prefix, including when a client sends it through
the API or SDK.

Receiving, counting, box-map selection and movement confirmation use one
resolver. It checks the scope of the operation and the identity represented
by the input. On a positional count, a matching readable occupant at the
scanned position supplies evidence of that label at that slot. If two
physical tubes bearing identical vendor labels are swapped, a vendor scan
alone cannot distinguish them. An independent Iki label can. An explicit
observation of a different identifiable tube at that position records what
the operator found.
An observation recorded before identity evidence existed has `identity_basis: null`
and cannot establish a unique identity.

An unregistered label is evidence about the slot where it was seen. A
single rack read showing that text at A1 and A2 records two observations
with separate line IDs. On a later read at A3, the text alone cannot tell
the operator which tube moved or whether a third arrived. The count
returns the previously observed slots and writes nothing until the
operator chooses. Adding another tube creates a line at A3. Moving one
requires the chosen line's reviewed revision and a correction reason.
The history preserves its earlier position and who corrected it.

When a label identifies several readable candidates, a choice shows their
owning projects and their recorded positions where those are readable. A
concealed occupant is never listed. A scan at its slot gets the same
neutral refusal whatever vendor code was supplied. An ambiguous scan
cannot mark a receipt line or a movement confirmation. If a receipt scan
misses the checklist and the vendor code has several readable matches, the
receiver can record an unlinked extra. The receipt explains why it has no
record link.

The receipt's observation history keeps the text the scanner actually sent
alongside the resolved record. The event says whether the operator used a
compact Iki symbol, a record URL, an explicit Iki ID, a vendor label or
an HRID. It records the scope and any deliberate selection. If the input
matched both HRID and vendor code, history preserves both. An unlinked
extra records whether the label had no match or several. Manual and older
observations have no scanner input to report. Readers who lose access to
a linked tube cannot read its scanned text from the receipt's history.

**Every Iki label carries the record's unique symbol and the human-readable
HRID.** A configured vendor code can also be printed as an additional
symbol for instruments. The preview and the print request check that the
requested symbols, their quiet zones and the HRID fit the selected stock.
An option that does not fit is refused before a print audit is written.

| Stock                    | Unique symbol (default)   | Alternative | Additional vendor symbol     |
| ------------------------ | ------------------------- | ----------- | ---------------------------- |
| Cryo, 32.5 × 12.7 mm     | Square ECC200 Data Matrix | QR, level Q | None                         |
| Standard, 50.8 × 25.4 mm | Square ECC200 Data Matrix | QR, level Q | Optional Code 128 if it fits |
| Box, 76.2 × 25.4 mm      | Square ECC200 Data Matrix | QR, level Q | Optional Code 128 if it fits |

The engineering minimum is **0.25 mm per module**, with a whole number of
printer dots per module. Reserve 24 × 24 modules for the Data Matrix plus
a one-module quiet zone on each side. On cryo stock this gives three dots
at 203 dpi (0.375 mm) or five at 300 dpi (0.423 mm). QR uses a four-module
quiet zone on each side. It fits the same compact payload on cryo stock at
two dots (0.250 mm) or three dots (0.254 mm), respectively. Keeping the
larger pitch is why cryo defaults to Data Matrix. [GS1 verification guidance](https://ref.gs1.org/guidelines/barcode-verification/2D/)

The browser draws codes with bwip-js. ZPL uses the printer's native
barcode commands. Checking symbol bounds, integer-dot pitch and
software decoding is **layout validation, not scanner qualification**.
Qualification exercises printed stock with the bench reader. Each
prepared label records its format, size and copies in an
`entity.labelled` audit event.

**Scanning works with what labs have.** A USB or Bluetooth scanner types the
code and a final Enter or Tab as a keyboard does. On Iki's storage, receiving
and counting pages, typing in an editable field belongs to that field. With
focus elsewhere, at least four characters form a scan if the gaps between
them, including the final Enter or Tab, are at most 50 ms, with one gap
allowed up to 250 ms. This accommodates a brief scheduling or rendering
stall while excluding a person's steadily slower typing. The gap budget
applies from the first character. A gap longer than 250 ms or a second
gap over 50 ms invalidates the whole sequence through its terminator.
Otherwise a stalled label such as `XRC001` could be accepted as `RC001`.

Two consecutive characters within 50 ms identify a possible scan. Once that
happens, Iki consumes Enter or Tab even when an earlier gap invalidated the
code. When the timing broke before that fast pair, the scan box explains
that a key arrived before the rest of the scan and asks for a rescan.
There is no idle timeout that starts a new code. With focus outside
editable controls, a stray character followed by a scanner burst can cost
one retry even minutes later. This prevents the suffix of a label from
matching a different tube. Enter or Tab ends the attempt. Typing in an
editable control, composing text, or using a Ctrl, Alt or Meta shortcut
also clears the listener’s buffer. Steadily slow typing keeps its ordinary
keyboard behavior.

On a tablet, the camera reads codes where the browser has `BarcodeDetector`.
Elsewhere, Iki offers the typed code.

**A type can require a scan before a draw or a move.** The
`storage.confirm` setting names the operations that require proof. The
server checks the scanned identity in the context of the selected records
and their source. When the vendor label could confirm more than one tube,
the operator scans the unique symbol or explicitly enters the tube's Iki
ID. The movement retains the confirmation used for the write.

For Move and check-in, one scan can identify a tube across the whole selection.
Send `POST /api/v1/storage/scan/resolve` with `scope: "confirmation"`, the scanned
`code` and up to 500 selected record UUIDs or HRIDs in `records`. Omit `record`
for this form. The resolver checks all selected source boxes. When a selected
tube has no source box, it checks the readable inventory. A unique result
identifies the selected tube by UUID. Readable collisions return candidates.
Move and check-in repeat the check against the entire selection under their
storage locks, so a duplicate that arrived since the scan prevents the write.
A concealed source or occupant requires independent Iki identity, and an
unavailable typed ID returns the same neutral refusal as an unknown one.

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

Scanning a fresh slot and tube needs no precondition. Operators can scan independent
positions at the same time. An operator replacing a prior scan must review each
affected line and give a correction reason. An explicitly empty position counts as
an observation. A repeat observation of the same tube or empty slot needs the
reviewed line version and records a fresh observation without a correction reason.
A rack file supplies all affected line versions together. Conflicting assignments
inside a file, or a changed observation on any affected line, refuse the whole file.

Each saved decision is bound to the exact scan, including the scan at the expected
slot that established a missing finding. Saving, clearing or replacing a decision
requires that line's revision. Changing or clearing an existing decision requires
a correction reason separate from its supporting note. Adding a first note to a
current decision needs no reason. A note-only edit preserves the original
resolver and decision time. Its event identifies the person who edited the note.
Omitting the note preserves it on a resolution edit. Clearing the entire decision
removes its attached note from the current line and retains it in history.

A new observation makes a decision based on the old scan stale, even when both
observations agree. The count
history retains the original observation, decision and note, their actors and times,
and the evidence that replaced them. Earlier counts keep the facts that survived;
an open decision with no surviving evidence identifier requires a fresh scan before
it can be confirmed.

On a count line, `observation` identifies the physical scan supporting its current
finding. `reviewed_observation` identifies the scan supporting its saved decision.
Both contain the event ID, the physical line ID, the scanned position and code, and
the operator and time. For a missing tube the physical line is the empty slot or
the different tube scanned there. To see that scan in history, filter by its
`line_id` and find its event ID. The missing tube's own line history contains its
decisions. A null scan means there is no recorded scan event, a null code within
a scan means the slot was observed empty, and a hidden linked tube's code is
redacted to an empty string.

Completing or cancelling the whole count requires the parent revision the operator
reviewed. Cancellation records its operator, time and reason. The count closes
with its observations intact. Completing it applies
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
  carry places yet. An inline run create can receive a fresh lot with its opening
  amount and storage placement when its action declares `receive_into` (ADR 0190).
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
