"""The SCIM 2.0 API, driven with the requests Microsoft Entra ID and Okta send."""

import json
from typing import Any

import pytest
from django.test import Client

from iki_server.accounts.models import User, UserSession
from iki_server.audit.models import AuditEvent, Via
from iki_server.scim import tokens
from iki_server.scim.models import ScimGroup

pytestmark = pytest.mark.django_db

BASE = "/scim/v2"
USER = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


class Result:
    """A SCIM response: status, headers and the parsed JSON body."""

    def __init__(self, response: Any) -> None:
        self.status_code: int = response.status_code
        self.headers = response.headers
        self.json_body: Any = json.loads(response.content) if response.content else None

    def __getitem__(self, header: str) -> str:
        return str(self.headers[header])


class Scim:
    """An identity provider calling Iki's SCIM API."""

    def __init__(self, token: str) -> None:
        self.client = Client()
        self.token = token

    def call(self, method: str, path: str, body: Any = None, token: str | None = None) -> Result:
        response = self.client.generic(
            method,
            BASE + path,
            json.dumps(body) if body is not None else "",
            content_type="application/scim+json",
            secure=True,
            HTTP_AUTHORIZATION=f"Bearer {token or self.token}",
        )
        return Result(response)

    def get(self, path: str) -> Result:
        return self.call("GET", path)

    def post(self, path: str, body: Any) -> Result:
        return self.call("POST", path, body)

    def patch(self, path: str, *operations: dict[str, Any]) -> Result:
        return self.call("PATCH", path, {"schemas": [PATCH], "Operations": list(operations)})


@pytest.fixture
def admin() -> User:
    return User.objects.create_user("admin@example.com", "Org Admin")


@pytest.fixture
def scim(admin: User) -> Scim:
    return Scim(tokens.create(by=admin))


def okta_user(user_name: str = "ada@example.com", **extra: Any) -> dict[str, Any]:
    return {
        "schemas": [USER],
        "userName": user_name,
        "name": {"givenName": "Ada", "familyName": "Lovelace"},
        "emails": [{"primary": True, "value": user_name, "type": "work"}],
        "displayName": "Ada Lovelace",
        "externalId": "00u1okta",
        "active": True,
        **extra,
    }


def entra_user(upn: str = "rosalind@example.com") -> dict[str, Any]:
    return {
        "schemas": [USER, "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"],
        "externalId": "entra-object-id",
        "userName": upn,
        "active": True,
        "displayName": "Rosalind Franklin",
        "emails": [{"primary": True, "type": "work", "value": upn}],
        "name": {
            "formatted": "Rosalind Franklin",
            "familyName": "Franklin",
            "givenName": "Rosalind",
        },
        "title": "Crystallographer",
        "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User": {"department": "Chemistry"},
    }


# --- Authentication ---------------------------------------------------------------


def test_requests_need_the_live_token(scim: Scim, admin: User) -> None:
    for token in ("", "iki_scim_wrong", "not-even-ours"):
        response = scim.call("GET", "/Users", token=token or " ")
        assert response.status_code == 401
        assert response["WWW-Authenticate"] == 'Bearer realm="iki-scim"'
        assert response.json_body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    old = scim.token
    replacement = tokens.create(by=admin)
    assert scim.call("GET", "/Users", token=old).status_code == 401
    assert scim.call("GET", "/Users", token=replacement).status_code == 200
    tokens.revoke(by=admin)
    assert scim.call("GET", "/Users", token=replacement).status_code == 401
    assert not tokens.scim_enabled()


def test_only_a_hash_of_the_token_is_stored(scim: Scim) -> None:
    record = tokens.live()
    assert record is not None
    assert scim.token not in record.token_hash
    assert record.hint == scim.token[-4:]


def test_requests_without_a_valid_token_are_rate_limited(scim: Scim) -> None:
    statuses = [scim.call("GET", "/Users", token="iki_scim_x").status_code for _ in range(31)]
    assert statuses[:30] == [401] * 30
    assert statuses[30] == 429
    assert scim.get("/Users").status_code == 200  # the real IdP isn't affected


def test_token_use_is_recorded(scim: Scim) -> None:
    scim.get("/Users")
    record = tokens.live()
    assert record is not None
    assert record.last_used_at is not None


def test_discovery_endpoints(scim: Scim) -> None:
    config = scim.get("/ServiceProviderConfig").json_body
    assert config["patch"]["supported"] is True
    assert config["bulk"]["supported"] is False
    assert config["authenticationSchemes"][0]["type"] == "oauthbearertoken"
    assert {r["id"] for r in scim.get("/ResourceTypes").json_body["Resources"]} == {"User", "Group"}
    assert {s["id"] for s in scim.get("/Schemas").json_body["Resources"]} == {USER, GROUP}


