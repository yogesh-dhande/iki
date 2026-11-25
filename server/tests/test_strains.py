"""Strain designs, physical isolates, and inventory-to-data lineage."""

from pathlib import Path

from first_party_dependencies import PLUGINS, ROOT, build_order
from test_plugin_migrations import database, iki, migrate, orm, sync

__all__ = ["database"]


def test_strain_identity_and_stock_to_data_lineage(tmp_path: Path, database: str) -> None:
    out = tmp_path / "build"
    plugins = build_order(PLUGINS / "lots", PLUGINS / "experiments")
    built = iki("plugin", "build", *plugins, "--output", out, cwd=ROOT)
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
        from iki_server.entities import registry, services
        from iki_server.files import listing
        from iki_server.graph.queries import graph
        from iki_server.org.models import LabRoleGrant, ProjectMembership, ProjectRole
        from iki_server.storage import services as storage

        user = User.objects.create_user("strains@example.org")
        member = ProjectMembership.objects.create(
            project=project, user=user, role=ProjectRole.EDITOR,
        )
        LabRoleGrant.objects.create(membership=member, role="iki/lots.qa")

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

        # Each physical isolate has its own label and evidence, even for the same design.
        notebook = make("iki/experiments.Experiment", title="Transformation log")
        report = File.objects.create(
            project=project, blob=blob, name="isolate-A.pdf", media_type="application/pdf",
        )
        def isolate(of, label, **values):
            return services.create(user, "iki/lots.StrainIsolate", project, {
                "strain": str(of.entry_id), "isolate_label": label, **values,
            }, amount={"value": 240, "unit": "uL"}).entity

        clone_a = isolate(strain, "Clone A", transformation=str(notebook.pk),
                          verification_evidence=[str(report.pk)])
        clone_b = isolate(strain, "Clone B", transformation=str(notebook.pk))
        assert clone_a.pk != clone_b.pk
        assert [e.pk for e, _ in listing.samples_of(user, report)] == [clone_a.pk]
        # A catalogue strain can have one lot with its usual lab label and no clone history.
        catalogue_lot = isolate(yeast, "BY4741 stock")
        catalogue = services.current_values_of([catalogue_lot])[catalogue_lot.pk]
        assert catalogue["strain"] == yeast.entry_id

        def stock(of, barcode, **values):
            return services.create(user, "iki/lots.GlycerolStock", project, {
                "isolate": str(of.pk), "contents": str(of.pk), "barcode": barcode,
                "prepared_on": "2025-01-02", **values,
            }, amount={"value": 100, "unit": "uL"}).entity

        portion = amounts.Given(Decimal("100"), "uL")
        banked = amounts.split(user, clone_a, 2, portion, values=[
            {"barcode": "SYNTHETIC-1"}, {"barcode": "SYNTHETIC-3"},
        ])
        source, sibling = banked.aliquots
        assert (banked.source.change, banked.source.balance) == (-200, 40)
        assert source.type == sibling.type == "iki/lots.GlycerolStock"
        values = services.current_values_of(banked.aliquots)
        for vial in banked.aliquots:
            assert values[vial.pk]["contents"] == values[vial.pk]["isolate"] == clone_a.pk
            assert Amount.objects.get(entity=vial).value == 100
        other = stock(clone_b, "SYNTHETIC-2")
        assert source.type in registry.sample_types()
        for contents in (None, str(clone_b.pk)):
            with pytest.raises(services.Invalid, match="contents"):
                stock(clone_a, "WRONG-CONTENTS", contents=contents)
        with pytest.raises(services.Invalid, match="barcode"):
            make("iki/inventory.Tube", barcode="SYNTHETIC-1")

        with pytest.raises(services.Invalid, match="parent stock"):
            isolate(strain, "Ambiguous source", parent_stock=str(source.pk),
                    transformation=str(notebook.pk))
        descendant = isolate(strain, "Subclone A1", parent_stock=str(source.pk))
        with transaction.atomic():
            parents = graph(user, descendant.pk, up=2, down=0)
        assert (descendant.pk, source.pk, "reference", "parent_stock") in [
            e[:4] for e in parents.edges
        ]

        rack = make("iki/inventory.Rack", name="Synthetic rack")
        box = make("iki/inventory.Box", name="Synthetic stock box", rows=2, columns=3)
        tube = make("iki/inventory.Tube", name="Second vial", contents=str(clone_a.pk))
        storage.move(user, [
            storage.Wanted(box, rack, "1"), storage.Wanted(source, box, "B3"),
            storage.Wanted(tube, box, "B2"),
        ], reason="Inventory fixture")
        assert [(s.entity.pk, s.position) for s in storage.where(user, source).path] == [
            (rack.pk, "1"), (box.pk, "B3"),
        ]
        assert storage.where(user, tube).path[-1].position == "B2"
        with pytest.raises(storage.Wrong, match="doesn't hold"):
            storage.move(user, [storage.Wanted(clone_a, box, "A1")], reason="Check contents")

        readout = make("iki/dna.DNASequence", name="Synthetic readout", bases="AAGTCA")
        quantity = amounts.Given(Decimal("10"), "uL")
        def sample(scan):
            return amounts.draw(user, source, quantity, used_in=readout,
                                purpose="qa_sampling", confirm=scan)

        with pytest.raises(amounts.Wrong, match="confirm"):
            sample("SYNTHETIC-2")
        with pytest.raises(amounts.Wrong, match="quarantined"):
            amounts.draw(user, source, quantity, confirm="SYNTHETIC-1")
        source = services.update(user, source, {"on_hold": True, "hold_reason": "Inspect vial"},
                                 expected_revision=source.revision).entity
        with pytest.raises(amounts.Wrong, match="tube is on hold"):
            sample("SYNTHETIC-1")
        source = services.update(
            user, source, {"on_hold": False, "hold_reason": None},
            reason="Inspection complete", expected_revision=source.revision,
        ).entity

        # Different isolates of the same strain must never be pooled as the same material.
        with pytest.raises(amounts.Wrong, match="pooling"):
            amounts.transfer(user, source, other, quantity)
        with pytest.raises(amounts.Wrong, match="pooling"):
            amounts.transfer(user, clone_a, clone_b, quantity)
        sent, received = amounts.transfer(user, source, tube, quantity)
        assert sent.operation == received.operation
        assert (sent.balance, received.balance) == (90, 10)
        movement = sample("SYNTHETIC-1")
        assert (movement.kind, movement.change, movement.balance, movement.unit) == (
            "drawn", -10, 80, "uL",
        )
        assert (movement.source_id, movement.confirmed) == (clone_a.pk, "SYNTHETIC-1")
        assert Movement.objects.filter(entity=source).count() == 3
        assert Amount.objects.get(entity=other).value == 100
        with transaction.atomic():
            history = graph(user, readout.pk, up=4, down=0)
        lineage = {source.pk, clone_a.pk, notebook.pk, strain.entry_id, yeast.entry_id,
                   a.entry_id, b.entry_id}
        assert lineage <= history.nodes.keys()
        assert (readout.pk, source.pk, "used") in [e[:3] for e in history.edges]
        result = None
        """,
    )
