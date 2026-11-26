# Sequencing analysis

Import one sample's results from a report your pipeline already produced. Each
format has its own action, with the inputs and fields it needs. Register the
report as an Iki file and choose the sequencing library and, when known, its
instrument run. Enter the pipeline or tool and its version. Optionally choose the
registered strain, cell line, donor or other material the library's sample
represents.

A result names the library's sample, pins the revisions of the records it read,
and keeps the original report and its SHA-256. The action stages only the report:
the library and run carry provenance without staging their FASTQ files. The
plugin needs only `iki/sequencing` installed.

| Action                         | Reads                                                                                                                                                                                                    | Keeps                                                                                                                                                                                                                                                       |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Import VCF sample**          | A [VCF](https://samtools.github.io/hts-specs/VCFv4.5.pdf) 4.0 to 4.5 file, plain or gzipped, with the reference assembly. Type the sample's exact column heading when there are several.                 | The sites where that sample's genotype (`GT`) calls an alternate allele. Calls of only the reference or a symbolic allele (`<NON_REF>`, `<*>`, `*`), and sites without a genotype call, are left out and counted. A sites-only VCF keeps its variant sites. |
| **Import RNA-seq counts**      | A [featureCounts](https://subread.sourceforge.net/SubreadUsersGuide.pdf) report or a gene-by-sample matrix whose first heading is `Geneid`, `gene_id` or empty, with the reference build and annotation. | One sample column's raw integer counts, by its exact heading. Normalized or fractional values are refused.                                                                                                                                                  |
| **Import AIRR rearrangements** | An [AIRR Rearrangement TSV](https://docs.airr-community.org/en/stable/datarep/rearrangements.html), optionally with its germline reference set. Choose the repertoire ID when there are several.         | Every rearrangement in that repertoire, with `locus` filled from the V and J calls when the file leaves it empty and they agree on an immunoglobulin locus.                                                                                                 |

**Report summary** shows the format, pipeline, version, reference, selection,
what the selection keeps, the counts and the first twelve selected rows. The
import run's `selected.tsv` artifact holds every selected row. Keep source BAM
and FASTQ files registered in their original storage. The actions run no
aligner, variant caller, counter or repertoire annotation tool, and use only the
Python standard library. A deployment's pipeline integrations supply their own
licensed software and images.

To record which registered antibody an AIRR rearrangement encodes, install
[`iki/antibody_rearrangements`](../antibody_rearrangements/README.md) too.

## Review

A project's Admin chooses the procedure for the project's new results with the
`sequencing-analysis-review` starter, in the project's settings under Starters, and picks who
holds each lab role it asks for. A project that hasn't chosen uses the
deployment setting `review_flow`
([ADR 0266](../../docs/adr/0266-a-project-chooses-its-omics-results-review.md)):

| Procedure                          | What a result needs                                                                                                           | Lab roles                |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| **Research** (`research`, default) | A reviewer signs the recorded result.                                                                                         | Reviewer                 |
| **Regulated QC** (`regulated_qc`)  | An analyst signs submission, a reviewer independent of the creator, submitter and editors signs review, and QA signs release. | Analyst, reviewer and QA |
| **Record only** (`record_only`)    | Nothing: the result is kept as evidence and offers no sign-off.                                                               | None                     |

Each result keeps the procedure it was recorded under. Changes to signed
findings return it to Recorded. Assign the matching project roles before review.

## Public fixtures

- The first four GRCh38 sites for [NIST Genome in a Bottle](https://www.nist.gov/programs-projects/genome-bottle)
  HG002 v4.2.1, from cell line [GM24385](https://www.cellosaurus.org/CVCL_1C78).
- The first four genes from the [GEO GSE176296](https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE176296)
  yeast study's raw count matrix. The fixture selects the WTJ.1 sample column.
- The first four contigs' annotation columns from the published human PBMC
  B-cell dataset used by [nf-core/airrflow](https://raw.githubusercontent.com/nf-core/test-datasets/d05f0f2a13d529e673068a61e9ff976d20d0f9ae/testdata-reveal/sc5p_v2_hs_PBMC_1k_b_airr_rearrangement.tsv).
