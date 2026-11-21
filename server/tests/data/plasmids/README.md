# The plasmid records the plasmid plugin's tests use

Six circular vectors and two linear molecules, as NCBI's E-utilities give their INSDC
records (GenBank format with their features, or FASTA where only the bases are needed).
NCBI places no restrictions on the use or distribution of GenBank's data.

| File             | What it is                                                                                                                                                                                     | Length    |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| `L09137.2.gb`    | Cloning vector pUC19 (Yanisch-Perron, Vieira & Messing, 1985)                                                                                                                                  | 2,686 bp  |
| `J01749.1.gb`    | Cloning vector pBR322                                                                                                                                                                          | 4,361 bp  |
| `U03450.1.gb`    | Yeast centromere vector pRS416, with the URA3 marker (Sikorski & Hieter)                                                                                                                       | 4,898 bp  |
| `MN996867.1.gb`  | Cloning vector pcDNA3.1(+)                                                                                                                                                                     | 5,428 bp  |
| `LT009457.1.gb`  | Lentiviral vector pLenti6-V5-Puro                                                                                                                                                              | 8,835 bp  |
| `OL989372.1.gb`  | Cloning vector pET-28a-ZaTdT12, a pET-28a(+) derivative                                                                                                                                        | 6,835 bp  |
| `X01095.1.gb`    | The linear killer plasmid pGKL1 of _Kluyveromyces lactis_, whose LOCUS line says `linear` and whose feature table gives the terminal inverted repeats at its two ends (Hishinuma et al., 1984) | 8,874 bp  |
| `J02459.1.fasta` | Escherichia phage lambda, wild type: a linear genome, as the bases alone, for the digests whose fragment counts a linear molecule and a circle differ in                                       | 48,502 bp |

Fetched with, for each accession (`rettype=fasta` for `J02459.1.fasta`):

```sh
curl "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=nuccore\
&id=L09137.2&rettype=gbwithparts&retmode=text&tool=iki-plasmid&email=<yours>"
```

`server/tests/test_plasmid_annotation.py` reads each record's bases and its own feature
table, and checks the plugin's annotation against them. The library the plugin annotates
from is built from four of these records and two more; see
`plugins/plasmid/data/README.md`.
