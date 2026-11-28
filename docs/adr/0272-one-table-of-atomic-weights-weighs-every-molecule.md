# 0272. One table of atomic weights weighs every molecule

- Status: Accepted
- Date: 2026-11-27
- Refines: [0161](0161-a-complex-is-its-parts-and-their-copies.md) and
  [0182](0182-a-conjugate-is-its-carrier-and-attachments.md)

## Context

Iki's plugins weighed average masses by three tables of atomic weights:

- the complex and protein plugins, and biologics MS for a chain, by
  Biopython's (IUPAC's of 2005: C 12.0107, H 1.00794, S 32.065);
- the oligo plugin, and biologics MS for glycans and disulfides, by CIAAW's
  abridged values (C 12.011, H 1.0080, S 32.06, P 30.974);
- the small-molecule and conjugate plugins by RDKit's own table (S 32.067,
  Cl 35.453).

So one formula weighed differently in each. Trastuzumab's
C6470H10012N1726O2013S42 weighed 145,529.81 Da in the complex plugin and
145,532.15 in the conjugate plugin, 16 ppm apart. An ADC's expected intact mass
mixed two tables: its carrier from one, its glycans from another. And the
difference between tables is a large share of a 20–50 ppm intact-mass
tolerance.

## Decision

**Every first-party plugin weighs by one table: CIAAW's standard atomic weights
2024** ([ciaaw.org](https://www.ciaaw.org/atomic-weights.htm); the "Atomic
Weights 2021" report, Prohaska et al., _Pure Appl. Chem._ 94, 573, 2022, with
the 2024 revisions). This is the current IUPAC recommendation, and the source
an auditor can check.

- For the 14 elements whose standard atomic weight is an interval (H, Li, B,
  C, N, O, Mg, Si, S, Cl, Ar, Br, Tl and Pb), Iki uses the single value CIAAW
  gives for each in its
  [abridged table](https://www.ciaaw.org/abridged-atomic-weights.htm):
  H 1.0080, C 12.011, N 14.007, O 15.999, S 32.06. Every other element has its
  standard atomic weight to full precision: P 30.973761998, I 126.90447.
- An element with no standard atomic weight, such as technetium, has no
  natural isotopic composition. Only a labelled isotope of it can be weighed;
  an unlabelled one is refused.
- A labelled atom weighs its nuclide's mass, as before.

**The table is one file, `scripts/weights.py`, copied into each plugin that
weighs a molecule.** Those plugins are complex, protein, oligo, biologics_ms,
conjugate, small_molecule and lots. The protein-translation example runs
protein's own action ([0298](0298-a-plugin-can-run-its-dependencies-actions.md)),
so it holds no copy. The file also holds the amino-acid residue formulas. Plugins share no library, so
the copies are byte-identical. A test checks that they match each other, the
web package's copy, and CIAAW's published values. A change to the table goes
into every copy at once, with an amendment here.

Monoisotopic masses are unchanged: each element's most abundant isotope's
mass, from AME2020.

## Consequences

- One formula weighs the same in every plugin and on every page, and an
  expected intact mass uses one table throughout.
- Average masses move by up to about 15 ppm for proteins, and by less for small
  molecules and oligonucleotides:
  - Haemoglobin α is 15,257.59 Da (ExPASy 15,257.55; Biopython 15,257.36).
  - Its globin chain alone is 15,126.40 u, which is what electrospray measures
    (15,126.4 Da).
  - Insulin is 5,807.63 (USP and DrugBank 5,807.57).
  - Amivantamab is 145,902.15 (KEGG 145,900.22).
  - Small molecules move in the third decimal where they have S or Cl:
    pheniramine hydrochloride is 276.808, RDKit 276.811.
- Monographs and databases computed with IUPAC's 2005 weights differ from
  Iki's in the fifth significant figure. A lab comparing against them should
  allow for that. The plugin READMEs give both values.
- Changing to another edition or another source later means changing one
  table, everywhere at once.
