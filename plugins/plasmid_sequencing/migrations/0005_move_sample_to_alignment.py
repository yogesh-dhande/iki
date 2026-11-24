"""Move sample attribution, including the revision when a previously empty sample was set."""

from django.db import migrations

from iki_server.plugins.operations import AppendOnly

PARENT = "iki__core_alignment"
CHILD = "iki__plasmid_sequencing_wholeplasmidalignment"


def copy_sample(source, destination):
    source_history = source + "__history"
    history = destination + "__history"
    # Each table has a snapshot only at revisions where one of its fields changed.
    # At a sample-only revision, carry the destination's preceding snapshot forward.
    return f"""
        UPDATE {destination} dst SET sample_id = src.sample_id FROM {source} src
        WHERE dst.entity_id = src.entity_id AND dst.sample_id IS DISTINCT FROM src.sample_id;

        ALTER TABLE {history} DISABLE TRIGGER {AppendOnly.ROW_TRIGGER};
        INSERT INTO {history}
        SELECT (jsonb_populate_record(NULL::{history}, to_jsonb(prior) ||
                    jsonb_build_object('revision', src.revision, 'sample_id', src.sample_id))).*
          FROM (SELECT entity_id, revision, sample_id,
                       lag(sample_id) OVER (PARTITION BY entity_id ORDER BY revision) AS previous
                  FROM {source_history}) src
          JOIN LATERAL (SELECT * FROM {history} dst
                         WHERE dst.entity_id = src.entity_id AND dst.revision <= src.revision
                         ORDER BY dst.revision DESC LIMIT 1) prior ON true
         WHERE src.sample_id IS DISTINCT FROM src.previous
        ON CONFLICT (entity_id, revision) DO NOTHING;

        WITH attribution AS (
            SELECT dst.entity_id, dst.revision, src.sample_id FROM {history} dst
              JOIN LATERAL (SELECT sample_id FROM {source_history}
                             WHERE entity_id = dst.entity_id AND revision <= dst.revision
                             ORDER BY revision DESC LIMIT 1) src ON true
        )
        UPDATE {history} dst SET sample_id = src.sample_id FROM attribution src
         WHERE dst.entity_id = src.entity_id AND dst.revision = src.revision
           AND dst.sample_id IS DISTINCT FROM src.sample_id;
        ALTER TABLE {history} ENABLE TRIGGER {AppendOnly.ROW_TRIGGER};
    """


class Migration(migrations.Migration):
    dependencies = [
        ("iki__core", "0005_alignment_sample_alignment_history_sample"),
        ("iki__plasmid_sequencing", "0004_alter_wholeplasmidalignment_agreement"),
    ]
    operations = [
        migrations.RunSQL(
            copy_sample(CHILD, PARENT),
            reverse_sql=copy_sample(PARENT, CHILD) + f"""
                UPDATE {PARENT} p SET sample_id = NULL FROM {CHILD} c
                 WHERE p.entity_id = c.entity_id AND p.sample_id IS NOT NULL;
                ALTER TABLE {PARENT}__history DISABLE TRIGGER {AppendOnly.ROW_TRIGGER};
                UPDATE {PARENT}__history p SET sample_id = NULL FROM {CHILD} c
                 WHERE p.entity_id = c.entity_id AND p.sample_id IS NOT NULL;
                ALTER TABLE {PARENT}__history ENABLE TRIGGER {AppendOnly.ROW_TRIGGER};
            """,
        ),
        migrations.RemoveField("wholeplasmidalignment", "legacy_sample"),
        migrations.RemoveField("wholeplasmidalignment_history", "legacy_sample"),
    ]
