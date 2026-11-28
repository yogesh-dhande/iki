"""A type keeps one HRID prefix while it has records (ADR 0033): HRIDs are printed on labels,
so the deploy refuses a build that gives such a type another, from a lockfile's ``hrids`` or
from its plugin."""

from dataclasses import replace

import pytest
from org_world import World
from test_rules import BATCH, made, world

from iki_server.entities import registry
from iki_server.plugins import deploy

__all__ = ["world"]

VECTOR = "acme/dna.Vector"


def building(monkeypatch: pytest.MonkeyPatch, prefixes: dict[str, str]) -> None:
    """The build being deployed gives these types these prefixes."""
    types = {
        name: replace(t, hrid_prefix=prefixes[name]) if name in prefixes else t
        for name, t in registry.types().items()
    }
    monkeypatch.setattr(registry, "types", lambda: types)


@pytest.mark.django_db
def test_a_type_with_records_keeps_its_prefix(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = made(world, {"plate": "P1", "well": "A1", "purity": 95})
    made(world, {"plate": "P1", "well": "A2", "purity": 99})
    deployed = registry.plugins()
    old = registry.types()[BATCH].hrid_prefix
    assert first["hrid"].startswith(f"{old}-")
    assert deploy.moved_prefixes(deployed) == []

    building(monkeypatch, {BATCH: "LOTB"})
    assert deploy.moved_prefixes(deployed) == [
        f"{BATCH} has records under the HRID prefix {old}, such as {first['hrid']}, and this "
        "build gives it LOTB. HRIDs are printed on labels, so a type's records keep one "
        f"prefix: keep {old} with `hrids: {{ {BATCH}: {old} }}` in plugins.lock.yaml, or take "
        "out the override that moves it."
    ]


@pytest.mark.django_db
def test_a_type_without_records_can_be_given_another(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    made(world, {"plate": "P1", "well": "A1", "purity": 95})
    deployed = registry.plugins()
    building(monkeypatch, {VECTOR: "AVEC"})
    assert deploy.moved_prefixes(deployed) == []
