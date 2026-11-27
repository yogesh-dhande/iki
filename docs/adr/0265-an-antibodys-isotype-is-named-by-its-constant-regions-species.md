# 0265. An antibody's isotype is named by its constant region's species

- Status: Accepted
- Date: 2026-11-26
- Refines: [0161](0161-a-complex-is-its-parts-and-their-copies.md) and
  [0257](0257-an-antibodys-methods-are-separate-choices.md)
- Amended: 2026-11-27 — a human constant region is told from a macaque's, and a
  macaque's isotype is read from IMGT's rhesus references (see Amendment)

## Context

An antibody's isotype is its heavy chain constant region's class and subclass.
Iki's vocabulary held only the human names, IgG1 to IgG4, IgA1 and IgA2, IgM,
IgE and IgD, and a rule held an IgG to IgG1 to IgG4. A mouse hybridoma's IgG2a,
a rat's IgG2c or a rabbit's single IgG couldn't be recorded. Subclass names
also mean different genes in different species: a mouse IgG1 isn't a human
IgG1. `assemble` reads the isotype from the heavy chain by aligning it with
IMGT's human constant regions. A chain no human reference matched kept any
isotype given, unchecked.

## Decision

An Antibody records `constant_species` (human, mouse, rat or rabbit): the
species whose constant region the heavy chain has. A chimeric antibody with
mouse variable domains on a human IgG1 is human. Like `isotype`, it's a
registry fact: given once, then changed only by registrars.

Isotypes are named as IMGT's Repertoire names each species' IGHC genes. The
vocabulary adds IgG2a, IgG2b and IgG2c, a single `igg` (rabbit), and an
undivided `iga` for a species whose IgA isn't split into subclasses. The rule
`the_isotype_is_its_species` holds an isotype to its species' own list: a mouse
has no IgG2 or IgG4, a rat no IgG3, and a rabbit no IgD. An IgG's isotype is
any species' IgG subclass. An Fv, scFv or VHH has no constant region species.

`assemble` gives `human` when the heavy chain matches a human reference, and
refuses another species for it. For a chain no human reference matches, the
registrant states the species with the isotype, the two must agree, and both
are kept as stated, with a warning on the run.

## Consequences

Mouse, rat and rabbit antibodies record their real isotypes, and a search for
human IgG1s doesn't return mouse ones. Every human result is unchanged.

Iki doesn't read non-human isotypes from sequence (but a macaque's, since the
amendment below). That would need IMGT's
constant regions for each species, checked against primary sources the way the
human references are. Until then the species and isotype of those chains are the
registrant's statement. Bird IgY isn't a mammalian IgG and isn't included.

## Amendment. A human constant region is told from a macaque's

A match to a human reference at 90% identity told a human constant region from a
mouse's (about 65%) but not from an Old World monkey's. A rhesus macaque's IgG1,
IgG4 and IgM are 90.2-90.9% identical to the nearest human ones, so `assemble`
recorded a rhesus IgG1 as `constant_species: human`, `isotype: igg1`, as
registry facts. A rhesus Fab's CH1 (PDB 9ZRZ) is 93.2% identical to a human
one, and the most engineered human chains are only a little higher: a CH1 with
R214 and four charge-pair changes is 95.4%, and an IgG1 Fc with 15 changes
(knob-into-hole, LALA-PG, YTE, H435R/Y436F) is 95.5%. A threshold would have to
sit in that 2-point gap, so identity alone isn't the test.

`assemble` now also compares the chain with IMGT's constant regions of a rhesus
macaque (EMBL/GenBank BK063715; IGHG1\*03, IGHG2\*08, IGHG3\*07, IGHG4\*05,
IGHA\*22, IGHM\*03, IGHE\*01 and IGHD\*03), counting each residue of the chain
that differs from each reference. It's human only when it's at least 93%
identical to a human reference and at least 3 differences nearer it than any
macaque one. The 93% floor alone would pass the rhesus CH1; the comparison
doesn't, because it's 6 differences nearer a macaque reference, while the
engineered CH1 is 11 nearer a human one and the engineered Fc 27. Every human
allele and marketed antibody in the tests is 7 or more nearer; every rhesus, cynomolgus, baboon and African green monkey constant
region checked is nearer a macaque one, or, for a few CH1s alone (a baboon's
IgG4, one rhesus assembly's), as near, and so not human.

`constant_species` gains `macaque`, a rhesus or cynomolgus macaque's, which are
the same genes (a cynomolgus IgG1, GenBank L13307, is 98.5% identical to rhesus
IGHG1\*03). Its isotypes are IgG1 to IgG4, as IMGT names a macaque's IGHG genes
(not by their similarity to human subclasses: rhesus IgG2 and IgG3 are each
nearest human IgG1), and one IgA, IgM, IgE and IgD. Given as macaque, the
isotype is read off the macaque references as a human one is off the human
references, at 90% identity (other rhesus alleles are 94-100% identical to
IMGT's). Left out, a chain nearer a macaque's has its species and isotype left
out, with a warning: a baboon's or an African green monkey's is as near the
macaque references as other macaque alleles are, so the species is the
registrant's to give. Only a human isotype places a subclass's disulfides: a
macaque's hinges are other lengths.
