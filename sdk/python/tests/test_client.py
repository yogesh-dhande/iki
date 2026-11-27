"""The client, against a fake Iki: what it sends, and what it makes of the answers."""

import hashlib
import inspect
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx2
import pandas as pd
import pytest
from fake_iki import URL, FakeIki, config, entity, isolated, problem, store_with

import iki
from iki import auth
from iki.client import Client, _Part

__all__ = ["config", "isolated"]  # the fixtures, used here

PROJECT = "00000000-0000-7000-8000-0000000000p1".replace("p", "a")


@pytest.fixture
def fake() -> FakeIki:
    return FakeIki()


@pytest.fixture
def client(fake: FakeIki, config: Path) -> Client:
    return fake.client(store_with(config))


def page(items: list[Any], next_cursor: str | None = None) -> dict[str, Any]:
    return {"items": items, "next_cursor": next_cursor}


def run(hrid: str = "RUN-000001", status: str = "queued", **extra: Any) -> dict[str, Any]:
    return {
        "id": f"00000000-0000-7000-9000-{int(hrid[-6:]):012d}",
        "hrid": hrid,
        "action": "iki/protein.compute_properties",
        "status": status,
        "project": PROJECT,
        "requested_by": {"id": "u1", "name": "Rosalind Franklin"},
        "via": "api",
        "reason": "",
        "tier": "backend",
        "created_at": "2026-11-01T09:00:00Z",
        "started_at": None,
        "finished_at": None,
        "inputs": [{"name": "protein", "hrid": "PRT-000001"}],
        "metrics": {},
        "error": None,
        **extra,
    }


# --- Which Iki, as whom ------------------------------------------------------------------------


def test_it_says_how_to_sign_in_when_it_cant_tell_where() -> None:
    with pytest.raises(iki.NotSignedIn, match=r"iki.login\('https://iki.example.com'\)"):
        Client()


def test_it_says_when_youre_not_signed_in(config: Path) -> None:
    with pytest.raises(iki.NotSignedIn, match=f"not signed in to {URL}: run iki.login"):
        Client(URL, store=auth.Store(config))


def test_it_uses_the_deployment_you_last_signed_in_to(fake: FakeIki, config: Path) -> None:
    store = store_with(config)
    auth._remember_url(URL, config)
    fake.on("GET", "/api/v1/me", {"name": "Rosalind Franklin"})
    made = Client(transport=httpx2.MockTransport(fake.handle), store=store)
    assert made.url == URL
    assert made.me()["name"] == "Rosalind Franklin"
    sent = fake.requests[0]
    assert sent.headers["Authorization"] == "Bearer access-1"
    assert sent.headers["User-Agent"].startswith("ikisci/")


def test_an_agent_may_say_what_model_it_is(
    fake: FakeIki, monkeypatch: pytest.MonkeyPatch, config: Path
) -> None:
    monkeypatch.setenv("IKI_URL", URL)
    monkeypatch.setenv("IKI_TOKEN", "agent-token")
    fake.on("GET", "/api/v1/me", {"name": "Rosalind Franklin"})
    transport = httpx2.MockTransport(fake.handle)
    Client(transport=transport, store=auth.Store(config), agent_model="claude-opus-5-5").me()
    assert fake.requests[-1].headers["Iki-Agent-Model"] == "claude-opus-5-5"
    monkeypatch.setenv("IKI_AGENT_MODEL", "claude-sonnet-5-5")
    Client(transport=transport, store=auth.Store(config)).me()
    assert fake.requests[-1].headers["Iki-Agent-Model"] == "claude-sonnet-5-5"
    monkeypatch.delenv("IKI_AGENT_MODEL")
    Client(transport=transport, store=auth.Store(config)).me()
    assert "Iki-Agent-Model" not in fake.requests[-1].headers


def test_a_token_from_the_environment_is_used_as_it_is(
    fake: FakeIki, monkeypatch: pytest.MonkeyPatch, config: Path
) -> None:
    monkeypatch.setenv("IKI_URL", URL)
    monkeypatch.setenv("IKI_TOKEN", "service-token")
    fake.on("GET", "/api/v1/me", {"name": "Pipeline"})
    made = Client(transport=httpx2.MockTransport(fake.handle), store=auth.Store(config))
    made.me()
    assert fake.requests[0].headers["Authorization"] == "Bearer service-token"
    # It isn't renewed: there's nothing to renew it with.
    fake.on("GET", "/api/v1/me", problem(401, "Not signed in"))
    with pytest.raises(iki.NotSignedIn, match="check IKI_TOKEN"):
        made.me()


def test_an_expired_access_token_is_renewed_and_the_request_sent_again(
    fake: FakeIki, client: Client, config: Path
) -> None:
    fake.on("GET", "/api/v1/me", problem(401, "Expired"), {"name": "Rosalind Franklin"})
    fake.on(
        "POST",
        "/oauth/token/",
        {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
    )
    assert client.me()["name"] == "Rosalind Franklin"
    tokens = [r.headers["Authorization"] for r in fake.sent("GET", "/api/v1/me")]
    assert tokens == ["Bearer access-1", "Bearer access-2"]
    assert auth.Store(config).load(URL).refresh_token == "refresh-2"  # type: ignore[union-attr]


def test_a_sign_in_that_still_fails_after_renewing_says_to_sign_in_again(
    fake: FakeIki, client: Client
) -> None:
    fake.on("GET", "/api/v1/me", problem(401, "No"))
    fake.on("POST", "/oauth/token/", {"access_token": "access-2", "expires_in": 3600})
    with pytest.raises(iki.NotSignedIn, match=r"run iki\.login"):
        client.me()
    assert len(fake.sent("POST", "/oauth/token/")) == 1


def test_a_token_about_to_expire_is_renewed_first(fake: FakeIki, config: Path) -> None:
    made = fake.client(store_with(config, expires_at=0.0))
    fake.on("POST", "/oauth/token/", {"access_token": "access-2", "expires_in": 3600})
    fake.on("GET", "/api/v1/me", {"name": "R"})
    made.me()
    assert fake.sent("GET", "/api/v1/me")[0].headers["Authorization"] == "Bearer access-2"


# --- When Iki refuses --------------------------------------------------------------------------


def test_it_waits_as_long_as_iki_asks_then_tries_again(fake: FakeIki, client: Client) -> None:
    slow = httpx2.Response(429, headers={"Retry-After": "7"}, json={"detail": "Slow down"})
    fake.on("GET", "/api/v1/me", slow, {"name": "R"})
    assert client.me()["name"] == "R"
    assert fake.slept == [7.0]


def test_it_gives_up_after_waiting_a_few_times(fake: FakeIki, client: Client) -> None:
    fake.on("GET", "/api/v1/me", problem(429, "Too many requests", "rate_limited"))
    with pytest.raises(iki.RateLimited):
        client.me()
    assert len(fake.sent("GET", "/api/v1/me")) == 5


def test_problems_become_errors_that_say_what_was_wrong(fake: FakeIki, client: Client) -> None:
    fake.on(
        "POST",
        "/api/v1/entities",
        problem(
            422,
            "Some values are invalid.",
            "validation_failed",
            errors=[{"pointer": "#/values/sequence", "detail": "isn't a protein sequence."}],
            request_id="req-1",
        ),
    )
    with pytest.raises(iki.Invalid) as raised:
        client.entities.create("iki/protein.Protein", {"sequence": "123"}, project=PROJECT)
    error = raised.value
    assert (error.status, error.code, error.request_id) == (422, "validation_failed", "req-1")
    assert [str(e) for e in error.errors] == ["#/values/sequence: isn't a protein sequence."]
    assert str(error) == (
        "Some values are invalid. (422, validation_failed)\n"
        "  #/values/sequence: isn't a protein sequence.\n"
        "  Request ID: req-1"
    )


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (403, iki.Forbidden),
        (404, iki.NotFound),
        (409, iki.Conflict),
        (412, iki.Conflict),
        (500, iki.ApiError),
    ],
)
def test_each_kind_of_refusal_has_its_own_error(
    fake: FakeIki, client: Client, status: int, kind: type[iki.ApiError]
) -> None:
    fake.on("GET", "/api/v1/entities/PRT-000001", problem(status, "No"))
    with pytest.raises(kind):
        client.entities.get("PRT-000001")


