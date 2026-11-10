"""File fields: entities refer to files people can read, checked against the field's rules."""

from typing import Any

import pytest
from django.db import connection, transaction
from django.db.utils import DatabaseError

from iki_server.accounts.models import User
from iki_server.core.ids import uuid7
from iki_server.entities import access, schema, services, values
from iki_server.entities.models import Entity
from iki_server.files import services as files
from iki_server.files.models import Blob, File, FileUse
from iki_server.org.models import Project, ProjectMembership, ProjectRole, Team

pytestmark = pytest.mark.django_db

GiB = 1024**3
STRUCTURE = "protein.ProteinStructure"


def person(email: str) -> User:
    return User.objects.create_user(email=email, name=email.split("@")[0])


@pytest.fixture
def lab() -> dict[str, Any]:
    team = Team.objects.create(name="Structural biology")
    first = Project.objects.create(team=team, name="Kinases")
    second = Project.objects.create(team=team, name="Proteases")
    alice, bob = person("alice@example.org"), person("bob@example.org")
    ProjectMembership.objects.create(project=first, user=alice, role=ProjectRole.EDITOR)
    ProjectMembership.objects.create(project=second, user=alice, role=ProjectRole.EDITOR)
    ProjectMembership.objects.create(project=second, user=bob, role=ProjectRole.VIEWER)
    return {"first": first, "second": second, "alice": alice, "bob": bob}


def add_file(
    project: Project,
    *,
    name: str = "1abc.pdb",
    media_type: str = "chemical/x-pdb",
    size: int = 1024,
    digit: str = "a",
) -> File:
    blob, _ = Blob.objects.get_or_create(
        sha256=digit * 64, defaults={"size": size, "storage_uri": f"s3://iki/{digit}"}
    )
    return File.objects.create(blob=blob, name=name, media_type=media_type, project=project)


def errors(user: User, project: Project, structure: object) -> list[str]:
    with pytest.raises(services.Invalid) as raised:
        services.create(user, STRUCTURE, project, {"structure": structure})
    return raised.value.errors["structure"]


def test_an_entity_refers_to_a_file_and_keeps_it_in_history(lab: dict[str, Any]) -> None:
    first = add_file(lab["first"])
    second = add_file(lab["first"], name="2xyz.cif", media_type="chemical/x-mmcif", digit="b")
    saved = services.create(lab["alice"], STRUCTURE, lab["first"], {"structure": str(first.pk)})
    assert saved.entity.structure.sha256 == "a" * 64  # type: ignore[attr-defined]
    entity = Entity.objects.get(pk=saved.entity.pk)
    assert services.current_values(saved.entity)["structure"] == first.pk
    edited = services.update(
        lab["alice"], entity, {"structure": str(second.pk)}, expected_revision=1
    )
    assert edited.revision is not None
    assert edited.revision.changed_fields == ["structure"]
    assert services.values_at(entity, 1)["structure"] == first.pk
    assert services.values_at(entity, 2)["structure"] == second.pk
    uses = FileUse.objects.filter(entity_id=entity.pk).order_by("revision")
    assert [(u.file_id, u.revision) for u in uses] == [(first.pk, 1), (second.pk, 2)]


def test_files_are_checked_against_the_fields_rules(lab: dict[str, Any]) -> None:
    alice, project = lab["alice"], lab["first"]
    text = add_file(project, name="notes.txt", media_type="text/plain", digit="c")
    assert errors(alice, project, str(text.pk)) == [
        "must be chemical/x-mmcif or chemical/x-pdb, and this file is text/plain."
    ]
    huge = add_file(project, size=3 * GiB + GiB // 10, digit="d")
    assert errors(alice, project, str(huge.pk)) == [
        "can be at most 2 GiB, and this file is 3.1 GiB."
    ]
    assert errors(alice, project, "1abc.pdb") == ["must be a file's ID: upload the file first."]
    # A file in a project Alice can't read is no file at all, to her.
    other = Project.objects.create(team=project.team, name="Secret")
    hidden = add_file(other, digit="e")
    assert errors(alice, project, str(hidden.pk)) == ["refers to no file you can read."]
    assert errors(alice, project, str(uuid7())) == ["refers to no file you can read."]


def test_media_types_may_name_a_whole_kind() -> None:
    assert values.media_type_allowed("image/png", ["image/*"])
    assert values.media_type_allowed("application/json", ["application/json"])
    assert not values.media_type_allowed("application/jsonl", ["application/json"])
    assert not values.media_type_allowed("text/png", ["image/*"])


def test_whoever_can_read_an_entity_can_read_its_files(lab: dict[str, Any]) -> None:
    alice, bob = lab["alice"], lab["bob"]
    pdb = add_file(lab["first"])
    assert not files.readable(bob).filter(pk=pdb.pk).exists()
    # Alice puts the file on an entity in a project Bob can read.
    saved = services.create(alice, STRUCTURE, lab["second"], {"structure": str(pdb.pk)})
    assert files.readable(bob).filter(pk=pdb.pk).exists()
    # Replacing it keeps the old one readable: the entity's history still shows it.
    cif = add_file(lab["first"], name="1abc.cif", media_type="chemical/x-mmcif", digit="f")
    entity = Entity.objects.get(pk=saved.entity.pk)
    services.update(alice, entity, {"structure": str(cif.pk)}, expected_revision=1)
    readable = set(files.readable(bob).values_list("pk", flat=True))
    assert {pdb.pk, cif.pk} <= readable
    # People who can read neither the file's project nor an entity using it, can't.
    assert not access.readable_files(person("carol@example.org")).exists()


@pytest.mark.parametrize("table", ["iki_blob", "iki_file", "iki_file_use"])
def test_files_never_change_whoever_asks(lab: dict[str, Any], table: str) -> None:
    pdb = add_file(lab["first"])
    services.create(lab["alice"], STRUCTURE, lab["first"], {"structure": str(pdb.pk)})
    # The table names come from the parametrize list above.
    statements = (f"UPDATE {table} SET created_at = now()", f"DELETE FROM {table}")  # noqa: S608
    for statement in statements:
        with (
            pytest.raises(DatabaseError, match="append-only"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(statement)


def test_the_schema_says_a_field_takes_a_file() -> None:
    from iki_server.entities import registry

    field = registry.types()[STRUCTURE].fields["structure"]
    out = schema.field_schema(field)
    assert "readOnly" not in out
    assert out["x-iki-file"] is True
    assert out["x-iki-media-types"] == ["chemical/x-mmcif", "chemical/x-pdb"]
    assert out["x-iki-max-size"] == 2 * GiB
