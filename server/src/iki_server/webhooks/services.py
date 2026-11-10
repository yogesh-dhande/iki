"""Managing webhooks, sending them events, and recording what happened.

Who may do what:

- **Project webhooks** are managed by the project's Admins, while the
  organization lets project Admins add webhooks (on by default, as GitHub
  does). With that off, project webhooks stop sending, and project Admins
  can only turn theirs off or delete them.
- **Org webhooks** are managed by Org Admins. They receive organization
  events: people, teams, org roles, and projects' names and members, which
  Org Admins already see.
- **Org Admins govern every webhook.** They see where each project's
  webhook sends (its host, not its address or deliveries, which carry
  project data) and can turn any of them off. A webhook an Org Admin turned
  off stays off until an Org Admin turns it on.
"""

import json
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import structlog
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.core import crypto
from iki_server.notifications import kinds as notification_kinds
from iki_server.notifications.services import notify, org_admins
from iki_server.org import permissions
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.org.permissions import Action
from iki_server.org.services import Refused as OrgRefused
from iki_server.setup.models import Installation
from iki_server.webhooks import events, sender, signing
from iki_server.webhooks.models import Delivery, DeliveryStatus, DisabledReason, Webhook

logger = structlog.get_logger("iki.webhooks")

#: A webhook whose deliveries all fail for this long is turned off.
DISABLE_AFTER = timedelta(days=5)
#: After a rotation, messages are also signed with the previous secret this long.
ROTATION_OVERLAP = timedelta(hours=24)
#: Webhooks per project, and for the organization.
MAX_WEBHOOKS = 20
#: Attempts kept on a delivery's record.
MAX_ATTEMPTS_RECORDED = 20


class Refused(OrgRefused):
    """The change isn't allowed; the message says why, next to ``field`` if given."""


def display_url(url: str) -> str:
    """The address without its path and query, which may carry a token."""
    parts = urlsplit(url)
    rest = "/…" if (parts.path not in ("", "/") or parts.query) else "/"
    return f"{parts.scheme}://{parts.netloc}{rest}"


def project_webhooks_allowed() -> bool:
    return Installation.get().project_webhooks


# --- Permissions ---------------------------------------------------------------------


def can_manage(user: User, webhook: Webhook) -> bool:
    """Change, delete, rotate, test, read deliveries."""
    if webhook.project is None:
        return permissions.can(user, Action.ORG_WEBHOOKS_MANAGE)
    return permissions.can(user, Action.PROJECT_WEBHOOKS_MANAGE, webhook.project)


def can_govern(user: User) -> bool:
    """See every webhook's destination, and turn any of them off."""
    return permissions.can(user, Action.ORG_WEBHOOKS_MANAGE)


def can_see(user: User, webhook: Webhook) -> bool:
    return can_manage(user, webhook) or can_govern(user)


def manageable(user: User) -> QuerySet[Webhook]:
    """The webhooks this person manages: their projects', and the org's if Org Admin."""
    projects = [
        p.pk
        for p in permissions.visible_projects(user).select_related("team")
        if permissions.can(user, Action.PROJECT_WEBHOOKS_MANAGE, p)
    ]
    found = Q(project__in=projects)
    if can_govern(user):
        found |= Q(project__isnull=True)
    return Webhook.objects.filter(found)


# --- Changes -------------------------------------------------------------------------


def _check(url: str, chosen: list[str], scope: str) -> list[str]:
    try:
        sender.check_url(url)
    except sender.Refused as error:
        raise Refused(str(error), field="url") from error
    allowed = {e.key for e in events.for_scope(scope)} - {events.PING}
    unknown = sorted(set(chosen) - allowed)
    if unknown:
        raise Refused(
            f"These events aren’t sent to {scope} webhooks: {', '.join(unknown)}.",
            field="events",
        )
    if not chosen:
        raise Refused("Choose at least one event.", field="events")
    return sorted(set(chosen))


def _require_policy(project: Project | None) -> None:
    if project is not None and not project_webhooks_allowed():
        raise Refused("Your organization only lets Org Admins add webhooks.")


def create(
    *,
    project: Project | None,
    url: str,
    description: str,
    chosen: list[str],
    by: User,
) -> tuple[Webhook, str]:
    """Adds a webhook and sends it a ping. Returns it and its secret, shown only now."""
    _require_policy(project)
    scope = "project" if project else "org"
    chosen = _check(url, chosen, scope)
    secret = signing.new_secret()
    with transaction.atomic():
        existing = Webhook.objects.select_for_update().filter(project=project)
        if len(existing) >= MAX_WEBHOOKS:
            raise Refused(f"There are already {MAX_WEBHOOKS} webhooks here. Remove one first.")
        webhook = Webhook.objects.create(
            project=project,
            url=url,
            description=description.strip(),
            events=chosen,
            secret=crypto.encrypt(secret),
            created_by=by,
        )
        audit.record(
            "webhook.created",
            actor=by,
            target=webhook,
            project=project,
            data={"host": urlsplit(url).netloc, "events": chosen},
        )
        ping(webhook, by=by)
    return webhook, secret


