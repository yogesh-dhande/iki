"""Budgets (ADR 0071): what the organization, a team or a project may spend on runs'
machines in a month. Passing a warning level tells those who answer for it, once; an
enforced budget that's spent stops new container and VM runs in its scope."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from api_client import problem
from django.utils import timezone
from org_world import World
from test_api_entities import api_as
from test_container_tier import (  # noqa: F401 - fixtures
    COLABFOLD,
    FakeCluster,
    keep_connection,
    queue_run,
    rewrite_compute,
    world,
)

from iki_server.audit.models import AuditEvent
from iki_server.notifications.models import Notification
from iki_server.org.models import ProjectMembership, ProjectRole
from iki_server.runs import budgets, provisioner
from iki_server.runs.models import (
    ActionRun,
    Budget,
    BudgetAlert,
    CostKind,
    CostRecord,
    Launch,
    RunStatus,
)
from iki_server.webhooks import services as webhooks
from iki_server.webhooks.models import Delivery

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


_made = 0


def spend(of: World, amount: str, *, run: dict[str, Any] | None = None) -> dict[str, Any]:
    """Records `amount` spent this month on a run of the of's project."""
    global _made
    _made += 1
    run = run or queue_run(of, name="ACDEFGHIKLMN"[: 4 + _made % 8])
    launch = Launch.objects.create(
        name=f"iki-run-spent-{_made}",
        template="gpu-t4",
        provisioner="k8s_job",
        environment="structure_prediction.colabfold",
        plugin_sha="",
        image="ghcr.io/acme/x@sha256:" + "a" * 64,
        namespace="iki-runs",
        token_hash=f"spent-{_made}",
        expires_at=timezone.now(),
    )
    CostRecord.objects.create(
        launch=launch,
        kind=CostKind.RUN,
        run_id=run["id"],
        project=of.project,
        team=of.project.team,
        action=COLABFOLD,
        template="gpu-t4",
        tier="container",
        started_at=timezone.now() - timedelta(hours=1),
        ended_at=timezone.now(),
        seconds=Decimal(3600),
        price_per_hour=Decimal("1.8"),
        currency="USD",
        amount=Decimal(amount),
    )
    return run


def set_budget(of: World, **body: Any) -> dict[str, Any]:
    made = api_as(of, "compute_admin").post("/compute/budgets", body)
    assert made.status_code == 201, made.content
    found: dict[str, Any] = made.json()
    return found


def launch(of: World) -> Any:
    from test_runs import protein

    global _made
    _made += 1
    runner = api_as(of, "runner")
    abl = protein(runner, of.project, name="WYVWYVWYV"[: 2 + _made % 7] + "M" * _made)
    return runner.post(
        f"/actions/{COLABFOLD}/runs",
        {"project": str(of.project.pk), "inputs": {"protein": abl["id"]}},
    )


# --- Setting budgets -------------------------------------------------------------------------


def test_compute_admins_set_budgets_in_the_deployments_currency(priced: World) -> None:
    project = set_budget(priced, project=str(priced.project.pk), amount="500")
    assert (project["amount"], project["currency"], project["warn_at"]) == (
        500.0,
        "USD",
        [50, 80, 100],
    )
    assert (project["spent"], project["percent"], project["enforce"]) == (0.0, 0, False)
    assert project["project"]["name"] == priced.project.name
    org = set_budget(priced, amount="10000.50", warn_at=[90, 75, 75], enforce=True)
    assert (org["team"], org["project"], org["warn_at"]) == (None, None, [75, 90])
    # One per scope.
    problem(
        api_as(priced, "compute_admin").post(
            "/compute/budgets", {"project": str(priced.project.pk), "amount": "1"}
        ),
        409,
        "refused",
    )
    # Auditors and Org Admins see them, but only Compute Admins set them.
    listed = api_as(priced, "auditor").get("/compute/budgets").json()
    assert [b["id"] for b in listed] == [org["id"], project["id"]]
    problem(api_as(priced, "org_admin").post("/compute/budgets", {"amount": "1"}), 403, "forbidden")
    event = AuditEvent.objects.get(action="budget.created", project=priced.project)
    assert event.data == {
        "amount": "500.00",
        "currency": "USD",
        "warn_at": [50, 80, 100],
        "enforce": False,
    }


