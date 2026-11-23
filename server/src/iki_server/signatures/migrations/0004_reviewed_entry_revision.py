from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_signatures", "0003_history_seen"),
    ]

    operations = [
        migrations.AddField(
            model_name="signing",
            name="entry_revision_reviewed",
            field=models.BooleanField(default=False, db_default=False),
        ),
    ]
