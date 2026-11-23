from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_storage", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="reconciliationline",
            name="resolved_position",
            field=models.CharField(blank=True, max_length=16, null=True),
        )
    ]
