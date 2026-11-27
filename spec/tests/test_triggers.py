"""Triggers: their schema, and the rules that check them against the plugin's types and
actions (ADR 0080)."""

import shutil
from pathlib import Path

import pytest

from iki_spec.model import Related, Trigger, plugin_from_dict, plugin_to_dict
from iki_spec.validate import Result, load_plugin

PLUGIN = """\
apiVersion: iki/v1
kind: Plugin
name: acme/lab
version: 1.0.0
roles:
  analyst: { label: Analyst }
"""

TYPES = """\
apiVersion: iki/v1
kind: EntityType
name: Base
schema_version: 1
abstract: true
fields:
  note: { type: string }
---
apiVersion: iki/v1
kind: EntityType
name: Assay
schema_version: 1
extends: Base
hrid: { prefix: ASY }
fields:
  score: { type: float, computed: true }
  plate: { type: string }
  stage:
    type: state
    initial: queued
    states: { queued: {}, read: { set_by: [analyst] }, failed: { set_by: [analyst] } }
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
tiers: [backend, self_hosted]
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
  tag: { type: string, default: x }
effects:
  updates: { assay: { fields: [score] } }
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
---
apiVersion: iki/v1
kind: Action
name: compare
inputs:
  left: { type: Assay }
  right: { type: Assay }
effects:
  updates: { left: { fields: [score] } }
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
---
apiVersion: iki/v1
kind: Action
name: pair
inputs:
  assay: { type: Assay }
  readout: { type: Readout }
effects:
  updates: { readout: { fields: [total] } }
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
---
apiVersion: iki/v1
kind: Action
name: review
inputs:
  assay: { type: Assay }
  readouts: { type: Readout, cardinality: many }
  other: { type: Readout, optional: true }
effects:
  updates: { readouts: { fields: [total] } }
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
---
apiVersion: iki/v1
kind: Action
name: view
inputs:
  assay: { type: Assay }
effects:
  updates: { assay: { fields: [score] } }
runs_on: { tier: self_hosted, environment: tools }
command: [python, ./run.py]
"""

HEAD = """\
apiVersion: iki/v1
kind: Trigger
name: on_assay
"""


def make(tmp_path: Path, *triggers: str) -> Result:
    root = tmp_path / "acme"
    shutil.rmtree(root, ignore_errors=True)
    every = {
        "plugin.yaml": PLUGIN,
        "types/types.yaml": TYPES,
        "actions/actions.yaml": ACTIONS,
        "environments/env.yaml": ENVIRONMENT,
        "requirements.txt": "numpy==2.1.0 --hash=sha256:" + "a" * 64 + "\n",
        "run.py": "",
        **{f"triggers/t{i}.yaml": text for i, text in enumerate(triggers)},
    }
    for name, text in every.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return load_plugin(root)


def trigger(body: str, head: str = HEAD) -> str:
    return head + body


def codes(result: Result) -> list[str]:
    return sorted(p.code for p in result.problems)


def only(result: Result, code: str) -> str:
    assert codes(result) == [code], [(p.code, p.message) for p in result.problems]
    return result.problems[0].message


GOOD = trigger("""\
label: Measure new assays
on: entity.created
type: Assay
action: measure
params: { mode: fast }
""")


def test_a_good_trigger(tmp_path: Path) -> None:
    result = make(tmp_path, GOOD)
    assert result.problems == []
    assert result.plugin is not None
    found = result.plugin.triggers["on_assay"]
    assert found == Trigger(
        plugin="acme/lab",
        name="on_assay",
        label="Measure new assays",
        on="entity.created",
        type="acme/lab.Assay",
        action="measure",
        # Left out, and the only input that takes an Assay.
        input="assay",
        params={"mode": "fast"},
        file=found.file,
    )
    assert found.qualified_name == "acme/lab.on_assay"
    assert found.file is not None and found.file.endswith("t0.yaml")


def test_it_survives_the_json_the_server_reads(tmp_path: Path) -> None:
    result = make(tmp_path, GOOD)
    assert result.plugin is not None
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


