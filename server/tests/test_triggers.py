"""Triggers: a plugin's actions run by themselves when entities change, in projects whose
Admins turn them on, as the Admin who did; what a triggered run writes fires nothing
(ADR 0080)."""

from datetime import timedelta
from typing import Any

import pytest
from api_client import problem
from org_world import World
from test_api_entities import api_as, patch
from test_oauth import bearer, signed_in_token
from test_runs import COMPUTE, launch, protein
from test_workers import WorkerClient, keep_connection, worker, world

from iki_server.audit.models import AuditEvent, Via
from iki_server.notifications.models import Notification
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.runs import approvals, triggers
from iki_server.runs.models import (
    ActionRun,
    FiringState,
    ProjectTrigger,
    RunApproval,
    RunStatus,
    TriggerFiring,
)
from iki_server.tasks.runner import Runner
from iki_spec.model import Trigger

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # fixtures, from test_workers

NEW = "protein.weigh_new_proteins"
CHANGED = "protein.weigh_changed_proteins"


def run_tasks() -> int:
    return Runner(queues=["default"]).run_ready()


def on(world: World, name: str = NEW, who: str = "admin") -> dict[str, Any]:
    made = api_as(world, who).post(f"/projects/{world.project.pk}/triggers/{name}/enable", {})
    assert made.status_code == 200, made.content
    body: dict[str, Any] = made.json()
    return body


def listed(world: World, who: str = "viewer") -> dict[str, dict[str, Any]]:
    found = api_as(world, who).get(f"/projects/{world.project.pk}/triggers").json()
    return {t["name"]: t for t in found}


def firings(world: World, **query: Any) -> list[dict[str, Any]]:
    found = api_as(world, "viewer").get(f"/projects/{world.project.pk}/trigger-firings", **query)
    items: list[dict[str, Any]] = found.json()["items"]
    return items


def weigh(pod: WorkerClient) -> dict[str, Any]:
    """Completes the leased compute_properties job, weighing its protein."""
    job = pod.lease().json()
    (entity,) = job["inputs"]["protein"]
    document = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "updates": {"protein": [{"entity": entity["id"], "values": {"molecular_weight": 1.5}}]},
    }
    answer = pod.report(job, "complete", output=document)
    assert answer.status_code == 200, answer.content
    return job  # type: ignore[no-any-return]


# --- The catalog -------------------------------------------------------------------------------


def test_lists_installed_triggers(world: World) -> None:
    found = api_as(world, "viewer").get("/triggers").json()
    assert [t["name"] for t in found] == [CHANGED, NEW]
    assert found[1] == {
        "name": NEW,
        "plugin": "protein",
        "label": "Weigh new proteins",
        "description": None,
        "on": "entity.created",
        "type": "protein.Protein",
        "fields": [],
        "action": COMPUTE,
        "action_label": "Compute properties",
        "input": "protein",
        "params": {},
        "tier": "backend",
    }


def test_triggers_are_off_until_an_admin_turns_one_on(world: World) -> None:
    before = listed(world)[NEW]
    assert before["enabled"] is False
    assert before["enabled_by"] is None and before["last_fired_at"] is None
    assert before["can"] == {"enable": False, "disable": False}
    assert before["why_not_enable"] is None
    assert listed(world, "admin")[NEW]["can"] == {"enable": True, "disable": False}
    # Nothing fires while it's off.
    protein(api_as(world, "editor"), world.project, "kit")
    assert TriggerFiring.objects.count() == 0

    body = on(world)
    assert body["enabled"] is True
    assert body["enabled_by"]["name"] == world.users["admin"].name
    assert body["can"] == {"enable": False, "disable": True}
    event = AuditEvent.objects.get(action="trigger.enabled")
    assert (event.actor, event.project, event.target_label) == (
        world.users["admin"],
        world.project,
        NEW,
    )
    assert event.data == {"trigger": NEW, "action": COMPUTE, "on": "entity.created"}
    # Turning it on again changes nothing.
    on(world)
    assert AuditEvent.objects.filter(action="trigger.enabled").count() == 1
    # Only in this project.
    assert not ProjectTrigger.objects.exclude(project=world.project).exists()


