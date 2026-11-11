"""Webhooks: a project's, the organization's, and their deliveries (ADR 0031).

Managing webhooks needs the ``webhooks`` scope: they send Iki's data to
other systems, so a token doesn't get that power with ``write``.
"""

from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from django.db.models import OuterRef, QuerySet, Subquery
from django.http import HttpRequest
from django.utils import timezone
from ninja import Field, Query, Router, Schema, Status

from iki_server.api.common import caller, project, require, require_scope
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page, PersonRef, ProjectRef
from iki_server.org.permissions import Action
from iki_server.webhooks import events, services
from iki_server.webhooks.models import Delivery, Webhook

router = Router(tags=["webhooks"])

Scope = Literal["project", "org"]


class EventTypeOut(Schema):
    type: str
    description: str


class LastDelivery(Schema):
    status: str
    event: str
    created_at: datetime


class WebhookOut(Schema):
    id: UUID
    scope: Scope
    project: ProjectRef | None
    url: str
    description: str
    events: list[str]
    active: bool
    disabled_reason: str = Field(
        description="Why it's off: failing (Iki turned it off), org_admin, or empty."
    )
    failing_since: datetime | None = Field(
        description="When deliveries started failing with no success since."
    )
    rotation_ends_at: datetime | None = Field(
        description="During a secret rotation: when the previous secret stops signing."
    )
    created_by: PersonRef | None
    created_at: datetime
    last_delivery: LastDelivery | None

    @staticmethod
    def resolve_rotation_ends_at(webhook: Webhook) -> datetime | None:
        expires = webhook.previous_secret_expires_at
        return expires if webhook.previous_secret and expires and expires > timezone.now() else None

    @staticmethod
    def resolve_last_delivery(webhook: Webhook) -> dict[str, Any] | None:
        status = getattr(webhook, "last_status", None)
        if status is None:
            return None
        return {
            "status": status,
            "event": webhook.last_event,  # type: ignore[attr-defined]
            "created_at": webhook.last_at,  # type: ignore[attr-defined]
        }


class WebhookCreated(WebhookOut):
    secret: str = Field(
        description=(
            "The signing secret (Standard Webhooks, whsec_…). Shown only now: store it in "
            "your receiver."
        )
    )

    @staticmethod
    def resolve_secret(webhook: Webhook) -> str:
        return str(webhook.new_secret)  # type: ignore[attr-defined]


class WebhookIn(Schema):
    url: str = Field(max_length=2000, description="An https:// address.")
    description: str = Field("", max_length=300)
    events: list[str] = Field(min_length=1, description="Event types to send; see /webhook-events.")


class WebhookPatch(Schema):
    url: str | None = Field(None, max_length=2000)
    description: str | None = Field(None, max_length=300)
    events: list[str] | None = None
    active: bool | None = Field(
        None, description="false turns it off. Org Admins may turn off any project's webhook."
    )


class SecretOut(Schema):
    secret: str
    previous_secret_expires_at: datetime = Field(
        description="Until then, messages are signed with both secrets."
    )


class InventoryOut(Schema):
    """A project webhook as Org Admins see it: where it sends, not what."""

    id: UUID
    project: ProjectRef
    host: str
    events: list[str]
    active: bool
    disabled_reason: str
    created_by: PersonRef | None
    created_at: datetime

    @staticmethod
    def resolve_host(webhook: Webhook) -> str:
        return urlsplit(webhook.url).netloc


class AttemptOut(Schema):
    at: datetime
    status_code: int | None
    error: str
    duration_ms: int


class DeliverySummary(Schema):
    id: UUID
    message_id: str = Field(description="The webhook-id header: the same when sent again.")
    event: str
    status: Literal["pending", "succeeded", "failed"]
    attempts: list[AttemptOut]
    redelivered_by: PersonRef | None
    created_at: datetime
    finished_at: datetime | None


class DeliveryOut(DeliverySummary):
    payload: dict[str, Any] = Field(description="The body sent, exactly.")


class DeliveryFilters(PageParams):
    status: Literal["pending", "succeeded", "failed"] | None = None


class PolicyOut(Schema):
    project_webhooks: bool = Field(description="Project Admins may add webhooks to their projects.")


class PolicyIn(Schema):
    project_webhooks: bool


# --- Lookups -----------------------------------------------------------------------