def test_plugins_built_before_triggers_have_none(tmp_path: Path) -> None:
    result = make(tmp_path)
    assert result.plugin is not None
    data = plugin_to_dict(result.plugin)
    del data["triggers"]
    assert plugin_from_dict(data).triggers == {}


def test_a_label_defaults_to_the_name(tmp_path: Path) -> None:
    result = make(tmp_path, trigger("on: entity.created\ntype: Assay\naction: describe\n"))
    assert result.plugin is not None
    assert result.plugin.triggers["on_assay"].label == "On assay"


def test_subtypes_fire_a_parents_input(tmp_path: Path) -> None:
    # describe takes any Base, and an Assay is one.
    result = make(tmp_path, trigger("on: entity.created\ntype: Assay\naction: describe\n"))
    assert result.problems == []
    assert result.plugin is not None
    assert result.plugin.triggers["on_assay"].input == "thing"


def test_updates_can_watch_fields_of_the_type_and_its_parents(tmp_path: Path) -> None:
    body = "on: entity.updated\ntype: Assay\nfields: [plate, note]\naction: describe\n"
    result = make(tmp_path, trigger(body))
    assert result.problems == []
    assert result.plugin is not None
    assert result.plugin.triggers["on_assay"].fields == ("plate", "note")


def test_watched_fields_must_be_the_types(tmp_path: Path) -> None:
    body = "on: entity.updated\ntype: Assay\nfields: [plat]\naction: describe\n"
    message = only(make(tmp_path, trigger(body)), "unknown-field")
    assert message == "`acme/lab.Assay` has no field `plat`."


def test_a_new_entity_has_no_changed_fields(tmp_path: Path) -> None:
    body = "on: entity.created\ntype: Assay\nfields: [plate]\naction: describe\n"
    message = only(make(tmp_path, trigger(body)), "trigger-fields-not-updated")
    assert "`on: entity.updated`" in message


def test_the_event_is_one_it_knows(tmp_path: Path) -> None:
    result = make(tmp_path, trigger("on: entity.deleted\ntype: Assay\naction: describe\n"))
    assert codes(result) == ["schema"]


def test_the_type_must_exist(tmp_path: Path) -> None:
    result = make(tmp_path, trigger("on: entity.created\ntype: Asay\naction: describe\n"))
    assert "unknown-type" in codes(result)


def test_the_action_must_be_the_plugins(tmp_path: Path) -> None:
    message = only(
        make(tmp_path, trigger("on: entity.created\ntype: Assay\naction: measur\n")),
        "trigger-unknown-action",
    )
    assert message == "This plugin has no action `measur`."


def test_it_can_run_on_a_self_hosted_machine(tmp_path: Path) -> None:
    # A team's machine is there when the trigger fires, as a person's laptop may not be.
    assert make(tmp_path, trigger("on: entity.created\ntype: Assay\naction: view\n")).problems == []


def test_some_input_must_take_the_type(tmp_path: Path) -> None:
    message = only(
        make(
            tmp_path,
            trigger("on: entity.created\ntype: Readout\naction: measure\nparams: { mode: fast }\n"),
        ),
        "trigger-wrong-type",
    )
    assert message == (
        "None of `measure`'s inputs takes `acme/lab.Readout`, which the trigger fires for."
    )


def test_a_named_input_must_take_the_type(tmp_path: Path) -> None:
    body = "on: entity.created\ntype: Readout\naction: describe\ninput: thing\n"
    message = only(make(tmp_path, trigger(body)), "trigger-wrong-type")
    assert message == (
        "The trigger fires for `acme/lab.Readout`, and `describe`'s `thing` takes `acme/lab.Base`."
    )


def test_a_named_input_must_exist(tmp_path: Path) -> None:
    body = "on: entity.created\ntype: Assay\naction: describe\ninput: thng\n"
    message = only(make(tmp_path, trigger(body)), "trigger-unknown-input")
    assert message == "`describe` has no input `thng`."


