"""Run against the DNA and Protein plugins pinned by the lab deployment.

The workflow has no action of its own: both steps run Protein's actions (ADR 0298). These
tests check its wiring against the Protein release the lab deploys, so a release that
renames an action, an input or a param fails here, before a run fails.
"""

import os
from pathlib import Path

from iki_spec.first_party_dependencies import dependency_closures
from iki_spec.model import Plugin
from iki_spec.validate import load_plugin

EXAMPLE = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ["IKI_CHECKOUT"]) if "IKI_CHECKOUT" in os.environ else EXAMPLE.parents[2]
PLUGINS = ROOT / "plugins"


def loaded(directory: Path) -> Plugin:
    found = load_plugin(directory, dependencies=dependency_closures(PLUGINS, directory)[directory])
    assert found.plugin is not None, found.problems
    assert found.problems == []
    return found.plugin


def test_both_steps_run_proteins_own_actions() -> None:
    example, protein = loaded(EXAMPLE), loaded(PLUGINS / "protein")
    workflow = example.workflows["translate_and_characterize"]
    assert workflow.order() == ["translate", "properties"]
    assert {i: s.action for i, s in workflow.steps.items()} == {
        "translate": "iki/protein.translate",
        "properties": "iki/protein.compute_properties",
    }
    assert not example.actions and not example.environments
    assert {"translate", "compute_properties"} <= set(protein.actions)


def test_the_translated_protein_is_the_one_whose_properties_are_computed() -> None:
    example, protein = loaded(EXAMPLE), loaded(PLUGINS / "protein")
    steps = example.workflows["translate_and_characterize"].steps
    translate = protein.actions["translate"]
    assert "iki/protein.Protein" in {c.type for c in translate.creates}
    assert steps["properties"].inputs == {"protein": "$steps.translate.created.iki/protein.Protein"}
    # The workflow's params reach Protein's own params of the same names.
    assert set(steps["translate"].params) <= set(translate.params)
