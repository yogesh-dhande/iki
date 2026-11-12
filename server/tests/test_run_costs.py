"""Costs (ADR 0070): the machines Iki starts for runs cost their template's price for as long
as they're up, and each record says whose that time was. Runs show what they cost, actions
what a run may cost, and reports add it up for those who answer for it."""

import csv
import io
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from api_client import problem
from django.http import StreamingHttpResponse
from django.utils import timezone
from org_world import World
from test_api_entities import api_as
from test_container_tier import (  # noqa: F401 - fixtures
    COLABFOLD,
    FakeCluster,
    keep_connection,
    queue_run,
    register,
    rewrite_compute,
    screen,
    start_pod,
    warm,
    world,
)
from test_runs import COMPUTE, launch, protein
from test_workers import WorkerClient

from iki_server.org.models import ProjectMembership, ProjectRole
from iki_server.runs import launches, provisioner
from iki_server.runs.kubernetes import RUN_LABELS
from iki_server.runs.models import (
    ActionRun,
    CostKind,
    CostRecord,
    Job,
    Launch,
    LaunchState,
    RunStatus,
    Worker,
)

pytestmark = pytest.mark.django_db

PER_HOUR = 1.8


@pytest.fixture
def priced(world: World) -> World:  # noqa: F811 - the fixture
    def change(compute: dict[str, Any]) -> None:
        for template in compute["templates"].values():
            if template["provisioner"].startswith("k8s_"):
                template["price_per_hour"] = PER_HOUR
                template["currency"] = "USD"

    rewrite_compute(change)
    return world


def ago(hours: float) -> datetime:
    return timezone.now() - timedelta(hours=hours)


def started(cluster: FakeCluster) -> tuple[Launch, WorkerClient]:
    """Registers the newest pod's worker."""
    name = list(cluster.made)[-1][1]
    made = register(cluster.token(name), name=f"{name}-x1y2z")
    assert made.status_code == 201, made.content
    return Launch.objects.get(name=name), WorkerClient(made.json()["token"])


def leased(pod: WorkerClient) -> dict[str, Any]:
    got = pod.lease()
    assert got.status_code == 200, got.content
    body: dict[str, Any] = got.json()
    return body


def cost(of: World, run_id: str) -> dict[str, Any]:
    got = api_as(of, "runner").get(f"/runs/{run_id}")
    assert got.status_code == 200, got.content
    found: dict[str, Any] = got.json()["cost"]
    return found


# --- What machines cost, and whose it was ----------------------------------------------------


def test_an_ephemeral_pods_whole_life_is_its_runs(priced: World) -> None:
    run = queue_run(priced)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    launch, pod = started(cluster)
    Launch.objects.filter(pk=launch.pk).update(created_at=ago(2))
    leased(pod)
    # While it's up, the run's cost so far counts from when its pod was asked for.
    going = cost(priced, run["id"])
    assert going["currency"] == "USD"
    assert going["final"] is False
    assert going["seconds"] == pytest.approx(7200, abs=60)
    assert going["amount"] == pytest.approx(2 * PER_HOUR, abs=0.05)
    assert not CostRecord.objects.exists()
    # Its pod ends: then it's recorded, the run's.
    assert pod.post("/deregister").status_code == 204
    (record,) = CostRecord.objects.all()
    assert (record.kind, str(record.run_id), record.project_id) == (
        CostKind.RUN,
        run["id"],
        priced.project.pk,
    )
    assert (record.team_id, record.action, record.template, record.tier) == (
        priced.project.team_id,
        COLABFOLD,
        "gpu-t4",
        "container",
    )
    assert record.price_per_hour == Decimal("1.8")
    assert float(record.amount or 0) == pytest.approx(2 * PER_HOUR, abs=0.05)
    done = cost(priced, run["id"])
    assert done["amount"] == pytest.approx(float(record.amount or 0))
    assert done["seconds"] == pytest.approx(float(record.seconds))


