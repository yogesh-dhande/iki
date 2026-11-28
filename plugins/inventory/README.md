# Inventory

`iki/inventory` is the storage plugin for Freezer, Shelf, Rack, Box, and the
containers they hold, and the registry of the suppliers material comes from. Its manifest has no plugin dependencies. Ownership is
described in [ADR 0211](../../docs/adr/0211-lots-inventory-samples-and-requests.md).

## Describe your storage

A **Freezer** is any cold store: a freezer, a refrigerator, a cold room or a
liquid nitrogen dewar. It holds shelves, or racks, boxes and containers
directly, as a chest freezer or a dewar does. A **Shelf** holds racks, boxes,
and containers that stand on it, such as a bottle in a refrigerator.

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

## Suppliers

A **Supplier** is a company, culture collection or lab that material is bought or
received from, registered once for every project in the registry by its name, such as
Sigma-Aldrich, ATCC or a contract manufacturer. A lot, a reagent's catalogue pack, a
compound batch and a cell line's catalogue entry each name one, so one search by
supplier finds them all, whichever project they're in. Name it as its catalogue and
certificates do, and a culture collection as Cellosaurus does (ATCC, DSMZ), so a cell
line import can check its catalogue number. Imports that read a supplier's name, such as
an SDF file's Supplier field, register it the same way. Whether a supplier is approved
for GMP material is the lab's own procedure, which its own plugin adds.

## Holds

Withdrawing a container from use records a reason, and a draw from it is
refused until the hold is lifted. Lifting a hold needs a reason too. A lot's
release and restrictions are its own and follow its containers through the
ledger. [Amount ledger](../../docs/adr/0139-a-records-amount-is-a-ledger-of-what-moved.md)

What else goes on hold when a container does is the lab's procedure. Choose
one per project:

| Procedure          | How                                                                        | What's held                                                                                               |
| ------------------ | -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| Case by case       | Run **Hold stock derived from a withdrawn container** after withdrawing it | The containers selected, with the reason entered or the source's                                          |
| All derived stock  | A project admin turns on **Hold all stock derived from withdrawn stock**   | Every container split or transferred from it in the project, up to ten steps on, with the source's reason |
| The container only | Neither                                                                    | Nothing else                                                                                              |

Either way, each derived hold names the source's HRID, a container already on
hold keeps its own hold and reason, nothing is ever released automatically, and
the run checks that neither the source nor a derived container changed since
it read them. [Hold procedures](../../docs/adr/0264-what-a-hold-covers-is-the-labs-procedure.md)
