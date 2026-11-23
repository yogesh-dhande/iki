# Oligo chemistry

Import `@ikisci/science/oligo-chemistry` to register `<iki-oligo-chemistry>`.
Assign an `OligoChemistryData` object to the element's `.data` property.

| Property       | Meaning                                                                                                                                                                                                                                                                          |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `nucleotides`  | Array ordered 5′ to 3′. Each entry has a 1-based `position`, an optional `base` (natural analogue, such as `"C"`), and optional `nucleobase`, `sugar`, `linker` and `others` (array). Absent base, nucleobase, sugar or linker can be `null`. The linker follows its nucleotide. |
| `ends`         | Optional object with `"5′"` and/or `"3′"` keys. Each value is an ordered array of monomers attached at that end.                                                                                                                                                                 |
| `emptyMessage` | Optional plain text shown when the nucleotide array is empty.                                                                                                                                                                                                                    |

Each monomer has a `symbol` and a human-readable `name` (strings), and may set
`modified: true`. Screen readers receive the same position and modification
information as the diagram. The `details-change` event bubbles through the shadow
root when the positions table opens or closes. Its detail is
`{ section: "positions", open: boolean }`. Assign a new `.data` value to redraw.
[Runnable example](oligo-chemistry.html)
