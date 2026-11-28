# The Cellosaurus importer's test files

- `wi-38.txt` is WI-38's Cellosaurus text export, as
  `https://www.cellosaurus.org/CVCL_0579.txt` gave it on 24 November 2026
  (entry version 46, SHA-256 `0aa6102d14a216863cf64b1de1a329016abd4f4c1db5fe09887ef42287579d98`),
  kept byte for byte: the cell line's entry, then the 42 references it cites,
  each ending in `//`.
- `two-cell-lines.txt` joins two lines' exports, to test that the importer
  refuses a file naming two lines: 1-5c-4's whole export,
  `https://www.cellosaurus.org/CVCL_2260.txt` (27 November 2026), then the
  entry alone from `wi-38.txt`.

Cellosaurus is the SIB Swiss Institute of Bioinformatics's, under the Creative
Commons Attribution 4.0 International licence (CC BY 4.0). These files are
unchanged but for the join described above.
[Cellosaurus](https://www.cellosaurus.org/) ·
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