def test_unknown_paths_and_methods_answer_in_scim_shape(scim: Scim) -> None:
    missing = scim.get("/Users/not-a-uuid")
    assert missing.status_code == 404
    assert missing.json_body["status"] == "404"
    assert scim.get("/Users/0190f7b6-7a1e-7000-8000-000000000000").status_code == 404
    assert scim.call("DELETE", "/Users").status_code == 405
    bad = scim.client.post(
        BASE + "/Users",
        "{not json",
        content_type="application/scim+json",
        secure=True,
        HTTP_AUTHORIZATION=f"Bearer {scim.token}",
    )
    assert bad.status_code == 400
    assert json.loads(bad.content)["scimType"] == "invalidSyntax"


# --- Users -------------------------------------------------------------------------


def test_okta_creates_a_person(scim: Scim) -> None:
    response = scim.post("/Users", okta_user())
    assert response.status_code == 201, response.json_body
    body = response.json_body
    user = User.objects.get(email="ada@example.com")
    assert body["id"] == str(user.pk)
    assert response["Location"] == f"https://testserver{BASE}/Users/{user.pk}"
    assert body["userName"] == "ada@example.com"
    assert body["externalId"] == "00u1okta"
    assert body["active"] is True
    assert body["emails"] == [{"value": "ada@example.com", "type": "work", "primary": True}]
    assert body["groups"] == []
    assert user.name == "Ada Lovelace"
    assert user.is_provisioned
    assert not user.has_usable_password()
    event = AuditEvent.objects.get(action="user.provisioned")
    assert event.via == Via.SCIM
    assert event.actor is None
    assert event.actor_label == f"SCIM token …{scim.token[-4:]}"
    assert event.data == {"adopted": False, "external_id": "00u1okta"}


def test_entra_creates_a_person_and_extra_attributes_are_ignored(scim: Scim) -> None:
    response = scim.post("/Users", entra_user())
    assert response.status_code == 201, response.json_body
    user = User.objects.get(email="rosalind@example.com")
    assert user.name == "Rosalind Franklin"
    assert user.scim_external_id == "entra-object-id"


def test_a_person_needs_a_user_name_and_an_email(scim: Scim) -> None:
    assert scim.post("/Users", {"schemas": [USER]}).status_code == 400
    response = scim.post("/Users", {"schemas": [USER], "userName": "ada"})
    assert response.status_code == 400
    assert response.json_body["scimType"] == "invalidValue"


def test_provisioning_someone_twice_conflicts(scim: Scim) -> None:
    scim.post("/Users", okta_user())
    response = scim.post("/Users", okta_user())
    assert response.status_code == 409
    assert response.json_body["scimType"] == "uniqueness"


def test_provisioning_takes_over_an_existing_account(scim: Scim) -> None:
    existing = User.objects.create_user("ADA@example.com", "Ada")
    response = scim.post("/Users", okta_user())
    assert response.status_code == 201
    assert response.json_body["id"] == str(existing.pk)
    existing.refresh_from_db()
    assert existing.is_provisioned
    assert AuditEvent.objects.get(action="user.provisioned").data["adopted"] is True


def test_people_are_found_by_user_name_external_id_and_email(scim: Scim) -> None:
    scim.post("/Users", okta_user())
    scim.post("/Users", entra_user())
    User.objects.create_user("unprovisioned@example.com")
    for query in (
        'userName eq "ADA@example.com"',
        'externalId eq "00u1okta"',
        'emails[type eq "work"].value eq "ada@example.com"',
        'emails.value eq "ada@example.com"',
        'userName eq "ada@example.com" and externalId eq "00u1okta"',
    ):
        found = scim.call("GET", f"/Users?filter={query}").json_body
        assert found["totalResults"] == 1, query
        assert found["Resources"][0]["userName"] == "ada@example.com"
    everyone = scim.get("/Users").json_body
    assert everyone["totalResults"] == 2  # only provisioned people are visible over SCIM
    assert scim.get('/Users?filter=userName eq "nobody@example.com"').json_body["totalResults"] == 0
    bad = scim.get('/Users?filter=title sw "Dr"')
    assert bad.status_code == 400
    assert bad.json_body["scimType"] == "invalidFilter"


