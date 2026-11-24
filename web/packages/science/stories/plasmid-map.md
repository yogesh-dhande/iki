# Plasmid map

Import `@ikisci/science/plasmid-map` to register `<iki-plasmid-map>`. Assign a `PlasmidMapData` object to `data`. It takes a display `name`, a positive integer `length` in base pairs, and a `topology` of `circular` or `linear`. Every coordinate starts at one and includes both ends. On a circle an end before the start spans the origin. The map displays supplied annotations.

| Optional layer | Properties of each item                                                                                                                                     |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `parts`        | `start`, `end`, `type`, `name`, optional `strand`, and optional `qualifiers` with `role`, `label` or `identity` strings                                     |
| `orfs`         | `start`, `end`, `frame`, `length` in bases, `amino_acids` count, optional `strand`, `start_codon` and `translation`                                         |
| `sites`        | `start`, `end`, `primer` ID, `name`, `three_prime` position, `annealed` and `tail` lengths, optional `strand`, `melting_temperature` in °C and `mismatches` |

Each layer has an `items` array and optional `source` text. An empty array represents a completed annotation with zero hits. Omit a layer if its annotations are unavailable. Each mismatch has `primer` and `template` residue letters and integer `from_three_prime`. A `strand` may be `forward`, `reverse` or `none`.

The `linkedPrimerIds` array identifies primers the host can open. An activated primer dispatches `primer-open` with `{ id }`. Switching between the circular and linear views dispatches `view-change` with `{ view: "ring" | "line" }`. Toggling a layer dispatches `layer-change` with `{ key: "parts" | "orfs" | "primers", shown: boolean }`. For an unannotated map, supply optional `emptyMessage` and `emptyAction` strings to show a status and an action button. It dispatches `empty-action`. All events bubble and are composed.

[Run the circular and linear example](plasmid-map.html).
