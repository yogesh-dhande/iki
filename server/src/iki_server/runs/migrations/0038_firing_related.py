"""A trigger can give its action's inputs the entities related to the one that fired it
(ADR 0123): each firing says how many each input got."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_runs", "0037_registries"),
    ]

    operations = [
        migrations.AddField(
            model_name="triggerfiring",
            name="related",
            field=models.JSONField(
                blank=True,
                db_comment="How many related entities each input was given, by input (ADR 0123).",
                null=True,
            ),
        ),
    ]
