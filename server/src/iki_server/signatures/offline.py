"""Verifying a bundle's electronic signatures offline, with no deployment (ADR 0126).

``iki bundle verify`` reads the bundle as an import would (``bundles.reading``: every
member's SHA-256, and ``bundle.json``'s signature by a key the reader trusts), then checks
each of ``signatures.ndjson``'s lines against the bundle itself:

- its document is canonical, of its format, and signed by a key the reader trusts: one
  ``bundle.json`` lists (as trusted as the bundle) or one given on the command line;
- the content kept with it is the one its document names, by SHA-256, for its record
  and revision;
- the bundle's own history of the record, at that revision, holds that content: each of
  the record's own fields, and the SHA-256 of each file they name. A record of a registry
  entry's entry fields are bound by the signature alone: the bundle has the entry as it
  is now, not at the revision signed;
- if a revision of the record carries it, that revision is the one after it and sets
  the state it signed for.

No Django: this module, ``proof`` and ``bundles.reading`` run anywhere ``iki`` does.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from iki_server.bundles import reading
from iki_server.signatures import proof
from iki_spec.model import SIGNATURE_MEANINGS


@dataclass
class Verified:
    """One signature, and each check made of it."""

    id: str
    record: str
    hrid: str
    manifestation: str
    status: str
    checks: list[proof.Check] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return all(c.ok for c in self.checks)


def verify(bundle: reading.Bundle, keys: Mapping[str, str] | None = None) -> list[Verified]:
    """Each of the bundle's signatures, checked. ``keys``: more keys to trust, public key
    to the deployment it's trusted as. The keys ``bundle.json`` lists are trusted only if
    the bundle is (``bundle.trusted_as``)."""
    trusted: dict[str, str] = dict(keys or {})
    if bundle.trusted_as is not None:
        for item in bundle.manifest.get("signing_keys", []):
            trusted.setdefault(item["key"], item["deployment"])
    records = {line["id"]: line for line in bundle.lines("entities.ndjson")}
    files = {line["id"]: line["sha256"] for line in bundle.lines("files.ndjson")}
    return [
        _one(line, trusted, records.get(line["record"]), files)
        for line in bundle.lines("signatures.ndjson")
    ]


def _one(
    line: Mapping[str, Any],
    trusted: Mapping[str, str],
    record: Mapping[str, Any] | None,
    files: Mapping[str, str],
) -> Verified:
    document = _parsed(line["document"])
    checks = proof.check_document(line["document"], proof.unb64(line["signature"]), trusted)
    signed_record = document.get("record") or {}
    wrong = [
        name
        for name, (a, b) in {
            "id": (document.get("id"), line["id"]),
            "record": (signed_record.get("id"), line["record"]),
            "revision": (signed_record.get("revision"), line["revision"]),
            "field": ((document.get("change") or {}).get("field"), line["field"]),
            "state": ((document.get("change") or {}).get("to"), line["state"]),
            "key": (document.get("key"), line["key"]),
        }.items()
        if a != b
    ]
    checks.append(
        proof.Check(
            "row",
            not wrong,
            "Its record, revision and change are what was signed."
            if not wrong
            else f"Its {', '.join(wrong)} aren't what was signed.",
        )
    )
    checks.append(proof.check_content(document, line["content"]))
    status = "pending"
    if record is None:
        checks.append(proof.Check("record", False, "The bundle doesn't hold its record."))
    else:
        checks.append(_history_check(line, record, files))
        carried = _carried(line, record, document)
        if carried is not None:
            checks.append(carried)
            status = "carried"
    if line.get("withdrawn") is not None and status != "carried":
        status = "withdrawn"
    hrid = str(signed_record.get("hrid", ""))
    return Verified(
        id=line["id"],
        record=line["record"],
        hrid=record.get("hrid", hrid) if record else hrid,
        manifestation=_manifestation(document),
        status=status,
        checks=checks,
    )


def _history_check(
    line: Mapping[str, Any], record: Mapping[str, Any], files: Mapping[str, str]
) -> proof.Check:
    revision = int(line["revision"])
    latest: dict[str, tuple[int, dict[str, Any]]] = {}
    for h in record.get("history", []):
        number = int(h["revision"])
        if number <= revision and number >= latest.get(h["table"], (0, {}))[0]:
            latest[h["table"]] = (number, h["values"])
    own: dict[str, Any] = {}
    for _, values in latest.values():
        own.update(values)
    signed = _parsed(line["content"])
    signed_values = signed.get("values") or {}
    held = {k: v for k, v in own.items() if v is not None and v != []}
    mine = {k: v for k, v in signed_values.items() if k in own}
    changed = sorted(
        k
        for k in held.keys() | mine.keys()
        if proof.canonical(held.get(k)) != proof.canonical(mine.get(k))
    )
    named = _strings(mine.values())
    signed_files = signed.get("files") or {}
    wrong_files = sorted(
        k for k, sha in signed_files.items() if k in named and files.get(k) not in (None, sha)
    )
    if changed or wrong_files:
        return proof.Check(
            "record",
            False,
            f"The bundle's history of revision {revision} isn't what was signed: "
            + ", ".join([f"`{c}`" for c in changed] + [f"file {f}" for f in wrong_files])
            + " changed.",
        )
    return proof.Check(
        "record", True, f"The bundle's history of revision {revision} holds what was signed."
    )


def _carried(
    line: Mapping[str, Any], record: Mapping[str, Any], document: Mapping[str, Any]
) -> proof.Check | None:
    for r in record.get("revisions", []):
        if line["id"] not in (r.get("signatures") or ()):
            continue
        moved = document.get("change") or {}
        made = r.get("state") or {}
        ok = (int(r["number"]), made.get("field"), made.get("from") or "", made.get("to")) == (
            int(line["revision"]) + 1,
            moved.get("field"),
            moved.get("from") or "",
            moved.get("to"),
        )
        return proof.Check(
            "change",
            ok,
            f"Revision {r['number']} set it to {moved.get('to_label')} with it, as it signed."
            if ok
            else f"Revision {r['number']} carries it, but isn't the change it signed.",
        )
    return None


def _manifestation(document: Mapping[str, Any]) -> str:
    sig = document.get("signature") or {}
    signer = document.get("signer") or {}
    when = str(document.get("signed_at", ""))
    shown = when[:10] + " at " + when[11:19] + " UTC" if len(when) >= 19 else when
    meaning = SIGNATURE_MEANINGS.get(str(sig.get("meaning", "")))
    verb = meaning.verb if meaning else "Signed"
    who = signer.get("name") or signer.get("email")
    return f"{verb} by {who} on {shown}. Meaning: {sig.get('label') or sig.get('meaning_label')}."


def _parsed(text: str) -> dict[str, Any]:
    try:
        found = json.loads(text)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _strings(values: Iterable[Any]) -> set[str]:
    out: set[str] = set()
    for v in values:
        if isinstance(v, str):
            out.add(v)
        elif isinstance(v, list):
            out |= _strings(v)
    return out
