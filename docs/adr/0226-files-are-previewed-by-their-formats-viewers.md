# 0226. Files are previewed by their formats' viewers

- Status: Proposed
- Date: 2026-11-25
- Refines: [0036](0036-files-content-addressed-uploads.md),
  [0177](0177-a-projects-files-are-found-by-name-and-say-what-uses-them.md),
  [0213](0213-plugins-declare-their-file-formats.md),
  [0215](0215-file-tools-use-the-files-list-and-its-download-links.md)

## Context

Scientists download a file to see whether it's the one they want: the plate
reader's export, the Sanger trace that failed, the certificate of analysis a
supplier sent. Most of these are formats a browser can draw, or that Iki can
read in a few milliseconds. Downloading them leaves copies on laptops and
moves the reading out of Iki's audit trail.

A preview reads the same bytes as a download. It must reach no file its
reader couldn't download, and be recorded as a download is. A file is
hostile until read: a reader for it must be bounded in what it reads, how
long it takes and what it returns.

## Decision

A file's format declaration names its viewer (ADR 0213). Iki's core formats
name these: `pdf`, `image` (PNG and JPEG), `image-tiff` (TIFF, BigTIFF and
OME-TIFF), `table-csv`, `table-tsv`, `table-xlsx` (and XLSM),
`chromatogram-abif` (AB1), `spectrum-mzml` and `spectrum-jcamp-dx`. A plugin's
format can name any of them. The viewers are Iki's own code, loaded the first
time one is opened; a format can't bring its own.

A preview opens on a file's page, under its facts, and from the records and
runs that hold the file, in a dialog beside its link. A file up to 25 MiB is
shown with its page. A larger one is shown when asked, since showing it reads
it, unless its viewer reads ranges (ADR 0227). A file past what its viewer
opens, of a format with no viewer, or that a viewer can't read, says why and
offers the download.

Every read starts at `GET /files/{id}/content-url`, the link a download takes:
the same access check, the same `file.download_link_issued` audit event, the
same five-minute signed link. There's no preview endpoint. The page reads the
bytes from the store; they never pass through Iki's web pods (ADR 0036). A file
the deployment's scan holds is refused its link, so its preview shows why and
neither its bytes nor its download. A file read whole is checked against the
SHA-256 Iki recorded; one that doesn't match isn't shown.

The readers run in a worker of their own, in `iki_previews.wasm`, a Rust crate
apart from iki-wasm so a preview's code doesn't grow every page that reads
sequences. Like iki-wasm, the module has no imports, its source and SHA-256 are
in git, and the build checks the module it made against that digest. Each read
has 30 seconds; past that the worker is ended and the next read starts another.
Each reader caps what it reads and says what it left out:

| Viewer   | Reads                                              | Caps                                                        |
| -------- | -------------------------------------------------- | ----------------------------------------------------------- |
| PDF      | the whole file, up to 100 MiB                      | a page has 30 s to draw; canvases of at most 32 megapixels  |
| Image    | the whole file, up to 64 MiB                       | the browser's own decoder                                   |
| TIFF     | ranges (ADR 0227)                                  | 10,000 images, 16-megapixel tiles                           |
| CSV, TSV | the whole file up to 16 MiB, else its first 16 MiB | 50,000 rows, 500 columns, 10,000 characters a cell          |
| XLSX     | the whole file, up to 64 MiB                       | the same; 256 MiB unpacked a part, 2 million shared strings |
| AB1      | the whole file, up to 32 MiB                       | 500,000 trace points, 100,000 bases                         |
| mzML     | ranges (ADR 0227)                                  | 2 million index entries, 2 million points a spectrum        |
| JCAMP-DX | the whole file, up to 64 MiB                       | 64 blocks, 2 million points                                 |

pdf.js draws PDFs, in a worker started under the app's Trusted Types policy,
with its fonts, character maps and image decoders served as the app's own
assets. It draws pages on the main thread (its renderer worker would start
without the policy), runs no script in the PDF and draws no forms. A PDF with
a password isn't asked for one.

The table readers follow the spreadsheet importer's rules (ADR 0178): the same
encodings and separators, a workbook's saved values with nothing calculated,
no macro run, and refusals for encrypted workbooks, DTDs and archives whose
parts unpack past their limits.

## Consequences

Each preview is an audited download link, so the audit trail records who
looked at a file as well as who downloaded it. Opening a file's page shows its
preview, so that page view is recorded as a read. A deployment that audits
downloads separately from views can't tell them apart from the event alone.

A format without a viewer is offered for download as before. Viewers for
structures and alignments stay where they are, on the records that hold them.
A new viewer is a change to Iki, not to a plugin.

The table, chromatogram and spectrum readers are new Rust code that reads
hostile files; their tests use public and synthetic samples, and bounds are
tested at their edges.
