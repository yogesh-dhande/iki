"""What a signature signs, and how anyone checks it: without Django, so ``iki bundle verify``
checks a bundle's signatures where no Iki runs (ADR 0126).

A signature is Ed25519, by the deployment's signing key, over a **document**: canonical
JSON (RFC 8785) naming the record, its revision, the change to its state it signs for,
what the signature means, who signed, when, and how they signed in again. The document
names the SHA-256 of the record's **content** at that revision: canonical JSON too, of its
values as its history keeps them. So a signature can't be moved to another record or
revision, and a changed value, by any path, no longer matches what was signed.

Canonical JSON is RFC 8785's (JCS): object keys sorted by their UTF-16 code units, no
whitespace, strings as ECMAScript's ``JSON.stringify`` writes them, and numbers as
ECMAScript writes them (``5.0`` is ``5``, ``1e21`` is ``1e+21``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from iki_server.bundles import keys

#: The format of what a signature signs.
DOCUMENT_FORMAT = "iki/signature/v1"
#: The format of a record's content, whose SHA-256 the document names.
CONTENT_FORMAT = "iki/signed-content/v1"


class NotCanonical(ValueError):  # noqa: N818 - reads as "the JSON is not canonical"
    """A value canonical JSON can't hold: NaN, infinity, or a key that isn't a string."""


def canonical(value: Any) -> str:  # noqa: ANN401 - any JSON value
    """``value`` as RFC 8785 canonical JSON."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return _integer(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise NotCanonical("Canonical JSON's keys are strings.")
        ordered = sorted(value, key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(f"{canonical(k)}:{canonical(value[k])}" for k in ordered) + "}"
    raise NotCanonical(f"Canonical JSON can't hold a {type(value).__name__}.")


def canonical_bytes(value: Any) -> bytes:  # noqa: ANN401 - any JSON value
    return canonical(value).encode()


def _integer(value: int) -> str:
    # ECMAScript numbers are doubles: integers beyond 2**53 are written as the double.
    return str(value) if abs(value) <= 2**53 else _number(float(value))


def _number(value: float) -> str:
    """As ECMAScript's Number::toString writes a double (RFC 8785 section 3.2.2.3)."""
    if not math.isfinite(value):
        raise NotCanonical("Canonical JSON can't hold NaN or infinity.")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    # Python's repr is the shortest string that reads back as the same double, as
    # ECMAScript's is: only the layout differs.
    digits_tuple, exponent = Decimal(repr(abs(value))).as_tuple()[1:]
    digits = "".join(map(str, digits_tuple)).rstrip("0") or "0"
    assert isinstance(exponent, int)  # noqa: S101 - finite
    exponent += len("".join(map(str, digits_tuple))) - len(digits)
    k = len(digits)
    n = exponent + k  # the value is 0.digits * 10**n
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * -n + digits
    e = n - 1
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    return f"{sign}{mantissa}e{'+' if e >= 0 else '-'}{abs(e)}"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return b""


# --- Content ------------------------------------------------------------------------------------


def content(
    *,
    record: str,
    type_name: str,
    revision: int,
    entry: Mapping[str, Any] | None,
    values: Mapping[str, Any],
    files: Mapping[str, str],
) -> dict[str, Any]:
    """A record's content at a revision, as a signature binds it.

    ``values``: each field's value as its history keeps it (a reference or file by its
    ID, a time in ISO 8601), removed fields' included, keyed by field name. Empty ones
    (null, an empty list) are left out, so a field a plugin adds later, empty at the
    signed revision, doesn't change what was signed. ``files``: the SHA-256 of each file
    the values name, by ID. ``entry``: for a record of a registry entry, the entry's ID
    and revision, whose values are among ``values``.
    """
    return {
        "format": CONTENT_FORMAT,
        "record": record,
        "type": type_name,
        "revision": revision,
        "entry": dict(entry) if entry is not None else None,
        "values": {k: v for k, v in values.items() if v is not None and v != []},
        "files": dict(files),
    }


# --- Checking -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One thing a verification checks, and what it found."""

    name: str
    ok: bool
    detail: str


def check_document(document: str, signature: bytes, trusted: Mapping[str, str]) -> list[Check]:
    """Whether ``document`` reads as a signature's, and its key, which must be one of
    ``trusted`` (public key → the deployment it's trusted as), signed it."""
    try:
        parsed = json.loads(document)
    except ValueError:
        return [Check("document", False, "What was signed isn't JSON.")]
    if not isinstance(parsed, dict) or parsed.get("format") != DOCUMENT_FORMAT:
        return [Check("document", False, f"What was signed isn't a {DOCUMENT_FORMAT} document.")]
    try:
        canon = canonical(parsed)
    except NotCanonical:
        canon = ""
    if canon != document:
        return [Check("document", False, "What was signed isn't canonical JSON.")]
    key = parsed.get("key")
    out: list[Check] = []
    if not isinstance(key, str) or key not in trusted:
        out.append(
            Check("key", False, f"It's signed by a key this deployment doesn't trust: {key}.")
        )
    else:
        out.append(Check("key", True, f"Signed by {trusted[key]}'s key {key}."))
    if isinstance(key, str) and keys.verify(key, signature, document.encode()):
        out.append(Check("signature", True, "Its Ed25519 signature matches what was signed."))
    else:
        out.append(
            Check(
                "signature",
                False,
                "Its Ed25519 signature doesn't match what was signed: one or the other changed.",
            )
        )
    return out


def check_content(document: Mapping[str, Any], content_text: str) -> Check:
    """Whether ``content_text`` is the content the document names, by its SHA-256."""
    record = document.get("record") or {}
    if sha256(content_text) != record.get("content_sha256"):
        return Check(
            "content",
            False,
            "The content kept with it isn't what was signed: its SHA-256 differs.",
        )
    try:
        parsed = json.loads(content_text)
    except ValueError:
        parsed = None
    entry = record.get("entry")
    if not isinstance(parsed, dict) or (
        parsed.get("format"),
        parsed.get("record"),
        parsed.get("revision"),
        parsed.get("entry"),
    ) != (CONTENT_FORMAT, record.get("id"), record.get("revision"), entry):
        return Check(
            "content",
            False,
            "The content signed isn't the record's, at the revision the signature names.",
        )
    return Check("content", True, "The content kept with it is what was signed.")


def differences(signed: Mapping[str, Any], now: Mapping[str, Any]) -> list[str]:
    """The fields whose values differ between two contents' ``values``, sorted."""
    a, b = signed.get("values") or {}, now.get("values") or {}
    return sorted(k for k in a.keys() | b.keys() if canonical(a.get(k)) != canonical(b.get(k)))
