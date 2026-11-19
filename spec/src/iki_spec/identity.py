"""Identity hashes: what makes two entities the same (spec ``iki/v1``).

A type's ``identity`` names fields whose values tell its entities apart. Iki
stores the SHA-256 of a canonical encoding of those values with each
entity, so a duplicate is found with one indexed lookup. Subtypes share the
identity of the type that declares it, so the hash names that type.

The encoding, which other implementations must reproduce byte for byte, is
UTF-8 JSON with sorted keys, no spaces and no escaped non-ASCII characters,
such as ``{"type":"iki/dna.DNASequence","v":1,"values":{"bases":"ACGT"}}``.

Each value is encoded by its field's type:

=============================  ===============================================
``string``, ``text``, ``enum``  the text, in Unicode NFC
``int``                         a JSON integer
``bool``                        ``true`` or ``false``
``date``                        ``"YYYY-MM-DD"``
``datetime``                    UTC, ``"YYYY-MM-DDTHH:MM:SS.ffffffZ"``
``ref``                         the entity's UUID, lower case
``file``                        the SHA-256 of the file's content, lower-case hex
=============================  ===============================================

An optional field without a value is ``null``: an antibody's light chain, which a
single-domain antibody hasn't got. Every identity has a required field too.

Version 2 (ADR 0138) reads sequences and lists as what they are, not as they're
written:

- A string with a sequence format (``dna_sequence``, ``rna_sequence``,
  ``protein_sequence``) is upper case: lower case marks masked regions, not other
  residues. A protein's terminal stops (``*`` at its end) go, as a stop ends a chain
  rather than being a residue of it, so MKWV, mkwv and MKWV* are one protein; a stop
  inside it stays, so MK*WV isn't MKWV. DNA keeps T and RNA keeps U: each format has its
  own alphabet, and a DNA and an RNA are different molecules.
- A sequence the field's ``topology`` field says is ``circular`` is its least rotation,
  so a plasmid entered from another origin is the same plasmid.
- A DNA or RNA sequence the field's ``strands`` field says is ``double`` is the lesser
  of its two strands, each read 5' to 3' (each the least of its rotations, if
  circular), so a duplex entered as its other strand is the same duplex. A single
  strand is only itself (and its rotations): its complement is another molecule.
- A string with the ``helm`` format is its canonical HELM (ADR 0144), as iki-wasm writes
  it: the same molecule however it's written, and another molecule if a monomer differs.
- A list (of references, or rows) is a multiset: its items sorted, duplicates kept, so
  a complex's parts are the same whatever their order. A row is an object of its
  identity fields.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from iki_spec.model import Field

__all__ = [
    "VERSION",
    "Kind",
    "Placement",
    "canonical",
    "encode",
    "identity_hash",
    "kind_of",
    "place",
    "sequence",
]

VERSION = 2
#: The field types an identity can name: they compare exactly.
IDENTITY_TYPES = frozenset(
    {"string", "text", "enum", "int", "bool", "date", "datetime", "ref", "file"}
)
#: The formats read as sequences, and those with two strands.
SEQUENCE_FORMATS = frozenset({"dna_sequence", "rna_sequence", "protein_sequence"})
NUCLEIC_FORMATS = frozenset({"dna_sequence", "rna_sequence"})
#: Each IUPAC nucleotide code's complement, upper case.
_COMPLEMENT = {
    "dna_sequence": str.maketrans("ACGTRYSWKMBDHVN", "TGCAYRSWMKVHDBN"),
    "rna_sequence": str.maketrans("ACGURYSWKMBDHVN", "UGCAYRSWMKVHDBN"),
}

type Encoded = str | int | bool | list[Any] | dict[str, Any] | None


@dataclass(frozen=True)
class Kind:
    """How an identity value is read: its field's type, and for a sequence, its format
    and whether this one is circular and double-stranded; for a list, its items'; for a
    row, its identity fields'."""

    type: str
    format: str | None = None
    circular: bool = False
    double: bool = False
    items: Kind | None = None
    row: tuple[tuple[str, Kind], ...] = ()


def kind_of(f: Field, values: Mapping[str, Any]) -> Kind:
    """``f``'s kind, reading its ``topology`` and ``strands`` fields from ``values``
    (enum values, as stored)."""
    if f.type == "list" and f.items is not None:
        item = f.items
        if item.type == "row":
            names = item.identity or tuple(item.fields)
            return Kind(
                "list",
                items=Kind("row", row=tuple((n, kind_of(item.fields[n], {})) for n in names)),
            )
        return Kind("list", items=kind_of(item, {}))
    if f.format == "helm":
        return Kind(f.type, format="helm")
    if f.format in SEQUENCE_FORMATS:
        return Kind(
            f.type,
            format=f.format,
            circular=f.topology is not None and values.get(f.topology) == "circular",
            double=f.strands is not None and values.get(f.strands) == "double",
        )
    return Kind(f.type)


def sequence(format: str, text: str, *, circular: bool = False, double: bool = False) -> str:
    """A sequence as its identity reads it (see above)."""
    out = text.upper()
    if format == "protein_sequence":
        if double:
            raise ValueError("a protein has no second strand")
        out = out.rstrip("*")
    elif format not in NUCLEIC_FORMATS:
        raise ValueError(f"{format} isn't a sequence format")
    if not double:
        return _least_rotation(out) if circular else out
    other = out.translate(_COMPLEMENT[format])[::-1]
    if circular:
        return min(_least_rotation(out), _least_rotation(other))
    return min(out, other)


