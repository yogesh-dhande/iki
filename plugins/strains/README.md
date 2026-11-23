# Bacterial and yeast strains

Use `iki/strains` to register microbial strains and track the glycerol stocks
made from them. It uses the plasmid registry and the cryoboxes and racks in
`iki/example_lims`. The plugin's records use the standard entity forms, storage
map, amount ledger and lineage graph.

## Register base strains

Run **Import a strain catalogue** with the [example CSV](data/atcc.csv) to
register two base strains and their species. The examples are the bacterial
strain MG1655 and the yeast strain BY4741. Their designations and genotypes
come from [ATCC 700926](https://www.atcc.org/products/700926) and
[ATCC 201388](https://www.atcc.org/products/201388). The CSV writes the yeast
deletions with the symbol Δ. Species references use NCBI Taxonomy IDs
[562](https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=562) and
[4932](https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=4932).

For a lab catalogue, supply a UTF-8 CSV with the same seven headers.
`authority` is the collection or lab's uppercase code. `accession` is its
case-sensitive identifier, including any leading zeros. `kind` is
`bacterium` or `yeast`. The designation (`name`) and `genotype` cells can
be empty. They record what the catalogue actually gives. A file can hold
up to 1,000 strains and 512 KiB. A repeated accession within its authority
or a malformed row fails the import. A successful import records the file
and CSV record digest on each strain.

The **Base strain** form also registers one strain directly. The collection
and accession identify its registry entry. The project supplies a display
name, and a registrar can correct its shared genotype.

## Register a transformed strain

Create a **Transformed strain**, choose its base, and select each plasmid it
carries. Plasmids are entries from `iki/plasmid`. Select each once. Entering
the same base and the same plasmids in another order finds the same strain.
A different base or a different plasmid set identifies another strain.

## Store a vial and trace its use

1. Create one **Glycerol stock** per vial, referring to its base or transformed
   strain. Enter the initial volume and its barcode when available.
2. Create a **Strain stock box**, place it in a rack, and use Storage to place
   each stock at a position. The box map and the vial's location history show
   where it is and where it has been.
3. On the stock's amount ledger, draw the volume used, confirm the barcode
   or HRID, and choose the existing result or experiment under **Used in**.
   Iki records the draw against that vial. Transfers between stocks require
   the same strain.
4. Open the result's **Lineage** page and follow **Upstream** to the stock and
   its strain, then to the base and plasmids of a transformed strain.
   **Downstream** from the strain finds results made using its stocks.

The registry and stock design is documented in
[ADR 0185](../../docs/adr/0185-strain-identity-and-physical-stocks.md).
