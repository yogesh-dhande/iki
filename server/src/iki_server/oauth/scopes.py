"""OAuth scopes, and which permission-service actions each one allows (ADR 0028).

A token can do what both its scopes and its user's roles allow. Scopes only
narrow: a ``write`` token held by a Viewer still only reads.
"""

from typing import Any

from oauth2_provider.scopes import BaseScopes

SCOPES: dict[str, str] = {
    "read": "See your projects and teams, and read project data.",
    "write": "Create projects and change their data, settings and members.",
    "run": "Run actions, on every compute tier your project roles allow.",
    "apps": "Build and change apps and views.",
    "admin": "Administer the organization as you can: people, teams and service accounts.",
    "webhooks": "Manage webhooks, which send changes in Iki to other systems.",
}

#: What a client gets when it asks for no scope.
DEFAULT_SCOPES = ["read"]


def ordered(scopes: Any) -> list[str]:  # noqa: ANN401 - any iterable of names
    """Known scopes in display order."""
    wanted = set(scopes)
    return [scope for scope in SCOPES if scope in wanted]


class IkiScopes(BaseScopes):
    """Every client may ask for any scope, except service-account credentials,
    which are limited to the scopes chosen when they were created."""

    def get_all_scopes(self) -> dict[str, str]:
        return dict(SCOPES)

    def get_available_scopes(
        self,
        application: Any = None,  # noqa: ANN401
        request: Any = None,  # noqa: ANN401
        *args: object,
        **kwargs: object,
    ) -> list[str]:
        allowed = getattr(application, "allowed_scopes", "") if application else ""
        return ordered(allowed.split()) if allowed else list(SCOPES)

    def get_default_scopes(
        self,
        application: Any = None,  # noqa: ANN401
        request: Any = None,  # noqa: ANN401
        *args: object,
        **kwargs: object,
    ) -> list[str]:
        allowed = getattr(application, "allowed_scopes", "") if application else ""
        return ordered(allowed.split()) if allowed else list(DEFAULT_SCOPES)
