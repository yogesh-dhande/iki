# The SnapGene files the DNA plugin's import tests read

Six `.dna` files, each written by SnapGene itself, from Biopython's test suite
(`Tests/SnapGene/` in the Biopython 1.88 source distribution). SnapGene doesn't publish
its file format, so files SnapGene wrote are what a reader of it is checked against;
`server/tests/test_dna_snapgene.py` reads each with the plugin's import and with
Biopython's own reader (`Bio.SeqIO`, format `snapgene`), and checks that the two agree.

They're copied unmodified from
<https://files.pythonhosted.org/packages/f6/a0/cf657d076ec56a5f9e5c29a560c1f97b8eb6ae6608dc8bc95b4e2437f129/biopython-1.88.tar.gz>
(SHA-256 `9aaa31c0bda4d059f7b2ee00bfdb5cbb73ade3057aa4b737a7cc0187091d071a`, the hash
`uv.lock` pins Biopython 1.88 by). They have no headers of their own, so they're under
the Biopython License Agreement, as Biopython's files are unless they say otherwise; its
text is in `BIOPYTHON-LICENSE.rst`, and the repository's `NOTICE` names them.

| File                              | Made by, and with which SnapGene file version | What the tests read in it                                                                                                                                                    |
| --------------------------------- | --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sample-d.dna`                    | Damien Goutte-Gattat, 2019 (14.16)            | A linear, double-stranded 1,000 bp sequence of random bases; features with and without a direction, a qualifier with two values, a name that differs from its label          |
| `sample-e.dna`                    | Damien Goutte-Gattat, 2019 (13.12)            | The same as a circle, marked Dam, Dcm and EcoKI methylated; a reverse-strand feature                                                                                         |
| `sample-f.dna`                    | Damien Goutte-Gattat, 2019 and 2023 (15.19)   | Features split into segments with gaps between, forward and reverse, some segments named; a primer and its binding site                                                      |
| `sample-hybridization-params.dna` | A Biopython contributor, 2025 (15.19)         | A _Schizosaccharomyces japonicus_ gene (SJAG_03543) with 1 kb either side; two primers with 5′ tails, one also binding weakly below the file's hybridization settings; a CDS |
| `looped_feature.dna`              | A Biopython contributor, 2026 (15.20)         | A 10 bp circle with a feature from base 3 round the origin to base 2, and SnapGene's history of how it was made                                                              |
| `linebreak_in_qualifier_text.dna` | A Biopython contributor, 2025 (15.19)         | RP4's origin of transfer, a qualifier of rich text with line breaks and comments, and primers with descriptions                                                              |

The version is the second and third numbers of the file's first packet, which other
readers of the format take to be the format versions SnapGene wrote it in and can read it
from.

Biopython's tests have two more SnapGene files that aren't copied: `pFA-KanMX4.dna`,
which looks to be a map from SnapGene's own plasmid collection, whose terms of use aren't
Biopython's, and `looped_feature_origin.dna`, which tests nothing `looped_feature.dna`
doesn't.
