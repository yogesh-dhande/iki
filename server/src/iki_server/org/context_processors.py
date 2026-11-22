from collections.abc import Callable

from django.http import HttpRequest

from iki_server.notifications import services as notifications
from iki_server.web.navigation import AccountMenu, Sidebar, account_menu, sidebar


def navigation(request: HttpRequest) -> dict[str, Callable[[], Sidebar | AccountMenu]]:
    """The sidebar for the person signed in, and the admin area's account menu, the same as
    the sidebar's. Worked out only by a page that shows them: templates call ``sidebar``
    and ``account_menu``."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or user.is_service_account:
        return {}

    def build() -> Sidebar:
        return sidebar(user, request.path, request.COOKIES, unread=notifications.unread_count(user))

    def account() -> AccountMenu:
        return account_menu(user, request.path)

    return {"sidebar": build, "account_menu": account}