def test_paths_are_escaped(fake: FakeIki, client: Client) -> None:
    with pytest.raises(iki.NotFound):
        client.entities.get("../me")
    assert fake.requests[0].url.raw_path == b"/api/v1/entities/..%2Fme"


def test_names_keep_their_namespace(fake: FakeIki, client: Client) -> None:
    # A plugin's name is `namespace/name`: its `/` goes as it is, and nothing else does.
    fake.on("GET", "/api/v1/entity-types/iki/protein.Protein", {"name": "iki/protein.Protein"})
    assert client.types.get("iki/protein.Protein")["name"] == "iki/protein.Protein"
    assert fake.requests[0].url.raw_path == b"/api/v1/entity-types/iki/protein.Protein"
    for odd, sent in [
        ("../runs", b"..%2Fruns"),
        ("a/b/c", b"a%2Fb%2Fc"),
        ("/x", b"%2Fx"),
        ("iki/p q", b"iki/p%20q"),
    ]:
        with pytest.raises(iki.NotFound):
            client.actions.get(odd)
        assert fake.requests[-1].url.raw_path == b"/api/v1/actions/" + sent


# --- Entities ----------------------------------------------------------------------------------


def test_searching_goes_through_every_page(fake: FakeIki, client: Client) -> None:
    fake.on(
        "POST",
        "/api/v1/entities/search",
        page([entity("PRT-000001"), entity("PRT-000002")], "next"),
        page([entity("PRT-000003")]),
    )
    found = client.entities.search(
        "iki/protein.Protein",
        project=PROJECT,
        where={"field": "length", "op": "gte", "value": 300},
        sort="-created_at",
        limit=2,
    )
    assert [e["hrid"] for e in found] == ["PRT-000001", "PRT-000002", "PRT-000003"]
    sent = [json.loads(r.content) for r in fake.sent("POST", "/api/v1/entities/search")]
    assert sent[0] == {
        "type": "iki/protein.Protein",
        "project": PROJECT,
        "where": {"field": "length", "op": "gte", "value": 300},
        "sort": {"field": "created_at", "descending": True},
        "archived": False,
        "exact_type": False,
        "limit": 2,
        "cursor": None,
    }
    assert sent[1]["cursor"] == "next"


def test_first_fetches_one_page(fake: FakeIki, client: Client) -> None:
    fake.on("POST", "/api/v1/entities/search", page([entity()], "more"))
    assert client.entities.search().first()["hrid"] == "PRT-000001"  # type: ignore[index]
    assert len(fake.requests) == 1


def test_projects_are_named_by_slug_or_id(fake: FakeIki, client: Client) -> None:
    fake.on(
        "GET",
        "/api/v1/projects",
        page([{"id": "x", "slug": "enzymes"}], "c2"),
        page([{"id": PROJECT, "slug": "kinase-atlas"}]),
    )
    fake.on("POST", "/api/v1/entities/search", page([]))
    list(client.entities.search(project="kinase-atlas"))
    assert fake.body("POST", "/api/v1/entities/search")["project"] == PROJECT
    with pytest.raises(iki.IkiError, match="No project 'nope'"):
        client.projects.get("nope")


def test_entities_are_looked_up_by_an_external_id(fake: FakeIki, client: Client) -> None:
    fake.on("GET", "/api/v1/entities/lookup", page([entity("PRT-000007", lims_id="LIMS-7")]))
    found = client.entities.lookup("iki/protein.Protein", "lims_id", "LIMS-7", project=PROJECT)
    assert [e["hrid"] for e in found] == ["PRT-000007"]
    sent = fake.requests[-1].url.params
    assert dict(sent) == {
        "type": "iki/protein.Protein",
        "field": "lims_id",
        "value": "LIMS-7",
        "project": PROJECT,
        "limit": "50",
    }


def test_a_registry_id_names_one_of_your_projects_records(fake: FakeIki, client: Client) -> None:
    fake.on("GET", "/api/v1/entities/CPD-000123", entity("CPD-000123"))
    client.entities.get("CPD-000123", project=PROJECT)
    assert dict(fake.requests[-1].url.params) == {"project": PROJECT}
    fake.on("POST", "/api/v1/entities", {**entity("CPD-000123"), "registered": False})
    client.entities.create("iki/small_molecule.Compound", {"smiles": "CCO"}, project=PROJECT)
    assert fake.body("POST", "/api/v1/entities") == {
        "type": "iki/small_molecule.Compound",
        "project": PROJECT,
        "values": {"smiles": "CCO"},
        "reason": "",
    }


def test_registrars_curate_entries_on_the_revision_they_read(fake: FakeIki, client: Client) -> None:
    fake.on("PATCH", "/api/v1/entries/CPD-000123", {"hrid": "CPD-000123"})
    client.entries.curate("CPD-000123", {"inn": "imatinib"}, revision=2, reason="INN granted")
    sent = fake.sent("PATCH", "/api/v1/entries/CPD-000123")[0]
    assert sent.headers["If-Match"] == '"2"'
    assert json.loads(sent.content) == {"values": {"inn": "imatinib"}, "reason": "INN granted"}
    fake.on("POST", "/api/v1/entries/CPD-000123/supersede", {"hrid": "CPD-000123"})
    client.entries.supersede(
        "CPD-000123", replacement="CPD-000124", revision=3, reason="Wrong salt form"
    )
    superseded = fake.sent("POST", "/api/v1/entries/CPD-000123/supersede")[0]
    assert superseded.headers["If-Match"] == '"3"'
    assert json.loads(superseded.content) == {
        "replacement": "CPD-000124",
        "reason": "Wrong salt form",
    }


def test_a_record_moves_from_the_revision_read(fake: FakeIki, client: Client) -> None:
    path = "/api/v1/entities/CPD-000123/move"
    fake.on("POST", path, problem(412, "Someone changed it since you read it.", "stale"), {})
    with pytest.raises(iki.Stale) as raised:
        client.entities.move("CPD-000123", revision=4, reason="Superseded")
    assert iki.is_stale(raised.value) and isinstance(raised.value, iki.Conflict)
    client.entities.move("CPD-000123", revision=5, reason="Superseded")
    assert [r.headers["If-Match"] for r in fake.sent("POST", path)] == ['"4"', '"5"']
    assert fake.body("POST", path) == {"reason": "Superseded"}


def test_a_recount_names_the_balance_counted_from_or_that_there_was_none(
    fake: FakeIki, client: Client
) -> None:
    path = "/api/v1/entities/SMP-000001/amount/adjust"
    fake.on("POST", path, {}, {})
    client.entities.adjust("SMP-000001", (240, "uL"), revision=7, reason="Recount")
    client.entities.adjust("SMP-000001", (100, "uL"), revision=None, reason="First count")
    counted, first = fake.sent("POST", path)
    assert counted.headers["If-Match"] == '"7"' and "If-None-Match" not in counted.headers
    assert first.headers["If-None-Match"] == "*" and "If-Match" not in first.headers


def test_a_change_that_named_no_revision_is_told_so(fake: FakeIki, client: Client) -> None:
    path = "/api/v1/entities/SMP-000001/amount/adjust"
    fake.on("POST", path, problem(428, "Send If-Match.", "precondition_required"))
    with pytest.raises(iki.PreconditionRequired) as raised:
        client.entities.adjust("SMP-000001", (1, "uL"), revision=1, reason="Recount")
    assert iki.is_precondition_required(raised.value) and not iki.is_stale(raised.value)
    assert iki.if_match(3) == {"If-Match": '"3"'}
    assert iki.if_none_match_any() == {"If-None-Match": "*"}


