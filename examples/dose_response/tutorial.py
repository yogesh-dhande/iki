"""Prepare the published plate through the signed-in user's public Iki API."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode
from uuid import UUID, uuid4

from . import public_plate as plate

FLOWS = {
    "research": ("Research plate assays", "Research plate assay requester", "ResearchRequest"),
    "regulated_qc": ("QC plate assays", "QC plate assay requester", "QCRequest"),
}
VERSION = "2.0.0"
PLATE = "iki/plate_assays."


class Api(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any: ...  # noqa: ANN401 - JSON returned by the public API

    def upload(self, project: str, path: Path) -> str: ...


def state_path(url: str, team: str, flow: str) -> Path:
    """A local receipt per deployment, team and flow. It contains no credentials."""
    digest = hashlib.sha256(f"{url.rstrip('/')}|{team}|{flow}".encode()).hexdigest()
    return Path.home() / ".local/state/iki/dose-response" / f"{digest}.sqlite3"


def pages(api: Api, path: str, **filters: str) -> Iterator[dict[str, Any]]:
    query = {"limit": "100", **filters}
    while True:
        page = api.request("GET", path + "?" + urlencode(query))
        yield from page["items"]
        if not page.get("next_cursor"):
            return
        query["cursor"] = page["next_cursor"]


@dataclass
class Receipt:
    db: sqlite3.Connection

    def get(self, name: str) -> str | None:
        row = self.db.execute("SELECT id FROM objects WHERE name=?", (name,)).fetchone()
        return str(row[0]) if row else None

    def save(self, name: str, identifier: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO objects VALUES(?, ?)", (name, identifier))


def projects(api: Api, team: str, flow: str, key: str, receipt: Receipt) -> tuple[str, str]:
    service, requester, _ = FLOWS[flow]
    names = (service, requester)
    mine = f"Tox21 AID 1224835 training projects. Tutorial run {key}."
    existing = {}
    for p in pages(api, "/projects", team=team):
        if p["description"] == mine and p["name"] not in names:
            raise ValueError(f"The tutorial project {p['id']} has been renamed.")
        if p["name"] in names:
            if p["description"] != mine:
                raise ValueError(
                    f"{p['name']} belongs to a different run. "
                    "Use the original receipt or a different team."
                )
            if p["archived"] or not p["can"]["manage"] or not p["allow_sharing"]:
                raise ValueError(
                    f"{p['name']} must be active, shared and administered by the operator."
                )
            existing[p["name"]] = p
    for name in names:
        known = receipt.get(name)
        if known and (name not in existing or existing[name]["id"] != known):
            raise ValueError(f"The original tutorial project {known} is missing.")
    for name in names:
        if name not in existing:
            existing[name] = api.request(
                "POST", "/projects", json={"team": team, "name": name, "description": mine}
            )
        if receipt.get(name) is None:
            receipt.save(name, existing[name]["id"])
    return existing[service]["id"], existing[requester]["id"]


def configure(
    api: Api,
    core: str,
    requester: str,
    flow: str,
    coordinator: str,
    analyst: str,
    reviewer: str,
    qa: str | None,
) -> None:
    catalogue = api.request("GET", f"/starters?project={core}")
    starter = next((s for s in catalogue if s["id"] == "dose-response"), None)
    if not starter or starter["version"] != VERSION:
        raise ValueError(f"Install dose-response {VERSION} before loading this tutorial.")
    if starter["applied"] and starter["applied"]["flow"] != flow:
        raise ValueError(
            "The training project's review flow has changed. Leave its settings intact."
        )
    members = {m["user"]["id"]: m for m in pages(api, f"/projects/{core}/members") if m["user"]}
    assignments = {PLATE + "analyst": analyst, PLATE + "reviewer": reviewer}
    if qa:
        assignments[PLATE + "qa"] = qa
    for person in set(assignments.values()):
        if person not in members:
            role = "editor" if person == analyst else "viewer"
            members[person] = api.request(
                "PUT", f"/projects/{core}/members/users/{person}", json={"role": role}
            )
    request_roles = {coordinator: {"iki/lab_roles.coordinator"}}
    if qa:
        request_roles.setdefault(qa, set()).add("iki/lab_roles.qa")
    else:
        request_roles.setdefault(reviewer, set()).add("iki/lab_roles.reviewer")
    for person, required in request_roles.items():
        member = members[person]
        held = {r["role"] for r in member["lab_roles"]}
        if required - held:
            api.request(
                "PUT",
                f"/projects/{core}/members/{member['id']}/lab-roles",
                json={"roles": sorted(held | required)},
            )
    choice = {
        "project": core,
        "flow": flow,
        "version": VERSION,
        "role_assignments": {role: [members[person]["id"]] for role, person in assignments.items()},
    }
    plan = api.request("POST", "/starters/dose-response/preview", json=choice)
    if plan["changed"]:
        api.request("POST", "/starters/dose-response/apply", json={**choice, "plan": plan["plan"]})
    intake_path = f"/projects/{core}/intake"
    intake = api.request("GET", intake_path)
    request_type = "iki/requests." + FLOWS[flow][2]
    if not (intake["types"] or intake["projects"] or intake["from_registry"]):
        api.request("PUT", intake_path, json={"types": [request_type], "projects": [requester]})
    elif (
        [t["name"] for t in intake["types"]] != [request_type]
        or [p["id"] for p in intake["projects"]] != [requester]
        or intake["from_registry"]
    ):
        raise ValueError("The training project's intake has changed. Leave it intact.")


def record(
    api: Api,
    receipt: Receipt,
    project: str,
    kind: str,
    values: dict[str, Any],
    *,
    match: str,
    amount: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recover the record of an interrupted step without rewriting an existing record."""
    search = {
        "project": project,
        "type": kind,
        "exact_type": True,
        "limit": 2,
        "where": {"field": match, "op": "eq", "value": values[match]},
    }
    key = json.dumps([project, kind, match, values[match]])
    known = receipt.get(key)
    if known:
        row: dict[str, Any] = api.request("GET", f"/entities/{known}")
    else:
        rows = api.request("POST", "/entities/search", json=search)["items"]
        if len(rows) > 1:
            raise ValueError(f"Ambiguous {kind} in the training project ({match}={values[match]}).")
        if rows:
            row = rows[0]
        else:
            body: dict[str, Any] = {"project": project, "type": kind, "values": values}
            if amount is not None:
                body["amount"] = amount
            row = api.request("POST", "/entities", json=body)
    if row["type"] != kind or (row["owner_project"] or {}).get("id") != project or row["archived"]:
        raise ValueError(f"The original {kind} is unavailable in the training project.")
    for field, expected in values.items():
        actual = row["values"].get(field)
        # Row fields the input omits are serialized as null by the API.
        if field == "assignments" and isinstance(actual, list):
            actual = [{k: v for k, v in well.items() if v is not None} for well in actual]
        if actual != expected:
            raise ValueError(f"The {field} of {row['hrid']} differs from the tutorial.")
    if not known:
        receipt.save(key, row["id"])
    return row


