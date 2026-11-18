"""Guards on setting a record's state (ADR 0128): allowed moves, four eyes, a person rather
than an agent, and conditions across state fields, each refusing with its message."""

from typing import Any

import pytest
from api_client import problem
from org_world import World
from test_api_entities import api_as
from test_rules import BATCH, change, made
from test_runs import world
from test_signatures import begin, evidence, ready, signed
from test_states import ANALYST, QA, REVIEWER, put, roles, set_

from iki_server.audit import log as audit
from iki_server.audit.models import Via
from iki_server.entities import guards, states
from iki_server.entities.models import Entity
from iki_server.org.services import Refused
from iki_server.signatures import services as signing
from iki_server.signatures.models import Status

pytestmark = pytest.mark.django_db

__all__ = ["world"]  # a fixture, from test_runs


def offered(world: World, who: str, ref: str, field: str = "status") -> dict[str, Any]:
    body = api_as(world, who).get(f"/entities/{ref}").json()
    return {s["name"]: (s["why_not"], s["guard"]) for s in body["can"]["states"][field]}


def test_a_type_says_its_states_guards(world: World) -> None:
    found = api_as(world, "viewer").get(f"/entity-types/{BATCH}").json()
    status = next(f for f in found["fields"] if f["name"] == "status")
    approved = next(s for s in status["states"] if s["name"] == "approved")
    assert [g["name"] for g in approved["guards"]] == ["reviewed", "four_eyes", "a_person"]
    assert approved["guards"][0]["check"] == 'state.from == "in_review"'


def test_a_guard_says_which_state_a_state_follows(world: World) -> None:
    roles(world, editor=(ANALYST,), admin=(REVIEWER,))
    batch = made(world, {"result": "fail"})
    assert offered(world, "admin", batch["id"])["approved"] == (
        "A batch is approved from In review.",
        "reviewed",
    )
    body = problem(put(world, "admin", batch["id"], "status", "approved"), 409, "guard_refused")
    assert (body["detail"], body["guard"]) == ("A batch is approved from In review.", "reviewed")
    set_(world, "editor", batch["id"], "status", "in_review")
    assert offered(world, "admin", batch["id"])["approved"] == (None, None)
    assert set_(world, "admin", batch["id"], "status", "approved")["locked"]


def test_whoever_submitted_it_doesnt_approve_it(world: World) -> None:
    roles(world, editor=(ANALYST, REVIEWER), admin=(REVIEWER,))
    batch = made(world, {"name": "L-7"})
    set_(world, "editor", batch["id"], "status", "in_review")
    why = "You submitted L-7 for review, so someone else approves it."
    assert offered(world, "editor", batch["id"])["approved"] == (why, "four_eyes")
    body = problem(put(world, "editor", batch["id"], "status", "approved"), 409, "guard_refused")
    assert body["guard"] == "four_eyes"
    # Sent back and submitted again by someone else, the latest submission counts.
    set_(world, "admin", batch["id"], "status", "in_testing")
    roles(world, admin=(REVIEWER, ANALYST))
    set_(world, "admin", batch["id"], "status", "in_review")
    assert set_(world, "editor", batch["id"], "status", "approved")["locked"]


def test_a_guard_reads_another_state_field(world: World) -> None:
    roles(world, editor=(ANALYST,), admin=(QA,))
    batch = made(world, {"result": "pass", "purity": 95.0})
    set_(world, "admin", batch["id"], "custody", "quarantined")
    assert offered(world, "admin", batch["id"])["released"] == (
        "It's quarantined, so it isn't released.",
        "not_quarantined",
    )
    set_(world, "admin", batch["id"], "custody", "in_lab")
    assert set_(world, "admin", batch["id"], "status", "released")["values"]["status"] == "released"


def test_a_guard_that_cant_be_worked_out_refuses(world: World) -> None:
    roles(world, admin=(QA,))
    batch = made(world, {"purity": 95.0})
    # `when` holds (it has a purity), and its result is empty: no pass, no release.
    assert offered(world, "admin", batch["id"])["released"] == (
        "Only a batch that passed is released.",
        "passed",
    )


def test_a_guard_can_keep_agents_from_a_state(world: World) -> None:
    roles(world, editor=(ANALYST,), admin=(REVIEWER,))
    batch = made(world, {})
    set_(world, "editor", batch["id"], "status", "in_review")
    entity = Entity.objects.select_related("owner_project").get(pk=batch["id"])
    f = states.state_fields(BATCH)["status"]
    agent = audit.AgentInfo(client_id="cli-1", client_name="Lab bot")
    with audit.using(audit.Context(via=Via.MCP, oauth_client="Lab bot", agent=agent)):
        found = guards.refusals(
            world.users["admin"], entity, "status", [f.states["approved"]], {"status": "in_review"}
        )
    assert found["approved"].guard.name == "a_person"
    assert found["approved"].message == "A person approves it, not an agent."


def test_a_guard_isnt_checked_for_someone_without_the_role(world: World) -> None:
    batch = made(world, {})
    why, guard = offered(world, "viewer", batch["id"])["approved"]
    assert guard is None and why is not None and "Reviewer role" in why


# --- A state set by signing for it (ADR 0126) ---------------------------------------------------


def signable(world: World, who: str, ref: str) -> list[str | None]:
    """Why ``who`` may not give each of the signatures that certify the batch."""
    body = api_as(world, who).get(f"/entities/{ref}").json()
    found = next(t for t in body["can"]["sign"] if t["to"] == "certified")
    return [s["why_not"] for s in found["signatures"]]


def status(world: World, ref: str) -> str:
    found: str = api_as(world, "viewer").get(f"/entities/{ref}").json()["values"]["status"]
    return found


def test_a_signed_states_guards_are_checked_at_each_signature(world: World) -> None:
    """With its signer acting, and the signatures given so far: nobody signs a change a
    guard refuses, and the plugin says whether one person may give two."""
    ready(world)
    roles(world, editor=(ANALYST,), admin=(REVIEWER, QA), viewer=(QA,))
    batch = made(world, {"result": "fail"})
    passed = "Only a batch that passed is certified."
    assert signable(world, "admin", batch["id"]) == [passed, passed]
    with pytest.raises(Refused, match=passed):
        begin(world, "admin", batch["id"], "certified")
    assert change(world, batch["id"], {"result": "pass"}).status_code == 200
    signed(world, "admin", batch["id"], "certified", index=0)
    other = "You gave one of its signatures, so someone else gives the other."
    assert signable(world, "admin", batch["id"])[1] == other
    assert signable(world, "viewer", batch["id"])[1] is None
    with pytest.raises(Refused, match=other):
        begin(world, "admin", batch["id"], "certified", index=1)
    signed(world, "viewer", batch["id"], "certified")
    assert status(world, batch["id"]) == "certified"


def test_the_last_signature_checks_the_guards_again(world: World) -> None:
    """Two signings started at once each pass at their start; the one finished second is
    checked again, under the record's lock, and its guard refuses it."""
    ready(world)
    roles(world, admin=(REVIEWER, QA))
    batch = made(world, {"result": "pass"})
    second = begin(world, "admin", batch["id"], "certified", index=1, session="tab-2")
    signed(world, "admin", batch["id"], "certified", index=0)
    done = signing.finish(second, evidence(world, "admin"))
    assert done.status == Status.FAILED
    assert done.message == "You gave one of its signatures, so someone else gives the other."
    assert status(world, batch["id"]) == "in_testing"
