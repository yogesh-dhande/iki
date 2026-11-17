from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "iki_server.core"
    label = "iki_core"
    verbose_name = "Iki core"

    def ready(self) -> None:
        from iki_server.core import converters

        converters.register()
