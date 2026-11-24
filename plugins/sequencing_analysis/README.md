# Sequencing analysis

Use **Import sequencing result** with a sequencing library and, when known,
its instrument run. Register the existing pipeline report as an Iki file.
Select the report format and enter the pipeline and version that produced it.
A result names the library's sample, pins the selected input record revisions,
and keeps the original file and SHA-256. The action stages the report. Its
library and run inputs carry provenance without staging the corresponding FASTQ.

- **VCF.** Supply the reference assembly and select the strain or cell line
  that the library represents. Type the exact sample heading for a
  multisample VCF. Iki keeps calls with an alternate allele for that sample.
  Sites-only VCFs keep the variant sites supplied for the selected material.
  The [VCF specification](https://samtools.github.io/hts-specs/VCFv4.5.pdf)
  defines the position, alternate allele and genotype fields.
- **RNA-seq.** Supply the reference build and annotation. Use a
  [featureCounts](https://subread.sourceforge.net/SubreadUsersGuide.pdf)
  gene count report or a gene-by-sample TSV starting with `Geneid`,
  `gene_id` or an empty heading for row names. Select one column of raw
  integer counts by its exact sample heading.
- **AIRR.** Supply a
  [Rearrangement TSV](https://docs.airr-community.org/en/stable/datarep/rearrangements.html)
  and select the repertoire ID if present and ambiguous. To record a curated
  association, select the existing antibody and enter its HRID with the exact
  `sequence_id` from the report, separated by a tab. The importer verifies
  that the rearrangement is present in the chosen repertoire and has an
  immunoglobulin locus. It records the analyst's association.

**Report summary** shows the selection, counts and the first twelve records.
The run's `selected.tsv` artifact contains all selected rows. Keep source
BAM and FASTQ files registered in their original storage. The shipped action
reads VCF, count and rearrangement reports that the lab's pipelines produced.
It uses the Python standard library. A deployment's pipeline integrations
supply their own licensed software and images.

Set `review_flow` at deployment time. **Research** records one sign-off.
**Regulated QC** records signed submission, independent review, and QA release.
Assign the matching project roles.

## Public fixtures

- The first four GRCh38 sites for [NIST Genome in a Bottle](https://www.nist.gov/programs-projects/genome-bottle)
  HG002 v4.2.1, from cell line [GM24385](https://www.cellosaurus.org/CVCL_1C78).
- The first four genes from the [GEO GSE176296](https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE176296)
  yeast study's raw count matrix. The fixture selects the WTJ.1 sample column.
- The first four contigs' annotation columns from the published human PBMC
  B-cell dataset used by [nf-core/airrflow](https://raw.githubusercontent.com/nf-core/test-datasets/d05f0f2a13d529e673068a61e9ff976d20d0f9ae/testdata-reveal/sc5p_v2_hs_PBMC_1k_b_airr_rearrangement.tsv).
