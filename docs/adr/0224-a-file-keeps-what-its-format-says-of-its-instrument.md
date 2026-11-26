# 0224. A file keeps what its format says of its instrument

- Status: Proposed
- Date: 2026-11-25
- Refines: [0177](0177-a-projects-files-are-found-by-name-and-say-what-uses-them.md), [0213](0213-plugins-declare-their-file-formats.md), [0214](0214-recognize-uploaded-formats-before-recording-them.md)

## Context

Most instrument formats record which instrument made the file and who ran it.
An FCS file has `$CYT`, `$CYTSN` and `$OP`. An mzML run names its instrument
configuration. An AB1 trace has `HCFG` and `MCHN` tags. A BAM file has `@RG`
lines. A reviewer checking a result needs these to confirm which instrument
produced it. A scientist looking for a run needs them to find it. Until now,
Iki read only a file's acquisition time
([ADR 0177](0177-a-projects-files-are-found-by-name-and-say-what-uses-them.md)).

Formats often state facts loosely. For example:

- An mzML configuration can list ion optics beside the model.
- A CZI document names both the person who acquired it and the person who
  last saved it.
- An AB1 trace's `User` tag is whoever created the plate.

A wrong instrument or operator on a GxP record is worse than none.

## Decision

When a file is added, the bounded reader its format names
([ADR 0213](0213-plugins-declare-their-file-formats.md)) reads these
**facts**. It reads them in the same pass as the acquisition time, within the
same byte and read budget. A fact that fails to read leaves the date
unaffected, and a date that fails to read leaves the facts unaffected.

The vocabulary is fixed:

- `instrument` (its model)
- `manufacturer`
- `serial`
- `instrument_name` (the name a lab gave it)
- `operator`
- `software`
- `protocol`

Each fact is a value plus `stated_in`, which says where in the file it was
read, such as `FCS keyword $CYT`. A reviewer can check the value against the
file.

A reader states a fact only when the format's own specification defines it:

| Format   | Read                                                                                                                                                        |
| -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| AB1      | Instrument from `HCFG` 3, else `MODL` 1. Serial from `HCFG` 4's `SerialNumber`. Instrument name from `MCHN`. Software from `SVER` 1. Protocol from `RPrN`.  |
| FCS      | Instrument from `$CYT`. Serial from `$CYTSN` (3.0 on). Operator from `$OP`.                                                                                 |
| mzML 1.1 | Instrument and serial from the run's default instrument configuration, with its parameter groups resolved. Software from the configuration's `softwareRef`. |
| OME-TIFF | The microscope and experimenter that the first Image refers to.                                                                                             |
| CZI      | The microscope that the image's `MicroscopeRef` names. Operator from `Information/User`.                                                                    |
| JCAMP-DX | Instrument from `##SPECTROMETER/DATA SYSTEM`.                                                                                                               |
| SAM, BAM | Every `@RG` line's standard tags.                                                                                                                           |
| MARS CSV | User and test name from a BMG LABTECH MARS export's header, recognised by content.                                                                          |

For mzML, the model is the most specific term under MS:1000031 and the
software the most specific term under MS:1000531. These are checked against
the PSI-MS vocabulary's term tree, which Iki vendors under CC BY 4.0. Other
terms that appear alongside are never taken for the model or the software.

### When a fact is unknown

A fact is unknown, and never inferred, in any of these cases:

- The file doesn't state it.
- The file states it twice with different values.
- The format's layout doesn't hold.
- The value is blank, longer than 255 characters, or contains control
  characters or undecodable bytes.

Read groups are kept whole or not at all: up to 512, each with a unique ID,
each standard tag at most once. Iki never shows a partial list as if it were
complete.

### What isn't read

Some fields look like an operator but name someone else. Iki doesn't read
them:

- AB1 `User`
- mzML `contact`
- OME `Creator`
- CZI `Document/UserName`
- EXIF `Make` and `Model`

The MARS export's date isn't read either, because its regional format is
ambiguous.

### Handling untrusted files

Every file is treated as hostile:

- Declared lengths and positions are checked against the file's size and the
  read budget.
- XML is parsed with expat and any DTD is refused, so no entity can expand.
- Nesting depth, element count and text length are bounded.

### Storage, search and sharing

The file record is append-only. It stores the facts as a JSON object and the
read groups as a list, and the database checks both types. It also stores a
search text of the facts' values and the read groups' names, capped at
64 KiB and indexed with trigrams.

The Files list's `q` matches that text as well as the name. This applies to
the browser, the API, MCP `find_files` and both SDKs.

Each row in the list carries the facts. The file's page, `GET /files/{id}`,
MCP `get_file` and the SDKs' `files.get` also carry the read groups.

A bundle ([ADR 0177](0177-a-projects-files-are-found-by-name-and-say-what-uses-them.md))
carries both. On import they are checked as a reader's would be, and the
search text is rebuilt from them.

## Consequences

- Positive: A reviewer sees which instrument and operator produced a file, and
  where in the file each is stated. A scientist finds a run by its cytometer's
  serial, its operator or its sample's read group, from the browser, an agent
  or a notebook.
- Positive: Adding a fact to a format is a reader change and a test against
  that format's specification. It needs no schema change.
- Negative: Files added before this have no facts. Re-reading them is a
  backfill that this change doesn't do.
- Negative: Some formats can't be read further without a guess, so some
  files show fewer facts than a vendor's own software shows. Examples are
  FCS's supplemental TEXT segment and vendor-specific mzML user parameters.

## Alternatives considered

- **One free-form metadata map per file.** It would hide which fields are
  defined and checked. It would also let each reader name the same fact
  differently.
- **Reading facts on demand when a page opens.** This would refetch the bytes
  for every view. The Files list couldn't search the facts.
- **Taking the first plausible field, such as a document's last saver as the
  operator.** This produces confident wrong answers on records that people
  sign.
