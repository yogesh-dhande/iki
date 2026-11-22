"""Electronic signatures under 21 CFR Part 11 (ADR 0126, ADR 0124): a state that needs
``signatures`` is set by signing for it, each signer signing in again at the
identity provider; each signature binds its signer, its meaning and the record's content
at the revision it signs, and verifying it again finds anything changed since."""

import datetime as dt
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from allauth.socialaccount.models import SocialAccount
from api_client import Api, problem
from django.db import connection, transaction
from django.db.utils import ProgrammingError
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from fake_idp import FakeIdP
from org_world import World
from test_api_entities import api_as, create, patch
from test_oauth import bearer, signed_in_token
from test_runs import world
from test_states import ANALYST, QA, REVIEWER, roles, set_

from iki_server.accounts import service_accounts
from iki_server.accounts.models import IdentityProvider, UserSession
from iki_server.audit.models import AuditEvent
from iki_server.entities import states
from iki_server.entities.models import Entity, Revision
from iki_server.entities.values import Invalid
from iki_server.notifications.models import Notification
from iki_server.org.services import Refused
from iki_server.signatures import proof, services, signing_keys
from iki_server.signatures.models import Acknowledgment, Signature, Signing, Status

pytestmark = pytest.mark.django_db

__all__ = ["world"]  # a fixture, from test_runs

LOT = "acme/protein.Lot"


# --- The world: an identity provider, people with accounts there, a certification -------------


def provider(issuer: str = "https://idp.example.com/realms/test", **kw: Any) -> IdentityProvider:
    found = IdentityProvider.objects.filter(is_enabled=True).first()
    if found is not None:
        return found
    made = IdentityProvider(
        display_name="Test SSO",
        issuer=issuer,
        client_id=kw.pop("client_id", "iki-test"),
        authorization_endpoint=kw.pop("authorization_endpoint", f"{issuer}/authorize"),
        is_enabled=True,
        enabled_at=timezone.now(),
        **kw,
    )
    made.client_secret = "secret"
    made.save()
    return made


def ready(
    world: World, found: IdentityProvider | None = None, *, certify: bool = True
) -> IdentityProvider:
    """Everyone in the world has an account at the identity provider, and the organization
    recorded its certification."""
    found = found or provider()
    for user in world.users.values():
        # uid-editor, or uid-foreign-editor in a second world.
        uid = f"uid-{user.email.partition('@')[0]}"
        SocialAccount.objects.get_or_create(
            user=user, provider=found.provider_id, defaults={"uid": uid}
        )
    if certify and services.certified() is None:
        services.certify(world.users["org_admin"], dt.date(2026, 11, 1), "QA-DOC-0042")
    return found


def lot(world: World, label: str = "LOT A", **values: Any) -> dict[str, Any]:
    made = create(api_as(world, "editor"), world.project, {"label": label, **values}, LOT)
    assert made.status_code == 201, made.content
    body: dict[str, Any] = made.json()
    return body


def evidence(world: World, who: str, **kw: Any) -> services.Evidence:
    found = provider()
    return services.Evidence(
        protocol=kw.pop("protocol", "oidc"),
        issuer=found.issuer,
        subject=kw.pop("subject", f"uid-{world.users[who].email.partition('@')[0]}"),
        authenticated_at=kw.pop("authenticated_at", timezone.now()),
        methods=kw.pop("methods", ("pwd", "otp")),
        context=kw.pop("context", ""),
    )


def begin(
    world: World,
    who: str,
    ref: str,
    state: str,
    *,
    index: int | None = None,
    reason: str = "",
    session: str | None = None,
) -> Signing:
    entity = Entity.objects.get(pk=ref)
    return services.begin(
        world.users[who],
        session or f"session-{who}",
        entity,
        "status",
        state,
        index=index,
        expected_revision=entity.revision,
        reason=reason,
        return_to=f"/entities/{entity.hrid}",
        acknowledge=True,
    )


def signed(world: World, who: str, ref: str, state: str, **kw: Any) -> Signing:
    """Signs as ``who``, as if they signed in again at the identity provider just now."""
    claims = kw.pop("evidence", {})
    signing = begin(world, who, ref, state, **kw)
    done = services.finish(signing, evidence(world, who, **claims))
    assert done.status == Status.SIGNED, done.message
    return done


def standing(world: World, ref: str, who: str = "viewer") -> list[dict[str, Any]]:
    answer = api_as(world, who).get(f"/entities/{ref}/signatures", verify="true")
    assert answer.status_code == 200, answer.content
    body: dict[str, Any] = answer.json()
    assert body["missing"] == []
    items: list[dict[str, Any]] = body["items"]
    return items


