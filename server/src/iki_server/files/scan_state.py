"""Scan evidence and the decision every consumer checks after authorizing its caller."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.db import transaction
from django.db.models import Q, QuerySet

from iki_server.accounts.models import User
from iki_server.api.problems import Problem
from iki_server.audit import log
from iki_server.files.models import Blob, BlobScan, ScanRelease, Upload
from iki_server.org import permissions


@dataclass(frozen=True)
class ScanStatus:
    state: str = "pending"
    decision_id: UUID | None = None
    scanner: str = ""
    signature_version: str = ""
    scanned_at: datetime | None = None
    reason: str = ""
    allow_unscanned: bool = False
    released: bool = False

    @property
    def usable(self) -> bool:
        return (
            self.state == "clean"
            or (self.state == "infected" and self.released)
            or (self.state == "not_scanned" and self.allow_unscanned)
        )

    def require_usable(self) -> None:
        if self.usable:
            return
        if self.state == "pending":
            raise Problem(
                409,
                "This file is being scanned. Try again shortly.",
                code="file_scanning",
                headers={"Retry-After": "5"},
            )
        if self.state == "infected":
            raise Problem(409, "This file is quarantined.", code="file_quarantined")
        if self.reason == "too_large":
            detail = "This file is too large to scan under the deployment's policy."
        else:
            detail = "This file is held because the scanner could not inspect it completely."
        raise Problem(409, detail, code="file_not_scanned")


_PENDING = ScanStatus()


def status_from_decision(decision: BlobScan | None) -> ScanStatus:
    """The decision from a read that joined the source's scan and its release."""
    if decision is None:
        return _PENDING
    return ScanStatus(
        state=decision.state,
        decision_id=decision.pk,
        scanner=decision.scanner,
        signature_version=decision.signature_version,
        scanned_at=decision.scanned_at,
        reason=decision.reason,
        allow_unscanned=decision.allow_unscanned,
        released=decision.state == "infected" and hasattr(decision, "release"),
    )


def _source(source: Blob | Upload) -> dict[str, UUID]:
    if isinstance(source, Blob):
        return {"blob_id": source.pk}
    return {"upload_id": source.pk}


def status_for(source: Blob | Upload) -> ScanStatus:
    decision = BlobScan.objects.select_related("release").filter(**_source(source)).first()
    return status_from_decision(decision)


def require_usable(source: Blob | Upload) -> None:
    status_for(source).require_usable()


def _statuses(ids: Iterable[UUID], *, blobs: bool) -> dict[UUID, ScanStatus]:
    result = dict.fromkeys(ids, _PENDING)
    decisions = BlobScan.objects.select_related("release")
    if blobs:
        decisions = decisions.filter(blob_id__in=result)
    else:
        decisions = decisions.filter(upload_id__in=result)
    for decision in decisions:
        source_id = decision.blob_id if blobs else decision.upload_id
        if source_id is not None:
            result[source_id] = status_from_decision(decision)
    return result


def statuses_for_blobs(ids: Iterable[UUID]) -> dict[UUID, ScanStatus]:
    return _statuses(ids, blobs=True)


def statuses_for_uploads(ids: Iterable[UUID]) -> dict[UUID, ScanStatus]:
    return _statuses(ids, blobs=False)


#: Decisions whose bytes may be used, as :attr:`ScanStatus.usable` says, for queries.
USABLE = (
    Q(state="clean")
    | Q(state="infected", release__isnull=False)
    | Q(state="not_scanned", allow_unscanned=True)
)


def usable_upload_ids() -> QuerySet[BlobScan, UUID | None]:
    """For selecting checkpoints eligible to be kept or restored in one query."""
    return BlobScan.objects.filter(USABLE, upload__isnull=False).values_list("upload_id", flat=True)


def held() -> QuerySet[BlobScan]:
    """Current holds, including inspections that the deployment's size policy refuses."""
    return (
        BlobScan.objects.filter(
            Q(state="infected", release__isnull=True)
            | Q(state="not_scanned", allow_unscanned=False)
        )
        .select_related("blob", "upload")
        .order_by("-id")
    )


