from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_entities", "0025_migrator_record_guards"),
        ("iki_plugins", "0006_history_changes"),
    ]
    operations = [
        migrations.RunSQL(
            "CREATE FUNCTION iki_history_seen() RETURNS bigint LANGUAGE sql STABLE "
            "AS $$ SELECT COALESCE(MAX(id), 0) FROM iki_history_change $$",
            "DROP FUNCTION iki_history_seen()",
        ),
        migrations.AddField(
            model_name="revision",
            name="history_seen",
            field=models.PositiveBigIntegerField(
                db_default=models.Func(function="iki_history_seen")
            ),
        ),
    ]
