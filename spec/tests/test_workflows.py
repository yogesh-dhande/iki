"""Workflows: their schema, and the rules that check them against the plugin's actions."""

import shutil
import textwrap
from pathlib import Path

import pytest

from iki_spec.model import Approval, plugin_from_dict, plugin_to_dict
from iki_spec.validate import Result, load_plugin
from iki_spec.workflows import literal

EXAMPLES = Path(__file__).parents[2]
DNA = EXAMPLES / "plugins" / "dna"

PLUGIN = """\
apiVersion: iki/v1
kind: Plugin
name: acme
version: 1.0.0
"""

TYPES = """\
apiVersion: iki/v1
kind: EntityType
name: Base
schema_version: 1
abstract: true
---
apiVersion: iki/v1
kind: EntityType
name: Assay
schema_version: 1
extends: Base
hrid: { prefix: ASY }
fields:
  score: { type: float, computed: true }
---
apiVersion: iki/v1
kind: EntityType
name: Readout
schema_version: 1
hrid: { prefix: RDO }
fields:
  total: { type: float, computed: true }
"""

ENVIRONMENT = """\
apiVersion: iki/v1
kind: Environment
name: tools
tiers: [backend]
python: { version: "3.12", requirements: requirements.txt }
"""

ACTIONS = """\
apiVersion: iki/v1
kind: Action
name: measure
inputs:
  assay: { type: Assay }
params:
  mode: { type: enum, values: [fast, slow], required: true }
  depth: { type: int, min: 1, max: 5, default: 1 }
  tag: { type: string, default: x }
effects:
  updates: [{ input: assay, fields: [score] }]
  creates: [{ type: Readout, derived_from: [assay] }]
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
metrics:
  score: { label: Score, description: How good the assay was. }
---
apiVersion: iki/v1
kind: Action
name: split
inputs:
  assay: { type: Assay }
effects:
  creates: [{ type: Readout, cardinality: many }]
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
---
apiVersion: iki/v1
kind: Action
name: total
inputs:
  readout: { type: Readout }
effects:
  updates: [{ input: readout, fields: [total] }]
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
---
apiVersion: iki/v1
kind: Action
name: total_all
inputs:
  readouts: { type: Readout, cardinality: many }
effects:
  updates: [{ input: readouts, fields: [total] }]
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
---
apiVersion: iki/v1
kind: Action
name: describe
inputs:
  thing: { type: Base }
  note: { type: Readout, optional: true }
effects:
  creates: [{ type: Readout, derived_from: [thing] }]
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
"""

HEAD = """\
apiVersion: iki/v1
kind: Workflow
name: pipeline
inputs:
  assay: { type: Assay }
"""


def make(tmp_path: Path, workflow: str, *more: str) -> Result:
    root = tmp_path / "acme"
    shutil.rmtree(root, ignore_errors=True)
    every = {
        "plugin.yaml": PLUGIN,
        "types/types.yaml": TYPES,
        "actions/actions.yaml": ACTIONS,
        "environments/env.yaml": ENVIRONMENT,
        "requirements.txt": "numpy==2.1.0 --hash=sha256:" + "a" * 64 + "\n",
        "run.py": "",
        "workflows/pipeline.yaml": workflow,
        **{f"workflows/more{i}.yaml": text for i, text in enumerate(more)},
    }
    for name, text in every.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return load_plugin(root)


def flow(steps: str, head: str = HEAD) -> str:
    return head + "steps:\n" + textwrap.indent(textwrap.dedent(steps), "  ")


def codes(result: Result) -> list[str]:
    return sorted(p.code for p in result.problems)


def only(result: Result, code: str) -> str:
    assert codes(result) == [code], [(p.code, p.message) for p in result.problems]
    return result.problems[0].message


GOOD = flow("""\
    measure:
      action: measure
      inputs: { assay: $inputs.assay }
      params: { mode: fast }
    total:
      action: total
      inputs: { readout: $steps.measure.created.Readout }
    describe:
      action: describe
      label: Describe what was measured
      inputs: { thing: $steps.measure.inputs.assay }
      needs: [total]
""")


# --- The examples ------------------------------------------------------------------------------


