# 0213. Plugins declare their file formats

- Status: Proposed
- Date: 2026-11-25
- Refines: [0032](0032-plugin-manifest-spec-v1.md), [0052](0052-imports-file-params-and-origins.md), [0085](0085-types-configure-ikis-own-viewers.md), [0177](0177-a-projects-files-are-found-by-name-and-say-what-uses-them.md)

## Context

A lab adding a format to Iki had to change the file picker, the worker and the
server separately. A worker could produce a file that its own action's input
field then refused. The Files list knew only the media type recorded on a file.

## Decision

A plugin declares the formats it owns under `formats` in its manifest. Core is
a plugin present in every deployment. A local format name such as `fastq` in
`iki/core` gives the stable ID `iki/core.fastq`. Each declaration gives a label,
canonical media type, any aliases and the filename extensions. It can also
give byte signatures, a metadata reader and a viewer. Reader and viewer names
refer to implementations supplied by Iki. A manifest cannot supply executable
code to the upload service or the browser this way.

The deployment combines installed declarations. Two plugins claiming an
extension or media type fail validation. A plugin can describe two variants
that share a suffix, such as nucleotide and protein FASTA. The accepted input
types disambiguate them when a caller knows the intended variant. Otherwise
the first declaration wins. A compound suffix takes precedence over a shorter
suffix, and case in a filename is immaterial.

File fields and file parameters accept qualified format IDs alongside exact
media types and media type families. The actions associated with a format
are derived from file input parameters. Existing record viewers continue to
obtain fields and record context from `display.viewers`. The format identifies
the file decoder where that viewer needs one.

The API exposes the installed formats. File pickers, worker uploads, Files
facets and generated reference documentation use the declarations. The Files
facet still counts only files visible to the caller under the active filters.
Aliases allow older files to be found without rewriting immutable file records.

## Consequences

Adding an instrument format with a new suffix and an existing reader takes one
plugin declaration. Adding a new executable reader or decoder requires an Iki
change. A conflicting plugin requires an explicit decision about which plugin
owns the format before the installation can proceed.
