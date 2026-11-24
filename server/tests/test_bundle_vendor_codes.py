"""A signed bundle can carry a former vendor code in a record's immutable history."""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from api_client import Api, problem
from org_world import World
from test_bundle_imports import applied, bundle_of, entities_of, upload
from test_bundle_imports import world as world

from iki_server.audit.models import AuditEvent
from iki_server.bundles import export
from iki_server.entities import services
from iki_server.entities.models import Entity, Revision

pytestmark = pytest.mark.django_db

FREEZER = "acme/protein.Freezer"
SOURCE_TAG = "OLD-SOURCE-001"
LEGACY_TAG = "  iKi:legacy-vendor"


def freezer(world: World, name: str, tag: str) -> Entity:
    saved = services.create(
        world.users["admin"], FREEZER, world.project, {"name": name, "asset_tag": tag}
    )
    return Entity.objects.get(pk=saved.entity.pk)


def source_bundle(world: World, tmp_path: Path, selected: Callable[[], list[Entity]]) -> Path:
    # Serialize the value a deployment predating the reserved prefix could have stored.
    # The normal exporter signs these bytes. The destination uploads, verifies and plans them.
    serialize = export._entity

    def old_entity(*args: Any, **kwargs: Any) -> dict[str, Any]:
        line = serialize(*args, **kwargs)
        for revision in line["history"]:
            values = revision["values"]
            if values.get("asset_tag") == SOURCE_TAG:
                values["asset_tag"] = LEGACY_TAG
        return line

    with patch.object(export, "_entity", old_entity):
        return bundle_of(world, tmp_path, selected)


@pytest.mark.parametrize("existing", (False, True), ids=("new-record", "append-to-record"))
def test_a_reserved_current_vendor_code_blocks_its_whole_bundle(
    world: World, tmp_path: Path, existing: bool
) -> None:
    api = Api(world.browser("admin"))
    here = freezer(world, "Freezer", "LOCAL") if existing else None

    def elsewhere() -> list[Entity]:
        if here is None:
            item = freezer(world, "Freezer", SOURCE_TAG)
        else:
            item = Entity.objects.get(pk=here.pk)
            services.update(
                world.users["admin"], item, {"asset_tag": SOURCE_TAG}, expected_revision=1
            )
        return [item, freezer(world, "Companion", "NORMAL-TAG")]

    archive = source_bundle(world, tmp_path, elsewhere)
    before = (Entity.objects.count(), Revision.objects.count())
    made = upload(api, world.project, archive)
    assert made["state"] == "planned" and not made["plan"]["applicable"]
    assert made["plan"]["counts"]["conflict"] == 1 and made["plan"]["counts"]["new"] == 1
    rows = {r["action"]: r for r in entities_of(api, made)}
    assert "asset_tag" in rows["conflict"]["why"] and "IKI:" in rows["conflict"]["why"]
    assert rows["new"]["title"] == "Companion"
    problem(
        api.post(
            f"/imports/{made['id']}/apply",
            {"reason": "Import inventory", "planned_at": made["planned_at"]},
        ),
        409,
        "refused",
    )
    assert (Entity.objects.count(), Revision.objects.count()) == before
    assert not AuditEvent.objects.filter(action="bundle.imported", project=world.project).exists()
    if here is not None:
        assert api.get(f"/entities/{here.pk}").json()["values"]["asset_tag"] == "LOCAL"


def test_applying_a_bundle_keeps_a_former_reserved_code_only_in_history(
    world: World, tmp_path: Path
) -> None:
    def elsewhere() -> list[Entity]:
        item = freezer(world, "IKI: ordinary name", SOURCE_TAG)
        services.update(
            world.users["admin"], item, {"asset_tag": "CURRENT-TAG"}, expected_revision=1
        )
        return [item]

    api = Api(world.browser("admin"))
    archive = source_bundle(world, tmp_path, elsewhere)
    made = upload(api, world.project, archive)
    assert made["plan"]["applicable"] and made["plan"]["counts"]["new"] == 1
    (record,) = entities_of(api, made)
    assert applied(api, made, "Import inventory")["state"] == "applied"
    url = f"/entities/{record['id']}"
    current = api.get(url).json()["values"]
    assert current["asset_tag"] == "CURRENT-TAG" and current["name"] == "IKI: ordinary name"
    assert api.get(f"{url}/revisions/1").json()["values"]["asset_tag"] == LEGACY_TAG
    assert api.get(f"{url}/revisions/2").json()["values"]["asset_tag"] == "CURRENT-TAG"
