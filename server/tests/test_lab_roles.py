"""Lab roles (ADR 0116): named roles plugins declare, given with project memberships beside
their project role, which workflow approvals and state fields' states (ADR 0125) need."""

from typing import Any

import pytest
from api_client import problem
from org_world import World
from test_api_entities import api_as
from test_runs import protein
from test_workers import keep_connection, worker, world
from test_workflows import complete, get, run_tasks, start, steps

from iki_server.audit.models import AuditEvent
from iki_server.notifications.models import Notification
from iki_server.org import permissions, services
from iki_server.org.models import LabRoleGrant, ProjectMembership, ProjectRole

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # fixtures, from test_workers

QA, REVIEWER, ANALYST = "acme/protein.qa", "acme/protein.reviewer", "acme/protein.analyst"
RELEASE_THEN_WEIGH = "acme/protein.release_then_weigh"


def membership(world: World, principal: str) -> ProjectMembership:
    return world.project.memberships.get(user=world.users[principal])


def team_membership(world: World) -> ProjectMembership:
    return world.project.memberships.get(team=world.other_team)


def give(world: World, grant: ProjectMembership, *roles: str, by: str = "admin") -> Any:
    path = f"/projects/{world.project.pk}/members/{grant.pk}/lab-roles"
    return api_as(world, by).put(path, {"roles": list(roles)})


def held(world: World, principal: str) -> frozenset[str]:
    user = world.users[principal]
    permissions.forget(user)
    return permissions.lab_roles(user, world.project)


# --- What there is ------------------------------------------------------------------------------


def test_lists_the_lab_roles_plugins_declare(world: World) -> None:
    listed = api_as(world, "viewer").get("/lab-roles").json()
    for role in listed:  # the rules and guards tests' own type (ADRs 0127, 0128)
        role["uses"] = [u for u in role["uses"] if u["owner"] != "acme/protein.Batch"]
    assert [r["name"] for r in listed] == [ANALYST, QA, REVIEWER]
    qa = listed[1]
    assert (qa["label"], qa["plugin"], qa["description"]) == (
        "QA",
        "acme/protein",
        "Releases what was reviewed.",
    )
    assert qa["uses"][0] == {
        "kind": "approval",
        "owner": RELEASE_THEN_WEIGH,
        "name": "weigh",
        "description": "approve “Weigh” in Release, then weigh",
    }
    # And the states of state fields it sets (ADR 0125), and the electronic signatures
    # setting some of them needs, which it gives (ADR 0126).
    assert [(u["kind"], u["name"], u["description"]) for u in qa["uses"][1:]] == [
        ("state", "status", "set Status to Submitted, Delivered or Declined on assay requests"),
        ("state", "review", "set Review to Released on assay results"),
        ("state", "status", "set Status to Open or Released on lots"),
        ("signature", "status", "sign “QA approval” to set Status to Approved on lots"),
        ("signature", "status", "sign “Responsibility” to set Status to Released on lots"),
        ("state", "status", "set Status to In testing or Released on samples"),
        ("state", "custody", "set Custody to In the lab, Quarantined or Disposed on samples"),
    ]
    assert {u["owner"] for u in qa["uses"][1:]} == {
        "acme/protein.AssayRequest",
        "acme/protein.AssayResult",
        "acme/protein.Lot",
        "acme/protein.Sample",
    }
    # Fields naming people need them too (ADR 0122).
    fields = [u for u in listed[0]["uses"] if u["kind"] == "field"]
    assert fields[1] == {
        "kind": "field",
        "owner": "acme/protein.Sample",
        "name": "analyst",
        "description": "be named Analyst on samples",
    }
    assert [u["description"] for u in listed[0]["uses"]] == [
        "be named Technician on assay requests",
        "set Standing to Open or Withdrawn on assay requests",
        "set Status to Accepted on assay requests",
        "set Review to Draft on assay results",
        "set Status to In review on lots",
        "sign “Authorship” to set Status to In review on lots",
        "be named Analyst on samples",
        "set Status to Received, In testing or In review on samples",
        "set Custody to In the lab on samples",
    ]


