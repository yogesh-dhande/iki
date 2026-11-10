from importlib.metadata import version

from iki_spec.builtin import CORE_DIRECTORY, core_plugin
from iki_spec.model import BUILTIN_TYPES
from iki_spec.validate import load_plugin


def test_the_core_plugin_is_valid_and_strict() -> None:
    result = load_plugin(CORE_DIRECTORY, core=True)
    assert result.problems == []
    assert core_plugin().name == "core"


def test_its_types_are_the_builtin_ones() -> None:
    assert list(BUILTIN_TYPES) == ["core.Entity"]
    entity = BUILTIN_TYPES["core.Entity"]
    assert entity.abstract
    assert entity.fields == {}
    assert "core.Entity" in BUILTIN_TYPES
    assert len(BUILTIN_TYPES) == 1


def test_its_version_is_ikis() -> None:
    # The core plugin is released with Iki: 0.1.0.dev0 → 0.1.0.
    release = version("iki-spec").split(".dev")[0].split("rc")[0]
    assert core_plugin().version == release


def test_the_package_ships_its_manifests() -> None:
    assert (CORE_DIRECTORY / "plugin.yaml").is_file()
    assert (CORE_DIRECTORY / "types" / "entity.yaml").is_file()
