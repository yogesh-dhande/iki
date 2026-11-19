# 0161. A complex is its parts and their copies

- Status: Accepted
- Date: 2026-11-19
- Refines: [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0137](0137-a-list-can-hold-rows-of-typed-fields.md),
  [0138](0138-an-identity-reads-sequences-and-sets-of-parts-as-what-they-are.md)

## Context

Much of what a biologics lab registers is several molecules held together:
an IgG (two heavy chains, two light chains), a bispecific (two different
heavy chains, with one light chain both arms share, as emicizumab's, or one
each, as amivantamab's), an Fc fusion (etanercept: one fusion protein
twice), a trispecific (tilrekimig: five chains, one with a light chain fused
to it), an antibody bound to its antigen (trastuzumab's Fab and HER2, PDB
1N8Z), hemoglobin (two α chains, two β chains and four hemes).

Before this, Iki had one: `iki/antibody.Antibody`, a heavy chain and a light
chain as two references, without copies. A bispecific didn't fit, and the
test plugin's `acme/protein.Bispecific` was an inventory of chains, a row
per chain, that couldn't say which pair.

Registration systems split here. Benchling's biologics registry and
Dotmatics' Bioregister register a molecule by its chains and their
stoichiometry ("2 × HC1, 2 × LC1"), with templates per format that fix the
chains each takes; Genedata Biologics registers formats as templated
assemblies of chains, with the pairing drawn; the IUPHAR/Complex Portal
convention names a complex by its participants and stoichiometry
("2xHBA1:2xHBB"). None makes a complex's chain order, or the names a format
gives its chains, part of what it is. INN monographs and KEGG DRUG describe
a multispecific by its chains, then its disulfides and its pairing.

## Decision

**An abstract core type, `iki/core.Complex`, that every complex type
extends**, as every sequence type extends `iki/core.Sequence`. Its
`components` are rows (ADR 0137) of a `part`, any registry entry (a protein,
a nucleic acid, a small molecule, another complex), its `copies` (1 to
10,000) and its `role`, a string. Searching `iki/core.Complex` finds every
kind at once, the graph's `components.part` edges find every complex a
molecule is part of, and an action can take any complex.

**Its identity is its parts and their copies, not their roles or order**
(ADR 0138's multisets of rows, by `[part, copies]`). The roles are what each
type calls its parts, and the same molecule registered by two types is one
molecule: a protein complex of an antibody's two chains, two copies each, is
that antibody, and registering it is refused: "This is registered already,
as a iki/antibody.Antibody (ABY-000001): register it as one". Each part is
given once, with its copies: two rows of one protein would be the same
molecule written two ways, so a core rule refuses it ("lists PRT-000003
again: give each part once, with how many copies of it there are"). Its parts must be registry entries: a project's
own record can't be part of what an entry is.

**Generic and specific types.** `iki/complex.ProteinComplex` takes any parts
in any role, loosely checked (each part once, its bonds within its parts). A
specific type fixes the roles its parts take and checks them strictly
(ADR 0164): `iki/antibody.Antibody` takes a heavy chain and at most one
light chain, each a protein, and its format says their copies. A lab
chooses either, and adds its own specific types in its own plugin, as the
test plugin's `acme/protein.Bispecific` does. Until there are specific types
for them (bispecifics and multispecifics, Fc fusions, antibody–antigen
complexes, after M1), those are protein complexes, registered with their
parts and bonds as they are.

**The two-chain antibody becomes a specific complex.** Its heavy and light
chain references become two rows, `heavy` and `light`, with copies (two each
for an IgG, one for a Fab, an Fv, an scFv or a VHH). Its identity changes
from `{heavy_chain, light_chain}` to its parts and copies, declared by
`iki/core.Complex`. That breaks no antibody's identity in a way that
matters: before M1 there are no databases to keep (migrations are
regenerated), and the new identity tells apart what the old one couldn't: a
whole IgG and a half-antibody of the same heavy and light chain (one copy of
each, as knobs-into-holes bispecifics are made from) were one entry, and are
now two. The alternative, keeping `Antibody` apart with its two references and
mapping it onto complexes for search, would leave two identities for one
molecule: a protein complex of the same chains would be another entry.

**How the parts pair is a fact, not the identity.** Which light chain pairs
with which heavy chain is what the chains were made to do: a mispaired
assembly of amivantamab's four chains (Schaefer et al., PNAS 108:11187, 2011)
is a product-related impurity of the same molecule, not another molecule.
So the test plugin's `arms` (which heavy chain is each arm) are a registry
fact (ADR 0115): given with the entry, then
changed only by its registrars, and a registration with other pairing is
refused, as any differing fact is.

**Bonds are a fact too, by copy.** A complex's `bonds` are rows of a
disulfide (or isopeptide, thioether, other) between residue `residue` of
copy `copy` of one part and `partner_residue` of `partner_copy` of another,
or of the same part: an IgG's hinge disulfides join copy 1 of its heavy
chain to copy 2. The core rule checks each end is one of its parts, its copy
is one it has, its residue is in the part's sequence and, for a disulfide,
a cysteine, and that no residue is in two. The antibody plugin's actions
give them as each subclass places them (an IgG1's light chain to C220 and
heavy chains at C226 and C229, EU numbering), checked against KEGG DRUG's.

**What its parts make it is derived from its identity.** Its molecular
formula, average and monoisotopic masses, chain count and molar absorbance at
280 nm are its parts', each times its copies, computed by the complex plugin
for every complex type (`registry: derived` on `iki/core.Complex`), so a lot
or anything else can read them from any complex. They're its parts as they
are, each cysteine's thiol free: its bonds are a fact that may be corrected,
so what they change (2.016 u off for each disulfide, and a cystine's 125
L/(mol.cm) on at 280 nm, Pace's) is worked out where they're shown, by its
Composition tab and the run's message, and by whatever else reads them,
never stored to go stale. Cystines are counted from its disulfides, not from
every pair of cysteines, as one protein's oxidized absorbance is: an
antibody's light chain has an odd number of cysteines, one of them bonding
the heavy chain. A complex with a part that isn't a protein (a heme) has no
absorbance, as Pace's values weigh only proteins.

**An action can include a list of rows' references**: `include:
[components.part]` gives a run each row's part, in the rows' order, at the
revision it had at launch (ADR 0105).

## Consequences

- One molecule has one entry whichever type registered it, and a generic and
  a specific type can coexist in one registry: a lab that registered a
  complex as a protein complex and registers it again as an antibody is told
  it's registered already, and as what (ADR 0165).
- An antibody's chains are rows: `heavy_chain` and `light_chain` are gone
  from its fields, its API and its bundles. Its actions read
  `components.part`.
- Types that differ only in role names can't make two entries of one
  molecule, which is what a registry is for, but nor can a lab use roles to
  register two "different" complexes of the same parts and copies.
- A mispaired or misbonded registration is refused rather than registered
  as a new molecule: correcting a fact is a registrar's.
- Search of complexes by part and copies is one query over one table,
  whatever the type.
