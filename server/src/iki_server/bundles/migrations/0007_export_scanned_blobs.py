"""Remember the immutable file sources checked when an export was written."""

from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_bundles", "0006_export_references")]

    operations = [
        migrations.AddField(
            model_name="bundleexport",
            name="scanned_blobs",
            field=ArrayField(models.UUIDField(), null=True),
        ),
    ]
