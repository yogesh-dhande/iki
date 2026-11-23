"""Actions and environments: their schemas, and the rules beyond them."""

import dataclasses
import json
import os
import shutil
import textwrap
from pathlib import Path

import pytest

from iki_spec import apis
from iki_spec.compute import compute_from_dict, compute_to_dict, load_compute
from iki_spec.model import (
    action_from_dict,
    parse_duration,
    parse_quantity,
    plugin_from_dict,
    plugin_to_dict,
)
from iki_spec.problems import Severity
from iki_spec.validate import Result, check_settings_text, load_plugin

PLUGINS = Path(__file__).parent / "plugins"
DNA, PROTEIN, PREDICTION = PLUGINS / "dna", PLUGINS / "protein", PLUGINS / "structure_prediction"

PLUGIN = """\
apiVersion: iki/v1
kind: Plugin
name: acme/lab
version: 1.0.0
"""

TYPES = """\
apiVersion: iki/v1
kind: EntityType
name: Assay
schema_version: 3
hrid: { prefix: ASY }
fields:
  score: { type: float, computed: true }
  barcode: { type: string, immutable: true }
  old: { type: int, removed: true }
---
apiVersion: iki/v1
kind: EntityType
name: Readout
schema_version: 1
hrid: { prefix: RDO }
---
apiVersion: iki/v1
kind: EntityType
name: Base
schema_version: 1
abstract: true
"""

REQUIREMENTS = "numpy==2.1.0 --hash=sha256:" + "a" * 64 + "\n"

BACKEND = """\
apiVersion: iki/v1
kind: Environment
name: tools
tiers: [backend, self_hosted]
python: { version: "3.12", requirements: requirements.txt }
"""

GPU = (
    """\
apiVersion: iki/v1
kind: Environment
name: gpu
tiers: [vm, container]
resources: { cpu: 4, memory: 16Gi, gpu: { count: 1 } }
image: ghcr.io/acme/tool@sha256:"""
    + "b" * 64
    + "\n"
)

ACTION = """\
apiVersion: iki/v1
kind: Action
name: score
inputs:
  assay: { type: Assay }
effects:
  updates: [{ input: assay, fields: [score] }]
runs_on: { tier: backend, environment: tools }
command: [python, ./run.py]
"""


def make(
    tmp_path: Path,
    action: str = ACTION,
    *environments: str,
    files: dict[str, str] | None = None,
    with_: tuple[Path, ...] = (),
) -> Result:
    root = tmp_path / "acme"
    shutil.rmtree(root, ignore_errors=True)
    every = {
        "plugin.yaml": PLUGIN,
        "types/types.yaml": TYPES,
        "actions/action.yaml": action,
        "environments/env.yaml": "---\n".join(environments or (BACKEND,)),
        "requirements.txt": REQUIREMENTS,
        "run.py": "",
        **(files or {}),
    }
    for name, text in every.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(textwrap.dedent(text))
    return load_plugin(root, dependencies=with_)


def codes(result: Result) -> list[str]:
    return [p.code for p in result.problems]


def only(result: Result, code: str) -> str:
    assert codes(result) == [code], [(p.code, p.message) for p in result.problems]
    return result.problems[0].message


def action(**changes: str) -> str:
    """ACTION with top-level keys replaced (or added) by YAML text."""
    out = []
    replacing = False
    for line in ACTION.splitlines():
        if line.startswith(" ") and replacing:
            continue  # part of the value being replaced
        key = line.split(":")[0]
        replacing = key in changes
        if replacing:
            value = changes.pop(key)
            out.append(f"{key}: {value}" if value != "" else "")
            continue
        out.append(line)
    out.extend(f"{k}: {v}" for k, v in changes.items())
    return "\n".join(line for line in out if line) + "\n"


# --- The examples ------------------------------------------------------------------------------


def test_the_example_actions_and_environments_are_valid() -> None:
    protein = load_plugin(PROTEIN, dependencies=[DNA])
    assert protein.problems == []
    assert protein.plugin is not None
    compute = protein.plugin.actions["compute_properties"]
    assert compute.qualified_name == "acme/protein.compute_properties"
    assert compute.inputs["protein"].type == "acme/protein.Protein"
    assert compute.tier == "backend"
    assert compute.lifecycle is None
    assert compute.batchable == 100
    assert compute.timeout == 300
    assert (compute.retry_max, compute.retry_on) == (2, ("worker_lost", "interrupted"))

    prediction = load_plugin(PREDICTION, dependencies=[PROTEIN, DNA])
    assert prediction.problems == []
    assert prediction.plugin is not None
    predict = prediction.plugin.actions["predict_structure"]
    assert predict.resources.memory == 96 * 1024**3
    assert predict.artifact_retention == 365 * 86400
    assert predict.creates[0].derived_from == ("protein",)
    env = prediction.plugin.environments["alphafold-gpu"]
    assert env.resources.gpu_memory == 40 * 1024**3
    assert env.volumes["af-db"].size == 3 * 1024**4
    assert env.setup_timeout == 12 * 3600
    # An action's resources only ever add to its environment's.
    wanted = env.resources.at_least(predict.resources)
    assert (wanted.cpu, wanted.memory, wanted.gpu_count) == (8, 96 * 1024**3, 1)


def test_an_action_cannot_replace_draw_evidence(tmp_path: Path) -> None:
    types = TYPES.replace("hrid: { prefix: ASY }", "hrid: { prefix: ASY }\ndraws_received: score")
    types = types.replace("score: { type: float,", "score: { type: json,")
    assert codes(make(tmp_path, files={"types/types.yaml": types})) == ["updates-draws-received"]


def test_a_minimal_action_is_valid(tmp_path: Path) -> None:
    result = make(tmp_path)
    assert result.problems == []


def test_quantities_and_durations() -> None:
    assert parse_quantity("512Mi") == 512 * 1024**2
    assert parse_duration("90s") == 90
    assert parse_duration("30m") == 1800
    assert parse_duration("2d") == 172800
    with pytest.raises(ValueError, match="1G"):
        parse_quantity("1G")
    with pytest.raises(ValueError, match="0s"):
        parse_duration("0s")


# --- Where manifests live ----------------------------------------------------------------------


