"""A run's effect on a record of a registry entry keeps the entry's revisions it made too,
so the entry's values name the run that registered or filled them in (ADR 0115)."""

import django.contrib.postgres.fields
import django.db.models.deletion
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("iki_entities", "0018_hrid_sorted"),
        ("iki_runs", "0043_several_origins_a_run"),
    ]

    operations = [
        migrations.AddField(
            model_name="runeffect",
            name="entry",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="iki_entities.entity",
            ),
        ),
        migrations.AddField(
            model_name="runeffect",
            name="entry_revisions",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.PositiveIntegerField(),
                blank=True,
                db_default=[],
                default=list,
                size=None,
            ),
        ),
        migrations.AddConstraint(
            model_name="runeffect",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("entry_revisions", []), ("entry__isnull", False), _connector="OR"
                ),
                name="iki_run_effect_entry_of",
            ),
        ),
        AddIndexConcurrently(
            model_name="runeffect",
            index=models.Index(
                condition=models.Q(("entry__isnull", False)),
                fields=["entry"],
                name="iki_run_effect_entry",
            ),
        ),
    ]
