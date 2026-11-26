"""A run that reads a file still being scanned waits for it (ADR 0234). However it's started,
it's queued and no worker takes it. It starts once the scan finds the file clean, through a
scanner outage too, or fails saying why once the file is held. Its history records the wait
and the decisions that ended it."""

from typing import Any
from unittest.mock import patch

import pytest
from api_client import Api
from org_world import World
from test_api_entities import api_as
from test_api_entities import patch as edit
from test_request_ordering import ON_ARRIVAL, client_rank, ranked, ranker
from test_requests import build_lab, submit, takes
from test_run_imports import IMPORT
from test_runs import launch, protein
from test_scan_gates import _file, _infected, _legacy_lot
from test_triggers import FLAG, on, sample
from test_workers import WorkerClient, keep_connection, worker, world
from test_workflows import IMPORT_AND_WEIGH
from test_workflows import start as start_workflow

from iki_server.audit.models import AuditEvent
from iki_server.entities import services
from iki_server.entities.models import Entity
from iki_server.files import scan_state, scanner
from iki_server.files import services as files
from iki_server.files.models import File
from iki_server.runs import scan_waits
from iki_server.runs import services as runs
from iki_server.runs.models import ActionRun, FiringState, Job, RunStatus, TriggerFiring
from iki_server.tasks.runner import Runner

__all__ = ["keep_connection", "world"]  # fixtures, from test_workers

pytestmark = pytest.mark.django_db

PURITY = "acme/protein.measure_purity"
#: What the scanner reports of the signatures it scanned with.
CLAMAV, SIGNATURES = "ClamAV 1.4.3", "27512"


def run_tasks() -> int:
    return Runner(queues=["default"]).run_ready()


def clean(file: File) -> None:
    scan_state.record_result(file.blob, state="clean", scanner=CLAMAV, signature_version=SIGNATURES)


def legacy(make: Any) -> Any:
    """A record holding a file from before scanning: as an older deployment could have it."""
    with patch.object(scan_state.ScanStatus, "require_usable"):
        return make()


def sample_of_lot(world: World, file: File) -> Entity:
    """A sample whose lot's report is `file`, which measure_purity reads (ADR 0105)."""
    lot = _legacy_lot(world, file)
    made = services.create(
        world.users["editor"], "acme/protein.Sample", world.project, {"label": "S1", "lot": lot.pk}
    )
    return Entity.objects.get(pk=made.entity.pk)


def shown(world: World, run: str) -> dict[str, Any]:
    found: dict[str, Any] = api_as(world, "viewer").get(f"/runs/{run}").json()
    return found