# --- Giving them --------------------------------------------------------------------------------


def test_an_admin_gives_a_member_lab_roles(world: World) -> None:
    grant = membership(world, "viewer")
    answer = give(world, grant, QA, REVIEWER)
    assert answer.status_code == 200, answer.content
    out = answer.json()
    assert (out["id"], out["role"]) == (str(grant.pk), "viewer")
    assert [(r["role"], r["label"], r["declared"]) for r in out["lab_roles"]] == [
        (QA, "QA", True),
        (REVIEWER, "Reviewer", True),
    ]
    assert out["lab_roles"][0]["granted_by"]["name"] == "Admin"
    assert held(world, "viewer") == {QA, REVIEWER}
    # The members list shows them beside the role, and the project says what's yours.
    members = api_as(world, "viewer").get(f"/projects/{world.project.pk}/members").json()
    (mine,) = [m for m in members["items"] if m["user"] and m["user"]["name"] == "Viewer"]
    assert [r["role"] for r in mine["lab_roles"]] == [QA, REVIEWER]
    project = api_as(world, "viewer").get(f"/projects/{world.project.pk}").json()
    assert project["my_lab_roles"] == [QA, REVIEWER]
    assert api_as(world, "editor").get(f"/projects/{world.project.pk}").json()["my_lab_roles"] == []

    # Each one given is audited, and the person told once.
    events = AuditEvent.objects.filter(action="project.lab_role_granted").order_by("data__role")
    admin, viewer = world.users["admin"].pk, str(world.users["viewer"].pk)
    assert [(e.actor_id, e.target_id, e.data) for e in events] == [
        (admin, viewer, {"role": QA, "label": "QA"}),
        (admin, viewer, {"role": REVIEWER, "label": "Reviewer"}),
    ]
    assert all(e.project == world.project for e in events)
    (told,) = Notification.objects.filter(kind="project.lab_roles_changed")
    assert told.recipient == world.users["viewer"]
    assert told.title == "Admin made you QA and Reviewer in Kinase Atlas"

    # Setting them again takes away what isn't listed, and keeps what is.
    first = LabRoleGrant.objects.get(membership=grant, role=QA)
    out = give(world, grant, QA, ANALYST).json()
    assert [r["role"] for r in out["lab_roles"]] == [ANALYST, QA]
    assert LabRoleGrant.objects.get(membership=grant, role=QA).pk == first.pk
    (revoked,) = AuditEvent.objects.filter(action="project.lab_role_revoked")
    assert revoked.data == {"role": REVIEWER, "label": "Reviewer"}
    told = Notification.objects.filter(kind="project.lab_roles_changed").latest("created_at")
    assert told.title == "Admin made you Analyst in Kinase Atlas, and you're no longer Reviewer"
    # Nothing changed, nothing recorded.
    lab_role_events = AuditEvent.objects.filter(action__startswith="project.lab_role")
    before = lab_role_events.count()
    give(world, grant, ANALYST, QA)
    assert lab_role_events.count() == before
    give(world, grant)
    assert held(world, "viewer") == frozenset()
    told = Notification.objects.filter(kind="project.lab_roles_changed").latest("created_at")
    assert told.title == "Admin took away your Analyst and QA roles in Kinase Atlas"


def test_a_teams_lab_roles_are_its_members(world: World) -> None:
    give(world, team_membership(world), REVIEWER)
    assert held(world, "via_team") == {REVIEWER}
    # Viewer directly, and a reviewer through the team: both count.
    give(world, membership(world, "mixed"), QA)
    assert held(world, "mixed") == {QA, REVIEWER}
    event = AuditEvent.objects.get(action="project.lab_role_granted", data__role=REVIEWER)
    assert (event.target_id, event.team) == (str(world.other_team.pk), world.other_team)
    # Nobody's told of a team's: as for a team's role.
    assert not Notification.objects.filter(kind="project.lab_roles_changed").exclude(
        recipient=world.users["mixed"]
    )


