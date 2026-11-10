"""Recording audit events.

Call :func:`record` in the same transaction as the change it describes, so
the event exists exactly when the change does. Once the transaction
commits, the event is also written to the ``iki.audit`` log stream for a
SIEM.

Where a change came from (web, command line, ...), the client address and
the request ID are taken from the current context, which
:class:`AuditContextMiddleware` sets for web requests and :func:`cli` for
management commands.
"""

import contextlib
import dataclasses
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import structlog
from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.db import models, transaction
from django.http import HttpRequest, HttpResponseBase

from iki_server.audit.models import AuditEvent, Via
from iki_server.org.models import Project, Team

logger = structlog.get_logger("iki.audit")

#: Every action Iki records, with how the audit page describes it.
ACTIONS: dict[str, str] = {
    "setup.link_created": "Created a setup link",
    "setup.completed": "Finished setup",
    "identity_provider.enabled": "Turned on an identity provider",
    "user.signed_in": "Signed in",
    "user.signed_out": "Signed out",
    "user.sign_in_refused": "Sign-in refused",
    "user.created": "Account created on first sign-in",
    "user.provisioned": "Provisioned a person",
    "user.updated": "Changed a person’s profile",
    "user.deprovisioned": "Deprovisioned a person",
    "user.identity_linked": "Linked a first sign-in to a provisioned account",
    "user.deactivated": "Deactivated a person",
    "user.activated": "Reactivated a person",
    "user.sessions_revoked": "Ended someone's sessions",
    "service_account.created": "Created a service account",
    "service_account.updated": "Changed a service account",
    "org_role.granted": "Granted an org role",
    "org_role.revoked": "Revoked an org role",
    "team.created": "Created a team",
    "team.updated": "Changed a team",
    "team.archived": "Archived a team",
    "team.restored": "Restored a team",
    "team.member_added": "Added a team member",
    "team.member_removed": "Removed a team member",
    "project.created": "Created a project",
    "project.updated": "Changed a project",
    "project.archived": "Archived a project",
    "project.restored": "Restored a project",
    "project.role_granted": "Granted a project role",
    "project.member_removed": "Removed a project member",
    "entity.linked": "Shared an entity into a project",
    "entity.unlinked": "Took an entity out of a project",
    "encryption.rotated": "Re-encrypted stored secrets",
    "oauth.client_registered": "An app registered itself",
    "oauth.client_approval_requested": "Asked an Org Admin to approve an app",
    "notifications.email_preferences_changed": "Changed which notifications they get by email",
    "notifications.email_details_changed": "Changed what notification emails say",
    "oauth.client_approved": "Approved an app",
    "oauth.client_blocked": "Blocked an app",
    "oauth.approval_policy_changed": "Changed whether apps need approval",
    "oauth.consent_granted": "Allowed an app to act for them",
    "oauth.consent_revoked": "Revoked an app’s access",
    "oauth.device_approved": "Signed in a device",
    "oauth.credential_created": "Created service-account credentials",
    "oauth.credential_revoked": "Revoked service-account credentials",
    "webhook.created": "Added a webhook",
    "webhook.updated": "Changed a webhook",
    "webhook.deleted": "Deleted a webhook",
    "webhook.secret_rotated": "Rotated a webhook’s secret",
    "webhook.disabled": "Iki turned off a failing webhook",
    "webhook.redelivered": "Sent a webhook delivery again",
    "webhooks.project_policy_changed": "Changed whether project Admins may add webhooks",
    "scim.token_created": "Created a SCIM token",
    "scim.token_revoked": "Revoked a SCIM token",
    "scim.group_created": "Identity provider pushed a group",
    "scim.group_updated": "Identity provider changed a group",
    "scim.group_deleted": "Identity provider removed a group",
    "scim.group_linked": "Linked a group to a team",
    "scim.group_unlinked": "Unlinked a group from a team",
}


@dataclass(frozen=True)
class Context:
    via: str
    client_ip: str | None = None
    #: Names who acted when no person did, e.g. which SCIM token.
    actor_label: str = ""
    #: The OAuth client the request came through, if any.
    oauth_client: str = ""


_SYSTEM = Context(via=Via.SYSTEM)
_context: ContextVar[Context] = ContextVar("iki_audit_context")


@contextlib.contextmanager
def using(context: Context) -> Iterator[None]:
    token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(token)


def current_via() -> str:
    """How the current change is being made: web, api, mcp, cli or system."""
    return _context.get(_SYSTEM).via


def note_oauth_client(label: str) -> None:
    """Marks the rest of this request as made through an OAuth client."""
    _context.set(dataclasses.replace(_context.get(_SYSTEM), oauth_client=label[:254]))


def cli() -> contextlib.AbstractContextManager[None]:
    """Marks changes made by a management command."""
    return using(Context(via=Via.CLI))


class _Unset:
    pass


UNSET: Any = _Unset()


def _label(target: models.Model) -> str:
    return str(target)[:254]


def record(
    action: str,
    *,
    actor: Any = UNSET,  # noqa: ANN401 - a User, None for the system, or UNSET
    target: models.Model | None = None,
    project: Project | None = None,
    team: Team | None = None,
    data: dict[str, Any] | None = None,
) -> AuditEvent:
    """Records an audit event. ``actor`` defaults to nobody (the system)."""
    if action not in ACTIONS:
        raise ValueError(f"unknown audit action {action!r}; add it to ACTIONS")
    context = _context.get(_SYSTEM)
    user = None if actor is UNSET else actor
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    event = AuditEvent.objects.create(
        action=action,
        actor=user,
        actor_label=getattr(user, "email", "") if user is not None else context.actor_label,
        via=context.via,
        target_type=(target._meta.model_name or "") if target is not None else "",
        target_id=str(target.pk) if target is not None else "",
        target_label=_label(target) if target is not None else "",
        project=project,
        team=team,
        data=data or {},
        request_id=str(structlog.contextvars.get_contextvars().get("request_id", "")),
        client_ip=context.client_ip,
        oauth_client=context.oauth_client,
    )
    for listener in LISTENERS:
        listener(event, target)
    transaction.on_commit(lambda: _emit(event))
    return event


#: Called with each event and its target as it's recorded, in the change's
#: transaction: what follows from a change (webhooks) exists exactly when it does.
LISTENERS: list[Callable[[AuditEvent, models.Model | None], None]] = []


def _emit(event: AuditEvent) -> None:
    logger.info(
        event.action,
        audit_id=str(event.id),
        actor_id=str(event.actor_id) if event.actor_id else None,
        actor=event.actor_label or None,
        via=event.via,
        target_type=event.target_type or None,
        target_id=event.target_id or None,
        target=event.target_label or None,
        project_id=str(event.project_id) if event.project_id else None,
        team_id=str(event.team_id) if event.team_id else None,
        data=event.data or None,
        client_ip=event.client_ip,
        oauth_client=event.oauth_client or None,
    )


GetResponse = Callable[[HttpRequest], Awaitable[HttpResponseBase]]


class AuditContextMiddleware:
    """Marks changes made during a web request with their channel and client address."""

    async_capable = True
    sync_capable = False

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response
        if iscoroutinefunction(get_response):
            markcoroutinefunction(self)

    async def __call__(self, request: HttpRequest) -> HttpResponseBase:
        via = Via.API if request.path.startswith("/api/") else Via.WEB
        with using(Context(via=via, client_ip=request.META.get("REMOTE_ADDR") or None)):
            return await self.get_response(request)
