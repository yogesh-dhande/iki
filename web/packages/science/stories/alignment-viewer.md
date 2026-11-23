# Alignment viewer

Import `@ikisci/science/alignment-viewer` and assign an `AlignmentData` object to
the `.data` property of `<iki-alignment-viewer>`. The element draws the visible
rows and columns on a canvas. Its keyboard cursor describes the selected residue,
its number in the sequence, and agreement with the consensus. A reference row
remains pinned when scrolling. N and Shift+N step between differing columns.

| Property       | Data                                                                                                                                                                                                                  |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `title`        | Accessible name for the alignment.                                                                                                                                                                                    |
| `alignment`    | The `kind: "alignment"` result returned by the FASTA parser, or the equivalent plain data described below.                                                                                                            |
| `referenceRow` | Optional one-based file row number.                                                                                                                                                                                   |
| `position`     | Optional starting `{ row, column }`, both one-based in the original file.                                                                                                                                             |
| `labels`       | Optional array indexed in file order. A null entry keeps the FASTA identifier. A label supplies `text`, optional `description`, a relative or HTTP(S) `href`, and an optional `selectionKey` that enables a checkbox. |

An alignment supplies `rows`, `length` in columns, `alphabet` (`"nucleotide"` or
`"protein"`), and `alphabet_guessed`. All rows have the same aligned length. A row
has an `id`, `description`, `aligned` string (`-` for a gap, `.` for unobserved),
`residues` excluding gaps, one-based `start`, `spanned` indicating an explicit
coordinate range, and `reverse` indicating descending residue numbers.
`conservation` contains a Clustal mark per column and `consensus` its consensus
residue. `agreement` is an array of consensus percentages. `identical` and `gapped`
count identical columns and columns with gaps. The viewer accepts up to 1,000,000 columns.

The element emits bubbling, composed DOM events.

| Event              | `CustomEvent.detail`                                                                            |
| ------------------ | ----------------------------------------------------------------------------------------------- |
| `position-change`  | `{ row, column }` in one-based file coordinates. Also available as `.position`.                 |
| `selection-change` | An array of selected row keys. Set `.selection` to change the selection programmatically.       |
| `row-open`         | `{ row, href }` for an unmodified row-link click. Call `preventDefault()` to handle navigation. |

Replacing `.data` starts a new view with the specified initial position and clears the
selection. `@ikisci/science/alignment-compare` exports the pure `compared(rows, reference,
alphabet)` summary. Pass the reference row from the same array. The result contains its
residue count, the count covered by another row, the number of differing columns and
their zero-based indices. A comparison above the exported `MAX_COMPARED` limit returns
`null`. [Runnable alignment story](alignment-viewer.html)
