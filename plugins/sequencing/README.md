# Sequencing runs

Create a **Sequencing run** for the instrument run and a **Sequencing library** for
each prepared sample. Select the existing material record, such as a tube
returned by a core facility or a collected cell sample. Add a **FASTQ read set**
for each library and lane or output partition. Select read 1, plus read 2 for
paired-end data. Record distinct read sets if the run produced separate files
for different lanes.

Register instrument files in an approved, versioned bucket using the [Iki file
registration API](../../docs/adr/0036-files-content-addressed-uploads.md). Iki
pins the object version and checks its SHA-256. Use file IDs for the read set.
Source files remain under the bucket's retention policy. The worker stages the
chosen versions when an action uses them, so its scratch space must fit both
compressed input files together. The local demo uploads only its small excerpt.

Run **Summarize FASTQ quality** on the read set. It streams plain or gzip
FASTQ, checks record structure and paired identifiers in file order, and records
the read and base counts, shortest and longest reads, mean Phred score and
percentage of bases at Q30 or above. The action expects synchronized separate
files for paired-end data and modern Sanger/Illumina Phred+33. Where a read's
title says which mate it is, the action checks it against the file it came in
and refuses a pair given the wrong way round, so one mate's quality is never
reported as the other's. It reads the mate from a `/1` or `/2` ending a token of
the title (older Illumina titles, and ENA's, which keep the instrument's title
after the run's read name), from CASAVA 1.8's `1:N:0:…` comment, and from SRA
`fastq-dump --readids` names such as `SRR001666.1.2`. Read quality
displays each mate separately. The report retains the source file IDs, names,
checksums and the revisions of the read set and library that the run read. Iki
keeps the action run and its input provenance.

A project's Admin chooses the procedure for the project's new summaries with the
`sequencing-quality-review` starter, in the project's settings under Starters, and picks who
holds each lab role it asks for. A project that hasn't chosen uses the
deployment setting `review_flow`
([ADR 0266](../../docs/adr/0266-a-project-chooses-its-omics-results-review.md)):

| Procedure                          | What a summary needs                                                                                                          | Lab roles                |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| **Research** (`research`, default) | A reviewer signs the recorded summary.                                                                                        | Reviewer                 |
| **Regulated QC** (`regulated_qc`)  | An analyst signs submission, a reviewer independent of the creator, submitter and editors signs review, and QA signs release. | Analyst, reviewer and QA |
| **Record only** (`record_only`)    | Nothing: the summary is kept as evidence and offers no sign-off.                                                              | None                     |

Each summary keeps the procedure it was recorded under, so a change to the
procedure applies to new summaries only. To review a summary recorded under
Record only, summarize the read set again once the project or the deployment
names a review procedure.
Changes to signed findings return the summary to Recorded. Give the relevant lab
roles to project members before review. See [ADR 0255](../../docs/adr/0255-a-quality-summary-can-be-recorded-without-sign-off.md).

When the files are in an INSDC archive, give the read set its run accession
(SRR, ERR or DRR). An archived run belongs to one library, so each read set from a
multiplexed instrument run names its own; read sets for the lanes of one library
can share one.

## Public example

[ENA SRR8690267](https://www.ebi.ac.uk/ena/browser/view/SRR8690267) is a paired
Illumina HiSeq 2500 run from [BioSample SAMN11070697](https://www.ebi.ac.uk/biosamples/samples/SAMN11070697),
a _Saccharomyces cerevisiae_ RNA-seq sample from the published
[yeast transcriptomics dataset](https://doi.org/10.1186/s13104-019-4286-0).
The fixture files contain the first four complete records of each ENA mate,
decompressed. Their SHA-256 values identify the excerpts. They are not the
checksums of the complete archive files, and their quality statistics describe
only those four pairs. The action fixture verifies the two mates against these
excerpt checksums and expected Phred+33 statistics.

The FASTQ representation and Phred+33 offset follow [Cock et al.,
2010](https://doi.org/10.1093/nar/gkp1137), Table 1. Q30 means a Phred score
of 30 or greater.
