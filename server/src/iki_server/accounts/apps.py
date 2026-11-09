from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "iki_server.accounts"
    label = "iki_accounts"
    verbose_name = "Iki accounts"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from iki_server.accounts import signals

        signals.connect()
