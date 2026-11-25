"""The construct-review example adds a review to the lab's chosen assembly records."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

from api_client import problem
from first_party_dependencies import PLUGINS, ROOT, build_order, withs
from org_world import build_world
from test_plugin_migrations import database, iki, made, migrate, orm, sync
from test_states import put, roles, set_

from iki_server.entities import services, states
from iki_server.entities.models import Entity
from iki_server.notifications import declared, watches
from iki_server.notifications.models import Notification, WatchMode
from iki_server.runs import catalog
from iki_server.runs.models import TriggerFiring

__all__ = ["database"]

PLUGIN = "examples/construct_review"
REVIEW = f"{PLUGIN}.ReviewedAssembly"


def test_selected_construct_review_checks_current_evidence_and_notifies_its_subscribers(
    tmp_path: Path, database: str
) -> None:
    example = ROOT / "examples/plugins/construct_review"
    dependencies = build_order(PLUGINS / "plasmid")
    made(example, *withs(dependencies), "--check", "--no-input", cwd=ROOT)
    build = tmp_path / "build"
    built = iki("plugin", "build", *dependencies, example, "--output", build, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    orm(
        build,
        database,
        """
        from test_construct_review import exercise_review
        exercise_review()
        result = {}
        """,
    )


def exercise_review() -> None:
    world = build_world()
    reviewer = f"{PLUGIN}.construct_reviewer"
    roles(world, editor=(f"{PLUGIN}.cloner", reviewer), admin=(reviewer,), mixed=(reviewer,))

    def make(kind: str, values: dict[str, Any]) -> Entity:
        entity = services.create(world.users["editor"], kind, world.project, values).entity
        assert isinstance(entity, Entity)
        return entity

    reference = make("iki/dna.DNASequence", {"bases": "ACGT", "topology": "linear"})
    source = {"backbone": str(reference.pk), "method": "gibson", "product": str(reference.pk)}
    ordinary = make("iki/plasmid.Assembly", source)
    assert states.values_of(ordinary) == {}
    assert not declared.of_type(ordinary.type)
    reviewed = make(REVIEW, source)
    ref = str(reviewed.pk)
    assert states.values_of(reviewed) == {"status": "planned"}
    watches.choose(world.users["viewer"], reviewed, WatchMode.WATCHING)

    def edit(who: str, values: dict[str, Any], *, by_run: bool = False) -> None:
        reviewed.refresh_from_db()
        services.update(
            world.users[who], reviewed, values, expected_revision=reviewed.revision, by_run=by_run
        )

    def refused(who: str, state: str, guard: str) -> None:
        response = put(world, who, ref, "status", state)
        assert problem(response, 409, "guard_refused")["guard"] == guard

    def assert_recipients(name: str) -> None:
        notes = Notification.objects.filter(entity=reviewed, kind=f"{REVIEW}.{name}")
        assert set(notes.values_list("recipient_id", flat=True)) == {
            world.users["editor"].pk,
            world.users["viewer"].pk,
        }

    edit("admin", {"matches_product": False}, by_run=True)
    assert_recipients("plan_doesnt_match")
    refused("editor", "checked", "the_plan_matches")
    edit("admin", {"matches_product": True}, by_run=True)
    refused("editor", "built", "checked_first")
    set_(world, "editor", ref, "status", "checked")
    set_(world, "editor", ref, "status", "built")
    refused("editor", "verified", "four_eyes")

    # The parent Assembly trigger also invalidates a subtype's result, even if its
    # comparison worker cannot start. No reviewer can lock the old comparison.
    unavailable = catalog.Availability(False, "Comparison worker unavailable.")
    with patch.object(catalog, "availability", return_value=unavailable):
        edit("mixed", {"method": "gateway_lr"})
    result = services.current_values_of([reviewed])[reviewed.pk]
    assert result["matches_product"] is None
    assert "Comparison worker unavailable" in result["verification_note"]
    firing = TriggerFiring.objects.get(
        entity=reviewed, trigger__trigger="iki/plasmid.check_stated_product"
    )
    assert firing.editor == world.users["mixed"] and firing.state == "failed"
    refused("admin", "verified", "the_plan_still_matches")
    edit("editor", {"matches_product": True}, by_run=True)
    refused("mixed", "verified", "independent_verifier")
    assert set_(world, "admin", ref, "status", "verified")["locked"]
    assert_recipients("verified")
