# Biologics mass spectrometry

Run **Compare mass or peptide coverage** with the registered antibody. For
an ADC, select its conjugate record and use the antibody as the sequence
reference. Optionally select a protein lot of that antibody or a conjugate
lot of that ADC. Iki checks the relationships and pins the versions it read.
Register and attach any raw instrument files before sign-off.

The action reads two versioned Iki interchange tables, described below: **Iki
mass table 1** (`iki_mass_table_1`) for a mass comparison and **Iki peptide map
1** (`iki_peptide_map_1`) for a peptide map. It doesn't read vendor exports or
assign species: deconvolution, peptide identification and glycoform assignment
happen in the lab's processing software. Convert its result into the table, then
attach the software's own exports to the result as **Source exports** and name
the software and version in **Processing software**. The result records which
table profile it read, and keeps the table, its SHA-256 and the exports
together. A new table version gets a new profile name, so older results keep
naming the columns they were read with.

The subject is a registered antibody, with an optional ADC and lot: the
comparison reads the antibody's heavy and light chains. Other proteins are out
of scope for this action.

## Mass comparison: Iki mass table 1

Choose the mass basis used to deconvolute the report and the lab's absolute
ppm tolerance: a mass comparison refuses to run without one. The result keeps
both. For an intact antibody or defined ADC, run its existing composition
action first. The starting mass is the neutral theoretical
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

## Peptide map: Iki peptide map 1

The peptide TSV requires `chain`, `start`, `sequence` and
`modifications`. Use the start position on the reference chain, at
1-based coordinates. Leave the modification column empty or give
semicolon-separated `position:UNIMOD:id` assignments. Positions 1
through the peptide's length name residues. Use 0 for the peptide
N-terminus and the peptide's length plus 1 for its C-terminus. The
importer records the site with its chain coordinate and counts each
covered residue once, including across overlapping peptides. The
position convention follows [mzTab 1.0](https://github.com/HUPO-PSI/mzTab).
[Unimod's acetylation entry](https://www.unimod.org/modifications_view.php?editid1=1)
gives an example with both terminal and residue sites.

The original report and its SHA-256 stay linked to the result.

## Review

A project's Admin chooses the procedure for the project's new results with the
`biologics-ms-review` starter, in the project's settings under Starters, and picks who
holds each lab role it asks for. A project that hasn't chosen uses the
deployment setting `review_flow`. Each result keeps the procedure it was
recorded under, so a change applies to new results only
([ADR 0266](../../docs/adr/0266-a-project-chooses-its-omics-results-review.md)).

| Procedure                          | What a result needs                                                                                                           | Lab roles                |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| **Research** (`research`, default) | A reviewer signs the recorded result.                                                                                         | Reviewer                 |
| **Regulated QC** (`regulated_qc`)  | An analyst signs submission, a reviewer independent of the creator, submitter and editors signs review, and QA signs release. | Analyst, reviewer and QA |
| **Record only** (`record_only`)    | Nothing: the result is kept as evidence and offers no sign-off.                                                               | None                     |

To review a result recorded under Record only, import it again once the project
or the deployment names a review procedure. Changes to signed findings return a result to
Recorded. See [ADR 0258](../../docs/adr/0258-mass-spectrometry-results-name-their-report-format.md).

## Public observations

The test sequences are trastuzumab [KEGG D03257](https://www.genome.jp/entry/D03257).
The intact G0F/G0F observation comes from
[an iCIEF–MS characterization](https://doi.org/10.1002/elps.202200170).
The reduced-chain observations and G0F composition come from
[Table 5 of a trastuzumab middle-up study](https://www.sigmaaldrich.com/US/en/technical-documents/technical-article/pharmaceutical-and-biopharmaceutical-manufacturing/biologics-biosimilars-characterization/middle-up-mass-analysis-using-trastuzumab-as-model-protein).
The peptide fixture is one identified heavy-chain peptide from
[the published LC–MS peptide map](https://doi.org/10.1021/acs.analchem.1c02807).
Each file is an excerpt transcribed into Iki mass table 1 or peptide map 1.
