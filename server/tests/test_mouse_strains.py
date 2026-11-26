"""Mouse strain and allele identities, genotype evidence and banked germplasm (ADR 0187)."""

from pathlib import Path

from first_party_dependencies import PLUGINS, ROOT, build_order
from test_plugin_migrations import database, iki, migrate, orm, sync

__all__ = ["database"]


def test_banked_germplasm_traces_to_its_catalogue_and_data(tmp_path: Path, database: str) -> None:
    out = tmp_path / "build"
    built = iki(
        "plugin", "build", *build_order(PLUGINS / "mouse_strains"), "--output", out, cwd=ROOT
    )
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(out, database)
    sync(out, database)
    result = orm(
        out,
        database,
        """
        from decimal import Decimal

        import pytest

        from iki_server.accounts.models import User
        from iki_server.amounts import services as amounts
        from iki_server.entities import search, services
        from iki_server.files import listing
        from iki_server.graph.queries import graph
        from iki_server.org.models import ProjectMembership, ProjectRole
        from iki_server.storage import services as storage
        from iki_server.storage.models import Event, Place

        user = User.objects.create_user("editor@example.com")
        ProjectMembership.objects.create(project=project, user=user, role=ProjectRole.EDITOR)
        def make(type_name, values, **kwargs):
            return services.create(
                user, "iki/mouse_strains." + type_name, project, values, **kwargs,
            ).entity

        # MGI's catalogue lists this allele in the JAX stock. All local subjects, carrier
        # labels, amounts and files below are synthetic examples.
        # https://www.informatics.jax.org/strain/MGI:2163869
        # https://www.informatics.jax.org/allele/MGI:1855985
        # https://www.jax.org/strain/000058
        allele = make("MouseAllele", {"mgi_id": "MGI:1855985", "symbol": "Tyr<c-2J>"})
        strain_values = {
            "mgi_id": "MGI:2163869", "nomenclature": "B6(Cg)-Tyr<c-2J>/J",
            "jax_stock": "000058", "alleles": [str(allele.entry_id)],
        }
        strain = make("MouseStrain", strain_values)
        assert make("MouseStrain", {"mgi_id": "MGI:2163869"}).pk == strain.pk
        assert services.current_values_of([strain])[strain.pk]["jax_stock"] == "000058"
        with pytest.raises(services.Invalid, match="jax_stock"):
            make("MouseStrain", {"mgi_id": "MGI:3028467", "jax_stock": "664"})

        # An in-house cross has the lab's own ID, and no MGI accession. Its designation
        # and stocks are synthetic.
        local_values = {
            "catalogue": "local", "local_id": "EXAMPLE-0042",
            "nomenclature": "B6(Cg)-Tyr<c-2J>/J x local reporter F1",
            "repository_stocks": ["EXAMPLEREPO:0042"],
        }
        local = make("MouseStrain", local_values)
        again = make("MouseStrain", {"catalogue": "local", "local_id": "EXAMPLE-0042"})
        assert again.pk == local.pk and local.entry_id != strain.entry_id
        for wrong in (
            {"catalogue": "local", "local_id": "EXAMPLE-0043", "mgi_id": "MGI:2163869"},
            {"catalogue": "local"},
            {"catalogue": "mgi", "local_id": "EXAMPLE-0044"},
        ):
            with pytest.raises(services.Invalid, match="local strain"):
                make("MouseStrain", wrong)
        with pytest.raises(services.Invalid, match="repository_stocks"):
            make("MouseStrain", {**local_values, "local_id": "EXAMPLE-0045",
                                 "repository_stocks": ["no repository"]})

        genotype = make("MouseGenotype", {
            "strain": str(strain.entry_id), "subject": "Example donor A",
            "notation": "Tyr<c-2J>/Tyr<c-2J>", "alleles": [str(allele.entry_id)],
        })
        common = {"strain": str(strain.entry_id), "banked_on": "2025-01-02"}
        sperm = make("MouseCryostock", {
            **common, "kind": "sperm", "carrier": "cryovial", "genotype": str(genotype.pk),
            "barcode": "EXAMPLE-S",
        }, amount={"unit": "uL", "value": 50})
        embryos = make("MouseCryostock", {
            **common, "kind": "embryos", "carrier": "straw", "embryos_at_banking": 4,
            "barcode": "EXAMPLE-E",
        }, amount={"unit": "uL", "value": 10})
        with pytest.raises(services.Invalid, match="embryo count"):
            make("MouseCryostock", {
                **common, "kind": "sperm", "carrier": "straw", "embryos_at_banking": 4,
            })
        with pytest.raises(services.Invalid, match="carrier"):
            make("MouseCryostock", {**common, "kind": "sperm"})

        # C57BL/6J is a different registry entry from the albino stock.
        # https://www.informatics.jax.org/strain/MGI:3028467
        b6 = make("MouseStrain", {"mgi_id": "MGI:3028467", "jax_stock": "000664"})
        with pytest.raises(services.Invalid, match="genotype record for the strain"):
            make("MouseCryostock", {
                **common, "strain": str(b6.entry_id), "kind": "sperm", "carrier": "straw",
                "genotype": str(genotype.pk),
            })

        box = make("MouseCryobox", {"name": "Example box", "rows": 2, "columns": 2})
        rack = services.create(
            user, "iki/inventory.Rack", project, {"name": "Example rack"},
        ).entity
        # The donor's tissue is a tube in the same box.
        tissue = services.create(
            user, "iki/inventory.Tube", project, {"name": "Donor A tail", "barcode": "EXAMPLE-T"},
        ).entity
        storage.move(user, [storage.Wanted(box, rack, "1"), storage.Wanted(sperm, box, "A1"),
                            storage.Wanted(embryos, box, "B2"), storage.Wanted(tissue, box, "A2")],
                     reason="Banked")
        assert Place.objects.get(pk=sperm.pk).container_id == box.pk
        assert Place.objects.get(pk=tissue.pk).container_id == box.pk
        assert Place.objects.get(pk=box.pk).container_id == rack.pk
        assert Event.objects.filter(entity=embryos, reason="Banked", user=user).exists()

        report = File.objects.create(
            blob=blob, project=project, name="example.tsv", media_type="text/tab-separated-values",
        )
        observation = make("MouseGenotype", {
            "strain": str(strain.entry_id), "subject": "Example pooled sample",
            "notation": "Tyr<c-2J> detected", "report": str(report.pk),
        })
        portion = amounts.Given(Decimal(5), "uL")
        with pytest.raises(amounts.Wrong, match="confirm"):
            amounts.draw(user, sperm, portion, used_in=observation, confirm="EXAMPLE-E")
        amounts.draw(user, sperm, portion, used_in=observation, confirm="EXAMPLE-S")
        assert amounts.amounts_of([sperm])[sperm.pk].value == 45
        with pytest.raises(amounts.Wrong):
            amounts.split(user, sperm, 1, portion)
        # A straw is thawed whole: its recorded volume is drawn into the result.
        amounts.draw(user, embryos, amounts.Given(Decimal(10), "uL"), used_in=observation,
                     confirm="EXAMPLE-E")
        assert amounts.amounts_of([embryos])[embryos.pk].value == 0
        assert [e.pk for e, _ in listing.samples_of(user, report)] == [observation.pk]
        with search.time_limit():
            history = graph(user, observation.pk, 5, 0)
        edges = {row[:4] for row in history.edges}
        result = {
            "draw": any(e[:3] == (observation.pk, sperm.pk, "used") for e in edges),
            "donor": (sperm.pk, genotype.pk, "reference", "genotype") in edges,
            "strain": (sperm.pk, strain.entry_id, "reference", "strain") in edges,
            "allele": (strain.entry_id, allele.entry_id, "reference", "alleles") in edges,
            "straw": any(e[:3] == (observation.pk, embryos.pk, "used") for e in edges),
        }
        """,
    )
    assert result == {"draw": True, "donor": True, "strain": True, "allele": True, "straw": True}
