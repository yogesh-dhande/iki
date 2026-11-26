"""Each sample profile: a reviewed sample's reasoned return to testing keeps its signed
review and revision history, and a plain sample is completed by its analyst, without a
signed review, and reopened only with a reason."""

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from api_client import Api, problem
from django.core.exceptions import PermissionDenied
from django.db import connection
from first_party_dependencies import PLUGINS, ROOT, build_order
from org_world import build_world
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import begin, ready, signed

from iki_server.amounts import services as amounts
from iki_server.amounts.models import Amount, DrawPurpose, Movement
from iki_server.audit.models import AuditEvent
from iki_server.entities import services
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid
from iki_server.org.models import LabRoleGrant, ProjectRole
from iki_server.org.services import Refused
from iki_server.signatures.models import Signature
from iki_server.signatures.services import verify

__all__ = ["database"]

PLUGIN = PLUGINS / "samples"


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
    for who, roles in {
        "editor": ["analyst"],
        "runner": ["reviewer"],
        "admin": ["reviewer"],
    }.items():
        membership = world.project.memberships.get(user=world.users[who])
        if who == "runner":
            membership.role = ProjectRole.EDITOR
            membership.save(update_fields=["role"])
        for role in roles:
            LabRoleGrant.objects.create(role=f"iki/lab_roles.{role}", membership=membership)

    editor = world.users["editor"]

    def make(
        type_name: str, values: dict[str, Any], amount: dict[str, Any] | None = None
    ) -> Entity:
        return Entity.objects.get(
            pk=services.create(editor, type_name, world.project, values, amount=amount).entity.pk
        )

    work = make("iki/core.Collection", {"name": "Training measurements"})
    tube = make("iki/inventory.Tube", {"contents": str(work.pk)}, {"value": 100, "unit": "uL"})
    source = {"source": str(tube.pk)}
    with pytest.raises(Invalid):
        make("iki/samples.ReviewedSample", source)
    with pytest.raises(Invalid):
        make("iki/samples.ReviewedSample", {**source, "purpose": str(work.pk), "run": "RUN-000001"})
    sample = make(
        "iki/samples.ReviewedSample", {**source, "purpose": str(work.pk), "analyst": str(editor.pk)}
    )
    analyst, reviewer = Api(world.browser("editor")), Api(world.browser("runner"))
    path = f"/entities/{sample.pk}"
    status = f"{path}/states/status"

    def advance(client: Api, state: str) -> Any:
        return client.post(status, {"state": state}, headers={"If-Match": client.get(path)["ETag"]})

    problem(advance(analyst, "received"), 409, "guard_refused")
    given = amounts.Given.of({"value": 5, "unit": "uL"})
    other = make("iki/inventory.Tube", {"contents": str(work.pk)}, {"value": 20, "unit": "uL"})
    with pytest.raises(amounts.Wrong, match="assigned"):
        amounts.draw(editor, other, given, used_in=sample, confirm=other.hrid)
    with pytest.raises(amounts.Wrong, match="Scan"):
        amounts.draw(editor, tube, given, used_in=sample)
    start = Barrier(2, timeout=10)

    def take(_: int) -> Movement | None:
        try:
            start.wait()
            return amounts.draw(editor, tube, given, used_in=sample, confirm=tube.hrid)
        except amounts.Wrong as error:
            assert "already recorded" in str(error)
            return None
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        (moved,) = [receipt for receipt in pool.map(take, range(2)) if receipt is not None]
    assert Amount.objects.get(entity=tube).value == 95
    [receipt] = analyst.get(path).json()["values"]["receipt"]
    assert receipt["movement"] == str(moved.pk) and receipt["record"] == str(tube.pk)
    assert receipt["amount"] == "5" and receipt["unit"] == "uL" and receipt["by"] == str(editor.pk)
    assert receipt["at"] == moved.created_at.isoformat()

    # Direct lot withdrawals are weighed, carry lot QA restrictions and have no tube scan.
    lot = make("iki/lots.GenericLot", {"of": str(work.pk)}, {"value": 12, "unit": "mg"})
    direct = make("iki/samples.ReviewedSample", {"source": str(lot.pk), "run": "RUN-000001"})
    mass = amounts.Given.of({"value": 2, "unit": "mg"})
    with pytest.raises(amounts.Wrong, match="quarantined"):
        amounts.draw(editor, lot, mass, used_in=direct)
    with pytest.raises(PermissionDenied):
        amounts.draw(editor, lot, mass, used_in=direct, purpose=DrawPurpose.QA_SAMPLING)
    qa = world.users["admin"]
    LabRoleGrant.objects.create(
        role="iki/lots.qa", membership=world.project.memberships.get(user=qa)
    )
    movement = amounts.draw(qa, lot, mass, used_in=direct, purpose=DrawPurpose.QA_SAMPLING)
    [weight] = services.current_values_of([direct])[direct.pk]["receipt"]
    assert (weight["movement"], weight["record"], weight["amount"], weight["unit"]) == (
        str(movement.pk),
        str(lot.pk),
        "2",
        "mg",
    )
    assert Amount.objects.get(entity=lot).value == 10

    for state in ("received", "in_testing"):
        response = advance(analyst, state)
        assert response.status_code == 200, response.content

    def measure(client: Api, value: str, etag: str, reason: str = "") -> Any:
        return client.patch(
            path,
            {
                "values": {
                    "measurements": [{"method": "Absorbance", "value": value, "unit": "AU"}]
                },
                "reason": reason,
            },
            headers={"If-Match": etag},
        )

    assert measure(analyst, "0.50", analyst.get(path)["ETag"]).status_code == 200
    signed(world, "editor", str(sample.pk), "in_review")
    original = signed(world, "runner", str(sample.pk), "reviewed").signature
    assert original and all(check.ok for check in verify(original))
    inspected = analyst.get(path)
    assert inspected.json()["locked"] and inspected.json()["values"]["status"] == "reviewed"
    etag = inspected["ETag"]
    reason = "Correct the absorbance transcription"
    with pytest.raises(PermissionDenied):
        begin(world, "editor", str(sample.pk), "retesting", reason=reason)
    with pytest.raises(Invalid, match="reason"):
        begin(world, "runner", str(sample.pk), "retesting", reason=" ")
    problem(
        reviewer.post(status, {"state": "retesting", "reason": reason}, headers={"If-Match": etag}),
        409,
        "signatures_required",
    )
    returned = signed(world, "runner", str(sample.pk), "retesting", reason=reason).signature
    assert returned and all(check.ok for check in verify(returned))
    assert returned.revision == inspected.json()["revision"]
    assert json.loads(returned.document)["reason"] == reason
    problem(measure(analyst, "0.55", etag), 412, "stale")
    event = AuditEvent.objects.get(
        action="entity.state_changed", target_id=sample.pk, data__from="reviewed"
    )
    assert event.data["reason"] == reason and event.actor == world.users["runner"]
    current = reviewer.get(path)["ETag"]
    problem(measure(reviewer, "0.55", current), 422, "validation_failed")
    assert measure(reviewer, "0.55", current, reason).status_code == 200
    signed(world, "editor", str(sample.pk), "in_review", reason="Submit the corrected measurements")
    with pytest.raises(Refused, match="measurement editors"):
        begin(world, "runner", str(sample.pk), "reviewed", reason="Review the correction")
    fresh = signed(
        world, "admin", str(sample.pk), "reviewed", reason="Review the corrected measurement"
    ).signature
    assert fresh and all(check.ok for check in verify(fresh))
    assert all(check.ok for check in verify(original))

    _plain_sample(world, make, work)