def test_actions_live_under_actions(tmp_path: Path) -> None:
    result = make(tmp_path, files={"types/action.yaml": ACTION})
    assert "Files under types/ hold entity types and extensions, not an action" in only(
        result, "wrong-file"
    )
    assert result.problems[0].hint == "Move it to a file under actions/."


def test_environments_live_under_environments(tmp_path: Path) -> None:
    result = make(tmp_path, files={"actions/env.yaml": BACKEND})
    assert "Files under actions/ hold actions, not an environment." in only(result, "wrong-file")


def test_names_are_unique(tmp_path: Path) -> None:
    result = make(tmp_path, files={"actions/again.yaml": ACTION})
    assert "`score` is defined twice" in only(result, "duplicate-action")
    result = make(tmp_path, ACTION, BACKEND, BACKEND)
    assert "`tools` is defined twice" in only(result, "duplicate-environment")


# --- Schemas -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"name": "Score"}, "name"),
        ({"command": "[]"}, "command"),
        ({"timeout": "1w"}, "timeout"),
        ({"timeout": "0s"}, "timeout"),
        ({"secrets": "[ncbi_key]"}, "secrets"),
        ({"network": "['*.com']"}, "network"),
        ({"network": "[https://x.org]"}, "network"),
        ({"batchable": "1001"}, "batchable"),
        ({"retries": "{ max: 11 }"}, "retries"),
        ({"runs_on": "{ tier: gpu, environment: tools }"}, "tier"),
        ({"runs_on": "{ tier: backend, environment: Tools }"}, "environment"),
        ({"artifacts": "{ keep: [/etc/passwd] }"}, "artifacts"),
        ({"extra": "1"}, "extra"),
    ],
)
def test_the_action_schema(tmp_path: Path, changes: dict[str, str], fragment: str) -> None:
    result = make(tmp_path, action(**changes))
    assert codes(result) == ["schema"], [(p.code, p.message) for p in result.problems]
    assert fragment in result.problems[0].message + str(result.problems[0].path)


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("image: ghcr.io/acme/tool:latest", "image"),
        ("image: alpine@sha256:" + "b" * 64, "image"),
        ("python: { version: '2.7', requirements: r.txt }", "version"),
        ("python: { version: '3.12', requirements: ../r.txt }", "requirements"),
        ("python: { version: '3.12', requirements: /r.txt }", "requirements"),
        ("resources: { memory: 16GB }", "memory"),
        ("resources: { cpu: 0 }", "cpu"),
        ("volumes: { db: { size: 1Ti, mount: data } }", "mount"),
        ("volumes: { db: { size: 1Ti, mount: /data/../etc } }", "mount"),
        ("volumes: { DB: { size: 1Ti, mount: /data } }", "DB"),
    ],
)
def test_the_environment_schema(tmp_path: Path, text: str, fragment: str) -> None:
    env = "apiVersion: iki/v1\nkind: Environment\nname: tools\ntiers: [vm]\n" + text + "\n"
    result = make(tmp_path, ACTION, env)
    problems = [p for p in result.problems if p.code == "schema"]
    assert problems, [(p.code, p.message) for p in result.problems]
    assert fragment in problems[0].message + str(problems[0].path)


def test_python_and_conda_are_one_or_the_other(tmp_path: Path) -> None:
    env = BACKEND + "conda: { lockfile: conda.lock }\n"
    result = make(tmp_path, ACTION, env)
    assert "schema" in codes(result)


# --- Environments ------------------------------------------------------------------------------


def test_provisioned_tiers_need_an_image(tmp_path: Path) -> None:
    env = GPU.replace("image:", "# image:")
    result = make(tmp_path, ACTION, BACKEND, env)
    assert "environment-needs-image" in codes(result)


def test_the_backend_needs_packages(tmp_path: Path) -> None:
    env = "apiVersion: iki/v1\nkind: Environment\nname: tools\ntiers: [backend]\n"
    assert "neither" in only(make(tmp_path, ACTION, env), "environment-needs-packages")


def test_a_self_hosted_machine_can_run_an_image(tmp_path: Path) -> None:
    env = (
        "apiVersion: iki/v1\nkind: Environment\nname: tools\ntiers: [self_hosted]\n"
        "image: ghcr.io/acme/tool@sha256:" + "b" * 64 + "\n"
    )
    hosted = action(runs_on="{ tier: self_hosted, environment: tools }", command="[/app/run]")
    assert make(tmp_path, hosted, env).problems == []


def test_an_environment_names_what_a_self_hosted_machine_needs(tmp_path: Path) -> None:
    result = make(tmp_path, ACTION, BACKEND + "labels: [lab-3, chromeleon]\n")
    assert result.problems == []
    assert result.plugin is not None
    assert result.plugin.environments["tools"].labels == ("chromeleon", "lab-3")
    # Only the self-hosted tier picks machines by their labels.
    backend = BACKEND.replace("[backend, self_hosted]", "[backend]") + "labels: [gpu]\n"
    assert only(make(tmp_path, ACTION, backend), "labels-wrong-tier")
    bad = make(tmp_path, ACTION, BACKEND + "labels: [Lab 3]\n")
    assert not bad.ok


def test_packages_only_the_image_tiers_would_ignore(tmp_path: Path) -> None:
    env = GPU + 'python: { version: "3.12", requirements: requirements.txt }\n'
    vm = action(runs_on="{ tier: vm, environment: gpu }")
    result = make(tmp_path, vm, env)
    assert only(result, "unused-packages")
    assert result.problems[0].severity is Severity.WARNING
    assert result.ok


def test_gpus_are_on_provisioned_tiers(tmp_path: Path) -> None:
    env = BACKEND + "resources: { gpu: { count: 1 } }\n"
    assert only(make(tmp_path, ACTION, env), "gpu-wrong-tier")
    gpu = action(runs_on="{ tier: backend, environment: tools, resources: { gpu: { count: 1 } } }")
    assert only(make(tmp_path, gpu), "gpu-wrong-tier")


