import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from iki_codegen import REGISTRY, build, conflicts, in_dependency_order, render_models
from iki_codegen.build import with_hosts
from iki_spec import naming
from iki_spec.model import BUILTIN_TYPES, Plugin, plugin_from_dict
from iki_spec.validate import load_plugin

PLUGINS = Path(__file__).parents[2] / "spec" / "tests" / "plugins"
GOLDEN = Path(__file__).parent / "golden"
SOURCES = {
    "acme/dna": PLUGINS / "dna",
    "acme/protein": PLUGINS / "protein",
    "acme/prediction": PLUGINS / "structure_prediction",
}


def load(name: str, *dependencies: str) -> Plugin:
    result = load_plugin(PLUGINS / name, dependencies=[PLUGINS / d for d in dependencies])
    assert result.plugin is not None, result.problems
    return result.plugin


@pytest.fixture(scope="module")
def plugins() -> list[Plugin]:
    return [
        load("dna"),
        load("protein", "dna"),
        load("structure_prediction", "protein", "dna"),
    ]


def types(plugins: list[Plugin]) -> dict[str, object]:
    out: dict[str, object] = dict(BUILTIN_TYPES)
    for plugin in plugins:
        out.update(plugin.types)
    return out


@pytest.mark.parametrize("index", [0, 1, 2])
def test_models_match_the_golden_files(plugins: list[Plugin], index: int) -> None:
    """Set IKI_UPDATE_GOLDEN=1 to rewrite them after a deliberate change, then review the diff."""
    plugin = plugins[index]
    rendered = render_models(plugin, types(plugins))  # type: ignore[arg-type]
    golden = GOLDEN / f"{naming.app_label(plugin.name)}.models.py.txt"
    if os.environ.get("IKI_UPDATE_GOLDEN"):
        golden.write_text(rendered)
    assert rendered == golden.read_text()
    compile(rendered, f"{plugin.name}/models.py", "exec")


def test_rendering_is_deterministic(plugins: list[Plugin]) -> None:
    first = [render_models(p, types(plugins)) for p in plugins]  # type: ignore[arg-type]
    again = [render_models(p, types(list(reversed(plugins)))) for p in plugins]  # type: ignore[arg-type]
    assert first == again


def test_build_writes_a_package_per_plugin(plugins: list[Plugin], tmp_path: Path) -> None:
    migrations = tmp_path / "dna-migrations"
    migrations.mkdir()
    (migrations / "0001_initial.py").write_text("# a migration\n")
    (migrations / "notes.txt").write_text("not copied\n")
    packages = build(
        list(reversed(plugins)),
        tmp_path / "out",
        migrations={"acme/dna": migrations},
        sources=SOURCES,
    )
    assert [p.name for p in packages] == [
        "iki_plugin_acme__dna",
        "iki_plugin_acme__protein",
        "iki_plugin_acme__prediction",
    ]
    dna = tmp_path / "out" / "iki_plugin_acme__dna"
    assert sorted(p.name for p in (dna / "migrations").iterdir()) == [
        "0001_initial.py",
        "__init__.py",
    ]
    assert "label = 'acme__dna'" in (dna / "apps.py").read_text()
    manifest = json.loads((dna / "manifest.json").read_text())
    assert manifest["apiVersion"] == "iki/v1"
    assert plugin_from_dict(manifest["plugin"]) == plugins[0]
    # Each component's module, as it was checked.
    residues = tmp_path / "out" / "iki_plugin_acme__protein" / "components" / "residues.js"
    assert (
        residues.read_bytes() == (PLUGINS / "protein" / "components" / "residues.js").read_bytes()
    )
    assert not (dna / "components").exists()
    registry = (tmp_path / "out" / REGISTRY / "__init__.py").read_text()
    assert registry.endswith(
        "APPS = [\n"
        "    'iki_plugin_acme__dna.apps.PluginConfig',\n"
        "    'iki_plugin_acme__protein.apps.PluginConfig',\n"
        "    'iki_plugin_acme__prediction.apps.PluginConfig',\n"
        "]\n"
    )