def offered(world: World, who: str, ref: str) -> dict[str, dict[str, Any]]:
    """The states the record may be set to by signing, as ``who`` reads it."""
    body = api_as(world, who).get(f"/entities/{ref}").json()
    return {t["to"]: t for t in body["can"]["sign"]}


@pytest.fixture
def lab(world: World) -> World:
    """The editor is an Analyst, the admin a Reviewer and the viewer QA."""
    ready(world)
    roles(world, editor=(ANALYST,), admin=(REVIEWER,), viewer=(QA,))
    return world


# --- What a type declares -----------------------------------------------------------------------


def test_a_type_says_which_states_are_signed_for(world: World) -> None:
    found = api_as(world, "viewer").get(f"/entity-types/{LOT}").json()
    [status] = [f for f in found["fields"] if f["name"] == "status"]
    by_name = {st["name"]: st for st in status["states"]}
    assert by_name["approved"]["signatures"] == [
        {
            "meaning": "review",
            "meaning_label": "Review",
            "verb": "Reviewed",
            "label": "Review",
            "statement": "I reviewed this record and found it in order.",
            "roles": [REVIEWER],
        },
        {
            "meaning": "approval",
            "meaning_label": "Approval",
            "verb": "Approved",
            "label": "QA approval",
            "statement": "I approve this lot.",
            "roles": [QA],
        },
    ]
    assert by_name["open"]["signatures"] == []
    # What each lab role lets its holders sign is listed with it.
    listed = {r["name"]: r["uses"] for r in api_as(world, "viewer").get("/lab-roles").json()}
    assert {
        "kind": "signature",
        "owner": LOT,
        "name": "status",
        "description": "sign “QA approval” to set Status to Approved on lots",
    } in listed[QA]


def test_a_signed_state_isnt_set_directly(lab: World) -> None:
    made = lot(lab)
    answer = api_as(lab, "editor").post(
        f"/entities/{made['id']}/states/status",
        {"state": "in_review", "reason": ""},
        headers={"If-Match": f'"{made["revision"]}"'},
    )
    problem(answer, 409, "signatures_required")
    assert "signs in again" in answer.json()["detail"]
    body = api_as(lab, "editor").get(f"/entities/{made['id']}").json()
    [in_review] = [st for st in body["can"]["states"]["status"] if st["name"] == "in_review"]
    assert (in_review["signed"], in_review["allowed"]) == (True, False)
    assert offered(lab, "editor", made["id"])["in_review"]["allowed"] is True
    # Nor through the service, whoever calls it.
    with pytest.raises(states.NeedsSignatures):
        states.set_state(
            lab.users["editor"],
            Entity.objects.get(pk=made["id"]),
            "status",
            "in_review",
            expected_revision=1,
        )


# --- Signing ------------------------------------------------------------------------------------


def test_one_signature_sets_its_state(lab: World) -> None:
    made = lot(lab)
    signing = signed(lab, "editor", made["id"], "in_review", reason="Ready")
    entity = Entity.objects.get(pk=made["id"])
    assert entity.revision == 2
    revision = Revision.objects.get(entity=entity, number=2)
    assert (revision.kind, revision.state_field, revision.state_to) == (
        "state_changed",
        "status",
        "in_review",
    )
    assert revision.signatures == [signing.signature_id]
    assert revision.reason == "Ready"
    [shown] = standing(lab, made["id"])
    assert shown["status"] == "carried" and shown["carried_by"] == 2
    assert shown["revision"] == 1
    assert shown["manifestation"].startswith("Authored by Editor on ")
    assert shown["manifestation"].endswith(" UTC. Meaning: Authorship.")
    assert shown["verification"]["valid"] is True, shown["verification"]
    document = json.loads(shown["document"])
    assert document["change"] == {
        "field": "status",
        "field_label": "Status",
        "from": "open",
        "from_label": "Open",
        "to": "in_review",
        "to_label": "In review",
    }
    assert document["format"] == "iki/signature/v1"
    assert document["record"]["revision"] == 1
    assert document["signer"] == {
        "id": str(lab.users["editor"].pk),
        "name": "Editor",
        "email": "editor@example.com",
    }
    assert document["authentication"]["methods"] == ["pwd", "otp"]
    assert proof.canonical(document) == shown["document"]
    # The first signature records the signer's acknowledgment (21 CFR 11.100).
    assert Acknowledgment.objects.get(user=lab.users["editor"]).statement == (
        services.ACKNOWLEDGMENT
    )
    actions = list(
        AuditEvent.objects.filter(action__startswith="signature.")
        .order_by("occurred_at")
        .values_list("action", flat=True)
    )
    assert actions[-3:] == ["signature.requested", "signature.acknowledged", "signature.signed"]
    moved = AuditEvent.objects.get(action="entity.state_changed")
    assert moved.data["signatures"] == [str(signing.signature_id)]
    # The revision says it was signed, and by whom.
    history = api_as(lab, "viewer").get(f"/entities/{made['id']}/revisions/2").json()
    [carried] = history["state"]["signatures"]
    assert carried["present"] is True
    assert carried["manifestation"] == shown["manifestation"]


