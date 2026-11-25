"""A plugin's action tests (ADR 0093): fixtures checked against it, the run input made from
each, and outputs compared with what they expect."""

import dataclasses
import shutil
from pathlib import Path

import pytest

from iki_spec import action_tests, outputs
from iki_spec.builtin import core_plugin
from iki_spec.model import Creates, EntityType, Input, Plugin, Updates
from iki_spec.validate import load_plugin

PLUGINS = Path(__file__).parent / "plugins"


@pytest.fixture
def protein(tmp_path: Path) -> tuple[Path, Plugin, dict[str, EntityType]]:
    shutil.copytree(PLUGINS / "protein", tmp_path / "protein")
    result = load_plugin(tmp_path / "protein", dependencies=[PLUGINS / "dna"])
    assert result.plugin is not None, result.problems
    dna = load_plugin(PLUGINS / "dna").plugin
    assert dna is not None
    types = {t.qualified_name: t for t in core_plugin().types.values()}
    types.update({t.qualified_name: t for t in dna.types.values()})
    return tmp_path / "protein", result.plugin, types


def write(directory: Path, name: str, text: str) -> None:
    (directory / "tests").mkdir(exist_ok=True)
    (directory / "tests" / name).write_text(text)


HEADER = "apiVersion: iki/v1\nkind: ActionTest\n"


def test_after_names_one_expected_value_map_per_upgrade_sample(
    protein: tuple[Path, Plugin, dict[str, EntityType]],
) -> None:
    directory, plugin, types = protein
    fixture = (
        HEADER + "action: compute_properties\ninputs: { protein: [{values: {sequence: MKV}}] }\n"
    )
    write(directory, "backfill.yaml", fixture + "after: { protein: [{}] }\n")
    loaded = action_tests.load_tests(directory, plugin, types)
    assert any(p.code == "schema" for p in loaded.problems)
    write(
        directory,
        "backfill.yaml",
        fixture + "upgrade: true\nafter: { protein: [], missing: [{}] }\n",
    )
    loaded = action_tests.load_tests(directory, plugin, types)
    assert [p.code for p in loaded.problems] == ["upgrade-after", "upgrade-after"]
    write(
        directory,
        "backfill.yaml",
        fixture + "upgrade: true\nafter: { protein: [{ molecular_weight: 0 }] }\n",
    )
    loaded = action_tests.load_tests(directory, plugin, types)
    assert loaded.ok, loaded.problems
    assert loaded.tests[0].after == {"protein": [{"molecular_weight": 0}]}


def test_fixtures_are_checked_against_the_plugin(
    protein: tuple[Path, Plugin, dict[str, EntityType]],
) -> None:
    directory, plugin, types = protein
    write(
        directory,
        "bad.yaml",
        HEADER
        + "action: compute_properties\n"
        + "params: { ph: 7 }\n"
        + "inputs:\n"
        + "  protein:\n"
        + "    - values: { sequence: MKV, weight: 1, structure: { $file: missing } }\n"
        + "  target: []\n"
        + "files: { pdb: data/abl.pdb }\n"
        + "---\n"
        + HEADER
        + "action: fold\n",
    )
    loaded = action_tests.load_tests(directory, plugin, types)
    assert not loaded.ok
    assert [(p.code, p.message) for p in loaded.problems] == [
        ("unknown-param", "compute_properties has no param ph."),
        ("unknown-field", "acme/protein.Protein has no field weight."),
        ("unknown-field", "acme/protein.Protein has no field structure."),
        ("unknown-file", "There's no file missing in `files`."),
        ("unknown-input", "compute_properties has no input target."),
        ("missing-file", "There's no file at `data/abl.pdb`."),
        ("unknown-action", "acme/protein has no action fold."),
    ]
    assert loaded.problems[0].location is not None
    assert loaded.problems[0].file == "tests/bad.yaml"


