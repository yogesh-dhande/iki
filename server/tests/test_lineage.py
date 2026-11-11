"""An entity's lineage graph: what it was made from and what was made from it, as many
generations as asked, and only as far as the caller can read."""

import itertools
from typing import Any

import pytest
from org_world import World
from test_api_entities import api_as, other
from test_runs import launch, protein, world

from iki_server.api import entities as api
from iki_server.org.models import Project
from iki_server.runs.models import Lineage

pytestmark = pytest.mark.django_db

__all__ = ["other", "world"]  # the fixtures, used here too


def run_on(world: World, project: Project, entity: dict[str, Any]) -> Any:
    launched = launch(api_as(world, "editor"), project, {"protein": entity["id"]})
    assert launched.status_code == 201, launched.content
    return launched.json()


def made(child: dict[str, Any], parent: dict[str, Any], run: dict[str, Any]) -> None:
    Lineage.objects.create(
        entity_id=child["id"],
        parent_id=parent["id"],
        parent_revision=1,
        run_id=run["id"],
        input="protein",
    )


def chain(world: World) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """a → b → c → d, each made by a run on the one before."""
    editor = api_as(world, "editor")
    entities = [protein(editor, world.project, name) for name in ("abl1", "src", "lck", "fyn")]
    runs = []
    for parent, child in itertools.pairwise(entities):
        runs.append(run_on(world, world.project, parent))
        made(child, parent, runs[-1])
    return entities, runs


def graph(world: World, entity: dict[str, Any], principal: str = "editor", **query: int) -> Any:
    answer = api_as(world, principal).get(f"/entities/{entity['hrid']}/lineage/graph", **query)
    assert answer.status_code == 200, answer.content
    return answer.json()


def test_goes_back_and_on_as_many_generations_as_asked(world: World) -> None:
    (a, b, c, d), (r1, r2, r3) = chain(world)
    whole = graph(world, b)
    assert whole["root"] == b["id"]
    assert [e["hrid"] for e in whole["entities"]] == sorted(e["hrid"] for e in (a, b, c, d))
    assert whole["entities"][0] == {
        "id": a["id"],
        "hrid": a["hrid"],
        "title": a["title"],
        "type": "protein.Protein",
        "archived": False,
    }
    assert [(link["parent"], link["entity"], link["run"]) for link in whole["links"]] == [
        (a["id"], b["id"], r1["hrid"]),
        (b["id"], c["id"], r2["hrid"]),
        (c["id"], d["id"], r3["hrid"]),
    ]
    assert whole["links"][0]["input"] == "protein"
    assert whole["links"][0]["revision"] == 1
    assert whole["runs"][0] == {
        "key": r1["hrid"],
        "run": {"id": r1["id"], "hrid": r1["hrid"], "action": r1["action"]},
    }
    assert (whole["earlier"], whole["later"], whole["capped"]) == (False, False, False)

    # Fewer generations say there's more.
    near = graph(world, b, up=0, down=1)
    assert {e["id"] for e in near["entities"]} == {b["id"], c["id"]}
    assert (near["earlier"], near["later"], near["capped"]) == (True, True, False)
    assert graph(world, a, up=0, down=3)["later"] is False
    assert graph(world, c, up=1, down=0)["earlier"] is True
    too_far = api_as(world).get(f"/entities/{b['hrid']}/lineage/graph", up=api.MAX_GENERATIONS + 1)
    assert too_far.status_code == 422


def test_shows_only_what_you_can_read(world: World, other: Project) -> None:
    """The viewer reads the project but not ``other``: its entities are left out, and its
    runs aren't named."""
    (a, b, _c, _d), _ = chain(world)
    editor = api_as(world, "editor")
    secret = protein(editor, other, "secret")
    shared = protein(editor, world.project, "shared")
    elsewhere = run_on(world, other, secret)
    made(a, secret, elsewhere)
    made(a, shared, elsewhere)

    seen = graph(world, b, "viewer")
    assert secret["id"] not in {e["id"] for e in seen["entities"]}
    assert shared["id"] in {e["id"] for e in seen["entities"]}
    assert {"key": "hidden-1", "run": None} in seen["runs"]
    assert elsewhere["hrid"] not in str(seen)
    hidden = [link for link in seen["links"] if link["run"] == "hidden-1"]
    assert [(link["parent"], link["entity"]) for link in hidden] == [(shared["id"], a["id"])]

    full = graph(world, b)
    assert {secret["id"], shared["id"]} <= {e["id"] for e in full["entities"]}
    assert elsewhere["hrid"] in {r["key"] for r in full["runs"]}

    # Nor can it be asked for what can't be read.
    assert (
        api_as(world, "viewer").get(f"/entities/{secret['hrid']}/lineage/graph").status_code == 404
    )


def test_stops_when_it_gets_too_big(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    (_a, b, _c, _d), _ = chain(world)
    monkeypatch.setattr(api, "MAX_GRAPH_ENTITIES", 2)
    small = graph(world, b)
    assert len(small["entities"]) == 2
    assert small["capped"] is True
    monkeypatch.setattr(api, "MAX_GRAPH_LINKS", 1)
    assert len(graph(world, b)["links"]) == 1


def test_a_loop_ends(world: World) -> None:
    """A run can reuse an entity that exists already, so lineage can come back on itself."""
    (a, b, c, d), (_r1, _r2, r3) = chain(world)
    made(a, d, r3)
    looped = graph(world, b, up=10, down=10)
    assert {e["id"] for e in looped["entities"]} == {a["id"], b["id"], c["id"], d["id"]}
    assert len(looped["links"]) == 4
    assert (looped["earlier"], looped["later"], looped["capped"]) == (False, False, False)
