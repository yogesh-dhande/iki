"""A plugin's triggers, workflow steps and components can run a declared dependency's
actions, by their qualified names, checked exactly as its own are (ADR 0298)."""

import shutil
from pathlib import Path

from iki_spec.model import plugin_from_dict, plugin_to_dict
from iki_spec.validate import Result, load_plugin

#: The dependency: a vendor's validated calculations on its assays.
LAB = {
    "plugin.yaml": """\
apiVersion: iki/v1
kind: Plugin
name: acme/lab
version: 1.0.0
iki: ">=0.1"
""",
    "types/types.yaml": """\
apiVersion: iki/v1
kind: EntityType
name: Assay
schema_version: 1
hrid: { prefix: ASY }
fields:
  score: { type: float, computed: true }
  plate: { type: string }
  checked: { type: string, computed: true }
---
apiVersion: iki/v1
kind: EntityType
name: Readout
schema_version: 1
hrid: { prefix: RDO }
fields:
  total: { type: float, computed: true }
""",
    "actions/actions.yaml": """\
apiVersion: iki/v1
kind: Action
name: measure
inputs:
  assay: { type: Assay }
params:
  mode: { type: enum, values: [fast, slow], required: true }
effects:
  updates: { assay: { fields: [score] } }
metrics: { wells: { label: Wells } }
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
---
apiVersion: iki/v1
kind: Action
name: summarize
inputs:
  assay: { type: Assay }
effects:
  creates: [{ type: Readout, derived_from: [assay] }]
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
""",
    "environments/tools.yaml": """\
apiVersion: iki/v1
kind: Environment
name: tools
tiers: [backend]
python: { version: "3.12", requirements: requirements.txt }
""",
    "requirements.txt": "numpy==2.1.0 --hash=sha256:" + "a" * 64 + "\n",
    "run.py": "",
}

#: The lab's plugin: its own subtype of the vendor's assay, and no action of its own.
SCREENS = """\
apiVersion: iki/v1
kind: Plugin
name: acme/screens
version: 1.0.0
iki: ">=0.1"
dependencies:
  acme/lab:
    types: { Assay: ">=1", Readout: ">=1" }
"""

TYPES = """\
apiVersion: iki/v1
kind: EntityType
name: Screen
schema_version: 1
extends: acme/lab.Assay
hrid: { prefix: SCR }
fields:
  library: { type: string }
"""

TRIGGER = """\
apiVersion: iki/v1
kind: Trigger
name: measure_new_screens
on: entity.created
type: Screen
action: {action}
params: {{ mode: fast }}
"""

WORKFLOW = """\
apiVersion: iki/v1
kind: Workflow
name: measure_and_summarize
inputs:
  screen: {{ type: Screen }}
steps:
  measure:
    action: {measure}
    inputs: {{ assay: $inputs.screen }}
    params: {{ mode: slow }}
  summarize:
    action: acme/lab.summarize
    inputs: {{ assay: $steps.measure.inputs.assay }}
    when: [{{ value: $steps.measure.metrics.{metric}, above: 0 }}]
"""

COMPONENT = """\
apiVersion: iki/v1
kind: Component
name: screen_plot
label: Screen plot
module: ./components/screen-plot.js
element: acme-screen-plot
slots: [{{ slot: entity.tab, type: {tab} }}]
events: {{ run: {{ actions: [{action}] }} }}
"""


def make(tmp_path: Path, *, plugin: str = SCREENS, types: str = TYPES, **manifests: str) -> Result:
    lab, screens = tmp_path / "lab", tmp_path / "screens"
    for root in (lab, screens):
        shutil.rmtree(root, ignore_errors=True)
    files = {"plugin.yaml": plugin, "types/types.yaml": types, **manifests}
    if any(name.startswith("components/") for name in manifests):
        files["components/screen-plot.js"] = "export {};\n"
    for root, every in ((lab, LAB), (screens, files)):
        for name, text in every.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
    return load_plugin(screens, dependencies=(lab,))


def codes(result: Result) -> list[str]:
    return sorted(p.code for p in result.problems)


def only(result: Result, code: str) -> str:
    assert codes(result) == [code], [(p.code, p.message) for p in result.problems]
    return result.problems[0].message


def trigger(action: str = "acme/lab.measure") -> dict[str, str]:
    return {"triggers/measure.yaml": TRIGGER.format(action=action)}


def test_a_trigger_runs_its_dependencys_calculation_on_the_labs_own_subtype(
    tmp_path: Path,
) -> None:
    result = make(tmp_path, **trigger())
    assert result.problems == [], [(p.code, p.message) for p in result.problems]
    assert result.plugin is not None
    found = result.plugin.triggers["measure_new_screens"]
    # Recorded by its qualified name, so the server runs the vendor's action as declared.
    assert (found.action, found.input) == ("acme/lab.measure", "assay")
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


