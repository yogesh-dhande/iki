# 0245. Stock is received with or without an order

- Status: Accepted
- Date: 2026-11-26
- Refines: [0190](0190-receive-packs-into-storage-and-count-what-remains.md)

## Context

ADR 0190 received stock only against a reorder line, into a storage location
with numbered positions, and gave every catalogue item a low-stock threshold.
That is one lab's procedure. Many labs order through a purchasing system Iki
doesn't see, receive material another lab or site sends them, and start with
stock already on the shelf. A cabinet or cold room has no positions. A box of
gloves isn't reordered by its level. Each of these labs had to invent an order
line, a grid or a threshold to record what it had.

What makes a receipt trustworthy is not the order. It is the document that
names it, who recorded it and when, the lot and its first ledger movement, and
that the same document line can't be received twice. QA release still decides
whether the material may be used.

## Decision

**A lot says how it was received.** A stock lot records `received_as`: against
an `order` line, as a supplier `delivery` without one, as a `transfer` from
another lab, site or organization, or as `opening_balance` stock. Only a lot
received against an order names its line, and the line's item must be the
lot's. A lot can only be made by a receipt.

**A document line is received once.** A receipt's identity is a digest of
whoever issued its document, the document's reference and its line. For a
delivery the issuer is the supplier, whether or not it was ordered, so a line
received against its order can't be received again without one. A transfer's
issuer is its sender, named on the lot. Opening stock is identified by the
lab's count sheet and line. The digest is unique in the project, as before.

**Receiving without an order is the lab's choice.** `receive_stock` receives
any of the three from the catalogue item. The deployment setting
`receipts_without_order` is `allowed` by default. A lab whose purchasing
procedure requires an order for every receipt sets it to `refused`, and gets
ADR 0190's procedure exactly: `receive_stock` refuses, and `receive_delivery`
is unchanged.

**Storage can be a place without positions.** A stock location keeps its
numbered grid. A stock area holds lots without positions. Both receive lots.

**A threshold is optional.** An item without one is never low and nobody is
alerted. With one, low stock works as ADR 0190 says.

## Consequences

A lab can record the stock it has on its first day, and material that comes to
it from anywhere, with the same identity, ledger, storage and QA release as
ordered stock. An order-only lab sets one setting. Lot disposition is
unchanged: every received lot starts in quarantine until QA releases it
(ADR 0162).
