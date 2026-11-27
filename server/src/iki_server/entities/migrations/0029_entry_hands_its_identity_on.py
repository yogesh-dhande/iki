from django.db import migrations, models


class Migration(migrations.Migration):
    """An entry superseded by one of its identity as another type (ADR 0165) hands its
    identity hash to its replacement, which registering the identity finds from then on."""

    dependencies = [("iki_entities", "0028_record_exports")]

    operations = [
        migrations.RemoveConstraint(model_name="entity", name="iki_entity_entry_or_record"),
        migrations.AddConstraint(
            model_name="entity",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("entry__isnull", True),
                        ("owner_project__isnull", True),
                        ("registry__isnull", False),
                        models.Q(
                            ("identity_hash__isnull", False),
                            ("purged_at__isnull", False),
                            ("superseded_by__isnull", False),
                            _connector="OR",
                        ),
                    ),
                    models.Q(
                        ("identity_hash__isnull", True),
                        ("owner_project__isnull", False),
                        ("registry__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="iki_entity_entry_or_record",
            ),
        ),
    ]