def test_a_pod_that_never_ran_a_job_is_the_organizations(priced: World) -> None:
    queue_run(priced)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    launch = Launch.objects.get()
    Launch.objects.filter(pk=launch.pk).update(created_at=ago(0.5))
    assert launches.end(launch, "failed", "It couldn't pull its image.")
    (record,) = CostRecord.objects.all()
    assert (record.kind, record.run, record.project, record.team) == (
        CostKind.IDLE,
        None,
        None,
        None,
    )
    assert float(record.amount or 0) == pytest.approx(PER_HOUR / 2, abs=0.05)


def test_a_persistent_pod_charges_runs_for_the_time_it_held_them(warm: World) -> None:  # noqa: F811
    rewrite_compute(
        lambda c: c["templates"]["gpu-warm"].update(price_per_hour=PER_HOUR, currency="USD")
    )
    cluster = FakeCluster()
    provisioner.tick(cluster)
    launch, pod = started(cluster)
    Launch.objects.filter(pk=launch.pk).update(created_at=ago(3))
    run = screen(warm)
    job = leased(pod)
    Job.objects.filter(pk=job["job"]["id"]).update(started_at=ago(1))
    # While it holds the job, the pod is labelled with whose it is.
    provisioner.tick(cluster)
    pod_name = f"{launch.name}-x1y2z"
    assert cluster.labelled[("iki-runs", pod_name)] == dict(
        zip(RUN_LABELS, (run["id"], str(warm.project.pk), str(warm.project.team_id)), strict=True)
    )
    going = cost(warm, run["id"])
    assert going["seconds"] == pytest.approx(3600, abs=60)
    ended = pod.report(job, "fail", reason="failed", message="no")
    assert ended.status_code == 200, ended.content
    (used,) = CostRecord.objects.all()
    assert (used.kind, str(used.run_id)) == (CostKind.RUN, run["id"])
    assert float(used.seconds) == pytest.approx(3600, abs=60)
    # Idle again, its labels go.
    provisioner.tick(cluster)
    assert cluster.labelled[("iki-runs", pod_name)] == {}
    # The rest of its life is the organization's, recorded when it ends.
    assert launches.end(Launch.objects.get(pk=launch.pk), "stopped")
    idle = CostRecord.objects.get(kind=CostKind.IDLE)
    assert float(idle.seconds) == pytest.approx(7200, abs=60)
    assert idle.project is None


def test_runs_on_tiers_iki_doesnt_start_machines_for_have_no_cost(priced: World) -> None:
    editor = api_as(priced, "editor")
    abl = protein(editor, priced.project, name="ACDE")
    made = launch(editor, priced.project, {"protein": abl["id"]})
    assert made.status_code == 201, made.content
    assert editor.get(f"/runs/{made.json()['id']}").json()["cost"] is None
    assert editor.get(f"/actions/{COMPUTE}").json()["estimate"] is None


# --- What a run may cost ---------------------------------------------------------------------


def test_an_action_says_what_a_run_typically_costs_and_at_most(priced: World) -> None:
    runner = api_as(priced, "runner")
    first = runner.get(f"/actions/{COLABFOLD}").json()["estimate"]
    assert first["template"] == "gpu-t4"
    assert (first["price_per_hour"], first["currency"]) == (PER_HOUR, "USD")
    # Nothing to go on yet but its timeout.
    assert (first["typical"], first["runs_seen"]) == (None, 0)
    for i, amount in enumerate(("0.10", "0.20", "0.90")):
        run = queue_run(priced, name="ACDEFG"[: i + 4])
        ActionRun.objects.filter(pk=run["id"]).update(
            status=RunStatus.SUCCEEDED, finished_at=timezone.now()
        )
        launch = Launch.objects.create(
            name=f"iki-run-{i}",
            template="gpu-t4",
            environment="structure_prediction.colabfold",
            provisioner="k8s_job",
            plugin_sha="",
            image="ghcr.io/acme/x@sha256:" + "a" * 64,
            namespace="iki-runs",
            spec="x",
            token_hash=f"h{i}",
            expires_at=timezone.now(),
        )
        CostRecord.objects.create(
            launch=launch,
            kind=CostKind.RUN,
            run_id=run["id"],
            project=priced.project,
            team=priced.project.team,
            action=COLABFOLD,
            template="gpu-t4",
            tier="container",
            started_at=ago(1),
            ended_at=timezone.now(),
            seconds=Decimal(200),
            price_per_hour=Decimal("1.8"),
            currency="USD",
            amount=Decimal(amount),
        )
    found = runner.get(f"/actions/{COLABFOLD}").json()["estimate"]
    assert (found["typical"], found["runs_seen"]) == (0.2, 3)
    assert found["timeout_cost"] == first["timeout_cost"] > 0


