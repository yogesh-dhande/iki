### 3D structures

Import `@ikisci/science/structure-viewer` to register `<iki-structure-viewer>`.
Assign `data = { structures, camera? }`. Each structure has a `label`, its `format`
(`"pdb"` or `"mmcif"`), and `structure` containing coordinate text or its UTF-8
`ArrayBuffer`. The element accepts several structures in the same scene. An empty
array or `null` clears the scene. Set the element's CSS height to size the viewer.

Optional per-structure fields are `colour` (a six-digit hex CSS colour), `hidden`,
`transform` (a column-major array of 16 numbers), and `selection` (residue ranges).
Each range has `chain`, inclusive integer `start` and `end` in the author's numbering
and optional `start_insertion` and `end_insertion` strings. The camera has three XYZ
vectors named `position`, `target` and `up`, with optional `fov` (radians), `radius`
and `mode` (`"perspective"` or `"orthographic"`).

Events bubble and cross shadow boundaries. `structure-ready` announces that the scene
can be controlled. `structure-unsupported` carries `"webgl"` and `structure-error`
carries a message string. `structure-selection` carries one array of residue ranges
per input structure. `getState()` returns the camera and each structure's current
transform, visibility and selection. It returns `null` before the scene is ready.
`setHidden(index, hidden)`, `await setColour(index, hexOrNull)` and
`await superpose(ontoIndex)` take zero-based indices. Superposition returns the RMSD
in ångströms and fitted residue count (or a failure message) for each moving structure.
[Open the crambin example](structure-viewer.html).
