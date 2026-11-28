from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("iki_runs", "0055_job_scan_wait")]

    operations = [
        migrations.AddField(
            model_name="triggerfiring",
            name="failures",
            field=models.PositiveSmallIntegerField(default=0),
        ),
    ]
