"""Keep the evidence of a run append-only for every database role (ADR 0025)."""

from django.db import migrations

from iki_server.plugins.operations import AppendOnly


class Migration(migrations.Migration):
    dependencies = [("iki_runs", "0053_actionrun_reference_mask")]

    operations = [
        AppendOnly("RunInput"),
        AppendOnly("RunEffect"),
        AppendOnly("Lineage"),
        AppendOnly("EntityMaking"),
        AppendOnly("JobLog"),
        AppendOnly("ApiCall"),
    ]
