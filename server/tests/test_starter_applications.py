"""Preview, apply and switch a starter, preserving project grants and its example work."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Barrier
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
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
    assert not StarterApplication.objects.exists() and not LabRoleGrant.objects.exists()
    assert not AuditEvent.objects.filter(action="starter.applied").exists()
    setup.examples.seed_example.assert_not_called()

    applied = starters.apply(*args, requester_project=ask, by=admin)
    original = applied["examples"]
    assert applied["changed"] and str(UUID(original["plate"])) == original["plate"]
    replay = starters.apply(*args, requester_project=ask, by=admin)
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
    assert starters.list_starters(project, by=admin)[0]["applied"]["requester_project"] == str(
        ask.pk
    )
    visible = starters.list_starters(project, by=world.users["viewer"])[0]["applied"]
    assert visible == {"version": "1.0.0", "flow": "regulated_qc", "requester_project": None}

    research_roles = {k: v for k, v in roles.items() if k != QA}
    switched = starters.apply(
        project,
        "dose-response",
        "1.0.0",
        "research",
        research_roles,
        requester_project=ask,
        by=admin,
    )
    assert switched["changed"] and switched["examples"] == original
    assert switched["grants_to_add"] == {} and switched["examples_to_create"] == []
    assert LabRoleGrant.objects.filter(role=QA).exists()
    assert starters.effective_flow(project, "dose-response") == "research"
    change = events.exclude(pk=first.pk).get()
    assert change.data["before"]["flow"] == "regulated_qc"
    assert change.data["after"] == {"version": "1.0.0", "flow": "research"}
    setup.examples.seed_example.assert_called_once()
    # If a requested grant was subsequently revoked, reapplying restores just that one.
    LabRoleGrant.objects.filter(role=REVIEWER).delete()
    restored = starters.apply(
        project,
        "dose-response",
        "1.0.0",
        "research",
        research_roles,
        requester_project=ask,
        by=admin,
    )
    assert restored["grants_to_add"] == {REVIEWER: list(map(str, roles[REVIEWER]))}
    assert restored["examples"] == original


def test_rejects_unusable_role_choices_and_stale_recipe_without_writes(setup: Setup) -> None:
    world, ask, roles = setup.world, setup.requester, setup.roles
    project, admin = world.project, world.users["admin"]

    def attempt(
        assign: dict[str, list[UUID]], *, version: str = "1.0.0", starter: str = "dose-response"
    ) -> None:
        starters.apply(
            project, starter, version, "regulated_qc", assign, requester_project=ask, by=admin
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


@pytest.mark.parametrize("operation", [starters.preview, starters.apply])
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
