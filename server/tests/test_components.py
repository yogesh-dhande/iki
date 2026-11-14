"""Components: what installed plugins add to Iki's pages, and their modules (ADR 0084)."""

import dataclasses
import hashlib

import pytest
from org_world import World
from test_api_entities import api_as
from test_workers import world

from iki_server.components import installed

__all__ = ["world"]

pytestmark = pytest.mark.django_db

RESIDUES = "protein.residues"


def test_components_are_listed_with_where_they_appear(world: World) -> None:
    listed = {c["name"]: c for c in api_as(world, "viewer").get("/components").json()}
    found = listed[RESIDUES]
    module = installed.module(RESIDUES)
    assert found == {
        "name": RESIDUES,
        "plugin": "protein",
        "label": "Residues",
        "description": "How many residues a protein has.",
        "element": "protein-residues",
        "slots": [{"slot": "entity.tab", "type": "protein.Protein", "label": "Residues"}],
        "selects": "protein.Protein",
        "sha256": hashlib.sha256(module).hexdigest(),
    }
    assert b"customElements.define" in module


def test_subtypes_pages_show_their_parents_components() -> None:
    assert [c.qualified_name for c in installed.for_type("protein.Protein", "entity.tab")] == [
        RESIDUES
    ]
    assert installed.for_type("dna.DNASequence", "entity.tab") == []


def test_a_module_that_changed_after_the_build_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    component = installed.components()[RESIDUES]
    installed.module.cache_clear()
    changed = dataclasses.replace(component, sha256="0" * 64)
    monkeypatch.setitem(installed.components(), RESIDUES, changed)
    with pytest.raises(installed.ModuleChanged):
        installed.module(RESIDUES)
    installed.module.cache_clear()
