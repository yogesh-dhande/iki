# The library of vector parts

`features.json` holds the 34 common vector parts the plasmid plugin annotates a plasmid
from (`annotate_plasmid`): its origins of replication, the genes it's selected by in
bacteria and in the cells it's expressed in, its promoters and terminators, its affinity
tags, the elements a lentiviral vector is packaged by, and the sites Gateway recombines
at. `build_features.py` builds it.

[`dna_features.json`](../../../worker/crates/iki-wasm/data/dna_features.json) contains
the 11 parts in **Routine cloning-vector features 1.0**, used by **Annotate from
feature library**. It takes accession-pinned parts from the same data, with the
original feature intervals and provenance.

| Record                                                        | Included parts                                                                                                               |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| [OL989372.1](https://www.ncbi.nlm.nih.gov/nuccore/OL989372.1) | pMB1/pUC origin, Rop, lac repressor and its promoter, T7 promoter, lac operator, 6xHis tag, thrombin site, and T7 terminator |
| [FJ457000.1](https://www.ncbi.nlm.nih.gov/nuccore/FJ457000.1) | Yeast centromere CEN6 and replication origin ARSH4                                                                           |

## Where each part is from

Every part is **the bases of a feature of a public INSDC record**, taken at the
location that record's own feature table gives. Nothing here is Iki's judgement of where
a part lies, and each part names its record, so it can be checked against it:

| Record                                                        | What it is                                   | Parts taken from it                                                                                                                                                                                                                                                    |
| ------------------------------------------------------------- | -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [OL989372.1](https://www.ncbi.nlm.nih.gov/nuccore/OL989372.1) | Cloning vector pET-28a-ZaTdT12, 6,835 bp     | f1 ori, KanR (`aph(3')-Ia`), the ColE1/pMB1/pBR322/pUC ori, `rop`, `lacI` and its promoter, the T7 promoter, the lac operator, a 6xHis tag, a thrombin site, the T7 terminator                                                                                         |
| [LT009457.1](https://www.ncbi.nlm.nih.gov/nuccore/LT009457.1) | Lentiviral vector pLenti6-V5-Puro, 8,835 bp  | cPPT, the 3′ and 5′ LTRs, the SV40 poly(A) signal, the SV40 ori, AmpR (`bla`), the lac and T3 promoters, the HIV-1 packaging signal and Rev response element, the CMV promoter, attR1 and attR2, CmR (`cat`), `ccdB`, a V5 tag, the SV40 early promoter, PuroR (`pac`) |
| [J01749.1](https://www.ncbi.nlm.nih.gov/nuccore/J01749.1)     | Cloning vector pBR322, 4,361 bp              | TetR (`tet`)                                                                                                                                                                                                                                                           |
| [MN996867.1](https://www.ncbi.nlm.nih.gov/nuccore/MN996867.1) | Cloning vector pcDNA3.1(+), 5,428 bp         | NeoR/KanR (`nptII`), which selects with kanamycin in E. coli and with neomycin or G418 in mammalian cells                                                                                                                                                              |
| [K02207.1](https://www.ncbi.nlm.nih.gov/nuccore/K02207.1)     | _Saccharomyces cerevisiae_ URA3 gene         | URA3                                                                                                                                                                                                                                                                   |
| [FJ457000.1](https://www.ncbi.nlm.nih.gov/nuccore/FJ457000.1) | Yeast shuttle vector pRS416-10xGly, 4,930 bp | CEN6 and ARSH4                                                                                                                                                                                                                                                         |

Where two records annotate the same part, the one whose feature spans it fully is taken:
the 589 bp ColE1/pMB1/pBR322/pUC origin from OL989372.1, not pLenti6's 403 bp `pMB1 ori`,
and the f1 origin from OL989372.1, not pLenti6's. No two parts are one sequence, which
`build_features.py` checks.

A part's presence says something about the plasmid, which it gives in `implies`: a field
of `iki/plasmid.Plasmid` and the value to add. `NeoR/KanR` implies both a bacterial
resistance and a selection marker, since the one gene does both.

## Building it again

From this directory, with the address NCBI asks automated clients to give:

```sh
python build_features.py lab@example.com > features.json
```

It fetches each record from NCBI's E-utilities in GenBank format and checks what it
writes: every part's bases are A, C, G or T, each part's length is its location's, no two
parts are one sequence, and each part is in the record it came from. Raise `VERSION`
whenever a part changes, so each plasmid's `annotated_with` says which library found it.

Regenerate the Rust data file from the checked-in library. From the repository root,
run this command.

```sh
python plugins/plasmid/data/build_features.py --dna-subset > worker/crates/iki-wasm/data/dna_features.json
```

Update `DNA_LIBRARY_VERSION` when the selection or its source data changes.

To add a part, find a public INSDC record whose own feature table annotates it,
and add its accession and location to `PARTS`. Don't write bases in by hand: a part
nobody can check against a record is a part nobody can trust.

## Licence

The International Nucleotide Sequence Database Collaboration provides unrestricted
access to its records and permits use and redistribution without imposing licensing
requirements. NCBI likewise places no restrictions on use or distribution of GenBank
data and notes that submitters may assert independent intellectual property rights.
[INSDC data policy](https://www.insdc.org/policy/),
[GenBank data usage](https://www.ncbi.nlm.nih.gov/genbank/#genbank-data-usage)
