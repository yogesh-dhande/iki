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
files for paired-end data and modern Sanger/Illumina Phred+33. Read quality
displays each mate separately. The report retains the source file IDs, names,
checksums and the revisions of the read set and library that the run read. Iki
keeps the action run and its input provenance.

The deployment setting `review_flow` controls new summaries. In **Research** a
reviewer signs the recorded summary. In **Regulated QC** an analyst signs
submission, an independent reviewer signs review and QA signs release. Changes
to signed findings return the summary to Recorded. Give the relevant lab roles
to project members before review.

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
