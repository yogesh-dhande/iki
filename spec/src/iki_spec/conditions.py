"""What a search's conditions can say: each kind of field's operators, and what it can be
sorted by (ADR 0037). The server's searches run them; the validator checks the searches
components declare (ADR 0117) against the same table, so the two can't drift."""

from __future__ import annotations

from collections.abc import Mapping

__all__ = [
    "ALL_OPERATORS",
    "BUILT_IN",
    "BY_FORMAT",
    "MAX_CONDITIONS",
    "MAX_DEPTH",
    "OPERATORS",
    "SORTABLE",
    "WORDS_SEARCHED",
    "operators",
]

#: The most a component's search may say: its declared search, and a refinement of it
#: each. Together, under the ``all`` that joins them, they stay within what the server's
#: searches take (50 conditions, 6 levels), so a refinement can't make one too big.
MAX_CONDITIONS = 20
MAX_DEPTH = 4

EQUALITY = ("eq", "ne", "in")
ORDER = ("lt", "lte", "gt", "gte")

#: The operators each kind of field takes, besides ``is_set``, which all do.
OPERATORS: Mapping[str, tuple[str, ...]] = {
    "string": (*EQUALITY, "contains", "starts_with"),
    "text": ("contains", "matches"),
    "enum": EQUALITY,
    "int": (*EQUALITY, *ORDER),
    "float": (*EQUALITY, *ORDER),
    "quantity": (*EQUALITY, *ORDER),
    "date": (*EQUALITY, *ORDER),
    "datetime": (*EQUALITY, *ORDER),
    "bool": ("eq",),
    "ref": EQUALITY,
    # A person, by their ID, or `{{ me }}` (ADR 0122).
    "person": EQUALITY,
    "file": ("eq", "ne"),
    "json": (),
    "list": ("has",),
    # Columns every entity has.
    "user": EQUALITY,
    "project": EQUALITY,
    "type": EQUALITY,
    # How it was made (ADR 0106): one of its makings has every key the value gives.
    "making": ("has",),
    # Across the graph (ADR 0123): upstream or downstream `of` an entity.
    "graph": ("of",),
    # What plugins' rules caught (ADR 0127): by a rule's name, or by any with `is_set`.
    "rule": EQUALITY,
}
#: What a string field takes besides, by its format: a sequence's ``motif`` (ADR 0175),
#: its bases or residues in IUPAC codes, found on a duplex's other strand too, and across a
#: circle's origin.
BY_FORMAT: Mapping[str, tuple[str, ...]] = {
    "dna_sequence": ("motif",),
    "rna_sequence": ("motif",),
    "protein_sequence": ("motif",),
}
ALL_OPERATORS = (
    "is_set",
    *dict.fromkeys(op for ops in (*OPERATORS.values(), *BY_FORMAT.values()) for op in ops),
)


#: How much of a text field's value its words are searched in (ADR 0233): its first this
#: many characters. Postgres keeps a value's words in a tsvector of at most 1 MiB, and
#: refuses a bigger one; a character adds at most 8 bytes to it (a hyphenated word of
#: 4-byte letters is kept whole and as its parts), so this many always fit, and a save
#: never fails for its index. It's some 15,000 words of prose, about as far as Postgres
#: keeps words' positions (16,383) for a phrase.
WORDS_SEARCHED = 100_000


def operators(kind: str, format: str | None = None, *, searchable: bool = True) -> tuple[str, ...]:
    """The operators a kind of field takes, besides ``is_set``: a string's, with those its
    format adds; a text field's without ``matches`` when it isn't ``searchable`` (ADR 0233)."""
    if kind == "text" and not searchable:
        return tuple(op for op in OPERATORS[kind] if op != "matches")
    extra = BY_FORMAT.get(format or "", ()) if kind == "string" else ()
    return (*OPERATORS[kind], *extra)


#: Kinds a search can be sorted by.
SORTABLE = frozenset({"string", "enum", "int", "float", "quantity", "date", "datetime", "bool"})

#: The columns every entity has, and the kind of each.
BUILT_IN: Mapping[str, str] = {
    "hrid": "string",
    "type": "type",
    "owner_project": "project",
    # The project a request was sent to (ADR 0132); unset for one not sent, or not a request.
    "sent_to": "project",
    "created_by": "user",
    "created_at": "datetime",
    "updated_at": "datetime",
    "archived_at": "datetime",
    "made": "making",
    # Who's assigned it now (ADR 0122): named in one of its assignment fields that counts in
    # its state, and able to be named there.
    "assigned": "person",
    # Whether one of its assignment fields that counts in its state names someone who can't
    # be named there any more: deactivated, gone from the project, or without its lab role.
    "needs_reassigning": "bool",
    # What it was derived from, and what was derived from it (ADR 0123): `of` an entity,
    # by its ID or HRID, or `{"entity": …, "depth": …, "edges": […]}`.
    "upstream": "graph",
    "downstream": "graph",
    # What plugins' rules caught (ADR 0127): records a rule holds for review now, and those
    # that break a rule they were saved before (one added or changed since, or brought).
    "held": "rule",
    "breaks": "rule",
}
