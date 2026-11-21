# The GenBank records the export tests read

Two INSDC records, as NCBI's E-utilities give them in GenBank format with their features.
NCBI places no restrictions on the use or distribution of GenBank's data. With pUC19
(`server/tests/data/plasmids/L09137.2.gb`), they're what
`server/tests/test_sequence_files.py` imports with the DNA plugin and writes back out as
GenBank (ADR 0169), checking the file Iki writes against NCBI's, as Biopython reads both,
and that it imports back into the same record.

| File             | What it is                                                                                                                                                                 | Length    |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| `J02459.1.gb`    | Escherichia phage lambda, wild type: a linear genome, whose features include partial ends on one base (`complement(<23231)`, `21737..>21737`), which INSDC writes two ways | 48,502 bp |
| `NC_012920.1.gb` | _Homo sapiens_ mitochondrion, the revised Cambridge Reference Sequence: a circle whose D-loop is written across its origin (`complement(join(16024..16569,1..576))`)       | 16,569 bp |

Fetched with, for each accession:

```sh
curl "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore\
&id=J02459.1&rettype=gbwithparts&retmode=text&tool=iki-dna&email=<yours>"
```
