from importlib.metadata import version

from iki_spec.builtin import CORE_DIRECTORY, core_plugin
from iki_spec.model import BUILTIN_TYPES
from iki_spec.validate import load_plugin


def test_the_core_plugin_is_valid_and_strict() -> None:
    result = load_plugin(CORE_DIRECTORY, core=True)
    assert result.problems == []
    assert core_plugin().name == "iki/core"


def test_its_types_are_the_builtin_ones() -> None:
    assert sorted(BUILTIN_TYPES) == [
        "iki/core.Alignment",
        "iki/core.App",
        "iki/core.Collection",
        "iki/core.Complex",
        "iki/core.Entity",
        "iki/core.Organism",
        "iki/core.Sequence",
        "iki/core.View",
    ]
    entity = BUILTIN_TYPES["iki/core.Entity"]
    assert entity.abstract
    assert entity.fields == {}
    collection = BUILTIN_TYPES["iki/core.Collection"]
    assert not collection.abstract
    assert set(collection.fields) == {"name", "description", "members", "search"}
    assert collection.hrid_prefix == "COL"
    sequence = BUILTIN_TYPES["iki/core.Sequence"]
    assert sequence.abstract
    assert set(sequence.fields) == {"name", "description", "organism"}
    assert sequence.fields["organism"].to == ("iki/core.Organism",)
    # An organism is a taxon, by its NCBI Taxonomy ID (ADR 0140).
    organism = BUILTIN_TYPES["iki/core.Organism"]
    assert (organism.identity, organism.hrid_prefix) == (("taxon_id",), "TAX")
    assert {n: f.registry for n, f in organism.fields.items()} == {
        "taxon_id": None,
        "scientific_name": "fact",
        "rank": "fact",
        "parent": "fact",
    }
    app = BUILTIN_TYPES["iki/core.App"]
    assert not app.abstract
    assert set(app.fields) == {"name", "description", "spec"}
    assert app.hrid_prefix == "APP"
    view = BUILTIN_TYPES["iki/core.View"]
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
    alignment = BUILTIN_TYPES["iki/core.Alignment"]
    assert set(alignment.fields) == {
        "name",
        "description",
        "reference",
        "members",
        "aligned",
        "verdict",
        "method",
    }
    verdict = alignment.fields["verdict"]
    assert verdict.values == ("matches", "differs", "incomplete")
    assert verdict.immutable and not verdict.required
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


def test_a_complexs_own_bonds_and_its_parts_disulfides_read_apart() -> None:
    # An antibody's 16 disulfides are its own bonds, and its chains have none of their
    # own, so its parts' count is 0 a few lines below its 16 bonds: its label says it
    # counts something else, since an entity's page shows labels, not descriptions.
    fields = BUILTIN_TYPES["iki/core.Complex"].fields
    assert fields["part_disulfides"].label == "Its parts' own disulfides, besides its bonds"
    # And its ε280 is a molar extinction coefficient, not an absorbance.
    assert "molar extinction coefficient" in (
        fields["extinction_coefficient_reduced"].description or ""
    )
