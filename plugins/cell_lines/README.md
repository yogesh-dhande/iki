# Cell lines

Register a parental cell line under its primary Cellosaurus accession. A **Cell line
catalogue source** links a supplier and its catalogue number to the registered
line. A line can have several suppliers or several catalogue numbers. For example,
WI-38 is `CVCL_0579`, sold by ATCC as `CCL-75`. The accession field links to
the catalogue. [Cellosaurus](https://www.cellosaurus.org/CVCL_0579) · [ATCC](https://www.atcc.org/products/ccl-75)

A banked CellVial is an inventory Tube. Set its bank and contents to the same CellBank record and scan the vial when drawing material.

## From a catalogue to a sample

1. Create a **Parental cell line**, or run **Register a parental line from
   Cellosaurus**. Upload one Cellosaurus text export, and enter the exact
   supplier and catalogue ID. The action checks the primary accession and
   the supplier cross-reference, and records the input file as provenance.
   Repeating registration reuses the line and catalogue source. Importing a
   different supplier's record keeps its source alongside the first. The WI-38 example file
   contains only the catalogue fields the action reads.
2. For a local derivative, create an **Engineered cell line**, choose its
   parental or engineered parent, and list each documented knock-out, knock-in
   or stable transfection and its description. Record each independently
   isolated clone separately. For a parent from a supplier, select the
   catalogue source of the material used.
3. Create a **Cell bank** for the line, recording the banking date and passage.
   A master bank can name its catalogue source when made from a parental line.
   A working bank names its source master bank of the same line. Create one
   **Banked cell vial** per physical vial, select its bank and scan or enter
   its barcode. Use **Storage** to place it in an existing box position.
4. Create a **Cell bank check** for each test of a bank: STR authentication,
   species identity, mycoplasma, sterility, viral testing, karyotype, or
   another test the lab names. Select its bank, enter the method, result and
   test date from the lab, and attach the report if available. Enter the repeat
   date when the lab assigns one. Search checks by date to find those due for
   repeat testing. Keep each repeat as its own record.
5. When a vial is used, create a **Cell culture** with event **Thaw**.
   Name the vial, bank and frozen passage number. Use **Storage** to remove
   the vial and cite the thaw HRID in the movement reason. A culture can
   reference a final STR check and a final mycoplasma check that cover the
   event date for that bank: accepted, signed off or released by QA, tested
   on or before the event and, when the check has a repeat date, due on or
   after it. A missing reference records no claim about QC.
6. For each subculture, create a **Cell culture** with event **Passage**,
   select its immediate parent culture and the same bank, and increment the
   passage number by one. Two passages can cite the same parent when a
   culture branches. A passage can reference newer applicable bank checks.
7. Create a **Cell culture sample** for the observation, select its culture
   and collection date, and attach its measurement file. Open the sample's
   upstream graph to see the culture, thaw, banked vial, working and master
   banks, line and cited QC results. Open a check for its result and repeat
   date, or a vial for its movement history. From the line, walk downstream
   to find samples. Analysis runs that use a sample link their outputs
   through Iki's run lineage.

## Choose a check procedure

At project setup, an admin grants the roles of one procedure to project
members. The permission to finish a check follows the role grant, and the
check's history shows which procedure it followed.

| Procedure    | Project role grants                                                                                                      | Path                                                           |
| ------------ | ------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------- |
| Record only  | Cell bank check recorder                                                                                                 | Recorded → Accepted, with the report attached and no signature |
| Research     | Cell bank research signatory                                                                                             | Recorded → Signed off                                          |
| Regulated QC | Cell bank analyst, Cell bank reviewer and Cell bank QA. Leave Cell bank check recorder and research signatory ungranted. | Recorded → Submitted → Independently reviewed → Released by QA |

Record only suits a lab whose procedure keeps the testing laboratory's report
as the evidence. Accepting a check locks it, so a culture cites exactly what
was accepted. Research and Regulated QC are the signed procedures.

Assign independent people for regulated review and release. The creator,
submitting analyst and editors of the current cycle cannot independently
review or release the check. QA must also be independent of the reviewer.
Correcting a submitted or reviewed check returns it to Recorded with a reason
and requires fresh signatures. Accepted, Signed off and Released by QA close
the check with its reported outcome, including adverse or inconclusive results.
A wrong final check is followed by a new check, never changed.
[Check procedures](../../docs/adr/0261-a-bank-check-follows-the-procedure-its-project-grants.md)

## Plugin tests

From the repository root, run the catalogue import scenarios with:

```sh
uv run iki plugin test plugins/cell_lines
```

The server tests trace a synthetic local bank history, including storage
and the checks its cultures cite under each procedure, and test the guards
for all three. The catalogue identifiers in the tests are public. The banking
dates, local clone, passage numbers and results are synthetic examples.
