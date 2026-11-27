"""The server pins each reviewed record's procedure when it's created (ADR 0302) and enforces
the procedure's compiled guards (ADR 0301).

The fixture's Purity check says what it needs of a procedure. Core's result review and
the fixture lab's own two-person SOP both fit it. A project's Admin chooses one, or the
deployment's default applies, or the record is refused. A release case's findings follow
the procedure the case was opened under.
"""

from typing import Any

import pytest
from api_client import problem
from django.db import IntegrityError, transaction
from org_world import World
from test_api_entities import api_as, create
from test_runs import world
from test_signatures import begin, ready, signed
from test_states import ANALYST, QA, REVIEWER, roles

from iki_server.audit.models import AuditEvent
from iki_server.entities import registry, states
from iki_server.entities.models import Entity
from iki_server.org.services import Refused
from iki_server.reviews import pins, sync
from iki_server.reviews.models import Choice, Procedure

pytestmark = pytest.mark.django_db

__all__ = ["world"]  # a fixture, from test_runs

PURITY = "acme/protein.PurityCheck"
CASE = "acme/protein.ReleaseCase"
FINDING = "acme/protein.CaseFinding"
CORE = "iki/core.result_review@1"
LAB = "acme/protein.two_person@1"
BINDING = {"analyst": ANALYST, "reviewer": REVIEWER, "qa": QA}


def choose(
    world: World,
    flow: str,
    *,
    type_name: str = PURITY,
    procedure: str = CORE,
    binding: dict[str, str] | None = None,
    project: bool = True,
    reason: str = "",
    who: str | None = None,
) -> Any:
    query = f"?project={world.project.pk}" if project else ""
    who = who or ("admin" if project else "org_admin")
    return api_as(world, who).put(
        f"/review-choices/{type_name}{query}",
        {
            "procedure": procedure,
            "flow": flow,
            "binding": BINDING if binding is None else binding,
            "reason": reason,
        },
    )


def chosen(world: World, flow: str, **kw: Any) -> dict[str, Any]:
    answer = choose(world, flow, **kw)
    assert answer.status_code == 200, answer.content
    body: dict[str, Any] = answer.json()
    return body


def purity(world: World, who: str = "editor", **values: Any) -> Any:
    return create(api_as(world, who), world.project, {"purity": 98.2, **values}, PURITY)


def made(world: World, **values: Any) -> dict[str, Any]:
    answer = purity(world, **values)
    assert answer.status_code == 201, answer.content
    body: dict[str, Any] = answer.json()
    return body


def pin(ref: str) -> tuple[str, str] | None:
    found = pins.pin_of(Entity.objects.get(pk=ref))
    return (found.procedure, found.flow) if found else None


def offered(world: World, who: str, ref: str) -> dict[str, str | None]:
    """The review states ``who`` is offered, each with why they can't move it there: a
    signed state's why from its signatures, which are how it's set."""
    can = api_as(world, who).get(f"/entities/{ref}").json()["can"]
    signing = {s["to"]: s["why_not"] for s in can["sign"] if s["field"] == "review"}
    return {
        s["name"]: signing.get(s["name"], s["why_not"]) if s["signed"] else s["why_not"]
        for s in can["states"]["review"]
    }


#: How an independence guard refuses someone (ADR 0301).
SOMEONE_ELSE = "It must be signed by someone other than"


def test_a_record_isnt_made_until_its_procedure_is_chosen(world: World) -> None:
    body = problem(purity(world), 409, "procedure_not_chosen")
    assert body["detail"] == (
        "Purity check has no review procedure in Kinase Atlas. A project Admin chooses one "
        "in Project settings → Starters."
    )
    assert not Entity.objects.filter(type=PURITY).exists()


def test_the_deployment_default_is_an_org_admins_with_a_reason(world: World) -> None:
    assert choose(world, "research", project=False, who="admin").status_code == 403
    refused = problem(choose(world, "research", project=False), 409, "refused")
    assert refused["errors"] == {"reason": [refused["detail"]]} or "Say why" in refused["detail"]
    body = chosen(world, "research", project=False, reason="SOP-QA-001 applies site-wide.")
    assert body["default"]["flow"] == "research"
    event = AuditEvent.objects.get(action="review.default_set")
    assert event.data["reason"] == "SOP-QA-001 applies site-wide."
    assert event.data["binding"] == BINDING
    # A project that hasn't chosen follows it.
    assert pin(made(world)["id"]) == (CORE, "research")


def test_each_record_keeps_the_procedure_it_was_made_under(world: World) -> None:
    chosen(world, "research", project=False, reason="Site default.")
    project = chosen(world, "regulated_qc")
    assert project["effective"]["flow"] == "regulated_qc"
    first = made(world)
    assert pin(first["id"]) == (CORE, "regulated_qc")
    assert first["values"]["review"] == "recorded"
    assert first["values"]["review_flow"] == "regulated_qc"
    chosen(world, "research")
    second = made(world)
    assert pin(second["id"]) == (CORE, "research")
    assert pin(first["id"]) == (CORE, "regulated_qc")
    assert list(offered(world, "editor", first["id"])) == ["submitted", "reviewed", "released"]
    assert list(offered(world, "editor", second["id"])) == ["signed_off"]
    # Clearing the project's choice leaves the deployment's for new records.
    api_as(world, "admin").post(f"/review-choices/{PURITY}/clear?project={world.project.pk}", {})
    assert pin(made(world)["id"]) == (CORE, "research")
    assert Choice.objects.filter(project=world.project).count() == 2


