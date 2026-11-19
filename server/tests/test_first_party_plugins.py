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
        # What a format says of its chains and isotype holds however an antibody is
        # registered, not only by assemble: the plugin's rules (ADR 0127).
        def refusal(**values):
            try:
                make("iki/antibody.Antibody", **values)
            except services.Invalid as invalid:
                return invalid.errors
            return None
        other = make("iki/protein.Protein", sequence="EVQLVESG")
        format_rules = [
            refusal(heavy_chain=str(other.pk), light_chain=str(light.pk), format="fv",
                    isotype="igg1"),
            refusal(heavy_chain=str(other.pk), light_chain=str(light.pk), format="igg",
                    isotype="igm"),
            refusal(heavy_chain=str(other.pk), format="igg", isotype="igg1"),
            refusal(heavy_chain=str(other.pk), light_chain=str(light.pk), format="scfv"),
            refusal(heavy_chain=str(other.pk), light_chain=str(other.pk), format="fv"),
        ]
        whole_igm = refusal(heavy_chain=str(other.pk), light_chain=str(light.pk),
                            format="other", isotype="igm")
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
                  "other_isotype": other_isotype, "format_rules": format_rules,
                  "whole_igm": whole_igm}
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
    assert result["format_rules"] == [
        {
            "isotype": [
                "An Fv, scFv or VHH has no constant region, so no isotype: leave it out, "
                "or choose the format its chains are."
            ]
        },
        {
            "isotype": [
                "An IgG's isotype is one of its subclasses (igg1 to igg4), not igm: for a "
                "whole antibody of another class, choose other."
            ]
        },
        {
            "light_chain": [
                "An IgG, a Fab and an Fv have a light chain: give one, or choose vhh or "
                "scfv for an antibody of one chain."
            ]
        },
        {"light_chain": ["A VHH and an scFv have one chain, so no light chain."]},
        {"light_chain": ["One protein can't be both its heavy and its light chain."]},
    ]
    # A whole IgM is `other`, which may take any class.
    assert result["whole_igm"] is None
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


def test_a_plasmid_is_found_from_any_origin_and_on_either_strand(
    tmp_path: Path, database: str
) -> None:
    """ADR 0138, F-056: a circular duplex registered again rotated, or as its other strand,
    finds its registry entry, which keeps the bases as first registered; the record keeps
    them as given, and its features are placed along the entry's, each of a joined
    feature's parts too. A single-stranded circle with the same bases is another
    molecule."""
    out = tmp_path / "build"
    built = iki("plugin", "build", PLUGINS / "dna", "--output", out, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(out, database)
    sync(out, database)
    result = orm(
        out,
        database,
        """
        from iki_server.accounts.models import User
        from iki_server.entities import services
        from iki_server.entities.models import Revision
        from iki_server.org.models import Project, ProjectMembership, ProjectRole

        alice = User.objects.create_user("alice@example.com")
        others = [Project.objects.create(team=team, name=f"Plasmids {n}") for n in (1, 2, 3)]
        for p in [project, *others]:
            ProjectMembership.objects.create(project=p, user=alice, role=ProjectRole.EDITOR)

        def make(where, bases, **values):
            values = {"topology": "circular", "features": [ori, cds], **values}
            return services.create(alice, "iki/dna.DNASequence", where, {"bases": bases, **values})

        # AAGTCCGATT, with an origin of replication on its bases 3 to 6 (GTCC).
        ori = {"type": "rep_origin", "start": 3, "end": 6, "strand": "forward"}
        # And a joined feature across the origin: TT, AA, then TC (9..10, 1..2, 4..5).
        def joined(start, end, strand, location, *parts):
            return {
                "type": "CDS",
                "start": start,
                "end": end,
                "strand": strand,
                "parts": [{"start": a, "end": b, "strand": strand} for a, b in parts],
                "qualifiers": {"location": location},
            }

        cds = joined(9, 5, "forward", "join(9..10,1..2,4..5)", (9, 10), (1, 2), (4, 5))
        first = make(project, "aagtccgatt")
        # From its fifth base: CCGATTAAGT. Its ori is bases 9, 10, 1 and 2.
        ori = {"type": "rep_origin", "start": 9, "end": 2, "strand": "forward"}
        # Its joined feature is 5..8 (TTAA, one stretch here), then T at 10 and C at 1.
        cds = joined(5, 1, "forward", "join(5..8,10,1)", (5, 8), (10, 10), (1, 1))
        rotated = make(others[0], "CCGATTAAGT")
        # Its other strand, 5' to 3': AATCGGACTT. Its ori is GGAC, on the reverse strand.
        ori = {"type": "rep_origin", "start": 5, "end": 8, "strand": "reverse"}
        # Its joined feature is on the reverse strand, read 2..1, 10..9, then 7..6.
        cds = joined(
            6, 2, "reverse", "complement(join(6..7,9..10,1..2))", (1, 2), (9, 10), (6, 7)
        )
        flipped = make(others[1], "AATCGGACTT")
        # The same bases as one strand is another molecule.
        single = make(others[2], "AAGTCCGATT", strandedness="single")

        def submitted(saved):
            return Revision.objects.get(entity=saved.entity, number=1).submitted

        def values(saved):
            return services.current_values_of([saved.entity])[saved.entity.pk]

        result = {
            "entries": [str(s.entity.entry_id) for s in (first, rotated, flipped, single)],
            "registered": [s.registered for s in (first, rotated, flipped, single)],
            "bases": [values(s)["bases"] for s in (rotated, flipped)],
            "features": [values(s)["features"] for s in (rotated, flipped)],
            "submitted": [submitted(s) for s in (first, rotated, flipped)],
        }
        """,
    )
    first, rotated, flipped, single = result["entries"]
    assert first == rotated == flipped != single
    assert result["registered"] == [True, False, False, True]
    # The entry keeps its bases as first given, lower case and all.
    assert result["bases"] == ["aagtccgatt", "aagtccgatt"]
    ori = {"type": "rep_origin", "start": 3, "end": 6, "strand": "forward"}
    cds = {
        "type": "CDS",
        "start": 9,
        "end": 5,
        "strand": "forward",
        "parts": [
            {"start": 9, "end": 10, "strand": "forward"},
            {"start": 1, "end": 2, "strand": "forward"},
            {"start": 4, "end": 5, "strand": "forward"},
        ],
        "qualifiers": {"location": "join(9..10,1..2,4..5)"},
    }
    assert result["features"] == [[ori, cds], [ori, cds]]
    assert result["submitted"] == [None, {"bases": "CCGATTAAGT"}, {"bases": "AATCGGACTT"}]
