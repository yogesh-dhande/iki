"""Viewers: Iki's own viewers on a type's pages, as the type API lists them (ADR 0085)."""

import pytest
from org_world import World
from test_api_entities import api_as
from test_workers import world

__all__ = ["world"]

pytestmark = pytest.mark.django_db


def test_a_type_lists_the_viewers_it_configures(world: World) -> None:
    types = {t["name"]: t for t in api_as(world, "viewer").get("/entity-types").json()}
    assert types["protein.Protein"]["viewers"] == [
        {
            "viewer": "sequence",
            "label": "Sequence",
            "sequence": "sequence",
            "alphabet": "protein",
            "features": None,
        }
    ]
    assert types["dna.DNASequence"]["viewers"] == []
