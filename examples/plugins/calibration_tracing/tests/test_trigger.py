"""Run against the Equipment plugin pinned by the lab deployment.

The example has no action of its own: its trigger runs Equipment's trace (ADR 0298). These
tests check the wiring a lab relies on against the Equipment release it deploys, so a
release that renames the trace or its inputs fails here, before a calibration fails.
"""

import os
from pathlib import Path

from iki_spec.first_party_dependencies import dependency_closures
from iki_spec.model import Plugin
from iki_spec.validate import load_plugin

EXAMPLE = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ["IKI_CHECKOUT"]) if "IKI_CHECKOUT" in os.environ else EXAMPLE.parents[2]
PLUGINS = ROOT / "plugins"
TRACE = "iki/equipment.trace_failed_calibrations"


def loaded(directory: Path) -> Plugin:
    found = load_plugin(directory, dependencies=dependency_closures(PLUGINS, directory)[directory])
    assert found.plugin is not None, found.problems
    assert found.problems == []
    return found.plugin


def test_the_trigger_runs_equipments_own_trace_on_the_failed_instrument() -> None:
    example, equipment = loaded(EXAMPLE), loaded(PLUGINS / "equipment")
    trigger = example.triggers["trace_failed_instrument"]
    assert (trigger.type, trigger.state_field, trigger.states) == (
        "iki/equipment.Instrument",
        "calibration",
        ("failed",),
    )
    assert (trigger.action, trigger.input) == (TRACE, "instrument")
    # Off until a project's Admin turns it on: a recheck would be on everywhere.
    assert not trigger.recheck
    assert not example.actions and not example.environments
    trace = equipment.actions["trace_failed_calibrations"]
    assert trace.qualified_name == TRACE


def test_each_related_search_fills_the_trace_input_of_its_name() -> None:
    example, equipment = loaded(EXAMPLE), loaded(PLUGINS / "equipment")
    related = example.triggers["trace_failed_instrument"].related
    inputs = equipment.actions["trace_failed_calibrations"].inputs
    assert set(related) == {"calibrations", "uses", "existing"}
    for name in related:
        assert inputs[name].cardinality == "many", name
    # Everything else the trace needs is the instrument the trigger fired for.
    needed = {n for n, put in inputs.items() if not put.optional and n not in related}
    assert needed == {"instrument"}
