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
            name="ListCurator",
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
                    "list_type",
                    models.CharField(
                        db_comment="The list's type, such as iki/lots.PurityMethod (ADR 0270).",
                        max_length=112,
                    ),
                ),
                ("appointed_at", models.DateTimeField(auto_now_add=True)),
                (
                    "appointed_by",
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
                        related_name="list_curators",
                        to="iki_org.registry",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="list_curator_of",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "iki_list_curator",
                "db_table_comment": "Who curates each of a registry's lab lists (ADR 0270).",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("registry", "list_type", "user"), name="iki_list_curator_once"
                    )
                ],
            },
        ),
    ]
