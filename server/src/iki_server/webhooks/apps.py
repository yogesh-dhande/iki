from django.apps import AppConfig


class WebhooksConfig(AppConfig):
    name = "iki_server.webhooks"
    label = "iki_webhooks"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from iki_server.audit import log
        from iki_server.webhooks.events import from_audit

        if from_audit not in log.LISTENERS:
            log.LISTENERS.append(from_audit)
