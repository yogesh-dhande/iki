"""A witness signs the experiment's captured evidence, including the actual lot withdrawal."""

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.utils import timezone
from first_party_dependencies import PLUGINS, build_order
from org_world import World, build_world
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import begin, ready, signed
from test_signatures import evidence as authenticated

from iki_server.amounts import services as amounts
from iki_server.amounts.models import Amount, Movement
from iki_server.entities import services, states
from iki_server.entities.models import Entity
from iki_server.files.models import Blob, File
from iki_server.graph.models import Edge
from iki_server.org.models import LabRoleGrant, Project, ProjectMembership, ProjectRole
from iki_server.org.services import Refused
from iki_server.runs import catalog, queue, results
from iki_server.runs import services as runs
from iki_server.runs.models import ActionRun, Job
from iki_server.signatures.models import Signature, Status
from iki_server.signatures.services import finish, verify
from iki_spec.compute import Binding

__all__ = ["database"]

EXPERIMENTS = PLUGINS / "experiments"
LOTS = PLUGINS / "lots"
PREFIX = "iki/experiments."
OBSERVED_AT = "2026-01-08T10:15:00Z"


def test_witnesses_sign_the_experiment_at_its_recorded_versions(
    tmp_path: Path, database: str
) -> None:
    build = tmp_path / "build"
    plugins = build_order(EXPERIMENTS, LOTS, PLUGINS / "protein")
    built = iki("plugin", "build", *plugins, "--output", build, cwd=PLUGINS)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    # Each run sees its own build's selected review procedure.
    for flow in ("research", "regulated_qc"):
        (build / "iki_plugin_iki__experiments" / "settings.json").write_text(
            json.dumps({"review_flow": flow})
        )
        exercised = django(
            build,
            database,
            "-c",
            "import django; django.setup(); from test_experiment_records import exercise; "
            f"exercise({flow!r})",
        )
        assert exercised.returncode == 0, exercised.stdout + exercised.stderr


