from django.db import migrations

# A reference whose foreign key is to iki_entity (to an abstract type, or to several types,
# ADR 0136) names an entity of one of its types: ``sync_plugins`` puts this on every table
# holding one, after each insert and update, with pairs of arguments: a column, and the
# types it may name (its types and their subtypes, as a text[] literal). A foreign key to a
# type's own table holds the rest to their type already.
FUNCTION = r"""
CREATE OR REPLACE FUNCTION iki_reference_types() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    i integer := 0;
    named uuid;
    wrong text;
BEGIN
    WHILE i < TG_NARGS LOOP
        EXECUTE format(
            'SELECT n.%2$I, e.type FROM added n JOIN %1$I.iki_entity e ON e.id = n.%2$I '
            'WHERE NOT (e.type = ANY ($1::text[])) LIMIT 1',
            TG_TABLE_SCHEMA, TG_ARGV[i])
            INTO named, wrong USING TG_ARGV[i + 1];
        IF wrong IS NOT NULL THEN
            RAISE EXCEPTION 'iki: %.% can''t refer to % (a %): it refers to one of %',
                TG_TABLE_NAME, TG_ARGV[i], named, wrong, TG_ARGV[i + 1]
                USING ERRCODE = 'check_violation';
        END IF;
        i := i + 2;
    END LOOP;
    RETURN NULL;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [("iki_entities", "0015_requests")]

    operations = [
        migrations.RunSQL(FUNCTION, "DROP FUNCTION IF EXISTS iki_reference_types() CASCADE"),
    ]
