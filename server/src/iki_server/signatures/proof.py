"""What a signature signs, and how anyone checks it: without Django, so ``iki bundle verify``
checks a bundle's signatures where no Iki runs (ADR 0126).

A signature is Ed25519, by the deployment's signing key, over a **document**: canonical
JSON (RFC 8785) naming the record, its revision, the change to its state it signs for,
what the signature means, who signed, when, and how they signed in again. The document
names the SHA-256 of the record's **content** at that revision: canonical JSON too, of its
values as its history keeps them, and each quantity's unit. So a signature can't be moved
to another record or revision, and a changed value, by any path, no longer matches what
was signed. What a plugin's migration did to that history since, renaming a field or
converting a quantity to another unit (ADR 0148), and how an import names the people and
registry entries it names (ADR 0096), :func:`migrated` does to the content, so it's
compared with the history as it's kept now. A deployment vouches for that list, for each
signature it holds, with its signing key: :func:`read_through`.

Canonical JSON is RFC 8785's (JCS): object keys sorted by their UTF-16 code units, no
whitespace, strings as ECMAScript's ``JSON.stringify`` writes them, and numbers as
ECMAScript writes them (``5.0`` is ``5``, ``1e21`` is ``1e+21``).
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from iki_server.bundles import keys
from iki_spec.model import SIGNATURE_MEANINGS

#: The format of what a signature signs.
DOCUMENT_FORMAT = "iki/signature/v1"
#: The format of a record's content, whose SHA-256 the document names: v2 names each
#: quantity's unit (ADR 0148); v3 says which of its values are its registry entry's.
CONTENT_FORMAT = "iki/signed-content/v3"
#: The format of what a deployment signs to vouch for what was done to a signed record's
#: history since it was signed (ADR 0148).
READ_THROUGH_FORMAT = "iki/read-through/v1"


def manifestation(document: Mapping[str, Any]) -> str:
    """The printed name, UTC time and meaning read from the signed document."""
    sig = document.get("signature") or {}
    signer = document.get("signer") or {}
    meaning = SIGNATURE_MEANINGS.get(str(sig.get("meaning", "")))
    verb = meaning.verb if meaning else "Signed"
    label = sig.get("label") or (meaning.label if meaning else "")
    who = signer.get("name") or signer.get("email")
    stamp = str(document.get("signed_at", ""))
    try:
        when = dt.datetime.fromisoformat(stamp)
    except ValueError:
        shown = stamp
    else:
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        shown = when.astimezone(dt.UTC).strftime("%Y-%m-%d at %H:%M:%S UTC")
    return f"{verb} by {who} on {shown}. Meaning: {label}."


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
    units: Mapping[str, str],
    files: Mapping[str, str],
    entry_fields: Iterable[str] = (),
) -> dict[str, Any]:
    """A record's content at a revision, as a signature binds it.

    ``values``: each field's value as its history keeps it (a reference or file by its
    ID, a time in ISO 8601), removed fields' included, keyed by field name. Empty ones
    (null, an empty list) are left out, so a field a plugin adds later, empty at the
    signed revision, doesn't change what was signed. ``units``: the UCUM unit each
    quantity among them is kept in, so 1.5 reads as the 1.5 g it was. ``files``: the
    SHA-256 of each file the values name, by ID. ``entry``: for a record of a registry
    entry, the entry's ID and revision, whose values are among ``values``: those named in
    ``entry_fields``. The rest are the record's own, which a bundle's history of it must
    hold, each one.
    """
    kept = {k: v for k, v in values.items() if v is not None and v != []}
    return {
        "format": CONTENT_FORMAT,
        "record": record,
        "type": type_name,
        "revision": revision,
        "entry": dict(entry) if entry is not None else None,
        "entry_fields": sorted(set(entry_fields) & kept.keys()),
        "values": kept,
        "units": {k: u for k, u in units.items() if k in kept},
        "files": dict(files),
    }


class Unexplained(ValueError):  # noqa: N818 - reads as "the content's units are unexplained"
    """Migrations' changes that don't follow from what was signed: a conversion from a unit
    the field wasn't in."""


