"""Run against the Requests plugin pinned by the lab deployment.

Each case saves a sequencing request or deliverable as a form or the API would, and asks
which of its rules refuse it. Iki checks `reads_returned` again at each signature.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from iki_spec import rules
from iki_spec.model import EntityType, Plugin, as_refined
from iki_spec.validate import load_plugin

EXAMPLE = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ["IKI_CHECKOUT"]) if "IKI_CHECKOUT" in os.environ else EXAMPLE.parents[2]
PLUGINS = ROOT / "plugins"
NAME = "acme/sequencing_service"


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


def refused(types: Mapping[str, EntityType], name: str, values: Mapping[str, Any]) -> set[str]:
    """The rules a record of ``name`` with ``values`` breaks."""
    entity_type = types[f"{NAME}.{name}"]
    lineage = [entity_type]
    while lineage[-1].extends:
        lineage.append(types[lineage[-1].extends])
    fields = {n: as_refined(lineage, n, f) for t in reversed(lineage) for n, f in t.fields.items()}
    variables = rules.rule_variables(rules.record_schema(entity_type.label, fields, None))
    checks = [
        (f"!({r.when}) || ({r.check})" if r.when else r.check, rules.CONDITION)
        for r in entity_type.rules.values()
    ]
    (outcomes,) = rules.evaluate(variables, checks, [{"record": dict(values)}])
    assert all(o.error is None or o.empty for o in outcomes), outcomes
    return {
        rule
        for rule, outcome in zip(entity_type.rules, outcomes, strict=True)
        if not outcome.empty and not outcome.value
    }


def test_a_request_sends_the_tubes_to_sequence(types: dict[str, EntityType]) -> None:
    assert refused(types, "SequencingRequest", {"method": "long_read", "tubes": ["t"]}) == set()
    for tubes in ([], None):
        assert refused(types, "SequencingRequest", {"method": "long_read", "tubes": tubes}) == {
            "tubes_sent"
        }


@pytest.mark.parametrize(
    ("values", "broken"),
    [
        ({"run_accession": "ERR13376132"}, set()),
        ({"data_files": ["reads.fastq.gz"]}, set()),
        ({}, {"reads_returned"}),
        ({"data_files": []}, {"reads_returned"}),
        # An unsuccessful tube returns its report and no reads.
        ({"outcome": "unsuccessful"}, set()),
        ({"material": None, "run_accession": "ERR13376132"}, {"sequenced_tube"}),
    ],
)
def test_a_fulfilled_tube_returns_its_reads(
    types: dict[str, EntityType], values: dict[str, Any], broken: set[str]
) -> None:
    record = {"material": "tube-id", "outcome": "fulfilled", **values}
    assert refused(types, "SequencingDeliverable", record) == broken


def test_the_reads_are_checked_again_at_signing(types: dict[str, EntityType]) -> None:
    rule = types[f"{NAME}.SequencingDeliverable"].rules["reads_returned"]
    assert rule.recheck_before_signing