def test_files_must_exist(tmp_path: Path) -> None:
    env = BACKEND.replace("requirements.txt", "missing.txt")
    message = only(make(tmp_path, ACTION, env), "missing-file")
    assert message == "There's no file `missing.txt` in this plugin."


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symbolic links")
def test_files_cant_be_symbolic_links(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text(REQUIREMENTS)
    root = tmp_path / "acme"
    make(tmp_path)
    (root / "requirements.txt").unlink()
    (root / "requirements.txt").symlink_to(outside)
    result = load_plugin(root)
    assert only(result, "symlink")
    # A link to a directory on the way counts too.
    (root / "requirements.txt").unlink()
    (tmp_path / "deps").mkdir()
    (tmp_path / "deps" / "requirements.txt").write_text(REQUIREMENTS)
    (root / "deps").symlink_to(tmp_path / "deps")
    (root / "environments" / "env.yaml").write_text(
        BACKEND.replace("requirements.txt", "deps/requirements.txt")
    )
    assert only(load_plugin(root), "symlink")


@pytest.mark.parametrize(
    ("requirements", "code", "fragment"),
    [
        ("numpy>=2\n", "requirements-unpinned", "Line 1 (`numpy>=2`)"),
        ("numpy==2.1.0\n", "requirements-unpinned", "isn't pinned"),
        ("numpy --hash=sha256:" + "a" * 64 + "\n", "requirements-unpinned", "isn't pinned"),
        ("-r base.txt\n", "requirements-include", "includes another file"),
        ("-e ./lib\n", "requirements-unpinned", "editable"),
        (
            "# comment\n--index-url https://pypi.org/simple\n\nnumpy==2 \\\n  --hash=sha256:"
            + "a" * 64
            + "\nscipy==1\n",
            "requirements-unpinned",
            "Line 6 (`scipy==1`)",
        ),
    ],
)
def test_requirements_are_pinned_with_hashes(
    tmp_path: Path, requirements: str, code: str, fragment: str
) -> None:
    result = make(tmp_path, files={"requirements.txt": requirements})
    assert fragment in only(result, code)


def test_a_pinned_requirements_file_with_markers_and_continuations(tmp_path: Path) -> None:
    text = (
        'colorama==0.4.6 ; sys_platform == "win32" \\\n'
        "    --hash=sha256:" + "c" * 64 + " \\\n"
        "    --hash=sha256:" + "d" * 64 + "\n"
        "    # via click\n"
    )
    assert make(tmp_path, files={"requirements.txt": text}).problems == []


def test_conda_lockfiles_are_explicit_and_hashed(tmp_path: Path) -> None:
    env = BACKEND.replace(
        'python: { version: "3.12", requirements: requirements.txt }',
        "conda: { lockfile: conda.lock }",
    )
    good = (
        "# platform: linux-64\n@EXPLICIT\n"
        "https://conda.anaconda.org/conda-forge/linux-64/numpy-2.1.0.conda#" + "e" * 32 + "\n"
    )
    assert make(tmp_path, ACTION, env, files={"conda.lock": good}).problems == []
    result = make(tmp_path, ACTION, env, files={"conda.lock": "name: x\ndependencies: [numpy]\n"})
    assert only(result, "conda-not-explicit")
    unhashed = "@EXPLICIT\nhttps://conda.anaconda.org/conda-forge/linux-64/numpy-2.1.0.conda\n"
    assert "Line 2" in only(
        make(tmp_path, ACTION, env, files={"conda.lock": unhashed}), "conda-unpinned"
    )


def test_volumes(tmp_path: Path) -> None:
    vm = action(runs_on="{ tier: vm, environment: gpu }")
    volumes = (
        "volumes:\n"
        "  db: { size: 1Ti, mount: /data, populated_by: setup }\n"
        "  more: { size: 1Ti, mount: /data/more }\n"
    )
    result = make(tmp_path, vm, GPU + volumes)
    assert sorted(codes(result)) == ["overlapping-mounts", "volume-needs-setup"]
    setup = "setup: { script: setup.sh, timeout: 2h }\n"
    fixed = volumes.replace("/data/more", "/scratch")
    assert make(tmp_path, vm, GPU + fixed + setup, files={"setup.sh": ""}).problems == []
    unused = make(tmp_path, vm, GPU + setup, files={"setup.sh": ""})
    assert only(unused, "setup-fills-nothing")
    assert unused.ok


def test_volumes_are_on_provisioned_tiers(tmp_path: Path) -> None:
    env = BACKEND + "volumes: { db: { size: 1Ti, mount: /data } }\n"
    assert only(make(tmp_path, ACTION, env), "volumes-wrong-tier")


def test_unused_environments_are_mentioned(tmp_path: Path) -> None:
    result = make(tmp_path, ACTION, BACKEND, GPU)
    assert only(result, "unused-environment") == "No action of this plugin runs in `gpu`."
    assert result.ok


# --- Actions -----------------------------------------------------------------------------------


def test_inputs_resolve_and_fit(tmp_path: Path) -> None:
    result = make(tmp_path, action(inputs="{ assay: { type: Nope } }", effects="{}"))
    assert "unknown-type" in codes(result)
    result = make(tmp_path, action(inputs='{ assay: { type: Assay, schema: ">=4" } }'))
    message = only(result, "input-schema-unsatisfied")
    assert message == "`acme/lab.Assay` is at schema version 3, which `>=4` doesn't allow."
    result = make(tmp_path, action(inputs="{ assay: { type: Assay, max: 10 } }"))
    assert only(result, "max-needs-many")


def test_an_input_includes_the_entities_its_references_name(tmp_path: Path) -> None:
    plugin = PLUGIN + 'dependencies: { acme/protein: { types: { Protein: ">=1" } } }\n'

    def check(include: str) -> Result:
        other = action(
            inputs=f"{{ protein: {{ type: acme/protein.Protein, include: {include} }} }}",
            effects="",
            artifacts="{ keep: [out.txt] }",
        )
        return make(tmp_path, other, files={"plugin.yaml": plugin}, with_=(DNA, PROTEIN))

    # A reference, and one to any entity.
    result = check("[encoded_by, source]")
    assert result.ok, [(p.code, p.message) for p in result.problems]
    assert result.plugin is not None
    made = result.plugin.actions["score"]
    assert made.inputs["protein"].include == ("encoded_by", "source")
    message = only(check("[nope]"), "include-unknown-field")
    assert message == "`acme/protein.Protein` has no field `nope`."
    assert check("[nope]").problems[0].hint == (
        "Name one of its references: `encoded_by`, `organism`, `source`."
    )
    assert only(check("[sequence]"), "include-not-reference").startswith(
        "`sequence` isn't a reference"
    )
    assert "schema" in codes(check("[encoded_by, encoded_by]"))[0]


def test_a_list_of_references_is_included_item_by_item(tmp_path: Path) -> None:
    plugin = PLUGIN + 'dependencies: { acme/protein: { types: { ProteinStructure: ">=1" } } }\n'
    other = action(
        inputs="{ structure: { type: acme/protein.ProteinStructure, include: [chains] } }",
        effects="",
        artifacts="{ keep: [out.txt] }",
    )
    result = make(tmp_path, other, files={"plugin.yaml": plugin}, with_=(DNA, PROTEIN))
    assert result.ok, [(p.code, p.message) for p in result.problems]


def test_inputs_from_other_plugins_need_a_dependency(tmp_path: Path) -> None:
    other = action(
        inputs="{ seq: { type: acme/dna.DNASequence } }",
        effects="",
        artifacts="{ keep: [out.txt] }",
    )
    assert only(make(tmp_path, other, with_=(DNA,)), "undeclared-dependency")


def test_params(tmp_path: Path) -> None:
    params = (
        "\n  depth: { type: int, default: 0, min: 1 }"
        "\n  api_key: { type: string }"
        "\n  assay: { type: bool }"
        "\n  mode: { type: enum, values: [a, b], default: a, required: true }"
        "\n  temp: { type: quantity, unit: degF }"
    )
    result = make(tmp_path, action(params=params))
    assert sorted(codes(result)) == [
        "duplicate-name",
        "invalid-default",
        "param-looks-secret",
        "required-with-default",
        "unknown-unit",
    ]


def test_params_take_dates_and_quantities(tmp_path: Path) -> None:
    params = (
        "\n  since: { type: date, default: '2026-01-01' }"
        "\n  temp: { type: quantity, unit: Cel, default: 37 }"
        "\n  tags: { type: list, items: { type: string }, default: [a] }"
    )
    assert make(tmp_path, action(params=params)).problems == []


def test_params_take_files_to_import(tmp_path: Path) -> None:
    params = (
        "\n  file: { type: file, required: true, media_types: [chemical/seq-na-fasta],"
        " max_size: 1MiB }"
    )
    assert make(tmp_path, action(params=params)).problems == []


def test_params_take_several_files(tmp_path: Path) -> None:
    params = (
        "\n  reads: { type: list, required: true, max_items: 96,"
        " items: { type: file, media_types: [application/octet-stream], max_size: 10MiB } }"
    )
    assert make(tmp_path, action(params=params)).problems == []


@pytest.mark.parametrize(
    "param",
    [
        "{ type: ref, to: Assay }",  # entities are inputs, checked as inputs are
        "{ type: file, default: 0190e9a4-0000-7000-8000-000000000000 }",
        "{ type: list, items: { type: file }, default: [0190e9a4-0000-7000-8000-000000000000] }",
    ],
)
def test_params_cant_be_refs_or_default_files(tmp_path: Path, param: str) -> None:
    result = make(tmp_path, action(params=f"{{ f: {param} }}"))
    assert codes(result) == ["schema"]


def test_creates(tmp_path: Path) -> None:
    creates = "{ creates: [{ type: Base }, { type: Readout, derived_from: [asay] }] }"
    result = make(tmp_path, action(effects=creates))
    assert sorted(codes(result)) == ["creates-abstract", "unknown-input"]
    unknown = next(p for p in result.problems if p.code == "unknown-input")
    assert unknown.hint == "Did you mean `assay`?"


@pytest.mark.parametrize(
    ("field", "code"),
    [("barcode", "match-on-not-unique"), ("nope", "match-on-unknown-field")],
)
def test_creates_match_on_a_unique_field(tmp_path: Path, field: str, code: str) -> None:
    creates = f"{{ creates: [{{ type: Assay, cardinality: many, match_on: {field} }}] }}"
    assert codes(make(tmp_path, action(effects=creates))) == [code]


def test_an_extensions_unique_field_can_be_matched_on() -> None:
    result = load_plugin(PREDICTION, dependencies=[PROTEIN, DNA])
    assert result.problems == []
    assert result.plugin is not None
    predict = result.plugin.actions["predict_structure"]
    assert predict.creates[0].match_on == "job_id"


def test_imports_create_without_inputs(tmp_path: Path) -> None:
    fetch = action(
        inputs="",
        effects="{ creates: [{ type: Readout, cardinality: many }] }",
        network="[eutils.ncbi.nlm.nih.gov, '*.ncbi.nlm.nih.gov']",
        secrets="[NCBI_API_KEY]",
    )
    result = make(tmp_path, fetch)
    assert result.problems == []
    assert result.plugin is not None
    imported = result.plugin.actions["score"]
    # Runs that reach the network aren't retried unless they say so.
    assert imported.retry_max == 0


def test_an_action_must_leave_something(tmp_path: Path) -> None:
    assert only(make(tmp_path, action(effects="")), "action-does-nothing")
    kept = action(effects="", artifacts="{ keep: [report.pdf], retention: 90d }")
    assert make(tmp_path, kept).problems == []


def test_updates(tmp_path: Path) -> None:
    updates = (
        "{ updates: [{ input: assay, fields: [score, barcode, old, scor] },"
        " { input: nope, fields: [x] }] }"
    )
    result = make(tmp_path, action(effects=updates))
    assert codes(result) == ["updates-immutable", "unknown-field", "unknown-field", "unknown-input"]
    messages = [p.message for p in result.problems]
    assert messages[1] == "`acme/lab.Assay` has no field `old` any more (it was removed)."
    assert messages[2] == "`acme/lab.Assay` has no field `scor`."
    assert result.problems[2].hint == "Did you mean `score`?"
    twice = "{ updates: [{ input: assay, fields: [score] }, { input: assay, fields: [score] }] }"
    assert only(make(tmp_path, action(effects=twice)), "duplicate-updates")


def test_updates_leave_state_fields_to_people(tmp_path: Path) -> None:
    status = (
        "  status:\n    type: state\n    initial: open\n"
        "    states: { open: {}, done: { set_by: [qa] } }\n"
    )
    files = {
        "plugin.yaml": PLUGIN + "roles:\n  qa: { label: QA }\n",
        "types/types.yaml": TYPES.replace(
            "  old: { type: int, removed: true }\n",
            "  old: { type: int, removed: true }\n" + status,
        ),
    }
    updates = "{ updates: [{ input: assay, fields: [score, status] }] }"
    result = make(tmp_path, action(effects=updates), files=files)
    assert "set it, one revision each" in only(result, "updates-state")


def test_updates_reach_inherited_and_extension_fields(tmp_path: Path) -> None:
    plugin = PLUGIN + 'dependencies: { acme/protein: { types: { Protein: ">=1" } } }\n'
    other = action(
        inputs="{ protein: { type: acme/protein.Protein } }",
        effects="{ updates: [{ input: protein, fields: [molecular_weight, acme_score] }] }",
    )
    extension = (
        "apiVersion: iki/v1\nkind: EntityTypeExtension\nextends_type: acme/protein.Protein\n"
        "fields: { acme_score: { type: float, computed: true } }\n"
    )
    result = make(
        tmp_path,
        other,
        files={"plugin.yaml": plugin, "types/protein.yaml": extension},
        with_=(PROTEIN, DNA),
    )
    assert result.problems == []


def test_runs_in_an_environment_that_supports_its_tier(tmp_path: Path) -> None:
    result = make(tmp_path, action(runs_on="{ tier: backend, environment: tool }"))
    assert only(result, "unknown-environment")
    assert result.problems[0].hint == "Did you mean `tools`?"
    result = make(tmp_path, action(runs_on="{ tier: vm, environment: tools }"))
    message = only(result, "tier-not-supported")
    assert message == "`tools` doesn't run on the vm tier; it runs on backend, self_hosted."


def test_lifecycles_are_for_provisioned_tiers(tmp_path: Path) -> None:
    result = make(
        tmp_path, action(runs_on="{ tier: backend, environment: tools, lifecycle: persistent }")
    )
    assert only(result, "lifecycle-wrong-tier")
    vm = make(tmp_path, action(runs_on="{ tier: vm, environment: gpu }"), GPU)
    assert vm.problems == []
    assert vm.plugin is not None
    assert vm.plugin.actions["score"].lifecycle == "ephemeral"


def test_the_self_hosted_tier(tmp_path: Path) -> None:
    hosted = action(
        runs_on="{ tier: self_hosted, environment: tools, resources: { cpu: 2 } }",
        secrets="[TOKEN_A]",
        network="[example.org]",
    )
    result = make(tmp_path, hosted)
    assert sorted(codes(result)) == ["self-hosted-network", "self-hosted-secrets"]
    # Its workers say what they have, so it may need more, GPUs included, and run software
    # the machine has.
    sized = action(
        runs_on="{ tier: self_hosted, environment: tools, resources: { gpu: { count: 1 } } }",
        command="[/opt/vendor/bin/acquire]",
    )
    assert make(tmp_path, sized).problems == []


def test_commands(tmp_path: Path) -> None:
    result = make(tmp_path, action(command="[python, ./missing.py]"))
    assert only(result, "missing-file")
    result = make(tmp_path, action(command="[/usr/bin/python3, run.py]"))
    assert only(result, "absolute-path-on-backend")


def test_reserved_secrets(tmp_path: Path) -> None:
    result = make(tmp_path, action(secrets="[IKI_TOKEN, PATH, NCBI_API_KEY]"))
    assert codes(result) == ["reserved-secret", "reserved-secret"]
    proxy = make(tmp_path, action(secrets="[HTTPS_PROXY]"))
    assert codes(proxy) == ["reserved-secret"]


def test_secrets_can_be_optional_and_say_what_theyre_for(tmp_path: Path) -> None:
    declared = (
        "[ LIMS_TOKEN, { name: NCBI_API_KEY, required: false, description: Raises NCBI's "
        "rate limit. } ]"
    )
    result = make(tmp_path, action(secrets=declared))
    assert result.problems == []
    assert result.plugin is not None
    (found,) = result.plugin.actions.values()
    assert [(s.name, s.required, s.description) for s in found.secrets] == [
        ("LIMS_TOKEN", True, ""),
        ("NCBI_API_KEY", False, "Raises NCBI's rate limit."),
    ]
    twice = make(tmp_path, action(secrets="[LIMS_TOKEN, { name: LIMS_TOKEN, required: false }]"))
    assert codes(twice) == ["secret-twice"]
    assert codes(make(tmp_path, action(secrets="[{ name: lims }]"))) == ["schema"]


def test_an_action_lists_the_software_it_makes_entities_with(tmp_path: Path) -> None:
    declared = (
        "[{ name: ImmuneBuilder, version: '1.2', url: 'https://github.com/oxpig/ImmuneBuilder' },"
        " { name: OpenMM, version: 8.6.1 }]"
    )
    result = make(tmp_path, action(software=declared))
    assert result.problems == []
    assert result.plugin is not None
    (found,) = result.plugin.actions.values()
    assert [(w.name, w.version, w.url) for w in found.software] == [
        ("ImmuneBuilder", "1.2", "https://github.com/oxpig/ImmuneBuilder"),
        ("OpenMM", "8.6.1", ""),
    ]
    # As a run keeps it.
    from dataclasses import asdict

    from iki_spec.model import action_from_dict

    assert action_from_dict(asdict(found)).software == found.software
    # As a build's manifest keeps it.
    data = json.loads(json.dumps(plugin_to_dict(result.plugin)))
    assert plugin_from_dict(data).actions[found.name].software == found.software
    twice = "[{ name: OpenMM, version: '8.6' }, { name: openmm, version: 8.6.1 }]"
    assert only(make(tmp_path, action(software=twice)), "duplicate-software")
    # A tool needs its version, and names have no space at either end.
    for bad in ("[{ name: OpenMM }]", "[{ name: ' OpenMM', version: '8' }]"):
        assert codes(make(tmp_path, action(software=bad))) == ["schema"]


def test_timeouts(tmp_path: Path) -> None:
    assert only(make(tmp_path, action(timeout="8d")), "timeout-too-long")
    assert make(tmp_path, action(timeout="7d")).problems == []


def test_batches_take_one_input(tmp_path: Path) -> None:
    assert make(tmp_path, action(batchable="50")).problems == []
    many = action(batchable="50", inputs="{ assay: { type: Assay, cardinality: many } }")
    assert only(make(tmp_path, many), "batch-needs-one-input")
    keeping = action(batchable="50", artifacts='{ keep: ["*.log"] }')
    assert only(make(tmp_path, keeping), "batch-keeps-artifacts")
    saving = action(batchable="50", checkpointable="true")
    assert only(make(tmp_path, saving), "batch-checkpoints")


def test_a_setting_can_give_a_host(tmp_path: Path) -> None:
    plugin = PLUGIN + (
        "settings:\n"
        "  lims_host: { type: string, format: hostname, default: lims.example.org }\n"
        "  lims_url: { type: string, format: uri }\n"
    )
    reaching = action(network='["{settings.lims_host}", api.example.org]')
    result = make(tmp_path, reaching, files={"plugin.yaml": plugin})
    assert result.problems == []
    assert result.plugin is not None
    (found,) = result.plugin.actions.values()
    assert found.network == ("{settings.lims_host}", "api.example.org")
    unknown = make(
        tmp_path, action(network='["{settings.lims_hots}"]'), files={"plugin.yaml": plugin}
    )
    assert only(unknown, "network-unknown-setting")
    assert unknown.problems[0].hint == "Did you mean `lims_host`?"
    not_host = make(
        tmp_path, action(network='["{settings.lims_url}"]'), files={"plugin.yaml": plugin}
    )
    assert only(not_host, "network-setting-not-hostname")
    # Only a whole entry: a setting can't be part of a host.
    part = make(tmp_path, action(network='["{settings.lims_host}.org"]'))
    assert only(part, "schema")


def test_an_action_can_need_a_setting_a_deployment_may_leave_unset(tmp_path: Path) -> None:
    plugin = PLUGIN + (
        "settings:\n"
        "  ncbi_email: { type: string, format: email }\n"
        "  batch_size: { type: int, min: 1, default: 100 }\n"
        "  lab: { type: string, required: true }\n"
    )
    files = {"plugin.yaml": plugin}
    result = make(tmp_path, action(requires_settings="[ncbi_email]"), files=files)
    assert result.problems == []
    assert result.plugin is not None
    (found,) = result.plugin.actions.values()
    assert found.requires_settings == ("ncbi_email",)
    unknown = make(tmp_path, action(requires_settings="[ncbi_mail]"), files=files)
    assert only(unknown, "requires-unknown-setting") == "The plugin has no setting `ncbi_mail`."
    assert unknown.problems[0].hint == "Did you mean `ncbi_email`?"
    # One every deployment has is no prerequisite.
    for name, why in (("batch_size", "given a default"), ("lab", "required")):
        always = make(tmp_path, action(requires_settings=f"[{name}]"), files=files)
        assert only(always, "requires-setting-always-set") == (
            f"`{name}` is {why}, so every deployment has it."
        )
        assert always.ok
    assert only(make(tmp_path, action(requires_settings="[Bad-Name]"), files=files), "schema")


def test_a_deployment_is_told_which_actions_its_settings_leave_unable_to_run(
    tmp_path: Path,
) -> None:
    plugin = PLUGIN + (
        "settings:\n"
        "  ncbi_email:\n"
        "    type: string\n"
        "    format: email\n"
        "    description: The address NCBI asks automated clients to give.\n"
    )
    found = make(
        tmp_path,
        action(label="Score assays", requires_settings="[ncbi_email]"),
        files={"plugin.yaml": plugin},
    ).plugin
    assert found is not None
    (unset,) = check_settings_text(found, "", "acme.yaml")
    assert (unset.code, unset.severity, unset.message, unset.hint) == (
        "setting-needed",
        Severity.WARNING,
        "`ncbi_email` isn't set, so Score assays can't run on this deployment.",
        "The address NCBI asks automated clients to give.",
    )
    blank = check_settings_text(found, "ncbi_email: ''\n", "acme.yaml")
    assert "setting-needed" in [p.code for p in blank]
    assert check_settings_text(found, "ncbi_email: lab@example.org\n", "acme.yaml") == []


def test_deterministic_warnings(tmp_path: Path) -> None:
    result = make(tmp_path, action(deterministic="true", network="[example.org]"))
    assert only(result, "deterministic-with-network")
    assert result.ok
    retried = action(deterministic="true", retries="{ max: 1, on: [failed] }")
    result = make(tmp_path, retried)
    assert only(result, "retries-deterministic-failure")
    assert result.ok


# --- Outside APIs (ADR 0130) -------------------------------------------------------------------

API = """\
apis:
  fold:
    label: FoldCloud
    host: fold.example.com
    purpose: Predicts structures.
    credential: { secret: FOLD_KEY }
    reports:
      model: /model
      version: header:x-version
      parameters: [/recycles]
      usage: { gpu_seconds: /usage/gpu_seconds }
    jobs:
      poll: /v1/jobs/{id}
      status: /status
      done: [succeeded]
      failed: [failed]
      cancel: /v1/jobs/{id}/cancel
      every: 10s
      timeout: 2d
      webhook: { signature: standard-webhooks, secret: FOLD_HOOK }
"""

USES = """\
apis:
  fold:
    sends:
      inputs: { assay: [barcode] }
"""


def test_a_plugin_declares_its_apis_and_actions_say_what_they_send(tmp_path: Path) -> None:
    result = make(tmp_path, ACTION + USES, files={"plugin.yaml": PLUGIN + API})
    assert result.problems == []
    assert result.plugin is not None
    api = result.plugin.apis["fold"]
    assert api.qualified_name == "acme/lab.fold"
    assert api.credential is not None
    assert (api.credential.header, api.credential.prefix) == ("Authorization", "Bearer ")
    assert api.jobs is not None
    assert (api.jobs.every, api.jobs.timeout) == (10, 2 * 86400)
    (use,) = result.plugin.actions["score"].apis
    assert (use.name, use.inputs, use.params, use.api) == ("fold", {"assay": ("barcode",)}, (), api)
    # A run keeps its action as JSON, with the API it calls, and reads it back the same.
    again = plugin_from_dict(plugin_to_dict(result.plugin))
    assert again == result.plugin
    action = result.plugin.actions["score"]
    assert action_from_dict(dataclasses.asdict(action)) == action
    # Runs that call outside APIs aren't retried by default: they'd pay again.
    assert action.retry_max == 0


def test_a_header_credential_has_no_prefix_unless_it_says(tmp_path: Path) -> None:
    api = API.replace(
        "credential: { secret: FOLD_KEY }", "credential: { secret: FOLD_KEY, header: X-Api-Key }"
    )
    result = make(tmp_path, ACTION + USES, files={"plugin.yaml": PLUGIN + api})
    assert result.plugin is not None
    credential = result.plugin.apis["fold"].credential
    assert credential is not None
    assert (credential.header, credential.prefix) == ("X-Api-Key", "")


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (("every: 10s", "every: 2s"), "api-polls-too-often"),
        (("timeout: 2d", "timeout: 8d"), "api-job-timeout"),
        (("failed: [failed]", "failed: [failed, succeeded]"), "api-status-twice"),
        (("signature: standard-webhooks", "signature: hmac-sha256"), "webhook-needs-header"),
        (("FOLD_HOOK }", "FOLD_HOOK, header: X-Sig }"), "webhook-header"),
        (("secret: FOLD_HOOK", "secret: FOLD_KEY"), "webhook-secret-is-credential"),
        (("{ secret: FOLD_KEY }", "{ secret: FOLD_KEY, header: Host }"), "api-credential-header"),
        (("host: fold.example.com", "host: '{settings.fold_host}'"), "network-unknown-setting"),
    ],
)
def test_an_apis_declaration_is_checked(tmp_path: Path, change: tuple[str, str], code: str) -> None:
    api = API.replace(*change)
    assert api != API
    assert only(make(tmp_path, ACTION + USES, files={"plugin.yaml": PLUGIN + api}), code)


