"""A plugin's own starter (ADR 0303): listed with what it reads from its procedure, previewed
and applied by a project's Admin as the project's choice for its type (ADR 0302)."""

from typing import Any

import pytest
from api_client import problem
from org_world import World
from test_api_entities import api_as, create
from test_runs import world
from test_states import ANALYST, QA, REVIEWER

from iki_server.entities.models import Entity
from iki_server.org.models import ProjectMembership
from iki_server.reviews import pins
from iki_server.reviews.models import Choice

pytestmark = pytest.mark.django_db

__all__ = ["world"]  # a fixture, from test_runs

STARTER = "acme/protein.purity_review"
PURITY = "acme/protein.PurityCheck"


def listed(world: World) -> dict[str, Any]:
    found = api_as(world, "admin").get(f"/starters?project={world.project.pk}").json()
    return next(s for s in found if s["id"] == STARTER)


def membership(world: World, who: str) -> str:
    return str(ProjectMembership.objects.get(project=world.project, user=world.users[who]).pk)


def setup(world: World, flow: str, **people: tuple[str, ...]) -> dict[str, Any]:
    return {
        "version": "1.0.0",
        "flow": flow,
        "project": str(world.project.pk),
        "role_assignments": {
            role: [membership(world, w) for w in who] for role, who in people.items()
        },
    }


def applied(world: World, flow: str, **people: tuple[str, ...]) -> dict[str, Any]:
    api = api_as(world, "admin")
    body = setup(world, flow, **people)
    plan = api.post(f"/starters/{STARTER}/preview", body)
    assert plan.status_code == 200, plan.content
    done = api.post(f"/starters/{STARTER}/apply", {**body, "plan": plan.json()["plan"]})
    assert done.status_code == 200, done.content
    result: dict[str, Any] = done.json()
    return result


def test_a_plugins_starter_reads_its_flows_from_the_procedure(world: World) -> None:
    starter = listed(world)
    assert starter["procedure"] == "iki/core.result_review@1"
    assert starter["suggested"] == "research"
    research, regulated = starter["flows"]
    assert research["states"] == ["recorded", "signed_off"]
    assert research["required_roles"] == [REVIEWER]
    assert research["independent_roles"] == []
    assert regulated["states"] == ["recorded", "submitted", "reviewed", "released"]
    assert regulated["required_roles"] == sorted([ANALYST, REVIEWER, QA])
    assert regulated["independent_roles"] == sorted([ANALYST, REVIEWER, QA])
    assert starter["applied"] is None


def test_applying_it_is_the_projects_choice_for_its_type(world: World) -> None:
    result = applied(world, "research", **{REVIEWER: ("admin",)})
    assert result["grants_to_add"] == {REVIEWER: [membership(world, "admin")]}
    choice = Choice.objects.get(project=world.project, superseded_at__isnull=True)
    assert (choice.entity_type, choice.flow) == (PURITY, "research")
    assert choice.starter == f"{STARTER}@1.0.0"
    assert listed(world)["applied"] == {"version": "1.0.0", "flow": "research"}
    made = create(api_as(world, "editor"), world.project, {"purity": 97.0}, PURITY)
    assert made.status_code == 201, made.content
    pin = pins.pin_of(Entity.objects.get(pk=made.json()["id"]))
    assert pin == pins.Pin("iki/core.result_review@1", "research")


def test_regulated_qc_needs_three_people_who_can_each_be_someone_else(world: World) -> None:
    api = api_as(world, "admin")
    same = setup(
        world, "regulated_qc", **{ANALYST: ("editor",), REVIEWER: ("editor",), QA: ("admin",)}
    )
    body = problem(api.post(f"/starters/{STARTER}/preview", same), 409, "refused")
    assert "each sign independently of the others" in body["detail"]
    enough = setup(
        world,
        "regulated_qc",
        **{ANALYST: ("editor", "admin"), REVIEWER: ("editor", "admin"), QA: ("viewer",)},
    )
    assert api.post(f"/starters/{STARTER}/preview", enough).status_code == 200


def test_a_part_is_played_by_the_role_the_projects_admin_binds_it_to(world: World) -> None:
    """ADR 0304: the starter binds a slot to a role, and the Admin may bind another."""
    other = QA  # the fixture's own QA role, as a lab would bind its second reviewer
    body = {**setup(world, "research", **{other: ("viewer",)}), "slots": {"reviewer": other}}
    api = api_as(world, "admin")
    plan = api.post(f"/starters/{STARTER}/preview", body)
    assert plan.status_code == 200, plan.content
    assert plan.json()["slots"]["reviewer"] == other
    done = api.post(f"/starters/{STARTER}/apply", {**body, "plan": plan.json()["plan"]})
    assert done.status_code == 200, done.content
    choice = Choice.objects.get(project=world.project, superseded_at__isnull=True)
    assert choice.binding["reviewer"] == other
    # The listing starts the next setup from it.
    slots = {s["id"]: s for s in listed(world)["slots"]}
    assert (slots["reviewer"]["role"], slots["reviewer"]["starter_role"]) == (other, REVIEWER)
    wrong = {**body, "slots": {"witness": other}}
    problem(api.post(f"/starters/{STARTER}/preview", wrong), 409, "refused")
    unknown = {**body, "slots": {"reviewer": "acme/protein.nobody"}}
    problem(api.post(f"/starters/{STARTER}/preview", unknown), 409, "refused")


def test_a_recipe_has_no_slots_to_bind(world: World) -> None:
    body = {**setup(world, "research"), "slots": {"reviewer": REVIEWER}}
    problem(
        api_as(world, "admin").post("/starters/lot-release-testing/preview", body),
        422,
        "validation_failed",
    )


def test_an_org_admin_sets_the_deployments_default_with_a_reason(world: World) -> None:
    """ADR 0302: a project that hasn't chosen follows it; one that has keeps its own."""
    url = f"/starters/{STARTER}/default"
    body = {"version": "1.0.0", "flow": "regulated_qc", "reason": "SOP-QA-014 v3"}
    problem(api_as(world, "admin").post(url, body), 403, "forbidden")
    blank = api_as(world, "org_admin").post(url, {**body, "reason": ""})
    assert blank.status_code == 422, blank.content
    made = api_as(world, "org_admin").post(url, body)
    assert made.status_code == 200, made.content
    assert made.json()["default"]["flow"] == "regulated_qc"
    assert made.json()["default"]["reason"] == "SOP-QA-014 v3"
    assert listed(world)["default"]["flow"] == "regulated_qc"
    default = Choice.objects.get(project=None, superseded_at__isnull=True)
    assert (default.entity_type, default.starter) == (PURITY, f"{STARTER}@1.0.0")
    created = create(api_as(world, "editor"), world.project, {"purity": 97.0}, PURITY)
    assert created.status_code == 201, created.content
    pin = pins.pin_of(Entity.objects.get(pk=created.json()["id"]))
    assert pin == pins.Pin("iki/core.result_review@1", "regulated_qc")

    # The project's own choice wins; clearing the default leaves it.
    applied(world, "research", **{REVIEWER: ("admin",)})
    cleared = api_as(world, "org_admin").post(f"{url}/clear", {"reason": "SOP retired"})
    assert cleared.status_code == 200, cleared.content
    assert cleared.json()["default"] is None
    again = create(api_as(world, "editor"), world.project, {"purity": 96.0}, PURITY)
    pin = pins.pin_of(Entity.objects.get(pk=again.json()["id"]))
    assert pin == pins.Pin("iki/core.result_review@1", "research")
