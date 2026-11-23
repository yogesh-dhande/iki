"""Where each record that's a thing in the lab is kept (ADR 0180).

A type declares ``storage``: what its records hold, the grid of a container's positions,
the field with its barcode. Iki keeps where each record is in ``iki_storage_place``, one
row each (its container, its position, who has it checked out), and every change as an
append-only ledger, ``iki_storage_event``: who, how, when and why. A request's receipts
check what came against what was expected, and a reconciliation what's scanned in a box
against what's recorded there.
"""
