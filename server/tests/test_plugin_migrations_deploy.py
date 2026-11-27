"""Deploying and rolling back plugin migrations, including the guards during a deploy."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from test_plugin_migrations import (
    LAB,
    LAB_ROLES,
    SAMPLE,
    TESTS,
    applied,
    build_to,
    columns,
    database,
    deploy,
    deployed,
    django,
    lab,
    made,
    migrate,
    orm,
    query,
    refused,
    sync,
    write_sample,
)

__all__ = ["database", "lab"]  # fixtures shared with test_plugin_migrations


RECORDS = """
from iki_server.accounts.models import User
from iki_server.entities import services, states
from iki_server.org.models import LabRoleGrant, ProjectMembership, ProjectRole
user = User.objects.create_user("a@example.com", "A")
member = ProjectMembership.objects.create(project=project, user=user, role=ProjectRole.ADMIN)
def record(kind, **values):
    return services.create(user, "acme/lab." + kind, project, values).entity
"""

SAMPLE_ROW = (
    RECORDS
    + """
record("Sample", name="S1", NOTES)
result = None
"""
)


@pytest.fixture
def two_builds(lab: Path) -> tuple[Path, Path]:
    """The lab plugin before and after adding a `notes` field."""
    made("lab", "--no-input", cwd=lab.parent)
    v1 = build_to(lab.parent, "v1", "lab")
    write_sample(lab, ("  count: { type: int }", "  count: { type: int }\n  notes: { type: text }"))
    made("lab", "--name", "notes", "--no-input", cwd=lab.parent)
    return v1, build_to(lab.parent, "v2", "lab")


def test_deploying_an_older_build_reverses_what_it_lacks(
    two_builds: tuple[Path, Path], database: str
) -> None:
    v1, v2 = two_builds
    assert "Kept a copy of acme/lab 0001_initial" in deployed(v1, database)
    out = deployed(v2, database)
    assert "Kept a copy of acme/lab 0002_notes" in out
    assert "acme/lab 0001_initial" not in out  # kept once
    orm(v2, database, SAMPLE_ROW.replace("NOTES", "notes=None"))
    sync(v2, database)  # its views read notes, which the rollback drops

    out = deployed(v1, database)
    assert "Reversed acme/lab 0002_notes (rolled back)" in out
    assert applied(database, "acme__lab") == ["0001_initial"]
    assert "notes" not in columns(database, "acme__lab_sample")
    assert "notes" not in columns(database, "acme__lab_sample__history")
    assert query(database, "SELECT name FROM acme__lab_sample") == [("S1",)]
    # Deploying the same build again changes nothing; the newer one applies again.
    assert "Reversed" not in deployed(v1, database)
    deployed(v2, database)
    assert "notes" in columns(database, "acme__lab_sample")


def test_rollback_reads_a_replacement_state_without_reversing_its_retained_tables(
    lab: Path, database: str
) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    v1 = build_to(lab.parent, "v1", "lab")
    migrations = lab / "migrations"
    original = (migrations / "0001_initial.py").read_text()
    replacement = original.replace(
        "    initial = True",
        '    initial = True\n    replaces = [("acme__lab", "0001_initial")]',
    )
    replacement, renamed = re.subn(
        r"\('old', (models\.\w+Field)\(",
        r"('legacy_old', \1(db_column='old', ",
        replacement,
    )
    assert renamed == 2
    (migrations / "0001_replacement.py").write_text(replacement)
    (migrations / "0002_remove_legacy.py").write_text(
        "from django.db import migrations\n\n"
        "class Migration(migrations.Migration):\n"
        '    dependencies = [("acme__lab", "0001_initial")]\n'
        "    operations = [\n"
        '        migrations.RemoveField("sample", "legacy_old"),\n'
        '        migrations.RemoveField("sample_history", "legacy_old"),\n'
        "    ]\n"
    )
    write_sample(lab, ("  old: { type: string }\n", ""), ("schema_version: 1", "schema_version: 2"))
    (lab / "plugin.yaml").write_text(LAB.replace("1.0.0", "1.1.0"))
    v2 = build_to(lab.parent, "v2", "lab")

    deployed(v1, database)
    orm(v1, database, SAMPLE_ROW.replace("NOTES", ""))
    for _ in range(2):
        deployed(v2, database)
        assert "0001_replacement" in applied(database, "acme__lab")
        assert "old" not in columns(database, "acme__lab_sample")

        out = deployed(v1, database)
        assert "Reversed acme/lab 0002_remove_legacy (rolled back)" in out
        assert applied(database, "acme__lab") == ["0001_initial"]
        assert "old" in columns(database, "acme__lab_sample")
        assert query(database, "SELECT name FROM acme__lab_sample") == [("S1",)]


def test_a_rollback_that_would_lose_data_changes_nothing(
    two_builds: tuple[Path, Path], database: str
) -> None:
    v1, v2 = two_builds
    deployed(v1, database)
    deployed(v2, database)
    orm(v2, database, SAMPLE_ROW.replace("NOTES", "notes='Grown at 37 °C'"))
    err = refused(v1, database)
    assert "Rolling back would lose data, so nothing was changed:" in err
    assert (
        "acme/lab 0002_notes: acme__lab_sample.notes has values, and reversing would drop it."
    ) in err
    assert "restore the database from the backup" in err
    assert applied(database, "acme__lab") == ["0001_initial", "0002_notes"]
    assert "notes" in columns(database, "acme__lab_sample")


def test_irreversible_migrations_block_a_rollback(lab: Path, database: str) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    v1 = build_to(lab.parent, "v1", "lab")
    (lab / "migrations" / "0002_backfill.py").write_text(
        "from django.db import migrations\n\n\n"
        "def backfill(apps, schema_editor):\n    pass\n\n\n"
        "class Migration(migrations.Migration):\n"
        '    dependencies = [("acme__lab", "0001_initial")]\n'
        "    operations = [migrations.RunPython(backfill)]\n"
    )
    v2 = build_to(lab.parent, "v2", "lab")
    deployed(v1, database)
    deployed(v2, database)
    err = refused(v1, database)
    assert "acme/lab 0002_backfill: Raw Python operation can't be reversed." in err
    assert applied(database, "acme__lab") == ["0001_initial", "0002_backfill"]


def test_plugins_cant_simply_disappear(two_builds: tuple[Path, Path], database: str) -> None:
    v1, _ = two_builds
    deployed(v1, database)
    empty = v1.parent / "empty" / "iki_installed_plugins"
    empty.mkdir(parents=True)
    from iki_server import __version__

    (empty / "__init__.py").write_text(f"IKI_VERSION = {__version__!r}\nLOCAL = False\nAPPS = []\n")
    err = refused(empty.parent, database)
    assert "The database has the plugin(s) acme/lab, but this build doesn't." in err
    assert "needs it detached, then purged (ADR 0092)" in err


def test_a_database_a_newer_iki_migrated_is_refused(
    two_builds: tuple[Path, Path], database: str
) -> None:
    v1, _ = two_builds
    deployed(v1, database)
    with psycopg.connect(database) as connection:
        connection.execute(
            "INSERT INTO django_migrations (app, name, applied) "
            "VALUES ('iki_entities', '0099_future', now())"
        )
    err = refused(v1, database)
    assert "A newer Iki migrated this database (iki_entities 0099_future)" in err


def test_a_plugin_made_for_another_iki_is_refused(
    two_builds: tuple[Path, Path], database: str
) -> None:
    # As built by a newer CLI: this one refuses it at build (ADR 0095).
    v1, _ = two_builds
    manifest = v1 / "iki_plugin_acme__lab" / "manifest.json"
    data = json.loads(manifest.read_text())
    data["plugin"]["iki"] = ">=99"
    manifest.write_text(json.dumps(data))
    err = refused(v1, database)
    from iki_server import __version__
    from iki_spec import releases

    this = ".".join(map(str, releases.release(__version__)))
    assert f"This is Iki {this}, and acme/lab works with Iki >=99." in err
    assert "nothing was migrated" in err
    assert query(database, "SELECT to_regclass('django_migrations')") == [(None,)]


def test_a_plugin_migration_that_needs_a_newer_iki_is_refused_naming_both(
    two_builds: tuple[Path, Path], database: str
) -> None:
    """A plugin built with a newer Iki can depend on one of that Iki's migrations while its
    `iki:` range still admits this one: refused before anything changes, naming the
    plugin's migration and the one of Iki's it needs, not as Django's missing node."""
    v1, _ = two_builds
    (migration,) = (v1 / "iki_plugin_acme__lab" / "migrations").glob("0001_*.py")
    source = migration.read_text()
    needs = "dependencies = ["
    assert needs in source
    migration.write_text(source.replace(needs, f'{needs}("iki_entities", "0099_future"), ', 1))
    err = refused(v1, database)
    assert "acme/lab 0001_" in err
    assert "needs Iki's migration iki_entities 0099_future" in err
    assert "made with a newer Iki" in err and "nothing was migrated" in err
    assert "NodeNotFoundError" not in err and "nonexistent parent node" not in err
    assert query(database, "SELECT to_regclass('django_migrations')") == [(None,)]


