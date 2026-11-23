"""An import confirms file storage decisions, outside references and the reviewed archive."""

import gzip
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest
from api_client import Api
from org_world import World
from test_bundle_imports import PDB, applied, bundle_of, protein, upload, world
from test_bundles import LIMIT, TRUSTED, stored

from iki_server.audit.models import AuditEvent
from iki_server.bundles import imports, reading
from iki_server.bundles.models import BundleImport
from iki_server.bundles.review import ReviewedPlan
from iki_server.entities import services
from iki_server.entities.models import Entity
from iki_server.files import storage
from iki_server.files.models import File
from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = pytest.mark.django_db
__all__ = ["world"]


def test_a_file_whose_blob_arrived_since_preview_needs_a_new_confirmation(
    world: World, tmp_path: Path
) -> None:
    def structure() -> list[Any]:
        pdb = stored(world.project, PDB, "bundle.pdb")
        return [
            services.create(
                world.users["admin"],
                "acme/protein.ProteinStructure",
                world.project,
                {"structure": str(pdb.pk), "method": "xray"},
            ).entity
        ]

    path = bundle_of(world, tmp_path, structure)
    archive = reading.read(path, TRUSTED, limit=LIMIT)
    (source,) = archive.lines("files.ndjson")
    (record,) = archive.lines("entities.ndjson")
    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)
    # The file itself still has to be created, but its storage target has changed.
    local = stored(world.project, PDB, "locally-added.pdb")
    refreshed = applied(api, made)
    assert refreshed["state"] == "planned", refreshed
    assert refreshed["plan"] == made["plan"]
    assert refreshed["planned_at"] != made["planned_at"]
    assert "bundle.pdb" in refreshed["error"]
    assert not File.objects.filter(pk=source["id"]).exists()
    assert not Entity.objects.filter(pk=record["id"]).exists()
    assert applied(api, refreshed)["state"] == "applied"
    assert File.objects.get(pk=source["id"]).blob_id == local.blob_id


def test_a_swap_beyond_the_outside_reference_display_limit_is_still_reviewed(world: World) -> None:
    admin = world.users["admin"]
    target = Project.objects.create(team=world.team, name="Receiving lab")
    ProjectMembership.objects.create(project=target, user=admin, role=ProjectRole.ADMIN)
    first, second = protein(world, "MKG"), protein(world, "MKL")
    outside = [
        {"id": str(UUID(int=i + 1)), "hrid": None, "type": None, "why": "history"}
        for i in range(imports.OUTSIDE_SHOWN)
    ]
    outside.extend(
        {"id": str(p.entry_id), "hrid": p.hrid, "type": p.type, "why": "history"}
        for p in (first, second)
    )
    archive = Mock(spec=reading.Bundle)
    archive.lines.return_value = outside

    def review() -> ReviewedPlan:
        planner = imports._Planner(BundleImport(project=target), archive)
        shown = planner._outside()
        return ReviewedPlan({"outside": shown, "members": planner.members}, {}, {})

    services.link(admin, first, target)
    before = review()
    services.unlink(admin, first, target)
    services.link(admin, second, target)
    after = review()
    displayed = after.summary["outside"]
    assert displayed == before.summary["outside"]
    assert displayed["here"] == 1 and len(displayed["items"]) == imports.OUTSIDE_SHOWN
    difference = before.difference(after)
    assert first.hrid in difference and second.hrid in difference


def test_applying_rechecks_the_archive_hash_even_when_its_signed_contents_match(
    world: World, tmp_path: Path
) -> None:
    path = bundle_of(world, tmp_path, lambda: [protein(world, "MKH")])
    # Two valid archives of the same signed manifest, differing only in their gzip timestamp.
    tar = gzip.decompress(path.read_bytes())
    path.write_bytes(gzip.compress(tar, mtime=1))
    other_path = tmp_path / "other.tar.gz"
    other_path.write_bytes(gzip.compress(tar, mtime=2))
    original = reading.read(path, TRUSTED, limit=LIMIT)
    other = reading.read(other_path, TRUSTED, limit=LIMIT)
    assert original.trusted_as == other.trusted_as == "test"
    assert original.manifest == other.manifest and original.size == other.size
    assert original.sha256 != other.sha256

    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)
    storage.upload_file(other_path, storage.imported_key(made["upload"]), "application/gzip")
    done = applied(api, made)
    assert done["state"] == "failed" and "SHA-256" in done["error"], done
    (source,) = original.lines("entities.ndjson")
    assert not Entity.objects.filter(pk=source["id"]).exists()
    assert not AuditEvent.objects.filter(action="bundle.imported", data__import=done["id"]).exists()
    failed = AuditEvent.objects.get(action="bundle.import_failed", data__import=done["id"])
    assert failed.data["sha256"] == original.sha256
