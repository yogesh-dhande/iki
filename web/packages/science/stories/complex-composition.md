# Complex composition

Import `@ikisci/science/complex-composition` to register `<iki-complex-composition>`.
Assign a `ComplexCompositionData` object to `.data`.

| Property                                | Meaning                                                                                                                                                                                                                                                                                                         |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `parts`                                 | Array of `{ name, role?, copies? }`. Names and roles are strings. Copies default to one.                                                                                                                                                                                                                        |
| `chainCount`                            | Optional total number of chains in the complex.                                                                                                                                                                                                                                                                 |
| `bonds`                                 | Optional array of `{ kind, from, to }`. Each endpoint is `{ part, copy?, residue }`, with a human-readable part name, 1-based copy (default one) and residue number. The kind `"disulfide"` contributes to the bonded mass. Other kinds appear in the bond table.                                               |
| `reduced`                               | The composition of all parts together with thiols free, or null when unknown. The object has `formula` (neutral Hill formula), `average` and `monoisotopic` (masses in u), and `extinctionCoefficient` (ε280 in L/(mol·cm)). A numeric field can be null when unavailable, including for non-protein absorbers. |
| `partDisulfides`                        | Optional count of additional bonds within parts, across all copies. Zero means accounted for and none present. Null or absent means unknown.                                                                                                                                                                    |
| `bondMap`, `partBondsComplete`          | Coverage of the reported bonds. Map values are `"complete"`, `"incomplete"` and `"predominant"`. Setting `partBondsComplete: false` marks the counts within parts as incomplete.                                                                                                                                |
| `emptyMessage`, `uncountedPartsMessage` | Optional plain-text descriptions of missing composition or uncounted bonds within parts.                                                                                                                                                                                                                        |
| `canCompute`                            | Shows the compute button while the reduced composition is absent. The host can handle its request.                                                                                                                                                                                                              |

Activating the button emits `compute-request` with no payload. Toggling the bonds
table emits `details-change` with `{ section: "bonds", open: boolean }`.
Both events bubble and cross shadow roots. [Runnable example](complex-composition.html)

## Masses and absorbance

`@ikisci/science/composition-mass` exports pure functions that also run on the server.

- `withDisulfides(reduced, count)` returns a new `CompositionMass` in the same format as `reduced`. Each bond removes two ordinary hydrogens and adds one cystine contribution to ε280. D, T and bracketed isotope labels remain intact. Supply a nonnegative integer count and a neutral formula in Hill notation, with each atom type occurring once. An invalid count, formula or shortage of hydrogens for the specified bonds raises `RangeError`.
- `absorbanceAtOneGramPerLitre(composition)` gives A280 at 1 g/L over 1 cm. It returns null for an unavailable coefficient or missing or nonpositive average mass.
- `formulaAtoms(formula)` returns ordered `{ symbol, count }` tokens including isotope labels such as `[13C]`. The grammar accepts element symbols, D, T and bracketed isotopes with positive integer counts. Invalid syntax raises `RangeError`.

The calculations use the conventional H weight used in the source composition table, the mass of ¹H and the measured cystine contribution for folded proteins in water.
[CIAAW hydrogen weight](https://doi.org/10.1351/pac198355071101),
[NIST hydrogen isotope masses](https://physics.nist.gov/cgi-bin/Compositions/stand_alone.pl?ele=H),
[Pace et al.](https://doi.org/10.1002/pro.5560041120)
