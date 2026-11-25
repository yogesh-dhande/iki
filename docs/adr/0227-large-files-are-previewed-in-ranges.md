# 0227. Large files are previewed in ranges

- Status: Proposed
- Date: 2026-11-25
- Refines: [0036](0036-files-content-addressed-uploads.md),
  [0226](0226-files-are-previewed-by-their-formats-viewers.md)

## Context

A whole-slide image or a confocal stack is often 2 GB or more as a pyramidal
TIFF or OME-TIFF. An LC-MS run's mzML file is as large. A browser can't hold
either, and a scientist wants one region or one spectrum, not the file.

Both formats say where their parts are. A TIFF's directories list each tile's
offset and length, and a pyramid's lower levels. An indexed mzML file ends with
the offset of an index that lists each spectrum's offset. The content link Iki
gives a download is a signed URL to the store, which serves byte ranges.

## Decision

A large file is read in ranges through the same audited content link (ADR
0226). The page asks the store for `Range: bytes=a-b`; it doesn't ask Iki for
tiles. Iki's web pods serve no file bytes, as for downloads (ADR 0036), and need
no image or mass spectrometry code.

A TIFF's reader is given what the page has read and answers with the ranges it
needs next, until it has the file's layout: its images, their tiles or strips,
their pyramid levels (SubIFDs or OME-TIFF's reduced-resolution images) and,
for OME-TIFF, its channels, Z and T planes. The page then reads the tiles in
view at the level that fits the zoom, a few at a time, and the reader decodes
each to pixels: uncompressed, LZW, Deflate, PackBits or 8-bit JPEG, with the
TIFF predictors, 1 to 32 bits and floats. Images past 8 bits share one contrast
window across tiles, measured from the smallest level; 8-bit images are shown
as they are, so tiles meet without seams.
A TIFF up to 64 MiB is read whole and checked instead.

An mzML file's reader reads its last 4 KiB for the index's offset, then the
index, then the spectrum asked for, from its offset to the next one's. A file
without an index is scanned from its start, up to 64 MiB, and the preview says
how much it looked through. An mzML file up to 64 MiB is read whole and checked.

A CSV or TSV file past 16 MiB is read from its start only; its reader stops at
the last whole row and the preview says how much it read.

Each range is checked: the store must answer 206 with a `Content-Range` for the
bytes asked for, of a file the size Iki recorded. A store that sends the whole
file instead is stopped. One preview reads at most 512 MiB in all.

Browsers read a cross-origin `Content-Range` only if the store exposes it, and
send `Range` only if the store allows it. `iki-server setup_storage` and the GCP
module's bucket now allow `Range` and expose `Content-Range`, as they allow
`Content-Type`. A store set up before this says so in the preview, which names
the command.

## Consequences

A file read in ranges can't be checked against its SHA-256 without reading all
of it. Each range is checked to be the bytes asked for, of a file of the
recorded size, and the preview says the file wasn't checked whole. A download
remains the way to verify a file.

Each range reuses the five-minute link while it's fresh and asks for another
before it expires, so a long look at a slide issues several audited links.

A compressed TIFF whose image is one huge strip, or whose tiles are past 16
megapixels, isn't drawn: the preview names desktop viewers instead. (An
uncompressed strip is read in parts.) JPEG 2000, Zstandard, LERC, WebP and
CCITT compression, 12- and 24-bit samples, CMYK and Lab aren't read either, and
the preview says which. MS-Numpress-compressed mzML arrays
aren't read, and the preview says so.
