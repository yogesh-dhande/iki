from django.db import migrations

# An import keeps the HRIDs entities and runs had where they were made, when nothing here
# has them. Their sequences must then never hand those numbers out. The app's role has only
# USAGE on sequences (ADR 0025), which can't setval, so this function, owned by the
# migrator, moves an HRID sequence forward for it, and does nothing else: it refuses any
# other sequence and never moves one back. Drawing first makes the step atomic with
# everything else drawing from it, so the sequence can't go back past numbers handed out
# meanwhile, short of that many in the same instant; HRIDs are unique, so even then a
# clash fails a create rather than reusing one.
ADVANCE = """
CREATE FUNCTION iki_hrid_advance(sequence text, number bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    drawn bigint;
BEGIN
    IF sequence !~ '^iki_hrid_[a-z0-9_]+$' THEN
        RAISE EXCEPTION '% is not an HRID sequence', sequence
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    drawn := nextval(format('public.%I', sequence)::regclass);
    IF drawn < number THEN
        PERFORM setval(format('public.%I', sequence)::regclass, number);
    END IF;
END
$$;
REVOKE ALL ON FUNCTION iki_hrid_advance(text, bigint) FROM PUBLIC;
"""

REMOVE = "DROP FUNCTION iki_hrid_advance(text, bigint);"


class Migration(migrations.Migration):
    dependencies = [("iki_bundles", "0003_imports_applied")]

    operations = [migrations.RunSQL(ADVANCE, REMOVE)]
