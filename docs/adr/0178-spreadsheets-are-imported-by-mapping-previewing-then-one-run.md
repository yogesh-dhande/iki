# 0178. Spreadsheets are imported by mapping, previewing, then one run

- Status: Accepted
- Date: 2026-11-22
- Builds on: [0036](0036-files-content-addressed-uploads.md),
  [0052](0052-imports-file-params-and-origins.md),
  [0056](0056-bulk-imports.md),
  [0082](0082-unique-fields-link-records-in-other-systems.md),
  [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0177](0177-a-projects-files-are-found-by-name-and-say-what-uses-them.md)

## Context

Labs keep most of what they know in spreadsheets: a vendor's compound list, a
plasmid inventory, a sample manifest, a plate reader's export. Getting them
into records was the first thing pilot scientists asked for, and the one
thing Iki could only do through the API, one create at a time, or by writing
a plugin action.

Benchling, Dotmatics and every LIMS have a spreadsheet import, and they agree
on its shape: choose the file and the sheet, map its columns to the fields,
see what will happen, then import. What they do badly is where the time goes:

- **What's wrong is found late.** A tool that writes row by row stops at row
  812, with 811 rows written; one that checks only types finds a bad
  reference or a duplicate on import, or not at all.
- **Duplicates are created.** A sheet often lists what the registry already
  has, or lists the same thing twice.
- **Numbers and dates are guessed.** `1,234` is a thousand and more in
  Boston and one and a bit in Berlin; `03/04/2026` is March or April. A tool
  that reads them by the browser's locale writes wrong values silently, the
  worst outcome under GxP.
- **Where a record came from is lost.** The workbook is gone once it's read.

Iki already has the hard parts. A run's bulk import (ADR 0056) checks a
million creates a chunk at a time, then writes them in one transaction, with
duplicates resolved against the registry in SQL (ADR 0115) and HRIDs in line
order. Origins (ADR 0052) say where each record came from, and files (ADR 0036) are kept, content-addressed, as they were added.

## Decision

A spreadsheet import (`iki_spreadsheet_import`) takes an XLSX workbook or a
CSV file added to a project, and the type its rows become. It's the
requester's until it's committed; it needs them to be able to add records to
the project, and the workbook to be one of the project's files that they can
read, as its Files list them (ADR 0177): added to it, or held by its records.
The person uploads a workbook, or picks one already there with the file
picker, held to workbooks and CSV files. A file a record holds stays where it
is: the import names it, as each record it writes does.

### 1. Reading the workbook

Iki reads cell values with `python-calamine` (MIT; Rust's calamine). Only
values: a formula's value is the one the workbook saved when it was last
calculated, and a formula that was never calculated reads as empty. Nothing
in a workbook runs; a macro-enabled workbook's macros are never read.
Workbooks are untrusted, so before calamine parses one, Iki:

- refuses an encrypted workbook, or a legacy `.xls`, by its first bytes (an
  OLE file), saying how to save it as `.xlsx` without a password, and refuses
  `.xlsb`;
- reads its zip through once, counting what each part decompresses to: a part
  may grow at most 200 times its compressed size (past a megabyte), and all of
  them to 512 MiB, so a zip bomb stops early whatever its headers claim;
  encrypted parts and compression methods workbooks don't use are refused;
- refuses XML that declares a document type, which OOXML never does and the
  billion laughs needs.

A workbook is at most 100 MiB, with at most 1,000 sheets, a sheet at most a
million rows and 1,000 columns, a cell at most Excel's 32,767 characters.
A sheet's size is what it holds, not what its `<dimension>` claims. A CSV file
is UTF-8 (with or without its byte order mark), UTF-16 with its mark, or
failing those Windows-1252, as Excel writes it; its separator is a comma,
semicolon, tab or bar, whichever its first lines are split by.

A task reads the sheets, and each one's first 30 rows, as text and as typed
values. Iki chooses the first visible sheet, its first row with anything in it
as the header, and every row after it, then suggests a mapping. Choosing
another sheet starts with its own header, row range and column suggestions.
The new selection clears skipped rows, the previous mapping and number/date
choices, and the previous preview. The same change can set choices for the
new sheet.

### 2. Mapping columns

Each column maps to a field, or to none. Iki suggests the field whose name or
label the header is, ignoring case, punctuation and a bracketed unit
(`Conc. (ug/mL)`), or the one field it abbreviates (`Conc.` for
Concentration), each field once. Computed fields, states, files, structured
fields and a request's service fields can't be mapped, and say why.

A cell is read as its field:

- **Quantities** are in the column's unit (the header's, if it names one), or
  in the cell's own (`2.1 mg/mL`), and converted to the field's by UCUM. A
  unit that can't convert says why.