def test_a_batch_packs_as_many_as_the_action_does(
    protein: tuple[Path, Plugin, dict[str, EntityType]],
) -> None:
    directory, plugin, types = protein
    entities = "".join(f"    - values: {{ sequence: MK{'V' * n} }}\n" for n in range(101))
    write(
        directory,
        "many.yaml",
        HEADER + "action: compute_properties\ninputs:\n  protein:\n" + entities,
    )
    (problem,) = action_tests.load_tests(directory, plugin, types).problems
    assert problem.message == (
        "protein takes one entity, not 101: compute_properties packs up to 100 runs into one "
        "job, so give at most 100."
    )


def test_the_run_input_is_the_servers(
    protein: tuple[Path, Plugin, dict[str, EntityType]], tmp_path: Path
) -> None:
    directory, plugin, types = protein
    (directory / "data").mkdir()
    (directory / "data" / "a.fasta").write_text(">a\nMKV\n")
    write(
        directory,
        "import.yaml",
        HEADER
        + "action: import_proteins\n"
        + "params: { file: { $file: fasta } }\n"
        + "files: { fasta: data/a.fasta }\n"
        + "---\n"
        + HEADER
        + "name: two\n"
        + "action: compute_properties\n"
        + "inputs:\n  protein:\n    - values: { sequence: MKV }\n"
        + "    - { hrid: PRT-9, values: { sequence: MKW } }\n"
        + "expect: { output: { updates: {} }, tolerance: 0.01 }\n",
    )
    loaded = action_tests.load_tests(directory, plugin, types)
    assert loaded.ok, loaded.problems
    imported, two = loaded.tests
    assert (imported.name, two.name) == ("import_1", "two")
    assert two.tolerance == 0.01
    work = tmp_path / "work"
    work.mkdir()
    made = action_tests.run_input(imported, plugin, types, directory, {"a": 1}, work)
    (file_id,) = made["files"]
    assert made["params"] == {"file": file_id}
    assert made["files"][file_id] == {"path": "../protein/data/a.fasta"}
    assert made["command"] == ["python", "./scripts/imports.py"]
    assert made["environment"] == "acme/protein.protein-tools"
    assert made["settings"] == {"a": 1}
    assert made["run"]["seed"] == action_tests.SEED
    assert "pack" not in made

    packed = action_tests.run_input(two, plugin, types, directory, {}, work)
    first, second = packed["inputs"]["protein"]
    assert (first["hrid"], second["hrid"]) == ("PRT-000001", "PRT-9")
    assert first["schema_version"] == plugin.types["acme/protein.Protein"].schema_version
    assert [p["entity"] for p in packed["pack"]] == [first["id"], second["id"]]
    assert [p["hrid"] for p in packed["pack"]] == ["RUN-000001", "RUN-000002"]


def test_an_input_gives_what_its_references_name(
    protein: tuple[Path, Plugin, dict[str, EntityType]], tmp_path: Path
) -> None:
    directory, plugin, types = protein
    (directory / "data").mkdir()
    (directory / "data" / "abl.pdb").write_text("ATOM\n")
    structure = (
        "inputs:\n  structure:\n    - values: { structure: { $file: pdb }, method: xray }\n"
        "      included:\n        chains:\n"
        "          - values: { sequence: MKV }\n"
        "          - { hrid: PRT-9, values: { sequence: MKW } }\n"
    )
    write(
        directory,
        "relax.yaml",
        HEADER + "action: relax_structure\nfiles: { pdb: data/abl.pdb }\n" + structure,
    )
    loaded = action_tests.load_tests(directory, plugin, types)
    assert loaded.ok, loaded.problems
    (test,) = loaded.tests
    work = tmp_path / "work"
    work.mkdir()
    made = action_tests.run_input(test, plugin, types, directory, {}, work)
    (given,) = made["inputs"]["structure"]
    first, second = given["included"]["chains"]
    assert given["values"]["chains"] == [first["id"], second["id"]]
    assert (first["hrid"], second["hrid"]) == ("PRT-000002", "PRT-9")
    assert first["type"] == "acme/protein.Protein" and first["values"] == {"sequence": "MKV"}

    wrong = structure.replace("chains:", "source:").replace("sequence: MKV }", "weight: 1 }")
    write(
        directory,
        "relax.yaml",
        HEADER + "action: relax_structure\nfiles: { pdb: data/abl.pdb }\n" + wrong,
    )
    problems = action_tests.load_tests(directory, plugin, types).problems
    assert [(p.code, p.message) for p in problems] == [
        ("not-included", "The input doesn't include source.")
    ]
    assert problems[0].hint == "It includes: chains."


