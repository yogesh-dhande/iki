"""Preview, apply and switch a starter, preserving project grants and the reviewed configuration."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
from threading import Barrier
from uuid import UUID

import pytest
from api_client import Api, problem
from django.db import connection, transaction
from django.http import Http404
from org_world import World, build_world

from iki_server.accounts.models import User
from iki_server.amounts.models import Movement
from iki_server.audit.models import AuditEvent
from iki_server.entities import registry
from iki_server.entities.models import Entity, Intake
from iki_server.files.models import File
from iki_server.org import services
from iki_server.org.models import LabRoleGrant, ProjectMembership, ProjectRole
from iki_server.setup import starters
from iki_server.setup.models import StarterApplication

pytestmark = pytest.mark.django_db

# Use the ordinary test installation's real roles to exercise the config service.
ANALYST, REVIEWER, QA = (f"acme/protein.{r}" for r in ("analyst", "reviewer", "qa"))


@dataclass
class Setup:
    world: World
    roles: dict[str, list[UUID]]


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> Setup:
    world = build_world()
    recipe = replace(
        starters.DOSE_RESPONSE,
        roles={ANALYST: ProjectRole.EDITOR, REVIEWER: ProjectRole.VIEWER, QA: ProjectRole.VIEWER},
        required_roles={"research": (ANALYST, REVIEWER), "regulated_qc": (ANALYST, REVIEWER, QA)},
        independent_roles={"regulated_qc": (ANALYST, REVIEWER, QA)},
    )
    monkeypatch.setattr(starters, "installed_starters", lambda: {recipe.id: recipe})
    member = world.project.memberships.get
    roles = {
        ANALYST: [member(user=world.users["editor"]).pk],
        REVIEWER: [member(user=world.users["runner"]).pk],
        QA: [member(user=world.users["viewer"]).pk],
    }
    return Setup(world, roles)


def test_upgrade_records_the_reviewed_version(setup: Setup) -> None:
    project, admin = setup.world.project, setup.world.users["admin"]
    existing = StarterApplication.objects.create(
        project=project,
        starter="dose-response",
        version="1.2.0",
        flow="regulated_qc",
        applied_by=admin,
    )
    args = (project, "dose-response", "2.0.0", "regulated_qc", setup.roles)
    plan = starters.preview(*args, by=admin)
    assert plan["changed"]
    assert starters.apply(*args, by=admin, plan=plan["plan"])["changed"]
    assert not starters.apply(*args, by=admin, plan=plan["plan"])["changed"]
    existing.refresh_from_db()
    assert existing.version == "2.0.0"
    assert StarterApplication.objects.count() == 1
    assert AuditEvent.objects.get(action="starter.applied").data["before"] == {
        "version": "1.2.0",
        "flow": "regulated_qc",
    }


def test_preview_apply_replay_and_switch(setup: Setup) -> None:
    world, roles = setup.world, setup.roles
    project, admin = world.project, world.users["admin"]
    args = (project, "dose-response", "2.0.0", "regulated_qc", roles)
    plan = starters.preview(*args, by=admin)
    assert plan["changed"]
    assert plan["result_type"] == starters.PLATE + "QCAssayResult"
    assert plan["grants_to_add"] == {k: list(map(str, v)) for k, v in roles.items()}
    assert len(plan["plan"]) == 64
    assert not StarterApplication.objects.exists() and not LabRoleGrant.objects.exists()
    assert not AuditEvent.objects.filter(action="starter.applied").exists()

    applied = starters.apply(*args, by=admin, plan=plan["plan"])
    assert applied == plan
    # Sent again, the same approval changes nothing: it's set up that way already.
    replay = starters.apply(*args, by=admin, plan=plan["plan"])
    assert not replay["changed"] and replay["grants_to_add"] == {}
    assert LabRoleGrant.objects.filter(granted_by=admin).count() == 3
    events = AuditEvent.objects.filter(action="starter.applied", project=project, actor=admin)
    assert events.count() == 1
    first = events.get()
    assert first.data["before"] is None and first.data["after"]["flow"] == "regulated_qc"
    assert first.data["grants_added"] == plan["grants_to_add"]
    assert first.data["plan"] == plan["plan"]
    assert starters.list_starters(project)[0]["applied"] == {
        "version": "2.0.0",
        "flow": "regulated_qc",
    }

    research_roles = {k: v for k, v in roles.items() if k != QA}
    switch = (project, "dose-response", "2.0.0", "research", research_roles)
    reviewed = starters.preview(*switch, by=admin)
    assert reviewed["changed"] and reviewed["grants_to_add"] == {}
    # Another Admin revokes the reviewer's grant before this switch is applied. The switch
    # reviewed grants nothing (ADR 0207): applying it is refused, and changes nothing.
    revoked = LabRoleGrant.objects.get(role=REVIEWER)
    revoked.delete()
    with pytest.raises(starters.PlanChanged) as refused:
        starters.apply(*switch, by=admin, plan=reviewed["plan"])
    now = refused.value.plan
    assert now["grants_to_add"] == {REVIEWER: list(map(str, roles[REVIEWER]))}
    assert now["plan"] != reviewed["plan"]
    assert starters.effective_flow(project, "dose-response") == "regulated_qc"
    assert not LabRoleGrant.objects.filter(role=REVIEWER).exists()
    assert events.count() == 1
    # Reviewed again, the switch shows the grant it adds back; applying that plan adds it.
    again = starters.preview(*switch, by=admin)
    assert again == now
    switched = starters.apply(*switch, by=admin, plan=again["plan"])
    assert switched["changed"] and switched["result_type"] == starters.PLATE + "ResearchAssayResult"
    assert switched["grants_to_add"] == {REVIEWER: list(map(str, roles[REVIEWER]))}
    assert LabRoleGrant.objects.filter(role=QA).exists()
    assert LabRoleGrant.objects.filter(role=REVIEWER, granted_by=admin).exists()
    assert starters.effective_flow(project, "dose-response") == "research"
    change = events.exclude(pk=first.pk).get()
    assert change.data["before"]["flow"] == "regulated_qc"
    assert change.data["after"] == {"version": "2.0.0", "flow": "research"}
    assert change.data["grants_added"] == switched["grants_to_add"]
    assert change.data["plan"] == again["plan"]
    # No records: only the registry's lab lists' options, which the sync seeded (ADR 0270).
    assert not Entity.objects.exclude(type__in=list(registry.lab_lists())).exists()
    assert not File.objects.exists()
    assert not Movement.objects.exists()
    assert not Intake.objects.exists()


def test_a_stale_first_apply_adds_no_grants(setup: Setup) -> None:
    """Changing the choice after another Admin applied a flow requires a fresh preview."""
    world, roles = setup.world, setup.roles
    project, admin = world.project, world.users["admin"]
    args = (project, "dose-response", "2.0.0", "regulated_qc", roles)
    mine = starters.preview(*args, by=admin)
    # The other Admin applies Research first, which adds both common grants.
    fewer = {k: v for k, v in roles.items() if k != QA}
    theirs = starters.preview(project, "dose-response", "2.0.0", "research", fewer, by=admin)
    starters.apply(
        project,
        "dose-response",
        "2.0.0",
        "research",
        fewer,
        by=admin,
        plan=theirs["plan"],
    )
    grants = set(LabRoleGrant.objects.values_list("membership_id", "role"))
    with pytest.raises(starters.PlanChanged) as refused:
        starters.apply(*args, by=admin, plan=mine["plan"])
    assert refused.value.plan["grants_to_add"] == {QA: list(map(str, roles[QA]))}
    assert set(LabRoleGrant.objects.values_list("membership_id", "role")) == grants
    assert starters.effective_flow(project, "dose-response") == "research"
    assert AuditEvent.objects.filter(action="starter.applied").count() == 1


def test_the_api_refuses_a_changed_plan_and_answers_what_applying_does_now(setup: Setup) -> None:
    """409 ``starter_plan_changed``, with the plan as it is now: applying that applies it."""
    world, roles = setup.world, setup.roles
    api = Api(world.browser("admin"))
    body = {
        "project": str(world.project.pk),
        "version": "2.0.0",
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
    applied = api.post("/starters/dose-response/apply", {**body, "plan": now["plan"]})
    assert applied.status_code == 200, applied.content
    assert applied.json()["grants_to_add"] == now["grants_to_add"]
    missing = problem(api.post("/starters/dose-response/apply", body), 422, "validation_failed")
    assert missing["errors"][0]["pointer"] == "#/plan"


def test_rejects_unusable_role_choices_and_stale_recipe_without_writes(setup: Setup) -> None:
    world, roles = setup.world, setup.roles
    project, admin = world.project, world.users["admin"]

    def attempt(
        assign: dict[str, list[UUID]], *, version: str = "2.0.0", starter: str = "dose-response"
    ) -> None:
        starters.apply(
            project,
            starter,
            version,
            "regulated_qc",
            assign,
            by=admin,
            plan="0" * 64,
        )

    with pytest.raises(Http404):
        attempt(roles, starter="unknown")
    with pytest.raises(services.Refused, match="installed version"):
        attempt(roles, version="1.2.0")
    with pytest.raises(services.Refused, match="project member"):
        attempt({ANALYST: roles[ANALYST]})
    with pytest.raises(services.Refused, match="not a role"):
        attempt({**roles, "acme/protein.unrelated": roles[QA]})
    with pytest.raises(services.Refused, match="direct membership"):
        attempt({**roles, ANALYST: [world.archived.memberships.get(user=admin).pk]})
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


@pytest.mark.parametrize("operation", [starters.preview, partial(starters.apply, plan="0" * 64)])
def test_archived_service_refuses_setup(setup: Setup, operation: object) -> None:
    assert callable(operation)
    with pytest.raises(services.Refused, match="Restore the project"):
        operation(
            setup.world.archived,
            "dose-response",
            "2.0.0",
            "research",
            setup.roles,
            by=setup.world.users["admin"],
        )


@pytest.mark.django_db(transaction=True)
def test_concurrent_applies_keep_one_configuration_and_other_role_grants(setup: Setup) -> None:
    """An ordinary membership update committed during two first applies stays on the member."""
    project, admin = setup.world.project, setup.world.users["admin"]
    membership = ProjectMembership.objects.get(pk=setup.roles[ANALYST][0])
    barrier = Barrier(3, timeout=20)
    # Both reviewed the same plan: the membership update below doesn't change what it does.
    reviewed = starters.preview(
        project,
        "dose-response",
        "2.0.0",
        "regulated_qc",
        setup.roles,
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
                    "2.0.0",
                    "regulated_qc",
                    setup.roles,
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


def test_an_experiment_review_flow_asks_only_for_the_signers_it_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every experiment-review flow's records are experiments (ADR 0247). An author's own
    sign-off needs one author; a witness must be someone else."""
    world = build_world()
    author, witness, qa = ANALYST, REVIEWER, QA
    recipe = replace(
        starters.EXPERIMENT_REVIEW,
        roles={author: ProjectRole.EDITOR, witness: ProjectRole.VIEWER, qa: ProjectRole.VIEWER},
        required_roles={
            "author_signed": (author,),
            "research": (author, witness),
            "regulated_qc": (author, witness, qa),
        },
        independent_roles={"research": (author, witness), "regulated_qc": (author, witness, qa)},
    )
    monkeypatch.setattr(starters, "installed_starters", lambda: {recipe.id: recipe})
    project, admin = world.project, world.users["admin"]
    editor = [project.memberships.get(user=world.users["editor"]).pk]
    listed = starters.list_starters(project)[0]
    assert [(f["id"], f["states"]) for f in listed["flows"]] == [
        ("author_signed", ["recorded", "signed"]),
        ("research", ["recorded", "witnessed"]),
        ("regulated_qc", ["recorded", "submitted", "reviewed", "released"]),
    ]
    assert {f["result_type"] for f in listed["flows"]} == {starters.EXPERIMENT + "Experiment"}
    assert set(listed["types"]) == {starters.EXPERIMENT + "Experiment"}
    assert [f["summary"] for f in listed["flows"]] == [
        recipe.summaries[f] for f in ("author_signed", "research", "regulated_qc")
    ]
    with pytest.raises(services.Refused, match="two different people"):
        starters.preview(
            project,
            "experiment-review",
            "1.0.0",
            "research",
            {author: editor, witness: editor},
            by=admin,
        )
    args = (project, "experiment-review", "1.0.0", "author_signed", {author: editor})
    plan = starters.preview(*args, by=admin)
    assert plan["result_type"] == starters.EXPERIMENT + "Experiment"
    assert starters.apply(*args, by=admin, plan=plan["plan"])["changed"]
    assert starters.effective_flow(project, "experiment-review") == "author_signed"