def test_a_state_needs_each_of_its_signatures(lab: World) -> None:
    made = lot(lab)
    signed(lab, "editor", made["id"], "in_review")
    approve = offered(lab, "viewer", made["id"])["approved"]
    assert [s["why_not"] for s in approve["signatures"]] == [
        "It needs someone with the Reviewer role in Kinase Atlas.",
        None,
    ]
    signed(lab, "admin", made["id"], "approved", reason="Data checked")
    entity = Entity.objects.get(pk=made["id"])
    assert entity.revision == 2  # still in review: one signature of two
    assert {s["status"] for s in standing(lab, made["id"])} == {"carried", "pending"}
    approve = offered(lab, "viewer", made["id"])["approved"]
    assert approve["allowed"] is True
    assert approve["signatures"][0]["given"]["signer"]["name"] == "Admin"
    signed(lab, "viewer", made["id"], "approved", reason="Approved for release")
    entity.refresh_from_db()
    assert entity.revision == 3
    last = Revision.objects.get(entity=entity, number=3)
    assert last.state_to == "approved"
    assert len(last.signatures) == 2
    assert last.reason == "Data checked\nApproved for release"
    assert all(s["verification"]["valid"] for s in standing(lab, made["id"]))


def test_each_signature_is_its_own_act(lab: World) -> None:
    """Whether one person may give two of a state's signatures is the plugin's rule: core
    asks only that each is a separate signing, re-authenticated, for its own slot."""
    roles(lab, runner=(REVIEWER, QA))
    made = lot(lab)
    signed(lab, "editor", made["id"], "in_review")
    first = signed(lab, "runner", made["id"], "approved")
    assert first.index == 0
    with pytest.raises(Refused, match="Runner gave it"):
        begin(lab, "runner", made["id"], "approved", index=0)
    second = signed(lab, "runner", made["id"], "approved")
    assert second.index == 1 and second.signature_id != first.signature_id
    assert Entity.objects.get(pk=made["id"]).revision == 3


def test_a_change_to_the_record_lapses_its_pending_signatures(lab: World) -> None:
    made = lot(lab)
    signed(lab, "editor", made["id"], "in_review")
    signed(lab, "admin", made["id"], "approved")
    api = api_as(lab, "editor")
    etag = api.get(f"/entities/{made['id']}")["ETag"]
    assert patch(api, made["id"], {"values": {"yield_mg": 12.5}}, etag).status_code == 200
    shown = {s["change"]["to"]: s for s in standing(lab, made["id"])}
    assert shown["approved"]["status"] == "lapsed"
    assert shown["approved"]["verification"]["valid"] is True  # still what it signed
    approve = offered(lab, "admin", made["id"])["approved"]
    assert approve["signatures"][0]["given"] is None
    assert approve["signatures"][0]["allowed"] is True


def test_a_signer_who_loses_the_role_no_longer_counts(lab: World) -> None:
    made = lot(lab)
    signed(lab, "editor", made["id"], "in_review")
    signed(lab, "admin", made["id"], "approved")
    roles(lab, admin=())
    approve = offered(lab, "viewer", made["id"])["approved"]
    assert approve["signatures"][0]["given"] is None
    assert "no longer hold the Reviewer role" in approve["signatures"][0]["lapsed"]


def test_a_signer_withdraws_a_pending_signature(lab: World) -> None:
    made = lot(lab)
    signed(lab, "editor", made["id"], "in_review")
    signing = signed(lab, "admin", made["id"], "approved")
    path = f"/signatures/{signing.signature_id}/withdrawal"
    assert api_as(lab, "viewer").post(path, {"reason": ""}).status_code == 403
    answer = api_as(lab, "admin").post(path, {"reason": "Wrong lot"})
    assert answer.status_code == 200, answer.content
    assert answer.json()["status"] == "withdrawn"
    assert "Wrong lot" in answer.json()["status_detail"]
    problem(api_as(lab, "admin").post(path, {"reason": ""}), 409, "refused")
    # A carried one stays.
    [submitted] = [s for s in standing(lab, made["id"]) if s["change"]["to"] == "in_review"]
    path = f"/signatures/{submitted['id']}/withdrawal"
    problem(api_as(lab, "editor").post(path, {"reason": ""}), 409, "refused")


