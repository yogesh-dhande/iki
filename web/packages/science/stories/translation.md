# Reading-frame and annotated feature translation

Import `translateFrame`, `translateFeature`, and `proteinOf` from
`@ikisci/science/translation`. These functions run synchronously on strings
and plain data using the NCBI genetic codes shipped with the entry.

```js
import { proteinOf, translateFrame } from "@ikisci/science/translation";

// Synthetic example. Read frame +1 under NCBI's standard genetic code.
const codons = translateFrame("ATGGCTTAA", 1, 1);
console.log(proteinOf(codons)); // MA
```

`translateFrame(sequence, frame, table)` accepts frames 1 through 3 on either
strand, with a negative number selecting the reverse strand. Supply an NCBI
genetic-code number listed in `GENETIC_CODES`. Each output codon contains
the original one-based base positions in reading order, its letters, its residue
and its one-based position in the translation. The result has only whole codons.
An unknown table yields an empty array.

`translateFeature(feature, sequence)` accepts the `Feature` data returned by
`readFeatures` from `@ikisci/science/sequence`. It reads the supplied parts in
order, respects strand and partial ends, and applies the INSDC `codon_start`,
`transl_table` and `transl_except` qualifiers. It returns `{table, codons}` or
`{error}` when the table is unknown. `proteinOf(codons)` returns one-letter
amino-acid text with a terminal stop removed.

The tables and their start, stop and context-dependent codons come from
[NCBI's genetic codes](https://www.ncbi.nlm.nih.gov/Taxonomy/Utils/wprintgc.cgi).
