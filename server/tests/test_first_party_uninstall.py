"""Every shipped plugin's applied migrations can be fully reversed on PostgreSQL."""

from pathlib import Path

from first_party_dependencies import PRODUCT_PLUGINS, ROOT
from test_plugin_migrations import database, django, iki, migrate, query, sync

from iki_spec import naming

__all__ = ["database"]


def test_all_first_party_migrations_reverse_after_sync(tmp_path: Path, database: str) -> None:
    out = tmp_path / "build"
    # Action-only plugins can be installed without adding any database tables.
    built = iki("plugin", "build", *PRODUCT_PLUGINS.values(), "--output", out, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(out, database)
    sync(out, database)
    # This rule names the FK that the initial cell-lines migration removes
    # before it drops the culture table. Reverse the migration with that rule live.
    assert query(
        database,
        "SELECT tgname FROM pg_trigger WHERE tgrelid = %s::regclass "
        "AND tgname = %s AND NOT tgisinternal",
        "iki__cell_lines_cellculture",
        "iki_rule__thaw_once",
    ) == [("iki_rule__thaw_once",)]

    labels = sorted(
        naming.app_label(name)
        for name, path in PRODUCT_PLUGINS.items()
        if (path / "migrations").is_dir()
    )
    migration_apps = "SELECT DISTINCT app FROM django_migrations WHERE app = ANY(%s) ORDER BY app"
    assert [app for (app,) in query(database, migration_apps, labels)] == labels

    # The migrate command sends pre_migrate to remove analysts' views. Django
    # reverses dependent migrations as each app returns to zero.
    rollback = django(
        out,
        database,
        "-c",
        """
import sys
import django
django.setup()
from django.core.management import call_command

for app in sys.argv[1:]:
    call_command("migrate", app, "zero", interactive=False)
""",
        *labels,
    )
    assert rollback.returncode == 0, rollback.stdout + rollback.stderr
    assert query(database, migration_apps, labels) == []
    remaining = query(database, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    prefixes = tuple(f"{label}_" for label in labels)
    assert [table for (table,) in remaining if table.startswith(prefixes)] == []
    assert ("iki_entity",) in remaining
