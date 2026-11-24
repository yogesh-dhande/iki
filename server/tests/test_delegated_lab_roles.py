"""Lab-role decisions and attribution under a delegated credential."""

from dataclasses import replace

import pytest
from org_world import build_world
from test_delegation import Bot, delegate
from test_lab_roles import ANALYST, QA, give
from test_oauth import bearer
from test_requests import Lab, _member
from test_states import roles, sample
from test_storage import lab, sent_with_tubes

from iki_server.audit.models import AuditEvent
from iki_server.entities import guards, states
from iki_server.entities.models import Entity, Revision
from iki_server.org import permissions
from iki_server.org.models import LabRoleGrant, ProjectRole
from iki_spec.model import Guard

pytestmark = pytest.mark.django_db

__all__ = ["lab"]


def test_delegated_state_changes_and_guards_use_the_same_shared_role() -> None:
    world = build_world()
    made = sample(world)
    roles(world, editor=(ANALYST,))
    bot = Bot(world)
    membership = world.project.memberships.get(user=bot.account)
    assert give(world, membership, QA).status_code == 200
    delegation = delegate(world, bot)
    token = bot.token(delegation.grant)
    path = f"/entities/{made['id']}"
    body = {"state": "in_testing"}
    etag = bearer(token, "GET", path)["ETag"]
    # Both roles can set In testing. They still need one qualifying role in common.
    assert (
        bearer(token, "POST", path + "/states/status", body, headers={"If-Match": etag}).status_code
        == 403
    )
    entity = Entity.objects.get(pk=made["id"])
    field = states.state_fields(entity.type)["status"]
    guard = Guard(name="analyst", check='"analyst" in actor.roles', message="Analyst required.")
    guarded = replace(field.states["in_testing"], guards={guard.name: guard})
    person = world.users["editor"]
    permissions.limit_to(
        person, permissions.TokenLimits(frozenset({"read", "write"}), alongside=bot.account)
    )
    permissions.remember_roles(person, [world.project])
    assert guards.refusals(person, entity, "status", [guarded], {})
    assert give(world, membership, QA, ANALYST).status_code == 200
    permissions.forget(bot.account)
    assert not guards.refusals(person, entity, "status", [guarded], {})
    etag = bearer(token, "GET", path)["ETag"]
    changed = bearer(token, "POST", path + "/states/status", body, headers={"If-Match": etag})
    assert changed.status_code == 200, changed.content
    revision = Revision.objects.get(entity=entity, number=changed.json()["revision"])
    assert revision.user == person and revision.agent is not None
    assert (revision.agent.client_id, revision.agent.grant) == (
        bot.client.client_id,
        delegation.grant,
    )


def test_receiving_requires_a_shared_service_role_and_records_both_identities(lab: Lab) -> None:
    ask, _ = sent_with_tubes(lab, 1)
    bot = Bot(lab.world)
    _member(lab.core, bot.account, ProjectRole.EDITOR)
    delegation = delegate(lab.world, bot, "tech")
    token = bot.token(delegation.grant)
    path = f"/entities/{ask['id']}/receipts"
    assert lab.api("tech").get(path).json()["can_receive"]
    assert not bearer(token, "GET", path).json()["can_receive"]
    assert bearer(token, "POST", path).status_code == 403
    grant = LabRoleGrant.objects.create(
        membership=lab.core.memberships.get(user=bot.account), role=ANALYST
    )
    received = bearer(token, "POST", path)
    assert received.status_code == 201, received.content
    assert bearer(token, "POST", path).json()["id"] == received.json()["id"]
    event = AuditEvent.objects.get(
        action="request.receipt_started", target_id=received.json()["id"]
    )
    assert event.actor == lab.users["tech"] and event.agent is not None
    assert (event.agent.client_id, event.agent.grant) == (bot.client.client_id, delegation.grant)
    grant.delete()
    assert bearer(token, "POST", path).status_code == 403