def _with_last(webhooks: QuerySet[Webhook]) -> QuerySet[Webhook]:
    latest = Delivery.objects.filter(webhook=OuterRef("pk")).order_by("-id")
    return webhooks.select_related("project", "created_by").annotate(
        last_status=Subquery(latest.values("status")[:1]),
        last_event=Subquery(latest.values("event")[:1]),
        last_at=Subquery(latest.values("created_at")[:1]),
    )


def _webhook(request: HttpRequest, webhook_id: UUID, *, governing: bool = False) -> Webhook:
    """The webhook, if the caller manages it (or, with ``governing``, may turn it off)."""
    me = caller(request)
    # Before looking: a token without the scope learns nothing about which ids exist.
    require_scope(me, "webhooks")
    found = _with_last(Webhook.objects.filter(pk=webhook_id)).first()
    allowed = found is not None and (
        services.can_manage(me, found) or (governing and services.can_govern(me))
    )
    if found is None or not allowed:
        raise Problem(404, "No such webhook, or you can’t manage it.")
    return found


def _delivery(webhook: Webhook, delivery_id: UUID) -> Delivery:
    found = (
        Delivery.objects.select_related("redelivered_by").filter(pk=delivery_id, webhook=webhook)
    ).first()
    if found is None:
        raise Problem(404, "No such delivery.")
    return found


# --- Events and policy -------------------------------------------------------------


@router.get(
    "/webhook-events", response=list[EventTypeOut], summary="The events webhooks can receive"
)
def webhook_events(request: HttpRequest, scope: Scope = "project") -> list[dict[str, str]]:
    caller(request)
    return [
        {"type": e.key, "description": e.description}
        for e in events.for_scope(scope)
        if e.key != events.PING
    ]


@router.get("/webhook-policy", response=PolicyOut, summary="Who may add webhooks")
def webhook_policy(request: HttpRequest) -> dict[str, bool]:
    caller(request)
    return {"project_webhooks": services.project_webhooks_allowed()}


@router.patch("/webhook-policy", response=PolicyOut, summary="Choose who may add webhooks")
def change_webhook_policy(request: HttpRequest, payload: PolicyIn) -> dict[str, bool]:
    """Off: only Org Admins add webhooks, and project webhooks stop sending."""
    me = caller(request)
    require(me, Action.ORG_WEBHOOKS_MANAGE)
    services.set_project_webhooks_allowed(payload.project_webhooks, by=me)
    return {"project_webhooks": services.project_webhooks_allowed()}


# --- Listing and creating ----------------------------------------------------------


@router.get(
    "/projects/{project_id}/webhooks", response=Page[WebhookOut], summary="A project's webhooks"
)
def project_webhooks(
    request: HttpRequest, project_id: UUID, params: Query[PageParams]
) -> dict[str, Any]:
    found = project(caller(request), project_id, Action.PROJECT_WEBHOOKS_MANAGE)
    return page(_with_last(Webhook.objects.filter(project=found)), params)


@router.post(
    "/projects/{project_id}/webhooks",
    response={201: WebhookCreated},
    summary="Add a webhook to a project",
)
def create_project_webhook(
    request: HttpRequest, project_id: UUID, payload: WebhookIn
) -> Status[Webhook]:
    """Iki sends it a ``ping`` at once. The response holds the secret, shown only now."""
    me = caller(request)
    found = project(me, project_id, Action.PROJECT_WEBHOOKS_MANAGE)
    return _created(me, found, payload)


@router.get("/webhooks", response=Page[WebhookOut], summary="The organization's webhooks")
def org_webhooks(request: HttpRequest, params: Query[PageParams]) -> dict[str, Any]:
    require(caller(request), Action.ORG_WEBHOOKS_MANAGE)
    return page(_with_last(Webhook.objects.filter(project__isnull=True)), params)


@router.post("/webhooks", response={201: WebhookCreated}, summary="Add an organization webhook")
def create_org_webhook(request: HttpRequest, payload: WebhookIn) -> Status[Webhook]:
    me = caller(request)
    require(me, Action.ORG_WEBHOOKS_MANAGE)
    return _created(me, None, payload)


def _created(me: Any, found: Any, payload: WebhookIn) -> Status[Webhook]:  # noqa: ANN401
    webhook, secret = services.create(
        project=found,
        url=payload.url.strip(),
        description=payload.description,
        chosen=payload.events,
        by=me,
    )
    shown = _with_last(Webhook.objects.filter(pk=webhook.pk)).get()
    shown.new_secret = secret  # type: ignore[attr-defined]
    return Status(201, shown)


