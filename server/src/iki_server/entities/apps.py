from django.apps import AppConfig


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
