# 0145. A registry of HELM monomers

- Status: Accepted
- Date: 2026-11-18
- Refines: [0115](0115-registries-keep-identities-projects-keep-records.md), [0143](0143-a-registry-fact-can-be-fixed.md)

## Context

HELM names monomers by symbol (`[MOE]`, `[sP]`, `[5FAM]`) and leaves what
each one is to a monomer library: its structure, with its attachment points.
An oligonucleotide's weight, formula and every other property come from those
structures, so whichever library a deployment reads is part of every
oligonucleotide's record. Libraries differ: the Pistoia Alliance's own sets
(HELMCoreLibrary, monomerLib2.0, the Ionis library) give some symbols
different structures, some of their SMILES lose the stereochemistry their
molfiles have, and a few are wrong (a peroxide where Cy5's linker should end,
a protonated nitro group in BHQ-2). Labs add their own: a new linker, a dye
from another supplier, a sugar of their own design.

A run has no token to read Iki's API with (ADR 0105), so it can't look a symbol up in a
registry as it computes. And a monomer's structure, once an oligonucleotide
was weighed from it, can't silently change: that oligonucleotide's recorded
weight would no longer be its monomers'.

## Decision

**Monomers are registry entries** (`iki/oligo.Monomer`), identified by
polymer type and symbol, as HELM names them. Symbols are compared exactly:
HELM's IDs are case-sensitive, and a run naming `[moe]` fails, saying it may
mean `[MOE]`.

**A monomer's structure is a fixed fact** (ADR 0143), as are its role,
natural analogue and ε260: given when it's registered, never changed, even by
a registrar. A wrong one is superseded (ADR 0115): a registrar registers the
right structure under a new symbol, since a superseded identity can't be
registered again, supersedes the old entry with it, and the
oligonucleotides downstream of the old one (ADR 0123) are registered again
with the new symbol, their weights computed from the right structure. Its
name and where it's from are facts a registrar may correct.

**Its structure is SMILES with its caps mapped**, `[H:1]` or `[OH:3]`, as
HELM's libraries write them, so it's the monomer as it is unjoined. A derived
structure key, the SHA-256 of its polymer type and RDKit's canonical SMILES
with each attachment point as `[*:n]`, is unique: two symbols can't name one
structure, however it's drawn.

**The plugin ships a library, and its runs resolve symbols from it**: 28
monomers, from the Pistoia sets at a fixed commit, with corrections, each
checked against its supplier's published mass (the plugin's `data/README.md`
lists them). A run registering an oligonucleotide takes the monomers it's
given as inputs (a lab's own) and the library's for the rest, and creates the
library ones it used, with all their facts. A create of an entry the registry
has fills only its name and sources; its structure must be the registry's, or
the run fails (ADR 0115), so a library that changed under a registry, or a
lab's own monomer with a library symbol, is never weighed from the wrong
structure. The oligonucleotide refers to the monomers it's made of, so what
it was computed from is on its record.

**An oligonucleotide registered otherwise** (its form, the API) is computed
by a triggered run from the monomers it lists (an `include`), and fails,
naming them, if its HELM names monomers it doesn't list.

## Consequences

- Every property of an oligonucleotide traces to registry entries whose
  structures can't change.
- A lab's monomer is a registry entry like any other, with its form, its
  history and its registrars.
- A symbol means one structure in a registry, while two deployments may give
  one symbol different structures; HELM exchanged between them should travel
  with its monomers.
- A run registering an oligonucleotide with a lab's monomer must be given it
  as an input: the run can't find it in the registry by itself.

## Alternatives considered

- **Structures in HELM** (inline SMILES, which HELM 2 allows): one record
  would hold everything, but the same monomer would be written differently in
  every string, so the identity would differ, and nothing would stop a typo
  making a new monomer. Refused in this plugin.
- **The library only, no registry**: simple, but a lab couldn't add a monomer
  without a plugin release, and nothing would record which structures a
  weight came from.
- **Monomers changed in place by registrars**: matches most registration
  systems' UI, but every oligonucleotide's computed values would then
  silently disagree with its monomers.
