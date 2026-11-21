from collections.abc import Callable

from django.http import HttpRequest

from iki_server.web.navigation import Link, main_navigation


def navigation(request: HttpRequest) -> dict[str, Callable[[], list[Link]]]:
    """The header's main navigation for the signed-in person. Worked out only by a page
    that shows it: templates call ``nav_items``."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {"nav_items": lambda: main_navigation(user, request.path)}
