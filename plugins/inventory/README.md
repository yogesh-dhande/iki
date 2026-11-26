# Inventory

`iki/inventory` is the storage plugin for Freezer, Shelf, Rack, Box, and the
containers they hold. Its manifest has no plugin dependencies. Ownership is
described in [ADR 0211](../../docs/adr/0211-lots-inventory-samples-and-requests.md).

## Describe your storage

A **Freezer** is any cold store: a freezer, a refrigerator, a cold room or a
liquid nitrogen dewar. It holds shelves, or racks and boxes directly, as a
chest freezer or a dewar does. A **Shelf** holds racks and boxes.

Choose a **Rack**'s format when you create it: how many boxes high and how many
deep. A rack made without a format is five high and four deep, slots 1 to 20.
A dewar's vertical rack of six boxes is six high and one deep. Its slots are
numbered from the top, front to back. The format is fixed once the rack exists.

A **Box** has the rows and columns you give it, such as 9 × 9 for 2 mL cryovials
or 10 × 10 for 1.5 mL tubes, positions A1 onwards along each row.

## Containers

A container is one barcoded vessel holding material from one record. Every
container has a barcode, contents, hold and preparation date, whatever it holds.
A **Tube** is the container for liquid: its ledger is a volume in µL, it can
record a concentration, and drawing from it needs its code scanned first. A
glycerol stock from `iki/lots` or a banked cell vial is a tube, so it can sit
beside an ordinary tube in a box. Its contents identify the particular isolate or bank in
that vial. The ledger records the starting amount, draws, transfers and splits.
When a container holds material from a lot, the lot's draw restrictions apply.

A lab that keeps something other than liquid declares its own container kind in
its own plugin, extending `iki/inventory.Container` with the amount it measures
and whether a draw needs a scan. A box holds it beside tubes, and it keeps the
same hold and lot restrictions:

```yaml
apiVersion: iki/v1
kind: EntityType
name: PowderVial
extends: iki/inventory.Container
schema_version: 1
hrid: { prefix: PWV }
amount: { unit: mg, label: Mass, material: contents, follows: [contents] }
storage: { barcode: barcode, confirm: [draw] } # leave confirm off to draw without a scan
```

A lab whose racks number their slots another way declares a Rack subtype with
its own `storage.grid` labels and order. [Layouts and containers](../../docs/adr/0260-a-labs-storage-layout-and-containers-are-its-own.md)

## Holds

Withdrawing a container from use records a reason. Lifting a hold needs a
reason too. The `hold_withdrawn` action follows split and transfer history to
put derived containers in the same project on hold. [Amount ledger](../../docs/adr/0139-a-records-amount-is-a-ledger-of-what-moved.md)
