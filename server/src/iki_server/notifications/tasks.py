"""Background work for notifications."""

import smtplib

import structlog
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from iki_server.notifications import kinds
from iki_server.notifications.models import Notification
from iki_server.setup.models import Installation
from iki_server.tasks import task
from iki_server.tasks.backend import GiveUp

logger = structlog.get_logger("iki.notifications")


def absolute(path: str) -> str:
    return settings.IKI_PUBLIC_URL.rstrip("/") + path


def message(notification: Notification) -> EmailMultiAlternatives:
    installation = Installation.get()
    organization = installation.organization_name or "Iki"
    context = {
        "notification": notification,
        "details": installation.email_details,
        "organization": organization,
        "url": absolute(notification.path or "/notifications/"),
        "preferences_url": absolute("/account/notifications/"),
    }
    subject = notification.title if installation.email_details else "You have a new notification"
    email = EmailMultiAlternatives(
        subject=f"[{organization}] {subject}".replace("\n", " "),
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
    """Emails one notification, unless it's gone or the person no longer wants it."""
    from iki_server.notifications.services import email_enabled, wants_email

    with transaction.atomic():
        notification = (
            Notification.objects.select_for_update(of=("self",))
            .select_related("recipient")
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
        kind = kinds.KINDS.get(notification.kind)
        if kind is None:
            raise GiveUp(f"The notification kind {notification.kind!r} isn't registered anymore.")
        if recipient.pk not in wants_email([recipient], kind):
            return "turned off"
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
    logger.info("notification_emailed", notification=notification_id, kind=notification.kind)
    return "sent"