def test_a_reason_the_state_requires_is_each_signers(lab: World) -> None:
    made = lot(lab)
    signed(lab, "editor", made["id"], "in_review")
    signed(lab, "admin", made["id"], "approved")
    signed(lab, "viewer", made["id"], "approved")
    with pytest.raises(Invalid):
        begin(lab, "viewer", made["id"], "released")
    signed(lab, "viewer", made["id"], "released", reason="Batch record complete")


def test_the_first_signature_needs_the_acknowledgment(lab: World) -> None:
    made = lot(lab)
    api = api_as(lab, "editor")
    answer = api.post(
        f"/entities/{made['id']}/signatures",
        {
            "field": "status",
            "state": "in_review",
            "reason": "",
            "acknowledge": False,
            "return_to": "/",
        },
        headers={"If-Match": '"1"'},
    )
    body = problem(answer, 422, "validation_failed")
    assert body["errors"][0]["pointer"] == "#/acknowledge"
    me = api.get("/me/signing").json()
    assert me["acknowledged"] is False and me["can_sign"] is True
    assert me["acknowledgment"] == services.ACKNOWLEDGMENT


def test_signing_comes_back_only_to_this_site(lab: World) -> None:
    made = lot(lab)
    for bad in ("https://evil.example.com/", "//evil.example.com/", "/\\evil", "entities"):
        answer = api_as(lab, "editor").post(
            f"/entities/{made['id']}/signatures",
            {"field": "status", "state": "in_review", "acknowledge": True, "return_to": bad},
            headers={"If-Match": '"1"'},
        )
        problem(answer, 422, "validation_failed")


def test_signing_signs_the_revision_its_signer_read(lab: World) -> None:
    made = lot(lab)
    answer = api_as(lab, "editor").post(
        f"/entities/{made['id']}/signatures",
        {"field": "status", "state": "in_review", "acknowledge": True, "return_to": "/"},
        headers={"If-Match": '"7"'},
    )
    problem(answer, 412, "stale")
    # It changed while they signed in again: nothing is signed.
    signing = begin(lab, "editor", made["id"], "in_review")
    api = api_as(lab, "editor")
    assert patch(api, made["id"], {"values": {"yield_mg": 1.0}}, '"1"').status_code == 200
    done = services.finish(signing, evidence(lab, "editor"))
    assert done.status == Status.FAILED
    assert "changed while you were signing in" in done.message
    assert not Signature.objects.exists()


# --- Who can't sign -----------------------------------------------------------------------------


def test_no_token_can_sign(lab: World) -> None:
    made = lot(lab)
    _, tokens = signed_in_token(lab, "editor")
    answer = bearer(
        tokens["access_token"],
        "POST",
        f"/entities/{made['id']}/signatures",
        {"field": "status", "state": "in_review", "acknowledge": True, "return_to": "/"},
        {"If-Match": '"1"'},
    )
    body = problem(answer, 403, "cannot_sign")
    assert "no token, app or AI agent" in body["detail"]


def test_nobody_signs_before_the_organization_certifies(world: World) -> None:
    ready(world, certify=False)
    roles(world, editor=(ANALYST,))
    made = lot(world)
    with pytest.raises(Refused, match=r"11\.100\(c\)"):
        begin(world, "editor", made["id"], "in_review")
    answer = api_as(world, "org_admin").post(
        "/signatures/certification", {"submitted_on": "2026-11-02", "reference": "QA-DOC-7"}
    )
    assert answer.status_code == 200, answer.content
    assert answer.json()["certification"]["reference"] == "QA-DOC-7"
    assert (
        api_as(world, "admin")
        .post("/signatures/certification", {"submitted_on": "2026-11-02", "reference": "x"})
        .status_code
        == 403
    )
    signed(world, "editor", made["id"], "in_review")


def test_service_accounts_and_the_nameless_cant_sign(lab: World) -> None:
    user = lab.users["editor"]
    user.name = ""
    user.save(update_fields=["name"])
    assert "name" in (services.why_cant_sign(user) or "")
    robot = service_accounts.create("Robot", "", by=lab.users["org_admin"])
    assert "Service accounts" in (services.why_cant_sign(robot) or "")


# --- Signing in again ---------------------------------------------------------------------------


