"""Actions and environments: their schemas, and the rules beyond them."""

import json
import os
import shutil
import textwrap
from pathlib import Path

import pytest

from iki_spec.model import parse_duration, parse_quantity, plugin_from_dict, plugin_to_dict
from iki_spec.problems import Severity
from iki_spec.validate import Result, load_plugin

PLUGINS = Path(__file__).parent / "plugins"
DNA, PROTEIN, PREDICTION = PLUGINS / "dna", PLUGINS / "protein", PLUGINS / "structure_prediction"

PLUGIN = """\
apiVersion: iki/v1
kind: Plugin
name: acme
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
tiers: [backend, client]
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
    assert compute.qualified_name == "protein.compute_properties"
    assert compute.inputs["protein"].type == "protein.Protein"
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


def test_actions_and_environments_survive_json() -> None:
    result = load_plugin(PREDICTION, dependencies=[PROTEIN, DNA])
    assert result.plugin is not None
    data = json.loads(json.dumps(plugin_to_dict(result.plugin)))
    assert plugin_from_dict(data) == result.plugin


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


def test_the_client_can_run_an_image(tmp_path: Path) -> None:
    env = (
        "apiVersion: iki/v1\nkind: Environment\nname: tools\ntiers: [client]\n"
        "image: ghcr.io/acme/tool@sha256:" + "b" * 64 + "\n"
    )
    client = action(runs_on="{ tier: client, environment: tools }", command="[/app/run]")
    assert make(tmp_path, client, env).problems == []


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
    assert message == "`acme.Assay` is at schema version 3, which `>=4` doesn't allow."
    result = make(tmp_path, action(inputs="{ assay: { type: Assay, max: 10 } }"))
    assert only(result, "max-needs-many")


def test_inputs_from_other_plugins_need_a_dependency(tmp_path: Path) -> None:
    other = action(
        inputs="{ seq: { type: dna.DNASequence } }", effects="", artifacts="{ keep: [out.txt] }"
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


@pytest.mark.parametrize(
    "param",
    [
        "{ type: ref, to: Assay }",  # entities are inputs, checked as inputs are
        "{ type: file, default: 0190e9a4-0000-7000-8000-000000000000 }",
        "{ type: list, items: { type: file } }",
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
    assert messages[1] == "`acme.Assay` has no field `old` any more (it was removed)."
    assert messages[2] == "`acme.Assay` has no field `scor`."
    assert result.problems[2].hint == "Did you mean `score`?"
    twice = "{ updates: [{ input: assay, fields: [score] }, { input: assay, fields: [score] }] }"
    assert only(make(tmp_path, action(effects=twice)), "duplicate-updates")


def test_updates_reach_inherited_and_extension_fields(tmp_path: Path) -> None:
    plugin = PLUGIN + 'dependencies: { protein: { types: { Protein: ">=1" } } }\n'
    other = action(
        inputs="{ protein: { type: protein.Protein } }",
        effects="{ updates: [{ input: protein, fields: [molecular_weight, acme_score] }] }",
    )
    extension = (
        "apiVersion: iki/v1\nkind: EntityTypeExtension\nextends_type: protein.Protein\n"
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
    assert message == "`tools` doesn't run on the vm tier; it runs on backend, client."


def test_lifecycles_are_for_provisioned_tiers(tmp_path: Path) -> None:
    result = make(
        tmp_path, action(runs_on="{ tier: backend, environment: tools, lifecycle: persistent }")
    )
    assert only(result, "lifecycle-wrong-tier")
    vm = make(tmp_path, action(runs_on="{ tier: vm, environment: gpu }"), GPU)
    assert vm.problems == []
    assert vm.plugin is not None
    assert vm.plugin.actions["score"].lifecycle == "ephemeral"


def test_the_client_tier(tmp_path: Path) -> None:
    client = action(
        runs_on="{ tier: client, environment: tools, resources: { cpu: 2 } }",
        secrets="[TOKEN_A]",
        network="[example.org]",
    )
    result = make(tmp_path, client)
    assert sorted(codes(result)) == ["client-network", "client-resources", "client-secrets"]


def test_commands(tmp_path: Path) -> None:
    result = make(tmp_path, action(command="[python, ./missing.py]"))
    assert only(result, "missing-file")
    result = make(tmp_path, action(command="[/usr/bin/python3, run.py]"))
    assert only(result, "absolute-path-on-backend")
    client = action(runs_on="{ tier: client, environment: tools }", command="[/opt/run]")
    assert only(make(tmp_path, client), "absolute-path-on-client")


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


def test_timeouts(tmp_path: Path) -> None:
    assert only(make(tmp_path, action(timeout="8d")), "timeout-too-long")
    assert make(tmp_path, action(timeout="7d")).problems == []


def test_batches_take_one_input(tmp_path: Path) -> None:
    assert make(tmp_path, action(batchable="50")).problems == []
    many = action(batchable="50", inputs="{ assay: { type: Assay, cardinality: many } }")
    assert only(make(tmp_path, many), "batch-needs-one-input")
    keeping = action(batchable="50", artifacts='{ keep: ["*.log"] }')
    assert only(make(tmp_path, keeping), "batch-keeps-artifacts")


def test_deterministic_warnings(tmp_path: Path) -> None:
    result = make(tmp_path, action(deterministic="true", network="[example.org]"))
    assert only(result, "deterministic-with-network")
    assert result.ok
    retried = action(deterministic="true", retries="{ max: 1, on: [failed] }")
    result = make(tmp_path, retried)
    assert only(result, "retries-deterministic-failure")
    assert result.ok
