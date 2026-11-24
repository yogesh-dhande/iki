"""Preview, apply and switch a starter, preserving project grants and its example work."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
from threading import Barrier
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from api_client import Api, problem
from django.db import connection, transaction
from django.http import Http404
from org_world import World, build_world

from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.org import services
from iki_server.org.models import LabRoleGrant, Project, ProjectMembership, ProjectRole
from iki_server.setup import starters
from iki_server.setup.models import StarterApplication

pytestmark = pytest.mark.django_db

# Use the ordinary test installation's real, declared roles. The two-project recipe with
# the shipping plate plugin, including its actual examples, runs in test_dose_response_starter.
ANALYST, REVIEWER, QA = (f"acme/protein.{r}" for r in ("analyst", "reviewer", "qa"))


@dataclass
class Setup:
    world: World
    requester: Project
    roles: dict[str, list[UUID]]
    examples: Mock


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> Setup:
    world = build_world()
    examples = Mock()
    examples.seed_example.side_effect = lambda *a, **kw: {"plate": str(uuid4())}
    recipe = replace(
        starters.DOSE_RESPONSE,
        roles={ANALYST: ProjectRole.EDITOR, REVIEWER: ProjectRole.VIEWER, QA: ProjectRole.VIEWER},
        required_roles={"research": (ANALYST, REVIEWER), "regulated_qc": (ANALYST, REVIEWER, QA)},
        independent_roles={"regulated_qc": (ANALYST, REVIEWER, QA)},
        examples=examples,
        example_keys=("plate",),
    )
    monkeypatch.setattr(starters, "installed_starters", lambda: {recipe.id: recipe})
    requester = Project.objects.create(team=world.team, name="Requester")
    ProjectMembership.objects.create(
        project=requester, user=world.users["admin"], role=ProjectRole.EDITOR
    )
    member = world.project.memberships.get
    roles = {
        ANALYST: [member(user=world.users["editor"]).pk],
        REVIEWER: [member(user=world.users["runner"]).pk],
        QA: [member(user=world.users["viewer"]).pk],
    }
    return Setup(world, requester, roles, examples)


def test_preview_apply_replay_and_switch(setup: Setup) -> None:
    world, ask, roles = setup.world, setup.requester, setup.roles
    project, admin = world.project, world.users["admin"]
    args = (project, "dose-response", "1.0.0", "regulated_qc", roles)
    plan = starters.preview(*args, requester_project=ask, by=admin)
    assert plan["changed"] and plan["examples_to_create"] == ["plate"] and plan["examples"] == {}
    assert plan["grants_to_add"] == {k: list(map(str, v)) for k, v in roles.items()}
    assert len(plan["plan"]) == 64
    assert not StarterApplication.objects.exists() and not LabRoleGrant.objects.exists()
    assert not AuditEvent.objects.filter(action="starter.applied").exists()
    setup.examples.seed_example.assert_not_called()

    applied = starters.apply(*args, requester_project=ask, by=admin, plan=plan["plan"])
    original = applied["examples"]
    assert applied["changed"] and str(UUID(original["plate"])) == original["plate"]
    # Sent again, the same approval changes nothing: it's set up that way already.
    replay = starters.apply(*args, requester_project=ask, by=admin, plan=plan["plan"])
    assert (
        not replay["changed"] and replay["grants_to_add"] == {} and replay["examples"] == original
    )
    setup.examples.seed_example.assert_called_once()
    assert LabRoleGrant.objects.filter(granted_by=admin).count() == 3
    events = AuditEvent.objects.filter(action="starter.applied", project=project, actor=admin)
    assert events.count() == 1
    first = events.get()
    assert first.data["before"] is None and first.data["after"]["flow"] == "regulated_qc"
    assert first.data["grants_added"] == plan["grants_to_add"]
    assert first.data["plan"] == plan["plan"]
    assert starters.list_starters(project, by=admin)[0]["applied"]["requester_project"] == str(
        ask.pk
    )
    visible = starters.list_starters(project, by=world.users["viewer"])[0]["applied"]
    assert visible == {"version": "1.0.0", "flow": "regulated_qc", "requester_project": None}

    research_roles = {k: v for k, v in roles.items() if k != QA}
    switch = (project, "dose-response", "1.0.0", "research", research_roles)
    reviewed = starters.preview(*switch, requester_project=ask, by=admin)
    assert reviewed["changed"] and reviewed["grants_to_add"] == {}
    # Another Admin revokes the reviewer's grant before this switch is applied. The switch
    # reviewed grants nothing (ADR 0207): applying it is refused, and changes nothing.
    revoked = LabRoleGrant.objects.get(role=REVIEWER)
    revoked.delete()
    with pytest.raises(starters.PlanChanged) as refused:
        starters.apply(*switch, requester_project=ask, by=admin, plan=reviewed["plan"])
    now = refused.value.plan
    assert now["grants_to_add"] == {REVIEWER: list(map(str, roles[REVIEWER]))}
    assert now["plan"] != reviewed["plan"]
    assert starters.effective_flow(project, "dose-response") == "regulated_qc"
    assert not LabRoleGrant.objects.filter(role=REVIEWER).exists()
    assert events.count() == 1
    # Reviewed again, the switch shows the grant it adds back; applying that plan adds it.
    again = starters.preview(*switch, requester_project=ask, by=admin)
    assert again == now
    switched = starters.apply(*switch, requester_project=ask, by=admin, plan=again["plan"])
    assert switched["changed"] and switched["examples"] == original
    assert switched["grants_to_add"] == {REVIEWER: list(map(str, roles[REVIEWER]))}
    assert switched["examples_to_create"] == []
    assert LabRoleGrant.objects.filter(role=QA).exists()
    assert LabRoleGrant.objects.filter(role=REVIEWER, granted_by=admin).exists()
    assert starters.effective_flow(project, "dose-response") == "research"
    change = events.exclude(pk=first.pk).get()
    assert change.data["before"]["flow"] == "regulated_qc"
    assert change.data["after"] == {"version": "1.0.0", "flow": "research"}
    assert change.data["grants_added"] == switched["grants_to_add"]
    assert change.data["plan"] == again["plan"]
    setup.examples.seed_example.assert_called_once()


def test_a_stale_first_apply_seeds_nothing(setup: Setup) -> None:
    """Applied first by another Admin, a first apply's plan (examples to create, every grant)
    no longer holds: it creates no second example and grants nothing it didn't show."""
    world, ask, roles = setup.world, setup.requester, setup.roles
    project, admin = world.project, world.users["admin"]
    args = (project, "dose-response", "1.0.0", "regulated_qc", roles)
    mine = starters.preview(*args, requester_project=ask, by=admin)
    # The other Admin applies the Research flow first; then this one's plan, which showed
    # every grant and a new example, is stale.
    fewer = {k: v for k, v in roles.items() if k != QA}
    theirs = starters.preview(
        project, "dose-response", "1.0.0", "research", fewer, requester_project=ask, by=admin
    )
    starters.apply(
        project,
        "dose-response",
        "1.0.0",
        "research",
        fewer,
        requester_project=ask,
        by=admin,
        plan=theirs["plan"],
    )
    grants = set(LabRoleGrant.objects.values_list("membership_id", "role"))
    with pytest.raises(starters.PlanChanged) as refused:
        starters.apply(*args, requester_project=ask, by=admin, plan=mine["plan"])
    assert refused.value.plan["examples_to_create"] == []
    assert set(LabRoleGrant.objects.values_list("membership_id", "role")) == grants
    assert starters.effective_flow(project, "dose-response") == "research"
    assert AuditEvent.objects.filter(action="starter.applied").count() == 1
    setup.examples.seed_example.assert_called_once()


