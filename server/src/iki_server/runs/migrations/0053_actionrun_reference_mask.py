from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_runs", "0052_recalculation")]

    operations = [
        migrations.AddField(
            model_name="actionrun",
            name="reference_mask",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