def test_hosts_from_settings_are_filled_in(plugins: list[Plugin], tmp_path: Path) -> None:
    prediction = plugins[2]
    action = prediction.actions["predict_structure"]
    network = ("{settings.lims_host}", "example.org", "{settings.unset}")
    reaching = replace(
        prediction, actions={**prediction.actions, action.name: replace(action, network=network)}
    )
    values = {"lims_host": "lims.acme.example", "unset": None}
    assert with_hosts(reaching, values).actions[action.name].network == (
        "lims.acme.example",
        "example.org",
    )
    # A deployment's build writes the hosts it gives into the manifest the server reads.
    build(
        [*plugins[:2], reaching],
        tmp_path,
        sources=SOURCES,
        settings={"acme/prediction": {"jobs_url": "https://x.example", **values}},
    )
    package = tmp_path / "iki_plugin_acme__prediction"
    manifest = json.loads((package / "manifest.json").read_text())
    by_name = {a["name"]: a for a in manifest["plugin"]["actions"].values()}
    assert by_name[action.name]["network"] == ["lims.acme.example", "example.org"]


def test_a_component_changed_since_it_was_checked_isnt_built(
    plugins: list[Plugin], tmp_path: Path
) -> None:
    protein = plugins[1]
    residues = protein.components["residues"]
    changed = replace(protein, components={"residues": replace(residues, sha256="0" * 64)})
    with pytest.raises(ValueError, match="changed after the plugin was checked"):
        build([plugins[0], changed], tmp_path, sources=SOURCES)
    with pytest.raises(ValueError, match="components need its directory"):
        build(plugins[:2], tmp_path)


def test_build_replaces_earlier_builds(plugins: list[Plugin], tmp_path: Path) -> None:
    build(plugins[:1], tmp_path)
    stale = tmp_path / "iki_plugin_acme__dna" / "migrations" / "0009_gone.py"
    stale.write_text("")
    build(plugins[:1], tmp_path)
    assert not stale.exists()


def test_dependencies_come_first(plugins: list[Plugin]) -> None:
    dna, protein, prediction = plugins
    assert in_dependency_order([prediction, protein, dna]) == [dna, protein, prediction]
    with pytest.raises(ValueError, match="acme/prediction needs the acme/protein plugin"):
        in_dependency_order([prediction, dna])
    a = Plugin(name="acme/a", version="1.0.0", dependencies={"acme/b": {}})
    b = Plugin(name="acme/b", version="1.0.0", dependencies={"acme/a": {}})
    with pytest.raises(ValueError, match="plugins depend on each other: acme/a → acme/b → acme/a"):
        in_dependency_order([a, b])


def test_long_constraint_names_stay_unique_and_fit() -> None:
    table = "a" * 40
    one = naming.constraint_name(table, "b" * 30, "min")
    two = naming.constraint_name(table, "b" * 30, "max")
    assert len(one) == len(two) == naming.MAX_IDENTIFIER
    assert one != two
    assert one.startswith(table)
    assert naming.constraint_name("acme__dna_dnasequence", "bases", "format") == (
        "acme__dna_dnasequence__bases__format"
    )


def test_plugins_that_cant_be_installed_together(plugins: list[Plugin], tmp_path: Path) -> None:
    _, protein, prediction = plugins
    assert conflicts(plugins) == []
    # A second plugin with the same prefix and the same field on ProteinStructure.
    copy = replace(
        prediction,
        name="acme/folding",
        types={
            "acme/folding.Model": replace(
                protein.types["acme/protein.Protein"], plugin="acme/folding", name="Model"
            )
        },
        extensions=tuple(
            replace(e, plugin="acme/folding")
            for e in prediction.extensions
            if e.extends_type == "acme/protein.ProteinStructure"
        ),
    )
    problems = conflicts([*plugins, copy])
    assert problems == [
        "acme/folding and acme/prediction both add the field `plddt_mean` to "
        "acme/protein.ProteinStructure",
        "acme/folding and acme/prediction both add the field `job_id` to "
        "acme/protein.ProteinStructure",
        "acme/folding and acme/prediction both add the field `pae` to "
        "acme/protein.ProteinStructure",
        "acme/folding and acme/prediction both add the field `ranking` to "
        "acme/protein.ProteinStructure",
        "acme/folding.Model and acme/protein.Protein both use the HRID prefix PRT",
    ]
    with pytest.raises(ValueError, match="both use the HRID prefix PRT"):
        build([*plugins, copy], tmp_path)


