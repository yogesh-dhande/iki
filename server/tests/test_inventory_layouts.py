"""A lab's own storage layouts and containers in the shared inventory (ADR 0260)."""

from pathlib import Path

from first_party_dependencies import PLUGINS, ROOT, build_order, withs
from test_plugin_migrations import database, iki, made, migrate, orm, sync

__all__ = ["database"]

INVENTORY = PLUGINS / "inventory"

#: A lab's compound store: dry powder weighed out of a vial, so its ledger is a mass.
#: It takes the inherited barcode, contents, hold and draw guards, and needs no scan to draw.
LAB = """\
apiVersion: iki/v1
kind: Plugin
name: acme/compound_store
version: 1.0.0
dependencies:
  iki/inventory: { types: { Container: ">=1", Box: ">=1" } }
"""

POWDER_VIAL = """\
apiVersion: iki/v1
kind: EntityType
name: PowderVial
extends: iki/inventory.Container
schema_version: 1
hrid: { prefix: PWV }
amount: { unit: mg, label: Mass, material: contents, follows: [contents] }
"""


def test_racks_freezers_and_boxes_take_the_labs_layout(tmp_path: Path, database: str) -> None:
    lab = tmp_path / "plugins" / "compound_store"
    (lab / "types").mkdir(parents=True)
    (lab / "plugin.yaml").write_text(LAB)
    (lab / "types" / "powder_vial.yaml").write_text(POWDER_VIAL)
    made(lab, "--no-input", *withs(build_order(INVENTORY)), cwd=ROOT)
    out = tmp_path / "build"
    built = iki("plugin", "build", *build_order(INVENTORY), lab, "--output", out, cwd=ROOT)
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
        from iki_server.entities import services
        from iki_server.org.models import ProjectMembership, ProjectRole
        from iki_server.storage import services as storage
        from iki_server.storage.models import Place

        user = User.objects.create_user("editor@example.com")
        ProjectMembership.objects.create(project=project, user=user, role=ProjectRole.EDITOR)

        def make(type_name, values, **kwargs):
            return services.create(user, type_name, project, values, **kwargs).entity

        def put(item, container, position=None):
            storage.move(user, [storage.Wanted(item, container, position)], reason="Stored")
            return Place.objects.get(pk=item.pk)

        def refused(item, container, position=None):
            with pytest.raises(storage.Wrong) as raised:
                put(item, container, position)
            return str(raised.value)

        result = {}
        # An upright freezer's shelves, and a dewar's racks and boxes, without a shelf.
        upright = make("iki/inventory.Freezer", {"name": "-80 freezer 3"})
        dewar = make("iki/inventory.Freezer", {"name": "LN2 dewar 1", "set_point_c": -196})
        shelf = make("iki/inventory.Shelf", {"name": "Shelf 2"})
        put(shelf, upright)

        # A rack made without a format is five boxes high and four deep, as before.
        standard = make("iki/inventory.Rack", {"name": "Rack C"})
        values = services.current_values_of([standard])[standard.pk]
        result["standard"] = [values["rows"], values["columns"]]
        put(standard, shelf)
        boxes = [make("iki/inventory.Box", {"name": f"Box {n}"}) for n in (1, 2)]
        result["last_slot"] = put(boxes[0], standard, "20").position
        result["past_last"] = "21" in refused(boxes[1], standard, "21")

        # A dewar's vertical rack holds one box on each of its six levels.
        tall = make("iki/inventory.Rack", {"name": "Dewar rack 4", "rows": 6, "columns": 1})
        result["dewar_rack"] = put(tall, dewar).container_id == dewar.pk
        result["tall_slot"] = put(boxes[1], tall, "6").position
        loose = make("iki/inventory.Box", {"name": "Loose box", "rows": 2, "columns": 2})
        result["dewar_box"] = put(loose, dewar).container_id == dewar.pk
        with pytest.raises(services.Invalid, match="rows"):
            services.update(user, tall, {"rows": 5}, expected_revision=tall.revision)

        # A tube and a lab's own powder vial share one box and the same hold.
        compound = make("iki/core.Collection", {"name": "Example compound"})
        tube = make("iki/inventory.Tube", {"name": "10 mM DMSO", "barcode": "T-1",
                                           "contents": str(compound.pk)},
                    amount={"value": 100, "unit": "uL"})
        vial = make("acme/compound_store.PowderVial", {"name": "Powder", "barcode": "P-1",
                                                       "contents": str(compound.pk)},
                    amount={"value": 25, "unit": "mg"})
        result["tube_at"] = put(tube, loose, "A1").position
        result["vial_at"] = put(vial, loose, "A2").position
        shelf_in_box = refused(shelf, loose)
        result["box_refuses_shelf"] = "doesn't hold" in shelf_in_box
        bottle = make("iki/inventory.Tube", {"name": "Buffer 50 mL", "contents": str(compound.pk)})
        standing = put(bottle, shelf)
        result["on_shelf"] = [standing.container_id == shelf.pk, standing.position]

        weighed = amounts.Given(Decimal(5), "mg")
        amounts.draw(user, vial, weighed)
        result["vial_left"] = str(amounts.amounts_of([vial])[vial.pk].value)
        with pytest.raises(amounts.Wrong, match="confirm"):
            amounts.draw(user, tube, amounts.Given(Decimal(5), "uL"))
        amounts.draw(user, tube, amounts.Given(Decimal(5), "uL"), confirm="T-1")
        with pytest.raises(amounts.Wrong):
            amounts.draw(user, vial, amounts.Given(Decimal(5), "uL"))

        vial.refresh_from_db()
        services.update(user, vial, {"on_hold": True, "hold_reason": "Label smudged"},
                        expected_revision=vial.revision)
        with pytest.raises(amounts.Wrong, match="on hold"):
            amounts.draw(user, vial, weighed)
        vial.refresh_from_db()
        with pytest.raises(services.Invalid):
            services.update(user, vial, {"on_hold": False}, expected_revision=vial.revision)
        services.update(user, vial, {"on_hold": False}, reason="Relabelled and checked",
                        expected_revision=vial.revision)
        amounts.draw(user, vial, weighed)
        result["vial_after_hold"] = str(amounts.amounts_of([vial])[vial.pk].value)

        # Withdrawing a vial holds the vials split from it, as it does a tube's aliquots.
        from iki_server.runs import catalog, triggers
        from iki_server.runs.models import FiringState, TriggerFiring
        from iki_spec.compute import Binding

        admin = User.objects.create_user("admin@example.com")
        ProjectMembership.objects.create(project=project, user=admin, role=ProjectRole.ADMIN)
        (portion,) = amounts.split(user, vial, 1, weighed).aliquots
        env = "iki/inventory.stock-tools"
        binding = Binding(environment=env, templates=(("backend-small", 0),))
        catalog.compute().bindings[env] = binding
        trigger = triggers.enable(admin, project, "iki/inventory.hold_derived_tubes")
        vial.refresh_from_db()
        services.update(user, vial, {"on_hold": True, "hold_reason": "Wrong weight"},
                        expected_revision=vial.revision)
        vial.refresh_from_db()
        firing = TriggerFiring.objects.get(trigger=trigger, entity=vial, revision=vial.revision)
        triggers.fire(str(firing.pk))
        firing.refresh_from_db()
        assert firing.state == FiringState.LAUNCHED, firing.error
        types = {vial.pk: vial.type, portion.pk: portion.type}
        result["derived"] = sorted(
            (i.name, types[i.entity_id]) for i in firing.run.inputs.all()
        )
        """,
    )
    assert result == {
        "standard": [5, 4],
        "last_slot": "20",
        "past_last": True,
        "dewar_rack": True,
        "tall_slot": "6",
        "dewar_box": True,
        "tube_at": "A1",
        "vial_at": "A2",
        "box_refuses_shelf": True,
        "on_shelf": [True, None],
        "vial_left": "20",
        "vial_after_hold": "15",
        "derived": [
            ["source", "acme/compound_store.PowderVial"],
            ["tubes", "acme/compound_store.PowderVial"],
        ],
    }
