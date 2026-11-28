import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import iki_server.core.ids


class Migration(migrations.Migration):
    dependencies = [
        ("iki_org", "0007_quarantine_admin"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RegistryRole",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=iki_server.core.ids.uuid7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[("registrar", "Registrar"), ("list_curator", "List curator")],
                        db_comment="registrar or list_curator (ADR 0292).",
                        max_length=20,
                    ),
                ),
                (
                    "list_type",
                    models.CharField(
                        blank=True,
                        db_comment="A list curator's list, such as iki/lots.PurityMethod; null for a role over the whole registry (ADR 0292).",
                        max_length=112,
                        null=True,
                    ),
                ),
                ("granted_at", models.DateTimeField(auto_now_add=True)),
                (
                    "granted_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "registry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="roles",
                        to="iki_org.registry",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="registry_roles",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "iki_registry_role",
                "db_table_comment": "Who holds each registry's roles: its registrars, and its lab lists' curators (ADR 0292).",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("list_type__isnull", True), ("role", "registrar")),
                            models.Q(
                                ("list_type__isnull", False),
                                ("role", "list_curator"),
                                models.Q(("list_type", ""), _negated=True),
                            ),
                            _connector="OR",
                        ),
                        name="iki_registry_role_scope",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("list_type__isnull", True)),
                        fields=("registry", "user", "role"),
                        name="iki_registry_role_once",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("list_type__isnull", False)),
                        fields=("registry", "user", "role", "list_type"),
                        name="iki_registry_role_once_per_list",
                    ),
                ],
            },
        ),
    ]