def test_only_admins_who_can_run_its_action_turn_it_on(world: World) -> None:
    for who in ("viewer", "editor", "runner"):
        url = f"/projects/{world.project.pk}/triggers/{NEW}/enable"
        problem(api_as(world, who).post(url, {}), 403, "forbidden")
    problem(
        api_as(world, "admin").post(
            f"/projects/{world.project.pk}/triggers/protein.nope/enable", {}
        ),
        404,
        "not_found",
    )
    assert ProjectTrigger.objects.count() == 0


def test_no_token_turns_one_on(world: World) -> None:
    """Its runs are launched as the person: no agent can set that up for them."""
    _, tokens = signed_in_token(world, "admin", scope="read write")
    url = f"/projects/{world.project.pk}/triggers/{NEW}/enable"
    turned = bearer(tokens["access_token"], "POST", url, {})
    assert turned.status_code == 403, turned.content
    assert turned.json()["code"] == "web_only"
    assert ProjectTrigger.objects.count() == 0
    # Turning one off is fine: it only stops runs.
    on(world)
    off = bearer(
        tokens["access_token"],
        "POST",
        f"/projects/{world.project.pk}/triggers/{NEW}/disable",
        {},
    )
    assert off.status_code == 200, off.content
    assert off.json()["enabled"] is False


# --- Firing ------------------------------------------------------------------------------------


def test_a_new_entity_fires_a_run_as_the_admin(world: World) -> None:
    on(world)
    kit = protein(api_as(world, "editor"), world.project, "kit")
    (firing,) = TriggerFiring.objects.all()
    assert (firing.state, firing.event, str(firing.entity_id), firing.revision) == (
        FiringState.PENDING,
        "entity.created",
        kit["id"],
        1,
    )
    assert run_tasks() >= 1
    firing.refresh_from_db()
    assert firing.state == FiringState.LAUNCHED and firing.fired_at is not None
    run = firing.run
    assert run is not None
    assert (run.action, run.requested_by, run.via) == (COMPUTE, world.users["admin"], "trigger")
    assert run.reason == f"Trigger “Weigh new proteins”: {kit['hrid']} was created."
    assert run.status == RunStatus.QUEUED
    launched = AuditEvent.objects.get(action="run.launched")
    assert (launched.actor, launched.via) == (world.users["admin"], Via.TRIGGER)

    body = api_as(world, "viewer").get(f"/runs/{run.hrid}").json()
    assert body["via"] == "trigger"
    assert body["trigger"] == {
        "name": NEW,
        "label": "Weigh new proteins",
        "event": "entity.created",
        "entity": kit["id"],
        "revision": 1,
    }
    (listed_firing,) = firings(world)
    assert listed_firing["trigger"] == NEW
    assert listed_firing["label"] == "Weigh new proteins"
    assert listed_firing["entity"] == {
        "id": kit["id"],
        "hrid": kit["hrid"],
        "type": "protein.Protein",
    }
    assert listed_firing["run"] == {"id": str(run.pk), "hrid": run.hrid, "status": "queued"}
    assert (listed_firing["state"], listed_firing["error"]) == ("launched", None)
    assert listed(world)[NEW]["last_fired_at"] is not None
    # A person's own runs say nothing of triggers.
    own = launch(api_as(world, "editor"), world.project, {"protein": kit["id"]})
    assert own.json()["trigger"] is None


def test_firing_twice_launches_once(world: World) -> None:
    on(world)
    protein(api_as(world, "editor"), world.project, "kit")
    firing = TriggerFiring.objects.get()
    assert triggers.fire(str(firing.pk)) == "launched"
    assert triggers.fire(str(firing.pk)) == "already fired"
    run_tasks()
    assert ActionRun.objects.count() == 1