def test_the_schema_refuses_a_wildcard_host_and_a_path_without_the_id(tmp_path: Path) -> None:
    wildcard = API.replace("host: fold.example.com", "host: '*.example.com'")
    assert "schema" in codes(
        make(tmp_path, ACTION + USES, files={"plugin.yaml": PLUGIN + wildcard})
    )
    for poll in ("/v1/jobs", "/v1/jobs/{id}/{id}"):
        no_id = API.replace("poll: /v1/jobs/{id}", f"poll: {poll}")
        found = make(tmp_path, ACTION + USES, files={"plugin.yaml": PLUGIN + no_id})
        assert "schema" in codes(found)


@pytest.mark.parametrize(
    ("action", "code"),
    [
        (ACTION + USES.replace("assay: [barcode]", "sample: [barcode]"), "unknown-input"),
        (ACTION + USES.replace("[barcode]", "[barcod]"), "unknown-field"),
        (ACTION + USES.replace("inputs: { assay: [barcode] }", "params: [depth]"), "unknown-param"),
        (ACTION + USES + "network: [fold.example.com]\n", "api-in-network"),
        (ACTION + USES + "secrets: [FOLD_KEY]\n", "api-credential-is-secret"),
        (ACTION + USES + "secrets: [FOLD_HOOK]\n", "api-credential-is-secret"),
        (ACTION.replace("tier: backend", "tier: self_hosted") + USES, "self-hosted-apis"),
        (ACTION + USES + "deterministic: true\n", "deterministic-with-api"),
    ],
)
def test_what_an_action_sends_is_checked(tmp_path: Path, action: str, code: str) -> None:
    assert only(make(tmp_path, action, files={"plugin.yaml": PLUGIN + API}), code)


