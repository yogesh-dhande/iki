"""Sign real plate results in the project doing the work and deliver them to the requester."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from api_client import Api
from django.core.exceptions import PermissionDenied
from first_party_dependencies import PLUGINS, build_order
from org_world import World, build_world
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import begin, ready, signed

from iki_server.entities import registry, requests, services, states
from iki_server.entities.models import Entity, ProjectLink, Revision
from iki_server.entities.values import Invalid
from iki_server.files.models import Blob, File
from iki_server.org.models import LabRoleGrant, Project, ProjectMembership, ProjectRole
from iki_server.org.services import Refused
from iki_server.runs import catalog
from iki_server.runs.services import plugin_settings
from iki_server.setup import starters
from iki_server.setup.models import StarterApplication
from iki_server.signatures.models import Signature
from iki_server.signatures.services import verify

__all__ = ["database"]

PLUGIN = PLUGINS / "plate_assays"
PREFIX = "iki/plate_assays."
REQUEST = "iki/example_lims.AnalysisRequest"


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


def _check_inherited_flows(world: World) -> None:
    api = Api(world.browser("viewer"))
    research = api.get(f"/entity-types/{PREFIX}RequestedResearchAssayResult/flow")
    assert research.status_code == 200, research.content
    (field,) = research.json()["fields"]
    assert field["transitions"] == [{"from": "recorded", "to": "signed", "conditional": True}]
    regulated = api.get(f"/entity-types/{PREFIX}RequestedQCAssayResult/flow")
    assert regulated.status_code == 200, regulated.content
    (field,) = regulated.json()["fields"]
    assert field["initial"] == "recorded"
    assert {(e["from"], e["to"], e["conditional"]) for e in field["transitions"]} == {
        ("submitted", "recorded", False),
        ("reviewed", "recorded", False),
        ("recorded", "submitted", True),
        ("submitted", "reviewed", True),
        ("reviewed", "released", True),
    }
    assert {s["name"]: s["on_edit"] for s in field["states"] if s["on_edit"]} == {
        "submitted": "recorded",
        "reviewed": "recorded",
    }


def _request(world: World) -> Entity:
    asking = Project.objects.create(team=world.team, name="Project requesting an assay")
    editor = world.users["editor"]
    ProjectMembership.objects.create(project=asking, user=editor, role=ProjectRole.EDITOR)
    requests.set_intake(
        world.project, types=[REQUEST], from_registry=True, sources=[], by=world.users["admin"]
    )
    saved = services.create(editor, REQUEST, asking, {"title": "Synthetic plate review"})
    request = Entity.objects.get(pk=saved.entity.pk)
    requests.submit(editor, request, world.project, expected_revision=request.revision)
    return request


def _sign(world: World, who: str, record: Entity, to: str) -> Signature:
    signature = signed(world, who, str(record.pk), to, field="review").signature
    assert signature is not None
    record.refresh_from_db()
    assert states.values_of(record)["review"] == to
    assert all(check.ok for check in verify(signature))
    return signature


def exercise_reviews() -> None:
    world = build_world()
    _check_inherited_flows(world)
    ready(world)
    for principal, held in {
        "editor": ("analyst", "reviewer", "qa"),
        "admin": ("reviewer", "qa"),
        "runner": ("reviewer", "qa"),
        "viewer": ("qa",),
    }.items():
        membership = world.project.memberships.get(user=world.users[principal])
        for role in held:
            LabRoleGrant.objects.create(membership=membership, role=PREFIX + role)

    request = _request(world)
    editor = world.users["editor"]
    sample = services.create(
        editor,
        "iki/protein.Protein",
        world.project,
        {"name": "Synthetic plate review fixture", "sequence": "AGAGAGAG"},
    ).entity
    read_id = _read_evidence(world, str(sample.pk))

    def result(type_name: str, flow: str) -> Entity:
        fields = {
            "read": read_id,
            "sample": str(sample.pk),
            "request": str(request.pk),
            "review_flow": flow,
            "fit_version": "4PL-OLS/1",
            "fit_status": "unreportable",
        }
        saved = services.create(editor, PREFIX + type_name, world.project, fields, by_run=True)
        return Entity.objects.get(pk=saved.entity.pk)

    asking = request.owner_project
    assert asking
    fit = catalog.action(PREFIX + "fit_curve")
    assert fit
    application = StarterApplication.objects.create(
        project=world.project,
        requester_project=asking,
        starter="dose-response",
        version="1.0.0",
        flow="research",
        applied_by=world.users["admin"],
    )
    research = result("RequestedResearchAssayResult", "research")
    with pytest.raises(Invalid, match="starter in this project uses research"):
        result("RequestedQCAssayResult", "regulated_qc")
    with pytest.raises(Invalid, match="A research assay result records the Research flow"):
        result("RequestedResearchAssayResult", "regulated_qc")

    # The administrator switches while the research result is awaiting sign-off.
    application.flow = "regulated_qc"
    application.save()
    # A newly installed plugin may advertise a recipe this server doesn't yet offer.
    # The previously applied choice remains binding for runs and direct creates.
    with patch.dict(registry.plugins()[fit.plugin].starters, {"dose-response": "1.1.0"}):
        starters.installed_starters.cache_clear()
        assert "dose-response" not in starters.installed_starters()
        assert plugin_settings(fit, world.project)["review_flow"] == "regulated_qc"
        assert plugin_settings(fit, asking)["review_flow"] == "research"
        with pytest.raises(Invalid, match="starter in this project uses regulated_qc"):
            services.create(
                editor,
                PREFIX + "ResearchAssayResult",
                world.project,
                {"read": read_id, "sample": str(sample.pk)},
            )
    starters.installed_starters.cache_clear()
    with pytest.raises(PermissionDenied):
        begin(world, "viewer", str(research.pk), "signed", field="review")
    witness = _sign(world, "admin", research, "signed")
    assert Signature.objects.filter(entity=research).count() == 1
    assert json.loads(witness.content)["values"]["fit_status"] == "unreportable"
    assert ProjectLink.objects.filter(entity=research, via_request=request).exists()

    with pytest.raises(Refused):
        states.set_state(
            editor, research, "review", "recorded", expected_revision=research.revision
        )
    qc = result("RequestedQCAssayResult", "regulated_qc")
    with pytest.raises(Invalid):
        services.update(editor, qc, {"review_flow": "research"}, expected_revision=qc.revision)
    with pytest.raises(Invalid, match="can't change once it's set"):
        services.update(
            editor, qc, {"review_flow": "research"}, expected_revision=qc.revision, by_run=True
        )
    with pytest.raises(Refused):
        begin(world, "viewer", str(qc.pk), "released", field="review")
    services.update(
        world.users["admin"],
        qc,
        {"notes": "Checked dilution labels"},
        expected_revision=qc.revision,
    )
    _sign(world, "editor", qc, "submitted")
    # The submitted QC result still needs an independent reviewer and QA after switching.
    application.flow = "research"
    application.save()
    with pytest.raises(Invalid, match="starter in this project uses research"):
        result("RequestedQCAssayResult", "regulated_qc")
    for author in ("editor", "admin"):
        with pytest.raises(Refused):
            begin(world, author, str(qc.pk), "reviewed", field="review")
    first_review = _sign(world, "runner", qc, "reviewed")
    assert not ProjectLink.objects.filter(entity=qc, via_request=request).exists()

    previous_revision = qc.revision
    correction = "Checked the retained reader export"
    services.update(
        editor,
        qc,
        {"notes": correction},
        expected_revision=previous_revision,
        reason=correction,
        allow_correction=True,
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
    review = _sign(world, "runner", qc, "reviewed")
    for author in ("editor", "runner"):
        with pytest.raises(Refused):
            begin(world, author, str(qc.pk), "released", field="review")
    release = _sign(world, "viewer", qc, "released")
    assert json.loads(release.content)["values"]["notes"] == correction
    assert json.loads(first_review.content)["values"]["notes"] == "Checked dilution labels"
    assert qc.released_review_revision == review.revision
    assert (
        ProjectLink.objects.get(entity=qc, via_request=request).project_id
        == request.owner_project_id
    )


def _read_evidence(world: World, sample_id: str) -> str:
    """A synthetic reviewed fit with an unreportable endpoint still needs a retained read."""
    blob = Blob.objects.create(sha256="a" * 64, size=10, storage_uri="s3://iki/test")
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
