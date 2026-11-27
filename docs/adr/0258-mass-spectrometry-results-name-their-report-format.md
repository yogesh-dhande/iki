# 0258. Mass spectrometry results name their report format

- Status: Accepted
- Date: 2026-11-26

## Context

The proteomics importer read four tools' reports under one engine name each:
`mztab`, `maxquant`, `diann` and `fragpipe`. A name like "DIA-NN" claims more
than the importer reads. It reads the main report as tab-separated text, while
recent DIA-NN releases write it as Parquet, and it reads some of each tool's
tables and refuses others. A reviewer couldn't tell from the result which
table of a report had been read.

The biologics MS comparison reads two tab-separated tables whose columns Iki
defines. A lab makes them from its deconvolution or peptide-mapping software's
results. Neither the tables' contract nor the software's own exports were
named on the result, so a later change to the columns would leave old results
ambiguous.

Both results offered only Research sign-off and Regulated QC. A lab that keeps
search or comparison results as evidence, and signs a later analysis or
release, saw every result waiting for a signature nobody would give.

## Decision

The proteomics report formats are `mztab_1_0` (proteomics mzTab 1.0.0, or the
PRIDE 1.0 rc5 export), `maxquant_txt` (`proteinGroups.txt`, `peptides.txt`,
`evidence.txt`), `diann_tsv` (the main report as tab-separated text) and
`fragpipe_tsv` (protein and peptide TSVs, single or combined). Each source
report in the result's summary records the table it was read as. A Parquet
file is refused with what to do: convert it to TSV with its column names
unchanged. The importer adds no Parquet reader, so its runtime stays the
Python standard library.

The biologics MS tables are named, versioned interchange profiles: **Iki mass
table 1** and **Iki peptide map 1**. A result records the profile it read in
`report_format`. The lab attaches the processing software's own exports as the
result's source exports and names the software and version. A change to a
table's columns is a new profile. Mass basis and the lab's ppm tolerance stay
explicit, and a mass comparison needs the tolerance. The subject stays a
registered antibody, with an optional ADC and lot. A protein-only profile
would need a different chain model, so it isn't offered.

Both plugins' `review_flow` setting gains **Record only** beside Research and
Regulated QC. A result recorded under it keeps the same evidence and pins the
procedure. Every signed state refuses it with one guard,
`reviewed_procedure`, which names the procedure. Research and Regulated QC keep
their states, signatures, locks, returns on edit and independence guards
unchanged, and guard tests cover all three procedures for both result types.

## Consequences

A reviewer reads which report and table a result came from, and which column
contract an MS comparison was read with. Labs on recent DIA-NN convert their
report first, rather than finding the importer reading something else. A lab
that records MS results without sign-off isn't left with results that look as
if they await a signature.

Adding a reader for another tool or table is a new format value with a real
exporter file as its test. The choice of procedure is still made for the whole
deployment, not per project.
