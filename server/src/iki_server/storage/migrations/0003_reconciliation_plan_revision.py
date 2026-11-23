from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_storage", "0002_reconciliationline_resolved_position")]

    operations = [
        migrations.AddField(
            model_name="reconciliation",
            name="plan_revision",
            field=models.PositiveIntegerField(db_default=1, default=1),
        ),
        migrations.AddConstraint(
            model_name="reconciliation",
            constraint=models.CheckConstraint(
                condition=models.Q(plan_revision__gt=0),
                name="iki_storage_reconciliation_positive_revision",
            ),
        ),
    ]
