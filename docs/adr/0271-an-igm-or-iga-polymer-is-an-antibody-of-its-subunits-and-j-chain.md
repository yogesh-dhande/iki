# 0271. An IgM or IgA polymer is an antibody of its subunits and J chain

- Status: Accepted
- Date: 2026-11-27
- Refines: [0161](0161-a-complex-is-its-parts-and-their-copies.md) and
  [0164](0164-a-subtype-says-which-roles-its-rows-take.md)

## Context

An antibody is its chains and their copies
([ADR 0161](0161-a-complex-is-its-parts-and-their-copies.md)), with a heavy and
a light chain role ([ADR 0164](0164-a-subtype-says-which-roles-its-rows-take.md)).
`assemble` made an IgM one of its subunits, two heavy and two light chains, and
said to register its pentamer or hexamer as a generic protein complex. An IgM is
secreted as a pentamer of five subunits with a J chain, or a hexamer of six
without one (Hiramoto et al., _Sci Adv_ 4:eaau1199, 2018), and a mucosal IgA as
a dimer or larger polymer with its J chain (Kumar et al., _Science_
367:1008-1014, 2020). The molecule a lab makes and tests is the polymer, so a
registry that couldn't record it as an antibody, with its isotype and its J
chain, recorded a part of it instead.

## Decision

An antibody has a third role, `j_chain`, at most one protein. `assemble` takes
an optional J chain and, for format `other`, how many `subunits` an IgM's or an
IgA's polymer has: each is two heavy and two light chains, so a pentamer is 10
of each and its J chain. The counts follow what the molecules are: an IgM has
1, 5 or 6 subunits, and an IgA 1 to 5. A hexamer has no J chain (Wiersma et al.,
_J Immunol_ 160:5979-5989, 1998: pentamers are made with or without one,
hexamers without), and a pentamer may be given without one. A J chain is
refused without subunits, a polymer whose heavy chain isn't an IgM's or an
IgA's is refused, and so is a J chain that's numbered as an antibody's chain.

The rule `a_j_chain_joins_a_polymer` holds a record with a J chain, however it
was saved, to one copy of it in format `other`, with an IgM or IgA isotype and at
least two subunits' chains, and not in an IgM hexamer. The bonds between the
subunits and to the J chain aren't placed: the record's bond map says it's
incomplete.

## Consequences

A pentameric IgM is one antibody record, found as an antibody with its isotype,
and its identity is its chains, their copies and its J chain: a pentamer with a
J chain and one without are different molecules, as they are. A secretory IgA,
with its secretory component, a fourth kind of chain, is still a protein
complex, as is any antibody of more than one kind of heavy or light chain.
