"""The graph (ADR 0123): edges kept by triggers whatever writes, and walks up and down it,
as far as the caller can read."""

import csv
import io
import itertools
import uuid
from typing import Any

import pytest
from api_client import problem
from django.db import connection
from django.utils import timezone
from org_world import World
from test_api_entities import api_as, create, other
from test_runs import launch, protein, world

from iki_server.api import graph as graph_api
from iki_server.audit.models import AuditEvent
from iki_server.entities import search
from iki_server.entities.models import Entity
from iki_server.files import services as files
from iki_server.files.models import Blob, File, FileUse
from iki_server.files.services import EMPTY_SHA256
from iki_server.graph import queries
from iki_server.graph.models import Edge
from iki_server.org.models import Project
from iki_server.plugins import sync
from iki_server.runs.models import Job, Lineage, RunEffect, RunUpload

pytestmark = pytest.mark.django_db

__all__ = ["other", "world"]  # the fixtures, used here too

PROTEIN = "acme/protein.Protein"
SAMPLE = "acme/protein.Sample"
STRUCTURE = "acme/protein.ProteinStructure"
ALIGN = "acme/protein.align_pair"
IMPORT = "acme/protein.import_samples"


def sample(world: World, project: Project, label: str, of: dict[str, Any] | None) -> Any:
    values: dict[str, Any] = {"label": label}
    if of is not None:
        values["protein"] = of["id"]
    made = create(api_as(world), project, values, SAMPLE)
    assert made.status_code == 201, made.content
    return made.json()


def edges(node: Any) -> list[tuple[str, str, str]]:
    return sorted(
        (e.kind, e.field, str(e.on_id))
        for e in Edge.objects.filter(node_id=node["id"] if isinstance(node, dict) else node)
    )


def walk(world: World, root: Any, direction: str, principal: str = "editor", **kw: Any) -> Any:
    user = world.users[principal]
    with search.time_limit():
        found = queries.reach(user, queries.Query(root, direction, **kw))
        rows = found.page(None, 100)
        return {
            "hrids": [hrid for _, _, hrid in rows],
            "depths": {hrid: depth for _, depth, hrid in rows},
            "hidden": found.hidden(),
            "paths": found.paths([node for node, _, _ in rows]),
            "beyond": found.beyond(),
        }


def entry_of(record: dict[str, Any]) -> Any:
    return Entity.objects.get(pk=record["id"]).entry_id


def test_a_reference_is_an_edge_kept_whatever_changes_it(world: World) -> None:
    editor = api_as(world)
    abl1 = protein(editor, world.project, "abl1")
    src = protein(editor, world.project, "src")
    s1 = sample(world, world.project, "S-1", abl1)
    assert edges(s1) == [("reference", "protein", str(entry_of(abl1)))]

    # Changed, then cleared, by any path: here, SQL.
    table = "acme__protein_sample"
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {table} SET protein_id = %s WHERE entity_id = %s",  # noqa: S608 - a test
            [entry_of(src), s1["id"]],
        )
    assert edges(s1) == [("reference", "protein", str(entry_of(src)))]
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {table} SET protein_id = NULL WHERE entity_id = %s",  # noqa: S608 - a test
            [s1["id"]],
        )
    assert edges(s1) == []


def test_a_list_of_references_is_an_edge_each(world: World) -> None:
    editor = api_as(world)
    abl1, src = protein(editor, world.project, "abl1"), protein(editor, world.project, "src")
    blob = Blob.objects.create(sha256="a" * 64, size=10, storage_uri="s3://iki/a")
    pdb = File.objects.create(
        blob=blob, name="1abc.pdb", media_type="chemical/x-pdb", project=world.project
    )
    values = {"structure": str(pdb.pk), "chains": [abl1["id"], src["id"]]}
    made = create(editor, world.project, values, STRUCTURE)
    assert made.status_code == 201, made.content
    body = made.json()
    assert edges(body) == sorted(
        [
            ("reference", "chains", str(entry_of(abl1))),
            ("reference", "chains", str(entry_of(src))),
        ]
    )
    changed = editor.patch(
        f"/entities/{body['id']}",
        {"values": {"chains": [src["id"]]}},
        headers={"If-Match": f'"{body["revision"]}"'},
    )
    assert changed.status_code == 200, changed.content
    assert edges(body) == [("reference", "chains", str(entry_of(src)))]


