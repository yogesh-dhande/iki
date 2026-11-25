from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_audit", "0005_via_trigger"),
        ("iki_entities", "0026_revision_history_seen"),
        ("iki_org", "0006_legal_hold_release_reason"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="revision",
            name="iki_revision_kind",
        ),
        migrations.AlterField(
            model_name="revision",
            name="kind",
            field=models.CharField(
                choices=[
                    ("created", "Created"),
                    ("edited", "Edited"),
                    ("migrated", "Migrated"),
                    ("archived", "Archived"),
                    ("restored", "Restored"),
                    ("state_changed", "State changed"),
                    ("curated", "Curated"),
                    ("superseded", "Superseded"),
                    ("moved", "Moved"),
                ],
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="revision",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "kind__in",
                        [
                            "created",
                            "edited",
                            "migrated",
                            "archived",
                            "restored",
                            "state_changed",
                            "curated",
                            "superseded",
                            "moved",
                        ],
                    )
                ),
                name="iki_revision_kind",
            ),
        ),
        migrations.AddConstraint(
            model_name="revision",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("kind", "migrated"), _negated=True),
                    models.Q(("reason__gt", ""), ("user__isnull", True), ("via", "system")),
                    _connector="OR",
                ),
                name="iki_revision_migration_reason",
            ),
        ),
    ]
