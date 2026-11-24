"""A portable report follows the signatures of its final review cycle."""

import hashlib
import json
import shutil
from pathlib import Path

from api_client import Api, problem
from org_world import build_world
from test_plugin_migrations import build, database, django, made, migrate, sync
from test_signatures import past_the_triggers, ready, signed
from test_states import roles

from iki_server.audit.models import AuditEvent
from iki_server.signatures import proof, signing_keys
from iki_server.signatures.models import Signature

__all__ = ["database"]

FIXTURE = Path(__file__).parents[2] / "spec" / "tests" / "data" / "signed_report"
PLUGIN = "acme/signed_reports"
TYPE = f"{PLUGIN}.SignedReport"


def test_report_exports_are_audited_and_carry_the_verified_issuance(
    tmp_path: Path, database: str
) -> None:
    plugins = tmp_path / "plugins"
    shutil.copytree(FIXTURE, plugins / FIXTURE.name)
    made(FIXTURE.name, "--no-input", cwd=plugins)
    installed = build(plugins, FIXTURE.name)
    migrate(installed, database)
    sync(installed, database)
    checked = django(
        installed,
        database,
        "-c",
        "import django; django.setup(); from test_signed_report_exports import exercise_exports; "
        "exercise_exports()",
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def exercise_exports() -> None:
    lab = build_world()
    ready(lab)
    roles(
        lab,
        editor=(f"{PLUGIN}.analyst",),
        admin=(f"{PLUGIN}.reviewer",),
        viewer=(f"{PLUGIN}.qa",),
    )
    editor = Api(lab.browser("editor"))
    reader = Api(lab.browser("viewer"))
    shown = reader.get(f"/entity-types/{TYPE}").json()
    assert shown["exports"][0]["issued"] == ["issued"]
    made = editor.post(
        "/entities",
        {"type": TYPE, "project": str(lab.project.pk), "values": {"report": {"value": 1}}},
    )
    assert made.status_code == 201, made.content
    report = made.json()
    ref = report["id"]
    export = f"/entities/{ref}/export"
    problem(reader.get(export, format="signed-report-json"), 404, "report_not_issued")

    old = signed(lab, "editor", ref, "submitted", field="review")
    changed = editor.patch(
        f"/entities/{ref}",
        {
            "values": {
                "report": {"title": "Final report", "value": 2, "note": "<script>alert(1)</script>"}
            },
            "reason": "Correct a transcription error",
        },
        headers={"If-Match": '"2"'},
    )
    assert changed.status_code == 200, changed.content
    assert changed.json()["values"]["review"] == "recorded"
    signed(lab, "editor", ref, "submitted", field="review")
    signed(lab, "admin", ref, "reviewed", field="review")
    issued = signed(lab, "viewer", ref, "issued", field="review")

    response = reader.get(export, format="signed-report-json")
    assert response.status_code == 200, response.content
    packet = response.json()
    assert packet["format"] == "iki/signed-report/v1"
    assert packet["record"]["hrid"] == report["hrid"]
    assert packet["report"] == changed.json()["values"]["report"]
    assert packet["record"]["issued_revision"] == 7
    assert (
        response["Content-Disposition"]
        == f'attachment; filename="{report["hrid"]} (revision 7).json"'
    )
    assert "no-store" in response["Cache-Control"]
    assert reader.get(export, format="signed-report-json", revision=7).content == response.content
    problem(reader.get(export, format="signed-report-json", revision=2), 404, "report_not_issued")

    signatures = packet["signatures"]
    assert [p["carried_by"] for p in signatures] == [5, 6, 7]
    documents = [json.loads(p["document"]) for p in signatures]
    assert [d["change"]["to"] for d in documents] == ["submitted", "reviewed", "issued"]
    assert documents[-1]["id"] == str(issued.signature_id)
    assert str(old.signature_id) not in [d["id"] for d in documents]
    trusted = signing_keys.trusted()
    for document, signed_proof in zip(documents, signatures, strict=True):
        assert all(
            check.ok
            for check in proof.check_document(
                signed_proof["document"], proof.unb64(signed_proof["signature"]), trusted
            )
        )
        assert proof.check_content(document, signed_proof["content"]).ok
        assert json.loads(signed_proof["content"])["values"]["report"] == packet["report"]

    printable = reader.get(export, format="signed-report-html")
    assert printable.status_code == 200, printable.content
    assert printable["Content-Disposition"].endswith('.html"')
    html = printable.content.decode()
    assert "<h1>Final report</h1>" in html
    assert all(p["manifestation"] in html for p in signatures)
    assert "<script>" not in html and "&lt;script&gt;" in html
    events = AuditEvent.objects.filter(action="entity.exported", target_id=ref)
    assert events.count() == 3
    assert {e.data["sha256"] for e in events} == {
        hashlib.sha256(response.content).hexdigest(),
        hashlib.sha256(printable.content).hexdigest(),
    }
    assert all(e.actor == lab.users["viewer"] for e in events)
    problem(Api(lab.browser("outsider")).get(export, format="signed-report-json"), 404, "not_found")

    assert issued.signature_id is not None
    with past_the_triggers():
        Signature.objects.filter(pk=issued.signature_id).update(signature=b"corrupted")
    problem(reader.get(export, format="signed-report-json"), 404, "report_not_issued")
    assert events.count() == 3
