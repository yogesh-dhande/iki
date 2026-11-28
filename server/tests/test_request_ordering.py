"""A service orders its own work from primitives (ADR 0232): a subtype of a request type adds
the service's own fields, a request's arrival fires the service's triggers as a service
account, a run reads the request's two sides and its project's own records of a type, and the
queue sorts by several fields, by either side's project, and by an enum's declared order."""

from typing import Any

import pytest
from api_client import Api, problem
from django.db import DatabaseError
from org_world import World
from test_api_entities import create, patch
from test_requests import (
    ANALYST,
    Lab,
    _member,
    build_lab,
    change,
    raw,
    sent,
    state,
    submit,
    takes,
)
from test_workers import keep_connection, worker, world

from iki_server.accounts import service_accounts
from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.entities import requests
from iki_server.entities.models import ProjectLink, Revision
from iki_server.org import permissions
from iki_server.org.models import Project, ProjectRole
from iki_server.runs import catalog
from iki_server.runs import services as runs
from iki_server.runs.models import ActionRun, FiringState, ProjectTrigger, TriggerFiring
from iki_server.tasks.runner import Runner

pytestmark = pytest.mark.django_db

__all__ = ["keep_connection", "world"]  # fixtures, from test_workers

RANKED = "acme/protein.RankedAssayRequest"
CLIENT_RANK = "acme/protein.ClientRank"
RANK = "acme/protein.rank_requests"
ON_ARRIVAL = "acme/protein.rank_received_requests"


@pytest.fixture
def lab(world: World) -> Lab:
    return build_lab(world)


def run_tasks() -> int:
    return Runner(queues=["default"]).run_ready()


def antibodies(lab: Lab) -> Project:
    """A second project asking the core, whose Editor asks as its Analyst."""
    project = Project.objects.create(team=lab.world.team, name="Antibody Discovery")
    _member(project, lab.users["editor"], ProjectRole.EDITOR, ANALYST)
    return project


def ranked(lab: Lab, project: Project | None = None, **values: Any) -> dict[str, Any]:
    made = create(
        lab.api("editor"),
        project or lab.world.project,
        {"title": "Purity of the kinases", **values},
        RANKED,
    )
    assert made.status_code == 201, made.content
    body: dict[str, Any] = made.json()
    return body


def sent_ranked(lab: Lab, project: Project | None = None, **values: Any) -> dict[str, Any]:
    made = ranked(lab, project, **values)
    answer = submit(lab, made["id"])
    assert answer.status_code == 200, answer.content
    return made


def client_rank(lab: Lab, project: Project, rank: int, owner: Project | None = None) -> str:
    """A rank the core keeps, or with ``owner``, one another project keeps, its Editor's."""
    made = create(
        lab.api("editor" if owner else "tech"),
        owner or lab.core,
        {"project_slug": project.slug, "rank": rank},
        CLIENT_RANK,
    )
    assert made.status_code == 201, made.content
    return str(made.json()["id"])


def ranker(lab: Lab) -> User:
    """A service account in the core, which its arrival trigger runs as."""
    account = service_accounts.create("Queue ranker", "", by=lab.users["org_admin"])
    _member(lab.core, account, ProjectRole.EDITOR)
    return account


def launch(lab: Lab, who: str, inputs: dict[str, Any]) -> Any:
    catalog.compute.cache_clear()
    return lab.api(who).post(
        f"/actions/{RANK}/runs", {"project": str(lab.core.pk), "inputs": inputs}
    )


# --- A subtype's service fields ---------------------------------------------------------------


def test_a_subtype_adds_service_fields_of_its_own(lab: Lab) -> None:
    takes(lab)
    assert requests.service_fields(RANKED) >= {"client_rank", "status", "core_notes"}
    found = lab.api("viewer").get(f"/entity-types/{RANKED}").json()["request"]
    assert found["service_fields"][-1] == "client_rank"
    ref = sent_ranked(lab)["id"]
    refused = problem(change(lab, "editor", ref, {"client_rank": 1}), 422, "validation_failed")
    assert refused["errors"] == [
        {
            "pointer": "#/values/client_rank",
            "detail": "is Protein Core's: the project serving the request changes it.",
        }
    ]
    refused = problem(change(lab, "tech", ref, {"turnaround": "express"}), 422, "validation_failed")
    assert refused["errors"][0]["pointer"] == "#/values/turnaround"
    assert change(lab, "tech", ref, {"client_rank": 3}).status_code == 200
    assert change(lab, "editor", ref, {"turnaround": "express"}).status_code == 200
    # The database holds each side to its columns in the subtype's table too.
    guarded = next(g for g in requests.guarded_tables() if "client_rank" in g.service_columns)
    update = f'UPDATE "{guarded.table}" SET client_rank = %s WHERE entity_id = %s'  # noqa: S608
    with pytest.raises(DatabaseError, match="the service writes its fields"):
        raw(update, [1, ref])
    raw(update, [2, ref], requests.SERVICE)