def test_a_setup_build_cant_call_an_outside_api(tmp_path: Path) -> None:
    """A setup build belongs to the deployment: no project to approve an API for, nobody to
    consent, none to bill (ADR 0141). So it may neither name an API nor download from one's
    host, given as it is or by the same setting."""
    vm = action(runs_on="{ tier: vm, environment: gpu }")
    volumes = "volumes:\n  weights: { size: 100Gi, mount: /weights, populated_by: setup }\n"
    files = {"plugin.yaml": PLUGIN + API, "setup.sh": ""}
    fine = "setup: { script: setup.sh, network: [weights.example.org] }\n"
    assert codes(make(tmp_path, vm + USES, GPU + volumes + fine, files=files)) == []
    to_api = fine.replace("weights.example.org", "fold.example.com")
    message = only(
        make(tmp_path, vm + USES, GPU + volumes + to_api, files=files), "setup-calls-api"
    )
    assert message.startswith(
        "`fold.example.com` is FoldCloud's host, an outside API, and setup builds can't call "
        "outside APIs"
    )
    by_setting = PLUGIN + "settings:\n  fold_host: { type: string, format: hostname }\n"
    by_setting += API.replace("host: fold.example.com", "host: '{settings.fold_host}'")
    setting = fine.replace("weights.example.org", "'{settings.fold_host}'")
    found = make(
        tmp_path, vm + USES, GPU + volumes + setting, files={**files, "plugin.yaml": by_setting}
    )
    assert codes(found) == ["setup-calls-api"]
    unknown = fine.replace("weights.example.org", "'{settings.weights_host}'")
    found = make(tmp_path, vm + USES, GPU + volumes + unknown, files=files)
    assert codes(found) == ["network-unknown-setting"]
    assert found.problems[0].path == ("setup", "network", 0)
    # Naming an API under the setup is refused, saying why rather than only that it's unknown.
    named = "setup:\n  script: setup.sh\n  apis: { fold: {} }\n"
    found = make(tmp_path, vm + USES, GPU + volumes + named, files=files)
    # (The environment doesn't load, so the action can't find it.)
    assert codes(found) == ["unknown-environment", "schema"]
    problem = found.problems[1]
    assert problem.path == ("setup", "apis")
    assert problem.message.startswith("Setup builds can't call outside APIs: a build belongs")
    assert problem.hint is not None and problem.hint.endswith("list it under `network`.")