# --- Who may still run -----------------------------------------------------------------------


def test_a_run_whose_launcher_lost_the_role_doesnt_start(priced: World) -> None:
    run = queue_run(priced)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    _, pod = started(cluster)
    ProjectMembership.objects.filter(project=priced.project, user=priced.users["runner"]).update(
        role=ProjectRole.EDITOR
    )
    assert pod.lease().status_code == 204
    found = ActionRun.objects.get(pk=run["id"])
    assert found.status == RunStatus.REJECTED
    assert found.error is not None
    assert found.error["code"] == "not_allowed"
    assert "can no longer run actions on the container tier" in found.error["message"]


# --- Reports ---------------------------------------------------------------------------------


@pytest.fixture
def spent(priced: World) -> World:
    """Two hours of a run's pod, and half an hour of one that never ran a job."""
    run = queue_run(priced)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    launch, pod = started(cluster)
    Launch.objects.filter(pk=launch.pk).update(created_at=ago(2))
    leased(pod)
    pod.post("/deregister")
    queue_run(priced, name="ACDEF")
    provisioner.tick(cluster)
    idle = Launch.objects.exclude(state=LaunchState.ENDED).get()
    Launch.objects.filter(pk=idle.pk).update(created_at=ago(0.5))
    launches.end(idle, "failed")
    assert ActionRun.objects.filter(pk=run["id"]).exists()
    return priced


def test_compute_admins_see_what_runs_cost_by_project(spent: World) -> None:
    got = api_as(spent, "compute_admin").get("/compute/costs")
    assert got.status_code == 200, got.content
    body = got.json()
    (total,) = body["totals"]
    assert total["currency"] == "USD"
    assert total["amount"] == pytest.approx(2.5 * PER_HOUR, abs=0.1)
    project, idle = body["groups"]
    assert (project["key"], project["label"], project["runs"]) == (
        str(spent.project.pk),
        spent.project.name,
        1,
    )
    assert (idle["key"], idle["label"], idle["runs"]) == (None, "Idle machines", 0)
    by_template = api_as(spent, "auditor").get("/compute/costs?by=template").json()
    assert [g["key"] for g in by_template["groups"]] == ["gpu-t4"]


def test_costs_are_for_those_who_answer_for_them(spent: World) -> None:
    for who in ("compute_admin", "auditor", "org_admin"):
        assert api_as(spent, who).get("/compute/costs").status_code == 200, who
    for who in ("runner", "team_member", "admin"):
        problem(api_as(spent, who).get("/compute/costs"), 403, "forbidden")
        problem(api_as(spent, who).get("/compute/costs/records.csv"), 403, "forbidden")


def test_a_project_shows_what_its_runs_cost_to_those_who_can_read_it(spent: World) -> None:
    got = api_as(spent, "runner").get(f"/projects/{spent.project.pk}/costs")
    assert got.status_code == 200, got.content
    body = got.json()
    (group,) = body["groups"]
    assert (group["key"], group["runs"]) == (COLABFOLD, 1)
    # The organization's idle machines aren't any project's.
    assert body["totals"][0]["amount"] == pytest.approx(2 * PER_HOUR, abs=0.1)
    problem(api_as(spent, "outsider").get(f"/projects/{spent.project.pk}/costs"), 404, "not_found")


