from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_spreadsheets", "0003_import_revision")]

    operations = [
        migrations.RemoveField(model_name="spreadsheetimport", name="mapping"),
        migrations.AddField(
            model_name="savedmapping",
            name="revision",
            field=models.PositiveIntegerField(db_default=1, default=1),
        ),
        migrations.AddConstraint(
            model_name="savedmapping",
            constraint=models.CheckConstraint(
                condition=models.Q(revision__gt=0), name="iki_spreadsheet_mapping_rev"
            ),
        ),
    ]
