# 0228. Records are edited many at once, as a sheet

- Status: Accepted
- Date: 2026-11-25
- Builds on: [0035](0035-entity-revisions-provenance-and-edit-rules.md),
  [0072](0072-agents-changes-name-the-person-and-the-agent.md),
  [0125](0125-a-state-is-a-field-each-state-says-who-sets-it.md),
  [0127](0127-plugins-rules-are-checked-at-every-save.md),
  [0139](0139-a-records-amount-is-a-ledger-of-what-moved.md),
  [0175](0175-a-searchs-results-download-as-csv-and-a-sequence-is-found-by-a-motif.md),
  [0178](0178-spreadsheets-are-imported-by-mapping-previewing-then-one-run.md),
  [0207](0207-commit-the-plan-that-was-reviewed.md)

## Context

A lab changes many records at once all the time: purities from a SEC run for
the whole batch, a due date pushed for twelve samples, notes on every tube in
a box, forty new aliquots with where each went and how much it holds. In Iki
that was one record's form at a time, or a spreadsheet exported, changed and
imported, or the API.

Benchling's bulk edit and every LIMS's grid let people do it where the
records are: in the table, as a spreadsheet. What they do badly:

- **Values are guessed or half-checked.** A grid checks a cell's type, then
  the save finds the duplicate, the locked record or the rule broken, often
  after some rows are written.
- **A paste goes where the tool decides.** Matching pasted headers to columns,
  or reordering them, surprises people: a column pasted a column off writes
  wrong values in every row.
- **Someone else's change is overwritten.** The grid was loaded an hour ago.
- **Nothing says why.** A hundred values change with no reason in the record.

Iki already reads a spreadsheet's cell as its field (ADR 0178), checks an
edit by every rule (ADRs 0125, 0127), and keeps each change as a revision with
its reason and who made it, a person and their agent (ADRs 0035, 0072).

## Decision

The records table edits a type's records in one project as a sheet. "Edit as
sheet" shows when the table has a type, a project and the person is an Editor
there. It feels like a plain spreadsheet:

- A cell is edited by typing, Enter or F2; Escape puts it back. Arrow keys,
  Tab, Home and End move; Shift extends the selection; Ctrl/Cmd+A selects all.
  The sheet is an ARIA grid (`role="grid"`, one tab stop, roving focus),
  virtualised as the table is.
- **A paste fills right and down from the selected cell, in the copied
  order.** Tab-separated text or an HTML table, from Excel, Sheets or Iki's
  own copy. No header is matched and nothing is reordered: putting the right
  columns in the right place is the person's, and each cell's check catches
  what's wrong. A single value pasted into a selection fills all of it.
- Fill down (Ctrl/Cmd+D) copies the top row of the selection down. Delete
  empties the selected cells. Copy writes what the cells say, as a search's
  CSV does: references by HRID, numbers in full, dates in ISO 8601.
- "Add row" adds a new record of the type, in the project, at the top. A
  paste past the new rows adds more; past the loaded records, it's refused
  and says so.
- Undo and redo (Ctrl/Cmd+Z, Shift+Ctrl/Cmd+Z) step back through every edit,
  paste and fill until Save. **Nothing is written until Save.**
- **Rows don't move under the person typing.** The table reads its records
  again as it goes (when the window gets focus, say), and sorted by when they
  changed, someone else's save moves a record to the top. The sheet keeps each
  record's row where it first was, shows a record no one has typed in as it is
  now, adds records it hadn't loaded at the end, and keeps a changed record
  even when it leaves the table. The selection is held by record, so a typed
  value or a paste goes where it was aimed.
- Leaving with changes unsaved (a link, Back, the command palette, closing
  the tab) asks first; staying keeps them.

