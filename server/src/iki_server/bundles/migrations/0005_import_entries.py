"""Registries (ADR 0115): an import's plan notes, for a record of a registry entry, the
entry here, or the ID it registers it with."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_bundles", "0004_hrid_advance"),
    ]

    operations = [
        migrations.AddField(
            model_name="importentity",
            name="entry_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="importentity",
            name="registers",
            field=models.BooleanField(default=False),
        ),
    ]
