"""Bulk imports (ADR 0056): creates from a file, checked a chunk at a time and resumable,
then written all at once, or not at all."""

import contextlib
import hashlib
import json
from typing import Any

import pytest
import requests
from org_world import World
from test_api_entities import api_as, create
from test_files import run_tasks
from test_run_imports import IMPORT, origin, stored
from test_run_results import output
from test_runs import launch, world
from test_workers import WorkerClient, keep_connection, worker

from iki_server.entities.models import Entity, ProjectLink, Revision
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
        "type": "protein.Protein",
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


def started(world: World, make: Any, **extra: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """An import run whose command wrote ``make(file_id)``'s lines to a creates file.
    Returns the run and the worker's answer."""
    file_id = stored(world)
    run = launch(api_as(world, "editor"), world.project, {}, IMPORT, params={"file": file_id})
    assert run.status_code == 201, run.content
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
    assert not Entity.objects.filter(type="protein.Protein").exists()

    body = finished(world, run)
    assert body["status"] == "succeeded", body
    assert body["metrics"] == {"records": 4}
    assert [e["kind"] for e in body["effects"]] == ["created"] * 4
    made = list(Entity.objects.filter(type="protein.Protein").order_by("hrid"))
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
    assert sorted(revision.changed_fields) == ["name", "sequence"]
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


def test_its_lines_take_every_kind_of_value_a_create_does(world: World) -> None:
    editor = api_as(world, "editor")
    source = create(
        editor, world.project, {"name": "S", "sequence": "MKKK"}, "protein.Protein"
    ).json()
    run, _ = started(
        world,
        lambda f: lines(
            protein(
                KINASES[0],
                "ABL1",
                f,
                organism="Homo sapiens",
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
    assert values["organism"] == "Homo sapiens"
    assert values["source"] == source["id"]
    assert values["temperature_optimum"] == pytest.approx(37.0)
    assert values["uniprot_ids"] == ["P00519", "Q59FK4"]
    assert values["summary"] == "A tyrosine kinase."


def test_duplicates_are_reused_as_one_at_a_time(world: World) -> None:
    editor = api_as(world, "editor")
    existing = create(
        editor, world.project, {"name": "Old", "sequence": KINASES[1]}, "protein.Protein"
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
    assert Entity.objects.filter(type="protein.Protein").count() == 3
    imported = RunImport.objects.get(run_id=run["id"])
    assert (imported.created, imported.reused, imported.linked) == (2, 1, 0)


def test_a_project_that_keeps_its_entities_has_none_reused_or_referred_to(world: World) -> None:
    """ADR 0067: to an import elsewhere, its entities are as if the person couldn't read them."""
    editor = api_as(world, "editor")
    cro = Project.objects.create(team=world.team, name="Oncology CRO", allow_sharing=False)
    ProjectMembership.objects.create(
        project=cro, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    theirs = create(editor, cro, {"name": "Old", "sequence": KINASES[1]}, "protein.Protein").json()
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


def test_a_line_that_doesnt_fit_fails_the_run_and_writes_nothing(world: World) -> None:
    run, _ = started(
        world,
        lambda f: lines(
            protein(KINASES[0], "A", f),
            b"",
            b"{not json",
            protein("not a protein!", "B", f),
            {"type": "dna.DNASequence", "values": {"name": "X", "bases": "ACGT"}},
            {"type": "protein.Protein", "values": {"name": "C", "sequence": "MKK"}, "extra": 1},
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
    assert "doesn't declare that it creates dna.DNASequence" in problems["#/creates_file/5/type"]
    assert not Entity.objects.filter(type="protein.Protein").exists()
    assert RunImport.objects.get(run_id=run["id"]).phase == ImportPhase.ENDED
    assert not _staged_tables()


def test_a_creates_files_lines_cant_refer_to_one_another(world: World) -> None:
    run, _ = started(
        world,
        lambda f: lines(
            {**protein(KINASES[0], "A", f), "key": "a"},
            {
                "type": "protein.ProteinStructure",
                "values": {"method": "predicted", "chains": [{"$create": "a"}]},
            },
        ),
    )
    body = finished(world, run)
    assert body["status"] == "failed"
    problems = {e["pointer"]: e["detail"] for e in body["error"]["errors"]}
    refused = (
        "A creates file's lines can't refer to one another: give creates that do (with `key` "
        "and `$create`) in `creates`."
    )
    assert problems["#/creates_file/1"] == refused
    assert not Entity.objects.filter(type="protein.Protein").exists()


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
    assert Entity.objects.filter(type="protein.Protein").count() == 20


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
    assert not Entity.objects.filter(type="protein.Protein").exists()
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
        {"type": "protein.Protein", "values": {}},
        {"type": "protein.Protein", "values": {}, "derived_from": ["a", "a"]},
        {"type": "protein.Protein", "values": {}, "derived_from": "a"},
        {"type": "protein.Protein", "values": []},
        {"type": "Protein", "values": {}},
        {"type": "protein.Protein"},
        {"type": "protein.Protein", "values": {}, "origin": None},
        {"type": "protein.Protein", "values": {}, "origin": {"source": "x"}},
        {"type": "protein.Protein", "values": {}, "origin": {"source": "", "id": "x"}},
        {"type": "protein.Protein", "values": {}, "origin": {"source": "s" * 101, "id": "x"}},
        {
            "type": "protein.Protein",
            "values": {},
            "origin": {"source": "s", "id": "x", "url": "http://a"},
        },
        {
            "type": "protein.Protein",
            "values": {},
            "origin": {"source": "s", "id": "x", "sha256": "A" * 64},
        },
        {"type": "protein.Protein", "values": {}, "origin": {"source": "s", "id": "x", "file": 1}},
        {"type": "protein.Protein", "values": {}, "origin": {"source": "s", "id": "x", "more": 1}},
        {
            "type": "protein.Protein",
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
