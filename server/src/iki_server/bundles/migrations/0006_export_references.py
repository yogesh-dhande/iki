"""Remember the record identities a retained archive names at the time it is written."""

from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_bundles", "0005_import_entries")]

    operations = [
        migrations.AddField(
            model_name="bundleexport",
            name="references",
            field=ArrayField(models.UUIDField(), null=True),
        ),
    ]
