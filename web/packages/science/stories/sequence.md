# Sequence functions

Import the functions you need from `@ikisci/science/sequence`. Feature starts and
ends are one-based and inclusive. An end before the start describes a span over
the origin. `readFeatures(values, length)` parses an array of feature objects
into `Feature[]`. Each accepts `start`, `end`, `type`, `name`, `strand` and
optional ordered `parts` and `qualifiers`. `readTrack(values, length)`
accepts one finite number or missing value per residue.

The sequence functions run synchronously. The separate
`@ikisci/science/oligo-temperature` entry and `meltingTemperature(bases)` from
`@ikisci/science/sequence` give the same one-decimal temperature in degrees Celsius
for a supplied unmodified DNA strand and its exact complement. The fixed reference
conditions are 50 mM sodium and 50 nM total strand concentration. Supply ASCII
A/C/G/T in either case with at least two bases. Other input returns `null`, as does
input beyond the shared 64 MiB limit. The browser uses JavaScript generated from
the DNA record’s Rust model, so it can run synchronously under a strict page CSP.
[Model and thermodynamic parameters](https://doi.org/10.1073/pnas.95.4.1460)

For example,
`reverseComplement("ACGA")` gives `"TCGT"` and `gcContent("ACGA")` gives
50 for this synthetic sequence. The generated declarations describe the
feature, track and selection types.
