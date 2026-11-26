# 0265. An antibody's isotype is named by its constant region's species

- Status: Accepted
- Date: 2026-11-26
- Refines: [0161](0161-a-complex-is-its-parts-and-their-copies.md) and
  [0257](0257-an-antibodys-methods-are-separate-choices.md)

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

Iki doesn't read non-human isotypes from sequence. That would need IMGT's
constant regions for each species, checked against primary sources the way the
human references are. Until then the species and isotype of those chains are the
registrant's statement. Bird IgY isn't a mammalian IgG and isn't included.
