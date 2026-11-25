# 0214. Recognize uploaded formats before recording them

- Status: Proposed
- Date: 2026-11-25
- Refines: [0036](0036-files-content-addressed-uploads.md), [0177](0177-a-projects-files-are-found-by-name-and-say-what-uses-them.md), [0213](0213-plugins-declare-their-file-formats.md)

## Context

An uploader may have only the media type guessed by its operating system. Two
callers may consequently describe the same instrument file differently. Slack
documented using both filenames and magic bytes for file uploads. GitHub
Linguist uses filename and content evidence when identifying languages.
[Slack upload method](https://github.com/slackhq/slack-api-docs/blob/master/methods/files.upload.md) ·
[GitHub Linguist detection](https://github.com/github-linguist/linguist/blob/main/docs/how-linguist-works.md)

## Decision

At verification, Iki reads only as far as the last declared signature in the
first 4,096 bytes. The longest matching byte pattern identifies the format.
Equally long matches for different formats fail verification. In the absence
of a match, Iki uses the longest matching suffix, with the caller's specific
type available for ambiguous names such as `.fa`. A format with mandatory
magic must match one of its patterns. A specific claimed media type that
contradicts a byte match also fails verification before the file is recorded.
An unknown format can still be stored with its stated media type, a standard
type inferred from its name, or `application/octet-stream`. Each parser
validates the contents it consumes.

Core uses established scientific media types and registered types where
available. Four choices affect existing callers.

- A `.mol` file is `chemical/x-mdl-molfile`. An `.sdf` file is
  `chemical/x-mdl-sdfile` and can hold multiple records and their properties.
  The worker used to assign the SDF type to both.
  [Open Babel's MOL and SD format registrations](https://github.com/openbabel/openbabel/blob/master/src/formats/mdlformat.cpp)
- A `.fna` file is `chemical/seq-na-fasta` and a `.faa` file is
  `chemical/seq-aa-fasta`. A `.fasta` or `.fa` can hold either. Its input
  field can select protein FASTA. The registry preserves a caller's
  `text/plain` or `text/x-fasta` declaration for either shared extension.
  Neither declaration identifies an alphabet. Callers that provide no type
  or field hint retain the existing nucleotide FASTA default. The Files filter
  includes files recorded as `text/x-fasta` under “FASTA (unspecified alphabet)”.
  [Wolfram's FASTA formats](https://reference.wolfram.com/language/ref/format/FASTA.html.en)
- MOL2 and HDF5 use `application/vnd.sybyl.mol2` and
  `application/vnd.hdfgroup.hdf5`. Their previous `chemical/x-mol2` and
  `application/x-hdf5` spellings are aliases.
  [MOL2 registration](https://www.iana.org/assignments/media-types/application/vnd.sybyl.mol2) ·
  [HDF5 registration](https://www.iana.org/assignments/media-types/application/vnd.hdfgroup.hdf5)
- Gzipped FASTQ is stored as `application/gzip`, the type of its stored bytes.
  Plain FASTQ remains `text/x-fastq`. Other extensions previously known to
  only one client become available to both, including the worker's plain-text
  alignment suffixes. The generated reference lists each one.
  [Gzip registration](https://www.iana.org/assignments/media-types/application/gzip)

The selected format names the bounded metadata reader used for acquisition.
Its result can carry structured metadata alongside the acquisition date.
The reader preserves the source, precision and fallback rules of ADR 0177.

## Consequences

A worker's MOL output can be distinguished from an SDF output during upload
and selection. Formats with shared container headers or variable preambles can
omit signatures and rely on extensions. The declaration can describe a new
reader's metadata without changing how callers select the reader.
