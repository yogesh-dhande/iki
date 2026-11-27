"""Run against the Requests and Protein plugins pinned by the lab deployment.

Each case saves a production request or deliverable as a form or the API would, and asks
which of its rules refuse it. Iki checks the deliverable's rules again as a reviewer signs
it off, so a request changed since can't be signed off with an old answer.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from iki_spec import rules
from iki_spec.model import EntityType, Field, Plugin, as_refined
from iki_spec.validate import load_plugin

EXAMPLE = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ["IKI_CHECKOUT"]) if "IKI_CHECKOUT" in os.environ else EXAMPLE.parents[2]
PLUGINS = ROOT / "plugins"
NAME = "acme/protein_production"
DEPENDENCIES = ("inventory", "lab_roles", "requests", "dna", "protein")


def loaded(directory: Path, *dependencies: str) -> Plugin:
    found = load_plugin(directory, dependencies=tuple(PLUGINS / name for name in dependencies))
    assert found.plugin is not None, found.problems
    assert found.problems == []
    return found.plugin


@pytest.fixture(scope="module")
def types() -> dict[str, EntityType]:
    requests = loaded(PLUGINS / "requests", "inventory", "lab_roles")
    example = loaded(EXAMPLE, *DEPENDENCIES)
    return {**requests.types, **example.types}


def fields_of(types: Mapping[str, EntityType], name: str) -> dict[str, Field]:
    lineage = [types[name]]
    while lineage[-1].extends in types:
        lineage.append(types[lineage[-1].extends])
    return {n: as_refined(lineage, n, f) for t in reversed(lineage) for n, f in t.fields.items()}


def refused(types: Mapping[str, EntityType], name: str, values: Mapping[str, Any]) -> set[str]:
    """The rules a record of ``name`` with ``values`` breaks."""
    entity_type = types[f"{NAME}.{name}"]

    def refs(target: str) -> tuple[str, Mapping[str, Field]] | None:
        # A protein is read by its ID here: its own fields don't matter to these rules.
        return (
            (types[target].label, fields_of(types, target))
            if target.startswith(NAME)
            else (target, {})
        )

    record = rules.record_schema(entity_type.label, fields_of(types, f"{NAME}.{name}"), refs)
    checks = [
        (f"!({r.when}) || ({r.check})" if r.when else r.check, rules.CONDITION)
        for r in entity_type.rules.values()
    ]
    (outcomes,) = rules.evaluate(rules.rule_variables(record), checks, [{"record": dict(values)}])
    assert all(o.error is None or o.empty for o in outcomes), outcomes
    return {
        rule
        for rule, outcome in zip(entity_type.rules, outcomes, strict=True)
        if not outcome.empty and not outcome.value
    }


@pytest.mark.parametrize(
    ("values", "broken"),
    [
        ({"records": ["adh1"], "requested_mass": 1.0}, set()),
        ({"records": ["adh1", "adh2"], "requested_mass": 1.0}, {"one_protein"}),
        ({"records": [], "requested_mass": 1.0}, {"one_protein"}),
        ({"records": ["adh1"], "requested_mass": 0.0}, {"positive_mass"}),
    ],
)
def test_a_request_sends_one_protein_and_a_positive_mass(
    types: dict[str, EntityType], values: dict[str, Any], broken: set[str]
) -> None:
    assert refused(types, "ProductionRequest", values) == broken


@pytest.mark.parametrize(
    ("values", "broken"),
    [
        ({"lot_number": "TRAINING-ADH1-01", "delivered_mass": 0.8}, set()),
        ({"lot_number": "TRAINING-ADH1-01", "delivered_mass": 0.0}, {"produced_lot"}),
        ({"lot_number": "", "delivered_mass": 0.8}, {"produced_lot"}),
        ({"delivered_mass": 0.8}, {"produced_lot"}),
        # An unsuccessful production returns its report and no lot.
        ({"outcome": "unsuccessful"}, set()),
        (
            {"protein": "adh2", "lot_number": "TRAINING-ADH1-01", "delivered_mass": 0.8},
            {"requested_protein"},
        ),
    ],
)
def test_a_fulfilled_production_returns_the_requested_protein_lot(
    types: dict[str, EntityType], values: dict[str, Any], broken: set[str]
) -> None:
    record = {
        "protein": {"id": "adh1"},
        "outcome": "fulfilled",
        "request": {"records": ["adh1"]},
        **values,
    }
    if isinstance(record["protein"], str):
        record["protein"] = {"id": record["protein"]}
    assert refused(types, "ProductionDeliverable", record) == broken


def test_the_deliverable_is_checked_again_at_sign_off(types: dict[str, EntityType]) -> None:
    deliverable = types[f"{NAME}.ProductionDeliverable"]
    assert all(r.recheck_before_signing for r in deliverable.rules.values())
