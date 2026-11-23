"""Strain registration, glycerol stock custody, and material-to-data lineage (ADR 0185)."""

from pathlib import Path

from first_party_dependencies import PLUGINS, ROOT, build_order
from test_plugin_migrations import database, iki, migrate, orm, sync

__all__ = ["database"]


def test_strain_identity_and_stock_to_data_lineage(tmp_path: Path, database: str) -> None:
    out = tmp_path / "build"
    built = iki("plugin", "build", *build_order(PLUGINS / "strains"), "--output", out, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(out, database)
    sync(out, database)
    orm(
        out,
        database,
        """
        from concurrent.futures import ThreadPoolExecutor
        from decimal import Decimal
        from threading import Barrier

        import pytest
        from django.db import connections, transaction
        from iki_server.accounts.models import User
        from iki_server.amounts import services as amounts
        from iki_server.amounts.models import Amount, Movement
        from iki_server.entities import services
        from iki_server.graph.queries import graph
        from iki_server.org.models import ProjectMembership, ProjectRole
        from iki_server.storage import services as storage

        user = User.objects.create_user("strains@example.org")
        ProjectMembership.objects.create(project=project, user=user, role=ProjectRole.EDITOR)

        def make(type_name, **values):
            return services.create(user, type_name, project, values).entity

        # The catalogue accessions and yeast genotype are from the ATCC deposit records.
        # https://www.atcc.org/products/201388
        # https://www.atcc.org/products/47076
        def base(accession, name, kind, taxon_id, **values):
            organism = make("iki/core.Organism", taxon_id=taxon_id)
            return make("iki/strains.BaseStrain", authority="ATCC", accession=accession,
                        name=name, kind=kind, organism=str(organism.pk), **values)

        genotype = "MATa his3Δ1 leu2Δ0 met15Δ0 ura3Δ0"
        yeast = base("201388", "BY4741", "yeast", 4932, genotype=genotype)
        bacterium = base("47076", "MG1655", "bacterium", 562)
        assert services.current_values_of([yeast])[yeast.pk]["genotype"] == genotype

        # The plasmid sequences, transformations, stock volumes and readout here are
        # synthetic database fixtures.
        def plasmid(bases):
            sequence = make("iki/dna.DNASequence", bases=bases, topology="circular")
            return make("iki/plasmid.Plasmid", sequence=str(sequence.pk))

        a, b = plasmid("ACGT"), plasmid("AAAC")

        def transform(base, *plasmids):
            return services.create(user, "iki/strains.TransformedStrain", project, {
                "base": str(base.pk), "plasmids": [str(p) for p in plasmids],
            })

        # Concurrent registrations of the same set in opposite orders find one strain.
        ready = Barrier(2)
        def register_pair(pair):
            try:
                ready.wait(timeout=20)
                return transform(yeast, *pair)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(register_pair, [(a.pk, b.pk), (b.entry_id, a.entry_id)])
        assert first.entity.pk == second.entity.pk
        assert sorted([first.reused, second.reused]) == [False, True]
        strain = first.entity
        single = transform(yeast, a.pk).entity
        another_base = transform(bacterium, a.pk, b.pk).entity
        assert len({strain.entry_id, single.entry_id, another_base.entry_id}) == 3
        with pytest.raises(services.Invalid) as duplicate:
            transform(yeast, a.pk, a.entry_id)
        assert "plasmids" in duplicate.value.errors
        with pytest.raises(services.Invalid) as edited:
            services.update(user, strain, {"base": str(bacterium.pk)},
                            expected_revision=strain.revision)
        assert "base" in edited.value.errors

        def stock(of, barcode):
            return services.create(user, "iki/strains.GlycerolStock", project,
                                   {"strain": str(of.pk), "barcode": barcode},
                                   amount={"value": 100, "unit": "uL"}).entity

        source = stock(strain, "SYNTHETIC-1")
        different = stock(yeast, "SYNTHETIC-2")
        rack = make("iki/example_lims.Rack", name="Synthetic rack")
        box = make("iki/strains.StrainBox", name="Synthetic stock box", rows=2, columns=3)
        storage.move(user, [storage.Wanted(box, rack, "1"), storage.Wanted(source, box, "B3")],
                     reason="Inventory fixture")
        assert [(s.entity.pk, s.position) for s in storage.where(user, source).path] == [
            (rack.pk, "1"), (box.pk, "B3"),
        ]

        readout = make("iki/dna.DNASequence", name="Synthetic readout", bases="AAGTCA")
        quantity = amounts.Given(Decimal("10"), "uL")
        with pytest.raises(amounts.Wrong) as wrong_scan:
            amounts.draw(user, source, quantity, used_in=readout, confirm="SYNTHETIC-2")
        assert "confirm" in wrong_scan.value.errors
        with pytest.raises(amounts.Wrong) as mismatch:
            amounts.transfer(user, source, different, quantity)
        assert "to" in mismatch.value.errors
        movement = amounts.draw(user, source, quantity, used_in=readout, confirm="SYNTHETIC-1")
        assert (movement.kind, movement.change, movement.balance, movement.unit) == (
            "drawn", -10, 90, "uL",
        )
        assert Movement.objects.filter(entity=source).count() == 2
        assert Amount.objects.get(entity=different).value == 100
        with transaction.atomic():
            history = graph(user, readout.pk, up=3, down=0)
        lineage = {source.pk, strain.entry_id, yeast.entry_id, a.entry_id, b.entry_id}
        assert lineage <= history.nodes.keys()
        assert (readout.pk, source.pk, "used") in [e[:3] for e in history.edges]
        result = None
        """,
    )