def test_the_same_name_in_two_namespaces_builds_side_by_side(
    plugins: list[Plugin], tmp_path: Path
) -> None:
    dna = plugins[0]
    other = replace(
        dna,
        name="globex/dna",
        types={
            f"globex/dna.{t.name}": replace(
                t,
                plugin="globex/dna",
                hrid_prefix=f"G{t.hrid_prefix}"[:8] if t.hrid_prefix else None,
                extends=t.extends.replace("acme/dna.", "globex/dna.") if t.extends else None,
            )
            for t in dna.types.values()
        },
    )
    assert conflicts([dna, other]) == []
    packages = build([dna, other], tmp_path)
    assert [p.name for p in packages] == ["iki_plugin_acme__dna", "iki_plugin_globex__dna"]
    assert "label = 'globex__dna'" in (packages[1] / "apps.py").read_text()
    assert "db_table = 'globex__dna_dnasequence'" in (packages[1] / "models.py").read_text()


def test_plugins_whose_django_names_would_coincide(plugins: list[Plugin]) -> None:
    # Django's fields can't hold `__`, so they name a plugin `namespace_name`.
    one = Plugin(name="acme_bio/assays", version="1.0.0")
    two = Plugin(name="acme/bio_assays", version="1.0.0")
    assert conflicts([one, two]) == [
        "acme/bio_assays and acme_bio/assays can't be installed together: Django's names for "
        "them would both be acme_bio_assays"
    ]


def test_the_worker_protocol_types_are_current() -> None:
    from iki_codegen import protocol

    crate = Path(__file__).parents[2] / "worker" / "crates" / "iki-protocol"
    document = json.loads((crate / "worker-openapi.json").read_text(encoding="utf-8"))
    committed = (crate / "src" / "generated.rs").read_text(encoding="utf-8")
    assert protocol.generate(document) == committed, (
        "The protocol types are out of date. Run `make protocol` and commit worker/."
    )


def test_the_python_sdk_types_are_current() -> None:
    from iki_codegen import sdk

    root = Path(__file__).parents[2]
    document = json.loads((root / "web/packages/sdk/openapi.json").read_text(encoding="utf-8"))
    committed = (root / "sdk/python/src/iki/schema.py").read_text(encoding="utf-8")
    assert sdk.generate(document) == committed, (
        "The Python SDK's types are out of date. Run `make client` and commit sdk/python/."
    )


def test_a_plugins_writers_are_staged_for_the_image_to_build(tmp_path: Path) -> None:
    """Each writer's crate, its tracked digest and the SDK, for the plugin image's builder
    (ADR 0230); and the lock names each writer's digest beside its plugin's commit."""
    example = Path(__file__).resolve().parents[2] / "examples" / "plugins" / "liquid_handling"
    loaded = load_plugin(example)
    assert loaded.plugin is not None, loaded.problems
    name = loaded.plugin.name
    lock = {"plugins": [{"name": name, "sha": "a" * 40}]}
    build([loaded.plugin], tmp_path, sources={name: example}, lock=lock)
    writers = tmp_path / "iki_installed_plugins" / "writers"
    crate = "examples__liquid_handling/tecan-worklist"
    assert (writers / "writers.txt").read_text() == f"{crate}\n"
    described = json.loads((writers / "writers.json").read_text())
    entry = described["writers"]["examples/liquid_handling.tecan-worklist"]
    tracked = (example / "writers" / "tecan-worklist.wasm.sha256").read_text().split()[0]
    assert (entry["crate"], entry["sha256"]) == (crate, tracked)
    assert (writers / crate / "Cargo.lock").is_file()
    assert not (writers / crate / "target").exists()
    assert (writers / "sdk" / "iki-writer" / "build-writer.sh").is_file()
    locked = json.loads((tmp_path / "iki_installed_plugins" / "lock.json").read_text())
    assert locked["plugins"][0]["writers"] == {"examples/liquid_handling.tecan-worklist": tracked}
