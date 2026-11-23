"""Installed release testing preserves the original OOS through signed disposition and issue."""

import datetime as dt
import hashlib
import importlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from api_client import Api, problem
from django.db import transaction
from django.utils import timezone
from first_party_dependencies import PLUGINS, ROOT, build_order
from org_world import build_world
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import begin, ready, signed

from iki_server.entities import services, states
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid
from iki_server.files.models import Blob, File
from iki_server.org.models import LabRoleGrant
from iki_server.org.services import Refused
from iki_server.runs import catalog, queue, results
from iki_server.runs import services as runs
from iki_server.runs.models import ActionRun, EffectKind, Job, RunEffect
from iki_server.signatures import proof, signing_keys
from iki_server.signatures.services import verify
from iki_spec.compute import Binding

__all__ = ["database"]

PLUGIN = PLUGINS / "qc_release"
PREFIX = "iki/qc_release."


def test_the_installed_qc_plugin_issues_a_signed_coa(tmp_path: Path, database: str) -> None:
    build = tmp_path / "build"
    built = iki("plugin", "build", *build_order(PLUGIN), "--output", build, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    (build / "iki_plugin_iki__qc_release" / "settings.json").write_text(
        '{"review_flow":"regulated_qc"}', encoding="utf-8"
    )
    migrate(build, database)
    sync(build, database)
    checked = django(
        build,
        database,
        "-c",
        "import django; django.setup(); from test_qc_release_workflow import exercise_qc; "
        "exercise_qc()",
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def exercise_qc() -> None:
    sys.path.insert(0, str(PLUGIN / "scripts"))
    actions = importlib.import_module("qc_release")
    world = build_world()
    ready(world)
    project, editor = world.project, world.users["editor"]
    for person, roles in {
        "editor": ("analyst", "reviewer", "qa"),
        "runner": ("reviewer",),
        "admin": ("qa",),
    }.items():
        membership = project.memberships.get(user=world.users[person])
        LabRoleGrant.objects.bulk_create(
            [LabRoleGrant(membership=membership, role=PREFIX + role) for role in roles]
        )
    environment = PREFIX + "qc-tools"
    catalog.compute().bindings[environment] = Binding(
        environment=environment, templates=(("backend-small", 0),)
    )

    def make(kind: str, **values: Any) -> Entity:
        return Entity.objects.get(pk=services.create(editor, kind, project, values).entity.pk)

    def sign(
        person: str, entity: Entity, state: str, *, field: str = "review", reason: str = ""
    ) -> None:
        result = signed(world, person, str(entity.pk), state, field=field, reason=reason).signature
        assert result is not None and all(check.ok for check in verify(result))
        entity.refresh_from_db()
        assert states.values_of(entity)[field] == state

    def start(
        name: str, entities: Mapping[str, Sequence[UUID]], **params: Any
    ) -> tuple[ActionRun, dict[str, Any]]:
        action = catalog.action(PREFIX + name)
        assert action is not None
        run = runs.launch(editor, action, project, entities, params, reuse_results=False)
        job = Job.objects.get(run=run)
        job.lease_token, job.lease_expires_at = uuid4(), timezone.now() + queue.LEASE_TTL
        return run, queue.payload(job, run)

    def commit(run: ActionRun, output: dict[str, Any]) -> None:
        with transaction.atomic():
            results.commit(ActionRun.objects.select_for_update().get(pk=run.pk), output)

    def execute(name: str, entities: Mapping[str, Sequence[UUID]], **params: Any) -> ActionRun:
        run, document = start(name, entities, **params)
        output = actions.ACTIONS[name](document)
        try:
            commit(run, output)
        except results.InvalidOutput as error:
            raise AssertionError(error.errors) from error
        return run

    def made(run: ActionRun, kind: str) -> Entity:
        return RunEffect.objects.get(
            run=run, kind=EffectKind.CREATED, entity__type=PREFIX + kind
        ).entity

    # PubChem CID 5234 (https://pubchem.ncbi.nlm.nih.gov/compound/5234) identifies sodium chloride.
    # The lot, observations and workflow evidence are synthetic. The assay and drying limits
    # come from the historical USP Stage 6 excerpt loaded below.
    material = make(
        "iki/small_molecule.Compound",
        name="Sodium chloride",
        smiles="[Na+].[Cl-]",
        inchikey="FAPWRFPIFSIZLT-UHFFFAOYSA-M",
        pubchem_cid="5234",
    )
    assert material.entry_id is not None
    lot = make("iki/lots.GenericLot", of=str(material.entry_id), lot_number="Synthetic NaCl lot")
    schedule = make(PREFIX + "SpecificationSchedule", material=str(material.entry_id))
    specification = json.loads(
        (PLUGIN / "tests/data/sodium-chloride-specification.json").read_text(encoding="utf-8")
    )
    version = make(PREFIX + "SpecificationVersion", schedule=str(schedule.pk), **specification)
    sign("admin", version, "approved", field="approval")
    execute("publish_version", {"schedule": [schedule.pk], "version": [version.pk]})

    today = dt.datetime.now(dt.UTC).date()
    successor = make(
        PREFIX + "SpecificationVersion",
        schedule=str(schedule.pk),
        **{
            **specification,
            "version": "Synthetic successor for effective-date testing",
            "effective_from": (today + dt.timedelta(days=30)).isoformat(),
        },
    )
    sign("admin", successor, "approved", field="approval")
    execute("publish_version", {"schedule": [schedule.pk], "version": [successor.pk]})
    opening = {"lot": [lot.pk], "schedule": [schedule.pk]}
    case = made(execute("open_release", opening, testing_on=today.isoformat()), "ReleaseCase")
    with pytest.raises(results.InvalidOutput) as duplicate:
        run, document = start("open_release", opening, testing_on=today.isoformat())
        commit(run, actions.open_release(document))
    assert any("existing release case" in issue["detail"] for issue in duplicate.value.errors)
    pinned = services.current_values_of([case])[case.pk]
    assert pinned["specification"] == version.pk
    assert pinned["specification_revision"] == version.revision
    assert pinned["lot_revision"] == lot.revision
    assert pinned["lot_number"] == "Synthetic NaCl lot"
    services.update(
        editor, lot, {"lot_number": "Renamed after testing"}, expected_revision=lot.revision
    )

    assay, drying = specification["criteria"]

    def params(criterion: dict[str, Any], measured: float) -> dict[str, Any]:
        return {
            **{key: criterion[key] for key in ("attribute", "method", "unit")},
            "value": measured,
        }

    inputs = {"case": [case.pk]}
    first, document = start("record_test", inputs, **params(assay, 98.7))
    pending, stale = start("record_test", inputs, **params(assay, 98.7))
    commit(first, actions.record_test(document))
    with pytest.raises(results.Rejected, match="changed after this run"):
        commit(pending, actions.record_test(stale))
    original = made(first, "ReleaseTest")
    investigation = made(first, "QCOOSInvestigation")
    assert services.current_values_of([investigation])[investigation.pk]["test"] == original.pk
    with pytest.raises(Invalid) as corrected:
        services.update(editor, original, {"value": 100.0}, expected_revision=original.revision)
    assert "computed" in corrected.value.errors["value"][0]

    evidence_data = (
        b"Synthetic investigation evidence linking the first result to the wrong specimen."
    )
    digest = hashlib.sha256(evidence_data).hexdigest()
    blob = Blob.objects.create(
        sha256=digest, size=len(evidence_data), storage_uri=f"s3://iki/{digest}"
    )
    attachment = File.objects.create(
        blob=blob, project=project, name="Synthetic investigation.txt", media_type="text/plain"
    )
    services.update(
        editor,
        investigation,
        {
            "resolution": "invalidated",
            "findings": "Synthetic example: the report belonged to another specimen.",
            "evidence": str(attachment.pk),
        },
        expected_revision=investigation.revision,
    )
    sign("editor", investigation, "submitted")
    with pytest.raises(Refused, match="independent reviewer"):
        begin(world, "editor", str(investigation.pk), "reviewed", field="review")
    sign("runner", investigation, "reviewed")
    sign("admin", investigation, "released", reason="Training conclusion and assignable cause")

    repeat = made(
        execute(
            "record_test",
            {**inputs, "repeat_of": [original.pk], "investigation": [investigation.pk]},
            **params(assay, 99.8),
        ),
        "ReleaseTest",
    )
    moisture = made(execute("record_test", inputs, **params(drying, 0.3)), "ReleaseTest")
    evidence = {**inputs, "investigations": [investigation.pk]}
    _, omission = start("prepare_certificate", {**evidence, "tests": [repeat.pk, moisture.pk]})
    with pytest.raises(ValueError, match="every attempt"):
        actions.prepare_certificate(omission)
    certificate = made(
        execute(
            "prepare_certificate", {**evidence, "tests": [moisture.pk, repeat.pk, original.pk]}
        ),
        "QCCertificate",
    )
    report = services.current_values_of([certificate])[certificate.pk]["report"]
    assert [test["value"] for test in report["tests"]] == [98.7, 99.8, 0.3]
    assert report["lot"]["revision"] == pinned["lot_revision"]
    assert report["lot"]["lot_number"] == pinned["lot_number"]
    assert report["investigations"][0]["revision"] == investigation.revision

    reader = Api(world.browser("viewer"))
    export = f"/entities/{certificate.pk}/export"
    problem(reader.get(export, format="signed-report-json"), 404, "report_not_issued")
    sign("editor", certificate, "submitted")
    with pytest.raises(Refused, match="reviewer must be independent"):
        begin(world, "editor", str(certificate.pk), "reviewed", field="review")
    sign("runner", certificate, "reviewed")
    sign("admin", certificate, "released")
    downloaded = reader.get(export, format="signed-report-json")
    assert downloaded.status_code == 200, downloaded.content
    packet = downloaded.json()
    assert packet["report"] == report
    assert packet["record"]["issued_revision"] == certificate.revision
    assert len(packet["signatures"]) == 3
    trusted = signing_keys.trusted()
    documents = []
    for signed_proof in packet["signatures"]:
        document = json.loads(signed_proof["document"])
        documents.append(document)
        assert all(
            check.ok
            for check in proof.check_document(
                signed_proof["document"], proof.unb64(signed_proof["signature"]), trusted
            )
        )
        assert proof.check_content(document, signed_proof["content"]).ok
    assert [doc["change"]["to"] for doc in documents] == ["submitted", "reviewed", "released"]
    assert (
        reader.get(export, format="signed-report-json", revision=certificate.revision).content
        == downloaded.content
    )
