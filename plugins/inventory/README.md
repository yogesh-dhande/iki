# Inventory

`iki/inventory` is the storage plugin for Freezer, Shelf, Rack, Box and Tube. Its manifest has no plugin dependencies. Their ownership is described in [ADR 0211](../../docs/adr/0211-lots-inventory-samples-and-requests.md).

A filled tube identifies the record it contains and its position in storage. For example, a rack can contain one box of cell vials and another of compound stock. Each tube's barcode identifies the physical container at the bench. Its ledger records the starting amount, draws, transfers and splits. When a tube holds material from a lot, the lot's draw restrictions apply.

Withdrawing a tube from use records a reason. The `hold_withdrawn` action follows split and transfer history to put derived tubes in the same project on hold. A person records the reason for lifting a hold. [Amount ledger](../../docs/adr/0139-a-records-amount-is-a-ledger-of-what-moved.md)
