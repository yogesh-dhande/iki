# 0177. A project's files are found by their name, and say what uses them

- Status: Accepted
- Date: 2026-11-22
- Refines: [0036](0036-files-content-addressed-uploads.md),
  [0052](0052-imports-file-params-and-origins.md),
  [0089](0089-a-param-can-take-several-files.md),
  [0176](0176-the-app-is-laid-out-around-a-sidebar-and-what-you-can-read.md)

## Context

A scientist often starts from a file, not a record. An instrument names a trace
`A01_clone-17-F.ab1`; a core emails a plate's `.fcs` files; a collaborator
sends `coa-lot-12.pdf`. Iki stored every file (ADR 0036), and a file field or a
file param offered the project's newest hundred in a select. There was no way
to find a file by its name, to see the files of a day's acquisition, of a
sample or of a run, or to ask what records and runs a file is in. An auditor
asks that last one first: which results came from this raw data file?

Benchling keeps files on the notebook entries and results that hold them, and
finds them by name in its global search. Dotmatics finds instrument files by
their name, date and the sample they were acquired for. GitHub's file finder
starts from part of a name. None of them takes a file's upload time for when its
data was acquired: an instrument's file records that itself, and in regulated
work the instrument's own record is the original (ALCOA+).

## Decision

**A project has a Files tab,** and Iki has one Files list across every project
its reader can read (`/files/`), with a Project filter, as Runs has (ADR 0176).
They're one table. A project's files are those added to it, and those its
records hold in a file field at any revision (`access.project_files`). Across
projects, they're every file the reader can read (`access.readable_files`).
Nothing from a project the reader can't read appears, nor is counted.

- **Search** finds part of a file's name, in any case (by a trigram index on
  the names in upper case, as a case-insensitive match compares them). `%` and
  `_` are text.
- **Filters:** format (media types, each exact or a family such as `image/*`,
  with the formats present and their counts), when its data was acquired (a
  range of days), sample, and run. A file field or a file param also narrows
  it to the media types and the size it takes.
- **Sort:** newest added first, by default; oldest; by name; or by when its
  data was acquired, latest or earliest first, unknown last either way.
- **Counts** stop at 1,000, as search everything's do.

`GET /files` answers it, with `GET /files/count` and `GET /files/formats`.
Search everything (⌘K) finds files too, as a kind of its own (`files`).

