from django.http import HttpRequest

from iki_server.org import permissions
from iki_server.org.permissions import Action


def navigation(request: HttpRequest) -> dict[str, object]:
    """Which top-level sections the signed-in person can open."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {
        "nav_people": permissions.can(user, Action.ORG_MANAGE),
        "nav_settings": permissions.can(user, Action.ORG_MANAGE),
        "nav_audit": permissions.can(user, Action.AUDIT_READ),
    }
