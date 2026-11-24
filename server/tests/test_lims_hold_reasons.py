"""Ordinary LIMS hold flags keep the reason for a second session's edit."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from api_client import Api, problem
from first_party_dependencies import PLUGINS, ROOT, build_order
from org_world import build_world
from test_plugin_migrations import database, django, iki, migrate, sync

from iki_server.entities import services
from iki_server.entities.models import Entity

__all__ = ["database"]

PLUGIN = PLUGINS / "example_lims"
LIMS = "iki/example_lims."


def test_shipping_hold_flags_require_the_clearing_editors_reason(
    tmp_path: Path, database: str
) -> None:
    build = tmp_path / "build"
    built = iki("plugin", "build", *build_order(PLUGIN), "--output", build, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    checked = django(
        build,
        database,
        "-c",
        "import django; django.setup(); from test_lims_hold_reasons import exercise_hold_reasons; "
        "exercise_hold_reasons()",
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def exercise_hold_reasons() -> None:
    world = build_world()
    editor, admin = world.users["editor"], world.users["admin"]
    first, second = Api(world.browser("editor")), Api(world.browser("admin"))

    def make(kind: str, fields: Mapping[str, Any]) -> Entity:
        made = services.create(editor, kind, world.project, fields).entity
        return Entity.objects.get(pk=made.pk)

    # Synthetic specimen for exercising the application workflow.
    protein = make("iki/protein.Protein", {"sequence": "MKV"})
    assert protein.entry_id is not None
    sample = make(LIMS + "Sample", {"protein": str(protein.entry_id)})
    aliquot = make(LIMS + "Aliquot", {"sample": str(sample.pk)})
    result = make(LIMS + "Result", {"aliquot": str(aliquot.pk), "assay": "Training measurement"})

    original_reason = "The label needs a second check."
    clear_reason = "The specimen and recorded identifier match."
    for record, cleared in ((aliquot, False), (result, None)):
        field = next(
            f
            for f in first.get(f"/entity-types/{record.type}").json()["fields"]
            if f["name"] == "on_hold"
        )
        assert field["reason_on_clear"] is True

        path = f"/entities/{record.pk}"
        held = first.patch(
            path,
            {"values": {"on_hold": True, "hold_reason": original_reason}},
            headers={"If-Match": first.get(path)["ETag"]},
        )
        assert held.status_code == 200, held.content
        etag = second.get(path)["ETag"]
        assert etag == held["ETag"]
        headers = {"If-Match": etag}
        held_revision = held.json()["revision"]
        changes = {"values": {"on_hold": cleared, "hold_reason": None}}

        for extra in ({}, {"reason": " \t", "hold_reason": "Reason for a rule exception."}):
            refused = problem(
                second.patch(path, {**changes, **extra}, headers=headers),
                422,
                "clear_reason_required",
            )
            assert [error["pointer"] for error in refused["errors"]] == ["#/reason"]
            unchanged = first.get(path)
            assert unchanged["ETag"] == etag
            assert unchanged.json()["values"]["on_hold"] is True
            assert unchanged.json()["values"]["hold_reason"] == original_reason

        edited = second.patch(path, {**changes, "reason": f"  {clear_reason}  "}, headers=headers)
        assert edited.status_code == 200, edited.content
        assert edited.json()["revision"] == held_revision + 1
        assert edited.json()["values"].get("on_hold") is cleared
        assert edited.json()["values"].get("hold_reason") is None
        problem(first.patch(path, changes, headers=headers), 412, "stale")

        original = second.get(f"{path}/revisions/{held_revision}").json()
        correction = first.get(f"{path}/revisions/{held_revision + 1}").json()
        assert original["user"]["id"] == str(editor.pk)
        assert original["created_at"]
        assert original["values"]["on_hold"] is True
        assert original["values"]["hold_reason"] == original_reason
        assert correction["user"]["id"] == str(admin.pk)
        assert correction["created_at"]
        assert correction["reason"] == clear_reason
        assert correction["changes"]["on_hold"] == {"before": True, "after": cleared}
        assert correction["changes"]["hold_reason"] == {"before": original_reason, "after": None}
