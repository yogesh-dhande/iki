"""Registering workers, and the tokens they use (ADR 0043).

A compute admin makes a registration token for a tier (``iki_wreg_…``) and
gives it to that tier's workers: a Kubernetes secret for backend pods, the
launch configuration of a VM. A worker registers with it once and gets its
own token (``iki_wkr_…``), which it uses for everything else. Only SHA-256
hashes of both are stored; each is shown once. Revoking a registration
token stops new workers registering with it (and, if asked, revokes those
that did). Revoking a worker interrupts its jobs, which are retried elsewhere.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.runs.models import RegistrationToken, Worker

__all__ = [
    "REGISTRATION_PREFIX",
    "WORKER_PREFIX",
    "authenticate",
    "create_registration",
    "register",
    "revoke_registration",
    "revoke_worker",
]

REGISTRATION_PREFIX = "iki_wreg_"
WORKER_PREFIX = "iki_wkr_"
#: ``last_seen_at`` and ``last_used_at`` are written at most this often, not on every call.
SEEN_RESOLUTION = timedelta(seconds=15)

#: What a worker reports it can give one job.
CAPACITY_KEYS = ("cpu", "memory", "gpu_count", "gpu_memory", "scratch_disk")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def create_registration(
    *, label: str, tier: str, by: User, expires_at: datetime | None = None
) -> tuple[RegistrationToken, str]:
    """A new registration token, and its value (shown once)."""
    value = _token(REGISTRATION_PREFIX)
    with transaction.atomic():
        token = RegistrationToken.objects.create(
            label=label,
            tier=tier,
            token_hash=_hash(value),
            hint=value[-4:],
            created_by=by,
            expires_at=expires_at,
        )
        audit.record("worker_registration.created", actor=by, target=token, data={"tier": tier})
    return token, value


def revoke_registration(token: RegistrationToken, *, by: User, workers: bool = False) -> int:
    """Stops new workers registering with it. Workers that did keep working (as GitHub's
    runners do) unless `workers`, for a token that leaked: then they're revoked too, and
    their jobs interrupted. Returns how many workers were revoked."""
    with transaction.atomic():
        locked = RegistrationToken.objects.select_for_update().get(pk=token.pk)
        if locked.revoked_at is None:
            locked.revoked_at = timezone.now()
            locked.save(update_fields=["revoked_at"])
            audit.record("worker_registration.revoked", actor=by, target=locked)
        revoked = 0
        if workers:
            for worker in Worker.objects.filter(registration=locked, revoked_at__isnull=True):
                revoked += revoke_worker(worker, by=by)
    return revoked


def _bearer(header: str, prefix: str) -> str | None:
    scheme, _, value = header.partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value.startswith(prefix) or len(value) > 200:
        return None
    return value


def registration_for(header: str) -> RegistrationToken | None:
    """The live registration token an ``Authorization: Bearer`` header names, or None."""
    value = _bearer(header, REGISTRATION_PREFIX)
    if value is None:
        return None
    token = RegistrationToken.objects.filter(
        token_hash=_hash(value), revoked_at__isnull=True
    ).first()
    if token is None or (token.expires_at is not None and token.expires_at <= timezone.now()):
        return None
    return token


def register(
    registration: RegistrationToken,
    *,
    name: str,
    version: str,
    capacity: dict[str, Any],
    plugins: list[dict[str, Any]],
) -> tuple[Worker, str]:
    """A new worker for the registration's tier, and its token (shown once)."""
    value = _token(WORKER_PREFIX)
    now = timezone.now()
    with transaction.atomic():
        worker = Worker.objects.create(
            name=name,
            tier=registration.tier,
            token_hash=_hash(value),
            hint=value[-4:],
            registration=registration,
            version=version,
            capacity={k: capacity.get(k) for k in CAPACITY_KEYS},
            plugins=plugins,
            last_seen_at=now,
        )
        RegistrationToken.objects.filter(pk=registration.pk).update(last_used_at=now)
        audit.record(
            "worker.registered",
            target=worker,
            data={
                "tier": worker.tier,
                "registration": str(registration.pk),
                "version": version,
                "plugins": plugins,
            },
        )
    return worker, value


def update(
    worker: Worker, *, version: str, capacity: dict[str, Any], plugins: list[dict[str, Any]]
) -> Worker:
    """What a worker that kept its token says it is now, each time it starts: a new image
    can bring a new version, plugins or environments. Audited when it changes."""
    capacity = {k: capacity.get(k) for k in CAPACITY_KEYS}
    changed = {
        key: value
        for key, value in (("version", version), ("capacity", capacity), ("plugins", plugins))
        if getattr(worker, key) != value
    }
    with transaction.atomic():
        Worker.objects.filter(pk=worker.pk).update(**changed, last_seen_at=timezone.now())
        if changed:
            audit.record("worker.updated", target=worker, data=changed)
    for key, value in changed.items():
        setattr(worker, key, value)
    return worker


def authenticate(header: str) -> Worker | None:
    """The live worker an ``Authorization: Bearer`` header names, or None.

    A worker stops working when it's revoked, and (for the client tier) when its person is
    deactivated. Revoking its registration token doesn't stop it.
    """
    value = _bearer(header, WORKER_PREFIX)
    if value is None:
        return None
    worker = (
        Worker.objects.select_related("owner")
        .filter(token_hash=_hash(value), revoked_at__isnull=True)
        .first()
    )
    if worker is None:
        return None
    if worker.owner is not None and not worker.owner.is_active:
        return None
    now = timezone.now()
    if now - worker.last_seen_at >= SEEN_RESOLUTION:
        Worker.objects.filter(pk=worker.pk).update(last_seen_at=now)
        worker.last_seen_at = now
    return worker


def revoke_worker(worker: Worker, *, by: User | None) -> bool:
    """Revoke a worker's token. Its jobs are interrupted, and retried under their policy."""
    from iki_server.runs import queue

    with transaction.atomic():
        locked = Worker.objects.select_for_update().get(pk=worker.pk)
        if locked.revoked_at is not None:
            return False
        locked.revoked_at = timezone.now()
        locked.save(update_fields=["revoked_at"])
        audit.record("worker.revoked", actor=by, target=locked)
        queue.interrupt_worker(locked, code="worker_revoked")
    return True
