"""What people were told, and how they want to be told."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from iki_server.core.ids import uuid7


class Notification(models.Model):
    """Something one person should know about, shown in Iki and maybe emailed.

    The title and body are written when the notification is created, so they
    say what was true then, as an email would.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    #: A registered kind (see ``kinds.py``), such as ``project.added``.
    kind = models.CharField(max_length=100)
    title = models.CharField(max_length=300)
    body = models.TextField(blank=True)
    #: Where it leads in Iki: a path on this site, never another site.
    path = models.CharField(max_length=500, blank=True)
    #: The project it's about, if any. People who can no longer see the
    #: project stop seeing the notification (and tokens limited to other
    #: projects never see it).
    project = models.ForeignKey(
        "iki_org.Project", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True, blank=True)
    #: When the email went out, so a retried task never sends it twice.
    emailed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_notification"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["recipient", "-id"], name="iki_notification_inbox"),
            models.Index(
                fields=["recipient"],
                name="iki_notification_unread",
                condition=Q(read_at__isnull=True),
            ),
            models.Index(fields=["created_at"], name="iki_notification_age"),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(path="") | (Q(path__startswith="/") & ~Q(path__startswith="//")),
                name="iki_notification_local_path",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} for {self.recipient_id}: {self.title}"


class EmailPreference(models.Model):
    """One person's choice about email for one kind. Without a row, the kind's default applies."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_preferences"
    )
    kind = models.CharField(max_length=100)
    email = models.BooleanField()

    class Meta:
        db_table = "iki_notification_email_preference"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["user", "kind"], name="iki_email_preference_once"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} {self.kind}: {'email' if self.email else 'no email'}"
