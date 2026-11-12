"""Seeds, reuse and Reproduce exactly (ADR 0057): what each run was given, which run it
reused, and which it reproduces."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_org", "0001_initial"),
        ("iki_runs", "0014_run_effect_order"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="actionrun",
            name="reproduces",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="reproductions",
                to="iki_runs.actionrun",
            ),
        ),
        migrations.AddField(
            model_name="actionrun",
            name="reproduction",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="actionrun",
            name="reuse_key",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="actionrun",
            name="reused_from",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="iki_runs.actionrun",
            ),
        ),
        migrations.AddField(
            model_name="actionrun",
            name="seed",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="actionrun",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("reused_from__isnull", True), ("reproduces__isnull", True), _connector="OR"
                ),
                name="iki_action_run_reused_or_reproduces",
            ),
        ),
    ]
