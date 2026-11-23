# Conjugates

`iki/conjugate.Conjugate` registers a molecule by its carrier and attachments.
Use its registry entry for all lots of the same design. Each physical lot
retains its own measurements, including any drug-to-antibody ratio (DAR).

## Register a conjugate

1. Select the registered carrier. For an antibody–drug conjugate (ADC), use
   the antibody. For a PEGylated protein, use the protein. For an oligo
   conjugate, use the unmodified carrier strand or duplex, preserving its
   other modifications and terminal groups.
2. Add each attachment as a row. Choose the registered compound, name its
   site on the carrier (including the chain or strand when relevant), and
   enter the number of copies per carrier if fixed. A prelinked payload is
   one compound including its linker. Use `unlocalized` when the site has
   not been resolved. Leave copies empty for variable or unknown loading.
3. Record the net atoms lost from the two registered free parts for **one**
   attachment. For instance, `H2O` accounts for one water. Enter `none` for
   a known zero loss or leave the field empty when the balance is unknown.
4. Open **Composition** to inspect the sites, parts and masses. The backend
   action `compute_composition` adds the carrier formula and each attachment's
   formula less its stated loss, multiplied by that row's copies. The neutral
   formula determines the average and monoisotopic masses. The composition
   note explains the basis or which details are needed.

For a protein or antibody the calculation starts from reduced chains and
excludes glycans and other post-translational modifications. An oligo or duplex
contributes its registered free-acid formula. Use the reported basis when
comparing a calculation with a measured mass.

### Worked example

Chain C of PDB 8V1W is a seven-base DNA strand, `GTCGGAC`, with one adenosine
monophosphate (AMP) on its 5′ phosphate. The coordinates record the bond
between the phosphate of the first G and O3P of the AMP. The dev seed registers
the 5′-phosphorylated carrier by HELM and AMP by its published structure, then
creates a conjugate with one attachment and a demonstration lot referencing it.
[PDB 8V1W](https://www.rcsb.org/structure/8V1W) ·
[AMP in the PDB Chemical Component Dictionary](https://www.rcsb.org/ligand/AMP)

| Registered part or result          | Neutral formula |
| ---------------------------------- | --------------- |
| Carrier, terminal phosphate intact | C68H87N28O43P7  |
| AMP                                | C10H14N5O7P     |
| Loss at the diphosphate bond       | H2O             |
| Adenylated strand                  | C78H99N33O49P8  |

The strand weighs 2,530.62 u on average and 2,529.4170 u monoisotopically.
These are calculated masses of the neutral free acid. The deposited sample
supplies the connectivity. [8V1W coordinate file](https://files.rcsb.org/view/8V1W.pdb)

### GalNAc and PEG

Givosiran carries one L96 ligand at the sense strand's 3′ end. Register the
duplex as its carrier and the complete ligand as one attachment. Its three
GalNAc branches belong to that ligand, so the attachment row has one copy.
[Givlaari label, section 11](https://www.accessdata.fda.gov/drugsatfda_docs/nda/2019/212194Orig1s000Lbl.pdf)

Pegfilgrastim carries one nominal 20 kDa monomethoxy PEG chain on filgrastim's
N-terminal methionine. For a distributed PEG, select its precursor monomer
as the compound reference, give the PEG grade and termini in `peg_grade`, and
use the number of PEG chains as the copy count. A distribution of chain
lengths leaves the exact formula and both formula-derived masses empty. For
a discrete PEG of known structure, register the complete oligomer as a compound
and leave `peg_grade` empty. [Neulasta label, section 11](https://www.accessdata.fda.gov/drugsatfda_docs/label/2019/125031s198lbl.pdf)

## Record a lot and its drug loading

Create an `iki/lots.ConjugateLot` and select the conjugate in **Lot of**.
For brentuximab vedotin, the carrier is the cAC10 antibody and the attached
part is the linker–payload. The label gives approximately four drugs per
antibody across the product. Leave the registered copy count empty for this
distribution. [Adcetris label, section 11](https://www.accessdata.fda.gov/drugsatfda_docs/label/2025/125388Orig1s108lbl.pdf)

On the lot, select the DAR method and enter the measured distribution. For
example, Agilent found peak areas of 8.11%, 38.83%, 36.88%, 12.78% and 3.40%
for intact loads of 0, 2, 4, 6 and 8, respectively. With `hic` selected,
`compute_conjugate_lot` returns an area-weighted DAR of **3.29** and an
unconjugated area share of **8.11%** for that sample. The lots plugin also
supports reduced-chain measurements and a directly measured UV/Vis DAR.
[Agilent 5994-3522EN, equation 1 and table 1](https://www.agilent.com/cs/library/applications/an-adc-dar-1260-ii-prime-bio-lc-5994-3522en-agilent.pdf) ·
[Lots plugin](../lots/README.md)
