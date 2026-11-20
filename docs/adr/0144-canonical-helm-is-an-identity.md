# 0144. Canonical HELM is an identity

- Status: Accepted
- Date: 2026-11-18
- Refines: [0138](0138-an-identity-reads-sequences-and-sets-of-parts-as-what-they-are.md)

## Context

A modified oligonucleotide isn't a sequence of four letters. Nusinersen is
eighteen 2′-O-methoxyethyl nucleosides with 5-methylcytosine for each C,
joined by phosphorothioates; a 5′-biotin primer is its plain primer with a
biotin on a linker; a FAM/BHQ probe has a dye at one end and a quencher at the
other. Two records of these must be the same exactly when the molecules are.

HELM, the Pistoia Alliance's Hierarchical Editing Language for
Macromolecules, is the notation the industry exchanges such molecules in:
ChEMBL, the HELM toolkits, and the registration systems of Biovia, Dotmatics
and others write it. Its specification is version 2.04 (Pistoia Alliance,
1 November 2018). A HELM string names its polymers (`RNA1{[dR](A)[sP].[dR](C)}`),
each a chain of monomers from a monomer library, then the bonds between them
(`RNA1,CHEM1,1:R1-1:R2`), groups of polymers, and an annotation.

One molecule can be written in HELM many ways: `R` or `[R]`, a repeat as
`'3'` or written out, a mixture's members in any order, annotations or none,
its polymers and connections in any order and numbered as you like, either
end of a bond first, HELM 1 or HELM 2. HELM has no canonical form of its own
that keeps all of a molecule. The Pistoia toolkits' canonical form is HELM 1's:
the HELM 2 toolkit (HELM2NotationToolkit 1.3.13) keeps a polymer's order, but
not its connections' and drops base pairs and annotations, and when polymers
are identical the numbering it gives can come from the wrong permutation.
Indigo (1.49) writes HELM but doesn't canonicalise it, and RDKit reads only
plain RNA, DNA and peptides.

## Decision

**A string field can have the format `helm`.** Its value is HELM 2 (ending
`V2.0`), or HELM 1, which is read as HELM 2: a HELM 1 base pair moves to the
connections, and its free-text annotation is set aside. The database checks
its shape: a polymer, four `$` and the version. iki-wasm reads the rest, in
the server as in the browser, so a form says what a save will refuse as it's
typed, at which character, and why.

**An identity reads it as its canonical form,** which iki-wasm writes:

1. Monomer IDs are bracketed only when they're longer than one character
   (`[R]` is `R`).
2. A repeat of a fixed count is written out (`(R(A)P)'2'` is
   `R(A)P.R(A)P`); a range of counts (`'2-5'`) stays, as it's another
   molecule from each count.
3. The members of a list are in order, and so are a mixture's, each ratio
   without trailing zeros (`(T:0.50+C:0.5)` is `(C:0.5+T:0.5)`). Without
   ratios, each member is written once, as Pistoia's page on mixtures and lists
   says `(A+A+L+L+A+V)` "is exactly the same as" `(A+L+V)`, and a list of one
   monomer is that monomer. With ratios, the members stay as written.
4. A missing monomer (`_`) on its own, or as a base, is left out, since
   Pistoia's page on ambiguous monomers says it adds nothing there: `A._.G`
   is `A.G`, and `R(_)P` is `RP`. In a list (`(K,_)`, a lysine or none) it
   says something, so it stays. A connection to a missing monomer is refused.
5. Annotations are left out: the molecule is the same whatever's said of it,
   including a polymer's `"sense"`. A strand's role in a duplex belongs to the
   duplex, not to its HELM.
6. A ring is written from the unit it reads least from. A ring is a PEPTIDE
   or RNA polymer whose last backbone monomer's R2 is joined to its first
   one's R1: a head-to-tail cyclic peptide, or a cyclic oligonucleotide. Its
   units are ranked by how they're written, and the least rotation of that
   order is taken, so the same ring written from any unit is one form. Its
   closing bond and its other connections move with it. A ring that repeats,
   such as `(A.C)` twice, reads least from more than one unit, and each of
   those is tried, as identical polymers' numberings are (step 7). A ring
   with a range of repeats is left as written, and a disulfide or another
   side-chain bridge doesn't make a ring of the backbone.
7. Polymers are in order of their type (PEPTIDE, RNA, CHEM, BLOB) and their
   notation, and numbered from 1 within their type. Identical polymers, such
   as the two strands of a self-complementary duplex, are numbered whichever
   way makes their connections least, so the order they were written in makes
   no difference (up to 40,320 numberings, rings' turns among them; more
   refuses it).
8. A connection's lesser end comes first, ordering by polymer, position and
   attachment point, and the connections are in order. Base pairs (`pair`)
   are kept: they're how a duplex in one HELM string is written.
9. The version is `V2.0`.

Positions are counted as HELM 2.04 counts them (5.2.3), each monomer, a
branch's too, with a list or a group of units counted once, as its reference
parser does. A connection to a monomer inside a repeat that's written out has
no single position, so it's refused: write the repeat out to connect it.

The rest is read as written. An in-line SMILES monomer stays as its text,
since iki-wasm doesn't read chemistry; a type that needs one molecule to be
one record whatever its SMILES says so, as the `iki/oligo` plugin does by
refusing them. A monomer ID is compared exactly, case and all, as the HELM
libraries and toolkits compare them, though HELM 2.02 says it's not case
sensitive: `[MOE]` and `[moe]` are two monomers in a library that has both.

**HELM can be read without being saved.** `POST /helm/read`, in both SDKs,
and MCP's `read_helm` give each string's canonical form, its
polymers' monomers in order and its connections, or what's wrong and where.
Each polymer says how long it may be (the fewest and most amino acids, or
nucleotides with a base, for a range, `*` or a list with `_`), and whether
it's ambiguous: a range, a list, or a monomer HELM keeps for an unknown one
(`*`, `_`, a PEPTIDE's `X` or an RNA's `N`). Its polymer groups are listed
with whether each is a mixture (`+`) or one of its members (`,`), so a form
can say "either" for one and "a mixture" for the other.

## Consequences

- The same oligonucleotide entered from two systems, or by two people, is
  one registry entry; one with any monomer changed, or another bond, is
  another.
- Canonical HELM is Iki's: another system's canonical HELM may differ, so
  an import compares by reading both here, not by comparing text.
- What a monomer ID means isn't the format's: it's its monomer library's.
  For oligonucleotides, that's the `iki/oligo` plugin's registry of monomers
  (ADR 0145).
- A cyclic peptide or oligonucleotide is one record whichever residue it's
  written from, as a cyclic sequence is (ADR 0138). One read in the other
  direction is another molecule, and another record.
- Steps 3, 4 and 6 came after records were first made from this form (on
  2026-11-20). Before M1 there are no databases to keep, so nothing was
  migrated: a database made before then is made again, and so are the
  identity keys of anything with a HELM field.
