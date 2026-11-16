"""Whether a restored deployment is whole (ADR 0099): its database is at the code's schema,
its encrypted values read with the keys it has, and the files its records name are in
storage, at their size (and, if asked, their SHA-256). Run after every restore, and by the
automated restore test."""

from __future__ import annotations

import random
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Max
from django.utils import timezone

from iki_server.core import crypto
from iki_server.files import storage

#: At most this many problems are named in a report; the count says how many there were.
SHOWN = 20


@dataclass
class Check:
    name: str
    checked: int = 0
    problems: list[str] = field(default_factory=list)
    problem_count: int = 0
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.problem_count == 0

    def problem(self, what: str) -> None:
        self.problem_count += 1
        if len(self.problems) < SHOWN:
            self.problems.append(what)


@dataclass
class Report:
    checks: list[Check]
    #: The newest change recorded: every change is audited, so the data reaches this far.
    data_until: datetime | None
    counts: dict[str, int]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data_until": self.data_until.isoformat() if self.data_until else None,
            "counts": self.counts,
            "checks": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "checked": c.checked,
                    "problems": c.problem_count,
                    "shown": c.problems,
                    "note": c.note,
                }
                for c in self.checks
            ],
        }


def check(*, sample: int | None = None, hashes: bool = False, external: bool = False) -> Report:
    """Every check. `sample` checks that many files of each kind, chosen at random, rather
    than all; `hashes` reads each one to compare its SHA-256; `external` checks files
    registered where they already were, in buckets that aren't Iki's, too."""
    return Report(
        checks=[schema(), encryption(), files(sample=sample, hashes=hashes, external=external)],
        data_until=data_until(),
        counts=counts(),
    )


def schema() -> Check:
    found = Check("Database schema")
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    found.checked = len(targets)
    for migration, _ in executor.migration_plan(targets):
        found.problem(f"{migration.app_label}.{migration.name} isn't applied")
    if not found.ok:
        found.note = "Run iki-server migrate, or restore with the code the backup was taken with."
    return found


def encryption() -> Check:
    found = Check("Encrypted values")
    for model, name in crypto.encrypted_fields():
        rows = model._default_manager.exclude(**{name: ""}).values_list("pk", name)
        for pk, token in rows.iterator():
            found.checked += 1
            try:
                crypto.decrypt(token)
            except crypto.DecryptionError:
                found.problem(f"{model._meta.label}.{name} of {pk}")
    if not found.ok:
        found.note = (
            "IKI_ENCRYPTION_KEYS doesn't hold the key these were encrypted with: restore the "
            "keys from the same time as the database."
        )
    return found


@dataclass(frozen=True)
class _Stored:
    what: str
    storage_uri: str
    version: str
    size: int
    sha256: str


def files(*, sample: int | None, hashes: bool, external: bool) -> Check:
    found = Check("Files in storage")
    if not storage.configured():
        found.note = "File storage isn't configured, so no file was checked."
        return found
    kinds: list[Iterable[_Stored]] = [_own_blobs(), _artifacts()]
    if external:
        kinds.append(_external_blobs())
    for kind in kinds:
        for stored in _sampled(kind, sample):
            found.checked += 1
            _check_one(found, stored, hashes=hashes)
    if not external:
        found.note = "Files registered from other buckets weren't checked (--external)."
    return found


def _check_one(found: Check, stored: _Stored, *, hashes: bool) -> None:
    bucket_name, key = storage.split_uri(stored.storage_uri)
    try:
        head = storage.head(bucket_name, key, stored.version)
    except Exception as error:
        found.problem(f"{stored.what}: couldn't be read ({type(error).__name__})")
        return
    if head is None:
        found.problem(f"{stored.what}: missing from {bucket_name}")
        return
    if head["size"] != stored.size:
        found.problem(f"{stored.what}: {head['size']} bytes, not {stored.size}")
        return
    if hashes:
        sha256, _ = storage.digest_object(bucket_name, key, stored.version)
        if sha256 != stored.sha256:
            found.problem(f"{stored.what}: its SHA-256 isn't the one recorded")


def _own_blobs() -> Iterator[_Stored]:
    from iki_server.files.models import Blob

    rows = Blob.objects.filter(registered_external=False).order_by("pk")
    for pk, uri, version, size, sha256 in rows.values_list(
        "pk", "storage_uri", "object_version", "size", "sha256"
    ).iterator():
        yield _Stored(f"File contents {sha256[:12]} ({pk})", uri, version, size, sha256)


def _external_blobs() -> Iterator[_Stored]:
    from iki_server.files.models import Blob

    rows = Blob.objects.filter(registered_external=True).order_by("pk")
    for pk, uri, version, size, sha256 in rows.values_list(
        "pk", "storage_uri", "object_version", "size", "sha256"
    ).iterator():
        yield _Stored(f"Registered file {uri} ({pk})", uri, version, size, sha256)


def _artifacts() -> Iterator[_Stored]:
    """Runs' kept files whose retention hasn't ended: Iki deletes the others."""
    from iki_server.files.models import UploadState
    from iki_server.runs.models import RunArtifact

    rows = (
        RunArtifact.objects.filter(deleted_at__isnull=True, upload__state=UploadState.READY)
        .order_by("pk")
        .values_list("pk", "upload_id", "upload__size", "upload__sha256")
    )
    for pk, upload_id, size, sha256 in rows.iterator():
        uri = storage.uri(storage.artifact_key(upload_id))
        yield _Stored(f"Run artifact {pk}", uri, "", size, sha256)


def _sampled(stored: Iterable[_Stored], sample: int | None) -> Iterable[_Stored]:
    """All of them, or `sample` of them chosen at random (reservoir sampling, so a large
    table isn't read into memory)."""
    if sample is None:
        return stored
    chosen: list[_Stored] = []
    pick = random.SystemRandom()
    for seen, item in enumerate(stored):
        if seen < sample:
            chosen.append(item)
        else:
            slot = pick.randrange(seen + 1)
            if slot < sample:
                chosen[slot] = item
    return chosen


def data_until() -> datetime | None:
    from iki_server.audit.models import AuditEvent

    newest: datetime | None = AuditEvent.objects.aggregate(newest=Max("occurred_at"))["newest"]
    return newest


def counts() -> dict[str, int]:
    """What the database holds, to compare with the deployment it was backed up from."""
    from iki_server.accounts.models import User
    from iki_server.audit.models import AuditEvent
    from iki_server.entities.models import Entity, Revision
    from iki_server.files.models import Blob, File
    from iki_server.org.models import Project, Team
    from iki_server.runs.models import ActionRun

    tables: dict[str, Callable[[], int]] = {
        "people": User.objects.count,
        "teams": Team.objects.count,
        "projects": Project.objects.count,
        "entities": Entity.objects.count,
        "revisions": Revision.objects.count,
        "files": File.objects.count,
        "file_contents": Blob.objects.count,
        "runs": ActionRun.objects.count,
        "audit_events": AuditEvent.objects.count,
    }
    return {name: count() for name, count in tables.items()}


def behind(until: datetime | None) -> str:
    """How long before now the data ends, for people."""
    if until is None:
        return "no change recorded"
    seconds = int((timezone.now() - until).total_seconds())
    if seconds < 120:
        return f"{seconds} seconds ago"
    if seconds < 7200:
        return f"{seconds // 60} minutes ago"
    return f"{seconds // 3600} hours ago"
