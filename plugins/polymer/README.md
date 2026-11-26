# Synthetic polymers

`iki/polymer.Polymer` registers a polymer design by its BigSMILES
representation and curated chemistry. A design can describe an ensemble of
chains. Each physical sample refers back to that design and carries its own
molar-mass measurements.

## Register a polymer

1. Register the precursor monomers as `iki/small_molecule.Compound` records.
   A lab-specific monomer uses the same compound registration as a catalog
   monomer. For styrene, use `C=Cc1ccccc1` (PubChem CID 7501).
   [PubChem styrene](https://pubchem.ncbi.nlm.nih.gov/compound/7501)
2. Create a polymer with the submitted BigSMILES, its monomer references,
   architecture and sequence distribution. Enter the end groups and any
   further design restrictions, such as tacticity, when known. Retain
   stereochemistry and end-group notation from the source.
3. Open **Structure** and run `inspect_bigsmiles` to inspect the stochastic
   objects, their repeat fragments and any implicit end-group alternatives.
   The tab also identifies syntax problems and constructs the inspector cannot
   interpret. The monomer references identify the starting compounds, which
   can differ chemically from the repeat units they supply.
   [BigSMILES line notation](https://olsenlabmit.github.io/BigSMILES/docs/line_notation.html)

Use a consistent spelling across projects. The registry preserves the
submitted notation, so equivalent spellings can receive different entries.
A search finds a polymer by text its BigSMILES contains, such as `[<]CC(`:
its notation has no words to search.
[ADR 0233](../../docs/adr/0233-a-text-fields-word-index-never-fails-its-save.md)
The precursors are fixed facts of the registered design. Supersede the entry
to correct a precursor assignment. [ADR 0183](../../docs/adr/0183-a-polymer-design-and-its-molar-mass-distribution.md)

For a linear polystyrene homopolymer, `{[][<]CC(c1ccccc1)[>][]}` represents
the head-to-tail repeat. The curly braces delimit one stochastic object, and
the `[<]` and `[>]` descriptors mark complementary connection points. The
empty terminal descriptors `[]` leave the repeat fragment uncapped. This
representation makes no assignment of a physical sample's end groups. The
author may specify end groups outside the braces or list allowed end groups
after a semicolon inside the object.
[BigSMILES syntax and end-group examples](https://olsenlabmit.github.io/BigSMILES/docs/line_notation.html#end-groups)

## Record a characterized lot

Create an `iki/lots.PolymerLot` and select the registered polymer in **Lot of**.
Enter the number-average molar mass (Mn), the mass-average molar mass (Mw), and
the method used for each. For conventional size-exclusion chromatography, record
the calibration standard and eluent as well. The `compute_polymer_lot` action
calculates the dimensionless molar-mass dispersity Đ = Mw/Mn when both values
are available and Mn is positive. [IUPAC definition](https://doi.org/10.1351/PAC-REC-08-05-02) ·
[Lots plugin](../lots/README.md)

The dev seed uses the July 1992 NIST certificate for polystyrene SRM 1478.
It registers a representative polystyrene repeat and leaves the sample's end
groups unspecified. The certificate supplies the following values for the
demonstration lot. [NIST SRM 1478 certificate](https://tsapps.nist.gov/srmext/certificates/1478.pdf)

| Property | Value        | Method                                   |
| -------- | ------------ | ---------------------------------------- |
| Mn       | 35,800 g/mol | Membrane osmometry                       |
| Mw       | 37,400 g/mol | Sedimentation equilibrium                |
| Đ        | 1.045        | Calculated from Mw/Mn, to three decimals |

Other lots of the same registered polymer can have different Mn, Mw and Đ.