def test_an_input_may_have_the_fields_this_plugins_extension_adds(tmp_path: Path) -> None:
    """A plugin's actions read and write the fields its extension adds to another plugin's
    type, so a fixture's input entities can have values in them, and in nothing else."""
    shutil.copytree(PLUGINS / "structure_prediction", tmp_path / "prediction")
    directory = tmp_path / "prediction"
    (directory / "actions" / "rescore.yaml").write_text(
        "apiVersion: iki/v1\nkind: Action\nname: rescore\n"
        'inputs:\n  structure: { type: acme/protein.ProteinStructure, schema: ">=1" }\n'
        "effects:\n  updates: [{ input: structure, fields: [plddt_mean] }]\n"
        "runs_on: { tier: container, environment: colabfold }\n"
        "command: [python, /app/rescore.py]\ntimeout: 10m\n"
    )
    result = load_plugin(directory, dependencies=[PLUGINS / "protein", PLUGINS / "dna"])
    assert result.plugin is not None, result.problems
    protein = load_plugin(PLUGINS / "protein", dependencies=[PLUGINS / "dna"]).plugin
    assert protein is not None
    types = {t.qualified_name: t for t in core_plugin().types.values()}
    types.update({t.qualified_name: t for t in protein.types.values()})
    write(
        directory,
        "rescore.yaml",
        HEADER
        + "action: rescore\n"
        + "inputs:\n  structure:\n    - values: { method: xray, plddt_mean: 90.5, nope: 1 }\n",
    )

    loaded = action_tests.load_tests(directory, result.plugin, types)

    assert [(p.code, p.message) for p in loaded.problems] == [
        ("unknown-field", "acme/protein.ProteinStructure has no field nope.")
    ]


def test_names_are_unique(protein: tuple[Path, Plugin, dict[str, EntityType]]) -> None:
    directory, plugin, types = protein
    one = HEADER + "name: same\naction: fetch_proteins\nparams: { accession: P1 }\n"
    write(directory, "a.yaml", one)
    write(directory, "b.yaml", one)
    (problem,) = action_tests.load_tests(directory, plugin, types).problems
    assert (problem.code, problem.file) == ("duplicate-test", "tests/b.yaml")
    assert problem.message == "There's already a test called same, in tests/a.yaml."


def test_required_params_and_inputs_must_be_given(
    protein: tuple[Path, Plugin, dict[str, EntityType]],
) -> None:
    directory, plugin, types = protein
    write(
        directory,
        "a.yaml",
        HEADER + "action: fetch_proteins\n---\n" + HEADER + "action: align_pair\n",
    )
    assert [p.message for p in action_tests.load_tests(directory, plugin, types).problems] == [
        "Give fetch_proteins's accession.",
        "Give align_pair's query input.",
        "Give align_pair's target input.",
    ]


def test_outputs_are_compared_by_what_a_test_gives() -> None:
    got = {
        "updates": {"protein": [{"entity": "x", "values": {"weight": 15257.3601, "pi": 8.72}}]},
        "metrics": {"score": 1},
    }
    assert action_tests.compare({"updates": {"protein": [{"values": {"pi": 8.72}}]}}, got) == []
    assert (
        action_tests.compare(
            {"updates": {"protein": [{"values": {"weight": 15257.36}}]}}, got, 1e-6
        )
        == []
    )
    assert action_tests.compare(
        {"updates": {"protein": [{"values": {"weight": 15257.36, "gravy": 0.1}}]}}, got
    ) == [
        "updates.protein[0].values.weight: expected 15257.36, got 15257.3601",
        "updates.protein[0].values.gravy: missing (expected 0.1)",
    ]
    assert action_tests.compare({"updates": {"protein": [{}, {}]}}, got) == [
        "updates.protein: expected 2 item(s), got 1"
    ]
    assert action_tests.compare({"metrics": {"score": True}}, got) == [
        "metrics.score: expected true, got 1"
    ]
    assert action_tests.compare({"metrics": {"score": 0}}, {"metrics": {"score": 1e-12}}) == []


