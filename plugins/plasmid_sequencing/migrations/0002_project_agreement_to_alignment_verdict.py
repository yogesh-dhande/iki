"""Expose the existing immutable agreement on the common alignment type."""

from django.db import migrations

from iki_server.plugins.operations import AppendOnly


class Migration(migrations.Migration):
    dependencies = [
        ("iki__core", "0004_alignment_verdict_alignment_history_verdict"),
        ("iki__plasmid_sequencing", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=f"""
                UPDATE iki__core_alignment AS alignment
                   SET verdict = result.agreement
                  FROM iki__plasmid_sequencing_wholeplasmidalignment AS result
                 WHERE alignment.entity_id = result.entity_id AND alignment.verdict IS NULL;

                ALTER TABLE iki__core_alignment__history
                    DISABLE TRIGGER {AppendOnly.ROW_TRIGGER};
                UPDATE iki__core_alignment__history AS alignment
                   SET verdict = result.agreement
                  FROM iki__plasmid_sequencing_wholeplasmidalignment AS result
                 WHERE alignment.entity_id = result.entity_id AND alignment.verdict IS NULL;
                ALTER TABLE iki__core_alignment__history
                    ENABLE TRIGGER {AppendOnly.ROW_TRIGGER};
            """,
            reverse_sql=f"""
                UPDATE iki__core_alignment AS alignment
                   SET verdict = NULL
                  FROM iki__plasmid_sequencing_wholeplasmidalignment AS result
                 WHERE alignment.entity_id = result.entity_id AND alignment.verdict = result.agreement;

                ALTER TABLE iki__core_alignment__history
                    DISABLE TRIGGER {AppendOnly.ROW_TRIGGER};
                UPDATE iki__core_alignment__history AS alignment
                   SET verdict = NULL
                  FROM iki__plasmid_sequencing_wholeplasmidalignment AS result
                 WHERE alignment.entity_id = result.entity_id AND alignment.verdict = result.agreement;
                ALTER TABLE iki__core_alignment__history
                    ENABLE TRIGGER {AppendOnly.ROW_TRIGGER};
            """,
        ),
    ]
