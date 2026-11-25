# 0221. Strain isolates and their glycerol stocks

- Status: Accepted
- Date: 2026-11-25
- Refines [0185](0185-strain-identity-and-physical-stocks.md).
- Builds on [0162](0162-a-lot-is-a-projects-record-of-an-entrys-material.md), [0180](0180-where-material-is-and-how-its-received-moved-and-counted.md) and [0211](0211-lots-inventory-samples-and-requests.md).

## Context

Two clones of the same transformed strain have the same registered design.
A lab may verify each clone separately, prepare several vials of one clone,
and later need to identify which physical isolate supplied an experiment.

## Decision

Keep strain identities in `iki/strains`. Record a physical clone as an
`iki/lots.StrainIsolate` of a registered base or transformed strain. Each
isolate has its own project record, label and verification evidence. Where
the source is known, the lab links the transformation record or chooses a
parent glycerol stock. A lab that tracks only the strain
can use a single lot for all its stocks of that strain.

Declare `iki/lots.GlycerolStock` as an `iki/inventory.Tube`. The stock
references its isolate and requires the inherited `contents` field to name
that same isolate. An isolate uses its own record ID as its ledger material.
Its optional balance measures unvialed stock. Splitting that balance into vials
fills each vial's isolate and contents with the source isolate ID and debits
the dispensed volume from the isolate. Both physical records live in Lots so the dependency
runs from Lots to the strain registry and to Inventory. The registry can
be installed on its own with its plasmid dependency.

## Consequences

Each vial has a position in a standard inventory box, a scan identifier,
and a ledger of its volume. A draw checks the vial's hold and the isolate's
disposition and dates. A result that records the draw links upstream to the
stock, the isolate and the strain design. Different vials of one isolate
can exchange material where the ledger permits it. A transfer between
vials of different isolates fails even when they share the same design.
