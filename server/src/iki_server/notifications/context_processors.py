from django.http import HttpRequest

from iki_server.notifications import services


def unread(request: HttpRequest) -> dict[str, object]:
    """The unread count for the header's bell (server-rendered pages)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or user.is_service_account:
        return {}
    return {"notifications_unread": services.unread_count(user)}