def test_the_example_workflows_are_valid() -> None:
    protein = load_plugin(EXAMPLES / "plugins" / "protein", dependencies=[DNA])
    assert protein.problems == []
    assert protein.plugin is not None
    flow_ = protein.plugin.workflows["translate_and_characterize"]
    assert flow_.qualified_name == "protein.translate_and_characterize"
    assert flow_.inputs["sequence"].type == "dna.DNASequence"
    assert flow_.order() == ["translate", "properties"]
    created = "$steps.translate.created.protein.Protein"
    assert flow_.steps["properties"].inputs == {"protein": created}

    template = load_plugin(EXAMPLES / "plugin-template", dependencies=[DNA])
    assert template.problems == []
    assert template.plugin is not None
    assert template.plugin.workflows["store_sample"].order() == ["assign", "decode"]


# --- What a valid workflow reads as ------------------------------------------------------------


def test_a_valid_workflow(tmp_path: Path) -> None:
    result = make(tmp_path, GOOD)
    assert result.problems == []
    assert result.plugin is not None
    workflow = result.plugin.workflows["pipeline"]
    assert workflow.label == "Pipeline"
    assert workflow.steps["describe"].label == "Describe what was measured"
    assert workflow.steps["total"].label == "Total"
    assert workflow.steps["describe"].after() == {"measure", "total"}
    assert workflow.order() == ["measure", "total", "describe"]
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


def test_steps_that_dont_depend_on_each_other_come_in_name_order(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        flow("""\
            b: { action: split, inputs: { assay: $inputs.assay } }
            a: { action: split, inputs: { assay: $inputs.assay } }
        """),
    )
    assert result.problems == []
    assert result.plugin is not None
    assert result.plugin.workflows["pipeline"].order() == ["a", "b"]


def test_a_subtype_goes_where_its_parent_is_taken(tmp_path: Path) -> None:
    result = make(tmp_path, flow("d: { action: describe, inputs: { thing: $inputs.assay } }"))
    assert result.problems == []


def test_one_goes_where_many_are_taken(tmp_path: Path) -> None:
    steps = """\
        m: { action: measure, inputs: { assay: $inputs.assay }, params: { mode: slow } }
        t: { action: total_all, inputs: { readouts: $steps.m.created.Readout } }
    """
    assert make(tmp_path, flow(steps)).problems == []


def test_a_dollar_value_is_written_twice() -> None:
    assert literal("$$5") == "$5"
    assert literal("5") == "5"
    assert literal(5) == 5


def test_a_literal_dollar_value_is_checked_as_written(tmp_path: Path) -> None:
    steps = """\
        m:
          action: measure
          inputs: { assay: $inputs.assay }
          params: { mode: fast, tag: $$tag }
    """
    assert make(tmp_path, flow(steps)).problems == []


# --- Shape -------------------------------------------------------------------------------------


def test_a_workflow_needs_steps(tmp_path: Path) -> None:
    assert "`steps` is missing" in only(make(tmp_path, HEAD), "schema")


def test_a_reference_has_one_of_three_forms(tmp_path: Path) -> None:
    result = make(tmp_path, flow("t: { action: total, inputs: { readout: steps.m.created } }"))
    assert codes(result) == ["schema"]


def test_two_workflows_cant_share_a_name(tmp_path: Path) -> None:
    result = make(tmp_path, GOOD, GOOD)
    assert codes(result) == ["duplicate-workflow"]


def test_an_input_and_param_cant_share_a_name(tmp_path: Path) -> None:
    head = HEAD + "params:\n  assay: { type: int, default: 1 }\n"
    result = make(tmp_path, flow("s: { action: split, inputs: { assay: $inputs.assay } }", head))
    assert "both an input and a param" in only(result, "duplicate-name")


# --- Actions, inputs, params -------------------------------------------------------------------


def test_a_step_runs_one_of_the_plugins_actions(tmp_path: Path) -> None:
    result = make(tmp_path, flow("s: { action: splt, inputs: { assay: $inputs.assay } }"))
    assert "no action `splt`" in only(result, "workflow-unknown-action")
    assert result.problems[0].hint is not None
    assert "split" in result.problems[0].hint


def test_a_step_says_where_each_input_comes_from(tmp_path: Path) -> None:
    result = make(tmp_path, flow("s: { action: split }"))
    assert "`split` needs `assay`" in only(result, "workflow-input-missing")
    # An optional input can be left out: `describe`'s `note` is.
    assert make(tmp_path, flow("d: { action: describe, inputs: { thing: $inputs.assay } }")).ok


