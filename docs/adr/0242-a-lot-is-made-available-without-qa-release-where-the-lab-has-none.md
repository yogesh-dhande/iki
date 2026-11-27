# 0242. A lot is made available without a QA release where the lab has none

- Status: Accepted
- Date: 2026-11-26
- Refines: [0162](0162-a-lot-is-a-projects-record-of-an-entrys-material.md),
  [0139](0139-a-records-amount-is-a-ledger-of-what-moved.md)

## Context

Every lot starts Quarantined, and until now only Lot QA's signed release made it usable.
That release needs a certificate of analysis, a stated source, a named maker and an
independent signer, as GMP needs for material entering production (21 CFR 211.84).

A discovery group's antibodies, buffers and solvents have no QA unit and often no
certificate. To use them in Iki it had either to grant Lot QA to its scientists, so that
"Released" no longer meant a QA release, or keep everything Quarantined, where only QA
sampling is allowed. Both made the disposition say something untrue.

The same lab also needed to tell two dates apart. A lot past its retest date isn't spent:
it's retested, and released again on the result (ICH Q7 11.6). Until now it could only
become Expired, which is final.

Two smaller gaps followed. A bought lot has no maker, so nobody was told when it was held.
A generic lot could only be kept by mass, which doesn't fit a litre of solvent or a box of
vials.

## Decision

**Available is a disposition of its own, set by a stock keeper.** Lots adds a Stock keeper
role (`iki/lots.stock_keeper`). A stock keeper makes a Quarantined lot Available, with no
signature, certificate or independence check. An Available lot is drawn from routinely, as
a released one is, but it isn't Released:

- it doesn't lock, so its values stay editable;
- a released lot takes none of it in a transfer;
- its page and exports say Available.

A stock keeper may also put a lot on hold and mark it Expired. A stock keeper lifts only a
hold on a lot that was Available before it. A hold on a released lot stays QA's to lift.

**Which route a project's lots take is who holds the roles there.** A regulated project
leaves Stock keeper unassigned, and nothing changes for it: only Lot QA's signed release
makes a lot usable, under every guard it had. A research project assigns Stock keeper and
needs no Lot QA. A project with both can release critical reagents and make consumables
available.

There is no setting to choose the route. The role is the setting. Assigning it is an
audited admin action that a reviewer can see in the project's members.

**Due for retest is not Expired.** Once a lot's retest date has come, QA can mark a
Released or Available lot Due for retest. It then allows only QA sampling, as its date
already did. Its values stay editable so the retest can be recorded. QA then releases it
again, or rejects it, or it expires. For a lot made in the lab, its edits while Due for
retest count against the releasing signer's independence, as its edits in quarantine do.
Where a lab expires lots at the retest date instead, Expired still accepts it.

**Someone is responsible for a lot, and stock keepers hear about holds.** A lot names who's
responsible for it, such as whoever ordered a bought lot. They and its maker are told when
it's released or made available. They, the maker and the project's stock keepers are told
when it's held or rejected. That notification stays required, because nobody should miss
material that can't be used.

**A generic lot is kept by mass, by volume or by count.** The Generic lot by volume
(`GenericVolumeLot`, mL) and the Generic lot by count (`GenericCountLot`, whole items) sit
beside the Generic lot by mass. Each has the same disposition, ledger and material checks.
This follows the reagents plugin's mass, volume and count stock lots.

## Consequences

A research lab uses its material in Iki without anyone claiming a QA release that never
happened. A QC lab's release is exactly what it was. A lot's disposition says which of the
two happened.

Granting Stock keeper in a regulated project opens a route without a QA release. That is
the project admin's audited choice, and an Available lot never passes for a released one.
A lab that wants this enforced across projects keeps the role unassigned there, as it
would keep Lot QA from people who shouldn't release.
