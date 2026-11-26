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
from scan_fixtures import clean_blob
from test_api_entities import patch
from test_plate_review import _read_evidence, _requests
from test_plugin_migrations import database, django, iki, migrate, sync
from test_signatures import begin, ready, signed

from iki_server.entities import services, states
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid
from iki_server.files.models import File
from iki_server.org.models import LabRoleGrant
from iki_server.org.services import Refused
from iki_server.runs import catalog, queue, results
from iki_server.runs import services as runs
from iki_server.runs.models import ActionRun, EffectKind, Job, Lineage, RunEffect
from iki_server.signatures import proof, signing_keys
from iki_server.signatures.services import verify
from iki_spec.compute import Binding

__all__ = ["database"]

PLUGIN = PLUGINS / "qc_release"
#: A synthetic manufacturer, as a certificate states its issuer (ICH Q7 §11.43).
ISSUER = "Synthetic Example Chemicals, 1 Example Way, Example City · +1 555 0100"
PREFIX = "iki/qc_release."


def test_the_installed_qc_plugin_issues_a_signed_coa(tmp_path: Path, database: str) -> None:
    build = tmp_path / "build"
    built = iki("plugin", "build", *build_order(PLUGIN), "--output", build, cwd=ROOT)
    assert built.returncode == 0, built.stdout + built.stderr
    settings = {"review_flow": "regulated_qc", "certificate_issuer": ISSUER}
    (build / "iki_plugin_iki__qc_release" / "settings.json").write_text(
        json.dumps(settings), encoding="utf-8"
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
    plate = "iki/plate_assays."
    for person, roles in {
        "editor": (PREFIX + "analyst", PREFIX + "reviewer", PREFIX + "qa", plate + "analyst"),
        "runner": (PREFIX + "reviewer", plate + "reviewer"),
        "admin": (PREFIX + "qa", plate + "qa"),
    }.items():
        membership = project.memberships.get(user=world.users[person])
        LabRoleGrant.objects.bulk_create(
            [LabRoleGrant(membership=membership, role=role) for role in roles]
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
    # A submitted request covers only its tube. Even the right procedure cannot
    # deliver a fit on this other lot to that requester. Internal QC may use it
    # for its own lot release only under the procedure revision in the specification.
    _, request, _ = _requests(world)
    control = make(
        "iki/small_molecule.Compound",
        name="Water",
        smiles="O",
        inchikey="XLYOFNOQVPJJNP-UHFFFAOYSA-N",
    )
    assert control.entry_id is not None
    control_schedule = make(PREFIX + "SpecificationSchedule", material=str(control.entry_id))
    fit_method = {"attribute": "IC50", "method": "4PL-OLS/1", "unit": "nM"}
    fit_criterion = {**fit_method, "upper": 10, "procedure": "SOP-PLATE", "procedure_revision": "2"}
    control_spec = make(
        PREFIX + "SpecificationVersion",
        schedule=str(control_schedule.pk),
        version="1",
        source="Synthetic plate QC fixture",
        effective_from=today.isoformat(),
        criteria=[fit_criterion],
    )
    sign("admin", control_spec, "approved", field="approval")
    execute("publish_version", {"schedule": [control_schedule.pk], "version": [control_spec.pk]})
    assay_lot = make("iki/lots.GenericLot", of=str(control.entry_id), lot_number="Plate QC lot")
    read = _read_evidence(world, str(assay_lot.pk))
    assay_inputs = {"lot": [assay_lot.pk], "schedule": [control_schedule.pk]}
    assay_case = made(
        execute("open_release", assay_inputs, testing_on=today.isoformat()),
        "ReleaseCase",
    )
    fit_values = {
        "read": read,
        "sample": str(assay_lot.pk),
        "review_flow": "regulated_qc",
        "fit_version": fit_method["method"],
        "fit_status": "fitted",
        "qualifier": "within_range",
        "endpoint_kind": fit_method["attribute"],
        "endpoint_value": 5,
        "concentration_unit": fit_method["unit"],
        "procedure": fit_criterion["procedure"],
    }
    with pytest.raises(Invalid, match="sample or tube"):
        services.create(
            editor,
            plate + "RequestedQCAssayResult",
            project,
            {**fit_values, "request": str(request.pk), "procedure_revision": "2"},
            by_run=True,
        )
    for revision in ("1", "2"):
        saved = services.create(
            editor,
            plate + "QCAssayResult",
            project,
            {**fit_values, "procedure_revision": revision},
            by_run=True,
        )
        fit = Entity.objects.get(pk=saved.entity.pk)
        sign("editor", fit, "submitted")
        sign("runner", fit, "reviewed")
        sign("admin", fit, "released")
        chosen = {"case": [assay_case.pk], "assay": [fit.pk]}
        if revision == "1":
            with pytest.raises(ValueError, match="procedure"):
                execute("record_test", chosen, **fit_method)
        else:
            recorded = made(execute("record_test", chosen, **fit_method), "ReleaseTest")
            measured = services.current_values_of([recorded])[recorded.pk]
            assert (measured["evidence"], measured["evidence_revision"]) == (fit.pk, fit.revision)

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
    shown = Api(world.browser("editor")).get(f"/entities/{case.pk}")
    assert shown.status_code == 200, shown.content
    assert shown.json()["title"] == f"Synthetic NaCl lot · {today.isoformat()}"

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
    blob = clean_blob(sha256=digest, size=len(evidence_data), storage_uri=f"s3://iki/{digest}")
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
    recorder_api, reviewer_api = Api(world.browser("editor")), Api(world.browser("runner"))
    investigation_path = f"/entities/{investigation.pk}"
    viewed = reviewer_api.get(investigation_path)
    sign("editor", investigation, "recorded", reason="Reopen the findings for correction")
    corrected = patch(
        recorder_api,
        str(investigation.pk),
        {
            "values": {"findings": "Synthetic correction clarifying the assignable cause."},
            "reason": "Clarified the investigation finding.",
        },
        recorder_api.get(investigation_path)["ETag"],
    )
    assert corrected.status_code == 200, corrected.content
    assert corrected.json()["values"]["review"] == "recorded"
    stale_review = reviewer_api.post(
        f"{investigation_path}/signatures",
        {"field": "review", "state": "reviewed", "acknowledge": True, "return_to": "/"},
        headers={"If-Match": viewed["ETag"]},
    )
    assert "Read it again" in problem(stale_review, 412, "stale")["detail"]
    assert reviewer_api.get(investigation_path)["ETag"] == corrected["ETag"]
    sign("editor", investigation, "submitted")
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
    # What the certificate states: the issuer, and each criterion's final result.
    coa = services.current_values_of([certificate])[certificate.pk]["coa"]
    assert coa["issuer"] == ISSUER
    assert [(row["attribute"], row["value"]) for row in coa["results"]] == [
        (assay["attribute"], 99.8),
        (drying["attribute"], 0.3),
    ]
    assert "intended_use" not in coa
    assert "investigations" not in coa

    reader = Api(world.browser("viewer"))
    # The record holds the evidence: it's audited in Iki, and never exported to send.
    record_export = reader.get(f"/entities/{certificate.pk}/export", format="signed-report-json")
    assert record_export.json()["code"] == "no_export", record_export.content
    with pytest.raises(ValueError, match="Finish signing the certificate record"):
        execute("issue_certificate", {"certificate": [certificate.pk]})
    sign("editor", certificate, "submitted")
    with pytest.raises(Refused, match="reviewer must be independent"):
        begin(world, "editor", str(certificate.pk), "reviewed", field="review")
    sign("runner", certificate, "reviewed")
    qa_api = Api(world.browser("admin"))
    certificate_path = f"/entities/{certificate.pk}"
    qa_view = qa_api.get(certificate_path)
    assert qa_view.json()["values"]["report"] == report
    alternative = [dict(criterion) for criterion in report["specification"]["criteria"]]
    alternative[0]["lower"] -= 0.1
    for record, replacement, status, code in (
        (original, {"value": 99.5}, 422, "validation_failed"),
        (original, {"unit": "mg/g"}, 422, "validation_failed"),
        (version, {"criteria": alternative}, 409, "refused"),
    ):
        path = f"/entities/{record.pk}"
        before = recorder_api.get(path)
        problem(
            patch(recorder_api, str(record.pk), {"values": replacement}, before["ETag"]),
            status,
            code,
        )
        after = recorder_api.get(path)
        assert after["ETag"] == before["ETag"]
        assert after.json()["values"] == before.json()["values"]
    assert qa_api.get(certificate_path)["ETag"] == qa_view["ETag"]
    sign("admin", certificate, "released")
    record_signatures = (
        Api(world.browser("admin"))
        .get(f"/entities/{certificate.pk}/signatures?verify=true")
        .json()["items"]
    )
    assert [item["verification"]["valid"] for item in record_signatures] == [True] * 3

    # QA issues the certificate of analysis from the approved record. It's signed on its
    # own, so what the customer receives holds only what the certificate states.
    issued = made(
        execute("issue_certificate", {"certificate": [certificate.pk]}), "CertificateOfAnalysis"
    )
    held = services.current_values_of([issued, certificate])
    assert held[issued.pk]["coa"] == coa
    # The record names its certificate by its digest, inside Iki only.
    assert held[issued.pk]["digest"] == held[certificate.pk]["coa_digest"]
    assert Lineage.objects.filter(entity=issued, parent=certificate).exists()
    with pytest.raises(Refused):
        begin(world, "runner", str(issued.pk), "issued", field="review")
    export = f"/entities/{issued.pk}/export"
    problem(reader.get(export, format="signed-report-json"), 404, "report_not_issued")
    sign("admin", issued, "released", reason="Released to the customer")
    downloaded = reader.get(export, format="signed-report-json")
    printable = reader.get(export, format="signed-report-html")
    assert downloaded.status_code == printable.status_code == 200, downloaded.content
    packet = downloaded.json()
    assert packet["report"] == coa
    assert packet["record"] == {
        "id": str(issued.pk),
        "hrid": issued.hrid,
        "type": PREFIX + "CertificateOfAnalysis",
        "issued_revision": issued.revision,
    }
    (signed_proof,) = packet["signatures"]
    assert signed_proof["signature"] in printable.content.decode()

    # Nothing of the evidence is anywhere in either file: not the failed attempt, its
    # investigation, the analysts or any of the record's or tests' identities.
    analyst, reviewer = world.users["editor"], world.users["runner"]
    internal = [
        str(original.pk),
        original.hrid,
        str(repeat.pk),
        repeat.hrid,
        str(moisture.pk),
        str(investigation.pk),
        investigation.hrid,
        str(certificate.pk),
        certificate.hrid,
        str(case.pk),
        case.hrid,
        str(analyst.pk),
        analyst.name,
        analyst.email,
        str(reviewer.pk),
        reviewer.name,
        reviewer.email,
        "98.7",
        "Synthetic correction clarifying the assignable cause.",
        "report belonged to another specimen",
        str(attachment.pk),
    ]
    for shared in (downloaded.content.decode(), printable.content.decode()):
        assert [text for text in internal if text in shared] == []

    # The recipient verifies exactly the certificate they hold, and an edit fails.
    def verified(received: dict[str, Any]) -> bool:
        trusted = signing_keys.trusted()
        for item in received["signatures"]:
            document = json.loads(item["document"])
            content = json.loads(item["content"])
            if (
                not all(
                    check.ok
                    for check in proof.check_document(
                        item["document"], proof.unb64(item["signature"]), trusted
                    )
                )
                or not proof.check_content(document, item["content"]).ok
            ):
                return False
            if proof.canonical(content["values"]["coa"]) != proof.canonical(received["report"]):
                return False
        return True

    assert verified(packet)
    assert json.loads(signed_proof["document"])["change"]["to"] == "released"
    edited = json.loads(downloaded.content)
    edited["report"]["results"][0]["value"] = 101.0
    assert not verified(edited)
    swapped = json.loads(downloaded.content)
    content = json.loads(swapped["signatures"][0]["content"])
    content["values"]["coa"]["results"][0]["value"] = 101.0
    swapped["signatures"][0]["content"] = proof.canonical(content)
    swapped["report"] = content["values"]["coa"]
    assert not verified(swapped)
    assert (
        reader.get(export, format="signed-report-json", revision=issued.revision).content
        == downloaded.content
    )
    with pytest.raises(results.InvalidOutput) as reissued:
        run, document = start("issue_certificate", {"certificate": [certificate.pk]})
        commit(run, actions.issue_certificate(document))
    assert any("issued already" in issue["detail"] for issue in reissued.value.errors)
    # The record keeps the full evidence for audit.
    assert held[certificate.pk]["report"] == report
