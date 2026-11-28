"""Plugins compiled to Django apps, with their migrations made and run on PostgreSQL (ADR 0033).

Each test copies plugins into a temporary directory, makes their migrations
with ``iki plugin makemigrations`` (as an author would), builds them with
``iki plugin build`` (as a deployment would), and migrates a database of its
own, in subprocesses, since the apps can't be installed in this process.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

from iki_spec.builtin import core_plugin
from iki_spec.validate import load_plugin

TESTS = Path(__file__).parent
PLUGINS = TESTS.parents[1] / "spec" / "tests" / "plugins"


@contextmanager
def private_database(_postgres_url: str) -> Iterator[str]:
    """A database of the test's own, which plugin apps migrate."""
    name = f"iki_plugins_{secrets.token_hex(4)}"
    with psycopg.connect(_postgres_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parts = urlsplit(_postgres_url)
    try:
        yield urlunsplit(parts._replace(path=f"/{name}"))
    finally:
        with psycopg.connect(_postgres_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def database(_postgres_url: str) -> Iterator[str]:
    with private_database(_postgres_url) as url:
        yield url


def iki(*args: str | Path, cwd: Path, answers: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - this interpreter, the iki CLI
        [sys.executable, "-m", "iki_cli", *map(str, args)],
        cwd=cwd,
        input=answers,
        capture_output=True,
        text=True,
        check=False,
        # The suite's own server is in $IKI_TEST_DATABASE_URL, which `iki plugin test` reads:
        # its tests give --database when they mean one.
        env={
            **{k: v for k, v in os.environ.items() if k != "IKI_TEST_DATABASE_URL"},
            "NO_COLOR": "1",
        },
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
            "IKI_ALLOW_LOCAL_PLUGINS": "true",
            # These tests install their own builds, not the session's fixture plugins.
            "IKI_TEST_PLUGINS": "0",
        },
    )


def migrate(build: Path, database: str, *args: str) -> str:
    result = django(build, database, "-m", "django", "migrate", "--no-input", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


WORLD = """
from iki_server.org.models import Project, Team
from iki_server.files.models import File
from scan_fixtures import clean_blob
team = Team.objects.create(name="Biologics")
project = Project.objects.create(team=team, name="Antibodies")
blob = clean_blob(sha256="a" * 64, size=10,
                           storage_uri="s3://iki/a")
pdb = File.objects.create(blob=blob, name="a.pdb", media_type="chemical/x-pdb", project=project)


# A registry entry (ADR 0115) and the project's record of it.
def register(record_model, entry_model, entry, **values):
    made = entry_model.objects.create(registry=project.registry, **entry)
    return made, record_model.objects.create(
        owner_project=project, entry=made, hrid=made.hrid, **values
    )
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
    """The rows ``statement`` gives, or none for one that gives none (an insert)."""
    with psycopg.connect(database) as connection:
        cursor = connection.execute(statement, params)
        return cursor.fetchall() if cursor.description is not None else []


@pytest.fixture
def plugins(tmp_path: Path) -> Path:
    shutil.copytree(PLUGINS, tmp_path / "plugins")
    return tmp_path / "plugins"


def build(plugins: Path, *names: str) -> Path:
    output = plugins.parent / "build"
    result = iki("plugin", "build", *names, "--output", output, cwd=plugins)
    assert result.returncode == 0, result.stdout + result.stderr
    return output


def test_lab_defined_material_and_aliquot_fields(plugins: Path, database: str) -> None:
    """A lab's own Lot, Sample and assay result use its declared physical parent."""
    types = plugins / "protein" / "types"
    request = types / "assay_request.yaml"
    request.write_text(
        request.read_text()
        .replace(
            "    sends: [samples, protein, tubes]",
            "    sends: [samples, protein, tubes, specimens]\n    materials: [specimens]",
        )
        .replace(
            "fields:\n",
            "fields:\n  specimens: { type: list, items: { type: ref, to: Lot }, max_items: 20 }\n",
            1,
        )
    )
    sample = types / "sample.yaml"
    sample.write_text(
        sample.read_text().replace(
            "lot: { type: ref, to: Lot }", "lot: { type: ref, to: Lot, immutable: true }"
        )
    )
    result = types / "assay_result.yaml"
    result.write_text(
        result.read_text()
        .replace("  result:\n", "  result:\n    material: sample\n")
        .replace(
            "sample: { type: ref, to: Sample }",
            "sample: { type: ref, to: Sample, required: true, immutable: true }",
        )
    )
    made("dna", "--no-input", cwd=plugins)
    made("protein", "--with", "dna", "--no-input", cwd=plugins)
    app = build(plugins, "dna", "protein")
    migrate(app, database)
    sync(app, database)
    orm(
        app,
        database,
        """
        import pytest
        from org_world import build_world
        from iki_server.entities import services, requests
        from iki_server.entities.models import Entity
        from iki_server.org.models import Project, ProjectMembership, ProjectRole
        world = build_world()
        admin = world.users["admin"]
        lab = world.project
        facility = Project.objects.create(team=world.team, name="Assay facility")
        ProjectMembership.objects.create(project=facility, user=admin, role=ProjectRole.ADMIN)
        def make(kind, project, **values):
            row = services.create(admin, "acme/protein." + kind, project, values).entity
            return Entity.objects.get(pk=row.pk)
        root, other = [make("Lot", lab, label=n) for n in ("Requested", "Other")]
        children = [make("Sample", lab, label=n, lot=str(lot.pk))
                    for lot, n in ((root, "First"), (other, "Second"))]
        request = make("AssayRequest", lab, title="One lot", specimens=[str(root.pk)])
        requests.set_intake(facility, types=[request.type], from_registry=False,
                            sources=[lab], by=admin)
        requests.submit(admin, request, facility, expected_revision=request.revision)
        for child in children:
            services.link(admin, child, facility)
        make("AssayResult", facility, request=str(request.pk), sample=str(children[0].pk))
        with pytest.raises(services.Invalid) as refused:
            make("AssayResult", facility, request=str(request.pk), sample=str(children[1].pk))
        assert "sample" in refused.value.errors

        # A file import resolves its keyed creates before checking the staged rows.
        from uuid import uuid4
        from django.db import transaction
        from iki_server.entities.values import shape
        from iki_server.runs import imports, results
        from iki_server.runs.models import ImportLine, RunImport
        kind = "acme/protein.AssayResult"
        table = shape(kind).part_of("sample").model._meta.db_table
        staged = RunImport(id=uuid4(), round=1)
        ImportLine.objects.create(bulk_import_id=staged.pk, round=1, line=1, type=kind,
            entity_id=uuid4(), changed=["sample", "request"], parts={table: {
                "request": str(request.pk), "sample": str(imports.stand_in(staged, "portion"))}})
        with transaction.atomic():
            imports._replace_stand_ins(staged, {"portion": children[0].pk})
            imports._check_request_results(staged, facility, [kind])
        request.refresh_from_db()
        services.update(admin, request, {"specimens": []}, expected_revision=request.revision)
        with transaction.atomic(), pytest.raises(results.InvalidOutput):
            imports._check_request_results(staged, facility, [kind])
        result = {}
    """,
    )


# --- The first-party style plugins -------------------------------------------------------------


@pytest.fixture(scope="module")
def _migrated_plugins(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The fixture plugins with their first migrations, made once for the module."""
    plugins = tmp_path_factory.mktemp("migrated") / "plugins"
    shutil.copytree(PLUGINS, plugins)
    made("dna", "--no-input", cwd=plugins)
    made("protein", "--with", "dna", "--no-input", cwd=plugins)
    made("structure_prediction", "--with", "protein", "--with", "dna", "--no-input", cwd=plugins)
    return plugins


@pytest.fixture
def migrated(tmp_path: Path, _migrated_plugins: Path) -> Path:
    """A copy of the fixture plugins with their migrations, for one test."""
    shutil.copytree(_migrated_plugins, tmp_path / "plugins")
    return tmp_path / "plugins"


def test_makemigrations_writes_to_the_plugin(plugins: Path) -> None:
    out = made("dna", "--no-input", cwd=plugins)
    assert "Migrations for 'acme__dna':\n  dna/migrations/0001_initial.py\n" in out
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
        "error: acme/dna has no migrations. Run `iki plugin makemigrations` in dna first.\n"
    )


def test_check_fails_when_manifests_change(plugins: Path) -> None:
    made("dna", "--no-input", cwd=plugins)
    types = plugins / "dna" / "types" / "sequence.yaml"
    types.write_text(
        types.read_text().replace(
            "extends: iki/core.Sequence\n",
            "extends: iki/core.Sequence\nfields:\n  note: { type: text }\n",
        )
    )
    result = iki("plugin", "makemigrations", "dna", "--check", cwd=plugins)
    assert result.returncode == 1
    assert "+ Add field note to sequence" in result.stdout
    assert not (
        plugins / "dna" / "migrations" / "0002_sequence_note_sequence_history_note.py"
    ).exists()


def test_fixture_plugins_migrate_forwards_and_back(migrated: Path, database: str) -> None:
    out = build(migrated, "dna", "protein", "structure_prediction")
    migrate(out, database)
    tables = {
        row[0]
        for row in query(
            database,
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND "
            "tablename LIKE 'acme\\_\\_%%'",
        )
    }
    assert tables == {
        "acme__dna_sequence",
        "acme__dna_sequence__history",
        "acme__dna_dnasequence",
        "acme__dna_dnasequence__history",
        "acme__dna_dnasequence__entry",
        "acme__dna_dnasequence__entry__history",
        "acme__dna_plasmid",
        "acme__dna_plasmid__history",
        "acme__dna_plasmid__entry",
        "acme__dna_plasmid__entry__history",
        "acme__dna_vector",
        "acme__dna_vector__history",
        "acme__dna_vector__entry",
        "acme__dna_vector__entry__history",
        "acme__protein_protein",
        "acme__protein_protein__history",
        "acme__protein_protein__entry",
        "acme__protein_protein__entry__history",
        "acme__protein_compound",
        "acme__protein_compound__history",
        "acme__protein_compound__entry",
        "acme__protein_compound__entry__history",
        "acme__prediction__acme__protein_protein",
        "acme__prediction__acme__protein_protein__history",
        "acme__prediction__acme__protein_protein__entry",
        "acme__prediction__acme__protein_protein__entry__history",
        "acme__protein_proteinstructure",
        "acme__protein_proteinstructure__history",
        "acme__protein_proteinstructure__chains",
        "acme__protein_sample",
        "acme__protein_sample__history",
        "acme__protein_sample__helpers",
        # A list of rows (ADR 0137): its rows' table.
        "acme__protein_sample__additions",
        "acme__protein_bispecific",
        "acme__protein_bispecific__history",
        "acme__protein_bispecific__entry",
        "acme__protein_bispecific__entry__history",
        # Its components are iki/core.Complex's, in the core's table (ADR 0161); its arms,
        # a fact of its own entry, are its own (ADR 0165).
        "acme__protein_bispecific__entry__arms",
        "acme__protein_multimer",
        "acme__protein_multimer__history",
        "acme__protein_multimer__entry",
        "acme__protein_multimer__entry__history",
        "acme__protein_binding",
        "acme__protein_binding__history",
        # Two types under one identity (ADR 0165), as Bispecific and Multimer are, under an
        # abstract one of the lab's own whose tables both share.
        "acme__protein_reagent",
        "acme__protein_reagent__history",
        "acme__protein_reagent__entry",
        "acme__protein_reagent__entry__history",
        "acme__protein_primaryantibody",
        "acme__protein_primaryantibody__history",
        "acme__protein_primaryantibody__entry",
        "acme__protein_primaryantibody__entry__history",
        "acme__protein_secondaryantibody",
        "acme__protein_secondaryantibody__history",
        "acme__protein_secondaryantibody__entry",
        "acme__protein_secondaryantibody__entry__history",
        "acme__protein_lot",
        "acme__protein_lot__history",
        "acme__protein_lot__inputs",
        "acme__protein_batch",
        "acme__protein_batch__history",
        "acme__protein_batch__proteins",
        "acme__protein_assayrequest",
        "acme__protein_assayrequest__history",
        "acme__protein_assayrequest__comparison_samples",
        "acme__protein_assayrequest__sample_notes",
        "acme__protein_assayrequest__samples",
        "acme__protein_assayrequest__tubes",
        "acme__protein_rankedassayrequest",
        "acme__protein_rankedassayrequest__history",
        "acme__protein_clientrank",
        "acme__protein_clientrank__history",
        # Where material is kept, and the tubes it's kept in (ADR 0180).
        "acme__protein_freezer",
        "acme__protein_freezer__history",
        "acme__protein_rack",
        "acme__protein_rack__history",
        "acme__protein_box",
        "acme__protein_box__history",
        "acme__protein_tube",
        "acme__protein_tube__history",
        "acme__protein_retaintube",
        "acme__protein_retaintube__history",
        "acme__protein_assayresult",
        "acme__protein_assayresult__history",
        # Lab lists (ADR 0270): their options are registry entries, with no records' fields.
        "acme__protein_methodcategory",
        "acme__protein_methodcategory__history",
        "acme__protein_methodcategory__entry",
        "acme__protein_methodcategory__entry__history",
        "acme__protein_purificationmethod",
        "acme__protein_purificationmethod__history",
        "acme__protein_purificationmethod__entry",
        "acme__protein_purificationmethod__entry__history",
        "acme__protein_prep",
        "acme__protein_prep__history",
        "acme__protein_prep__polishing",
        "acme__protein_prep__steps",
        "acme__prediction__acme__protein_proteinstructure",
        "acme__prediction__acme__protein_proteinstructure__history",
    }
    for app in ("acme__prediction", "acme__protein", "acme__dna"):
        migrate(out, database, app, "zero")
    left = query(
        database,
        "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND "
        "tablename LIKE 'acme\\_\\_%%'",
    )
    assert left == [(0,)]


def test_generated_models_store_entities(migrated: Path, database: str) -> None:
    out = build(migrated, "dna", "protein", "structure_prediction")
    migrate(out, database)
    result = orm(
        out,
        database,
        """
        from iki_plugin_acme__dna.models import DNASequence, DNASequence_Entry
        from iki_plugin_acme__protein.models import (
            Protein,
            Protein_Entry,
            ProteinStructure,
            ProteinStructure_chains,
        )
        from iki_plugin_acme__prediction.models import (
            Ext_acme_protein_Protein_Entry,
            Ext_acme_protein_ProteinStructure,
        )
        from iki_server.entities.models import Entity

        dna_entry, dna = register(
            DNASequence,
            DNASequence_Entry,
            {"hrid": "DNA-000001", "bases": "ACGTN", "length": 2686},
            name="pUC19",
        )
        # A reference to a type with an identity holds its entry.
        protein_entry, protein = register(
            Protein,
            Protein_Entry,
            {"hrid": "PRT-000001", "sequence": "MKV"},
            encoded_by=dna_entry, source=dna_entry, molecular_weight=12000.5,
            uniprot_ids=["P69905"],
        )
        Ext_acme_protein_Protein_Entry.objects.create(entity=protein_entry, disorder=0.25)
        structure = ProteinStructure.objects.create(
            type="acme/protein.ProteinStructure", hrid="PST-001", owner_project=project,
            structure=pdb,
        )
        ProteinStructure_chains.objects.create(entity=structure, position=0, item=protein_entry)
        Ext_acme_protein_ProteinStructure.objects.create(entity=structure, plddt_mean=91.5)
        dna.refresh_from_db()
        result = {
            "topology": dna.topology,
            "length": DNASequence_Entry.objects.get().length,
            "entities": sorted(Entity.objects.values_list("type", flat=True)),
            "entries": sorted(
                Entity.objects.filter(registry__isnull=False).values_list("hrid", flat=True)
            ),
            "name": Protein.objects.get(pk=protein.pk).sequence_ptr.name,
            "chain": ProteinStructure_chains.objects.get().item.sequence,
            "encoded by": Protein.objects.get().encoded_by.bases,
            "disorder": Protein_Entry.objects.get().acme_prediction_Entry.disorder,
            "plddt": structure.acme_prediction_Ext.plddt_mean,
        }
        """,
    )
    assert result == {
        "topology": "linear",
        "length": 2686.0,
        "entities": [
            "acme/dna.DNASequence",
            "acme/dna.DNASequence",
            "acme/protein.Protein",
            "acme/protein.Protein",
            "acme/protein.ProteinStructure",
        ],
        "entries": ["DNA-000001", "PRT-000001"],
        "name": None,
        "chain": "MKV",
        "encoded by": "ACGTN",
        "disorder": 0.25,
        "plddt": 91.5,
    }


def sync(build: Path, database: str, *args: str) -> str:
    result = django(build, database, "-m", "django", "sync_plugins", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_a_deployment_built_from_its_lockfile(migrated: Path, database: str) -> None:
    deployment = migrated.parent / "deployments" / "dev"
    deployment.mkdir(parents=True)
    (deployment / "dna.yaml").write_text("ncbi_email: lab@example.org\n")
    (deployment / "plugins.lock.yaml").write_text(
        "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n"
        "  - {name: acme/protein, local: ../../plugins/protein, state: active}\n"
        "  - {name: acme/dna, local: ../../plugins/dna, state: active, settings: dna.yaml}\n"
    )
    out = migrated.parent / "image-plugins"
    result = iki("deploy", "build", deployment, "-o", out, "--allow-local", cwd=migrated)
    assert result.returncode == 0, result.stdout + result.stderr
    migrate(out, database)
    sync(out, database)
    lock = json.loads((out / "iki_installed_plugins" / "lock.json").read_text())
    assert query(database, "SELECT revision FROM iki_plugin_baseline") == [
        (lock["lockfile_sha256"],)
    ]
    result = orm(
        out,
        database,
        """
        from iki_server.entities import registry
        from iki_plugin_acme__dna.models import DNASequence_Entry

        made = DNASequence_Entry.objects.create(registry=project.registry, bases="ACGT")
        result = {
            "settings": registry.settings("acme/dna"),
            "from": [
                (p["name"], p.get("local"), p["version"]) for p in registry.installed()["plugins"]
            ],
            "hrid": made.hrid,
        }
        """,
    )
    assert result == {
        "settings": {"max_sequence_length": 10_000_000, "ncbi_email": "lab@example.org"},
        "from": [
            ["acme/protein", "../../plugins/protein", "3.0.0"],
            ["acme/dna", "../../plugins/dna", "1.2.0"],
        ],
        "hrid": "DNA-000001",
    }


def test_sync_makes_hrids_identities_views_and_comments(migrated: Path, database: str) -> None:
    names = ("dna", "protein", "structure_prediction")
    out = build(migrated, *names)
    migrate(out, database)
    report = sync(out, database)

    # Compare the deployed report to the source manifests, independently of the installed registry.
    directories = [migrated / name for name in names]
    declared = [core_plugin()]
    for directory in directories:
        loaded = load_plugin(directory, dependencies=[d for d in directories if d != directory])
        assert loaded.plugin is not None and not loaded.errors, loaded.problems
        declared.append(loaded.plugin)
    prefixes = {
        entity_type.hrid_prefix.lower()
        for plugin in declared
        for entity_type in plugin.types.values()
        if not entity_type.abstract and entity_type.hrid_prefix is not None
    }
    expected = [f"  HRID sequence iki_hrid_{prefix}" for prefix in sorted(prefixes)]
    assert [line for line in report.splitlines() if line.startswith("  HRID sequence ")] == expected
    # Idempotent: again, it seeds no list's starting options (ADR 0270), as they're there.
    seeded = [line for line in report.splitlines() if line.startswith("  starting options of ")]
    assert seeded == [
        "  starting options of acme/protein.MethodCategory in Main: chromatography",
        "  starting options of acme/protein.PurificationMethod in Main: protein_a, sec, dialysis",
    ]
    again = sync(out, database)
    assert again.splitlines() == [line for line in report.splitlines() if line not in seeded]
    result = orm(
        out,
        database,
        """
        from iki_plugin_acme__dna.models import DNASequence, DNASequence_Entry, Sequence
        from iki_plugin_acme__protein.models import (
            Protein,
            Protein_Entry,
            ProteinStructure,
            ProteinStructure_chains,
        )
        from iki_plugin_acme__prediction.models import Ext_acme_protein_ProteinStructure
        from iki_spec.identity import identity_hash

        entry, first = register(DNASequence, DNASequence_Entry, {"bases": "ACGT"}, name="pUC19")
        _, second = register(DNASequence, DNASequence_Entry, {"bases": "ACGTT"})
        protein_entry, protein = register(
            Protein, Protein_Entry, {"sequence": "MKV"}, encoded_by=entry
        )
        structure = ProteinStructure.objects.create(owner_project=project, structure=pdb)
        ProteinStructure_chains.objects.create(entity=structure, position=0, item=protein_entry)
        Ext_acme_protein_ProteinStructure.objects.create(entity=structure, plddt_mean=91.5)
        try:
            Sequence.objects.create(owner_project=project, name="abstract")
            abstract = "saved"
        except ValueError as error:
            abstract = str(error)
        result = {
            "hrids": [first.hrid, second.hrid, protein.hrid, structure.hrid],
            "types": [first.type, protein.type],
            "identity": entry.identity_hash == identity_hash(
                "acme/dna.DNASequence", {"bases": ("string", "ACGT")}
            ),
            "record": first.identity_hash,
            "no identity": structure.identity_hash,
            "abstract": abstract,
            "protein": str(protein_entry.pk),
        }
        """,
    )
    assert result["hrids"] == ["DNA-000001", "DNA-000002", "PRT-000001", "PST-000001"]
    assert result["types"] == ["acme/dna.DNASequence", "acme/protein.Protein"]
    assert result["identity"] is True
    assert result["record"] is None and result["no identity"] is None
    assert result["abstract"] == "acme/dna.Sequence is abstract: create one of its subtypes."

    # Views of each project's records join a type's tables, its parents' and extensions',
    # and its registry entry's.
    assert query(
        database,
        "SELECT hrid, name, bases, topology FROM iki_views.acme__dna_dnasequence ORDER BY hrid",
    ) == [("DNA-000001", "pUC19", "ACGT", "linear"), ("DNA-000002", None, "ACGTT", "linear")]
    assert query(database, "SELECT hrid FROM iki_views.acme__dna_sequence ORDER BY hrid") == [
        ("DNA-000001",),
        ("DNA-000002",),
        ("PRT-000001",),
    ]
    ((hrid, chains, plddt),) = query(
        database, "SELECT hrid, chains, plddt_mean FROM iki_views.acme__protein_proteinstructure"
    )
    assert (hrid, [str(c) for c in chains], plddt) == ("PST-000001", [result["protein"]], 91.5)
    assert query(
        database,
        "SELECT sequence, encoded_by_id IS NOT NULL, entry_id IS NOT NULL "
        "FROM iki_views.acme__protein_protein",
    ) == [("MKV", True, True)]

    def comment(table: str, column: str) -> str:
        ((text,),) = query(
            database,
            "SELECT col_description(%s::regclass, attnum) FROM pg_attribute "
            "WHERE attrelid = %s::regclass AND attname = %s",
            table,
            table,
            column,
        )
        return str(text)

    assert comment("acme__protein_protein", "molecular_weight") == "Molecular weight. In u (UCUM)."
    assert comment("acme__dna_dnasequence", "gc_content") == "Gc content. In % (UCUM)."
    assert (
        comment("acme__protein_protein", "encoded_by_id")
        == "Encoded by. Refers to a acme/dna.DNASequence."
    )
    assert comment("iki_views.acme__protein_protein", "molecular_weight") == (
        "Molecular weight. In u (UCUM)."
    )
    assert query(database, "SELECT obj_description('acme__dna_dnasequence'::regclass)") == [
        ("DNA sequence (acme/dna.DNASequence), schema version 2.",)
    ]
    assert comment("acme__dna_dnasequence__entry", "length") == "Length. In {bp} (UCUM)."
    assert comment("acme__dna_dnasequence__entry__history", "bases") == "Bases."
    assert comment("iki_views.acme__dna_dnasequence", "bases") == "Bases."


def test_history_is_append_only_for_every_role(migrated: Path, database: str) -> None:
    out = build(migrated, "dna")
    migrate(out, database)
    with psycopg.connect(database, autocommit=True) as connection:
        connection.execute(
            "INSERT INTO acme__dna_sequence__history (entity_id, revision) "
            "VALUES (gen_random_uuid(), 1)"
        )
        for statement in (
            "UPDATE acme__dna_sequence__history SET revision = 2",
            "DELETE FROM acme__dna_sequence__history",
            "TRUNCATE acme__dna_sequence__history",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
                connection.execute(statement)


def test_an_analyst_role_reads_only_the_views(migrated: Path, database: str) -> None:
    out = build(migrated, "dna")
    migrate(out, database)
    role = f"analyst_{secrets.token_hex(3)}"
    with psycopg.connect(database, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
    try:
        assert sync(out, database, "--analyst-role", role).splitlines()[-2] == (
            f"  {role} can read the views"
        )
        with psycopg.connect(database) as connection:
            connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            assert connection.execute(
                "SELECT count(*) FROM iki_views.acme__dna_dnasequence"
            ).fetchall()
            # Core types too, and the core tables plugins' types extend (iki/core.Sequence).
            assert connection.execute(
                "SELECT count(*) FROM iki_views.iki__core_alignment"
            ).fetchall()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM iki_audit_event")
    finally:
        with psycopg.connect(database, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
    result = django(out, database, "-m", "django", "sync_plugins", "--analyst-role", "nobody")
    assert result.returncode == 1
    assert "Postgres has no role 'nobody'. Create it first." in result.stderr


#: Writes the database refuses, and the CHECK constraint that refuses each.
VIOLATIONS = {
    "bases": (
        "DNASequence_Entry",
        {"bases": "ACGU"},
        "acme__dna_dnasequence__entry__bases__format",
    ),
    "empty": (
        "DNASequence_Entry",
        {"bases": ""},
        "acme__dna_dnasequence__entry__bases__not_empty",
    ),
    "two lines": ("DNASequence", {"name": "pUC\n19"}, "iki__core_sequence__name__one_line"),
    "topology": ("DNASequence", {"topology": "knotted"}, "acme__dna_dnasequence__topology__values"),
    "gc": ("DNASequence", {"gc_content": 101.0}, "acme__dna_dnasequence__gc_content__max"),
    "nan": ("DNASequence", {"gc_content": "nan"}, "acme__dna_dnasequence__gc_content__finite"),
    "inf": (
        "DNASequence_Entry",
        {"length": "inf"},
        "acme__dna_dnasequence__entry__length__finite",
    ),
    "uniprot": (
        "Protein",
        {"uniprot_ids": ["p69905"]},
        "acme__protein_protein__uniprot_ids__item_pattern",
    ),
    "null item": (
        "Protein",
        {"uniprot_ids": ["P69905", None]},
        "acme__protein_protein__uniprot_ids__plain_list",
    ),
    "empty item": (
        "Protein",
        {"uniprot_ids": [""]},
        "acme__protein_protein__uniprot_ids__item_length",
    ),
}


def test_the_database_enforces_field_rules(migrated: Path, database: str) -> None:
    out = build(migrated, "dna", "protein")
    migrate(out, database)
    cases = {name: [model, values] for name, (model, values, _) in VIOLATIONS.items()}
    result = orm(
        out,
        database,
        """
        import itertools
        from django.db import IntegrityError, DataError, transaction
        from iki_plugin_acme__dna.models import DNASequence, DNASequence_Entry
        from iki_plugin_acme__protein.models import Protein
        models = {
            "DNASequence": DNASequence,
            "DNASequence_Entry": DNASequence_Entry,
            "Protein": Protein,
        }
        base = {
            "DNASequence": {"type": "acme/dna.DNASequence", "owner_project": project},
            "DNASequence_Entry": {"bases": "ACGT", "registry": project.registry},
            "Protein": {"type": "acme/protein.Protein", "owner_project": project},
        }
        numbers = itertools.count(1)
        result = {}
        for name, (model, values) in CASES.items():
            values = {k: float(v) if v in ("nan", "inf") else v for k, v in values.items()}
            try:
                with transaction.atomic():
                    models[model].objects.create(
                        hrid=f"X{next(numbers):03}-001", **{**base[model], **values}
                    )
                result[name] = "saved"
            except (IntegrityError, DataError) as error:
                result[name] = getattr(error.__cause__.diag, "constraint_name", None) or str(error)
        # Arrays the ORM never writes, but SQL could.
        from django.db import connection
        protein = Protein.objects.create(
            owner_project=project, hrid="Y001-001", type="acme/protein.Protein"
        )
        for name, value in {"nested": "{{P69905},{P69906}}", "from 0": "[0:0]={P69905}"}.items():
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE acme__protein_protein SET uniprot_ids = %s WHERE entity_id = %s",
                        [value, protein.pk],
                    )
                result[name] = "saved"
            except IntegrityError as error:
                result[name] = error.__cause__.diag.constraint_name
        """.replace("CASES", repr(cases)),
    )
    assert result == {
        **{name: constraint for name, (_, _, constraint) in VIOLATIONS.items()},
        "nested": "acme__protein_protein__uniprot_ids__plain_list",
        "from 0": "acme__protein_protein__uniprot_ids__plain_list",
    }


# --- Changing a plugin ---------------------------------------------------------------------------

LAB = """\
apiVersion: iki/v1
kind: Plugin
name: acme/lab
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


def test_reversing_an_added_field_only_drops_its_column_guards(lab: Path, database: str) -> None:
    unique_field = "  name: { type: string, max_length: 100, indexed: true, unique: true }"
    initial = SAMPLE.replace("  name: { type: string, max_length: 100 }", unique_field)
    manifest = lab / "types" / "sample.yaml"
    manifest.write_text(initial)
    made("lab", "--no-input", cwd=lab.parent)
    manifest.write_text(
        initial
        + "  alias: { type: string, max_length: 100, indexed: true, unique: true }\n"
        + "rules:\n  one_alias_per_name:\n    unique: [alias, name]\n"
        + "    message: This name and alias are already used.\n"
    )
    made("lab", "--no-input", "--name", "alias", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    sync(out, database)

    def guards() -> set[str]:
        return {
            name
            for (name,) in query(
                database,
                "SELECT tgname FROM pg_trigger WHERE tgrelid = 'acme__lab_sample'::regclass "
                "AND (starts_with(tgname, 'iki_unique__') OR starts_with(tgname, 'iki_rule__'))",
            )
        }

    expected = {"iki_unique__name", "iki_unique__alias", "iki_rule__one_alias_per_name"}
    assert guards() == expected
    # A local trigger belongs to the deployment. Dropping the column must still fail and
    # leave Iki's guards intact until the deployment removes that dependency.
    query(
        database,
        "CREATE TRIGGER local_alias_guard BEFORE UPDATE OF alias ON acme__lab_sample "
        "FOR EACH ROW EXECUTE FUNCTION iki_refuse_history_change()",
    )
    refused = django(out, database, "-m", "django", "migrate", "acme__lab", "0001")
    assert refused.returncode != 0 and "local_alias_guard" in refused.stderr
    assert guards() == expected
    query(database, "DROP TRIGGER local_alias_guard ON acme__lab_sample")
    migrate(out, database, "acme__lab", "0001")
    assert "alias" not in columns(database, "acme__lab_sample")
    assert guards() == {"iki_unique__name"}
    migrate(out, database)
    sync(out, database)
    assert guards() == expected


def test_changes_keep_history(lab: Path, database: str) -> None:
    readings = (
        "  mass: { type: quantity, unit: g, min: 0 }",
        "  mass: { type: quantity, unit: g, min: 0 }\n"
        "  readings: { type: list, items: { type: quantity, unit: g, min: 0 } }",
    )
    write_sample(lab, readings)
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    orm(
        out,
        database,
        """
        from iki_plugin_acme__lab.models import Sample, Sample_History
        sample = Sample.objects.create(
            type="acme/lab.Sample", hrid="SMP-000001", owner_project=project,
            name="S1", mass=1.5, count=3, old="x", temperature=20.0, readings=[1.5, 0.25],
        )
        Sample_History.objects.create(
            entity_id=sample.pk, revision=1, name="S1", mass=1.5, count=3, old="x",
            temperature=20.0, readings=[1.5, 0.25],
        )
        result = None
        """,
    )

    write_sample(
        lab,
        readings,
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
    row = query(database, "SELECT title, mass, count, temperature FROM acme__lab_sample")
    assert row == [("S1", 1500.0, 3.0, 293.15)]
    history = query(
        database, "SELECT title, mass, count, old, temperature FROM acme__lab_sample__history"
    )
    assert history == [("S1", 1500.0, 3.0, "x", 293.15)]
    assert query(database, "SELECT readings FROM acme__lab_sample__history") == [([1500.0, 250.0],)]
    columns = {
        r[0]
        for r in query(
            database,
            "SELECT attname FROM pg_attribute "
            "WHERE attrelid = 'acme__lab_sample'::regclass AND attnum > 0",
        )
    }
    assert "old" not in columns

    migrate(out, database, "acme__lab", "0001")
    assert query(database, "SELECT readings FROM acme__lab_sample__history") == [([1.5, 0.25],)]
    assert query(database, "SELECT name, mass, count, temperature FROM acme__lab_sample") == [
        ("S1", 1.5, 3, pytest.approx(20.0))
    ]


def test_a_type_whose_only_people_are_in_rows_loads(lab: Path, database: str) -> None:
    """A person in a row (ADR 0137) is a column naming a user, as a type's own field is."""
    write_sample(
        lab,
        (
            "  temperature: { type: quantity, unit: Cel }\n",
            "  temperature: { type: quantity, unit: Cel }\n"
            "  weighings:\n"
            "    type: list\n"
            "    items:\n"
            "      type: row\n"
            "      fields:\n"
            "        mass: { type: quantity, unit: mg, min: 0, required: true }\n"
            "        weighed_by: { type: person, required: true }\n",
        ),
    )
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    result = orm(
        out,
        database,
        """
        from iki_server.accounts.models import User
        from iki_plugin_acme__lab.models import Sample, Sample_weighings
        analyst = User.objects.create_user("analyst@example.com", "Bench Analyst")
        sample = Sample.objects.create(
            type="acme/lab.Sample", hrid="SMP-000001", owner_project=project, name="S1",
            mass=1.5, count=3, old="x", temperature=20.0,
        )
        Sample_weighings.objects.create(entity=sample, position=0, mass=12.5, weighed_by=analyst)
        result = Sample_weighings.objects.get().weighed_by.email
        """,
    )
    assert result == "analyst@example.com"


def test_a_removed_fields_values_and_changes_are_read_back_at_earlier_revisions(
    lab: Path, database: str
) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    sync(out, database)
    orm(
        out,
        database,
        """
        from iki_server.accounts.models import User
        from iki_server.entities import services
        from iki_server.org.models import ProjectMembership, ProjectRole
        user = User.objects.create_user("analyst@example.com", "Bench Analyst")
        ProjectMembership.objects.create(project=project, user=user, role=ProjectRole.EDITOR)
        values = {"name": "S1", "mass": 1.5, "count": 3, "old": "lot 7", "temperature": 20.0}
        saved = services.create(user, "acme/lab.Sample", project, values)
        services.update(user, saved.entity, {"old": "lot 8"}, expected_revision=1)
        result = None
        """,
    )

    write_sample(lab, ("old: { type: string }", "old: { type: string, removed: true }"))
    made("lab", "--name", "remove_old", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    sync(out, database)
    # The world is made already: this reads what's there.
    script = textwrap.dedent(
        """
        import django, json
        django.setup()
        from api_client import Api
        from browser import Browser
        from iki_server.accounts.models import User
        from iki_server.entities import services
        from iki_server.entities.models import Entity
        sample = Entity.objects.get(hrid="SMP-000001")
        user = User.objects.get(email="analyst@example.com")
        # A revision after the field was removed doesn't have it.
        services.update(user, sample, {"name": "S1b"}, expected_revision=2)
        browser = Browser()
        browser.force_login(user)
        api = Api(browser)
        result = {
            n: (lambda r: [r.status_code, r.json()["values"], r.json()["changes"]])(
                api.get(f"/entities/SMP-000001/revisions/{n}")
            )
            for n in (1, 2, 3)
        }
        print(json.dumps(result))
        """
    )
    read = django(out, database, "-c", script)
    assert read.returncode == 0, read.stdout + read.stderr
    result = json.loads(read.stdout.strip().splitlines()[-1])
    first, second, third = result["1"], result["2"], result["3"]
    assert first[0] == second[0] == third[0] == 200
    assert first[1]["old"] == "lot 7"
    assert second[1]["old"] == "lot 8"
    assert second[2] == {"old": {"before": "lot 7", "after": "lot 8"}}
    assert "old" not in third[1]
    assert third[2] == {"name": {"before": "S1", "after": "S1b"}}


def test_migrations_change_columns_under_the_views_and_sync_makes_them_again(
    lab: Path, database: str
) -> None:
    """A view stops Postgres changing or dropping the columns it reads, so migrations run
    without Iki's, and `sync_plugins` makes them again."""
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    sync(out, database)
    assert query(database, "SELECT count(*) FROM iki_views.acme__lab_sample") == [(0,)]

    write_sample(
        lab,
        ("count: { type: int }", "count: { type: float }"),
        ("old: { type: string }", "old: { type: string, removed: true }"),
    )
    made("lab", "--name", "changes", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    migrate(out, database, "acme__lab", "0001")
    migrate(out, database)
    sync(out, database)

    types = dict(
        query(
            database,
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'iki_views' AND table_name = 'acme__lab_sample'",
        )
    )
    assert types["count"] == "double precision"
    assert "old" not in types


def test_indexing_a_field_adds_search_indexes_after_pg_trgm(lab: Path, database: str) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    write_sample(
        lab,
        (
            "name: { type: string, max_length: 100 }",
            "name: { type: string, max_length: 100, indexed: true }",
        ),
        ("old: { type: string }", "old: { type: string }\n  notes: { type: text }"),
    )
    made("lab", "--name", "search", cwd=lab.parent)
    migration = (lab / "migrations" / "0002_search.py").read_text()
    assert "('iki_entities', '0004_search')" in migration
    migrate(build(lab.parent, "lab"), database)
    indexes = dict(
        query(
            database,
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'acme__lab_sample'",
        )
    )
    defs = sorted(indexes.values())
    # name keeps its B-tree and gains trigrams; notes, as text, is searched by the words of
    # its first 100,000 characters, so a long value still saves (ADR 0233).
    assert any("btree (name)" in d for d in defs)
    assert any("gin (name gin_trgm_ops)" in d for d in defs)
    assert any(
        "gin (to_tsvector('simple'::regconfig, COALESCE(\"left\"(notes, 100000), ''::text)))" in d
        for d in defs
    ), defs


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
            "error: `old` is gone from acme/lab.Sample. Iki keeps every field's history, so a "
            "field can't be deleted.",
        ),
        (
            ("old: { type: string }", "old: { type: int }"),
            "error: `old` of acme/lab.Sample would change from text to a whole number, which its "
            "history can't be converted to.",
        ),
        (
            ("unit: g,", "unit: m,"),
            "error: `mass` of acme/lab.Sample can't change from g to m: they measure different "
            "things.",
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


LAB_ROLES = """\
roles:
  analyst: { label: Analyst }
  qa: { label: QA }
"""

STATES = """\
  status:
    type: state
    initial: received
    states:
      received: { label: Received, set_by: [analyst] }
      on_hold: { label: On hold, set_by: [analyst] }
      released: { label: Released, set_by: [qa], locks: true }
  stage:
    type: state
    initial: open
    states:
      open: { set_by: [analyst] }
      closed: { set_by: [qa], locks: true }
"""
STAGE = """\
  stage:
    type: state
    initial: open
    states:
      open: { set_by: [analyst] }
      closed: { set_by: [qa], locks: true }
"""
STRING_STAGE = STATES.replace(STAGE, "  stage: { type: string }\n")
ON_HOLD = "      on_hold: { label: On hold, set_by: [analyst] }\n"
#: The live rows' states, by HRID.
STATUS = (
    "SELECT e.hrid, t.status, t.stage, e.locked_by FROM acme__lab_sample t "
    "JOIN iki_entity e ON e.id = t.entity_id ORDER BY e.hrid"
)
TWO = """
from iki_plugin_acme__lab.models import Sample, Sample_History
from iki_server.entities.models import Entity
held = Sample.objects.create(type="acme/lab.Sample", hrid="SMP-000001",
                             owner_project=project, name="S1")
kept = Sample.objects.create(type="acme/lab.Sample", hrid="SMP-000002",
                             owner_project=project, name="S2")
Sample.objects.filter(pk=held.pk).update(status="on_hold", stage="closed")
Entity.objects.filter(pk=held.pk).update(locked_by=["stage"])
Entity.objects.filter(pk__in=[held.pk, kept.pk]).update(revision=1)
# Their first revisions' values, as Iki's services keep them.
for row in Sample.objects.filter(pk__in=[held.pk, kept.pk]):
    Sample_History.objects.create(entity_id=row.pk, revision=1, name=row.name,
                                  status=row.status, stage=row.stage)
result = [str(held.pk), Sample.objects.get(pk=kept.pk).status]
"""


def write_states(lab: Path, states: str = STATES) -> None:
    (lab.parent / "lab" / "plugin.yaml").write_text(LAB + LAB_ROLES)
    (lab / "types" / "sample.yaml").write_text(SAMPLE + states)


def test_state_fields_migrate_and_a_removed_state_moves_its_records(
    lab: Path, database: str
) -> None:
    """ADR 0125: a state field's states are its column's, records in a removed state move
    where the plugin says, as revisions with history, and a state that locks now locks."""
    write_states(lab)
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    held_id, initial = orm(out, database, TWO)
    assert initial == "received"
    assert query(database, STATUS) == [
        ("SMP-000001", "on_hold", "closed", ["stage"]),
        ("SMP-000002", "received", "open", []),
    ]
    unknown = "UPDATE acme__lab_sample SET status = 'shipped' WHERE entity_id = %s"
    with pytest.raises(psycopg.errors.CheckViolation):
        query(database, unknown, held_id)

    # Taking a state away strands its records, unless the plugin says where they go.
    write_states(lab, STATES.replace(ON_HOLD, ""))
    refused = iki("plugin", "makemigrations", "lab", "--no-input", cwd=lab.parent)
    assert refused.returncode == 1, refused.stdout
    assert "on_hold" in refused.stderr and "moved_to" in refused.stderr, refused.stderr

    # Kept, removed and moved.
    write_states(
        lab,
        STATES.replace(
            ON_HOLD, "      on_hold: { label: On hold, removed: true, moved_to: received }\n"
        ),
    )
    text = made("lab", "--name", "states", cwd=lab.parent)
    migration = (lab / "migrations" / "0002_states.py").read_text()
    assert "iki_server.plugins.operations.SyncStates(" in migration, text
    out = build(lab.parent, "lab")
    migrate(out, database)
    assert query(database, STATUS) == [
        ("SMP-000001", "received", "closed", ["stage"]),
        ("SMP-000002", "received", "open", []),
    ]
    revisions = query(
        database,
        "SELECT number, kind, changed_fields, state_field, state_from, state_to, via, user_id,"
        " reason FROM iki_revision WHERE entity_id = %s ORDER BY number",
        held_id,
    )
    assert revisions == [
        (
            2,
            "state_changed",
            ["status"],
            "status",
            "on_hold",
            "received",
            "system",
            None,
            "Its plugin removed the state on_hold: its migration moved it to received.",
        )
    ]
    assert query(
        database,
        "SELECT via, data->>'reason' FROM iki_audit_event WHERE target_id = %s"
        " AND action = 'entity.state_changed'",
        str(held_id),
    ) == [("system", revisions[0][-1])]
    assert query(database, "SELECT revision FROM iki_entity WHERE id = %s", held_id) == [(2,)]
    # Its history has the revision's values: the record's, with its new state.
    history = "SELECT revision, name, status, stage FROM acme__lab_sample__history"
    history += " WHERE entity_id = %s ORDER BY revision"
    assert query(database, history, held_id) == [
        (1, "S1", "on_hold", "closed"),
        (2, "S1", "received", "closed"),
    ]

    # Backwards, it goes back where it was, as a revision too.
    migrate(out, database, "acme__lab", "0001")
    assert query(database, STATUS)[0] == ("SMP-000001", "on_hold", "closed", ["stage"])
    assert query(database, "SELECT revision FROM iki_entity WHERE id = %s", held_id) == [(3,)]
    assert query(database, history, held_id) == [
        (1, "S1", "on_hold", "closed"),
        (2, "S1", "received", "closed"),
        (3, "S1", "on_hold", "closed"),
    ]


def test_a_state_that_locks_locks_the_records_in_it(lab: Path, database: str) -> None:
    write_states(lab, STATES.replace(", locks: true }", " }"))
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    orm(
        out,
        database,
        """
        from iki_plugin_acme__lab.models import Sample
        Sample.objects.create(type="acme/lab.Sample", hrid="SMP-000001", owner_project=project,
                              name="S1", status="released", stage="closed")
        Sample.objects.create(type="acme/lab.Sample", hrid="SMP-000002", owner_project=project,
                              name="S2", stage="closed")
        result = None
        """,
    )
    # Locked while its locked_by names a field (ADR 0125).
    locks = (
        "SELECT hrid, cardinality(locked_by) > 0, locked_by FROM iki_entity "
        "WHERE hrid LIKE 'SMP-%%' ORDER BY hrid"
    )
    assert query(database, locks) == [("SMP-000001", False, []), ("SMP-000002", False, [])]
    write_states(lab)
    made("lab", "--name", "locks", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    # Each field locks the records in its locking states, on its own.
    first, second = query(database, locks)
    assert (first[1], sorted(first[2])) == (True, ["stage", "status"])
    assert second == ("SMP-000002", True, ["stage"])
    migrate(out, database, "acme__lab", "0001")
    assert query(database, locks) == [("SMP-000001", False, []), ("SMP-000002", False, [])]


STAGE_MOVED = """\
    removed: true
    moved_to: { field: status, states: { open: received, closed: released } }
"""


def test_a_removed_state_field_moves_its_records_states(lab: Path, database: str) -> None:
    write_states(lab)
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    held_id, _ = orm(out, database, TWO)

    # Removed with nowhere for its states to go, its migration is refused while records
    # have it.
    write_states(
        lab,
        STATES.replace(
            "  stage:\n    type: state\n", "  stage:\n    type: state\n    removed: true\n"
        ),
    )
    made("lab", "--name", "drop_stage", cwd=lab.parent)
    text = (lab / "migrations" / "0002_drop_stage.py").read_text()
    assert "iki_server.plugins.operations.RetireStateField(" in text
    failed = django(build(lab.parent, "lab"), database, "-m", "django", "migrate", "--no-input")
    assert failed.returncode != 0
    assert "2 records are in its states, which would be lost" in failed.stderr, failed.stderr
    assert query(database, STATUS)[0] == ("SMP-000001", "on_hold", "closed", ["stage"])
    (lab / "migrations" / "0002_drop_stage.py").unlink()

    # With moved_to, each record's state there moves to the field it names, as a revision,
    # and locks as that field's states do.
    write_states(
        lab,
        STATES.replace("  stage:\n    type: state\n", "  stage:\n    type: state\n" + STAGE_MOVED),
    )
    made("lab", "--name", "retire_stage", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    rows = "SELECT e.hrid, t.status, e.locked_by FROM acme__lab_sample t "
    rows += "JOIN iki_entity e ON e.id = t.entity_id ORDER BY e.hrid"
    assert query(database, rows) == [
        ("SMP-000001", "released", ["status"]),
        ("SMP-000002", "received", []),
    ]
    revisions = query(
        database,
        "SELECT number, state_field, state_from, state_to, reason FROM iki_revision"
        " WHERE entity_id = %s ORDER BY number",
        held_id,
    )
    assert revisions == [
        (
            2,
            "status",
            "on_hold",
            "released",
            "Its plugin removed the state field stage: its migration moved its closed to "
            "status released.",
        )
    ]
    history = "SELECT revision, status, stage FROM acme__lab_sample__history"
    history += " WHERE entity_id = %s ORDER BY revision"
    assert query(database, history, held_id) == [(1, "on_hold", "closed"), (2, "released", None)]

    # Backwards, the field is back with each record's state in it, and the move undone.
    migrate(out, database, "acme__lab", "0001")
    assert query(database, STATUS) == [
        ("SMP-000001", "on_hold", "closed", ["stage"]),
        ("SMP-000002", "received", "open", []),
    ]


def test_a_renamed_state_field_keeps_its_locks(lab: Path, database: str) -> None:
    write_states(lab)
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    held_id, _ = orm(out, database, TWO)
    query(
        database,
        "INSERT INTO iki_revision (id, entity_id, number, kind, changed_fields, via, client,"
        " reason, state_field, state_from, state_to, created_at) VALUES (uuidv7(), %s, 2,"
        " 'state_changed', '{stage}', 'web', '', '', 'stage', 'open', 'closed', now())",
        held_id,
    )
    write_states(lab, STATES.replace("  stage:\n", "  phase:\n"))
    text = made("lab", "--name", "phase", cwd=lab.parent, answers="y\n")
    assert "RenameStateField(" in (lab / "migrations" / "0002_phase.py").read_text(), text
    out = build(lab.parent, "lab")
    migrate(out, database)
    locked = "SELECT locked_by FROM iki_entity WHERE id = %s"
    assert query(database, locked, held_id) == [(["phase"],)]
    # Its earlier changes keep the name it had: history isn't rewritten.
    field = "SELECT state_field FROM iki_revision WHERE entity_id = %s AND number = 2"
    assert query(database, field, held_id) == [("stage",)]
    migrate(out, database, "acme__lab", "0001")
    assert query(database, locked, held_id) == [(["stage"],)]


@pytest.mark.parametrize(
    ("before", "after", "message"),
    [
        (STATES, STRING_STAGE, "is a state field"),
        (STRING_STAGE, STATES, "would become a state field"),
    ],
)
def test_a_field_cant_become_or_stop_being_a_state_field(
    lab: Path, before: str, after: str, message: str
) -> None:
    write_states(lab, before)
    made("lab", "--no-input", cwd=lab.parent)
    write_states(lab, after)
    result = iki("plugin", "makemigrations", "lab", "--no-input", cwd=lab.parent)
    assert result.returncode == 1
    assert message in result.stderr, result.stderr


def test_types_cant_be_deleted(lab: Path) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    (lab / "types" / "sample.yaml").unlink()
    result = iki("plugin", "makemigrations", "lab", "--no-input", cwd=lab.parent)
    assert result.returncode == 1
    assert result.stderr.startswith(
        "error: acme/lab.Sample is gone. Iki keeps every entity's history, so a plugin can't "
        "delete a type or an extension: put its manifest back."
    ), result.stderr


def test_renaming_a_type_renames_its_entities(lab: Path, database: str) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    orm(
        out,
        database,
        """
        from iki_plugin_acme__lab.models import Sample
        Sample.objects.create(type="acme/lab.Sample", hrid="SMP-000001", owner_project=project)
        result = None
        """,
    )
    write_sample(lab, ("name: Sample", "name: Specimen"))
    made("lab", "--name", "specimen", cwd=lab.parent, answers="y\n")
    migration = (lab / "migrations" / "0002_specimen.py").read_text()
    assert "RenameEntityType(" in migration
    out = build(lab.parent, "lab")
    migrate(out, database)
    assert query(database, "SELECT type FROM iki_entity") == [("acme/lab.Specimen",)]
    assert query(database, "SELECT count(*) FROM acme__lab_specimen__history") == [(0,)]
    migrate(out, database, "acme__lab", "0001")
    assert query(database, "SELECT type FROM iki_entity") == [("acme/lab.Sample",)]


def test_two_namespaces_plugins_of_one_name_install_side_by_side(lab: Path, database: str) -> None:
    # Another lab's plugin, also named `lab`, with a type also named Sample (ADR 0113).
    other = lab.parent / "globex_lab"
    (other / "types").mkdir(parents=True)
    (other / "plugin.yaml").write_text(LAB.replace("acme/lab", "globex/lab"))
    (other / "types" / "sample.yaml").write_text(SAMPLE.replace("SMP", "GSMP"))
    made("lab", "--no-input", cwd=lab.parent)
    made("globex_lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab", "globex_lab")
    migrate(out, database)
    tables = {
        row[0]
        for row in query(
            database,
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND "
            "tablename LIKE '%%\\_\\_lab\\_sample%%'",
        )
    }
    assert tables == {
        "acme__lab_sample",
        "acme__lab_sample__history",
        "globex__lab_sample",
        "globex__lab_sample__history",
    }
    counts = orm(
        out,
        database,
        """
        from iki_plugin_acme__lab.models import Sample as Ours
        from iki_plugin_globex__lab.models import Sample as Theirs
        Ours.objects.create(type="acme/lab.Sample", hrid="SMP-000001", owner_project=project)
        Theirs.objects.create(type="globex/lab.Sample", hrid="GSMP-000001", owner_project=project)
        result = [Ours.objects.count(), Theirs.objects.count()]
        """,
    )
    assert counts == [1, 1]
    assert sorted(query(database, "SELECT type FROM iki_entity")) == [
        ("acme/lab.Sample",),
        ("globex/lab.Sample",),
    ]
    assert applied(database, "acme__lab") == ["0001_initial"]
    assert applied(database, "globex__lab") == ["0001_initial"]


# --- Lists of rows, changed by an upgrade (ADR 0137) -------------------------------------------

#: A list of rows, each a reference and an amount, both checked.
ADDITIONS = (
    "  temperature: { type: quantity, unit: Cel }\n",
    "  temperature: { type: quantity, unit: Cel }\n"
    "  additions:\n"
    "    type: list\n"
    "    items:\n"
    "      type: row\n"
    "      fields:\n"
    "        part: { type: ref, to: Sample, required: true }\n"
    "        amount: { type: quantity, unit: mg, min: 0 }\n",
)
#: A field added with the list, whose CHECK constraints the migration also adds.
NOTES = ("  count: { type: int }", "  count: { type: int }\n  notes: { type: string }")


def samples(out: Path, database: str) -> list[str]:
    """Makes two samples, and gives their IDs."""
    ids: list[str] = orm(
        out,
        database,
        """
        from iki_plugin_acme__lab.models import Sample
        result = [
            str(Sample.objects.create(
                type="acme/lab.Sample", hrid=f"SMP-00000{n}", owner_project=project,
                name=f"S{n}", mass=1.5, count=3, old="x", temperature=20.0,
            ).pk)
            for n in (1, 2)
        ]
        """,
    )
    return ids


def add_rows(database: str, table: str, first: str, second: str) -> None:
    """Gives the first sample two rows, each of the second at an amount."""
    for position, amount in enumerate((5.0, 7.5)):
        query(
            database,
            f"INSERT INTO {table} (entity_id, position, part_id, amount) "  # noqa: S608 - the test's own table
            "VALUES (%s, %s, %s, %s)",
            first,
            position,
            second,
            amount,
        )


def rows(database: str, table: str, *columns: str) -> list[tuple[Any, ...]]:
    names = ", ".join(("position", "part_id::text", *columns))
    return query(database, f"SELECT {names} FROM {table} ORDER BY entity_id, position")  # noqa: S608 - the test's own table


def keyed_by_entity_and_position(database: str, table: str) -> bool:
    """Whether the table's primary key is its entity and position, its entity a foreign key."""
    found = dict(
        query(
            database,
            "SELECT contype, string_agg(pg_get_constraintdef(oid), '; ') FROM pg_constraint "
            "WHERE conrelid = %s::regclass AND contype IN ('p', 'f') GROUP BY contype",
            table,
        )
    )
    return found.get("p") == 'PRIMARY KEY (entity_id, "position")' and (
        "FOREIGN KEY (entity_id) REFERENCES acme__lab_sample(entity_id)" in found.get("f", "")
    )


def created_whole(migration: str, model: str) -> bool:
    """Whether a migration creates a list's model with the entity it's keyed by, rather than
    adding it afterwards: a model whose key names a field it lacks can't be made."""
    create = re.search(
        rf"migrations\.CreateModel\(\n\s+name='{model}',\n\s+fields=\[\n(.*?)\n\s+\],",
        migration,
        re.DOTALL,
    )
    return (
        create is not None
        and "('entity', models.ForeignKey(" in create.group(1)
        and f"model_name='{model.lower()}',\n            name='entity'," not in migration
    )


def test_an_upgrade_adds_a_list_of_rows(lab: Path, database: str) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    first, second = samples(out, database)

    write_sample(lab, NOTES, ADDITIONS)
    made("lab", "--name", "additions", "--no-input", cwd=lab.parent)
    migration = (lab / "migrations" / "0002_additions.py").read_text()
    assert created_whole(migration, "Sample_additions"), migration
    out = build(lab.parent, "lab")
    table = "acme__lab_sample__additions"
    for _ in range(2):  # forwards, back, and forwards again
        migrate(out, database)
        assert keyed_by_entity_and_position(database, table)
        add_rows(database, table, first, second)
        assert rows(database, table, "amount") == [(0, second, 5.0), (1, second, 7.5)]
        assert "additions" in columns(database, "acme__lab_sample__history")
        migrate(out, database, "acme__lab", "0001")
        assert query(database, "SELECT to_regclass(%s)", table) == [(None,)]
        assert "additions" not in columns(database, "acme__lab_sample__history")
        assert query(database, "SELECT name FROM acme__lab_sample ORDER BY name") == [
            ("S1",),
            ("S2",),
        ]


def test_an_upgrade_adds_a_field_to_a_list_of_rows(lab: Path, database: str) -> None:
    write_sample(lab, ADDITIONS)
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    first, second = samples(out, database)
    table = "acme__lab_sample__additions"
    add_rows(database, table, first, second)

    write_sample(
        lab,
        ADDITIONS,
        NOTES,
        (
            "        amount: { type: quantity, unit: mg, min: 0 }\n",
            "        amount: { type: quantity, unit: mg, min: 0 }\n"
            "        lot: { type: string, max_length: 20 }\n",
        ),
    )
    made("lab", "--name", "lots", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    for _ in range(2):
        migrate(out, database)
        assert keyed_by_entity_and_position(database, table)
        assert rows(database, table, "amount", "lot") == [
            (0, second, 5.0, None),
            (1, second, 7.5, None),
        ]
        query(database, f"UPDATE {table} SET lot = 'L-17' WHERE position = 0")  # noqa: S608 - the test's own table
        with pytest.raises(psycopg.errors.CheckViolation):
            query(database, f"UPDATE {table} SET lot = '' WHERE position = 1")  # noqa: S608 - the test's own table
        migrate(out, database, "acme__lab", "0001")
        assert keyed_by_entity_and_position(database, table)
        assert rows(database, table, "amount") == [(0, second, 5.0), (1, second, 7.5)]
        assert "lot" not in columns(database, table)


ALIQUOT = "INSERT INTO acme__lab_sample__aliquots (entity_id, position, item_id) VALUES (%s, 0, %s)"


def test_an_upgrade_removes_lists_of_rows_and_references(lab: Path, database: str) -> None:
    aliquots = (
        "  count: { type: int }\n",
        "  count: { type: int }\n  aliquots: { type: list, items: { type: ref, to: Sample } }\n",
    )
    write_sample(lab, ADDITIONS, aliquots)
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    first, second = samples(out, database)
    table, refs = "acme__lab_sample__additions", "acme__lab_sample__aliquots"
    add_rows(database, table, first, second)
    query(database, ALIQUOT, first, second)
    kept = [{"part": second, "amount": 5.0}, {"part": second, "amount": 7.5}]
    query(
        database,
        "INSERT INTO acme__lab_sample__history (entity_id, revision, additions, aliquots) "
        "VALUES (%s, 1, %s, %s::uuid[])",
        first,
        json.dumps(kept),
        [second],
    )

    # Both at once: neither table loses its key before it goes.
    write_sample(
        lab,
        ADDITIONS,
        aliquots,
        ("  additions:\n    type: list\n", "  additions:\n    type: list\n    removed: true\n"),
        ("aliquots: { type: list,", "aliquots: { type: list, removed: true,"),
    )
    made("lab", "--name", "no_lists", "--no-input", cwd=lab.parent)
    migration = (lab / "migrations" / "0002_no_lists.py").read_text()
    assert "name='entity'" not in migration, migration
    out = build(lab.parent, "lab")
    for _ in range(2):
        migrate(out, database)
        assert query(database, "SELECT to_regclass(%s), to_regclass(%s)", table, refs) == [
            (None, None)
        ]
        # Their history stays.
        assert query(
            database, "SELECT additions, aliquots::text[] FROM acme__lab_sample__history"
        ) == [(kept, [second])]
        # Back, the tables are made again, empty, as a removed column comes back empty.
        migrate(out, database, "acme__lab", "0001")
        assert keyed_by_entity_and_position(database, table)
        assert keyed_by_entity_and_position(database, refs)
        assert rows(database, table) == []
        add_rows(database, table, first, second)
        assert rows(database, table, "amount") == [(0, second, 5.0), (1, second, 7.5)]
        query(database, ALIQUOT, first, second)


def test_an_upgrade_renames_a_list_of_rows(lab: Path, database: str) -> None:
    write_sample(lab, ADDITIONS)
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    first, second = samples(out, database)
    old, new = "acme__lab_sample__additions", "acme__lab_sample__ingredients"
    add_rows(database, old, first, second)

    write_sample(lab, NOTES, ADDITIONS, ("  additions:\n", "  ingredients:\n"))
    # Renamed: the list's table, then its history column.
    made("lab", "--name", "ingredients", cwd=lab.parent, answers="y\ny\n")
    migration = (lab / "migrations" / "0002_ingredients.py").read_text()
    assert "RecordRename(" in migration
    out = build(lab.parent, "lab")
    for _ in range(2):
        migrate(out, database)
        assert query(database, "SELECT to_regclass(%s)", old) == [(None,)]
        assert rows(database, new, "amount") == [(0, second, 5.0), (1, second, 7.5)]
        assert "ingredients" in columns(database, "acme__lab_sample__history")
        migrate(out, database, "acme__lab", "0001")
        assert query(database, "SELECT to_regclass(%s)", new) == [(None,)]
        assert keyed_by_entity_and_position(database, old)
        assert rows(database, old, "amount") == [(0, second, 5.0), (1, second, 7.5)]
        assert "additions" in columns(database, "acme__lab_sample__history")


# --- Deploying and rolling back -------------------------------------------------------------------


def build_to(plugins: Path, name: str, *plugin_names: str) -> Path:
    output = plugins.parent / name
    result = iki("plugin", "build", *plugin_names, "--output", output, cwd=plugins)
    assert result.returncode == 0, result.stdout + result.stderr
    return output


def deploy(build: Path, database: str) -> subprocess.CompletedProcess[str]:
    """Deploys as the migrate Job does, with the database's one role (no grants)."""
    return django(build, database, "-m", "django", "migrate_deployment", "--no-grant")


def deployed(build: Path, database: str) -> str:
    result = deploy(build, database)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def refused(build: Path, database: str) -> str:
    result = deploy(build, database)
    assert result.returncode != 0, result.stdout
    return result.stderr


def test_revisions_follow_field_names_through_upgrade_and_rollback(
    lab: Path, database: str
) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    first = build_to(lab.parent, "v1", "lab")
    deployed(first, database)
    orm(
        first,
        database,
        """
        from iki_server.accounts.models import User
        from iki_server.entities import services
        from iki_server.org.models import ProjectMembership, ProjectRole
        user = User.objects.create_user("a@example.com", "A")
        ProjectMembership.objects.create(project=project, user=user, role=ProjectRole.ADMIN)
        e = services.create(user, "acme/lab.Sample", project, {"name": "S1", "count": 3}).entity
        services.update(user, e, {"name": "S2"}, expected_revision=1)
        result = None
        """,
    )
    write_sample(lab, ("  name: {", "  title: {"))
    made("lab", "--name", "rename", cwd=lab.parent, answers="y\n")
    second = build_to(lab.parent, "v2", "lab")
    deployed(second, database)
    read = """
from django.test import Client
from iki_server.accounts.models import User
client = Client()
client.force_login(User.objects.get(email="a@example.com"))
def get(path):
    response = client.get("/api/v1/entities/SMP-000001/" + path, secure=True)
    assert response.status_code == 200, response.content
    return response.json()
result = [
    get("revisions/1")["changes"][field],
    get("revisions/2")["changes"],
    get("provenance")[field]["number"],
    [r["number"] for r in get("revisions?field=" + field)["items"]],
]
"""
    before = {"before": None, "after": "S1"}
    edit = {"before": "S1", "after": "S2"}
    assert run(second, database, "field = 'title'\n" + read) == [before, {"title": edit}, 2, [2, 1]]
    run(
        second,
        database,
        """
        from iki_server.accounts.models import User
        from iki_server.entities import services
        from iki_server.entities.models import Entity
        record = Entity.objects.get(hrid="SMP-000001")
        services.update(User.objects.get(email="a@example.com"), record,
                        {"title": "S3"}, expected_revision=2)
        result = None
        """,
    )
    deployed(first, database)
    assert run(first, database, "field = 'name'\n" + read) == [before, {"name": edit}, 3, [3, 2, 1]]
    assert query(
        database, "SELECT changed_fields, history_seen FROM iki_revision ORDER BY number"
    ) == [(["count", "name"], 0), (["name"], 0), (["title"], 1)]


def test_deploy_requires_matching_live_and_history_renames_in_both_directions(
    lab: Path, database: str
) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    first = build_to(lab.parent, "v1", "lab")
    deployed(first, database)
    write_sample(lab, ("  name: {", "  title: {"))
    made("lab", "--name", "rename", cwd=lab.parent, answers="y\n")
    migration = lab / "migrations" / "0002_rename.py"
    source, marker = migration.read_text().split("RecordRename(", 1)
    migration.write_text(source + "RecordRename(" + marker.replace("'title'", "'label'", 1))
    second = build_to(lab.parent, "v2", "lab")
    assert "needs matching RenameField operations" in refused(second, database)
    assert "name" in columns(database, "acme__lab_sample__history")
    assert query(database, "SELECT id FROM iki_history_change") == []

    # A database migrated before the forward check still has to preflight its kept copy.
    migrate(second, database)
    deployed(second, database)
    assert "needs matching RenameField operations" in refused(first, database)
    assert "title" in columns(database, "acme__lab_sample__history")


def applied(database: str, app: str) -> list[str]:
    rows = query(database, "SELECT name FROM django_migrations WHERE app = %s ORDER BY id", app)
    return [r[0] for r in rows]


def columns(database: str, table: str) -> set[str]:
    rows = query(
        database,
        "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 "
        "AND NOT attisdropped",
        table,
    )
    return {r[0] for r in rows}


# --- Purging a plugin at deploy (ADR 0092) ----------------------------------------------------


def run(build: Path, database: str, code: str) -> Any:
    """Like :func:`orm`, without making ``WORLD`` again."""
    script = "import django, json\ndjango.setup()\n" + textwrap.dedent(code)
    script += "\nprint(json.dumps(result, default=str))\n"
    out = django(build, database, "-c", script)
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_purged_plugin_is_dropped_once_its_purge_is_due(migrated: Path, database: str) -> None:
    deployment = migrated.parent / "deployments" / "dev"
    deployment.mkdir(parents=True)
    (deployment / "dna.yaml").write_text("ncbi_email: lab@example.org\n")

    def built(state: str) -> Path:
        entry = (
            "  - {name: acme/dna, state: purged}\n"
            if state == "purged"
            else f"  - {{name: acme/dna, local: ../../plugins/dna, state: {state}, "
            "settings: dna.yaml}\n"
        )
        (deployment / "plugins.lock.yaml").write_text(
            "apiVersion: iki/v1\nkind: PluginLock\nplugins:\n" + entry
        )
        out = migrated.parent / f"image-{state}"
        result = iki("deploy", "build", deployment, "-o", out, "--allow-local", cwd=migrated)
        assert result.returncode == 0, result.stdout + result.stderr
        return out

    active, detached, purged = built("active"), built("detached"), built("purged")
    deployed(active, database)
    sync(active, database)
    made = orm(
        active,
        database,
        """
        from iki_plugin_acme__dna.models import DNASequence, DNASequence_Entry
        from iki_server.accounts.models import User
        _, dna = register(DNASequence, DNASequence_Entry, {"bases": "ACGT"}, name="pUC19")
        User.objects.create_user("admin@example.com", "Org Admin")
        result = str(dna.pk)
        """,
    )
    query(
        database,
        "INSERT INTO iki__core_sequence__history (entity_id, revision) VALUES (%s, 1) RETURNING 1",
        made,
    )
    query(
        database,
        "INSERT INTO acme__dna_dnasequence__history (entity_id, revision) VALUES (%s, 1) "
        "RETURNING 1",
        made,
    )
    deployed(detached, database)
    sync(detached, database)

    assert "no purge of it was asked for" in refused(purged, database)
    run(
        detached,
        database,
        """
        import datetime as dt
        from django.utils import timezone
        from iki_server.accounts.models import User
        from iki_server.plugins.models import PluginPurge
        PluginPurge.objects.create(
            plugin="acme/dna", reason="Retention over", report={}, requested_by=User.objects.get(),
            run_after=timezone.now() + dt.timedelta(days=30),
        )
        result = None
        """,
    )
    assert "acme/dna's purge isn't due until" in refused(purged, database)
    run(
        detached,
        database,
        """
        from django.utils import timezone
        from iki_server.plugins import purge
        from iki_server.plugins.models import PluginPurge
        PluginPurge.objects.update(run_after=timezone.now())
        result = purge.tombstone_due()
        """,
    )
    hold = run(
        detached,
        database,
        """
        from iki_server.accounts.models import User
        from iki_server.org.models import LegalHold, Project
        result = str(LegalHold.objects.create(
            project=Project.objects.get(), matter="Case 24-117", placed_by=User.objects.get(),
        ).pk)
        """,
    )
    err = refused(purged, database)
    assert "Some of acme/dna's data is on legal hold" in err
    assert "Case 24-117" not in err and "Antibodies" not in err  # holds are confidential
    query(database, "DELETE FROM iki_legal_hold WHERE id = %s RETURNING 1", hold)
    assert query(database, "SELECT count(*) FROM acme__dna_dnasequence") == [(1,)]

    out = deployed(purged, database)
    assert "Purged acme/dna: dropped 14 table(s); 2 entities keep their tombstones" in out
    assert "row(s) their entities had in other plugins' tables" in out
    left = "SELECT tablename FROM pg_tables WHERE tablename LIKE 'acme\\_\\_dna\\_%%'"
    assert query(database, left) == []
    for gone in (
        "SELECT count(*) FROM iki__core_sequence WHERE entity_id = %s",
        "SELECT count(*) FROM iki__core_sequence__history WHERE entity_id = %s",
        "SELECT count(*) FROM iki_unique_value WHERE entity_id = %s",
    ):
        assert query(database, gone, made) == [(0,)]
    assert (
        query(
            database,
            "SELECT e.hrid, e.type, e.purged_at IS NOT NULL, e.identity_hash, "
            "length(t.content_sha256) FROM iki_entity e JOIN iki_tombstone t ON t.entity_id = e.id",
        )
        == [("DNA-000001", "acme/dna.DNASequence", True, None, 64)] * 2
    )  # its entry and record
    assert applied(database, "acme__dna") == []
    assert query(database, "SELECT done_at IS NOT NULL FROM iki_plugin_purge") == [(True,)]
    assert query(database, "SELECT state FROM iki_plugin_state WHERE name = 'acme/dna'") == [
        ("purged",)
    ]
    events = query(database, "SELECT data FROM iki_audit_event WHERE action = 'plugin.purged'")
    assert [e[0]["plugin"] for e in events] == ["acme/dna"]
    assert "dna" not in sync(purged, database)  # its views went; the rest are made again
    assert "Purged" not in deployed(purged, database)  # once


# --- iki plugin test --upgrade-from (ADR 0094) -----------------------------------------------

WEIGH = """\
apiVersion: iki/v1
kind: Action
name: weigh
inputs:
  sample: { type: Sample, schema_version: ">=1" }
effects:
  updates: { sample: { fields: [mass] } }
runs_on: { tier: backend, environment: tools }
command: [python, ./weigh.py]
"""

TOOLS = """\
apiVersion: iki/v1
kind: Environment
name: tools
tiers: [backend]
python: { version: "3.12", requirements: requirements.txt }
"""

WEIGHED = """\
apiVersion: iki/v1
kind: ActionTest
action: weigh
inputs:
  sample:
    - values: { name: S1, mass: 1.5, count: 3, temperature: 20 }
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - git, in the test's own repository
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=t@example.com", *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


def commit_weighing_fixture(lab: Path, action: str = WEIGH, fixture: str = WEIGHED) -> Path:
    """The lab plugin, with an action and a test, committed at its first migration."""
    (lab / "actions").mkdir()
    (lab / "actions" / "weigh.yaml").write_text(action)
    (lab / "environments").mkdir()
    (lab / "environments" / "tools.yaml").write_text(TOOLS)
    (lab / "requirements.txt").write_text("")
    (lab / "weigh.py").write_text("")
    (lab / "tests").mkdir()
    (lab / "tests" / "weighed.yaml").write_text(fixture)
    made("lab", "--no-input", cwd=lab.parent)
    repo = lab.parent
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "v1")
    return lab


@pytest.fixture
def versioned(lab: Path) -> Path:
    return commit_weighing_fixture(lab)


def upgrade(lab: Path, server: str, *args: str) -> subprocess.CompletedProcess[str]:
    return iki(
        "plugin", "test", lab, "--upgrade-from", "main", "--database", server, *args, cwd=lab
    )


ADD_NOTES = ("  count: { type: int }", "  count: { type: int }\n  notes: { type: text }")


def test_an_upgrade_keeps_the_samples_and_rolls_back(versioned: Path, _postgres_url: str) -> None:
    write_sample(versioned, ADD_NOTES, ("schema_version: 1", "schema_version: 2"))
    (versioned / "plugin.yaml").write_text(LAB.replace("1.0.0", "1.1.0"))
    made("lab", "--name", "notes", "--no-input", cwd=versioned.parent)
    result = upgrade(versioned, _postgres_url, "-k", "none")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    header = re.fullmatch(
        r"Upgrading acme/lab from [0-9a-f]{12} \(1\.0\.0\) to 1\.1\.0 on the scratch database "
        r"(iki_upgrade_[0-9a-f]{8}), with 1 sample\(s\):",
        lines[0],
    )
    assert header is not None, lines[0]
    assert lines[1] == "  ok      each type whose fields changed has a higher schema_version"
    assert lines[2].startswith("  ok      migrated to the earlier commit and made the samples")
    assert lines[3].startswith("  ok      upgraded: the samples and their history are intact")
    assert lines[4].startswith("  ok      rolled back: the samples have their values")
    with psycopg.connect(_postgres_url) as connection:
        left = connection.execute(
            "SELECT datname FROM pg_database WHERE datname = %s", [header.group(1)]
        ).fetchall()
    assert left == []  # the scratch database is dropped


@pytest.mark.parametrize("earlier_marked", [False, True])
def test_upgrade_uses_complete_fixtures_with_included_references(
    lab: Path, _postgres_url: str, earlier_marked: bool
) -> None:
    material = """
apiVersion: iki/v1
kind: EntityType
name: Material
schema_version: 1
hrid: { prefix: MAT }
identity: [code]
fields:
  code: { type: string, required: true, immutable: true, max_length: 20 }
  description: { type: text }
"""
    (lab / "types" / "material.yaml").write_text(material)
    material_ref = (
        "  count: { type: int }",
        "  count: { type: int }\n  material: { type: ref, to: Material, required: true }",
    )
    write_sample(lab, material_ref, ("schema_version: 1", "schema_version: 2"))
    action = WEIGH.replace('schema_version: ">=1"', 'schema_version: ">=1", include: [material]')
    earlier = (
        WEIGHED
        + """
      included:
        material:
          - values: { description: A worker only reads this description. }
"""
    )
    marked = earlier.replace(
        "action: weigh", "name: complete_weight_sample\nupgrade: true\naction: weigh"
    )
    complete = marked.replace(
        "description: A worker only reads this description.", "code: M1, description: Sample"
    )
    commit_weighing_fixture(lab, action, complete if earlier_marked else earlier)
    tests = lab / "tests" / "weighed.yaml"
    tests.write_text(earlier + "---\n" + (marked if earlier_marked else complete))
    write_sample(
        lab,
        material_ref,
        ("  old: { type: string }", "  old: { type: string }\n  notes: { type: text }"),
        ("schema_version: 1", "schema_version: 3"),
    )
    made("lab", "--no-input", cwd=lab.parent)
    result = upgrade(lab, _postgres_url, "-k", "none")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "with 1 sample(s)" in result.stdout
    source = "earlier" if earlier_marked else "current"
    assert f"Using fixtures marked upgrade in the {source} plugin." in result.stdout
    assert "upgraded: the samples and their history are intact" in result.stdout
    assert "rolled back: the samples have their values" in result.stdout


def test_a_type_that_changes_needs_a_higher_schema_version(
    versioned: Path, _postgres_url: str
) -> None:
    write_sample(versioned, ADD_NOTES, ("max_length: 100", "max_length: 50"))
    made("lab", "--name", "notes", "--no-input", cwd=versioned.parent)
    result = upgrade(versioned, _postgres_url, "-k", "none")
    assert result.returncode == 1
    assert "  FAILED  each type whose fields changed has a higher schema_version" in result.stdout
    assert (
        "acme/lab.Sample changed (added notes; changed name's max_length), and its "
        "schema_version is still 1. Increase it to 2: plugins that depend on acme/lab pin its "
        "types' schema versions."
    ) in result.stdout
    assert "migrated to the earlier commit" not in result.stdout  # nothing ran

    write_sample(versioned, ("schema_version: 1", "schema_version: 2"))
    git(versioned.parent, "commit", "-q", "-am", "v2")
    write_sample(versioned)
    result = upgrade(versioned, _postgres_url, "-k", "none")
    assert result.returncode == 1
    assert "acme/lab.Sample's schema_version went down, from 2 to 1." in result.stdout


def test_how_a_field_is_shown_isnt_a_new_schema(versioned: Path, _postgres_url: str) -> None:
    write_sample(
        versioned,
        ("  count: { type: int }", "  count: { type: int, label: Tubes, indexed: true }"),
    )
    made("lab", "--name", "count_index", "--no-input", cwd=versioned.parent)
    result = upgrade(versioned, _postgres_url, "-k", "none", "--no-rollback")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "  ok      each type whose fields changed has a higher schema_version" in result.stdout
    # The index is a migration, and the plugin is still 1.0.0.
    assert (
        "Note: the migrations changed, and acme/lab's version is still 1.0.0. Increase it when you "
        "release this, so a deployment's history tells the two apart."
    ) in result.stdout


def test_an_upgrade_that_cant_be_rolled_back_fails_unless_its_one_way(
    versioned: Path, _postgres_url: str
) -> None:
    (versioned / "migrations" / "0002_backfill.py").write_text(
        "from django.db import migrations\n\n\n"
        "def backfill(apps, schema_editor):\n    pass\n\n\n"
        "class Migration(migrations.Migration):\n"
        '    dependencies = [("acme__lab", "0001_initial")]\n'
        "    operations = [migrations.RunPython(backfill)]\n"
    )
    result = upgrade(versioned, _postgres_url, "-k", "none")
    assert result.returncode == 1
    assert "  FAILED  rolled back: the samples have their values" in result.stdout
    assert "The deploy refused it: Rolling back would lose data" in result.stdout
    assert "acme/lab 0002_backfill: Raw Python operation can't be reversed." in result.stdout
    one_way = upgrade(versioned, _postgres_url, "-k", "none", "--no-rollback")
    assert one_way.returncode == 0, one_way.stdout + one_way.stderr
    assert "rolled back" not in one_way.stdout


def test_a_rollback_that_loses_a_value_fails(versioned: Path, _postgres_url: str) -> None:
    (versioned / "migrations" / "0002_rename_all.py").write_text(
        "from django.db import migrations\n\n\n"
        "class Migration(migrations.Migration):\n"
        '    dependencies = [("acme__lab", "0001_initial")]\n'
        "    operations = [\n"
        "        migrations.RunSQL(\n"
        "            migrations.RunSQL.noop, \"UPDATE acme__lab_sample SET name = 'renamed'\"\n"
        "        )\n"
        "    ]\n"
    )
    result = upgrade(versioned, _postgres_url, "-k", "none")
    assert result.returncode == 1
    assert "FAILED  rolled back" in result.stdout
    assert 'SMP-000001\'s name is "renamed", but its revision 2 holds "S1".' in result.stdout


def test_an_upgrade_keeps_each_revisions_values_as_its_migrations_say(
    versioned: Path, _postgres_url: str
) -> None:
    """Each sample is made in two revisions, the first with only what it needs, and each
    revision's values are compared after the upgrade: renamed and converted as its
    migrations say they were (ADR 0148), and otherwise as they were."""
    write_sample(
        versioned,
        ("  name: {", "  title: {"),
        ("unit: g,", "unit: mg,"),
        ("schema_version: 1", "schema_version: 2"),
    )
    (versioned / "plugin.yaml").write_text(LAB.replace("1.0.0", "1.1.0"))
    (versioned / "tests" / "weighed.yaml").write_text(WEIGHED.replace("name: S1", "title: S1"))
    made("lab", "--name", "changes", cwd=versioned.parent, answers="y\n")
    result = upgrade(versioned, _postgres_url, "-k", "none")
    assert "rolled back: the samples have their values" in result.stdout
    assert result.returncode == 0, result.stdout + result.stderr
    assert "  ok      upgraded: the samples and their history are intact" in result.stdout


def test_an_upgrade_that_loses_a_revisions_values_fails(
    versioned: Path, _postgres_url: str
) -> None:
    """A migration that empties a column of history, its records' own values intact: every
    revision still reads, at the same revision count, but not what it held."""
    (versioned / "migrations" / "0002_lose_counts.py").write_text(
        "from django.db import migrations\n\n\n"
        "class Migration(migrations.Migration):\n"
        '    dependencies = [("acme__lab", "0001_initial")]\n'
        "    operations = [\n"
        "        migrations.RunSQL(\n"
        '            "ALTER TABLE acme__lab_sample__history DROP COLUMN count; "\n'
        '            "ALTER TABLE acme__lab_sample__history ADD COLUMN count integer",\n'
        "            migrations.RunSQL.noop,\n"
        "        )\n"
        "    ]\n"
    )
    result = upgrade(versioned, _postgres_url, "-k", "none", "--no-rollback")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "  FAILED  upgraded: the samples and their history are intact" in result.stdout
    assert "SMP-000001's revision 2 in acme__lab_sample__history was rewritten" in result.stdout


def backfill_migration(lab: Path, operations: str, functions: str = "") -> None:
    write_sample(lab, ADD_NOTES, ("schema_version: 1", "schema_version: 2"))
    (lab / "plugin.yaml").write_text(LAB.replace("1.0.0", "1.1.0"))
    made("lab", "--name", "notes", "--no-input", cwd=lab.parent)
    (lab / "migrations" / "0003_fill.py").write_text(
        "from django.db import migrations\n"
        "from iki_server.plugins.operations import MigrateValues\n"
        + functions
        + "\nclass Migration(migrations.Migration):\n"
        '    dependencies = [("acme__lab", "0002_notes"), '
        '("iki_entities", "0027_migrated_revisions")]\n'
        f"    operations = [{operations}]\n"
    )


FILL = """MigrateValues("Sample", fill, version="1.1.0", migration="0003_fill",
                        reason="Copy the sample label into the notes.")"""
FILL_FN = 'def fill(row):\n    return {"notes": row.name}\n'


def test_after_cannot_explain_an_unrecorded_fill(versioned: Path, _postgres_url: str) -> None:
    backfill_migration(
        versioned,
        'migrations.RunSQL("UPDATE acme__lab_sample SET notes = name", migrations.RunSQL.noop)',
    )
    (versioned / "tests" / "weighed.yaml").write_text(
        WEIGHED + "upgrade: true\nafter: { sample: [{ notes: S1 }] }\n"
    )
    result = upgrade(versioned, _postgres_url, "-k", "none", "--no-rollback")
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'SMP-000001\'s notes is "S1", but its revision 2 holds null.' in result.stdout


def test_an_empty_history_table_keeps_its_guard_throughout_a_migration(
    lab: Path, database: str
) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    first = build_to(lab.parent, "v1", "lab")
    deployed(first, database)
    backfill_migration(
        lab,
        "migrations.RunPython(change_guard, migrations.RunPython.noop)",
        "def change_guard(apps, editor):\n"
        "    for action in ('DISABLE', 'ENABLE'):\n"
        "        editor.execute(f'ALTER TABLE acme__lab_sample__history {action} "
        "TRIGGER iki_history_no_truncate')\n",
    )
    second = build_to(lab.parent, "v2", "lab")
    assert "append-only guard iki_history_no_truncate" in refused(second, database)
    assert applied(database, "acme__lab") == ["0001_initial"]


def test_upgrade_checks_the_value_of_a_recorded_fill(versioned: Path, _postgres_url: str) -> None:
    backfill_migration(versioned, FILL, FILL_FN)
    fixture = versioned / "tests" / "weighed.yaml"
    for expected, message in [
        ("", "expected notes in after"),
        ("after: { sample: [{ notes: mistake }] }", "after expects notes"),
        (
            "after: { sample: [{ notes: S1 }] }",
            "upgraded: the samples and their history are intact",
        ),
    ]:
        fixture.write_text(WEIGHED + "upgrade: true\n" + expected + "\n")
        result = upgrade(versioned, _postgres_url, "-k", "none", "--no-rollback")
        assert message in result.stdout, result.stdout + result.stderr
        assert result.returncode == (0 if "S1" in expected else 1)


def test_a_manifest_default_is_enough_for_a_new_field(versioned: Path, _postgres_url: str) -> None:
    write_sample(
        versioned,
        ("  count: { type: int }", "  count: { type: int }\n  priority: { type: int, default: 3 }"),
        ("schema_version: 1", "schema_version: 2"),
    )
    made("lab", "--name", "priority", "--no-input", cwd=versioned.parent)
    result = upgrade(versioned, _postgres_url, "-k", "none")
    assert result.returncode == 0, result.stdout + result.stderr


def test_deploy_preserves_the_history_table_of_an_inherited_field(lab: Path, database: str) -> None:
    child = lab / "types" / "aliquot.yaml"
    original = """\
apiVersion: iki/v1
kind: EntityType
name: Aliquot
extends: Sample
schema_version: 1
hrid: { prefix: ALQ }
fields:
  label: { type: string }
"""
    child.write_text(original)
    made("lab", "--no-input", cwd=lab.parent)
    first = build_to(lab.parent, "v1", "lab")
    deployed(first, database)
    orm(
        first,
        database,
        """
        from iki_server.accounts.models import User
        from iki_server.entities import services
        from iki_server.org.models import ProjectMembership, ProjectRole
        user = User.objects.create_user("a@example.com", "A")
        ProjectMembership.objects.create(project=project, user=user, role=ProjectRole.ADMIN)
        record = services.create(user, "acme/lab.Aliquot", project, {"name": "S1"}).entity
        services.update(user, record, {"label": "original"}, expected_revision=1)
        result = None
    """,
    )
    child.write_text(original.replace("  label:", "  annotation:"))
    made("lab", "--name", "rename_label", cwd=lab.parent, answers="y\n")
    write_sample(
        lab, ("  count: { type: int }", "  count: { type: int }\n  label: { type: string }")
    )
    made("lab", "--name", "sample_label", "--no-input", cwd=lab.parent)
    second = build_to(lab.parent, "v2", "lab")
    problem = refused(second, database)
    assert "acme/lab.Aliquot.label and its parent acme/lab.Sample.label" in problem, problem
    assert "different history tables" in problem
    assert applied(database, "acme__lab") == ["0001_initial"]
    assert query(database, "SELECT label FROM acme__lab_aliquot__history WHERE revision=2") == [
        ("original",)
    ]


def test_a_reverse_backfill_records_the_value_it_restores(
    versioned: Path, _postgres_url: str
) -> None:
    backfill_migration(
        versioned,
        'MigrateValues("Sample", increase, backwards=decrease, version="1.1.0", '
        'migration="0003_fill", reason="Adjust the tube count.")',
        'def increase(row):\n    return {"count": row.count + 1}\n'
        'def decrease(row):\n    return {"count": row.count - 1}\n',
    )
    (versioned / "tests" / "weighed.yaml").write_text(
        WEIGHED + "upgrade: true\nafter: { sample: [{ count: 4 }] }\n"
    )
    result = upgrade(versioned, _postgres_url, "-k", "none")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "rolled back: the samples have their values" in result.stdout


@pytest.mark.parametrize(
    "rewrite", ["existing", "new", "history", "both", "append", "recorded", "reverse"]
)
def test_deploy_preserves_and_attributes_migrated_values(
    lab: Path, database: str, rewrite: str
) -> None:
    made("lab", "--no-input", cwd=lab.parent)
    first = build_to(lab.parent, "v1", "lab")
    deployed(first, database)
    orm(
        first,
        database,
        """
        from iki_server.accounts.models import User
        from iki_server.entities import services
        from iki_server.org.models import ProjectMembership, ProjectRole
        user = User.objects.create_user("a@example.com", "A")
        ProjectMembership.objects.create(project=project, user=user, role=ProjectRole.ADMIN)
        record = services.create(user, "acme/lab.Sample", project, {"name": "S1"}).entity
        services.update(user, record, {"count": 3}, expected_revision=1)
        result = None
    """,
    )
    snippets = {
        "existing": "UPDATE acme__lab_sample SET count = 99",
        "new": "UPDATE acme__lab_sample SET notes = 'undeclared'",
        "both": "ALTER TABLE acme__lab_sample__history DISABLE TRIGGER iki_history_no_change; "
        "UPDATE acme__lab_sample SET count = 99; "
        "UPDATE acme__lab_sample__history SET count = 99; "
        "ALTER TABLE acme__lab_sample__history ENABLE TRIGGER iki_history_no_change",
        "history": "UPDATE acme__lab_sample SET count = 99; "
        "UPDATE acme__lab_sample__history SET count = 99",
    }
    if rewrite == "recorded":
        backfill_migration(lab, FILL, FILL_FN)
    elif rewrite == "append":
        backfill_migration(
            lab,
            "migrations.RunPython(copy_snapshot, migrations.RunPython.noop)",
            "from django.db import transaction\n"
            "def copy_snapshot(apps, editor):\n"
            "    with transaction.atomic():\n"
            "        history = apps.get_model('acme__lab', 'Sample_History').objects\n"
            "        latest = history.order_by('-revision').values().first()\n"
            "        history.create(**(latest | {'revision': 3}))\n",
        )
    elif rewrite == "reverse":
        backfill_migration(
            lab,
            'migrations.RunSQL(migrations.RunSQL.noop, "UPDATE acme__lab_sample SET count = 99")',
        )
    else:
        backfill_migration(lab, f"migrations.RunSQL({snippets[rewrite]!r}, migrations.RunSQL.noop)")
    second = build_to(lab.parent, "v2", "lab")
    if rewrite == "recorded":
        deployed(second, database)
        [(reason,)] = query(
            database,
            "SELECT reason FROM iki_revision WHERE kind='migrated' "
            "AND number=3 AND via='system' AND user_id IS NULL "
            "AND changed_fields=ARRAY['notes']::varchar[] AND history_seen=iki_history_seen()",
        )
        assert all(word in reason for word in ("acme/lab", "1.1.0", "0003_fill", "Copy the sample"))
        assert query(database, "SELECT notes FROM acme__lab_sample__history ORDER BY revision") == [
            (None,),
            (None,),
            ("S1",),
        ]
        assert query(
            database,
            "SELECT data->>'reason' FROM iki_audit_event "
            "WHERE action='entity.migrated' AND via='system' AND target_label='SMP-000001'",
        ) == [(reason,)]
        assert run(
            second,
            database,
            """
            from django.test import Client
            from iki_server.accounts.models import User
            client = Client()
            client.force_login(User.objects.get(email="a@example.com"))
            base = "/api/v1/entities/SMP-000001/"
            revision = client.get(base + "revisions/3", secure=True).json()
            provenance = client.get(base + "provenance", secure=True).json()
            result = [revision["kind"], revision["changes"], provenance["notes"]["number"]]
        """,
        ) == ["migrated", {"notes": {"before": None, "after": "S1"}}, 3]
        deployed(second, database)
        assert query(database, "SELECT count(*) FROM iki_revision") == [(3,)]
    else:
        if rewrite == "reverse":
            deployed(second, database)
        problem = refused(first if rewrite == "reverse" else second, database)
        if rewrite == "history":
            assert "append-only" in problem, problem
        elif rewrite == "both":
            assert "changed acme__lab_sample__history's append-only guard" in problem, problem
        elif rewrite == "append":
            assert (
                "revision 3 in acme__lab_sample__history was appended by a raw migration" in problem
            )
        else:
            field = "notes" if rewrite == "new" else "count"
            assert f"SMP-000001's {field} is" in problem and "its revision 2 holds" in problem, (
                problem
            )
        assert query(database, "SELECT count FROM acme__lab_sample") == [(3,)]
        assert applied(database, "acme__lab") == (
            ["0001_initial", "0002_notes", "0003_fill"]
            if rewrite == "reverse"
            else ["0001_initial"]
        )


def test_an_upgrade_needs_a_commit_and_a_database(versioned: Path, _postgres_url: str) -> None:
    result = iki("plugin", "test", versioned, "--upgrade-from", "main", "-k", "none", cwd=versioned)
    assert result.returncode == 1
    assert "an upgrade test needs PostgreSQL" in result.stderr
    result = iki(
        "plugin",
        "test",
        versioned,
        "--upgrade-from",
        "v9",
        "--database",
        _postgres_url,
        "-k",
        "none",
        cwd=versioned,
    )
    assert result.returncode == 1
    assert "v9 isn't a commit of the git repository" in result.stderr
    # A pull request that adds a plugin has nothing to upgrade from.
    added = versioned.parent / "lab_two"
    shutil.copytree(versioned, added)
    result = iki(
        "plugin",
        "test",
        added,
        "--upgrade-from",
        "main",
        "--database",
        _postgres_url,
        "-k",
        "none",
        cwd=added,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "acme/lab isn't at main (" in result.stdout
    assert "there's nothing to upgrade." in result.stdout
    # A commit from before plugins had namespaces has nothing to upgrade from: no deployment
    # ran it (ADR 0113).
    manifest = versioned / "plugin.yaml"
    manifest.write_text(manifest.read_text().replace("name: acme/lab", "name: lab"))
    git(versioned.parent, "commit", "-q", "-am", "before namespaces")
    manifest.write_text(manifest.read_text().replace("name: lab", "name: acme/lab"))
    result = upgrade(versioned, _postgres_url, "-k", "none")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "acme/lab wasn't at main (" in result.stdout
    assert "it was `lab`, from before plugins had namespaces (ADR 0113)" in result.stdout