**Pages merge two sets, each read by its own index.** A project's files are two
disjoint sets: added to it (found by `(project, …)` indexes), and added
elsewhere but held by its records (found through their uses). Across projects,
the sets are files added to a readable project and files held by readable
records from elsewhere. Each set's first rows in the page's order are read,
then merged. An `OR` of the two would hide the indexes from Postgres, and at a
million files it would read them all. Pages are keyset pages: the cursor holds
the last row's sort values and ID, signed. Names sort by their lowercase bytes
(collation `C`), as Python compares them, so the merge agrees with Postgres's
order and no row is skipped or repeated. Indexes: names' trigrams; `(project,
media_type, id)`; `(project, acquired_on, id)`, and descending with unknown last;
`(project, lower(name) COLLATE "C", id)`. Files held from elsewhere are found
from the records' uses, by the files' keys.

**A name search asks the trigram index first** which files' names hold the
text, on its own, up to 5,000 of them. Postgres can't tell from the text how
many files have it: it would read a million files in the page's order for a
name only one has. A rare name, as most searched for are, is then a page of
those few; a common one reads the page's order, which finds a page quickly
because so many match. Text under three characters, which trigrams can't find,
reads the page's order too. `make check-scale-files` checks the plans and times
at a million files.

**When a file's data was acquired** is read from the file, where its format
records it; otherwise it's what whoever added it said; otherwise it's unknown.
It's never the time the file was added. That's another fact, shown apart.

- **Read from the file** when Iki checks an upload's bytes, or a registered
  file's (ADR 0036), with a few ranged reads of at most 4 MiB in all:
  - an AB1 trace's run start (`RUND` and `RUNT`);
  - an FCS file's `$BEGINDATETIME`, or `$DATE` and `$BTIM`;
  - an mzML run's `startTimeStamp`;
  - a JCAMP-DX spectrum's `##LONGDATE`, or `##DATE` and `##TIME`;
  - an image's EXIF `DateTimeOriginal`, or an OME-TIFF's `AcquisitionDate`
    (never IFD0's `DateTime`, which is when the file last changed);
  - the earliest `@RG DT` of a SAM or BAM header.

  A malformed field, an ambiguous two-digit year, or a date outside 1980–2200
  is unknown, not guessed. The readers are Iki's, in the standard library: these
  are open formats that labs' instruments write. A format a plugin knows better
  can still set the date through its own import.

- **Given when it's added**: `acquired_at` on an upload or a registration, a
  day or a moment in ISO 8601, not after tomorrow. The file's own wins, since
  it's the instrument's record.
- **Kept as precisely as its source gave it**: a day, or a time to the second or
  to the fraction recorded, with an offset only if the source stated one. Most
  instruments record their local time without a zone, and Iki doesn't invent
  one. `acquired_on` is that day, which the filter reads; `acquired_from` says
  how it's known (`given`, or the format, such as `abif`).
- Files never change (ADR 0036), so a file's acquisition is set once. A bundle
  carries it, with how it's known, to the deployment it's imported into.

**A file belongs to a sample** when a record of a sample type holds it in a
file field, at any revision, or when a run that read the sample also read or
made the file. A type says `sample: true` in its manifest, and its subtypes'
records are samples too. Iki doesn't guess from a type's name or fields: labs
call their samples specimens, aliquots, lots or wells, and some of their records
with amounts aren't samples. `iki/example-lims`'s Sample and Aliquot, `iki/lots`'
Lot and the plugin template's Sample say so.

**A run's files are recorded,** append-only (`iki_run_file`):

- `param`: a file param's, or one of a list of them (ADR 0089);
- `input`: held by one of its inputs, or a reference they include, at the
  revision it's given, recorded when it's launched;
- `made`: uploaded by its worker for its results (ADR 0044).

A reused run (ADR 0057) records what it was given, too. So a file lists the runs
that used it, and the list finds files by run.

**Used by** lists, for each file, every record the reader can read that holds
it, with the revisions that held it, and every run they can read that read or
made it, with how. The list shows the counts.

- **Revisions** are spans, worked out from the record's history: from the
  revision that set it in a field to the one before the revision that took it
  out, or to now. A file in a list of rows is held by the list's field.
- **A file's page** (`/files/<id>/`) shows its name, size, SHA-256, format, who
  added it and when, when its data was acquired and how that's known, where it
  came from, its samples, and Used by. It also has a download.
- **Where it came from**: an upload; a run's results, naming the run if the
  reader can read it; a registration in place, with its location and version; or
  an import from another deployment.

`GET /files/{id}`, `GET /files/{id}/records` and `GET /files/{id}/runs`
answer it.

**The file picker is the same view, in a dialog.** Wherever someone chooses an
existing file, the picker offers the same search, filters and sort, held to
what the field or param takes: entity file fields, lists of files and file
cells in rows, run file params, lists of file params (ADR 0089), and the
spreadsheet importer. Uploading a new file stays beside it. A list of files
can choose several at once, up to its `max_items`.

## Consequences

- Someone with only a filename from an instrument or an email finds the file,
  then the records and runs that use it, in two steps.
- An auditor reads, from a raw data file, every record revision and run that
  used it, and when its data was acquired according to the instrument.
- Files without a date in their format, and added without one, stay unknown.
  The list says so, and sorts them last. A file's date can't be corrected
  later, since files never change. A wrong one is fixed by adding the file
  again, with the right date.
- Plugins decide which of their types are samples. A plugin that doesn't say
  has no samples to filter by, and the filter says there are none.
- Search under three characters reads names without the trigram index, so it's
  slower. A short query in a large project still returns its first page as soon
  as it finds that many.
- MCP and the SDK read files by ID as before. They can list them through the
  same API.

## Alternatives considered

- **The upload time as acquisition.** Rejected: it's when the file reached
  Iki, often days after the run, and it would look like the instrument's.
- **Guessing samples from the type's name or fields.** Rejected: labs name them
  differently, and a wrong guess would file data under the wrong sample.
- **One query with `OR`.** Rejected: Postgres can't use an index across it, and
  reads every file of a large project to sort the first page.
- **Recording which revisions held a file when each is written.** Rejected for
  now: every write path would need to close spans. A record's history already
  has the answer, and Used by reads it a page of records at a time.
