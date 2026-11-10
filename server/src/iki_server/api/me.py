from typing import Any

from django.http import HttpRequest
from ninja import Router

from iki_server.api.common import caller
from iki_server.api.schemas import MeOut
from iki_server.org import permissions

router = Router(tags=["me"])


@router.get("/me", response=MeOut, summary="Who you are")
def me(request: HttpRequest) -> dict[str, Any]:
    user = caller(request)
    return {
        "id": user.pk,
        "email": user.email,
        "name": user.name,
        "kind": user.kind,
        "is_active": user.is_active,
        "org_roles": sorted(str(role) for role in permissions.org_roles(user)),
    }