def test_a_triggered_runs_changes_fire_nothing(world: World) -> None:
    """Loop protection: weigh_changed_proteins fires on any change to a protein, and the
    runs it starts change proteins."""
    on(world, CHANGED)
    editor = api_as(world, "editor")
    kit = protein(editor, world.project, "kit")
    assert TriggerFiring.objects.count() == 0  # created, not changed
    pod = worker(world)
    # A person's run changes the protein: that fires the trigger.
    launch(editor, world.project, {"protein": kit["id"]})
    weigh(pod)
    (firing,) = TriggerFiring.objects.all()
    assert firing.event == "entity.updated" and firing.revision == 2
    run_tasks()
    firing.refresh_from_db()
    assert firing.run is not None and firing.run.via == "trigger"
    # The triggered run changes it again, and fires nothing.
    weigh(pod)
    firing.run.refresh_from_db()
    assert firing.run.status == RunStatus.SUCCEEDED
    assert TriggerFiring.objects.count() == 1
    run_tasks()
    assert ActionRun.objects.count() == 2


def test_a_change_by_a_person_fires_an_update_trigger(world: World) -> None:
    on(world, CHANGED)
    editor = api_as(world, "editor")
    kit = protein(editor, world.project, "kit")
    changed = patch(editor, kit["id"], {"values": {"organism": "Homo sapiens"}}, '"1"')
    assert changed.status_code == 200, changed.content
    (firing,) = TriggerFiring.objects.all()
    assert (firing.event, firing.revision) == ("entity.updated", 2)
    run_tasks()
    firing.refresh_from_db()
    assert firing.run is not None
    assert firing.run.reason.endswith(f"{kit['hrid']} changed.")


def test_watched_fields_narrow_what_fires() -> None:
    spec = Trigger(
        plugin="protein",
        name="t",
        on="entity.updated",
        type="protein.Protein",
        fields=("organism",),
        run="compute_properties",
        input="protein",
    )
    assert triggers._fires(spec, "entity.updated", "protein.Protein", ["organism", "summary"])
    assert not triggers._fires(spec, "entity.updated", "protein.Protein", ["summary"])
    assert not triggers._fires(spec, "entity.created", "protein.Protein", [])
    assert not triggers._fires(spec, "entity.updated", "dna.DNASequence", ["organism"])


def test_entities_of_other_projects_fire_nothing(world: World) -> None:
    on(world)
    other = Project.objects.create(team=world.team, name="Elsewhere")
    ProjectMembership.objects.create(
        project=other, user=world.users["editor"], role=ProjectRole.EDITOR
    )
    protein(api_as(world, "editor"), other, "kit")
    assert TriggerFiring.objects.count() == 0


def test_turned_off_triggers_fire_nothing_more(world: World) -> None:
    on(world)
    editor = api_as(world, "editor")
    protein(editor, world.project, "kit")
    off = api_as(world, "admin").post(f"/projects/{world.project.pk}/triggers/{NEW}/disable", {})
    assert off.status_code == 200, off.content
    body = off.json()
    assert (body["enabled"], body["disabled_by"]["name"]) == (False, world.users["admin"].name)
    assert body["enabled_by"]["name"] == world.users["admin"].name  # who it last ran as
    assert AuditEvent.objects.get(action="trigger.disabled").actor == world.users["admin"]
    # What fired before it was turned off is skipped; nothing new fires.
    run_tasks()
    firing = TriggerFiring.objects.get()
    assert firing.state == FiringState.SKIPPED
    assert firing.error == {"code": "turned_off", "message": "The trigger was turned off first."}
    protein(editor, world.project, "met")
    assert TriggerFiring.objects.count() == 1
    assert ActionRun.objects.count() == 0


def test_an_archived_entity_is_skipped(world: World) -> None:
    on(world)
    editor = api_as(world, "editor")
    kit = protein(editor, world.project, "kit")
    archived = patch(editor, kit["id"], {"archived": True}, '"1"')
    assert archived.status_code == 200, archived.content
    run_tasks()
    firing = TriggerFiring.objects.get()
    assert firing.state == FiringState.SKIPPED
    assert firing.error is not None and firing.error["code"] == "archived"