@dataclass(frozen=True)
class Placement:
    """Where a sequence lies in another that's the same molecule: read from its other
    strand if ``reverse``, then from ``offset`` (counting from 0) on."""

    offset: int = 0
    reverse: bool = False

    def position(self, at: int, length: int) -> int:
        """A position (counting from 1) in the sequence, in the other."""
        if self.reverse:
            at = length + 1 - at
        return (at - 1 - self.offset) % length + 1


def place(
    format: str,
    given: str,
    stored: str,
    *,
    circular: bool = False,
    double: bool = False,
) -> Placement | None:
    """How ``given`` lies in ``stored``, if they're the same molecule as their identity
    reads them: so what's given along it (its features) can be placed along the stored."""
    one, other = given.upper(), stored.upper()
    if format == "protein_sequence":
        one, other = one.rstrip("*"), other.rstrip("*")
    if len(one) != len(other):
        return None
    strands = [(False, one)]
    if double and format in NUCLEIC_FORMATS:
        strands.append((True, one.translate(_COMPLEMENT[format])[::-1]))
    for reverse, strand in strands:
        if circular:
            found = (strand + strand).find(other) if other else 0
            if found != -1 and found < max(len(strand), 1):
                return Placement(found, reverse)
        elif strand == other:
            return Placement(0, reverse)
    return None


def _least_rotation(text: str) -> str:
    """The lexicographically least rotation of ``text`` (Booth's algorithm, linear time)."""
    n = len(text)
    if n < 2:
        return text
    doubled = text + text
    failure = [-1] * (2 * n)
    least = 0
    for j in range(1, 2 * n):
        c = doubled[j]
        i = failure[j - least - 1]
        while i != -1 and c != doubled[least + i + 1]:
            if c < doubled[least + i + 1]:
                least = j - i - 1
            i = failure[i]
        if c != doubled[least + i + 1]:  # i == -1
            if c < doubled[least]:
                least = j
            failure[j - least] = -1
        else:
            failure[j - least] = i + 1
    return doubled[least : least + n]


def canonical(kind: str | Kind, value: Any) -> Encoded:  # noqa: ANN401 - any value
    """``value`` as the encoding has it. Raises ``ValueError`` for values it can't take."""
    if isinstance(kind, Kind):
        if kind.type == "list":
            return _multiset(kind, value)
        if kind.format is not None and value is not None:
            if not isinstance(value, str):
                raise ValueError(f"a sequence identity value must be text, not {value!r}")
            if kind.format == "helm":
                from iki_spec import helm  # iki-wasm, loaded only for HELM

                return helm.canonical(value)
            return sequence(kind.format, value, circular=kind.circular, double=kind.double)
        return canonical(kind.type, value)
    field_type = kind
    if field_type == "list":
        raise ValueError("a list's identity needs its items' kind")
    if value is None:
        if field_type not in IDENTITY_TYPES:
            raise ValueError(f"{field_type} fields can't be part of an identity")
        return None
    if field_type in ("string", "text", "enum"):
        if not isinstance(value, str):
            raise ValueError(f"a {field_type} identity value must be text, not {value!r}")
        return unicodedata.normalize("NFC", value)
    if field_type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"an int identity value must be a whole number, not {value!r}")
        return value
    if field_type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"a bool identity value must be true or false, not {value!r}")
        return value
    if field_type == "datetime":
        if not isinstance(value, dt.datetime) or value.tzinfo is None:
            raise ValueError(f"a datetime identity value needs a time zone, not {value!r}")
        return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if field_type == "date":
        if not isinstance(value, dt.date) or isinstance(value, dt.datetime):
            raise ValueError(f"a date identity value must be a date, not {value!r}")
        return value.isoformat()
    if field_type == "ref":
        return str(uuid.UUID(str(value)))
    if field_type == "file":
        text = str(value).lower()
        if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
            raise ValueError(f"a file identity value is its content's SHA-256, not {value!r}")
        return text
    raise ValueError(f"{field_type} fields can't be part of an identity")


def _multiset(kind: Kind, value: Any) -> list[Any]:  # noqa: ANN401 - a list
    """A list's items, each encoded, in the order of their encodings: duplicates kept."""
    item = kind.items
    if item is None:
        raise ValueError("a list's identity needs its items' kind")
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"a list identity value must be a list, not {value!r}")
    if item.type == "row":
        encoded: list[Any] = []
        for row in value:
            if not isinstance(row, Mapping):
                raise ValueError(f"a row identity value must be an object, not {row!r}")
            encoded.append({n: canonical(k, row.get(n)) for n, k in item.row})
    else:
        encoded = [canonical(item, v) for v in value]
    return sorted(encoded, key=_text)


def _text(value: Any) -> str:  # noqa: ANN401 - JSON
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def encode(type_name: str, values: Mapping[str, tuple[str | Kind, Any]]) -> bytes:
    """The canonical bytes for ``values``: field name → (its kind or type, value)."""
    document = {
        "type": type_name,
        "v": VERSION,
        "values": {name: canonical(kind, value) for name, (kind, value) in values.items()},
    }
    return _text(document).encode("utf-8")


def identity_hash(type_name: str, values: Mapping[str, tuple[str | Kind, Any]]) -> str:
    """The SHA-256 of `encode`, in lower-case hex: what ``iki_entity.identity_hash`` holds."""
    return hashlib.sha256(encode(type_name, values)).hexdigest()
