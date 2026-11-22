from collections.abc import Callable

from django.http import HttpRequest

from iki_server.notifications import services as notifications
from iki_server.web.navigation import Sidebar, sidebar


def navigation(request: HttpRequest) -> dict[str, Callable[[], Sidebar]]:
    """The sidebar for the person signed in. Worked out only by a page that shows it:
    templates call ``sidebar``."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or user.is_service_account:
        return {}

    def build() -> Sidebar:
        return sidebar(user, request.path, request.COOKIES, unread=notifications.unread_count(user))

    return {"sidebar": build}