def test_what_runs_wrote_depends_on_what_they_read(world: World, s3: Any) -> None:
    """An updated entity depends on the run's other inputs; what a run made from a file
    depends on what held the file, and on what the run that wrote it read."""
    editor = api_as(world)
    query, target = protein(editor, world.project, "abl1"), protein(editor, world.project, "src")
    aligned = launch(editor, world.project, {"query": query["id"], "target": target["id"]}, ALIGN)
    assert aligned.status_code == 201, aligned.content
    RunEffect.objects.create(
        run_id=aligned.json()["id"], entity_id=query["id"], kind="updated", revision=2
    )
    assert ("run", "target", target["id"]) in edges(query)
    assert ("run", "query", query["id"]) not in edges(query)

    holder = protein(editor, world.project, "holder")
    upload = files.start(
        world.users["editor"],
        world.project,
        name="samples.csv",
        media_type="text/csv",
        size=0,
        sha256=EMPTY_SHA256,
    )
    upload.refresh_from_db()
    assert upload.file_id is not None
    FileUse.objects.create(file_id=upload.file_id, entity_id=holder["id"], revision=1)
    writer = launch(editor, world.project, {"protein": target["id"]}).json()
    job = Job.objects.filter(run_id=writer["id"]).first()
    assert job is not None
    RunUpload.objects.create(upload=upload, run_id=writer["id"], job=job)
    importing = launch(editor, world.project, {}, IMPORT, params={"file": str(upload.file_id)})
    assert importing.status_code == 201, importing.content
    s1 = sample(world, world.project, "S-1", None)
    RunEffect.objects.create(
        run_id=importing.json()["id"], entity_id=s1["id"], kind="created", revision=1
    )
    assert edges(s1) == sorted([("file", "file", holder["id"]), ("file", "file", target["id"])])
    up = walk(world, s1["id"], "upstream", kinds=("file",))
    assert set(up["hrids"]) == {holder["hrid"], target["hrid"]}
    [step] = up["paths"][_id(holder)]
    assert (step.kind, step.file) == ("file", upload.file_id)


def test_sync_fills_in_what_was_written_while_no_trigger_kept_it(world: World) -> None:
    abl1 = protein(api_as(world), world.project, "abl1")
    with connection.cursor() as cursor:
        cursor.execute("DROP TRIGGER iki_graph__ins ON acme__protein_sample")
    s1 = sample(world, world.project, "S-1", abl1)
    assert edges(s1) == []
    report = sync.sync()
    assert "acme__protein_sample" in report.graph
    assert edges(s1) == [("reference", "protein", str(entry_of(abl1)))]
    # A stale edge goes too.
    with connection.cursor() as cursor:
        cursor.execute("DROP TRIGGER iki_graph__upd ON acme__protein_sample")
        cursor.execute(
            "UPDATE acme__protein_sample SET protein_id = NULL WHERE entity_id = %s", [s1["id"]]
        )
    assert edges(s1) != []
    sync.sync()
    assert edges(s1) == []


def test_only_the_triggers_write_edges(world: World) -> None:
    """The app's role reads the table, and can't write it (core.db_roles)."""
    from iki_server.core import db_roles

    assert "iki_graph_edge" in db_roles.READ_ONLY_TABLES


def test_records_and_their_entries_are_one_thing(world: World) -> None:
    abl1 = protein(api_as(world), world.project, "abl1")
    entry = entry_of(abl1)
    assert edges(abl1) == [("record", "", str(entry))]
    assert ("record", "", abl1["id"]) in edges(entry)