def test_an_action_calls_only_its_plugins_apis(tmp_path: Path) -> None:
    result = make(
        tmp_path, ACTION + USES.replace("fold:", "folds:"), files={"plugin.yaml": PLUGIN + API}
    )
    assert codes(result) == ["unknown-api", "unused-api"]


def test_a_batchable_action_cant_call_an_api_it_parks_on(tmp_path: Path) -> None:
    single = ACTION.replace(
        "effects:\n  updates: [{ input: assay, fields: [score] }]\n",
        "effects:\n  updates: [{ input: assay, fields: [score] }]\nbatchable: 10\n",
    )
    assert only(make(tmp_path, single + USES, files={"plugin.yaml": PLUGIN + API}), "batch-parks")


def test_an_api_no_action_calls_is_warned_about(tmp_path: Path) -> None:
    assert only(make(tmp_path, ACTION, files={"plugin.yaml": PLUGIN + API}), "unused-api")


def test_the_example_plugins_api(tmp_path: Path) -> None:
    result = load_plugin(PLUGINS / "protein", dependencies=[PLUGINS / "dna"])
    assert result.problems == []
    assert result.plugin is not None
    (use,) = result.plugin.actions["predict_hosted"].apis
    assert use.api is not None and use.api.host == "fold.models.example"