def test_a_budget_is_checked(priced: World) -> None:
    admin = api_as(priced, "compute_admin")
    for body in (
        {"amount": "0"},
        {"amount": "-5"},
        {"amount": "1.234"},
        {"amount": "10", "warn_at": [0]},
        {"amount": "10", "warn_at": [101]},
        {"amount": "10", "team": str(priced.team.pk), "project": str(priced.project.pk)},
    ):
        problem(admin.post("/compute/budgets", body), 422, "validation_failed")


def test_nothing_to_budget_until_something_has_a_price(world: World) -> None:  # noqa: F811
    problem(
        api_as(world, "compute_admin").post("/compute/budgets", {"amount": "10"}), 409, "refused"
    )


def test_changing_and_removing_a_budget_is_audited(priced: World) -> None:
    made = set_budget(priced, team=str(priced.team.pk), amount="100")
    admin = api_as(priced, "compute_admin")
    changed = admin.patch(f"/compute/budgets/{made['id']}", {"amount": "250", "enforce": True})
    assert changed.status_code == 200, changed.content
    assert (changed.json()["amount"], changed.json()["enforce"]) == (250.0, True)
    event = AuditEvent.objects.get(action="budget.updated")
    assert event.data["before"]["amount"] == "100.00"
    assert event.data["after"] == {
        "amount": "250.00",
        "currency": "USD",
        "warn_at": [50, 80, 100],
        "enforce": True,
    }
    assert admin.delete(f"/compute/budgets/{made['id']}").status_code == 204
    assert not Budget.objects.exists()
    assert AuditEvent.objects.filter(action="budget.removed").exists()


# --- Spending --------------------------------------------------------------------------------


def test_spent_is_this_months_records_in_the_budgets_scope(priced: World) -> None:
    spend(priced, "1.25")
    spend(priced, "2.00")
    # Last month's isn't this month's.
    CostRecord.objects.filter(amount=Decimal("2.00")).update(
        ended_at=timezone.now().replace(day=1) - timedelta(days=2)
    )
    project = Budget.objects.get(
        pk=set_budget(priced, project=str(priced.project.pk), amount="10")["id"]
    )
    team = Budget.objects.get(pk=set_budget(priced, team=str(priced.team.pk), amount="10")["id"])
    other = Budget.objects.get(
        pk=set_budget(priced, team=str(priced.other_team.pk), amount="10")["id"]
    )
    assert budgets.spent(project) == Decimal("1.25")
    assert budgets.spent(team) == Decimal("1.25")
    assert budgets.spent(other) == Decimal(0)


def test_a_project_shows_its_own_budget_to_those_who_can_read_it(priced: World) -> None:
    spend(priced, "3.00")
    set_budget(priced, project=str(priced.project.pk), amount="4", enforce=True)
    got = api_as(priced, "viewer").get(f"/projects/{priced.project.pk}/costs").json()
    assert got["budget"] == {
        "amount": 4.0,
        "currency": "USD",
        "month": timezone.now().date().replace(day=1).isoformat(),
        "spent": 3.0,
        "percent": 75,
        "enforce": True,
        "stopped": False,
    }


# --- Warnings --------------------------------------------------------------------------------


