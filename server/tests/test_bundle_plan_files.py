"""An import confirms file storage decisions, outside references and the reviewed archive."""

import gzip
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest
from api_client import Api
from botocore.exceptions import ClientError
from django.db.models import Model
from org_world import World
from test_bundle_imports import PDB, applied, bundle_of, protein, upload, world
from test_bundles import LIMIT, TRUSTED, stored
from test_registrations import lab, put

from iki_server.audit.models import AuditEvent
from iki_server.bundles import imports, reading
from iki_server.bundles.models import BundleImport
from iki_server.bundles.review import ReviewedPlan
from iki_server.entities import services
from iki_server.entities.models import Entity
from iki_server.files import storage
from iki_server.files.models import Blob, File
from iki_server.org.models import Project, ProjectMembership, ProjectRole

pytestmark = pytest.mark.django_db
__all__ = ["lab", "world"]


def structure(world: World, pdb: File) -> Model:
    return services.create(
        world.users["admin"],
        "acme/protein.ProteinStructure",
        world.project,
        {"structure": str(pdb.pk), "method": "xray"},
    ).entity


def registered_blob(uri: str, version: str) -> Blob:
    return Blob.objects.create(
        registered_external=True,
        storage_uri=uri,
        object_version=version,
        sha256=hashlib.sha256(PDB).hexdigest(),
        size=len(PDB),
    )


def registered_bundle(world: World, tmp_path: Path, uri: str, version: str) -> Path:
    def elsewhere() -> list[Model]:
        blob = registered_blob(uri, version)
        pdb = File.objects.create(
            project=world.project, blob=blob, name="registered.pdb", media_type="chemical/x-pdb"
        )
        return [structure(world, pdb)]

    return bundle_of(world, tmp_path, elsewhere)


def test_a_bundle_registers_the_verified_version_of_an_external_file(
    world: World, tmp_path: Path, lab: dict[str, Any]
) -> None:
    uri, first = put(lab, "source.pdb", PDB)
    path = registered_bundle(world, tmp_path, uri, first)
    put(lab, "source.pdb", b"A newer version")
    api = Api(world.browser("admin"))
    plan = upload(api, world.project, path)
    assert plan["plan"]["applicable"], plan
    done = applied(api, plan)
    assert done["state"] == "applied", done
    blob = File.objects.get(name="registered.pdb").blob
    assert blob.registered_external
    assert (blob.storage_uri, blob.object_version) == (uri, first)
    assert (blob.size, blob.sha256) == (len(PDB), hashlib.sha256(PDB).hexdigest())


@pytest.mark.parametrize("unpinned", ["", "null"])
def test_a_bundle_requires_an_immutable_external_file_version(
    world: World, tmp_path: Path, lab: dict[str, Any], unpinned: str
) -> None:
    uri, _ = put(lab, "source.pdb", PDB)
    path = registered_bundle(world, tmp_path, uri, unpinned)
    plan = upload(Api(world.browser("admin")), world.project, path)
    assert plan["state"] == "refused" and plan["plan"] is None, plan
    assert "immutable object version" in plan["error"]
    audit = AuditEvent.objects.get(action="bundle.import_refused", data__import=plan["id"])
    assert audit.data["why"] == plan["error"]
    assert not File.objects.filter(name="registered.pdb").exists()


def test_bundled_registration_requires_a_currently_approved_location(
    world: World, tmp_path: Path, lab: dict[str, Any], settings: Any
) -> None:
    uri, version = put(lab, "source.pdb", PDB)
    path = registered_bundle(world, tmp_path, uri, version)
    # The destination registered this version while the location was approved.
    registered_blob(uri, version)
    settings.IKI_FILES_REGISTER_LOCATIONS = []
    plan = upload(Api(world.browser("admin")), world.project, path)
    assert plan["state"] == "planned" and not plan["plan"]["applicable"], plan
    (problem,) = plan["plan"]["problems"]
    assert "registered.pdb" in problem and "approved" in problem