def test_a_step_gives_each_required_param(tmp_path: Path) -> None:
    result = make(tmp_path, flow("m: { action: measure, inputs: { assay: $inputs.assay } }"))
    assert "the param `mode`" in only(result, "workflow-param-missing")


def test_a_step_names_only_its_actions_inputs_and_params(tmp_path: Path) -> None:
    steps = """\
        m:
          action: measure
          inputs: { assay: $inputs.assay, asay: $inputs.assay }
          params: { mode: fast, deep: 2 }
    """
    result = make(tmp_path, flow(steps))
    assert codes(result) == ["workflow-unknown-input", "workflow-unknown-param"]


def test_a_steps_param_value_fits_the_param(tmp_path: Path) -> None:
    steps = """\
        m:
          action: measure
          inputs: { assay: $inputs.assay }
          params: { mode: medium, depth: 9 }
    """
    result = make(tmp_path, flow(steps))
    assert codes(result) == ["workflow-invalid-param", "workflow-invalid-param"]
    assert "at most 5" in " ".join(p.message for p in result.problems)


# --- References --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "why"),
    [
        ("$inputs.asay", "it has no input `asay`"),
        ("$steps.nope.created.Readout", "it has no step `nope`"),
        ("$steps.m.created.Assay", "`measure` creates `acme.Readout`"),
        ("$steps.m.inputs.other", "step `m` isn't given `other`"),
    ],
)
def test_a_reference_names_something(tmp_path: Path, ref: str, why: str) -> None:
    steps = f"""\
        m: {{ action: measure, inputs: {{ assay: $inputs.assay }}, params: {{ mode: fast }} }}
        s: {{ action: split, inputs: {{ assay: {ref} }} }}
    """
    assert why in only(make(tmp_path, flow(steps)), "workflow-unknown-reference")


def test_a_step_cant_take_from_itself(tmp_path: Path) -> None:
    steps = "t: { action: total, inputs: { readout: $steps.t.created.Readout } }"
    result = make(tmp_path, flow(steps))
    assert "itself" in only(result, "workflow-unknown-step")


def test_a_reference_is_of_the_type_the_input_takes(tmp_path: Path) -> None:
    result = make(tmp_path, flow("t: { action: total, inputs: { readout: $inputs.assay } }"))
    message = only(result, "workflow-wrong-type")
    assert "`$inputs.assay` is `acme.Assay`" in message
    assert "takes `acme.Readout`" in message


def test_many_cant_go_where_one_is_taken(tmp_path: Path) -> None:
    steps = """\
        s: { action: split, inputs: { assay: $inputs.assay } }
        t: { action: total, inputs: { readout: $steps.s.created.Readout } }
    """
    assert "can be many" in only(make(tmp_path, flow(steps)), "workflow-many-into-one")
    # What a step was given keeps its number: many given stay many.
    steps = """\
        s: { action: split, inputs: { assay: $inputs.assay } }
        a: { action: total_all, inputs: { readouts: $steps.s.created.Readout } }
        t: { action: total, inputs: { readout: $steps.a.inputs.readouts } }
    """
    assert codes(make(tmp_path, flow(steps))) == ["workflow-many-into-one"]


def test_each_runs_a_step_once_for_each_of_many(tmp_path: Path) -> None:
    steps = """\
        s: { action: split, inputs: { assay: $inputs.assay } }
        t: { action: total, each: readout, inputs: { readout: $steps.s.created.Readout } }
        a: { action: total_all, inputs: { readouts: $steps.t.inputs.readout } }
        d: { action: describe, each: thing, inputs: { thing: $inputs.assay } }
    """
    result = make(tmp_path, flow(steps))
    assert result.problems == []
    assert result.plugin is not None
    workflow = result.plugin.workflows["pipeline"]
    assert workflow.steps["t"].each == "readout"
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin
    # What a step run for each makes is many, however many each run makes.
    steps = """\
        d: { action: describe, each: thing, inputs: { thing: $inputs.assay } }
        t: { action: total, inputs: { readout: $steps.d.created.Readout } }
    """
    assert codes(make(tmp_path, flow(steps))) == ["workflow-many-into-one"]
    assert "each: readout" in (make(tmp_path, flow(steps)).problems[0].hint or "")


