"""The small_molecule plugin's ChEMBL fetch: `identifiers` names compounds, one a line, by
ChEMBL ID, InChIKey or name.

For each, it asks ChEMBL's web services for the molecule (a name is looked up as its
preferred name, then as a synonym), and registers its structure as a compound,
standardised and identified as `chemistry` says, from ChEMBL's canonical SMILES, with its
preferred name, synonyms and ChEMBL ID. Each records the molecule's ChEMBL record, the
ChEMBL release it's from, its link, when it was fetched and the SHA-256 of what ChEMBL
gave.

When ChEMBL says to slow down (429) or fails on its side (5xx), it waits and asks again.
A name that's several molecules takes the first, saying so. All or nothing: an ID,
InChIKey or name ChEMBL doesn't have, or a molecule without a structure (a biologic),
fails the run, naming it.

The fetch reads ChEMBL's record and computes nothing of its own, so the action lists no
software (ADR 0106). ChEMBL's data is CC BY-SA 3.0.
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from chemistry import (
    LIMITS,
    MAX_SYNONYMS,
    Creates,
    Messages,
    UnreadableError,
    compound_values,
    cut,
    fail,
    from_smiles,
    run_input,
    standardise,
    write_output,
)
from fetching import ANY_INCHIKEY, NotFound, get_json, identifiers

API = "https://www.ebi.ac.uk/chembl/api/data"
AGENT = "iki-small-molecule"
PAUSE = 0.2
SERVICE = "ChEMBL"
CHEMBL_ID = re.compile(r"^CHEMBL[1-9][0-9]*$", re.IGNORECASE)


def release() -> str:
    """The ChEMBL release its web services serve, such as ChEMBL_37."""
    try:
        found, _ = get_json(f"{API}/status.json", SERVICE, AGENT)
    except NotFound:
        return ""
    return str(found.get("chembl_db_version") or "")


def by_name(name: str) -> tuple[str, int]:
    """The ChEMBL ID a name is, as its preferred name or else a synonym, and how many
    molecules it names."""
    for field in ("pref_name__iexact", "molecule_synonyms__molecule_synonym__iexact"):
        query = urllib.parse.urlencode({field: name, "only": "molecule_chembl_id", "limit": 20})
        found, _ = get_json(f"{API}/molecule.json?{query}", SERVICE, AGENT)
        molecules = found.get("molecules") or []
        if molecules:
            ids = list(dict.fromkeys(str(m["molecule_chembl_id"]) for m in molecules))
            return ids[0], len(ids)
        time.sleep(PAUSE)
    raise UnreadableError(
        f"ChEMBL has no molecule named {name}. Search for it at https://www.ebi.ac.uk/chembl."
    )


def molecule(identifier: str, messages: Messages) -> tuple[dict[str, Any], bytes]:
    if CHEMBL_ID.match(identifier) or ANY_INCHIKEY.match(identifier.upper()):
        key = identifier.upper()
    else:
        key, count = by_name(identifier)
        if count > 1:
            messages.add(
                "warning",
                f"{identifier} names {count} molecules in ChEMBL: {key} was fetched.",
            )
        time.sleep(PAUSE)
    try:
        return get_json(f"{API}/molecule/{urllib.parse.quote(key)}.json", SERVICE, AGENT)
    except NotFound:
        named = key if key == identifier.upper() else f"{key} (for {identifier})"
        raise UnreadableError(
            f"ChEMBL has no molecule {named}. Search for it at https://www.ebi.ac.uk/chembl."
        ) from None


def fetch(run: dict[str, Any]) -> dict[str, Any]:
    params = run.get("params") or {}
    parent = bool(params.get("parent", False))
    asked = identifiers(params.get("identifiers") or "", "CHEMBL25, aspirin or an InChIKey")
    version = release()
    creates = Creates(inline=True)
    messages = Messages()
    seen: set[str] = set()
    for done, identifier in enumerate(asked, start=1):
        if done > 1:
            time.sleep(PAUSE)
        found, raw = molecule(identifier, messages)
        chembl_id = str(found["molecule_chembl_id"])
        if chembl_id in seen:
            continue
        seen.add(chembl_id)
        smiles = (found.get("molecule_structures") or {}).get("canonical_smiles")
        if not smiles:
            kind = str(found.get("molecule_type") or "unknown")
            raise UnreadableError(
                f"ChEMBL's {chembl_id} ({identifier}) has no structure to register (its "
                f"molecule type is {kind})"
            )
        try:
            registered = standardise(from_smiles(smiles), parent)
        except UnreadableError as why:
            raise UnreadableError(f"ChEMBL's {chembl_id} ({identifier}): {why}") from None
        values = compound_values(registered, str(found.get("pref_name") or ""))
        stripped = registered.as_drawn != registered.smiles
        hierarchy = found.get("molecule_hierarchy") or {}
        if parent and stripped:
            messages.add(
                "warning",
                f"{chembl_id} ({identifier}) is {registered.as_drawn}; its parent was "
                "registered, which isn't that molecule, so it doesn't take its ChEMBL ID"
                + (
                    f" (ChEMBL's parent is {hierarchy['parent_chembl_id']})."
                    if hierarchy.get("parent_chembl_id")
                    and hierarchy["parent_chembl_id"] != chembl_id
                    else "."
                ),
            )
        else:
            values["chembl_id"] = chembl_id
        names = [
            cut(str(s["molecule_synonym"]), LIMITS["name"])
            for s in found.get("molecule_synonyms") or []
            if s.get("molecule_synonym")
        ]
        names = [n for n in dict.fromkeys(names) if n.lower() != values.get("name", "").lower()]
        if names:
            values["synonyms"] = names[:MAX_SYNONYMS]
        theirs = (found.get("molecule_structures") or {}).get("standard_inchi_key")
        if theirs and theirs != registered.inchikey and not (parent and stripped):
            messages.add(
                "warning",
                f"ChEMBL gives {chembl_id} the InChIKey {theirs}; its SMILES, as RDKit reads "
                f"it, is {registered.inchikey}, which it's registered as.",
            )
        origin = {
            "source": "ChEMBL",
            "id": chembl_id,
            "url": f"https://www.ebi.ac.uk/chembl/explore/compound/{chembl_id}",
            "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if version:
            origin["version"] = version
        creates.add({"type": "iki/small_molecule.Compound", "values": values, "origin": origin})
        print(f"{identifier}: {chembl_id}, {registered.inchikey} {registered.smiles}")
        print(f"::progress {done / len(asked):.2f} {done} of {len(asked)}", flush=True)
    return {**creates.output(), "metrics": {"compounds": len(seen)}, "messages": messages.output()}


def main() -> None:
    try:
        output = fetch(run_input())
    except UnreadableError as why:
        fail(str(why))
        return
    write_output(output)


if __name__ == "__main__":
    main()