def test_applied_migrations_cant_change(two_builds: tuple[Path, Path], database: str) -> None:
    v1, _ = two_builds
    deployed(v1, database)
    migration = v1 / "iki_plugin_acme__lab" / "migrations" / "0001_initial.py"
    migration.write_text(migration.read_text() + "# edited\n")
    err = refused(v1, database)
    assert "Applied migrations changed since they ran here: acme/lab 0001_initial." in err


def test_deploy_requires_review_before_adding_a_rule_existing_records_break(
    lab: Path, database: str
) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    v1 = build_to(lab.parent, "v1", "lab")
    deployed(v1, database)
    orm(
        v1,
        database,
        RECORDS + '\nresult = [record("Sample", name=n, count=3).hrid for n in ("A", "B")]',
    )
    rule = "rules:\n  minimum_count:\n    check: record.count >= 5\n    message: At least five.\n"
    (lab / "types" / "sample.yaml").write_text(SAMPLE + "  notes: { type: text }\n" + rule)
    made("lab", "--name", "notes", "--no-input", cwd=lab.parent)
    v2 = build_to(lab.parent, "v2", "lab")

    message = refused(v2, database)
    assert "acme/lab.Sample minimum_count (exactly 2 affected)" in message
    acknowledgement = re.search(r"--acknowledge-preflight (\S+)", message)
    assert acknowledgement, message
    assert applied(database, "acme__lab") == ["0001_initial"]
    previous = query(database, "SELECT revision FROM iki_plugin_baseline")

    args = ["-m", "django", "migrate_deployment", "--no-grant", "--acknowledge-preflight"]
    wrong = django(v2, database, *args, "a" * 64)
    assert wrong.returncode != 0 and "minimum_count" in wrong.stderr
    reviewed = django(v2, database, *args, acknowledgement[1])
    assert reviewed.returncode == 0, reviewed.stdout + reviewed.stderr
    assert applied(database, "acme__lab") == ["0001_initial", "0002_notes"]
    assert query(database, "SELECT revision FROM iki_plugin_baseline") != previous