def test_two_inputs_that_fit_need_one_named(tmp_path: Path) -> None:
    result = make(tmp_path, trigger("on: entity.created\ntype: Assay\naction: compare\n"))
    assert codes(result) == ["trigger-input-ambiguous"]
    # Naming one leaves the other, which a trigger can't give.
    named = make(
        tmp_path, trigger("on: entity.created\ntype: Assay\naction: compare\ninput: left\n")
    )
    message = only(named, "trigger-input-missing")
    assert message.startswith("`compare` also needs `right`")


def test_it_gives_only_the_entity(tmp_path: Path) -> None:
    message = only(
        make(tmp_path, trigger("on: entity.created\ntype: Assay\naction: pair\n")),
        "trigger-input-missing",
    )
    assert "a trigger gives it only the entity that changed" in message


def test_optional_inputs_can_be_left(tmp_path: Path) -> None:
    # describe's note is optional.
    result = make(tmp_path, trigger("on: entity.created\ntype: Assay\naction: describe\n"))
    assert result.problems == []


# --- Related entities (ADR 0123) ----------------------------------------------------------------

REVIEW = """\
on: entity.updated
type: Assay
fields: [plate]
action: review
related:
  readouts: { direction: downstream, depth: 3, edges: [reference, run] }
  other: { direction: upstream }
"""


def test_inputs_can_be_given_what_is_related(tmp_path: Path) -> None:
    # `other` takes one, so it's in the next test; here, it's left out.
    body = REVIEW.replace("  other: { direction: upstream }\n", "")
    body = body.replace("action: review\n", "action: review\ninput: assay\n")
    result = make(tmp_path, trigger(body))
    assert result.problems == []
    assert result.plugin is not None
    found = result.plugin.triggers["on_assay"]
    assert found.input == "assay"
    assert found.related == {"readouts": Related("downstream", 3, ("reference", "run"))}
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin
    # A walk's defaults: downstream, ten edges, along every kind.
    plain = make(tmp_path, trigger(body.replace(", depth: 3, edges: [reference, run]", "")))
    assert plain.plugin is not None
    assert plain.plugin.triggers["on_assay"].related["readouts"] == Related("downstream", 10, ())


def test_an_input_given_what_is_related_is_not_the_entitys(tmp_path: Path) -> None:
    # Two take an Assay; one is given those upstream, so the entity goes to the other.
    body = """\
on: entity.created
type: Assay
action: compare_many
related:
  earlier: { direction: upstream }
"""
    global ACTIONS
    saved = ACTIONS
    ACTIONS = (
        saved
        + """\
---
apiVersion: iki/v1
kind: Action
name: compare_many
inputs:
  assay: { type: Assay }
  earlier: { type: Assay, cardinality: many }
effects:
  updates: { assay: { fields: [score] } }
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
"""
    )
    try:
        result = make(tmp_path, trigger(body))
    finally:
        ACTIONS = saved
    assert result.problems == []
    assert result.plugin is not None
    assert result.plugin.triggers["on_assay"].input == "assay"


def test_what_takes_one_is_not_given_what_is_related(tmp_path: Path) -> None:
    message = only(make(tmp_path, trigger(REVIEW)), "trigger-related-one")
    assert message == "`review`'s `other` takes one entity, and related ones can be any number."


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (("readouts:", "notes:"), "trigger-unknown-input"),
        (("related:\n  readouts:", "input: assay\nrelated:\n  assay:"), "trigger-related-is-input"),
        (("depth: 3", "depth: 31"), "schema"),
        (("depth: 3", "depth: 0"), "schema"),
        (("downstream", "sideways"), "schema"),
        (("[reference, run]", "[record]"), "schema"),
    ],
)
def test_related_inputs_are_checked(tmp_path: Path, change: tuple[str, str], code: str) -> None:
    body = REVIEW.replace("  other: { direction: upstream }\n", "").replace(*change)
    assert code in codes(make(tmp_path, trigger(body)))


