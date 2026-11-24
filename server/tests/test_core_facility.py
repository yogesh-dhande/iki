"""Protein production and sequencing on the example LIMS cross-project request path."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from api_client import Api
from first_party_dependencies import PLUGINS, build_order
from org_world import World, build_world
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import begin, ready, signed

from iki_server.entities import requests, services, states
from iki_server.entities.models import Entity, ProjectLink
from iki_server.entities.values import Invalid
from iki_server.files.models import Blob, File
from iki_server.org.models import LabRoleGrant, Project, ProjectMembership, ProjectRole
from iki_server.org.services import Refused
from iki_server.signatures.models import Signature
from iki_server.signatures.services import verify
from iki_server.storage import receipts
from iki_server.storage.models import ReceiptStatus

__all__ = ["database"]

PLUGIN = PLUGINS / "example_lims"
EXAMPLES = PLUGIN / "examples"
LIMS = "iki/example_lims."
RESEARCH = LIMS + "ResearchCoreRequest"
REGULATED = LIMS + "QCCoreRequest"
RESEARCH_DELIVERY = LIMS + "ResearchCoreDeliverable"
REGULATED_DELIVERY = LIMS + "QCCoreDeliverable"
REQUESTER = "outsider"


def test_the_shipping_lims_serves_both_facility_requests(tmp_path: Path, database: str) -> None:
    build = tmp_path / "build"
    built = iki("plugin", "build", *build_order(PLUGIN), "--output", build, cwd=PLUGINS)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    exercise = django(
        build,
        database,
        "-c",
        "import django; django.setup(); from test_core_facility import exercise_core_requests; "
        "exercise_core_requests()",
    )
    assert exercise.returncode == 0, exercise.stdout + exercise.stderr


def _report(project: Project, name: str, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    blob = Blob.objects.create(sha256=digest, size=len(content), storage_uri=f"s3://iki/{digest}")
    return str(
        File.objects.create(project=project, blob=blob, name=name, media_type="text/plain").pk
    )


def _sign(world: World, person: str, entity: Entity, target: str) -> Signature:
    signature = signed(world, person, str(entity.pk), target, field="review").signature
    assert signature is not None
    entity.refresh_from_db()
    assert states.values_of(entity)["review"] == target
    assert all(check.ok for check in verify(signature))
    return signature


def _status(world: World, request: Entity, person: str, state: str) -> None:
    states.set_state(
        world.users[person], request, "status", state, expected_revision=request.revision
    )
    request.refresh_from_db()


def _send(world: World, request: Entity, *, receive_code: str | None = None) -> None:
    requests.submit(
        world.users[REQUESTER], request, world.project, expected_revision=request.revision
    )
    request.refresh_from_db()
    coordinator = world.users["admin"]
    _status(world, request, "admin", "accepted")
    services.update(
        coordinator,
        request,
        {"analyst": str(world.users["editor"].pk), "scheduled_on": "2026-11-24"},
        expected_revision=request.revision,
    )
    request.refresh_from_db()
    if receive_code is not None:
        receipt = receipts.start(coordinator, request)
        scanned = receipts.scan(coordinator, receipt, receive_code)
        assert scanned.found == "matched"
        receipt = scanned.receipt
        receipt = receipts.describe(
            coordinator,
            receipt,
            handed_over_by="Training courier",
            plan_revision=receipt.plan_revision,
        )
        done = receipts.complete(
            coordinator,
            receipt,
            plan_revision=receipt.plan_revision,
            keep_in_custody=[scanned.line.pk],
        )
        assert done.status == ReceiptStatus.COMPLETED
        [received] = Api(world.browser("admin")).get(f"/receipts/{receipt.pk}").json()["lines"]
        assert received["id"] == str(scanned.line.pk) and received["received_unplaced"]
    _status(world, request, "editor", "in_progress")


def _make(world: World, person: str, type_name: str, project: Project, **values: Any) -> Entity:
    saved = services.create(world.users[person], type_name, project, values)
    return Entity.objects.get(pk=saved.entity.pk)


def exercise_core_requests() -> None:
    world = build_world()
    ready(world)
    core = world.project
    asking = Project.objects.create(team=world.team, name="Training requester")
    ProjectMembership.objects.create(
        project=asking, user=world.users[REQUESTER], role=ProjectRole.EDITOR
    )
    for person, roles in {
        "editor": ("analyst", "reviewer", "qa"),
        "admin": ("coordinator", "reviewer", "qa"),
        "viewer": ("qa",),
    }.items():
        membership = core.memberships.get(user=world.users[person])
        for role in roles:
            LabRoleGrant.objects.create(membership=membership, role=LIMS + role)
    request_api = Api(world.browser(REQUESTER))
    requests.set_intake(
        core, types=[RESEARCH], from_registry=False, sources=[asking], by=world.users["admin"]
    )
    _production(world, asking)
    _sequencing(world, asking, request_api)


def _production(world: World, asking: Project) -> None:
    # UniProtKB P00330, Alcohol dehydrogenase 1, Saccharomyces cerevisiae S288c.
    # https://www.uniprot.org/uniprotkb/P00330/entry
    fasta = (EXAMPLES / "P00330.fasta").read_text().splitlines()
    protein = _make(
        world,
        REQUESTER,
        "iki/protein.Protein",
        asking,
        name="Yeast alcohol dehydrogenase 1",
        sequence="".join(fasta[1:]),
        uniprot_accession="P00330",
    )
    subject = {"title": "Training: ADH1 production", "facility_service": "protein_production"}
    with pytest.raises(Invalid, match="positive requested mass"):
        _make(world, REQUESTER, RESEARCH, asking, **subject, protein=str(protein.entry_id))
    research = _make(
        world,
        REQUESTER,
        RESEARCH,
        asking,
        **subject,
        protein=str(protein.entry_id),
        requested_mass=1,
    )
    _send(world, research)
    report_path = EXAMPLES / "P00330-production.txt"
    report = _report(world.project, report_path.name, report_path.read_bytes())
    fields = {
        "request": str(research.pk),
        "protein": str(protein.entry_id),
        "outcome": "fulfilled",
        "lot_number": "TRAINING-ADH1-01",
        "delivered_mass": 0.8,
        "report": report,
    }
    with pytest.raises(Invalid):
        _make(world, "editor", REGULATED_DELIVERY, world.project, **fields)
    deliverable = _make(world, "editor", RESEARCH_DELIVERY, world.project, **fields)
    assert not ProjectLink.objects.filter(entity=deliverable, project=asking).exists()
    review = _sign(world, "admin", deliverable, "signed_off")
    assert Signature.objects.filter(entity=deliverable).count() == 1
    assert json.loads(review.content)["values"]["report"] == report
    assert ProjectLink.objects.get(entity=deliverable, project=asking).via_request_id == research.pk
    _status(world, research, "admin", "delivered")


def _sequencing(world: World, asking: Project, api: Api) -> None:
    # ERR13376132 is the public long-read run for natural yeast isolate CEY652.
    # https://www.ncbi.nlm.nih.gov/sra/ERX12747051
    tube = _make(world, REQUESTER, LIMS + "Tube", asking, name="CEY652", barcode="TRAINING-CEY652")
    private_tube = _make(world, REQUESTER, LIMS + "Tube", asking, name="Another project tube")
    fields = {
        "title": "Training: long-read sequencing",
        "facility_service": "sequencing",
        "sequencing_method": "long_read",
    }
    with pytest.raises(Invalid, match="name the tubes"):
        _make(world, REQUESTER, REGULATED, asking, **fields)
    qc = _make(world, REQUESTER, REGULATED, asking, **fields, tubes=[str(tube.pk)])
    with pytest.raises(requests.NoSuchService):
        requests.submit(world.users[REQUESTER], qc, world.project, expected_revision=qc.revision)
    requests.set_intake(
        world.project,
        types=[REGULATED],
        from_registry=False,
        sources=[asking],
        by=world.users["admin"],
    )
    _send(world, qc, receive_code="TRAINING-CEY652")
    assert ProjectLink.objects.get(entity=tube, project=world.project).via_request_id == qc.pk
    assert not ProjectLink.objects.filter(entity=private_tube, project=world.project).exists()

    report_path = EXAMPLES / "ERR13376132-delivery.txt"
    report = _report(world.project, report_path.name, report_path.read_bytes())
    fields = {
        "request": str(qc.pk),
        "outcome": "fulfilled",
        "tube": str(tube.pk),
        "report": report,
        "run_accession": "ERR13376132",
    }
    with pytest.raises(Invalid):
        _make(world, "editor", RESEARCH_DELIVERY, world.project, **fields)
    duplicate_tube = _make(world, "editor", LIMS + "Tube", world.project, name="Unsent tube")
    with pytest.raises(Invalid, match="Name a tube on the request"):
        _make(
            world,
            "editor",
            REGULATED_DELIVERY,
            world.project,
            **{**fields, "tube": str(duplicate_tube.pk)},
        )
    with pytest.raises(Invalid, match="attach files or a public run accession"):
        _make(
            world, "editor", REGULATED_DELIVERY, world.project, **{**fields, "run_accession": None}
        )
    deliverable = _make(world, "editor", REGULATED_DELIVERY, world.project, **fields)
    _sign(world, "editor", deliverable, "submitted")
    with pytest.raises(Refused, match="did not create, submit or edit"):
        begin(world, "editor", str(deliverable.pk), "reviewed", field="review")
    first = _sign(world, "admin", deliverable, "reviewed")
    assert api.get(f"/entities/{deliverable.pk}").status_code == 404
    assert api.get(f"/files/{report}").status_code == 404

    corrected = _report(world.project, "corrected-report.txt", b"Training: confirmed run accession")
    services.update(
        world.users["editor"],
        deliverable,
        {"report": corrected},
        expected_revision=deliverable.revision,
        reason="Attach the corrected training report",
        allow_correction=True,
    )
    deliverable.refresh_from_db()
    assert states.values_of(deliverable)["review"] == "recorded"
    with pytest.raises(Refused):
        begin(world, "viewer", str(deliverable.pk), "released", field="review")
    _sign(world, "editor", deliverable, "submitted")
    review = _sign(world, "admin", deliverable, "reviewed")
    with pytest.raises(Refused, match="did not create, submit, edit or review"):
        begin(world, "admin", str(deliverable.pk), "released", field="review")

    requester = world.users[REQUESTER]
    services.update(requester, qc, {"tubes": [str(private_tube.pk)]}, expected_revision=qc.revision)
    qc.refresh_from_db()
    with pytest.raises(Refused, match="must still be on the request"):
        begin(world, "viewer", str(deliverable.pk), "released", field="review")
    services.update(requester, qc, {"tubes": [str(tube.pk)]}, expected_revision=qc.revision)
    qc.refresh_from_db()
    release = _sign(world, "viewer", deliverable, "released")
    hall = api.get(f"/entities/{qc.pk}/request").json()
    assert hall["result_releases"] == [
        {
            "entity": str(deliverable.pk),
            "reviewed_revision": review.revision,
            "signed_revision": release.revision,
            "released_revision": deliverable.revision,
        }
    ]
    assert deliverable.released_review_revision == review.revision
    assert json.loads(first.content)["values"]["report"] == report
    assert json.loads(release.content)["values"]["report"] == corrected
    assert api.get(f"/files/{corrected}").status_code == 200
    # The prior signed review and its attachments remain readable in record history.
    assert api.get(f"/files/{report}").status_code == 200
    assert api.get(f"/entities/{deliverable.pk}").json()["values"]["report"] == corrected
    _status(world, qc, "admin", "delivered")
