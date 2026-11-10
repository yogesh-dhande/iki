from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = "iki_server.audit"
    label = "iki_audit"
    verbose_name = "Iki audit log"