def test_listing_people_is_paginated(scim: Scim) -> None:
    for n in range(5):
        scim.post("/Users", okta_user(f"p{n}@example.com", externalId=f"x{n}"))
    page = scim.get("/Users?startIndex=2&count=2").json_body
    assert (page["totalResults"], page["startIndex"], page["itemsPerPage"]) == (5, 2, 2)
    assert [r["userName"] for r in page["Resources"]] == ["p1@example.com", "p2@example.com"]
    assert scim.get("/Users?count=abc").status_code == 400


def test_entra_deactivates_with_a_string_boolean_and_sessions_end(scim: Scim) -> None:
    user_id = scim.post("/Users", entra_user()).json_body["id"]
    user = User.objects.get(pk=user_id)
    Client().force_login(user)
    assert UserSession.objects.filter(user=user).exists()
    response = scim.patch(
        f"/Users/{user_id}", {"op": "Replace", "path": "active", "value": "False"}
    )
    assert response.status_code == 200
    assert response.json_body["active"] is False
    user.refresh_from_db()
    assert not user.is_active
    assert not UserSession.objects.filter(user=user).exists()
    event = AuditEvent.objects.get(action="user.deactivated")
    assert (event.via, event.target_id) == (Via.SCIM, user_id)
    scim.patch(f"/Users/{user_id}", {"op": "Replace", "path": "active", "value": "True"})
    user.refresh_from_db()
    assert user.is_active


def test_okta_deactivates_with_a_value_object(scim: Scim) -> None:
    user_id = scim.post("/Users", okta_user()).json_body["id"]
    scim.patch(f"/Users/{user_id}", {"op": "replace", "value": {"active": False}})
    assert not User.objects.get(pk=user_id).is_active


def test_profile_changes_follow_the_identity_provider(scim: Scim) -> None:
    user_id = scim.post("/Users", entra_user()).json_body["id"]
    response = scim.patch(
        f"/Users/{user_id}",
        {"op": "Replace", "path": "name.familyName", "value": "Franklin-Klug"},
        {"op": "Replace", "path": "displayName", "value": ""},
        {"op": "Replace", "path": 'emails[type eq "work"].value', "value": "rf@example.com"},
        {"op": "Add", "path": "title", "value": "ignored"},
        {"op": "Replace", "value": {"name.givenName": "Ros", "externalId": "new-id"}},
    )
    assert response.status_code == 200, response.json_body
    user = User.objects.get(pk=user_id)
    assert (user.email, user.name, user.scim_external_id) == (
        "rf@example.com",
        "Ros Franklin-Klug",
        "new-id",
    )
    changed = AuditEvent.objects.get(action="user.updated")
    assert changed.data["email"] == ["rosalind@example.com", "rf@example.com"]


def test_put_replaces_a_person(scim: Scim) -> None:
    user_id = scim.post("/Users", okta_user()).json_body["id"]
    body = okta_user(
        name={"givenName": "Augusta", "familyName": "King"}, displayName="", active=False
    )
    response = scim.call("PUT", f"/Users/{user_id}", body)
    assert response.status_code == 200
    user = User.objects.get(pk=user_id)
    assert (user.name, user.is_active) == ("Augusta King", False)


def test_changing_to_someone_elses_email_conflicts(scim: Scim) -> None:
    scim.post("/Users", okta_user())
    other = scim.post("/Users", entra_user()).json_body["id"]
    response = scim.patch(
        f"/Users/{other}", {"op": "replace", "path": "userName", "value": "ada@example.com"}
    )
    assert response.status_code == 409


def test_delete_deactivates_and_keeps_the_account(scim: Scim) -> None:
    user_id = scim.post("/Users", okta_user()).json_body["id"]
    assert scim.call("DELETE", f"/Users/{user_id}").status_code == 204
    user = User.objects.get(pk=user_id)
    assert not user.is_active
    assert not user.is_provisioned
    assert scim.get(f"/Users/{user_id}").status_code == 404
    assert AuditEvent.objects.filter(action="user.deprovisioned", target_id=user_id).exists()
    # Provisioning them again takes the same account back over.
    again = scim.post("/Users", okta_user())
    assert again.status_code == 201
    assert again.json_body["id"] == user_id
    assert again.json_body["active"] is True


# --- Groups ------------------------------------------------------------------------


def _people(scim: Scim, *names: str) -> list[str]:
    return [
        scim.post("/Users", okta_user(f"{n}@example.com", externalId=n)).json_body["id"]
        for n in names
    ]


def _member_ids(scim: Scim, group_id: str) -> set[str]:
    return {m["value"] for m in scim.get(f"/Groups/{group_id}").json_body["members"]}