# --- Arrival --------------------------------------------------------------------------------


def test_a_request_arriving_is_ranked_as_the_services_account_on_the_admins_authority(
    lab: Lab,
) -> None:
    takes(lab)
    other = antibodies(lab)
    atlas_rank = client_rank(lab, lab.world.project, 2)
    client_rank(lab, other, 1)
    admin = lab.api("core_admin")
    url = f"/projects/{lab.core.pk}/triggers/{ON_ARRIVAL}/enable"
    listed = {t["name"]: t for t in admin.every(f"/projects/{lab.core.pk}/triggers")}
    assert listed[ON_ARRIVAL]["runs_as_account"] is True
    assert (
        "A trigger on a request's arrival runs as a service account"
        in (listed[ON_ARRIVAL]["why_not_enable"])
    )
    account = ranker(lab)
    problem(admin.post(url, {}), 409, "refused")
    enabled = admin.post(url, {"run_as": str(account.pk)})
    assert enabled.status_code == 200, enabled.content
    assert enabled.json()["run_as"]["name"] == "Queue ranker"
    event = AuditEvent.objects.get(action="trigger.enabled")
    assert event.data["run_as"] == "Queue ranker" and "schedule" not in event.data

    # Made, it's the asking project's alone: nothing fires until it's sent.
    made = ranked(lab)
    assert not TriggerFiring.objects.exists()
    assert submit(lab, made["id"]).status_code == 200
    firing = TriggerFiring.objects.get()
    assert (firing.event, str(firing.entity_id), firing.trigger.project) == (
        "request.received",
        made["id"],
        lab.core,
    )
    run_tasks()
    firing.refresh_from_db()
    assert firing.state == FiringState.LAUNCHED, firing.error
    run = firing.run
    assert run is not None
    assert (run.requested_by, run.via, run.project) == (account, "trigger", lab.core)
    assert run.reason == (
        f"Trigger “Rank requests as they arrive”: {made['hrid']} arrived from Kinase Atlas."
    )
    shown = lab.api("core_viewer").get(f"/runs/{run.hrid}").json()
    assert shown["trigger"]["event"] == "request.received"
    assert shown["trigger"]["enabled_by"]["name"] == lab.users["core_admin"].name

    # The run reads both sides of the request, and the core's whole ranking.
    pod = worker(lab.world)
    job = pod.lease().json()
    (request,) = job["inputs"]["requests"]
    assert request["request"] == {
        "asking_project": {
            "id": str(lab.world.project.pk),
            "slug": lab.world.project.slug,
            "name": "Kinase Atlas",
        },
        "service": {"id": str(lab.core.pk), "slug": lab.core.slug, "name": "Protein Core"},
    }
    ranks = {r["id"]: r["values"] for r in job["inputs"]["ranks"]}
    assert ranks[atlas_rank] == {"project_slug": lab.world.project.slug, "rank": 2}
    assert sorted(v["project_slug"] for v in ranks.values()) == sorted(
        [lab.world.project.slug, other.slug]
    )
    output = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "updates": {"requests": [{"entity": request["id"], "values": {"client_rank": 2}}]},
    }
    answer = pod.report(job, "complete", output=output)
    assert answer.status_code == 200, answer.content
    # The asking project sees the core's rank, written as the core's account.
    values = lab.api("editor").get(f"/entities/{made['id']}").json()["values"]
    assert values["client_rank"] == 2
    wrote = Revision.objects.filter(entity_id=made["id"]).order_by("number").last()
    assert wrote is not None
    assert (wrote.user, wrote.changed_fields) == (account, ["client_rank"])
    # What the triggered run wrote fires nothing; sending another fires once more.
    assert TriggerFiring.objects.count() == 1
    sent_ranked(lab, other)
    assert TriggerFiring.objects.count() == 2


