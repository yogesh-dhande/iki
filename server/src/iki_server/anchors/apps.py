from django.apps import AppConfig


class AnchorsConfig(AppConfig):
    """One migration per minor release, after every one of Iki's migrations in it.

    Plugin migrations depend on these, never on Iki's other migrations, which a later Iki
    may squash or rename. They change nothing in the database (ADR 0296).
    """

    name = "iki_server.anchors"
    label = "iki_anchors"
    verbose_name = "Iki release anchors"
