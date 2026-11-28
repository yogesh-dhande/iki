# 0190. Receive packs into storage and count what remains

- Status: Accepted. [ADR 0245](0245-stock-is-received-with-or-without-an-order.md) adds receipts without an order, storage without positions and optional thresholds. Amended by [0294](0294-a-manifest-says-each-thing-one-way.md): `stock_catalogue.notify` is `{ roles: [...] }`.
- Date: 2026-11-22

## Context

A catalogue number and pack size describe what the lab can reorder. Material
on the shelf has a supplier lot, expiry, location and remaining quantity. One
supplier order may arrive in several deliveries, go to different shelves and
still have unopened packs outstanding. Looking only at the original order or
the quantity delivered overstates the material available after a withdrawal.

## Decision

A project keeps a catalogue of its reagents and consumables. Each item has a
stock unit, pack size and low-stock threshold. A reorder line records the
packs ordered and links to the item’s fixed pack size. Receiving a delivery
records its supplier delivery-line identity and lot, opens the lot's amount
ledger in the item's stock unit, places it in the selected storage and updates
the order. These writes share a transaction. An occupied position, stale
order revision or duplicate delivery leaves the order and stock as they were
before the attempt. [ADR 0139](0139-a-records-amount-is-a-ledger-of-what-moved.md)
[ADR 0180](0180-where-material-is-and-how-its-received-moved-and-counted.md)

Stock totals come from the balances of the project's lots. The stock page
shows the storage breakdown and distinguishes physical stock from material
available for routine use. An open reorder stays visible with its
outstanding quantity until all ordered packs have arrived. The low-stock
check uses the same availability calculation, including lot eligibility and
expiry. The hourly maintenance check notifies the project’s designated stock
managers when it observes low stock. Once it observes a recovery above the
threshold, a later drop can notify them again.

## Consequences

Partial deliveries can be reconciled to an order and a storage location.
Routine withdrawals and corrections made on the amount ledger appear in
stock availability without a second inventory entry. A purchasing system
can continue to own supplier communication, payment and invoice matching.