def _plain_sample(world: Any, make: Callable[..., Entity], work: Entity) -> None:
    """A discovery aliquot drawn as an assay's input: its analyst completes it, with or
    without measurements, and nobody signs."""
    editor = world.users["editor"]
    tube = make("iki/inventory.Tube", {"contents": str(work.pk)}, {"value": 40, "unit": "uL"})
    sample = make(
        "iki/samples.PlainSample",
        {"source": str(tube.pk), "purpose": str(work.pk), "analyst": str(editor.pk)},
    )
    assert sample.hrid.startswith("SMP-")
    analyst, reviewer = Api(world.browser("editor")), Api(world.browser("runner"))
    path = f"/entities/{sample.pk}"
    status = f"{path}/states/status"

    def advance(client: Api, state: str, reason: str = "") -> Any:
        etag = client.get(path)["ETag"]
        return client.post(status, {"state": state, "reason": reason}, headers={"If-Match": etag})

    # Received only once its draw is recorded, as a reviewed sample is.
    problem(advance(analyst, "received"), 409, "guard_refused")
    problem(advance(analyst, "done"), 409, "guard_refused")
    amounts.draw(
        editor,
        tube,
        amounts.Given.of({"value": 5, "unit": "uL"}),
        used_in=sample,
        confirm=tube.hrid,
    )
    assert advance(analyst, "received").status_code == 200
    # Used as it was received: done, with no measurements and no signature.
    problem(advance(reviewer, "done"), 403, "forbidden")
    done = advance(analyst, "done")
    assert done.status_code == 200, done.content
    current = analyst.get(path)
    assert current.json()["locked"] and current.json()["values"]["status"] == "done"
    assert not Signature.objects.filter(entity=sample).exists()
    measurement = {"values": {"measurements": [{"method": "A280", "value": "0.81", "unit": "AU"}]}}
    problem(analyst.patch(path, measurement, headers={"If-Match": current["ETag"]}), 409, "refused")

    # Reopened only with a reason, then measured and done again.
    problem(advance(analyst, "retesting"), 422, "validation_failed")
    assert advance(analyst, "retesting", "Record the A280 reading").status_code == 200
    # Its values changed after it was locked, so the edit says why, as any correction does.
    etag = analyst.get(path)["ETag"]
    unexplained = analyst.patch(path, {**measurement, "reason": ""}, headers={"If-Match": etag})
    problem(unexplained, 422, "validation_failed")
    explained = {**measurement, "reason": "A280 read after the plate was set up"}
    reopened = analyst.patch(path, explained, headers={"If-Match": etag})
    assert reopened.status_code == 200, reopened.content
    assert advance(analyst, "done").status_code == 200
    event = AuditEvent.objects.get(
        action="entity.state_changed", target_id=sample.pk, data__to="retesting"
    )
    assert event.data["reason"] == "Record the A280 reading"
