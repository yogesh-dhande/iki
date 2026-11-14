import django.db.models.deletion
from django.db import migrations, models

# Unique fields' tables call this from their triggers, made by `sync_plugins`
# (iki_server.plugins.sync), with the column and its key. It keeps
# iki_unique_value in step with the column, so the database refuses a second
# entity in a project with the same value, whatever writes it.
KEEP = r"""
CREATE FUNCTION iki_unique_value_keep() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    new_value text;
    old_value text;
BEGIN
    EXECUTE format('SELECT ($1).%I::text', TG_ARGV[0]) INTO new_value USING NEW;
    IF TG_OP = 'UPDATE' THEN
        EXECUTE format('SELECT ($1).%I::text', TG_ARGV[0]) INTO old_value USING OLD;
        IF new_value IS NOT DISTINCT FROM old_value THEN
            RETURN NULL;
        END IF;
    END IF;
    EXECUTE format('DELETE FROM %I.iki_unique_value WHERE entity_id = $1 AND key = $2',
                   TG_TABLE_SCHEMA)
        USING NEW.entity_id, TG_ARGV[1];
    IF new_value IS NOT NULL THEN
        EXECUTE format(
            'INSERT INTO %1$I.iki_unique_value (project_id, key, value, entity_id) '
            'SELECT owner_project_id, $2, $3, id FROM %1$I.iki_entity WHERE id = $1',
            TG_TABLE_SCHEMA)
            USING NEW.entity_id, TG_ARGV[1], new_value;
    END IF;
    RETURN NULL;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("iki_entities", "0006_revision_agents"),
        ("iki_org", "0002_project_allow_sharing"),
    ]

    operations = [
        migrations.CreateModel(
            name="UniqueValue",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("key", models.CharField(max_length=127)),
                ("value", models.CharField(max_length=600)),
                (
                    "entity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="iki_entities.entity",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="iki_org.project",
                    ),
                ),
            ],
            options={
                "db_table": "iki_unique_value",
                "db_table_comment": "Each unique field's values, once per project. Kept by triggers on the plugin tables.",
                "indexes": [models.Index(fields=["key", "value"], name="iki_unique_value_lookup")],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("project", "key", "value"), name="iki_unique_value_once"
                    ),
                    models.UniqueConstraint(
                        fields=("entity", "key"), name="iki_unique_value_entity"
                    ),
                ],
            },
        ),
        migrations.RunSQL(KEEP, "DROP FUNCTION iki_unique_value_keep()"),
    ]
