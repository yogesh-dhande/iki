# 0260. A lab's storage layout and containers are its own

- Status: Accepted
- Date: 2026-11-26
- Refines: [0180](0180-where-material-is-and-how-its-received-moved-and-counted.md),
  [0211](0211-lots-inventory-samples-and-requests.md)

## Context

`iki/inventory` described one facility. A freezer held only shelves, every rack
was five boxes high and four deep, and the only container a box held was a tube
of liquid, measured in microlitres and scanned before each draw. Real storage
differs. A liquid nitrogen dewar or a chest freezer holds racks or boxes with no
shelf. Dewar racks hold one box on each of six or more levels, and upright racks
come in several formats. A compound store weighs powder out of a vial, so its
ledger is a mass. Benchling configures parent and child container restrictions,
grid sizes and container quantities per lab for the same reason.

The parts that every lab shares are the physical record: a barcode, a position,
custody, a ledger of what moved, an identified material, a hold with its reason,
and the restrictions of the material's lot. Those must stay exact whatever the
layout or the measure.

## Decision

**A freezer holds shelves, racks or boxes.** A lab with shelves keeps them; a
dewar or chest freezer takes its racks and boxes directly.

**A rack's format is set as it's made.** `Rack` has required, immutable `rows`
(boxes high, 1 to 16) and `columns` (boxes deep, 1 to 8), defaulting to five
and four. Its slots are numbered from the top, front to back, as before. A lab
whose racks are labelled another way declares a subtype with its own `storage`
grid, as any type may.

**A container is the shared physical record; a tube is its liquid profile.**
The abstract `Container` has the name, barcode, contents, hold, hold reason and
preparation date, the `available` and `identified` draw guards and the
`hold_explained` rule. `Tube` extends it with the microlitre ledger, its
concentration, its contents as the ledger's material and lot restrictions, and a
scan before each draw. A box holds any container. A lab declares its own
container kind by extending `Container` with its amount, such as a powder vial
measured in milligrams, and chooses its own scan confirmation. Holding derived
stock (`hold_withdrawn`, `hold_derived_tubes`) applies to every container kind.

Tube keeps its fields, HRIDs and behaviour. Its fields are now stored with the
container's. Before M1 there are no deployments, so the inventory migration is
regenerated.

## Consequences

Existing manifests that name `Tube` and `Box` keep working. A tube subtype, such
as a banked cell vial or a glycerol stock, still inherits the liquid ledger. A
powder vial or another lab container sits in a standard box beside tubes and
keeps the same hold and lot restrictions.

A container type defines exactly one amount unit, because a record has one
ledger. A lab that measures one container kind in two units declares two kinds.
A rack's slot labels and filling order are its type's. A lab changes them with
a subtype, not a value on each rack.