def attempts(world: World, run: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = api_as(world, "viewer").get(f"/runs/{run}/attempts").json()
    return found


def waits_then_starts(world: World, run: ActionRun, file: File, pod: WorkerClient) -> Any:
    """`run` waits for `file`: no worker takes it until it's scanned clean, then one does."""
    assert run.status == RunStatus.QUEUED
    job = Job.objects.get(run=run)
    assert job.waiting_for_scan and job.scan_wait_files == [file.pk]
    assert pod.lease().status_code == 204
    clean(file)
    run_tasks()
    leased = pod.lease()
    assert leased.status_code == 200, leased.content
    assert str(file.pk) in leased.json()["files"]
    return leased.json()


# --- A person's launch --------------------------------------------------------------------------


def test_a_launch_waits_for_a_file_being_scanned_and_starts_once_its_clean(world: World) -> None:
    file = _file(world)
    made = launch(
        api_as(world), world.project, {"sample": str(sample_of_lot(world, file).pk)}, PURITY
    )
    assert made.status_code == 201, made.content
    run = made.json()
    assert run["status"] == "queued"
    seen = shown(world, run["hrid"])
    assert seen["waiting_for_scan"]["files"] == [{"id": str(file.pk), "name": "structure.pdb"}]
    assert seen["waiting_for_scan"]["others"] == 0
    assert seen["waiting"] == (
        "A file it reads (structure.pdb) is being scanned for malware. It starts once the scan "
        "finds it clean."
    )
    job = Job.objects.get(run_id=run["id"])
    queued_at = job.queued_at
    actions = list(
        AuditEvent.objects.filter(target_id=run["id"]).order_by("id").values_list("action", "data")
    )
    assert actions[0][0] == "run.launched"
    assert actions[1] == ("run.waiting_for_scan", {"attempt": 1, "files": [str(file.pk)]})
    (attempt,) = attempts(world, run["hrid"])
    assert attempt["scan_wait"]["ended_at"] is None and attempt["scan_wait"]["files"] == 1

    pod = worker(world)
    assert pod.lease().status_code == 204
    # Until a decision, the sweep leaves it waiting.
    assert scan_waits.recover() == 0
    clean(file)
    assert run_tasks() >= 1
    job.refresh_from_db()
    assert job.scan_wait_ended_at is not None and not job.waiting_for_scan
    # Its place in the queue is as it was.
    assert job.queued_at == queued_at
    ended = AuditEvent.objects.get(action="run.scan_wait_ended", target_id=run["id"])
    assert ended.data["outcome"] == "usable" and ended.data["waited_seconds"] >= 0
    (decision,) = ended.data["decisions"]
    assert decision == {
        "file": str(file.pk),
        "decision": str(scan_state.status_for(file.blob).decision_id),
        "state": "clean",
        "scanner": CLAMAV,
        "signature_version": SIGNATURES,
        "scanned_at": decision["scanned_at"],
        "reason": "",
        "released": False,
        "allow_unscanned": False,
    }
    assert decision["scanned_at"] is not None
    seen = shown(world, run["hrid"])
    assert seen["waiting_for_scan"] is None and seen["waiting"] is None
    (attempt,) = attempts(world, run["hrid"])
    assert attempt["scan_wait"]["ended_at"] is not None
    leased = pod.lease()
    assert leased.status_code == 200, leased.content
    assert list(leased.json()["files"]) == [str(file.pk)]


def test_a_file_param_being_scanned_is_waited_for(world: World) -> None:
    fasta = _file(world, "panel.fasta")
    made = launch(api_as(world), world.project, {}, IMPORT, params={"file": str(fasta.pk)})
    assert made.status_code == 201, made.content
    job = waits_then_starts(
        world, ActionRun.objects.get(pk=made.json()["id"]), fasta, worker(world)
    )
    assert job["params"]["file"] == str(fasta.pk)


@pytest.mark.parametrize(
    ("state", "reason", "code", "said"),
    [
        (
            "infected",
            "Eicar-Test-Signature",
            "file_quarantined",
            "structure.pdb is quarantined: the malware scanner flagged it.",
        ),
        (
            "not_scanned",
            "too_large",
            "file_not_scanned",
            "structure.pdb is held: it's larger than this deployment scans",
        ),
    ],
)
def test_a_held_file_ends_a_waiting_run_saying_why(
    world: World, state: str, reason: str, code: str, said: str
) -> None:
    file, certificate = _file(world), _file(world, "certificate.pdb")
    lot = _legacy_lot(world, file)
    kit = protein(api_as(world), world.project, "kit")
    additions = [{"material": kit["id"], "certificate": str(certificate.pk)}]
    held = legacy(
        lambda: (
            services.create(
                world.users["editor"],
                "acme/protein.Sample",
                world.project,
                {"label": "S1", "lot": lot.pk, "additions": additions},
            ).entity
        )
    )
    made = launch(api_as(world), world.project, {"sample": str(held.pk)}, PURITY)
    assert made.status_code == 201, made.content
    waiting = shown(world, made.json()["hrid"])
    assert waiting["waiting_for_scan"]["files"] == [
        {"id": str(certificate.pk), "name": "certificate.pdb"},
        {"id": str(file.pk), "name": "structure.pdb"},
    ]
    assert waiting["waiting"] == (
        "2 files it reads (certificate.pdb, structure.pdb) are being scanned for malware. It "
        "starts once the scans find them clean."
    )
    # A held file ends it, though the certificate's scan hasn't finished.
    scan_state.record_result(
        file.blob, state=state, scanner=CLAMAV, signature_version=SIGNATURES, reason=reason
    )
    run_tasks()
    run = ActionRun.objects.get(pk=made.json()["id"])
    assert run.status == RunStatus.FAILED and run.error is not None
    assert run.error["code"] == code and run.error["message"].startswith(said)
    ended = AuditEvent.objects.get(action="run.scan_wait_ended", target_id=str(run.pk))
    assert ended.data["outcome"] == "held"
    assert {d["state"] for d in ended.data["decisions"]} == {state, "pending"}
    assert worker(world).lease().status_code == 204


def test_a_launch_with_a_held_file_is_refused_naming_it(world: World) -> None:
    file = _file(world)
    held = sample_of_lot(world, file)
    _infected(file)
    refused = launch(api_as(world), world.project, {"sample": str(held.pk)}, PURITY)
    assert refused.status_code == 409, refused.content
    assert refused.json()["detail"].startswith("structure.pdb is quarantined")
    assert not ActionRun.objects.exists()


def test_a_run_waits_through_a_scanner_outage(world: World) -> None:
    file = _file(world)
    made = launch(
        api_as(world), world.project, {"sample": str(sample_of_lot(world, file).pk)}, PURITY
    )
    assert made.status_code == 201, made.content
    pod = worker(world)
    down = scanner.ScannerError("clamd closed the connection during the scan.")
    with (
        patch.object(scanner, "scan_object", side_effect=down),
        pytest.raises(scanner.ScannerError),
    ):
        files.scan_source(file.blob)
    # The file is still pending, so the run waits on: neither failed nor taken.
    assert scan_state.status_for(file.blob).state == "pending"
    assert scan_waits.recover() == 0
    run = ActionRun.objects.get(pk=made.json()["id"])
    assert run.status == RunStatus.QUEUED and Job.objects.get(run=run).waiting_for_scan
    assert pod.lease().status_code == 204
    # The scanner is back: the retried scan finds the file clean, and the run starts.
    back = scanner.ScanResult(status="clean", scanner=CLAMAV, signature_version=SIGNATURES)
    with patch.object(scanner, "scan_object", return_value=back):
        assert files.scan_source(file.blob).state == "clean"
    run_tasks()
    assert pod.lease().status_code == 200


def test_a_job_queued_before_scanning_waits_at_its_lease_and_the_sweep_releases_it(
    world: World,
) -> None:
    file = _file(world)
    held = sample_of_lot(world, file)
    # Launched when nothing was scanned: its files were taken as usable.
    usable = scan_state.ScanStatus(state="clean")
    with patch(
        "iki_server.runs.included.statuses_for_blobs", lambda ids: dict.fromkeys(ids, usable)
    ):
        made = launch(api_as(world), world.project, {"sample": str(held.pk)}, PURITY)
    assert made.status_code == 201, made.content
    before = Job.objects.get(run_id=made.json()["id"])
    assert not before.waiting_for_scan
    pod = worker(world)
    assert pod.lease().status_code == 204
    job = Job.objects.get(pk=before.pk)
    assert job.waiting_for_scan and job.scan_wait_files == [file.pk]
    assert job.queued_at == before.queued_at and job.state == "queued"
    # A decision recorded with no task to find it (one lost, say) is found by the sweep.
    with patch.object(scan_waits, "any_waiting", return_value=False):
        clean(file)
    run_tasks()
    assert pod.lease().status_code == 204
    assert scan_waits.recover() == 1
    assert pod.lease().status_code == 200


def test_a_run_reuses_nothing_before_its_files_are_scanned(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Built from a lockfile, its plugin has a commit: so its runs have a reuse key.
    monkeypatch.setattr(runs, "plugin_sha", lambda plugin: "a" * 40)
    file = _file(world)
    held = sample_of_lot(world, file)
    inputs = {"sample": str(held.pk)}
    usable = scan_state.ScanStatus(state="clean")
    with patch(
        "iki_server.runs.included.statuses_for_blobs", lambda ids: dict.fromkeys(ids, usable)
    ):
        earlier = launch(api_as(world), world.project, inputs, PURITY)
    assert earlier.status_code == 201, earlier.content
    ActionRun.objects.filter(pk=earlier.json()["id"]).update(status=RunStatus.SUCCEEDED)

    again = launch(api_as(world), world.project, inputs, PURITY)
    assert again.status_code == 201, again.content
    assert again.json()["reused"] is False
    keys = {r.reuse_key for r in ActionRun.objects.all()}
    assert len(keys) == 1 and "" not in keys
    assert Job.objects.get(run_id=again.json()["id"]).waiting_for_scan
    clean(file)
    run_tasks()
    # Once it's scanned, an identical run reuses the earlier one's results.
    reused = launch(api_as(world), world.project, inputs, PURITY)
    assert reused.status_code == 201, reused.content
    assert reused.json()["reused_from"]["hrid"] == earlier.json()["hrid"]


# --- Triggers and workflows ------------------------------------------------------------------


def test_a_triggered_run_waits_for_its_related_records_files(world: World) -> None:
    on(world, FLAG)
    editor = api_as(world)
    kit = protein(editor, world.project, "kit")
    certificate = _file(world, "certificate.pdb")
    additions = [{"material": kit["id"], "certificate": str(certificate.pk)}]
    legacy(lambda: sample(world, world.project, "S-1", kit, additions=additions))
    changed = edit(editor, kit["id"], {"values": {"summary": "New"}}, '"1"')
    assert changed.status_code == 200, changed.content
    run_tasks()
    firing = TriggerFiring.objects.get()
    # It launched a run that waits, rather than failing for good.
    assert firing.state == FiringState.LAUNCHED, firing.error
    assert firing.run is not None
    waits_then_starts(world, firing.run, certificate, worker(world))


def test_a_received_requests_run_waits_for_its_form(world: World) -> None:
    lab = build_lab(world)
    takes(lab)
    client_rank(lab, lab.world.project, 1)
    form = _file(world, "form.pdb")
    admin = lab.api("core_admin")
    url = f"/projects/{lab.core.pk}/triggers/{ON_ARRIVAL}/enable"
    assert admin.post(url, {"run_as": str(ranker(lab).pk)}).status_code == 200
    made = legacy(lambda: ranked(lab, form=str(form.pk)))
    assert submit(lab, made["id"]).status_code == 200
    run_tasks()
    firing = TriggerFiring.objects.get(event="request.received")
    assert firing.state == FiringState.LAUNCHED, firing.error
    assert firing.run is not None
    waits_then_starts(world, firing.run, form, worker(world))


def test_a_workflow_step_waits_for_its_file_param(world: World) -> None:
    fasta = _file(world, "panel.fasta")
    editor: Api = api_as(world)
    started = start_workflow(editor, world, {}, IMPORT_AND_WEIGH, params={"file": str(fasta.pk)})
    assert started.status_code == 201, started.content
    step = {s["id"]: s for s in started.json()["steps"]}["import"]
    assert step["state"] == "running", step
    run = ActionRun.objects.get(workflow_id=started.json()["id"], workflow_step="import")
    waits_then_starts(world, run, fasta, worker(world))