def test_draws_tell_the_server_their_purpose(fake: FakeIki, client: Client) -> None:
    ref = "SMP-000001"
    path = f"/api/v1/entities/{ref}/amount/draw"
    fake.on("POST", path, {}, {})
    client.entities.draw(ref, (1, "uL"))
    client.entities.draw(ref, (1, "uL"), purpose="qa_sampling", reason="Release testing")
    routine, sample = (json.loads(r.content) for r in fake.sent("POST", path))
    assert routine["purpose"] == "routine"
    assert sample == {
        "amount": {"value": "1", "unit": "uL"},
        "purpose": "qa_sampling",
        "reason": "Release testing",
    }


def test_draw_records_what_received_the_material_and_the_scanned_source(
    fake: FakeIki, client: Client
) -> None:
    path = "/api/v1/entities/LOT-000001/amount/draw"
    fake.on("POST", path, {})
    client.entities.draw(
        "LOT-000001", (5, "mL"), used_in="EXP-000002", confirm="LABEL-17", reason="Buffer"
    )
    assert fake.body("POST", path) == {
        "amount": {"value": "5", "unit": "mL"},
        "purpose": "routine",
        "reason": "Buffer",
        "used_in": "EXP-000002",
        "confirm": "LABEL-17",
    }


def test_draw_passes_an_explicitly_entered_iki_id(fake: FakeIki, client: Client) -> None:
    ref = "TUB-000001"
    path = f"/api/v1/entities/{ref}/amount/draw"
    fake.on("POST", path, {})
    client.entities.draw(ref, (1, "uL"), confirm={"mode": "iki_id", "code": ref})
    assert fake.body("POST", path)["confirm"] == {"mode": "iki_id", "code": ref}


def test_read_the_revision_a_result_release_identifies(fake: FakeIki, client: Client) -> None:
    ref = entity("ANR-000012")["id"]
    release = {"entity": ref, "reviewed_revision": 4, "signed_revision": 5, "released_revision": 6}
    fake.on("GET", "/api/v1/entities/REQ-000001/request", {"result_releases": [release]})
    fake.on("GET", f"/api/v1/entities/{ref}/revisions/4", {"number": 4, "values": {"purity": 98}})
    [received] = client.entities.request("REQ-000001")["result_releases"]
    assert received["reviewed_revision"] is not None
    reviewed = client.entities.revision(received["entity"], received["reviewed_revision"])
    assert (reviewed["number"], reviewed["values"]["purity"]) == (4, 98)


def test_changes_name_the_revision_they_were_decided_on(fake: FakeIki, client: Client) -> None:
    fake.on("PATCH", "/api/v1/entities/PRT-000001", entity(name="ABL1"))
    client.entities.update("PRT-000001", {"name": "ABL1"}, revision=3, reason="Typo")
    sent = fake.sent("PATCH", "/api/v1/entities/PRT-000001")[0]
    assert sent.headers["If-Match"] == '"3"'
    assert json.loads(sent.content) == {"values": {"name": "ABL1"}, "reason": "Typo"}
    client.entities.archive("PRT-000001", revision=4, reason="Duplicate")
    assert fake.body("PATCH", "/api/v1/entities/PRT-000001") == {
        "archived": True,
        "reason": "Duplicate",
    }


def test_state_changes_name_the_revision_they_were_decided_on(
    fake: FakeIki, client: Client
) -> None:
    path = "/api/v1/entities/SAM-000001/states/status"
    fake.on("POST", path, entity(name="S1"))
    client.entities.set_state(
        "SAM-000001", "status", "in_testing", revision=4, reason="Purity looks off"
    )
    sent = fake.sent("POST", path)[0]
    assert sent.headers["If-Match"] == '"4"'
    assert json.loads(sent.content) == {"state": "in_testing", "reason": "Purity looks off"}
    # A dataframe of records with state fields has a column for each, of its states.
    sample = entity("SAM-000001", name="S1", status="in_testing", custody="in_lab")
    fake.on("POST", "/api/v1/entities/search", page([sample]))
    frame = client.entities.search("acme/lims.Sample").to_pandas()
    assert frame.loc["SAM-000001", "status"] == "in_testing"
    assert frame.loc["SAM-000001", "custody"] == "in_lab"


def test_a_record_is_taken(fake: FakeIki, client: Client) -> None:
    taken = "/api/v1/entities/SMP-000001/take"
    fake.on("POST", taken, entity("SMP-000001", name="S1"))
    client.entities.take("SMP-000001", "qa", revision=5)
    sent = fake.sent("POST", taken)[0]
    assert sent.headers["If-Match"] == '"5"'
    assert json.loads(sent.content) == {"field": "qa", "reason": ""}


def test_requests_go_to_a_service_and_come_back_with_results(fake: FakeIki, client: Client) -> None:
    project, core = "0190d5a4-0000-7000-8000-000000000001", "0190d5a4-0000-7000-8000-000000000002"
    listed: Any = [{"project": {"id": core, "name": "Core"}, "description": "", "types": []}]
    fake.on("GET", f"/api/v1/projects/{project}/services", page(listed))
    services = list(client.projects.services(project, type="iki/requests.PlainRequest"))
    assert services == listed
    assert fake.sent("GET", f"/api/v1/projects/{project}/services")[0].url.params.get_list(
        "type"
    ) == ["iki/requests.PlainRequest"]
    submitted = "/api/v1/entities/REQ-000001/submit"
    fake.on("POST", submitted, entity("REQ-000001", title="Lot 12"))
    client.entities.submit("REQ-000001", services[0]["project"], revision=2, reason="Lot 12")
    sent = fake.sent("POST", submitted)[0]
    assert sent.headers["If-Match"] == '"2"'
    assert json.loads(sent.content) == {"service": core, "reason": "Lot 12"}
    sides: Any = {"service": {"id": core}, "sent": [], "results": []}
    fake.on("GET", "/api/v1/entities/REQ-000001/request", sides)
    assert client.entities.request("REQ-000001") == sides
    # A service's Admin chooses what it takes.
    intake = {"types": [], "from_registry": True, "projects": []}
    fake.on("PUT", f"/api/v1/projects/{core}/intake", intake)
    client.projects.set_intake(core, types=["iki/requests.PlainRequest"], projects=[project])
    assert fake.body("PUT", f"/api/v1/projects/{core}/intake") == {
        "types": ["iki/requests.PlainRequest"],
        "from_registry": False,
        "projects": [project],
    }
    fake.on("GET", f"/api/v1/projects/{core}/intake", intake)
    assert client.projects.intake(core) == intake
    named: Any = [{"id": core, "name": "Core", "slug": "core"}]
    fake.on("GET", f"/api/v1/projects/{project}/sent-to", page(named))
    assert list(client.projects.sent_to(project)) == named
    fake.on("GET", f"/api/v1/projects/{core}/sent-from", page([]))
    assert list(client.projects.sent_from(core)) == []


def test_many_records_are_reassigned_at_once(fake: FakeIki, client: Client) -> None:
    first, second = entity("SMP-000001"), entity("SMP-000002")
    second["revision"] = 3
    done = {"changed": [first["id"]], "skipped": [], "group": "g"}
    fake.on("POST", "/api/v1/entities/assign", done)
    answer = client.entities.assign([first, second], "analyst", "ana@example.com", reason="Away")
    assert answer == done
    assert fake.body("POST", "/api/v1/entities/assign") == {
        "entities": [first["id"], second["id"]],
        "expected_revisions": {first["id"]: first["revision"], second["id"]: second["revision"]},
        "field": "analyst",
        "person": "ana@example.com",
        "op": "set",
        "reason": "Away",
    }


