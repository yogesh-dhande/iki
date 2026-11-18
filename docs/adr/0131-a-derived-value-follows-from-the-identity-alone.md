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
IMGT's CDRs, unchanged and without a word. An antibody's isotype, also
marked derived, is a claim a person makes about the chains: assembling
the same chains again with another isotype was dropped the same way, and
within a project that already held the antibody nothing was checked at
all, since registering an identity the project holds returned its record
before looking at what was given.

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

**A value someone asserts about an identity is a registry fact.** An
antibody's isotype is set once, by whoever registers it with one, and
then changed only by a registrar; a registration that claims another is
refused. Where the identity determines what's asserted (an isotype is
its heavy chain's constant region), the registering action checks the
claim against the identity as far as it can.

**Registering an identity a project already holds checks what's given
for its entry too.** It fills the entry's empty derived values and
facts, and refuses a fact that differs, as a registration in another
project does. The project's record is still returned as it was.

## Consequences

- A rerun with other parameters always lands, in the running project's
  record, and two projects can number one chain in different schemes.
- What every holder sees on an entry is what its identity gives, or
  what a registrar curates.
- A derived value from another version of the software still loses to
  the entry's, silently, as ADR 0115 decided; that's safe only because
  it's the same value.
- Plugins review their derived fields against this rule. The
  first-party antibody plugin's were corrected with it.
