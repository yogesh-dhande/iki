# Open reading frames

`openReadingFrames(sequence, options?)` annotates complete start-to-stop
intervals on both strands using an NCBI genetic code. It runs in a browser or
Node.js. Positions count from one on the forward reference. A reverse ORF reads
from its reported end towards its start.

```js
import { openReadingFrames } from "@ikisci/science/open-reading-frames";

// Synthetic fragment with one short forward ORF.
const [orf] = openReadingFrames("ATGAAATAA", { minimumCodons: 1 });
// orf.start === 1, orf.end === 9, orf.translation === "MK"
```

| Option              | Default | Meaning                                                                                       |
| ------------------- | ------- | --------------------------------------------------------------------------------------------- |
| `table`             | `1`     | NCBI genetic code number. Codes with context-dependent stops (27, 28, 31) raise `RangeError`. |
| `alternativeStarts` | `false` | Allow the selected table's annotated start codons. The default recognizes ATG.                |
| `minimumCodons`     | `75`    | Minimum sense codon count, including the start and excluding the stop.                        |
| `circular`          | `false` | Allow an ORF to cross the origin. Its complete interval spans at most one lap.                |

Each result supplies numeric `start`, `end`, `length` (bp, including the stop),
`amino_acids`, `frame` (±1 to ±3), a `strand` of `"forward"` or `"reverse"`,
the `start_codon` and `translation`. Results are ordered by reference coordinates.
An end below the start crosses the origin. Only the longest ORF terminating at
each stop in a reading frame is reported. IUPAC ambiguities decode as X within
the ORF and cannot serve as start or stop signals. [NCBI genetic codes](https://www.ncbi.nlm.nih.gov/Taxonomy/Utils/wprintgc.cgi),
[NCBI ORFfinder](https://www.ncbi.nlm.nih.gov/orffinder/).