def test_work_is_listed_page_by_page(fake: FakeIki, client: Client) -> None:
    item = {"entity": entity("SMP-000001"), "fields": ["analyst"], "due": "2026-12-01"}
    fake.on("GET", "/api/v1/me/work-items", page([item], "next"), page([item]))
    found = list(
        client.work.mine(
            project=PROJECT, type="acme/lims.Sample", state={"status": ["received", "in_testing"]}
        )
    )
    assert found == [item, item]
    first, second = fake.sent("GET", "/api/v1/me/work-items")
    assert first.url.params.get_list("project") == [PROJECT]
    assert first.url.params.get_list("type") == ["acme/lims.Sample"]
    assert first.url.params.get_list("state") == ["status.received", "status.in_testing"]
    assert (first.url.params["sort"], second.url.params["cursor"]) == ("due", "next")
    fake.on("GET", "/api/v1/me/open-work-items", page([]))
    assert list(client.work.open(sort="updated")) == []
    assert fake.sent("GET", "/api/v1/me/open-work-items")[0].url.params["sort"] == "updated"


def test_notifications_are_listed_and_chosen(fake: FakeIki, client: Client) -> None:
    told: dict[str, Any] = {
        "id": "n1",
        "kind": "acme/lims.Sample.in_review",
        "title": "SMP-000001: In review",
    }
    fake.on("GET", "/api/v1/notifications", page([told]))
    assert [dict(n) for n in client.notifications.list(unread=True)] == [told]
    assert fake.sent("GET", "/api/v1/notifications")[0].url.params["unread"] == "true"
    fake.on("PATCH", "/api/v1/notifications/n1", told)
    client.notifications.mark_read(told)
    assert fake.body("PATCH", "/api/v1/notifications/n1") == {"read": True}

    settings: dict[str, Any] = {"kinds": [], "projects": []}
    fake.on("PATCH", "/api/v1/me/notification-settings", settings, settings)
    client.notifications.choose({"acme/lims.Sample.in_review": "in_app"})
    client.notifications.choose({"acme/lims.Sample.in_review": None}, project=PROJECT)
    first, second = fake.sent("PATCH", "/api/v1/me/notification-settings")
    assert json.loads(first.content) == {"kinds": {"acme/lims.Sample.in_review": "in_app"}}
    assert json.loads(second.content) == {
        "projects": [{"project": PROJECT, "kind": "acme/lims.Sample.in_review", "delivery": None}]
    }


def test_a_record_is_watched_or_muted(fake: FakeIki, client: Client) -> None:
    record = entity("SMP-000001")
    path = f"/api/v1/entities/{record['id']}/watch"
    watching = {"mode": "watching", "can_watch": True, "can_mute": True}
    fake.on("PUT", path, watching)
    assert client.notifications.watch(record) == watching
    assert fake.body("PUT", path) == {"mode": "watching"}
    fake.on("GET", path, watching)
    assert client.notifications.watching(record)["mode"] == "watching"


def test_entities_make_a_dataframe_a_row_each(fake: FakeIki, client: Client) -> None:
    linked = entity(
        "PRT-000002",
        name="SRC",
        length=536,
        parent="00000000-0000-7000-8000-000000000001",
        parts=[{"part": "00000000-0000-7000-8000-000000000001", "copies": 2}],
    )
    linked["refs"] = {"00000000-0000-7000-8000-000000000001": {"hrid": "PRT-000001"}}
    fake.on(
        "POST",
        "/api/v1/entities/search",
        page([entity("PRT-000001", name="ABL1", length=1130, type="kinase"), linked]),
    )
    frame = client.entities.search("iki/protein.Protein").to_pandas()
    assert list(frame.index) == ["PRT-000001", "PRT-000002"]
    assert frame.loc["PRT-000001", "name"] == "ABL1"
    assert frame.loc["PRT-000002", "length"] == 536
    # References read as the HRIDs they refer to.
    assert frame.loc["PRT-000002", "parent"] == "PRT-000001"
    # So do a list of rows' (ADR 0137).
    assert frame.loc["PRT-000002", "parts"] == [{"part": "PRT-000001", "copies": 2}]
    # A field named like one of every entity's columns keeps both.
    assert frame.loc["PRT-000001", "type"] == "iki/protein.Protein"
    assert frame.loc["PRT-000001", "values.type"] == "kinase"
    assert frame.loc["PRT-000001", "project"] == "kinase-atlas"
    assert frame["updated_at"].iloc[0] == pd.Timestamp("2026-11-02T10:30:00Z")
    assert client.entities.search("iki/protein.Protein").to_pandas(limit=1).shape[0] == 1


def test_a_dataframe_names_the_rules_that_caught_each_record(fake: FakeIki, client: Client) -> None:
    held = entity("SAM-000001", purity=82.5)
    held["holds"] = [{"rule": "purity_in_specification", "label": "Purity in specification"}]
    broken = entity("SAM-000002", purity=97.0)
    broken["breaches"] = [{"rule": "expires_after_made", "label": "Expires after it's made"}]
    fake.on("POST", "/api/v1/entities/search", page([held, broken, entity("SAM-000003")]))
    where = {"any": [{"field": "held", "op": "is_set", "value": True}]}
    frame = client.entities.search("acme/lims.Sample", where=where).to_pandas()
    assert json.loads(fake.sent("POST", "/api/v1/entities/search")[0].content)["where"] == where
    assert frame.loc["SAM-000001", "held"] == ["purity_in_specification"]
    assert frame.loc["SAM-000002", "breaks"] == ["expires_after_made"]
    assert frame.loc["SAM-000003", "held"] == [] and frame.loc["SAM-000003", "breaks"] == []


def test_an_empty_search_is_an_empty_dataframe(fake: FakeIki, client: Client) -> None:
    fake.on("POST", "/api/v1/entities/search", page([]))
    frame = client.entities.search().to_pandas()
    assert frame.empty
    assert {"revision", "project", "held", "breaks"} <= set(frame.columns)


# --- Runs --------------------------------------------------------------------------------------


def test_runs_take_inputs_by_hrid_id_or_entity(fake: FakeIki, client: Client) -> None:
    fake.on("GET", "/api/v1/entities/PRT-000002", entity("PRT-000002"))
    fake.on("POST", "/api/v1/actions/iki/protein.compute_properties/runs", run())
    client.runs.start(
        "iki/protein.compute_properties",
        project=PROJECT,
        inputs={
            "protein": "PRT-000002",
            "references": [entity("PRT-000003"), "00000000-0000-7000-8000-000000000004"],
        },
        params={"ph": 7.4},
        reason="Weights",
    )
    assert fake.body("POST", "/api/v1/actions/iki/protein.compute_properties/runs") == {
        "project": PROJECT,
        "inputs": {
            "protein": "00000000-0000-7000-8000-000000000002",
            "references": [
                "00000000-0000-7000-8000-000000000003",
                "00000000-0000-7000-8000-000000000004",
            ],
        },
        "params": {"ph": 7.4},
        "reason": "Weights",
        "reuse": True,
        "add_to_project": False,
        "labels": [],
        "consent": [],
    }


def test_runs_that_send_data_outside_need_consent(fake: FakeIki, client: Client) -> None:
    statement = {
        "api": "acme/hosted_models.foldcloud",
        "statement": "This sends your data to FoldCloud (fold.example.com): each Protein's "
        "sequence.",
    }
    path = "/api/v1/actions/acme/hosted_models.predict_structure/runs"
    fake.on(
        "POST", path, problem(409, statement["statement"], "consent_required", consent=[statement])
    )
    with pytest.raises(iki.ConsentRequired) as refused:
        client.runs.start("acme/hosted_models.predict_structure", project=PROJECT)
    assert refused.value.statements == [statement]
    assert refused.value.apis == ["acme/hosted_models.foldcloud"]
    assert isinstance(refused.value, iki.Conflict)
    fake.on("POST", path, run())
    client.runs.start(
        "acme/hosted_models.predict_structure", project=PROJECT, consent=refused.value.apis
    )
    assert fake.body("POST", path)["consent"] == ["acme/hosted_models.foldcloud"]

    fake.on(
        "POST",
        path,
        problem(409, "Not approved.", "outside_api_not_approved", approvers=["Ada Lovelace"]),
    )
    with pytest.raises(iki.NotApproved) as unapproved:
        client.runs.start("acme/hosted_models.predict_structure", project=PROJECT)
    assert unapproved.value.approvers == ["Ada Lovelace"]


