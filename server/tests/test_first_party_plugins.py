"""Iki's first-party plugins (plugins/) and the plugin template are valid, current and migrate.

Each is checked the way its own CI and a deployment's build check it: valid
against the plugins it depends on, with migrations that match its manifests,
and migrating a database forwards and back.
"""

from pathlib import Path

import pytest
from test_plugin_migrations import database, iki, migrate, orm, query, sync

from iki_spec.identity import identity_hash

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ROOT / "plugins"
TEMPLATE = ROOT / "plugin-template"
#: Each plugin (a directory) and the plugins it depends on.
FIRST_PARTY = {
    PLUGINS / "dna": (),
    PLUGINS / "protein": (PLUGINS / "dna",),
    PLUGINS / "antibody": (PLUGINS / "dna", PLUGINS / "protein"),
    PLUGINS / "small_molecule": (),
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
        "plugin",
        "build",
        PLUGINS / "dna",
        PLUGINS / "protein",
        PLUGINS / "antibody",
        PLUGINS / "small_molecule",
        TEMPLATE,
        "--output",
        out,
        cwd=ROOT,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(out, database)
    sync(out, database)
    result = orm(
        out,
        database,
        """
        from iki_plugin_iki__dna.models import DNASequence, Primer
        from iki_plugin_acme__example.models import Ext_iki_dna_DNASequence, Sample
        from iki_plugin_iki__protein.models import Protein
        from iki_plugin_iki__antibody.models import Antibody
        from iki_plugin_iki__small_molecule.models import Compound, CompoundBatch

        puc = DNASequence.objects.create(
            owner_project=project, bases="ACGT", topology="circular", name="pUC19",
            features=[{"type": "rep_origin", "start": 1, "end": 4, "strand": "forward"}],
        )
        primer = Primer.objects.create(owner_project=project, bases="ACG", target=puc)
        Ext_iki_dna_DNASequence.objects.create(entity_id=puc.pk, construct_id="PUC-19")
        sample = Sample.objects.create(owner_project=project, name="S1", kind="dna", construct=puc)
        bla = Protein.objects.create(
            owner_project=project, sequence="MSIQHFRVAL", name="bla (+1)", encoded_by=puc,
            uniprot_accession="P62593",
        )
        vhh = Antibody.objects.create(
            owner_project=project, heavy_chain=bla, format="vhh", name="anti-bla",
        )
        light = Protein.objects.create(owner_project=project, sequence="DIQMTQSP")
        aspirin = Compound.objects.create(
            owner_project=project, smiles="CC(=O)Oc1ccccc1C(=O)O",
            inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N", name="aspirin", pubchem_cid="2244",
        )
        lot = CompoundBatch.objects.create(
            owner_project=project, compound=aspirin, salt="sodium", salt_equivalents=1.0,
        )
        fab = Antibody.objects.create(
            owner_project=project, heavy_chain=bla, light_chain=light, format="fab",
        )
        # Giving the VHH a light chain would make it another antibody: it's refused.
        from iki_server.accounts.models import User
        from iki_server.entities import services
        try:
            services.update(
                User.objects.create_user("alice@example.com"), vhh,
                {"light_chain": str(light.pk)}, expected_revision=vhh.revision,
            )
            refused = None
        except services.Invalid as invalid:
            refused = invalid.errors
        result = {"types": [puc.type, primer.type, sample.type, bla.type, vhh.type,
                            aspirin.type, lot.type],
                  "hrids": [puc.hrid, primer.hrid, sample.hrid, bla.hrid, vhh.hrid,
                            aspirin.hrid, lot.hrid],
                  "aspirin": aspirin.identity_hash,
                  "heavy": str(bla.pk), "light": str(light.pk),
                  "identities": [vhh.identity_hash, fab.identity_hash], "refused": refused}
        """,
    )
    assert result["types"] == [
        "iki/dna.DNASequence",
        "iki/dna.Primer",
        "acme/example.Sample",
        "iki/protein.Protein",
        "iki/antibody.Antibody",
        "iki/small_molecule.Compound",
        "iki/small_molecule.CompoundBatch",
    ]
    assert [h.split("-")[0] for h in result["hrids"]] == [
        "DNA",
        "PRM",
        "SMP",
        "PRT",
        "ABY",
        "CPD",
        "LOT",
    ]
    # A compound is its standard InChIKey.
    assert result["aspirin"] == identity_hash(
        "iki/small_molecule.Compound", {"inchikey": ("string", "BSYNRYMUTXBXSQ-UHFFFAOYSA-N")}
    )
    # An antibody is its chains, its light chain if it has one (a VHH hasn't).
    chains = {"heavy_chain": ("ref", result["heavy"]), "light_chain": ("ref", None)}
    paired = {**chains, "light_chain": ("ref", result["light"])}
    assert result["identities"] == [
        identity_hash("iki/antibody.Antibody", chains),
        identity_hash("iki/antibody.Antibody", paired),
    ]
    assert result["refused"] == {
        "light_chain": [
            "is part of what makes it this one, so it can't be added later: "
            "register another with it instead."
        ]
    }
    for app in (
        "iki__small_molecule",
        "iki__antibody",
        "acme__example",
        "iki__protein",
        "iki__dna",
    ):
        migrate(out, database, app, "zero")
    left = query(
        database,
        "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND "
        "(tablename LIKE 'dna%%' OR tablename LIKE 'example%%' OR tablename LIKE 'protein%%' "
        "OR tablename LIKE 'antibody%%' OR tablename LIKE 'small_molecule%%')",
    )
    assert left == [(0,)]
