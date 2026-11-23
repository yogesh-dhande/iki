# Molecular masses and absorbance

These plain functions run in Node.js and browsers. They return unrounded values.
A molecule's average and monoisotopic masses are in unified atomic mass units.
Molar extinction coefficients are in L/(mol·cm).

```js
import {
  molecularMass,
  dnaProperties,
  proteinProperties,
  strandExtinction260,
} from "@ikisci/science/molecular-properties";

molecularMass("D2O"); // Water with both hydrogen atoms labelled.
proteinProperties("GG"); // Glycylglycine, C4H8N2O3.
dnaProperties("A"); // dAMP, with a 5′ phosphate.
strandExtinction260("ACU", "RNA"); // Synthetic trimer, ε260 = 30,000.
```

| Function                                                     | Input and output                                                                                                                                                                                                                                                                                                                                                                                                                     |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `molecularMass(formula)`                                     | A neutral sum formula listing each symbol once, such as `C2H6O` or `C4[13C]5H12N2O6`. Returns `{ average, monoisotopic }`. `D` and `T` name hydrogen-2 and hydrogen-3. An unknown atom, unsupported isotope, charge or malformed count raises `RangeError`.                                                                                                                                                                          |
| `proteinProperties(sequence, disulfides = 0)`                | One peptide chain with free termini. Returns `{ formula, average, monoisotopic, extinctionCoefficient }`. An observed disulfide removes two hydrogens and adds one cystine to ε280. Uppercase and lowercase one-letter codes are accepted, including J (I/L), U and O. Terminal stop symbols are stripped. Ambiguous identities raise `RangeError`. U and O return null for ε280. The sequence alone describes the unmodified chain. |
| `dnaProperties(sequence, { circular, doubleStranded } = {})` | A, C, G and T, upper or lowercase. Returns `{ formula, average, monoisotopic }`. Defaults to one linear strand with a 5′ phosphate and 3′ hydroxyl. Circular strands have a phosphodiester bond across the origin. Set `doubleStranded` to include the complementary strand.                                                                                                                                                         |
| `strandExtinction260(sequence, kind)`                        | Natural, linear single-stranded `"DNA"` or `"RNA"`. The empty sequence gives zero. Ambiguous, modified and mixed DNA/RNA bases fall outside this model.                                                                                                                                                                                                                                                                              |

Masses follow the complex plugin’s atomic weights from Biopython’s IUPACData
table. Fixed isotopes contribute the same nuclide mass to both sums. The
residue composition assumes condensation of the free monomers into a chain.
[NIST isotopic compositions](https://physics.nist.gov/PhysRefData/Compositions/),
[wwPDB Chemical Component Dictionary](https://www.wwpdb.org/data/ccd).

Protein ε280 uses the contributions of tryptophan, tyrosine and cystine in a
folded protein in water. Single-strand ε260 sums adjacent nucleotide doublets
and subtracts internal nucleotides once. [Pace et al.](https://doi.org/10.1002/pro.5560041120),
[Cantor et al.](https://doi.org/10.1002/bip.1970.360090909),
[Puglisi & Tinoco](<https://doi.org/10.1016/0076-6879(89)80108-9>).