def reference(row: dict[str, Any]) -> str:
    """An identity-bearing record stores its registry entry as the reference."""
    return str((row["entry"] or row)["id"])


def reader_file(api: Api, core: str, receipt: Receipt) -> str:
    source = plate.READER_FILE
    known = receipt.get("reader_file")
    if known:
        found = [api.request("GET", f"/files/{known}")]
    else:
        found = [
            f
            for f in pages(api, "/files", project=core, q=source.name)
            if f["name"] == source.name and (f["project"] or {}).get("id") == core
        ]
        if not found:
            saved = api.upload(core, source)
            found = [api.request("GET", f"/files/{saved}")]
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if (
        len(found) != 1
        or found[0]["sha256"] != digest
        or found[0]["name"] != source.name
        or (found[0]["project"] or {}).get("id") != core
    ):
        raise ValueError(f"The training project's {source.name} differs from the published plate.")
    file_id = str(found[0]["id"])
    if not known:
        receipt.save("reader_file", file_id)
    return file_id


def material(api: Api, receipt: Receipt, core: str, requester: str, flow: str) -> dict[str, str]:
    source = reader_file(api, core, receipt)
    protein = record(
        api,
        receipt,
        core,
        "iki/protein.Protein",
        {"name": "Firefly luciferase (Photinus pyralis)", "sequence": plate.LUCIFERASE},
        match="sequence",
    )
    tube = record(
        api,
        receipt,
        core,
        "iki/inventory.Tube",
        {
            "name": "Firefly luciferase assay target",
            "contents": reference(protein),
        },
        match="name",
        amount={"value": 100, "unit": "uL"},
    )
    tubes = {}
    for key, series in plate.SERIES.items():
        compound = record(
            api,
            receipt,
            requester,
            "iki/small_molecule.Compound",
            plate.compound_values(key),
            match="inchikey",
        )
        lot = record(
            api,
            receipt,
            requester,
            "iki/lots.GenericLot",
            {"of": reference(compound), "lot_number": f"Tox21 SID {series['sid']}"},
            match="lot_number",
        )
        item = record(
            api,
            receipt,
            requester,
            "iki/inventory.Tube",
            {"name": series["name"], "contents": reference(lot)},
            match="name",
        )
        tubes[key] = item["id"]
    req = record(
        api,
        receipt,
        requester,
        "iki/requests." + FLOWS[flow][2],
        {
            "title": plate.PLATE_NAME,
            "tubes": list(tubes.values()),
            "instructions": (
                "Fit the luciferase IC50 for apigenin and daidzein using all three "
                "deposited replicate series from PubChem AID 1224835."
            ),
        },
        match="title",
    )
    target = api.request("GET", f"/entities/{req['id']}/request")["service"]
    if target is None:
        api.request(
            "POST",
            f"/entities/{req['id']}/submit",
            json={"service": core},
            headers={"If-Match": f'"{req["revision"]}"'},
        )
    elif target["id"] != core:
        raise ValueError("The tutorial request was sent to a different project.")
    assay = record(
        api,
        receipt,
        core,
        PLATE + "AssayPlate",
        {
            "name": plate.PLATE_NAME,
            "procedure": plate.ASSAY,
            "procedure_revision": plate.SOURCE["procedure_revision"],
            "size": "plate_96",
            "reader_file": source,
            "request": req["id"],
            "reagent_tube": tube["id"],
            "assignments": plate.assignments(tubes),
        },
        match="name",
    )
    sample = record(
        api,
        receipt,
        core,
        # Research input is completed by the analyst; a QC sample's measurements are
        # reviewed as well.
        "iki/samples." + ("ReviewedSample" if flow == "regulated_qc" else "PlainSample"),
        {
            "source": tube["id"],
            "purpose": assay["id"],
            "notes": (
                "Target enzyme for PubChem AID 1224835. "
                "Stock and draw volumes are practice quantities."
            ),
        },
        match="purpose",
    )
    draws = sample["values"].get("receipt")
    if not draws:
        api.request(
            "POST",
            f"/entities/{tube['id']}/amount/draw",
            json={
                "amount": {"value": 10, "unit": "uL"},
                "used_in": sample["id"],
                "confirm": tube["hrid"],
            },
        )
    elif (
        len(draws) != 1
        or draws[0]["record"] != tube["id"]
        or draws[0]["unit"] != "uL"
        or Decimal(draws[0]["amount"]) != 10
    ):
        raise ValueError("The sample already has a different material draw.")
    return {
        **tubes,
        "project": core,
        "requester_project": requester,
        "request": req["id"],
        "protein": protein["id"],
        "sample": sample["id"],
        "tube": tube["id"],
        "plate": assay["id"],
        "reader_file": source,
    }


