"""A reviewed type's flow is drawn from its procedures (ADR 0306): `GET …/flow` lists each
flow its records can follow, compiled as the server enforces it, and marks the project's
choice, the deployment's default, the plugin's suggestion and a record's own pin."""

from typing import Any

import pytest
from org_world import World
from test_api_entities import api_as
from test_review_pins import CASE, CORE, FINDING, LAB, PURITY, chosen, made
from test_runs import world
from test_states import ANALYST, QA, REVIEWER

pytestmark = pytest.mark.django_db

__all__ = ["world"]  # a fixture, from test_runs

STARTER = "acme/protein.purity_review"


def drawn(world: World, type_name: str = PURITY, **query: Any) -> dict[str, Any]:
    answer = api_as(world, "viewer").get(f"/entity-types/{type_name}/flow", **query)
    assert answer.status_code == 200, answer.content
    body: dict[str, Any] = answer.json()
    return body


def flows(body: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(f["procedure"], f["flow"]): f for f in body["review"]["flows"]}


def test_a_reviewed_types_flow_is_each_flow_its_starters_offer(world: World) -> None:
    body = drawn(world)
    # The review field isn't drawn from the type: its states are each flow's.
    assert "review" not in [f["name"] for f in body["fields"]]
    assert body["review"]["field"] == "review"
    found = flows(body)
    assert list(found) == [(CORE, "research"), (CORE, "regulated_qc")]
    research = found[(CORE, "research")]
    assert research["starter"] == STARTER
    assert research["suggested"] is True
    assert not research["chosen"] and not research["default"] and not research["pinned"]
    assert research["procedure_label"]
    # Drawn as the server enforces it: who sets each state, and who must be someone else.
    qc = found[(CORE, "regulated_qc")]["field"]
    assert [s["name"] for s in qc["states"]] == ["recorded", "submitted", "reviewed", "released"]
    reviewed = next(s for s in qc["states"] if s["name"] == "reviewed")
    assert reviewed["set_by"] == [REVIEWER]
    assert {g["name"] for g in reviewed["guards"]} >= {"route", "independent", "measured"}
    assert {"from": "submitted", "to": "reviewed", "conditional": True} in qc["transitions"]


def test_it_marks_the_projects_choice_and_the_deployments_default(world: World) -> None:
    chosen(world, "research", project=False, reason="SOP-QA-001 applies site-wide.")
    chosen(world, "two_person", procedure=LAB, binding={"analyst": ANALYST, "checker": QA})
    # Without a project, only the deployment's default.
    found = flows(drawn(world))
    assert found[(CORE, "research")]["default"] is True
    assert (LAB, "two_person") not in found
    found = flows(drawn(world, project=str(world.project.pk)))
    lab = found[(LAB, "two_person")]
    assert lab["chosen"] is True and lab["starter"] is None
    # Drawn with the roles the project's choice binds its slots to.
    released = next(s for s in lab["field"]["states"] if s["name"] == "released")
    assert released["set_by"] == [QA]
    assert found[(CORE, "research")]["default"] is True


def test_a_records_pin_is_drawn_after_its_project_chooses_another(world: World) -> None:
    chosen(world, "regulated_qc")
    record = made(world)
    chosen(world, "research")
    found = flows(
        drawn(
            world,
            project=str(world.project.pk),
            procedure=record["values"]["review_procedure"],
            flow=record["values"]["review_flow"],
        )
    )
    assert found[(CORE, "regulated_qc")]["pinned"] is True
    assert found[(CORE, "research")]["chosen"] is True
    answer = api_as(world, "viewer").get(
        f"/entity-types/{PURITY}/flow", procedure=CORE, flow="no_such_flow"
    )
    assert answer.status_code == 404


def test_a_follower_is_drawn_as_the_record_it_follows(world: World) -> None:
    body = drawn(world, FINDING)
    assert body["review"]["follows"] == "case"
    case = drawn(world, CASE)
    assert case["review"]["field"] is None
    assert all(f["field"] is None for f in case["review"]["flows"])


def test_a_type_without_review_has_none(world: World) -> None:
    from iki_server.entities import registry

    plain = next(
        name
        for name, t in registry.types().items()
        if t.review is None and registry.effective(name).review is None and not t.abstract
    )
    assert drawn(world, plain)["review"] is None