def test_a_schedule_has_nothing_related(tmp_path: Path) -> None:
    body = (
        "on: schedule\nschedule: 0 6 * * *\naction: sync\nrelated: { x: { direction: upstream } }\n"
    )
    assert "trigger-schedule-entity" in codes(scheduled(tmp_path, body))


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ("{ mode: fast, colour: red }", "trigger-unknown-param"),
        ("{ mode: quick }", "trigger-invalid-param"),
        ("{ mode: $params.mode }", "trigger-invalid-param"),
        ("{ tag: y }", "trigger-param-missing"),
    ],
)
def test_params_fit_the_action(tmp_path: Path, params: str, code: str) -> None:
    body = f"on: entity.created\ntype: Assay\naction: measure\nparams: {params}\n"
    assert codes(make(tmp_path, trigger(body))) == [code]


def test_a_dollar_is_written_twice(tmp_path: Path) -> None:
    body = "on: entity.created\ntype: Assay\naction: measure\nparams: { mode: fast, tag: $$x }\n"
    result = make(tmp_path, trigger(body))
    assert result.problems == []


def test_names_are_unique(tmp_path: Path) -> None:
    result = make(tmp_path, GOOD, GOOD)
    assert codes(result) == ["duplicate-trigger"]


def test_a_state_change_names_its_field_and_states(tmp_path: Path) -> None:
    body = "on: entity.state_changed\ntype: Assay\nfield: stage\nstates: [read]\naction: describe\n"
    result = make(tmp_path, trigger(body))
    assert result.problems == []
    assert result.plugin is not None
    found = result.plugin.triggers["on_assay"]
    assert (found.on, found.state_field, found.states) == (
        "entity.state_changed",
        "stage",
        ("read",),
    )
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin
    # Without states, any of the field's.
    body = "on: entity.state_changed\ntype: Assay\nfield: stage\naction: describe\n"
    assert make(tmp_path, trigger(body)).problems == []


@pytest.mark.parametrize(
    ("body", "code", "message"),
    [
        (
            "on: entity.state_changed\ntype: Assay\nstates: [read]\naction: describe\n",
            "trigger-state-field-missing",
            "",
        ),
        (
            "on: entity.state_changed\ntype: Assay\nfield: plate\naction: describe\n",
            "trigger-not-a-state-field",
            "`plate`",
        ),
        (
            "on: entity.state_changed\ntype: Assay\nfield: stage\nstates: [done]\n"
            "action: describe\n",
            "unknown-state",
            "`done`",
        ),
        (
            "on: entity.updated\ntype: Assay\nfield: stage\naction: describe\n",
            "trigger-state-not-state-changed",
            "",
        ),
        (
            "on: entity.updated\ntype: Assay\nfields: [stage]\naction: describe\n",
            "trigger-field-is-state",
            "`stage`",
        ),
    ],
)
def test_state_change_triggers_are_checked(
    tmp_path: Path, body: str, code: str, message: str
) -> None:
    assert message in only(make(tmp_path, trigger(body)), code)


def test_triggers_live_under_triggers(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    make(tmp_path)
    (root / "actions" / "stray.yaml").write_text(GOOD)
    result = load_plugin(root)
    assert "wrong-file" in codes(result)


# --- Scheduled ---------------------------------------------------------------------------------

IMPORT = """\
---
apiVersion: iki/v1
kind: Action
name: sync
params:
  since: { type: string }
effects:
  creates: [{ type: Readout, cardinality: many }]
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
"""


def scheduled(tmp_path: Path, body: str) -> Result:
    global ACTIONS
    saved = ACTIONS
    ACTIONS = saved + IMPORT
    try:
        return make(tmp_path, trigger(body))
    finally:
        ACTIONS = saved


def test_a_scheduled_trigger(tmp_path: Path) -> None:
    body = "on: schedule\nschedule: 0 6 * * MON-FRI\ntimezone: Europe/Zurich\naction: sync\n"
    result = scheduled(tmp_path, body)
    assert result.problems == []
    assert result.plugin is not None
    found = result.plugin.triggers["on_assay"]
    assert (found.on, found.schedule, found.timezone, found.type, found.input) == (
        "schedule",
        "0 6 * * MON-FRI",
        "Europe/Zurich",
        "",
        "",
    )
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("on: schedule\naction: sync\n", "trigger-schedule-missing"),
        ("on: schedule\nschedule: '* * * * *'\naction: sync\n", "trigger-schedule-invalid"),
        ("on: schedule\nschedule: 0 6 * * * *\naction: sync\n", "trigger-schedule-invalid"),
        ("on: schedule\nschedule: 0 6 31 2 *\naction: sync\n", "trigger-schedule-never"),
        (
            "on: schedule\nschedule: 0 6 * * *\ntimezone: Mars/Olympus\naction: sync\n",
            "trigger-timezone-unknown",
        ),
        (
            "on: schedule\nschedule: 0 6 * * *\ntype: Assay\naction: sync\n",
            "trigger-schedule-entity",
        ),
        ("on: schedule\nschedule: 0 6 * * *\naction: describe\n", "trigger-input-missing"),
        ("on: entity.created\naction: describe\n", "trigger-type-missing"),
        (
            "on: entity.created\ntype: Assay\nschedule: 0 6 * * *\naction: describe\n",
            "trigger-schedule-not-scheduled",
        ),
    ],
)
def test_schedules_are_checked(tmp_path: Path, body: str, code: str) -> None:
    assert code in codes(scheduled(tmp_path, body)), scheduled(tmp_path, body).problems


