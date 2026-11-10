"""SCIM 2.0 provisioning: the identity provider's token, and the groups it pushes.

People provisioned over SCIM are ordinary :class:`~iki_server.accounts.models.User`
rows with ``provisioned_at`` set. Groups are kept here. A group affects
nothing until an Org Admin links it to a team; from then on the linked
groups decide who is on that team (ADR 0026).
"""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from iki_server.accounts.models import CASE_INSENSITIVE
from iki_server.core.ids import uuid7
from iki_server.org.models import Team


class ScimToken(models.Model):
    """The bearer token the identity provider uses to call Iki's SCIM API.

    Only its SHA-256 is stored; the token is shown once, when created. At
    most one token is live: creating a new one revokes the old one. SCIM
    provisioning is on while a token is live.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    token_hash = models.CharField(max_length=64, unique=True)
    #: The token's last four characters, so people can tell tokens apart.
    hint = models.CharField(max_length=4)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_scim_token"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["revoked_at"],
                condition=Q(revoked_at__isnull=True),
                name="iki_one_live_scim_token",
                nulls_distinct=False,
            )
        ]

    def __str__(self) -> str:
        return f"SCIM token …{self.hint}"


class ScimGroup(models.Model):
    """A group the identity provider pushed, such as "Chemistry" in Entra ID."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    display_name = models.CharField(max_length=255, unique=True, db_collation=CASE_INSENSITIVE)
    external_id = models.CharField(max_length=255, blank=True)
    #: The team whose members this group decides. Several groups may feed one team.
    team = models.ForeignKey(
        Team, null=True, blank=True, on_delete=models.SET_NULL, related_name="scim_groups"
    )
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through="ScimGroupMember", related_name="scim_groups"
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "iki_scim_group"
        ordering = ("display_name",)

    def __str__(self) -> str:
        return self.display_name


class ScimGroupMember(models.Model):
    group = models.ForeignKey(ScimGroup, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")

    class Meta:
        db_table = "iki_scim_group_member"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["group", "user"], name="iki_scim_group_member_once"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} in {self.group_id}"