def test_a_workflow_chains_its_dependencys_actions(tmp_path: Path) -> None:
    flow = WORKFLOW.format(measure="acme/lab.measure", metric="wells")
    result = make(tmp_path, **{"workflows/flow.yaml": flow})
    assert result.problems == [], [(p.code, p.message) for p in result.problems]
    assert result.plugin is not None
    workflow = result.plugin.workflows["measure_and_summarize"]
    assert [s.action for s in workflow.steps.values()] == ["acme/lab.measure", "acme/lab.summarize"]
    assert workflow.order() == ["measure", "summarize"]


def test_a_component_offers_its_dependencys_action_on_the_labs_tab(tmp_path: Path) -> None:
    component = COMPONENT.format(tab="Screen", action="acme/lab.measure")
    result = make(tmp_path, **{"components/screen-plot.yaml": component})
    assert result.problems == [], [(p.code, p.message) for p in result.problems]
    assert result.plugin is not None
    (found,) = result.plugin.components.values()
    assert found.runs == ("acme/lab.measure",)


def test_an_action_of_a_plugin_it_doesnt_declare_is_refused(tmp_path: Path) -> None:
    message = only(make(tmp_path, **trigger("acme/assays.measure")), "undeclared-dependency")
    assert "acme/assays" in message and "`dependencies`" in message


def test_a_dependencys_action_needs_its_namespace(tmp_path: Path) -> None:
    result = make(tmp_path, **trigger("lab.measure"))
    assert only(result, "unqualified-reference") == (
        "`lab.measure` doesn't say which namespace the lab plugin is in."
    )
    assert result.problems[0].hint == "Write `acme/lab.measure`."


def test_an_action_the_dependency_doesnt_have_is_refused_with_the_nearest(
    tmp_path: Path,
) -> None:
    result = make(tmp_path, **trigger("acme/lab.measures"))
    assert only(result, "trigger-unknown-action") == "The acme/lab plugin has no action `measures`."
    assert "measure" in (result.problems[0].hint or "")


def test_a_bare_name_it_doesnt_have_points_to_the_dependencys_action(tmp_path: Path) -> None:
    # The lab wrote the vendor's action as if it were its own.
    result = make(tmp_path, **trigger("measure"))
    assert only(result, "trigger-unknown-action") == "This plugin has no action `measure`."
    assert result.problems[0].hint == "Write `acme/lab.measure` for the acme/lab plugin's."


def test_a_dependencys_action_is_wired_as_strictly_as_its_own(tmp_path: Path) -> None:
    # The vendor's action takes assays, not readouts.
    readouts = TRIGGER.format(action="acme/lab.measure").replace(
        "type: Screen", "type: acme/lab.Readout"
    )
    assert "trigger-wrong-type" in codes(make(tmp_path, **{"triggers/measure.yaml": readouts}))
    # Its required param is still required.
    unparameterized = TRIGGER.format(action="acme/lab.measure").replace(
        "params: { mode: fast }\n", ""
    )
    assert only(
        make(tmp_path, **{"triggers/measure.yaml": unparameterized}), "trigger-param-missing"
    ) == ("`acme/lab.measure` needs the param `mode`, and this trigger doesn't give it.")
    # A step's condition reads only a metric the dependency's action declares.
    flow = WORKFLOW.format(measure="acme/lab.measure", metric="plates")
    assert only(make(tmp_path, **{"workflows/flow.yaml": flow}), "workflow-unknown-metric") == (
        "`acme/lab.measure` declares no metric `plates` (it declares `wells`)."
    )
    # A component's tab must be a type the action takes.
    component = COMPONENT.format(tab="acme/lab.Readout", action="acme/lab.measure")
    assert "component-action-takes-no-entity" in codes(
        make(tmp_path, **{"components/screen-plot.yaml": component})
    )


def test_a_recheck_runs_only_its_own_plugins_calculation(tmp_path: Path) -> None:
    # A recheck is on in every project without an Admin turning it on: only the
    # calculation's own plugin may declare one.
    recheck = """\
apiVersion: iki/v1
kind: Trigger
name: recheck_screens
on: entity.updated
type: Screen
fields: [plate]
action: acme/lab.measure
params: { mode: fast }
recheck: checked
"""
    message = only(make(tmp_path, **{"triggers/recheck.yaml": recheck}), "trigger-recheck")
    assert "`acme/lab.measure` is the acme/lab plugin's" in message
