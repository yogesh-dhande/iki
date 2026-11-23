from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_spreadsheets", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="spreadsheetimport",
            name="mapping_revision",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="savedmapping",
            name="revision",
            field=models.PositiveIntegerField(default=1),
        ),
    ]
