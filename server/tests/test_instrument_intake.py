"""The instrument intake lab template (examples/plugins/instrument_intake), end to end on a
deployment that installed it: the uploader on the instrument's PC sends a QuantStudio
export with its service account's credential, Iki checks and scans it, the uploader makes
the run's record, and the record's trigger reads the export into it, on a worker that gets
the file only once it's clean (ADR 0080, ADR 0216).

The uploader is the example's own script, through the real SDK, the API and object
storage. Each test runs in a process of its own, on a database that installed the example.
"""

import contextlib
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import boto3
import httpx2
import pytest
from first_party_dependencies import ROOT
from test_plugin_migrations import database, iki, migrate, orm, sync

from iki_server.files import scanner

__all__ = ["database"]

EXAMPLE = ROOT / "examples" / "plugins" / "instrument_intake"
DATA = EXAMPLE / "tests" / "data"
STANDARD_CURVE = DATA / "qs7-flex-3utr-standard-curve.txt"
RELATIVE = DATA / "qs7-flex-rnasep-relative-standard-curve.txt"
COMPARATIVE = DATA / "qs7-flex-tgfb-comparative-ct.txt"
TYPE = "acme/instrument_intake.QPCRRun"
TRIGGER = "acme/instrument_intake.read_new_qpcr_runs"
ENVIRONMENT = "acme/instrument_intake.qpcr-tools"
#: The qPCR core's own bucket, which keeps versions, and the prefix its PCs write under.
FACILITY_BUCKET = "acme-qpcr-core"
FACILITY_PREFIX = "quantstudio-bay-3/"
# The harmless industry-standard antivirus test string, split so its source is ordinary text.
EICAR = "X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

#: The deployment's scanner, as the test sets it: available, or out (its scans wait).
SCANNER = {"available": True}


def scan(*, stream: scanner.ScanStream, sha256: str, size: int) -> scanner.ScanResult:
    """The test deployment's malware scanner: it finds the EICAR string."""
    if not SCANNER["available"]:
        raise scanner.ScannerError("The scanner isn't answering.")
    seen = b""
    while block := stream.read(64 * 1024):
        seen += block
    if EICAR.encode() in seen:
        return scanner.ScanResult("infected", "test scanner", "27", "Eicar-Test-Signature")
    return scanner.ScanResult("clean", "test scanner", "27")


@pytest.fixture(scope="module")
def build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("intake") / "build"
    built = iki("plugin", "build", EXAMPLE, "--output", out, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    return out


@pytest.fixture
def deployment(
    build: Path,
    database: str,
    _s3_endpoint: str,
    _s3_bucket: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[str], Any]]:
    """Runs code in a process on the deployment, its files in the test store and its
    registered files in the facility's bucket."""
    from conftest import S3_ACCESS_KEY, S3_SECRET_KEY

    s3 = boto3.client(
        "s3",
        endpoint_url=_s3_endpoint,
        region_name="us-east-1",
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
    )
    with contextlib.suppress(s3.exceptions.BucketAlreadyOwnedByYou):
        s3.create_bucket(Bucket=FACILITY_BUCKET)
    s3.put_bucket_versioning(Bucket=FACILITY_BUCKET, VersioningConfiguration={"Status": "Enabled"})
    prefix = f"{FACILITY_PREFIX}{uuid.uuid4().hex}/"
    for name, value in {
        "AWS_ACCESS_KEY_ID": S3_ACCESS_KEY,
        "AWS_SECRET_ACCESS_KEY": S3_SECRET_KEY,
        "AWS_DEFAULT_REGION": "us-east-1",
        "IKI_FILES_BUCKET": _s3_bucket,
        "IKI_FILES_PREFIX": f"test-{uuid.uuid4().hex}",
        "IKI_FILES_ENDPOINT_URL": _s3_endpoint,
        # The test store speaks plain HTTP, which a deployment allows only in development.
        "IKI_HTTPS": "false",
        "IKI_FILES_PUBLIC_ENDPOINT_URL": _s3_endpoint,
        "IKI_FILES_REGISTER_LOCATIONS": f"s3://{FACILITY_BUCKET}/{prefix}",
        "IKI_FILES_SCANNER": "test_instrument_intake.scan",
        "IKI_INTAKE_ENDPOINT": _s3_endpoint,
        "IKI_INTAKE_PREFIX": prefix,
    }.items():
        monkeypatch.setenv(name, value)
    migrate(build, database)
    sync(build, database)
    yield lambda code: orm(build, database, code)


