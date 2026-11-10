import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from iki_codegen import REGISTRY, build, conflicts, in_dependency_order, render_models
from iki_spec import naming
from iki_spec.model import BUILTIN_TYPES, Plugin, plugin_from_dict
from iki_spec.validate import load_plugin

PLUGINS = Path(__file__).parents[2] / "spec" / "tests" / "plugins"
GOLDEN = Path(__file__).parent / "golden"


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
    golden = GOLDEN / f"{plugin.name}.models.py.txt"
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
    packages = build(list(reversed(plugins)), tmp_path / "out", migrations={"dna": migrations})
    assert [p.name for p in packages] == [
        "iki_plugin_dna",
        "iki_plugin_protein",
        "iki_plugin_structure_prediction",
    ]
    dna = tmp_path / "out" / "iki_plugin_dna"
    assert sorted(p.name for p in (dna / "migrations").iterdir()) == [
        "0001_initial.py",
        "__init__.py",
    ]
    assert "label = 'dna'" in (dna / "apps.py").read_text()
    manifest = json.loads((dna / "manifest.json").read_text())
    assert manifest["apiVersion"] == "iki/v1"
    assert plugin_from_dict(manifest["plugin"]) == plugins[0]
    registry = (tmp_path / "out" / REGISTRY / "__init__.py").read_text()
    assert registry.endswith(
        "APPS = [\n"
        "    'iki_plugin_dna.apps.PluginConfig',\n"
        "    'iki_plugin_protein.apps.PluginConfig',\n"
        "    'iki_plugin_structure_prediction.apps.PluginConfig',\n"
        "]\n"
    )


def test_build_replaces_earlier_builds(plugins: list[Plugin], tmp_path: Path) -> None:
    build(plugins[:1], tmp_path)
    stale = tmp_path / "iki_plugin_dna" / "migrations" / "0009_gone.py"
    stale.write_text("")
    build(plugins[:1], tmp_path)
    assert not stale.exists()


def test_dependencies_come_first(plugins: list[Plugin]) -> None:
    dna, protein, prediction = plugins
    assert in_dependency_order([prediction, protein, dna]) == [dna, protein, prediction]
    with pytest.raises(ValueError, match="structure_prediction needs the protein plugin"):
        in_dependency_order([prediction, dna])
    a = Plugin(name="a", version="1.0.0", dependencies={"b": {}})
    b = Plugin(name="b", version="1.0.0", dependencies={"a": {}})
    with pytest.raises(ValueError, match="plugins depend on each other: a → b → a"):
        in_dependency_order([a, b])


def test_long_constraint_names_stay_unique_and_fit() -> None:
    table = "a" * 40
    one = naming.constraint_name(table, "b" * 30, "min")
    two = naming.constraint_name(table, "b" * 30, "max")
    assert len(one) == len(two) == naming.MAX_IDENTIFIER
    assert one != two
    assert one.startswith(table)
    assert naming.constraint_name("dna_dnasequence", "bases", "format") == (
        "dna_dnasequence__bases__format"
    )


def test_plugins_that_cant_be_installed_together(plugins: list[Plugin], tmp_path: Path) -> None:
    _, protein, prediction = plugins
    assert conflicts(plugins) == []
    # A second plugin with the same prefix and the same field on ProteinStructure.
    copy = replace(
        prediction,
        name="folding",
        types={
            "folding.Model": replace(
                protein.types["protein.Protein"], plugin="folding", name="Model"
            )
        },
        extensions=tuple(replace(e, plugin="folding") for e in prediction.extensions),
    )
    problems = conflicts([*plugins, copy])
    assert problems == [
        "folding.Model and protein.Protein both use the HRID prefix PRT",
        "folding and structure_prediction both add the field `plddt_mean` to "
        "protein.ProteinStructure",
        "folding and structure_prediction both add the field `pae` to protein.ProteinStructure",
        "folding and structure_prediction both add the field `ranking` to protein.ProteinStructure",
    ]
    with pytest.raises(ValueError, match="both use the HRID prefix PRT"):
        build([*plugins, copy], tmp_path)
