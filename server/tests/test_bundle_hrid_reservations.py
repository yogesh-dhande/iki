"""An earlier allocation cannot take an HRID the reviewed plan keeps for a later member."""

from pathlib import Path

import pytest
from api_client import Api
from org_world import World
from test_bundle_imports import applied, bundle_of, entities_of, protein, ran, upload, world

from iki_server.bundles import reading
from iki_server.entities import hrids, registry, services
from iki_server.entities.models import Entity
from iki_server.runs import services as run_services
from iki_server.runs.models import ActionRun

pytestmark = pytest.mark.django_db
__all__ = ["world"]


@pytest.mark.parametrize("registered", [True, False], ids=["new-registration", "standalone-record"])
def test_the_next_entity_number_belongs_to_the_record_keeping_it(
    world: World, tmp_path: Path, registered: bool
) -> None:
    type_name = "acme/protein.Protein" if registered else "acme/protein.Sample"

    def record(value: str) -> Entity:
        values = {"sequence": value} if registered else {"label": value}
        made = services.create(world.users["admin"], type_name, world.project, values)
        return Entity.objects.get(pk=made.entity.pk)

    def give(record: Entity, hrid: str) -> None:
        Entity.objects.filter(pk__in=[record.pk, record.entry_id]).update(hrid=hrid)
        record.hrid = hrid

    taken = record("MKPALA")

    def elsewhere() -> list[Entity]:
        first, later = sorted([record("MKTAGA"), record("MKAATA")], key=lambda r: r.pk)
        last = hrids.next_hrid(registry.types()[type_name])
        prefix, _, number = last.rpartition("-")
        give(later, hrids.format_hrid(prefix, len(number), int(number) + 1))
        return [first, later]

    path = bundle_of(world, tmp_path, elsewhere)
    api = Api(world.browser("admin"))
    # Take the first source HRID without drawing another number before the apply.
    source = reading.read(path, {}, limit=1024**3)
    first, later = source.lines("entities.ndjson")
    give(taken, first["hrid"])
    made = upload(api, world.project, path)
    rows = {r["id"]: r for r in entities_of(api, made)}
    assert rows[first["id"]]["hrid_here"] == ""
    assert rows[later["id"]]["hrid_here"] == later["hrid"]
    done = applied(api, made)
    assert done["state"] == "applied", done
    assert Entity.objects.get(pk=later["id"]).hrid == later["hrid"]
    assert Entity.objects.get(pk=first["id"]).hrid not in {first["hrid"], later["hrid"]}


def test_the_next_run_number_belongs_to_the_run_keeping_it(world: World, tmp_path: Path) -> None:
    first_hrid = run_services._next_hrid()
    _, _, n = first_hrid.rpartition("-")
    later_hrid = hrids.format_hrid("RUN", len(n), int(n) + 1)

    def elsewhere() -> list[Entity]:
        first = protein(world, "MKDLGG")
        later = protein(world, "MKALGA")
        ran(world, first, first, hrid=first_hrid)
        ran(world, later, later, hrid=later_hrid)
        return [first, later]

    path = bundle_of(world, tmp_path, elsewhere)
    local = protein(world, "MKGLAD")
    ran(world, local, local, hrid=first_hrid)
    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)
    count = made["plan"]["counts"]
    assert count["runs"] == 2 and count["runs_renumbered"] == 1
    done = applied(api, made)
    assert done["state"] == "applied", done
    imported = {r.imported["hrid"]: r.hrid for r in ActionRun.objects.exclude(imported={})}
    assert imported[later_hrid] == later_hrid
    assert imported[first_hrid] not in {first_hrid, later_hrid}
