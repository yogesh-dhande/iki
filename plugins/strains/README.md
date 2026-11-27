# Bacterial and yeast strains

Use `iki/strains` to register microbial strains. A base strain has a collection
accession. A transformed strain has a base and a set of registered plasmids.
With `iki/lots` installed, the lab can trace individual isolates and the
glycerol stocks made from them.

## Register base strains

Run **Import a strain catalogue** with the [example CSV](tests/data/atcc.csv) to
register two base strains and their species. The examples are the bacterial
strain MG1655 and the yeast strain BY4741. Their designations and genotypes
come from [ATCC 700926](https://www.atcc.org/products/700926) and
[ATCC 201388](https://www.atcc.org/products/201388). The CSV writes the yeast
deletions with the symbol Δ. Species references use NCBI Taxonomy IDs
[562](https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=562) and
[4932](https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=4932).

For a lab catalogue, supply a UTF-8 CSV with a header row. Iki's own
exchange format has seven columns:

| Column            | Holds                                                                      |
| ----------------- | -------------------------------------------------------------------------- |
| `authority`       | The collection or lab's uppercase code, such as `ATCC`                     |
| `accession`       | Its case-sensitive identifier there, including any leading zeros           |
| `taxon_id`        | The species' NCBI Taxonomy ID                                              |
| `scientific_name` | The species' scientific name                                               |
| `kind`            | `bacterium` or `yeast`                                                     |
| `name`            | The strain designation, if the catalogue gives one                         |
| `genotype`        | The reported genotype in the source's notation, if the catalogue gives one |

A file with other headers, such as a collection's own export, can be imported
as it is. In the action's form, enter the header that holds each value. Give
the collection code or the kind for every row when the file has no column for
it, such as an export of one collection's bacteria. Columns Iki doesn't read
are ignored, and `name` and `genotype` can be left out. The run records the
mapping with its other parameters.

A file can hold up to 1,000 strains and 512 KiB. A repeated accession within
its authority, a malformed row, a missing identity column, or a value given
both in a column and for every row fails the import. A successful import
records the file and CSV record digest on each strain.

The **Base strain** form also registers one strain directly. The collection
and accession identify its registry entry. The project supplies a display
name, and a registrar can correct its shared genotype.

## Register a transformed strain

Create a **Transformed strain**, choose its base, and select each plasmid it
carries. Plasmids are entries from `iki/plasmid`. Select each once. Entering
the same base and the same plasmids in another order finds the same strain.
A different base or a different plasmid set identifies another strain.

## Track isolates and stocks

1. In Lots, create a **Strain isolate lot** for each clone of interest. Choose its
   base or transformed strain and enter the label on the isolate. Link the
   transformation record or the parent stock when known. Attach the verification
   evidence for that isolate. A project that tracks strains without distinguishing
   clones can use one isolate lot per strain.
2. Create a **Glycerol stock** for each physical vial. Select the same isolate
   for **Isolate** and **Contents**. Record the vial's starting volume and its
   barcode when available. If you recorded a bulk volume on the isolate, choose
   **Split into aliquots** on its ledger to fill new vials and debit that volume once.
3. Place the stock in an Inventory **Box** at its labeled position. The same
   box can also hold ordinary tubes. The stock's location history records
   each move.
4. When you use a stock, scan its barcode or HRID and select the result or
   experiment under **Used in**. The lot's disposition governs the draw.
   Stocks of two separately tracked isolates have different material identities,
   even if their strains are identical.
5. Open **Lineage** on the result. Follow **Upstream** to the vial, its
   isolate and the strain. A transformed strain also links to its base and
   its plasmids.

[Strain identity](../../docs/adr/0185-strain-identity-and-physical-stocks.md) ·
[Isolates and stocks](../../docs/adr/0221-strain-isolates-and-their-glycerol-stocks.md)