#: What a Quarantine Admin can search held bytes by besides a name: a SHA-256 or its start.
_SHA256_PREFIX = re.compile(r"[0-9a-f]{6,64}")


def quarantine(*, released: bool = False, state: str = "", q: str = "") -> QuerySet[BlobScan]:
    """What Admin → Quarantine lists: current holds, or the infected findings a Quarantine
    Admin released (ADR 0217). ``state`` narrows to ``infected`` or ``not_scanned``, and
    ``q`` to the bytes a file or upload of that name holds, or whose SHA-256 starts so."""
    found = BlobScan.objects.filter(state="infected", release__isnull=False) if released else held()
    if state:
        found = found.filter(state=state)
    if q := q.strip():
        named = Q(blob__in=Blob.objects.filter(files__name__icontains=q)) | Q(
            upload__name__icontains=q
        )
        if _SHA256_PREFIX.fullmatch(q.lower()):
            named |= Q(blob__sha256__startswith=q.lower()) | Q(upload__sha256__startswith=q.lower())
        found = found.filter(named)
    return found.select_related("blob", "upload", "release__actor")


def reviewed(decision_id: UUID) -> BlobScan | None:
    """One decision Admin → Quarantine shows: held, or a released finding. A clean one, or
    an oversized file the deployment's policy lets be used, was never quarantined."""
    return (
        BlobScan.objects.filter(pk=decision_id)
        .filter(Q(state="infected") | Q(state="not_scanned", allow_unscanned=False))
        .select_related("blob", "upload", "release__actor")
        .first()
    )


def _audit_data(decision: BlobScan) -> dict[str, object]:
    return {
        "decision": str(decision.pk),
        "source": "blob" if decision.blob_id else "upload",
        "source_id": str(decision.blob_id or decision.upload_id),
        "scanner": decision.scanner,
        "signature_version": decision.signature_version,
        "finding": decision.reason,
        "allow_unscanned": decision.allow_unscanned,
    }


@transaction.atomic
def record_result(
    source: Blob | Upload,
    *,
    state: str,
    scanner: str,
    signature_version: str = "",
    reason: str = "",
    allow_unscanned: bool = False,
) -> ScanStatus:
    """Record one completed decision. Duplicate task deliveries keep its original evidence."""
    decision, created = BlobScan.objects.select_related("release").get_or_create(
        **_source(source),
        defaults={
            "state": state,
            "scanner": scanner,
            "signature_version": signature_version,
            "reason": reason,
            "allow_unscanned": allow_unscanned,
        },
    )
    if created and state in ("infected", "not_scanned"):
        action = "file.quarantined" if state == "infected" else "file.not_scanned"
        log.record(action, target=decision, data=_audit_data(decision))
    if created and isinstance(source, Blob):
        # Runs waiting for these bytes' scan can start, or end with why (ADR 0234).
        from iki_server.runs import scan_waits
        from iki_server.runs.tasks import scan_decided

        if scan_waits.any_waiting():
            scan_decided.enqueue(str(source.pk))
    return status_from_decision(decision)


@transaction.atomic
def release(decision_id: UUID, *, actor: User, reason: str) -> ScanStatus:
    permissions.require(actor, permissions.Action.QUARANTINE_MANAGE)
    reason = reason.strip()
    if not reason or len(reason) > 2000:
        raise Problem(422, "Give a release reason of 1 to 2,000 characters.")
    decision = BlobScan.objects.filter(pk=decision_id).first()
    if decision is None:
        raise Problem(404, "No such scan decision.")
    if decision.state != "infected":
        raise Problem(409, "Only an infected finding can be released.")
    _, created = ScanRelease.objects.get_or_create(
        decision=decision, defaults={"actor": actor, "reason": reason}
    )
    if not created:
        raise Problem(409, "This finding has already been released.")
    log.record(
        "file.quarantine_released",
        actor=actor,
        target=decision,
        data={**_audit_data(decision), "reason": reason},
    )
    if decision.upload is not None:
        from iki_server.files.services import scan_released

        scan_released(decision.upload)
    return status_from_decision(decision)
