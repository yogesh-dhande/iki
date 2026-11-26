# 0246. A plate reader file is read in a named format, one read per export

- Status: Accepted
- Date: 2026-11-26
- Refines: [0188](0188-a-plate-read-keeps-the-wells-behind-a-fitted-endpoint.md)

## Context

ADR 0188 gave plate assays one parser. It guessed from a file's first line
whether it held a BMG LABTECH MARS absorbance endpoint or a matrix of
normalized activity, and joined either to the layout in the same code. The
matrix is Iki's own arrangement of a public deposit, not a vendor export, but
nothing said so. A plate could commit once: a second read of the same
physical plate, later or at another wavelength, needed a copy of the plate.
A well measurement held absorbance or activity in one of two fields, and the
fit told which by which field was empty.

A reviewer needs to know which reading of an export produced a measurement,
and an export that a format doesn't cover should be refused, not guessed at.

## Decision

**A plate names its reader file format.** `reader_format` is
`bmg_mars_absorbance` (the default) or `iki_plate_matrix`. Each format is a
declared subset with a version: the MARS format reads one raw absorbance
endpoint at one wavelength, displayed in OD, from a MicroPlate View CSV;
the Iki plate matrix reads signed normalized activity in percent under its
one-line heading. The plugin README and `data/reader.md` list what each
reads and doesn't. A file in the other format is refused, naming the format
to choose. Each format's parser yields a matrix, its readout and the export's
own header. One join, the same for every format, matches each cell to its
well by labelled row and numbered column.

**Each export is a read of the plate.** Committing a clean preview creates a
plate read with the format and version it was read in (`bmg_mars_absorbance/1`)
and the export's run header, kept verbatim. A MARS date stays text, as its
order follows the instrument PC's locale. The first commit fixes the plate's
size, assignments, procedure and request. Attaching another export and
committing it adds the plate's next read on that layout. A read's key is a
name-based UUID of the export's content digest within the plate (RFC 9562,
UUIDv5). It fits the existing key column, and the same export commits to a
plate once, including when two commits race, and its preview says it's already
committed.

**A reading is held in its readout's field.** A well measurement records its
`readout`, `absorbance` or `normalized_activity`, and a rule holds the reading
in that readout's typed field and only there. The fit reads the readout, never
which field is empty. Absorbance is never read as activity, nor the reverse.

## Consequences

A lab can re-read a plate without copying it, and every result still pins the
read, format version and wells it came from. Adding a reader format means a
new parser, its golden files and its documented subset; the join, commit and
fit don't change. A lab whose reader isn't listed can convert normalized
activity to the Iki plate matrix; other readouts need a format first.
Fluorescence and luminescence aren't read yet.