def test_downstream_and_upstream_through_references_and_runs(world: World) -> None:
    editor = api_as(world)
    abl1 = protein(editor, world.project, "abl1")
    s1 = sample(world, world.project, "S-1", abl1)
    made = protein(editor, world.project, "made")
    run = launch(editor, world.project, {"protein": abl1["id"]}).json()
    Lineage.objects.create(
        entity_id=made["id"], parent_id=abl1["id"], parent_revision=1, run_id=run["id"], input="p"
    )
    assert ("run", "p", abl1["id"]) in edges(made)

    down = walk(world, abl1["id"], "downstream")
    assert set(down["hrids"]) == {s1["hrid"], made["hrid"]}
    assert down["hidden"] == 0
    # The sample refers to the protein's entry, which is the protein: one step, as the
    # record and its entry are one thing.
    assert down["depths"] == {s1["hrid"]: 1, made["hrid"]: 1}
    assert [step.kind for step in down["paths"][_id(s1)]] == ["record", "reference"]

    up = walk(world, s1["id"], "upstream")
    assert up["hrids"] == [abl1["hrid"]]
    assert up["depths"] == {abl1["hrid"]: 1}
    assert [s.kind for s in up["paths"][_id(abl1)]] == ["reference", "record"]
    assert up["beyond"] is False

    # Only samples; only runs; and no further than asked.
    assert walk(world, abl1["id"], "downstream", types=(SAMPLE,))["hrids"] == [s1["hrid"]]
    assert walk(world, abl1["id"], "downstream", kinds=("run",))["hrids"] == [made["hrid"]]
    up_one = walk(world, made["id"], "upstream", depth=1)
    assert up_one["hrids"] == [abl1["hrid"]]
    assert up_one["beyond"] is False
    s2 = sample(world, world.project, "S-2", made)
    two = walk(world, s2["id"], "upstream", depth=1)
    assert (two["hrids"], two["beyond"]) == ([made["hrid"]], True)
    assert walk(world, s2["id"], "upstream", depth=2)["depths"] == {
        made["hrid"]: 1,
        abl1["hrid"]: 2,
    }


def test_what_a_purge_tombstoned_is_gone_from_walks(world: World) -> None:
    """A purged plugin's entities stay as tombstones (ADR 0092), and their edges with them;
    walks treat them as unreadable: not found downstream, counted upstream."""
    editor = api_as(world)
    abl1 = protein(editor, world.project, "abl1")
    s1 = sample(world, world.project, "S-1", abl1)
    Entity.objects.filter(pk=s1["id"]).update(purged_at=timezone.now())
    assert walk(world, abl1["id"], "downstream")["hrids"] == []
    Entity.objects.filter(pk=s1["id"]).update(purged_at=None)
    Entity.objects.filter(pk=abl1["id"]).update(purged_at=timezone.now())
    up = walk(world, s1["id"], "upstream")
    assert (up["hrids"], up["hidden"]) == ([], 1)  # its entry, which has no record left


def _id(entity: dict[str, Any]) -> Any:
    return uuid.UUID(entity["id"])


def test_shows_only_what_you_can_read(world: World, other: Project) -> None:
    """The viewer reads the project, not ``other``. What refers to the protein from
    ``other`` isn't there for them; what the project's sample refers to in ``other`` is a
    dead end, counted and never named."""
    editor = api_as(world)
    abl1 = protein(editor, world.project, "abl1")
    theirs = protein(editor, other, "abl1")  # the same identity: a record in other
    secret = sample(world, other, "S-secret", theirs)
    visible = sample(world, world.project, "S-1", abl1)
    down = walk(world, abl1["id"], "downstream", "viewer")
    assert down["hrids"] == [visible["hrid"]]
    assert secret["hrid"] not in str(down)
    assert set(walk(world, abl1["id"], "downstream")["hrids"]) == {visible["hrid"], secret["hrid"]}

    hidden = protein(editor, other, "hidden")
    made = protein(editor, world.project, "made")
    run = launch(editor, other, {"protein": hidden["id"]}).json()
    Lineage.objects.create(
        entity_id=made["id"], parent_id=hidden["id"], parent_revision=1, run_id=run["id"], input="p"
    )
    up = walk(world, made["id"], "upstream", "viewer")
    assert up["hrids"] == []
    assert up["hidden"] == 1


