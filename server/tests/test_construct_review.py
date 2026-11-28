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

PLUGIN = "acme/construct_review"
REVIEW = f"{PLUGIN}.ReviewedAssembly"


def installed(tmp_path: Path, database: str, exercise: str) -> None:
    """The construct-review example over plasmid, installed, and ``exercise`` run in it."""
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
        f"from test_construct_review import {exercise}\n{exercise}()\nresult = {{}}",
    )


def test_selected_construct_review_checks_current_evidence_and_notifies_its_subscribers(
    tmp_path: Path, database: str
) -> None:
    installed(tmp_path, database, "exercise_review")


def test_moving_to_a_corrected_backbone_clears_the_plans_comparison(
    tmp_path: Path, database: str
) -> None:
    """A registrar supersedes the backbone entry an assembly names, and its project moves its
    records to the replacement: the comparison made against the old backbone is cleared, as
    any edit of the backbone clears it, so no one verifies the plan on it (ADR 0115)."""
    installed(tmp_path, database, "exercise_move")


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


def exercise_move() -> None:
    from iki_server.entities import entries
    from iki_server.org import registries
    from iki_server.org.models import (
        Project,
        ProjectMembership,
        ProjectRole,
        Registry,
        RegistryRole,
    )

    world = build_world()
    reviewer = f"{PLUGIN}.construct_reviewer"
    roles(world, editor=(f"{PLUGIN}.cloner", reviewer), admin=(reviewer,))
    editor = world.users["editor"]
    core = Project.objects.create(team=world.team, name="Vector Core")
    ProjectMembership.objects.create(project=core, user=editor, role=ProjectRole.EDITOR)

    def make(kind: str, values: dict[str, Any], project: Any = None) -> Entity:
        entity = services.create(editor, kind, project or world.project, values).entity
        assert isinstance(entity, Entity)
        return entity

    # pUC19's backbone was registered with a mistyped base; the core holds the right one.
    wrong = make("iki/dna.DNASequence", {"bases": "TCGCGCGTTTCGGTGATGAC", "topology": "linear"})
    right = make(
        "iki/dna.DNASequence", {"bases": "TCGCGCGTTTCGGTGATGAG", "topology": "linear"}, core
    )
    product = make(
        "iki/dna.DNASequence", {"bases": "TCGCGCGTTTCGGTGATGAC", "topology": "linear"}, core
    )
    reviewed = make(
        REVIEW,
        {"backbone": str(wrong.entry_id), "method": "gibson", "product": str(product.entry_id)},
    )
    ref = str(reviewed.pk)
    reviewed.refresh_from_db()
    services.update(
        world.users["admin"],
        reviewed,
        {"matches_product": True},
        expected_revision=reviewed.revision,
        by_run=True,
    )
    set_(world, "editor", ref, "status", "checked")
    set_(world, "editor", ref, "status", "built")

    auditor = world.users["auditor"]
    registries.grant_role(
        Registry.default(), RegistryRole.Role.REGISTRAR, auditor, by=world.users["org_admin"]
    )
    assert wrong.entry_id is not None and right.entry_id is not None
    entries.supersede(
        auditor,
        Entity.objects.get(pk=wrong.entry_id),
        Entity.objects.get(pk=right.entry_id),
        reason="Registered with a mistyped final base",
    )
    moved = entries.move(editor, wrong, reason="To the corrected backbone")
    assert [e.pk for e in moved.repointed] == [reviewed.pk]
    now = services.current_values_of([reviewed])[reviewed.pk]
    assert str(now["backbone"]) == str(right.entry_id)
    assert now["matches_product"] is None
    # The move queued the comparison against the corrected backbone, as the edit's.
    firing = TriggerFiring.objects.get(
        entity=reviewed, trigger__trigger="iki/plasmid.check_stated_product"
    )
    assert firing.editor == editor
    refused = put(world, "admin", ref, "status", "verified")
    assert problem(refused, 409, "guard_refused")["guard"] == "the_plan_still_matches"
