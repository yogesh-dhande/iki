"""Protein production and sequencing as cross-project requests."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from api_client import Api
from django.core.exceptions import PermissionDenied
from first_party_dependencies import PLUGINS, ROOT, build_order
from org_world import World, build_world
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import begin, ready, signed

from iki_server.amounts import services as amounts
from iki_server.amounts.models import DrawPurpose
from iki_server.entities import access, requests, services, states
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

PLUGIN = PLUGINS / "requests"
EXAMPLES = ROOT / "examples" / "core-facility-requests"
REQUESTS = "iki/requests."
PLAIN = REQUESTS + "PlainRequest"
RESEARCH = REQUESTS + "ResearchRequest"
REGULATED = REQUESTS + "QCRequest"
RESEARCH_DELIVERY = REQUESTS + "ResearchDeliverable"
REGULATED_DELIVERY = REQUESTS + "QCDeliverable"
BASIC_DELIVERY = REQUESTS + "PlainDeliverable"
WORK = REQUESTS + "WorkItem"
REQUESTER = "outsider"


def _facility_build(tmp_path: Path, database: str, plugin: str) -> Path:
    build = tmp_path / "build"
    plugins = build_order(PLUGIN, PLUGINS / plugin)
    built = iki("plugin", "build", *plugins, "--output", build, cwd=PLUGINS)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    return build


def test_the_shipping_lims_serves_both_facility_requests(tmp_path: Path, database: str) -> None:
    exercise = django(
        _facility_build(tmp_path, database, "protein"),
        database,
        "-c",
        "import django; django.setup(); from test_core_facility import exercise_core_requests; "
        "exercise_core_requests()",
    )
    assert exercise.returncode == 0, exercise.stdout + exercise.stderr


@pytest.mark.parametrize(
    ("request_type", "delivery_type"),
    [(PLAIN, BASIC_DELIVERY), (RESEARCH, RESEARCH_DELIVERY), (REGULATED, REGULATED_DELIVERY)],
    ids=["plain-tube", "research-lot", "qc-nested-aliquot"],
)
def test_returned_sample_covers_its_draw(
    tmp_path: Path, database: str, request_type: str, delivery_type: str
) -> None:
    exercise = django(
        _facility_build(tmp_path, database, "samples"),
        database,
        "-c",
        "import django; django.setup(); from test_core_facility import exercise_sample_delivery; "
        f"exercise_sample_delivery({request_type!r}, {delivery_type!r})",
    )
    assert exercise.returncode == 0, exercise.stdout + exercise.stderr


def _report(project: Project, name: str, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    blob = Blob.objects.create(sha256=digest, size=len(content), storage_uri=f"s3://iki/{digest}")
    return str(
        File.objects.create(project=project, blob=blob, name=name, media_type="text/plain").pk
    )


def _sign(world: World, person: str, entity: Entity, target: str, reason: str = "") -> Signature:
    signature = signed(
        world, person, str(entity.pk), target, field="review", reason=reason
    ).signature
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


def _make(
    world: World,
    person: str,
    type_name: str,
    project: Project,
    *,
    amount: Any = None,
    **values: Any,
) -> Entity:
    saved = services.create(world.users[person], type_name, project, values, amount=amount)
    return Entity.objects.get(pk=saved.entity.pk)


def _facility(*request_types: str) -> tuple[World, Project]:
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
            LabRoleGrant.objects.create(membership=membership, role="iki/lab_roles." + role)
    requests.set_intake(
        core,
        types=request_types,
        from_registry=False,
        sources=[asking],
        by=world.users["admin"],
    )
    return world, asking


def exercise_core_requests() -> None:
    world, asking = _facility(RESEARCH, PLAIN)
    _production(world, asking)
    _plain_delivery_coverage(world, asking)
    _sequencing(world, asking, Api(world.browser(REQUESTER)))


def exercise_sample_delivery(request_type: str, delivery_type: str) -> None:
    world, asking = _facility(request_type)
    core = world.project
    analyst = world.users["editor"]
    membership = ProjectMembership.objects.create(
        project=asking, user=analyst, role=ProjectRole.EDITOR
    )
    stock = _make(world, REQUESTER, "iki/core.Collection", asking, name="Reference material")
    if request_type == RESEARCH:
        subject = _make(
            world,
            REQUESTER,
            "iki/lots.GenericLot",
            asking,
            of=str(stock.pk),
            amount={"value": 10, "unit": "mg"},
        )
        LabRoleGrant.objects.create(membership=membership, role="iki/lots.qa")
        scope = {"materials": [str(subject.pk)]}
    else:
        subject = _make(
            world,
            REQUESTER,
            "iki/inventory.Tube",
            asking,
            contents=str(stock.pk),
            amount={"value": 10, "unit": "uL"},
        )
        scope = {"tubes": [str(subject.pk)]}
        if request_type == REGULATED:
            scope["analyses"] = ["sec_purity"]
    request = _make(world, REQUESTER, request_type, asking, title="Sample report", **scope)
    _send(world, request)
    source = subject
    if request_type == REGULATED:
        for aliquot_volume in (4, 2):
            (source,) = amounts.split(
                analyst, source, 1, amounts.Given.of({"value": aliquot_volume, "unit": "uL"})
            ).aliquots
        services.link(analyst, source, core)

    def pending_sample(source: Entity) -> Entity:
        return _make(
            world,
            "editor",
            "iki/samples.Sample",
            core,
            source=str(source.pk),
            purpose=str(request.pk),
        )

    fields: dict[str, Any] = {
        "request": str(request.pk),
        "outcome": "fulfilled",
        "report": _report(core, "sample-report.txt", b"Sample measurements"),
    }
    if request_type == REGULATED:
        fields.update(analysis="sec_purity", result="pass", purity=99)

    def report_on(material: Entity) -> Entity:
        return _make(world, "editor", delivery_type, core, **fields, material=str(material.pk))

    sample = pending_sample(source)
    with pytest.raises(Refused, match="has no returned result yet"):
        _status(world, request, "admin", "delivered")
    with pytest.raises(Invalid, match="Choose a sample or tube on this request"):
        report_on(sample)

    if request_type == RESEARCH:
        # Drawing from a tube containing the requested lot debits the tube's own balance.
        tube = _make(
            world,
            "editor",
            "iki/inventory.Tube",
            core,
            contents=str(subject.pk),
            amount={"value": 10, "unit": "uL"},
        )
        other = pending_sample(tube)
        amounts.draw(
            analyst,
            tube,
            amounts.Given.of({"value": 1, "unit": "uL"}),
            used_in=other,
            purpose=DrawPurpose.QA_SAMPLING,
            confirm=tube.hrid,
        )
        with pytest.raises(Invalid, match="Choose a sample or tube on this request"):
            report_on(other)
        amounts.draw(
            analyst,
            source,
            amounts.Given.of({"value": 1, "unit": "mg"}),
            used_in=sample,
            purpose=DrawPurpose.QA_SAMPLING,
        )
    else:
        volume = amounts.Given.of({"value": 1, "unit": "uL"})
        if request_type == PLAIN:
            work = _make(world, "editor", "iki/core.Collection", core, name="Assay measurements")
            amounts.draw(analyst, source, volume, used_in=work, confirm=source.hrid)
            with pytest.raises(Invalid, match="Choose a sample or tube on this request"):
                report_on(work)
        amounts.draw(analyst, source, volume, used_in=sample, confirm=source.hrid)
    result = report_on(sample)
    with pytest.raises(Refused, match="has no returned result yet"):
        _status(world, request, "admin", "delivered")
    if request_type == PLAIN:
        _status(world, result, "admin", "delivered")
    elif request_type == RESEARCH:
        _sign(world, "admin", result, "signed_off")
    else:
        _sign(world, "editor", result, "submitted")
        _sign(world, "admin", result, "reviewed")
        with pytest.raises(Refused, match="has no returned result yet"):
            _status(world, request, "admin", "delivered")
        _sign(world, "viewer", result, "released")
    assert ProjectLink.objects.get(entity=result, project=asking).via_request_id == request.pk
    _status(world, request, "admin", "delivered")


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
        _make(world, REQUESTER, RESEARCH, asking, **subject, records=[str(protein.pk)])
    research = _make(
        world,
        REQUESTER,
        RESEARCH,
        asking,
        **subject,
        records=[str(protein.pk)],
        requested_mass=1,
    )
    with pytest.raises(Invalid, match="isn't a request sent to"):
        _make(world, REQUESTER, WORK, asking, request=str(research.pk))
    _send(world, research)
    with pytest.raises(Refused, match="returned"):
        _status(world, research, "admin", "delivered")
    report_path = EXAMPLES / "P00330-production.txt"
    report = _report(world.project, report_path.name, report_path.read_bytes())
    fields = {
        "request": str(research.pk),
        "protein": str(protein.pk),
        "outcome": "fulfilled",
        "lot_number": "TRAINING-ADH1-01",
        "delivered_mass": 0.8,
        "report": report,
    }
    with pytest.raises(Invalid):
        _make(world, "editor", REGULATED_DELIVERY, world.project, **fields)
    deliverable = _make(world, "editor", RESEARCH_DELIVERY, world.project, **fields)
    assert not ProjectLink.objects.filter(entity=deliverable, project=asking).exists()
    with pytest.raises(Refused, match="returned"):
        _status(world, research, "admin", "delivered")
    review = _sign(world, "admin", deliverable, "signed_off")
    assert Signature.objects.filter(entity=deliverable).count() == 1
    assert json.loads(review.content)["values"]["report"] == report
    assert ProjectLink.objects.get(entity=deliverable, project=asking).via_request_id == research.pk
    with pytest.raises(Invalid):
        _make(world, "editor", BASIC_DELIVERY, world.project, **fields)
    plain = _make(world, REQUESTER, PLAIN, asking, title="Training bench work")
    _send(world, plain)
    _private_work_and_basic_deliveries(
        world, asking, plain, {"request": str(plain.pk), "outcome": "fulfilled", "report": report}
    )
    _status(world, research, "admin", "delivered")


def _private_work_and_basic_deliveries(
    world: World, asking: Project, request: Entity, fields: dict[str, Any]
) -> None:
    core = world.project
    assert ProjectLink.objects.get(entity=request, project=core).via_request_id == request.pk
    item = _make(
        world,
        "editor",
        WORK,
        core,
        request=str(request.pk),
        scheduled_on="2026-11-24",
        instrument="Training bench",
        notes="Internal queue notes",
    )
    cancelled = _make(world, "editor", WORK, core, request=str(request.pk))
    assert item.owner_project_id == core.pk
    assert states.values_of(item)["status"] == "queued"
    assert services.current_values(services.specific(item))["priority"] == "routine"
    with pytest.raises(Refused):
        _status(world, item, "editor", "running")
    with pytest.raises(PermissionDenied):
        _status(world, item, "admin", "scheduled")
    for target in ("scheduled", "running", "done"):
        _status(world, item, "editor", target)
    with pytest.raises(Invalid, match="needs a reason"):
        _status(world, cancelled, "editor", "cancelled")
    states.set_state(
        world.users["editor"],
        cancelled,
        "status",
        "cancelled",
        expected_revision=cancelled.revision,
        reason="Cancelled by the core",
    )
    cancelled.refresh_from_db()
    assert states.values_of(cancelled)["status"] == "cancelled"

    recorded = _make(world, "editor", BASIC_DELIVERY, core, **fields)
    delivered = _make(world, "editor", BASIC_DELIVERY, core, **fields)
    with pytest.raises(PermissionDenied):
        _status(world, delivered, "viewer", "delivered")
    _status(world, delivered, "admin", "delivered")
    assert not Signature.objects.filter(entity=delivered).exists()
    assert ProjectLink.objects.get(entity=delivered, project=asking).via_request_id == request.pk
    assert not ProjectLink.objects.filter(
        entity__in=[item, cancelled, recorded], project=asking
    ).exists()

    # As in the request access matrix, compare UUID reads, listings and search for both sides.
    candidates = [item, cancelled, recorded, delivered]
    all_ids = {str(record.pk) for record in candidates}
    for who, expected in (("editor", all_ids), (REQUESTER, {str(delivered.pk)})):
        api = Api(world.browser(who))
        for record in candidates:
            status = api.get(f"/entities/{record.pk}").status_code
            assert status == (200 if str(record.pk) in expected else 404), (who, record.hrid)
        visible = {str(pk) for pk in access.visible(world.users[who]).values_list("pk", flat=True)}
        assert visible & all_ids == expected
        found = api.post("/entities/search", {"limit": 100}).json()["items"]
        assert {e["id"] for e in found} & all_ids == expected

    request_view = Api(world.browser(REQUESTER)).get(f"/entities/{request.pk}/request").json()
    assert str(delivered.pk) in {result["id"] for result in request_view["results"]}
    assert not {str(item.pk), str(cancelled.pk), str(recorded.pk)} & {
        result["id"] for result in request_view["results"]
    }

    # A person in both projects may see the work item in the core and still cannot share it.
    analyst = world.users["editor"]
    ProjectMembership.objects.create(project=asking, user=analyst, role=ProjectRole.EDITOR)
    with pytest.raises(Refused, match="stays in"):
        services.link(analyst, item, asking)
    member = {"name": "Work collection", "members": [str(item.pk)]}
    _make(world, "editor", "iki/core.Collection", core, **member)
    with pytest.raises(Invalid, match="stays in"):
        _make(world, "editor", "iki/core.Collection", asking, **member)
    with pytest.raises(Invalid, match="isn't a request sent to"):
        _make(world, "editor", WORK, asking, request=str(request.pk))

    with pytest.raises(Refused, match="Every recorded deliverable"):
        _status(world, request, "admin", "delivered")
    _status(world, recorded, "admin", "delivered")


def _plain_delivery_coverage(world: World, asking: Project) -> None:
    core = world.project
    first, second = [
        _make(world, REQUESTER, "iki/inventory.Tube", asking, name=name)
        for name in ("First tube", "Second tube")
    ]
    unrequested = _make(world, "editor", "iki/inventory.Tube", core, name="Unrequested tube")
    subjects = [str(first.pk), str(second.pk)]
    request = _make(
        world,
        REQUESTER,
        PLAIN,
        asking,
        title="Two tube reports",
        tubes=subjects,
    )
    _send(world, request)
    fields = {
        "request": str(request.pk),
        "outcome": "unsuccessful",
        "report": _report(core, "service-report.txt", b"Service could not complete the work."),
    }
    with pytest.raises(Invalid, match="Choose a sample or tube on this request"):
        _make(world, "editor", BASIC_DELIVERY, core, **fields, material=str(unrequested.pk))
    for _ in range(2):
        result = _make(world, "editor", BASIC_DELIVERY, core, **fields, material=str(first.pk))
        _status(world, result, "admin", "delivered")
    with pytest.raises(Refused, match="Second tube has no returned result"):
        _status(world, request, "admin", "delivered")
    last = _make(world, "editor", BASIC_DELIVERY, core, **fields, material=str(second.pk))
    services.update(
        world.users[REQUESTER], request, {"tubes": subjects[:1]}, expected_revision=request.revision
    )
    request.refresh_from_db()
    with pytest.raises(Refused, match="Choose a sample or tube on this request"):
        _status(world, last, "admin", "delivered")
    services.update(
        world.users[REQUESTER], request, {"tubes": subjects}, expected_revision=request.revision
    )
    request.refresh_from_db()
    _status(world, last, "admin", "delivered")
    assert not Signature.objects.filter(entity=last).exists()
    _status(world, request, "admin", "delivered")


def _sequencing(world: World, asking: Project, api: Api) -> None:
    # ERR13376132 is the public long-read run for natural yeast isolate CEY652.
    # https://www.ncbi.nlm.nih.gov/sra/ERX12747051
    sender = world.users[REQUESTER]
    stock = _make(world, REQUESTER, "iki/core.Collection", asking, name="CEY652 stock")
    tube = services.create(
        sender,
        "iki/inventory.Tube",
        asking,
        {"name": "CEY652", "barcode": "TRAINING-CEY652", "contents": str(stock.pk)},
        amount={"value": 10, "unit": "uL"},
    ).entity
    tube = Entity.objects.get(pk=tube.pk)
    (child,) = amounts.split(sender, tube, 1, amounts.Given.of({"value": 4, "unit": "uL"})).aliquots
    (portion,) = amounts.split(
        sender, child, 1, amounts.Given.of({"value": 2, "unit": "uL"})
    ).aliquots
    private_tube = _make(
        world, REQUESTER, "iki/inventory.Tube", asking, name="Another project tube"
    )
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

    services.link(world.users["editor"], portion, world.project)
    report_path = EXAMPLES / "ERR13376132-delivery.txt"
    report = _report(world.project, report_path.name, report_path.read_bytes())
    fields = {
        "request": str(qc.pk),
        "outcome": "fulfilled",
        "material": str(portion.pk),
        "report": report,
        "run_accession": "ERR13376132",
    }
    for type_name in (RESEARCH_DELIVERY, BASIC_DELIVERY):
        with pytest.raises(Invalid):
            _make(world, "editor", type_name, world.project, **fields)
    duplicate_tube = _make(world, "editor", "iki/inventory.Tube", world.project, name="Unsent tube")
    with pytest.raises(Invalid, match="Choose a sample or tube on this request"):
        _make(
            world,
            "editor",
            REGULATED_DELIVERY,
            world.project,
            **{**fields, "material": str(duplicate_tube.pk)},
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
    _sign(world, "admin", deliverable, "recorded", "Reopen the report for correction")
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
    review = _sign(world, "admin", deliverable, "reviewed", "Review the corrected report")
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
    second_tube = _make(world, REQUESTER, "iki/inventory.Tube", asking, name="Tube B")
    services.update(
        requester, qc, {"tubes": [str(tube.pk), str(second_tube.pk)]}, expected_revision=qc.revision
    )
    qc.refresh_from_db()
    for material, outcome in [(tube, "fulfilled"), (second_tube, "unsuccessful")]:
        with pytest.raises(Refused, match="Tube B has no returned result yet"):
            _status(world, qc, "admin", "delivered")
        result = _make(
            world,
            "editor",
            REGULATED_DELIVERY,
            world.project,
            **{
                **fields,
                "material": str(material.pk),
                "outcome": outcome,
                "run_accession": None if outcome == "unsuccessful" else fields["run_accession"],
            },
        )
        _sign(world, "editor", result, "submitted")
        _sign(world, "admin", result, "reviewed")
        _sign(world, "viewer", result, "released")
    draft = _make(world, "editor", REGULATED_DELIVERY, world.project, **fields)
    with pytest.raises(Refused, match="Every recorded deliverable"):
        _status(world, qc, "admin", "delivered")
    _sign(world, "editor", draft, "submitted")
    _sign(world, "admin", draft, "reviewed")
    _sign(world, "viewer", draft, "released")
    _status(world, qc, "admin", "delivered")
    with pytest.raises(Invalid, match="already been delivered"):
        _make(world, "editor", REGULATED_DELIVERY, world.project, **fields)