@pytest.fixture
def oidc(lab: World, fake_idp: FakeIdP) -> FakeIdP:
    """The organization signs in at a real (fake) OpenID Connect provider."""
    IdentityProvider.objects.all().delete()
    found = provider(
        fake_idp.issuer,
        client_id=fake_idp.client_id,
        authorization_endpoint=fake_idp.discovery()["authorization_endpoint"],
    )
    found.client_secret = fake_idp.client_secret
    found.save()
    SocialAccount.objects.all().delete()
    ready(lab, found)
    return fake_idp


def start(api: Api, ref: str, state: str, **body: Any) -> dict[str, Any]:
    etag = api.get(f"/entities/{ref}")["ETag"]
    answer = api.post(
        f"/entities/{ref}/signatures",
        {
            "field": "status",
            "state": state,
            "acknowledge": True,
            "return_to": f"/entities/{ref}",
            **body,
        },
        headers={"If-Match": etag},
    )
    assert answer.status_code == 201, answer.content
    started: dict[str, Any] = answer.json()
    return started


def at_idp(api: Api, idp: FakeIdP, started: dict[str, Any], **claims: Any) -> Any:
    """Follows the signing to the provider, signs in there, and returns Iki's answer."""
    go = api.browser.get(started["sign_in_url"])
    assert go.status_code == 302, go.content
    callback = urlsplit(idp.authorize(go["Location"], claims))
    answer = api.browser.get(f"{callback.path}?{callback.query}")
    answer.callback = f"{callback.path}?{callback.query}"  # type: ignore[attr-defined]
    return answer


def test_signing_signs_in_again_at_the_identity_provider(lab: World, oidc: FakeIdP) -> None:
    made = lot(lab)
    api = api_as(lab, "editor")
    started = start(api, made["id"], "in_review", reason="Ready")
    assert started["status"] == "started"
    back = at_idp(api, oidc, started, sub="uid-editor", amr=["pwd", "mfa"], acr="loa2")
    replay = back.callback
    assert back.status_code == 302
    assert back["Location"] == f"/entities/{made['id']}?signing={started['id']}"
    request = oidc.authorizations[-1]
    assert request["prompt"] == "login" and request["max_age"] == "0"
    assert request["login_hint"] == "editor@example.com"
    assert len(request["nonce"]) >= 32 and request["code_challenge_method"] == "S256"
    done = api.get(f"/signings/{started['id']}").json()
    assert done["status"] == "signed", done
    assert done["set"] is True and done["state"] == "in_review"
    [shown] = standing(lab, made["id"])
    assert shown["authentication"]["protocol"] == "oidc"
    assert shown["authentication"]["issuer"] == oidc.issuer
    assert shown["authentication"]["subject"] == "uid-editor"
    assert shown["authentication"]["methods"] == ["pwd", "mfa"]
    assert shown["authentication"]["context"] == "loa2"
    # The session is the one it was: signing in again signs nobody in.
    assert api.get("/me").json()["email"] == "editor@example.com"
    # The provider's answer signs once: again, it's no signing's, and signs nothing.
    assert api.browser.get(started["sign_in_url"]).status_code == 404
    api.browser.get(replay)
    assert Signature.objects.count() == 1
    assert api.get("/me").json()["email"] == "editor@example.com"


def test_an_earlier_sign_in_doesnt_sign(lab: World, oidc: FakeIdP) -> None:
    made = lot(lab)
    api = api_as(lab, "editor")
    started = start(api, made["id"], "in_review")
    at_idp(api, oidc, started, sub="uid-editor", auth_time=int(time.time()) - 3600)
    done = api.get(f"/signings/{started['id']}").json()
    assert done["status"] == "failed"
    assert "didn’t make you sign in again" in done["message"]
    started = start(api, made["id"], "in_review")
    at_idp(api, oidc, started, sub="uid-editor", auth_time=None)
    assert "auth_time" in api.get(f"/signings/{started['id']}").json()["message"]
    assert not Signature.objects.exists()
    assert not AuditEvent.objects.filter(action="signature.failed", data__counts=True).exists()


def test_an_answer_to_another_request_doesnt_sign(lab: World, oidc: FakeIdP) -> None:
    made = lot(lab)
    api = api_as(lab, "editor")
    started = start(api, made["id"], "in_review")
    at_idp(api, oidc, started, sub="uid-editor", nonce="someone-elses-nonce")
    done = api.get(f"/signings/{started['id']}").json()
    assert done["status"] == "failed" and "another request" in done["message"]
    assert AuditEvent.objects.get(action="signature.failed").data["counts"] is True