def test_a_period_is_checked(spent: World) -> None:
    admin = api_as(spent, "compute_admin")
    problem(admin.get("/compute/costs?since=2026-03-01&until=2026-03-01"), 422, "validation_failed")
    problem(admin.get("/compute/costs?since=2025-01-01&until=2026-03-01"), 422, "validation_failed")
    # Last year's costs aren't this month's.
    old = admin.get("/compute/costs?since=2025-01-01&until=2025-02-01").json()
    assert (old["totals"], old["groups"]) == ([], [])


def test_cost_records_download_for_finance(spent: World) -> None:
    spent.project.name = "=HYPERLINK(evil)"
    spent.project.save(update_fields=["name"])
    got = api_as(spent, "auditor").get("/compute/costs/records.csv")
    assert got.status_code == 200
    assert got["Content-Type"].startswith("text/csv")
    assert got["Cache-Control"] == "no-store"
    today = timezone.now().astimezone(UTC).date().replace(day=1)
    assert f'filename="iki-costs-{today}-' in got["Content-Disposition"]
    streamed = cast("StreamingHttpResponse", got).streaming_content
    content = b"".join(cast("Iterable[bytes]", streamed))
    rows = list(csv.DictReader(io.StringIO(content.decode())))
    assert sorted(r["kind"] for r in rows) == ["idle", "run"]
    run_row = next(r for r in rows if r["kind"] == "run")
    # A name never runs as a formula in a spreadsheet.
    assert run_row["project"] == "'=HYPERLINK(evil)"
    assert run_row["currency"] == "USD"
    recorded = CostRecord.objects.select_related("run").get(kind=CostKind.RUN)
    assert recorded.run is not None
    assert run_row["run"] == recorded.run.hrid


def test_a_pack_splits_its_machine_time_evenly_between_its_runs(priced: World) -> None:
    from test_run_batches import batch
    from test_workers import worker

    launched = batch(priced)
    pod = worker(priced)
    leased(pod)
    # As though its worker were on a machine Iki started for it, now ended.
    machine = Launch.objects.create(
        name="iki-run-pack",
        template="gpu-t4",
        provisioner="k8s_job",
        environment="protein.protein-tools",
        plugin_sha="",
        image="ghcr.io/acme/x@sha256:" + "a" * 64,
        namespace="iki-runs",
        token_hash="pack",
        expires_at=timezone.now(),
    )
    Launch.objects.filter(pk=machine.pk).update(created_at=ago(1))
    (only,) = Worker.objects.all()
    Worker.objects.filter(pk=only.pk).update(launch=machine)
    assert launches.end(Launch.objects.get(pk=machine.pk), "finished")
    records = list(CostRecord.objects.order_by("run_id"))
    assert sorted(str(r.run_id) for r in records) == sorted(r["id"] for r in launched["runs"])
    for record in records:
        assert record.kind == CostKind.RUN
        assert float(record.seconds) == pytest.approx(1200, abs=30)
    total = sum(float(r.amount or 0) for r in records)
    assert total == pytest.approx(PER_HOUR, abs=0.05)


def test_a_short_runs_pod_is_labelled_even_once_its_machine_has_ended(priced: World) -> None:
    run = queue_run(priced)
    cluster = FakeCluster()
    provisioner.tick(cluster)
    launch, pod = started(cluster)
    leased(pod)
    # It ran and finished between ticks; Kubernetes keeps its pod for a while.
    assert pod.post("/deregister").status_code == 204
    provisioner.tick(cluster)
    labels = cluster.labelled[("iki-runs", f"{launch.name}-x1y2z")]
    assert labels == dict(
        zip(
            RUN_LABELS,
            (run["id"], str(priced.project.pk), str(priced.project.team_id)),
            strict=True,
        )
    )
