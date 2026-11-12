"""Iki's first-party plugins (plugins/) and the plugin template are valid, current and migrate.

Each is checked the way its own CI and a deployment's build check it: valid
against the plugins it depends on, with migrations that match its manifests,
and migrating a database forwards and back.
"""

from pathlib import Path

import pytest
from test_plugin_migrations import database, iki, migrate, orm, query, sync

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ROOT / "plugins"
TEMPLATE = ROOT / "plugin-template"
#: Each plugin (a directory) and the plugins it depends on.
FIRST_PARTY = {
    PLUGINS / "dna": (),
    PLUGINS / "protein": (PLUGINS / "dna",),
    TEMPLATE: (PLUGINS / "dna",),
}

__all__ = ["database"]


def withs(dependencies: tuple[Path, ...]) -> list[str]:
    return [arg for d in dependencies for arg in ("--with", str(d))]


@pytest.mark.parametrize("plugin", FIRST_PARTY, ids=lambda p: p.name)
def test_it_is_valid_and_its_migrations_are_current(plugin: Path) -> None:
    dependencies = FIRST_PARTY[plugin]
    checked = iki("plugin", "validate", plugin, *withs(dependencies), cwd=ROOT)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "is valid" in checked.stdout
    current = iki(
        "plugin", "makemigrations", plugin, *withs(dependencies), "--check", "--no-input", cwd=ROOT
    )
    assert current.returncode == 0, current.stdout + current.stderr


def test_they_migrate_forwards_and_back_and_store_entities(tmp_path: Path, database: str) -> None:
    out = tmp_path / "build"
    built = iki(
        "plugin", "build", PLUGINS / "dna", PLUGINS / "protein", TEMPLATE, "--output", out, cwd=ROOT
    )
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(out, database)
    sync(out, database)
    result = orm(
        out,
        database,
        """
        from iki_plugin_dna.models import DNASequence, Primer
        from iki_plugin_example.models import Ext_dna_DNASequence, Sample
        from iki_plugin_protein.models import Protein

        puc = DNASequence.objects.create(
            owner_project=project, bases="ACGT", topology="circular", name="pUC19",
            features=[{"type": "rep_origin", "start": 1, "end": 4, "strand": "forward"}],
        )
        primer = Primer.objects.create(owner_project=project, bases="ACG", target=puc)
        Ext_dna_DNASequence.objects.create(entity_id=puc.pk, construct_id="PUC-19")
        sample = Sample.objects.create(owner_project=project, name="S1", kind="dna", construct=puc)
        bla = Protein.objects.create(
            owner_project=project, sequence="MSIQHFRVAL", name="bla (+1)", encoded_by=puc,
            uniprot_accession="P62593",
        )
        result = {"types": [puc.type, primer.type, sample.type, bla.type],
                  "hrids": [puc.hrid, primer.hrid, sample.hrid, bla.hrid]}
        """,
    )
    assert result["types"] == ["dna.DNASequence", "dna.Primer", "example.Sample", "protein.Protein"]
    assert [h.split("-")[0] for h in result["hrids"]] == ["DNA", "PRM", "SMP", "PRT"]
    for app in ("example", "protein", "dna"):
        migrate(out, database, app, "zero")
    left = query(
        database,
        "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND "
        "(tablename LIKE 'dna%%' OR tablename LIKE 'example%%' OR tablename LIKE 'protein%%')",
    )
    assert left == [(0,)]
