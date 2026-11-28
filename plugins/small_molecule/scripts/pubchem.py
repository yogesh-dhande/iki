"""The small_molecule plugin's PubChem fetch: `identifiers` names compounds, one a line, by
name, CID or InChIKey.

For each, it asks PubChem's PUG REST API for the compound's SMILES, InChI, InChIKey,
IUPAC name and title, then its synonyms, and registers its structure as a compound,
standardised and identified as `chemistry` says, with its title as its name, its IUPAC
name and synonyms, its CID, and the first CAS Registry Number among its synonyms whose
check digit is right. Each records the compound's PubChem record, its link, when it was
fetched and the SHA-256 of what PubChem gave.

PubChem asks for at most five requests a second, so it waits a fifth of a second between
them, and when PubChem says to slow down (429) or is busy (503), it waits and asks again.
A name that's several compounds (PubChem lists more than one CID) takes the first, saying
so. All or nothing: a name, CID or InChIKey PubChem doesn't have fails the run, naming
it.

The compound registered is RDKit's standardisation of PubChem's SMILES (with the parent,
its parent), identified by the InChI library, as a SMILES line is, so the action lists
them as its software (ADR 0106) and each compound is recorded as made with them; the run
keeps its parameters, such as whether it took the parent. PubChem's records don't say how
a compound was made.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, datetime
from typing import Any

import iki_action as iki
from chemistry import (
    LIMITS,
    MAX_SYNONYMS,
    Creates,
    Messages,
    UnreadableError,
    cas_number,
    compound_values,
    cut,
    from_smiles,
    standardise,
)
from fetching import ANY_INCHIKEY, NotFound, get_json, identifiers

API = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound"
PROPERTIES = "SMILES,InChI,InChIKey,IUPACName,Title"
AGENT = "iki-small-molecule"
#: Between requests: PubChem asks for no more than five a second.
PAUSE = 0.25
#: Synonyms kept: PubChem has hundreds for some compounds, most of them codes.
SYNONYMS = 50
SERVICE = "PubChem"


def namespace(identifier: str) -> tuple[str, str]:
    """How PubChem looks it up (`cid`, `inchikey` or `name`), and what by."""
    cid = re.match(r"^(?:CID\s*:?\s*)?([1-9][0-9]*)$", identifier, re.IGNORECASE)
    if cid is not None:
        return "cid", cid[1]
    if ANY_INCHIKEY.match(identifier.upper()):
        return "inchikey", identifier.upper()
    return "name", identifier


def lookup(identifier: str) -> tuple[list[dict[str, Any]], bytes]:
    kind, value = namespace(identifier)
    try:
        # Posted, so a name with a slash in it is a name.
        found, raw = get_json(
            f"{API}/{kind}/property/{PROPERTIES}/JSON", SERVICE, AGENT, form={kind: value}
        )
    except NotFound:
        what = {"cid": "CID", "inchikey": "InChIKey", "name": "name"}[kind]
        raise UnreadableError(
            f"PubChem has no compound with the {what} {value}. Search for it at "
            "https://pubchem.ncbi.nlm.nih.gov."
        ) from None
    rows = (found.get("PropertyTable") or {}).get("Properties") or []
    if not rows:
        raise UnreadableError(f"PubChem gave no compound for {identifier}")
    return rows, raw


def synonyms(cid: int) -> list[str]:
    try:
        found, _ = get_json(f"{API}/cid/{cid}/synonyms/JSON", SERVICE, AGENT)
    except NotFound:
        return []
    listed = (found.get("InformationList") or {}).get("Information") or [{}]
    return [str(s) for s in listed[0].get("Synonym") or []]


def fetch(run: dict[str, Any]) -> dict[str, Any]:
    params = run.get("params") or {}
    parent = bool(params.get("parent", False))
    asked = identifiers(
        params.get("identifiers") or "", "aspirin, 2244 or BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
    )
    creates = Creates(inline=True)
    messages = Messages()
    seen: set[int] = set()
    for done, identifier in enumerate(asked, start=1):
        if done > 1:
            time.sleep(PAUSE)
        rows, raw = lookup(identifier)
        row = rows[0]
        cid = int(row["CID"])
        if len(rows) > 1:
            others = ", ".join(str(r["CID"]) for r in rows[1:6])
            messages.add(
                "warning",
                f"{identifier} is more than one compound in PubChem: CID {cid} was "
                f"fetched, not {others}" + ("…" if len(rows) > 6 else "") + ".",
            )
        if cid in seen:
            continue
        seen.add(cid)
        smiles = row.get("SMILES")
        if not smiles:
            raise UnreadableError(f"PubChem's CID {cid} ({identifier}) has no structure")
        try:
            registered = standardise(from_smiles(smiles), parent)
        except UnreadableError as why:
            raise UnreadableError(f"PubChem's CID {cid} ({identifier}): {why}") from None
        time.sleep(PAUSE)
        names = synonyms(cid)
        values = compound_values(registered, str(row.get("Title") or ""))
        stripped = registered.as_drawn != registered.smiles
        if parent and stripped:
            messages.add(
                "warning",
                f"CID {cid} ({identifier}) is {registered.as_drawn}; its parent was "
                "registered, which isn't that CID, so it doesn't take it, its names or its "
                "CAS number.",
            )
        else:
            values["pubchem_cid"] = str(cid)
        kept = [cut(s, LIMITS["name"]) for s in [row.get("IUPACName") or "", *names] if s]
        title = str(values.get("name") or "").lower()
        kept = [s for s in dict.fromkeys(kept) if s.lower() != title][:SYNONYMS]
        if kept and not (parent and stripped):
            values["synonyms"] = kept[:MAX_SYNONYMS]
        cas = next((c for c in map(cas_number, names) if c), None)
        if cas is not None and not (parent and stripped):
            values["cas_number"] = cas
        theirs = row.get("InChIKey")
        if theirs and theirs != registered.inchikey and not (parent and stripped):
            messages.add(
                "warning",
                f"PubChem gives CID {cid} the InChIKey {theirs}; its SMILES, as RDKit reads "
                f"it, is {registered.inchikey}, which it's registered as.",
            )
        creates.add(
            {
                "type": "iki/small_molecule.Compound",
                "values": values,
                "origin": {
                    "source": "PubChem",
                    "id": str(cid),
                    "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                    "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                },
            }
        )
        print(f"{identifier}: CID {cid}, {registered.inchikey} {registered.smiles}")
        print(f"::progress {done / len(asked):.2f} {done} of {len(asked)}", flush=True)
    return {**creates.output(), "metrics": {"compounds": len(seen)}, "messages": messages.output()}


if __name__ == "__main__":
    iki.main(fetch, refuse=UnreadableError)