@pytest.mark.parametrize(("content", "reason"), [(PDB + b"x", "size"), (PDB.lower(), "SHA-256")])
def test_bundled_registration_checks_the_external_bytes_before_creating_a_file(
    world: World, tmp_path: Path, lab: dict[str, Any], content: bytes, reason: str
) -> None:
    uri, version = put(lab, "source.pdb", content)
    path = registered_bundle(world, tmp_path, uri, version)
    api = Api(world.browser("admin"))
    plan = upload(api, world.project, path)
    assert plan["plan"]["applicable"], plan
    done = applied(api, plan)
    assert done["state"] == "failed" and reason in done["error"], done
    audit = AuditEvent.objects.get(action="bundle.import_failed", data__import=done["id"])
    assert audit.data["why"] == done["error"]
    assert not Blob.objects.filter(storage_uri=uri).exists()
    assert not File.objects.filter(name="registered.pdb").exists()


@pytest.mark.parametrize("readable", [True, False], ids=["wrong_digest", "provider_refuses_read"])
def test_reusing_a_local_external_blob_for_carried_bytes_checks_its_content(
    world: World,
    tmp_path: Path,
    lab: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    readable: bool,
) -> None:
    path = bundle_of(
        world,
        tmp_path,
        lambda: [structure(world, stored(world.project, PDB, "carried.pdb"))],
    )
    uri, version = put(lab, "local.pdb", PDB.lower())
    registered_blob(uri, version)
    api = Api(world.browser("admin"))
    plan = upload(api, world.project, path)
    assert plan["plan"]["applicable"], plan
    if not readable:
        denied = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": f"Cannot read {uri} at {version}"}},
            "GetObject",
        )
        monkeypatch.setattr(storage, "digest_object", Mock(side_effect=denied))
    done = applied(api, plan)
    assert done["state"] == "failed", done
    error = done["error"]
    assert "SHA-256" in error if readable else "could not read the pinned version" in error
    assert "carried.pdb" in error and uri not in error and version not in error
    assert "AccessDenied" not in error
    audit = AuditEvent.objects.get(action="bundle.import_failed", data__import=done["id"])
    assert audit.data["why"] == error
    assert not File.objects.filter(name="carried.pdb").exists()


def test_a_file_whose_blob_arrived_since_preview_needs_a_new_confirmation(
    world: World, tmp_path: Path
) -> None:
    path = bundle_of(
        world,
        tmp_path,
        lambda: [structure(world, stored(world.project, PDB, "bundle.pdb"))],
    )
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


def test_a_file_arrives_with_what_it_states(world: World, tmp_path: Path) -> None:
    """A lot's intact-mass run, with the instrument its mzML named where it was read (ADR
    0224): the facts and read groups come in the bundle, and the Files list here finds it
    by them. Its bytes aren't read again."""
    from test_file_metadata import GROUP, configuration, mzml

    data = mzml(configuration("IC1", GROUP))
    facts = {
        "instrument": {
            "value": "Orbitrap Eclipse",
            "stated_in": "mzML instrument configuration “IC1”, MS:1003029",
        },
        "serial": {
            "value": "FSN40152",
            "stated_in": "mzML instrument configuration “IC1”, MS:1000529",
        },
    }

    def elsewhere() -> list[Model]:
        sha = hashlib.sha256(data).hexdigest()
        storage.client().put_object(Bucket=storage.bucket(), Key=storage.blob_key(sha), Body=data)
        blob = Blob.objects.create(
            sha256=sha, size=len(data), storage_uri=storage.uri(storage.blob_key(sha))
        )
        run = File.objects.create(
            project=world.project,
            blob=blob,
            name="LOT-0042_intact.mzML",
            media_type="application/x-mzml",
            facts=facts,
            stated_text="Orbitrap Eclipse\nFSN40152",
        )
        return [
            services.create(
                world.users["admin"],
                "acme/protein.Lot",
                world.project,
                {"label": "LOT-0042", "report": str(run.pk)},
            ).entity
        ]

    path = bundle_of(world, tmp_path, elsewhere)
    archive = reading.read(path, TRUSTED, limit=LIMIT)
    (line,) = archive.lines("files.ndjson")
    assert line["facts"] == facts and line["read_groups"] == []
    api = Api(world.browser("admin"))
    done = applied(api, upload(api, world.project, path))
    assert done["state"] == "applied", done
    arrived = File.objects.get(pk=line["id"])
    assert (arrived.facts, arrived.read_groups) == (facts, [])
    assert arrived.stated_text == "Orbitrap Eclipse\nFSN40152"
    found = api.get("/files", q="fsn4015").json()["items"]
    assert [f["name"] for f in found] == ["LOT-0042_intact.mzML"]
    assert found[0]["facts"]["serial"]["value"] == "FSN40152"