def test_the_api_refuses_a_changed_plan_and_answers_what_applying_does_now(setup: Setup) -> None:
    """409 ``starter_plan_changed``, with the plan as it is now: applying that applies it."""
    world, roles = setup.world, setup.roles
    api = Api(world.browser("admin"))
    body = {
        "project": str(world.project.pk),
        "requester_project": str(setup.requester.pk),
        "version": "1.0.0",
        "flow": "regulated_qc",
        "role_assignments": {k: list(map(str, v)) for k, v in roles.items()},
    }
    reviewed = api.post("/starters/dose-response/preview", body).json()
    # Another Admin grants the reviewer role first: applying would no longer grant it.
    membership = ProjectMembership.objects.get(pk=roles[REVIEWER][0])
    services.set_lab_roles(membership, {REVIEWER}, by=world.users["admin"])
    refused = problem(
        api.post("/starters/dose-response/apply", {**body, "plan": reviewed["plan"]}),
        409,
        "starter_plan_changed",
    )
    now = refused["plan"]
    assert REVIEWER in reviewed["grants_to_add"] and REVIEWER not in now["grants_to_add"]
    assert now == api.post("/starters/dose-response/preview", body).json()
    assert not StarterApplication.objects.exists()
    assert not AuditEvent.objects.filter(action="starter.applied").exists()
    assert LabRoleGrant.objects.count() == 1
    setup.examples.seed_example.assert_not_called()
    applied = api.post("/starters/dose-response/apply", {**body, "plan": now["plan"]})
    assert applied.status_code == 200, applied.content
    assert applied.json()["grants_to_add"] == now["grants_to_add"]
    missing = problem(api.post("/starters/dose-response/apply", body), 422, "validation_failed")
    assert missing["errors"][0]["pointer"] == "#/plan"


