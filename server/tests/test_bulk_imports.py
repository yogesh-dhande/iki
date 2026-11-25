"""Bulk imports (ADR 0056): creates from a file, checked a chunk at a time and resumable,
then written all at once, or not at all."""

import contextlib
import dataclasses
import hashlib
import json
from typing import Any

import pytest
import requests
from django.db import connection
from django.test.utils import CaptureQueriesContext
from org_world import World
from test_api_entities import api_as, create
from test_files import run_tasks
from test_run_imports import IMPORT, importing, origin, stored
from test_run_results import output
from test_runs import launch, world
from test_workers import WorkerClient, keep_connection, worker

from iki_server.entities import services
from iki_server.entities.models import Entity, ProjectLink, Revision
from iki_server.entities.values import shape
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.runs import imports
from iki_server.runs.models import (
    ActionRun,
    ExternalOrigin,
    ImportPhase,
    Job,
    JobState,
    RunImport,
)
from iki_server.webhooks.models import Delivery

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # the fixtures, used here too

KINASES = ["MKVLEICAAA", "MKVLEICCCC", "MKVLEICDDD", "MKVLEICEEE"]


@pytest.fixture(autouse=True)
def storage(s3: Any) -> Any:
    return s3


def protein(sequence: str, name: str, file_id: str, **values: Any) -> dict[str, Any]:
    return {
        "type": "acme/protein.Protein",
        "values": {"name": name, "sequence": sequence, **values},
        "origin": origin(file_id, name),
    }