def test_cancelling_at_the_identity_provider_doesnt_count(lab: World, oidc: FakeIdP) -> None:
    made = lot(lab)
    api = api_as(lab, "editor")
    started = start(api, made["id"], "in_review")
    go = api.browser.get(started["sign_in_url"])
    state = parse_qs(urlsplit(go["Location"]).query)["state"][0]
    callback = f"/accounts/oidc/{provider().provider_id}/login/callback/"
    api.browser.get(callback, {"error": "access_denied", "state": state})
    done = api.get(f"/signings/{started['id']}").json()
    assert done["status"] == "cancelled"
    assert AuditEvent.objects.get(action="signature.failed").data["counts"] is False


def test_a_signing_is_only_its_starters_in_their_session(lab: World, oidc: FakeIdP) -> None:
    made = lot(lab)
    started = start(api_as(lab, "editor"), made["id"], "in_review")
    # The same person in another browser, and someone else, can't take it on.
    assert lab.browser("editor").get(started["sign_in_url"]).status_code == 404
    assert lab.browser("admin").get(started["sign_in_url"]).status_code == 404
    assert api_as(lab, "admin").get(f"/signings/{started['id']}").status_code == 404
    assert lab.browser("editor").get("/accounts/signing/not-a-uuid/").status_code == 404


def test_someone_else_signing_in_locks_signing_after_three_tries(lab: World, oidc: FakeIdP) -> None:
    made = lot(lab)
    api = api_as(lab, "editor")
    for attempt in range(3):
        started = start(api, made["id"], "in_review")
        at_idp(api, oidc, started, sub="uid-admin")
        done = api.get(f"/signings/{started['id']}").json()
        if attempt < 2:
            assert done["status"] == "failed" and "Someone else signed in" in done["message"]
    editor = lab.users["editor"]
    alerts = Notification.objects.filter(recipient=lab.users["org_admin"])
    assert alerts.filter(kind="signature.attempt_refused").count() == 3
    assert alerts.filter(kind="signature.locked").count() == 1
    assert Notification.objects.filter(recipient=editor, kind="signature.locked").exists()
    assert services.locked_out(editor)
    assert not UserSession.objects.filter(user=editor).exists()
    assert "locked" in (services.why_cant_sign(editor) or "")
    # An Org Admin unlocks it; nobody else can.
    path = f"/users/{editor.pk}/signing/unlock"
    assert api_as(lab, "admin").post(path).status_code == 403
    assert api_as(lab, "org_admin").post(path).status_code == 204
    assert not services.locked_out(editor)
    problem(api_as(lab, "org_admin").post(path), 409, "refused")
    signed(lab, "editor", made["id"], "in_review")


def test_org_admins_certify_and_unlock_in_settings(world: World) -> None:
    """Settings → Signatures: the certification (21 CFR 11.100(c)), who's locked out, and
    the keys. Org Admins act there; Auditors read it."""
    from org_world import post

    from iki_server.signatures import alerts

    ready(world, certify=False)
    admin = world.browser("org_admin")
    page = admin.get("/admin/signatures/").content.decode()
    assert "Nobody can sign until an Org Admin records" in page
    # A date to come is refused, as the service refuses it.
    later = (timezone.localdate() + dt.timedelta(days=2)).isoformat()
    refused = post(
        admin,
        "/admin/signatures/",
        {"action": "certify", "submitted_on": later, "reference": "QA-DOC-9"},
    )
    assert "It was sent on or before today." in refused.content.decode()
    assert services.certified() is None
    done = post(
        admin,
        "/admin/signatures/",
        {"action": "certify", "submitted_on": "2026-11-01", "reference": "QA-DOC-9"},
    )
    assert done.status_code == 302
    assert services.certified() is not None
    # Locked signing, and its unlocking.
    editor = world.users["editor"]
    alerts.lock(editor, 3)
    alert = Notification.objects.get(recipient=world.users["org_admin"], kind="signature.locked")
    assert alert.path == "/admin/signatures/"
    page = admin.get("/admin/signatures/").content.decode()
    assert "Unlock Editor’s signing" in page
    reader = world.browser("auditor").get("/admin/signatures/").content.decode()
    assert "QA-DOC-9" in reader and "Unlock Editor" not in reader
    post(admin, "/admin/signatures/", {"action": "unlock", "user": str(editor.pk)})
    assert not services.locked_out(editor)
    assert AuditEvent.objects.filter(action="signature.unlocked", target_id=str(editor.pk)).exists()


