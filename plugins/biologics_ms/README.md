# Biologics mass spectrometry

Run **Compare mass or peptide coverage** with the registered antibody. For
an ADC, select its conjugate record and use the antibody as the sequence
reference. Optionally select a protein lot of that antibody or a conjugate
lot of that ADC. Iki checks the relationships and pins the versions it read.
Register and attach any raw instrument files before sign-off. The worker
reads the selected deconvoluted peak list or peptide report.

## Mass comparison

Choose the mass basis used to deconvolute the report and the lab's absolute
ppm tolerance. For an intact antibody or defined ADC, run its existing
composition action first. The starting mass is the neutral theoretical
composition, with fully reduced chains and no glycans. For a subunit, Iki
computes the neutral mass of the referenced chain or interval using
[Biopython](https://biopython.org/docs/1.88/api/Bio.SeqUtils.html).

The tab-separated peak list requires `name`, `analyte`, `observed_da`,
`disulfides`, `hex`, `hexnac`, `dhex`, `neuac` and `neugc`. For
`analyte`, use `intact`, `heavy` or `light`. Optional `start` and
`end` are inclusive 1-based chain positions. Report the number of retained
disulfides and total incorporated glycan residues for the species. For a
subunit with an assigned linker–payload or another independently assigned
mass change, supply `other_delta_da` and `delta_evidence`. For intact ADCs
the computed conjugate mass already includes the defined attachments.

Expected mass adds the glycan residue mass and any separately assigned
delta, and subtracts two hydrogens per retained disulfide. Reported mass
error is one million times observed minus expected, divided by expected.
The calculation uses [CIAAW](https://ciaaw.org/abridged-atomic-weights.htm)
for average atomic weights, [AME2020](https://doi.org/10.1088/1674-1137/abddaf)
for isotope masses, and [Unimod](https://www.unimod.org/masses.html) for
attached glycan residue formulas. Glycan assignments are compositions
and do not resolve isomers. A distribution of ADC loads needs a defined
species or a separately assigned subunit delta for comparison.

## Peptide map

The peptide TSV requires `chain`, `start`, `sequence` and
`modifications`. Use the start position on the reference chain, at
1-based coordinates. Leave the modification column empty or give
semicolon-separated `position:UNIMOD:id` assignments with 1-based
positions inside the peptide. The importer checks every peptide against
the reference, carries modifications at chain positions and counts each
covered residue once, including across overlapping peptides.

The original report and its SHA-256 stay linked to the result. In Research
a reviewer signs off. In Regulated QC the analyst signs submission, the
independent reviewer signs review and QA signs release.

## Public observations

The test sequences are trastuzumab [KEGG D03257](https://www.genome.jp/entry/D03257).
The intact G0F/G0F observation comes from
[an iCIEF–MS characterization](https://doi.org/10.1002/elps.202200170).
The reduced-chain observations and G0F composition come from
[Table 5 of a trastuzumab middle-up study](https://www.sigmaaldrich.com/US/en/technical-documents/technical-article/pharmaceutical-and-biopharmaceutical-manufacturing/biologics-biosimilars-characterization/middle-up-mass-analysis-using-trastuzumab-as-model-protein).
The peptide fixture is one identified heavy-chain peptide from
[the published LC–MS peptide map](https://doi.org/10.1021/acs.analchem.1c02807).
Each file is an excerpt transcribed into the plugin's TSV format.