def test_arrival_fires_only_the_services_own_triggers(lab: Lab) -> None:
    takes(lab)
    client_rank(lab, lab.world.project, 1)
    # Turned on in the asking project, it never fires there: nothing arrives there.
    asker_account = service_accounts.create("Atlas ranker", "", by=lab.users["org_admin"])
    _member(lab.world.project, asker_account, ProjectRole.EDITOR)
    answer = lab.api("admin").post(
        f"/projects/{lab.world.project.pk}/triggers/{ON_ARRIVAL}/enable",
        {"run_as": str(asker_account.pk)},
    )
    assert answer.status_code == 200, answer.content
    sent_ranked(lab)
    assert not TriggerFiring.objects.exists()
    # Nor does a plain assay request fire one for ranked ones.
    account = ranker(lab)
    lab.api("core_admin").post(
        f"/projects/{lab.core.pk}/triggers/{ON_ARRIVAL}/enable", {"run_as": str(account.pk)}
    )
    sent(lab)
    assert not TriggerFiring.objects.exists()


def test_an_admin_who_can_no_longer_run_it_turns_it_off(lab: Lab) -> None:
    takes(lab)
    client_rank(lab, lab.world.project, 1)
    account = ranker(lab)
    lab.api("core_admin").post(
        f"/projects/{lab.core.pk}/triggers/{ON_ARRIVAL}/enable", {"run_as": str(account.pk)}
    )
    membership = lab.core.memberships.get(user=lab.users["core_admin"])
    membership.role = ProjectRole.VIEWER
    membership.save()
    permissions.forget(lab.users["core_admin"])
    sent_ranked(lab)
    run_tasks()
    firing = TriggerFiring.objects.get()
    assert firing.state == FiringState.FAILED and firing.run is None
    assert not ProjectTrigger.objects.get(project=lab.core, trigger=ON_ARRIVAL).enabled


# --- An input from the project ------------------------------------------------------------------


