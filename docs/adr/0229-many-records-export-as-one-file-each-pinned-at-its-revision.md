# 0229. Many records export as one file, each pinned at its revision

- Status: Accepted
- Date: 2026-11-25
- Refines: [0171](0171-a-types-records-export-as-files-iki-writes-from-its-fields.md),
  [0175](0175-a-searchs-results-download-as-csv-and-a-sequence-is-found-by-a-motif.md),
  [0176](0176-the-app-is-laid-out-around-a-sidebar-and-what-you-can-read.md),
  [0052](0052-imports-file-params-and-origins.md)

## Context

ADR 0171 let one record download as GenBank or FASTA from its page. Most real
hand-offs are of many records: a plate's worth of constructs sent to a
sequencing core, the antibodies of a campaign sent to a structure group, a
compound library sent to a CRO or loaded into a docking tool. Benchling,
Geneious and Dotmatics export a selection, or a search's results, as one
multi-record file. Until now, Iki's way to do that was to download each record
in turn, or, for compounds, to run `export_sdf`, an action that wrote an
artifact from each compound's latest revision. A Runner had to start it. The
artifact expired. Downloading it wasn't in the audit log.

Only DNA and RNA exported. A protein's residues had no FASTA, and a compound's
structure had no Molfile, although Iki imports both.

Exporting many records raises questions one record doesn't:

- **Which revision of each record.** A file of a thousand records takes a
  while to write. If someone edits record 600 while record 10 is being
  written, what does the file hold?
- **How large.** A table's whole search may find a million records.
- **What happens when one record can't be written.** A shorter file is
  easily mistaken for the whole selection.
- **Who may download it, and when.** A file may name records its requester
  could read when they asked, and can't read any more.

## Decision

### A type names the fields each kind of export writes

ADR 0171's `exports:` gains two kinds:

```yaml
exports:
  - export: protein
    sequence: sequence # a string field with format protein_sequence
    description: description # optional: string or text
    organism: organism # optional: a ref to iki/core.Organism
  - export: compound
    structure: molfile # a text field holding an MDL Molfile, V2000 or V3000
```

- **`protein`** writes FASTA as NCBI's protein FASTA does:
  - the header is `>`, the record's HRID, its name, its description, then its
    organism in brackets (`[Homo sapiens]`), which Iki's protein import reads
    back;
  - the residues are one-letter codes, 70 to a line.

  The first-party Protein type declares it. Antibody chains are Proteins, so
  they export through it too.

- **`compound`** writes a Molfile (`molfile`) and an SDF (`sdf`), as BIOVIA's
  CTfile Formats (2020) set them out.
  - **The Molfile's header block.** Its first line is the record's name, cut to
    80 characters with a note. Its second line is kept as RDKit wrote it,
    naming the program and dimensions. Its third line, the comment, says
    `Exported from Iki: CPD-000042 (revision 3).`
  - **An SDF record's data.** After the Molfile come data items:
    `IKI_HRID`, `IKI_UUID` and `IKI_REVISION`, the fields chosen as data, then
    `IKI_NOTES` when a note is needed. Each item has a `>  <name>` header, its
    value, and a blank line. The record ends with `$$$$`.
  - **What's left out.** A blank line ends an SD value, so a value's blank
    lines are left out. A value with a line that would read as the record's end
    (`$$$$`) or another item's header (`>`) is left out whole. Both cases are
    noted.
  - **Which fields can be data.** Every field of a kind SD holds as text can be
    data: strings, text, numbers, quantities in their own unit, enums, states,
    dates, references by HRID, and lists, their items separated by `; ` as the
    small_molecule import reads them. Files, people and JSON can't.
  - **Which fields are written.** The request names the fields; if it names
    none, every field that can be data is written.

  The first-party Compound type declares it, with a new `molfile` field.

A format says whether many records make one file of it: `many: file` (FASTA,
GenBank and SDF records follow one another) or `many: one` (a Molfile, a signed
report). `GET /entity-types` lists each format's `many`. A compound's export
also lists the fields an SDF can carry as `data`.

