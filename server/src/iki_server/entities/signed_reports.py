"""Portable copies of reports at the revisions that issued them (ADRs 0126 and 0171)."""

from __future__ import annotations

import json
from html import escape
from typing import Any
from uuid import UUID

from iki_server.entities.models import Entity, Revision
from iki_server.entities.services import values_at
from iki_server.signatures import proof, services, signing_keys
from iki_server.signatures.models import Signature
from iki_spec.model import ReportExport

FORMAT = "iki/signed-report/v1"


class Unissued(ValueError):  # noqa: N818 - this revision has no issuable report
    """An unsigned report or an invalid issuance, with the reason."""


def snapshot(entity: Entity, number: int, export: ReportExport) -> dict[str, Any]:
    """Read the report, and verify every carried signature of the continuous signed cycle."""
    values = values_at(entity, number)
    revisions = iter(
        Revision.objects.filter(entity=entity, number__lte=number, state_field=export.review)
        .order_by("-number")
        .iterator()
    )
    issuance = next(revisions, None)
    if (
        issuance is None
        or values.get(export.review) not in export.issued
        or issuance.state_to != values[export.review]
        or not issuance.signatures
        or values.get(export.report) is None
    ):
        raise Unissued("This revision has no signed, issued report.")

    # An unsigned transition back to Recorded starts a new cycle and ends this walk.
    # Render the report at the issuance revision even if another field changed later.
    carried = [issuance]
    for earlier in revisions:
        if not earlier.signatures or earlier.state_to != carried[-1].state_from:
            break
        carried.append(earlier)
    signatures = Signature.objects.in_bulk([pk for step in carried for pk in step.signatures])
    trusted = signing_keys.trusted()
    report = values[export.report]
    checked = [
        _check(signatures, pk, index, step, report, export, trusted)
        for step in reversed(carried)
        for index, pk in enumerate(step.signatures)
    ]
    return {
        "format": FORMAT,
        "record": {
            "id": str(entity.pk),
            "hrid": entity.hrid,
            "type": entity.type,
            "issued_revision": issuance.number,
        },
        "report_field": export.report,
        "report": report,
        "signatures": checked,
    }


def _check(
    found: dict[UUID, Signature],
    pk: UUID,
    index: int,
    step: Revision,
    report: Any,  # noqa: ANN401 - arbitrary JSON report
    export: ReportExport,
    trusted: dict[str, str],
) -> dict[str, Any]:
    signature = found.get(pk)
    if (
        signature is None
        or signature.entity_id != step.entity_id
        or signature.revision + 1 != step.number
        or signature.index != index
        or signature.field != step.state_field
        or signature.state != step.state_to
        or not all(check.ok for check in services.verify(signature, trusted))
    ):
        raise Unissued("A signature in this report's issuance could not be verified.")
    document = json.loads(signature.document)
    content = json.loads(signature.content)
    if document["signature"]["of"] != len(step.signatures) or proof.canonical(
        content["values"].get(export.report)
    ) != proof.canonical(report):
        raise Unissued("The report differs from the one signed in its issuance.")
    return {
        "manifestation": services.manifestation(document),
        "carried_by": step.number,
        "key": signature.key,
        "signature": proof.b64(bytes(signature.signature)),
        "document": signature.document,
        "content": signature.content,
    }


def json_file(snapshot: dict[str, Any]) -> bytes:
    """The deterministic machine-readable output, including the exact signature proofs."""
    return (json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def html_file(snapshot: dict[str, Any]) -> bytes:
    """A scriptless, printable copy, with electronic signature manifestations and proof."""
    record = snapshot["record"]
    report = snapshot["report"]
    title = report.get("title") if isinstance(report, dict) else None
    header = escape(title if isinstance(title, str) and title else "Signed report")
    reference = escape(f"{record['hrid']} · Issuance revision {record['issued_revision']}")
    manifestations = "".join(
        f"<li>{escape(s['manifestation'])}</li>" for s in snapshot["signatures"]
    )
    proofs = escape(json_file(snapshot).decode())
    body = _html_value(report)
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{header}</title>
<style>
body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 65ch; margin: 2rem auto;
  padding: 0 1rem; overflow-wrap: anywhere; color-scheme: light dark; }}
dt {{ font-weight: 600; }}
dd {{ margin-inline-start: 1rem; margin-block-end: .35rem; }}
ol {{ padding-inline-start: 1.5rem; }}
ol > li {{ margin-block-end: .5rem; }}
pre {{ white-space: pre-wrap; }}
@media print {{ body {{ max-width: none; margin: 0; padding: 0; color: #000; }} }}
</style>
<main>
<h1>{header}</h1>
<p>{reference}</p>
{body}
<h2>Electronic signatures</h2>
<ol>{manifestations}</ol>
<details><summary>Verification data</summary><pre>{proofs}</pre></details>
</main>
</html>
""".encode()


def _html_value(value: Any) -> str:  # noqa: ANN401 - arbitrary JSON report
    if isinstance(value, dict):
        return (
            "<dl>"
            + "".join(
                f"<dt>{escape(key.replace('_', ' ').capitalize())}</dt><dd>{_html_value(item)}</dd>"
                for key, item in sorted(value.items())
            )
            + "</dl>"
        )
    if isinstance(value, list):
        return "<ol>" + "".join(f"<li>{_html_value(item)}</li>" for item in value) + "</ol>"
    if value is None:
        return "—"
    return escape(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
