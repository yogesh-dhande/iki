"""Agents use the Files list and read its files as the person who granted access."""

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import fake_gcs
import httpx2
import pytest
import requests
from asgiref.sync import sync_to_async
from mcp import Client as McpClient
from mcp.client.streamable_http import streamable_http_client
from org_world import World
from real_server import Server, app_role, serve, server_env
from test_mcp import data, error, mcp_running, token_for, tool, world
from test_oauth import signed_in_token
from test_project_files import add, api_as, protein, sample, structure
from test_registrations import CSV, register, run_tasks
from test_runs import launch

from iki_server.audit.models import AuditEvent
from iki_server.files import scan_state, storage
from iki_server.files.models import Blob, File
from iki_server.oauth.models import Consent
from iki_server.org.models import Project

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["mcp_running", "world"]


@pytest.fixture
def mcp_server(transactional_db: None, tmp_path: Path) -> Iterator[Server]:
    """Serve the MCP client's HTTP requests. The registration test runs its own task queue."""
    with app_role() as role, serve(server_env(role, tmp_path)) as server:
        yield server


async def test_find_files_matches_the_files_list_and_counts(world: World) -> None:
    def setup() -> tuple[str, str, str]:
        trace = add(world.project, "Plate-A.pdb", acquired="2026-03-14")
        add(world.project, "Plate-B.pdb", acquired="2026-03-15")
        add(world.project, "Plate-C.csv", "text/csv", acquired="2026-03-16")
        add(world.project, "control.pdb", acquired="2026-03-14")
        add(world.project, "Plate-D.pdb", acquired="2026-04-01")
        add(world.project, "Plate-F.pdb", acquired="2026-03-13")
        add(world.archived, "Plate-E.pdb", acquired="2026-03-14")
        holder = structure(world, trace)
        structure(world, trace)
        specimen = sample(world, "S-1")
        run = launch(
            api_as(world),
            world.project,
            {
                "protein": [str(protein(world).pk)],
                "samples": [str(specimen.pk)],
                "structures": [str(holder.pk)],
            },
            "acme/protein.flag_samples",
        )
        assert run.status_code == 201, run.content
        return str(trace.pk), specimen.hrid, run.json()["hrid"]

    file_id, sample_hrid, run_hrid = await sync_to_async(setup)()
    token = await token_for(world, "viewer", scope="read")
    filters = {
        "project": str(world.project.pk),
        "q": "pLaTe",
        "format": ["iki/core.pdb", "image/*"],
        "acquired_from": "2026-03-14",
        "acquired_to": "2026-03-31",
    }
    api = await sync_to_async(api_as)(world, "viewer")

    @sync_to_async
    def browser(path: str, **params: Any) -> Any:
        response = api.get(path, **params)
        assert response.status_code == 200, response.content
        return response.json()

    first = data(await tool(token, "find_files", **filters, sort="name", limit=1))
    rest = await browser("/files", **filters, sort="name", limit=1)
    assert first["items"] == rest["items"]
    assert first["items"][0]["id"] == file_id
    assert first["next_cursor"] and rest["next_cursor"]
    count = await browser("/files/count", **filters)
    assert count == {"total": 2, "more_than_total": False}
    assert {key: first[key] for key in count} == count
    assert first["formats"] == await browser("/files/formats", **filters)
    assert {"id": "iki/core.csv", "label": "CSV", "media_type": "text/csv", "count": 1} in first[
        "formats"
    ]
    second = data(
        await tool(
            token, "find_files", **filters, sort="name", limit=1, cursor=first["next_cursor"]
        )
    )
    assert [row["name"] for row in second["items"]] == ["Plate-B.pdb"]
    assert second["next_cursor"] is None and second["total"] == 2

    for query in ({"sample": sample_hrid}, {"run": run_hrid}):
        found = data(await tool(token, "find_files", **query))
        assert found["items"] == (await browser("/files", **query))["items"]
        assert [row["id"] for row in found["items"]] == [file_id]
    detail = data(await tool(token, "get_file", file_id=file_id, limit=1))
    assert detail["file"] == await browser(f"/files/{file_id}")
    assert detail["file"]["used_by"] == {"records": 2, "runs": 1}
    records = (await browser(f"/files/{file_id}/records"))["items"]
    assert detail["records"]["items"] == records[:1]
    assert detail["records"]["items"][0]["spans"] == [
        {"first": 1, "last": None, "fields": ["structure"]}
    ]
    assert detail["runs"] == await browser(f"/files/{file_id}/runs")
    assert detail["runs"]["items"][0]["hrid"] == run_hrid
    next_records = data(
        await tool(
            token,
            "get_file",
            file_id=file_id,
            limit=1,
            records_cursor=detail["records"]["next_cursor"],
        )
    )
    assert next_records["records"] == {"items": records[1:], "next_cursor": None}
    assert next_records["runs"] == detail["runs"]


