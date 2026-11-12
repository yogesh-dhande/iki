"""Each consent names its grant (ADR 0072): added empty, filled one by one, then made
unique, as a unique field with a generated default must be."""

from typing import Any

from django.db import migrations, models

import iki_server.core.ids


def fill(apps: Any, schema_editor: Any) -> None:  # noqa: ANN401
    Consent = apps.get_model("iki_oauth", "Consent")
    for consent in Consent.objects.filter(grant=None).only("pk"):
        Consent.objects.filter(pk=consent.pk).update(grant=iki_server.core.ids.uuid7())


class Migration(migrations.Migration):
    dependencies = [("iki_oauth", "0003_iki_worker")]

    operations = [
        migrations.AddField(
            model_name="consent",
            name="grant",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(fill, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="consent",
            name="grant",
            field=models.UUIDField(default=iki_server.core.ids.uuid7, editable=False, unique=True),
        ),
    ]