@pytest.mark.parametrize(
    ("step", "code"),
    [
        (
            "{ action: total_all, each: readouts, inputs: { readouts: $inputs.r } }",
            "workflow-each-takes-many",
        ),
        (
            "{ action: total, each: nope, inputs: { readout: $inputs.one } }",
            "workflow-unknown-input",
        ),
        (
            "{ action: describe, each: note, inputs: { thing: $inputs.assay } }",
            "workflow-input-missing",
        ),
    ],
)
def test_each_names_an_input_that_takes_one(tmp_path: Path, step: str, code: str) -> None:
    head = HEAD + "  r: { type: Readout, cardinality: many }\n  one: { type: Readout }\n"
    assert codes(make(tmp_path, flow(f"x: {step}", head))) == [code]


def test_needs_names_another_step(tmp_path: Path) -> None:
    steps = """\
        s: { action: split, inputs: { assay: $inputs.assay }, needs: [s, nope] }
    """
    assert codes(make(tmp_path, flow(steps))) == ["workflow-unknown-step", "workflow-unknown-step"]


def test_no_step_comes_before_itself(tmp_path: Path) -> None:
    steps = """\
        first: { action: split, inputs: { assay: $inputs.assay } }
        a: { action: split, inputs: { assay: $inputs.assay }, needs: [b] }
        b: { action: split, inputs: { assay: $steps.a.inputs.assay } }
    """
    result = make(tmp_path, flow(steps))
    assert "`a`, `b`" in only(result, "workflow-cycle")
    assert result.problems[0].message.count("`") == 4  # `first` isn't in the loop


# --- The workflow's params ---------------------------------------------------------------------


PARAMS = """\
params:
  mode: { type: enum, values: [fast], default: fast }
  depth: { type: int, min: 1, max: 3, default: 2 }
"""


def test_a_workflows_param_goes_where_it_fits(tmp_path: Path) -> None:
    steps = """\
        m:
          action: measure
          inputs: { assay: $inputs.assay }
          params: { mode: $params.mode, depth: $params.depth }
    """
    assert make(tmp_path, flow(steps, HEAD + PARAMS)).problems == []


@pytest.mark.parametrize(
    ("param", "says"),
    [
        ("mode: { type: enum, values: [fast, medium], default: fast }", "one of `fast`, `medium`"),
        ("mode: { type: string, default: fast }", "a `string`"),
        ("depth: { type: int, min: 0, max: 3, default: 1 }", "an `int` from 0 to 3"),
        ("depth: { type: int, default: 1 }", "an `int`, and"),
    ],
)
def test_a_workflows_param_must_fit_where_it_goes(tmp_path: Path, param: str, says: str) -> None:
    name = param.split(":")[0]
    given = f"{name}: $params.{name}" + ("" if name == "mode" else ", mode: fast")
    steps = f"""\
        m:
          action: measure
          inputs: {{ assay: $inputs.assay }}
          params: {{ {given} }}
    """
    result = make(tmp_path, flow(steps, HEAD + f"params:\n  {param}\n"))
    assert says in only(result, "workflow-wrong-type")


def test_a_param_reference_names_a_workflow_param(tmp_path: Path) -> None:
    steps = """\
        m:
          action: measure
          inputs: { assay: $inputs.assay }
          params: { mode: $params.mode }
    """
    assert "`$$`" in only(make(tmp_path, flow(steps)), "workflow-unknown-reference")


# --- Conditions --------------------------------------------------------------------------------


CONDITIONAL = (
    HEAD
    + """\
params:
  threshold: { type: float, default: 0.5 }
  mode: { type: enum, values: [fast, slow], default: fast }
  label: { type: string, default: x }
"""
)

MEASURE = "m: { action: measure, inputs: { assay: $inputs.assay }, params: { mode: fast } }\n"


def when(*conditions: str) -> str:
    listed = ", ".join(conditions)
    return (
        MEASURE
        + "t: { action: total, inputs: { readout: $steps.m.created.Readout }, "
        + f"when: [{listed}] }}\n"
    )


def test_a_step_runs_only_when_its_conditions_hold(tmp_path: Path) -> None:
    result = make(
        tmp_path,
        flow(
            when(
                "{ value: $steps.m.metrics.score, at_least: $params.threshold }",
                "{ value: $steps.m.metrics.score, below: 10 }",
                "{ value: $params.mode, is: fast }",
                "{ value: $params.mode, in: [fast, slow] }",
                "{ value: $params.label, is_not: $$x }",
            ),
            CONDITIONAL,
        ),
    )
    assert result.problems == []
    assert result.plugin is not None
    step = result.plugin.workflows["pipeline"].steps["t"]
    assert [(c.value, c.operator, c.operand) for c in step.when[:2]] == [
        ("$steps.m.metrics.score", "at_least", "$params.threshold"),
        ("$steps.m.metrics.score", "below", 10),
    ]
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin
    assert result.plugin.actions["measure"].metrics["score"].label == "Score"