def capture(world: World, experiment: Entity, result: Entity) -> None:
    """Launch the installed capture action, give its command the worker payload and commit it."""
    action = catalog.action(PREFIX + "capture_results")
    assert action is not None
    env = f"{action.plugin}.{action.environment}"
    catalog.compute().bindings[env] = Binding(environment=env, templates=(("backend-small", 0),))
    run = runs.launch(
        world.users["editor"],
        action,
        world.project,
        {"experiment": [experiment.pk], "results": [result.pk]},
        {},
        reuse_results=False,
    )
    job = Job.objects.get(run=run)
    job.lease_token = uuid4()
    job.lease_expires_at = timezone.now() + queue.LEASE_TTL
    payload = queue.payload(job, run)
    with TemporaryDirectory() as staging:
        input_file, output_file = Path(staging) / "input.json", Path(staging) / "output.json"
        input_file.write_text(json.dumps(payload))
        command = subprocess.run(  # noqa: S603 - the installed plugin command
            [sys.executable, *action.command[1:]],
            cwd=EXPERIMENTS,
            env={**os.environ, "IKI_INPUT": str(input_file), "IKI_OUTPUT": str(output_file)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert command.returncode == 0, command.stdout + command.stderr
        with transaction.atomic():
            locked = ActionRun.objects.select_for_update().get(pk=run.pk)
            results.commit(locked, json.loads(output_file.read_text()))
    experiment.refresh_from_db()


def sign(world: World, experiment: Entity, who: str, state: str) -> Signature:
    signature = signed(world, who, str(experiment.pk), state, field="review").signature
    assert signature is not None
    assert all(check.ok for check in verify(signature))
    experiment.refresh_from_db()
    assert states.values_of(experiment)["review"] == state
    return signature


def denied_draws(world: World, lot: Entity, given: amounts.Given) -> None:
    """Both ends need access, and private source IDs cannot be copied into another project."""
    other = Project.objects.create(team=world.other_team, name="Private experiment")
    visitor = world.users["outsider"]
    editor = world.users["editor"]
    ProjectMembership.objects.create(user=visitor, project=other, role=ProjectRole.EDITOR)
    target = Entity.objects.get(
        pk=services.create(
            visitor, PREFIX + "Experiment", other, {"title": "Private run"}
        ).entity.pk
    )
    with pytest.raises(PermissionDenied):
        amounts.draw(visitor, lot, given, used_in=target)
    with pytest.raises(PermissionDenied):
        amounts.draw(editor, lot, given, used_in=target)
    # This operator can now edit both projects, but the source project's sharing policy
    # still applies to an experiment receipt that everyone in the destination could read.
    ProjectMembership.objects.create(user=editor, project=other, role=ProjectRole.EDITOR)
    world.project.allow_sharing = False
    world.project.save(update_fields=["allow_sharing"])
    with pytest.raises(PermissionDenied):
        amounts.draw(editor, lot, given, used_in=target)
    target.refresh_from_db()
    assert target.revision == 1
    assert not Movement.objects.filter(used_in=target).exists()
    assert not Edge.objects.filter(node=target, on=lot, kind="used").exists()
    assert Amount.objects.get(entity=lot).value == 2


def concurrent_draws(world: World, sample: Entity) -> None:
    """Two independent lots can reach one experiment without losing a receipt."""
    operator = world.users["admin"]
    target = Entity.objects.get(
        pk=services.create(
            operator, PREFIX + "Experiment", world.project, {"title": "Simultaneous draws"}
        ).entity.pk
    )
    lots = [
        Entity.objects.get(
            pk=services.create(
                operator,
                "iki/lots.GenericLot",
                world.project,
                {"of": str(sample.pk), "source": "bought"},
                amount={"value": 1, "unit": "mg"},
            ).entity.pk
        )
        for _ in range(2)
    ]
    start = Barrier(2, timeout=10)

    def withdraw(lot: Entity) -> str:
        try:
            start.wait()
            movement = amounts.draw(
                operator,
                lot,
                amounts.Given(Decimal("0.125"), "mg"),
                purpose="qa_sampling",
                used_in=target,
            )
            return str(movement.pk)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        movements = set(pool.map(withdraw, lots))
    target.refresh_from_db()
    receipts = services.current_values_of([target])[target.pk]["material_draws"]
    assert target.revision == 3
    assert {receipt["movement"] for receipt in receipts} == movements
    assert {receipt["record"] for receipt in receipts} == {str(lot.pk) for lot in lots}
    assert [Amount.objects.get(entity=lot).value for lot in lots] == [Decimal("0.875")] * 2


def exercise(flow: str) -> None:
    world = build_world(flow + "-")
    ready(world)
    editor = world.users["editor"]
    for who, held in {
        "editor": ("author", "reviewer", "qa"),
        "admin": ("reviewer", "qa"),
        "runner": ("reviewer", "qa"),
        "viewer": ("qa",),
    }.items():
        membership = world.project.memberships.get(user=world.users[who])
        for role in held:
            LabRoleGrant.objects.create(membership=membership, role=PREFIX + role)
    LabRoleGrant.objects.create(
        membership=world.project.memberships.get(user=world.users["admin"]), role="iki/lots.qa"
    )
    body = b"Synthetic observations for " + flow.encode()
    digest = hashlib.sha256(body).hexdigest()
    blob = Blob.objects.create(
        sha256=digest,
        size=len(body),
        storage_uri=f"s3://test/{digest}",
    )
    attachment = File.objects.create(
        blob=blob, project=world.project, name="observations.csv", media_type="text/csv"
    )
    result = Entity.objects.get(
        pk=services.create(
            editor,
            "iki/protein.Protein",
            world.project,
            {
                "name": "Synthetic sequence result",
                "sequence": "GSGA" if flow == "research" else "GAGS",
            },
        ).entity.pk
    )
    lot = Entity.objects.get(
        pk=services.create(
            editor,
            "iki/lots.GenericLot",
            world.project,
            {"of": str(result.pk), "source": "bought", "certificates": [str(attachment.pk)]},
            amount={"value": 2, "unit": "mg"},
        ).entity.pk
    )
    experiment = Entity.objects.get(
        pk=services.create(
            editor,
            PREFIX + "Experiment",
            world.project,
            {
                "title": "Synthetic run",
                "eln_id": "ELN-42",
                "eln_url": "https://eln.example.org/entry/42",
                "eln_revision": "entry-v3",
                "protocol_id": "SOP-7",
                "protocol_revision": "2",
                "observations": [{"observed_at": OBSERVED_AT, "note": "Synthetic run completed"}],
                "attachments": [str(attachment.pk)],
                "samples": [str(lot.pk)],
            },
        ).entity.pk
    )
    given = amounts.Given(Decimal("0.25"), "mg")
    with pytest.raises(amounts.Wrong, match="quarantined"):
        amounts.draw(editor, lot, given, used_in=experiment)
    assert Amount.objects.get(entity=lot).value == 2
    assert not Movement.objects.filter(used_in=experiment).exists()
    signed(world, "admin", str(lot.pk), "released", field="disposition")
    lot.refresh_from_db()
    if flow == "research":
        concurrent_draws(world, result)
        denied_draws(world, lot, given)
    capture(world, experiment, result)
    signer, opening = ("admin", "witnessed") if flow == "research" else ("editor", "submitted")
    signing = begin(world, signer, str(experiment.pk), opening, field="review")
    # The author records another withdrawal while the signer is reauthenticating.
    before = experiment.revision
    movement = amounts.draw(editor, lot, given, used_in=experiment)
    experiment.refresh_from_db()
    assert experiment.revision == before + 1
    interrupted = finish(signing, authenticated(world, signer))
    assert interrupted.status == Status.FAILED and interrupted.signature_id is None
    assert "changed while you were signing in" in interrupted.message
    assert Edge.objects.filter(node=experiment, on=lot, kind="used").exists()
    assert movement.entity_id == lot.pk and movement.used_in_id == experiment.pk
    assert movement.change == Decimal("-0.25") and movement.balance == Decimal("1.75")

    # Corrections to the material evidence must come from a movement of the ledger.
    for by_run in (False, True):
        with pytest.raises(services.Invalid):
            services.update(
                editor,
                experiment,
                {"material_draws": []},
                expected_revision=experiment.revision,
                by_run=by_run,
            )
    # A later withdrawal changes the evidence the author closed. A new capture
    # reaffirms the result pins and includes the latest lot use.
    ref = str(experiment.pk)
    with pytest.raises(Refused):
        begin(world, signer, ref, opening, field="review")
    capture(world, experiment, result)
    if flow == "research":
        with pytest.raises(PermissionDenied):
            begin(world, "viewer", ref, "witnessed", field="review")
        with pytest.raises(Refused):
            begin(world, "editor", ref, "witnessed", field="review")
        with pytest.raises(Refused):
            begin(world, "editor", ref, "submitted", field="review")
        signature = sign(world, experiment, "admin", "witnessed")
        assert Signature.objects.filter(entity=experiment).count() == 1
    else:
        with pytest.raises(Refused):
            begin(world, "admin", ref, "witnessed", field="review")
        sign(world, experiment, "editor", "submitted")
        with pytest.raises(Refused):
            begin(world, "editor", ref, "reviewed", field="review")
        first_review = sign(world, experiment, "admin", "reviewed")
        services.update(
            editor,
            experiment,
            {"protocol_revision": "2.1"},
            expected_revision=experiment.revision,
            reason="Corrected the SOP revision",
            allow_correction=True,
        )
        experiment.refresh_from_db()
        assert states.values_of(experiment)["review"] == "recorded"
        assert json.loads(first_review.content)["values"]["protocol_revision"] == "2"
        with pytest.raises(Refused):
            begin(world, "viewer", ref, "released", field="review")
        with pytest.raises(Refused):
            begin(world, "editor", ref, "submitted", field="review")
        capture(world, experiment, result)
        sign(world, experiment, "editor", "submitted")
        sign(world, experiment, "admin", "reviewed")
        with pytest.raises(Refused):
            begin(world, "admin", ref, "released", field="review")
        signature = sign(world, experiment, "viewer", "released")

    signed_content = json.loads(signature.content)
    evidence: dict[str, Any] = signed_content["values"]
    assert (evidence["eln_id"], evidence["eln_url"], evidence["eln_revision"]) == (
        "ELN-42",
        "https://eln.example.org/entry/42",
        "entry-v3",
    )
    assert evidence["protocol_id"] == "SOP-7"
    assert evidence["protocol_revision"] == ("2" if flow == "research" else "2.1")
    assert evidence["samples"] == [str(lot.pk)]
    (observation,) = evidence["observations"]
    assert datetime.fromisoformat(observation["observed_at"]) == datetime.fromisoformat(OBSERVED_AT)
    assert observation["note"] == "Synthetic run completed"
    assert signed_content["files"] == {str(attachment.pk): digest}
    (draw,) = evidence["material_draws"]
    assert draw["record"] == str(lot.pk) and draw["record_revision"] == lot.revision
    assert (draw["movement"], draw["by"]) == (str(movement.pk), str(editor.pk))
    assert Decimal(draw["amount"]) == Decimal("0.25") and draw["unit"] == "mg"
    (pin,) = evidence["result_pins"]
    assert (pin["record_id"], pin["record_revision"]) == (str(result.pk), result.revision)
    assert (pin["entry_id"], pin["entry_revision"]) == (
        str(result.entry_id),
        services.entry_revision(result),
    )

    # Change a shared sequence fact and its project record independently after sign-off.
    services.update(editor, result, {"length": 4}, expected_revision=result.revision, by_run=True)
    services.update(
        editor, result, {"name": "Corrected result caption"}, expected_revision=result.revision
    )
    result.refresh_from_db()
    assert result.revision > pin["record_revision"]
    assert services.entry_revision(result) != pin["entry_revision"]
    historical = services.values_at(result, pin["record_revision"], pin["entry_revision"])
    assert historical["name"] == "Synthetic sequence result" and historical["length"] is None
    assert all(check.ok for check in verify(signature))

    closed = experiment.revision
    with pytest.raises(amounts.Wrong, match="lock"):
        amounts.draw(editor, lot, given, used_in=experiment)
    experiment.refresh_from_db()
    assert experiment.revision == closed
    assert Movement.objects.filter(used_in=experiment).count() == 1
    assert Amount.objects.get(entity=lot).value == Decimal("1.75")