def test_mixed_preflight_refusal_identifies_partial_checks_and_exact_counts() -> None:
    from iki_server.plugins.deploy import _preflight_refusal
    from iki_server.plugins.preflight_types import Finding, Report, TypeReport

    result = TypeReport(
        type="acme/lab.Sample",
        checked=500,
        sampled=True,
        total=200_000,
        sampled_checks=["minimum_count", "aliquots"],
        findings=[
            Finding("backfill", "notes", "Supply a value.", True, 34_782, []),
            Finding("rule", "minimum_count", "Below the minimum.", True, 8, [], sampled=True),
        ],
    )
    report = Report("acme/lab", "2", "deployed", "candidate", [result])
    message = "\n".join(_preflight_refusal(report))
    assert "Sampled 500 of 200,000 acme/lab.Sample records for minimum_count, aliquots." in message
    assert "notes (exactly 34,782 affected)" in message
    assert "minimum_count (at least 8 affected)" in message


def test_deploy_needs_the_current_manifest_when_plugin_migrations_are_applied(
    two_builds: tuple[Path, Path], database: str
) -> None:
    v1, v2 = two_builds
    migrate(v1, database)
    # A database created with plain migrate has tables, but hasn't synced any manifests.
    err = refused(v2, database)
    assert "sync_plugins from the currently deployed build" in err
    assert applied(database, "acme__lab") == ["0001_initial"]
    sync(v1, database)
    deployed(v2, database)
    assert "notes" in columns(database, "acme__lab_sample")