def test_outputs_may_change_only_what_the_action_declares(
    protein: tuple[Path, Plugin, dict[str, EntityType]],
) -> None:
    _, plugin, _ = protein
    action = plugin.actions["compute_properties"]
    assert (
        action_tests.effects(
            action, {"updates": {"protein": [{"values": {"molecular_weight": 1}}]}}
        )
        == []
    )
    assert action_tests.effects(
        action, {"updates": {"protein": [{"values": {"sequence": "M"}}], "other": []}}
    ) == [
        "It sets sequence on protein, which compute_properties doesn't declare it updates.",
        "It updates other, which compute_properties doesn't declare it updates.",
    ]


def test_an_update_cant_refer_to_what_its_run_creates(
    protein: tuple[Path, Plugin, dict[str, EntityType]],
) -> None:
    """A run's updates are written before its creates, so `$create` names a create only in
    another create's references (ADR 0064): in an update's reference, or an item of a list
    of them, it is refused as the server refuses it, saying what to do instead. In a field
    that isn't a reference it is a value like any other."""
    _, plugin, types = protein
    every = outputs.SpecTypes(
        {**types, **{t.qualified_name: t for t in plugin.types.values()}}, plugin.extensions
    )
    action = dataclasses.replace(
        plugin.actions["fetch_proteins"],
        inputs={"request": Input(name="request", type="acme/protein.AssayRequest")},
        updates=(Updates(input="request", fields=("protein", "samples", "title")),),
    )
    made = {"$create": "made"}
    updates = {
        "request": [
            {"entity": "x", "values": {"protein": made, "samples": ["y", made], "title": made}}
        ],
        "undeclared": [{"entity": "x", "values": {"protein": made}}],
    }
    assert outputs.created_refs_in_updates(action, updates, every) == [
        outputs.Problem("#/updates/request/0/values/protein", outputs.UPDATE_REFERS_TO_A_CREATE),
        outputs.Problem("#/updates/request/0/values/samples/1", outputs.UPDATE_REFERS_TO_A_CREATE),
    ]
    assert "derived_from" in outputs.UPDATE_REFERS_TO_A_CREATE


def test_creates_are_checked_as_the_server_checks_them(
    protein: tuple[Path, Plugin, dict[str, EntityType]],
) -> None:
    """A subtype of a type the action creates is one it may create; `fills` names values
    it gives of its registry entry's; keys are each used once, and referred to after."""
    _, plugin, types = protein
    every = outputs.SpecTypes(
        {**types, **{t.qualified_name: t for t in plugin.types.values()}}, plugin.extensions
    )
    action = dataclasses.replace(
        plugin.actions["fetch_proteins"],
        creates=(Creates(type="acme/dna.Sequence", cardinality="many"),),
    )
    made = [
        {
            "type": "acme/protein.Protein",
            "values": {"sequence": "MKV", "hazard": "bsl1"},
            "fills": ["hazard"],
            "key": "p",
        },
        {"type": "acme/dna.DNASequence", "values": {"bases": "ACGT"}, "fills": ["topology"]},
        {"type": "acme/protein.Lot", "values": {}, "key": "p"},
    ]
    assert [(p.pointer, p.detail) for p in outputs.check_creates(action, made, every)] == [
        ("#/creates/1/fills/0", "It gives no value for `topology`."),
        ("#/creates/2/key", "Another create has the key 'p'."),
        (
            "#/creates/2/type",
            "The action doesn't declare that it creates acme/protein.Lot (it creates: "
            "acme/dna.Sequence).",
        ),
    ]
    made[1]["values"] = {"bases": "ACGT", "topology": "linear"}
    assert [p.detail for p in outputs.check_creates(action, made[:2], every)] == [
        "`topology` isn't its registry entry's, so there's nothing to fill."
    ]