def test_a_condition_on_a_metric_waits_for_its_step(tmp_path: Path) -> None:
    steps = MEASURE + (
        "s: { action: split, inputs: { assay: $inputs.assay }, "
        "when: [{ value: $steps.m.metrics.score, above: 1 }] }\n"
    )
    result = make(tmp_path, flow(steps))
    assert result.plugin is not None
    assert result.plugin.workflows["pipeline"].order() == ["m", "s"]


@pytest.mark.parametrize(
    ("condition", "code", "says"),
    [
        ("{ value: $steps.m.metrics.nope, above: 1 }", "workflow-unknown-metric", "`score`"),
        ("{ value: $steps.x.metrics.score, above: 1 }", "workflow-unknown-step", "`x`"),
        ("{ value: $params.nope, is: 1 }", "workflow-unknown-reference", "$params.nope"),
        ("{ value: $params.mode, above: 1 }", "workflow-condition-type", "isn't a number"),
        ("{ value: $params.mode, is: medium }", "workflow-condition-type", "must be one of"),
        ("{ value: $params.mode, in: [fast, medium] }", "workflow-condition-type", "one of"),
        ("{ value: $steps.m.metrics.score, is: high }", "workflow-condition-type", "number"),
        (
            "{ value: $steps.m.metrics.score, at_least: $params.mode }",
            "workflow-condition-type",
            "one of",
        ),
        (
            "{ value: $steps.m.metrics.score, at_least: $params.nope }",
            "workflow-unknown-reference",
            "nope",
        ),
    ],
)
def test_conditions_are_checked(tmp_path: Path, condition: str, code: str, says: str) -> None:
    assert says in only(make(tmp_path, flow(when(condition), CONDITIONAL)), code)


def test_a_condition_takes_one_operator(tmp_path: Path) -> None:
    result = make(
        tmp_path, flow(when("{ value: $params.mode, is: fast, is_not: slow }"), CONDITIONAL)
    )
    assert codes(result) == ["schema"]


def test_a_condition_reads_one_metric_not_one_for_each_run(tmp_path: Path) -> None:
    steps = (
        "m: { action: measure, each: assay, inputs: { assay: $inputs.assay }, "
        "params: { mode: fast } }\n"
        "s: { action: split, inputs: { assay: $inputs.assay }, "
        "when: [{ value: $steps.m.metrics.score, above: 1 }] }\n"
    )
    assert "for each run" in only(make(tmp_path, flow(steps)), "workflow-condition-each")


def test_a_step_cant_read_its_own_metrics(tmp_path: Path) -> None:
    steps = (
        "m: { action: measure, inputs: { assay: $inputs.assay }, params: { mode: fast }, "
        "when: [{ value: $steps.m.metrics.score, above: 1 }] }\n"
    )
    assert "its own" in only(make(tmp_path, flow(steps)), "workflow-unknown-step")


# --- Approval ----------------------------------------------------------------------------------


def test_a_step_can_wait_for_approval(tmp_path: Path) -> None:
    steps = (
        MEASURE
        + "t: { action: total, inputs: { readout: $steps.m.created.Readout }, "
        + "approval: { role: admin, instructions: '  Check the readouts first.  ' } }\n"
        + "d: { action: describe, inputs: { thing: $inputs.assay }, approval: {} }\n"
    )
    result = make(tmp_path, flow(steps))
    assert result.problems == []
    assert result.plugin is not None
    found = result.plugin.workflows["pipeline"].steps
    assert found["t"].approval == Approval(role="admin", instructions="Check the readouts first.")
    assert found["d"].approval == Approval(role="editor", instructions="")
    assert found["m"].approval is None
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


@pytest.mark.parametrize(
    "approval", ["{ role: owner }", "{ instructions: '' }", "{ by: me }", "yes"]
)
def test_an_approval_names_a_project_role(tmp_path: Path, approval: str) -> None:
    steps = f"d: {{ action: describe, inputs: {{ thing: $inputs.assay }}, approval: {approval} }}\n"
    assert codes(make(tmp_path, flow(steps))) == ["schema"]
