"""The core entity row and blobs, which every plugin type and file field builds on."""

import pytest
from django.db import IntegrityError, transaction

from iki_server.entities.models import Entity
from iki_server.files.models import Blob
from iki_server.org.models import Project, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def project() -> Project:
    return Project.objects.create(team=Team.objects.create(name="Biologics"), name="Antibodies")


def test_entities_get_their_creation_time_from_the_database(project: Project) -> None:
    entity = Entity.objects.create(type="protein.Protein", hrid="PRT-000001", owner_project=project)
    assert entity.created_at is not None
    assert str(entity) == "PRT-000001"


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"type": "Protein"}, "iki_entity_type"),
        ({"type": "protein.protein"}, "iki_entity_type"),
        ({"hrid": "prt-1"}, "iki_entity_hrid"),
        ({"hrid": "PRT-01"}, "iki_entity_hrid"),
        ({"identity_hash": "x" * 64}, "iki_entity_identity_hash"),
    ],
)
def test_the_database_checks_entity_rows(
    project: Project, values: dict[str, str], constraint: str
) -> None:
    fields = {"type": "protein.Protein", "hrid": "PRT-000001", **values}
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        Entity.objects.create(owner_project=project, **fields)


def test_hrids_are_unique(project: Project) -> None:
    Entity.objects.create(type="protein.Protein", hrid="PRT-000001", owner_project=project)
    with pytest.raises(IntegrityError), transaction.atomic():
        Entity.objects.create(type="protein.Protein", hrid="PRT-000001", owner_project=project)


def test_blobs_are_stored_once_per_content() -> None:
    blob = {"sha256": "a" * 64, "size": 1, "media_type": "text/plain"}
    Blob.objects.create(storage_uri="s3://iki/a", **blob)
    with pytest.raises(IntegrityError, match="iki_blob_content"), transaction.atomic():
        Blob.objects.create(storage_uri="s3://iki/b", **blob)
    # Registered objects are pinned by their location and version instead.
    external = {**blob, "registered_external": True, "storage_uri": "s3://lab/a"}
    Blob.objects.create(object_version="1", **external)
    Blob.objects.create(object_version="2", **external)
    with pytest.raises(IntegrityError, match="iki_blob_external_object"), transaction.atomic():
        Blob.objects.create(object_version="2", **external)
    with pytest.raises(IntegrityError, match="iki_blob_size"), transaction.atomic():
        Blob.objects.create(storage_uri="s3://iki/c", **{**blob, "sha256": "b" * 64, "size": -1})