def test_a_schedules_problem_says_why(tmp_path: Path) -> None:
    result = scheduled(tmp_path, "on: schedule\nschedule: '*/2 * * * *'\naction: sync\n")
    assert only(result, "trigger-schedule-invalid") == (
        "It runs 2 minutes apart, and a schedule runs at most every 5 minutes."
    )


# --- A request's arrival (ADR 0232) ------------------------------------------------------------

ASK = """\
---
apiVersion: iki/v1
kind: EntityType
name: Ask
schema_version: 1
hrid: { prefix: ASK }
service:
  request:
    service_fields: [rank]
fields:
  title: { type: string }
  rank: { type: int }
"""

RANK = """\
---
apiVersion: iki/v1
kind: Action
name: rank
inputs:
  asks: { type: Ask, cardinality: many }
  ranking: { type: Readout, cardinality: many, from_project: true }
effects:
  updates: { asks: { fields: [rank] } }
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
"""


def received(tmp_path: Path, body: str) -> Result:
    global ACTIONS, TYPES
    saved = ACTIONS, TYPES
    ACTIONS, TYPES = saved[0] + RANK, saved[1] + ASK
    try:
        return make(tmp_path, trigger(body))
    finally:
        ACTIONS, TYPES = saved


def test_a_request_arriving_fires_its_services_trigger(tmp_path: Path) -> None:
    result = received(tmp_path, "on: request.received\ntype: Ask\naction: rank\n")
    assert result.problems == []
    assert result.plugin is not None
    found = result.plugin.triggers["on_assay"]
    # The input from the project takes nothing from the trigger, so `asks` is the one.
    assert (found.on, found.type, found.input) == ("request.received", "acme/lab.Ask", "asks")
    assert result.plugin.actions["rank"].inputs["ranking"].from_project
    assert plugin_from_dict(plugin_to_dict(result.plugin)) == result.plugin


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            "on: request.received\ntype: Assay\naction: measure\nparams: { mode: fast }\n",
            "trigger-received-not-a-request",
        ),
        (
            "on: request.received\ntype: Ask\naction: rank\ninput: ranking\n",
            "trigger-input-from-project",
        ),
        (
            "on: request.received\ntype: Ask\naction: rank\n"
            "related:\n  ranking: { direction: downstream }\n",
            "trigger-input-from-project",
        ),
    ],
)
def test_arrival_triggers_are_checked(tmp_path: Path, body: str, code: str) -> None:
    result = received(tmp_path, body)
    assert code in codes(result), result.problems


def test_an_input_from_the_project_takes_many(tmp_path: Path) -> None:
    global RANK
    saved = RANK
    RANK = saved.replace("cardinality: many, from_project", "from_project")
    try:
        result = received(tmp_path, "on: request.received\ntype: Ask\naction: rank\n")
    finally:
        RANK = saved
    assert "from-project-one" in codes(result), result.problems
