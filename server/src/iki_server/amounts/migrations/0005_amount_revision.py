from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("iki_amounts", "0004_low_stock_alert"),
    ]

    operations = [
        migrations.AddField(
            model_name="amount",
            name="revision",
            field=models.PositiveIntegerField(
                db_comment="One more with each movement: its ETag, which a recount names (ADR 0289).",
                db_default=1,
                default=1,
            ),
        ),
    ]
