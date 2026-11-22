"""Verifying a bundle's electronic signatures offline, with no deployment (ADR 0126).

``iki bundle verify`` reads the bundle as an import would (``bundles.reading``: every
member's SHA-256, and ``bundle.json``'s signature by a key the reader trusts), then checks
each of ``signatures.ndjson``'s lines against the bundle itself:

- its document is canonical, of its format, and signed by a key the reader trusts: one
  ``bundle.json`` lists (as trusted as the bundle) or one given on the command line;
- the content kept with it is the one its document names, by SHA-256, for its record
  and revision;
- the bundle's own history of the record, at that revision, holds that content: each of
  the record's own fields signed (a value it doesn't hold there differs from it), and
  the SHA-256 of each file they name, once what was done to that history since it was
  signed is done to the content (``since``: fields renamed, quantities converted, ADR
  0148, and people and registry entries named as an import named them, ADR 0096). That
  list is vouched for by the signing key of the deployment that exported it, which must
  be one the reader trusts, as the signature's is: nothing else explains a difference.
  A record of a registry entry's entry fields, which its content names, are bound by the
  signature alone: the bundle has the entry as it is now, not at the revision signed;
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
    lines: dict[str, dict[str, Any]] = {}
    for line in bundle.lines("signatures.ndjson"):
        if line["id"] in lines:
            raise reading.BundleError(f"Signature {line['id']} appears twice in the bundle.")
        lines[line["id"]] = line
    verified = {
        sid: _one(line, trusted, records.get(line["record"]), files) for sid, line in lines.items()
    }
    # A QA signature additionally vouches for an earlier review. Check earlier revisions
    # first so every referenced signature has already had all of its own checks.
    for line in sorted(lines.values(), key=lambda item: item["revision"]):
        document = _parsed(line["document"])
        if "reviewed" not in document:
            continue
        record = records.get(line["record"]) or {}
        number = line["revision"]
        revisions = {r["number"]: r for r in record.get("revisions", [])}
        review = revisions.get(number, {})
        state = review.get("state") or {}
        carried = review.get("signatures") or []
        change = document.get("change") or {}
        history_field = line["field"]
        # ADR 0148. The state column in the bundle's history can have a new name.
        try:
            for migration in proof.vouched(line.get("since"), line, trusted):
                if migration.get("kind") == "rename" and migration.get("from") == history_field:
                    history_field = migration["to"]
        except (KeyError, proof.Unexplained):
            continue  # Its record check already refused the unexplained history.
        reference = None
        if (
            review.get("kind") == "state_changed"
            and state.get("field") == change.get("field")
            and state.get("to") == change.get("from")
            and review.get("changed_fields") == [line["field"]]
            and all(sid in verified and verified[sid].valid for sid in carried)
            and _only_state_changed(record, number, history_field, state)
        ):
            reference = proof.review_reference(
                line["record"],
                line["revision"],
                line["field"],
                state["to"],
                [_parsed(lines[sid]["document"]) for sid in carried],
            )
        if verified[line["id"]].status == "carried":
            delivered = revisions.get(number + 1, {})
            if (
                delivered.get("kind") != "state_changed"
                or delivered.get("changed_fields") != [line["field"]]
                or not _only_state_changed(record, number + 1, history_field, change)
            ):
                reference = None
        verified[line["id"]].checks.append(proof.review_check(document["reviewed"], reference))
    return list(verified.values())


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
        checks.append(_history_check(line, record, files, trusted))
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


def _values_at(record: Mapping[str, Any], revision: int) -> dict[str, Any]:
    latest: dict[str, tuple[int, dict[str, Any]]] = {}
    for h in record.get("history", []):
        number = int(h["revision"])
        if number <= revision and number >= latest.get(h["table"], (0, {}))[0]:
            latest[h["table"]] = (number, h["values"])
    own: dict[str, Any] = {}
    for _, values in latest.values():
        own.update(values)
    return own


def _only_state_changed(
    record: Mapping[str, Any], number: int, field: str, change: Mapping[str, Any]
) -> bool:
    before, after = _values_at(record, number - 1), _values_at(record, number)
    if before.get(field) != change.get("from"):
        return False
    before[field] = change.get("to")
    return proof.canonical(before) == proof.canonical(after)


def _history_check(
    line: Mapping[str, Any],
    record: Mapping[str, Any],
    files: Mapping[str, str],
    trusted: Mapping[str, str],
) -> proof.Check:
    revision = int(line["revision"])
    own = _values_at(record, revision)
    try:
        since = proof.vouched(line.get("since"), line, trusted)
        signed = proof.migrated(_parsed(line["content"]), since)
    except proof.Unexplained as unexplained:
        return proof.Check(
            "record",
            False,
            f"The bundle's history of revision {revision} can't be said to be what was "
            f"signed: {unexplained}",
        )
    except (ValueError, TypeError, KeyError):
        signed = {}
    entry_fields = set(signed.get("entry_fields") or ())
    held = {k: v for k, v in own.items() if v is not None and v != [] and k not in entry_fields}
    mine = {k: v for k, v in (signed.get("values") or {}).items() if k not in entry_fields}
    changed = sorted(
        k
        for k in held.keys() | mine.keys()
        if proof.canonical(held.get(k)) != proof.canonical(mine.get(k))
    )
    named = _strings(mine.values())
    signed_files = signed.get("files") or {}
    wrong_files = sorted(k for k, sha in signed_files.items() if k in named and files.get(k) != sha)
    if changed or wrong_files:
        return proof.Check(
            "record",
            False,
            f"The bundle's history of revision {revision} isn't what was signed: "
            + ", ".join([f"`{c}`" for c in changed] + [f"file {f}" for f in wrong_files])
            + " changed.",
        )
    return proof.Check(
        "record",
        True,
        f"The bundle's history of revision {revision} holds what was signed"
        + (
            f", read through what was done to it since: {proof.described(since)}." if since else "."
        ),
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
    """Every string among ``values``, in lists and rows (ADR 0137) too."""
    out: set[str] = set()
    for v in values:
        if isinstance(v, str):
            out.add(v)
        elif isinstance(v, list):
            out |= _strings(v)
        elif isinstance(v, dict):
            out |= _strings(v.values())
    return out
