"""Reserve the record guards' migration mode for the table owner (ADRs 0025 and 0120)."""

from importlib import import_module

from django.db import migrations

LOCK_GUARD = import_module("iki_server.entities.migrations.0012_states").GUARD
REQUEST_GUARD = import_module("iki_server.entities.migrations.0015_requests").GUARD

# Both invoker-security triggers receive TG_RELID from PostgreSQL. The migrator owns
# the guarded table, regardless of the role name the installation chose.
UNLOCKED = "current_setting('iki.unlocked', true) = 'on'"
OWNER = "(SELECT pg_get_userbyid(relowner) FROM pg_catalog.pg_class WHERE oid = TG_RELID)"
MIGRATING = f"({UNLOCKED} AND current_user = {OWNER})"


class Migration(migrations.Migration):
    dependencies = [("iki_entities", "0024_remember_record_lock")]
    operations = [
        migrations.RunSQL(guard.replace(UNLOCKED, MIGRATING), guard)
        for guard in (LOCK_GUARD, REQUEST_GUARD)
    ]