# --- The API ------------------------------------------------------------------------------------


def run_on(world: World, project: Project, entity: dict[str, Any]) -> Any:
    launched = launch(api_as(world, "editor"), project, {"protein": entity["id"]})
    assert launched.status_code == 201, launched.content
    return launched.json()


def made_from(child: dict[str, Any], parent: dict[str, Any], run: dict[str, Any]) -> None:
    Lineage.objects.create(
        entity_id=child["id"],
        parent_id=parent["id"],
        parent_revision=1,
        run_id=run["id"],
        input="protein",
    )


def chain(world: World) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """a → b → c → d, each made by a run on the one before."""
    editor = api_as(world, "editor")
    entities = [protein(editor, world.project, name) for name in ("abl1", "src", "lck", "fyn")]
    runs = []
    for parent, child in itertools.pairwise(entities):
        runs.append(run_on(world, world.project, parent))
        made_from(child, parent, runs[-1])
    return entities, runs


def get(world: World, path: str, principal: str = "editor", **query: Any) -> Any:
    answer = api_as(world, principal).get(path, **query)
    assert answer.status_code == 200, answer.content
    return answer.json()


def test_walks_page_nearest_first_and_say_why(world: World) -> None:
    (a, b, c, d), (r1, r2, r3) = chain(world)
    down = get(world, f"/entities/{a['hrid']}/downstream", limit=2)
    assert down["root"]["id"] == a["id"]
    assert [(i["entity"]["hrid"], i["depth"]) for i in down["items"]] == [
        (b["hrid"], 1),
        (c["hrid"], 2),
    ]
    assert (down["total"], down["hidden"], down["beyond"]) == (3, 0, False)
    assert down["items"][1]["path"] == [
        {
            "kind": "run",
            "field": "protein",
            "run": {"id": r["id"], "hrid": r["hrid"], "action": r["action"]},
            "hidden_run": False,
            "file": None,
            "revision": 1,
            "entity": e,
        }
        for r, e in ((r1, down["items"][0]["entity"]), (r2, down["items"][1]["entity"]))
    ]
    assert down["items"][0]["entity"]["project"]["id"] == str(world.project.pk)
    rest = get(world, f"/entities/{a['hrid']}/downstream", limit=2, cursor=down["next_cursor"])
    assert [i["entity"]["hrid"] for i in rest["items"]] == [d["hrid"]]
    assert rest["next_cursor"] is None
    assert [s["run"]["hrid"] for s in rest["items"][0]["path"]] == [
        r1["hrid"],
        r2["hrid"],
        r3["hrid"],
    ]

    # A cursor is the walk's own.
    other = api_as(world).get(f"/entities/{b['hrid']}/downstream", cursor=down["next_cursor"])
    assert problem(other, 422, "validation_failed")["errors"][0]["parameter"] == "cursor"
    # Upstream, as far as asked.
    up = get(world, f"/entities/{d['hrid']}/upstream", depth=2)
    assert [i["entity"]["hrid"] for i in up["items"]] == [c["hrid"], b["hrid"]]
    assert up["beyond"] is True
    assert api_as(world).get(f"/entities/{d['hrid']}/upstream", depth=31).status_code == 422
    bad = api_as(world).get(f"/entities/{d['hrid']}/upstream", type="acme/nothing.None")
    assert problem(bad, 422, "validation_failed")["errors"][0]["parameter"] == "type"


def test_a_loop_ends(world: World) -> None:
    """A run can reuse an entity that exists already, so lineage can come back on itself."""
    (a, b, c, d), (_r1, _r2, r3) = chain(world)
    made_from(a, d, r3)
    down = get(world, f"/entities/{b['hrid']}/downstream", depth=30)
    assert [i["entity"]["hrid"] for i in down["items"]] == [c["hrid"], d["hrid"], a["hrid"]]
    assert down["beyond"] is False
    looped = get(world, f"/entities/{b['hrid']}/graph", up=10, down=10)
    assert {e["id"] for e in looped["entities"]} == {a["id"], b["id"], c["id"], d["id"]}
    assert len(looped["edges"]) == 4
    assert (looped["earlier"], looped["later"], looped["capped"]) == (False, False, False)