def test_groups_are_created_with_members(scim: Scim) -> None:
    ada, grace = _people(scim, "ada", "grace")
    response = scim.post(
        "/Groups",
        {
            "schemas": [GROUP],
            "displayName": "Chemistry",
            "externalId": "g1",
            "members": [{"value": ada}],
        },
    )
    assert response.status_code == 201, response.json_body
    body = response.json_body
    assert body["displayName"] == "Chemistry"
    assert [m["value"] for m in body["members"]] == [ada]
    assert response["Location"].endswith(f"/Groups/{body['id']}")
    assert scim.post("/Groups", {"displayName": "chemistry"}).status_code == 409
    listed = scim.get(f"/Users/{ada}").json_body["groups"]
    assert [g["display"] for g in listed] == ["Chemistry"]
    assert grace not in _member_ids(scim, body["id"])


def test_groups_are_found_by_display_name_even_with_and_in_it(scim: Scim) -> None:
    scim.post("/Groups", {"displayName": "Research and Development"})
    scim.post("/Groups", {"displayName": "Chemistry"})
    found = scim.get(
        '/Groups?filter=displayName eq "Research and Development"&excludedAttributes=members'
    ).json_body
    assert found["totalResults"] == 1
    assert "members" not in found["Resources"][0]


def test_okta_changes_members_by_path(scim: Scim) -> None:
    ada, grace, alan = _people(scim, "ada", "grace", "alan")
    group_id = scim.post(
        "/Groups", {"displayName": "Chemistry", "members": [{"value": ada}]}
    ).json_body["id"]
    response = scim.patch(
        f"/Groups/{group_id}",
        {"op": "add", "path": "members", "value": [{"value": grace}, {"value": alan}]},
    )
    assert response.status_code == 204
    assert _member_ids(scim, group_id) == {ada, grace, alan}
    scim.patch(f"/Groups/{group_id}", {"op": "remove", "path": f'members[value eq "{grace}"]'})
    assert _member_ids(scim, group_id) == {ada, alan}
    scim.patch(
        f"/Groups/{group_id}", {"op": "replace", "value": {"id": group_id, "displayName": "Chem"}}
    )
    assert ScimGroup.objects.get(pk=group_id).display_name == "Chem"
    event = AuditEvent.objects.filter(action="scim.group_updated").order_by("occurred_at").first()
    assert event is not None
    assert event.data["added"] == ["alan@example.com", "grace@example.com"]


def test_entra_removes_members_with_a_value_list_and_replaces_them(scim: Scim) -> None:
    ada, grace = _people(scim, "ada", "grace")
    group_id = scim.post(
        "/Groups", {"displayName": "Chemistry", "members": [{"value": ada}, {"value": grace}]}
    ).json_body["id"]
    scim.patch(
        f"/Groups/{group_id}", {"op": "Remove", "path": "members", "value": [{"value": ada}]}
    )
    assert _member_ids(scim, group_id) == {grace}
    scim.patch(
        f"/Groups/{group_id}", {"op": "Replace", "path": "members", "value": [{"value": ada}]}
    )
    assert _member_ids(scim, group_id) == {ada}
    scim.patch(f"/Groups/{group_id}", {"op": "Remove", "path": "members"})
    assert _member_ids(scim, group_id) == set()


def test_unknown_members_are_ignored(scim: Scim) -> None:
    group_id = scim.post(
        "/Groups",
        {
            "displayName": "Chemistry",
            "members": [{"value": "0190f7b6-7a1e-7000-8000-000000000000"}, {"value": "x"}],
        },
    ).json_body["id"]
    assert _member_ids(scim, group_id) == set()


def test_put_replaces_a_group_and_delete_removes_it(scim: Scim) -> None:
    ada, grace = _people(scim, "ada", "grace")
    group_id = scim.post(
        "/Groups", {"displayName": "Chemistry", "members": [{"value": ada}]}
    ).json_body["id"]
    response = scim.call(
        "PUT", f"/Groups/{group_id}", {"displayName": "Biology", "members": [{"value": grace}]}
    )
    assert response.status_code == 200
    assert response.json_body["displayName"] == "Biology"
    assert _member_ids(scim, group_id) == {grace}
    assert scim.call("DELETE", f"/Groups/{group_id}").status_code == 204
    assert scim.get(f"/Groups/{group_id}").status_code == 404
    assert AuditEvent.objects.filter(action="scim.group_deleted").exists()


def test_renaming_into_another_groups_name_conflicts(scim: Scim) -> None:
    scim.post("/Groups", {"displayName": "Chemistry"})
    group_id = scim.post("/Groups", {"displayName": "Biology"}).json_body["id"]
    response = scim.patch(
        f"/Groups/{group_id}", {"op": "replace", "path": "displayName", "value": "Chemistry"}
    )
    assert response.status_code == 409
