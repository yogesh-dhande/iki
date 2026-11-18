"""What people were told, how they want to be told, and which records they watch."""

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from iki_server.core.ids import uuid7


class Notification(models.Model):
    """Something one person should know about, shown in Iki and maybe emailed.

    The title and body are written when the notification is created, so they
    say what was true then, as an email would. One about records (ADR 0129) says which:
    ``entity`` while it's about one, and its ``records`` as more are told of together.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    #: A kind (see ``kinds.py``), such as ``project.added``, or a type's declared one,
    #: such as ``iki/example_lims.Sample.in_review``.
    kind = models.CharField(max_length=200)
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
    #: The one record it's about, whose values its title may show: people who can no
    #: longer read it stop seeing the notification.
    entity = models.ForeignKey(
        "iki_entities.Entity", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    #: How many records it's about: more than one once several are told of together.
    count = models.PositiveIntegerField(default=1)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    #: The agent that made the change, for the actor (ADR 0072).
    agent = models.ForeignKey(
        "iki_audit.Agent", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
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
            # What a new one about records is told with: the person's unread, unemailed
            # one of its kind, in its project (ADR 0129).
            models.Index(
                fields=["recipient", "kind", "project", "-created_at"],
                name="iki_notification_open",
                condition=Q(read_at__isnull=True, emailed_at__isnull=True),
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(path="") | (Q(path__startswith="/") & ~Q(path__startswith="//")),
                name="iki_notification_local_path",
            ),
            models.CheckConstraint(condition=Q(count__gte=1), name="iki_notification_count"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} for {self.recipient_id}: {self.title}"


class NotificationRecord(models.Model):
    """A record a notification is about (ADR 0129)."""

    notification = models.ForeignKey(Notification, on_delete=models.CASCADE, related_name="records")
    entity = models.ForeignKey("iki_entities.Entity", on_delete=models.CASCADE, related_name="+")

    class Meta:
        db_table = "iki_notification_record"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["notification", "entity"], name="iki_notification_record_once"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.notification_id}: {self.entity_id}"


class Delivery(models.TextChoices):
    """How someone is told of a kind of notification."""

    EMAIL = "email", "In Iki and by email"
    IN_APP = "in_app", "In Iki only"
    #: Not at all: for a type's notifications that aren't required (ADR 0129).
    OFF = "off", "Off"


class NotificationSetting(models.Model):
    """One person's choice for one kind: everywhere, or in one project, which overrides it
    there. Without a row, the kind's default applies."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_settings"
    )
    kind = models.CharField(max_length=200)
    #: The project it's for, or None for every project.
    project = models.ForeignKey(
        "iki_org.Project", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    delivery = models.CharField(max_length=10, choices=Delivery.choices)

    class Meta:
        db_table = "iki_notification_setting"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["user", "kind", "project"],
                name="iki_notification_setting_once",
                nulls_distinct=False,
            ),
            models.CheckConstraint(
                condition=Q(delivery__in=[d.value for d in Delivery]),
                name="iki_notification_setting_delivery",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.user_id} {self.kind} in {self.project_id or 'every project'}: {self.delivery}"
        )


class WatchMode(models.TextChoices):
    #: Told what its type tells its watchers, as well as what names them.
    WATCHING = "watching", "Watching"
    #: Told nothing of it but what's required.
    MUTED = "muted", "Muted"


class Watch(models.Model):
    """One person watching a record, or muting it (ADR 0129). Without a row, they're told
    what names them: as its fields, its creator, or a lab role."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="watches"
    )
    entity = models.ForeignKey("iki_entities.Entity", on_delete=models.CASCADE, related_name="+")
    mode = models.CharField(max_length=10, choices=WatchMode.choices)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "iki_notification_watch"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["user", "entity"], name="iki_watch_once"),
            models.CheckConstraint(
                condition=Q(mode__in=[m.value for m in WatchMode]), name="iki_watch_mode"
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["entity", "mode"], name="iki_watch_entity"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} {self.mode} {self.entity_id}"
