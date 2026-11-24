from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_spreadsheets", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="spreadsheetimport",
            name="mapping_source",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="spreadsheetimport",
            name="applied_mapping",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
