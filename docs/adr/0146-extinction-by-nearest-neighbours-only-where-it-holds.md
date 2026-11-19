# 0146. Extinction by nearest neighbours, only where it holds

- Status: Accepted
- Date: 2026-11-18

## Context

An oligonucleotide's amount is measured by its absorbance at 260 nm, divided
by its molar extinction coefficient (ε260). For natural DNA and RNA single
strands, the nearest-neighbour model gives it from the sequence: each
doublet's ε, less each inner nucleotide's (Cantor, Warshaw & Shapiro,
_Biopolymers_ 9, 1059, 1970, for DNA; Puglisi & Tinoco, _Methods Enzymol._
180, 304, 1989, for RNA). Vendors' calculators (IDT's OligoAnalyzer,
Sigma-Aldrich's OligoEvaluator) use these tables.

Modified oligonucleotides are another matter. No published nearest-neighbour
parameters describe 2′-MOE, LNA, cEt, 2′-OMe or 2′-F sugars or
5-methylcytosine; calculators that give a value for them substitute the
natural nucleotide's, silently. Manufacturers of therapeutic oligonucleotides
measure ε for each molecule. A value that looks computed but isn't the
molecule's would mislead every concentration made from it.

## Decision

**ε260 is computed only where the parameters describe the strand**: a single
strand of natural DNA, or of natural RNA, by the tables above.

- **Phosphorothioates are read as phosphates**, as every calculator does; the
  note says so, since the parameters were measured on phosphodiesters.
- **A modifier's own ε260 is added**, as its supplier gives it (IDT's for
  6-FAM, the BHQs, Cy3 and Cy5), so a labelled probe's is the strand's and its
  dyes'. One that doesn't absorb at 260 nm (biotin, amino linkers, spacers,
  cholesterol, GalNAc) adds 0.
- **Otherwise there's no ε260**, and its note names the monomers no
  parameters describe, with how many of each: "Not computed: no
  nearest-neighbour parameters describe 2′-O-Methoxyethylribose (18),
  5-Methylcytosine (4)." A strand mixing DNA and RNA nucleotides (an siRNA's
  dTdT overhang) has none either, since neither table describes it.
- **A duplex has none**: its hypochromicity depends on its pairing, which the
  nearest-neighbour model for duplexes (Tataurov et al., 2008) describes, but
  only for natural DNA, which siRNAs aren't.

## Consequences

- Every ε260 Iki shows is the published model's, with its source.
- Most therapeutic oligonucleotides have none computed: their ε is to be
  measured and recorded, as their manufacturers do.
- A lab wanting a vendor's approximate value can record it on the record:
  Iki doesn't present it as computed.

## Alternatives considered

- **Natural analogues' parameters for modified nucleotides**, as several
  calculators do: always a number, often within 10% for 2′-OMe, but not the
  molecule's, and not marked as approximate where it's shown and used.
- **Sum of nucleotides' ε, less 10–20% for hypochromicity**: a rule of thumb
  with no source for modified nucleotides.
