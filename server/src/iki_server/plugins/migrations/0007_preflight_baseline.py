from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_plugins", "0006_history_changes"),
    ]

    operations = [
        migrations.CreateModel(
            name="PluginBaseline",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("revision", models.CharField(max_length=64)),
                ("manifests", models.JSONField()),
            ],
            options={
                "db_table": "iki_plugin_baseline",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("id", 1)), name="iki_plugin_baseline_singleton"
                    )
                ],
            },
        ),
    ]