def test_no_one_gives_or_changes_a_pin(world: World) -> None:
    chosen(world, "research")
    body = problem(purity(world, review_flow="record_only"), 422, "validation_failed")
    assert {e["pointer"]: e["detail"] for e in body["errors"]}["#/values/review_flow"].startswith(
        "is set by Iki when the record is created"
    )
    ref = made(world)["id"]
    entity = Entity.objects.get(pk=ref)
    api = api_as(world, "editor")
    etag = api.get(f"/entities/{ref}")["ETag"]
    answer = api.patch(
        f"/entities/{ref}", {"values": {"review_flow": "record_only"}}, headers={"If-Match": etag}
    )
    assert answer.status_code in (409, 422), answer.content
    assert pin(ref) == (CORE, "research")
    # The database refuses a state the record's flow doesn't have.
    model = registry.model(PURITY)
    with pytest.raises(IntegrityError), transaction.atomic():
        model._default_manager.filter(pk=entity.pk).update(review="released")


def test_a_procedure_that_doesnt_fit_the_type_is_refused(world: World) -> None:
    body = problem(choose(world, "record_only"), 409, "refused")
    assert body["detail"] == (
        "iki/core.result_review@1 can't review Purity check: its flow `record_only` ends in "
        "`recorded`, and Purity check's readers take only `signed_off` or `released` as "
        "complete."
    )
    fitting = api_as(world, "viewer").get(f"/review-choices/{PURITY}").json()["fitting"]
    assert fitting == {CORE: ["research", "regulated_qc"], LAB: ["two_person"]}


def test_regulated_qc_takes_three_different_people(world: World) -> None:
    ready(world)
    chosen(world, "regulated_qc")
    roles(
        world,
        editor=(ANALYST, REVIEWER),
        admin=(REVIEWER, QA),
        viewer=(QA,),
    )
    ref = made(world)["id"]
    signed(world, "editor", ref, "submitted", field="review")
    # Whoever made and submitted it doesn't review it, whichever roles they hold.
    why = offered(world, "editor", ref)["reviewed"]
    assert why is not None
    assert why.startswith(f"{SOMEONE_ELSE} whoever created the record")
    with pytest.raises(Refused):
        begin(world, "editor", ref, "reviewed", field="review")
    signed(world, "admin", ref, "reviewed", field="review")
    # The reviewer doesn't release it either.
    assert str(offered(world, "admin", ref)["released"]).startswith(SOMEONE_ELSE)
    signed(world, "viewer", ref, "released", field="review")
    entity = Entity.objects.get(pk=ref)
    assert states.values_of(entity)["review"] == "released"
    assert entity.locked_by == ["review"]


def test_the_types_own_people_and_guards_join_the_procedure(world: World) -> None:
    ready(world)
    chosen(world, "research")
    roles(world, editor=(ANALYST,), admin=(REVIEWER,))
    admin = str(world.users["admin"].pk)
    ref = made(world, analysts=[admin])["id"]
    # The admin ran the column, so they can't sign it off.
    assert str(offered(world, "admin", ref)["signed_off"]).startswith(SOMEONE_ELSE)
    other = made(world, purity=None)["id"]
    assert offered(world, "admin", other)["signed_off"] == "Record the purity before signing."


def test_a_lab_applies_its_own_sop_to_a_type_it_didnt_write(world: World) -> None:
    ready(world)
    body = chosen(world, "two_person", procedure=LAB, binding={"analyst": ANALYST, "checker": QA})
    assert body["effective"]["procedure"] == LAB
    roles(world, editor=(ANALYST,), viewer=(QA,), admin=(QA,))
    ref = made(world)["id"]
    assert list(offered(world, "editor", ref)) == ["submitted", "released"]
    signed(world, "editor", ref, "submitted", field="review")
    signed(world, "viewer", ref, "released", field="review")
    assert states.values_of(Entity.objects.get(pk=ref))["review"] == "released"


def test_a_cases_findings_follow_the_procedure_it_was_opened_under(world: World) -> None:
    editor = api_as(world, "editor")

    def case(code: str) -> Any:
        return create(editor, world.project, {"lot_code": code}, CASE)

    assert problem(case("L-1"), 409, "procedure_not_chosen")
    chosen(world, "regulated_qc", type_name=CASE)
    opened = case("L-1").json()
    assert pin(opened["id"]) == (CORE, "regulated_qc")
    chosen(world, "research", type_name=CASE)
    finding = create(editor, world.project, {"case": opened["id"], "finding": "Clear."}, FINDING)
    assert finding.status_code == 201, finding.content
    assert pin(finding.json()["id"]) == (CORE, "regulated_qc")
    # A follower's procedure isn't chosen: it's its case's.
    assert problem(choose(world, "research", type_name=FINDING), 409, "refused")


def test_an_installed_procedure_version_never_changes(world: World) -> None:
    row = Procedure.objects.get(qualified=LAB)
    Procedure.objects.filter(pk=row.pk).update(digest="0" * 64)
    with pytest.raises(sync.Changed, match="declare the change as version 2"):
        sync.install_procedures()
