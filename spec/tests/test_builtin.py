from importlib.metadata import version

from iki_spec.builtin import CORE_DIRECTORY, core_plugin
from iki_spec.model import BUILTIN_TYPES
from iki_spec.validate import load_plugin


def test_the_core_plugin_is_valid_and_strict() -> None:
    result = load_plugin(CORE_DIRECTORY, core=True)
    assert result.problems == []
    assert core_plugin().name == "core"


def test_its_types_are_the_builtin_ones() -> None:
    assert sorted(BUILTIN_TYPES) == [
        "core.Alignment",
        "core.App",
        "core.Collection",
        "core.Entity",
        "core.Sequence",
        "core.View",
    ]
    entity = BUILTIN_TYPES["core.Entity"]
    assert entity.abstract
    assert entity.fields == {}
    collection = BUILTIN_TYPES["core.Collection"]
    assert not collection.abstract
    assert set(collection.fields) == {"name", "description", "members", "search"}
    assert collection.hrid_prefix == "COL"
    sequence = BUILTIN_TYPES["core.Sequence"]
    assert sequence.abstract
    assert set(sequence.fields) == {"name", "description", "organism"}
    app = BUILTIN_TYPES["core.App"]
    assert not app.abstract
    assert set(app.fields) == {"name", "description", "spec"}
    assert app.hrid_prefix == "APP"
    view = BUILTIN_TYPES["core.View"]
    assert not view.abstract
    # A view of one entity named it in `target`; those fields are removed, kept in history,
    # and a view lists the entities it shows (ADR 0110).
    removed = {"target", "target_revision", "viewer"}
    shown = {"name", "description", "targets", "target_revisions", "viewers", "state"}
    assert set(view.fields) == shown | removed
    assert all(view.fields[name].removed for name in removed)
    assert view.fields["targets"].immutable and view.fields["viewers"].immutable
    assert not view.fields["target_revisions"].immutable
    assert view.hrid_prefix == "VIEW"
    alignment = BUILTIN_TYPES["core.Alignment"]
    assert set(alignment.fields) == {
        "name",
        "description",
        "reference",
        "members",
        "aligned",
        "method",
    }
    assert not alignment.fields["members"].required
    assert alignment.fields["reference"].immutable
    assert alignment.hrid_prefix == "ALN"
    assert alignment.viewers[0].viewer == "alignment"


def test_its_version_is_ikis() -> None:
    # The core plugin is released with Iki: 0.1.0.dev0 → 0.1.0.
    release = version("iki-spec").split(".dev")[0].split("rc")[0]
    assert core_plugin().version == release


def test_the_package_ships_its_manifests() -> None:
    assert (CORE_DIRECTORY / "plugin.yaml").is_file()
    assert (CORE_DIRECTORY / "types" / "entity.yaml").is_file()
    assert (CORE_DIRECTORY / "types" / "collection.yaml").is_file()
