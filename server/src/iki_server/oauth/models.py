"""OAuth clients and the access people grant them (ADR 0028).

Clients come from four places:

- **First party:** Iki's own CLI and SDK, created by a migration.
- **Self-registered:** MCP clients and other tools register themselves, by
  Dynamic Client Registration (RFC 7591) or a Client ID Metadata Document.
  Unless the organization allows any client, an Org Admin approves each one
  before anyone can use it, as with GitHub's OAuth app access restrictions.
- **Service-account credentials:** client credentials an Org Admin creates
  for a service account; tokens act as that account.

People sign in to a client by granting it a :class:`Consent`: some scopes,
and either all their projects or only the ones they pick. Changing or
revoking a consent applies to the client's tokens at once.
"""

from typing import Any, ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from oauth2_provider.models import AbstractApplication

from iki_server.core.ids import uuid7
from iki_server.oauth import redirects
from iki_server.org.models import Project


class Approval(models.TextChoices):
    PENDING = "pending", "Waiting for approval"
    APPROVED = "approved", "Approved"
    BLOCKED = "blocked", "Blocked"


class Client(AbstractApplication):
    approval = models.CharField(max_length=16, choices=Approval.choices, default=Approval.PENDING)
    approval_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approval_changed_at = models.DateTimeField(null=True, blank=True)
    #: Someone who asked an Org Admin to approve the client.
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    requested_at = models.DateTimeField(null=True, blank=True)
    #: Iki's own CLI and SDK.
    first_party = models.BooleanField(default=False)
    #: For client credentials: the service account tokens act as.
    service_account = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="oauth_credentials",
    )
    #: Space-separated scopes this client may get; empty means any.
    allowed_scopes = models.CharField(max_length=200, blank=True)
    #: A service account's credential an Org Admin allowed to act for people who delegate
    #: to it, by token exchange (ADR 0073).
    acts_for_people = models.BooleanField(default=False)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta(AbstractApplication.Meta):
        db_table = "iki_oauth_client"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(service_account__isnull=True)
                | Q(client_type="confidential", authorization_grant_type="client-credentials"),
                name="iki_oauth_service_credentials_shape",
            ),
            models.CheckConstraint(
                condition=Q(acts_for_people=False) | Q(service_account__isnull=False),
                name="iki_oauth_only_credentials_act_for_people",
            ),
        ]

    @classmethod
    def from_db(cls, db: Any, field_names: Any, values: Any, **kwargs: Any) -> "Client":  # noqa: ANN401
        instance: Client = super().from_db(db, field_names, values, **kwargs)
        instance._loaded_redirect_uris = instance.__dict__.get("redirect_uris")
        return instance

    def clean(self) -> None:
        super().clean()
        if bad := redirects.refused(self.redirect_uris.split()):
            raise ValidationError(
                {
                    "redirect_uris": "Redirect URIs must use https, or http to a loopback "
                    f"address for apps on your own computer: {bad[0]}"
                }
            )

    def save(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        from iki_server.oauth.services import initial_approval

        registering = self._state.adding and self.is_self_registered
        if registering:
            self.approval = initial_approval()
        elif (
            self.is_self_registered
            and self.approval == Approval.APPROVED
            and getattr(self, "_loaded_redirect_uris", self.redirect_uris) != self.redirect_uris
        ):
            # A client that changes where it sends people needs approving again.
            self.approval = initial_approval()
        super().save(*args, **kwargs)
        self._loaded_redirect_uris = self.redirect_uris
        if registering:
            from iki_server.audit import log as audit

            audit.record(
                "oauth.client_registered",
                target=self,
                data={
                    "source": self.registration_source,
                    "redirect_uris": self.redirect_uris.split(),
                    "approval": self.approval,
                },
            )

    @property
    def is_approved(self) -> bool:
        return self.approval == Approval.APPROVED

    @property
    def is_self_registered(self) -> bool:
        return self.registration_source in (
            self.RegistrationSource.DCR,
            self.RegistrationSource.CIMD,
        )

    def is_usable(self, request: Any) -> bool:  # noqa: ANN401 - an oauthlib request
        if not self.is_approved:
            return False
        if self.service_account_id is not None:
            return bool(self.service_account and self.service_account.is_active)
        return True

    def display_name(self) -> str:
        return self.name or self.client_id


class Consent(models.Model):
    """What someone allowed a client to do for them."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="oauth_consents"
    )
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="consents")
    #: Names this grant on what the client does with it (ADR 0072). It stays while the
    #: consent changes; a consent given again after it was revoked is a new grant.
    grant = models.UUIDField(default=uuid7, unique=True, editable=False)
    #: Space-separated scopes.
    scopes = models.CharField(max_length=200)
    #: All the projects the person can access, now and later; otherwise only ``projects``.
    all_projects = models.BooleanField(default=False)
    projects = models.ManyToManyField(Project, blank=True, related_name="+")
    #: When a delegation to a service account's credential ends (ADR 0073). Consents to
    #: apps the person signs in to don't expire.
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "iki_oauth_consent"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["user", "client"], name="iki_oauth_consent_once"),
        ]

    def __str__(self) -> str:
        return f"{self.user} allowed {self.client.display_name()}"

    @property
    def scope_list(self) -> list[str]:
        return self.scopes.split()

    @property
    def is_delegation(self) -> bool:
        """A person letting a service account's credential act for them (ADR 0073)."""
        return self.client.service_account_id is not None

    @property
    def expired(self) -> bool:
        from django.utils import timezone

        return self.expires_at is not None and self.expires_at <= timezone.now()
