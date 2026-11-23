# Map thumbnail

Import `@ikisci/science/plasmid-thumbnail` to register `<iki-plasmid-thumbnail>`. Set its `data` property to a `ThumbnailData` object with a string `title`, positive integer `length`, boolean `circular`, `alphabet` of `dna`, `rna` or `protein`, and a `features` array. Features use the same one-based inclusive coordinates, names and types as the sequence viewer.

The image describes the first six features by name and counts the remainder. Supply an optional `openLabel` to add a labeled button below the image. Activation dispatches a bubbling, composed `thumbnail-open` event. The host decides how to display the full view.

[Run the thumbnail example](plasmid-thumbnail.html).