def test_a_runs_calls_to_outside_apis(fake: FakeIki, client: Client) -> None:
    call = {"id": 1, "kind": "call", "model": "fold-2", "version": "2.1"}
    fake.on("GET", "/api/v1/runs/RUN-000001/api-calls", {"items": [call], "next_cursor": None})
    assert list(client.runs.api_calls("RUN-000001")) == [call]


def test_runs_can_run_again_rather_than_reuse_and_be_reproduced(
    fake: FakeIki, client: Client
) -> None:
    fake.on("POST", "/api/v1/actions/iki/protein.import_proteins/runs", run())
    client.runs.start("iki/protein.import_proteins", project=PROJECT, reuse=False)
    sent = fake.body("POST", "/api/v1/actions/iki/protein.import_proteins/runs")
    assert sent["reuse"] is False
    fake.on("POST", "/api/v1/runs/RUN-000001/reproduce", run("RUN-000002"))
    again = client.runs.reproduce(run(), reason="Audit")
    assert again["hrid"] == "RUN-000002"
    assert fake.body("POST", "/api/v1/runs/RUN-000001/reproduce") == {
        "reason": "Audit",
        "consent": [],
    }


def test_a_batch_runs_one_for_each(fake: FakeIki, client: Client) -> None:
    fake.on(
        "POST",
        "/api/v1/actions/iki/protein.compute_properties/runs/batch",
        {"runs": [run("RUN-000001"), run("RUN-000002")]},
    )
    started = client.runs.start_each(
        "iki/protein.compute_properties",
        [entity("PRT-000001"), entity("PRT-000002")],
        each="protein",
        project=PROJECT,
    )
    assert [r["hrid"] for r in started] == ["RUN-000001", "RUN-000002"]
    assert fake.body("POST", "/api/v1/actions/iki/protein.compute_properties/runs/batch")[
        "entities"
    ] == ["00000000-0000-7000-8000-000000000001", "00000000-0000-7000-8000-000000000002"]


def test_waiting_follows_runs_until_they_end(fake: FakeIki, client: Client) -> None:
    fake.on("GET", "/api/v1/runs/RUN-000001", run(), run(status="running"), run(status="succeeded"))
    fake.on("GET", "/api/v1/runs/RUN-000002", run("RUN-000002", status="succeeded"))
    ended = client.runs.wait(["RUN-000001", run("RUN-000002")])
    assert [r["status"] for r in ended] == ["succeeded", "succeeded"]
    # It asks again sooner at first, then every couple of seconds.
    assert fake.slept == [1.0, 1.5]
    assert client.runs.wait("RUN-000002")["hrid"] == "RUN-000002"


def test_waiting_for_a_run_named_twice_follows_it_once(fake: FakeIki, client: Client) -> None:
    fake.on("GET", "/api/v1/runs/RUN-000001", run(status="running"), run(status="succeeded"))
    fake.on("GET", "/api/v1/runs/RUN-000002", run("RUN-000002", status="succeeded"))
    ended = client.runs.wait(
        ["RUN-000001", run("RUN-000002"), run("RUN-000001"), "RUN-000002"], timeout=30
    )
    assert [r["hrid"] for r in ended] == ["RUN-000001", "RUN-000002", "RUN-000001", "RUN-000002"]
    assert [r["status"] for r in ended] == ["succeeded"] * 4
    assert len(fake.sent("GET", "/api/v1/runs/RUN-000001")) == 2
    assert len(fake.sent("GET", "/api/v1/runs/RUN-000002")) == 1


def test_waiting_for_a_run_that_fails_raises(fake: FakeIki, client: Client) -> None:
    failed = run(status="failed", error={"message": "ValueError: no weights"})
    fake.on("GET", "/api/v1/runs/RUN-000001", failed)
    with pytest.raises(iki.RunFailed, match="RUN-000001 failed: ValueError: no weights") as raised:
        client.runs.wait("RUN-000001")
    assert raised.value.run["status"] == "failed"
    assert client.runs.wait("RUN-000001", check=False)["status"] == "failed"


def test_waiting_longer_than_allowed_raises(fake: FakeIki, client: Client) -> None:
    fake.on("GET", "/api/v1/runs/RUN-000001", run(status="running"))
    with pytest.raises(iki.TimedOut, match="Still going after 0 s: RUN-000001"):
        client.runs.wait("RUN-000001", timeout=0)


def test_a_log_is_read_whole(fake: FakeIki, client: Client) -> None:
    fake.on("GET", "/api/v1/runs/RUN-000001/attempts", page([{"attempt": 1}, {"attempt": 2}]))

    def log(request: httpx2.Request) -> httpx2.Response:
        offset = int(request.url.params["offset"])
        text = "Weighing ABL1\nDone\n"
        part = text[offset : offset + 14]
        more = offset + 14 < len(text)
        body = {"text": part, "more": more, "next_offset": offset + len(part)}
        return httpx2.Response(200, json=body)

    fake.on("GET", "/api/v1/runs/RUN-000001/attempts/2/log", log)
    assert client.runs.log("RUN-000001") == "Weighing ABL1\nDone\n"


def test_runs_make_a_dataframe(fake: FakeIki, client: Client) -> None:
    fake.on(
        "GET",
        "/api/v1/runs",
        page([run(status="succeeded", metrics={"seconds": 4}), run("RUN-000002", status="failed")]),
    )
    frame = client.runs.list(project=PROJECT, status="succeeded").to_pandas()
    assert list(frame.index) == ["RUN-000001", "RUN-000002"]
    assert frame.loc["RUN-000001", "requested_by"] == "Rosalind Franklin"
    assert frame.loc["RUN-000001", "inputs"] == ["PRT-000001"]
    assert frame.loc["RUN-000001", "metrics.seconds"] == 4
    assert fake.sent("GET", "/api/v1/runs")[0].url.params["status"] == "succeeded"


# --- Files -------------------------------------------------------------------------------------


def test_a_failed_registration_reports_the_verifiers_reason(fake: FakeIki, client: Client) -> None:
    checking = {"id": "r1", "name": "run.csv", "state": "checking", "file": None}
    fake.on("POST", "/api/v1/registrations", checking)
    fake.on(
        "GET",
        "/api/v1/registrations/r1",
        {**checking, "state": "failed", "error": "The pinned object version was removed."},
    )
    with pytest.raises(iki.IkiError, match="The pinned object version was removed"):
        client.files.register("s3://lab-data/runs/run.csv", project=PROJECT)
    assert len(fake.sent("POST", "/api/v1/registrations")) == 1


def test_registration_waits_for_the_scan_and_returns_a_held_files_finding(
    fake: FakeIki, client: Client
) -> None:
    checking = {"id": "r1", "name": "run.csv", "state": "checking", "file": None}
    file = {"id": "f1", "scan": {"state": "infected", "usable": False, "reason": "Test finding"}}
    fake.on("POST", "/api/v1/registrations", checking)
    fake.on(
        "GET",
        "/api/v1/registrations/r1",
        {**checking, "state": "scanning"},
        {**checking, "state": "ready", "file": file},
    )
    registered = client.files.register("s3://lab-data/runs/run.csv", project=PROJECT)
    assert registered["id"] == "f1"
    assert registered["scan"]["state"] == "infected"
    assert not registered["scan"]["usable"]
    assert registered["scan"]["reason"] == "Test finding"
    assert len(fake.sent("GET", "/api/v1/registrations/r1")) == 2


