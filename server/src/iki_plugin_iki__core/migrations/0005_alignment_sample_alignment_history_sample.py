"""A physical sample on any alignment, including each saved revision."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki__core", "0004_alignment_verdict_alignment_history_verdict"),
        ("iki_entities", "0023_ruleholdoccurrence"),
    ]

    operations = [
        migrations.RunSQL(
            """
                ALTER TABLE iki__core_alignment ADD COLUMN sample_id uuid
                    REFERENCES iki_entity(id) DEFERRABLE INITIALLY DEFERRED;
                CREATE INDEX iki_core_alignment_sample ON iki__core_alignment(sample_id);
                ALTER TABLE iki__core_alignment__history ADD COLUMN sample_id uuid;
            """,
            # Dependent plugins can restore samples to their old storage first. Check
            # after their reverse migrations, in the same transaction, for any remaining
            # attribution that the older deployment cannot represent.
            reverse_sql="""
                DO $$ BEGIN
                    IF EXISTS (SELECT FROM iki__core_alignment WHERE sample_id IS NOT NULL)
                    OR EXISTS (SELECT FROM iki__core_alignment__history WHERE sample_id IS NOT NULL)
                    THEN RAISE EXCEPTION 'Downgrading would lose recorded alignment samples.';
                    END IF;
                END $$;
                ALTER TABLE iki__core_alignment__history DROP COLUMN sample_id;
                ALTER TABLE iki__core_alignment DROP COLUMN sample_id;
            """,
            state_operations=[
                migrations.AddField(
                    model_name="alignment", name="sample",
                    field=models.ForeignKey(null=True, on_delete=models.PROTECT,
                                            related_name="+", to="iki_entities.entity"),
                ),
                migrations.AddField(
                    model_name="alignment_history", name="sample",
                    field=models.UUIDField(db_column="sample_id", null=True),
                ),
            ],
        ),
    ]
