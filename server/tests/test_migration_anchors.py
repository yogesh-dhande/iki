"""Plugin migrations are written in Iki's operations and depend on release anchors (ADR 0296).

Iki keeps both: the anchor of the release in development comes after every one of Iki's
migrations, released anchors stay, and every first-party migration reads back to itself
through the writer, so the wrappers stand for exactly what Django makes of them.
"""

import json
import types
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps
from django.db import migrations
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.migration import Migration

from iki_server import __version__
from iki_server.plugins import migration_writer
from iki_server.plugins import operations as ops
from iki_spec import migration_api, releases
from iki_spec.migration_api import Anchor

ROOT = Path(__file__).resolve().parents[2]
TABLE = "spec/src/iki_spec/migration_api.json"


def _iki_apps() -> set[str]:
    """Iki's own apps with migrations, the core plugin's included, but not the anchors'."""
    return {
        c.label
        for c in apps.get_app_configs()
        if (c.name.startswith("iki_server.") or c.name == "iki_plugin_iki__core")
        and c.label != migration_api.ANCHORS_APP
    }


@pytest.fixture(scope="module")
def loader() -> MigrationLoader:
    return MigrationLoader(None, ignore_no_migrations=True)


def test_the_anchor_of_the_release_in_development_follows_all_of_ikis_migrations(
    loader: MigrationLoader,
) -> None:
    major, minor, patch = releases.release(__version__)
    newest = migration_api.anchors()[-1]
    assert newest.release == (major, minor), (
        f"Iki is {__version__}: add {minor + 1:04}_iki_{major}_{minor}'s row to {TABLE} and its "
        "migration to iki_server/anchors/migrations."
    )
    if ".dev" not in __version__ or patch:
        return  # Released: the anchor is fixed, and a patch's migrations come after it.
    iki = _iki_apps()
    leaves = {app: name for app, name in loader.graph.leaf_nodes() if app in iki}
    assert newest.after == leaves, (
        f"One of Iki's apps has a new migration. In {TABLE}, set {newest.version}'s anchor's "
        f'"after" to {json.dumps(dict(sorted(leaves.items())))}, and copy it into the anchors '
        "table in spec/README.md."
    )


def test_every_anchor_is_a_migration_after_the_one_before(loader: MigrationLoader) -> None:
    on_disk = sorted(n for a, n in loader.disk_migrations if a == migration_api.ANCHORS_APP)
    assert on_disk == [a.migration for a in migration_api.anchors()]
    before: Anchor | None = None
    for anchor in migration_api.anchors():
        parents = {p.key for p in loader.graph.node_map[anchor.key].parents}
        assert parents == set(anchor.after.items()) | ({before.key} if before else set())
        assert not loader.disk_migrations[anchor.key].operations
        before = anchor


def test_the_operations_module_is_the_published_list() -> None:
    assert sorted(ops.__all__) == sorted(migration_api.names())
    written = {how.name for how in ops.WRITTEN_AS.values()}
    wrapped = {cls.__name__ for cls in ops.OF_DJANGO.values()}
    assert written | wrapped | set(ops.CONSTANTS) | {"Migration"} <= set(ops.__all__)
    for name in sorted(set(ops.__all__) - set(ops.CONSTANTS)):
        module = getattr(ops, name).__module__
        assert module in (ops.__name__, "iki_server.entities.checks"), name


def _committed_plugin_migrations() -> list[Path]:
    """Every first-party plugin's, the template's, the examples' and the dev fixtures'."""
    places = ("plugins/*", "plugin-template", "examples/plugins/*", "deploy/dev/fixtures/plugins/*")
    return sorted(f for place in places for f in ROOT.glob(f"{place}/migrations/0*.py"))


def _read(text: str, name: str) -> Migration:
    module = types.ModuleType(name)
    exec(compile(text, name, "exec"), module.__dict__)  # noqa: S102 - our own committed migrations
    migration: Migration = module.Migration(name, "acme__lab")
    return migration


def test_every_first_party_plugin_migration_reads_back_to_itself() -> None:
    files = _committed_plugin_migrations()
    assert len(files) >= 40
    for path in files:
        text = path.read_text()
        migration = _read(text, path.stem)
        written = migration_writer.render(migration, migration.dependencies)
        assert written == text, f"{path.relative_to(ROOT)} isn't what the writer writes."


def _made(name: str, **attributes: Any) -> Migration:
    """A migration of acme/lab's, as Django's autodetector makes one."""
    made: Migration = type("Migration", (Migration,), attributes)(name, "acme__lab")
    return made


def test_a_construct_without_a_wrapper_is_refused() -> None:
    raw = _made("0002_raw", operations=[migrations.RunPython(migrations.RunPython.noop)])
    with pytest.raises(migration_writer.WriteError, match=r"RunPython operation, which Iki"):
        migration_writer.render(raw, [])
    project = ops.ForeignKey(to="iki_org.project", on_delete=ops.PROTECT, null=True)
    elsewhere = _made(
        "0002_project", operations=[migrations.AddField("sample", "project", project)]
    )
    with pytest.raises(migration_writer.WriteError, match=r"Iki's model iki_org\.project"):
        migration_writer.render(elsewhere, [])


def test_django_operations_are_written_as_ikis_and_its_migrations_as_the_anchor() -> None:
    made = _made(
        "0001_initial",
        initial=True,
        dependencies=[
            ("iki_entities", "0028_record_exports"),
            ("__setting__", "AUTH_USER_MODEL"),
            ("iki__core", "0007_complex_bond_sources"),
            ("iki__dna", "0001_initial"),
        ],
        operations=[
            migrations.CreateModel(
                "Sample",
                [
                    ("entity", ops.OneToOneField(to=ops.ENTITY, on_delete=ops.PROTECT)),
                    ("owner", ops.ForeignKey(to="iki_accounts.User", on_delete=ops.PROTECT)),
                ],
                options={"db_table": "acme__lab_sample"},
                bases=("iki_entities.entity",),
            )
        ],
    )
    anchor = migration_api.anchors()[-1]
    text = migration_writer.render(made, migration_writer.dependencies(made, anchor))
    assert "import" not in text.replace("from iki_server.plugins import operations as iki", "")
    assert f"('iki__dna', '0001_initial'),\n        {anchor.key!r}," in text
    assert "iki_entities" not in text.replace("iki.ENTITY", "")
    assert "to=iki.USER" in text and "bases=(iki.ENTITY,)" in text
    assert "table='acme__lab_sample'" in text


def test_a_migration_needing_a_later_releases_model_is_refused_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Early(Anchor):
        """An older release's anchor, as early as Iki's first migration of records."""

        @property
        def key(self) -> tuple[str, str]:
            return ("iki_entities", "0001_initial")

    newest = migration_api.anchors()[-1]
    early = Early((0, 0), "0000_iki_0_0", {})
    monkeypatch.setattr(migration_api, "anchors", lambda: (early, newest))
    report = ops.ForeignKey(to=ops.FILE, on_delete=ops.PROTECT)
    made = _made("0001_initial", operations=[migrations.AddField("sample", "report", report)])
    with pytest.raises(migration_writer.WriteError) as refused:
        migration_writer.check_at(made, early)
    assert str(refused.value) == (
        f"acme/lab 0001_initial refers to iki_files.file, which Iki {newest.version} adds, and "
        f'acme/lab\'s `iki:` admits 0.0. Raise it to `">={newest.version}"`.'
    )
    migration_writer.check_at(made, newest)
