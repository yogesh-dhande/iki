"""The people and registry entries selected when confirming a bundle import."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from api_client import Api
from org_world import World
from test_bundle_imports import applied, bundle_of, protein, upload, world

from iki_server.accounts.models import User
from iki_server.audit.models import AuditEvent
from iki_server.bundles import applying
from iki_server.bundles.models import ImportEntity
from iki_server.entities import services
from iki_server.entities.models import Entity, Revision
from iki_server.org import registries
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.scim import services as scim

pytestmark = pytest.mark.django_db
__all__ = ["world"]


def test_changed_author_matches_require_confirmation_with_the_same_placeholder_count(
    world: World, tmp_path: Path
) -> None:
    first, second = world.users["editor"], world.users["runner"]
    first_email, second_email = first.email, second.email

    def authors() -> list[Any]:
        saved = services.create(
            first, "acme/protein.Protein", world.project, {"sequence": "MKAGGL", "name": "Standard"}
        )
        entity = Entity.objects.get(pk=saved.entity.pk)
        services.update(second, entity, {"name": "Working standard"}, expected_revision=1)
        return [entity]

    path = bundle_of(world, tmp_path, authors)
    scim.update(second, scim.Profile(user_name="staff-2", email="staff-2@lab.example"))
    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)

    def matches(review: dict[str, Any]) -> dict[str, str | None]:
        response = api.get(f"/imports/{review['id']}/people", planned_at=review["planned_at"])
        assert response.status_code == 200, response.content
        rows = response.json()["items"]
        return {p["email"]: p["user"]["id"] if p["user"] else None for p in rows}

    before = matches(made)
    assert before[first_email] == str(first.pk) and before[second_email] is None
    assert made["plan"]["counts"]["placeholders"] == 1
    scim.update(first, scim.Profile(user_name="staff-1", email="staff-1@lab.example"))
    scim.update(second, scim.Profile(email=second_email))
    people, records, revisions = (
        User.objects.count(),
        Entity.objects.count(),
        Revision.objects.count(),
    )
    with patch.object(applying._Writer, "write", return_value={}) as write:
        done = applied(api, made)
    write.assert_not_called()
    assert done["state"] == "planned", done
    assert done["plan"]["counts"] == made["plan"]["counts"]
    after = matches(done)
    assert after[first_email] is None and after[second_email] == str(second.pk)
    assert first_email in done["error"] or second_email in done["error"], done["error"]
    assert (User.objects.count(), Entity.objects.count(), Revision.objects.count()) == (
        people,
        records,
        revisions,
    )
    assert not AuditEvent.objects.filter(project=world.project, action="bundle.imported").exists()
    # After approval, the refreshed plan attributes the two revisions as shown.
    confirmed = applied(api, done)
    assert confirmed["state"] == "applied", confirmed
    entity = Entity.objects.get(type="acme/protein.Protein", owner_project=world.project)
    creator, editor = entity.revisions.select_related("user").order_by("number")
    assert (
        creator.user is not None
        and creator.user.email == first_email
        and creator.user.is_placeholder
    )
    assert editor.user_id == second.pk


def test_rechecking_a_registration_keeps_the_new_entry_uuid_from_preview(
    world: World, tmp_path: Path
) -> None:
    admin = world.users["admin"]
    # The source registry retains a record of this identity. The exported project record
    # is new at the destination, which needs its own entry for the same sequence.
    catalog = Project.objects.create(team=world.team, name="Source catalog")
    ProjectMembership.objects.create(project=catalog, user=admin, role=ProjectRole.ADMIN)
    sequence = "MKPLTG"
    original = services.create(admin, "acme/protein.Protein", catalog, {"sequence": sequence})
    source_entry = Entity.objects.get(pk=original.entity.pk).entry_id
    path = bundle_of(world, tmp_path, lambda: [protein(world, sequence)])
    registry = registries.create_registry("Partner", "", all_teams=True, teams=(), by=admin)
    destination = Project.objects.create(team=world.team, name="Receiving lab", registry=registry)
    ProjectMembership.objects.create(project=destination, user=admin, role=ProjectRole.ADMIN)
    api = Api(world.browser("admin"))
    made = upload(api, destination, path)
    row = ImportEntity.objects.get(bundle_import_id=made["id"])
    assert row.registers and row.entry_id is not None and row.entry_id != source_entry
    planned_id = row.entry_id
    done = applied(api, made)
    assert done["state"] == "applied", done
    record = Entity.objects.get(pk=row.entity_id, owner_project=destination)
    assert record.entry_id == planned_id
    assert Entity.objects.filter(pk=planned_id, registry=registry).exists()
