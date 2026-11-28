"""Lab lists' options in bundles (ADR 0270): a record names its option by its code, which
the importing registry's list must have; the plan says so when it hasn't."""

from pathlib import Path
from typing import Any

import pytest
from api_client import Api
from org_world import World
from test_bundle_imports import applied, bundle_of, entities_of, upload
from test_bundle_imports import world as world

from iki_server.entities import lists, services
from iki_server.entities.models import Entity
from iki_server.org.models import Registry, RegistryRole

pytestmark = pytest.mark.django_db

METHOD = "acme/protein.PurificationMethod"
PREP = "acme/protein.Prep"


def prep(world: World, name: str, method: Any) -> Any:
    return services.create(
        world.users["admin"], PREP, world.project, {"name": name, "method": str(method)}
    ).entity


def option(code: str) -> lists.Option:
    found = lists.find(Registry.default().pk, METHOD, code)
    assert found is not None
    return found


def test_a_record_names_the_option_of_its_code_here(world: World, tmp_path: Path) -> None:
    sec = option("sec").entry.pk
    path = bundle_of(world, tmp_path, lambda: [prep(world, "Prep 12", sec)])
    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)
    assert made["plan"]["applicable"], made["plan"]
    (row,) = entities_of(api, made)
    assert row["action"] == "new", row
    done = applied(api, made)
    assert done["state"] == "applied", done
    imported = Entity.objects.get(type=PREP, owner_project=world.project)
    assert services.current_values(services.specific(imported))["method"] == sec
    # The option itself wasn't imported: it's the registry's, as it was.
    assert Entity.objects.filter(type=METHOD).count() == 3


def test_an_option_the_list_hasnt_got_is_a_conflict(world: World, tmp_path: Path) -> None:
    def made_there() -> list[Any]:
        # Where the bundle comes from, the list has an option this one hasn't.
        RegistryRole.objects.create(
            registry=Registry.default(), user=world.users["admin"], role="registrar"
        )
        hplc = lists.add(
            world.users["admin"],
            Registry.default(),
            METHOD,
            {"code": "hplc", "label": "HPLC"},
            reason="",
        )
        return [prep(world, "Prep 13", hplc.entry.pk)]

    path = bundle_of(world, tmp_path, made_there)
    assert lists.find(Registry.default().pk, METHOD, "hplc") is None
    api = Api(world.browser("admin"))
    made = upload(api, world.project, path)
    (row,) = entities_of(api, made)
    assert row["action"] == "conflict", row
    assert "`hplc` of Purification methods" in row["why"]
    assert "its curators add it first" in row["why"]
    assert not made["plan"]["applicable"] or made["plan"]["counts"]["conflict"] == 1
