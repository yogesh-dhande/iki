"""Agents export records as one file (ADR 0229) and download it, as the person who granted
access, through MCP."""

import hashlib
import io
from typing import Any

import pytest
import requests
from asgiref.sync import sync_to_async
from Bio import SeqIO
from org_world import World
from test_mcp import data, mcp_running, token_for, tool, world
from test_record_exports import HBA, HBB, protein

from iki_server.audit.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ["mcp_running", "world"]


async def test_an_agent_exports_records_and_downloads_them_as_its_person(
    world: World, s3: Any
) -> None:
    alpha, beta = await sync_to_async(lambda: (protein(world, HBA), protein(world, HBB)))()
    token = await token_for(world, "viewer", scope="read")
    made = data(
        await tool(
            token, "export_entities", format="protein-fasta", entities=[beta.hrid, alpha.hrid]
        )
    )
    assert made["state"] == "ready"
    assert data(await tool(token, "get_entity_export", export_id=made["id"]))["count"] == 2
    link = data(await tool(token, "get_entity_export_download_link", export_id=made["id"]))
    content = await sync_to_async(lambda: requests.get(link["url"], timeout=30).content)()
    assert hashlib.sha256(content).hexdigest() == link["sha256"] == made["sha256"]
    assert [r.id for r in SeqIO.parse(io.StringIO(content.decode()), "fasta")] == [  # type: ignore[no-untyped-call]
        beta.hrid,
        alpha.hrid,
    ]
    taken = await AuditEvent.objects.select_related("agent").aget(action="entities.exported")
    assert taken.actor_id == world.users["viewer"].pk
    assert taken.agent is not None
    # A search, too: every protein the person can read.
    found = data(
        await tool(token, "export_entities", format="protein-fasta", type="acme/protein.Protein")
    )
    assert found["count"] == 2
