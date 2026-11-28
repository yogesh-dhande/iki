# The proteomics importer's test files

Each producer's tables are its own output, with their real headers, kept byte
for byte except where a file says it's the first rows only.

- `PRIDE-16649.mztab` is an excerpt of PRIDE's mzTab export of experiment 16649,
  one of the HUPO-PSI mzTab 1.0 examples:
  [PRIDE_Exp_Complete_Ac_16649.xml-mztab.txt](https://github.com/HUPO-PSI/mzTab/blob/master/examples/1_0-Proteomics-Release/PRIDE_Exp_Complete_Ac_16649.xml-mztab.txt).
- `maxquant-1.6.17/` is MaxQuant 1.6.17.0's `combined/txt` output for the
  Galaxy Australia MaxQuant wrapper's test run, from
  `usegalaxy-au/tools-au`, `tools/maxquant/test-data/01/combined/txt`, at
  commit `fc05e754bcc2bc14555f4953103e0e8720707b39` (MIT licence).
  `allPeptides.txt` and `msmsScans.txt` are their header and first five rows.
  [tools-au](https://github.com/usegalaxy-au/tools-au)
- `fragpipe-22.0/` is FragPipe 22.0's single-experiment `protein.tsv`,
  `peptide.tsv`, `psm.tsv` and `ion.tsv`, each its header and first three
  rows, from `laurenfields/MSIght`,
  `notebooks/example_data/MSFragger_output`, at commit
  `8617e5bc086901808ee12bb9e43b1d656cdcd26f` (MIT licence).
  [MSIght](https://github.com/laurenfields/MSIght)
- `fragpipe-23.1/` is FragPipe 23.1's `combined_protein.tsv`,
  `combined_peptide.tsv`, `combined_modified_peptide.tsv` and
  `combined_ion.tsv` for five LysN ETD experiments, each its header and first
  three rows, from `schlebachlab/PRF-Search`,
  `Proteomic Search/MSFragger Pipeline/second_pass_upstream_search/results/LysN_ETD`,
  at commit `f180977281b2c1bb2cda101338492051f337f8be` (MIT licence). Its
  protein IDs are the authors' own database's FASTA headers.
  [PRF-Search](https://github.com/schlebachlab/PRF-Search)
- `diann/1.8-report.tsv` is a DIA-NN 1.8 main report's header and first four
  rows, from `Roestlab/massdash`,
  `manuscript/main_figures/figure3_report_diann.tsv`, at commit
  `2dc6ff10c52a5d6b7e311a8ceaea4f9482ce91a6` (BSD 3-clause licence).
  [MassDash](https://github.com/Roestlab/massdash)
- `diann/2-report.tsv` has the DIA-NN 2.x main report's columns, in the order
  DIA-NN's "Main output reference" lists them
  ([DIA-NN](https://github.com/vdemichev/DiaNN#main-output-reference)), as a
  Parquet report converted to TSV has them. Its four rows were written for
  these tests and are no one else's data: two runs, a protein group seen in
  both, and a decoy precursor.