def upload(pod: WorkerClient, job: dict[str, Any], data: bytes) -> str:
    """Uploads the creates file as iki-worker does, and returns its file's ID."""
    started = pod.report(
        job,
        "uploads",
        name="creates.ndjson",
        media_type="application/x-ndjson",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    assert started.status_code == 201, started.content
    made: dict[str, Any] = started.json()
    offset = 0
    for part in made["parts"]:
        chunk = data[offset : offset + part["size"]]
        offset += part["size"]
        requests.put(part["url"], data=chunk, timeout=30).raise_for_status()
    if made["parts"]:
        made = pod.report(job, f"uploads/{made['id']}/complete").json()
    for _ in range(5):
        if made["state"] == "ready":
            break
        run_tasks()
        made = pod.report(job, f"uploads/{made['id']}/complete").json()
    assert made["state"] == "ready", made
    return str(made["file"])


def lines(*items: Any) -> bytes:
    return b"".join((i if isinstance(i, bytes) else json.dumps(i).encode()) + b"\n" for i in items)


def started(
    world: World, make: Any, software: list[dict[str, str]] | None = None, **extra: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """An import run whose command wrote ``make(file_id)``'s lines to a creates file.
    Returns the run and the worker's answer. ``software``: what its action lists."""
    file_id = stored(world)
    run = launch(api_as(world, "editor"), world.project, {}, IMPORT, params={"file": file_id})
    assert run.status_code == 201, run.content
    if software is not None:
        kept = ActionRun.objects.get(pk=run.json()["id"])
        kept.spec["software"] = software
        kept.save(update_fields=["spec"])
    pod = worker(world)
    job = pod.lease().json()
    data = make(file_id)
    answer = pod.report(
        job, "complete", output=output(creates_file=upload(pod, job, data), **extra)
    )
    assert answer.status_code == 200, answer.content
    return run.json(), answer.json()


def finished(world: World, run: dict[str, Any]) -> dict[str, Any]:
    """The run, once the task runner has nothing left to do."""
    for _ in range(100):
        if not run_tasks():
            break
    return seen(world, run)


def seen(world: World, run: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = api_as(world, "editor").get(f"/runs/{run['id']}").json()
    return body


def test_a_run_imports_its_creates_from_a_file(world: World) -> None:
    run, answer = started(
        world,
        lambda f: lines(*(protein(s, f"K{n}", f) for n, s in enumerate(KINASES))),
        metrics={"records": 4},
    )
    assert answer["run_status"] == "running"
    job = Job.objects.get(run_id=run["id"])
    assert job.state == JobState.COMMITTING
    assert job.lease_token is None
    # Nobody sees any of it before it's all written.
    assert not Entity.objects.filter(type="acme/protein.Protein").exists()

    body = finished(world, run)
    assert body["status"] == "succeeded", body
    assert body["metrics"] == {"records": 4}
    assert [e["kind"] for e in body["effects"]] == ["created"] * 4
    made = list(
        Entity.objects.filter(type="acme/protein.Protein", owner_project__isnull=False).order_by(
            "hrid"
        )
    )
    assert [e.hrid for e in made] == [e["hrid"] for e in body["effects"]]
    hrids = [int(e.hrid.split("-")[1]) for e in made]
    assert hrids == sorted(hrids)
    assert len(set(hrids)) == 4

    # Each is as an entity made one at a time is: its values, revision and history, its
    # project, and where it came from.
    first = api_as(world, "editor").get(f"/entities/{made[0].hrid}").json()
    assert first["values"]["sequence"] == KINASES[0]
    assert first["values"]["name"] == "K0"
    assert first["revision"] == 1
    revision = Revision.objects.get(entity=made[0])
    assert (revision.kind, revision.via, revision.user) == (
        "created",
        "run",
        world.users["editor"],
    )
    # The sequence is its registry entry's (ADR 0115), in the entry's own first revision.
    assert sorted(revision.changed_fields) == ["name"]
    entry = Revision.objects.get(entity__records=made[0])
    assert entry.changed_fields == ["sequence"]
    # Its entry's values name the run, as a create's do.
    provenance = api_as(world, "editor").get(f"/entities/{made[0].pk}/provenance").json()
    assert provenance["sequence"]["entry"] is True
    assert provenance["sequence"]["run"]["hrid"] == run["hrid"]
    assert ProjectLink.objects.filter(entity=made[0], project=world.project).exists()
    found = ExternalOrigin.objects.get(entity=made[0])
    assert (found.source, found.identifier, found.revision) == ("FASTA file", "K0", 1)
    history = api_as(world, "editor").get(f"/entities/{made[0].hrid}/revisions").json()
    assert history["items"][0]["number"] == 1
    assert Job.objects.get(pk=job.pk).progress == 1.0
    imported = RunImport.objects.get(run_id=run["id"])
    assert (imported.phase, imported.created, imported.reused) == (ImportPhase.ENDED, 4, 0)
    # Its staged lines are gone once it's written.
    assert imported.cleared
    assert not _staged_tables()


def test_a_worker_import_refuses_a_reserved_tube_barcode_atomically(world: World) -> None:
    type_name = "acme/protein.Tube"
    file_id = stored(world)
    run = importing(world, file_id).json()
    # The fixture importer normally creates proteins. Declare Tube before its worker leases it.
    stored_run = ActionRun.objects.get(pk=run["id"])
    stored_run.spec["creates"].append(
        {"type": type_name, "cardinality": "many", "derived_from": []}
    )
    stored_run.save(update_fields=["spec"])
    pod = worker(world)
    job = pod.lease().json()
    data = lines(
        *(
            {"type": type_name, "values": {"barcode": code}, "origin": origin(file_id, code)}
            for code in ("VENDOR-01", "iKi:reserved")
        )
    )
    answer = pod.report(job, "complete", output=output(creates_file=upload(pod, job, data)))
    assert answer.json()["run_status"] == "running", answer.content
    body = finished(world, run)
    assert body["status"] == "failed", body
    assert body["error"]["code"] == "invalid_output"
    (error,) = body["error"]["errors"]
    assert error["pointer"] == "#/creates_file/2/values/barcode"
    assert "cannot start with IKI:" in error["detail"]
    assert not Entity.objects.filter(type=type_name).exists()


def test_its_lines_take_every_kind_of_value_a_create_does(world: World) -> None:
    editor = api_as(world, "editor")
    source = create(
        editor, world.project, {"name": "S", "sequence": "MKKK"}, "acme/protein.Protein"
    ).json()
    run, _ = started(
        world,
        lambda f: lines(
            protein(
                KINASES[0],
                "ABL1",
                f,
                host="Homo sapiens",
                source=source["id"],
                temperature_optimum={"value": 310.15, "unit": "K"},
                uniprot_ids=["P00519", "Q59FK4"],
                summary="A tyrosine kinase.",
            )
        ),
    )
    body = finished(world, run)
    assert body["status"] == "succeeded", body
    (effect,) = body["effects"]
    values = editor.get(f"/entities/{effect['hrid']}").json()["values"]
    assert values["host"] == "Homo sapiens"
    assert values["source"] == source["entry"]["id"]  # its registry entry (ADR 0115)
    assert values["temperature_optimum"] == pytest.approx(37.0)
    assert values["uniprot_ids"] == ["P00519", "Q59FK4"]
    assert values["summary"] == "A tyrosine kinase."


def test_duplicates_are_reused_as_one_at_a_time(world: World) -> None:
    editor = api_as(world, "editor")
    existing = create(
        editor, world.project, {"name": "Old", "sequence": KINASES[1]}, "acme/protein.Protein"
    ).json()
    run, _ = started(
        world,
        lambda f: lines(
            protein(KINASES[0], "A", f),
            protein(KINASES[1], "B", f),
            protein(KINASES[0], "A again", f),
            protein(KINASES[2], "C", f),
        ),
    )
    body = finished(world, run)
    assert body["status"] == "succeeded", body
    kinds = {e["hrid"]: e["kind"] for e in body["effects"]}
    assert kinds[existing["hrid"]] == "reused"
    assert sorted(kinds.values()) == ["created", "created", "reused"]
    assert (
        Entity.objects.filter(type="acme/protein.Protein", owner_project__isnull=False).count() == 3
    )
    imported = RunImport.objects.get(run_id=run["id"])
    assert (imported.created, imported.reused, imported.registered) == (2, 1, 2)
    # Each line's origin is kept, the one that found its record again too (ADR 0154).
    by = {
        o.identifier: (o.entity_id, o.revision)
        for o in ExternalOrigin.objects.filter(run_id=run["id"])
    }
    assert sorted(by) == ["A", "A again", "B", "C"]
    assert by["A again"] == (by["A"][0], None)
    assert (by["A"][1], by["B"][1], by["C"][1]) == (1, None, 1)


@pytest.mark.parametrize("order", ["bispecific first", "multimer first"])
def test_one_new_identity_is_one_type_whichever_line_comes_first(world: World, order: str) -> None:
    """A bispecific and a lab's generic multimer of the same parts and copies are one
    molecule (ADR 0161), whichever type registers it (ADR 0165): two lines that register
    it as both are refused at the second, as two creates one after the other would be, and
    nothing is written. Emicizumab's chains (KEGG DRUG D10821): two heavy chains, and the
    light chain both arms share, twice."""
    editor = api_as(world, "editor")
    heavy, other_heavy, light = (
        create(editor, world.project, {"sequence": s}, "acme/protein.Protein").json()
        for s in (KINASES[0], KINASES[1], KINASES[2])
    )
    parts = [
        {"part": heavy["id"], "copies": 1, "role": "heavy"},
        {"part": other_heavy["id"], "copies": 1, "role": "heavy"},
        {"part": light["id"], "copies": 2, "role": "light"},
    ]

    def complex_(type_name: str, name: str, file_id: str) -> dict[str, Any]:
        return {
            "type": f"acme/protein.{type_name}",
            "values": {"name": name, "components": parts},
            "origin": origin(file_id, name),
        }

    first, second = (
        ("Bispecific", "Multimer") if order == "bispecific first" else ("Multimer", "Bispecific")
    )
    run, _ = started(
        world,
        lambda f: lines(complex_(first, "E1", f), complex_(second, "E2", f)),
    )
    body = finished(world, run)
    assert body["status"] == "failed", body
    assert body["error"]["errors"] == [
        {
            "pointer": "#/creates_file/2/type",
            "detail": f"Line 1 registers this as a acme/protein.{first}, the same molecule: "
            "register it as one.",
        }
    ]
    assert not Entity.objects.filter(type__in=[f"acme/protein.{t}" for t in (first, second)])


def entry_of(sequence: str) -> Entity:
    """The registry entry of a protein's identity (ADR 0115)."""
    (found,) = [
        e
        for e in Entity.objects.filter(type="acme/protein.Protein", registry__isnull=False)
        if services.current_values(services.specific(e))["sequence"] == sequence
    ]
    return found


def facts(entry: Entity) -> dict[str, Any]:
    values = services.current_values(services.specific(entry))
    return {n: values[n] for n in ("ec_number", "hazard")}


def test_every_line_of_an_identity_fills_what_its_entry_hasnt_got(world: World) -> None:
    """A fact a later line of the same identity gives is kept, as a second registration's
    would be, whether this import registers the identity or it's registered already."""
    editor = api_as(world, "editor")
    create(editor, world.project, {"name": "Old", "sequence": KINASES[1]}, "acme/protein.Protein")
    run, _ = started(
        world,
        lambda f: lines(
            protein(KINASES[0], "A", f),
            protein(KINASES[1], "B", f),
            protein(KINASES[0], "A again", f, ec_number="2.7.10.2"),
            protein(KINASES[1], "B again", f, hazard="bsl1"),
            # The same as the earlier line: nothing to fill, nor refuse.
            protein(KINASES[0], "A once more", f, ec_number="2.7.10.2", hazard="none"),
        ),
    )
    body = finished(world, run)
    assert body["status"] == "succeeded", body
    new, old = entry_of(KINASES[0]), entry_of(KINASES[1])
    assert facts(new) == {"ec_number": "2.7.10.2", "hazard": "none"}
    assert facts(old) == {"ec_number": None, "hazard": "bsl1"}
    # Each fill is a revision of the entry, by the run.
    filled = Revision.objects.filter(entity=new).order_by("number")
    assert [(r.number, sorted(r.changed_fields), r.via) for r in filled] == [
        (1, ["sequence"], "run"),
        (2, ["ec_number", "hazard"], "run"),
    ]
    # Each names the run, on the entry registered already too.
    for entry, numbers in ((new, [1, 2]), (old, [2])):
        told = editor.get(f"/entries/{entry.pk}/revisions").json()["items"]
        by = {r["number"]: r["run"] and r["run"]["hrid"] for r in told}
        assert [by[n] for n in numbers] == [run["hrid"]] * len(numbers)


def test_inline_and_file_creates_share_one_effect_with_each_entry_revision(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(imports, "FILL_BATCH", 1)
    run, _ = started(
        world,
        lambda f: lines(
            protein(KINASES[0], "A", f, ec_number="2.7.10.2"),
            protein(KINASES[0], "A", f, hazard="none"),
            protein(KINASES[1], "B", f),
        ),
        creates=[{"type": "acme/protein.Protein", "values": {"sequence": KINASES[0]}}],
    )
    body = finished(world, run)
    assert body["status"] == "succeeded", body
    effects = ActionRun.objects.get(pk=run["id"]).effects.order_by("id")
    assert list(effects.values_list("entry_id", "entry_revisions")) == [
        (entry_of(KINASES[0]).pk, [1, 2, 3]),
        (entry_of(KINASES[1]).pk, [1]),
    ]


def test_lines_of_an_identity_that_give_a_fact_two_values_fail_the_import(
    world: World,
) -> None:
    editor = api_as(world, "editor")
    create(
        editor,
        world.project,
        {"name": "Old", "sequence": KINASES[1], "ec_number": "1.1.1.1"},
        "acme/protein.Protein",
    )
    run, _ = started(
        world,
        lambda f: lines(
            protein(KINASES[0], "A", f, ec_number="2.7.10.2"),
            protein(KINASES[1], "B", f),
            protein(KINASES[0], "A again", f, ec_number="2.7.11.1"),
            protein(KINASES[1], "B again", f, ec_number="2.7.11.1"),
            protein(KINASES[2], "C", f),
            protein(KINASES[2], "C again", f, hazard="bsl2"),
            protein(KINASES[2], "C once more", f, hazard="bsl3"),
        ),
    )
    body = finished(world, run)
    assert body["status"] == "failed", body
    assert body["error"]["code"] == "invalid_output"
    a, b, c = body["error"]["errors"]
    assert a["pointer"] == "#/creates_file/3/values/ec_number"
    assert "another value than line 1 does" in a["detail"]
    assert b["pointer"] == "#/creates_file/4/values/ec_number"
    assert "only its registry's registrars change it" in b["detail"]
    assert c["pointer"] == "#/creates_file/7/values/hazard"
    assert "another value than line 6 does" in c["detail"]
    # Nothing was written: no entry but the one there was, which has what it had.
    assert Entity.objects.filter(type="acme/protein.Protein", registry__isnull=False).count() == 1
    assert facts(entry_of(KINASES[1])) == {"ec_number": "1.1.1.1", "hazard": None}


def _refresh_queries(world: World, count: int) -> int:
    """Imports `count` proteins with facts, then the same file again: how many queries the
    second import takes, from its check to its commit."""
    letters = "ACDEFGHIKLMNPQRSTVWY"
    sequences = [f"MKV{letters[n // 20]}{letters[n % 20]}LEIC" for n in range(count)]

    def make(f: str) -> bytes:
        return lines(
            *(
                protein(s, f"K{n}", f, ec_number="2.7.10.2", hazard="bsl1")
                for n, s in enumerate(sequences)
            )
        )

    first, _ = started(world, make)
    assert finished(world, first)["status"] == "succeeded"
    again, _ = started(world, make)
    with CaptureQueriesContext(connection) as queries:
        body = finished(world, again)
    assert body["status"] == "succeeded", body
    assert RunImport.objects.get(run_id=again["id"]).reused == count
    return len(queries)


def test_refreshing_registered_identities_takes_as_many_queries_however_many_there_are(
    world: World,
) -> None:
    """Lines that give their entries only what they have are found in SQL: not compared, nor
    locked, one by one, while the import holds the registry's lock on the type."""
    few = _refresh_queries(world, 5)
    many = _refresh_queries(world, 40)
    assert many == few


def test_a_project_that_keeps_its_entities_has_none_reused_or_referred_to(world: World) -> None:
    """ADR 0067: to an import elsewhere, its entities are as if the person couldn't read them."""
    editor = api_as(world, "editor")
    cro = Project.objects.create(team=world.team, name="Oncology CRO", allow_sharing=False)
    ProjectMembership.objects.create(
        project=cro, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    theirs = create(
        editor, cro, {"name": "Old", "sequence": KINASES[1]}, "acme/protein.Protein"
    ).json()
    run, _ = started(world, lambda f: lines(protein(KINASES[1], "B", f)))
    body = finished(world, run)
    assert body["status"] == "succeeded", body
    assert [e["kind"] for e in body["effects"]] == ["created"]
    assert not ProjectLink.objects.filter(entity_id=theirs["id"], project=world.project).exists()
    # Nor may a line refer to one.
    dna = create(editor, cro, {"bases": "ACGT"}).json()
    run, _ = started(world, lambda f: lines(protein(KINASES[2], "C", f, encoded_by=dna["id"])))
    body = finished(world, run)
    assert body["status"] == "failed", body
    assert body["error"]["errors"] == [
        {
            "pointer": "#/creates_file/1/values/encoded_by",
            "detail": "refers to an entity in Oncology CRO, which doesn't allow sharing its "
            "entities with other projects.",
        }
    ]


#: The first 60 bases of pUC19 (GenBank L09137.2), as a circle of its own.
PUC19_START = "TCGCGCGTTTCGGTGATGACGGTGAAAACCTCTGACACATGCAGCTCCCGGAGACGGTCA"


def plasmid(bases: str, name: str, file_id: str, *features: tuple[int, int]) -> dict[str, Any]:
    return {
        "type": "acme/dna.Plasmid",
        "values": {
            "name": name,
            "bases": bases,
            "topology": "circular",
            "features": [
                {"type": "misc_feature", "start": a, "end": b, "strand": "forward"}
                for a, b in features
            ],
        },
        "origin": origin(file_id, name),
    }


def test_lines_of_an_entry_spelled_otherwise_are_placed_along_it(world: World) -> None:
    """ADR 0138, as a create makes a record: a line giving a plasmid registered elsewhere
    from another origin has its features placed along the entry's bases, and its record's
    first revision keeps the bases it gave."""
    editor = api_as(world, "editor")
    elsewhere = Project.objects.create(team=world.team, name="Plasmids")
    ProjectMembership.objects.create(
        project=elsewhere, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    for bases in ("AAGTCC", PUC19_START, "GATTACA"):
        made = create(
            editor, elsewhere, {"bases": bases, "topology": "circular"}, "acme/dna.Plasmid"
        )
        assert made.status_code == 201, made.content
    # AAGTCC from its third base, GTCCAA: its bases 1 and 2 (GT) are the entry's 3 and 4.
    small = "AAGTCC"[2:] + "AAGTCC"[:2]
    # pUC19's from its 41st base: its 21..30 is the entry's 1..10, its 15..25 lies across
    # the entry's origin (55..60, 1..5), and its 55..5 across its own, the entry's 35..45.
    turn = 40
    large = PUC19_START[turn:] + PUC19_START[:turn]
    given = [(21, 30), (15, 25), (55, 5)]
    run, _ = started(
        world,
        lambda f: lines(
            plasmid(small, "Small", f, (1, 2)),
            plasmid(large, "pUC19 start", f, *given),
            # Spelled as its entry is: as given. And a new one, registered by its line.
            plasmid("GATTACA", "As registered", f, (6, 2)),
            plasmid("GATTACAGATTACAT", "New", f, (13, 2)),
        ),
    )
    body = finished(world, run)
    assert body["status"] == "succeeded", body

    records = {}
    for effect in body["effects"]:
        assert effect["kind"] == "created"
        values = editor.get(f"/entities/{effect['entity']}").json()["values"]
        records[values["name"]] = {
            "bases": values["bases"],
            "features": values["features"],
            "first": Revision.objects.get(entity_id=effect["entity"], number=1).submitted,
        }
    assert len(records) == 4

    def record(name: str) -> dict[str, Any]:
        found: dict[str, Any] = records[name]
        return found

    def placed(start: int, end: int, length: int) -> tuple[int, int]:
        # Base n of the line's rotation is the entry's base (n - 1 + turn) mod length + 1.
        return ((start - 1 + turn) % length + 1, (end - 1 + turn) % length + 1)

    def read(bases: str, start: int, end: int) -> str:
        return bases[start - 1 : end] if start <= end else bases[start - 1 :] + bases[:end]

    small_record = record("Small")
    assert small_record["bases"] == "AAGTCC"
    assert small_record["features"] == [
        {"type": "misc_feature", "start": 3, "end": 4, "strand": "forward"}
    ]
    assert read("AAGTCC", 3, 4) == read(small, 1, 2) == "GT"
    assert small_record["first"] == {"bases": small}

    large_record = record("pUC19 start")
    assert large_record["bases"] == PUC19_START
    found = [(f["start"], f["end"]) for f in large_record["features"]]
    assert found == [placed(a, b, len(PUC19_START)) for a, b in given]
    assert found == [(1, 10), (55, 5), (35, 45)]
    for (a, b), (c, d) in zip(given, found, strict=True):
        assert read(large, a, b) == read(PUC19_START, c, d)
    assert large_record["first"] == {"bases": large}
    # A part across the entry's origin is two, as INSDC writes it.
    assert large_record["features"][1]["parts"] == [
        {"start": 55, "end": 60, "strand": "forward"},
        {"start": 1, "end": 5, "strand": "forward"},
    ]
    for name, bases in (("As registered", "GATTACA"), ("New", "GATTACAGATTACAT")):
        assert record(name)["bases"] == bases
        assert record(name)["first"] is None
    assert [(f["start"], f["end"]) for f in record("As registered")["features"]] == [(6, 2)]
    assert [(f["start"], f["end"]) for f in record("New")["features"]] == [(13, 2)]


def test_a_line_that_doesnt_fit_fails_the_run_and_writes_nothing(world: World) -> None:
    run, _ = started(
        world,
        lambda f: lines(
            protein(KINASES[0], "A", f),
            b"",
            b"{not json",
            protein("not a protein!", "B", f),
            {"type": "acme/dna.DNASequence", "values": {"name": "X", "bases": "ACGT"}},
            {
                "type": "acme/protein.Protein",
                "values": {"name": "C", "sequence": "MKK"},
                "extra": 1,
            },
            protein(KINASES[1], "D", f, structure="0193a0b4-0000-7000-8000-000000000000"),
        ),
    )
    body = finished(world, run)
    assert body["status"] == "failed"
    assert body["error"]["code"] == "invalid_output"
    problems = {e["pointer"]: e["detail"] for e in body["error"]["errors"]}
    assert set(problems) == {
        "#/creates_file/3",
        "#/creates_file/4/values/sequence",
        "#/creates_file/5/type",
        "#/creates_file/6/extra",
        "#/creates_file/7/values/structure",
    }
    assert problems["#/creates_file/3"].startswith("The line isn't JSON")
    assert (
        "doesn't declare that it creates acme/dna.DNASequence" in problems["#/creates_file/5/type"]
    )
    assert not Entity.objects.filter(type="acme/protein.Protein").exists()
    assert RunImport.objects.get(run_id=run["id"]).phase == ImportPhase.ENDED
    assert not _staged_tables()


def test_two_lines_with_one_unique_value_fail_the_import(world: World) -> None:
    run, _ = started(
        world,
        lambda f: lines(
            protein(KINASES[0], "A", f, lims_id="LIMS-1"),
            protein(KINASES[1], "B", f, lims_id="LIMS-1"),
        ),
    )
    body = finished(world, run)
    assert body["status"] == "failed"
    assert body["error"]["errors"] == [
        {
            "pointer": "#/creates_file/2/values/lims_id",
            "detail": "Line 1 has the same lims_id: each record comes once in a file.",
        }
    ]
    assert not Entity.objects.filter(type="acme/protein.Protein").exists()


def test_lines_repeating_an_inherited_match_field_name_both_lines(world: World) -> None:
    file_id = stored(world)
    run = importing(world, file_id).json()
    stored_run = ActionRun.objects.get(pk=run["id"])
    stored_run.spec["creates"] = [
        {
            "type": "acme/protein.Tube",
            "cardinality": "many",
            "match_on": "barcode",
            "derived_from": [],
        }
    ]
    stored_run.save(update_fields=["spec"])
    pod = worker(world)
    job = pod.lease().json()
    data = lines(
        *(
            {"type": type_name, "values": {"barcode": "VENDOR-01"}}
            for type_name in ("acme/protein.Tube", "acme/protein.RetainTube")
        )
    )
    answer = pod.report(job, "complete", output=output(creates_file=upload(pod, job, data)))
    assert answer.json()["run_status"] == "running", answer.content
    body = finished(world, run)
    assert body["status"] == "failed", body
    (error,) = body["error"]["errors"]
    assert error["pointer"] == "#/creates_file/2/values/barcode"
    assert "Line 1" in error["detail"]
    assert not Entity.objects.filter(
        type__in=["acme/protein.Tube", "acme/protein.RetainTube"]
    ).exists()


def test_two_external_ids_in_a_file_cannot_resolve_to_one_existing_record(world: World) -> None:
    old = services.create(
        world.users["editor"],
        "acme/protein.Protein",
        world.project,
        {"sequence": KINASES[0], "lims_id": "LIMS-1"},
    ).entity
    run, _ = started(
        world,
        lambda f: lines(
            protein(KINASES[0], "Match", f, lims_id="LIMS-1"),
            protein(KINASES[0], "Other record", f, lims_id="LIMS-2"),
        ),
    )
    body = finished(world, run)
    assert body["status"] == "failed", body
    (error,) = body["error"]["errors"]
    assert error["pointer"] == "#/creates_file/2/values/lims_id"
    assert "#/creates_file/1" in error["detail"]
    assert Entity.objects.get(pk=old.pk).revision == 1
    assert body["effects"] == []


@pytest.mark.parametrize("second_id", ["LIMS-1", "LIMS-2"])
def test_an_external_record_occurs_once_across_inline_and_file(
    world: World, second_id: str
) -> None:
    inline = {
        "type": "acme/protein.Protein",
        "values": {"sequence": KINASES[0], "lims_id": "LIMS-1", "name": "Inline"},
    }
    run, _ = started(
        world,
        lambda f: lines(protein(KINASES[0], "In file", f, lims_id=second_id)),
        creates=[inline],
    )
    body = finished(world, run)
    assert body["status"] == "failed", body
    assert body["error"]["code"] == "invalid_output"
    (error,) = body["error"]["errors"]
    assert error["pointer"].startswith("#/creates_file/1")
    assert "#/creates/0" in error["detail"]
    assert not Entity.objects.filter(type=inline["type"]).exists()
    assert body["effects"] == []


@pytest.mark.parametrize("defaulted", [False, True], ids=["file-supplies-key", "file-omits-key"])
def test_one_external_key_preserves_both_registrations(
    world: World, defaulted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    type_name = "acme/protein.Protein"
    inline = {
        "type": type_name,
        "values": {"sequence": KINASES[0], **({"lims_id": "LIMS-1"} if defaulted else {})},
        "origin": {"source": "catalog", "id": "inline"},
    }
    if defaulted:
        fields = shape(type_name).part_of("lims_id").fields
        monkeypatch.setitem(
            fields,
            "lims_id",
            dataclasses.replace(fields["lims_id"], default="LIMS-1", has_default=True),
        )
    run, _ = started(
        world,
        lambda f: lines(
            protein(KINASES[0], "File", f, **({} if defaulted else {"lims_id": "LIMS-1"}))
        ),
        creates=[inline],
    )
    body = finished(world, run)
    assert body["status"] == "succeeded", body
    (effect,) = body["effects"]
    assert effect["kind"] == "created"
    record = Entity.objects.get(pk=effect["entity"])
    assert (
        api_as(world, "editor").get(f"/entities/{record.hrid}").json()["values"]["lims_id"]
        == "LIMS-1"
    )
    assert set(
        ExternalOrigin.objects.filter(entity=record).values_list("identifier", flat=True)
    ) == {"inline", "File"}
    imported = RunImport.objects.get(run_id=run["id"])
    assert (imported.created, imported.updated, imported.reused, imported.registered) == (
        1,
        0,
        0,
        1,
    )


def test_lines_that_match_entities_update_them(world: World) -> None:
    from test_webhooks import make

    first, _ = started(
        world,
        lambda f: lines(
            protein(KINASES[0], "A", f, lims_id="LIMS-1"),
            protein(KINASES[1], "B", f, lims_id="LIMS-2"),
        ),
    )
    assert finished(world, first)["status"] == "succeeded"
    make(world, "https://lims.example.com/hooks", ["entity.updated", "entity.imported"])
    second, _ = started(
        world,
        lambda f: lines(
            protein(KINASES[0], "A2", f, lims_id="LIMS-1"),  # changed
            protein(KINASES[1], "B", f, lims_id="LIMS-2"),  # the same
            protein(KINASES[2], "C", f, lims_id="LIMS-3"),  # new
        ),
    )
    body = finished(world, second)
    assert body["status"] == "succeeded", body
    imported = RunImport.objects.get(run_id=second["id"])
    assert (imported.created, imported.updated, imported.reused) == (1, 1, 1)
    assert (
        Entity.objects.filter(type="acme/protein.Protein", owner_project__isnull=False).count() == 3
    )
    api = api_as(world, "editor")
    (one,) = api.get(
        "/entities/lookup", type="acme/protein.Protein", field="lims_id", value="LIMS-1"
    ).json()
    assert (one["values"]["name"], one["revision"]) == ("A2", 2)
    origins = api.get(f"/entities/{one['hrid']}/origins").json()
    assert [(o["run"]["id"], o["revision"]) for o in origins] == [
        (second["id"], 2),
        (first["id"], 1),
    ]
    effects = {e["hrid"]: e["kind"] for e in seen(world, second)["effects"]}
    assert sorted(effects.values()) == ["created", "reused", "updated"]
    # One event for the import, none for each entity it changed.
    events = list(Delivery.objects.exclude(event="ping").values_list("event", flat=True))
    assert events == ["entity.imported"]


def test_each_line_keeps_how_it_was_made_and_who_says_so(world: World) -> None:
    """ADR 0106, through both ways a creates file is written: new entities in SQL, and
    lines that match one by one."""
    software = [{"name": "Fold", "version": "2"}, {"name": "Relax", "version": "1"}]
    predicted = {"kind": "computational", "software": [{"name": "AlphaFold", "version": "2.3.2"}]}

    def first_lines(f: str) -> bytes:
        a = protein(KINASES[0], "A", f, lims_id="LIMS-1")
        a["origin"]["made"] = predicted
        return lines(
            {**a, "made_with": ["fold"]},
            protein(KINASES[1], "B", f),
            protein(KINASES[1], "B again", f),  # found again: kept once, for its first line
        )

    first, _ = started(world, first_lines, software)
    assert finished(world, first)["status"] == "succeeded"
    api = api_as(world, "editor")

    a, b = (e["hrid"] for e in seen(world, first)["effects"])

    def making(hrid: str) -> list[tuple[str, str, str, str]]:
        return [
            (m["by"], m["kind"], m["software"], m["run"]["id"])
            for m in api.get(f"/entities/{hrid}/making").json()
        ]

    run = first["id"]
    assert making(a) == [
        ("iki", "computational", "Fold", run),
        ("source", "computational", "AlphaFold", run),
    ]
    assert making(b) == [
        ("iki", "computational", "Fold", run),
        ("iki", "computational", "Relax", run),
    ]

    # A line that matches LIMS-1 is written one by one: kept once, not again in SQL.
    def matching(f: str) -> bytes:
        a = protein(KINASES[0], "A2", f, lims_id="LIMS-1")
        a["origin"]["made"] = {"kind": "experimental", "method": "X-RAY DIFFRACTION"}
        return lines({**a, "made_with": ["Relax"]})

    second, _ = started(world, matching, software)
    assert finished(world, second)["status"] == "succeeded"
    # Iki's records first, then each source's word.
    assert making(a) == [
        ("iki", "computational", "Fold", run),
        ("iki", "computational", "Relax", second["id"]),
        ("source", "computational", "AlphaFold", run),
        ("source", "experimental", "", second["id"]),
    ]


def test_lines_claim_the_same_entities_without_a_record(world: World) -> None:
    from iki_server.entities import services

    editor = world.users["editor"]
    before = services.create(
        editor, "acme/protein.Protein", world.project, {"sequence": KINASES[0]}
    ).entity
    taken = services.create(
        editor, "acme/protein.Protein", world.project, {"sequence": KINASES[1], "lims_id": "LIMS-9"}
    ).entity
    clash, _ = started(
        world,
        lambda f: lines(
            protein(KINASES[0], "A", f, lims_id="LIMS-1"),
            protein(KINASES[1], "B", f, lims_id="LIMS-2"),
            protein(KINASES[2], "C", f, lims_id="LIMS-3"),
            protein(KINASES[2], "C2", f, lims_id="LIMS-4"),
        ),
    )
    body = finished(world, clash)
    assert body["status"] == "failed"
    problems = {e["pointer"]: e["detail"] for e in body["error"]["errors"]}
    hrid = Entity.objects.get(pk=taken.pk).hrid
    assert problems == {
        "#/creates_file/2/values/lims_id": f"It has the same sequence as {hrid}, whose "
        "lims_id is LIMS-9 already: they'd be one entity, which can't be two records.",
        "#/creates_file/4/values/lims_id": "It has the same sequence as line 3: they'd be one "
        "entity, which can't be two records.",
    }
    run, _ = started(world, lambda f: lines(protein(KINASES[0], "A", f, lims_id="LIMS-1")))
    assert finished(world, run)["status"] == "succeeded"
    (effect,) = seen(world, run)["effects"]
    claimed = Entity.objects.get(pk=before.pk)
    assert (effect["hrid"], effect["kind"]) == (claimed.hrid, "updated")
    (one,) = (
        api_as(world, "editor")
        .get("/entities/lookup", type="acme/protein.Protein", field="lims_id", value="LIMS-1")
        .json()
    )
    assert one["hrid"] == claimed.hrid


def test_a_creates_files_lines_cant_refer_to_one_another(world: World) -> None:
    run, _ = started(
        world,
        lambda f: lines(
            {**protein(KINASES[0], "A", f), "key": "a"},
            {
                "type": "acme/protein.ProteinStructure",
                "values": {"method": "predicted", "chains": [{"$create": "a"}]},
            },
        ),
    )
    body = finished(world, run)
    assert body["status"] == "failed"
    problems = {e["pointer"]: e["detail"] for e in body["error"]["errors"]}
    refused = (
        "A creates file's lines can't refer to one another: give the creates they refer to in "
        "`creates`, with a `key`, and refer to them with `$create`."
    )
    assert problems["#/creates_file/1/key"] == refused
    assert not Entity.objects.filter(type="acme/protein.Protein").exists()


HUMAN = {
    "type": "iki/core.Organism",
    "key": "taxon:9606",
    "values": {"taxon_id": 9606, "scientific_name": "Homo sapiens"},
    "fills": ["scientific_name"],
}


def test_a_creates_files_lines_refer_to_the_creates_written_before_them(world: World) -> None:
    """A million sequences from a few organisms (ADR 0140): the organisms go in `creates`,
    and each line refers to its own by key."""
    mouse = {"type": "iki/core.Organism", "key": "taxon:10090", "values": {"taxon_id": 10090}}
    run, _ = started(
        world,
        lambda f: lines(
            *(
                protein(s, f"K{n}", f, organism={"$create": "taxon:9606"})
                for n, s in enumerate(KINASES[:3])
            ),
            protein(KINASES[3], "K3", f, organism={"$create": "taxon:10090"}),
        ),
        creates=[HUMAN, mouse],
    )
    body = finished(world, run)
    assert body["status"] == "succeeded", body.get("error")
    editor = api_as(world, "editor")
    organisms = editor.get("/entities", type="iki/core.Organism").json()["items"]
    entries = {o["values"]["taxon_id"]: o["entry"]["id"] for o in organisms}
    assert sorted(entries) == [9606, 10090]
    found = {
        e["values"]["name"]: e["values"]["organism"]
        for e in editor.get("/entities", type="acme/protein.Protein").json()["items"]
    }
    # A reference to an organism holds its registry entry (ADR 0115).
    assert found == {
        "K0": entries[9606],
        "K1": entries[9606],
        "K2": entries[9606],
        "K3": entries[10090],
    }
    assert not _staged_tables()


def test_a_line_refers_only_to_a_keyed_create(world: World) -> None:
    run, _ = started(
        world,
        lambda f: lines(protein(KINASES[0], "A", f, organism={"$create": "taxon:1"})),
        creates=[HUMAN],
    )
    body = finished(world, run)
    assert body["status"] == "failed"
    problems = {e["pointer"]: e["detail"] for e in body["error"]["errors"]}
    assert set(problems) == {"#/creates_file/1/values/organism"}, problems
    # Nothing was written: not even the organism the lines could have referred to.
    assert not Entity.objects.filter(
        type__in=["acme/protein.Protein", "iki/core.Organism"]
    ).exists()


def _staged_tables() -> list[Any]:
    """Lines still staged: none, once an import has ended and been cleared."""
    from iki_server.runs.models import ImportLine

    return list(ImportLine.objects.values_list("line", flat=True))


def test_an_import_goes_on_from_where_it_was_interrupted(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(imports, "CHUNK_LINES", 3)
    sequences = [f"MKVLEIC{chr(65 + n)}{chr(65 + m)}" for n in range(5) for m in range(4)]
    run, _ = started(world, lambda f: lines(*(protein(s, s, f) for s in sequences)))
    imported = RunImport.objects.get(run_id=run["id"])
    # Its runner stops after two chunks…
    calls = {"n": 0}
    real = imports._check_chunk

    def stops(*args: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("The pod was evicted.")
        real(*args)

    monkeypatch.setattr(imports, "_check_chunk", stops)
    with pytest.raises(RuntimeError):
        imports._advance(imported.pk)
    imported.refresh_from_db()
    assert (imported.lines, imported.staged) == (6, 6)
    monkeypatch.setattr(imports, "_check_chunk", real)
    # …and the next goes on from the third line, not the first.
    body = finished(world, run)
    assert body["status"] == "succeeded", body
    assert len(body["effects"]) == 20
    assert (
        Entity.objects.filter(type="acme/protein.Protein", owner_project__isnull=False).count()
        == 20
    )


def test_an_import_that_keeps_failing_gives_up(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, _ = started(world, lambda f: lines(protein(KINASES[0], "A", f)))

    def broken(*args: Any) -> None:
        raise RuntimeError("The store is down.")

    monkeypatch.setattr(imports, "_check_chunk", broken)
    imported = RunImport.objects.get(run_id=run["id"])
    for _ in range(imports.MAX_FAILURES):
        _step_once(imported)
    body = seen(world, run)
    assert body["status"] == "failed"
    assert body["error"]["code"] == "import_failed"
    assert "The store is down." in body["error"]["message"]


def _step_once(imported: RunImport) -> None:
    from iki_server.tasks.backend import Retry

    with contextlib.suppress(Retry):
        imports.step(str(imported.pk))


def test_cancelling_an_import_writes_nothing(world: World) -> None:
    run, _ = started(world, lambda f: lines(protein(KINASES[0], "A", f)))
    cancelled = api_as(world, "editor").post(f"/runs/{run['id']}/cancel", {})
    assert cancelled.status_code == 200, cancelled.content
    body = finished(world, run)
    assert body["status"] == "cancelled"
    assert not Entity.objects.filter(type="acme/protein.Protein").exists()
    assert not _staged_tables()


def test_an_import_checks_again_when_a_plugin_changes_before_its_written(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, _ = started(world, lambda f: lines(protein(KINASES[0], "A", f)))
    imported = RunImport.objects.get(run_id=run["id"])
    imported.schema = "0" * 64  # As if checked against other plugins.
    imported.save()
    body = finished(world, run)
    assert body["status"] == "succeeded", body
    assert len(body["effects"]) == 1


def test_a_staged_import_checks_the_projects_flow_at_commit(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from iki_server.setup import starters
    from iki_server.setup.models import StarterApplication

    # Use the fixture deployment's importer and its real worker/COPY path.
    assay = dataclasses.replace(
        starters.DOSE_RESPONSE,
        result_base="acme/dna.Sequence",
        flows={"research": "acme/protein.Protein", "regulated_qc": "acme/dna.DNASequence"},
    )
    monkeypatch.setattr(starters, "CATALOG", (assay,))
    application = StarterApplication.objects.create(
        project=world.project,
        requester_project=Project.objects.create(team=world.team, name="Requests"),
        starter=assay.id,
        version=assay.version,
        flow="research",
        applied_by=world.users["admin"],
    )
    run, _ = started(world, lambda file: lines(protein(KINASES[0], "K1", file)))
    imported = RunImport.objects.get(run_id=run["id"])
    finish = imports._finish

    def switched(staged: RunImport) -> bool:
        assert staged.staged == 1
        application.flow = "regulated_qc"
        application.save()
        return finish(staged)

    monkeypatch.setattr(imports, "_finish", switched)
    body = finished(world, run)
    assert body["status"] == "failed", body
    assert "uses regulated_qc" in body["error"]["errors"][0]["detail"]
    assert not Entity.objects.filter(type="acme/protein.Protein").exists()
    imported.refresh_from_db()
    assert imported.created == 0


def test_webhooks_hear_of_an_import_once(world: World) -> None:
    from test_webhooks import make

    make(world, "https://lims.example.com/hooks", ["entity.created", "entity.imported"])
    run, _ = started(world, lambda f: lines(*(protein(s, s, f) for s in KINASES)))
    finished(world, run)
    events = list(Delivery.objects.values_list("event", flat=True))
    assert events.count("entity.imported") == 1
    assert "entity.created" not in events


def test_a_creates_file_must_be_the_runs_own_upload(world: World) -> None:
    file_id = stored(world)
    made = launch(api_as(world, "editor"), world.project, {}, IMPORT, params={"file": file_id})
    pod = worker(world)
    job = pod.lease().json()
    answer = pod.report(job, "complete", output=output(creates_file=file_id)).json()
    assert answer["run_status"] == "failed"
    assert answer["errors"] == [
        {
            "pointer": "#/creates_file",
            "detail": "Give a file this run uploaded, once it's ready.",
        }
    ]
    assert ActionRun.objects.get(pk=made.json()["id"]).status == "failed"


def test_a_line_is_plainly_a_create_only_when_the_schema_says_so() -> None:
    from iki_spec import schemas

    file_id = "0193a0b4-0000-7000-8000-000000000000"
    cases: list[Any] = [
        protein(KINASES[0], "A", file_id),
        {"type": "acme/protein.Protein", "values": {}},
        {"type": "acme/protein.Protein", "values": {}, "derived_from": ["a", "a"]},
        {"type": "acme/protein.Protein", "values": {}, "derived_from": "a"},
        {"type": "acme/protein.Protein", "values": []},
        {"type": "Protein", "values": {}},
        {"type": "acme/protein.Protein"},
        {"type": "acme/protein.Protein", "values": {}, "origin": None},
        {"type": "acme/protein.Protein", "values": {}, "origin": {"source": "x"}},
        {"type": "acme/protein.Protein", "values": {}, "origin": {"source": "", "id": "x"}},
        {"type": "acme/protein.Protein", "values": {}, "origin": {"source": "s" * 101, "id": "x"}},
        {
            "type": "acme/protein.Protein",
            "values": {},
            "origin": {"source": "s", "id": "x", "url": "http://a"},
        },
        {
            "type": "acme/protein.Protein",
            "values": {},
            "origin": {"source": "s", "id": "x", "sha256": "A" * 64},
        },
        {
            "type": "acme/protein.Protein",
            "values": {},
            "origin": {"source": "s", "id": "x", "file": 1},
        },
        {
            "type": "acme/protein.Protein",
            "values": {},
            "origin": {"source": "s", "id": "x", "more": 1},
        },
        {
            "type": "acme/protein.Protein",
            "values": {},
            "origin": {
                "source": "s",
                "id": "x",
                "url": "https://a",
                "version": "1",
                "retrieved_at": "2026-11-11T00:00:00Z",
            },
        },
        [],
        "x",
    ]
    for case in cases:
        if imports._plainly_a_create(case):
            assert not schemas.check("run-output", case, part="create"), case


def test_lines_split_across_reads_and_chunks_arrive_whole(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(imports, "READ_BLOCK", 7)
    monkeypatch.setattr(imports, "CHUNK_LINES", 2)
    sequences = [f"MKVLEIC{chr(65 + n)}" for n in range(9)]
    run, _ = started(
        world,
        # The last line has no newline: it counts all the same.
        lambda f: lines(*(protein(s, s, f) for s in sequences)).removesuffix(b"\n"),
    )
    body = finished(world, run)
    assert body["status"] == "succeeded", body
    names = [
        api_as(world, "editor").get(f"/entities/{e['hrid']}").json()["values"]["name"]
        for e in body["effects"]
    ]
    assert names == sequences
    imported = RunImport.objects.get(run_id=run["id"])
    assert (imported.lines, imported.offset) == (9, imported.size)