### Chemistry runs on the worker; the server only writes text

The server has no RDKit. It's a dependency of the small_molecule plugin's
worker image and of Iki's tests, not of the server. Adding it to the server
would put a large native library, and a parser of user-supplied structures, in
the process that holds every project's data. A structure laid out on request
would also make a file's bytes depend on the library version the server has
that day.

So the small_molecule plugin's `compute_descriptors` action writes each
compound's Molfile once, when the compound is registered, with its other
derived values:

- 2D coordinates from CoordGen;
- kekulized;
- V2000, or V3000 where V2000 can't hold the structure (more than 999 atoms or
  bonds).

A Molfile is kept only if reading it back gives the compound's own InChIKey.
The Molfile is a derived, computed field, which follows from the compound's
identity alone (ADR 0131). So it has a revision like any other value, and
exporting it is deterministic.

The server writes Molfile and SDF from that stored text in plain Python. It
only replaces the header block and appends data items. A compound without a
Molfile yet can't be written, and the export refuses it, naming the record.

`export_sdf` is removed. Its use, a library as one SDF, is this export, now
audited, pinned and available to anyone who can read the records.

### One request, its records pinned in one snapshot

`POST /entity-exports` takes a format and either:

- **`entities`**: the records picked, by UUID or HRID, in the file's order, up
  to 10,000; or
- **`search`**: a table's search as `POST /entities/search` takes it, every
  record it finds in its order, also up to 10,000.

A search that finds more is refused, not shortened, and so is a selection of
more.

Iki reads the records, their revisions, and whether each type exports the
format, in one REPEATABLE READ snapshot (`core.snapshots`). The export's row
stores `[id, HRID, revision]` for each record. The file is written from each
record's values at that revision, whenever the writing happens. Editing a
record after the export is asked for doesn't change the file. Each record in
the file says which revision it is where the format has a place for it: GenBank's
`COMMENT`, the Molfile's comment line, SDF's `IKI_REVISION`. FASTA has no such
place, so for FASTA the revision is only in the audit log.

Every type must export the format with `many: file`. Otherwise the request is
refused, naming the types and some of their records. An SDF's fields must be
ones every type can carry. By default they're the fields all the types share.

### Up to a hundred at once, more in the background

Up to 100 records are written during the request, which answers 201 with the
export ready. More are written by a task, and the request answers 202. Reading
the export shows `done` of `count` as the task goes, updated every hundred
records, and the page shows a progress bar.

The file is spooled to disk while it's hashed, then uploaded whole. Nothing is
served until it's complete. A file past 512 MiB fails the export.

### A record that can't be written fails the export

If any record can't be written, the export fails. Examples are a compound
with no Molfile yet, a record purged since the export was asked for, or a type
that no longer exports the format. The export lists each such record by HRID
(`refused`) with the reason, and no file is made. This is ADR 0230's
"refused, not shortened": a file never holds fewer records than were asked for.

What a format couldn't hold of a record that was written is a note, not a
refusal. Examples are a sequence's features in FASTA, a blank line in an SD
value, or a name past 80 characters in a Molfile header. Notes are by HRID:

- in the file, where the format has a place for them;
- in the export (`notes`) and on the page;
- in the audit log.

### Kept a week, downloaded while it can still be read

The file is kept for 7 days. `maintain` deletes expired files, except where
any of the export's projects is under a legal hold. Those are kept until the
hold is released, as bundles are.

An export is its requester's alone: nobody else reads or downloads it. When it's
asked for, Iki notes a project the requester reads each record through: the
record's own project, or one it's linked into. The export is listed only while
the requester can still read each of those, as a token limited to some
projects reads only those (ADR 0152).
Downloading it also checks that the requester can still read each record it
read and each record those refer to: an organism, or a supplier written as
an SD value. If not, the download is refused.

