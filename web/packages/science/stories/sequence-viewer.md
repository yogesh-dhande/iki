# Sequence viewer

Import `@ikisci/science/sequence-viewer` to register `<iki-sequence-viewer>`.
Assign a new `SequenceData` to its `data` property when switching sequences.

| Property              | Input                                                                                                                  |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `sequence`, `title`   | Residue string and display name                                                                                        |
| `alphabet`            | `dna`, `rna` or `protein`                                                                                              |
| `label`               | Optional caption for the sequence, e.g. “Bases”                                                                        |
| `strands`, `topology` | Optional `single` or `double` and `linear` or `circular`                                                               |
| `features`            | Optional array of annotations with `type`, `start` and `end`, plus optional `name` and `strand`                        |
| `library`             | Optional supplied annotations in `features`, in the same format as recorded features, and an optional `version` string |
| `tracks`              | Optional numeric tracks with `id`, `label` and `values`, plus optional `min` and `max`                                 |
| `feature`             | Optional initially shown feature, identified by `type`, `start`, `end` and optional `name`                             |

Coordinates are one-based and inclusive. An end before the start crosses the origin. A feature can carry string `qualifiers` and a list of `parts` in reading order, each with its own start, end, strand and optional `between`, `partial_start`, or `partial_end` flags. The optional `operator` is `join` or `order`. Provide one numeric track value per displayed position, using null for a missing observation and omitting trailing stop symbols in a protein. The table describes invalid or missing tracks.

Omit `library.features` before a search, or pass an empty array when the search found no matches. Supplied results appear in their own table with the source from `qualifiers.source`. Selecting an entry highlights it on the sequence.

A bubbling, composed `feature-change` event reports the feature being shown in `detail` or null when hidden. Read it synchronously through `shownFeature`. The `selection-change` event reports a zero-based insertion point in `detail.caret` and a one-based inclusive `detail.range` pair or null. Both events report initial state and subsequent changes.

The ES module uses React and the window virtualizer internally. Bundle it with its adjacent stylesheet when serving your page. Keyboard controls, the feature table and track readouts are available in the [runnable example](sequence-viewer.html).
