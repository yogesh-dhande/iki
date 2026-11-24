import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_storage", "0005_receipt_observation_revisions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="reconciliation",
            name="cancel_reason",
            field=models.CharField(blank=True, db_default="", default="", max_length=2000),
        ),
        migrations.AddField(
            model_name="reconciliation",
            name="cancelled_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="reconciliationline",
            name="current_observation",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="iki_storage.observationevent",
            ),
        ),
        migrations.AddField(
            model_name="reconciliationline",
            name="line_revision",
            field=models.PositiveIntegerField(db_default=1, default=1),
        ),
        migrations.AddField(
            model_name="reconciliationline",
            name="resolved_finding",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name="reconciliationline",
            name="reviewed_observation",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="iki_storage.observationevent",
            ),
        ),
        migrations.AddConstraint(
            model_name="reconciliationline",
            constraint=models.CheckConstraint(
                condition=models.Q(line_revision__gt=0),
                name="iki_storage_count_line_positive_revision",
            ),
        ),
    ]
