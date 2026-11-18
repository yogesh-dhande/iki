"""Background work for notifications."""

import smtplib
from typing import Any

import structlog
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from iki_server.audit import log as audit
from iki_server.notifications import kinds
from iki_server.notifications.kinds import Kind
from iki_server.notifications.models import Delivery, Notification
from iki_server.setup.models import Installation
from iki_server.tasks import task
from iki_server.tasks.backend import GiveUp

logger = structlog.get_logger("iki.notifications")


def absolute(path: str) -> str:
    return settings.IKI_PUBLIC_URL.rstrip("/") + path


def headline(notification: Notification, kind: Kind) -> str:
    """What an email says it's about. A type's notification (ADR 0129) says only what it is
    and which record, never the record's values: its title, written from them, stays in
    Iki, where reading it is checked."""
    if not kind.declared:
        return notification.title
    if notification.count > 1:
        from iki_server.entities import registry

        found = registry.types().get(kind.type)
        plural = (found.label_plural or found.label) if found is not None else "records"
        return f"{notification.count} {plural}: {kind.label}"
    hrid = notification.entity.hrid if notification.entity is not None else ""
    return f"{hrid}: {kind.label}" if hrid else kind.label


def who(notification: Notification) -> str:
    """Who caused it, as records name them (ADR 0072): a person, through an agent."""
    if notification.actor is None:
        return ""
    name = str(notification.actor.get_full_name())
    if notification.agent is not None:
        return f"{name}, through {notification.agent.client_name}"
    return name


def message(notification: Notification) -> EmailMultiAlternatives:
    installation = Installation.get()
    organization = installation.organization_name or "Iki"
    kind = kinds.find(notification.kind)
    declared = kind is not None and kind.declared
    title = headline(notification, kind) if kind is not None else notification.title
    context: dict[str, Any] = {
        "notification": notification,
        "title": title,
        # A type's notification's body is written from the record's values: Iki shows it.
        "body": "" if declared else notification.body,
        "description": kind.description if declared and kind is not None else "",
        "who": who(notification),
        # Core's bodies say which project themselves.
        "project": notification.project.name
        if declared and notification.project is not None
        else "",
        "details": installation.email_details,
        "organization": organization,
        "url": absolute(notification.path or "/notifications/"),
        "preferences_url": absolute("/account/notifications/"),
    }
    subject = title if installation.email_details else "You have a new notification"
    email = EmailMultiAlternatives(
        subject=f"[{organization}] {subject}".replace("\n", " ").replace("\r", " "),
        body=render_to_string("notifications/email.txt", context),
        to=[notification.recipient.email],
        headers={
            # RFC 3834: automatic, so autoresponders (out-of-office) stay quiet.
            "Auto-Submitted": "auto-generated",
            # Exchange and Outlook's equivalent.
            "X-Auto-Response-Suppress": "All",
            "List-Unsubscribe": f"<{context['preferences_url']}>",
        },
    )
    email.attach_alternative(render_to_string("notifications/email.html", context), "text/html")
    return email


@task(max_attempts=6)
def send_email(notification_id: str) -> str:
    """Emails one notification, unless it's gone, the person can no longer see it, or they no
    longer want it. Safe to run twice: it locks the notification, and records that it sent."""
    from iki_server.notifications.services import deliveries, email_enabled, inbox

    with transaction.atomic():
        notification = (
            Notification.objects.select_for_update(of=("self",))
            .select_related("recipient", "project", "entity", "actor", "agent")
            .filter(pk=notification_id)
            .first()
        )
        if notification is None:
            return "deleted"
        if notification.emailed_at is not None:
            return "already sent"
        recipient = notification.recipient
        if not email_enabled():
            return "email is off"
        if not recipient.is_active or not recipient.email:
            return "recipient can't receive it"
        kind = kinds.find(notification.kind)
        if kind is None:
            raise GiveUp(f"The notification kind {notification.kind!r} isn't registered anymore.")
        if deliveries([recipient], kind, notification.project)[recipient.pk] != Delivery.EMAIL:
            return "turned off"
        if not inbox(recipient).filter(pk=notification.pk).exists():
            # They can no longer read what it's about (or see its project) since it was sent.
            return "no longer theirs to see"
        try:
            message(notification).send()
        except smtplib.SMTPRecipientsRefused as error:
            raise GiveUp("The mail server refused the recipient.") from error
        except smtplib.SMTPResponseException as error:
            # 5xx is permanent (the mail server will never take it); 4xx is worth retrying.
            if error.smtp_code >= 500:
                raise GiveUp(f"The mail server refused it ({error.smtp_code}).") from error
            raise
        notification.emailed_at = timezone.now()
        notification.save(update_fields=["emailed_at"])
        if kind.declared:
            # Who was told of what by email, as well as in Iki (ADR 0129).
            audit.record(
                "notification.emailed",
                target=recipient,
                project=notification.project,
                agent=None,
                data={
                    "notification": str(notification.pk),
                    "kind": kind.key,
                    "label": kind.label,
                    "records": notification.count,
                    **({"hrid": notification.entity.hrid} if notification.entity else {}),
                },
            )
    logger.info("notification_emailed", notification=notification_id, kind=notification.kind)
    return "sent"
