"""One uniqueness mechanism (ADR 0082): a unique field is a one-field ``unique`` rule, so
every row of ``iki_unique_value`` is a rule's, keyed ``table:rule``, its values a JSON
array whose SHA-256 is what's unique, in a project or (``within: registry``) a registry.

One trigger function keeps them, :sql:`iki_unique_keep`, in place of a unique field's and
a rule's. A vendor barcode's reserved prefix, which the unique field's trigger checked,
has its own, :sql:`iki_vendor_barcode_check`. The old functions go with their triggers,
and the rows they kept: ``sync_plugins`` installs the new triggers and fills the rows in
again, as for a rule just added.
"""

from importlib import import_module

from django.db import migrations, models

# TG_ARGV: the rule's key (`table:rule`); its columns and what counts (`while`), as
# iki_rule_values takes them; and where its values are unique, `project` or `registry`.
# A record's row is in its owner project; an entry's in its registry. Datetimes compare
# as UTC, whatever the writer's session says.
KEEP = r"""
DELETE FROM iki_unique_value;
DROP FUNCTION IF EXISTS iki_unique_value_keep() CASCADE;
DROP FUNCTION IF EXISTS iki_rule_unique_keep() CASCADE;
ALTER FUNCTION iki_rule_range_keep() SET TimeZone = 'UTC';

CREATE FUNCTION iki_unique_keep() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog SET TimeZone = 'UTC' AS $$
DECLARE
    new_value text;
    old_value text;
    scope text := CASE TG_ARGV[3] WHEN 'registry' THEN 'registry_id' ELSE 'owner_project_id' END;
    target text := CASE TG_ARGV[3] WHEN 'registry' THEN 'registry_id' ELSE 'project_id' END;
BEGIN
    EXECUTE format('SELECT %I.iki_rule_values($1, $2, $3)', TG_TABLE_SCHEMA)
        INTO new_value USING to_jsonb(NEW), TG_ARGV[1], TG_ARGV[2];
    IF TG_OP = 'UPDATE' THEN
        EXECUTE format('SELECT %I.iki_rule_values($1, $2, $3)', TG_TABLE_SCHEMA)
            INTO old_value USING to_jsonb(OLD), TG_ARGV[1], TG_ARGV[2];
        IF new_value IS NOT DISTINCT FROM old_value THEN
            RETURN NULL;
        END IF;
    END IF;
    EXECUTE format('DELETE FROM %I.iki_unique_value WHERE entity_id = $1 AND key = $2',
                   TG_TABLE_SCHEMA)
        USING NEW.entity_id, TG_ARGV[0];
    IF new_value IS NOT NULL THEN
        EXECUTE format(
            'INSERT INTO %1$I.iki_unique_value (%2$I, key, value, digest, entity_id) '
            'SELECT %3$I, $2, $3, sha256(convert_to($3, ''UTF8'')), id FROM %1$I.iki_entity '
            'WHERE id = $1 AND %3$I IS NOT NULL',
            TG_TABLE_SCHEMA, target, scope)
            USING NEW.entity_id, TG_ARGV[0], new_value;
    END IF;
    RETURN NULL;
END
$$;

-- TG_ARGV: the vendor barcode's column, the concrete types that read it as theirs, and
-- the reserved prefix's pattern (codes.has_iki_prefix under any database locale).
CREATE FUNCTION iki_vendor_barcode_check() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    new_value text;
    barcode boolean;
BEGIN
    EXECUTE format('SELECT ($1).%I::text', TG_ARGV[0]) INTO new_value USING NEW;
    IF new_value COLLATE "C" ~ TG_ARGV[2] THEN
        EXECUTE format('SELECT type = ANY($2::text[]) FROM %I.iki_entity WHERE id = $1',
                       TG_TABLE_SCHEMA)
            INTO barcode USING NEW.entity_id, TG_ARGV[1];
        IF barcode THEN
            RAISE EXCEPTION 'Vendor barcode % uses the reserved IKI: prefix.', TG_ARGV[0]
                USING ERRCODE = 'check_violation', CONSTRAINT = 'iki_vendor_barcode_prefix';
        END IF;
    END IF;
    RETURN NULL;
END
$$;
"""

PREVIOUS = "\n".join(
    [
        "DELETE FROM iki_unique_value;",
        "DROP FUNCTION IF EXISTS iki_vendor_barcode_check() CASCADE;",
        "DROP FUNCTION IF EXISTS iki_unique_keep() CASCADE;",
        "ALTER FUNCTION iki_rule_range_keep() RESET TimeZone;",
        import_module("iki_server.entities.migrations.0022_reserved_vendor_barcode").KEEP,
        import_module("iki_server.entities.migrations.0014_rules").VALUES,
    ]
)


class Migration(migrations.Migration):
    dependencies = [("iki_entities", "0030_record_export_limits")]

    operations = [
        migrations.RunSQL(KEEP, PREVIOUS),
        migrations.RemoveConstraint(model_name="uniquevalue", name="iki_unique_value_once"),
        migrations.RemoveConstraint(
            model_name="uniquevalue", name="iki_unique_value_once_in_registry"
        ),
        migrations.RemoveIndex(model_name="uniquevalue", name="iki_unique_value_lookup"),
        migrations.AlterField(model_name="uniquevalue", name="value", field=models.TextField()),
        migrations.AddField(
            model_name="uniquevalue",
            name="digest",
            field=models.BinaryField(default=b"", max_length=32),
            preserve_default=False,
        ),
        migrations.AddConstraint(
            model_name="uniquevalue",
            constraint=models.UniqueConstraint(
                condition=models.Q(("project__isnull", False)),
                fields=("project", "key", "digest"),
                name="iki_unique_value_once",
            ),
        ),
        migrations.AddConstraint(
            model_name="uniquevalue",
            constraint=models.UniqueConstraint(
                condition=models.Q(("registry__isnull", False)),
                fields=("registry", "key", "digest"),
                name="iki_unique_value_once_in_registry",
            ),
        ),
        migrations.AddIndex(
            model_name="uniquevalue",
            index=models.Index(fields=["key", "digest"], name="iki_unique_value_lookup"),
        ),
        migrations.AlterModelTableComment(
            name="uniquevalue",
            table_comment="Each record's values under each unique rule, once per project or "
            "registry. Kept by triggers on the plugin tables.",
        ),
    ]
