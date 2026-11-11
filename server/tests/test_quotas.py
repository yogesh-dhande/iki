"""Run quotas: limits on running and waiting runs, per organization, team or project
(ADR 0045)."""

from collections.abc import Iterator
from typing import Any

import pytest
from api_client import problem
from org_world import World, build_world
from test_api_entities import api_as
from test_runs import launch, protein
from test_workers import keep_connection, queued, worker

from iki_server.audit.models import AuditEvent
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.runs import catalog
from iki_server.runs.models import RunQuota

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection"]  # the fixture, used here too


@pytest.fixture
def world() -> Iterator[World]:
    catalog.compute.cache_clear()
    yield build_world()
    catalog.compute.cache_clear()


def quota(world: World, **body: Any) -> dict[str, Any]:
    made = api_as(world, "compute_admin").post("/compute/quotas", body)
    assert made.status_code == 201, made.content
    result: dict[str, Any] = made.json()
    return result


def other_project(world: World) -> Project:
    other = Project.objects.create(team=world.project.team, name="Other")
    ProjectMembership.objects.create(
        project=other, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    return other


def test_a_project_runs_only_so_many_jobs_at_once(world: World) -> None:
    quota(world, project=str(world.project.pk), max_running=1)
    pod = worker(world)
    first, held_back = queued(world), queued(world)
    other = queued(world, other_project(world))
    assert pod.lease().json()["run"]["id"] == first["id"]
    # world.project is at its limit: the other project's run goes, though it's newer.
    assert pod.lease().json()["run"]["id"] == other["id"]
    assert pod.lease().status_code == 204
    body = api_as(world).get(f"/runs/{held_back['id']}").json()
    assert body["waiting"] == "The project is at its limit of 1 running job."
    assert api_as(world).get(f"/runs/{first['id']}").json()["waiting"] is None


def test_team_and_organization_limits_hold_too(world: World) -> None:
    team = quota(world, team=str(world.project.team_id), tier="backend", max_running=1)
    assert team["team"]["id"] == str(world.project.team_id)
    pod = worker(world)
    queued(world)
    late = queued(world, other_project(world))  # same team
    assert pod.lease().status_code == 200
    assert pod.lease().status_code == 204
    assert (
        api_as(world).get(f"/runs/{late['id']}").json()["waiting"]
        == "Its team is at its limit of 1 running job on the backend tier."
    )
    api_as(world, "compute_admin").delete(f"/compute/quotas/{team['id']}")
    quota(world, max_running=0)  # pauses the organization
    assert pod.lease().status_code == 204
    assert api_as(world).get(f"/runs/{late['id']}").json()["waiting"] == (
        "The organization is paused."
    )


def test_launching_past_a_waiting_limit_is_refused(world: World) -> None:
    quota(world, project=str(world.project.pk), max_queued=1)
    queued(world)
    editor = api_as(world, "editor")
    abl = protein(editor, world.project, name="second")
    refused = problem(launch(editor, world.project, {"protein": abl["id"]}), 409, "refused")
    assert refused["detail"] == (
        "The project has 1 run waiting to start, its limit. Launch this once some have "
        "started, or ask a Compute admin to raise it."
    )
    # Once it starts, there's room again.
    worker(world).lease()
    assert launch(editor, world.project, {"protein": abl["id"]}).status_code == 201


def test_compute_admins_manage_quotas(world: World) -> None:
    admin = api_as(world, "compute_admin")
    made = quota(world, project=str(world.project.pk), max_running=4, max_queued=50)
    assert (made["running"], made["queued"]) == (0, 0)
    assert made["tier"] is None
    again = admin.post("/compute/quotas", {"project": str(world.project.pk), "max_running": 1})
    assert "already" in problem(again, 409, "refused")["detail"]
    neither = admin.post("/compute/quotas", {"project": str(world.project.pk), "tier": "vm"})
    assert problem(neither, 422, "validation_failed")["errors"][0]["pointer"] == "#/max_running"
    both = admin.post(
        "/compute/quotas",
        {"team": str(world.project.team_id), "project": str(world.project.pk), "max_queued": 1},
    )
    assert problem(both, 422, "validation_failed")["errors"][0]["pointer"] == "#/project"
    changed = admin.patch(f"/compute/quotas/{made['id']}", {"max_running": 8})
    assert changed.status_code == 200, changed.content
    assert (changed.json()["max_running"], changed.json()["max_queued"]) == (8, None)
    assert [q["id"] for q in admin.get("/compute/quotas").json()] == [made["id"]]
    assert admin.delete(f"/compute/quotas/{made['id']}").status_code == 204
    assert not RunQuota.objects.exists()
    actions = list(
        AuditEvent.objects.filter(action__startswith="quota.").values_list("action", flat=True)
    )
    assert sorted(actions) == ["quota.created", "quota.removed", "quota.updated"]
    updated = AuditEvent.objects.get(action="quota.updated")
    assert updated.data["before"]["max_running"] == 4
    assert updated.data["after"] == {"tier": None, "max_running": 8, "max_queued": None}
