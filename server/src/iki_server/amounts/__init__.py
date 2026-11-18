"""How much there is of each record that's a thing in the lab (ADR 0139).

A type with an ``amount`` keeps how much of each of its records there is in
``iki_amount``, in its unit, as an exact decimal never below zero. It changes only by
drawing some, transferring some to another record, splitting it into aliquots, or
adjusting it, each a movement of the append-only ledger ``iki_amount_movement``: who,
how, when, why, how much was asked for and what it came to. Two people drawing at once
take turns, so neither draws what the other took.
"""