Columns are the record's ID, then the type's importable fields in the type's
order, as its import template lists them, then the new rows' Location and
Volume (or the type's amount label), then the fields a sheet can't change,
read-only. A cell a person can't change says why, in its title and to screen
readers: computed, set once and set, a state, structured data, the record
locked by its state, or not theirs to edit.

### 1. Each cell is checked as an import reads it, and as its edit is saved

A changed row is sent to `POST /projects/{id}/sheet-edits/check` as text,
the way a spreadsheet's cell is, two hundred rows at a time. The server reads
each cell by its field as an import does (ADR 0178): quantities in the
column's unit or the cell's own, converted by UCUM; references by ID, HRID or
name, with an ambiguous name naming its records; lists split by `;`; choices
and truth values in any case; dates as ISO 8601 or in the sheet's order.

**The sheet's locale is asked for, never guessed.** A bar above the sheet
says its decimal mark, its date order and whether plain numbers in date
columns are Excel serial dates; the time zone is the browser's. Until it's
said, `1,234` and `03/04/2027` are `ambiguous` problems naming both readings.

Then the check makes the change, by the same code a save does, in a savepoint
it rolls back: each existing record is updated (`entities.update`) at the
revision the sheet read it at, and each new one is created
(`entities.create`, with its starting amount) and put away
(`storage.plan`). So every rule a save meets is met in the check: required
values, ranges, unique values against the project and the other rows,
references the person can't use, a locked record (ADR 0125), a plugin's rule
(ADR 0127), a place taken or not in the container. HRIDs aren't spent:
records the check makes are numbered from a provisional range no sequence
reaches, and only the savepoint ever holds them. Every side effect of a
write (events, webhooks, notifications) runs on commit, so none runs.

Each row comes back with what it would do (`edited`, `unchanged`,
`created`, `registered`, `added`, `failed`), its values as read, each shown
as it would read in the table, and its problems, each at its cell. The sheet
marks a cell with a problem in red, an ambiguous one in amber, and lists every
problem with a link to its cell.

### 2. Save shows what changes, takes a reason, and writes all of it or none

A sheet with problems, or changes still being checked, can't be saved. Save
opens a review of every change, old value to new, with the new rows, where
each goes and how much it holds. It needs a reason, kept on every revision. A
row a plugin's rule holds for review needs the hold's reason too (ADR 0127).

`POST /projects/{id}/sheet-edits` takes what the check read, not the text:
each row's values as read, the revision of each record the person reviewed,
and each new record's container and exact position. So what's saved is what
was reviewed (ADR 0207), never re-derived. It runs in one transaction:

1. It locks the records, in order, and **refuses the whole save (412) if any
   has moved on from its revision**, saying which. The sheet offers to bring
   in the latest values of those rows, keeping the person's other changes.
2. It updates each record and creates each new one, each in a savepoint, so
   every row's problem is found, not just the first. Any problem refuses it
   all (422), naming each at its row and cell, and nothing is written.
3. It puts the new records away, all in one storage move, and records their
   starting amounts through the ledger (ADR 0139).
4. The audit trail keeps one `records.sheet_saved` event with the reason,
   the locale and each record's changed fields.

Each revision names the person and, through MCP or a token, their agent
(ADR 0072), as any edit does. A save takes at most 1,000 rows; larger changes
go through an import.

### 3. Where a record is kept and how much it holds

A new row's Location is a container's HRID, code or name, then a position
(`BOX-000012 B3`, `Batch 12 retains/A1`); just the container takes its next
free position, in its order. Volume is in the type's amount unit unless the
cell says another (`0.5 mL`). Both go through storage and the amount ledger,
never by writing a field. For an existing record they're read-only: moving
one, or drawing from it, has its own reasons and checks, on its page or in
Storage. A type whose storage asks to confirm a move can't be placed from the
sheet.

### 4. Import templates

`GET /entity-types/{type}/import-template.csv` is a CSV file with a heading
for each field an import sets, in the type's order, with its unit
(`Concentration (mg/mL)`), as the sheet's columns are. It's for a
collaborator or vendor to fill in and send back to be imported. It's written
as a search's CSV is (ADR 0175): UTF-8 with a byte order mark, every cell
quoted, and any cell that would start a formula made text.

### 5. The SDK, CLI and MCP

The endpoints are the API's, for any client: the Python SDK's
`spreadsheets.check_edits`, `save_edits` and `template`, and the MCP tools
`check_record_sheet` and `save_record_sheet`, which say to show the person
the changes before saving.

## Consequences

- One code path decides what an edit does: the check runs the save. There is
  no second validator to drift from it, and a plugin's rule is met in the
  sheet as in the form.
- A check writes and rolls back, so it costs what a save does. Checks are
  per changed row, debounced, and two hundred rows at a time.
- A sheet can't swap two records' unique values in one save: the first
  update meets the second's value. Change one, save, then the other.
- People are named by ID or email in a cell, as an import reads them.
- A record's location and amount change elsewhere, with their own reasons.