def test_the_impact_list_downloads_as_csv(world: World) -> None:
    editor = api_as(world)
    (a, b, _c, _d), _ = chain(world)
    s1 = sample(world, world.project, "s1", b)
    values = {"sequence": "MKQ", "name": "=HYPERLINK(1)", "source": b["id"]}
    formula = create(editor, world.project, values, PROTEIN).json()
    answer = editor.get(f"/entities/{a['hrid']}/downstream.csv")
    assert answer.status_code == 200, answer.content
    assert answer["Content-Type"] == "text/csv; charset=utf-8"
    assert answer["Content-Disposition"] == f'attachment; filename="{a["hrid"]}-downstream.csv"'
    rows = list(csv.DictReader(io.StringIO(answer.getvalue().decode())))
    assert rows[0]["hrid"] == b["hrid"]
    assert list(rows[0]) == ["hrid", "title", "type", "project", "depth", "archived", "path", "id"]
    found = {r["hrid"]: r for r in rows}
    assert found[s1["hrid"]]["depth"] == "2"
    assert found[formula["hrid"]]["title"] == "'=HYPERLINK(1)"  # never a formula
    assert f"[reference: protein] {s1['hrid']}" in found[s1["hrid"]]["path"]
    assert found[s1["hrid"]]["project"] == world.project.name
    only = editor.get(f"/entities/{a['hrid']}/downstream.csv", type=SAMPLE)
    text = only.getvalue().decode()
    assert [r["hrid"] for r in csv.DictReader(io.StringIO(text))] == [s1["hrid"]]
    event = AuditEvent.objects.filter(action="lineage.exported").latest("occurred_at")
    assert event.actor == world.users["editor"] and event.target_id == a["id"]
    assert event.data["direction"] == "downstream" and event.data["rows"] == 1
    assert event.data["type"] == [SAMPLE]
    assert editor.get(f"/entities/{b['hrid']}/upstream.csv", depth=1).status_code == 200
    event = AuditEvent.objects.filter(action="lineage.exported").latest("occurred_at")
    assert event.target_id == b["id"] and event.data["direction"] == "upstream"
    assert event.data["depth"] == 1 and event.data["file"] == f"{b['hrid']}-upstream.csv"


def test_the_api_shows_only_what_you_can_read(world: World, other: Project) -> None:
    """The viewer reads the project but not ``other``: what's there isn't named, its runs
    aren't, and upstream, the trails into it are counted."""
    (a, b, _c, _d), _ = chain(world)
    editor = api_as(world, "editor")
    secret = protein(editor, other, "secret")
    elsewhere = run_on(world, other, secret)
    made_from(a, secret, elsewhere)
    shared = protein(editor, world.project, "shared")
    made_from(a, shared, elsewhere)

    up = get(world, f"/entities/{b['hrid']}/upstream", "viewer")
    assert [i["entity"]["hrid"] for i in up["items"]] == [a["hrid"], shared["hrid"]]
    assert up["hidden"] == 1
    assert secret["hrid"] not in str(up) and secret["id"] not in str(up)
    assert elsewhere["hrid"] not in str(up) and elsewhere["id"] not in str(up)
    via = up["items"][1]["path"][-1]
    assert (via["run"], via["hidden_run"]) == (None, True)
    full = get(world, f"/entities/{b['hrid']}/upstream")
    assert {i["entity"]["hrid"] for i in full["items"]} == {
        a["hrid"],
        shared["hrid"],
        secret["hrid"],
    }
    assert full["hidden"] == 0

    seen = get(world, f"/entities/{b['hrid']}/graph", "viewer")
    assert secret["id"] not in {e["id"] for e in seen["entities"]}
    assert {"key": "hidden-1", "run": None} in seen["runs"]
    assert elsewhere["hrid"] not in str(seen)
    assert seen["hidden"] == 1

    # Nor can it be asked about what can't be read.
    for path in ("upstream", "downstream", "graph", "downstream.csv"):
        assert api_as(world, "viewer").get(f"/entities/{secret['hrid']}/{path}").status_code == 404