def test_passing_a_warning_level_tells_those_who_answer_for_it_once(priced: World) -> None:
    webhooks.create(
        project=priced.project,
        url="https://hooks.example.com/iki",
        description="",
        chosen=["budget.threshold_reached"],
        by=priced.users["admin"],
    )
    budget = set_budget(priced, project=str(priced.project.pk), amount="10")
    spend(priced, "5.50")
    assert budgets.send_alerts() == 1
    (sent,) = Delivery.objects.filter(event="budget.threshold_reached")
    data = sent.payload["data"]
    assert data["budget"]["scope"]["kind"] == "project"
    assert data["budget"]["scope"]["project"]["id"] == str(priced.project.pk)
    assert (data["threshold"], data["spent"], data["amount"], data["currency"]) == (
        50,
        "5.50",
        "10.00",
        "USD",
    )
    told = Notification.objects.filter(kind="budget.threshold_reached")
    admins = {
        m.user_id
        for m in ProjectMembership.objects.filter(
            project=priced.project, role=ProjectRole.ADMIN, user__is_active=True
        )
    }
    assert {n.recipient_id for n in told} == {priced.users["compute_admin"].pk} | admins
    (first, *_) = told
    assert first.title == f"50% of the budget for {priced.project.name} is spent for " + (
        timezone.now().strftime("%B")
    )
    assert first.body == "$5.50 of $10.00 spent on runs’ machines so far this month."
    assert first.path == f"/projects/{priced.project.slug}/"
    event = AuditEvent.objects.get(action="budget.threshold_reached")
    assert (event.data["threshold"], event.data["spent"]) == (50, "5.50")
    # Once a month each.
    assert budgets.send_alerts() == 0
    # Passing two more at once is one message, of the highest.
    spend(priced, "5.00")
    assert budgets.send_alerts() == 1
    latest = told.order_by("-created_at").first()
    assert latest is not None
    assert latest.title.startswith(f"The budget for {priced.project.name} is spent")
    assert sorted(
        BudgetAlert.objects.filter(budget_id=budget["id"]).values_list("threshold", flat=True)
    ) == [50, 80, 100]


def test_an_enforced_organization_budget_says_runs_stop(priced: World) -> None:
    set_budget(priced, amount="1", enforce=True)
    spend(priced, "1.50")
    budgets.send_alerts()
    told = Notification.objects.get(
        kind="budget.threshold_reached", recipient=priced.users["compute_admin"]
    )
    assert told.title.startswith("The budget for the organization is spent")
    assert "No more runs on the container and VM tiers start" in told.body
    assert told.path == "/costs/"


# --- Stopping spending -----------------------------------------------------------------------


def test_an_enforced_budget_thats_spent_stops_new_costly_runs(priced: World) -> None:
    waiting = spend(priced, "3.60")
    budget = set_budget(priced, project=str(priced.project.pk), amount="1", enforce=True)
    month = timezone.now().strftime("%B")
    refused = problem(launch(priced), 409, "refused")
    assert refused["detail"] == (
        f"The project’s budget for {month} is spent ($3.60 of $1.00), so runs on the "
        "container tier can't start until next month. A Compute Admin can raise it."
    )
    # What's queued waits, and says why; no pod starts for it.
    assert provisioner.tick(FakeCluster()) == (0, 0)
    found = api_as(priced, "runner").get(f"/runs/{waiting['id']}").json()
    assert found["waiting"] == f"The project’s budget for {month} is spent ($3.60 of $1.00)."
    # Raising it lets them start.
    api_as(priced, "compute_admin").patch(f"/compute/budgets/{budget['id']}", {"amount": "50"})
    assert launch(priced).status_code == 201
    assert provisioner.tick(FakeCluster())[0] >= 1
    assert ActionRun.objects.get(pk=waiting["id"]).status == RunStatus.QUEUED


def test_a_budget_thats_only_warned_of_stops_nothing(priced: World) -> None:
    spend(priced, "3.60")
    set_budget(priced, project=str(priced.project.pk), amount="1")
    assert launch(priced).status_code == 201


def test_a_launch_its_runs_typical_cost_would_take_past_the_budget_is_refused(
    priced: World,
) -> None:
    for amount in ("0.40", "0.50", "0.60"):
        run = spend(priced, amount)
        ActionRun.objects.filter(pk=run["id"]).update(
            status=RunStatus.SUCCEEDED, finished_at=timezone.now()
        )
    # $1.50 of $1.80 spent: $0.30 left, and a run typically costs $0.50.
    set_budget(priced, team=str(priced.team.pk), amount="1.80", enforce=True)
    refused = problem(launch(priced), 409, "refused")
    assert refused["detail"] == (
        f"Its team’s budget for {timezone.now().strftime('%B')} has $0.30 left, and a run "
        "typically costs about $0.50. Launch fewer, or ask a Compute Admin to raise it."
    )
