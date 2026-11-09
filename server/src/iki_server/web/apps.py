from django.apps import AppConfig


class WebConfig(AppConfig):
    """Server-rendered pages: layout, styles, sign-in, errors and security headers."""

    name = "iki_server.web"
    label = "iki_web"
    verbose_name = "Iki web"