def update(
    webhook: Webhook,
    *,
    by: User,
    url: str | None = None,
    description: str | None = None,
    chosen: list[str] | None = None,
    active: bool | None = None,
) -> Webhook:
    managing = can_manage(by, webhook)
    only_switching = url is None and chosen is None and description is None
    if not managing and not (can_govern(by) and only_switching and active is not None):
        raise Refused("Org Admins can turn a project’s webhook off, but not change it.")
    changes: dict[str, Any] = {}
    with transaction.atomic():
        webhook = Webhook.objects.select_for_update().get(pk=webhook.pk)
        wants_change = url is not None or chosen is not None or active is True
        if wants_change:
            _require_policy(webhook.project)
        if url is not None or chosen is not None:
            new_url = url if url is not None else webhook.url
            new_events = _check(
                new_url, chosen if chosen is not None else webhook.events, webhook.scope
            )
            if new_url != webhook.url:
                changes["host"] = [urlsplit(webhook.url).netloc, urlsplit(new_url).netloc]
                webhook.url = new_url
            if new_events != webhook.events:
                changes["events"] = [webhook.events, new_events]
                webhook.events = new_events
        if description is not None and description.strip() != webhook.description:
            changes["description"] = [webhook.description, description.strip()]
            webhook.description = description.strip()
        if active is not None and active != webhook.active:
            if active:
                if webhook.disabled_reason == DisabledReason.ORG_ADMIN and not can_govern(by):
                    raise Refused("An Org Admin turned this webhook off; only they can turn it on.")
                if not managing and webhook.disabled_reason != DisabledReason.ORG_ADMIN:
                    raise Refused("Org Admins can only turn on webhooks an Org Admin turned off.")
                webhook.disabled_reason = DisabledReason.NONE
                webhook.failing_since = None
            else:
                governing = webhook.project is not None and not managing
                webhook.disabled_reason = (
                    DisabledReason.ORG_ADMIN if governing else DisabledReason.NONE
                )
            changes["active"] = [webhook.active, active]
            webhook.active = active
        if changes:
            webhook.save()
            audit.record(
                "webhook.updated", actor=by, target=webhook, project=webhook.project, data=changes
            )
    return webhook


def delete(webhook: Webhook, *, by: User) -> None:
    with transaction.atomic():
        audit.record(
            "webhook.deleted",
            actor=by,
            target=webhook,
            project=webhook.project,
            data={"host": urlsplit(webhook.url).netloc},
        )
        webhook.delete()


def rotate_secret(webhook: Webhook, *, by: User) -> tuple[str, Any]:
    """A new secret, shown only now. The previous one signs too, for a day."""
    secret = signing.new_secret()
    with transaction.atomic():
        webhook = Webhook.objects.select_for_update().get(pk=webhook.pk)
        webhook.previous_secret = webhook.secret
        webhook.previous_secret_expires_at = timezone.now() + ROTATION_OVERLAP
        webhook.secret = crypto.encrypt(secret)
        webhook.save(update_fields=["secret", "previous_secret", "previous_secret_expires_at"])
        audit.record("webhook.secret_rotated", actor=by, target=webhook, project=webhook.project)
    return secret, webhook.previous_secret_expires_at


def set_project_webhooks_allowed(allowed: bool, *, by: User) -> None:
    with transaction.atomic():
        installation = Installation.objects.select_for_update().get(pk=1)
        if installation.project_webhooks == allowed:
            return
        installation.project_webhooks = allowed
        installation.save(update_fields=["project_webhooks", "updated_at"])
        audit.record(
            "webhooks.project_policy_changed", actor=by, data={"project_webhooks": allowed}
        )


# --- Sending -------------------------------------------------------------------------


def _payload(event: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"type": event, "timestamp": timezone.now().isoformat(), "data": data}


def _queue(
    webhook: Webhook,
    event: str,
    payload: dict[str, Any],
    *,
    message_id: str | None = None,
    redelivered_by: User | None = None,
) -> Delivery:
    from iki_server.webhooks.tasks import deliver

    delivery = Delivery.objects.create(
        message_id=message_id or f"msg_{uuid.uuid4().hex}",
        webhook=webhook,
        event=event,
        payload=payload,
        redelivered_by=redelivered_by,
    )
    deliver.enqueue(str(delivery.pk))
    return delivery