`GET /entity-exports/{id}/content` redirects to a signed URL for a few minutes,
the same as a bundle download.

### Asked for and downloaded, on the record

Asking is audited as `entities.export_requested`, with:

- the export's ID and format;
- the selection in words, and the search;
- the SDF fields;
- the count;
- each record as `{id, hrid, revision}`.

Each download is audited as `entities.exported`, with:

- the file's name, size and SHA-256;
- the records and their revisions;
- the notes per record (`left_out`);
- the fields;
- the writer.

The single-record download keeps `entity.exported` (ADR 0171) and gains
`writer` and `fields`.

### A writer is a per-record function and a container, so plugins can add one

Every file is written through one interface (ADR 0230):

- `begin(count)` returns the file's opening bytes;
- `record(r)` returns one record's bytes and its notes;
- `end()` returns the closing bytes.

Each export makes one writer and calls `record` for each pinned record in
order. Iki's built-in writers are a per-record function joined end to end.
FASTA, GenBank and SDF need no header or footer, so their `begin` and `end`
return nothing.

Each writer has an identity, `{builtin: "sdf", version: "<Iki's version>"}`,
which is in both audit events and on the export. A plugin's writer (ADR 0230)
plugs into the same loop, with its module's digest as its identity.

### On the page

A records table offers **Export** beside **Download CSV** when every type it
shows exports a format that many records make one file of. The CSV download
stays as it is.

- **Picking rows.** Picking is open to anyone who can read the records, so a
  Viewer, who runs and edits nothing, can pick rows to export them.
- **Where the dialog opens.** The selection bar offers **Export selected**;
  the toolbar's **Export** opens the dialog for the whole search.
- **The dialog** offers:
  - the formats every type shares;
  - for SDF, the data fields as checkboxes, all checked;
  - **Export**.
- **When it's written.** An export written at once downloads straight away.
  One written in the background shows a progress bar, then a link. Either way,
  the dialog lists what the format couldn't hold of each record and, if the
  export failed, which records and why.
- **Recent exports.** The requester's last five exports are listed, to
  download again until each expires.

## Consequences

- A selection, or a table's whole search, downloads as one FASTA, GenBank or
  SDF file. The single-record download (ADR 0171) adds protein FASTA,
  Molfile and SDF.
- The same records at the same revisions always make the same file, so an
  export can be checked against its hash and reproduced.
- A compound registered before this change has no Molfile until
  `compute_descriptors` runs again. Exporting it is refused, naming it.
- A compound's Molfile costs a few KB per compound in the database.
- 10,000 records and 512 MiB is the most one file holds. A lab that needs more
  narrows the search and exports again, or uses a bundle (ADR 0096).
- Iki's writers are pure functions of the stored values, so a fix to one
  reaches every type that exports it. A record's file can change between Iki
  versions, and the audit log says which version wrote it.

## Alternatives considered

- **RDKit in the server, laying out each structure on export.** This would
  have removed the stored Molfile, but it adds a native parser of
  user-supplied structures to the server. It would also have made a file's
  coordinates depend on the server's RDKit version, and turned a compound that
  RDKit can't lay out into an export that fails at download time rather than
  at registration. Rejected.
- **Keeping `export_sdf` as an action.** It needs a Runner, reads only each
  compound's latest revision, isn't audited, and its artifact expires. ADR
  0171 rejected this pattern for sequences, for the same reasons. Rejected.
- **Writing the file as it's read, streamed to the browser.** This would start
  the download sooner, but a failure partway through would send a partial
  file that looks whole, and the file's hash wouldn't be known until it had
  been served. Rejected for spooling it whole.
- **Leaving out the records that can't be written.** A file shorter than the
  selection is easily taken for all of it. Rejected: the export fails and
  names them.
- **A zip of one file per record.** Every tool that reads these formats reads
  many records in one file. A zip is for formats that hold one record, such as
  a Molfile, and is left to ADR 0230's `many: zip`.
