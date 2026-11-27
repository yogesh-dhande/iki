from typing import Any

from django.apps import AppConfig
from django.db.models.signals import post_save


class EntitiesConfig(AppConfig):
    name = "iki_server.entities"
    label = "iki_entities"
    verbose_name = "Iki entities"

    def ready(self) -> None:
        from iki_server.audit import log
        from iki_server.entities import lookups
        from iki_server.entities.tasks import on_audit

        lookups.register()
        if on_audit not in log.LISTENERS:
            log.LISTENERS.append(on_audit)
        post_save.connect(
            _seed_new_registry, sender="iki_org.Registry", dispatch_uid="iki_lists_seed"
        )


def _seed_new_registry(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:  # noqa: ANN401 - Django's signal
    """A new registry starts with each lab list's starting options (ADR 0270), as the
    registries there were at the last deploy got them."""
    if created and not kwargs.get("raw"):
        from iki_server.entities import lists

        lists.seed([instance])
