"""A reasoned return to sample testing keeps the signed release and revision history."""

import json
from pathlib import Path

import pytest
from api_client import Api, problem
from django.core.exceptions import PermissionDenied
from first_party_dependencies import PLUGINS, ROOT, build_order
from org_world import build_world
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import begin, ready, signed

from iki_server.audit.models import AuditEvent
from iki_server.entities import services
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid
from iki_server.org.models import LabRoleGrant, ProjectRole
from iki_server.org.services import Refused
from iki_server.signatures.services import verify

__all__ = ["database"]

PLUGIN = PLUGINS / "example_lims"


def test_retesting_uses_the_revision_and_reason_seen_by_both_clients(
    tmp_path: Path, database: str
) -> None:
    build = tmp_path / "build"
    result = iki("plugin", "build", *build_order(PLUGIN), "--output", build, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    migrate(build, database)
    sync(build, database)
    checked = django(
        build,
        database,
        "-c",
        "import django; django.setup(); from test_sample_retesting import exercise_retesting; "
        "exercise_retesting()",
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def exercise_retesting() -> None:
    world = build_world()
    ready(world)
    roles = {
        "editor": ("analyst",),
        "runner": ("reviewer",),
        "viewer": ("reviewer",),
        "admin": ("qa", "coordinator"),
        "mixed": ("qa",),
    }
    for person, lab_roles in roles.items():
        membership = world.project.memberships.get(user=world.users[person])
        if person == "runner":
            membership.role = ProjectRole.EDITOR
            membership.save(update_fields=["role"])
        for role in lab_roles:
            LabRoleGrant.objects.create(role=f"iki/example_lims.{role}", membership=membership)
    editor = world.users["editor"]
    # Synthetic specimen and measurements for this workflow test.
    made = services.create(editor, "iki/protein.Protein", world.project, {"sequence": "MKV"}).entity
    protein = Entity.objects.get(pk=made.pk)
    assert protein.entry_id is not None
    sample = services.create(
        editor,
        "iki/example_lims.Sample",
        world.project,
        {
            "protein": str(protein.entry_id),
            "purity": 97,
            "result": "pass",
            "analyst": str(editor.pk),
        },
    ).entity
    analyst = Api(world.browser("editor"))
    qa = Api(world.browser("admin"))
    path = f"/entities/{sample.pk}"
    status = f"{path}/states/status"

    def advance(state: str, reason: str = "") -> None:
        etag = analyst.get(path)["ETag"]
        changed = analyst.post(
            status, {"state": state, "reason": reason}, headers={"If-Match": etag}
        )
        assert changed.status_code == 200, changed.content

    def approve() -> None:
        signed(world, "editor", str(sample.pk), "in_review")
        signed(world, "runner", str(sample.pk), "approved")

    advance("received")
    advance("in_testing")
    approve()
    approved = analyst.get(path)
    corrected = analyst.patch(
        path,
        {"values": {"purity": 97.5}, "reason": "Correct the sample measurement transcription"},
        headers={"If-Match": approved["ETag"]},
    )
    assert corrected.status_code == 200, corrected.content
    assert corrected.json()["values"]["status"] == "in_testing"
    assert corrected.json()["revision"] == approved.json()["revision"] + 2
    approve()
    release = signed(world, "admin", str(sample.pk), "released").signature
    assert release is not None

    inspected_analyst, inspected_qa = analyst.get(path), qa.get(path)
    assert inspected_analyst["ETag"] == inspected_qa["ETag"]
    assert inspected_analyst.json()["values"]["status"] == "released"
    etag = inspected_qa["ETag"]
    reason = "Retest the sample after an independent measurement discrepancy"
    for ordinary_target in ("registered", "received", "in_testing"):
        refused = problem(
            qa.post(
                status, {"state": ordinary_target, "reason": reason}, headers={"If-Match": etag}
            ),
            409,
            "guard_refused",
        )
        assert refused["guard"] == "outside_retesting"
    problem(
        qa.post(status, {"state": "retesting", "reason": reason}, headers={"If-Match": etag}),
        409,
        "signatures_required",
    )
    with pytest.raises(PermissionDenied):
        begin(world, "editor", str(sample.pk), "retesting", reason=reason)
    with pytest.raises(Invalid) as denied:
        begin(world, "admin", str(sample.pk), "retesting", reason="  ")
    assert "reason" in denied.value.errors
    unchanged = analyst.get(path)
    assert unchanged["ETag"] == etag and unchanged.json()["locked"] is True

    returned = signed(world, "admin", str(sample.pk), "retesting", reason=reason).signature
    assert returned is not None and all(check.ok for check in verify(returned))
    assert returned.revision == unchanged.json()["revision"]
    assert json.loads(returned.document)["reason"] == reason
    assert returned.signer_id == release.signer_id
    moved = qa.get(path)
    assert moved.json()["values"]["status"] == "retesting" and moved.json()["locked"] is False
    assert moved.json()["values"]["analyst"] == str(editor.pk)
    assert moved.json()["revision"] == unchanged.json()["revision"] + 1
    problem(
        analyst.patch(
            path,
            {"values": {"purity": 98}},
            headers={"If-Match": inspected_analyst["ETag"]},
        ),
        412,
        "stale",
    )
    assert analyst.get(path).json()["values"]["purity"] == 97.5
    event = AuditEvent.objects.get(
        action="entity.state_changed", target_id=sample.pk, data__from="released"
    )
    assert event.data["to"] == "retesting" and event.data["reason"] == reason
    assert event.actor == world.users["admin"]
    assert all(check.ok for check in verify(release))

    reviewer = Api(world.browser("runner"))
    measured = reviewer.patch(path, {"values": {"purity": 98}}, headers={"If-Match": moved["ETag"]})
    assert measured.status_code == 200, measured.content
    noted = qa.patch(
        path,
        {"values": {"notes": "Retest measurement transcribed from the synthetic record"}},
        headers={"If-Match": measured["ETag"]},
    )
    assert noted.status_code == 200, noted.content
    signed(world, "editor", str(sample.pk), "in_review")
    with pytest.raises(Refused, match="You edited the test results"):
        begin(world, "runner", str(sample.pk), "approved")
    advance("in_testing")
    signed(world, "editor", str(sample.pk), "in_review")
    with pytest.raises(Refused, match="You edited the test results"):
        begin(world, "runner", str(sample.pk), "approved")
    signed(world, "viewer", str(sample.pk), "approved")
    with pytest.raises(Refused, match="You edited the test results"):
        begin(world, "admin", str(sample.pk), "released")
    rereleased = signed(world, "mixed", str(sample.pk), "released").signature
    assert rereleased is not None and all(check.ok for check in verify(rereleased))
