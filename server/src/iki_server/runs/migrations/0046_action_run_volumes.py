"""Pin each run's managed reference data before its results can be reused."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_runs", "0045_search_everything")]

    operations = [
        migrations.AddField(
            model_name="actionrun",
            name="volumes",
            field=models.JSONField(blank=True, default=dict, null=True),
        ),
    ]
