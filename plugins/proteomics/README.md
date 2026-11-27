# Proteomics

Register each instrument file in its approved versioned source bucket. Create a
**Raw mass spectrometry file** record that names the registered file and, if
known, its sample. Add these records to a **Proteomics dataset**. For a public
dataset you can also give its archive page. Existing Iki file registration pins
object versions and checks SHA-256.

Run **Import proteomics search reports** with the dataset, the report format
and software version, and one or more original reports from that tool. Optional
protein inputs must have distinct UniProt accessions. The importer links
reported exact accession matches, including group members. For UniProt FASTA
headers it reads the accession between the pipes. It reports selected
accessions with no matching hit, and keeps the original IDs for unmapped
proteins.

| Report format  | Tool                                                                                                                                                      | Supported reports and tables                                                            | Reported quantities retained                                            |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `mztab_1_0`    | [HUPO-PSI mzTab](https://github.com/HUPO-PSI/mzTab)                                                                                                       | Proteomics 1.0.0 and the legacy PRIDE 1.0 rc5 export, protein, peptide and PSM sections | Protein and peptide assay or study-variable abundance                   |
| `maxquant_txt` | [MaxQuant](https://massive.ucsd.edu/ProteoSAFe/DownloadResultFile?file=f.MSV000088694%2Fquant%2FMaxquant%2FBulkSet%2Ftxt%2Ftables.pdf&forceDownload=true) | `proteinGroups.txt`, `peptides.txt`, and `evidence.txt` from the `txt` folder           | Intensities, LFQ, iBAQ and ratios                                       |
| `diann_tsv`    | [DIA-NN](https://github.com/vdemichev/DiaNN)                                                                                                              | The main report as tab-separated text, with run, group and precursor columns            | Precursor quantities and one protein-group quantity per run and channel |
| `fragpipe_tsv` | [FragPipe](https://fragpipe.nesvilab.org/docs/tutorial_fragpipe_outputs.html)                                                                             | Protein and peptide reports with a Protein ID column, including the combined reports    | Intensity, MaxLFQ and spectral-count columns                            |

Each source report records the table it was read as (for example
`proteinGroups.txt`, `mzTab 1.0 rc5` or `peptide`), and the result records the
report format and the version you give. The result tab shows both.

Recent DIA-NN releases write the main report as Apache Parquet. The importer
reads the tab-separated form only and refuses a Parquet file, saying so:
convert the report to TSV with its column names unchanged, and keep the Parquet
original with the dataset's files. FragPipe spectrum-match and ion tables are a
different identification level and are refused. DIA-NN group quantities come from target precursor rows. mzTab
calculation errors are shown as NaN, distinct from missing values and numeric
zero. MaxQuant uses zero for unquantified Intensity, LFQ intensity and iBAQ
values. Iki displays those values as missing and retains the original reports.

The result keeps the original quantity column names and search-engine scores,
pins the dataset revision and records the raw-file references. The action
stages the selected reports and writes their normalized rows to the
`identifications.jsonl` run artifact. On the result tab, mzTab PSMs are counted
once per PSM ID per report. Peptides are counted by sequence and modifications.
The preview combines accessions for these repeated identifications, while the
artifact keeps each reported observation. Counts show target identifications
and separate totals for flagged evidence. The preview shows decoy,
contaminant and site-only flags when present.

Support for an engine's export uses its files. Deployments that run an
external search tool supply the tool and licence in their own environment.

A project's Admin chooses how the project's new search results are reviewed with the
`iki/proteomics.search_review` starter, in Project settings → Starters, and picks who holds each
shared lab role it asks for (`iki/lab_roles`). An Org Admin may set the
deployment's default in Admin → Review procedures. Until one of them chooses,
search results can't be recorded. Each result is pinned to the procedure it was
recorded under, so a change applies to new search results only
([ADR 0302](../../docs/adr/0302-a-project-chooses-each-types-review-procedure-and-the-server-pins-it.md)).
The flows are core's result review (`iki/core.result_review@1`):

| Flow                     | What a result needs                                                                                                                   | Lab roles                |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| **Research** (suggested) | A reviewer who didn't create or edit it signs it off.                                                                                 | Reviewer                 |
| **Regulated QC**         | An analyst signs the submission, a reviewer independent of the creator, submitter and editors signs the review, and QA signs release. | Analyst, reviewer and QA |
| **Record only**          | Nothing: the result is kept as evidence and offers no sign-off.                                                                       | None                     |

To review a result recorded under Record only, import it again once the project
chooses a flow with review.

## Public example

The bundled excerpt contains the first two protein rows and first two
peptide-spectrum matches from the [HUPO-PSI example export for PRIDE 16649](https://github.com/HUPO-PSI/mzTab/blob/master/examples/1_0-Proteomics-Release/PRIDE_Exp_Complete_Ac_16649.xml-mztab.txt).
The [published study](https://doi.org/10.1074/mcp.M110.005413) lists the legacy
PRIDE accessions where it deposited its spectra. The fixture keeps the
protein assay quantities and reported identifiers exactly as exported.
The native table tests for the other engines use labelled synthetic rows.
