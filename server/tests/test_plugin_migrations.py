"""Plugins compiled to Django apps, with their migrations made and run on PostgreSQL (ADR 0033).

Each test copies plugins into a temporary directory, makes their migrations
with ``iki plugin makemigrations`` (as an author would), builds them with
``iki plugin build`` (as a deployment would), and migrates a database of its
own, in subprocesses, since the apps can't be installed in this process.
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

TESTS = Path(__file__).parent
PLUGINS = TESTS.parents[1] / "spec" / "tests" / "plugins"


@pytest.fixture
def database(_postgres_url: str) -> Iterator[str]:
    """A database of the test's own, which plugin apps migrate."""
    name = f"iki_plugins_{secrets.token_hex(4)}"
    with psycopg.connect(_postgres_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parts = urlsplit(_postgres_url)
    yield urlunsplit(parts._replace(path=f"/{name}"))
    with psycopg.connect(_postgres_url, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def iki(*args: str | Path, cwd: Path, answers: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - this interpreter, the iki CLI
        [sys.executable, "-m", "iki_cli", *map(str, args)],
        cwd=cwd,
        input=answers,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NO_COLOR": "1"},
    )


def made(*args: str | Path, cwd: Path, answers: str = "") -> str:
    result = iki("plugin", "makemigrations", *args, cwd=cwd, answers=answers)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def django(build: Path, database: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - this interpreter, Django
        [sys.executable, *args],
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
        },
    )


def migrate(build: Path, database: str, *args: str) -> str:
    result = django(build, database, "-m", "django", "migrate", "--no-input", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


WORLD = """
from iki_server.org.models import Project, Team
from iki_server.files.models import Blob
team = Team.objects.create(name="Biologics")
project = Project.objects.create(team=team, name="Antibodies")
blob = Blob.objects.create(sha256="a" * 64, size=10, media_type="chemical/x-pdb",
                           storage_uri="s3://iki/a")
"""


def orm(build: Path, database: str, code: str) -> Any:
    """Runs ``code`` with Django set up, the plugins installed and ``WORLD`` made.

    Returns what the code puts in ``result``, through JSON.
    """
    script = "import django, json\ndjango.setup()\n" + WORLD + textwrap.dedent(code)
    script += "\nprint(json.dumps(result, default=str))\n"
    out = django(build, database, "-c", script)
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def query(database: str, statement: str, *params: object) -> list[tuple[Any, ...]]:
    with psycopg.connect(database) as connection:
        return connection.execute(statement, params).fetchall()


@pytest.fixture
def plugins(tmp_path: Path) -> Path:
    shutil.copytree(PLUGINS, tmp_path / "plugins")
    return tmp_path / "plugins"


def build(plugins: Path, *names: str) -> Path:
    output = plugins.parent / "build"
    result = iki("plugin", "build", *names, "--output", output, cwd=plugins)
    assert result.returncode == 0, result.stdout + result.stderr
    return output


# --- The first-party style plugins -------------------------------------------------------------


def make_fixture_migrations(plugins: Path) -> None:
    made("dna", "--no-input", cwd=plugins)
    made("protein", "--with", "dna", "--no-input", cwd=plugins)
    made("structure_prediction", "--with", "protein", "--with", "dna", "--no-input", cwd=plugins)


def test_makemigrations_writes_to_the_plugin(plugins: Path) -> None:
    out = made("dna", "--no-input", cwd=plugins)
    assert "Migrations for 'dna':\n  dna/migrations/0001_initial.py\n" in out
    assert sorted(p.name for p in (plugins / "dna" / "migrations").iterdir()) == ["0001_initial.py"]
    again = iki("plugin", "makemigrations", "dna", "--check", cwd=plugins)
    assert again.returncode == 0, again.stderr
    assert "No changes detected" in again.stdout


def test_makemigrations_needs_dependencies(plugins: Path) -> None:
    result = iki("plugin", "makemigrations", "protein", "--no-input", cwd=plugins)
    assert result.returncode == 1
    assert "[undeclared-dependency]" not in result.stdout
    assert "dna" in result.stdout + result.stderr
    result = iki("plugin", "makemigrations", "protein", "--with", "dna", cwd=plugins)
    assert result.returncode == 1
    assert result.stderr == (
        "error: dna has no migrations. Run `iki plugin makemigrations` in dna first.\n"
    )


def test_check_fails_when_manifests_change(plugins: Path) -> None:
    made("dna", "--no-input", cwd=plugins)
    types = plugins / "dna" / "types" / "sequence.yaml"
    types.write_text(types.read_text().replace("  name:", "  note: { type: text }\n  name:"))
    result = iki("plugin", "makemigrations", "dna", "--check", cwd=plugins)
    assert result.returncode == 1
    assert "+ Add field note to sequence" in result.stdout
    assert not (
        plugins / "dna" / "migrations" / "0002_sequence_note_sequence_history_note.py"
    ).exists()


def test_fixture_plugins_migrate_forwards_and_back(plugins: Path, database: str) -> None:
    make_fixture_migrations(plugins)
    out = build(plugins, "dna", "protein", "structure_prediction")
    migrate(out, database)
    tables = {
        row[0]
        for row in query(
            database,
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND "
            "(tablename LIKE 'dna%%' OR tablename LIKE 'protein%%' "
            "OR tablename LIKE 'structure%%')",
        )
    }
    assert tables == {
        "dna_sequence",
        "dna_sequence__history",
        "dna_dnasequence",
        "dna_dnasequence__history",
        "protein_protein",
        "protein_protein__history",
        "protein_proteinstructure",
        "protein_proteinstructure__history",
        "protein_proteinstructure__chains",
        "structure_prediction__protein_proteinstructure",
        "structure_prediction__protein_proteinstructure__history",
    }
    for app in ("structure_prediction", "protein", "dna"):
        migrate(out, database, app, "zero")
    left = query(
        database,
        "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND "
        "(tablename LIKE 'dna%%' OR tablename LIKE 'protein%%' OR tablename LIKE 'structure%%')",
    )
    assert left == [(0,)]


def test_generated_models_store_entities(plugins: Path, database: str) -> None:
    make_fixture_migrations(plugins)
    out = build(plugins, "dna", "protein", "structure_prediction")
    migrate(out, database)
    result = orm(
        out,
        database,
        """
        from iki_plugin_dna.models import DNASequence
        from iki_plugin_protein.models import Protein, ProteinStructure, ProteinStructure_chains
        from iki_plugin_structure_prediction.models import Ext_protein_ProteinStructure
        from iki_server.entities.models import Entity

        dna = DNASequence.objects.create(
            type="dna.DNASequence", hrid="DNA-000001", owner_project=project,
            name="pUC19", bases="ACGTN", length=2686,
        )
        protein = Protein.objects.create(
            type="protein.Protein", hrid="PRT-000001", owner_project=project,
            sequence="MKV", encoded_by=dna, source=dna, molecular_weight=12000.5,
            uniprot_ids=["P69905"],
        )
        structure = ProteinStructure.objects.create(
            type="protein.ProteinStructure", hrid="PST-001", owner_project=project,
            structure=blob,
        )
        ProteinStructure_chains.objects.create(entity=structure, position=0, item=protein)
        Ext_protein_ProteinStructure.objects.create(entity=structure, plddt_mean=91.5)
        dna.refresh_from_db()
        result = {
            "topology": dna.topology,
            "entities": sorted(Entity.objects.values_list("type", flat=True)),
            "name": Protein.objects.get(pk=protein.pk).sequence_ptr.name,
            "chain": ProteinStructure_chains.objects.get().item.sequence,
            "plddt": structure.structure_prediction_Ext.plddt_mean,
        }
        """,
    )
    assert result == {
        "topology": "linear",
        "entities": ["dna.DNASequence", "protein.Protein", "protein.ProteinStructure"],
        "name": None,
        "chain": "MKV",
        "plddt": 91.5,
    }


#: Writes the database refuses, and the CHECK constraint that refuses each.
VIOLATIONS = {
    "bases": ("DNASequence", {"bases": "ACGU"}, "dna_dnasequence__bases__format"),
    "empty": ("DNASequence", {"bases": ""}, "dna_dnasequence__bases__not_empty"),
    "two lines": ("DNASequence", {"name": "pUC\n19"}, "dna_sequence__name__one_line"),
    "topology": ("DNASequence", {"topology": "knotted"}, "dna_dnasequence__topology__values"),
    "gc": ("DNASequence", {"gc_content": 101.0}, "dna_dnasequence__gc_content__max"),
    "nan": ("DNASequence", {"gc_content": "nan"}, "dna_dnasequence__gc_content__finite"),
    "inf": ("DNASequence", {"length": "inf"}, "dna_dnasequence__length__finite"),
    "uniprot": (
        "Protein",
        {"uniprot_ids": ["p69905"]},
        "protein_protein__uniprot_ids__item_pattern",
    ),
    "null item": (
        "Protein",
        {"uniprot_ids": ["P69905", None]},
        "protein_protein__uniprot_ids__plain_list",
    ),
    "empty item": ("Protein", {"uniprot_ids": [""]}, "protein_protein__uniprot_ids__item_length"),
}


def test_the_database_enforces_field_rules(plugins: Path, database: str) -> None:
    make_fixture_migrations(plugins)
    out = build(plugins, "dna", "protein")
    migrate(out, database)
    cases = {name: [model, values] for name, (model, values, _) in VIOLATIONS.items()}
    result = orm(
        out,
        database,
        """
        import itertools
        from django.db import IntegrityError, DataError, transaction
        from iki_plugin_dna.models import DNASequence
        from iki_plugin_protein.models import Protein
        models = {"DNASequence": DNASequence, "Protein": Protein}
        base = {
            "DNASequence": {"type": "dna.DNASequence", "bases": "ACGT"},
            "Protein": {"type": "protein.Protein", "sequence": "MKV"},
        }
        numbers = itertools.count(1)
        result = {}
        for name, (model, values) in CASES.items():
            values = {k: float(v) if v in ("nan", "inf") else v for k, v in values.items()}
            try:
                with transaction.atomic():
                    models[model].objects.create(
                        owner_project=project, hrid=f"X{next(numbers):03}-001",
                        **{**base[model], **values},
                    )
                result[name] = "saved"
            except (IntegrityError, DataError) as error:
                result[name] = getattr(error.__cause__.diag, "constraint_name", None) or str(error)
        # Arrays the ORM never writes, but SQL could.
        from django.db import connection
        protein = Protein.objects.create(
            owner_project=project, hrid="Y001-001", type="protein.Protein", sequence="MKV"
        )
        for name, value in {"nested": "{{P69905},{P69906}}", "from 0": "[0:0]={P69905}"}.items():
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE protein_protein SET uniprot_ids = %s WHERE entity_id = %s",
                        [value, protein.pk],
                    )
                result[name] = "saved"
            except IntegrityError as error:
                result[name] = error.__cause__.diag.constraint_name
        """.replace("CASES", repr(cases)),
    )
    assert result == {
        **{name: constraint for name, (_, _, constraint) in VIOLATIONS.items()},
        "nested": "protein_protein__uniprot_ids__plain_list",
        "from 0": "protein_protein__uniprot_ids__plain_list",
    }


# --- Changing a plugin ---------------------------------------------------------------------------

LAB = """\
apiVersion: iki/v1
kind: Plugin
name: lab
version: 1.0.0
"""

SAMPLE = """\
apiVersion: iki/v1
kind: EntityType
name: Sample
schema_version: 1
hrid: { prefix: SMP }
fields:
  name: { type: string, max_length: 100 }
  mass: { type: quantity, unit: g, min: 0 }
  count: { type: int }
  old: { type: string }
  temperature: { type: quantity, unit: Cel }
"""


@pytest.fixture
def lab(tmp_path: Path) -> Path:
    root = tmp_path / "plugins" / "lab"
    (root / "types").mkdir(parents=True)
    (root / "plugin.yaml").write_text(LAB)
    (root / "types" / "sample.yaml").write_text(SAMPLE)
    return root


def write_sample(lab: Path, *changes: tuple[str, str]) -> None:
    text = SAMPLE
    for old, new in changes:
        assert old in text
        text = text.replace(old, new)
    (lab / "types" / "sample.yaml").write_text(text)


def test_changes_keep_history(lab: Path, database: str) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    orm(
        out,
        database,
        """
        from iki_plugin_lab.models import Sample, Sample_History
        sample = Sample.objects.create(
            type="lab.Sample", hrid="SMP-000001", owner_project=project,
            name="S1", mass=1.5, count=3, old="x", temperature=20.0,
        )
        Sample_History.objects.create(
            entity_id=sample.pk, revision=1, name="S1", mass=1.5, count=3, old="x",
            temperature=20.0,
        )
        result = None
        """,
    )

    write_sample(
        lab,
        ("  name: {", "  title: {"),
        ("unit: g,", "unit: mg,"),
        ("count: { type: int }", "count: { type: float }"),
        ("old: { type: string }", "old: { type: string, removed: true }"),
        ("unit: Cel }", "unit: K }"),
    )
    # One question about the rename: the history table follows the answer.
    out_text = made("lab", "--name", "changes", cwd=lab.parent, answers="y\n")
    assert out_text.count("renamed to") == 1
    migration = (lab / "migrations" / "0002_changes.py").read_text()
    assert "iki_server.plugins.operations.ConvertUnit(" in migration
    out = build(lab.parent, "lab")
    migrate(out, database)
    row = query(database, "SELECT title, mass, count, temperature FROM lab_sample")
    assert row == [("S1", 1500.0, 3.0, 293.15)]
    history = query(
        database, "SELECT title, mass, count, old, temperature FROM lab_sample__history"
    )
    assert history == [("S1", 1500.0, 3.0, "x", 293.15)]
    columns = {
        r[0]
        for r in query(
            database,
            "SELECT attname FROM pg_attribute "
            "WHERE attrelid = 'lab_sample'::regclass AND attnum > 0",
        )
    }
    assert "old" not in columns

    migrate(out, database, "lab", "0001")
    assert query(database, "SELECT name, mass, count, temperature FROM lab_sample") == [
        ("S1", 1.5, 3, pytest.approx(20.0))
    ]


def test_migrations_are_named_after_what_changed(lab: Path) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    write_sample(lab, ("  name: {", "  title: {"))
    made("lab", cwd=lab.parent, answers="y\n")
    assert sorted(p.name for p in (lab / "migrations").iterdir()) == [
        "0001_initial.py",
        "0002_rename_name_sample_title_and_more.py",
    ]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            ("  old: { type: string }\n", ""),
            "error: `old` is gone from lab.Sample. Iki keeps every field's history, so a field "
            "can't be deleted.",
        ),
        (
            ("old: { type: string }", "old: { type: int }"),
            "error: `old` of lab.Sample would change from text to a whole number, which its "
            "history can't be converted to.",
        ),
        (
            ("unit: g,", "unit: m,"),
            "error: `mass` of lab.Sample can't change from g to m: they measure different things.",
        ),
    ],
)
def test_changes_that_would_lose_history_are_refused(
    lab: Path, change: tuple[str, str], message: str
) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    write_sample(lab, change)
    result = iki("plugin", "makemigrations", "lab", "--no-input", cwd=lab.parent)
    assert result.returncode == 1, result.stdout
    assert result.stderr.startswith(message), result.stderr
    assert sorted(p.name for p in (lab / "migrations").iterdir()) == ["0001_initial.py"]