def test_compute_config_prices_apis(tmp_path: Path) -> None:
    protein = load_plugin(PLUGINS / "protein", dependencies=[PLUGINS / "dna"]).plugin
    assert protein is not None
    file = tmp_path / "compute.yaml"
    file.write_text(
        "apiVersion: iki/v1\nkind: ApiPrice\napi: acme/protein.fold\nper_call: 0.02\n"
        "usage: { gpu_seconds: 0.001 }\ncurrency: USD\n"
    )
    result = load_compute(file, [protein])
    assert [p.code for p in result.problems if p.code != "unbound-environment"] == []
    assert result.compute is not None
    price = result.compute.prices["acme/protein.fold"]
    assert (price.per_call, price.usage, price.currency) == (0.02, {"gpu_seconds": 0.001}, "USD")
    assert compute_from_dict(compute_to_dict(result.compute)) == result.compute

    file.write_text(
        "apiVersion: iki/v1\nkind: ApiPrice\napi: acme/protein.fold\nusage: { tokens: 1 }\n"
        "currency: USD\n---\napiVersion: iki/v1\nkind: ApiPrice\napi: acme/protein.flod\n"
        "per_call: 1\ncurrency: EUR\n"
    )
    found = sorted(p.code for p in load_compute(file, [protein]).problems)
    assert found == ["mixed-currencies", "unbound-environment", "unknown-api", "unknown-usage"]
    file.write_text("")
    assert "unpriced-api" in [p.code for p in load_compute(file, [protein]).problems]


