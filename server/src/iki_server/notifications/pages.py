"""Pages: each person's email choices, and the organization's email settings."""

import smtplib
import uuid
from itertools import groupby
from typing import Any

import structlog
from django.conf import settings
from django.contrib import messages
from django.core.mail import EmailMessage
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from iki_server.audit import log as audit
from iki_server.notifications import services
from iki_server.notifications.models import Delivery
from iki_server.org import permissions
from iki_server.org.permissions import Action
from iki_server.org.views import _user, page
from iki_server.setup.models import Installation

logger = structlog.get_logger("iki.notifications")


@page("GET")
def account(request: HttpRequest) -> HttpResponse:
    return redirect("notifications:preferences")


@page("GET", "POST")
def preferences(request: HttpRequest) -> HttpResponse:
    me = _user(request)
    available = services.kinds_for(me)
    if request.method == "POST":
        try:
            if request.POST.get("action") == "forget":
                services.set_choices(
                    me,
                    projects=[
                        (
                            uuid.UUID(request.POST.get("project", "")),
                            request.POST.get("kind", ""),
                            None,
                        )
                    ],
                )
                messages.success(request, "That project follows your choices here now.")
            else:
                services.set_choices(
                    me,
                    {
                        kind.key: request.POST[f"delivery:{kind.key}"]
                        for kind in available
                        if f"delivery:{kind.key}" in request.POST
                    },
                )
                messages.success(request, "Saved your notification choices.")
        except (services.Invalid, ValueError) as error:
            messages.error(request, str(error) or "Those choices couldn’t be saved.")
        return redirect(request.path)
    chosen = services.choices(me)
    groups = [
        (group, [(kind, chosen[kind.key].value) for kind in kinds])
        for group, kinds in groupby(available, key=lambda k: k.group)
    ]
    labels = {kind.key: kind.label for kind in available}
    projects = [
        {
            "project": row.project,
            "kind": row.kind,
            "label": labels.get(row.kind, row.kind),
            "delivery_label": Delivery(row.delivery).label,
        }
        for row in services.project_choices(me)
    ]
    return render(
        request,
        "notifications/preferences.html",
        {
            "groups": groups,
            "email_available": services.email_enabled(),
            "declared": any(kind.declared for kind in available),
            "projects": projects,
        },
    )


def _test_email(request: HttpRequest) -> None:
    me = _user(request)
    try:
        EmailMessage(
            subject="[Iki] Test email",
            body=(
                "This is a test from Iki's email settings. If you can read it, notification "
                "emails will reach people too.\n"
            ),
            to=[me.email],
            headers={"Auto-Submitted": "auto-generated", "X-Auto-Response-Suppress": "All"},
        ).send()
    except (smtplib.SMTPException, OSError) as error:
        logger.warning("test_email_failed", error=str(error))
        messages.error(
            request,
            f"Couldn’t send it: {type(error).__name__}: {error}. "
            "Check IKI_EMAIL_URL, then try again.",
        )
        return
    logger.info("test_email_sent")
    messages.success(request, f"Sent a test email to {me.email}.")


@page("GET", "POST")
def org_email(request: HttpRequest) -> HttpResponse:
    permissions.require(request.user, Action.ORG_MANAGE)
    me = _user(request)
    installation = Installation.get()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "details":
            details = request.POST.get("email_details") == "on"
            if details != installation.email_details:
                with transaction.atomic():
                    installation.email_details = details
                    installation.save(update_fields=["email_details", "updated_at"])
                    audit.record(
                        "notifications.email_details_changed",
                        actor=me,
                        data={"details": details},
                    )
            messages.success(
                request,
                "Emails now say what happened."
                if details
                else "Emails now only say there’s something new, with a link.",
            )
        elif action == "test" and services.email_enabled():
            _test_email(request)
        return redirect(request.path)
    mailer: dict[str, Any] = settings.MAILERS["default"].get("OPTIONS", {})
    return render(
        request,
        "notifications/org_email.html",
        {
            "email_available": services.email_enabled(),
            "sender": settings.DEFAULT_FROM_EMAIL,
            "server": f"{mailer.get('host', '')}:{mailer.get('port', '')}",
            "encryption": (
                "TLS" if mailer.get("use_ssl") else "STARTTLS" if mailer.get("use_tls") else "none"
            ),
            "details": installation.email_details,
        },
    )
