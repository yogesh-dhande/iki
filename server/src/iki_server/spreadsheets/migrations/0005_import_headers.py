from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_spreadsheets", "0004_saved_mapping_revision")]

    operations = [
        migrations.AddField(
            model_name="spreadsheetimport",
            name="headers",
            field=models.JSONField(default=list),
        ),
    ]
