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
        from iki_plugin_acme__example.models import Ext_iki_dna_DNASequence
        from iki_server.accounts.models import User
        from iki_server.entities import services
        from iki_server.entities.models import Entity

        from iki_server.org.models import ProjectMembership, ProjectRole

        alice = User.objects.create_user("alice@example.com")
        ProjectMembership.objects.create(project=project, user=alice, role=ProjectRole.EDITOR)

        def make(type_name, **values):
            return services.create(alice, type_name, project, values).entity

        # Each type with an identity is registered (ADR 0115): an entry, and the project's
        # record of it. References hold the entry.
        puc = make(
            "iki/dna.DNASequence", bases="ACGT", topology="circular", name="pUC19",
            construct_id="PUC-19",
            features=[{"type": "rep_origin", "start": 1, "end": 4, "strand": "forward"}],
        )
        primer = make("iki/dna.Primer", bases="ACG", target=str(puc.pk))
        assert Ext_iki_dna_DNASequence.objects.get(entity_id=puc.pk).construct_id == "PUC-19"
        sample = make("acme/example.Sample", name="S1", kind="dna", construct=str(puc.pk))
        bla = make(
            "iki/protein.Protein", sequence="MSIQHFRVAL", name="bla (+1)",
            encoded_by=str(puc.pk), uniprot_accession="P62593",
        )
        # A chain's numbering is its project's record's, as the scheme a run chose gives it:
        # numbering it again, in another scheme, replaces it. What its sequence alone gives
        # (its chain type) is the entry's.
        from iki_plugin_iki__antibody.models import (
            Ext_iki_protein_Protein, Ext_iki_protein_Protein_Entry,
        )
        for scheme, h1 in (("imgt", "GFNIKDTY"), ("kabat", "DTYIH")):
            bla.refresh_from_db()
            services.update(
                alice, bla,
                {"chain_type": "heavy", "numbering_scheme": scheme, "cdr_definition": scheme,
                 "cdr_h1": h1},
                expected_revision=bla.revision, by_run=True,
            )
        numbered = Ext_iki_protein_Protein.objects.get(entity_id=bla.pk)
        numbering = [
            numbered.numbering_scheme, numbered.cdr_definition, numbered.cdr_h1,
            Ext_iki_protein_Protein_Entry.objects.get(entity_id=bla.entry_id).chain_type,
        ]
        vhh = make("iki/antibody.Antibody", heavy_chain=str(bla.pk), format="vhh", name="anti-bla")
        light = make("iki/protein.Protein", sequence="DIQMTQSP")
        aspirin = make(
            "iki/small_molecule.Compound", smiles="CC(=O)Oc1ccccc1C(=O)O",
            inchikey="BSYNRYMUTXBXSQ-UHFFFAOYSA-N", name="aspirin", pubchem_cid="2244",
        )
        lot = make(
            "iki/small_molecule.CompoundBatch", compound=str(aspirin.pk), salt="sodium",
            salt_equivalents=1.0,
        )
        fab = make(
            "iki/antibody.Antibody", heavy_chain=str(bla.pk), light_chain=str(light.pk),
            format="fab", isotype="igg1",
        )
        # Its isotype is a registry fact: the same chains claimed as another isotype are
        # refused, not dropped, even where the project holds the antibody already.
        try:
            make(
                "iki/antibody.Antibody", heavy_chain=str(bla.pk), light_chain=str(light.pk),
                format="fab", isotype="igg4",
            )
            other_isotype = None
        except services.Invalid as invalid:
            other_isotype = sorted(invalid.errors)
        entries = {e.pk: e for e in Entity.objects.filter(registry__isnull=False)}
        # Giving the VHH a light chain would make it another antibody: it's refused.
        try:
            services.update(
                alice, vhh, {"light_chain": str(light.pk)}, expected_revision=vhh.revision,
            )
            refused = None
        except services.Invalid as invalid:
            refused = invalid.errors
        vhh.refresh_from_db()
        fab.refresh_from_db()
        aspirin.refresh_from_db()
        bla.refresh_from_db()
        light.refresh_from_db()
        result = {"types": [puc.type, primer.type, sample.type, bla.type, vhh.type,
                            aspirin.type, lot.type],
                  "hrids": [puc.hrid, primer.hrid, sample.hrid, bla.hrid, vhh.hrid,
                            aspirin.hrid, lot.hrid],
                  "aspirin": entries[aspirin.entry_id].identity_hash,
                  "record": aspirin.identity_hash,
                  "heavy": str(bla.entry_id), "light": str(light.entry_id),
                  "identities": [
                      entries[vhh.entry_id].identity_hash, entries[fab.entry_id].identity_hash
                  ],
                  "refused": refused, "numbering": numbering,
                  "other_isotype": other_isotype}
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
    # A compound is its standard InChIKey, its entry's.
    assert result["record"] is None
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
    assert result["numbering"] == ["kabat", "kabat", "DTYIH", "heavy"]
    assert result["other_isotype"] == ["isotype"]
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