def test_an_input_from_the_project_is_every_record_it_owns_and_never_chosen(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    takes(lab)
    ref = sent_ranked(lab)["id"]
    refused = problem(launch(lab, "tech", {"requests": [ref]}), 422, "validation_failed")
    assert refused["errors"] == [
        {"pointer": "#/inputs/ranks", "detail": "Protein Core has no client ranks of its own yet."}
    ]
    kept = client_rank(lab, lab.world.project, 1)
    archived = client_rank(lab, antibodies(lab), 2)
    tech = lab.api("tech")
    gone = patch(tech, archived, {"archived": True}, tech.get(f"/entities/{archived}")["ETag"])
    assert gone.status_code == 200, gone.content
    # Another project's, even shared into the core, isn't the core's own.
    theirs = client_rank(lab, lab.world.project, 9, owner=lab.world.project)
    ProjectLink.objects.create(entity_id=theirs, project=lab.core)
    chosen = problem(
        launch(lab, "tech", {"requests": [ref], "ranks": [theirs]}), 422, "validation_failed"
    )
    assert chosen["errors"] == [
        {
            "pointer": "#/inputs/ranks",
            "detail": "It's given every client rank Protein Core owns, so it isn't chosen: "
            "leave it out.",
        }
    ]
    launched = launch(lab, "tech", {"requests": [ref]})
    assert launched.status_code == 201, launched.content
    run = ActionRun.objects.get(hrid=launched.json()["hrid"])
    given = {str(e) for e in run.inputs.filter(name="ranks").values_list("entity_id", flat=True)}
    assert given == {kept}
    # Past the most it takes, it doesn't start.
    client_rank(lab, lab.world.project, 3)
    monkeypatch.setattr(runs, "FROM_PROJECT_MAX", 1)
    refused = problem(launch(lab, "tech", {"requests": [ref]}), 422, "validation_failed")
    assert "more than 1 client ranks" in refused["errors"][0]["detail"]


# --- The queue's order ---------------------------------------------------------------------------


def queue(api: Api, core: Project, sort: dict[str, Any], *then: dict[str, Any], **kw: Any) -> Any:
    return api.post(
        "/entities/search",
        {
            "type": RANKED,
            "where": {"field": "sent_to", "op": "eq", "value": str(core.pk)},
            "sort": sort,
            "then": list(then),
            **kw,
        },
    )


def titles(api: Api, core: Project, sort: dict[str, Any], *then: dict[str, Any]) -> list[str]:
    """Every request in the queue, two to a page, so each page's cursor is used."""
    out: list[str] = []
    cursor = None
    while True:
        page = queue(api, core, sort, *then, limit=2, **({"cursor": cursor} if cursor else {}))
        assert page.status_code == 200, page.content
        body = page.json()
        out += [item["values"]["title"] for item in body["items"]]
        cursor = body.get("next_cursor")
        if not cursor:
            return out


def test_the_queue_sorts_by_rank_then_turnaround_then_needed_by(lab: Lab) -> None:
    takes(lab)
    other = antibodies(lab)
    asked = [
        ("A", lab.world.project, 2, "standard", "2026-12-03"),
        ("B", other, 1, "standard", "2026-12-09"),
        ("C", lab.world.project, 2, "same_day", "2026-12-20"),
        ("D", other, 1, "express", "2026-12-01"),
        ("E", lab.world.project, None, "same_day", "2026-12-02"),
        ("F", other, 1, "express", "2026-11-30"),
    ]
    for title, project, rank, turnaround, needed_by in asked:
        ref = sent_ranked(lab, project, title=title, turnaround=turnaround, needed_by=needed_by)
        if rank is not None:
            assert change(lab, "tech", ref["id"], {"client_rank": rank}).status_code == 200
    tech = lab.api("tech")
    order = titles(
        tech,
        lab.core,
        {"field": "client_rank"},
        {"field": "turnaround", "descending": True},
        {"field": "needed_by"},
    )
    # Rank 1, express (2026-11-30, then 12-01) before standard; rank 2, same day first;
    # unranked last.
    assert order == ["F", "D", "B", "C", "A", "E"]
    # An enum sorts in its declared order (standard, express, same day), not alphabetically.
    assert titles(tech, lab.core, {"field": "turnaround"}, {"field": "needed_by"}) == [
        "A",
        "B",
        "F",
        "D",
        "E",
        "C",
    ]
    # By the project that asked, by its name: Antibody Discovery before Kinase Atlas.
    by_project = titles(tech, lab.core, {"field": "asked_by"}, {"field": "needed_by"})
    assert by_project == ["F", "D", "B", "E", "A", "C"]


def test_a_state_sorts_in_its_declared_order(lab: Lab) -> None:
    takes(lab)
    for title, to in (("Declined", "declined"), ("Accepted", "accepted"), ("New", None)):
        ref = sent_ranked(lab, title=title)["id"]
        if to is not None:
            who, why = ("core_qa", "Out of scope") if to == "declined" else ("tech", "")
            assert state(lab, who, ref, "status", to, why).status_code == 200
    # submitted, accepted, delivered, declined.
    order = titles(lab.api("tech"), lab.core, {"field": "status"}, {"field": "title"})
    assert order == ["New", "Accepted", "Declined"]


def test_further_sorts_need_a_first_and_differ(lab: Lab) -> None:
    takes(lab)
    tech = lab.api("tech")
    body = {"type": RANKED, "then": [{"field": "needed_by"}]}
    problem(tech.post("/entities/search", body), 422, "validation_failed")
    twice = queue(tech, lab.core, {"field": "needed_by"}, {"field": "needed_by"})
    problem(twice, 422, "validation_failed")
    many = queue(tech, lab.core, {"field": "title"}, *[{"field": f} for f in "abcd"])
    problem(many, 422, "validation_failed")
    csv = tech.get(
        "/entities/search.csv",
        type=RANKED,
        sort="client_rank",
        then=["-turnaround", "needed_by"],
    )
    assert csv.status_code == 200, csv.content


def test_a_saved_search_keeps_its_further_sorts(lab: Lab) -> None:
    query = {
        "type": RANKED,
        "sort": {"field": "client_rank"},
        "then": [{"field": "turnaround", "descending": True}, {"field": "needed_by"}],
    }
    made = lab.api("tech").post(
        "/saved-searches", {"name": "Queue", "project": str(lab.core.pk), "query": query}
    )
    assert made.status_code == 201, made.content
    assert made.json()["query"]["then"] == [
        {"field": "turnaround", "descending": True},
        {"field": "needed_by", "descending": False},
    ]
