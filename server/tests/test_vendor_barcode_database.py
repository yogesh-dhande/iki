"""The storage barcode namespace survives bulk writes and overlapping application versions."""

from collections.abc import Callable
from dataclasses import replace

import pytest
from api_client import Api
from django.db import IntegrityError, connection, transaction
from org_world import World
from test_storage import FREEZER, RACK, TUBE, made, tube
from test_storage import world as world

from iki_server.audit.models import AuditEvent
from iki_server.entities import registry, unique
from iki_server.entities.models import UniqueValue
from iki_server.entities.values import shape
from iki_server.plugins import sync
from iki_spec.model import EntityType

pytestmark = pytest.mark.django_db


def test_the_database_refuses_reserved_vendor_codes_and_rolls_back_the_whole_write(
    world: World,
) -> None:
    api = Api(world.browser("editor"))
    first, second = (tube(api, world.project, code) for code in ("ORDINARY-1", "ORDINARY-2"))
    with connection.cursor() as cursor:
        for reserved in (
            "iKi:reserved",
            "\t\u00a0IKI:reserved",
            "\u001c\u2003\u0131K\u0131:reserved",
        ):
            with pytest.raises(IntegrityError, match="IKI:"), transaction.atomic():
                cursor.execute(
                    "UPDATE acme__protein_tube SET barcode = CASE WHEN entity_id = %s THEN %s "
                    "ELSE 'ORDINARY-NEW' END WHERE entity_id IN (%s, %s)",
                    [first["id"], reserved, first["id"], second["id"]],
                )
            cursor.execute("SELECT barcode FROM acme__protein_tube ORDER BY barcode")
            assert cursor.fetchall() == [("ORDINARY-1",), ("ORDINARY-2",)]
    assert set(
        UniqueValue.objects.filter(key="acme__protein_tube.barcode").values_list("value", flat=True)
    ) == {"ORDINARY-1", "ORDINARY-2"}

    freezer = made(api, world.project, FREEZER, {"name": "North"})
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE acme__protein_freezer SET name = 'IKI: name' WHERE entity_id = %s",
            [freezer["id"]],
        )
        with pytest.raises(IntegrityError, match="IKI:"), transaction.atomic():
            cursor.execute(
                "UPDATE acme__protein_freezer SET asset_tag = 'iki:tag' WHERE entity_id = %s",
                [freezer["id"]],
            )
    assert api.get(f"/entities/{freezer['id']}").json()["values"]["name"] == "IKI: name"


def test_sync_reports_existing_reserved_codes_without_rewriting_records_or_history(
    world: World,
) -> None:
    api = Api(world.browser("editor"))
    record = tube(api, world.project, "VENDOR")
    with connection.cursor() as cursor:
        # A pre-upgrade deployment installed the two-argument uniqueness trigger.
        cursor.execute(
            "CREATE OR REPLACE TRIGGER iki_unique__barcode AFTER INSERT OR UPDATE OF barcode "
            "ON acme__protein_tube FOR EACH ROW EXECUTE FUNCTION "
            "iki_unique_value_keep('barcode', 'acme__protein_tube.barcode')"
        )
        cursor.execute(
            "UPDATE acme__protein_tube SET barcode = ' iki:legacy' WHERE entity_id = %s",
            [record["id"]],
        )
    before = api.get(f"/entities/{record['id']}").json()
    audits = AuditEvent.objects.count()
    with pytest.raises(unique.Clash) as failure, transaction.atomic():
        sync.sync()
    assert record["hrid"] in str(failure.value) and "acme__protein_tube.barcode" in str(
        failure.value
    )
    assert "IKI:" in str(failure.value)
    assert api.get(f"/entities/{record['id']}").json() == before
    assert before["values"]["barcode"] == " iki:legacy" and AuditEvent.objects.count() == audits
    assert UniqueValue.objects.get(entity_id=record["id"]).value == " iki:legacy"


def test_a_shared_parent_column_is_guarded_only_on_types_that_use_it_as_their_barcode(
    world: World, monkeypatch: pytest.MonkeyPatch, redeclare: Callable[[str, EntityType], None]
) -> None:
    api = Api(world.browser("editor"))
    inherits = made(api, world.project, RACK, {"name": "Inherits the tube barcode"})
    overrides = made(api, world.project, FREEZER, {"name": "Uses its own asset tag"})
    # Model a deployment with two subtypes of Tube, using existing physical fixture tables.
    # Rack inherits the declaration. Freezer replaces it with storage.barcode = asset_tag.
    types = registry.types()
    redeclare(RACK, replace(types[RACK], extends=TUBE, storage=None))
    redeclare(FREEZER, replace(types[FREEZER], extends=TUBE))
    monkeypatch.setattr(sync, "shape", shape.__wrapped__)  # read these manifests afresh
    sync.sync()
    with connection.cursor() as cursor:
        insert_parent = "INSERT INTO acme__protein_tube (entity_id, barcode) VALUES (%s, %s)"
        with pytest.raises(IntegrityError, match="IKI:"), transaction.atomic():
            cursor.execute(insert_parent, [inherits["id"], "IKI:inherited"])
        cursor.execute(insert_parent, [overrides["id"], "IKI:ordinary-parent-field"])
        with pytest.raises(IntegrityError, match="IKI:"), transaction.atomic():
            cursor.execute(
                "UPDATE acme__protein_freezer SET asset_tag = %s WHERE entity_id = %s",
                ["IKI:declared-override", overrides["id"]],
            )