def test_an_admin_who_loses_their_role_turns_it_off(world: World) -> None:
    on(world)
    ProjectMembership.objects.filter(project=world.project, user=world.users["admin"]).update(
        role=ProjectRole.VIEWER
    )
    protein(api_as(world, "editor"), world.project, "kit")
    run_tasks()
    firing = TriggerFiring.objects.get()
    assert firing.state == FiringState.FAILED
    assert firing.error is not None and firing.error["code"] == "not_allowed"
    row = ProjectTrigger.objects.get()
    assert (row.enabled, row.disabled_by) == (False, None)
    assert "can no longer run" in row.disabled_reason
    event = AuditEvent.objects.get(action="trigger.disabled")
    assert event.actor is None and event.data["reason"] == row.disabled_reason
    told = Notification.objects.get(kind="trigger.turned_off")
    assert told.recipient == world.users["admin"]
    assert told.title == f"“Weigh new proteins” was turned off in {world.project.name}"
    assert listed(world)[NEW]["disabled_reason"] == row.disabled_reason
    # Another Admin may turn it on again: its runs are then theirs.
    ProjectMembership.objects.filter(project=world.project, user=world.users["admin"]).update(
        role=ProjectRole.ADMIN
    )
    assert on(world)["disabled_reason"] == ""


def test_lost_firings_are_swept_up(world: World) -> None:
    on(world)
    protein(api_as(world, "editor"), world.project, "kit")
    firing = TriggerFiring.objects.get()
    assert triggers.recover() == 0  # too soon: its task will fire it
    TriggerFiring.objects.filter(pk=firing.pk).update(
        created_at=firing.created_at - timedelta(minutes=2)
    )
    assert triggers.recover() == 1
    firing.refresh_from_db()
    assert firing.state == FiringState.LAUNCHED


def test_firings_can_be_narrowed_to_a_trigger(world: World) -> None:
    on(world)
    on(world, CHANGED)
    editor = api_as(world, "editor")
    kit = protein(editor, world.project, "kit")
    patch(editor, kit["id"], {"values": {"organism": "Mus musculus"}}, '"1"')
    assert [f["trigger"] for f in firings(world)] == [CHANGED, NEW]  # newest first
    assert [f["trigger"] for f in firings(world, trigger=NEW)] == [NEW]
    outsider = api_as(world, "outsider")
    problem(outsider.get(f"/projects/{world.project.pk}/trigger-firings"), 404, "not_found")


# --- Approval ----------------------------------------------------------------------------------


def test_runs_on_costly_tiers_wait_for_the_admin(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test deployment's actions run on the backend; holding that tier stands in for the
    container and VM tiers."""
    monkeypatch.setattr(approvals, "APPROVAL_TIERS", approvals.APPROVAL_TIERS | {"backend"})
    on(world)
    kit = protein(api_as(world, "editor"), world.project, "kit")
    run_tasks()
    firing = TriggerFiring.objects.get()
    run = firing.run
    assert run is not None and run.status == RunStatus.AWAITING_APPROVAL
    approval = RunApproval.objects.get()
    assert (approval.requested_by, approval.trigger) == (world.users["admin"], firing.trigger)
    assert (approval.client, approval.agent) == ("", None)
    told = Notification.objects.get(kind="run.approval_requested")
    assert told.recipient == world.users["admin"]
    assert told.title == (
        f"The trigger “Weigh new proteins” asks to start a run of Compute properties in "
        f"{world.project.name}"
    )
    admin = api_as(world, "admin")
    body = admin.get(f"/runs/{run.hrid}").json()
    assert body["approval"]["trigger"] == {"name": NEW, "label": "Weigh new proteins"}
    assert body["approval"]["can"]["decide"] is True
    approved = admin.post(f"/run-approvals/{approval.pk}/approve", {})
    assert approved.status_code == 200, approved.content
    run.refresh_from_db()
    assert run.status == RunStatus.QUEUED
    assert str(firing.entity_id) == kit["id"]
