"""Run against the Plasmid and DNA sources pinned by the lab deployment."""

import os
from pathlib import Path

import pytest

from iki_spec import rules
from iki_spec.model import EntityType
from iki_spec.validate import load_plugin

EXAMPLE = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def review() -> EntityType:
    root = Path(os.environ["IKI_CHECKOUT"]) if "IKI_CHECKOUT" in os.environ else EXAMPLE.parents[2]
    plugins = root / "plugins"
    loaded = load_plugin(EXAMPLE, dependencies=(plugins / "dna", plugins / "plasmid"))
    plugin = loaded.plugin
    assert plugin is not None, loaded.problems
    return plugin.types[f"{plugin.name}.ReviewedAssembly"]


@pytest.mark.parametrize(
    ("builder", "editors", "approved"),
    [
        ("reviewer", [], False),
        ("builder", ["reviewer", "builder"], False),
        ("builder", [], True),
        ("builder", ["builder"], True),
    ],
)
def test_verifier_is_independent_of_build_and_later_edits(
    review: EntityType, builder: str, editors: list[str], approved: bool
) -> None:
    status = review.fields["status"]
    guards = status.states["verified"].guards
    schema = rules.record_schema(review.label, review.fields, None)
    variables = rules.guard_variables(schema, {"status": status}, [], status)
    context = {
        "actor": {"person": "reviewer"},
        "last": {"status": {"built": {"by": builder}}},
        "edits": {
            "status": {"built": [{"by": editor, "fields": ["product"]} for editor in editors]}
        },
    }
    (outcomes,) = rules.evaluate(
        variables,
        [(guards[name].check, rules.CONDITION) for name in ("four_eyes", "independent_verifier")],
        [context],
    )
    assert all(outcome.error is None for outcome in outcomes)
    assert all(outcome.value for outcome in outcomes) is approved
