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

The result preserves identification levels and the original quantity column
names. Missing values stay separate from zero. Imported scores and
annotations remain evidence from the search engine. Iki keeps the report
files and hashes, pins the dataset revision, and records references to the
raw files. The action stages the reports alone. It writes all imported rows
to the `identifications.jsonl` run artifact. The result tab previews the
first twelve rows and the counts by identification level.

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