def test_an_export_reaches_its_record_and_is_read_only_once_scanned_clean(
    deployment: Callable[[str], Any],
) -> None:
    found = deployment("from test_instrument_intake import uploaded\nresult = uploaded()")
    assert found == {
        "while the scanner was out": {"outcome": 0, "records": 0, "files": 0},
        "once it was clean": {"outcome": 0, "records": 1, "files": 1, "uploads": 1},
        "record": {
            "made_by": "QuantStudio bay 3 uploader",
            "instrument": "QuantStudio 7 Flex, bay 3",
            "file_scan": "clean",
        },
        "import": {
            "requested_by": "Priya Raman",
            "via": "trigger",
            "experiment_name": "01OCT2024",
            "wells": 24,
            "first_well": ["A1", "Standard 1 (1e8)", 16.484, 1e8],
            "standard_curve": [-3.196, 41.924, 0.999, 105.533],
            "revision_via": "run",
        },
        "sent again": {"outcome": 0, "records": 1, "files": 1},
        "sent again without the journal": {"outcome": 0, "records": 1, "files": 1},
        "renamed and sent again": {"outcome": 0, "records": 1, "files": 1},
        "reach": {
            "projects": ["qPCR core"],
            "create elsewhere": 404,
            "upload elsewhere": 404,
            "read elsewhere": 404,
            "look up elsewhere": 404,
            "search": ["qPCR core"],
        },
    }


def test_a_held_file_is_never_read_and_a_registered_one_is(
    deployment: Callable[[str], Any],
) -> None:
    found = deployment("from test_instrument_intake import held\nresult = held()")
    assert found == {
        "registered": {
            "outcome": 0,
            "external": True,
            "version_pinned": True,
            "wells": 16,
            "undetermined": 3,
            "expired_calibrations": ["Background", "RNaseP", "ROI", "Uniformity"],
        },
        "infected": {"outcome": 1, "records": 0, "refused": True},
        "older record holding it": {
            "firing": "failed",
            "why": {
                "code": "launch_failed",
                "message": "Its run couldn't start: TGF-B tissues.txt is quarantined: the "
                "malware scanner flagged it. A Quarantine Admin can review it under Admin → "
                "Security → Quarantine; once it's released, run this again.",
            },
            "run": None,
            "leased": False,
            "imported": False,
        },
        # Its run waits for the scan, and starts by itself once it's clean (ADR 0234).
        "older record still scanning": {
            "firing": "launched",
            "why": None,
            "waiting for": ["TGF-B tissues, 2010.txt"],
            "leased while scanning": False,
            "wells once clean": 9,
        },
        "released": {"outcome": 0, "records": 1, "wells": 9, "released": True},
    }


# --- In the deployment's process ---------------------------------------------------------


class Routes(httpx2.BaseTransport):
    """The SDK's requests: Iki's to the app in this process, the store's over the network."""

    def __init__(self) -> None:
        from django.core.wsgi import get_wsgi_application

        self.app = httpx2.WSGITransport(app=get_wsgi_application())
        self.network = httpx2.HTTPTransport()

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        transport = self.app if request.url.host == "testserver" else self.network
        return transport.handle_request(request)


def _uploader_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "iki_uploader", EXAMPLE / "uploader" / "iki_uploader.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["iki_uploader"] = module
    spec.loader.exec_module(module)
    return module


def current(record: Any) -> dict[str, Any]:
    from iki_server.entities import services as entities

    return entities.current_values_of([record])[record.pk]


def run_tasks() -> None:
    from django.utils import timezone

    from iki_server.tasks.models import TaskRecord
    from iki_server.tasks.runner import Runner

    # Retries wait a while: the test doesn't.
    TaskRecord.objects.filter(status="READY").update(run_after=timezone.now())
    Runner(queues=["default"]).run_ready()


