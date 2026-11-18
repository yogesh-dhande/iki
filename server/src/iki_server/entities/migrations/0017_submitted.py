from django.db import migrations, models


class Migration(migrations.Migration):
    """A record's first revision keeps its identity's values as given, where they're
    spelled other than its registry entry's (ADR 0138)."""

    dependencies = [("iki_entities", "0016_reference_types")]

    operations = [
        migrations.AddField(
            model_name="revision",
            name="submitted",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
