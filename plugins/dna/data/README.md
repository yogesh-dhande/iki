# The codon usage tables

`codon_usage.json` holds the codon usage of the nine hosts a gene is optimized for
(`optimize_codons`): 64 counts per host, with the assembly they were counted from, how
many coding sequences and how many codons went into them, and the NCBI translation table
the host's genes are read by. `build_codon_usage.py` builds it.

## Where each table is from

Every table is **counted from the RefSeq coding sequences of that organism's reference
assembly**, from the `_cds_from_genomic.fna.gz` file NCBI publishes beside the assembly.
Nothing here is copied from another database's table, and nothing is Iki's own estimate:
each count can be had again from the assembly it names.

| Host                                             | Assembly                              | Coding sequences | Codons     | Code |
| ------------------------------------------------ | ------------------------------------- | ---------------- | ---------- | ---- |
| _Escherichia coli_ K-12 substr. MG1655           | GCF_000005845.2 (ASM584v2)            | 4,286            | 1,326,622  | 11   |
| _Bacillus subtilis_ subsp. subtilis str. 168     | GCF_000009045.1 (ASM904v1)            | 4,230            | 1,232,807  | 11   |
| _Saccharomyces cerevisiae_ S288C                 | GCF_000146045.2 (R64)                 | 5,974            | 2,861,558  | 1    |
| _Komagataella phaffii_ GS115 (_Pichia pastoris_) | GCF_000027005.1 (ASM2700v1)           | 5,032            | 2,400,529  | 1    |
| _Homo sapiens_                                   | GCF_000001405.40 (GRCh38.p14)         | 20,005           | 11,681,983 | 1    |
| _Cricetulus griseus_ (CHO)                       | GCF_003668045.3 (CriGri-PICRH-1.0)    | 20,351           | 11,167,684 | 1    |
| _Spodoptera frugiperda_ (Sf9)                    | GCF_023101765.2 (AGI-APGP_CSIRO_2.0)  | 14,410           | 7,878,858  | 1    |
| _Drosophila melanogaster_                        | GCF_000001215.4 (Release 6 + ISO1 MT) | 13,892           | 7,415,301  | 1    |
| _Arabidopsis thaliana_                           | GCF_000001735.4 (TAIR10.1)            | 27,314           | 11,113,145 | 1    |

## What is counted

- A coding sequence NCBI marks `pseudo` or `partial`, or one with a translation
  exception, is left out: its codons are not a gene's codons as it is read.
- A coding sequence whose length is not a multiple of three, or which has a base other
  than A, C, G or T, is left out: its reading frame is not settled.
- **One coding sequence per gene**, the longest. A table counted transcript by transcript
  would be a table of the splicing rather than of the coding: human genes carry from one
  to over fifty annotated transcripts each, and counting every one would weight a gene by
  how much work has gone into annotating it.
- Every codon of a kept coding sequence is counted, its stop codon included, so the table
  says which stop codon an organism uses.

The counts are counts, not frequencies, so a reader can see how much evidence is behind
each one, and work out relative synonymous codon usage (Sharp, Tuohy and Mosurski,
_Nucleic Acids Res_ 14:5125, 1986) or relative adaptiveness (Sharp and Li, _Nucleic Acids
Res_ 15:1281, 1987) for themselves. `scripts/codons.py` says how each is computed.

A codon adaptation index is against the table it was computed from. Sharp and Li took
their reference set from very highly expressed genes; a genome-wide table, as these all
are, gives a different scale. Each `iki/dna.CodonOptimization` record says which table its
indexes are against (`cai_reference`), so two of Iki's numbers are compared with each other
and not with a published figure from some other reference set.

## Building it again

From this directory:

```sh
python build_codon_usage.py > codon_usage.json
```

It fetches each assembly's coding sequences from NCBI (about 60 MB in all), counts them,
and refuses to write a table with fewer than 1,000 coding sequences or 300,000 codons, or
one in which some codon never occurs: either means the wrong file. With
`--from <directory>` it reads files already fetched into that directory instead, so a
change to what is counted can be checked without fetching it all again. Raise `version`
whenever a table changes.

To add a host, add its reference assembly to `ORGANISMS`. Don't write counts in by hand: a
table nobody can check against an assembly is a table nobody can trust.

## Licence

The coding sequences are from NCBI's RefSeq assemblies above. NCBI places no restrictions
on the use or distribution of the data in its sequence databases:

> The NCBI itself places no restrictions on the use or distribution of the data contained
> therein. Nor do we accept data when the submitter has requested restrictions on reuse
> or redistribution.

(NCBI, [GenBank and WGS Submission Guidelines](https://www.ncbi.nlm.nih.gov/genbank/).)

Codon usage tables whose terms don't allow this were left out, among them the Kazusa Codon
Usage Database's: a lab that wants another table can count one from its own reference set
with this script.