def test_being_an_admin_holds_no_lab_role(world: World) -> None:
    give(world, membership(world, "viewer"), QA)
    for principal in ("admin", "org_admin", "runner", "outsider", "anonymous", "inactive"):
        user = world.user(principal)
        if user is None:
            from django.contrib.auth.models import AnonymousUser

            assert permissions.lab_roles(AnonymousUser(), world.project) == frozenset()
            continue
        assert held(world, principal) == frozenset(), principal
    # A deactivated person's count for nothing.
    give(world, membership(world, "inactive"), QA)
    assert held(world, "inactive") == frozenset()


def test_lab_roles_go_with_their_membership(world: World) -> None:
    grant = membership(world, "viewer")
    give(world, grant, QA, REVIEWER)
    answer = api_as(world, "admin").delete(f"/projects/{world.project.pk}/members/{grant.pk}")
    assert answer.status_code == 204
    assert not LabRoleGrant.objects.filter(membership_id=grant.pk).exists()
    event = AuditEvent.objects.get(action="project.member_removed")
    assert event.data == {"role": "Viewer", "lab_roles": [QA, REVIEWER]}
    assert held(world, "viewer") == frozenset()
    # A team's: its members hold them no longer, even with a grant of their own.
    give(world, team_membership(world), QA)
    services.remove_project_membership(team_membership(world), by=world.users["admin"])
    assert held(world, "mixed") == frozenset()


def test_only_declared_lab_roles_can_be_given(world: World) -> None:
    grant = membership(world, "viewer")
    given = give(world, grant, QA, "acme/protein.owner", "nope")
    refused = problem(given, 422, "validation_failed")
    assert [e["pointer"] for e in refused["errors"]] == ["#/roles/1", "#/roles/2"]
    assert not LabRoleGrant.objects.filter(membership=grant).exists()
    # One a plugin stopped declaring stays until taken away, and counts for nothing.
    LabRoleGrant.objects.create(membership=grant, role="acme/protein.retired", granted_by=None)
    assert held(world, "viewer") == frozenset()
    out = give(world, grant, QA, "acme/protein.retired").json()
    assert [(r["role"], r["label"], r["declared"]) for r in out["lab_roles"]] == [
        (QA, "QA", True),
        ("acme/protein.retired", "retired", False),
    ]
    assert held(world, "viewer") == {QA}


def test_only_those_who_manage_members_give_lab_roles(world: World) -> None:
    grant = membership(world, "viewer")
    problem(give(world, grant, QA, by="editor"), 403, "forbidden")
    problem(give(world, grant, QA, by="outsider"), 404, "not_found")
    assert give(world, grant, QA, by="org_admin").status_code == 200
    # An Admin giving themselves one is recorded as such.
    give(world, membership(world, "admin"), QA)
    event = AuditEvent.objects.get(action="project.lab_role_granted", actor=world.users["admin"])
    assert event.data == {"role": QA, "label": "QA", "self": True}
    # Another project's membership isn't this one's.
    other = ProjectMembership.objects.get(project=world.archived, user=world.users["viewer"])
    problem(give(world, other, QA), 404, "not_found")
    # Lab roles can change while the project is archived, as members can.
    path = f"/projects/{world.archived.pk}/members/{other.pk}/lab-roles"
    assert api_as(world, "admin").put(path, {"roles": [QA]}).status_code == 200