def prepare(
    api: Api,
    *,
    team: str,
    flow: str,
    analyst: str,
    reviewer: str,
    qa: str | None = None,
    state: Path,
) -> dict[str, str]:
    if flow not in FLOWS or (flow == "regulated_qc") != bool(qa):
        raise ValueError("Choose research without QA, or regulated_qc with a QA signer.")
    if qa and len({analyst, reviewer, qa}) != 3:
        raise ValueError("Regulated QC requires three different signers.")
    me = str(api.request("GET", "/me")["id"])
    selection = json.dumps([str(UUID(team)), flow, me, analyst, reviewer, qa])
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(state, timeout=10)) as db:
        # An exclusive SQLite connection keeps its lock across the per-step commits.
        # Each successful remote write is checkpointed before starting the next one.
        db.execute("PRAGMA locking_mode=EXCLUSIVE")
        db.execute("BEGIN EXCLUSIVE")
        db.execute("CREATE TABLE IF NOT EXISTS objects (name TEXT PRIMARY KEY, id TEXT NOT NULL)")
        receipt = Receipt(db)
        db.execute(
            "CREATE TABLE IF NOT EXISTS tutorial "
            "(id INTEGER PRIMARY KEY, choice TEXT, run_key TEXT, result TEXT)"
        )
        with db:
            db.execute(
                "INSERT OR IGNORE INTO tutorial VALUES(1, ?, ?, NULL)", (selection, str(uuid4()))
            )
        choice, key, result = db.execute(
            "SELECT choice, run_key, result FROM tutorial WHERE id=1"
        ).fetchone()
        if choice != selection:
            raise ValueError(
                "This receipt belongs to a different team, flow, operator or set of signers."
            )
        if result is not None:
            saved: dict[str, str] = json.loads(result)
            # A completed run never provisions replacements, even if either project
            # was renamed, deleted or made inaccessible to the operator.
            for field, name in zip(("project", "requester_project"), FLOWS[flow][:2], strict=True):
                project = api.request("GET", f"/projects/{saved[field]}")
                if project["name"] != name or project["team"]["id"] != team:
                    raise ValueError(f"The original tutorial project {saved[field]} was renamed.")
            return saved
        core, requester = projects(api, team, flow, key, receipt)
        configure(api, core, requester, flow, me, analyst, reviewer, qa)
        ids = material(api, receipt, core, requester, flow)
        db.execute("UPDATE tutorial SET result=? WHERE id=1", (json.dumps(ids),))
        db.commit()
        return ids
