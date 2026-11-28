from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_entities", "0029_entry_hands_its_identity_on"),
    ]

    operations = [
        migrations.AddField(
            model_name="recordexport",
            name="limits",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
