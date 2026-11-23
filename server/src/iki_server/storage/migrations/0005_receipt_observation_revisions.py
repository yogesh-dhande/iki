import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Give existing receipts and lines a starting revision without inventing their history."""

    dependencies = [
        ("iki_storage", "0004_observation_event"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="receipt",
            name="plan_revision",
            field=models.PositiveIntegerField(db_default=1, default=1),
        ),
        migrations.AddField(
            model_name="receipt",
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
            model_name="receipt",
            name="cancel_reason",
            field=models.TextField(blank=True, db_default="", default=""),
        ),
        migrations.AddField(
            model_name="receiptline",
            name="line_revision",
            field=models.PositiveIntegerField(db_default=1, default=1),
        ),
        migrations.AddField(
            model_name="receiptline",
            name="removed",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddConstraint(
            model_name="receipt",
            constraint=models.CheckConstraint(
                condition=models.Q(plan_revision__gt=0),
                name="iki_storage_receipt_positive_revision",
            ),
        ),
        migrations.AddConstraint(
            model_name="receiptline",
            constraint=models.CheckConstraint(
                condition=models.Q(line_revision__gt=0), name="iki_receipt_line_positive_revision"
            ),
        ),
        migrations.AddConstraint(
            model_name="receiptline",
            constraint=models.CheckConstraint(
                condition=models.Q(removed=False) | models.Q(expected=False),
                name="iki_receipt_only_extra_removed",
            ),
        ),
    ]