def test_the_graph_goes_as_far_as_asked_and_stops_when_too_big(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    (a, b, c, d), (r1, _r2, _r3) = chain(world)
    whole = get(world, f"/entities/{b['hrid']}/graph")
    assert whole["root"] == b["id"]
    by_id = {e["id"]: e for e in whole["entities"]}
    assert set(by_id) == {a["id"], b["id"], c["id"], d["id"]}
    assert (by_id[a["id"]]["at"], by_id[b["id"]]["at"], by_id[d["id"]]["at"]) == (-1, 0, 2)
    runs = [(e["on"], e["node"], e["run"]) for e in whole["edges"] if e["kind"] == "run"]
    assert (a["id"], b["id"], r1["hrid"]) in runs
    assert {
        "key": r1["hrid"],
        "run": {"id": r1["id"], "hrid": r1["hrid"], "action": r1["action"]},
    } in whole["runs"]
    # Each record and its entry are drawn as one.
    assert not [e for e in whole["edges"] if e["kind"] == "record"]
    assert not [e for e in whole["entities"] if e["entry"]]
    assert (whole["earlier"], whole["later"], whole["capped"]) == (False, False, False)

    near = get(world, f"/entities/{b['hrid']}/graph", up=0, down=1)
    assert {e["id"] for e in near["entities"]} == {b["id"], c["id"]}
    assert (near["earlier"], near["later"]) == (True, True)
    assert api_as(world).get(f"/entities/{b['hrid']}/graph", up=11).status_code == 422

    monkeypatch.setattr(graph_api, "MAX_GRAPH_ENTITIES", 2)
    small = get(world, f"/entities/{b['hrid']}/graph")
    assert len(small["entities"]) == 2
    assert small["capped"] is True
    monkeypatch.setattr(graph_api, "MAX_GRAPH_EDGES", 1)
    assert len(get(world, f"/entities/{b['hrid']}/graph")["edges"]) <= 1


def test_a_walk_that_runs_too_long_says_so(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    (a, *_), _ = chain(world)

    def slow(*args: Any) -> Any:
        raise search.TooSlow

    monkeypatch.setattr(queries, "reach", slow)
    for path in ("downstream", "upstream.csv", "graph"):
        answer = api_as(world).get(f"/entities/{a['hrid']}/{path}")
        assert problem(answer, 422, "search_too_slow")["detail"].startswith("This took too long")


# --- Search ------------------------------------------------------------------------------------


def found(world: World, where: Any, principal: str = "editor", **body: Any) -> Any:
    return api_as(world, principal).post("/entities/search", {"where": where, **body})


def hrids(answer: Any) -> set[str]:
    assert answer.status_code == 200, answer.content
    return {e["hrid"] for e in answer.json()["items"]}


def test_search_finds_what_is_up_or_downstream_with_any_other_condition(world: World) -> None:
    (a, b, c, _d), _ = chain(world)
    s1 = sample(world, world.project, "S-1", b)
    s2 = sample(world, world.project, "S-2", c)
    s3 = sample(world, world.project, "S-3", None)
    down = {"field": "downstream", "op": "of", "value": a["hrid"]}
    assert hrids(found(world, down, type=SAMPLE)) == {s1["hrid"], s2["hrid"]}
    near = {"field": "downstream", "op": "of", "value": {"entity": a["id"], "depth": 2}}
    assert hrids(found(world, near, type=SAMPLE)) == {s1["hrid"]}
    runs = {"field": "downstream", "op": "of", "value": {"entity": a["hrid"], "edges": ["run"]}}
    assert s1["hrid"] not in hrids(found(world, runs))
    both = {"all": [down, {"field": "label", "op": "eq", "value": "S-2"}]}
    assert hrids(found(world, both, type=SAMPLE)) == {s2["hrid"]}
    up = {"field": "upstream", "op": "of", "value": s2["hrid"]}
    assert {a["hrid"], b["hrid"], c["hrid"]} <= hrids(found(world, up))
    # Anything that way, or nothing.
    used = {"field": "downstream", "op": "is_set", "value": False}
    assert s3["hrid"] in hrids(found(world, used, type=SAMPLE))
    derived = {"field": "upstream", "op": "is_set", "value": True}
    assert hrids(found(world, derived, type=SAMPLE)) == {s1["hrid"], s2["hrid"]}
    # As far as the searcher reads: what they can't is as good as not there.
    hidden = problem(found(world, down, "outsider"), 422, "validation_failed")["errors"]
    missing = {**down, "value": "PRT-999999"}
    assert hidden == problem(found(world, missing), 422, "validation_failed")["errors"]
    assert hidden[0]["pointer"] == "#/where/value"


@pytest.mark.parametrize(
    ("value", "pointer"),
    [
        ("PRT-999999", "#/where/value"),
        ({"entity": "PRT-000001", "depth": 0}, "#/where/value/depth"),
        ({"entity": "PRT-000001", "depth": 31}, "#/where/value/depth"),
        ({"entity": "PRT-000001", "edges": ["record"]}, "#/where/value/edges"),
        ({"entity": "PRT-000001", "far": 1}, "#/where/value/far"),
        ({"depth": 2}, "#/where/value/entity"),
    ],
)
def test_search_says_what_is_wrong_with_a_walk(world: World, value: Any, pointer: str) -> None:
    protein(api_as(world), world.project, "abl1")
    answer = found(world, {"field": "downstream", "op": "of", "value": value})
    assert pointer in {e["pointer"] for e in problem(answer, 422, "validation_failed")["errors"]}


def test_a_search_walks_three_times_at_most(world: World) -> None:
    abl1 = protein(api_as(world), world.project, "abl1")
    one = {"field": "downstream", "op": "of", "value": abl1["hrid"]}
    assert found(world, {"all": [one] * 3}).status_code == 200
    assert (
        problem(found(world, {"all": [one] * 4}), 422, "validation_failed")["errors"][0]["pointer"]
        == "#/where/all/3"
    )


def test_an_identity_is_one_thing_in_every_project_you_read(world: World, other: Project) -> None:
    """Records of one registry entry in two projects: what refers to the entry, or was made
    from either record, is downstream of each, and the path says which project it went by."""
    editor = api_as(world)
    ours = protein(editor, world.project, "abl1")
    theirs = protein(editor, other, "abl1")
    assert entry_of(ours) == entry_of(theirs)
    referring = sample(world, other, "S-1", theirs)
    made = protein(editor, other, "made")
    made_from(made, theirs, run_on(world, other, theirs))

    down = get(world, f"/entities/{ours['hrid']}/downstream", project=str(world.project.pk))
    by = {i["entity"]["hrid"]: i for i in down["items"]}
    assert set(by) == {referring["hrid"], made["hrid"]}  # not theirs: it's the same protein
    assert [s["kind"] for s in by[referring["hrid"]]["path"]] == ["reference"]
    [via, step] = by[made["hrid"]]["path"]
    assert (via["kind"], via["entity"]["id"], via["entity"]["project"]["name"]) == (
        "record",
        theirs["id"],
        other.name,
    )
    assert (step["kind"], step["entity"]["id"]) == ("run", made["id"])
    # In a picture, the entry joins the two records.
    drawn = get(world, f"/entities/{ours['id']}/graph")
    entries = [e for e in drawn["entities"] if e["entry"]]
    assert [e["id"] for e in entries] == [str(entry_of(ours))]
    joins = {(e["node"], e["on"]) for e in drawn["edges"] if e["kind"] == "record"}
    assert joins == {(ours["id"], entries[0]["id"]), (theirs["id"], entries[0]["id"])}
    # The viewer reads only ours: nothing of the other project's is there for them.
    seen = get(world, f"/entities/{ours['hrid']}/downstream", "viewer")
    assert seen["items"] == [] and seen["total"] == 0
