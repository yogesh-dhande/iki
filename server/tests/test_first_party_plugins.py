"""Iki's first-party plugins (plugins/) and the plugin template are valid, current and migrate.

Each is checked the way its own CI and a deployment's build check it: valid
against the plugins it depends on, with migrations that match its manifests,
and migrating a database forwards and back.
"""

import uuid
from pathlib import Path

import pytest
from test_plugin_migrations import database, iki, migrate, orm, query, sync

from iki_spec.identity import Kind, identity_hash

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ROOT / "plugins"
TEMPLATE = ROOT / "plugin-template"
#: Each plugin (a directory) and the plugins it depends on.
FIRST_PARTY = {
    PLUGINS / "dna": (),
    PLUGINS / "protein": (PLUGINS / "dna",),
    PLUGINS / "antibody": (PLUGINS / "dna", PLUGINS / "protein"),
    PLUGINS / "small_molecule": (),
    PLUGINS / "oligo": (),
    PLUGINS / "plasmid": (PLUGINS / "dna",),
    PLUGINS / "complex": (PLUGINS / "dna", PLUGINS / "protein"),
    PLUGINS / "lots": (
        PLUGINS / "dna",
        PLUGINS / "protein",
        PLUGINS / "oligo",
        PLUGINS / "plasmid",
    ),
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
        PLUGINS / "oligo",
        PLUGINS / "plasmid",
        PLUGINS / "complex",
        PLUGINS / "lots",
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
        # An antibody is a complex of its chains (ADR 0161): its parts, with their copies.
        def parts(heavy, light=None, copies=1):
            rows = [{"part": str(heavy.pk), "copies": copies, "role": "heavy"}]
            if light is not None:
                rows.append({"part": str(light.pk), "copies": copies, "role": "light"})
            return rows
        vhh = make("iki/antibody.Antibody", components=parts(bla), format="vhh", name="anti-bla")
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
            "iki/antibody.Antibody", components=parts(bla, light), format="fab", isotype="igg1",
        )
        # Its isotype is a registry fact: the same chains claimed as another isotype are
        # refused, not dropped, even where the project holds the antibody already.
        try:
            make(
                "iki/antibody.Antibody", components=parts(bla, light), format="fab",
                isotype="igg4",
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
            refusal(components=parts(other, light), format="fv", isotype="igg1"),
            refusal(components=parts(other, light, 2), format="igg", isotype="igm"),
            refusal(components=parts(other, copies=2), format="igg", isotype="igg1"),
            refusal(components=parts(other, light), format="scfv"),
            refusal(components=parts(other, other), format="fv"),
            refusal(components=parts(other, light), format="igg"),
            refusal(components=[*parts(other), {"part": str(light.pk), "role": "linker"}],
                    format="other"),
        ]
        whole_igm = refusal(components=parts(other, light, 2), format="other", isotype="igm")
        # Every complex type is a complex (ADR 0161): one molecule is one entry whichever
        # type registers it, and searching iki/core.Complex finds every kind.
        try:
            make("iki/complex.ProteinComplex", components=[
                {"part": str(bla.pk), "copies": 1, "role": "alpha"},
                {"part": str(light.pk), "copies": 1, "role": "beta"},
            ])
            as_another = None
        except services.Invalid as invalid:
            as_another = invalid.errors
        dimer = make(
            "iki/complex.ProteinComplex", name="a dimer", complex_portal_id="CPX-1",
            components=[{"part": str(other.pk), "copies": 2, "role": "subunit"}],
        )
        from iki_server.entities.search import Search, queryset
        complexes = sorted(
            e.type for e in queryset(alice, Search(type="iki/core.Complex", project=project.pk))
        )
        from iki_server.graph.models import Edge
        part_of = sorted(
            Edge.objects.filter(on_id=other.entry_id, field="components.part")
            .values_list("node__type", flat=True)
        )
        entries = {e.pk: e for e in Entity.objects.filter(registry__isnull=False)}
        # Giving the VHH a light chain would make it another antibody: it's refused.
        try:
            services.update(
                alice, vhh, {"components": parts(bla, light)}, expected_revision=vhh.revision,
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
                  "whole_igm": whole_igm, "as_another": as_another,
                  "dimer": [dimer.type, dimer.hrid.split("-")[0]],
                  "complexes": complexes, "part_of": part_of, "fab": fab.hrid}
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
    # An antibody is a complex (ADR 0161): its parts and their copies, whatever their roles,
    # as iki/core.Complex declares the identity.
    rows = Kind("list", items=Kind("row", row=(("part", Kind("ref")), ("copies", Kind("int")))))
    heavy, light = uuid.UUID(result["heavy"]), uuid.UUID(result["light"])
    assert result["identities"] == [
        identity_hash("iki/core.Complex", {"components": (rows, [{"part": heavy, "copies": 1}])}),
        identity_hash(
            "iki/core.Complex",
            {"components": (rows, [{"part": heavy, "copies": 1}, {"part": light, "copies": 1}])},
        ),
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
            "components": [
                "An IgG, a Fab and an Fv have a light chain: give one, or choose vhh or "
                "scfv for an antibody of one chain."
            ]
        },
        {"components": ["A VHH and an scFv have one chain, so no light chain."]},
        # One part twice is one part with two copies (ADR 0161).
        {
            "components/1/part": [
                "lists PRT-000003 again: give each part once, with how many copies of it there are."
            ]
        },
        {
            "components": [
                "A whole IgG has two copies of its heavy chain and two of its light chain: "
                "give copies 2 for each."
            ]
        },
        # An antibody's roles are all its parts may have (ADR 0164).
        {"components/1/role": ["isn't one of its roles: heavy, light."]},
    ]
    # A whole IgM is `other`, which may take any class.
    assert result["whole_igm"] is None
    # The Fab's parts and copies, registered as a protein complex, are the Fab.
    assert result["as_another"] == {
        "type": [
            f"This is registered already, as a iki/antibody.Antibody ({result['fab']}): "
            "register it as one."
        ]
    }
    assert result["dimer"] == ["iki/complex.ProteinComplex", "PCX"]
    # The VHH, the Fab and the whole IgM; the dimer.
    assert result["complexes"] == [
        "iki/antibody.Antibody",
        "iki/antibody.Antibody",
        "iki/antibody.Antibody",
        "iki/complex.ProteinComplex",
    ]
    # The graph finds what a chain is part of, whatever type of complex (ADR 0161): its
    # other heavy chain is the whole IgM's and the dimer's.
    assert result["part_of"] == ["iki/antibody.Antibody", "iki/complex.ProteinComplex"]
    # Its parts are its identity: another chain is another antibody.
    assert result["refused"] == {"components": ["can't change once it's set."]}
    for app in (
        "iki__lots",
        "iki__plasmid",
        "iki__complex",
        "iki__oligo",
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
        "OR tablename LIKE 'antibody%%' OR tablename LIKE 'small_molecule%%' "
        "OR tablename LIKE 'oligo%%' OR tablename LIKE 'plasmid%%' "
        "OR tablename LIKE 'complex%%' "
        "OR tablename LIKE 'iki\\_\\_lots%%')",
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


#: pGKL1 (INSDC X01095.1), the linear killer plasmid of Kluyveromyces lactis, as the
#: record in ``server/tests/data/plasmids`` gives its bases.
PGKL1 = ROOT / "server" / "tests" / "data" / "plasmids" / "X01095.1.gb"


def genbank_bases(path: Path) -> str:
    """A GenBank record's bases, from its ORIGIN."""
    out, reading = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("ORIGIN"):
            reading = True
        elif line.startswith("//"):
            break
        elif reading:
            out.append("".join(c for c in line if c.isalpha()))
    return "".join(out).upper()


def test_a_linear_plasmid_is_one_molecule_from_either_strand_and_not_from_another_origin(
    tmp_path: Path, database: str
) -> None:
    """ADR 0168: a plasmid's topology is part of what it is. pGKL1 (X01095.1), a real
    linear plasmid, registered again as its other strand finds its registry entry, so it
    is one plasmid; rotated it is another molecule, since a linear molecule's two ends
    are its own and a rotation puts them elsewhere; and the same bases as a circle are
    another molecule again, with a plasmid of their own.

    The plasmid's own topology is its entry's, derived from its sequence: a run fills it,
    which is a revision of the record like any other, and a run that writes a topology
    its sequence doesn't have is refused by the type's rule.
    """
    out = tmp_path / "build"
    built = iki("plugin", "build", PLUGINS / "dna", PLUGINS / "plasmid", "--output", out, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(out, database)
    sync(out, database)
    bases = genbank_bases(PGKL1)
    assert len(bases) == 8874
    other_strand = bases.translate(str.maketrans("ACGT", "TGCA"))[::-1]
    rotated = bases[202:] + bases[:202]  # past its terminal inverted repeat
    result = orm(
        out,
        database,
        f"""
        from iki_server.accounts.models import User
        from iki_server.entities import services
        from iki_server.org.models import Project, ProjectMembership, ProjectRole

        alice = User.objects.create_user("alice@example.com")
        others = [Project.objects.create(team=team, name=f"Plasmids {{n}}") for n in (1, 2, 3)]
        for p in [project, *others]:
            ProjectMembership.objects.create(project=p, user=alice, role=ProjectRole.EDITOR)

        def errors(do):
            try:
                do()
            except services.Invalid as invalid:
                return invalid.errors
            return None

        def plasmid(where, bases, topology, name):
            sequence = services.create(
                alice, "iki/dna.DNASequence", where,
                {{"bases": bases, "topology": topology, "strandedness": "double"}},
            )
            made = services.create(
                alice, "iki/plasmid.Plasmid", where,
                {{"sequence": str(sequence.entity.entry_id), "name": name}},
            )
            return sequence, made

        first_sequence, first = plasmid(project, {bases!r}, "linear", "pGKL1")
        # Its other strand, 5' to 3': the same molecule, so the same entry.
        flipped_sequence, flipped = plasmid(others[0], {other_strand!r}, "linear", "pGKL1")
        # From its 203rd base: a linear molecule with its ends somewhere else.
        rotated_sequence, rotated = plasmid(others[1], {rotated!r}, "linear", "pGKL1 rotated")
        # The same bases as a circle: another molecule, and another plasmid.
        circle_sequence, circle = plasmid(others[2], {bases!r}, "circular", "pGKL1 as a circle")

        # A run fills the plasmid's topology, which is its registry entry's: that write is
        # a revision of the entry, audited like any other, and its value is then every
        # project's. The project's own record doesn't change, since the field isn't its.
        before = first.entity.revision
        saved = services.update(
            alice, first.entity, {{"topology": "linear"}},
            expected_revision=before, by_run=True,
        )
        first.entity.refresh_from_db()
        filled = saved.entry_revision

        # A run that writes a topology the sequence doesn't have is refused by the rule.
        wrong = errors(lambda: services.update(
            alice, circle.entity, {{"topology": "linear"}},
            expected_revision=circle.entity.revision, by_run=True,
        ))

        def values(saved):
            return services.current_values_of([saved.entity])[saved.entity.pk]

        result = {{
            "sequences": [
                str(s.entity.entry_id)
                for s in (first_sequence, flipped_sequence, rotated_sequence, circle_sequence)
            ],
            "plasmids": [
                str(s.entity.entry_id) for s in (first, flipped, rotated, circle)
            ],
            "hrids": [s.entity.hrid for s in (first, flipped, rotated, circle)],
            "registered": [s.registered for s in (first, flipped, rotated, circle)],
            "topology": values(first)["topology"],
            "filled": {{
                "number": filled.number,
                "via": filled.via,
                "kind": filled.kind,
                "changed": filled.changed_fields,
                "by": filled.user.email,
            }},
            "record_revision": [before, first.entity.revision],
            "wrong": wrong,
        }}
        """,
    )
    one, flipped, rotated_entry, circle = result["sequences"]
    assert one == flipped != rotated_entry
    assert circle not in (one, rotated_entry)
    # One plasmid for one molecule: the other strand finds it, a rotation and the circle don't.
    plasmids = result["plasmids"]
    assert plasmids[0] == plasmids[1]
    assert len({*plasmids}) == 3
    assert result["hrids"][0] == result["hrids"][1] == "PLS-000001"
    assert result["registered"] == [True, False, True, True]
    # The run's fill is a revision of the registry entry, recorded as a run's and by whom;
    # the project's record of it is unchanged, since topology isn't the record's.
    assert result["topology"] == "linear"
    assert result["filled"]["number"] == 2
    assert result["filled"]["via"] == "run"
    assert result["filled"]["changed"] == ["topology"]
    assert result["filled"]["by"] == "alice@example.com"
    assert result["record_revision"] == [1, 1]
    # And a wrong topology is refused, naming the field and saying why.
    assert list(result["wrong"]) == ["topology"]
    assert "the plasmid's topology is that" in result["wrong"]["topology"][0]


SEQUENCES = ROOT / "spec" / "tests" / "sequences"


def fasta(accession: str) -> str:
    return "".join((SEQUENCES / f"{accession}.fasta").read_text().splitlines()[1:])


def test_one_molecule_in_several_databases_records_keeps_each_projects_accession(
    tmp_path: Path, database: str
) -> None:
    """ADR 0154: an accession names the database record a sequence came from, and one
    molecule is several records. pUC19 is GenBank's L09137.2 and M77789.2 (that one from
    its other strand, from another origin), calmodulin UniProtKB's P0DP23 (CALM1) and
    P0DP24 (CALM2), and histone H4 the human P62805 and the pig P62802. Each pair is one
    registry entry, and registering the second is refused nowhere: each project's record
    keeps the accession it gave."""
    out = tmp_path / "build"
    built = iki("plugin", "build", PLUGINS / "dna", PLUGINS / "protein", "--output", out, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(out, database)
    sync(out, database)
    calmodulin = (
        "MADQLTEEQIAEFKEAFSLFDKDGDGTITTKELGTVMRSLGQNPTEAELQDMINEVDADGNGTIDFPEFLTMMARKMKDTDSEE"
        "EIREAFRVFDKDGNGYISAAELRHVMTNLGEKLTDEEVDEMIREADIDGDGQVNYEEFVQMMTAK"
    )
    histone_h4 = (
        "MSGRGKGGKGLGKGGAKRHRKVLRDNIQGITKPAIRRLARRGGVKRISGLIYEETRGVLKVFLENVIRDAVTYTEHAKRKTVT"
        "AMDVVYALKRQGRTLYGFGG"
    )
    assert (len(calmodulin), len(histone_h4)) == (149, 103)
    pairs = [
        (
            "iki/dna.DNASequence",
            "accession",
            {"topology": "circular"},
            (fasta("L09137.2"), "L09137.2"),
            (fasta("M77789.2"), "M77789.2"),
        ),
        (
            "iki/protein.Protein",
            "uniprot_accession",
            {},
            (calmodulin, "P0DP23"),
            (calmodulin, "P0DP24"),
        ),
        (
            "iki/protein.Protein",
            "uniprot_accession",
            {},
            (histone_h4, "P62805"),
            (histone_h4, "P62802"),
        ),
    ]
    result = orm(
        out,
        database,
        f"""
        from iki_server.accounts.models import User
        from iki_server.entities import services
        from iki_server.org.models import Project, ProjectMembership, ProjectRole

        alice = User.objects.create_user("alice@example.com")
        other = Project.objects.create(team=team, name="Vectors")
        for p in (project, other):
            ProjectMembership.objects.create(project=p, user=alice, role=ProjectRole.EDITOR)

        pairs = {pairs!r}
        sequence = {{"iki/dna.DNASequence": "bases", "iki/protein.Protein": "sequence"}}
        result = []
        for type_name, field, extra, (first, one), (second, two) in pairs:
            def make(where, given, accession):
                values = {{sequence[type_name]: given, field: accession, **extra}}
                return services.create(alice, type_name, where, values)

            a = make(project, first, one)
            b = make(other, second, two)
            # The project that has it already, given its other record: its own, as it was.
            again = make(project, second, two)
            def accession(saved):
                return services.current_values_of([saved.entity])[saved.entity.pk][field]
            result.append({{
                "entries": [str(s.entity.entry_id) for s in (a, b, again)],
                "registered": [s.registered for s in (a, b)],
                "reused": again.reused,
                "accessions": [accession(s) for s in (a, b, again)],
            }})
        """,
    )
    for found, (_, _, _, (_, one), (_, two)) in zip(result, pairs, strict=True):
        assert len(set(found["entries"])) == 1
        assert found["registered"] == [True, False]
        assert found["reused"] is True
        assert found["accessions"] == [one, two, one]


def test_an_oligonucleotide_is_its_canonical_helm(tmp_path: Path, database: str) -> None:
    """61e, ADR 0144 and 0147: one HELM written two ways is one registry entry, in any
    project; a 5′-biotin primer is another oligonucleotide from the plain one; a search by
    bases finds both, and Sequence's searches find them. A duplex is its strands by role,
    in either order."""
    out = tmp_path / "build"
    built = iki("plugin", "build", PLUGINS / "oligo", "--output", out, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(out, database)
    sync(out, database)
    result = orm(
        out,
        database,
        """
        from iki_server.accounts.models import User
        from iki_server.entities import search, services
        from iki_server.org.models import Project, ProjectMembership, ProjectRole

        alice = User.objects.create_user("alice@example.com")
        other = Project.objects.create(team=team, name="Probes")
        for p in (project, other):
            ProjectMembership.objects.create(project=p, user=alice, role=ProjectRole.EDITOR)

        def make(where, helm, bases):
            # Its bases are computed by a run: registered here as one would.
            return services.create(
                alice, "iki/oligo.Oligonucleotide", where, {"helm": helm, "bases": bases},
                by_run=True,
            )

        biotin = (
            "RNA1{P.[dR](A)P.[dR](C)P.[dR](G)P.[dR](T)}|CHEM1{[5BioTEG]}"
            "$RNA1,CHEM1,1:R1-1:R1$$$V2.0"
        )
        # The same molecule: the conjugate first, its brackets and an annotation kept.
        written = (
            'CHEM1{[5BioTEG]}|RNA1{[P].[dR]([A])P.[dR](C)P.[dR](G)P.[dR](T)}"5′-biotin"'
            "$CHEM1,RNA1,1:R1-1:R1$$$V2.0"
        )
        first = make(project, biotin, "ACGT")
        again = make(other, written, "ACGT")
        plain = make(project, "RNA1{[dR](A)P.[dR](C)P.[dR](G)P.[dR](T)}$$$$V2.0", "ACGT")
        other_bases = make(project, "RNA1{[dR](T)P.[dR](T)P.[dR](T)}$$$$V2.0", "TTT")
        try:
            make(project, "RNA1{[dR](A)P.[dR](C)", "AC")
            bad = None
        except services.Invalid as invalid:
            bad = invalid.errors

        def found(type_name, where):
            s = search.Search(type=type_name, project=project.pk, where=where)
            return sorted(str(e.pk) for e in search.queryset(alice, s))

        by_bases = found(
            "iki/oligo.Oligonucleotide", {"field": "bases", "op": "contains", "value": "acg"}
        )
        sequences = found("iki/core.Sequence", None)

        def duplex(rows):
            return services.create(alice, "iki/oligo.Duplex", project, {"strands": rows}).entity
        s, a = str(plain.entity.entry_id), str(other_bases.entity.entry_id)
        one = duplex([{"role": "sense", "strand": s}, {"role": "antisense", "strand": a}])
        two = duplex([{"role": "antisense", "strand": a}, {"role": "sense", "strand": s}])
        swapped = duplex([{"role": "sense", "strand": a}, {"role": "antisense", "strand": s}])

        result = {
            "entries": [str(x.entity.entry_id) for x in (first, again, plain)],
            "registered": [x.registered for x in (first, again, plain)],
            "by_bases": by_bases,
            "expected_by_bases": sorted(str(x.entity.pk) for x in (first, plain)),
            "sequences": sequences,
            "expected_sequences": sorted(str(x.entity.pk) for x in (first, plain, other_bases)),
            "bad": bad,
            "duplexes": [str(d.entry_id) for d in (one, two, swapped)],
        }
        """,
    )
    first, again, plain = result["entries"]
    assert first == again != plain
    assert result["registered"] == [True, False, True]
    assert result["by_bases"] == result["expected_by_bases"]
    assert result["sequences"] == result["expected_sequences"]
    assert list(result["bad"]) == ["helm"]
    assert "isn't HELM" in result["bad"]["helm"][0]
    one, two, swapped = result["duplexes"]
    assert one == two != swapped
