"""Keep the revision reviewed for a signed result delivered to its requester (ADR 0179)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_entities", "0020_changed")]
    operations = [
        migrations.AddField(
            model_name="entity",
            name="released_review_revision",
            field=models.PositiveIntegerField(null=True, blank=True),
        ),
        migrations.AddConstraint(
            model_name="entity",
            constraint=models.CheckConstraint(
                condition=models.Q(released_review_revision__isnull=True)
                | (
                    models.Q(revision=models.F("released_review_revision") + 2)
                    & ~models.Q(locked_by=[])
                ),
                name="iki_released_review_locked",
            ),
        ),
    ]
