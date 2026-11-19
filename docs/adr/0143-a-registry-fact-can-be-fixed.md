# 0143. A registry fact can be fixed

- Status: Accepted
- Date: 2026-11-18
- Refines: [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0131](0131-a-derived-value-follows-from-the-identity-alone.md)

## Context

A registry entry's fields are its identity, values derived from it, and
facts (ADR 0115). A fact is curated: anyone registering the entry may set it
while it's empty, and then only a registrar changes it, with a reason.

Some entries are what others are registered against. A HELM monomer
(ADR 0145) is a symbol, such as `MOE`, and a structure: the oligonucleotides
registered with `[MOE]` in their HELM mean that structure, and their weights
and formulas were computed from it. Its symbol is its identity, so that one
symbol names one monomer in a registry, as HELM's monomer libraries have it.
Its structure isn't derived from its symbol, and isn't an identity value
either: two structures under one symbol would be two monomers called `MOE`.
As a fact, a registrar could change it, and every oligonucleotide using it
would then say it's made of something it isn't, with weights that are wrong.
As a derived value, a second registration with another structure would be
ignored silently (ADR 0131): an import bringing its own `MOE` would compute
its oligonucleotides' weights from a structure the registry doesn't hold.

Registration systems that keep monomer libraries (the Pistoia Alliance's HELM
monomer manager, Biovia's and Dotmatics' biologics registries) don't let a
monomer that's in use change: a mistaken one is retired and replaced.

## Decision

**A fact can be fixed: `registry: fact` with `immutable: true`.** It's given
when its entry is registered, or once while it's empty, and then no one
changes it: not a holder editing a record, not a registrar. A registration of
the entry with another value is refused, as any fact's is, without saying
what the entry holds. A fixed fact may be `required`, which other facts may
not be: then the entry's first registration must give it.

**A wrong one is superseded.** A registrar supersedes the entry with one
that's right (ADR 0115), and the entries registered against it are found by
the graph (ADR 0123): what's downstream of the monomer. The refusal says so:
"is fixed in MON-000012's registry entry, as what's registered against it
reads it: no one changes it. If it's wrong, a registrar supersedes the entry
with the right one."

**Every surface says so.** The API's field is `registry: fact` with
`immutable: true`. The web app marks it "Fixed registry fact" and leaves it
out of a registrar's "Change facts". A bundle whose entry has another value
is a conflict in its plan, as for any fact.

States and locks (ADR 0118, ADR 0120) don't do this: a type with an identity
has its states on its projects' records, never on its entries, and a lock
freezes a record's own values, not its entry's.

## Consequences

- A value other entries are computed from can't drift under them, whoever
  writes it, and the database holds the entry's values as first registered.
- A registrar who finds a mistake supersedes rather than edits, which is more
  work for them and leaves a trail: the mistaken entry, its replacement, why,
  and each holder's move.
- A fixed fact that was left empty can be filled once, by anyone who may edit
  a record of the entry. Types that can't do without the value make it
  required.
