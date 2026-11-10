"""Identity hashes: what makes two entities the same (spec ``iki/v1``).

A type's ``identity`` names fields whose values tell its entities apart. Iki
stores the SHA-256 of a canonical encoding of those values with each
entity, so a duplicate is found with one indexed lookup. Subtypes share the
identity of the type that declares it, so the hash names that type.

The encoding, which other implementations must reproduce byte for byte, is
UTF-8 JSON with sorted keys, no spaces and no escaped non-ASCII characters,
such as ``{"type":"dna.DNASequence","v":1,"values":{"bases":"ACGT"}}``.

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
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import unicodedata
import uuid
from collections.abc import Mapping
from typing import Any

__all__ = ["VERSION", "canonical", "encode", "identity_hash"]

VERSION = 1


def canonical(field_type: str, value: Any) -> str | int | bool:  # noqa: ANN401 - any field value
    """``value`` as the encoding has it. Raises ``ValueError`` for values it can't take."""
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


def encode(type_name: str, values: Mapping[str, tuple[str, Any]]) -> bytes:
    """The canonical bytes for ``values``: field name → (field type, value)."""
    document = {
        "type": type_name,
        "v": VERSION,
        "values": {name: canonical(kind, value) for name, (kind, value) in values.items()},
    }
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def identity_hash(type_name: str, values: Mapping[str, tuple[str, Any]]) -> str:
    """The SHA-256 of `encode`, in lower-case hex: what ``iki_entity.identity_hash`` holds."""
    return hashlib.sha256(encode(type_name, values)).hexdigest()
