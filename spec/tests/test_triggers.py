"""Triggers: their schema, and the rules that check them against the plugin's types and
actions (ADR 0080)."""

import shutil
from pathlib import Path

import pytest

from iki_spec.model import Trigger, plugin_from_dict, plugin_to_dict
from iki_spec.validate import Result, load_plugin

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
tiers: [backend, client]
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
  updates: [{ input: assay, fields: [score] }]
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
  updates: [{ input: left, fields: [score] }]
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
  updates: [{ input: readout, fields: [total] }]
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
---
apiVersion: iki/v1
kind: Action
name: view
inputs:
  assay: { type: Assay }
effects:
  updates: [{ input: assay, fields: [score] }]
runs_on: { tier: client, environment: tools }
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
run: measure
params: { mode: fast }
""")


def test_a_good_trigger(tmp_path: Path) -> None:
    result = make(tmp_path, GOOD)
    assert result.problems == []
    assert result.plugin is not None
    found = result.plugin.triggers["on_assay"]
    assert found == Trigger(
        plugin="acme",
        name="on_assay",
        label="Measure new assays",
        on="entity.created",
        type="acme.Assay",
        run="measure",
        # Left out, and the only input that takes an Assay.
        input="assay",
        params={"mode": "fast"},
        file=found.file,
    )
    assert found.qualified_name == "acme.on_assay"
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
    result = make(tmp_path, trigger("on: entity.created\ntype: Assay\nrun: describe\n"))
    assert result.plugin is not None
    assert result.plugin.triggers["on_assay"].label == "On assay"


def test_subtypes_fire_a_parents_input(tmp_path: Path) -> None:
    # describe takes any Base, and an Assay is one.
    result = make(tmp_path, trigger("on: entity.created\ntype: Assay\nrun: describe\n"))
    assert result.problems == []
    assert result.plugin is not None
    assert result.plugin.triggers["on_assay"].input == "thing"


def test_updates_can_watch_fields_of_the_type_and_its_parents(tmp_path: Path) -> None:
    body = "on: entity.updated\ntype: Assay\nfields: [plate, note]\nrun: describe\n"
    result = make(tmp_path, trigger(body))
    assert result.problems == []
    assert result.plugin is not None
    assert result.plugin.triggers["on_assay"].fields == ("plate", "note")


def test_watched_fields_must_be_the_types(tmp_path: Path) -> None:
    body = "on: entity.updated\ntype: Assay\nfields: [plat]\nrun: describe\n"
    message = only(make(tmp_path, trigger(body)), "unknown-field")
    assert message == "`acme.Assay` has no field `plat`."


def test_a_new_entity_has_no_changed_fields(tmp_path: Path) -> None:
    body = "on: entity.created\ntype: Assay\nfields: [plate]\nrun: describe\n"
    message = only(make(tmp_path, trigger(body)), "trigger-fields-not-updated")
    assert "`on: entity.updated`" in message


def test_the_event_is_one_it_knows(tmp_path: Path) -> None:
    result = make(tmp_path, trigger("on: entity.deleted\ntype: Assay\nrun: describe\n"))
    assert codes(result) == ["schema"]


def test_the_type_must_exist(tmp_path: Path) -> None:
    result = make(tmp_path, trigger("on: entity.created\ntype: Asay\nrun: describe\n"))
    assert "unknown-type" in codes(result)


def test_the_action_must_be_the_plugins(tmp_path: Path) -> None:
    message = only(
        make(tmp_path, trigger("on: entity.created\ntype: Assay\nrun: measur\n")),
        "trigger-unknown-action",
    )
    assert message == "This plugin has no action `measur`."


def test_it_cant_run_on_the_client_tier(tmp_path: Path) -> None:
    message = only(
        make(tmp_path, trigger("on: entity.created\ntype: Assay\nrun: view\n")),
        "trigger-client-tier",
    )
    assert "nobody may be" in message


def test_some_input_must_take_the_type(tmp_path: Path) -> None:
    message = only(
        make(
            tmp_path,
            trigger("on: entity.created\ntype: Readout\nrun: measure\nparams: { mode: fast }\n"),
        ),
        "trigger-wrong-type",
    )
    assert message == (
        "None of `measure`'s inputs takes `acme.Readout`, which the trigger fires for."
    )


def test_a_named_input_must_take_the_type(tmp_path: Path) -> None:
    body = "on: entity.created\ntype: Readout\nrun: describe\ninput: thing\n"
    message = only(make(tmp_path, trigger(body)), "trigger-wrong-type")
    assert message == (
        "The trigger fires for `acme.Readout`, and `describe`'s `thing` takes `acme.Base`."
    )


def test_a_named_input_must_exist(tmp_path: Path) -> None:
    body = "on: entity.created\ntype: Assay\nrun: describe\ninput: thng\n"
    message = only(make(tmp_path, trigger(body)), "trigger-unknown-input")
    assert message == "`describe` has no input `thng`."


def test_two_inputs_that_fit_need_one_named(tmp_path: Path) -> None:
    result = make(tmp_path, trigger("on: entity.created\ntype: Assay\nrun: compare\n"))
    assert codes(result) == ["trigger-input-ambiguous"]
    # Naming one leaves the other, which a trigger can't give.
    named = make(tmp_path, trigger("on: entity.created\ntype: Assay\nrun: compare\ninput: left\n"))
    message = only(named, "trigger-input-missing")
    assert message.startswith("`compare` also needs `right`")


def test_it_gives_only_the_entity(tmp_path: Path) -> None:
    message = only(
        make(tmp_path, trigger("on: entity.created\ntype: Assay\nrun: pair\n")),
        "trigger-input-missing",
    )
    assert "a trigger gives it only the entity that changed" in message


def test_optional_inputs_can_be_left(tmp_path: Path) -> None:
    # describe's note is optional.
    result = make(tmp_path, trigger("on: entity.created\ntype: Assay\nrun: describe\n"))
    assert result.problems == []


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
    body = f"on: entity.created\ntype: Assay\nrun: measure\nparams: {params}\n"
    assert codes(make(tmp_path, trigger(body))) == [code]


def test_a_dollar_is_written_twice(tmp_path: Path) -> None:
    body = "on: entity.created\ntype: Assay\nrun: measure\nparams: { mode: fast, tag: $$x }\n"
    result = make(tmp_path, trigger(body))
    assert result.problems == []


def test_names_are_unique(tmp_path: Path) -> None:
    result = make(tmp_path, GOOD, GOOD)
    assert codes(result) == ["duplicate-trigger"]


def test_triggers_live_under_triggers(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    make(tmp_path)
    (root / "actions" / "stray.yaml").write_text(GOOD)
    result = load_plugin(root)
    assert "wrong-file" in codes(result)
