# Firefly luciferase inhibition by apigenin and daidzein

Tox21 measured these flavonoids against purified _Photinus pyralis_ luciferase. The starter uses the deposited signed percent activity. The source normalized vehicle to 0% and its inhibition control to −100%. Iki fits the deposited values directly. The target protein record uses sequence version 1 of UniProt P08659. [PubChem BioAssay 1224835, revision 1](https://pubchem.ncbi.nlm.nih.gov/bioassay/1224835). [Tox21 analysis and normalization](https://doi.org/10.1038/s41598-020-60747-3). [UniProt P08659](https://www.uniprot.org/uniprotkb/P08659/entry).

## The example plate

This 96-well teaching layout rearranges measurements from the source screen. Rows A–C hold apigenin and D–F hold daidzein. Each row is one deposited replicate. Columns 1–11 use the eleven highest concentrations shared by these series. In µM they are 0.017, 0.038, 0.085, 0.191, 0.435, 1.330, 4.074, 10.46, 23.64, 52.95 and 115.2. Column 12 alternates three measured replicates of the active comparator ataluren (SID 170466862 in A, C and E) and the inactive comparator caffeine (SID 144208883 in B, D and F), both at 0.435 µM. These control wells come from compound titrations in the deposit. [Example CSV](../tests/data/aid-1224835-activity.csv).

| Compound | PubChem CID | Tested SID | Deposited IC50s, replicates 1–3 (µM) | Iki pooled IC50 (95% interval, µM) |
| -------- | ----------: | ---------: | ------------------------------------ | ---------------------------------- |
| Apigenin |     5280443 |  144208740 | 10.1654, 12.7975, 8.0746             | 10.21 (8.55–12.19)                 |
| Daidzein |     5281708 |  144208642 | 24.9383, 19.8092, 14.0238            | 20.45 (15.60–26.81)                |

The deposit reports a potency for each replicate. The study summarizes replicate IC50s by their arithmetic mean. Iki fits one unweighted four-parameter curve to all 33 individual activity values for each compound. Both pooled midpoints fall within 5% of the respective source means and their approximate 95% fit intervals include those means. No observations are excluded from the selected eleven concentrations. [Source analysis](https://doi.org/10.1038/s41598-020-60747-3).

## Provenance and reuse

The NIH/National Center for Advancing Translational Sciences Tox21 measurements are U.S. government data in the public domain. To verify a well, download the four SID rows and read the column named `Activity at <concentration> uM-Replicate_<number>`. The source IC50s above are the `Potency-Replicate_<number>` fields. The compound structures and identifiers are from PubChem. The separately attributed UniProt sequence is available under CC BY 4.0. [Deposited CSV for these four SIDs](https://pubchem.ncbi.nlm.nih.gov/rest/pug/assay/aid/1224835/CSV?sid=144208740,144208642,170466862,144208883). [NCBI public-domain policy](https://www.ncbi.nlm.nih.gov/home/about/policies/). [UniProt terms](https://www.uniprot.org/help/license).
