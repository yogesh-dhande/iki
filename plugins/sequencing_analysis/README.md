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

A project's Admin chooses how the project's new analysis results are reviewed with the
`iki/sequencing_analysis.analysis_review` starter, in Project settings → Starters, and picks who holds each
shared lab role it asks for (`iki/lab_roles`). An Org Admin may set the
deployment's default in Admin → Review procedures. Until one of them chooses,
analysis results can't be recorded. Each result is pinned to the procedure it was
recorded under, so a change applies to new analysis results only
([ADR 0302](../../docs/adr/0302-a-project-chooses-each-types-review-procedure-and-the-server-pins-it.md)).
The flows are core's result review (`iki/core.result_review@1`):

| Flow                     | What a result needs                                                                                                                   | Lab roles                |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| **Research** (suggested) | A reviewer who didn't create or edit it signs it off.                                                                                 | Reviewer                 |
| **Regulated QC**         | An analyst signs the submission, a reviewer independent of the creator, submitter and editors signs the review, and QA signs release. | Analyst, reviewer and QA |
| **Record only**          | Nothing: the result is kept as evidence and offers no sign-off.                                                                       | None                     |

To review a result recorded under Record only, import the report again once the project
chooses a flow with review.

## Public fixtures

- The first four GRCh38 sites for [NIST Genome in a Bottle](https://www.nist.gov/programs-projects/genome-bottle)
  HG002 v4.2.1, from cell line [GM24385](https://www.cellosaurus.org/CVCL_1C78).
- Seven GRCh38 calls from the same benchmark, bgzipped as NIST distributes it
  ([HG002_GRCh38_1_22_v4.2.1_benchmark.vcf.gz](https://ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/release/AshkenazimTrio/HG002_NA24385_son/NISTv4.2.1/GRCh38/)):
  the file's header and its first calls of each genotype it has, heterozygous
  (0/1, 1/0), homozygous (1/1) and multiallelic (1/2, 2/1).
- DeepVariant 1.10's golden gVCF for NA12878 on chr20, from the
  [DeepVariant test data](https://github.com/google/deepvariant/blob/45f2627504c59785ea2b88d0256a2ec347bce7b4/deepvariant/testdata/golden.postprocess_gvcf_output.g.vcf)
  (BSD-3-Clause): reference blocks (`<*>`), no-calls and variant calls.
- The first four chrY sites of the [1000 Genomes phase 3](http://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/)
  cohort VCF, whose 1,233 male samples have haploid calls. The fixture selects HG00101.
- GATK's [spanning deletion test VCF](https://github.com/broadinstitute/gatk/blob/99963319154d1e8f3a5b4fd61819d883565a246b/src/test/resources/org/broadinstitute/hellbender/tools/walkers/variantutils/SelectVariants/spanning_deletion.vcf)
  (Apache-2.0), with a site whose only alternate in one sample is `*`.
- A featureCounts 2.0.1 table with its program line, from
  [bamtocov's test inputs](https://github.com/telatin/bamtocov/blob/8a79a6db596a5644ebcd5d954649f748efebbc69/input/tiny/fc) (MIT).
  The tests compress it and the AIRR fixture with gzip to read them as a pipeline writes them.
- The first four genes from the [GEO GSE176296](https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE176296)
  yeast study's raw count matrix. The fixture selects the WTJ.1 sample column.
- The first four contigs' annotation columns from the published human PBMC
  B-cell dataset used by [nf-core/airrflow](https://raw.githubusercontent.com/nf-core/test-datasets/d05f0f2a13d529e673068a61e9ff976d20d0f9ae/testdata-reveal/sc5p_v2_hs_PBMC_1k_b_airr_rearrangement.tsv).