def test_webhooks_hear_of_lab_roles(world: World) -> None:
    from iki_server.webhooks import services as webhooks

    hook, _ = webhooks.create(
        project=world.project,
        url="https://hooks.example.com/iki",
        description="",
        chosen=["project.lab_role_granted", "project.lab_role_revoked", "project.member_removed"],
        by=world.users["admin"],
    )
    grant = membership(world, "viewer")
    give(world, grant, QA)
    give(world, grant)
    give(world, grant, REVIEWER)
    services.remove_project_membership(grant, by=world.users["admin"])
    sent = [d.payload for d in hook.deliveries.exclude(event="ping").order_by("id")]
    assert [m["type"] for m in sent] == [
        "project.lab_role_granted",
        "project.lab_role_revoked",
        "project.lab_role_granted",
        "project.member_removed",
    ]
    first = sent[0]["data"]
    assert first["lab_role"] == {"name": QA, "label": "QA"}
    assert first["member"]["user"]["email"] == "viewer@example.com"
    assert first["actor"]["name"] == "Admin"
    assert sent[3]["data"]["lab_roles"] == [REVIEWER]


# --- What they're for: approvals ----------------------------------------------------------------


def releasing(world: World) -> dict[str, Any]:
    """A run of release_then_weigh, started by the editor, whose weighing waits for someone
    with the QA lab role."""
    editor = api_as(world, "editor")
    kit, met = protein(editor, world.project, "kit"), protein(editor, world.project, "met")
    body: dict[str, Any] = start(
        editor, world, {"query": kit["id"], "target": met["id"]}, RELEASE_THEN_WEIGH
    ).json()
    complete(worker(world))
    run_tasks()
    return get(world, body)


def decide(world: World, body: dict[str, Any], who: str, verb: str = "approve") -> Any:
    path = f"/workflow-runs/{body['hrid']}/steps/weigh/{verb}"
    return api_as(world, who).post(path, {"note": "Released."})


def test_an_approval_can_need_a_lab_role(world: World) -> None:
    give(world, membership(world, "viewer"), QA)
    give(world, team_membership(world), QA)  # via_team and mixed
    body = releasing(world)
    assert steps(body) == {"align": "succeeded", "weigh": "awaiting_approval"}
    approval = body["steps"][1]["approval"]
    assert (approval["role"], approval["approver"]) == (QA, "someone with the QA role")
    # Only its holders are told, and may decide: being an Admin isn't enough.
    told = Notification.objects.filter(kind="workflow.approval_requested")
    assert sorted(n.recipient.name for n in told) == ["Mixed", "Via Team", "Viewer"]
    assert get(world, body, "viewer")["steps"][1]["can"] == {"decide": True}
    assert get(world, body, "admin")["steps"][1]["can"] == {"decide": False}
    refused = problem(decide(world, body, "admin"), 403, "forbidden")
    assert refused["detail"] == "It needs someone with the QA role in the project."
    approved = decide(world, body, "viewer")
    assert approved.status_code == 200, approved.content
    assert approved.json()["steps"][1]["decision"]["by"]["name"] == "Viewer"


def test_whoever_started_it_cant_approve_it_with_the_role(world: World) -> None:
    give(world, membership(world, "editor"), QA)
    body = releasing(world)
    refused = problem(decide(world, body, "editor"), 409, "refused")
    assert "whoever started a workflow can't approve its steps" in refused["detail"]


def test_losing_the_role_loses_the_say(world: World) -> None:
    grant = membership(world, "viewer")
    give(world, grant, QA)
    body = releasing(world)
    give(world, grant)
    problem(decide(world, body, "viewer"), 403, "forbidden")
    # Nor does a project role do instead.
    ProjectMembership.objects.filter(pk=grant.pk).update(role=ProjectRole.ADMIN)
    problem(decide(world, body, "viewer"), 403, "forbidden")


def test_a_lab_role_is_held_in_its_project_only(world: World) -> None:
    give(world, membership(world, "viewer"), QA)
    give(world, team_membership(world), REVIEWER)
    for user in (world.users["viewer"], world.users["via_team"]):
        permissions.forget(user)
        assert permissions.lab_roles(user, world.archived) == frozenset()
    assert set(permissions.holders_of(world.project, QA)) == {world.users["viewer"]}
    assert set(permissions.holders_of(world.archived, QA)) == set()
    assert set(permissions.holders_of(world.project, REVIEWER)) == {
        world.users["via_team"],
        world.users["mixed"],
    }
