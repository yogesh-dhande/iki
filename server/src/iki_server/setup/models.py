"""State of this Iki installation and the one-time links that start setup."""

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import ClassVar

from django.db import models
from django.db.models import Q
from django.utils import timezone

from iki_server.core.ids import uuid7

#: How long a setup link can be opened after it is created.
LINK_LIFETIME = timedelta(minutes=60)
#: How long the wizard stays open after the link is opened.
GRANT_LIFETIME = timedelta(minutes=60)


class Installation(models.Model):
    """The single row describing this deployment (one organization per deployment)."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    organization_name = models.CharField(max_length=100, blank=True)
    setup_completed_at = models.DateTimeField(null=True, blank=True)
    #: New self-registered OAuth clients wait for an Org Admin's approval (ADR 0028).
    require_oauth_app_approval = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "iki_installation"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=Q(id=1), name="iki_installation_singleton")
        ]

    def __str__(self) -> str:
        return self.organization_name or "Iki"

    @classmethod
    def get(cls) -> "Installation":
        installation, _ = cls.objects.get_or_create(pk=1)
        return installation

    @property
    def is_set_up(self) -> bool:
        return self.setup_completed_at is not None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SetupTokenQuerySet(models.QuerySet["SetupToken"]):
    def live(self) -> "SetupTokenQuerySet":
        return self.filter(revoked_at__isnull=True)


class SetupToken(models.Model):
    """A one-time setup link, created with ``iki-server setup_link``.

    Only the SHA-256 of the token is stored. Opening the link exchanges the
    token for a setup grant in that browser's session; the token can't be
    used again. Creating a new link revokes every earlier one.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    token_hash = models.CharField(max_length=64, unique=True)
    #: Reconfigures sign-in on an installation that is already set up (break-glass).
    recovery = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    exchanged_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = SetupTokenQuerySet.as_manager()

    class Meta:
        db_table = "iki_setup_token"

    def __str__(self) -> str:
        return f"setup token {self.id}"

    @classmethod
    def issue(cls, *, recovery: bool) -> tuple["SetupToken", str]:
        """Revokes earlier tokens and returns a new one with its plaintext value."""
        now = timezone.now()
        cls.objects.live().update(revoked_at=now)
        token = secrets.token_urlsafe(32)
        record = cls.objects.create(
            token_hash=hash_token(token),
            recovery=recovery,
            created_at=now,
            expires_at=now + LINK_LIFETIME,
        )
        return record, token

    @property
    def grant_expires_at(self) -> datetime | None:
        return self.exchanged_at + GRANT_LIFETIME if self.exchanged_at else None
