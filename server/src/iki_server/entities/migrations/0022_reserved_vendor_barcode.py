"""Reject reserved machine identities in a concrete type's declared vendor barcode field."""

from importlib import import_module

from django.db import migrations

PREVIOUS = import_module("iki_server.entities.migrations.0011_registries").KEEP

KEEP = r"""
CREATE OR REPLACE FUNCTION iki_unique_value_keep() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    new_value text;
    old_value text;
    barcode boolean;
BEGIN
    EXECUTE format('SELECT ($1).%I::text', TG_ARGV[0]) INTO new_value USING NEW;
    -- sync_plugins supplies concrete type names and the reserved prefix pattern only
    -- for columns used as vendor barcodes. Existing two-argument triggers still work.
    IF TG_NARGS = 4 AND new_value COLLATE "C" ~ TG_ARGV[3] THEN
        EXECUTE format('SELECT type = ANY($2::text[]) FROM %I.iki_entity WHERE id = $1',
                       TG_TABLE_SCHEMA)
            INTO barcode USING NEW.entity_id, TG_ARGV[2];
        IF barcode THEN
            RAISE EXCEPTION 'Vendor barcode % uses the reserved IKI: prefix.', TG_ARGV[1]
                USING ERRCODE = 'check_violation', CONSTRAINT = 'iki_vendor_barcode_prefix';
        END IF;
    END IF;
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
            'INSERT INTO %1$I.iki_unique_value (project_id, registry_id, key, value, entity_id) '
            'SELECT owner_project_id, registry_id, $2, $3, id FROM %1$I.iki_entity WHERE id = $1',
            TG_TABLE_SCHEMA)
            USING NEW.entity_id, TG_ARGV[1], new_value;
    END IF;
    RETURN NULL;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [("iki_entities", "0021_released_review_revision")]
    operations = [migrations.RunSQL(KEEP, PREVIOUS)]