def test_registration_timeout_leaves_the_version_being_checked(
    fake: FakeIki, client: Client
) -> None:
    fake.on(
        "POST",
        "/api/v1/registrations",
        {"id": "r1", "name": "run.csv", "state": "checking", "file": None},
    )
    with pytest.raises(iki.TimedOut, match="/registrations/r1"):
        client.files.register("s3://lab-data/runs/run.csv", project=PROJECT, timeout=0)
    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    ("declared", "sent"),
    [(None, "application/octet-stream"), ("chemical/x-pdb", "chemical/x-pdb")],
)
def test_an_empty_upload_is_already_ready(
    fake: FakeIki, client: Client, tmp_path: Path, declared: str | None, sent: str
) -> None:
    path = tmp_path / "empty.pdb"
    path.touch()
    digest = hashlib.sha256(b"").hexdigest()
    record = {"id": "f1", "name": path.name, "size": 0, "sha256": digest}
    fake.on(
        "POST",
        "/api/v1/uploads",
        {"id": "u1", "state": "ready", "file": record, "parts": [], "part_count": 0},
    )

    assert client.files.upload(path, project=PROJECT, media_type=declared) == record
    assert fake.body("POST", "/api/v1/uploads") == {
        "project": PROJECT,
        "name": path.name,
        "media_type": sent,
        "size": 0,
        "sha256": digest,
    }
    assert len(fake.requests) == 1


class _Pipe(io.BytesIO):
    """A binary stream that supports only bounded reads and may return short chunks."""

    def seekable(self) -> bool:
        return False

    def seek(self, pos: int, whence: int = 0, /) -> int:
        raise io.UnsupportedOperation("cannot seek")

    def read(self, size: int | None = -1, /) -> bytes:
        assert size is not None and size >= 0
        return super().read(min(size, 2))


@pytest.mark.parametrize(
    ("acquired", "sent"),
    [
        (date(2026, 3, 14), "2026-03-14"),
        (
            datetime(2026, 3, 14, 9, 21, tzinfo=timezone(timedelta(hours=1))),
            "2026-03-14T09:21:00+01:00",
        ),
        ("2026-03-14T09:21", "2026-03-14T09:21"),
    ],
)
def test_an_upload_says_when_its_data_was_acquired(
    fake: FakeIki, client: Client, tmp_path: Path, acquired: date | str, sent: str
) -> None:
    # For a file whose format doesn't record it, such as a plate reader's CSV (ADR 0177).
    path = tmp_path / "plate-7.csv"
    path.touch()
    record = {"id": "f1", "name": path.name, "size": 0, "sha256": hashlib.sha256().hexdigest()}
    fake.on(
        "POST",
        "/api/v1/uploads",
        {"id": "u1", "state": "ready", "file": record, "parts": [], "part_count": 0},
    )

    assert client.files.upload(path, project=PROJECT, acquired=acquired) == record
    assert fake.body("POST", "/api/v1/uploads")["acquired_at"] == sent