def test_types_cant_be_deleted(lab: Path) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    (lab / "types" / "sample.yaml").unlink()
    result = iki("plugin", "makemigrations", "lab", "--no-input", cwd=lab.parent)
    assert result.returncode == 1
    assert result.stderr.startswith(
        "error: Lab.Sample is gone. Iki keeps every entity's history, so a plugin can't delete "
        "a type or an extension: put its manifest back."
    ), result.stderr


def test_renaming_a_type_renames_its_entities(lab: Path, database: str) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    orm(
        out,
        database,
        """
        from iki_plugin_lab.models import Sample
        Sample.objects.create(type="lab.Sample", hrid="SMP-000001", owner_project=project)
        result = None
        """,
    )
    write_sample(lab, ("name: Sample", "name: Specimen"))
    made("lab", "--name", "specimen", cwd=lab.parent, answers="y\n")
    migration = (lab / "migrations" / "0002_specimen.py").read_text()
    assert "RenameEntityType(" in migration
    out = build(lab.parent, "lab")
    migrate(out, database)
    assert query(database, "SELECT type FROM iki_entity") == [("lab.Specimen",)]
    assert query(database, "SELECT count(*) FROM lab_specimen__history") == [(0,)]
    migrate(out, database, "lab", "0001")
    assert query(database, "SELECT type FROM iki_entity") == [("lab.Sample",)]