- **References** are found by ID, by HRID, or by name (any case) among the
  records the requester can use in the project (ADR 0115). A name several
  entries have is a problem naming their HRIDs; of several, the project's own
  is meant.
- **Lists** are split by `;` (or the column's separator), and each item read.
- **Enumerations** and **truth values** match their choices in any case, and
  the column can say what its own spellings mean (`circ.` is circular).
- **Dates and times** are ISO 8601, `1 Mar 2026`, or numbers in the sheet's
  order; a date and time without a zone needs the column's.

### 3. How numbers and dates read: the sheet's locale

The person says how the sheet's text writes numbers and dates: a decimal
point or comma, dates day or month first, and whether plain numbers in date
columns are Excel serial dates. A column may say otherwise. Until they have,
nothing is guessed:

- A cell that reads two ways is a problem of its own kind, `ambiguous`,
  saying both readings: `1,234` "reads as 1 234 with a decimal point, and
  1.234 with a decimal comma"; `03/04/2026` "reads as 3 April 2026 day first,
  and 4 March 2026 month first".
- A cell that reads only one way is read so: `1,5`, `1.234,5`, `31/12/2026`.
- Cells the workbook holds as numbers or dates are never ambiguous: Excel
  stored them as such.
- A plain number in a date column (`46023`, where Excel lost the format) is a
  date only when the person says the sheet has serial dates.

Before the preview, and in it, the import shows a few of each mapped number
and date column's own cells, each as it reads, or both ways it could: those
written as text, which read by how the sheet writes them, and a couple the
workbook stored as numbers or dates, which read one way only. It says
numbers as SI does (ISO 80000-1): a decimal point, and thousands apart by a
space, never a comma or a point, which would be the very ambiguity it
shows. Dates are ISO 8601. A quantity in another unit than its field's reads
in both, as it would be kept: `1 250 ug/mL = 1.25 mg/mL`. The
locale chosen is kept with the import, in its run's params, and in the audit
trail, as a saved mapping keeps it.

### 4. Previewing

Previewing checks every row as it would be written, a chunk at a time and
resumably, as a bulk import checks its lines (ADR 0056), so a large sheet
never times out a request:

1. **Reading:** the chosen rows are read, as their columns map, into creates
   (one JSON line a row, in storage, not a file), with each cell's problems;
   references are found two thousand rows at a time.
2. **Checking:** two thousand at a time, each create is checked by
   `entities.prepare`, as an API create is, and by the plugins' rules;
   unique values the project (or, for a registry fact, the registry) has are
   found in one query a field.
3. **Resolving:** in SQL, as the bulk import does, rows with an identity are
   found in the registry: the project's record of the entry is **reused**, a
   new record of an entry registered elsewhere is **added**, otherwise a new
   entry is **registered**; a row with an earlier row's identity is the
   **same** record; two rows with one unique value are problems.

Each row (`iki_spreadsheet_row`) keeps what it would do and its problems,
each by its column. The preview counts rows to create, register, add, reuse
or merge, rows with problems (and how many are ambiguous), and rows held for
review by a plugin's rule (ADR 0127). Changing anything starts it afresh.

### 5. Committing: one run of a core action

An approval authorises the exact content that was reviewed, and is void if
that content changes. Each preview has its own positive round number.
`POST …/commit` must include that preview’s `round` as `preview_round`, along
with any choice to skip problem rows. The server checks the round while
holding the import’s row lock. A missing round returns 422. If a different
round is current, the server returns 409 without starting a run. The person
or client reviews the new preview before approving it.

Committing writes the rows through ADR 0056's bulk import and no other way.
A run carries an import, so Iki's core action `iki/core.import_spreadsheet`
does: its tier is `server`, a tier no worker leases, whose job starts as the
server commits it. Its params are what was imported: the workbook, sheet,
rows, columns and locale. It's built in code, not in the catalog, so it isn't
run again or reproduced.

A task writes the rows to import as the run's creates file, one create a line,
kept as a file of the project (`<workbook>, <sheet> (rows imported).ndjson`).
Each line's origin names the workbook as its file, and its sheet and row
(`Samples, row 14`; `row 14` for CSV) as its identifier. A row with problems
stops the commit, unless the person leaves them out; it's then a blank line,
so lines keep their rows' numbers. The bulk import checks every line again,
as things are then, and writes them all or none, with HRIDs in row order.
A run that fails says why at each row and column, including unique values
someone wrote after the preview.

The audit trail records `spreadsheet.imported` (who, the workbook, the sheet,
the locale, the run) besides the run's own events; each record's history
names the run, and through it the person and any agent acting for them
(ADR 0072). When it's done, the import links to its records: the entity
search's `run` filter.

### 6. Saved mappings

A project saves an import's mapping, by name, for the type: each mapped
column by its header, with how it reads, and the sheet's locale. A new import
whose headers include all of a saved mapping's is mapped by it, wherever the
columns are; others are offered, best matched first. Mappings are the
project's because a project's instruments and vendors are; one a team wants
everywhere is saved in each project, as views are.

### 7. Keeping and clearing

An import nobody committed or touched for a fortnight is discarded by
maintenance; a committed one's rows are cleared a fortnight after. The
workbook stays a file of the project either way. Previews whose task died
are picked up by the task runner's housekeeping.

### API and components

The REST API is the importer: the web app's steps use it, and so can the CLI,
the SDK and agents. `POST /projects/{id}/spreadsheet-imports`, then `PATCH`
it to choose and map, `POST …/preview`, `GET …/rows`, `POST …/commit`;
`/projects/{id}/spreadsheet-mappings` lists saved mappings. The web app's
steps are components a plugin's page can use.

Row numbers, column positions, saved mappings and number/date choices refer
to a particular sheet. A PATCH identifies its target with `sheet` or supplies
the selection the caller reviewed, for example
`{"basis": {"sheet": "Samples", "header_row": 1}, "skip": [[5, 7]]}`.
If both are supplied, `basis` identifies the current selection and `sheet`
names the destination. The server compares the basis under the import's row
lock. A changed sheet or header returns 409 and preserves the current choices
and preview. The caller reloads the import, reviews the current selection, and
decides whether to submit its edit there. A sheet-relative change with neither
a target nor a basis returns 422. An empty PATCH can still return a preview
to mapping.

The web steps keep a rejected row choice in its field with its explanation
and existing help. The person can correct it, press Escape, or restore the
saved value. Mapping and preview wait for pending changes and unresolved
errors. When the server accepts a different sheet, its settings replace the
previous draft. If the header row moves, the page announces the sheet and
the old and new header rows.

## Consequences

- Positive: a sheet's every problem is seen before anything is written, by
  its row and column, and nothing is written unless all of it can be.
- Positive: duplicates are found in the registry, the project and the sheet,
  by the same SQL a run's import uses, so a preview and its commit agree.
- Positive: no number or date is guessed; how they were read is kept.
- Positive: every record names the workbook, sheet and row it came from, and
  the workbook is kept.
- Negative: an import is checked twice, in the preview and by its run. The
  second check is what makes it right when the project changed in between.
- Negative: a sheet's rows are kept until it's committed or discarded, and a
  fortnight after it's committed.
- Negative: only `.xlsx` and CSV are read: `.xls`, `.xlsb`, `.ods` and
  Numbers files must be saved as one of them first.

## Alternatives considered

- **openpyxl.** Pure Python and permissive, but about ten times slower on a
  large sheet, and it trusts `<dimension>`, so a sheet claiming a million rows
  costs a million rows' work.
- **A second write path for spreadsheets.** Writing rows directly would have
  duplicated what the bulk import gets right: one transaction, duplicates in
  SQL, HRIDs in order. A run costs little and gives the import its history.
- **Reading numbers and dates by the browser's locale.** Silent, and wrong
  whenever the sheet came from elsewhere.
- **Mappings saved per person, or for the whole organization.** A person's
  are lost when they leave; the organization's mix every project's
  instruments.
