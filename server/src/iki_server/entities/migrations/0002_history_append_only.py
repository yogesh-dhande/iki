from django.db import migrations

# Plugins' history tables call this from their triggers (see
# iki_server.plugins.operations.AppendOnly), so no role, the tables' owner
# included, can change or delete history. Only a migration can lift that, and
# migrations are reviewed and recorded.
REFUSE = r"""
CREATE FUNCTION iki_refuse_history_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'history is append-only: % on % is not allowed', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [("iki_entities", "0001_initial")]

    operations = [
        migrations.RunSQL(REFUSE, "DROP FUNCTION iki_refuse_history_change();"),
    ]
