# 0267. A complex's bonds say where they come from

- Status: Accepted
- Date: 2026-11-26
- Refines: [0161](0161-a-complex-is-its-parts-and-their-copies.md) and
  [0265](0265-an-antibodys-isotype-is-named-by-its-constant-regions-species.md)

## Context

A complex's `bonds` and `bond_map` are registry facts
([ADR 0161](0161-a-complex-is-its-parts-and-their-copies.md)). `bond_map` says
how much of the molecule's bonding they give: complete, incomplete or
predominant. It doesn't say how they're known. `assemble` places an antibody's
disulfides where its format, subclass and numbering put them: an IgG1's
interchain bonds and each domain's own. That's a pattern, not a reading of the
molecule. A registrar who copies the bonds from KEGG DRUG, a solved structure or
a non-reduced peptide map records something else, and a reader couldn't tell
the two apart.

## Decision

Every complex records where its bonds come from, in two registry facts on
`iki/core.Complex`:

- `bonds_from`: `pattern`, placed where its kind places them; `curated`, from
  a source that records this molecule's; or `observed`, seen in this molecule.
  Left empty, it isn't said.
- `bonds_source`: a link to that source, such as a database entry, a PDB
  entry, a publication's DOI or the peptide map. A registry fact must be a
  constrained value, so it's a URI, not free text.

`assemble` sets `pattern` on the bonds it gives. Each concrete complex type
(`iki/antibody.Antibody`, `iki/complex.ProteinComplex`) holds the rule
`stated_bonds_name_their_source`: curated and observed bonds name their source.

An antibody whose composition comes from a curated source, not from its chains'
numbering, is entered directly, as any record is, or registered as a protein
complex. `assemble` stays the check for chains it can read. It doesn't get an
option to skip that check: an assembly that ignored a contradicting heavy chain
would weaken the evidence its output is.

## Consequences

A reader can tell assembled bonds from curated or observed ones, and how
complete either is. When peptide mapping confirms an assembled antibody's
disulfides, a registrar changes `bonds_from` to `observed` and links the map.
Existing complexes say nothing until someone sets it.
