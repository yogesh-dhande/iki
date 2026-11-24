# Proteomics

Register each instrument file in its approved versioned source bucket. Create a
**Raw mass spectrometry file** record that names the registered file and, if
known, its sample. Add these records to a **Proteomics dataset**. For a public
dataset you can also give its archive page. Existing Iki file registration pins
object versions and checks SHA-256.

Run **Import proteomics search reports** with the dataset, the format and
software version, and one or more original reports from that tool. Optional
protein inputs must have distinct UniProt accessions. The importer links
reported exact accession matches, including group members. For UniProt FASTA
headers it reads the accession between the pipes. It reports selected
accessions with no matching hit, and keeps the original IDs for unmapped
proteins.

| Format                                                                                                                                                    | Supported reports                                                                       | Reported quantities retained                                            |
| --------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| [HUPO-PSI mzTab](https://github.com/HUPO-PSI/mzTab)                                                                                                       | Proteomics 1.0.0 and the legacy PRIDE 1.0 rc5 export, protein, peptide and PSM sections | Protein and peptide assay or study-variable abundance                   |
| [MaxQuant](https://massive.ucsd.edu/ProteoSAFe/DownloadResultFile?file=f.MSV000088694%2Fquant%2FMaxquant%2FBulkSet%2Ftxt%2Ftables.pdf&forceDownload=true) | `proteinGroups.txt`, `peptides.txt`, and `evidence.txt`                                 | Intensities, LFQ, iBAQ and ratios                                       |
| [DIA-NN](https://github.com/vdemichev/DiaNN)                                                                                                              | Main tab-separated report with run, group and precursor columns                         | Precursor quantities and one protein-group quantity per run and channel |
| [FragPipe](https://fragpipe.nesvilab.org/docs/tutorial_fragpipe_outputs.html)                                                                             | Protein and peptide reports with a Protein ID column, including the combined reports    | Intensity, MaxLFQ and spectral-count columns                            |

FragPipe spectrum-match and ion tables are a different identification level and
are refused. DIA-NN group quantities come from target precursor rows. mzTab
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

Set `review_flow` to Research for one signed review, or Regulated QC for
signed submission, independent review and QA release. Assign the analyst,
reviewer and QA project roles as applicable.

## Public example

The bundled excerpt contains the first two protein rows and first two
peptide-spectrum matches from the [HUPO-PSI example export for PRIDE 16649](https://github.com/HUPO-PSI/mzTab/blob/master/examples/1_0-Proteomics-Release/PRIDE_Exp_Complete_Ac_16649.xml-mztab.txt).
The [published study](https://doi.org/10.1074/mcp.M110.005413) lists the legacy
PRIDE accessions where it deposited its spectra. The fixture keeps the
protein assay quantities and reported identifiers exactly as exported.
The native table tests for the other engines use labelled synthetic rows.
