"""Review plate results in the project doing the work, under the procedure it chose, and
deliver them to the requester (ADRs 0302, 0305)."""

import json
from pathlib import Path

import pytest
from api_client import Api
from django.core.exceptions import PermissionDenied
from first_party_dependencies import PLUGINS, build_order
from org_world import World, build_world
from scan_fixtures import clean_blob
from service_reviews import CORE, REQUEST, serve
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import begin, evidence, ready, signed

from iki_server.entities import requests, services, states
from iki_server.entities.models import Entity, ProjectLink, Revision
from iki_server.entities.values import Invalid
from iki_server.files.models import File
from iki_server.org.models import LabRoleGrant, Project, ProjectMembership, ProjectRole
from iki_server.org.services import Refused
from iki_server.reviews import pins
from iki_server.signatures.models import Signature
from iki_server.signatures.services import finish, verify

__all__ = ["database"]

PLUGIN = PLUGINS / "plate_assays"
PREFIX = "iki/plate_assays."
RESULT = PREFIX + "AssayResult"
REQUESTED = PREFIX + "RequestedAssayResult"


def test_both_plate_review_flows_deliver_the_signed_result(tmp_path: Path, database: str) -> None:
    """Install the shipping plugin in an isolated database, as in the first-party tests."""
    build = tmp_path / "build"
    built = iki("plugin", "build", *build_order(PLUGIN), "--output", build, cwd=PLUGINS)
    assert built.returncode == 0, built.stdout + built.stderr
    migrate(build, database)
    sync(build, database)
    exercise = django(
        build,
        database,
        "-c",
        "import django; django.setup(); from test_plate_review import exercise_reviews; "
        "exercise_reviews()",
    )
    assert exercise.returncode == 0, exercise.stdout + exercise.stderr


def _check_drawn_flows(world: World) -> None:
    """A requested result is reviewed as an assay result is: its flows are the plugin's
    starter's, drawn from core's result review (ADR 0306)."""
    api = Api(world.browser("viewer"))
    drawn = api.get(f"/entity-types/{REQUESTED}/flow")
    assert drawn.status_code == 200, drawn.content
    review = drawn.json()["review"]
    flows = {f["flow"]: f["field"] for f in review["flows"]}
    assert set(flows) == {"research", "regulated_qc"}
    assert flows["research"]["transitions"] == [
        {"from": "recorded", "to": "signed_off", "conditional": True}
    ]
    regulated = flows["regulated_qc"]
    assert regulated["initial"] == "recorded"
    assert {(e["from"], e["to"]) for e in regulated["transitions"]} == {
        ("submitted", "recorded"),
        ("reviewed", "recorded"),
        ("recorded", "submitted"),
        ("submitted", "reviewed"),
        ("reviewed", "released"),
    }
    assert all(state["on_edit"] is None for state in regulated["states"])


def _requests(world: World) -> tuple[Entity, Entity, Entity]:
    asking = Project.objects.create(team=world.team, name="Project requesting an assay")
    editor = world.users["editor"]
    ProjectMembership.objects.create(project=asking, user=editor, role=ProjectRole.EDITOR)
    requests.set_intake(
        world.project, types=[REQUEST], from_registry=True, sources=[], by=world.users["admin"]
    )
    serve(world.users["admin"], world.project, "research")
    sample = services.create(editor, "iki/inventory.Tube", asking, {"name": "Review tube"}).entity

    def send(tubes: list[str]) -> Entity:
        saved = services.create(
            editor, REQUEST, asking, {"title": "Synthetic plate review", "tubes": tubes}
        )
        request = Entity.objects.get(pk=saved.entity.pk)
        requests.submit(editor, request, world.project, expected_revision=request.revision)
        return request

    return send([str(sample.pk)]), send([str(sample.pk)]), Entity.objects.get(pk=sample.pk)


def _sign(world: World, who: str, record: Entity, to: str, reason: str = "") -> Signature:
    signature = signed(world, who, str(record.pk), to, field="review", reason=reason).signature
    assert signature is not None
    record.refresh_from_db()
    assert states.values_of(record)["review"] == to
    assert all(check.ok for check in verify(signature))
    return signature