# --- Reading answers ---------------------------------------------------------------------------


def test_a_locator_reads_a_json_pointer_or_a_header() -> None:
    answer = {"model": "fold-2", "usage": {"gpu_seconds": 12.5}, "a/b": [{"~x": 1}]}
    assert apis.locate("/model", [answer]) == "fold-2"
    assert apis.locate("/usage/gpu_seconds", [answer]) == 12.5
    assert apis.locate("/a~1b/0/~0x", [answer]) == 1
    assert apis.locate("/a~1b/01/~0x", [answer]) is None
    assert apis.locate("/missing", [answer]) is None
    assert apis.locate("header:X-Version", [], {"x-version": "2.1"}) == "2.1"
    # A streamed answer's events: the last one that has it.
    events = [{"model": "a"}, {"delta": "…"}, {"model": "b", "usage": {"tokens": 3}}, {}]
    assert apis.locate("/model", events) == "b"
    assert apis.locate("/usage/tokens", events) == 3


def test_a_job_id_goes_into_its_path_escaped() -> None:
    assert apis.job_path("/v1/jobs/{id}/cancel", "abc:1.2") == "/v1/jobs/abc%3A1.2/cancel"
    for bad in ("", "../x", "a b", "a/b", "x" * 201):
        with pytest.raises(ValueError, match="job ID"):
            apis.job_path("/v1/jobs/{id}", bad)


def test_kept_values_are_short() -> None:
    assert apis.short(3) == 3
    assert apis.short({"a": 1}) == '{"a":1}'
    assert apis.short("x" * 2000, 10) == "x" * 9 + "…"


def test_a_receipt_declares_a_single_container_and_a_fresh_lot(tmp_path: Path) -> None:
    from iki_spec import outputs, schemas

    types = (
        TYPES
        + """
---
apiVersion: iki/v1
kind: EntityType
name: Lot
schema_version: 1
hrid: { prefix: LOT }
amount: { unit: mg }
storage: {}
fields:
  delivery_key: { type: string, indexed: true, unique: true, max_length: 100 }
---
apiVersion: iki/v1
kind: EntityType
name: Bin
schema_version: 1
hrid: { prefix: BIN }
storage: { holds: [Lot] }
"""
    )

    def receiving(dest: str, effect: str) -> Result:
        return make(
            tmp_path,
            action(
                inputs=f"{{ storage: {{ {dest} }} }}", effects=f"{{ creates: [{{ {effect} }}] }}"
            ),
            files={"types/types.yaml": types},
        )

    effect = "type: Lot, receive_into: storage"
    assert only(receiving("type: Bin, optional: true", effect), "receipt-container-input")
    assert only(receiving("type: Assay", effect), "receipt-container-type")
    assert only(receiving("type: Bin", effect + ", match_on: delivery_key"), "receipt-match")
    result = receiving("type: Bin", effect)
    assert result.problems == []
    assert result.plugin is not None
    plugin = result.plugin
    declared = plugin.actions["score"]
    assert declared.creates[0].receive_into == "storage"
    checked = outputs.SpecTypes({t.qualified_name: t for t in plugin.types.values()}, ())
    receipt = {"amount": {"value": "1.5", "unit": "mg"}, "reason": "Delivery D-42"}
    row = {"type": "acme/lab.Lot", "values": {}, "receipt": receipt}
    assert not outputs.check_creates(declared, [row], checked)
    missing = outputs.check_creates(declared, [{"type": "acme/lab.Lot", "values": {}}], checked)
    assert len(missing) == 1 and missing[0].pointer == "#/creates/0/receipt"
    ordinary = dataclasses.replace(
        declared, creates=(dataclasses.replace(declared.creates[0], receive_into=None),)
    )
    assert outputs.check_creates(ordinary, [row], checked)[0].pointer == "#/creates/0/receipt"
    assert outputs.check_creates_file(declared, [json.dumps(row)], [], checked)[0].pointer.endswith(
        "/receipt"
    )
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "creates": [row]}
    assert not list(schemas.check("run-output", output))
    bad = {**row, "receipt": {**receipt, "destination": "an-unchecked-container"}}
    assert list(schemas.check("run-output", {**output, "creates": [bad]}))
