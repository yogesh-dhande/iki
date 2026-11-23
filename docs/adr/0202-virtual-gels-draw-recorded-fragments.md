# 0202. Virtual gels draw recorded fragments

- Status: Proposed
- Date: 2026-11-22
- Extends [0191](0191-web-components-are-published-by-entry-point.md).

## Context

An Assembly record can already hold the fragment lengths for each part of a
digest. A gel view lets a reader compare those lengths with a size marker and
see which fragments share a band. Selectable reference ladders and agarose
percentages are established controls for this view.
[SnapGene gel properties](https://support.snapgene.com/hc/en-us/articles/10384200142868-Configure-the-Gel-Properties) ·
[Benchling gel electrophoresis](https://help.benchling.com/hc/en-us/articles/9684226222093-Gel-Electrophoresis-and-Restriction-Enzymes)

## Decision

Publish the gel as `@ikisci/science/virtual-gel`, a custom element whose data
is an agarose percentage and labeled lanes. Sample lanes take fragment lengths
and, when known, loaded masses. The gel combines equally sized sample fragments
and scales band brightness by mass. For Iki's recorded results it estimates
relative mass within each lane from fragment length, treating each supplied
fragment as one copy. The size scale is schematic and the band table preserves
the supplied sizes. Ladder lanes use the sizes and band masses published for
NEB's 1 kb and 100 bp products.
[NEB N3232](https://www.neb.com/en-us/products/n3232-1-kb-dna-ladder) ·
[NEB N3231](https://www.neb.com/en-us/products/n3231-100-bp-dna-ladder)

The Assembly overview supplies the record's saved fragment rows, one lane per
part. The Plasmid overview lets readers select from digest results in the
record's project that reference its DNA sequence as a backbone or insert. The
existing entity search applies project and record read permissions. Ladder and
agarose choices are local to the view.

## Consequences

The same renderer works with Iki results and with ordinary fragment data from
other applications. The result record remains the source for its provenance
and its full fragment table.