async def test_file_tools_respect_project_membership_and_the_grant(
    world: World, mcp_server: Server
) -> None:
    other = await Project.objects.acreate(team=world.team, name="Assays")
    allowed = await sync_to_async(add)(world.project, "day.csv", "text/csv")
    outside_grant = await sync_to_async(add)(world.archived, "night.csv", "text/csv")
    outside_membership = await sync_to_async(add)(other, "week.csv", "text/csv")
    browser = await sync_to_async(api_as)(world, "viewer")
    assert (await sync_to_async(browser.get)(f"/files/{outside_grant.pk}")).status_code == 200
    assert (await sync_to_async(browser.get)(f"/files/{outside_membership.pk}")).status_code == 404
    token = await token_for(world, "viewer", scope="read", projects=[world.project])
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
        McpClient(streamable_http_client(f"{mcp_server.url}/mcp", http_client=http)) as agent,
    ):

        async def call(name: str, **args: Any) -> dict[str, Any]:
            return (await agent.call_tool(name, args)).model_dump(by_alias=True)

        found = data(await call("find_files"))
        assert [row["id"] for row in found["items"]] == [str(allowed.pk)]
        assert found["total"] == 1
        assert found["formats"] == [
            {"id": "iki/core.csv", "label": "CSV", "media_type": "text/csv", "count": 1}
        ]
        for name in ("get_file", "get_file_download_link"):
            missing = error(await call(name, file_id=str(uuid4())))
            assert "(404, not_found)" in missing
            for file in (outside_grant, outside_membership):
                assert error(await call(name, file_id=str(file.pk))) == missing


async def test_a_file_link_reads_the_registered_version_until_expiry_and_names_its_agent(
    world: World, gcs: fake_gcs.FakeGcs, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bucket, key = "instrument-files", f"runs/{uuid4().hex}/plate.csv"
    uri = f"gs://{bucket}/{key}"
    settings.IKI_FILES_REGISTER_LOCATIONS = [f"gs://{bucket}/runs/"]
    version = str(gcs.write(bucket, key, CSV, "text/csv").generation)

    @sync_to_async
    def registered_file() -> str:
        api = api_as(world)
        started = register(api, world, uri, version=version)
        assert started.status_code == 202, started.content
        run_tasks()
        result = api.get(f"/registrations/{started.json()['id']}").json()
        assert result["state"] == "ready", result
        return str(result["file"]["id"])

    file_id = await registered_file()
    gcs.write(bucket, key, b"new measurements\n", "text/csv")
    client, tokens = await sync_to_async(signed_in_token)(world, "viewer", scope="read")
    token = tokens["access_token"]
    detail = data(await tool(token, "get_file", file_id=file_id))
    assert detail["file"]["scan"]["state"] == "clean"
    assert detail["file"]["origin"] == {
        "kind": "registration",
        "uri": uri,
        "version": version,
        "run": None,
    }
    before = datetime.now(UTC).replace(microsecond=0)
    link = data(await tool(token, "get_file_download_link", file_id=file_id))
    expires = datetime.fromisoformat(link["expires_at"])
    assert before + timedelta(minutes=5) <= expires <= datetime.now(UTC) + timedelta(minutes=5)
    assert link["sha256"] == hashlib.sha256(CSV).hexdigest()
    assert requests.get(link["url"], timeout=10).content == CSV

    event = await AuditEvent.objects.select_related("agent").aget(
        action="file.download_link_issued", target_id=file_id
    )
    assert (event.actor_id, event.via) == (world.users["viewer"].pk, "mcp")
    consent = await Consent.objects.aget(user=world.users["viewer"], client=client)
    assert event.agent is not None
    assert (event.agent.client_id, event.agent.grant) == (client.client_id, consent.grant)

    # Move only the object store's clock past the signed five-minute deadline.
    signed = parse_qs(urlsplit(link["url"]).query)
    assert signed["X-Goog-Expires"] == ["300"]
    signed_at = datetime.strptime(signed["X-Goog-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    deadline = signed_at + timedelta(minutes=5)
    monkeypatch.setattr(fake_gcs, "time", SimpleNamespace(time=lambda: deadline.timestamp() + 1))
    denied = requests.get(link["url"], timeout=10)
    assert denied.status_code == 403 and "expired" in denied.text


async def test_held_files_never_issue_an_mcp_download_link(
    world: World, gcs: fake_gcs.FakeGcs
) -> None:
    blob = await Blob.objects.acreate(
        sha256=hashlib.sha256(CSV).hexdigest(), size=len(CSV), storage_uri="gs://iki/plate"
    )
    file = await File.objects.acreate(
        blob=blob, project=world.project, name="plate.csv", media_type="text/csv"
    )
    reader = await token_for(world, "viewer", scope="read")
    outsider = await token_for(world, "outsider", scope="read")
    file_id = str(file.pk)
    with patch.object(storage, "download_url") as sign:
        pending = error(await tool(reader, "get_file_download_link", file_id=file_id))
        assert "(409, file_scanning)" in pending
        await sync_to_async(scan_state.record_result)(
            blob,
            state="infected",
            scanner="test",
            signature_version="1",
            reason="Eicar-Test-Signature",
        )
        held = error(await tool(reader, "get_file_download_link", file_id=file_id))
        assert "(409, file_quarantined)" in held
        missing = error(await tool(outsider, "get_file_download_link", file_id=str(uuid4())))
        assert "(404, not_found)" in missing
        assert error(await tool(outsider, "get_file_download_link", file_id=file_id)) == missing
        sign.assert_not_called()
    assert not await AuditEvent.objects.filter(
        action="file.download_link_issued", target_id=file_id
    ).aexists()
