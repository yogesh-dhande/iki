"""What a plugin migration may use of Iki, checked from its files alone (ADR 0296)."""

# ruff: noqa: E501 - the migrations below are as makemigrations writes them, long lines included

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from iki_spec import migration_api, releases
from iki_spec.migration_api import Anchor
from iki_spec.validate import load_plugin

SPEC = Path(__file__).resolve().parents[1]
README = SPEC / "README.md"

GOOD = """\
# Made by `iki plugin makemigrations`.

import datetime

from iki_server.plugins import operations as iki


class Migration(iki.Migration):

    initial = True

    dependencies = [
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='Sample',
            fields=[
                ('entity', iki.OneToOneField(on_delete=iki.PROTECT, parent_link=True, primary_key=True, to=iki.ENTITY)),
                ('made', iki.DateField(db_default=datetime.date(2026, 1, 1))),
            ],
            table='acme__lab_sample',
            constraints=[
                iki.CheckConstraint(condition=iki.Q(('made__gte', datetime.date(2000, 1, 1))), name='acme__lab_sample__made__min'),
            ],
            bases=(iki.ENTITY,),
        ),
    ]
"""

DJANGO = """\
import django.db.models.lookups
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('iki_entities', '0028_record_exports'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AddConstraint(
            model_name='sample',
            constraint=models.CheckConstraint(condition=django.db.models.lookups.GreaterThanOrEqual(1, 1), name='x'),
        ),
    ]
"""


