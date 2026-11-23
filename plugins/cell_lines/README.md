# Cell lines

Register a parental cell line under its primary Cellosaurus accession. A **Cell line
catalogue source** links a supplier and its catalogue number to the registered
line. A line can have several suppliers or several catalogue numbers. For example,
WI-38 is `CVCL_0579`, sold by ATCC as `CCL-75`. The accession field links to
the catalogue. [Cellosaurus](https://www.cellosaurus.org/CVCL_0579) · [ATCC](https://www.atcc.org/products/ccl-75)

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
4. Create a **Cell bank check** for each STR authentication or mycoplasma
   test. Select its bank, enter the method, result, test date and repeat date
   from the lab, and attach the report if available. Search checks by date
   to find those due for repeat testing. Keep each repeat as its own record.
5. When a vial is used, create a **Cell culture** with event **Thaw**.
   Name the vial, bank and frozen passage number. Use **Storage** to remove
   the vial and cite the thaw HRID in the movement reason. A culture can
   reference a signed STR check and a signed mycoplasma check that cover the
   event date for that bank. A missing reference records no claim about QC.
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

## Select a review workflow

At project setup, an admin grants one of the following role sets to project
members. The permission to sign each transition follows the role grant.

| Workflow     | Project role grants                                                                                   | Signed path                                                    |
| ------------ | ----------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| Research     | Cell bank research signatory                                                                          | Recorded → Signed off                                          |
| Regulated QC | Cell bank analyst, Cell bank reviewer and Cell bank QA. Leave Cell bank research signatory ungranted. | Recorded → Submitted → Independently reviewed → Released by QA |

Assign independent people for regulated review and release. The creator,
submitting analyst and editors of the current cycle cannot independently
review or release the check. QA must also be independent of the reviewer.
Correcting a submitted or reviewed check returns it to Recorded with a reason
and requires fresh signatures. Signed off and Released by QA close the check
with its reported outcome, including adverse or inconclusive results.

## Plugin tests

From the repository root, run the catalogue import scenarios with:

```sh
uv run iki plugin test plugins/cell_lines
```

The server tests trace a synthetic local bank history, including storage
and the signed checks for its thaw, and test the guards for both review
workflows. The catalogue identifiers in the tests are public. The banking
dates, local clone, passage numbers and results are synthetic examples.
