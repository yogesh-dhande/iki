# Reading annotated sites on a restriction map

```js
import { bindingSiteCoordinate, digestFragments } from "@ikisci/science/restriction-map";

// Synthetic 100 bp map with two recorded cut boundaries.
const pieces = digestFragments(100, [25, 90], "circular");
// [{start: 26, end: 90, length: 65, topology: "linear"},
//  {start: 91, end: 25, length: 35, topology: "linear"}]

const annotated = { start: 91, end: 5, strand: "reverse" };
const terminal = bindingSiteCoordinate(annotated, 1, 100, "circular"); // 91
```

`digestFragments(length, cutBoundaries, topology)` takes known **top-strand**
cut positions. Boundary 0 lies before the first base, and boundary `k` after
base `k`. Repeated boundaries indicate one cut. Linear maps include both ends.
An uncut circle returns its full length with circular topology. After one cut
it gives one linear fragment. Fragment lengths ignore any complementary-strand
overhang, and sum to the molecule's length.

`bindingSiteCoordinate(site, offsetFromThreePrime, length, topology)` projects
one base of a known paired interval onto the reference map. The inputs are a
recorded `{ start, end, strand }` and the base's offset from its 3′ end,
counting the terminal paired base as 1. The strand is `"forward"` or `"reverse"`.
Offsets outside the interval raise `RangeError`.

Both functions accept `"circular"` and `"linear"` topology. Returned
coordinates are one-based and inclusive. A decreasing interval crosses a
circular origin. Length and positions must be integral, and the sequence
itself is optional data for the caller. [INSDC feature locations](https://www.insdc.org/submitting-standards/feature-table/).
