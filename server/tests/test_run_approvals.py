"""Runs an agent asks for on the container and VM tiers wait for the person it acts for
to approve them, in the web app (ADR 0013). test_mcp.py drives the same through MCP."""

from collections.abc import Iterator
from datetime import timedelta
from io import StringIO
from itertools import count as counting
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from api_client import Api, problem
from django.core.management import call_command
from django.utils import timezone
from org_world import World, build_world
from test_api_entities import api_as
from test_oauth import bearer, signed_in_token
from test_runs import COMPUTE, PREDICT, bind_vm, launch, protein
from test_workers import keep_connection, worker

from iki_server.audit import log as audit
from iki_server.audit.models import AuditEvent, Via
from iki_server.notifications.models import Notification
from iki_server.org.models import ProjectMembership, ProjectRole
from iki_server.runs import approvals, catalog, services
from iki_server.runs.models import ActionRun, ApprovalState, Job, JobState, RunApproval

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection"]  # the fixture, used here too

_names = counting()


def _fresh() -> str:
    """A new, valid protein name each time, so no two are duplicates."""
    n = next(_names)
    return "".join("ACDEFGHIKLMNPQRSTVWY"[(n // 20**i) % 20] for i in range(3))


AGENT = audit.Context(via=Via.MCP, oauth_client="Notebook (Claude)")


@pytest.fixture
def world() -> Iterator[World]:
    catalog.compute.cache_clear()
    yield build_world()
    catalog.compute.cache_clear()


@pytest.fixture
def held_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test deployment's workers lease backend jobs only; holding that tier too lets
    the tests follow a run from its request to a worker."""
    monkeypatch.setattr(approvals, "APPROVAL_TIERS", approvals.APPROVAL_TIERS | {"backend"})


def as_agent(world: World, principal: str = "editor", *, count: int = 1) -> list[ActionRun]:
    """Runs of compute_properties, as an agent acting for `principal` launches them."""
    user = world.users[principal]
    editor = api_as(world, principal)
    made = [UUID(protein(editor, world.project, _fresh())["id"]) for _ in range(count)]
    action = catalog.action(COMPUTE)
    assert action is not None
    with audit.using(AGENT):
        if count == 1:
            return [services.launch(user, action, world.project, {"protein": made}, {})]
        return services.launch_each(user, action, world.project, "protein", made, {}, {})


def request_of(api: Api, run: ActionRun) -> dict[str, Any]:
    body: dict[str, Any] = api.get(f"/runs/{run.hrid}").json()
    return body


def test_an_agents_runs_on_costly_tiers_wait(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bind_vm(monkeypatch, tmp_path)
    runner = world.users["runner"]
    abl = UUID(protein(api_as(world, "runner"), world.project)["id"])
    predict, compute = catalog.action(PREDICT), catalog.action(COMPUTE)
    assert predict is not None and compute is not None
    with audit.using(AGENT):
        held = services.launch(runner, predict, world.project, {"protein": [abl]}, {})
        # The backend tier runs at once, as the person's own runs do.
        going = services.launch(runner, compute, world.project, {"protein": [abl]}, {})
    # The person's own runs on the vm tier don't wait.
    own = launch(api_as(world, "runner"), world.project, {"protein": str(abl)}, PREDICT)
    assert own.json()["status"] == "queued"
    assert own.json()["approval"] is None
    assert going.status == "queued"

    body = request_of(api_as(world, "runner"), held)
    assert (body["status"], body["via"], body["client"]) == (
        "awaiting_approval",
        "mcp",
        "Notebook (Claude)",
    )
    request = body["approval"]
    assert (request["state"], request["client"], request["requested_by"]["name"]) == (
        "pending",
        "Notebook (Claude)",
        runner.name,
    )
    assert request["runs"] == [{"id": str(held.pk), "hrid": held.hrid}]
    assert request["can"] == {"decide": True}
    assert body["waiting"] is None
    assert Job.objects.get(run=held).state == JobState.AWAITING_APPROVAL

    # The person is told, with a link to decide.
    told = Notification.objects.get(recipient=runner, kind="run.approval_requested")
    assert told.title == (
        f"Notebook (Claude) asks to start a run of Predict structure in {world.project.name}"
    )
    assert told.path == f"/runs/{held.hrid}/"
    event = AuditEvent.objects.get(action="run.approval_requested")
    assert (event.via, event.data) == (
        Via.MCP,
        {"action": PREDICT, "tier": "vm", "runs": 1},
    )


@pytest.mark.usefixtures("held_backend")
def test_approving_queues_them_for_a_worker(world: World) -> None:
    pod = worker(world)
    (run,) = as_agent(world)
    assert pod.lease().status_code == 204  # held where no worker leases it
    approval = run.approval
    assert approval is not None

    decided = api_as(world).post(f"/run-approvals/{approval.pk}/approve")
    assert decided.status_code == 200, decided.content
    assert decided.json()["state"] == "approved"
    assert decided.json()["decided_by"]["name"] == world.users["editor"].name
    assert decided.json()["can"] == {"decide": False}
    assert request_of(api_as(world), run)["status"] == "queued"
    leased = pod.lease()
    assert leased.status_code == 200
    assert leased.json()["run"]["id"] == str(run.pk)
    event = AuditEvent.objects.get(action="run.approved")
    assert (event.via, event.data) == (Via.WEB, {"runs": [run.hrid]})

    # One decision: it can't be made again.
    again = problem(api_as(world).post(f"/run-approvals/{approval.pk}/approve"), 409, "refused")
    assert again["detail"] == "This request is approved already."


@pytest.mark.usefixtures("held_backend")
def test_declining_ends_them_unstarted(world: World) -> None:
    (run,) = as_agent(world)
    assert run.approval is not None
    decided = api_as(world).post(
        f"/run-approvals/{run.approval.pk}/decline", {"note": " Not today "}
    )
    assert decided.status_code == 200, decided.content
    assert (decided.json()["state"], decided.json()["note"]) == ("declined", "Not today")
    body = request_of(api_as(world), run)
    assert body["status"] == "declined"
    assert body["finished_at"] is not None
    assert body["can"] == {"cancel": False, "reproduce": False, "run_again": False}
    assert Job.objects.get(run=run).state == JobState.CANCELLED
    event = AuditEvent.objects.get(action="run.declined")
    assert event.data == {"runs": [run.hrid], "note": "Not today"}
    problem(api_as(world).post(f"/run-approvals/{run.approval.pk}/approve"), 409, "refused")


@pytest.mark.usefixtures("held_backend")
def test_only_the_person_decides_and_only_in_the_web_app(world: World) -> None:
    (run,) = as_agent(world)
    assert run.approval is not None
    where = f"/run-approvals/{run.approval.pk}"

    # Others who read the project see the request, but can't decide it.
    seen = api_as(world, "runner").get(where)
    assert seen.status_code == 200
    assert seen.json()["can"] == {"decide": False}
    assert problem(api_as(world, "runner").post(f"{where}/approve"), 403, "forbidden")[
        "detail"
    ] == ("Only the person the app asked for can decide.")
    problem(api_as(world, "outsider").get(where), 404, "not_found")

    # The person's own tokens can't either, whatever their scopes: else an agent could
    # approve its own request.
    _, tokens = signed_in_token(world, "editor")
    token = tokens["access_token"]
    assert bearer(token, "GET", where).json()["can"] == {"decide": False}
    for action, body in (("approve", None), ("decline", {})):
        refused = bearer(token, "POST", f"{where}/{action}", body)
        assert refused.status_code == 403, refused.content
        assert refused.json()["code"] == "web_only"
    assert RunApproval.objects.get(pk=run.approval.pk).state == ApprovalState.PENDING


@pytest.mark.usefixtures("held_backend")
def test_a_batch_is_decided_together(world: World) -> None:
    runs = as_agent(world, count=3)
    approvals_made = {r.approval_id for r in runs}
    assert len(approvals_made) == 1
    (approval_id,) = approvals_made
    request = api_as(world).get(f"/run-approvals/{approval_id}").json()
    assert [r["hrid"] for r in request["runs"]] == [r.hrid for r in runs]
    assert Notification.objects.get(kind="run.approval_requested").title.startswith(
        "Notebook (Claude) asks to start 3 runs of Compute properties"
    )
    api_as(world).post(f"/run-approvals/{approval_id}/approve")
    assert set(ActionRun.objects.values_list("status", flat=True)) == {"queued"}


@pytest.mark.usefixtures("held_backend")
def test_approving_checks_the_role_and_quotas_again(world: World) -> None:
    (run,) = as_agent(world)
    assert run.approval is not None
    approve = f"/run-approvals/{run.approval.pk}/approve"

    # Waiting runs aren't counted against a waiting-runs limit until they're approved.
    made = api_as(world, "compute_admin").post(
        "/compute/quotas", {"project": str(world.project.pk), "max_queued": 0}
    )
    assert made.status_code == 201, made.content
    assert problem(api_as(world).post(approve), 409, "refused")["detail"].startswith(
        "The project has 0 runs waiting to start, its limit."
    )
    api_as(world, "compute_admin").delete(f"/compute/quotas/{made.json()['id']}")

    ProjectMembership.objects.filter(user=world.users["editor"], project=world.project).update(
        role=ProjectRole.VIEWER
    )
    refused = problem(api_as(world).post(approve), 403, "forbidden")
    assert refused["detail"] == "You can’t run this tier in its project any more."
    assert request_of(api_as(world), run)["status"] == "awaiting_approval"


@pytest.mark.usefixtures("held_backend")
def test_cancelling_its_runs_settles_a_request(world: World) -> None:
    first, second = as_agent(world, count=2)
    assert first.approval is not None
    editor = api_as(world)
    cancelled = editor.post(f"/runs/{first.hrid}/cancel", {"reason": "Wrong one"})
    assert cancelled.json()["status"] == "cancelled"
    # The other still waits.
    assert RunApproval.objects.get(pk=first.approval.pk).state == ApprovalState.PENDING
    editor.post(f"/runs/{second.hrid}/cancel", {})
    settled = editor.get(f"/run-approvals/{first.approval.pk}").json()
    assert (settled["state"], settled["note"]) == ("declined", "Its runs were cancelled.")
    assert Job.objects.filter(state=JobState.AWAITING_APPROVAL).count() == 0


@pytest.mark.usefixtures("held_backend")
def test_requests_nobody_decides_expire(world: World) -> None:
    (stale,) = as_agent(world)
    (fresh,) = as_agent(world)
    assert stale.approval is not None and fresh.approval is not None
    RunApproval.objects.filter(pk=stale.approval.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    # Deciding one that's due is refused at once, before the sweep gets to it.
    refused = problem(
        api_as(world).post(f"/run-approvals/{stale.approval.pk}/approve"), 409, "refused"
    )
    assert refused["detail"] == "This request is expired already."
    assert request_of(api_as(world), stale)["status"] == "declined"

    RunApproval.objects.filter(pk=fresh.approval.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    out = StringIO()
    call_command("maintain", stdout=out)
    assert "expired 1 run approval request(s)," in out.getvalue()
    assert request_of(api_as(world), fresh)["status"] == "declined"
    assert RunApproval.objects.get(pk=fresh.approval.pk).state == ApprovalState.EXPIRED
    assert AuditEvent.objects.filter(action="run.approval_expired").count() == 2
    assert approvals.expire() == 0
