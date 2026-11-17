"""Self-hosted workers (ADR 0112): machines a Compute admin registers for teams, perhaps
only some of their projects, with labels. They take only runs in the projects they cover,
checked at lease and on every report, and an admin changes or revokes each one."""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from api_client import problem
from django.utils import timezone
from org_world import World, build_world
from test_api_entities import api_as
from test_oauth import form_post
from test_workers import LOCAL, ROOMY, WorkerClient, queued
from test_workers import worker as backend_worker

from iki_server.audit.models import AuditEvent
from iki_server.org.models import Project, ProjectMembership, ProjectRole, Team
from iki_server.runs import catalog
from iki_server.runs.models import ActionRun, Job, JobState, Worker

pytestmark = pytest.mark.django_db


@pytest.fixture
def world() -> Iterator[World]:
    catalog.compute.cache_clear()
    yield build_world()
    catalog.compute.cache_clear()


@pytest.fixture(autouse=True)
def keep_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from iki_server.realtime import views

    monkeypatch.setattr(views, "close_old_connections", lambda: None)


def token(
    world: World,
    teams: list[Team] | None = None,
    projects: list[Project] | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """A self-hosted registration token, as a Compute admin makes it."""
    made = api_as(world, "compute_admin").post(
        "/compute/registration-tokens",
        {
            "label": "Lab 3 instruments",
            "tier": "self_hosted",
            "teams": [str(t.pk) for t in (teams or [world.team])],
            "projects": [str(p.pk) for p in projects or []],
            "labels": labels or [],
        },
    )
    assert made.status_code == 201, made.content
    body: dict[str, Any] = made.json()
    return body


def machine(world: World, name: str = "hplc-pc-1", **scope: Any) -> WorkerClient:
    """A self-hosted worker, registered as ``iki-worker register`` does."""
    made = WorkerClient(token(world, **scope)["token"]).post(
        "/register", {"name": name, "version": "0.1.0", "capacity": ROOMY, "plugins": LOCAL}
    )
    assert made.status_code == 201, made.content
    assert made.json()["tier"] == "self_hosted"
    return WorkerClient(made.json()["token"])


def on_self_hosted(run: dict[str, Any]) -> dict[str, Any]:
    """A queued run, moved to the self-hosted tier (no test action runs there)."""
    ActionRun.objects.filter(pk=run["id"]).update(tier="self_hosted")
    Job.objects.filter(run_id=run["id"]).update(tier="self_hosted")
    return run


def second_project(world: World, team: Team | None = None) -> Project:
    other = Project.objects.create(team=team or world.team, name="Screens")
    ProjectMembership.objects.create(
        project=other, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    return other


def leased_run(worker: WorkerClient) -> str | None:
    leased = worker.lease()
    if leased.status_code == 204:
        return None
    assert leased.status_code == 200, leased.content
    return str(Job.objects.get(pk=leased.json()["job"]["id"]).run_id)


# --- Registering ------------------------------------------------------------------------------


def test_compute_admins_make_tokens_for_teams_machines(world: World) -> None:
    screens = second_project(world)
    made = token(world, projects=[screens], labels=["Chromeleon", "lab-3", "chromeleon"])
    assert made["tier"] == "self_hosted"
    assert [t["name"] for t in made["teams"]] == ["Structural Biology"]
    assert made["projects"] == [
        {
            "id": str(screens.pk),
            "name": "Screens",
            "team": {"id": str(world.team.pk), "name": "Structural Biology"},
        }
    ]
    # Lowercase, each once.
    assert made["labels"] == ["chromeleon", "lab-3"]
    event = AuditEvent.objects.get(action="worker_registration.created")
    assert event.data == {
        "tier": "self_hosted",
        "teams": [str(world.team.pk)],
        "projects": [str(screens.pk)],
        "labels": ["chromeleon", "lab-3"],
    }


def test_a_token_needs_teams_and_projects_in_them(world: World) -> None:
    admin = api_as(world, "compute_admin")

    def refused(body: dict[str, Any], pointer: str) -> str:
        found = problem(
            admin.post(
                "/compute/registration-tokens", {"label": "x", "tier": "self_hosted", **body}
            ),
            409,
            "refused",
        )["errors"][0]
        assert found["pointer"] == pointer, found
        detail: str = found["detail"]
        return detail

    assert "Choose the teams" in refused({}, "#/teams")
    elsewhere = Project.objects.create(team=world.other_team, name="Assays")
    assert "isn't in any of its teams" in refused(
        {"teams": [str(world.team.pk)], "projects": [str(elsewhere.pk)]}, "#/projects"
    )
    assert "isn't a label" in refused(
        {"teams": [str(world.team.pk)], "labels": ["Lab 3"]}, "#/labels"
    )
    archived = Team.objects.create(name="Gone", archived_at=timezone.now())
    assert refused({"teams": [str(archived.pk)]}, "#/teams") == "No such team."
    # Only self-hosted workers belong to teams and have labels.
    for body, pointer in (
        ({"tier": "backend", "teams": [str(world.team.pk)]}, "#/teams"),
        ({"tier": "backend", "labels": ["gpu"]}, "#/labels"),
    ):
        made = admin.post("/compute/registration-tokens", {"label": "Pods", **body})
        assert problem(made, 409, "refused")["errors"][0]["pointer"] == pointer
    # People without the role make none.
    problem(
        api_as(world, "org_admin").post(
            "/compute/registration-tokens",
            {"label": "x", "tier": "self_hosted", "teams": [str(world.team.pk)]},
        ),
        403,
        "forbidden",
    )


def test_a_machine_gets_its_tokens_teams_projects_and_labels(world: World) -> None:
    screens = second_project(world)
    machine(world, projects=[screens], labels=["chromeleon"])
    worker = Worker.objects.get()
    assert (worker.tier, worker.labels) == ("self_hosted", ["chromeleon"])
    assert list(worker.teams.all()) == [world.team]
    assert list(worker.projects.all()) == [screens]
    event = AuditEvent.objects.get(action="worker.registered")
    assert event.data["teams"] == [str(world.team.pk)]
    assert event.data["projects"] == [str(screens.pk)]
    assert event.data["labels"] == ["chromeleon"]


def test_iki_worker_no_longer_signs_in_as_someone(world: World) -> None:
    # The OAuth client and its scope went with the client tier (ADR 0112).
    gone = form_post("/oauth/device-authorization/", {"client_id": "iki-worker", "scope": "worker"})
    assert gone.status_code in (400, 401)
    assert gone.json()["error"] in ("invalid_client", "invalid_request", "invalid_scope")
    scoped = form_post("/oauth/device-authorization/", {"client_id": "iki-cli", "scope": "worker"})
    assert scoped.status_code == 400 and scoped.json()["error"] == "invalid_scope"


# --- What it runs ------------------------------------------------------------------------------


def test_it_runs_only_runs_in_its_teams_projects(world: World) -> None:
    other = machine(world, "assay-pc", teams=[world.other_team])
    mine = machine(world)
    backend = queued(world)
    run = on_self_hosted(queued(world))
    # Not on its tier, nor in its team's projects.
    assert leased_run(other) is None
    assert leased_run(mine) == run["id"]
    assert backend["id"] not in {j.run_id for j in Job.objects.filter(worker__isnull=False)}


def test_listing_projects_narrows_its_team_to_them(world: World) -> None:
    screens = second_project(world)
    narrowed = machine(world, projects=[screens])
    in_atlas = on_self_hosted(queued(world))
    assert leased_run(narrowed) is None
    in_screens = on_self_hosted(queued(world, screens))
    assert leased_run(narrowed) == in_screens["id"]
    # A second team, none of whose projects it lists, is taken whole.
    chemistry = Project.objects.create(team=world.other_team, name="Assays")
    ProjectMembership.objects.create(
        project=chemistry, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    both = machine(world, "both", teams=[world.team, world.other_team], projects=[screens])
    in_chemistry = on_self_hosted(queued(world, chemistry))
    assert {leased_run(both), leased_run(both)} - {None} == {in_chemistry["id"]}
    assert ActionRun.objects.get(pk=in_atlas["id"]).status == "queued"


# --- Changing and revoking ---------------------------------------------------------------------


def test_admins_see_its_labels_scope_and_what_it_runs(world: World) -> None:
    worker = machine(world, labels=["chromeleon"])
    run = on_self_hosted(queued(world))
    assert leased_run(worker) == run["id"]
    (listed,) = api_as(world, "compute_admin").get("/compute/workers").json()["items"]
    assert listed["labels"] == ["chromeleon"]
    assert [t["name"] for t in listed["teams"]] == ["Structural Biology"]
    assert listed["projects"] == []
    assert listed["running"] == [
        {
            "id": run["id"],
            "hrid": run["hrid"],
            "action": run["action"],
            "project": {"id": str(world.project.pk), "name": "Kinase Atlas"},
        }
    ]


def test_admins_change_its_labels_and_scope(world: World) -> None:
    admin = api_as(world, "compute_admin")
    worker = machine(world, labels=["chromeleon"])
    found = Worker.objects.get()
    changed = admin.patch(f"/compute/workers/{found.pk}", {"labels": ["empower", "lab-3"]})
    assert changed.status_code == 200, changed.content
    assert changed.json()["labels"] == ["empower", "lab-3"]
    assert [t["name"] for t in changed.json()["teams"]] == ["Structural Biology"]
    event = AuditEvent.objects.get(action="worker.changed")
    assert event.actor == world.users["compute_admin"]
    assert event.data["before"]["labels"] == ["chromeleon"]
    assert event.data["after"]["labels"] == ["empower", "lab-3"]
    # Nothing changed: nothing recorded.
    admin.patch(f"/compute/workers/{found.pk}", {"labels": ["lab-3", "empower"]})
    assert AuditEvent.objects.filter(action="worker.changed").count() == 1
    # Another team instead, and its runs with it.
    moved = admin.patch(f"/compute/workers/{found.pk}", {"teams": [str(world.other_team.pk)]})
    assert [t["name"] for t in moved.json()["teams"]] == ["Chemistry"]
    run = on_self_hosted(queued(world))
    assert leased_run(worker) is None
    assert ActionRun.objects.get(pk=run["id"]).status == "queued"
    # Only self-hosted workers have labels and teams; a revoked one changes no more.
    backend_worker(world)
    pod = Worker.objects.get(tier="backend")
    problem(admin.patch(f"/compute/workers/{pod.pk}", {"labels": ["gpu"]}), 409, "refused")
    admin.post(f"/compute/workers/{found.pk}/revoke")
    problem(admin.patch(f"/compute/workers/{found.pk}", {"labels": []}), 409, "refused")
    problem(
        api_as(world, "editor").patch(f"/compute/workers/{found.pk}", {"labels": []}),
        403,
        "forbidden",
    )


def test_taking_it_off_a_project_stops_its_jobs_there(world: World) -> None:
    admin = api_as(world, "compute_admin")
    worker = machine(world)
    run = on_self_hosted(queued(world))
    leased = worker.lease().json()
    assert worker.report(leased, "heartbeat", state="running").status_code == 200
    found = Worker.objects.get()
    screens = second_project(world)
    narrowed = admin.patch(f"/compute/workers/{found.pk}", {"projects": [str(screens.pk)]})
    assert narrowed.status_code == 200, narrowed.content
    # Interrupted, and queued again for a worker that covers its project.
    first = Job.objects.get(pk=leased["job"]["id"])
    assert first.state == JobState.INTERRUPTED
    assert first.error == {
        "code": "worker_lost",
        "message": "The worker was taken off this project.",
    }
    assert ActionRun.objects.get(pk=run["id"]).status == "queued"
    assert worker.report(leased, "heartbeat").status_code == 409


def test_every_report_checks_its_scope(world: World) -> None:
    worker = machine(world)
    on_self_hosted(queued(world))
    leased = worker.lease().json()
    # Its team taken away behind its back: what it holds there is no longer its to report.
    Worker.objects.get().teams.set([world.other_team])
    for what, body in (
        ("heartbeat", {}),
        ("progress", {"fraction": 0.5, "message": "half"}),
        ("logs", {"offset": 0, "text": "hello\n"}),
        ("fail", {"reason": "failed", "message": "no"}),
    ):
        refused = problem(worker.report(leased, what, **body), 409, "stale_lease")
        assert "no longer runs runs in this job’s project" in refused["detail"]


# --- Why a run waits -----------------------------------------------------------------------------


def test_a_queued_run_says_why_no_worker_takes_it(world: World) -> None:
    editor = api_as(world, "editor")
    run = on_self_hosted(queued(world))

    def why() -> Any:
        return editor.get(f"/runs/{run['hrid']}").json()["needs_worker"]

    assert why() == "unregistered"
    # Another team's machine doesn't count.
    machine(world, "assay-pc", teams=[world.other_team])
    assert why() == "unregistered"
    worker = machine(world)
    Worker.objects.filter(name="hplc-pc-1").update(
        last_seen_at=timezone.now() - timedelta(minutes=5)
    )
    assert why() == "offline"
    worker.call("GET", "/me")  # heard from again
    assert why() is None
    other = on_self_hosted(queued(world))
    assert leased_run(worker) in {run["id"], other["id"]}
    waiting = run if ActionRun.objects.get(pk=run["id"]).status == "queued" else other
    assert editor.get(f"/runs/{waiting['hrid']}").json()["needs_worker"] == "busy"
    assert editor.get(f"/runs/{queued(world)['hrid']}").json()["needs_worker"] is None


def test_admins_find_projects_of_the_teams_they_chose(world: World) -> None:
    second_project(world)
    Project.objects.create(team=world.other_team, name="Assays")
    admin = api_as(world, "compute_admin")
    found = admin.get(
        f"/compute/quota-scopes?kind=project&team={world.team.pk}&team={world.other_team.pk}"
    ).json()
    assert [p["name"] for p in found] == ["Assays", "Kinase Atlas", "Screens"]
    only = admin.get(f"/compute/quota-scopes?kind=project&team={world.other_team.pk}").json()
    assert [p["name"] for p in only] == ["Assays"]


# --- Labels ---------------------------------------------------------------------------------


def labelled(run: dict[str, Any], *labels: str) -> dict[str, Any]:
    """A queued self-hosted run that needs a machine with `labels`."""
    on_self_hosted(run)
    ActionRun.objects.filter(pk=run["id"]).update(labels=sorted(labels))
    Job.objects.filter(run_id=run["id"]).update(labels=sorted(labels))
    return run


def test_a_run_goes_only_to_a_machine_with_every_label_it_needs(world: World) -> None:
    hplc = machine(world, "hplc-pc-1", labels=["chromeleon"])
    run = labelled(queued(world), "chromeleon", "lab-3")
    assert leased_run(hplc) is None
    lab3 = machine(world, "hplc-pc-2", labels=["chromeleon", "gpu", "lab-3"])
    assert leased_run(lab3) == run["id"]
    # What its machine had, kept: an admin may change the machine's labels later.
    job = Job.objects.get(run_id=run["id"])
    assert job.worker_labels == ["chromeleon", "gpu", "lab-3"]
    editor = api_as(world, "editor")
    (attempt,) = editor.get(f"/runs/{run['id']}/attempts").json()
    assert (attempt["worker"], attempt["worker_labels"]) == (
        "hplc-pc-2",
        ["chromeleon", "gpu", "lab-3"],
    )
    assert editor.get(f"/runs/{run['id']}").json()["labels"] == ["chromeleon", "lab-3"]
    # A run needing none goes to any machine that covers its project.
    plain = on_self_hosted(queued(world))
    assert leased_run(hplc) == plain["id"]


def test_each_set_of_labels_gets_its_turn(world: World) -> None:
    worker = machine(world, labels=["chromeleon", "lab-3"])
    first = labelled(queued(world), "chromeleon")
    second = labelled(queued(world), "lab-3")
    third = on_self_hosted(queued(world))
    leased = set()
    for _ in range(3):
        found = leased_run(worker)
        assert found is not None
        leased.add(found)
        Job.objects.filter(run__pk=found).update(state=JobState.SUCCEEDED)
    assert leased == {first["id"], second["id"], third["id"]}


def test_a_retry_needs_the_same_labels(world: World) -> None:
    worker = machine(world, labels=["chromeleon"])
    run = labelled(queued(world), "chromeleon")
    leased = worker.lease().json()
    assert worker.report(leased, "fail", reason="interrupted", message="stopped").status_code == 200
    retry = Job.objects.get(run_id=run["id"], attempt=2)
    assert retry.labels == ["chromeleon"]


def test_a_queued_run_says_no_machine_has_its_labels(world: World) -> None:
    machine(world, labels=["chromeleon"])
    run = labelled(queued(world), "chromeleon", "lab-3")
    editor = api_as(world, "editor")
    assert editor.get(f"/runs/{run['hrid']}").json()["needs_worker"] == "unregistered"
    machine(world, "hplc-pc-2", labels=["chromeleon", "lab-3"])
    assert editor.get(f"/runs/{run['hrid']}").json()["needs_worker"] is None


def test_labels_come_from_the_environment_the_deployment_and_the_launcher(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace
    from functools import cache

    from iki_server.org.services import Refused
    from iki_server.runs import services
    from iki_spec.compute import Binding, Compute

    found = catalog.action("acme/protein.compute_properties")
    assert found is not None
    action = replace(found, tier="self_hosted")
    environment = replace(catalog.environment(action), labels=("chromeleon",))
    monkeypatch.setattr(catalog, "environment", lambda of: environment)
    qualified = f"{action.plugin}.{action.environment}"
    monkeypatch.setattr(
        catalog,
        "compute",
        # Cached, as the deployment's is: the test's teardown clears it.
        cache(
            lambda: Compute(bindings={qualified: Binding(environment=qualified, labels=("lab-3",))})
        ),
    )
    assert catalog.labels(action) == ["chromeleon", "lab-3"]
    assert catalog.labels(replace(action, tier="backend")) == []
    assert services.launch_labels(action, ["Bay-2", "bay-2"]) == ["bay-2"]
    assert services.launch_labels(action, []) == []
    with pytest.raises(Refused, match="isn't a label"):
        services.launch_labels(action, ["bay 2"])


def test_only_self_hosted_runs_take_labels_at_launch(world: World) -> None:
    from test_runs import launch, protein

    editor = api_as(world, "editor")
    abl = protein(editor, world.project)
    made = launch(editor, world.project, {"protein": abl["id"]}, labels=["lab-3"])
    refused = problem(made, 409, "refused")
    assert refused["errors"][0]["pointer"] == "#/labels"
    assert ActionRun.objects.count() == 0
