# 0131. A derived value follows from the identity alone

- Status: Accepted
- Date: 2026-11-18
- Refines: [0115](0115-registries-keep-identities-projects-keep-records.md)

## Context

ADR 0115 puts a type's `registry: derived` fields on its registry entry,
shared by every project that holds it. A run that offers a derived value
the entry already has, differently, is ignored: the value is the
identity's, computed again, perhaps rounded otherwise by another version
of the software.

That holds only for a value the identity alone gives. The antibody
plugin marked a chain's whole numbering derived, but its CDRs depend on
the numbering scheme and CDR definition a run chooses: trastuzumab's
CDR-H1 is GFNIKDTY by IMGT's definition and DTYIH by Kabat's. Numbering
the chain again in Kabat's scheme reported success while the entry kept
IMGT's CDRs, unchanged and without a word.

## Decision

**A field is `registry: derived` only if the identity alone gives its
value.** Given the identity, any run of the software computes the same
value, up to rounding. A result that depends on a run's parameters (a
scheme, a definition, a threshold, a model) is the project's record's:
each project keeps its own, and running again replaces it there, with a
revision. The antibody plugin's chain type, closest species and germline
genes are derived; its numbering, CDRs and the liabilities read off
them are each record's, with the scheme and definition they were found
by.

## Consequences

- A rerun with other parameters always lands, in the running project's
  record, and two projects can number one chain in different schemes.
- A derived value from another version of the software still loses to
  the entry's, silently, as ADR 0115 decided; that's safe only because
  it's the same value.
- Plugins review their derived fields against this rule. The
  first-party antibody plugin's were corrected with it.