@router.get(
    "/project-webhooks",
    response=Page[InventoryOut],
    summary="Every project's webhooks, for Org Admins",
)
def inventory(request: HttpRequest, params: Query[PageParams]) -> dict[str, Any]:
    """Where each project's webhooks send (the host), so Org Admins can see where
    data goes and turn any webhook off. Not their addresses or deliveries."""
    require(caller(request), Action.ORG_WEBHOOKS_MANAGE)
    found = Webhook.objects.filter(project__isnull=False).select_related("project", "created_by")
    return page(found, params)


# --- One webhook -----------------------------------------------------------------


@router.get("/webhooks/{webhook_id}", response=WebhookOut, summary="A webhook")
def get_webhook(request: HttpRequest, webhook_id: UUID) -> Webhook:
    return _webhook(request, webhook_id)


@router.patch("/webhooks/{webhook_id}", response=WebhookOut, summary="Change or turn off a webhook")
def patch_webhook(request: HttpRequest, webhook_id: UUID, payload: WebhookPatch) -> Webhook:
    found = _webhook(request, webhook_id, governing=payload.active is not None)
    services.update(
        found,
        by=caller(request),
        url=payload.url.strip() if payload.url is not None else None,
        description=payload.description,
        chosen=payload.events,
        active=payload.active,
    )
    shown = _with_last(Webhook.objects.filter(pk=found.pk)).get()
    if not services.can_manage(caller(request), shown):
        # An Org Admin governing a project's webhook sees only where it sends, as in the
        # inventory: the path and query may carry the receiver's token.
        shown.url = services.display_url(shown.url)
    return shown


@router.delete("/webhooks/{webhook_id}", response={204: None}, summary="Delete a webhook")
def delete_webhook(request: HttpRequest, webhook_id: UUID) -> Status[None]:
    """Its delivery log goes with it."""
    services.delete(_webhook(request, webhook_id), by=caller(request))
    return Status(204, None)


@router.post(
    "/webhooks/{webhook_id}/rotate-secret", response=SecretOut, summary="Rotate the secret"
)
def rotate_secret(request: HttpRequest, webhook_id: UUID) -> dict[str, Any]:
    """A new secret, shown only now. For a day, messages carry signatures from both, so
    you can update your receiver without missing any."""
    secret, expires = services.rotate_secret(_webhook(request, webhook_id), by=caller(request))
    return {"secret": secret, "previous_secret_expires_at": expires}


@router.post(
    "/webhooks/{webhook_id}/ping", response={202: DeliveryOut}, summary="Send a test message"
)
def ping(request: HttpRequest, webhook_id: UUID) -> Status[Delivery]:
    found = _webhook(request, webhook_id)
    if not found.active:
        raise Problem(409, "Turn the webhook on first.", code="refused")
    return Status(202, services.ping(found, by=caller(request)))


# --- Deliveries ----------------------------------------------------------------------


@router.get(
    "/webhooks/{webhook_id}/deliveries",
    response=Page[DeliverySummary],
    summary="What was sent, newest first",
)
def deliveries(
    request: HttpRequest, webhook_id: UUID, filters: Query[DeliveryFilters]
) -> dict[str, Any]:
    """Kept 30 days."""
    found = _webhook(request, webhook_id)
    sent = Delivery.objects.filter(webhook=found).select_related("redelivered_by")
    if filters.status:
        sent = sent.filter(status=filters.status)
    return page(sent, filters, newest_first=True)


@router.get(
    "/webhooks/{webhook_id}/deliveries/{delivery_id}",
    response=DeliveryOut,
    summary="One delivery, with the body sent",
)
def get_delivery(request: HttpRequest, webhook_id: UUID, delivery_id: UUID) -> Delivery:
    return _delivery(_webhook(request, webhook_id), delivery_id)


@router.post(
    "/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver",
    response={202: DeliveryOut},
    summary="Send a delivery again",
)
def redeliver(request: HttpRequest, webhook_id: UUID, delivery_id: UUID) -> Status[Delivery]:
    """The same message (same webhook-id and body), signed afresh."""
    found = _webhook(request, webhook_id)
    return Status(202, services.redeliver(_delivery(found, delivery_id), by=caller(request)))
