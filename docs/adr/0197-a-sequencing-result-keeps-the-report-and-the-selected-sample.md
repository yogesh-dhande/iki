# 0197. A sequencing result keeps the report and the selected sample

- Status: Proposed
- Date: 2026-11-23
- Refines: [0196](0196-a-sequencing-read-set-keeps-the-files-and-sample-it-came-from.md)

## Context

One report can contain multiple samples or repertoires. To review a result,
the lab needs the exact report, the selected sample and the library it came
from. A VCF also needs its originating strain or cell line and reference build.
An AIRR annotation can support a recorded association with a registered antibody.

## Decision

A worker action imports an existing VCF, raw RNA-seq counts or an AIRR
rearrangement TSV. It pins the input record revisions, preserves the original
file and its hash, and records pipeline and reference details. It retains the
chosen sample's or repertoire's rows as a result artifact. The result card
shows the selection, summary counts and a bounded preview. VCF results link
to the source strain or cell line. AIRR results can name present rearrangement
identifiers and the registered antibodies a scientist associated with them.

The import uses the sequencing library and optional instrument run as record
inputs. The report is the only file it needs. Labs can continue to keep raw
files in their original storage. The deployment chooses Research sign-off or
Regulated QC submission, independent review and QA release.

Work on whole-plasmid verification by long reads and amplicon/CRISPR-edit
outcomes follows in a separate slice.
