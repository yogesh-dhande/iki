"""Registering workers, and the tokens they use (ADR 0043).

A compute admin makes a registration token for a tier (``iki_wreg_…``) and
gives it to that tier's workers: a Kubernetes secret for backend pods, the
launch configuration of a VM. A worker registers with it once and gets its
own token (``iki_wkr_…``), which it uses for everything else. Only SHA-256
hashes of both are stored; each is shown once. Revoking a registration
token stops new workers registering with it (and, if asked, revokes those
that did). Revoking a worker interrupts its jobs, which are retried elsewhere.

A worker on someone's own computer (the client tier) signs in as them instead
(ADR 0058): ``iki-worker login`` takes them through OAuth's device flow as the
``iki-worker`` client, and registers with the access token that gives it, for the
``worker`` scope alone. Registering spends that token. The worker leases only its
person's runs, in the projects they allowed it, and stops working when they're
deactivated, revoke it, or revoke the app.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from packaging.version import InvalidVersion, Version

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.runs.models import RegistrationToken, Worker

__all__ = [
    "REGISTRATION_PREFIX",
    "WORKER_PREFIX",
    "Outdated",
    "allowed_projects",
    "authenticate",
    "check_version",
    "create_registration",
    "online_owners",
    "register",
    "register_client",
    "revoke_registration",
    "revoke_signed_in",
    "revoke_worker",
    "signed_in",
    "update_for",
]

REGISTRATION_PREFIX = "iki_wreg_"
WORKER_PREFIX = "iki_wkr_"
#: ``last_seen_at`` and ``last_used_at`` are written at most this often, not on every call.
SEEN_RESOLUTION = timedelta(seconds=15)

#: The OAuth client a worker signs in on someone's computer with, and the scope it needs.
CLIENT_ID = "iki-worker"
SCOPE = "worker"


class Outdated(Exception):  # noqa: N818 - reads as "the worker is outdated"
    """A worker more than one minor release behind the one the server wants (ADR 0060)."""

    def __init__(self, version: str) -> None:
        wanted = settings.IKI_WORKER_VERSION
        super().__init__(
            f"iki-worker {version} is too old for this server, which takes {wanted} or the "
            "minor release before it. Update iki-worker."
        )
        self.version = version


def _parsed(version: str) -> Version | None:
    try:
        return Version(version)
    except InvalidVersion:
        return None


def check_version(version: str) -> None:
    """Refuses (with :class:`Outdated`) a worker too old for this server: it takes the
    version it wants and the minor release before it, and anything newer."""
    have, want = _parsed(version), _parsed(settings.IKI_WORKER_VERSION)
    if have is None or want is None:
        return
    oldest = (want.major, max(want.minor - 1, 0))
    if (have.major, have.minor) < oldest:
        raise Outdated(version)


def update_for(worker: Worker) -> dict[str, str] | None:
    """The release a worker on someone's computer should update itself to, if any: the one
    the server wants, when that's a release and not what it runs. Workers in images (the
    other tiers) come with their deployment instead."""
    wanted = settings.IKI_WORKER_VERSION
    want, have = _parsed(wanted), _parsed(worker.version)
    if not settings.IKI_WORKER_UPDATES or worker.tier != "client" or want is None:
        return None
    if want.is_prerelease or want.is_devrelease or want == have:
        return None
    base = f"{settings.IKI_WORKER_DOWNLOADS}/v{wanted}/iki-worker-{wanted}-{{target}}.tar.gz"
    return {"version": wanted, "url": base, "bundle_url": f"{base}.sigstore.json"}


#: A worker not heard from for this long shows as offline.
OFFLINE_AFTER = timedelta(minutes=2)

#: What a worker reports it can give one job.
CAPACITY_KEYS = ("cpu", "memory", "gpu_count", "gpu_memory", "scratch_disk")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def create_registration(
    *, label: str, tier: str, by: User | None, expires_at: datetime | None = None
) -> tuple[RegistrationToken, str]:
    """A new registration token, and its value (shown once). ``by`` is None when an
    operator makes it with `iki-server create_registration_token`."""
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


def signed_in(token: str) -> tuple[User, Any] | None:
    """The person an ``iki-worker`` access token is for, and the token, if it may register
    a worker on their computer: it's current, for the ``worker`` scope, from the
    ``iki-worker`` client, and they're active and still allow the app."""
    from oauth2_provider.models import get_access_token_model

    from iki_server.oauth.models import Consent

    found = (
        get_access_token_model()
        .objects.select_related("user", "application")
        .filter(token_checksum=hashlib.sha256(token.encode()).hexdigest())
        .first()
    )
    if found is None or found.is_expired() or found.user is None or found.application is None:
        return None
    if found.application.client_id != CLIENT_ID or SCOPE not in found.scope.split():
        return None
    if not found.user.is_active or found.user.is_service_account:
        return None
    if not Consent.objects.filter(user=found.user, client=found.application).exists():
        return None
    return found.user, found


def online_owners(people: set[Any]) -> set[Any]:
    """Which of `people` have a client-tier worker online now."""
    return set(
        Worker.objects.filter(
            owner__in=people,
            tier="client",
            revoked_at__isnull=True,
            last_seen_at__gt=timezone.now() - OFFLINE_AFTER,
        ).values_list("owner_id", flat=True)
    )


def allowed_projects(worker: Worker) -> list[Any] | None:
    """The projects a client-tier worker may run its person's jobs in: None for all they
    can access, and none at all once they've revoked the app."""
    from iki_server.oauth.models import Consent

    consent = (
        Consent.objects.filter(user=worker.owner_id, client__client_id=CLIENT_ID)
        .prefetch_related("projects")
        .first()
    )
    if consent is None or SCOPE not in consent.scope_list:
        return []
    if consent.all_projects:
        return None
    return [p.pk for p in consent.projects.all()]


def revoke_signed_in(person: User, *, by: User | None) -> int:
    """Revoke the workers `person` signed in on their computers, when they revoke the app
    (or an admin does)."""
    revoked = 0
    for worker in Worker.objects.filter(owner=person, tier="client", revoked_at__isnull=True):
        revoked += revoke_worker(worker, by=by)
    return revoked


def register_client(
    person: User,
    access_token: Any,  # noqa: ANN401 - an oauth2_provider AccessToken
    *,
    name: str,
    version: str,
    capacity: dict[str, Any],
    plugins: list[dict[str, Any]],
) -> tuple[Worker, str]:
    """A client-tier worker on `person`'s computer, and its token (shown once). The access
    token it signed in with is spent: the worker's own token is all it keeps."""
    value = _token(WORKER_PREFIX)
    now = timezone.now()
    with transaction.atomic():
        worker = Worker.objects.create(
            name=name,
            tier="client",
            token_hash=_hash(value),
            hint=value[-4:],
            owner=person,
            version=version,
            capacity={k: capacity.get(k) for k in CAPACITY_KEYS},
            plugins=plugins,
            last_seen_at=now,
        )
        refresh = getattr(access_token, "refresh_token", None)
        if refresh is not None:
            refresh.revoke()
        access_token.revoke()
        audit.record(
            "worker.registered",
            actor=person,
            target=worker,
            data={"tier": "client", "version": version, "plugins": plugins},
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