def migrated(signed: Mapping[str, Any], changes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """``signed`` content as its record's history keeps it after ``changes``: what plugins'
    migrations did to that history since it was signed (ADR 0148), and how imports named
    what it names, in the order they did it. A field renamed is named anew; a quantity
    converted is converted as the migration converted it, ``new = old * scale + offset``,
    so it's the same double; a type renamed is named anew; an import's IDs (``ids``) name
    the people and registry entries of the fields and rows' cells it lists as they are
    where it was imported. Raises :class:`Unexplained` for a conversion from a unit the
    field wasn't in."""
    values = dict(signed.get("values") or {})
    units = dict(signed.get("units") or {})
    entry_fields = set(signed.get("entry_fields") or ())
    out = dict(signed)
    for change in changes:
        kind = change.get("kind")
        if kind == "rename":
            old, new = change["from"], change["to"]
            if old in values:
                values[new] = values.pop(old)
            if old in units:
                units[new] = units.pop(old)
            if old in entry_fields:
                entry_fields = (entry_fields - {old}) | {new}
        elif kind == "ids":
            _named_here(values, change)
        elif kind == "convert":
            name = change["field"]
            if name not in values:
                continue
            if units.get(name) != change["from_unit"]:
                raise Unexplained(
                    f"`{name}` was signed in {units.get(name) or 'no unit'}, but a migration "
                    f"converted it from {change['from_unit']}."
                )
            scale, offset = float(change["scale"]), float(change["offset"])
            value = values[name]
            if isinstance(value, list):
                values[name] = [None if v is None else v * scale + offset for v in value]
            elif value is not None:  # an empty value converts to an empty one
                values[name] = value * scale + offset
            units[name] = change["to_unit"]
        elif kind == "retype" and out.get("type") == change["from"]:
            out["type"] = change["to"]
    out["values"] = values
    out["units"] = units
    if "entry_fields" in signed:
        out["entry_fields"] = sorted(entry_fields)
    return out


def _named_here(values: dict[str, Any], change: Mapping[str, Any]) -> None:
    """An import's IDs (ADR 0096): ``fields``, each field's IDs where the bundle came from
    to its IDs here (a person's, or a registry entry's); ``cells``, the same for each of a
    list of rows' cells (ADR 0137)."""

    def here(value: Any, ids: Mapping[str, str]) -> Any:  # noqa: ANN401 - JSON
        if isinstance(value, list):
            return [ids.get(v, v) if isinstance(v, str) else v for v in value]
        return ids.get(value, value) if isinstance(value, str) else value

    for name, ids in (change.get("fields") or {}).items():
        if name in values:
            values[name] = here(values[name], ids)
    for name, by_cell in (change.get("cells") or {}).items():
        rows = values.get(name)
        if not isinstance(rows, list):
            continue
        values[name] = [
            {k: here(v, by_cell[k]) if k in by_cell else v for k, v in r.items()}
            if isinstance(r, dict)
            else r
            for r in rows
        ]


def described(changes: Sequence[Mapping[str, Any]]) -> str:
    """What was done since, for people: "`name` renamed `title`, `mass` converted from g
    to mg (× 1000), `made_by` naming people and registry entries as staging does"."""
    said = []
    for c in changes:
        if c.get("kind") == "rename":
            said.append(f"`{c['from']}` renamed `{c['to']}`")
        elif c.get("kind") == "convert":
            offset = float(c["offset"])
            how = f"× {_number(float(c['scale']))}" + (
                f" {'+' if offset >= 0 else '-'} {_number(abs(offset))}" if offset else ""
            )
            said.append(f"`{c['field']}` converted from {c['from_unit']} to {c['to_unit']} ({how})")
        elif c.get("kind") == "retype":
            said.append(f"its type renamed {c['to']}")
        elif c.get("kind") == "ids":
            names = [f"`{n}`" for n in sorted({*(c.get("fields") or {}), *(c.get("cells") or {})})]
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            said.append(
                f"{listed} naming people and registry entries as {c.get('here') or 'it'} does"
            )
    return ", ".join(said)


# --- Vouching for what was done since -----------------------------------------------------------


def read_through(
    *,
    signature: Mapping[str, Any],
    changes: Sequence[Mapping[str, Any]],
    deployment: str,
    key: str,
    made_at: str,
) -> dict[str, Any]:
    """What a deployment signs to vouch, with its signing key, for what was done to a signed
    record's history since it was signed, in order (``changes``, for :func:`migrated`):
    canonical JSON naming the signature, its record and revision. ``signature``: the
    signature's ``id``, ``record`` and ``revision``."""
    return {
        "format": READ_THROUGH_FORMAT,
        "signature": str(signature["id"]),
        "record": str(signature["record"]),
        "revision": int(signature["revision"]),
        "deployment": deployment,
        "key": key,
        "made_at": made_at,
        "changes": [dict(c) for c in changes],
    }


def vouched(
    given: Mapping[str, Any] | None, signature: Mapping[str, Any], trusted: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The changes ``given`` (a signed :func:`read_through`: its ``document``, ``key`` and
    ``signature``) vouches for, once its key is one of ``trusted`` (public key → the
    deployment it's trusted as), the key signed it, and it's for ``signature`` (its ``id``,
    ``record`` and ``revision``). Nothing if none is given. Raises :class:`Unexplained`,
    saying why, if it doesn't check out: then nothing explains a difference."""
    if not given:
        return []
    if not isinstance(given, Mapping):
        raise Unexplained("what was done to its history since it was signed isn't readable.")
    text = given.get("document")
    try:
        document = json.loads(text) if isinstance(text, str) else None
        canon = canonical(document) if isinstance(document, dict) else ""
    except (ValueError, NotCanonical):
        document, canon = None, ""
    if not isinstance(document, dict) or canon != text:
        raise Unexplained("what was done to its history since it was signed isn't readable.")
    key = document.get("key")
    if (
        document.get("format") != READ_THROUGH_FORMAT
        or key != given.get("key")
        or (document.get("signature"), document.get("record"), document.get("revision"))
        != (str(signature["id"]), str(signature["record"]), int(signature["revision"]))
    ):
        raise Unexplained("what's said to have been done to its history since is another's.")
    if not isinstance(key, str) or key not in trusted:
        raise Unexplained(
            "what was done to its history since it was signed is vouched for by "
            f"{document.get('deployment')}'s signing key {key}, which isn't trusted."
        )
    raw = given.get("signature")
    if not isinstance(raw, str) or not keys.verify(key, unb64(raw), text.encode()):
        raise Unexplained(
            "what was done to its history since it was signed doesn't match its signature."
        )
    changes = document.get("changes")
    if not isinstance(changes, list) or not all(isinstance(c, dict) for c in changes):
        raise Unexplained("what was done to its history since it was signed isn't readable.")
    return changes


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


def review_reference(
    record_id: str,
    revision: int,
    field: str,
    state: str,
    documents: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """The evidence for a signed release, from the signatures that set the preceding
    review state. Every signature on that transition must name the same record, source
    revision and content. A reviewer's meaning identifies the reviews among them."""
    digest = None
    reviews = []
    for document in documents:
        record = document.get("record") or {}
        change = document.get("change") or {}
        if (
            (record.get("id"), record.get("revision"), change.get("field"), change.get("to"))
            != (record_id, revision - 1, field, state)
            or not record.get("content_sha256")
            or (digest is not None and record["content_sha256"] != digest)
        ):
            return None
        digest = record["content_sha256"]
        if (document.get("signature") or {}).get("meaning") == "review":
            if not isinstance(document.get("id"), str):
                return None
            reviews.append(document["id"])
    if not reviews:
        return None
    return {"revision": revision - 1, "content_sha256": digest, "signatures": reviews}


def review_check(claim: Any, reference: Mapping[str, Any] | None) -> Check:  # noqa: ANN401 - JSON
    ok = reference is not None and claim == reference
    return Check(
        "review",
        ok,
        "The release identifies the revision, content and signatures of the preceding review."
        if ok
        else "The release's reviewed revision, content or signatures do not match its review.",
    )


def differences(signed: Mapping[str, Any], now: Mapping[str, Any]) -> list[str]:
    """The fields whose values, or units, differ between two contents, sorted."""
    out = set()
    for part in ("values", "units"):
        a, b = signed.get(part) or {}, now.get(part) or {}
        out |= {k for k in a.keys() | b.keys() if canonical(a.get(k)) != canonical(b.get(k))}
    return sorted(out)
