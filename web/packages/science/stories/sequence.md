# Sequence functions

Import the functions you need from `@ikisci/science/sequence`. Feature starts and
ends are one-based and inclusive. An end before the start describes a span over
the origin. `readFeatures(values, length)` parses an array of feature objects
into `Feature[]`. Each accepts `start`, `end`, `type`, `name`, `strand` and
optional ordered `parts` and `qualifiers`. `readTrack(values, length)`
accepts one finite number or missing value per residue.

The sequence functions run synchronously without WebAssembly. For example,
`reverseComplement("ACGA")` gives `"TCGT"` and `gcContent("ACGA")` gives
50 for this synthetic sequence. The generated declarations describe the
feature, track and selection types.
