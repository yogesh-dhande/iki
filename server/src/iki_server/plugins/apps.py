from typing import Any

from django.apps import AppConfig
from django.db import connections
from django.db.models.signals import pre_migrate


class PluginsConfig(AppConfig):
    name = "iki_server.plugins"
    label = "iki_plugins"
    verbose_name = "Iki plugins"

    def ready(self) -> None:
        # Once per `migrate`: sent for each app, but only this one is the sender.
        pre_migrate.connect(_drop_views, sender=self, dispatch_uid="iki_plugins.drop_views")


def _drop_views(using: str = "default", **kwargs: Any) -> None:  # noqa: ANN401 - Django's signal
    """Analysts' views go before migrations change the tables under them, and come back with
    ``sync_plugins``, which the migrate Job runs next (ADR 0033)."""
    from iki_server.plugins import sync

    with connections[using].cursor() as cursor:
        sync.drop_views(cursor)