class WorkerClient:
    """Calls ``/worker/v1`` as iki-worker does: JSON, with its own token."""

    def __init__(self, token: str) -> None:
        from django.test import Client

        self.token = token
        self.client = Client(raise_request_exception=False)

    def post(self, path: str, body: Any = None) -> Any:
        return self.client.post(
            f"/worker/v1{path}",
            "" if body is None else json.dumps(body),
            content_type="application/json",
            secure=True,
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def lease(self) -> Any:
        return self.post("/lease?wait=0")

    def report(self, job: dict[str, Any], what: str, **body: Any) -> Any:
        return self.post(
            f"/jobs/{job['job']['id']}/{what}", {"lease_token": job["job"]["lease_token"], **body}
        )


class Facility:
    """A qPCR core's project, its Admin, its uploader's service account and a worker."""

    def __init__(self, root: Path) -> None:
        from django.utils import timezone

        from iki_server.accounts import service_accounts
        from iki_server.accounts.models import User
        from iki_server.oauth import services as oauth
        from iki_server.org.models import (
            OrgRole,
            OrgRoleAssignment,
            Project,
            ProjectMembership,
            ProjectRole,
            Team,
        )
        from iki_server.runs import catalog, triggers, workers
        from iki_server.setup.models import Installation
        from iki_spec.compute import Binding

        Installation.objects.update_or_create(
            pk=1, defaults={"organization_name": "Acme Bio", "setup_completed_at": timezone.now()}
        )
        self.root = root
        self.org_admin = User.objects.create_user("it@acme.example", "Sam Okafor")
        OrgRoleAssignment.objects.create(user=self.org_admin, role=OrgRole.ORG_ADMIN)
        self.quarantine_admin = User.objects.create_user("security@acme.example", "Lena Park")
        OrgRoleAssignment.objects.create(user=self.quarantine_admin, role=OrgRole.QUARANTINE_ADMIN)
        compute_admin = User.objects.create_user("compute@acme.example", "Ravi Shah")
        OrgRoleAssignment.objects.create(user=compute_admin, role=OrgRole.COMPUTE_ADMIN)
        self.manager = User.objects.create_user("priya@acme.example", "Priya Raman")
        team = Team.objects.create(name="Genomics core")
        self.project = Project.objects.create(team=team, name="qPCR core")
        self.other = Project.objects.create(team=team, name="Oncology screens")
        ProjectMembership.objects.create(
            project=self.project, user=self.manager, role=ProjectRole.ADMIN
        )
        ProjectMembership.objects.create(
            project=self.other, user=self.manager, role=ProjectRole.ADMIN
        )
        self.account = service_accounts.create(
            "QuantStudio bay 3 uploader",
            "The PC beside QuantStudio 7 Flex, bay 3.",
            by=self.org_admin,
        )
        ProjectMembership.objects.create(
            project=self.project, user=self.account, role=ProjectRole.EDITOR
        )
        self.credential = oauth.create_credential(
            self.account, "Bay 3 PC", ["read", "write"], by=self.org_admin
        )
        catalog.compute().bindings[ENVIRONMENT] = Binding(
            environment=ENVIRONMENT, templates=(("backend-small", 0),)
        )
        triggers.enable(self.manager, self.project, TRIGGER)
        _, token = workers.create_registration(label="Pods", tier="backend", by=compute_admin)
        made = WorkerClient(token).post(
            "/register",
            {
                "name": "pod-1",
                "version": "0.1.0",
                "capacity": {"cpu": 4, "memory": 16 * 1024**3},
                "plugins": [
                    {
                        "name": "acme/instrument_intake",
                        "sha": None,
                        "environments": None,
                        "bundle": None,
                    }
                ],
            },
        )
        assert made.status_code == 201, made.content
        self.pod = WorkerClient(made.json()["token"])
        self.module = _uploader_module()
        self.exports = root / "exports"
        self.exports.mkdir()
        self.routes = Routes()

    def config(self, **more: Any) -> Any:
        return self.module.Config(
            url="https://testserver",
            project=str(self.project.pk),
            client_id=self.credential.client.client_id,
            client_secret=self.credential.secret,
            record_type=TYPE,
            file_field="results_file",
            key_field="source_sha256",
            values={"instrument": "QuantStudio 7 Flex, bay 3"},
            folder=self.exports,
            patterns=("*.txt",),
            settle=0,
            journal=self.root / "journal.json",
            scan_timeout=1.0,
            attempts=2,
            **more,
        )

    def send(self, **more: Any) -> int:
        config = self.config(**more)

        def pause(seconds: float) -> None:
            # While the SDK waits for Iki, Iki's task runners work.
            run_tasks()
            time.sleep(min(seconds, 0.05))

        session = self.module.Session(config, transport=self.routes, sleep=pause)
        journal = self.module.Journal(config.journal)
        bucket = None
        if config.mode != "upload":
            s3 = boto3.client("s3", endpoint_url=os.environ["IKI_INTAKE_ENDPOINT"])
            bucket = (
                self.module.S3Bucket(FACILITY_BUCKET, client=s3),
                f"s3://{FACILITY_BUCKET}/",
                os.environ["IKI_INTAKE_PREFIX"],
            )
        try:
            return int(
                self.module.run(
                    self.module.Uploader(config, session, journal, bucket=bucket, sleep=pause)
                )
            )
        finally:
            session.close()

    def export(self, source: Path, name: str | None = None, text: str | None = None) -> Path:
        path = self.exports / (name or source.name)
        path.write_bytes(source.read_bytes() if text is None else text.encode())
        return path

    def records(self) -> list[Any]:
        from iki_server.entities.models import Entity

        return list(Entity.objects.filter(type=TYPE, owner_project=self.project).order_by("pk"))

    def work(self) -> dict[str, Any] | None:
        """Leases the queued import and runs its command as a worker would: the file from
        the store, the plugin's own script, and its output reported back."""
        run_tasks()
        leased = self.pod.lease()
        if leased.status_code == 204:
            return None
        assert leased.status_code == 200, leased.content
        job = leased.json()
        directory = self.root / f"job-{job['job']['id']}"
        directory.mkdir()
        for file_id, described in job["files"].items():
            link = self.pod.report(job, f"files/{file_id}")
            assert link.status_code == 200, link.content
            content = httpx2.get(link.json()["url"]).content
            assert hashlib.sha256(content).hexdigest() == described["sha256"]
            described["path"] = str(directory / described["name"])
            Path(described["path"]).write_bytes(content)
        (directory / "input.json").write_text(json.dumps(job))
        ran = subprocess.run(
            [sys.executable, "./scripts/quantstudio.py"],
            cwd=EXAMPLE,
            env={
                **os.environ,
                "IKI_INPUT": str(directory / "input.json"),
                "IKI_OUTPUT": str(directory / "output.json"),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert ran.returncode == 0, ran.stdout + ran.stderr
        output = json.loads((directory / "output.json").read_text())
        done = self.pod.report(job, "complete", output=output)
        assert done.status_code == 200, done.content
        run_tasks()
        return job  # type: ignore[no-any-return]

    def token(self) -> str:
        token, _ = self.module.Session(self.config(), transport=self.routes)._token()
        return str(token)

    def api(self, method: str, path: str, **body: Any) -> httpx2.Response:
        with httpx2.Client(base_url="https://testserver", transport=self.routes) as http:
            return http.request(
                method,
                "/api/v1" + path,
                headers={"Authorization": f"Bearer {self.token()}"},
                **body,
            )


def uploaded() -> dict[str, Any]:
    """Run in the deployment's process (see `deployment`)."""
    import tempfile

    from iki_server.entities.models import Revision
    from iki_server.files import scan_recovery, scan_state
    from iki_server.files.models import File, Upload
    from iki_server.runs.models import ActionRun

    facility = Facility(Path(tempfile.mkdtemp()))
    result: dict[str, Any] = {}

    def counts(outcome: int) -> dict[str, Any]:
        return {
            "outcome": outcome,
            "records": len(facility.records()),
            "files": File.objects.filter(project=facility.project).count(),
        }

    export = facility.export(STANDARD_CURVE, "01OCT2024 3UTR.txt")
    SCANNER["available"] = False
    result["while the scanner was out"] = counts(facility.send())
    # No record could be made: the file isn't a file in Iki until its scan says so.
    assert facility.work() is None
    SCANNER["available"] = True
    # As the deployment's scan recovery does once its scanner is back.
    scan_recovery.recover()
    run_tasks()
    result["once it was clean"] = {
        **counts(facility.send()),
        "uploads": Upload.objects.filter(project=facility.project).count(),
    }
    (record,) = facility.records()
    first = Revision.objects.get(entity=record, number=1)
    file = File.objects.get(project=facility.project)
    result["record"] = {
        "made_by": first.user.name if first.user else None,
        "instrument": current(record)["instrument"],
        "file_scan": scan_state.status_for(file.blob).state,
    }
    job = facility.work()
    assert job is not None
    run = ActionRun.objects.get(pk=job["run"]["id"])
    values = current(record)
    wells = values["wells"]
    (curve,) = values["standard_curves"]
    latest = Revision.objects.filter(entity=record).order_by("-number").first()
    assert latest is not None
    result["import"] = {
        "requested_by": run.requested_by.name,
        "via": run.via,
        "experiment_name": values["experiment_name"],
        "wells": len(wells),
        "first_well": [wells[0]["well"], wells[0]["sample"], wells[0]["cq"], wells[0]["quantity"]],
        "standard_curve": [
            curve["slope"],
            curve["y_intercept"],
            curve["r_squared"],
            curve["efficiency"],
        ],
        "revision_via": latest.via,
    }
    # Its own change fires nothing: no second import.
    assert facility.work() is None

    result["sent again"] = counts(facility.send())
    (facility.root / "journal.json").unlink()
    result["sent again without the journal"] = counts(facility.send())
    export.rename(export.with_name("01OCT2024 3UTR (copy).txt"))
    result["renamed and sent again"] = counts(facility.send())

    # What the uploader's credential reaches: the facility's project and nothing else.
    from scan_fixtures import clean_blob

    from iki_server.entities import services as entities

    theirs = File.objects.create(
        blob=clean_blob(sha256="b" * 64, size=10, storage_uri="s3://iki/elsewhere"),
        project=facility.other,
        name="screen.txt",
        media_type="text/plain",
    )
    elsewhere = entities.create(
        facility.manager,
        TYPE,
        facility.other,
        {"results_file": str(theirs.pk), "source_sha256": "b" * 64},
    ).entity
    other = str(facility.other.pk)
    result["reach"] = {
        "projects": [p["name"] for p in facility.api("GET", "/projects").json()["items"]],
        "create elsewhere": facility.api(
            "POST",
            "/entities",
            json={
                "type": TYPE,
                "project": other,
                "values": {"results_file": str(theirs.pk), "source_sha256": "c" * 64},
            },
        ).status_code,
        "upload elsewhere": facility.api(
            "POST",
            "/uploads",
            json={
                "project": other,
                "name": "run.txt",
                "media_type": "text/plain",
                "size": 1,
                "sha256": "d" * 64,
            },
        ).status_code,
        "read elsewhere": facility.api("GET", f"/entities/{elsewhere.pk}").status_code,
        "look up elsewhere": facility.api(
            "GET",
            "/entities/lookup",
            params={"type": TYPE, "field": "source_sha256", "value": "b" * 64, "project": other},
        ).status_code,
        "search": sorted(
            {
                e["owner_project"]["name"]
                for e in facility.api("POST", "/entities/search", json={"type": TYPE}).json()[
                    "items"
                ]
            }
        ),
    }
    return result


def held() -> dict[str, Any]:
    """Run in the deployment's process (see `deployment`)."""
    import tempfile

    from iki_server.entities import services as entities
    from iki_server.files import scan_state
    from iki_server.files.models import Blob, File
    from iki_server.runs.models import Job, TriggerFiring

    facility = Facility(Path(tempfile.mkdtemp()))
    result: dict[str, Any] = {}

    # Registered in place: the PC writes the export to the facility's bucket.
    facility.export(RELATIVE, "RNaseP relative standard curve.txt")
    outcome = facility.send(mode="register")
    (record,) = facility.records()
    file = File.objects.select_related("blob").get(pk=current(record)["results_file"])
    assert facility.work() is not None
    values = current(record)
    result["registered"] = {
        "outcome": outcome,
        "external": file.blob.registered_external,
        "version_pinned": file.blob.object_version not in ("", "null"),
        "wells": len(values["wells"]),
        "undetermined": sum(1 for w in values["wells"] if w["undetermined"]),
        "expired_calibrations": values["expired_calibrations"],
    }
    for path in facility.exports.iterdir():
        path.unlink()

    # An export the scanner finds infected: held, and no record holds it.
    text = COMPARATIVE.read_bytes().decode()
    infected = text.replace("* Experiment Comment = NA", f"* Experiment Comment = {EICAR}", 1)
    assert infected != text
    facility.export(COMPARATIVE, "TGF-B tissues.txt", infected)
    outcome = facility.send()
    journal = json.loads((facility.root / "journal.json").read_text())
    (entry,) = (e for e in journal["exports"].values() if "held" in e)
    attempt = facility.api(
        "POST",
        "/entities",
        json={
            "type": TYPE,
            "project": str(facility.project.pk),
            "values": {
                "results_file": entry["file"],
                "source_sha256": hashlib.sha256(infected.encode()).hexdigest(),
            },
        },
    )
    result["infected"] = {
        "outcome": outcome,
        "records": len(facility.records()) - 1,
        "refused": attempt.status_code == 422 and "quarantin" in attempt.text.lower(),
    }

    def older_record(file_id: str, sha256: str) -> Any:
        # As a record made before the deployment scanned files could hold one.
        with patch.object(scan_state.ScanStatus, "require_usable"):
            made = entities.create(
                facility.manager,
                TYPE,
                facility.project,
                {"results_file": file_id, "source_sha256": sha256},
            ).entity
        run_tasks()
        return made

    held_file = File.objects.get(pk=entry["file"])
    older = older_record(str(held_file.pk), "e" * 64)
    firing = TriggerFiring.objects.get(entity=older)
    result["older record holding it"] = {
        "firing": firing.state,
        "why": firing.error,
        "run": firing.run_id,
        "leased": facility.work() is not None,
        "imported": current(older)["experiment_name"] is not None,
    }

    # An older record whose file was never scanned: its run waits for the scan (ADR 0234).
    content = COMPARATIVE.read_bytes()
    key = f"{os.environ['IKI_INTAKE_PREFIX']}older/TGF-B tissues, 2010.txt"
    s3 = boto3.client("s3", endpoint_url=os.environ["IKI_INTAKE_ENDPOINT"])
    blob = Blob.objects.create(
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        storage_uri=f"s3://{FACILITY_BUCKET}/{key}",
        registered_external=True,
        object_version=s3.put_object(Bucket=FACILITY_BUCKET, Key=key, Body=content)["VersionId"],
    )
    unscanned = File.objects.create(
        blob=blob, project=facility.project, name="TGF-B tissues, 2010.txt", media_type="text/plain"
    )
    pending = older_record(str(unscanned.pk), blob.sha256)
    firing = TriggerFiring.objects.get(entity=pending)
    assert firing.run is not None
    waiting = File.objects.filter(
        pk__in=Job.objects.get(run=firing.run).scan_wait_files
    ).values_list("name", flat=True)
    leased = facility.work() is not None
    scan_state.record_result(blob, state="clean", scanner="test scanner", signature_version="27")
    assert facility.work() is not None
    result["older record still scanning"] = {
        "firing": firing.state,
        "why": firing.error,
        "waiting for": list(waiting),
        "leased while scanning": leased,
        "wells once clean": len(current(pending)["wells"]),
    }

    # A Quarantine Admin finds the finding false and releases it: the next pass makes the
    # record, and the trigger reads it.
    decision = scan_state.status_for(held_file.blob).decision_id
    assert decision is not None
    scan_state.release(
        decision,
        actor=facility.quarantine_admin,
        reason="The comment field holds the EICAR test string a validation run typed in.",
    )
    outcome = facility.send()
    made = [r for r in facility.records() if r.pk not in (record.pk, older.pk, pending.pk)]
    (released,) = made
    assert facility.work() is not None
    result["released"] = {
        "outcome": outcome,
        "records": len(made),
        "wells": len(current(released)["wells"]),
        "released": scan_state.status_for(held_file.blob).released,
    }
    return result
