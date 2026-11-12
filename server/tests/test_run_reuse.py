"""Reuse, Reproduce exactly and seeds (ADR 0057): an identical deterministic run reuses an
earlier one's results, a run can be done again exactly to check its results, and every
run's command gets a seed it gets again then."""

from typing import Any

import pytest
from api_client import problem
from org_world import World
from test_api_entities import api_as, patch
from test_run_imports import IMPORT, importing, origin, stored
from test_run_results import done, output, weight
from test_runs import launch, launch_each, protein, world
from test_workers import WorkerClient, keep_connection, worker

from iki_server.audit.models import AuditEvent
from iki_server.entities.models import Entity, ProjectLink
from iki_server.files import services as files
from iki_server.files.services import EMPTY_SHA256
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.runs import reuse, services
from iki_server.runs.models import ActionRun, Job, RunEffect

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # the fixtures, used here too

SHA = "a" * 40
BUILT = "sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def storage(s3: Any) -> Any:
    return s3


@pytest.fixture(autouse=True)
def built(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment was built from a lockfile, so its plugins have commits."""
    monkeypatch.setattr(services, "plugin_sha", lambda plugin: SHA)


def pod(world: World, name: str = "pod-1") -> WorkerClient:
    code = [{"name": p, "sha": SHA, "environments": {"protein-tools": BUILT}} for p in ("protein",)]
    made: WorkerClient = worker(world, name=name, plugins=code)
    return made


KINASES: list[dict[str, Any]] = [
    {"type": "protein.Protein", "values": {"name": "ABL1", "sequence": "MKVLEIC"}},
    {"type": "protein.Protein", "values": {"name": "SRC", "sequence": "MGSNKSK"}},
]


def ran_import(world: World, file_id: str, **document: Any) -> tuple[dict[str, Any], Any]:
    """An import of `file_id` that ran and made the kinases."""
    run = importing(world, file_id).json()
    assert run["status"] == "queued", run
    worker_ = pod(world, name=f"pod-{ActionRun.objects.count()}")
    job = worker_.lease().json()
    creates = [{**c, "origin": origin(file_id, c["values"]["name"])} for c in KINASES]
    answer = done(worker_, job, output(creates=creates, metrics={"records": 2}, **document))
    assert answer["run_status"] == "succeeded", answer
    return run, job


def seen(world: World, run: dict[str, Any], principal: str = "editor") -> dict[str, Any]:
    got = api_as(world, principal).get(f"/runs/{run['hrid']}")
    assert got.status_code == 200, got.content
    body: dict[str, Any] = got.json()
    return body


# --- Reuse ---------------------------------------------------------------------------------------


def test_an_identical_run_reuses_the_results_instead_of_running(world: World) -> None:
    file_id = stored(world)
    first, _ = ran_import(world, file_id)
    made = sorted(e["hrid"] for e in seen(world, first)["effects"])

    again = importing(world, file_id)
    assert again.status_code == 201, again.content
    run = again.json()
    assert run["status"] == "succeeded"
    assert (run["reused"], run["reused_from"]["hrid"]) == (True, first["hrid"])
    assert run["attempts"] == 0
    assert run["can"] == {"cancel": False, "reproduce": True, "run_again": True}
    assert seen(world, first)["can"]["run_again"] is False
    assert not Job.objects.filter(run_id=run["id"]).exists()
    assert sorted(e["hrid"] for e in run["effects"]) == made
    assert {e["kind"] for e in run["effects"]} == {"reused"}
    assert run["effect_counts"] == {"created": 0, "updated": 0, "reused": 2}
    # It shows the results it reused, and the seed they were made with.
    assert run["metrics"] == {"records": 2}
    assert run["seed"] == seen(world, first)["seed"]
    launched = AuditEvent.objects.get(action="run.launched", target_id=run["id"])
    assert launched.data["reused_from"] == first["hrid"]


def test_a_file_param_counts_by_its_content(world: World) -> None:
    first, _ = ran_import(world, stored(world))
    # The same bytes, uploaded again as another file.
    other = stored(world, name="again.fasta")
    run = importing(world, other).json()
    assert run["reused_from"]["hrid"] == first["hrid"]


def test_asking_to_run_again_runs_it(world: World) -> None:
    file_id = stored(world)
    ran_import(world, file_id)
    run = launch(api_as(world), world.project, {}, IMPORT, params={"file": file_id}, reuse=False)
    assert run.status_code == 201, run.content
    assert (run.json()["status"], run.json()["reused"]) == ("queued", False)


def test_a_run_reads_a_new_revision_so_it_runs(world: World) -> None:
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    first = launch(editor, world.project, {"protein": abl["id"]}).json()
    worker_ = pod(world)
    job = worker_.lease().json()
    assert done(worker_, job, output(updates=weight(first)))["run_status"] == "succeeded"
    # It changed what it read: run on that, it runs.
    again = launch(editor, world.project, {"protein": abl["id"]}).json()
    assert again["status"] == "queued"
    assert again["inputs"][0]["revision"] == first["inputs"][0]["revision"] + 1


def test_local_code_is_never_reused(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "plugin_sha", lambda plugin: "")
    file_id = stored(world)
    run = importing(world, file_id).json()
    assert run["status"] == "queued"
    assert ActionRun.objects.get(pk=run["id"]).reuse_key == ""


def test_only_runs_the_launcher_can_read_are_reused(world: World) -> None:
    file_id = stored(world)
    first, _ = ran_import(world, file_id)
    # Someone in another project, with the same file: it runs again, so nothing tells them
    # an identical run happened somewhere they can't see.
    elsewhere = Project.objects.create(team=world.team, name="Elsewhere")
    stranger = world.users["outsider"]
    ProjectMembership.objects.create(project=elsewhere, user=stranger, role=ProjectRole.EDITOR)
    theirs = files.start(
        stranger, elsewhere, name="k.fasta", media_type="text/plain", size=0, sha256=EMPTY_SHA256
    )
    theirs.refresh_from_db()
    run = launch(
        api_as(world, "outsider"), elsewhere, {}, IMPORT, params={"file": str(theirs.file_id)}
    )
    assert run.status_code == 201, run.content
    assert run.json()["status"] == "queued"
    keys = ActionRun.objects.filter(pk__in=[first["id"], run.json()["id"]])
    assert len({r.reuse_key for r in keys}) == 1  # identical, but not theirs to reuse


def test_reused_entities_are_linked_into_the_runs_project(world: World) -> None:
    file_id = stored(world)
    first, _ = ran_import(world, file_id)
    second = Project.objects.create(team=world.team, name="Second")
    ProjectMembership.objects.create(
        project=second, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    # The same file, uploaded to the second project: a run reads only its own project's.
    again = stored(world, project=second)
    run = launch(api_as(world), second, {}, IMPORT, params={"file": again}).json()
    assert run["reused_from"]["hrid"] == first["hrid"]
    made = [e["entity"] for e in run["effects"]]
    assert ProjectLink.objects.filter(project=second, entity_id__in=made).count() == 2
    assert AuditEvent.objects.filter(action="entity.linked", project=second).count() == 2


def test_a_project_that_keeps_its_entities_has_no_results_reused_elsewhere(world: World) -> None:
    """ADR 0067: reusing them would add them to the other project."""
    file_id = stored(world)
    first, _ = ran_import(world, file_id)
    second = Project.objects.create(team=world.team, name="Second")
    ProjectMembership.objects.create(
        project=second, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    world.project.allow_sharing = False
    world.project.save()

    run = launch(api_as(world), second, {}, IMPORT, params={"file": stored(world, project=second)})

    assert run.status_code == 201, run.content
    assert run.json()["reused_from"] is None and run.json()["status"] == "queued"
    assert not ProjectLink.objects.filter(project=second).exists()
    # Its own project still reuses them.
    here = launch(api_as(world), world.project, {}, IMPORT, params={"file": file_id}).json()
    assert here["reused_from"]["hrid"] == first["hrid"]


def test_a_run_whose_entities_were_archived_isnt_reused(world: World) -> None:
    file_id = stored(world)
    first, _ = ran_import(world, file_id)
    gone = RunEffect.objects.filter(run_id=first["id"]).first()
    assert gone is not None
    Entity.objects.filter(pk=gone.entity_id).update(archived_at="2026-11-11T00:00:00Z")
    assert importing(world, file_id).json()["status"] == "queued"


def test_a_run_that_made_too_many_to_link_isnt_reused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reuse, "MAX_EFFECTS", 1)
    file_id = stored(world)
    ran_import(world, file_id)
    assert importing(world, file_id).json()["status"] == "queued"


def test_a_batch_reuses_what_it_can_and_runs_the_rest(world: World) -> None:
    editor = api_as(world, "editor")
    a, b = protein(editor, world.project, "a"), protein(editor, world.project, "b")
    # compute_properties on a, which changed nothing: so its revision is still the one read.
    first = launch(editor, world.project, {"protein": a["id"]}).json()
    worker_ = pod(world)
    job = worker_.lease().json()
    answer = done(worker_, job, output(metrics={"isoelectric_point": 7.1}))
    assert answer["run_status"] == "succeeded", ActionRun.objects.get(pk=first["id"]).error

    made = launch_each(editor, world.project, [a["id"], b["id"]])
    assert made.status_code == 201, made.content
    runs = made.json()["runs"]
    assert [r["status"] for r in runs] == ["succeeded", "queued"]
    assert runs[0]["reused_from"]["hrid"] == first["hrid"]
    assert made.json()["batch"]["statuses"] == {"succeeded": 1, "queued": 1}


# --- Seeds ---------------------------------------------------------------------------------------


def test_every_run_gets_a_seed_its_command_is_given(world: World) -> None:
    run = importing(world, stored(world)).json()
    assert 0 <= run["seed"] < 2**31
    job = pod(world).lease().json()
    assert job["run"]["seed"] == run["seed"]
    seeds = {importing(world, stored(world, f"{n}.fasta")).json()["seed"] for n in range(5)}
    assert len(seeds) > 1


# --- Reproduce exactly ---------------------------------------------------------------------------


def reproduce(world: World, run: dict[str, Any], principal: str = "editor") -> Any:
    return api_as(world, principal).post(f"/runs/{run['hrid']}/reproduce", {"reason": "Audit"})


def test_a_reproduction_runs_it_again_exactly_and_writes_nothing(world: World) -> None:
    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    first = launch(editor, world.project, {"protein": abl["id"]}).json()
    worker_ = pod(world)
    job = worker_.lease().json()
    document = output(updates=weight(first), metrics={"score": 0.9})
    assert done(worker_, job, document)["run_status"] == "succeeded"
    assert seen(world, first)["can"]["reproduce"] is True
    # Someone edits the protein since.
    etag = editor.get(f"/entities/{abl['id']}").headers["ETag"]
    edited = patch(editor, abl["id"], {"values": {"name": "ABL1 kinase"}}, etag)
    assert edited.status_code == 200, edited.content
    revision = Entity.objects.get(pk=abl["id"]).revision

    made = reproduce(world, first)
    assert made.status_code == 201, made.content
    again = made.json()
    assert again["reproduces"]["hrid"] == first["hrid"]
    assert (again["status"], again["reused"]) == ("queued", False)
    assert (again["seed"], again["params"]) == (seen(world, first)["seed"], first["params"])
    assert again["inputs"] == first["inputs"]
    assert again["reason"] == "Audit"

    # Its command reads what the first one read: the revision it read, and the seed.
    job = worker_.lease().json()
    assert job["run"]["seed"] == again["seed"]
    assert job["inputs"]["protein"][0]["revision"] == first["inputs"][0]["revision"]
    assert job["inputs"]["protein"][0]["values"]["name"] != "ABL1 kinase"
    assert done(worker_, job, document)["run_status"] == "succeeded"

    body = seen(world, again)
    assert body["reproduction"] == {
        "matches": True,
        "differences": [],
        "more_differences": 0,
        "environment_build": {"original": BUILT, "reproduced": BUILT},
    }
    # It wrote nothing.
    assert body["effects"] == []
    assert Entity.objects.get(pk=abl["id"]).revision == revision
    launched = AuditEvent.objects.get(action="run.launched", target_id=again["id"])
    assert launched.data["reproduces"] == first["hrid"]


def test_a_reproduction_says_where_its_results_differ(world: World) -> None:
    first, _ = ran_import(world, stored(world))
    again = reproduce(world, first).json()
    worker_ = pod(world, name="pod-again")
    job = worker_.lease().json()
    creates = [
        {
            **c,
            "values": dict(c["values"]),
            "origin": origin(first["params"]["file"], c["values"]["name"]),
        }
        for c in KINASES
    ]
    creates[1]["values"]["sequence"] = "MGSNKSQ"
    document = output(creates=creates, metrics={"records": 3})
    assert done(worker_, job, document)["run_status"] == "succeeded"
    reproduction = seen(world, again)["reproduction"]
    assert reproduction["matches"] is False
    assert reproduction["differences"] == [
        {"pointer": "#/creates/1/values/sequence", "original": "MGSNKSK", "reproduced": "MGSNKSQ"},
        {"pointer": "#/metrics/records", "original": 2, "reproduced": 3},
    ]
    # Nothing was imported.
    assert Entity.objects.filter(type="protein.Protein").count() == 2


def test_a_reused_run_is_reproduced_as_the_run_it_reused(world: World) -> None:
    file_id = stored(world)
    first, _ = ran_import(world, file_id)
    reused = importing(world, file_id).json()
    assert reused["can"]["reproduce"] is True
    again = reproduce(world, reused).json()
    assert again["reproduces"]["hrid"] == first["hrid"]


def test_runs_of_other_code_cant_be_reproduced_here(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = ran_import(world, stored(world))
    monkeypatch.setattr(services, "plugin_sha", lambda plugin: "c" * 40)
    assert seen(world, first)["can"]["reproduce"] is False
    body = problem(reproduce(world, first), 409, "refused")
    assert "can't be reproduced exactly here" in body["detail"]


def test_runs_without_results_cant_be_reproduced(world: World) -> None:
    run = importing(world, stored(world)).json()
    assert run["can"]["reproduce"] is False
    body = problem(reproduce(world, run), 409, "refused")
    assert body["detail"] == f"{run['hrid']} ended without results to compare with."


def test_reproducing_takes_the_role_to_run_its_tier(world: World) -> None:
    first, _ = ran_import(world, stored(world))
    assert seen(world, first, "viewer")["can"]["reproduce"] is False
    problem(reproduce(world, first, "viewer"), 403, "forbidden")


# --- Comparing ----------------------------------------------------------------------------------


def test_files_compare_by_their_contents() -> None:
    contents = {"f1": "a" * 64, "f2": "a" * 64, "f3": "c" * 64}
    before = reuse._normal({"values": {"pdb": "f1", "name": "x"}}, contents)
    assert before == reuse._normal({"values": {"pdb": "f2", "name": "x"}}, contents)
    assert before != reuse._normal({"values": {"pdb": "f3", "name": "x"}}, contents)


def test_differences_are_listed_up_to_a_limit() -> None:
    out: list[dict[str, Any]] = []
    counted = [0]
    reuse._diff(list(range(30)), list(range(1, 31)), "/metrics", out, counted)
    assert len(out) == reuse.MAX_DIFFERENCES and counted == [30]
    reuse._diff([1], [1, 2], "/a~b", out := [], counted := [0])
    assert out == [{"pointer": "#/a~b", "original": [1], "reproduced": [1, 2]}]
    reuse._diff({"a/b": 1}, {"a/b": 1.0}, "", out := [], counted := [0])
    assert out == [{"pointer": "#/a~1b", "original": 1, "reproduced": 1.0}]
