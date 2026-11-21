from typing import Any

from django.http import HttpRequest
from ninja import Router

from iki_server.api.common import caller
from iki_server.api.schemas import MeOut, agent_out
from iki_server.audit import log as audit
from iki_server.org import permissions

router = Router(tags=["me"])


def _delegation(request: HttpRequest) -> dict[str, Any] | None:
    consent = getattr(request, "iki_delegation", None)
    if consent is None:
        return None
    return {"service_account": consent.client.service_account, "expires_at": consent.expires_at}


@router.get("/me", response=MeOut, summary="Who you are")
def me(request: HttpRequest) -> dict[str, Any]:
    user = caller(request)
    return {
        "id": user.pk,
        "email": user.email,
        "name": user.name,
        "kind": user.kind,
        "is_active": user.is_active,
        "imported_from": user.imported_from,
        "agent": agent_out(audit.current_agent_info()),
        "delegation": _delegation(request),
        "org_roles": sorted(str(role) for role in permissions.org_roles(user)),
        "can": permissions.capabilities(user),
    }
