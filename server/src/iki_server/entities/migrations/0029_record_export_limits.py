from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_entities", "0028_record_exports"),
    ]

    operations = [
        migrations.AddField(
            model_name="recordexport",
            name="limits",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