@pytest.fixture
def two_releases(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Iki as it will be at 0.2: an anchor for 0.1 and one for 0.2, and a name 0.2 adds."""
    anchors = (
        Anchor((0, 1), "0001_iki_0_1", {"iki_entities": "0028_record_exports"}),
        Anchor((0, 2), "0002_iki_0_2", {"iki_entities": "0031_later"}),
    )
    names = {**migration_api.names(), "Newer": (0, 2)}
    monkeypatch.setattr(migration_api, "anchors", lambda: anchors)
    monkeypatch.setattr(migration_api, "names", lambda: names)
    monkeypatch.setattr(releases, "iki", lambda: "0.2.0")
    yield


def plugin(tmp_path: Path, *migrations: str, iki: str | None = '">=0.1"') -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugin.yaml").write_text(
        "apiVersion: iki/v1\nkind: Plugin\nname: acme/lab\nversion: 1.0.0\n"
        + (f"iki: {iki}\n" if iki else "")
    )
    (tmp_path / "migrations").mkdir()
    for number, text in enumerate(migrations, start=1):
        (tmp_path / "migrations" / f"{number:04}_m.py").write_text(text)
    return tmp_path


def problems(directory: Path, requirement: str | None = ">=0.1") -> list[tuple[str, str]]:
    found = migration_api.check(directory, "acme/lab", requirement)
    return [(p.code, p.message + " " + (p.hint or "")) for p in found]


def test_a_migration_in_ikis_operations_is_fine(tmp_path: Path) -> None:
    assert problems(plugin(tmp_path, GOOD)) == []
    result = load_plugin(plugin(tmp_path / "loaded", GOOD))
    assert result.plugin is not None, result.problems


def test_each_import_of_django_is_refused_naming_the_wrappers(tmp_path: Path) -> None:
    found = migration_api.check(plugin(tmp_path, DJANGO), "acme/lab", ">=0.1")
    imports = [p for p in found if p.code == "migration-import"]
    assert [(p.location.line if p.location else 0) for p in imports] == [1, 2, 3]
    lookups, conf, db = (p.hint or "" for p in imports)
    assert "`django.db.models.lookups.GreaterThanOrEqual` is `iki.GreaterThanOrEqual`" in lookups
    assert "`django.conf.settings.AUTH_USER_MODEL` is `iki.USER`" in conf
    assert "`django.db.models.CheckConstraint` is `iki.CheckConstraint`" in db
    assert "`django.db.migrations.swappable_dependency` has none" in db
    assert all(p.file == "migrations/0001_m.py" for p in found)
    assert {p.code for p in found} >= {"migration-shape", "migration-import"}


def test_a_dependency_on_one_of_ikis_own_migrations_names_the_anchor_instead(
    tmp_path: Path,
) -> None:
    text = GOOD.replace(
        "('iki_anchors', '0001_iki_0_1')", "('iki_entities', '0028_record_exports')"
    )
    assert problems(plugin(tmp_path, text)) == [
        (
            "migration-shape",
            "0001_m.py is the plugin's first migration and depends on none of Iki's anchors, so "
            "it may run before the tables it needs. Add `('iki_anchors', '0001_iki_0_1')` to its "
            "dependencies: the anchor of the oldest release `iki:` admits.",
        ),
        (
            "migration-core-dependency",
            "It depends on iki_entities 0028_record_exports, one of Iki's own migrations, which "
            "a later Iki may squash or rename. Depend on `('iki_anchors', '0001_iki_0_1')` "
            "instead: the anchor of the oldest release `iki:` admits, which `iki plugin "
            "makemigrations` writes.",
        ),
    ]


def test_the_core_plugins_migrations_are_ikis_own(tmp_path: Path) -> None:
    text = GOOD.replace(
        "('iki_anchors', '0001_iki_0_1'),",
        "('iki_anchors', '0001_iki_0_1'), ('iki__core', '0007_x'),",
    )
    assert [code for code, _ in problems(plugin(tmp_path, text))] == ["migration-core-dependency"]


@pytest.mark.usefixtures("two_releases")
def test_an_anchor_newer_than_iki_admits_is_refused_naming_its_release(tmp_path: Path) -> None:
    text = GOOD.replace("0001_iki_0_1", "0002_iki_0_2")
    assert problems(plugin(tmp_path, text)) == [
        (
            "migration-iki-range",
            "It depends on Iki 0.2's anchor, and `iki:` (>=0.1) admits 0.1.0, which hasn't got "
            'it. Raise `iki:` to `">=0.2"`, or make the migration with the Iki its oldest '
            "release is.",
        )
    ]
    assert problems(tmp_path, ">=0.2") == []
    # Without `iki:`, which a plugin must have (ADR 0293), there's no range to check.
    assert problems(tmp_path, None) == []


@pytest.mark.usefixtures("two_releases")
def test_a_name_newer_than_iki_admits_is_refused(tmp_path: Path) -> None:
    text = GOOD.replace("iki.CheckConstraint(", "iki.Newer(")
    assert problems(plugin(tmp_path, text)) == [
        (
            "migration-iki-range",
            "`iki.Newer` needs Iki 0.2 or later, and `iki:` (>=0.1) admits 0.1.0. "
            'Raise `iki:` to `">=0.2"`.',
        )
    ]


def test_an_anchor_this_iki_hasnt_got_was_made_with_a_newer_iki(tmp_path: Path) -> None:
    text = GOOD.replace("0001_iki_0_1", "0007_iki_9_4")
    ((code, message),) = problems(plugin(tmp_path, text))
    assert code == "migration-newer-iki"
    assert message.startswith(
        f"It depends on Iki 9.4's anchor (iki_anchors 0007_iki_9_4), which this Iki "
        f"({releases.iki()}) doesn't have: the migration was made with a newer Iki."
    )


def test_only_the_operations_and_the_standard_library_are_imported(tmp_path: Path) -> None:
    headers = {
        "import iki_server.plugins.operations\n": "iki_server.plugins.operations.",
        "import iki_server.plugins.operations as ops\n": "ops.",
        "from iki_server.plugins import operations\n": "operations.",
        "import math\nfrom iki_server.plugins import operations as iki\n": "iki.",
    }
    for index, (header, prefix) in enumerate(headers.items()):
        text = GOOD.replace("from iki_server.plugins import operations as iki\n", header)
        text = text.replace("iki.", prefix).replace("(iki.Migration)", f"({prefix}Migration)")
        directory = plugin(tmp_path / str(index), text)
        assert problems(directory) == [], header
    refused = {
        "from . import helpers\n": "`from . import …`",
        "import numpy\n": "imports `numpy`",
        "from iki_server.entities import fields\n": "imports `iki_server.entities`",
        "from iki_spec.identity import identity_hash\n": "imports `iki_spec.identity`",
        "from iki_server.plugins import operations, deploy\n": "imports `iki_server.plugins`",
    }
    for index, (header, says) in enumerate(refused.items()):
        directory = plugin(tmp_path / f"refused{index}", header + GOOD)
        found = problems(directory)
        assert [code for code, _ in found] == ["migration-import"], (header, found)
        assert says in found[0][1], found


def test_names_outside_the_api_are_refused(tmp_path: Path) -> None:
    text = GOOD.replace("iki.CheckConstraint(", "iki.unlocked(").replace(
        "import datetime\n", "import datetime\nimport iki_server.plugins.operations\n"
    )
    text += "\nRAW = iki.RunPython\nDEEP = iki_server.entities.fields\n"
    found = problems(plugin(tmp_path, text))
    assert [code for code, _ in found] == [
        "migration-unknown-name",
        "migration-unknown-name",
        "migration-import",
    ]
    assert "`unlocked` isn't one of the operations Iki keeps" in found[0][1]
    assert "`RunPython` isn't one" in found[1][1]
    assert "uses `iki_server.entities.fields`" in found[2][1]


def test_the_migration_class_and_its_dependencies_are_plain(tmp_path: Path) -> None:
    computed = GOOD.replace(
        "dependencies = [\n        ('iki_anchors', '0001_iki_0_1'),\n    ]",
        "dependencies = [ANCHOR]",
    )
    assert [c for c, _ in problems(plugin(tmp_path / "a", computed))] == ["migration-shape"]
    django = GOOD.replace("(iki.Migration)", "(object)")
    assert problems(plugin(tmp_path / "b", django))[0][0] == "migration-shape"
    missing = GOOD.replace("('iki_anchors', '0001_iki_0_1'),", "('acme__lab', '0009_gone'),")
    assert any(
        "0009_gone, which isn't in migrations/" in m
        for _, m in problems(plugin(tmp_path / "c", missing))
    )
    assert problems(plugin(tmp_path / "d", "def broken(:\n"))[0][0] == "migration-syntax"


def test_later_migrations_and_other_plugins_need_no_anchor(tmp_path: Path) -> None:
    second = GOOD.replace(
        "('iki_anchors', '0001_iki_0_1'),",
        "('acme__lab', '0001_m'), ('iki__dna', '0003_sequences'),",
    )
    assert problems(plugin(tmp_path, GOOD, second)) == []


def test_validate_reports_them_where_they_are(tmp_path: Path) -> None:
    result = load_plugin(plugin(tmp_path, DJANGO))
    assert result.plugin is None
    first = next(p for p in result.problems if p.code == "migration-import")
    assert (first.file, first.location and first.location.line) == ("migrations/0001_m.py", 1)
    # An earlier commit's migrations were checked when it was made.
    assert not [
        p for p in load_plugin(tmp_path, historical=True).problems if p.code.startswith("migration")
    ]


def test_the_anchor_of_the_oldest_release_iki_admits(two_releases: None) -> None:
    assert migration_api.anchor_for(">=0.1").migration == "0001_iki_0_1"  # type: ignore[union-attr]
    assert migration_api.anchor_for(">=0.2.3").migration == "0002_iki_0_2"  # type: ignore[union-attr]
    assert migration_api.anchor_for(">0.1,<0.3").migration == "0001_iki_0_1"  # type: ignore[union-attr]
    assert migration_api.anchor_for("<0.3") is None
    assert migration_api.anchor_for(None).migration == "0002_iki_0_2"  # type: ignore[union-attr]
    assert migration_api.anchor_dependencies("0.2") == [
        ("iki_entities", "0031_later"),
        ("iki_anchors", "0001_iki_0_1"),
    ]


def test_the_table_is_complete_and_never_loses_a_row() -> None:
    data = json.loads((SPEC / "src/iki_spec/migration_api.json").read_text())
    anchors = migration_api.anchors()
    assert [a.release for a in anchors] == sorted({a.release for a in anchors})
    for number, anchor in enumerate(anchors, start=1):
        assert anchor.migration == f"{number:04}_iki_{anchor.release[0]}_{anchor.release[1]}"
    # The release in development, or the one released last, has the last anchor.
    assert anchors[-1].release == releases.release(releases.iki())[:2]
    assert set(data["names"]) == set(migration_api.names())


def test_the_spec_prints_the_table() -> None:
    text = README.read_text()
    for anchor in migration_api.anchors():
        row = re.search(
            rf"^\| {re.escape(anchor.version)} +\| `{anchor.migration}` +\| (.+) \|$", text, re.M
        )
        assert row is not None, anchor
        printed = dict(re.findall(r"(\S+) `([^`]+)`", row.group(1)))
        assert printed == anchor.after, "Copy the anchor's row from migration_api.json."
    listed = set(re.findall(r"`iki\.([A-Za-z_]+)`", text.split("## Migrations", 1)[1]))
    assert set(migration_api.names()) <= listed