def test_rejects_unusable_role_choices_and_stale_recipe_without_writes(setup: Setup) -> None:
    world, ask, roles = setup.world, setup.requester, setup.roles
    project, admin = world.project, world.users["admin"]

    def attempt(
        assign: dict[str, list[UUID]], *, version: str = "1.0.0", starter: str = "dose-response"
    ) -> None:
        starters.apply(
            project,
            starter,
            version,
            "regulated_qc",
            assign,
            requester_project=ask,
            by=admin,
            plan="0" * 64,
        )

    with pytest.raises(Http404):
        attempt(roles, starter="unknown")
    with pytest.raises(services.Refused, match="installed version"):
        attempt(roles, version="2.0.0")
    with pytest.raises(services.Refused, match="project member"):
        attempt({ANALYST: roles[ANALYST]})
    with pytest.raises(services.Refused, match="not a role"):
        attempt({**roles, "acme/protein.unrelated": roles[QA]})
    with pytest.raises(services.Refused, match="direct membership"):
        attempt({**roles, ANALYST: [ask.memberships.get(user=admin).pk]})
    with pytest.raises(services.Refused, match="active people"):
        attempt({**roles, QA: [project.memberships.get(user=world.users["inactive"]).pk]})
    with pytest.raises(services.Refused, match="Editor"):
        attempt({**roles, ANALYST: roles[QA]})
    # Three unique candidates overall still cannot cover the three distinct signatures.
    with pytest.raises(services.Refused, match="three different people"):
        attempt(
            {ANALYST: roles[ANALYST], REVIEWER: roles[ANALYST] * 5, QA: roles[QA] + roles[REVIEWER]}
        )
    assert not StarterApplication.objects.exists() and not LabRoleGrant.objects.exists()
    setup.examples.seed_example.assert_not_called()


@pytest.mark.parametrize("operation", [starters.preview, partial(starters.apply, plan="0" * 64)])
def test_archived_service_refuses_setup(setup: Setup, operation: object) -> None:
    assert callable(operation)
    with pytest.raises(services.Refused, match="Restore the assay project"):
        operation(
            setup.world.archived,
            "dose-response",
            "1.0.0",
            "research",
            setup.roles,
            requester_project=setup.requester,
            by=setup.world.users["admin"],
        )


@pytest.mark.django_db(transaction=True)
def test_concurrent_applies_keep_one_example_and_other_role_grants(setup: Setup) -> None:
    """An ordinary membership update committed during two first applies stays on the member."""
    project, admin = setup.world.project, setup.world.users["admin"]
    membership = ProjectMembership.objects.get(pk=setup.roles[ANALYST][0])
    barrier = Barrier(3, timeout=20)
    # Both reviewed the same plan: the membership update below doesn't change what it does.
    reviewed = starters.preview(
        project,
        "dose-response",
        "1.0.0",
        "regulated_qc",
        setup.roles,
        requester_project=setup.requester,
        by=admin,
    )["plan"]

    def apply() -> bool:
        try:
            actor = User.objects.get(pk=admin.pk)
            barrier.wait()
            return bool(
                starters.apply(
                    project,
                    "dose-response",
                    "1.0.0",
                    "regulated_qc",
                    setup.roles,
                    requester_project=setup.requester,
                    by=actor,
                    plan=reviewed,
                )["changed"]
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        # The ordinary membership service acquires the project lock before reading its roles.
        # Both simultaneous first applies must see its commit before planning their additions.
        with transaction.atomic():
            services.set_lab_roles(membership, {QA}, by=admin)
            futures = [pool.submit(apply) for _ in range(2)]
            barrier.wait()
        assert sorted(f.result(timeout=20) for f in futures) == [False, True]
    assert set(membership.lab_roles.values_list("role", flat=True)) == {ANALYST, QA}
    assert StarterApplication.objects.count() == 1
    assert AuditEvent.objects.filter(action="starter.applied").count() == 1
    setup.examples.seed_example.assert_called_once()
