"""The client, against a fake Iki: what it sends, and what it makes of the answers."""

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pandas as pd
import pytest
from fake_iki import URL, FakeIki, config, entity, isolated, problem, store_with

import iki
from iki import auth
from iki.client import Client

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
        "action": "protein.compute_properties",
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
        client.entities.create("protein.Protein", {"sequence": "123"}, project=PROJECT)
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


# --- Entities ----------------------------------------------------------------------------------


def test_searching_goes_through_every_page(fake: FakeIki, client: Client) -> None:
    fake.on(
        "POST",
        "/api/v1/entities/search",
        page([entity("PRT-000001"), entity("PRT-000002")], "next"),
        page([entity("PRT-000003")]),
    )
    found = client.entities.search(
        "protein.Protein",
        project=PROJECT,
        where={"field": "length", "op": "gte", "value": 300},
        sort="-created_at",
        page_size=2,
    )
    assert [e["hrid"] for e in found] == ["PRT-000001", "PRT-000002", "PRT-000003"]
    sent = [json.loads(r.content) for r in fake.sent("POST", "/api/v1/entities/search")]
    assert sent[0] == {
        "type": "protein.Protein",
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


def test_entities_make_a_dataframe_a_row_each(fake: FakeIki, client: Client) -> None:
    linked = entity(
        "PRT-000002", name="SRC", length=536, parent="00000000-0000-7000-8000-000000000001"
    )
    linked["refs"] = {"00000000-0000-7000-8000-000000000001": {"hrid": "PRT-000001"}}
    fake.on(
        "POST",
        "/api/v1/entities/search",
        page([entity("PRT-000001", name="ABL1", length=1130, type="kinase"), linked]),
    )
    frame = client.entities.search("protein.Protein").to_pandas()
    assert list(frame.index) == ["PRT-000001", "PRT-000002"]
    assert frame.loc["PRT-000001", "name"] == "ABL1"
    assert frame.loc["PRT-000002", "length"] == 536
    # References read as the HRIDs they refer to.
    assert frame.loc["PRT-000002", "parent"] == "PRT-000001"
    # A field named like one of every entity's columns keeps both.
    assert frame.loc["PRT-000001", "type"] == "protein.Protein"
    assert frame.loc["PRT-000001", "values.type"] == "kinase"
    assert frame.loc["PRT-000001", "project"] == "kinase-atlas"
    assert frame["updated_at"].iloc[0] == pd.Timestamp("2026-11-02T10:30:00Z")
    assert client.entities.search("protein.Protein").to_pandas(limit=1).shape[0] == 1


def test_an_empty_search_is_an_empty_dataframe(fake: FakeIki, client: Client) -> None:
    fake.on("POST", "/api/v1/entities/search", page([]))
    frame = client.entities.search().to_pandas()
    assert frame.empty
    assert "revision" in frame.columns


# --- Runs --------------------------------------------------------------------------------------


def test_runs_take_inputs_by_hrid_id_or_entity(fake: FakeIki, client: Client) -> None:
    fake.on("GET", "/api/v1/entities/PRT-000002", entity("PRT-000002"))
    fake.on("POST", "/api/v1/actions/protein.compute_properties/runs", run())
    client.runs.start(
        "protein.compute_properties",
        project=PROJECT,
        inputs={
            "protein": "PRT-000002",
            "references": [entity("PRT-000003"), "00000000-0000-7000-8000-000000000004"],
        },
        params={"ph": 7.4},
        reason="Weights",
    )
    assert fake.body("POST", "/api/v1/actions/protein.compute_properties/runs") == {
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
    }


def test_runs_can_run_again_rather_than_reuse_and_be_reproduced(
    fake: FakeIki, client: Client
) -> None:
    fake.on("POST", "/api/v1/actions/protein.import_proteins/runs", run())
    client.runs.start("protein.import_proteins", project=PROJECT, reuse=False)
    sent = fake.body("POST", "/api/v1/actions/protein.import_proteins/runs")
    assert sent["reuse"] is False
    fake.on("POST", "/api/v1/runs/RUN-000001/reproduce", run("RUN-000002"))
    again = client.runs.reproduce(run(), reason="Audit")
    assert again["hrid"] == "RUN-000002"
    assert fake.body("POST", "/api/v1/runs/RUN-000001/reproduce") == {"reason": "Audit"}


def test_a_batch_runs_one_for_each(fake: FakeIki, client: Client) -> None:
    fake.on(
        "POST",
        "/api/v1/actions/protein.compute_properties/runs/batch",
        {"runs": [run("RUN-000001"), run("RUN-000002")]},
    )
    started = client.runs.start_each(
        "protein.compute_properties",
        [entity("PRT-000001"), entity("PRT-000002")],
        each="protein",
        project=PROJECT,
    )
    assert [r["hrid"] for r in started] == ["RUN-000001", "RUN-000002"]
    assert fake.body("POST", "/api/v1/actions/protein.compute_properties/runs/batch")[
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
    fake.on("GET", "/api/v1/runs/RUN-000001/attempts", [{"attempt": 1}, {"attempt": 2}])

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
    fake.on("GET", "/api/v1/entities/DNA-000001/origins", [record])
    (found,) = client.entities.origins("DNA-000001")
    assert (found["source"], found["version"]) == ("NCBI Nucleotide", "L09137.2")


def test_files_download_from_the_signed_url_without_ikis_token(
    fake: FakeIki, client: Client, tmp_path: Path
) -> None:
    content = b"HEADER    TEST\nEND\n"
    downloadable(fake, content, hashlib.sha256(content).hexdigest())
    saved = client.files.download("f1", tmp_path)
    assert Path(saved) == tmp_path / "abl1.pdb"
    assert Path(saved).read_bytes() == content
    signed = fake.sent("GET", "/abl1")[0]
    assert signed.url.host == "files.example.com"
    assert "Authorization" not in signed.headers


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
        ],
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
    fake.on("POST", "/api/v1/actions/protein.compute_properties/runs", run())
    fake.on("GET", "/api/v1/runs/RUN-000001", run(status="succeeded"))
    started = iki.run("protein.compute_properties", project=PROJECT, inputs={"protein": entity()})
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
    fake.on("GET", "/api/v1/compute/budgets", [{"id": "b1", "amount": 50.0, "spent": 40.25}])
    assert client.costs.budgets()[0]["spent"] == 40.25