def test_the_app_can_only_read_the_kept_copies() -> None:
    from iki_server.core import db_roles

    assert "iki_plugin_migration" in db_roles.READ_ONLY_TABLES
    assert "iki_plugin_baseline" in db_roles.READ_ONLY_TABLES
    # Nor what migrations did to kept history (ADR 0148).
    assert "iki_history_change" in db_roles.READ_ONLY_TABLES


# --- Guards while a deploy migrates (ADR 0134) ---------------------------------------------------

#: A sample with a unique field (ADR 0082), a state that locks (ADR 0125) and a unique rule
#: (ADR 0127): each guarded by triggers that `sync_plugins` makes.
GUARDED = """\
apiVersion: iki/v1
kind: EntityType
name: Sample
schema_version: 1
hrid: { prefix: SMP }
fields:
  name: { type: string, max_length: 100 }
  lims_id: { type: string, max_length: 40, indexed: true, unique: true }
  lot: { type: string, max_length: 40 }
  product: { type: string, max_length: 40 }
  status:
    type: state
    initial: received
    states:
      received: { set_by: [analyst] }
      released: { set_by: [qa], locks: true }
rules:
  one_lot_per_product:
    label: One lot number per product
    unique: [lot, product]
    message: Product {{ record.product }} has a lot {{ record.lot }} already.
"""
RENAMED = (
    GUARDED.replace("  lot: {", "  lot_number: {")
    .replace("[lot, product]", "[lot_number, product]")
    .replace("record.lot ", "record.lot_number ")
)

GUARDED_ROWS = (
    RECORDS
    + """
LabRoleGrant.objects.create(membership=member, role="acme/lab.qa")
released = record("Sample", name="S1", lims_id="LIMS-1", lot="L1", product="P")
states.set_state(user, released, "status", "released", expected_revision=released.revision)
other = record("Sample", name="S2", lims_id="LIMS-2", lot="L1", product="Q")
result = [str(released.pk), str(other.pk)]
"""
)

#: A migration a plugin's author wrote by hand, rebuilding the table under its guards.
REBUILD = """\
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("acme__lab", "0001_initial")]
    operations = [
        migrations.RunSQL(
            "CREATE TABLE acme__lab_sample_new (LIKE acme__lab_sample INCLUDING ALL); "
            "INSERT INTO acme__lab_sample_new SELECT * FROM acme__lab_sample; "
            "DROP TABLE acme__lab_sample; "
            "ALTER TABLE acme__lab_sample_new RENAME TO acme__lab_sample",
            migrations.RunSQL.noop,
        )
    ]
"""

FAILS = """\
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("acme__lab", "0002_rename")]
    operations = [migrations.RunSQL("SELECT 1 / 0", migrations.RunSQL.noop)]
"""


def guard_probes(released: str, other: str) -> dict[str, tuple[str, list[str]]]:
    """What each guard refuses, written as an old pod or any other client would."""
    table = "UPDATE acme__lab_sample SET {} WHERE entity_id = %s"
    return {
        # S2 becomes a second lot L1 of product P.
        "rule": (table.format("product = 'P'"), [other]),
        # S2 takes S1's LIMS ID.
        "unique": (table.format("lims_id = 'LIMS-1'"), [other]),
        # S1 is released, which locks it.
        "lock": (table.format("name = 'changed'"), [released]),
    }


def write_through(database: str, probes: dict[str, tuple[str, list[str]]]) -> dict[str, str]:
    """Tries each write on a connection of its own, as an old pod serving through a deploy
    does: ``written``, ``refused`` by a guard, or ``blocked`` by the deploy's locks. Each is
    rolled back, so the next sees the same rows."""
    out = {}
    for name, (statement, params) in probes.items():
        with psycopg.connect(database) as connection:
            connection.execute("SET lock_timeout = '1s'")
            try:
                connection.execute(statement, params)
                out[name] = "written"
            except psycopg.errors.LockNotAvailable:
                out[name] = "blocked"
            except (psycopg.errors.IntegrityError, psycopg.errors.CheckViolation):
                out[name] = "refused"
            connection.rollback()
    return out


