# The GenBank records the export tests and the sequence viewer's tests read

INSDC records, as NCBI's E-utilities give them in GenBank format with their features.
NCBI places no restrictions on the use or distribution of GenBank's data. With pUC19
(`server/tests/data/plasmids/L09137.2.gb`), they're what
`server/tests/test_sequence_files.py` imports with the DNA plugin and writes back out as
GenBank (ADR 0171), checking the file Iki writes against NCBI's, as Biopython reads both,
and that it imports back into the same record. The sequence viewer's tests
(`web/app/src/genetic-code.test.ts`, and `server/tests/test_browser.py`) translate every
coding sequence in them, and check each against the record's own `/translation`.

| File             | What it is                                                                                                                                                                 | Length    |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| `J02459.1.gb`    | Escherichia phage lambda, wild type: a linear genome, whose features include partial ends on one base (`complement(<23231)`, `21737..>21737`), which INSDC writes two ways | 48,502 bp |
| `NC_012920.1.gb` | _Homo sapiens_ mitochondrion, the revised Cambridge Reference Sequence: a circle whose D-loop is written across its origin (`complement(join(16024..16569,1..576))`)       | 16,569 bp |
| `J00265.1.gb`    | Human insulin gene: a coding sequence joined across an intron (`join(2424..2610,3397..3542)`)                                                                              | 4,044 bp  |
| `D10085.1.gb`    | Mouse complement component C4, exons 13 and 14: a join partial at both ends (`join(<1..105,972..>1035)`)                                                                   | 1,035 bp  |
| `PZ465884.1.gb`  | _Pteropus livingstonii_ ND1, partial: read from its third base (`/codon_start=3`) by the vertebrate mitochondrial code (`/transl_table=2`)                                 | 451 bp    |
| `PZ465879.1.gb`  | _Flammulina finlandica_ RPB2, partial: on the reverse strand (`complement(<1..>600)`)                                                                                      | 600 bp    |
| `PP826089.1.gb`  | _Forficula dentata_ COX1, partial: with ambiguous bases (Y, S and N), by the invertebrate mitochondrial code (`/transl_table=5`) from its second base                      | 489 bp    |

Fetched with, for each accession:

```sh
curl "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore\
&id=J02459.1&rettype=gbwithparts&retmode=text&tool=iki-dna&email=<yours>"
```