def exercise_reviews() -> None:
    world = build_world()
    _check_drawn_flows(world)
    ready(world)
    for principal, held in {
        "editor": ("analyst", "reviewer", "qa"),
        "admin": ("reviewer", "qa"),
        "runner": ("reviewer", "qa"),
        "viewer": ("qa",),
    }.items():
        membership = world.project.memberships.get(user=world.users[principal])
        for role in held:
            LabRoleGrant.objects.create(membership=membership, role="iki/lab_roles." + role)

    request, qc_request, sample = _requests(world)
    editor, admin = world.users["editor"], world.users["admin"]
    read_id = _read_evidence(world, str(sample.pk))

    def result(answers: Entity) -> Entity:
        fields = {
            "read": read_id,
            "sample": str(sample.pk),
            "request": str(answers.pk),
            "fit_version": "4PL-OLS/1",
            "fit_status": "unreportable",
        }
        saved = services.create(editor, REQUESTED, world.project, fields, by_run=True)
        return Entity.objects.get(pk=saved.entity.pk)

    # Until the project chooses how its fits are reviewed, none can be recorded.
    with pytest.raises(pins.ProcedureNotChosen):
        result(request)
    serve(admin, world.project, "research", RESULT)
    research = result(request)
    assert pins.pin_of(research) == pins.Pin(CORE, "research")
    # An action can't choose another: the server pins what the project chose (ADR 0302).
    with pytest.raises(Invalid, match="set by Iki"):
        services.create(
            editor,
            REQUESTED,
            world.project,
            {
                "read": read_id,
                "sample": str(sample.pk),
                "request": str(request.pk),
                "review_flow": "regulated_qc",
            },
            by_run=True,
        )

    # The Admin switches while the research result is awaiting sign-off: it keeps its flow.
    serve(admin, world.project, "regulated_qc", RESULT)
    with pytest.raises(PermissionDenied):
        begin(world, "viewer", str(research.pk), "signed_off", field="review")
    # Its creator can't sign its review (ADR 0301).
    with pytest.raises(Refused):
        begin(world, "editor", str(research.pk), "signed_off", field="review")
    pending = begin(world, "admin", str(research.pk), "signed_off", field="review")
    services.update(editor, request, {"tubes": []}, expected_revision=request.revision)
    refused = finish(pending, evidence(world, "admin"))
    assert refused.signature_id is None and "sample or tube" in refused.message
    assert not ProjectLink.objects.filter(entity=research, via_request=request).exists()
    assert states.values_of(research)["review"] == "recorded"
    qc_request.refresh_from_db()
    services.update(editor, qc_request, {"tubes": []}, expected_revision=qc_request.revision)
    with pytest.raises(Invalid, match="sample or tube"):
        result(qc_request)
    request.refresh_from_db()
    services.update(
        editor, request, {"tubes": [str(sample.pk)]}, expected_revision=request.revision
    )
    witness = _sign(world, "admin", research, "signed_off")
    assert Signature.objects.filter(entity=research).count() == 1
    assert json.loads(witness.content)["values"]["fit_status"] == "unreportable"
    assert ProjectLink.objects.filter(entity=research, via_request=request).exists()

    with pytest.raises(Refused):
        states.set_state(
            editor, research, "review", "recorded", expected_revision=research.revision
        )
    qc_request.refresh_from_db()
    services.update(
        editor, qc_request, {"tubes": [str(sample.pk)]}, expected_revision=qc_request.revision
    )
    qc = result(qc_request)
    assert pins.pin_of(qc) == pins.Pin(CORE, "regulated_qc")
    for by_run in (False, True):
        with pytest.raises(Invalid):
            services.update(
                editor,
                qc,
                {"review_flow": "research"},
                expected_revision=qc.revision,
                by_run=by_run,
            )
    with pytest.raises(Refused):
        begin(world, "viewer", str(qc.pk), "released", field="review")
    services.update(admin, qc, {"notes": "Checked dilution labels"}, expected_revision=qc.revision)
    _sign(world, "editor", qc, "submitted")
    # The submitted QC result still needs an independent reviewer and QA after switching.
    serve(admin, world.project, "research", RESULT)
    assert pins.pin_of(qc) == pins.Pin(CORE, "regulated_qc")
    for author in ("editor", "admin"):
        with pytest.raises(Refused):
            begin(world, author, str(qc.pk), "reviewed", field="review")
    first_review = _sign(world, "runner", qc, "reviewed")
    assert not ProjectLink.objects.filter(entity=qc, via_request=qc_request).exists()

    previous_revision = qc.revision
    correction = "Checked the retained reader export"
    _sign(world, "runner", qc, "recorded", correction)
    services.update(
        editor,
        qc,
        {"notes": correction},
        expected_revision=qc.revision,
        reason=correction,
    )
    qc.refresh_from_db()
    assert states.values_of(qc)["review"] == "recorded"
    back, edit = Revision.objects.filter(entity=qc, number__gt=previous_revision).order_by("number")
    assert (back.state_from, back.state_to, edit.changed_fields) == (
        "reviewed",
        "recorded",
        ["notes"],
    )
    with pytest.raises(Refused):
        begin(world, "viewer", str(qc.pk), "released", field="review")
    _sign(world, "editor", qc, "submitted")
    review = _sign(world, "runner", qc, "reviewed", "Reviewed the corrected measurements")
    for author in ("editor", "runner"):
        with pytest.raises(Refused):
            begin(world, author, str(qc.pk), "released", field="review")
    release = _sign(world, "viewer", qc, "released")
    assert json.loads(release.content)["values"]["notes"] == correction
    assert json.loads(first_review.content)["values"]["notes"] == "Checked dilution labels"
    assert qc.released_review_revision == review.revision
    assert (
        ProjectLink.objects.get(entity=qc, via_request=qc_request).project_id
        == request.owner_project_id
    )


def _read_evidence(world: World, sample_id: str) -> str:
    """A synthetic reviewed fit with an unreportable endpoint still needs a retained read."""
    blob = clean_blob(sha256="a" * 64, size=10, storage_uri="s3://iki/test")
    reader_file = File.objects.create(
        project=world.project, blob=blob, name="review.csv", media_type="text/csv"
    )
    plate = services.create(
        world.users["editor"],
        PREFIX + "AssayPlate",
        world.project,
        {
            "name": "Synthetic review plate",
            "size": "plate_96",
            "reader_file": str(reader_file.pk),
            "assignments": [{"well": "A1", "sample": sample_id, "replicate": 1}],
        },
    ).entity
    saved = services.create(
        world.users["editor"],
        PREFIX + "PlateRead",
        world.project,
        {"plate": str(plate.pk), "reader_file": str(reader_file.pk)},
        by_run=True,
    )
    return str(saved.entity.pk)