def emit(event: str, data: dict[str, Any], *, project: Project | None) -> list[Delivery]:
    """Sends the event to every active webhook that wants it. Call it in the change's
    transaction: messages exist exactly when the change does."""
    kind = events.EVENTS[event]
    wanted = Q()
    if kind.project and project is not None and project_webhooks_allowed():
        wanted |= Q(project=project)
    if kind.org:
        wanted |= Q(project__isnull=True)
    if not wanted:
        return []
    targets = Webhook.objects.filter(wanted, active=True, events__contains=[event])
    payload = _payload(event, data)
    return [_queue(webhook, event, payload) for webhook in targets]


def ping(webhook: Webhook, *, by: User) -> Delivery:
    data = {
        "webhook": {"id": str(webhook.pk), "events": webhook.events},
        "actor": events.actor_data(by),
    }
    if webhook.project is not None:
        data["project"] = events.project_data(webhook.project)
    return _queue(webhook, events.PING, _payload(events.PING, data))


def redeliver(delivery: Delivery, *, by: User) -> Delivery:
    """Sends the same message again (same ``webhook-id``, so receivers can deduplicate)."""
    webhook = delivery.webhook
    if not webhook.active:
        raise Refused("Turn the webhook on first.")
    _require_policy(webhook.project)
    with transaction.atomic():
        again = _queue(
            webhook,
            delivery.event,
            delivery.payload,
            message_id=delivery.message_id,
            redelivered_by=by,
        )
        audit.record(
            "webhook.redelivered",
            actor=by,
            target=webhook,
            project=webhook.project,
            data={"message_id": delivery.message_id, "event": delivery.event},
        )
    return again


def secrets_for(webhook: Webhook) -> list[str]:
    """The secrets messages are signed with now: the current one, then the previous."""
    found = [crypto.decrypt(webhook.secret)]
    expires = webhook.previous_secret_expires_at
    if webhook.previous_secret and expires and expires > timezone.now():
        found.append(crypto.decrypt(webhook.previous_secret))
    return found


def body(delivery: Delivery) -> bytes:
    return json.dumps(delivery.payload, separators=(",", ":"), ensure_ascii=False).encode()


def record_attempt(delivery_id: str, result: sender.Result, *, final: bool) -> Delivery:
    """Records one attempt, and turns off a webhook that's been failing too long."""
    now = timezone.now()
    with transaction.atomic():
        delivery = (
            Delivery.objects.select_for_update(of=("self",))
            .select_related("webhook__project")
            .get(pk=delivery_id)
        )
        attempt = {
            "at": now.isoformat(),
            "status_code": result.status_code,
            "error": result.error,
            "duration_ms": result.duration_ms,
        }
        delivery.attempts = [*delivery.attempts, attempt][-MAX_ATTEMPTS_RECORDED:]
        if result.ok:
            delivery.status = DeliveryStatus.SUCCEEDED
        elif final:
            delivery.status = DeliveryStatus.FAILED
        if delivery.status != DeliveryStatus.PENDING:
            delivery.finished_at = now
        delivery.save(update_fields=["attempts", "status", "finished_at"])

        webhook = Webhook.objects.select_for_update().get(pk=delivery.webhook_id)
        if result.ok:
            if webhook.failing_since is not None:
                webhook.failing_since = None
                webhook.save(update_fields=["failing_since", "updated_at"])
        elif webhook.failing_since is None:
            webhook.failing_since = now
            webhook.save(update_fields=["failing_since", "updated_at"])
        elif webhook.active and now - webhook.failing_since >= DISABLE_AFTER:
            _disable_failing(webhook)
    return delivery


def _managers(webhook: Webhook) -> QuerySet[User]:
    if webhook.project is None:
        return org_admins()
    admins = ProjectMembership.objects.filter(
        project=webhook.project, role=ProjectRole.ADMIN, user__isnull=False
    ).values("user")
    return User.objects.filter(pk__in=admins, is_active=True)


def _disable_failing(webhook: Webhook) -> None:
    webhook.active = False
    webhook.disabled_reason = DisabledReason.FAILING
    webhook.save(update_fields=["active", "disabled_reason", "updated_at"])
    audit.record(
        "webhook.disabled",
        target=webhook,
        project=webhook.project,
        data={"host": urlsplit(webhook.url).netloc, "failing_since": str(webhook.failing_since)},
    )
    where = webhook.project.name if webhook.project else "your organization"
    path = (
        f"/projects/{webhook.project.slug}/webhooks/{webhook.pk}/"
        if webhook.project
        else f"/settings/webhooks/{webhook.pk}/"
    )
    notify(
        _managers(webhook),
        notification_kinds.WEBHOOK_DISABLED,
        title=(
            f"Iki turned off a webhook in {where}: deliveries to "
            f"{urlsplit(webhook.url).netloc} failed for {DISABLE_AFTER.days} days"
        ),
        path=path,
        project=webhook.project,
    )
    logger.warning("webhook_disabled", webhook=str(webhook.pk))
