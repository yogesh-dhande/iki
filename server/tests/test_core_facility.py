"""Work sent to a core facility: its private work items, and a deliverable's return under
each flow its service can choose (ADR 0305). The facility examples' own request and
deliverable types are tested in test_facility_services.py."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from api_client import Api
from django.core.exceptions import PermissionDenied
from first_party_dependencies import PLUGINS, ROOT, build_order
from org_world import World, build_world
from scan_fixtures import clean_blob
from service_reviews import CORE, serve
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import begin, ready, signed

from iki_server.amounts import services as amounts
from iki_server.amounts.models import DrawPurpose
from iki_server.entities import access, requests, services, states
from iki_server.entities.models import Entity, ProjectLink
from iki_server.entities.values import Invalid
from iki_server.files.models import File
from iki_server.org.models import LabRoleGrant, Project, ProjectMembership, ProjectRole
from iki_server.org.services import Refused
from iki_server.reviews import pins
from iki_server.signatures.models import Signature
from iki_server.signatures.services import verify
from iki_server.storage import receipts
from iki_server.storage.models import ReceiptStatus

__all__ = ["database"]

PLUGIN = PLUGINS / "requests"
EXAMPLES = ROOT / "examples" / "core-facility-requests"
REQUESTS = "iki/requests."
REQUEST = REQUESTS + "Request"
DELIVERY = REQUESTS + "Deliverable"
WORK = REQUESTS + "WorkItem"
#: The flows of core's result review a service can return deliverables under.
PLAIN, RESEARCH, REGULATED = "accepted", "research", "regulated_qc"
SOMEONE_ELSE = "It must be signed by someone other than"
REQUESTER = "outsider"


def _facility_build(tmp_path: Path, database: str, *plugins: Path) -> Path:
    build = tmp_path / "build"
    plugins = build_order(PLUGIN, *plugins)
    built = iki("plugin", "build", *plugins, "--output", build, cwd=PLUGINS)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    return build


def test_a_core_keeps_its_work_and_returns_each_kind_of_deliverable(
    tmp_path: Path, database: str
) -> None:
    exercise = django(
        _facility_build(tmp_path, database),
        database,
        "-c",
        "import django; django.setup(); from test_core_facility import exercise_core_requests; "
        "exercise_core_requests()",
    )
    assert exercise.returncode == 0, exercise.stdout + exercise.stderr


@pytest.mark.parametrize(
    "flow", [PLAIN, RESEARCH, REGULATED], ids=["plain-tube", "research-lot", "qc-nested-aliquot"]
)
def test_returned_sample_covers_its_draw(tmp_path: Path, database: str, flow: str) -> None:
    exercise = django(
        _facility_build(tmp_path, database, PLUGINS / "samples"),
        database,
        "-c",
        "import django; django.setup(); from test_core_facility import exercise_sample_delivery; "
        f"exercise_sample_delivery({flow!r})",
    )
    assert exercise.returncode == 0, exercise.stdout + exercise.stderr


def _report(project: Project, name: str, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    blob = clean_blob(sha256=digest, size=len(content), storage_uri=f"s3://iki/{digest}")
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


def _accept(world: World, result: Entity, person: str = "admin") -> None:
    """Delivers ``result`` without a signature, as Accept without sign-off does."""
    states.set_state(
        world.users[person], result, "review", "accepted", expected_revision=result.revision
    )
    result.refresh_from_db()


def _status(world: World, request: Entity, person: str, state: str) -> None:
    states.set_state(
        world.users[person], request, "status", state, expected_revision=request.revision
    )
    request.refresh_from_db()


def _send(world: World, request: Entity, flow: str, *, receive_code: str | None = None) -> None:
    """Sends ``request`` to the core, which has chosen to review its deliverables under
    ``flow``: the request is pinned to that as it arrives."""
    serve(world.users["admin"], world.project, flow)
    requests.submit(
        world.users[REQUESTER], request, world.project, expected_revision=request.revision
    )
    request.refresh_from_db()
    assert pins.pin_of(request) == pins.Pin(CORE, flow)
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
    """A core taking ``request_types`` (any request, by default) from its asking project."""
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
        types=list(request_types) or [REQUEST],
        from_registry=False,
        sources=[asking],
        by=world.users["admin"],
    )
    return world, asking


def exercise_core_requests() -> None:
    world, asking = _facility()
    _research_sign_off(world, asking)
    _plain_delivery_coverage(world, asking)
    _qc_tube_delivery(world, asking, Api(world.browser(REQUESTER)))


def exercise_sample_delivery(flow: str) -> None:
    world, asking = _facility()
    core = world.project
    analyst = world.users["editor"]
    membership = ProjectMembership.objects.create(
        project=asking, user=analyst, role=ProjectRole.EDITOR
    )
    stock = _make(world, REQUESTER, "iki/core.Collection", asking, name="Reference material")
    if flow == RESEARCH:
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
    request = _make(world, REQUESTER, REQUEST, asking, title="Sample report", **scope)
    _send(world, request, flow)
    source = subject
    if flow == REGULATED:
        for aliquot_volume in (4, 2):
            (source,) = amounts.split(
                analyst, source, 1, amounts.Given.of({"value": aliquot_volume, "unit": "uL"})
            ).aliquots
        services.link(analyst, source, core)

    def pending_sample(source: Entity) -> Entity:
        return _make(
            world,
            "editor",
            "iki/samples." + ("ReviewedSample" if flow == REGULATED else "PlainSample"),
            core,
            source=str(source.pk),
            purpose=str(request.pk),
        )

    fields: dict[str, Any] = {
        "request": str(request.pk),
        "outcome": "fulfilled",
        "report": _report(core, "sample-report.txt", b"Sample measurements"),
    }

    def report_on(material: Entity) -> Entity:
        return _make(world, "editor", DELIVERY, core, **fields, material=str(material.pk))

    sample = pending_sample(source)
    with pytest.raises(Refused, match="has no returned result yet"):
        _status(world, request, "admin", "delivered")
    with pytest.raises(Invalid, match="Choose a sample or tube on this request"):
        report_on(sample)

    if flow == RESEARCH:
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
        if flow == PLAIN:
            work = _make(world, "editor", "iki/core.Collection", core, name="Assay measurements")
            amounts.draw(analyst, source, volume, used_in=work, confirm=source.hrid)
            with pytest.raises(Invalid, match="Choose a sample or tube on this request"):
                report_on(work)
        amounts.draw(analyst, source, volume, used_in=sample, confirm=source.hrid)
    result = report_on(sample)
    with pytest.raises(Refused, match="has no returned result yet"):
        _status(world, request, "admin", "delivered")
    if flow == PLAIN:
        _accept(world, result)
    elif flow == RESEARCH:
        _sign(world, "admin", result, "signed_off")
    else:
        _sign(world, "editor", result, "submitted")
        _sign(world, "admin", result, "reviewed")
        with pytest.raises(Refused, match="has no returned result yet"):
            _status(world, request, "admin", "delivered")
        _sign(world, "viewer", result, "released")
    assert ProjectLink.objects.get(entity=result, project=asking).via_request_id == request.pk
    _status(world, request, "admin", "delivered")


def _research_sign_off(world: World, asking: Project) -> None:
    # The core's production report for UniProtKB P00330, yeast alcohol dehydrogenase 1:
    # https://www.uniprot.org/uniprotkb/P00330/entry. The lot and masses are synthetic.
    construct = _make(world, REQUESTER, "iki/core.Collection", asking, name="ADH1 construct")
    research = _make(
        world,
        REQUESTER,
        REQUEST,
        asking,
        title="Training: ADH1 production",
        records=[str(construct.pk)],
    )
    with pytest.raises(Invalid, match="isn't a request sent to"):
        _make(world, REQUESTER, WORK, asking, request=str(research.pk))
    _send(world, research, RESEARCH)
    with pytest.raises(Refused, match="returned"):
        _status(world, research, "admin", "delivered")
    report_path = EXAMPLES / "P00330-production.txt"
    report = _report(world.project, report_path.name, report_path.read_bytes())
    fields = {"request": str(research.pk), "outcome": "fulfilled", "report": report}
    # The core now chooses Regulated QC. The request sent under Research keeps it, and so
    # does its deliverable: it's signed off once, as the request was sent (ADR 0305).
    serve(world.users["admin"], world.project, REGULATED)
    deliverable = _make(world, "editor", DELIVERY, world.project, **fields)
    assert pins.pin_of(deliverable) == pins.Pin(CORE, RESEARCH)
    assert not ProjectLink.objects.filter(entity=deliverable, project=asking).exists()
    with pytest.raises(Refused, match="returned"):
        _status(world, research, "admin", "delivered")
    review = _sign(world, "admin", deliverable, "signed_off")
    assert Signature.objects.filter(entity=deliverable).count() == 1
    assert json.loads(review.content)["values"]["report"] == report
    assert ProjectLink.objects.get(entity=deliverable, project=asking).via_request_id == research.pk
    plain = _make(world, REQUESTER, REQUEST, asking, title="Training bench work")
    _send(world, plain, PLAIN)
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
    with pytest.raises(PermissionDenied):
        _status(world, item, "admin", "scheduled")
    for target in ("scheduled", "running", "done"):
        _status(world, item, "editor", target)
    # Done is done: it isn't started or finished again.
    with pytest.raises(Refused):
        _status(world, item, "editor", "running")
    # A desk-based task is finished without being scheduled or started.
    desk = _make(world, "editor", WORK, core, request=str(request.pk))
    _status(world, desk, "editor", "done")
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

    recorded = _make(world, "editor", DELIVERY, core, **fields)
    delivered = _make(world, "editor", DELIVERY, core, **fields)
    with pytest.raises(PermissionDenied):
        _accept(world, delivered, "viewer")
    _accept(world, delivered)
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
    _accept(world, recorded)


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
        REQUEST,
        asking,
        title="Two tube reports",
        tubes=subjects,
    )
    _send(world, request, PLAIN)
    fields = {
        "request": str(request.pk),
        "outcome": "unsuccessful",
        "report": _report(core, "service-report.txt", b"Service could not complete the work."),
    }
    with pytest.raises(Invalid, match="Choose a sample or tube on this request"):
        _make(world, "editor", DELIVERY, core, **fields, material=str(unrequested.pk))
    for _ in range(2):
        result = _make(world, "editor", DELIVERY, core, **fields, material=str(first.pk))
        _accept(world, result)
    with pytest.raises(Refused, match="Second tube has no returned result"):
        _status(world, request, "admin", "delivered")
    last = _make(world, "editor", DELIVERY, core, **fields, material=str(second.pk))
    services.update(
        world.users[REQUESTER], request, {"tubes": subjects[:1]}, expected_revision=request.revision
    )
    request.refresh_from_db()
    # The delivery's own guard refuses it before Iki's release check would.
    with pytest.raises(Refused, match="must still be on the request"):
        _accept(world, last)
    services.update(
        world.users[REQUESTER], request, {"tubes": subjects}, expected_revision=request.revision
    )
    request.refresh_from_db()
    _accept(world, last)
    assert not Signature.objects.filter(entity=last).exists()
    _status(world, request, "admin", "delivered")


def _qc_tube_delivery(world: World, asking: Project, api: Api) -> None:
    # The core's delivery report for ERR13376132, the public long-read run for natural
    # yeast isolate CEY652: https://www.ncbi.nlm.nih.gov/sra/ERX12747051
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
    fields = {"title": "Training: long-read sequencing", "tubes": [str(tube.pk)]}
    qc = _make(world, REQUESTER, REQUEST, asking, **fields)
    # A service that hasn't chosen how its deliverables are reviewed can't take it, and
    # the request stays unsent and unpinned (ADR 0305).
    elsewhere = Project.objects.create(team=world.team, name="Sequencing Core")
    ProjectMembership.objects.create(
        project=elsewhere, user=world.users["admin"], role=ProjectRole.ADMIN
    )
    requests.set_intake(
        elsewhere, types=[REQUEST], from_registry=False, sources=[asking], by=world.users["admin"]
    )
    with pytest.raises(pins.ProcedureNotChosen, match="hasn't chosen how the deliverables"):
        requests.submit(world.users[REQUESTER], qc, elsewhere, expected_revision=qc.revision)
    assert requests.of(qc) is None and pins.pin_of(qc) is None
    requests.set_intake(
        world.project, types=[], from_registry=False, sources=[asking], by=world.users["admin"]
    )
    with pytest.raises(requests.NoSuchService):
        requests.submit(world.users[REQUESTER], qc, world.project, expected_revision=qc.revision)
    requests.set_intake(
        world.project,
        types=[REQUEST],
        from_registry=False,
        sources=[asking],
        by=world.users["admin"],
    )
    _send(world, qc, REGULATED, receive_code="TRAINING-CEY652")
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
    }
    duplicate_tube = _make(world, "editor", "iki/inventory.Tube", world.project, name="Unsent tube")
    with pytest.raises(Invalid, match="Choose a sample or tube on this request"):
        _make(
            world,
            "editor",
            DELIVERY,
            world.project,
            **{**fields, "material": str(duplicate_tube.pk)},
        )
    deliverable = _make(world, "editor", DELIVERY, world.project, **fields)
    _sign(world, "editor", deliverable, "submitted")
    with pytest.raises(Refused, match=SOMEONE_ELSE):
        begin(world, "editor", str(deliverable.pk), "reviewed", field="review")
    first = _sign(world, "admin", deliverable, "reviewed")
    assert api.get(f"/entities/{deliverable.pk}").status_code == 404
    assert api.get(f"/files/{report}").status_code == 404

    corrected = _report(world.project, "corrected-report.txt", b"Training: confirmed read counts")
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
    with pytest.raises(Refused, match=SOMEONE_ELSE):
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
            DELIVERY,
            world.project,
            **{**fields, "material": str(material.pk), "outcome": outcome},
        )
        _sign(world, "editor", result, "submitted")
        _sign(world, "admin", result, "reviewed")
        _sign(world, "viewer", result, "released")
    draft = _make(world, "editor", DELIVERY, world.project, **fields)
    with pytest.raises(Refused, match="Every recorded deliverable"):
        _status(world, qc, "admin", "delivered")
    _sign(world, "editor", draft, "submitted")
    _sign(world, "admin", draft, "reviewed")
    _sign(world, "viewer", draft, "released")
    _status(world, qc, "admin", "delivered")
    with pytest.raises(Invalid, match="already been delivered"):
        _make(world, "editor", DELIVERY, world.project, **fields)
