"""Run against the Requests plugin pinned by the lab deployment.

Each case saves an analytical result as a form or the API would, and asks which of its
rules refuse it. Iki checks the same rules again at each signature, so a result whose
request has changed since can't be signed.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from iki_spec import rules
from iki_spec.effective import resolve_one
from iki_spec.model import EntityType, Field, Plugin
from iki_spec.validate import load_plugin

EXAMPLE = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ["IKI_CHECKOUT"]) if "IKI_CHECKOUT" in os.environ else EXAMPLE.parents[2]
PLUGINS = ROOT / "plugins"
REQUESTS = "iki/requests."
EXAMPLE_NAME = "acme/analytical_testing"


def loaded(directory: Path, *dependencies: str) -> Plugin:
    found = load_plugin(directory, dependencies=tuple(PLUGINS / name for name in dependencies))
    assert found.plugin is not None, found.problems
    assert found.problems == []
    return found.plugin


@pytest.fixture(scope="module")
def types() -> dict[str, EntityType]:
    requests = loaded(PLUGINS / "requests", "inventory", "lab_roles")
    example = loaded(EXAMPLE, "inventory", "lab_roles", "requests")
    return {**requests.types, **example.types}


def fields_of(types: Mapping[str, EntityType], *names: str) -> dict[str, Field]:
    return {name: f for t in names for name, f in types[t].fields.items()}


def refused(types: Mapping[str, EntityType], values: Mapping[str, Any]) -> set[str]:
    """The rules an AnalyticalResult with ``values`` breaks, for a request asking for
    ``values["asked"]``."""
    result = types[f"{EXAMPLE_NAME}.AnalyticalResult"]
    request = fields_of(
        types, f"{REQUESTS}Request", f"{REQUESTS}QCRequest", f"{EXAMPLE_NAME}.AnalyticalRequest"
    )

    def refs(name: str) -> tuple[str, Mapping[str, Field]] | None:
        return ("Analytical request", request) if name.endswith(".AnalyticalRequest") else None

    # Its fields as its records have them: the result answers only analytical requests
    # (`refines`), so its rules read what they ask for.
    own = resolve_one(types, result.qualified_name).current_fields
    variables = rules.rule_variables(rules.record_schema(result.label, own, refs))
    checks = [
        (f"!({r.when}) || ({r.check})" if r.when else r.check, rules.CONDITION)
        for r in result.rules.values()
    ]
    record = {
        "material": "sample-id",
        "outcome": "fulfilled",
        "result": "pass",
        **{k: v for k, v in values.items() if k != "asked"},
        "request": {"analyses": values.get("asked", [values.get("analysis")])},
    }
    (outcomes,) = rules.evaluate(variables, checks, [{"record": record}])
    assert all(o.error is None or o.empty for o in outcomes), outcomes
    return {
        name
        for name, outcome in zip(result.rules, outcomes, strict=True)
        if not outcome.empty and not outcome.value
    }


@pytest.mark.parametrize(
    ("analysis", "measurement"),
    [
        ("sec_purity", "purity"),
        ("intact_mass", "measured_mass"),
        ("peptide_map", "peptide_coverage"),
        ("endotoxin", "endotoxin"),
    ],
)
def test_each_analysis_needs_its_own_measurement(
    types: dict[str, EntityType], analysis: str, measurement: str
) -> None:
    complete = {"analysis": analysis, measurement: 0.0}
    assert refused(types, complete) == set()
    # A different assay's measurement cannot stand in for the one ordered.
    wrong = {"purity": 99.0, "measured_mass": 24000.0, "peptide_coverage": 97.0, "endotoxin": 0.2}
    assert len(refused(types, {**complete, **wrong, measurement: None})) == 1


def test_a_result_measures_what_the_request_asks_for(types: dict[str, EntityType]) -> None:
    asked = {"analysis": "sec_purity", "purity": 98.5}
    assert refused(types, {**asked, "asked": ["sec_purity", "endotoxin"]}) == set()
    assert refused(types, {**asked, "asked": ["endotoxin"]}) == {"requested_analysis"}
    assert refused(types, {**asked, "asked": []}) == {"requested_analysis"}


def test_every_measurement_rule_is_checked_again_at_signing(
    types: dict[str, EntityType],
) -> None:
    result = types[f"{EXAMPLE_NAME}.AnalyticalResult"]
    assert result.rules["requested_analysis"].recheck_before_signing


def test_a_result_names_the_sample_it_measured(types: dict[str, EntityType]) -> None:
    # Required as its records have it: the deliverable's material, narrowed (ADR 0283).
    result = resolve_one(types, f"{EXAMPLE_NAME}.AnalyticalResult")
    assert result.fields["material"].required
    assert not types[f"{REQUESTS}Deliverable"].fields["material"].required
