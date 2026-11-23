# Oligo duplex

Import `@ikisci/science/oligo-duplex` to register `<iki-oligo-duplex>`. Assign
`OligoDuplexData` or `null` to `.data`. Both `sense` and `antisense` are strings
written in their own 5′ to 3′ direction. The display reverses the bottom strand.

Supply one entry in `pairs` per aligned column, including mismatches. Each is
`{ sense: number, antisense: number, paired: boolean }`. Both coordinates are
1-based. Consecutive entries advance the sense coordinate by one and decrease the
antisense coordinate by one. A true value draws a bar for a Watson–Crick pair.
A false value draws a dot. The reader can inspect overhangs, both original strands
and their inclusive coordinate ranges.

An empty array describes strands that do not pair. Null data shows a pending
state. The optional `empty-message` attribute customizes the pending message.
The `details-change` event bubbles through the shadow root when the strand
details open or close. Its detail is `{ section: "strands", open: boolean }`.
[Runnable example](oligo-duplex.html)