def test_upload_sends_parts_to_storage_and_renews_an_expired_part(
    fake: FakeIki, client: Client
) -> None:
    stream = _Pipe(b"skipabcdef")
    assert stream.read(2) + stream.read(2) == b"skip"
    digest = hashlib.sha256(b"abcdef").hexdigest()
    record = {"id": "f1", "size": 6, "sha256": digest}
    target = "https://files.example.com/staging/u1"
    first = {"number": 1, "size": 4, "url": f"{target}?part=1&sig=one"}
    second = {"number": 2, "size": 2, "url": f"{target}?part=2&sig=two"}
    fake.on(
        "POST",
        "/api/v1/uploads",
        httpx2.Response(
            201,
            json={
                "id": "u1",
                "state": "pending",
                "file": None,
                "parts": [first, second],
                "part_count": 2,
                "part_size": 4,
            },
            headers={"Set-Cookie": "session=private; Domain=.example.com; Path=/; Secure"},
        ),
    )

    def store(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(403 if request.url.params["sig"] == "two" else 200)

    fake.on("PUT", "/staging/u1", store)
    fake.on("POST", "/api/v1/uploads/u1/parts", [{**second, "url": f"{target}?part=2&sig=fresh"}])
    checking = {"id": "u1", "state": "checking", "file": None}
    fake.on("POST", "/api/v1/uploads/u1/complete", checking)
    fake.on(
        "GET",
        "/api/v1/uploads/u1",
        checking,
        {**checking, "state": "scanning"},
        {**checking, "state": "ready", "file": record},
    )

    assert client.files.upload(stream, name="run.bin", project=PROJECT) == record
    assert not stream.closed
    assert fake.body("POST", "/api/v1/uploads") == {
        "project": PROJECT,
        "name": "run.bin",
        "media_type": "application/octet-stream",
        "size": 6,
        "sha256": digest,
    }
    assert fake.body("POST", "/api/v1/uploads/u1/parts") == {"numbers": [2]}
    puts = fake.sent("PUT", "/staging/u1")
    assert sorted((r.url.params["sig"], r.content, r.headers["content-length"]) for r in puts) == [
        ("fresh", b"ef", "2"),
        ("one", b"abcd", "4"),
        ("two", b"ef", "2"),
    ]
    for req in puts:
        assert req.url.host == "files.example.com"
        assert "Authorization" not in req.headers
        assert "Cookie" not in req.headers


def offer_one_part(fake: FakeIki) -> None:
    part = {"number": 1, "size": 1, "url": "https://files.example.com/staging/u1?sig=one"}
    fake.on(
        "POST",
        "/api/v1/uploads",
        {"id": "u1", "state": "pending", "parts": [part], "part_size": 1, "part_count": 1},
    )


def test_a_finished_upload_reports_lost_file_access(fake: FakeIki, client: Client) -> None:
    offer_one_part(fake)
    fake.on("PUT", "/staging/u1", httpx2.Response(200))
    fake.on("POST", "/api/v1/uploads/u1/complete", {"state": "checking"})
    fake.on(
        "GET",
        "/api/v1/uploads/u1",
        {"state": "scanning", "file": None, "error": ""},
        {"state": "ready", "file": None, "error": ""},
    )
    with pytest.raises(iki.IkiError, match=r"finished.*no longer have access"):
        client.files.upload(io.BytesIO(b"x"), project=PROJECT, name="run.bin")


def test_a_failed_part_is_retried_then_cancelled(fake: FakeIki, client: Client) -> None:
    offer_one_part(fake)
    fake.on("PUT", "/staging/u1", httpx2.Response(503))
    fake.on("DELETE", "/api/v1/uploads/u1", httpx2.Response(204))
    with pytest.raises(iki.IkiError, match=r"part 1.*503"):
        client.files.upload(io.BytesIO(b"x"), project=PROJECT, name="run.bin")
    assert [r.content for r in fake.sent("PUT", "/staging/u1")] == [b"x"] * 4
    assert len(fake.sent("DELETE", "/api/v1/uploads/u1")) == 1
    assert not fake.sent("POST", "/api/v1/uploads/u1/complete")


def test_timing_out_leaves_iki_checking_the_upload(fake: FakeIki, client: Client) -> None:
    offer_one_part(fake)
    fake.on("PUT", "/staging/u1", httpx2.Response(200))
    fake.on("POST", "/api/v1/uploads/u1/complete", {"id": "u1", "state": "checking", "file": None})
    with pytest.raises(iki.TimedOut, match="u1"):
        client.files.upload(io.BytesIO(b"x"), project=PROJECT, name="run.bin", timeout=0)
    assert not fake.sent("DELETE", "/api/v1/uploads/u1")


def test_a_timed_out_upload_is_waited_for_again_without_sending_it_twice(
    fake: FakeIki, client: Client
) -> None:
    offer_one_part(fake)
    fake.on("PUT", "/staging/u1", httpx2.Response(200))
    fake.on("POST", "/api/v1/uploads/u1/complete", {"id": "u1", "state": "scanning", "file": None})
    with pytest.raises(iki.TimedOut) as raised:
        client.files.upload(io.BytesIO(b"x"), project=PROJECT, name="run.txt", timeout=0)
    assert raised.value.pending == {"upload": "u1"}
    file = {"id": "f1", "name": "run.txt", "scan": {"state": "clean", "usable": True}}
    fake.on(
        "GET",
        "/api/v1/uploads/u1",
        {"id": "u1", "state": "scanning", "file": None, "error": ""},
        {"id": "u1", "state": "ready", "file": file, "error": ""},
    )
    assert client.files.wait(raised.value.pending)["id"] == "f1"
    assert len(fake.sent("PUT", "/staging/u1")) == 1
    assert len(fake.sent("POST", "/api/v1/uploads")) == 1


def test_a_timed_out_registration_is_waited_for_again(fake: FakeIki, client: Client) -> None:
    fake.on("POST", "/api/v1/registrations", {"id": "r1", "name": "run.txt", "state": "scanning"})
    with pytest.raises(iki.TimedOut) as raised:
        client.files.register("s3://lab-data/runs/run.txt", project=PROJECT, timeout=0)
    assert raised.value.pending == {"registration": "r1"}
    fake.on(
        "GET",
        "/api/v1/registrations/r1",
        {"id": "r1", "state": "ready", "file": {"id": "f1"}, "error": ""},
    )
    assert client.files.wait({"registration": "r1"})["id"] == "f1"
    with pytest.raises(ValueError, match="pending upload or registration"):
        client.files.wait({"run": "r1"})


def downloadable(fake: FakeIki, content: bytes, sha256: str, name: str = "abl1.pdb") -> None:
    fake.on("GET", "/api/v1/files/f1", {"id": "f1", "name": name, "sha256": sha256})
    fake.on(
        "GET",
        "/api/v1/files/f1/content",
        httpx2.Response(302, headers={"Location": "https://files.example.com/abl1?sig=x"}),
    )
    fake.on("GET", "/abl1", httpx2.Response(200, content=content))


def test_entities_say_where_they_were_imported_from(fake: FakeIki, client: Client) -> None:
    record: dict[str, Any] = {"source": "NCBI Nucleotide", "id": "L09137", "version": "L09137.2"}
    fake.on("GET", "/api/v1/entities/DNA-000001/origins", page([record]))
    (found,) = client.entities.origins("DNA-000001")
    assert (found["source"], found["version"]) == ("NCBI Nucleotide", "L09137.2")


def test_files_download_from_the_signed_url_without_ikis_token(
    fake: FakeIki, client: Client, tmp_path: Path
) -> None:
    content = b"HEADER    TEST\nEND\n"
    downloadable(fake, content, hashlib.sha256(content).hexdigest())
    client._http.cookies.set("sessionid", "test-session", domain=".example.com")
    saved = client.files.download("f1", tmp_path)
    assert Path(saved) == tmp_path / "abl1.pdb"
    assert Path(saved).read_bytes() == content
    signed = fake.sent("GET", "/abl1")[0]
    assert signed.url.host == "files.example.com"
    assert "Authorization" not in signed.headers
    assert "Cookie" not in signed.headers


def test_signed_download_refuses_an_https_downgrade(
    fake: FakeIki, client: Client, tmp_path: Path
) -> None:
    downloadable(fake, b"x", "")
    fake.on(
        "GET",
        "/api/v1/files/f1/content",
        httpx2.Response(302, headers={"Location": "http://files.example.com/abl1?sig=x"}),
    )
    with pytest.raises(iki.IkiError, match="downgrades HTTPS"):
        client.files.download("f1", tmp_path)
    assert not fake.sent("GET", "/abl1")


def test_signed_download_follows_one_location(
    fake: FakeIki, client: Client, tmp_path: Path
) -> None:
    downloadable(fake, b"x", "")
    fake.on("GET", "/abl1", httpx2.Response(302, headers={"Location": "/another"}))
    with pytest.raises(iki.IkiError, match=r"refused the download of f1 \(302\)"):
        client.files.download("f1", tmp_path)
    assert not fake.sent("GET", "/another")


@pytest.mark.parametrize(
    ("name", "kept"),
    [("../../.bashrc", ".bashrc"), ("..\\..\\evil.pdb", "evil.pdb"), ("..", "f1"), ("", "f1")],
)
def test_a_files_name_cant_put_it_outside_the_folder(
    fake: FakeIki, client: Client, tmp_path: Path, name: str, kept: str
) -> None:
    folder = tmp_path / "downloads"
    folder.mkdir()
    downloadable(fake, b"x", hashlib.sha256(b"x").hexdigest(), name=name)
    assert Path(client.files.download("f1", folder)) == folder / kept
    assert [p.name for p in folder.iterdir()] == [kept]


def test_a_refused_download_says_so_and_leaves_nothing(
    fake: FakeIki, client: Client, tmp_path: Path
) -> None:
    folder = tmp_path / "downloads"
    folder.mkdir()
    downloadable(fake, b"x", hashlib.sha256(b"x").hexdigest())
    fake.on("GET", "/abl1", httpx2.Response(403, content=b"<Error>Request has expired</Error>"))
    with pytest.raises(iki.IkiError, match=r"refused the download of f1 \(403\)"):
        client.files.download("f1", folder)
    assert list(folder.iterdir()) == []


def test_a_damaged_download_isnt_kept(fake: FakeIki, client: Client, tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    downloadable(fake, b"truncated", hashlib.sha256(b"the real thing").hexdigest())
    with pytest.raises(iki.IkiError, match="damaged"):
        client.files.download("f1", downloads / "abl1.pdb")
    assert list(downloads.iterdir()) == []


def kept_artifacts(fake: FakeIki, content: bytes) -> None:
    sha = hashlib.sha256(content).hexdigest()
    fake.on(
        "GET",
        "/api/v1/runs/RUN-000001/artifacts",
        page(
            [
                {
                    "id": "a1",
                    "attempt": 1,
                    "path": "alignment.txt",
                    "name": "alignment.txt",
                    "sha256": sha,
                    "state": "kept",
                },
                {
                    "id": "a2",
                    "attempt": 2,
                    "path": "alignment.txt",
                    "name": "alignment.txt",
                    "sha256": sha,
                    "state": "kept",
                },
            ]
        ),
    )
    for artifact in ("a1", "a2"):
        fake.on(
            "GET",
            f"/api/v1/runs/RUN-000001/artifacts/{artifact}/content",
            httpx2.Response(302, headers={"Location": f"https://files.example.com/{artifact}"}),
        )
        fake.on("GET", f"/{artifact}", httpx2.Response(200, content=content))


def test_a_runs_artifacts_download_the_latest_attempts_unless_told(
    fake: FakeIki, client: Client, tmp_path: Path
) -> None:
    kept_artifacts(fake, b"X-1 MKV\n")

    saved = client.runs.download_artifact("RUN-000001", "alignment.txt", tmp_path)

    assert Path(saved) == tmp_path / "alignment.txt"
    assert Path(saved).read_bytes() == b"X-1 MKV\n"
    assert fake.sent("GET", "/a2") and not fake.sent("GET", "/a1")
    first = client.runs.download_artifact(
        "RUN-000001", "alignment.txt", tmp_path / "1.txt", attempt=1
    )
    assert Path(first).read_bytes() == b"X-1 MKV\n" and fake.sent("GET", "/a1")
    assert [a["attempt"] for a in client.runs.artifacts({"hrid": "RUN-000001"})] == [1, 2]
    with pytest.raises(iki.IkiError, match=r"RUN-000001 kept no artifact out\.log"):
        client.runs.download_artifact("RUN-000001", "out.log", tmp_path)


def test_an_artifact_past_its_retention_is_gone(
    fake: FakeIki, client: Client, tmp_path: Path
) -> None:
    kept_artifacts(fake, b"x")
    folder = tmp_path / "downloads"
    folder.mkdir()
    gone = {"status": 410, "code": "artifact_deleted", "detail": "alignment.txt was deleted."}
    fake.on("GET", "/api/v1/runs/RUN-000001/artifacts/a2/content", httpx2.Response(410, json=gone))

    with pytest.raises(iki.ApiError, match="was deleted"):
        client.runs.download_artifact("RUN-000001", "alignment.txt", folder)
    assert list(folder.iterdir()) == []


# --- The module's shortcuts --------------------------------------------------------------------


def test_the_module_works_through_one_client(
    fake: FakeIki, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_with(config)
    auth._remember_url(URL, config)
    made = fake.client(auth.Store(config))
    monkeypatch.setattr(iki, "_default", made)
    fake.on("POST", "/api/v1/actions/iki/protein.compute_properties/runs", run())
    fake.on("GET", "/api/v1/runs/RUN-000001", run(status="succeeded"))
    started = iki.run(
        "iki/protein.compute_properties", project=PROJECT, inputs={"protein": entity()}
    )
    assert iki.wait(started)["status"] == "succeeded"
    assert iki.entities is made.entities
    assert iki.client() is made
    with pytest.raises(AttributeError):
        iki.nothing  # noqa: B018


# --- Costs -------------------------------------------------------------------------------------

SUMMARY = {
    "since": "2026-11-01",
    "until": "2026-12-01",
    "by": "team",
    "totals": [{"currency": "USD", "amount": 41.5, "seconds": 90000.0}],
    "groups": [],
}


def test_costs_sum_up_a_period_and_download_their_records(fake: FakeIki, client: Client) -> None:
    fake.on("GET", "/api/v1/compute/costs", SUMMARY)
    assert client.costs.summary(by="team", since=date(2026, 11, 1)) == SUMMARY
    (sent,) = fake.sent("GET", "/api/v1/compute/costs")
    assert dict(sent.url.params) == {"by": "team", "since": "2026-11-01"}
    fake.on(
        "GET",
        "/api/v1/compute/costs/records.csv",
        httpx2.Response(
            200,
            text="kind,run,amount\r\nrun,RUN-000001,0.4200\r\nidle,,0.0100\r\n",
            headers={"Content-Type": "text/csv; charset=utf-8"},
        ),
    )
    assert client.costs.records(since="2026-11-01", until="2026-12-01") == [
        {"kind": "run", "run": "RUN-000001", "amount": "0.4200"},
        {"kind": "idle", "run": "", "amount": "0.0100"},
    ]


def test_a_projects_costs_by_its_slug(fake: FakeIki, client: Client) -> None:
    project = "00000000-0000-7000-8000-000000000a01"
    fake.on("GET", "/api/v1/projects", {"items": [{"id": project, "slug": "kinase-atlas"}]})
    fake.on("GET", f"/api/v1/projects/{project}/costs", {**SUMMARY, "by": "action"})
    assert client.projects.costs("kinase-atlas")["by"] == "action"
    (sent,) = fake.sent("GET", f"/api/v1/projects/{project}/costs")
    assert dict(sent.url.params) == {"by": "action"}


def test_budgets_say_what_has_been_spent(fake: FakeIki, client: Client) -> None:
    fake.on("GET", "/api/v1/compute/budgets", page([{"id": "b1", "amount": 50.0, "spent": 40.25}]))
    assert client.costs.budgets().first()["spent"] == 40.25  # type: ignore[index]


# --- Across the graph --------------------------------------------------------------------------


def _walk_page(hrids: list[str], cursor: str | None, **extra: Any) -> dict[str, Any]:
    items = [
        {
            "entity": {
                "id": entity(h)["id"],
                "hrid": h,
                "title": h,
                "type": "iki/samples.Sample" if h.startswith("SAM-") else "iki/inventory.Tube",
                "archived": False,
                "entry": None,
                "project": {"id": "p", "name": "Kinase Atlas", "slug": "k"},
            },
            "depth": 1,
            "path": [
                {
                    "kind": "reference",
                    "field": "source",
                    "run": None,
                    "hidden_run": False,
                    "file": None,
                    "revision": None,
                    "entity": entity(h),
                }
            ],
        }
        for h in hrids
    ]
    return {
        "root": entity("TUB-000001"),
        "direction": "downstream",
        "depth": 10,
        "items": items,
        "next_cursor": cursor,
        "total": 3,
        "hidden": 0,
        "beyond": False,
        **extra,
    }


def test_downstream_walks_a_page_at_a_time_and_sums_up(fake: FakeIki, client: Client) -> None:
    path = "/api/v1/entities/TUB-000001/downstream"
    fake.on(
        "GET",
        path,
        _walk_page(["SAM-000001", "SAM-000002"], "next"),
        _walk_page(["SAM-000003"], None),
    )
    found = client.entities.downstream("TUB-000001", type="iki/samples.Sample", depth=4)
    assert [i["entity"]["hrid"] for i in found] == ["SAM-000001", "SAM-000002", "SAM-000003"]
    first, second = fake.sent("GET", path)
    assert first.url.params.get_list("type") == ["iki/samples.Sample"]
    assert (first.url.params["depth"], first.url.params["limit"]) == ("4", "50")
    assert second.url.params["cursor"] == "next"
    assert found.summary()["total"] == 3


def test_upstream_as_a_frame_and_as_csv(fake: FakeIki, client: Client) -> None:
    fake.on("GET", "/api/v1/entities/SAM-000004/upstream", _walk_page(["TUB-000001"], None))
    frame = client.entities.upstream("SAM-000004", edges=["reference"]).to_pandas()
    assert list(frame.index) == ["TUB-000001"]
    assert frame.loc["TUB-000001", "path"] == "source TUB-000001"
    assert frame.loc["TUB-000001", "project"] == "Kinase Atlas"
    (sent,) = fake.sent("GET", "/api/v1/entities/SAM-000004/upstream")
    assert sent.url.params.get_list("edges") == ["reference"]
    fake.on(
        "GET",
        "/api/v1/entities/TUB-000001/downstream.csv",
        httpx2.Response(
            200, text="hrid,depth\r\nSAM-000001,1\r\n", headers={"Content-Type": "text/csv"}
        ),
    )
    assert client.entities.downstream_csv("TUB-000001", depth=2) == [
        {"hrid": "SAM-000001", "depth": "1"}
    ]


def test_signatures_are_read_and_checked_never_given(fake: FakeIki, client: Client) -> None:
    """ADR 0126: the SDK lists a record's signatures and verifies one; it has no way to
    sign, which takes a person signing in again in the web app."""
    signature = "01a1ee10-0000-7000-8000-000000000001"
    fake.on("GET", "/api/v1/entities/LOT-000001/signatures", {"signatures": [], "missing": []})
    assert client.signatures.list("LOT-000001", verify=True) == {"signatures": [], "missing": []}
    assert dict(fake.requests[-1].url.params) == {"verify": "true"}
    fake.on("GET", f"/api/v1/signatures/{signature}/verification", {"valid": True, "checks": []})
    assert client.signatures.verify(signature)["valid"] is True
    assert not [name for name in dir(client.signatures) if name.startswith("sign")]


def test_every_list_is_read_a_page_of_limit_at_a_time(client: Client) -> None:
    """ADR 0291: every method that reads a list takes ``limit``, 50 by default as the API's
    own, and returns a :class:`Results` that follows the pages; none says ``page_size``."""
    parts = [
        getattr(client, name) for name in vars(client) if isinstance(getattr(client, name), _Part)
    ]
    checked = 0
    for part in parts:
        for name, method in inspect.getmembers(part, inspect.ismethod):
            if name.startswith("_"):
                continue
            signature = inspect.signature(method)
            assert "page_size" not in signature.parameters, f"{type(part).__name__}.{name}"
            returned = str(signature.return_annotation)
            if returned.startswith("Results[") or "Walk" in returned:
                assert signature.parameters["limit"].default == 50, f"{type(part).__name__}.{name}"
                checked += 1
    assert checked >= 20