def test_saml_signing_forces_authentication(lab: World, saml_idp: Any, settings: Any) -> None:
    from fake_saml_idp import FakeSamlIdP, _cert_b64

    IdentityProvider.objects.all().delete()
    SocialAccount.objects.all().delete()
    found = IdentityProvider.objects.create(
        protocol="saml",
        display_name="Test SAML",
        issuer=saml_idp.entity_id,
        authorization_endpoint=saml_idp.sso_url,
        saml_certificate=_cert_b64(saml_idp.cert),
        is_enabled=True,
        enabled_at=timezone.now(),
    )
    ready(lab, found)
    SocialAccount.objects.filter(user=lab.users["editor"]).update(uid="editor@example.com")
    made = lot(lab)
    api = api_as(lab, "editor")

    def sign(**kw: Any) -> dict[str, Any]:
        started = start(api, made["id"], "in_review")
        go = api.browser.get(started["sign_in_url"])
        assert go.status_code == 302, go.content
        request = FakeSamlIdP.read_request(go["Location"])
        assert request.force_authn is True
        response = saml_idp.respond(request, name_id=kw.pop("name_id", "editor@example.com"), **kw)
        acs = api.browser.post(
            "/accounts/saml/sso/acs/",
            {"SAMLResponse": response, "RelayState": request.relay_state},
            HTTP_ORIGIN="https://idp.example.com",
        )
        assert acs.status_code == 302, acs.content
        back = api.browser.get(urlsplit(acs["Location"]).path)
        assert back.status_code == 302, back.content
        assert back["Location"].endswith(f"?signing={started['id']}")
        body: dict[str, Any] = api.get(f"/signings/{started['id']}").json()
        return body

    old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
    assert "didn’t make you sign in again" in sign(authn_instant=old)["message"]
    assert "Someone else" in sign(name_id="admin@example.com")["message"]
    assert sign(tamper=True)["status"] == "failed"
    done = sign()
    assert done["status"] == "signed", done
    [shown] = standing(lab, made["id"])
    assert shown["authentication"]["protocol"] == "saml"
    assert shown["authentication"]["context"].endswith("PasswordProtectedTransport")
    assert api.get("/me").json()["email"] == "editor@example.com"


# --- Verifying ----------------------------------------------------------------------------------


@contextmanager
def past_the_triggers() -> Iterator[Any]:
    """As a database superuser who turns the triggers off: what nothing in Iki can do."""
    with connection.cursor() as cursor:
        cursor.execute("SET session_replication_role = replica")
        try:
            yield cursor
        finally:
            cursor.execute("SET session_replication_role = DEFAULT")


def _failed(ref: str, world: World) -> dict[str, str]:
    body = api_as(world, "viewer").get(f"/entities/{ref}/signatures", verify="true").json()
    return {
        c["name"]: c["detail"]
        for s in body["items"]
        for c in s["verification"]["checks"]
        if not c["ok"]
    }


def test_changing_signed_history_in_sql_is_found(lab: World) -> None:
    made = lot(lab)
    signed(lab, "editor", made["id"], "in_review")
    table = Entity.objects.get(pk=made["id"])
    from iki_server.entities.values import shape

    history = shape(LOT).parts[0].history._meta.db_table
    with past_the_triggers() as cursor:
        cursor.execute(
            f'UPDATE "{history}" SET label = %s WHERE entity_id = %s AND revision = 1',  # noqa: S608 - a test's own table name
            ["LOT B", table.pk],
        )
    failed = _failed(made["id"], lab)
    assert list(failed) == ["record"]
    assert "`label` changed" in failed["record"]


def test_moving_a_signature_to_another_record_is_found(lab: World) -> None:
    made, other = lot(lab), lot(lab, "LOT C")
    signing = signed(lab, "editor", made["id"], "in_review")
    with past_the_triggers() as cursor:
        cursor.execute(
            "UPDATE iki_signature SET entity_id = %s WHERE id = %s",
            [other["id"], signing.signature_id],
        )
    failed = _failed(other["id"], lab)
    assert "row" in failed and "record" in failed["row"]
    body = api_as(lab, "viewer").get(f"/entities/{made['id']}/signatures").json()
    assert body["missing"] == [
        f"Revision 2 was made with signature {signing.signature_id}, which signed another "
        "record or revision."
    ]


def test_changing_what_was_signed_breaks_the_signature(lab: World) -> None:
    made = lot(lab)
    signing = signed(lab, "editor", made["id"], "in_review")
    assert signing.signature is not None
    found = signing.signature
    document = json.loads(found.document)
    document["signature"]["meaning_label"] = "Approval"
    with past_the_triggers() as cursor:
        cursor.execute(
            "UPDATE iki_signature SET document = %s WHERE id = %s",
            [proof.canonical(document), found.pk],
        )
    assert "signature" in _failed(made["id"], lab)