#: Deploys the build, writing through it from other connections once its migrations have
#: run and before its sync does: the moment old pods kept serving through, before.
DEPLOY_WRITING = """
import json, os
import django
django.setup()
from django.core.management import call_command
from iki_server.plugins import sync
from test_plugin_migrations_deploy import write_through

probes = json.loads(os.environ["PROBES"])
seen = {}
synced = sync.sync

def writing(*args, **kwargs):
    seen.update(write_through(os.environ["IKI_DATABASE_URL"], probes))
    return synced(*args, **kwargs)

sync.sync = writing
call_command("migrate_deployment", "--no-grant")
print(json.dumps(seen))
"""


def deploy_writing(
    build: Path, database: str, probes: dict[str, tuple[str, list[str]]]
) -> dict[str, str]:
    result = subprocess.run(  # noqa: S603 - this interpreter, Django
        [sys.executable, "-c", DEPLOY_WRITING],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "iki_test_settings",
            "PYTHONPATH": os.pathsep.join([str(build), str(TESTS)]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "IKI_DATABASE_URL": database,
            "OTEL_SDK_DISABLED": "true",
            "IKI_ALLOW_LOCAL_PLUGINS": "true",
            "IKI_TEST_PLUGINS": "0",
            "PROBES": json.dumps(probes),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    seen: dict[str, str] = json.loads(result.stdout.strip().splitlines()[-1])
    return seen


def guarded(lab: Path, database: str) -> tuple[Path, dict[str, tuple[str, list[str]]]]:
    """The lab plugin with its guards, deployed, with two samples; and the writes its guards
    refuse."""
    (lab / "plugin.yaml").write_text(LAB + LAB_ROLES)
    (lab / "types" / "sample.yaml").write_text(GUARDED)
    made("lab", "--no-input", cwd=lab.parent)
    v1 = build_to(lab.parent, "v1", "lab")
    deployed(v1, database)
    probes = guard_probes(*orm(v1, database, GUARDED_ROWS))
    assert write_through(database, probes) == dict.fromkeys(probes, "refused")
    return v1, probes


@pytest.mark.parametrize("change", ["rename", "rebuild"])
def test_no_write_skips_a_guard_while_a_deploy_migrates(
    lab: Path, database: str, change: str
) -> None:
    """ADR 0134. Migrations used to commit before `sync_plugins` put the guards back on what
    they changed: a rule's trigger reading a renamed column saw no value, and a rebuilt
    table had no triggers at all, so writes in between skipped them. Now the guards are put
    back in the migrations' own transaction, and a write can't land between the two."""
    _, probes = guarded(lab, database)
    if change == "rename":
        (lab / "types" / "sample.yaml").write_text(RENAMED)
        made("lab", "--name", "rename", cwd=lab.parent, answers="y\n")
    else:
        (lab / "migrations" / "0002_rebuild.py").write_text(REBUILD)
    v2 = build_to(lab.parent, "v2", "lab")

    during = deploy_writing(v2, database, probes)
    assert during == dict.fromkeys(probes, "blocked")
    assert write_through(database, probes) == dict.fromkeys(probes, "refused")
    assert applied(database, "acme__lab")[-1] == f"0002_{change}"


#: A migration a plugin's author wrote by hand, after the one changing the column: it
#: writes the rows no state locks, through whatever guards the table has at that point.
TOUCH = """\
from django.db import migrations
from iki_server.plugins.operations import MigrateValues

def lower(row):
    return {{"{column}": row.{column}.lower()}} if row.status != "released" else {{}}

class Migration(migrations.Migration):
    dependencies = [
        ("acme__lab", "0002_change"), ("iki_entities", "0027_migrated_revisions")
    ]
    operations = [
        MigrateValues("Sample", lower, version="1.1.0", migration="0003_touch",
                      reason="Normalize the label of each open sample."),
        migrations.RunSQL("UPDATE acme__lab_sample SET lot = lot WHERE status <> 'released'",
                          migrations.RunSQL.noop),
    ]
"""

#: Each change to the unique field `lims_id`, and to `lot`, which a rule compares: what the
#: type becomes, and the column the unique field is in afterwards (None once it's gone).
COLUMN_CHANGES = {
    # Postgres won't change the type of a column a trigger names in `UPDATE OF`.
    "type": (
        GUARDED.replace("max_length: 40, indexed", "max_length: 64, indexed").replace(
            "lot: { type: string, max_length: 40 }", "lot: { type: string, max_length: 64 }"
        ),
        "lims_id",
    ),
    # Its column is dropped from the table, its trigger with it.
    "drop": (GUARDED.replace("unique: true }", "unique: true, removed: true }"), None),
    # The trigger still names the old column, so it can't keep the renamed one's values.
    "rename": (GUARDED.replace("  lims_id: {", "  lims_code: {"), "lims_code"),
}


@pytest.mark.parametrize("change", sorted(COLUMN_CHANGES))
def test_a_migration_can_change_a_guarded_column(lab: Path, database: str, change: str) -> None:
    """A unique field's trigger, and a rule's, name the columns they guard (`UPDATE OF`), so
    Postgres refused to change their type, and a trigger kept reading a renamed column by
    its old name. The deploy now takes them off what its migrations change and puts them
    back, in its one transaction, before anything else can write (ADR 0134)."""
    _, probes = guarded(lab, database)
    text, column = COLUMN_CHANGES[change]
    (lab / "types" / "sample.yaml").write_text(text)
    made("lab", "--name", "change", cwd=lab.parent, answers="y\n")
    (lab / "migrations" / "0003_touch.py").write_text(TOUCH.format(column=column or "name"))
    v2 = build_to(lab.parent, "v2", "lab")

    deployed(v2, database)
    assert applied(database, "acme__lab")[-2:] == ["0002_change", "0003_touch"]
    if column is None:
        del probes["unique"]
        assert "lims_id" not in columns(database, "acme__lab_sample")
        assert query(database, "SELECT key FROM iki_unique_value WHERE key LIKE %s", "%lims%") == []
    else:
        statement, params = probes["unique"]
        probes["unique"] = (statement.replace("lims_id", column), params)
        values = query(
            database,
            "SELECT key, value FROM iki_unique_value WHERE key NOT LIKE %s ORDER BY 2",
            "%:%",
        )
        assert values == [
            (f"acme__lab_sample.{column}", "LIMS-1"),
            (f"acme__lab_sample.{column}", "lims-2"),
        ]
    assert write_through(database, probes) == dict.fromkeys(probes, "refused")
    triggers = query(
        database,
        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'acme__lab_sample'::regclass "
        "AND NOT tgisinternal AND starts_with(tgname, %s) ORDER BY 1",
        "iki_unique__",
    )
    assert triggers == ([] if column is None else [(f"iki_unique__{column}",)])


def test_a_rollback_can_change_a_guarded_column_back(lab: Path, database: str) -> None:
    """Reversing a migration changes columns too: deploying the older build changes the
    unique field's type back, under the trigger the newer build's sync made on it."""
    (lab / "plugin.yaml").write_text(LAB + LAB_ROLES)
    (lab / "types" / "sample.yaml").write_text(GUARDED)
    made("lab", "--no-input", cwd=lab.parent)
    v1 = build_to(lab.parent, "v1", "lab")
    deployed(v1, database)
    (lab / "types" / "sample.yaml").write_text(
        GUARDED.replace("max_length: 40, indexed", "max_length: 64, indexed")
    )
    made("lab", "--name", "longer", cwd=lab.parent, answers="y\n")
    deployed(build_to(lab.parent, "v2", "lab"), database)

    out = deployed(v1, database)
    assert "Reversed acme/lab 0002_longer" in out, out
    assert applied(database, "acme__lab") == ["0001_initial"]
    width = (
        "SELECT character_maximum_length FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'acme__lab_sample' "
        "AND column_name = 'lims_id'"
    )
    assert query(database, width) == [(40,)]
    triggers = query(
        database,
        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'acme__lab_sample'::regclass "
        "AND NOT tgisinternal AND starts_with(tgname, %s)",
        "iki_unique__",
    )
    assert triggers == [("iki_unique__lims_id",)]


#: A box holds a sample or another box, and keeps one spare: references to two types, which
#: the database holds to them by a trigger naming their columns (ADR 0136). A shelf is
#: neither.
SHELF = """\
apiVersion: iki/v1
kind: EntityType
name: Shelf
schema_version: 1
hrid: { prefix: SHF }
"""
BOX = """\
apiVersion: iki/v1
kind: EntityType
name: Box
schema_version: 1
hrid: { prefix: BOX }
fields:
  stored: { type: ref, to: [Sample, Box] }
  spare: { type: ref, to: [Sample, Box] }
"""

BOX_TOUCH = """\
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("acme__lab", "0002_change")]
    operations = [
        migrations.RunSQL("UPDATE acme__lab_box SET spare_id = spare_id", migrations.RunSQL.noop)
    ]
"""


def test_a_migration_can_drop_a_reference_to_several_types(lab: Path, database: str) -> None:
    """A reference's types' trigger reads its columns by name, as a unique field's does: a
    migration dropping one, then writing the table, failed under the trigger still naming
    it. The deploy takes it off and puts it back for the columns left."""
    (lab / "types" / "box.yaml").write_text(BOX)
    (lab / "types" / "shelf.yaml").write_text(SHELF)
    made("lab", "--no-input", cwd=lab.parent)
    v1 = build_to(lab.parent, "v1", "lab")
    deployed(v1, database)
    box, sample, shelf = orm(
        v1,
        database,
        RECORDS
        + """
sample = record("Sample", name="S1")
box = record("Box", stored=str(sample.pk), spare=str(sample.pk))
shelf = record("Shelf")
result = [str(box.pk), str(sample.pk), str(shelf.pk)]
""",
    )
    (lab / "types" / "box.yaml").write_text(
        BOX.replace(
            "  stored: { type: ref, to: [Sample, Box] }",
            "  stored: { type: ref, to: [Sample, Box], removed: true }",
        )
    )
    made("lab", "--name", "change", cwd=lab.parent)
    (lab / "migrations" / "0003_touch.py").write_text(BOX_TOUCH)

    deployed(build_to(lab.parent, "v2", "lab"), database)
    assert applied(database, "acme__lab")[-2:] == ["0002_change", "0003_touch"]
    assert "stored_id" not in columns(database, "acme__lab_box")
    assert query(database, "SELECT spare_id::text FROM acme__lab_box") == [(sample,)]
    triggers = query(
        database,
        "SELECT tgname, encode(tgargs, 'escape') FROM pg_trigger "
        "WHERE tgrelid = 'acme__lab_box'::regclass AND starts_with(tgname, %s) ORDER BY 1",
        "iki_reference_types__",
    )
    assert [name for name, _ in triggers] == [
        "iki_reference_types__ins",
        "iki_reference_types__upd",
    ]
    assert all(args.startswith("spare_id\\000") for _, args in triggers), triggers
    # And it holds the reference left to its types.
    with pytest.raises(psycopg.errors.CheckViolation, match="spare_id can't refer to"):
        query(database, "UPDATE acme__lab_box SET spare_id = %s WHERE entity_id = %s", shelf, box)


def test_a_deploy_that_fails_halfway_leaves_the_guards_as_they_were(
    lab: Path, database: str
) -> None:
    """ADR 0134. A failed migration used to leave those before it committed and the sync
    not run, so the guards stayed out of step until a deploy succeeded. Now nothing of the
    deploy's is kept: the old schema, its guards and its views."""
    _, probes = guarded(lab, database)
    views = "SELECT viewname FROM pg_views WHERE schemaname = 'iki_views' AND viewname = %s"
    assert query(database, views, "acme__lab_sample") == [("acme__lab_sample",)]
    (lab / "types" / "sample.yaml").write_text(RENAMED)
    made("lab", "--name", "rename", cwd=lab.parent, answers="y\n")
    (lab / "migrations" / "0003_fails.py").write_text(FAILS)
    v2 = build_to(lab.parent, "v2", "lab")

    failed = deploy(v2, database)
    assert failed.returncode != 0, failed.stdout
    assert "division by zero" in failed.stderr, failed.stderr
    assert "Nothing of the plugins' changes was kept" in failed.stderr, failed.stderr
    assert applied(database, "acme__lab") == ["0001_initial"]
    assert "lot" in columns(database, "acme__lab_sample")
    assert write_through(database, probes) == dict.fromkeys(probes, "refused")
    assert query(database, views, "acme__lab_sample") == [("acme__lab_sample",)]