def test_a_deleted_signature_is_missed(lab: World) -> None:
    made = lot(lab)
    signing = signed(lab, "editor", made["id"], "in_review")
    with past_the_triggers() as cursor:
        cursor.execute("DELETE FROM iki_signing")
        cursor.execute("DELETE FROM iki_signature WHERE id = %s", [signing.signature_id])
    body = api_as(lab, "viewer").get(f"/entities/{made['id']}/signatures").json()
    assert body["missing"] == [
        f"Revision 2 was made with signature {signing.signature_id}, which is gone."
    ]


def test_a_key_this_deployment_doesnt_trust_fails(lab: World) -> None:
    made = lot(lab)
    signed(lab, "editor", made["id"], "in_review")
    signing_keys.rotate()
    # A retired key still verifies what it signed.
    assert _failed(made["id"], lab) == {}
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM iki_signing")
        cursor.execute("UPDATE iki_signing_key SET private_encrypted = 'x'")
    signing_keys._private.cache_clear()
    assert "key" in _failed(made["id"], lab)


def test_the_signing_key_command_prints_and_rotates_keys(lab: World) -> None:
    from io import StringIO

    from django.core.management import call_command

    made = lot(lab)
    signed(lab, "editor", made["id"], "in_review")
    out = StringIO()
    call_command("signing_key", stdout=out)
    [first] = out.getvalue().splitlines()
    assert first.startswith("test=ed25519:") and first.endswith("signing now")
    out = StringIO()
    call_command("signing_key", "--rotate", stdout=out, stderr=StringIO())
    public = out.getvalue().strip().removeprefix("test=")
    assert signing_keys.current()[0].public == public
    rotated = AuditEvent.objects.get(action="signature.key_rotated")
    assert rotated.data["key"] == public
    assert rotated.data["retired"] == [first.split("=")[1].split()[0]]
    # What the old key signed still verifies; the next signature is the new key's.
    assert _failed(made["id"], lab) == {}
    signed(lab, "admin", made["id"], "approved")
    assert Signature.objects.filter(key=public).count() == 1


def test_signatures_are_append_only(lab: World) -> None:
    made = lot(lab)
    signing = signed(lab, "editor", made["id"], "in_review")
    for statement in (
        "UPDATE iki_signature SET meaning = 'approval'",
        "DELETE FROM iki_signature_acknowledgment",
        "UPDATE iki_signature_certification SET reference = 'x'",
        "DELETE FROM iki_signature",
    ):
        with pytest.raises(ProgrammingError, match="append-only"), transaction.atomic():
            connection.cursor().execute(statement)
    assert signing.signature is not None
    signing.signature.refresh_from_db()
    assert signing.signature.meaning == "authorship"


def test_only_readers_see_signatures(lab: World) -> None:
    made = lot(lab)
    signing = signed(lab, "editor", made["id"], "in_review")
    for path in (
        f"/entities/{made['id']}/signatures",
        f"/signatures/{signing.signature_id}",
        f"/signatures/{signing.signature_id}/verification",
    ):
        assert api_as(lab, "outsider").get(path).status_code == 404, path
        assert api_as(lab, "viewer").get(path).status_code == 200, path


# --- History --------------------------------------------------------------------------------------


def _history_queries(lab: World, ref: str, cycles: int) -> int:
    """Signs the lot into review and sends it back ``cycles`` times, then counts the queries
    a page of its revisions takes."""
    for _ in range(cycles):
        signed(lab, "editor", ref, "in_review")
        set_(lab, "admin", ref, "status", "open", reason="More data")
    api = api_as(lab, "viewer")
    with CaptureQueriesContext(connection) as queries:
        answer = api.get(f"/entities/{ref}/revisions", limit=100)
    assert answer.status_code == 200, answer.content
    items = answer.json()["items"]
    changed = [i for i in items if i["kind"] == "state_changed"]
    assert len(changed) == Revision.objects.filter(entity_id=ref, kind="state_changed").count()
    assert all(s["present"] for i in changed for s in i["state"]["signatures"])
    return len(queries)


def test_a_page_of_signed_history_takes_as_many_queries_however_long_it_is(lab: World) -> None:
    """Its state changes' type and signatures are found for the whole page at once."""
    made = lot(lab)
    few = _history_queries(lab, made["id"], 1)
    assert _history_queries(lab, made["id"], 6) == few
